# SPDX-FileCopyrightText: 2024-2026 Toyoaki Nishida
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# This file is part of FUJIN-P.
# Copyright (C) 2024-2026 Toyoaki Nishida
#
# FUJIN-P is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# FUJIN-P is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with FUJIN-P.  If not, see <https://www.gnu.org/licenses/>.
#
# Source: https://github.com/nishida-toyoaki/fujin-p

"""
official_data_archive routes.py
公式データ集 — 公式テーブルの登録簿管理と読み取り専用ビューア

設計方針:
  - 登録簿テーブル official_data_archive_tables（<owner>$default）に、
    公開対象のSQLテーブル名とその所在DB（default / fujinp / public）を登録する。
  - データへのアクセスは「登録簿に載っているテーブル」のみ許可
    （テーブル名の正規表現検査ではなく、DB照合による許可リスト方式）。
  - 削除は登録簿からの除外のみ。SQLテーブル自体は絶対に DROP しない。
  - 閲覧: users.category が regular / admin
  - 管理（追加・削除）: admin、または まいぐるグループ「公式データ集_管理者」
"""
import os
import re
import math
import datetime
import logging

from pytz import timezone
from flask import render_template, request, jsonify, session
import mysql.connector

from auth import redirect_to_dashboard
from config import Config
from db import DatabaseConfig
from decorators import login_required
from ..user_groups.utils import user_is_in_group

from . import official_data_archive_bp

JST = timezone('Asia/Tokyo')

# ────────────────────────────────────────────
# 定数
# ────────────────────────────────────────────

# 管理グループの命名規約：
#   まいぐる上で「公式データ集_」で始まる名前のグループが管理グループになる。
#   例：公式データ集_財務、公式データ集_教務 …（小刻みに定義できる）
#   各アイテムには登録時に管理グループを1つ紐づけ、その所属者が管理できる。
GROUP_PREFIX = '公式データ集_'

# 全体管理者グループ（admin カテゴリと同等：全アイテムを管理できる）
GROUP_GLOBAL = '公式データ集_管理者'

# 閲覧を許可する users.category
VIEW_CATEGORIES = ('admin', 'regular')

# アイテムの所在DBの選択肢。キーを登録簿の database_name に保存する。
# 接続情報そのものは db.py に集約されている（実値をここに書かない）。
DB_CHOICES = {
    'default': DatabaseConfig.default,
    'fujinp':  DatabaseConfig.fujinp,
    'public':  DatabaseConfig.public,
}

# テーブル名として許可する文字（追加時の検査。アクセス時は登録簿照合のみ）
# 日本語名・中点（・）を許可し、SQL組み立て上危険な文字だけを禁止する
# （空白・バッククォート・引用符・% ・; ・\ ・. ・/）。長さはMySQL上限の64文字。
TABLE_NAME_RE = re.compile(r'^[^\s`\'\"%;\\./]{1,64}$')

PAGE_LIMIT_MAX = 5000


def get_jst_now():
    """現在の日時をJSTで取得（naive datetime）。INSERT/UPDATEに使う。"""
    return datetime.datetime.now(JST).replace(tzinfo=None)


# ────────────────────────────────────────────
# 権限ヘルパー
# ────────────────────────────────────────────

def _get_user_category(user_id):
    """users.category を返す。"""
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT category FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()
        return row['category'] if row else None
    except Exception as e:
        logging.error("official_data_archive _get_user_category error: %s", e)
        return None
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


def can_view(user_id):
    """閲覧権限：regular 以上。"""
    return _get_user_category(user_id) in VIEW_CATEGORIES


# 構成員の判定はまいぐるの公開APIに任せる。台帳のルールから作られたグループ
# （総務課など）は user_group_memberships に行を持たないため、このテーブルを
# 直接引くと構成員が0人になる。取り込みは初回の呼び出し時に行う
# （起動時の読み込み順に左右されないようにするため）。
_UG_UTILS = None


def _ug(name):
    """まいぐるの utils から関数を取り出す。無ければ None（呼び出し元が従来処理に落ちる）"""
    global _UG_UTILS
    if _UG_UTILS is None:
        try:
            from fujinp.user_groups import utils as _u
        except Exception:
            _u = False
        _UG_UTILS = _u
    return getattr(_UG_UTILS, name, None) if _UG_UTILS else None


def _user_effective_group_ids(user_id):
    """
    ユーザの有効な所属グループID集合。
    まいぐるの get_user_group_ids()（直接メンバー ∪ 台帳のルール由来 − 除外）を使い、
    取得できない場合のみ有効期間つき所属を直接照合する。
    """
    if not user_id:
        return set()
    _fn = _ug('get_user_group_ids')
    if _fn is not None:
        try:
            return set(_fn(user_id))
        except Exception as e:
            logging.error("official_data_archive user_groups.get_user_group_ids: %s", e)
    try:
        now = get_jst_now()
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute("""
            SELECT group_id FROM user_group_memberships
            WHERE user_id = %s
              AND (valid_from  IS NULL OR valid_from  <= %s)
              AND (valid_until IS NULL OR valid_until >= %s)
        """, (user_id, now, now))
        return {r[0] for r in cursor.fetchall()}
    except Exception as e:
        logging.error("official_data_archive _user_effective_group_ids: %s", e)
        return set()
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


def _prefixed_manager_groups():
    """
    名前が GROUP_PREFIX で始まる まいぐるグループ一覧 [{id, name}] を返す。
    これが「管理グループ」の全体集合（全体管理者グループも含む）。
    """
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        # アンダースコアはLIKEのワイルドカードなのでエスケープして前方一致
        pattern = GROUP_PREFIX.replace('\\', '\\\\').replace('_', '\\_') + '%'
        cursor.execute("""
            SELECT id, name FROM user_groups
            WHERE name LIKE %s
            ORDER BY name
        """, (pattern,))
        return cursor.fetchall()
    except Exception as e:
        logging.error("official_data_archive _prefixed_manager_groups: %s", e)
        return []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


def is_global_manager(user_id):
    """全体管理者：admin カテゴリ、または GROUP_GLOBAL の所属者。"""
    if _get_user_category(user_id) == 'admin':
        return True
    try:
        return user_is_in_group(user_id, GROUP_GLOBAL)
    except Exception as e:
        logging.warning("official_data_archive is_global_manager: %s", e)
        return False


def user_manage_groups(user_id):
    """
    ユーザが担当できる管理グループ [{id, name}] を返す。
    全体管理者は全管理グループ、そうでなければ所属する管理グループのみ。
    """
    groups = _prefixed_manager_groups()
    if is_global_manager(user_id):
        return groups
    eff = _user_effective_group_ids(user_id)
    return [g for g in groups if g['id'] in eff]


def can_add(user_id):
    """追加権限：全体管理者、またはいずれかの管理グループの所属者。"""
    return is_global_manager(user_id) or bool(user_manage_groups(user_id))


# ────────────────────────────────────────────
# 登録簿ヘルパー
# ────────────────────────────────────────────

def _registry_lookup(table_name, database_name):
    """
    登録簿に (database_name, table_name) のアイテムがあるか照合する。
    あれば登録行（dict）、なければ None。
    データアクセス可否は必ずこの照合で判定する（許可リスト方式）。
    """
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT id, table_name, database_name, display_name, note
            FROM official_data_archive_tables
            WHERE table_name = %s AND database_name = %s
            LIMIT 1
        """, (table_name, database_name))
        return cursor.fetchone()
    except Exception as e:
        logging.error("official_data_archive _registry_lookup error: %s", e)
        return None
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


def _table_exists(table_name, database_name):
    """
    指定DBに実テーブルが存在するか information_schema で確認する。
    追加（登録）時の検査に使う。
    """
    try:
        conn = mysql.connector.connect(**DB_CHOICES[database_name]())
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema = DATABASE() AND table_name = %s
        """, (table_name,))
        return cursor.fetchone()[0] > 0
    except Exception as e:
        logging.error("official_data_archive _table_exists error: %s", e)
        return False
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


def safe_val(v):
    """JSONで返せる値に変換する（日時3層ルール：バックエンドで文字列化）。"""
    if v is None:
        return None
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    if isinstance(v, datetime.timedelta):
        s = int(v.total_seconds())
        return f"{s//3600:02d}:{(s%3600)//60:02d}"
    if isinstance(v, bytes):
        return v.decode('utf-8', errors='replace')
    return v


# ────────────────────────────────────────────
# 画面
# ────────────────────────────────────────────

@official_data_archive_bp.route('/')
@login_required
def index():
    """公式データ集メイン画面（2ペインビューア）。"""
    user_id = session.get('user_id')
    if not can_view(user_id):
        return redirect_to_dashboard()
    return render_template('official_data_archive/index.html')


# ────────────────────────────────────────────
# API: 公式テーブル一覧
# ────────────────────────────────────────────

@official_data_archive_bp.route('/api/tables', methods=['GET'])
@login_required
def api_tables():
    """
    登録簿の一覧を返す。各アイテムに行数（row_count）と
    呼び出しユーザの管理可否（can_manage：全体管理者、または
    そのアイテムの管理グループ所属者）を付ける。
    あわせて追加可否（can_add）と、ユーザが指定できる管理グループ
    一覧（manage_groups）も返し、フロントは追加・削除UIの表示判定に使う。
    """
    user_id = session.get('user_id')
    if not can_view(user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT t.id, t.table_name, t.database_name,
                   t.display_name, t.note, t.manager_group_id,
                   g.name AS manager_group_name
            FROM official_data_archive_tables t
            LEFT JOIN user_groups g ON t.manager_group_id = g.id
            ORDER BY COALESCE(t.display_name, t.table_name)
        """)
        tables = cursor.fetchall()
        cursor.close(); conn.close()

        # ユーザの管理範囲（アイテムごとの can_manage 判定用）
        is_global = is_global_manager(user_id)
        my_groups = user_manage_groups(user_id)
        my_group_ids = {g['id'] for g in my_groups}
        for t in tables:
            t['can_manage'] = bool(
                is_global or (t['manager_group_id'] in my_group_ids
                              if t['manager_group_id'] else False))

        # 行数はDBごとにまとめて取得する（接続を最小限に）
        by_db = {}
        for t in tables:
            by_db.setdefault(t['database_name'], []).append(t)
        for db_key, items in by_db.items():
            if db_key not in DB_CHOICES:
                for t in items:
                    t['row_count'] = None
                continue
            try:
                db_conn = mysql.connector.connect(**DB_CHOICES[db_key]())
                db_cur = db_conn.cursor()
                for t in items:
                    try:
                        db_cur.execute(
                            "SELECT COUNT(*) FROM `%s`" % t['table_name'])
                        t['row_count'] = db_cur.fetchone()[0]
                    except Exception as e:
                        logging.warning(
                            "official_data_archive row_count error %s.%s: %s",
                            db_key, t['table_name'], e)
                        t['row_count'] = None
                db_cur.close(); db_conn.close()
            except Exception as e:
                logging.error("official_data_archive db connect (%s): %s",
                              db_key, e)
                for t in items:
                    t['row_count'] = None

        return jsonify({
            'success': True,
            'tables': tables,
            'can_add': can_add(user_id),
            'is_global': is_global,
            'manage_groups': my_groups,
            'db_choices': list(DB_CHOICES.keys()),
        })
    except Exception as e:
        logging.error("api_tables error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


# ────────────────────────────────────────────
# API: アイテムの追加・削除（管理者）
# ────────────────────────────────────────────

@official_data_archive_bp.route('/api/tables/add', methods=['POST'])
@login_required
def api_table_add():
    """
    公式テーブルへアイテム（SQLテーブル）を追加する。
    リクエスト: {
        "table_name":       "T_10_01",    # 必須（英数字とアンダースコア）
        "database_name":    "fujinp",     # 必須（default / fujinp / public）
        "manager_group_id": 12,           # 管理グループ（まいぐる user_groups.id）
                                          #   全体管理者のみ null（＝全体管理者専管）可
        "display_name":     "（任意）表示名",
        "note":             "（任意）説明"
    }
    管理グループは「公式データ集_」で始まる まいぐるグループから選ぶ。
    全体管理者以外は、自分が所属する管理グループしか指定できない。
    実テーブルの存在を確認してから登録する。
    """
    user_id = session.get('user_id')
    if not can_add(user_id):
        return jsonify({'success': False, 'error': '管理権限がありません'}), 403
    try:
        data          = request.json or {}
        table_name    = (data.get('table_name') or '').strip()
        database_name = (data.get('database_name') or '').strip()
        display_name  = (data.get('display_name') or '').strip() or None
        note          = (data.get('note') or '').strip() or None
        manager_group_id = data.get('manager_group_id') or None

        if not TABLE_NAME_RE.match(table_name):
            return jsonify({'success': False,
                            'error': 'テーブル名は英数字とアンダースコア'
                                     '（64文字以内）で指定してください'}), 400
        if database_name not in DB_CHOICES:
            return jsonify({'success': False,
                            'error': 'データベースの指定が不正です'}), 400

        # ── 管理グループの検証 ──
        is_global = is_global_manager(user_id)
        allowed_ids = {g['id'] for g in user_manage_groups(user_id)}
        if manager_group_id is None:
            # 管理グループなし＝全体管理者専管。全体管理者のみ許可。
            if not is_global:
                return jsonify({'success': False,
                                'error': '管理グループを指定してください'}), 400
        else:
            try:
                manager_group_id = int(manager_group_id)
            except (TypeError, ValueError):
                return jsonify({'success': False,
                                'error': '管理グループの指定が不正です'}), 400
            if manager_group_id not in allowed_ids:
                return jsonify({'success': False,
                                'error': '指定できない管理グループです'
                                         '（所属していないか、'
                                         f'「{GROUP_PREFIX}」で始まる'
                                         'グループではありません）'}), 403

        if _registry_lookup(table_name, database_name):
            return jsonify({'success': False,
                            'error': f'{database_name} の {table_name} は'
                                     'すでに登録されています'}), 400
        if not _table_exists(table_name, database_name):
            return jsonify({'success': False,
                            'error': f'{database_name} に {table_name} という'
                                     'テーブルが見つかりません'}), 404

        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO official_data_archive_tables
                (table_name, database_name, display_name, note,
                 manager_group_id, created_by, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (table_name, database_name, display_name, note,
              manager_group_id, user_id, get_jst_now()))
        conn.commit()
        new_id = cursor.lastrowid
        return jsonify({'success': True, 'id': new_id})
    except Exception as e:
        logging.error("api_table_add error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@official_data_archive_bp.route('/api/tables/delete', methods=['POST'])
@login_required
def api_table_delete():
    """
    公式テーブルからアイテムを削除する。
    リクエスト: { "id": 登録簿のid }
    削除できるのは全体管理者、またはそのアイテムの管理グループ所属者。
    ※ 登録簿からの除外のみ。SQLテーブル自体は絶対に DROP しない。
    """
    user_id = session.get('user_id')
    try:
        data    = request.json or {}
        item_id = data.get('id')
        if not item_id:
            return jsonify({'success': False, 'error': 'idが必要です'}), 400

        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT id, manager_group_id FROM official_data_archive_tables
            WHERE id = %s
        """, (item_id,))
        item = cursor.fetchone()
        if not item:
            return jsonify({'success': False,
                            'error': '対象のアイテムが見つかりません'}), 404

        # ── アイテム単位の権限判定 ──
        if not is_global_manager(user_id):
            my_ids = {g['id'] for g in user_manage_groups(user_id)}
            if not (item['manager_group_id']
                    and item['manager_group_id'] in my_ids):
                return jsonify({'success': False,
                                'error': 'このアイテムの管理権限が'
                                         'ありません'}), 403

        cursor.execute(
            "DELETE FROM official_data_archive_tables WHERE id = %s",
            (item_id,))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("api_table_delete error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ────────────────────────────────────────────
# API: テーブルデータ（読み取り専用）
# ────────────────────────────────────────────

@official_data_archive_bp.route('/api/data', methods=['GET'])
@login_required
def api_data():
    """
    登録済みテーブルのデータをページ単位で返す。
    パラメータ: table, db, limit（最大5000）, offset
    アクセス可否は登録簿照合のみで判定する（許可リスト方式）。
    """
    user_id = session.get('user_id')
    if not can_view(user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403

    table_name    = request.args.get('table', '').strip()
    database_name = request.args.get('db', '').strip()
    limit  = min(int(request.args.get('limit', 500)), PAGE_LIMIT_MAX)
    offset = max(int(request.args.get('offset', 0)), 0)

    if not table_name or database_name not in DB_CHOICES:
        return jsonify({'success': False,
                        'error': 'テーブル名とデータベースが必要です'}), 400
    if not _registry_lookup(table_name, database_name):
        return jsonify({'success': False,
                        'error': 'アクセス不可: ' + table_name}), 403

    try:
        conn = mysql.connector.connect(**DB_CHOICES[database_name]())
        cursor = conn.cursor(dictionary=True)

        cursor.execute("SHOW COLUMNS FROM `%s`" % table_name)
        columns = [c['Field'] for c in cursor.fetchall()]

        cursor.execute("SELECT COUNT(*) AS cnt FROM `%s`" % table_name)
        total_count = cursor.fetchone()['cnt']

        cursor.execute(
            "SELECT * FROM `%s` LIMIT %%s OFFSET %%s" % table_name,
            (limit, offset))
        rows = cursor.fetchall()

        serialized = [{k: safe_val(v) for k, v in row.items()}
                      for row in rows]

        return jsonify({
            'success'      : True,
            'table'        : table_name,
            'columns'      : columns,
            'rows'         : serialized,
            'total_count'  : total_count,
            'fetched_count': len(rows),
            'offset'       : offset,
            'limit'        : limit,
        })
    except Exception as e:
        logging.error("api_data error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ────────────────────────────────────────────
# API: XLSXダウンロード
# ────────────────────────────────────────────

@official_data_archive_bp.route('/api/download', methods=['GET'])
@login_required
def api_download():
    """登録済みテーブルを全件XLSXでダウンロードする。"""
    import io
    import openpyxl
    from openpyxl.styles import PatternFill, Font
    from flask import send_file

    user_id = session.get('user_id')
    if not can_view(user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403

    table_name    = request.args.get('table', '').strip()
    database_name = request.args.get('db', '').strip()
    if not table_name or database_name not in DB_CHOICES:
        return jsonify({'success': False,
                        'error': 'テーブル名とデータベースが必要です'}), 400
    if not _registry_lookup(table_name, database_name):
        return jsonify({'success': False, 'error': 'アクセス不可'}), 403

    try:
        conn = mysql.connector.connect(**DB_CHOICES[database_name]())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM `%s`" % table_name)
        rows = cursor.fetchall()
        columns = ([d[0] for d in cursor.description]
                   if cursor.description else [])
        cursor.close(); conn.close()

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = table_name[:31]  # シート名は31文字まで

        ws.append(columns)
        for row in rows:
            ws.append([
                (v.isoformat() if isinstance(v, (datetime.datetime,
                                                 datetime.date))
                 else (f"{int(v.total_seconds())//3600:02d}:"
                       f"{(int(v.total_seconds())%3600)//60:02d}"
                       if isinstance(v, datetime.timedelta)
                       else (v.decode('utf-8', errors='replace')
                             if isinstance(v, bytes) else v)))
                for v in [row[c] for c in columns]
            ])

        header_fill = PatternFill('solid', fgColor='1A3A5C')
        header_font = Font(color='FFFFFF', bold=True)
        for cell in ws[1]:
            cell.fill = header_fill
            cell.font = header_font

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return send_file(
            buf,
            mimetype='application/vnd.openxmlformats-officedocument'
                     '.spreadsheetml.sheet',
            as_attachment=True,
            download_name=f"{table_name}.xlsx")
    except Exception as e:
        logging.error("api_download error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


# ────────────────────────────────────────────
# 指標ビュー
#   保存されたSELECT文による動的ビュー。
#   1行＝（指標名, SQLクエリ, 表示順位数）のタプル。
#   クエリは fujinp DB（公式テーブルの所在）に対する
#   単文のSELECTのみ実行を許可する（読み取り専用の建付けを守る）。
# ────────────────────────────────────────────

INDICATOR_DB        = 'fujinp'   # クエリ実行先（DB_CHOICES のキー）
INDICATOR_MAX_ROWS  = 1000       # 実行結果の最大行数
INDICATOR_PREVIEW_ROWS = 200     # 試し実行の最大行数


def _validate_select(query):
    """
    指標ビューのSQLクエリを検査する。
    許可：単文のSELECTのみ。
    戻り値 (正規化済みクエリ, None) または (None, エラーメッセージ)。
    """
    q = (query or '').strip()
    # 末尾のセミコロンは1つだけ許して落とす
    if q.endswith(';'):
        q = q[:-1].rstrip()
    if not q:
        return None, 'SQLクエリが空です'
    if ';' in q:
        return None, 'セミコロンを含む複文は実行できません（単文のSELECTのみ）'
    if not re.match(r'^select\b', q, flags=re.IGNORECASE):
        return None, 'SELECT文のみ実行できます'
    low = q.lower()
    for bad in ('into outfile', 'into dumpfile'):
        if bad in low:
            return None, f'許可されない構文が含まれています：{bad}'
    # 2026-10-26 セキュリティ改修（R-4）：読めるのは登録簿に載った公式テーブルだけにする
    err = _check_query_scope(q)
    if err:
        return None, err
    return q, None


# ── 指標クエリの読み取り範囲の検査（2026-10-26 R-4）──
#   旧版は SELECT で始まりさえすれば，users など同じDBの任意の表，他のDB
#   （fujinp$default.users のような修飾），information_schema，LOAD_FILE()・SLEEP() 等が通った．
#   ここでは文字列リテラルとコメントを除いた本文を調べ，
#     ・危険な関数・システムスキーマ・他DBの修飾（$ を含む名前）・INTO・ロック句を拒否し，
#     ・クエリに現れる名前のうち「実行先DBに実在するが登録簿に載っていない表」の名前があれば拒否する．
#   表を読むには名前を書くしかないので，名前の照合で読み取り範囲を登録簿に閉じ込められる．
#   （列名がたまたま未登録の表と同名だと誤って拒否されるが，安全側に倒す）
_Q_STRIP_RE = re.compile(
    r"'(?:[^'\\]|\\.|'')*'"          # '...'
    r'|"(?:[^"\\]|\\.|"")*"'         # "..."
    r'|/\*.*?\*/'                      # /* ... */
    r'|(?:--\s|#)[^\n]*',               # -- ... / # ...
    re.S)
_Q_DENY_RE = re.compile(
    r'\b(load_file|sleep|benchmark|get_lock|release_lock|release_all_locks|is_free_lock|'
    r'is_used_lock|master_pos_wait|source_pos_wait|sys_exec|sys_eval|load_data)\s*\('
    r'|`?\b(information_schema|performance_schema|mysql|sys)\b`?\s*\.'
    r'|\binto\b|\bfor\s+update\b|\block\s+in\s+share\s+mode\b|\bfor\s+share\b'
    r'|\bhandler\b|\bprocedure\b',
    re.I)
_Q_IDENT_RE = re.compile(r'`([^`]+)`|([0-9A-Za-z_$\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff\uff00-\uffef]+)')
_scope_cache = {'at': 0.0, 'forbidden': None}
_SCOPE_TTL = 5    # 登録簿の追加直後でも待たずに使えるよう短く保つ


def _forbidden_tables():
    """実行先DBにあって登録簿に載っていない表の名前（小文字）の集合。5秒キャッシュ。"""
    import time
    now = time.time()
    if _scope_cache['forbidden'] is not None and now - _scope_cache['at'] < _SCOPE_TTL:
        return _scope_cache['forbidden']
    conn = mysql.connector.connect(**DB_CHOICES[INDICATOR_DB]())
    try:
        cur = conn.cursor()
        cur.execute("SHOW TABLES")
        actual = {r[0] for r in cur.fetchall()}
        cur.close()
    finally:
        conn.close()
    conn = mysql.connector.connect(**DatabaseConfig.default())
    try:
        cur = conn.cursor()
        cur.execute("SELECT table_name FROM official_data_archive_tables "
                    "WHERE database_name = %s", (INDICATOR_DB,))
        registered = {r[0].lower() for r in cur.fetchall()}
        cur.close()
    finally:
        conn.close()
    forbidden = {t.lower() for t in actual} - registered
    _scope_cache.update(at=now, forbidden=forbidden)
    return forbidden


def _check_query_scope(q):
    """読み取り範囲の検査。問題があればエラーメッセージ，なければ None。"""
    # MySQL は /*! ... */ の中身を実行する（バージョン付きコメント）．コメントとして
    # 読み飛ばすと検査をすり抜けるので，この形は使わせない．
    if '/*!' in q:
        return 'バージョン付きコメント（/*! … */）は使えません'
    body = _Q_STRIP_RE.sub(' ', q)
    m = _Q_DENY_RE.search(body)
    if m:
        return '許可されない構文・関数が含まれています：' + m.group(0).strip()
    if '$' in body:
        return '他のデータベースを指す名前は使えません（登録簿の公式テーブルだけを読めます）'
    try:
        forbidden = _forbidden_tables()
    except Exception as e:
        logging.error("official_data_archive _forbidden_tables: %s", e)
        return '読み取り範囲を確認できないため実行しません'
    for a, b in _Q_IDENT_RE.findall(body):
        name = (a or b).lower()
        if name in forbidden:
            return f'登録簿に載っていないテーブルは読めません：{a or b}'
    # 二重引用符を名前として扱う設定（ANSI_QUOTES）への備え：
    # "..." の中身がそのまま未登録の表の名前なら拒否する
    for lit in re.findall(r'"((?:[^"\\]|\\.|"")*)"', q):
        if lit.lower() in forbidden:
            return f'登録簿に載っていないテーブルは読めません：{lit}'
    return None


def _run_indicator_query(query, max_rows):
    """
    検査済みクエリを INDICATOR_DB で実行し、
    (columns, rows(serialized), truncated) を返す。例外は呼び出し側で処理。
    """
    conn = mysql.connector.connect(**DB_CHOICES[INDICATOR_DB]())
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(query)
        columns = ([d[0] for d in cursor.description]
                   if cursor.description else [])
        rows = cursor.fetchmany(max_rows + 1)
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        serialized = [{k: safe_val(v) for k, v in row.items()}
                      for row in rows]
        return columns, serialized, truncated
    finally:
        if conn.is_connected():
            cursor.close(); conn.close()


def _get_indicator(iv_id):
    """指標ビュー1件を取得（管理グループ名つき）。なければ None。"""
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT v.id, v.name, v.query, v.sort_order, v.chart_type,
                   v.manager_group_id, g.name AS manager_group_name
            FROM official_data_archive_indicator_views v
            LEFT JOIN user_groups g ON v.manager_group_id = g.id
            WHERE v.id = %s
        """, (iv_id,))
        return cursor.fetchone()
    except Exception as e:
        logging.error("official_data_archive _get_indicator: %s", e)
        return None
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


def _can_manage_item(user_id, manager_group_id):
    """アイテム単位の管理可否（公式テーブル・指標ビュー共通の判定）。"""
    if is_global_manager(user_id):
        return True
    if not manager_group_id:
        return False
    my_ids = {g['id'] for g in user_manage_groups(user_id)}
    return manager_group_id in my_ids


@official_data_archive_bp.route('/api/indicators', methods=['GET'])
@login_required
def api_indicators():
    """
    指標ビュー一覧を表示順位（sort_order 昇順、同順位は名前順）で返す。
    各アイテムに can_manage を付け、追加可否（can_add）と
    指定できる管理グループ一覧（manage_groups）も返す。
    """
    user_id = session.get('user_id')
    if not can_view(user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT v.id, v.name, v.query, v.sort_order, v.chart_type,
                   v.manager_group_id, g.name AS manager_group_name
            FROM official_data_archive_indicator_views v
            LEFT JOIN user_groups g ON v.manager_group_id = g.id
            ORDER BY v.sort_order, v.name
        """)
        views = cursor.fetchall()
        cursor.close(); conn.close()

        is_global = is_global_manager(user_id)
        my_groups = user_manage_groups(user_id)
        my_group_ids = {g['id'] for g in my_groups}
        for v in views:
            v['can_manage'] = bool(
                is_global or (v['manager_group_id'] in my_group_ids
                              if v['manager_group_id'] else False))
            # DECIMAL は Decimal で返るため JSON 化できるよう float に正規化
            if v.get('sort_order') is not None:
                v['sort_order'] = float(v['sort_order'])

        return jsonify({
            'success': True,
            'views': views,
            'can_add': can_add(user_id),
            'is_global': is_global,
            'manage_groups': my_groups,
        })
    except Exception as e:
        logging.error("api_indicators error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@official_data_archive_bp.route('/api/indicators/run', methods=['GET'])
@login_required
def api_indicator_run():
    """
    登録済み指標ビューのクエリを実行し、結果を返す。
    パラメータ: id
    """
    user_id = session.get('user_id')
    if not can_view(user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    iv = _get_indicator(request.args.get('id', type=int))
    if not iv:
        return jsonify({'success': False,
                        'error': '指標ビューが見つかりません'}), 404
    query, err = _validate_select(iv['query'])
    if err:
        return jsonify({'success': False, 'error': err}), 400
    try:
        columns, rows, truncated = _run_indicator_query(
            query, INDICATOR_MAX_ROWS)
        return jsonify({
            'success': True,
            'id': iv['id'],
            'name': iv['name'],
            'query': iv['query'],
            'chart_type': iv.get('chart_type') or 'bar',
            'columns': columns,
            'rows': rows,
            'truncated': truncated,
        })
    except Exception as e:
        logging.error("api_indicator_run error (id=%s): %s",
                      iv.get('id'), e)
        return jsonify({'success': False,
                        'error': f'クエリ実行エラー：{e}'}), 400


@official_data_archive_bp.route('/api/indicators/preview', methods=['POST'])
@login_required
def api_indicator_preview():
    """
    試し実行（保存前のプレビュー）。管理者のみ。
    リクエスト: { "query": "SELECT ..." }
    """
    user_id = session.get('user_id')
    if not can_add(user_id):
        return jsonify({'success': False, 'error': '管理権限がありません'}), 403
    data = request.json or {}
    query, err = _validate_select(data.get('query'))
    if err:
        return jsonify({'success': False, 'error': err}), 400
    try:
        columns, rows, truncated = _run_indicator_query(
            query, INDICATOR_PREVIEW_ROWS)
        return jsonify({'success': True, 'columns': columns,
                        'rows': rows, 'truncated': truncated})
    except Exception as e:
        return jsonify({'success': False,
                        'error': f'クエリ実行エラー：{e}'}), 400


@official_data_archive_bp.route('/api/indicators/save', methods=['POST'])
@login_required
def api_indicator_save():
    """
    指標ビューの追加・編集。
    リクエスト: {
        "id":               null=新規 / 既存id=編集,
        "name":             "指標名",          # 必須
        "query":            "SELECT ...",      # 必須（単文SELECT）
        "sort_order":       10.5,              # 表示順位数（小さいほど上位）
                                               #   8桁整数部＋3桁小数部
                                               #   範囲 0〜99999999.999、小数第3位まで
        "chart_type":       "bar" / "line",    # グラフ種別（省略時 bar）
        "manager_group_id": 12                 # 公式テーブルと同じ規約
    }
    """
    user_id = session.get('user_id')
    if not can_add(user_id):
        return jsonify({'success': False, 'error': '管理権限がありません'}), 403
    try:
        data  = request.json or {}
        iv_id = data.get('id') or None
        name  = (data.get('name') or '').strip()
        manager_group_id = data.get('manager_group_id') or None
        # 表示順位数：8桁整数部＋3桁小数部の小数。
        # DECIMAL(11,3) に格納するため、桁あふれ・小数桁超過を検査する。
        try:
            sort_order = float(data.get('sort_order', 100))
        except (TypeError, ValueError):
            return jsonify({'success': False,
                            'error': '表示順位数は数値で指定してください'}), 400
        if not math.isfinite(sort_order):
            return jsonify({'success': False,
                            'error': '表示順位数は数値で指定してください'}), 400
        # 小数第3位までに丸めてから範囲・桁を検査
        sort_order = round(sort_order, 3)
        if not (0 <= sort_order <= 99999999.999):
            return jsonify({'success': False,
                            'error': '表示順位数は0〜99999999.999の範囲で'
                                     '指定してください'}), 400

        chart_type = (data.get('chart_type') or 'bar').strip().lower()
        if chart_type not in ('bar', 'line'):
            chart_type = 'bar'

        if not name:
            return jsonify({'success': False,
                            'error': '指標名を入力してください'}), 400
        query, err = _validate_select(data.get('query'))
        if err:
            return jsonify({'success': False, 'error': err}), 400

        # ── 管理グループの検証（公式テーブルの追加と同じ規則） ──
        is_global = is_global_manager(user_id)
        allowed_ids = {g['id'] for g in user_manage_groups(user_id)}
        if manager_group_id is None:
            if not is_global:
                return jsonify({'success': False,
                                'error': '管理グループを指定してください'}), 400
        else:
            try:
                manager_group_id = int(manager_group_id)
            except (TypeError, ValueError):
                return jsonify({'success': False,
                                'error': '管理グループの指定が不正です'}), 400
            if manager_group_id not in allowed_ids:
                return jsonify({'success': False,
                                'error': '指定できない管理グループです'}), 403

        now  = get_jst_now()
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        if iv_id:
            # ── 編集：対象アイテムの管理権限を確認 ──
            cursor.execute("""
                SELECT manager_group_id
                FROM official_data_archive_indicator_views WHERE id = %s
            """, (iv_id,))
            row = cursor.fetchone()
            if not row:
                return jsonify({'success': False,
                                'error': '指標ビューが見つかりません'}), 404
            if not _can_manage_item(user_id, row['manager_group_id']):
                return jsonify({'success': False,
                                'error': 'この指標ビューの管理権限が'
                                         'ありません'}), 403
            cursor.execute("""
                UPDATE official_data_archive_indicator_views
                SET name=%s, query=%s, sort_order=%s, chart_type=%s,
                    manager_group_id=%s, updated_by=%s, updated_at=%s
                WHERE id=%s
            """, (name, query, sort_order, chart_type, manager_group_id,
                  user_id, now, iv_id))
            conn.commit()
            return jsonify({'success': True, 'id': iv_id})
        else:
            cursor.execute("""
                INSERT INTO official_data_archive_indicator_views
                    (name, query, sort_order, chart_type, manager_group_id,
                     created_by, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (name, query, sort_order, chart_type, manager_group_id,
                  user_id, now))
            conn.commit()
            return jsonify({'success': True, 'id': cursor.lastrowid})
    except Exception as e:
        logging.error("api_indicator_save error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@official_data_archive_bp.route('/api/indicators/chart_type', methods=['POST'])
@login_required
def api_indicator_chart_type():
    """
    グラフ種別（棒／折れ線）だけを更新する軽量エンドポイント。
    リクエスト: { "id": 指標ビューのid, "chart_type": "bar" / "line" }
    管理権限のある利用者の選択のみ保存する（権限がなければ 403）。
    """
    user_id = session.get('user_id')
    if not can_view(user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    data = request.json or {}
    iv = _get_indicator(data.get('id'))
    if not iv:
        return jsonify({'success': False,
                        'error': '指標ビューが見つかりません'}), 404
    if not _can_manage_item(user_id, iv.get('manager_group_id')):
        return jsonify({'success': False,
                        'error': 'この指標ビューの管理権限がありません'}), 403
    chart_type = (data.get('chart_type') or '').strip().lower()
    if chart_type not in ('bar', 'line'):
        return jsonify({'success': False,
                        'error': 'グラフ種別の指定が不正です'}), 400
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE official_data_archive_indicator_views
            SET chart_type=%s, updated_by=%s, updated_at=%s
            WHERE id=%s
        """, (chart_type, user_id, get_jst_now(), iv['id']))
        conn.commit()
        return jsonify({'success': True, 'id': iv['id'],
                        'chart_type': chart_type})
    except Exception as e:
        logging.error("api_indicator_chart_type error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@official_data_archive_bp.route('/api/indicators/delete', methods=['POST'])
@login_required
def api_indicator_delete():
    """
    指標ビューの削除。全体管理者またはアイテムの管理グループ所属者のみ。
    リクエスト: { "id": 指標ビューのid }
    """
    user_id = session.get('user_id')
    try:
        data  = request.json or {}
        iv_id = data.get('id')
        if not iv_id:
            return jsonify({'success': False, 'error': 'idが必要です'}), 400

        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT manager_group_id
            FROM official_data_archive_indicator_views WHERE id = %s
        """, (iv_id,))
        row = cursor.fetchone()
        if not row:
            return jsonify({'success': False,
                            'error': '指標ビューが見つかりません'}), 404
        if not _can_manage_item(user_id, row['manager_group_id']):
            return jsonify({'success': False,
                            'error': 'この指標ビューの管理権限がありません'}), 403
        cursor.execute(
            "DELETE FROM official_data_archive_indicator_views WHERE id=%s",
            (iv_id,))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("api_indicator_delete error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@official_data_archive_bp.route('/api/indicators/download', methods=['GET'])
@login_required
def api_indicator_download():
    """指標ビューの実行結果をXLSXでダウンロードする。パラメータ: id"""
    import io
    import openpyxl
    from openpyxl.styles import PatternFill, Font
    from flask import send_file

    user_id = session.get('user_id')
    if not can_view(user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    iv = _get_indicator(request.args.get('id', type=int))
    if not iv:
        return jsonify({'success': False,
                        'error': '指標ビューが見つかりません'}), 404
    query, err = _validate_select(iv['query'])
    if err:
        return jsonify({'success': False, 'error': err}), 400
    try:
        columns, rows, truncated = _run_indicator_query(
            query, INDICATOR_MAX_ROWS)

        wb = openpyxl.Workbook()
        ws = wb.active
        # シート名に使えない文字を除き31文字に収める
        sheet = re.sub(r'[\[\]:*?/\\]', '_', iv['name'])[:31] or 'indicator'
        ws.title = sheet

        ws.append(columns)
        for row in rows:
            ws.append([row.get(c) for c in columns])

        header_fill = PatternFill('solid', fgColor='1A3A5C')
        header_font = Font(color='FFFFFF', bold=True)
        for cell in ws[1]:
            cell.fill = header_fill
            cell.font = header_font

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        fname = re.sub(r'[\\/:*?"<>|]', '_', iv['name']) or 'indicator'
        return send_file(
            buf,
            mimetype='application/vnd.openxmlformats-officedocument'
                     '.spreadsheetml.sheet',
            as_attachment=True,
            download_name=f"{fname}.xlsx")
    except Exception as e:
        logging.error("api_indicator_download error (id=%s): %s",
                      iv.get('id'), e)
        return jsonify({'success': False,
                        'error': f'クエリ実行エラー：{e}'}), 400


# ────────────────────────────────────────────
# 複合指標
#   1つ以上（高々 COMPOSITE_MAX 個）の単一指標と色のペアから構成。
#   表示は折れ線固定・縦軸0〜全系列の最大値（フロント側）。
#   ここでは構成の管理と、各構成指標のクエリの一括実行を提供する。
# ────────────────────────────────────────────

COMPOSITE_MAX = 5
COLOR_RE = re.compile(r'^#[0-9A-Fa-f]{6}$')


@official_data_archive_bp.route('/api/composites', methods=['GET'])
@login_required
def api_composites():
    """
    複合指標一覧を表示順位（sort_order 昇順、同順位は名前順）で返す。
    各アイテムに構成（単一指標と色のペア、seq順）と can_manage を付ける。
    モーダル用に単一指標の選択肢（indicator_options）も返す。
    """
    user_id = session.get('user_id')
    if not can_view(user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT c.id, c.name, c.sort_order,
                   c.manager_group_id, g.name AS manager_group_name
            FROM official_data_archive_composites c
            LEFT JOIN user_groups g ON c.manager_group_id = g.id
            ORDER BY c.sort_order, c.name
        """)
        composites = cursor.fetchall()

        cursor.execute("""
            SELECT cc.composite_id, cc.indicator_view_id, cc.color, cc.seq,
                   v.name AS indicator_name
            FROM official_data_archive_composite_components cc
            LEFT JOIN official_data_archive_indicator_views v
                   ON cc.indicator_view_id = v.id
            ORDER BY cc.composite_id, cc.seq
        """)
        comp_rows = cursor.fetchall()

        cursor.execute("""
            SELECT id, name FROM official_data_archive_indicator_views
            ORDER BY sort_order, name
        """)
        indicator_options = cursor.fetchall()
        cursor.close(); conn.close()

        by_comp = {}
        for r in comp_rows:
            by_comp.setdefault(r['composite_id'], []).append({
                'indicator_view_id': r['indicator_view_id'],
                'name':  r['indicator_name'] or '（削除された単一指標）',
                'color': r['color'],
                'seq':   r['seq'],
            })

        is_global = is_global_manager(user_id)
        my_groups = user_manage_groups(user_id)
        my_group_ids = {g['id'] for g in my_groups}
        for c in composites:
            c['components'] = by_comp.get(c['id'], [])
            c['can_manage'] = bool(
                is_global or (c['manager_group_id'] in my_group_ids
                              if c['manager_group_id'] else False))

        return jsonify({
            'success': True,
            'composites': composites,
            'indicator_options': indicator_options,
            'can_add': can_add(user_id),
            'is_global': is_global,
            'manage_groups': my_groups,
            'max_components': COMPOSITE_MAX,
        })
    except Exception as e:
        logging.error("api_composites error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@official_data_archive_bp.route('/api/composites/run', methods=['GET'])
@login_required
def api_composite_run():
    """
    複合指標の全構成クエリを実行し、系列の配列を返す。
    一部の構成が失敗しても他は返す（失敗分は error を持つ）。
    パラメータ: id
    """
    user_id = session.get('user_id')
    if not can_view(user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    comp_id = request.args.get('id', type=int)
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT id, name FROM official_data_archive_composites
            WHERE id = %s
        """, (comp_id,))
        comp = cursor.fetchone()
        if not comp:
            return jsonify({'success': False,
                            'error': '複合指標が見つかりません'}), 404
        cursor.execute("""
            SELECT cc.color, cc.seq, v.id AS iv_id, v.name, v.query
            FROM official_data_archive_composite_components cc
            LEFT JOIN official_data_archive_indicator_views v
                   ON cc.indicator_view_id = v.id
            WHERE cc.composite_id = %s
            ORDER BY cc.seq
        """, (comp_id,))
        comps = cursor.fetchall()
        cursor.close(); conn.close()

        series = []
        for c in comps:
            entry = {'name': c['name'] or '（削除された単一指標）',
                     'color': c['color']}
            if not c['iv_id']:
                entry['error'] = '構成する単一指標が削除されています'
                series.append(entry); continue
            query, err = _validate_select(c['query'])
            if err:
                entry['error'] = err
                series.append(entry); continue
            try:
                columns, rows, truncated = _run_indicator_query(
                    query, INDICATOR_MAX_ROWS)
                entry['columns'] = columns
                entry['rows'] = rows
                entry['truncated'] = truncated
            except Exception as qe:
                entry['error'] = f'クエリ実行エラー：{qe}'
            series.append(entry)

        return jsonify({'success': True, 'id': comp['id'],
                        'name': comp['name'], 'series': series})
    except Exception as e:
        logging.error("api_composite_run error (id=%s): %s", comp_id, e)
        return jsonify({'success': False, 'error': str(e)}), 500


@official_data_archive_bp.route('/api/composites/save', methods=['POST'])
@login_required
def api_composite_save():
    """
    複合指標の追加・編集。
    リクエスト: {
        "id":               null=新規 / 既存id=編集,
        "name":             "複合指標名",     # 必須
        "sort_order":       10,
        "manager_group_id": 12,
        "components": [                       # 1〜COMPOSITE_MAX 個
            {"indicator_view_id": 3, "color": "#2e6da4"}, ...
        ]
    }
    構成は全削除→再挿入で更新する。
    """
    user_id = session.get('user_id')
    if not can_add(user_id):
        return jsonify({'success': False, 'error': '管理権限がありません'}), 403
    try:
        data    = request.json or {}
        comp_id = data.get('id') or None
        name    = (data.get('name') or '').strip()
        manager_group_id = data.get('manager_group_id') or None
        components = data.get('components') or []
        try:
            sort_order = int(data.get('sort_order', 100))
        except (TypeError, ValueError):
            return jsonify({'success': False,
                            'error': '表示順位数は整数で指定してください'}), 400

        if not name:
            return jsonify({'success': False,
                            'error': '複合指標名を入力してください'}), 400
        if not (1 <= len(components) <= COMPOSITE_MAX):
            return jsonify({'success': False,
                            'error': f'構成は1〜{COMPOSITE_MAX}個で'
                                     '指定してください'}), 400

        # ── 管理グループの検証（既存と同じ規則） ──
        is_global = is_global_manager(user_id)
        allowed_ids = {g['id'] for g in user_manage_groups(user_id)}
        if manager_group_id is None:
            if not is_global:
                return jsonify({'success': False,
                                'error': '管理グループを指定してください'}), 400
        else:
            try:
                manager_group_id = int(manager_group_id)
            except (TypeError, ValueError):
                return jsonify({'success': False,
                                'error': '管理グループの指定が不正です'}), 400
            if manager_group_id not in allowed_ids:
                return jsonify({'success': False,
                                'error': '指定できない管理グループです'}), 403

        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        # ── 構成の検証（単一指標の存在・色の形式） ──
        cursor.execute(
            "SELECT id FROM official_data_archive_indicator_views")
        valid_iv_ids = {r['id'] for r in cursor.fetchall()}
        cleaned = []
        for i, comp in enumerate(components, 1):
            try:
                iv_id = int(comp.get('indicator_view_id'))
            except (TypeError, ValueError):
                return jsonify({'success': False,
                                'error': f'構成{i}：単一指標を選んで'
                                         'ください'}), 400
            if iv_id not in valid_iv_ids:
                return jsonify({'success': False,
                                'error': f'構成{i}：指定された単一指標が'
                                         '存在しません'}), 400
            color = (comp.get('color') or '').strip()
            if not COLOR_RE.match(color):
                return jsonify({'success': False,
                                'error': f'構成{i}：色は #RRGGBB 形式で'
                                         '指定してください'}), 400
            cleaned.append((iv_id, color))

        now = get_jst_now()
        if comp_id:
            # ── 編集：対象の管理権限を確認 ──
            cursor.execute("""
                SELECT manager_group_id
                FROM official_data_archive_composites WHERE id = %s
            """, (comp_id,))
            row = cursor.fetchone()
            if not row:
                return jsonify({'success': False,
                                'error': '複合指標が見つかりません'}), 404
            if not _can_manage_item(user_id, row['manager_group_id']):
                return jsonify({'success': False,
                                'error': 'この複合指標の管理権限が'
                                         'ありません'}), 403
            cursor.execute("""
                UPDATE official_data_archive_composites
                SET name=%s, sort_order=%s, manager_group_id=%s,
                    updated_by=%s, updated_at=%s
                WHERE id=%s
            """, (name, sort_order, manager_group_id, user_id, now, comp_id))
            cursor.execute("""
                DELETE FROM official_data_archive_composite_components
                WHERE composite_id = %s
            """, (comp_id,))
        else:
            cursor.execute("""
                INSERT INTO official_data_archive_composites
                    (name, sort_order, manager_group_id,
                     created_by, created_at)
                VALUES (%s, %s, %s, %s, %s)
            """, (name, sort_order, manager_group_id, user_id, now))
            comp_id = cursor.lastrowid

        for seq, (iv_id, color) in enumerate(cleaned, 1):
            cursor.execute("""
                INSERT INTO official_data_archive_composite_components
                    (composite_id, indicator_view_id, color, seq)
                VALUES (%s, %s, %s, %s)
            """, (comp_id, iv_id, color, seq))

        conn.commit()
        return jsonify({'success': True, 'id': comp_id})
    except Exception as e:
        logging.error("api_composite_save error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@official_data_archive_bp.route('/api/composites/delete', methods=['POST'])
@login_required
def api_composite_delete():
    """
    複合指標の削除（構成も併せて削除）。
    全体管理者またはアイテムの管理グループ所属者のみ。
    リクエスト: { "id": 複合指標のid }
    ※ 構成する単一指標そのものには影響しない。
    """
    user_id = session.get('user_id')
    try:
        data    = request.json or {}
        comp_id = data.get('id')
        if not comp_id:
            return jsonify({'success': False, 'error': 'idが必要です'}), 400

        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT manager_group_id
            FROM official_data_archive_composites WHERE id = %s
        """, (comp_id,))
        row = cursor.fetchone()
        if not row:
            return jsonify({'success': False,
                            'error': '複合指標が見つかりません'}), 404
        if not _can_manage_item(user_id, row['manager_group_id']):
            return jsonify({'success': False,
                            'error': 'この複合指標の管理権限がありません'}), 403
        cursor.execute("""
            DELETE FROM official_data_archive_composite_components
            WHERE composite_id = %s
        """, (comp_id,))
        cursor.execute(
            "DELETE FROM official_data_archive_composites WHERE id = %s",
            (comp_id,))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("api_composite_delete error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ────────────────────────────────────────────
# 各種データ一括ダウンロード
#   登録簿に載っている公式テーブル・指標の定義情報を、まとめて
#   テキスト（表示・クリップボード用）またはファイルで取り出す。
#   いずれも閲覧権限（regular以上）で利用できる読み取り専用機能。
#   - テーブル名一括：登録簿のテーブル名一覧
#   - スキーマ一括：各テーブルの SHOW CREATE TABLE
#   - 単一指標一括：指標ビューの名称・SQL等
#   - 複合指標一括：複合指標の構成（単一指標と色）
# ────────────────────────────────────────────

def _registered_tables():
    """登録簿の全アイテムを表示名順で返す（dictのlist）。"""
    conn = mysql.connector.connect(**DatabaseConfig.default())
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT t.id, t.table_name, t.database_name,
                   t.display_name, t.note, g.name AS manager_group_name
            FROM official_data_archive_tables t
            LEFT JOIN user_groups g ON t.manager_group_id = g.id
            ORDER BY COALESCE(t.display_name, t.table_name)
        """)
        return cursor.fetchall()
    finally:
        if conn.is_connected():
            cursor.close(); conn.close()


def _bulk_tablenames_text():
    """テーブル名一括：DBごとに表示名つきのテーブル名一覧を組み立てる。"""
    rows = _registered_tables()
    lines = ['# 公式テーブル名一覧（{0} 件）'.format(len(rows)),
             '# 生成日時: {0} JST'.format(
                 get_jst_now().strftime('%Y-%m-%d %H:%M:%S')),
             '']
    by_db = {}
    for r in rows:
        by_db.setdefault(r['database_name'], []).append(r)
    for db_key in sorted(by_db.keys()):
        lines.append('## [{0}]'.format(db_key))
        for r in by_db[db_key]:
            disp = r['display_name'] or ''
            note = ('  — ' + disp) if disp else ''
            lines.append('{0}{1}'.format(r['table_name'], note))
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


def _bulk_schema_text():
    """スキーマ一括：各テーブルの SHOW CREATE TABLE をDBごとにまとめる。"""
    rows = _registered_tables()
    out = ['-- 公式テーブル スキーマ一括（{0} 件）'.format(len(rows)),
           '-- 生成日時: {0} JST'.format(
               get_jst_now().strftime('%Y-%m-%d %H:%M:%S')),
           '']
    by_db = {}
    for r in rows:
        by_db.setdefault(r['database_name'], []).append(r)
    for db_key in sorted(by_db.keys()):
        out.append('-- ========================================')
        out.append('-- データベース: {0}'.format(db_key))
        out.append('-- ========================================')
        if db_key not in DB_CHOICES:
            out.append('-- （未対応のデータベース指定のため取得できません）')
            out.append('')
            continue
        try:
            conn = mysql.connector.connect(**DB_CHOICES[db_key]())
            cur = conn.cursor()
            for r in by_db[db_key]:
                tbl = r['table_name']
                disp = r['display_name'] or ''
                out.append('-- ----------------------------------------')
                out.append('-- {0}{1}'.format(
                    tbl, ('  ' + disp) if disp else ''))
                out.append('-- ----------------------------------------')
                try:
                    cur.execute('SHOW CREATE TABLE `%s`' % tbl)
                    row = cur.fetchone()
                    ddl = row[1] if row and len(row) > 1 else None
                    out.append((ddl or '-- （取得できませんでした）') + ';')
                except Exception as te:
                    out.append('-- 取得エラー: {0}'.format(te))
                out.append('')
            cur.close(); conn.close()
        except Exception as ce:
            out.append('-- データベース接続エラー: {0}'.format(ce))
            out.append('')
    return '\n'.join(out).rstrip() + '\n'


def _bulk_indicators_text():
    """単一指標一括：指標ビューの定義（名称・順位・グラフ種別・SQL）。"""
    conn = mysql.connector.connect(**DatabaseConfig.default())
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT v.id, v.name, v.query, v.sort_order, v.chart_type,
                   g.name AS manager_group_name
            FROM official_data_archive_indicator_views v
            LEFT JOIN user_groups g ON v.manager_group_id = g.id
            ORDER BY v.sort_order, v.name
        """)
        views = cursor.fetchall()
    finally:
        if conn.is_connected():
            cursor.close(); conn.close()

    out = ['-- 単一指標一括（{0} 件）'.format(len(views)),
           '-- 生成日時: {0} JST'.format(
               get_jst_now().strftime('%Y-%m-%d %H:%M:%S')),
           '']
    for v in views:
        so = v['sort_order']
        so = float(so) if so is not None else None
        out.append('-- ----------------------------------------')
        out.append('-- [{0}] {1}'.format(v['id'], v['name']))
        out.append('--   表示順位: {0} / グラフ: {1} / 管理: {2}'.format(
            so, v.get('chart_type') or 'bar',
            v.get('manager_group_name') or '（全体管理者専管）'))
        out.append('-- ----------------------------------------')
        q = (v['query'] or '').strip()
        out.append(q + (';' if not q.endswith(';') else ''))
        out.append('')
    return '\n'.join(out).rstrip() + '\n'


def _bulk_composites_text():
    """複合指標一括：複合指標と、その構成（単一指標と色）の一覧。"""
    conn = mysql.connector.connect(**DatabaseConfig.default())
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT c.id, c.name, c.sort_order, g.name AS manager_group_name
            FROM official_data_archive_composites c
            LEFT JOIN user_groups g ON c.manager_group_id = g.id
            ORDER BY c.sort_order, c.name
        """)
        composites = cursor.fetchall()
        cursor.execute("""
            SELECT cc.composite_id, cc.color, cc.seq,
                   v.id AS iv_id, v.name AS indicator_name
            FROM official_data_archive_composite_components cc
            LEFT JOIN official_data_archive_indicator_views v
                   ON cc.indicator_view_id = v.id
            ORDER BY cc.composite_id, cc.seq
        """)
        comp_rows = cursor.fetchall()
    finally:
        if conn.is_connected():
            cursor.close(); conn.close()

    by_comp = {}
    for r in comp_rows:
        by_comp.setdefault(r['composite_id'], []).append(r)

    out = ['# 複合指標一括（{0} 件）'.format(len(composites)),
           '# 生成日時: {0} JST'.format(
               get_jst_now().strftime('%Y-%m-%d %H:%M:%S')),
           '']
    for c in composites:
        so = c['sort_order']
        so = float(so) if so is not None else None
        out.append('## [{0}] {1}'.format(c['id'], c['name']))
        out.append('   表示順位: {0} / 管理: {1}'.format(
            so, c.get('manager_group_name') or '（全体管理者専管）'))
        comps = by_comp.get(c['id'], [])
        if not comps:
            out.append('   （構成なし）')
        for cc in comps:
            out.append('   {0}. {1}  [{2}]  (単一指標id={3})'.format(
                cc['seq'],
                cc['indicator_name'] or '（削除された単一指標）',
                cc['color'], cc['iv_id']))
        out.append('')
    return '\n'.join(out).rstrip() + '\n'


# 種別キー → (生成関数, 表示名, ダウンロードファイル名)
BULK_KINDS = {
    'tablenames':  (_bulk_tablenames_text, 'テーブル名一括',
                    'official_tablenames.txt'),
    'schema':      (_bulk_schema_text, 'スキーマ一括',
                    'official_schema.sql'),
    'indicators':  (_bulk_indicators_text, '単一指標一括',
                    'official_indicators.sql'),
    'composites':  (_bulk_composites_text, '複合指標一括',
                    'official_composites.txt'),
}


@official_data_archive_bp.route('/api/bulk', methods=['GET'])
@login_required
def api_bulk():
    """
    各種データ一括の本文を返す（表示・クリップボード用）。
    パラメータ: kind（tablenames / schema / indicators / composites）
    レスポンス: { success, kind, label, filename, text }
    """
    user_id = session.get('user_id')
    if not can_view(user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    kind = (request.args.get('kind') or '').strip()
    if kind not in BULK_KINDS:
        return jsonify({'success': False,
                        'error': '種別の指定が不正です'}), 400
    fn, label, filename = BULK_KINDS[kind]
    try:
        text = fn()
        return jsonify({'success': True, 'kind': kind, 'label': label,
                        'filename': filename, 'text': text})
    except Exception as e:
        logging.error("api_bulk error (kind=%s): %s", kind, e)
        return jsonify({'success': False,
                        'error': f'生成に失敗しました：{e}'}), 500


@official_data_archive_bp.route('/api/bulk/download', methods=['GET'])
@login_required
def api_bulk_download():
    """各種データ一括をテキストファイルでダウンロードする。パラメータ: kind"""
    import io
    from flask import send_file

    user_id = session.get('user_id')
    if not can_view(user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    kind = (request.args.get('kind') or '').strip()
    if kind not in BULK_KINDS:
        return jsonify({'success': False,
                        'error': '種別の指定が不正です'}), 400
    fn, label, filename = BULK_KINDS[kind]
    try:
        text = fn()
        buf = io.BytesIO(text.encode('utf-8-sig'))  # ExcelでもUTF-8と認識
        buf.seek(0)
        return send_file(buf, mimetype='text/plain; charset=utf-8',
                         as_attachment=True, download_name=filename)
    except Exception as e:
        logging.error("api_bulk_download error (kind=%s): %s", kind, e)
        return jsonify({'success': False,
                        'error': f'生成に失敗しました：{e}'}), 500


# ────────────────────────────────────────────
# データマイグレーション（全部一括JSONの書き出しと取り込み）
#   公式テーブル（登録簿・DDL・全行）、単一指標、複合指標を1本のJSONで
#   持ち運び、移行先で破壊的に再現する。全体管理者のみ。
#   書き出しも取り込みもブラウザが組み立て役になり、サーバとはテーブル単位
#   （大きい表は行を分割）の短いリクエストでやり取りする（タイムアウト対策）。
#   管理グループは名前で持ち運ぶ（グループ id はサイト間で一致しないため）。
#   2026-10-26：fujinpshowcase の廃止にあわせ，この版（format_version 3）を正本とする．
#   format_version 3 では，DDL から外部キー（FOREIGN KEY 制約）を外して持ち運ぶ．
#   公式テーブルは年度ごとのスナップショットで，表どうしの参照整合は保証されない
#   （例：時間割の科目コードが科目一覧で一意でない・存在しない）ため，外部キーは
#   取り込み先で作成に失敗する原因になる．取り込みでも同じ処理をかけるので，
#   古い版（format_version 1・2）のファイルもそのまま取り込める．
# ────────────────────────────────────────────

MIGRATION_FORMAT_VERSION = 3
MIGRATION_PAGE_MAX = 5000     # 書き出し1回あたりの最大行数
MIGRATION_EXPORT_TYPE = 'official_data_archive_migration'
MIGRATION_INSERT_CHUNK = 500


_FK_LINE_RE = re.compile(r'^\s*(CONSTRAINT\s+`[^`]*`\s+)?FOREIGN\s+KEY\b', re.IGNORECASE)


def _strip_foreign_keys(ddl):
    """CREATE TABLE 文から外部キー制約の行を取り除く。(新しいDDL, 外した制約の行のlist) を返す。
    SHOW CREATE TABLE の出力（1制約1行）を前提にし，残った最後の定義行の末尾カンマを整える。"""
    if not ddl:
        return ddl, []
    lines = ddl.split('\n')
    kept, removed = [], []
    for ln in lines:
        if _FK_LINE_RE.match(ln):
            removed.append(ln.strip().rstrip(','))
        else:
            kept.append(ln)
    if not removed:
        return ddl, []
    # 閉じ括弧の行（") ENGINE=..." 等）の直前の定義行から末尾カンマを外す
    for i, ln in enumerate(kept):
        if ln.lstrip().startswith(')') and i > 0:
            kept[i - 1] = kept[i - 1].rstrip().rstrip(',')
            break
    return '\n'.join(kept), removed


def _migration_val(v):
    """行データの値をJSON化可能にする（日時は文字列、Decimalは文字列で桁を保持）。"""
    import decimal
    if isinstance(v, decimal.Decimal):
        return str(v)
    return safe_val(v)


def _migration_indicators():
    """単一指標の全定義。"""
    conn = mysql.connector.connect(**DatabaseConfig.default())
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT v.id, v.name, v.query, v.sort_order, v.chart_type,
                   g.name AS manager_group_name
            FROM official_data_archive_indicator_views v
            LEFT JOIN user_groups g ON v.manager_group_id = g.id
            ORDER BY v.sort_order, v.name
        """)
        views = cursor.fetchall()
        cursor.close()
    finally:
        conn.close()
    return [{
        'id': v['id'],
        'name': v['name'],
        'query': v['query'],
        'sort_order': _migration_val(v['sort_order']),
        'chart_type': v.get('chart_type') or 'bar',
        'manager_group_name': v.get('manager_group_name'),
    } for v in views]


def _migration_composites():
    """複合指標の全定義と構成（単一指標は id と名前の両方で参照）。"""
    conn = mysql.connector.connect(**DatabaseConfig.default())
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT c.id, c.name, c.sort_order, g.name AS manager_group_name
            FROM official_data_archive_composites c
            LEFT JOIN user_groups g ON c.manager_group_id = g.id
            ORDER BY c.sort_order, c.name
        """)
        composites = cursor.fetchall()
        cursor.execute("""
            SELECT cc.composite_id, cc.color, cc.seq,
                   cc.indicator_view_id, v.name AS indicator_name
            FROM official_data_archive_composite_components cc
            LEFT JOIN official_data_archive_indicator_views v
                   ON cc.indicator_view_id = v.id
            ORDER BY cc.composite_id, cc.seq
        """)
        comp_rows = cursor.fetchall()
        cursor.close()
    finally:
        conn.close()
    by_comp = {}
    for r in comp_rows:
        by_comp.setdefault(r['composite_id'], []).append({
            'seq': r['seq'],
            'color': r['color'],
            'indicator_view_id': r['indicator_view_id'],
            'indicator_name': r['indicator_name'],
        })
    return [{
        'id': c['id'],
        'name': c['name'],
        'sort_order': _migration_val(c['sort_order']),
        'manager_group_name': c.get('manager_group_name'),
        'components': by_comp.get(c['id'], []),
    } for c in composites]


@official_data_archive_bp.route('/api/migration/manifest', methods=['GET'])
@login_required
def api_migration_manifest():
    """書き出しの目次：JSONの見出し情報、登録簿、単一指標、複合指標。
    テーブルの中身は /api/migration/table で1本ずつ取る。全体管理者のみ。"""
    user_id = session.get('user_id')
    if not is_global_manager(user_id):
        return jsonify({'success': False,
                        'error': '全体管理者のみ実行できます'}), 403
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT full_name FROM users WHERE id = %s", (user_id,))
            u = cursor.fetchone() or {}
            cursor.close()
        finally:
            conn.close()
        registry = [{
            'table_name': r['table_name'],
            'database_name': r['database_name'],
            'display_name': r.get('display_name'),
            'note': r.get('note'),
            'manager_group_name': r.get('manager_group_name'),
        } for r in _registered_tables()]
        return jsonify({
            'success': True,
            'header': {
                'export_type': MIGRATION_EXPORT_TYPE,
                'format_version': MIGRATION_FORMAT_VERSION,
                'app_name': 'official_data_archive',
                'site_name': getattr(Config, 'SITE_NAME', None) or request.host,
                'site_url': request.host_url.rstrip('/'),
                'generated_at': get_jst_now().strftime('%Y-%m-%d %H:%M:%S'),
                'generated_by': u.get('full_name'),
            },
            'registry': registry,
            'db_choices': list(DB_CHOICES.keys()),
            'indicator_views': _migration_indicators(),
            'composites': _migration_composites(),
        })
    except Exception as e:
        logging.error("api_migration_manifest error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@official_data_archive_bp.route('/api/migration/table', methods=['GET'])
@login_required
def api_migration_table():
    """登録済みテーブル1本の一部を返す。全体管理者のみ。
    パラメータ: table, db, offset, limit。offset=0 のときだけ DDL と列名を付ける。
    行の順序は主キー順（主キーが無ければ格納順）。"""
    import json
    user_id = session.get('user_id')
    if not is_global_manager(user_id):
        return jsonify({'success': False,
                        'error': '全体管理者のみ実行できます'}), 403
    table = (request.args.get('table') or '').strip()
    db = (request.args.get('db') or '').strip()
    if db not in DB_CHOICES or not _registry_lookup(table, db):
        return jsonify({'success': False,
                        'error': '登録簿にないテーブルです'}), 404
    try:
        offset = max(0, int(request.args.get('offset') or 0))
        limit = min(MIGRATION_PAGE_MAX,
                    max(1, int(request.args.get('limit') or 100)))
    except ValueError:
        return jsonify({'success': False, 'error': '数値の指定が不正です'}), 400

    conn = mysql.connector.connect(**DB_CHOICES[db]())
    try:
        cur = conn.cursor()
        out = {'success': True}
        if offset == 0:
            cur.execute('SHOW CREATE TABLE `%s`' % table)
            row = cur.fetchone()
            ddl = row[1] if row and len(row) > 1 else None
            out['ddl'], out['dropped_foreign_keys'] = _strip_foreign_keys(ddl)
        cur.execute("SHOW KEYS FROM `%s` WHERE Key_name = 'PRIMARY'" % table)
        keys = sorted(cur.fetchall(), key=lambda k: k[3])   # Seq_in_index
        order = (' ORDER BY ' + ', '.join('`%s`' % k[4] for k in keys)) if keys else ''
        cur.execute('SELECT * FROM `%s`%s LIMIT %d OFFSET %d'
                    % (table, order, limit, offset))
        cols = [d[0] for d in cur.description]
        rows = [[_migration_val(v) for v in rec] for rec in cur.fetchall()]
        cur.close()
        if offset == 0:
            out['columns'] = cols
        out['rows'] = rows
        out['done'] = len(rows) < limit
        out['bytes'] = len(json.dumps(rows, ensure_ascii=False, default=str))
        return jsonify(out)
    except Exception as e:
        logging.error("api_migration_table %s: %s", table, e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()


# ── 2026-10-26 改修：取り込みの安全化と全消去 ──
#   ・取り込み（全消去・テーブル・定義）は admin のみ（旧版は全体管理者＝まいぐるの役割でも可で，
#     リクエストの DDL を無検査で実行し，任意テーブルを DROP できた）．
#   ・DDL は「同名テーブルの CREATE TABLE 1 文」に限る．
#   ・カーネルや他アプリの表の名前は，取り込みでも全消去でも扱わない．
#   ・全消去：移行先の登録簿にある公式テーブル，データ更新のバックアップ表，
#     登録簿・単一指標・複合指標・データ更新の記録をすべて消す（何度実行しても同じ結果）．

# 取り込み・全消去で決して触らない表（カーネルと主要アプリの表）
MIG_PROTECTED_TABLES = {
    'users', 'user_groups', 'user_group_memberships', 'features', 'user_features',
    'password_reset_tokens', 'user_events', 'registration_requests', 'approved_users',
    'apps', 'app_permission_labels', 'app_feature_permissions', 'notify_ledger',
}
MIG_PROTECTED_PREFIXES = ('official_data_archive_', 'app_share_', 'user_group')
MIG_BACKUP_RE = re.compile(r'^[^\s`\'\"%;\\./]{1,43}_bak_\d{14}$')


def _mig_is_admin(user_id):
    """取り込み系は admin カテゴリのみ（DB の区分で判定）。"""
    return _get_user_category(user_id) == 'admin'


def _mig_table_ok(name):
    """取り込み・全消去で扱ってよいテーブル名か。"""
    if not name or not TABLE_NAME_RE.match(name):
        return False
    low = name.lower()
    if low in MIG_PROTECTED_TABLES:
        return False
    return not any(low.startswith(p) for p in MIG_PROTECTED_PREFIXES)


def _mig_ddl_ok(ddl, table_name):
    """DDL が「同名テーブルの CREATE TABLE 1 文」か。"""
    if not ddl or ';' in ddl:
        return False
    m = re.match(r'\s*CREATE\s+TABLE\s+`([^`]+)`\s*\(', ddl, re.IGNORECASE)
    if not m or m.group(1) != table_name:
        return False
    # CREATE TABLE ... SELECT / LIKE で他表を読む形を拒む
    if re.search(r'\)\s*(AS\s+)?SELECT\b', ddl, re.IGNORECASE):
        return False
    if re.search(r'\bLIKE\s+`', ddl, re.IGNORECASE):
        return False
    return True


def _migration_importable(item):
    """取り込み対象のテーブル項目か（DDLが同名表のCREATE 1文で，所在DBがこのサイトにあり，
    名前が保護対象でない）。外部キーを外したあとの DDL で判定する。"""
    reg = item.get('registry') or {}
    tbl = reg.get('table_name')
    ddl, _ = _strip_foreign_keys(item.get('ddl'))
    return bool(not item.get('error') and ddl
                and tbl and reg.get('database_name') in DB_CHOICES
                and _mig_table_ok(tbl)
                and _mig_ddl_ok(ddl, tbl))


def _migration_current_state():
    """移行先の現状：登録簿・バックアップ表・定義の件数。全消去の確認と実行に使う。"""
    conn = mysql.connector.connect(**DatabaseConfig.default())
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT table_name, database_name FROM official_data_archive_tables")
        registry = cur.fetchall()
        cur.execute("""SELECT DISTINCT database_name, backup_table
                       FROM official_data_archive_updates
                       WHERE backup_table IS NOT NULL AND backup_table <> ''""")
        backups = cur.fetchall()
        counts = {}
        for key, tbl in (('indicator_views', 'official_data_archive_indicator_views'),
                         ('composites', 'official_data_archive_composites'),
                         ('updates', 'official_data_archive_updates')):
            cur.execute("SELECT COUNT(*) AS n FROM %s" % tbl)
            counts[key] = cur.fetchone()['n']
        cur.close()
    finally:
        conn.close()
    return registry, backups, counts


def _migration_wipe():
    """移行先の公式データ集をすべて消す。消した件数を返す。"""
    registry, backups, counts = _migration_current_state()
    dropped, skipped = [], []
    targets = [(r['database_name'], r['table_name'], False) for r in registry]
    targets += [(b['database_name'], b['backup_table'], True) for b in backups]
    for db, tbl, is_backup in targets:
        ok = (db in DB_CHOICES and _mig_table_ok(tbl)
              and (not is_backup or MIG_BACKUP_RE.match(tbl or '')))
        if not ok:
            skipped.append('%s.%s' % (db, tbl))
            continue
        conn = mysql.connector.connect(**DB_CHOICES[db]())
        try:
            cur = conn.cursor()
            cur.execute("SET FOREIGN_KEY_CHECKS=0")
            cur.execute("DROP TABLE IF EXISTS `%s`" % tbl)
            cur.execute("SET FOREIGN_KEY_CHECKS=1")
            conn.commit()
            cur.close()
            dropped.append('%s.%s' % (db, tbl))
        finally:
            conn.close()
    conn = mysql.connector.connect(**DatabaseConfig.default())
    try:
        cur = conn.cursor()
        for tbl in ('official_data_archive_composite_components',
                    'official_data_archive_composites',
                    'official_data_archive_indicator_views',
                    'official_data_archive_updates',
                    'official_data_archive_tables'):
            cur.execute("DELETE FROM %s" % tbl)
        conn.commit()
        cur.close()
    finally:
        conn.close()
    return {'dropped': len(dropped), 'skipped': skipped,
            'registry': len(registry), 'indicator_views': counts['indicator_views'],
            'composites': counts['composites'], 'updates': counts['updates']}


def _migration_import_table(item, reset):
    """1テーブル分（または行の一部）を書き込む。
    reset=True なら DROP → 同梱DDL（外部キーを外したもの）で CREATE してから INSERT、
    False なら既存テーブルへ INSERT のみ（行を分割して送る2回目以降）。
    (挿入行数, 外した外部キーの行のlist) を返す。"""
    reg = item['registry']
    tbl = reg['table_name']
    ddl, dropped_fk = _strip_foreign_keys(item['ddl'])
    conn = mysql.connector.connect(**DB_CHOICES[reg['database_name']]())
    cur = conn.cursor()
    try:
        cur.execute("SET FOREIGN_KEY_CHECKS=0")
        if reset:
            cur.execute("DROP TABLE IF EXISTS `%s`" % tbl)
            cur.execute(ddl)
        cols = item.get('columns') or []
        rows = item.get('rows') or []
        if cols and rows:
            sql = "INSERT INTO `%s` (%s) VALUES (%s)" % (
                tbl, ', '.join('`%s`' % c for c in cols),
                ', '.join(['%s'] * len(cols)))
            for i in range(0, len(rows), MIGRATION_INSERT_CHUNK):
                cur.executemany(sql, [tuple(r) for r in
                                      rows[i:i + MIGRATION_INSERT_CHUNK]])
        conn.commit()
        return len(rows), (dropped_fk if reset else [])
    finally:
        try:
            cur.execute("SET FOREIGN_KEY_CHECKS=1")
        except Exception:
            pass
        cur.close(); conn.close()


def _migration_import_definitions(pkg, user_id, now):
    """登録簿・単一指標・複合指標を全削除して再作成する。件数を返す。"""
    conn = mysql.connector.connect(**DatabaseConfig.default())
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, name FROM user_groups")
        gid = {name: i for i, name in cur.fetchall()}

        # ── 登録簿 ──
        cur.execute("DELETE FROM official_data_archive_tables")
        n_reg = 0
        seen = set()
        for item in pkg.get('tables') or []:
            reg = item.get('registry') or {}
            key = (reg.get('database_name'), reg.get('table_name'))
            if not all(key) or key in seen:
                continue
            if key[0] not in DB_CHOICES or not _mig_table_ok(key[1]):
                continue
            seen.add(key)
            cur.execute("""
                INSERT INTO official_data_archive_tables
                    (table_name, database_name, display_name, note,
                     manager_group_id, created_by, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (reg['table_name'], reg['database_name'],
                  reg.get('display_name'), reg.get('note'),
                  gid.get(reg.get('manager_group_name')), user_id, now))
            n_reg += 1

        # ── 単一指標・複合指標 ──
        cur.execute("DELETE FROM official_data_archive_composite_components")
        cur.execute("DELETE FROM official_data_archive_composites")
        cur.execute("DELETE FROM official_data_archive_indicator_views")

        iv_ids = set()
        iv_by_name = {}
        for v in pkg.get('indicator_views') or []:
            cur.execute("""
                INSERT INTO official_data_archive_indicator_views
                    (id, name, query, sort_order, chart_type,
                     manager_group_id, created_by, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (v['id'], v['name'], v['query'], v.get('sort_order') or 100,
                  v.get('chart_type') or 'bar',
                  gid.get(v.get('manager_group_name')), user_id, now))
            iv_ids.add(v['id'])
            iv_by_name.setdefault(v['name'], v['id'])

        n_comp = 0
        for c in pkg.get('composites') or []:
            cur.execute("""
                INSERT INTO official_data_archive_composites
                    (id, name, sort_order, manager_group_id,
                     created_by, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (c['id'], c['name'], c.get('sort_order') or 100,
                  gid.get(c.get('manager_group_name')), user_id, now))
            n_comp += 1
            for m in c.get('components') or []:
                iv = m.get('indicator_view_id')
                if iv not in iv_ids:
                    iv = iv_by_name.get(m.get('indicator_name'))
                if iv is None:
                    continue
                cur.execute("""
                    INSERT INTO official_data_archive_composite_components
                        (composite_id, indicator_view_id, color, seq)
                    VALUES (%s, %s, %s, %s)
                """, (c['id'], iv, m.get('color') or '#2e6da4',
                      m.get('seq') or 1))
        conn.commit()
        return {'registry': n_reg, 'indicator_views': len(iv_ids),
                'composites': n_comp}
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close(); conn.close()


# 取り込みはブラウザ側で JSON を分解し、テーブルごと（大きい表は行を分割して）
# 順に送る。1リクエストを短く保ってタイムアウトを避けるため。
# 各テーブルは最初の送信（reset=true）で作り直すので、途中で止まっても
# 最初からやり直せば同じ結果になる（冪等）。

@official_data_archive_bp.route('/api/migration/import/plan', methods=['GET'])
@login_required
def api_migration_import_plan():
    """全消去の対象（移行先の現状）を返す。admin のみ。画面の確認表示に使う。"""
    if not _mig_is_admin(session.get('user_id')):
        return jsonify({'success': False,
                        'error': '取り込みは管理者（admin）のみ実行できます'}), 403
    try:
        registry, backups, counts = _migration_current_state()
        return jsonify({'success': True,
                        'registry': [{'table_name': r['table_name'],
                                      'database_name': r['database_name']}
                                     for r in registry],
                        'backups': len(backups), 'counts': counts})
    except Exception as e:
        logging.error("api_migration_import_plan: %s", e)
        return jsonify({'success': False, 'error': '現状の取得に失敗しました'}), 500


@official_data_archive_bp.route('/api/migration/import/wipe', methods=['POST'])
@login_required
def api_migration_import_wipe():
    """移行先の公式データ集をすべて消す（取り込みの最初の段）。admin のみ。
    リクエスト: { confirm: "wipe" }"""
    if not _mig_is_admin(session.get('user_id')):
        return jsonify({'success': False,
                        'error': '取り込みは管理者（admin）のみ実行できます'}), 403
    data = request.get_json(silent=True) or {}
    if data.get('confirm') != 'wipe':
        return jsonify({'success': False, 'error': '確認の指定がありません'}), 400
    try:
        result = _migration_wipe()
        return jsonify({'success': True, 'wiped': result})
    except Exception as e:
        logging.error("api_migration_import_wipe: %s", e)
        return jsonify({'success': False,
                        'error': f'全消去に失敗しました：{e}'}), 500


@official_data_archive_bp.route('/api/migration/import/table', methods=['POST'])
@login_required
def api_migration_import_table():
    """1テーブル分を取り込む。admin のみ。
    リクエスト: { registry, ddl, columns, rows, reset }"""
    user_id = session.get('user_id')
    if not _mig_is_admin(user_id):
        return jsonify({'success': False,
                        'error': '取り込みは管理者（admin）のみ実行できます'}), 403
    item = request.get_json(silent=True) or {}
    if not _migration_importable(item):
        return jsonify({'success': False,
                        'error': '取り込み対象外のテーブルです'}), 400
    try:
        n, dropped_fk = _migration_import_table(item, bool(item.get('reset')))
        return jsonify({'success': True, 'rows': n, 'dropped_foreign_keys': dropped_fk})
    except Exception as e:
        logging.error("api_migration_import_table %s: %s",
                      (item.get('registry') or {}).get('table_name'), e)
        return jsonify({'success': False, 'error': str(e)}), 500


@official_data_archive_bp.route('/api/migration/import/definitions', methods=['POST'])
@login_required
def api_migration_import_definitions():
    """登録簿・単一指標・複合指標を全削除して再作成する。admin のみ。
    リクエスト: { tables: [{registry}], indicator_views, composites }"""
    user_id = session.get('user_id')
    if not _mig_is_admin(user_id):
        return jsonify({'success': False,
                        'error': '取り込みは管理者（admin）のみ実行できます'}), 403
    pkg = request.get_json(silent=True) or {}
    try:
        counts = _migration_import_definitions(pkg, user_id, get_jst_now())
        return jsonify({'success': True, 'counts': counts})
    except Exception as e:
        logging.error("api_migration_import_definitions: %s", e)
        return jsonify({'success': False,
                        'error': f'登録簿・指標の再作成に失敗しました：{e}'}), 500


# ────────────────────────────────────────────
# データ更新（公式テーブルのExcel更新ワークフロー）
#   担当者（アイテムの管理グループ所属者）がExcelを提供し、
#   全体管理者がDBバックアップを取りながら正式テーブルへ転記する。
#   data_post の思想を公式テーブル固定・登録簿直結で単純化したもの。
# ────────────────────────────────────────────

UPDATE_STATUS_LABELS = {
    'pending':  '提出済（転記待ち）',
    'applied':  '転記済',
    'rejected': '却下',
}
UPLOAD_MAX_BYTES = 10 * 1024 * 1024   # 10MB
UPLOAD_MAX_ROWS  = 50000              # 転記の最大行数（暴走防止）


def _upload_dir():
    """提供ファイルの保存先（プラットフォーム独立：Config経由）。"""
    d = os.path.join(Config.UPLOAD_BASE_DIR, 'fujinp', 'static',
                     'official_data_archive_uploads')
    os.makedirs(d, exist_ok=True)
    return d


def _get_table_item(item_id):
    """公式テーブル登録簿の1件を返す。なければ None。"""
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT id, table_name, database_name, display_name,
                   manager_group_id
            FROM official_data_archive_tables WHERE id = %s
        """, (item_id,))
        return cursor.fetchone()
    except Exception as e:
        logging.error("official_data_archive _get_table_item: %s", e)
        return None
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


def _table_columns(table_name, database_name):
    """対象テーブルの列名リスト。失敗時は None。"""
    try:
        conn = mysql.connector.connect(**DB_CHOICES[database_name]())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SHOW COLUMNS FROM `%s`" % table_name)
        return [c['Field'] for c in cursor.fetchall()]
    except Exception as e:
        logging.error("official_data_archive _table_columns %s: %s",
                      table_name, e)
        return None
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


def _read_xlsx(path):
    """
    提供されたxlsxを読み、(header(list), rows(list of list)) を返す。
    先頭シートの1行目をヘッダーとみなす。
    """
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    header = []
    rows = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            header = [str(c).strip() if c is not None else '' for c in row]
            # 末尾の空ヘッダーを落とす
            while header and not header[-1]:
                header.pop()
            continue
        vals = list(row)[:len(header)]
        # 完全に空の行はスキップ
        if all(v is None or (isinstance(v, str) and not v.strip())
               for v in vals):
            continue
        # 不足分は None で埋める
        vals += [None] * (len(header) - len(vals))
        rows.append(vals)
        if len(rows) > UPLOAD_MAX_ROWS:
            raise ValueError(f'行数が上限（{UPLOAD_MAX_ROWS}行）を'
                             '超えています')
    wb.close()
    return header, rows


def _check_header(header, columns):
    """ヘッダーとテーブル列の照合。問題なければ None、あればエラー文字列。"""
    if not header:
        return 'Excelの1行目（ヘッダー行）が読み取れません'
    hset, cset = set(header), set(columns)
    if len(hset) != len(header):
        return 'Excelのヘッダーに重複する列名があります'
    missing = cset - hset
    extra   = hset - cset
    if missing or extra:
        msgs = []
        if missing:
            msgs.append('不足列：' + '、'.join(sorted(missing)))
        if extra:
            msgs.append('余分な列：' + '、'.join(sorted(extra)))
        return ('列構成がテーブルと一致しません（' + '　'.join(msgs) + '）。'
                'まず公式テーブルタブから現在のXLSXをダウンロードし、'
                'それを編集してください')
    return None


@official_data_archive_bp.route('/api/updates', methods=['GET'])
@login_required
def api_updates():
    """
    データ更新の履歴一覧（提供日時の降順）。
    あわせて、呼び出しユーザが提供できる公式テーブルアイテム一覧
    （uploadable_items）と、転記・却下の可否（can_apply）を返す。
    """
    user_id = session.get('user_id')
    if not can_view(user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT u.id, u.table_item_id, u.table_name, u.database_name,
                   u.original_filename, u.stored_filename,
                   u.note, u.status, u.source_ref,
                   u.uploaded_by, u.uploaded_at,
                   u.applied_by, u.applied_at,
                   u.backup_table, u.reject_reason,
                   t.display_name, t.manager_group_id,
                   COALESCE(up.full_name, u.uploaded_by_name)
                       AS uploaded_by_name,
                   ap.full_name AS applied_by_name
            FROM official_data_archive_updates u
            LEFT JOIN official_data_archive_tables t
                   ON u.table_item_id = t.id
            LEFT JOIN users up ON u.uploaded_by = up.id
            LEFT JOIN users ap ON u.applied_by  = ap.id
            ORDER BY u.uploaded_at DESC
            LIMIT 500
        """)
        updates = cursor.fetchall()

        cursor.execute("""
            SELECT id, table_name, database_name, display_name,
                   manager_group_id
            FROM official_data_archive_tables
            ORDER BY COALESCE(display_name, table_name)
        """)
        items = cursor.fetchall()
        cursor.close(); conn.close()

        is_global = is_global_manager(user_id)
        my_ids = {g['id'] for g in user_manage_groups(user_id)}

        def manageable(mgid):
            return bool(is_global or (mgid in my_ids if mgid else False))

        for u in updates:
            u['uploaded_at'] = fmt_minutes(u['uploaded_at'])
            u['applied_at']  = fmt_minutes(u['applied_at'])
            u['status_label'] = UPDATE_STATUS_LABELS.get(
                u['status'], u['status'])
            # 提供ファイルのダウンロード可否（全体管理者・提供者・担当者）。
            # 他アプリから移行した記録（stored_filename が 'migrated:'）は
            # 実ファイルが無いのでダウンロード対象外。
            u['can_file'] = bool(
                (is_global or u['uploaded_by'] == user_id
                 or manageable(u.get('manager_group_id')))
                and u.get('stored_filename')
                and not str(u['stored_filename']).startswith('migrated:'))

        uploadable = [
            {'id': it['id'],
             'label': (it['display_name'] or it['table_name'])
                      + '（' + it['database_name'] + ' / '
                      + it['table_name'] + '）'}
            for it in items if manageable(it.get('manager_group_id'))
        ]

        return jsonify({
            'success': True,
            'updates': updates,
            'uploadable_items': uploadable,
            'can_apply': is_global,
        })
    except Exception as e:
        logging.error("api_updates error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


def fmt_minutes(d):
    """datetime → 'YYYY-MM-DD HH:MM' 文字列。None は ''。"""
    if d is None:
        return ''
    if isinstance(d, (datetime.datetime, datetime.date)):
        return d.strftime('%Y-%m-%d %H:%M')
    return str(d)


@official_data_archive_bp.route('/api/updates/upload', methods=['POST'])
@login_required
def api_update_upload():
    """
    更新データ（Excel）の提供。multipart/form-data：
        item_id    : 公式テーブル登録簿のid
        note       : メモ（任意）
        excel_file : .xlsx ファイル
    対象アイテムの管理グループ所属者（または全体管理者）のみ。
    アップロード時に列構成をテーブルと照合し、不一致は受け付けない。
    """
    user_id = session.get('user_id')
    try:
        item_id = request.form.get('item_id', type=int)
        note    = (request.form.get('note') or '').strip() or None
        f       = request.files.get('excel_file')

        item = _get_table_item(item_id)
        if not item:
            return jsonify({'success': False,
                            'error': '対象の公式テーブルが見つかりません'}), 404
        if not _can_manage_item(user_id, item.get('manager_group_id')):
            return jsonify({'success': False,
                            'error': 'このテーブルの更新データを提供する'
                                     '権限がありません'}), 403
        if not f or not f.filename:
            return jsonify({'success': False,
                            'error': 'Excelファイルを選択してください'}), 400
        if not f.filename.lower().endswith('.xlsx'):
            return jsonify({'success': False,
                            'error': '.xlsx ファイルのみ受け付けます'}), 400
        if request.content_length and request.content_length > UPLOAD_MAX_BYTES:
            return jsonify({'success': False,
                            'error': 'ファイルが大きすぎます（10MBまで）'}), 400

        columns = _table_columns(item['table_name'], item['database_name'])
        if columns is None:
            return jsonify({'success': False,
                            'error': '対象テーブルの列構成を取得できません'}), 500

        now = get_jst_now()
        stored = 'upd_{0}_u{1}_i{2}.xlsx'.format(
            now.strftime('%Y%m%d%H%M%S'), user_id, item_id)
        path = os.path.join(_upload_dir(), stored)
        f.save(path)

        # ── 列構成の照合（不一致なら保存を取り消して受付拒否） ──
        try:
            header, rows = _read_xlsx(path)
            err = _check_header(header, columns)
            if err:
                os.remove(path)
                return jsonify({'success': False, 'error': err}), 400
        except Exception as xe:
            try:
                os.remove(path)
            except OSError:
                pass
            return jsonify({'success': False,
                            'error': f'Excelの読み取りに失敗しました：{xe}'}), 400

        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO official_data_archive_updates
                (table_item_id, table_name, database_name,
                 original_filename, stored_filename, note,
                 uploaded_by, uploaded_at, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending')
        """, (item_id, item['table_name'], item['database_name'],
              f.filename[:255], stored, note, user_id, now))
        conn.commit()
        return jsonify({'success': True, 'id': cursor.lastrowid,
                        'rows': len(rows)})
    except Exception as e:
        logging.error("api_update_upload error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


def _get_update(update_id):
    """更新レコード1件。なければ None。"""
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT u.*, t.manager_group_id
            FROM official_data_archive_updates u
            LEFT JOIN official_data_archive_tables t
                   ON u.table_item_id = t.id
            WHERE u.id = %s
        """, (update_id,))
        return cursor.fetchone()
    except Exception as e:
        logging.error("official_data_archive _get_update: %s", e)
        return None
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@official_data_archive_bp.route('/api/updates/file', methods=['GET'])
@login_required
def api_update_file():
    """提供ファイル（xlsx）のダウンロード。パラメータ: id"""
    from flask import send_file
    user_id = session.get('user_id')
    u = _get_update(request.args.get('id', type=int))
    if not u:
        return jsonify({'success': False,
                        'error': '更新レコードが見つかりません'}), 404
    if not (is_global_manager(user_id) or u['uploaded_by'] == user_id
            or _can_manage_item(user_id, u.get('manager_group_id'))):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    path = os.path.join(_upload_dir(), os.path.basename(u['stored_filename']))
    if not os.path.exists(path):
        return jsonify({'success': False,
                        'error': '提供ファイルが見つかりません'}), 404
    return send_file(path, as_attachment=True,
                     download_name=u['original_filename'] or u['stored_filename'])


@official_data_archive_bp.route('/api/updates/apply', methods=['POST'])
@login_required
def api_update_apply():
    """
    転記（全体管理者のみ）。リクエスト: { "id": 更新レコードのid }
    手順：
      1. 正式テーブルのDBバックアップを作成
         （CREATE TABLE `<name>_bak_<日時>` LIKE → INSERT SELECT）
      2. 正式テーブルを全件削除し、Excelの内容を挿入（同一トランザクション。
         失敗時はロールバック＝正式テーブルは転記前の状態のまま）
      3. レコードを applied に更新（転記者・日時・バックアップ名を記録）
    """
    user_id = session.get('user_id')
    if not is_global_manager(user_id):
        return jsonify({'success': False,
                        'error': '転記は全体管理者のみ実行できます'}), 403
    data = request.json or {}
    u = _get_update(data.get('id'))
    if not u:
        return jsonify({'success': False,
                        'error': '更新レコードが見つかりません'}), 404
    if u['status'] != 'pending':
        return jsonify({'success': False,
                        'error': 'このレコードはすでに処理済みです'
                                 f"（{UPDATE_STATUS_LABELS.get(u['status'])}）"}), 400
    if u['database_name'] not in DB_CHOICES:
        return jsonify({'success': False,
                        'error': '対象データベースの指定が不正です'}), 400

    table = u['table_name']
    # 転記はテーブルを全件消して入れ替えるので，名前の形と保護対象を必ず確かめる（2026-10-26 最終点検）
    if not _mig_table_ok(table):
        return jsonify({'success': False,
                        'error': 'このテーブルには転記できません（名前が不正か，保護対象のテーブルです）'}), 400
    path = os.path.join(_upload_dir(), os.path.basename(u['stored_filename']))
    if not os.path.exists(path):
        return jsonify({'success': False,
                        'error': '提供ファイルが見つかりません'}), 404

    try:
        # ── Excel再読込と列構成の最終確認 ──
        header, rows = _read_xlsx(path)
        columns = _table_columns(table, u['database_name'])
        if columns is None:
            return jsonify({'success': False,
                            'error': '対象テーブルの列構成を取得できません'}), 500
        err = _check_header(header, columns)
        if err:
            return jsonify({'success': False, 'error': err}), 400

        now = get_jst_now()
        backup = '{0}_bak_{1}'.format(table[:43], now.strftime('%Y%m%d%H%M%S'))

        db_conn = mysql.connector.connect(**DB_CHOICES[u['database_name']]())
        db_cur  = db_conn.cursor()

        # ── 1. バックアップ（DDLは即時コミットされる） ──
        db_cur.execute("CREATE TABLE `%s` LIKE `%s`" % (backup, table))
        db_cur.execute("INSERT INTO `%s` SELECT * FROM `%s`" % (backup, table))
        db_conn.commit()

        # ── 2. 全件削除→挿入（単一トランザクション） ──
        try:
            db_cur.execute("SET FOREIGN_KEY_CHECKS=0")
            db_cur.execute("DELETE FROM `%s`" % table)
            col_list = ', '.join('`%s`' % c for c in header)
            ph       = ', '.join(['%s'] * len(header))
            ins_sql  = ("INSERT INTO `%s` (%s) VALUES (%s)"
                        % (table, col_list, ph))
            cleaned = [
                tuple((v.strip() if isinstance(v, str) else v)
                      if not (isinstance(v, str) and not v.strip()) else None
                      for v in row)
                for row in rows
            ]
            if cleaned:
                db_cur.executemany(ins_sql, cleaned)
            db_cur.execute("SET FOREIGN_KEY_CHECKS=1")
            db_conn.commit()
        except Exception:
            db_conn.rollback()
            try:
                db_cur.execute("SET FOREIGN_KEY_CHECKS=1")
            except Exception:
                pass
            raise
        finally:
            db_cur.close(); db_conn.close()

        # ── 3. レコード更新 ──
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE official_data_archive_updates
            SET status='applied', applied_by=%s, applied_at=%s,
                backup_table=%s
            WHERE id=%s
        """, (user_id, now, backup, u['id']))
        conn.commit()
        return jsonify({'success': True, 'rows': len(rows),
                        'backup_table': backup})
    except Exception as e:
        logging.error("api_update_apply error (id=%s): %s",
                      data.get('id'), e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False,
                        'error': f'転記に失敗しました：{e}'}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@official_data_archive_bp.route('/api/updates/reject', methods=['POST'])
@login_required
def api_update_reject():
    """
    却下（全体管理者のみ）。リクエスト: { "id": ..., "reason": "（任意）" }
    提供ファイルは記録として残す。
    """
    user_id = session.get('user_id')
    if not is_global_manager(user_id):
        return jsonify({'success': False,
                        'error': '却下は全体管理者のみ実行できます'}), 403
    try:
        data   = request.json or {}
        u      = _get_update(data.get('id'))
        reason = (data.get('reason') or '').strip()[:255] or None
        if not u:
            return jsonify({'success': False,
                            'error': '更新レコードが見つかりません'}), 404
        if u['status'] != 'pending':
            return jsonify({'success': False,
                            'error': 'このレコードはすでに処理済みです'}), 400
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE official_data_archive_updates
            SET status='rejected', applied_by=%s, applied_at=%s,
                reject_reason=%s
            WHERE id=%s
        """, (user_id, get_jst_now(), reason, u['id']))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("api_update_reject error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ────────────────────────────────────────────
# 一覧ドキュメント（Web文書）
#   所定のURLにアクセスすると、サーバがその時点の最新状態からHTML文書を
#   組み立てて返す。いずれも閲覧権限（regular以上）で読める読み取り専用の文書。
#     /official_data_archive/report/indicators … 指標の動き（HTML＋SVG＋CSS）
#     /official_data_archive/report/tables     … テーブル一覧（名前と列構成・DDL）
#   ?dl=1 を付けるとファイルとして保存させる（Content-Disposition: attachment）。
#   グラフのSVGは画面の「⬇ SVG」（index.html の svgSingle / svgMulti）と
#   同じ規則で、ここで Python により生成する。
# ────────────────────────────────────────────

import html as _html_mod

_NUM_HEAD_RE = re.compile(r'^\s*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?')


def _h(s):
    """HTML/SVG 用のエスケープ（bytes は UTF-8 で読む）。"""
    if s is None:
        return ''
    if isinstance(s, (bytes, bytearray)):
        s = s.decode('utf-8', errors='replace')
    return _html_mod.escape(str(s))


def _parse_float(v):
    """JavaScript の parseFloat 相当（先頭の数値部分を読む。読めなければ None）。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return f if math.isfinite(f) else None
    m = _NUM_HEAD_RE.match(str(v))
    if not m:
        return None
    try:
        f = float(m.group(0))
    except ValueError:
        return None
    return f if math.isfinite(f) else None


def _label(v):
    """横軸ラベルの文字列化（画面の String(r[x] ?? '') に合わせる）。"""
    if v is None:
        return ''
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(safe_val(v))


def _extract_series(columns, rows):
    """1列目を横軸、2列目以降で最初に数値が取れる列を縦軸にする（画面と同じ規則）。"""
    if not columns or len(columns) < 2 or not rows:
        return None
    x_col = columns[0]
    for c in columns[1:]:
        vals = [_parse_float(r.get(c)) for r in rows]
        if any(v is not None for v in vals):
            return {'xLabel': x_col, 'yLabel': c,
                    'labels': [_label(r.get(x_col)) for r in rows],
                    'values': vals}
    return None


def _nice_ticks(vmin, vmax, n):
    span = (vmax - vmin) or 1
    step0 = span / n
    mag = 10 ** math.floor(math.log10(step0))
    step = 10 * mag
    for m in (1, 2, 5, 10):
        if step0 <= m * mag:
            step = m * mag
            break
    ticks = []
    for i in range(math.ceil(vmin / step), math.floor(vmax / step) + 1):
        ticks.append(float('%.12g' % (i * step)))
    return ticks


def _fmt_num(x):
    """目盛の数値表示（整数は小数点なし）。"""
    if float(x).is_integer():
        return str(int(x))
    return ('%.12g' % x)


def _data_line(s, name=None):
    """グラフの元データを [年度：値，…] の辞書式で書いた1行（読み手のAI向け）。
    値が無い年度は null と書く。name を渡すと系列名を前に置く（複合指標用）。"""
    items = []
    for l, v in zip(s['labels'], s['values']):
        items.append('%s：%s' % (l, 'null' if v is None else _fmt_num(v)))
    head = ('%s（%s）' % (name, s['yLabel'])
            if name and name != s['yLabel'] else (name or s['yLabel']))
    return ('<p class="data"><span class="data-k">%s</span> [%s]</p>'
            % (_h(head), _h('，'.join(items))))


def _c(x):
    """座標の書式（小数2桁）。"""
    return ('%.2f' % x).rstrip('0').rstrip('.')


def _natural_key(s):
    """年度などを数値順に並べるキー（画面の localeCompare numeric 相当）。"""
    parts = re.split(r'(\d+)', str(s))
    return [(0, int(p)) if p.isdigit() else (1, p) for p in parts if p != '']


def _x_labels_svg(o, labels, X, base_y):
    rotate = len(labels) > 12
    for i, lb in enumerate(labels):
        x = X(i)
        if rotate:
            o.append('<text x="%s" y="%s" text-anchor="end" font-size="10" '
                     'fill="#374151" transform="rotate(-45 %s %s)">%s</text>'
                     % (_c(x), _c(base_y), _c(x), _c(base_y), _h(lb)))
        else:
            o.append('<text x="%s" y="%s" text-anchor="middle" font-size="11" '
                     'fill="#374151">%s</text>' % (_c(x), _c(base_y), _h(lb)))


def _svg_single(title, s, mode):
    """単一指標のSVG（棒または折れ線。縦軸は0〜最大値の約10%上）。"""
    nums = [v for v in s['values'] if v is not None]
    if not nums:
        return None
    vmax0 = max(nums)
    if not math.isfinite(vmax0) or vmax0 <= 0:
        vmax0 = 1
    ymin, ymax = 0, vmax0 * 1.1
    W, H, mL, mR, mT, mB = 720, 340, 76, 24, 40, 64
    pw, ph = W - mL - mR, H - mT - mB
    n = len(s['labels'])
    X = lambda i: mL + pw * (i + 0.5) / n
    Y = lambda v: mT + ph * (1 - (v - ymin) / (ymax - ymin))
    o = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
         'viewBox="0 0 %d %d" font-family="sans-serif" role="img" aria-label="%s">'
         % (W, H, W, H, _h(title)),
         '<rect width="%d" height="%d" fill="#ffffff"/>' % (W, H),
         '<text x="%s" y="22" text-anchor="middle" font-size="14" font-weight="bold" '
         'fill="#1a3a5c">%s</text>' % (_c(W / 2), _h(title))]
    for tk in _nice_ticks(ymin, ymax, 5):
        y = Y(tk)
        o.append('<line x1="%d" y1="%s" x2="%d" y2="%s" stroke="#e5e7eb" stroke-width="1"/>'
                 % (mL, _c(y), W - mR, _c(y)))
        o.append('<text x="%d" y="%s" text-anchor="end" font-size="11" fill="#6b7280">%s</text>'
                 % (mL - 8, _c(y + 3.5), _fmt_num(tk)))
    o.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#9ca3af" stroke-width="1"/>'
             % (mL, mT, mL, mT + ph))
    o.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#9ca3af" stroke-width="1"/>'
             % (mL, mT + ph, W - mR, mT + ph))
    if mode == 'bar':
        bw = pw / n * 0.6
        for i, v in enumerate(s['values']):
            if v is None:
                continue
            y = Y(v)
            o.append('<rect x="%s" y="%s" width="%s" height="%s" fill="#2e6da4" '
                     'fill-opacity="0.75"/>' % (_c(X(i) - bw / 2), _c(min(y, mT + ph)),
                                                _c(bw), _c(abs(mT + ph - y))))
    else:
        pts = ['%s,%s' % (_c(X(i)), _c(Y(v)))
               for i, v in enumerate(s['values']) if v is not None]
        if len(pts) > 1:
            o.append('<polyline points="%s" fill="none" stroke="#2e6da4" stroke-width="2"/>'
                     % ' '.join(pts))
        for i, v in enumerate(s['values']):
            if v is not None:
                o.append('<circle cx="%s" cy="%s" r="3" fill="#2e6da4"/>'
                         % (_c(X(i)), _c(Y(v))))
    _x_labels_svg(o, s['labels'], X, mT + ph + 16)
    o.append('<text x="%s" y="%d" text-anchor="middle" font-size="11" fill="#6b7280">%s</text>'
             % (_c(mL + pw / 2), H - 10, _h(s['xLabel'])))
    o.append('<text x="16" y="%s" text-anchor="middle" font-size="11" fill="#6b7280" '
             'transform="rotate(-90 16 %s)">%s</text>'
             % (_c(mT + ph / 2), _c(mT + ph / 2), _h(s['yLabel'])))
    o.append('</svg>')
    return '\n'.join(o)


def _svg_multi(title, series_list):
    """複合指標のSVG（折れ線の重ね合わせ・凡例付き。縦軸は0〜全系列の最大値の約10%上）。"""
    if not series_list:
        return None
    labels = sorted({l for s in series_list for l in s['labels']}, key=_natural_key)
    vmax = None
    mapped = []
    for s in series_list:
        m = {}
        for l, v in zip(s['labels'], s['values']):
            m[l] = v
        data = [m.get(l) for l in labels]
        for v in data:
            if v is not None and (vmax is None or v > vmax):
                vmax = v
        mapped.append({'name': s['name'], 'color': s['color'], 'data': data})
    if vmax is None or not math.isfinite(vmax) or vmax <= 0:
        vmax = 1
    ymin, ymax = 0, vmax * 1.1
    legH = 18 * len(mapped)
    W, mL, mR, mT, mB = 720, 76, 24, 44 + legH, 64
    ph = 240
    H = mT + ph + mB
    pw = W - mL - mR
    n = len(labels)
    X = lambda i: mL + pw * (i + 0.5) / n
    Y = lambda v: mT + ph * (1 - (v - ymin) / (ymax - ymin))
    o = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
         'viewBox="0 0 %d %d" font-family="sans-serif" role="img" aria-label="%s">'
         % (W, H, W, H, _h(title)),
         '<rect width="%d" height="%d" fill="#ffffff"/>' % (W, H),
         '<text x="%s" y="22" text-anchor="middle" font-size="14" font-weight="bold" '
         'fill="#1a3a5c">%s</text>' % (_c(W / 2), _h(title))]
    for i, s in enumerate(mapped):
        ly = 38 + i * 18
        col = _h(s['color'])
        o.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s" stroke-width="2"/>'
                 % (mL, ly, mL + 24, ly, col))
        o.append('<circle cx="%d" cy="%d" r="3" fill="%s"/>' % (mL + 12, ly, col))
        o.append('<text x="%d" y="%s" font-size="11" fill="#374151">%s</text>'
                 % (mL + 30, _c(ly + 3.5), _h(s['name'])))
    for tk in _nice_ticks(ymin, ymax, 5):
        y = Y(tk)
        o.append('<line x1="%d" y1="%s" x2="%d" y2="%s" stroke="#e5e7eb" stroke-width="1"/>'
                 % (mL, _c(y), W - mR, _c(y)))
        o.append('<text x="%d" y="%s" text-anchor="end" font-size="11" fill="#6b7280">%s</text>'
                 % (mL - 8, _c(y + 3.5), _fmt_num(tk)))
    o.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#9ca3af" stroke-width="1"/>'
             % (mL, mT, mL, mT + ph))
    o.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#9ca3af" stroke-width="1"/>'
             % (mL, mT + ph, W - mR, mT + ph))
    for s in mapped:
        col = _h(s['color'])
        pts = ['%s,%s' % (_c(X(i)), _c(Y(v)))
               for i, v in enumerate(s['data']) if v is not None]
        if len(pts) > 1:
            o.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="2"/>'
                     % (' '.join(pts), col))
        for i, v in enumerate(s['data']):
            if v is not None:
                o.append('<circle cx="%s" cy="%s" r="3" fill="%s"/>'
                         % (_c(X(i)), _c(Y(v)), col))
    _x_labels_svg(o, labels, X, mT + ph + 16)
    o.append('<text x="%s" y="%d" text-anchor="middle" font-size="11" fill="#6b7280">%s</text>'
             % (_c(mL + pw / 2), H - 10, _h(series_list[0]['xLabel'])))
    o.append('</svg>')
    return '\n'.join(o)


_REPORT_BASE_CSS = """
*{box-sizing:border-box}
body{margin:0;font-family:"Helvetica Neue",Arial,"Hiragino Kaku Gothic ProN","Yu Gothic",sans-serif;
     color:#222;background:#f4f6f8;line-height:1.6}
header{background:#1a3a5c;color:#fff;padding:22px 28px}
header h1{margin:0;font-size:22px}
header p{margin:6px 0 0;font-size:12px;opacity:.85}
nav,section{background:#fff;border:1px solid #dde3e9;border-radius:8px;padding:14px 18px;margin-bottom:18px}
h2.part{font-size:18px;color:#1a3a5c;border-bottom:3px solid #2e6da4;padding-bottom:4px;margin:30px 0 14px}
section h3{margin:0 0 8px;font-size:15px;color:#1a3a5c}
.err{color:#991b1b;background:#fee2e2;border-radius:4px;padding:6px 10px;font-size:12px}
.note{color:#856404;font-size:11px;margin:4px 0 0}
@media print{body{background:#fff}header{background:#fff;color:#000;border-bottom:2px solid #000}
  nav{display:none}section{break-inside:avoid;page-break-inside:avoid;border:none;padding:0}}
"""

_REPORT_IV_CSS = """
main{max-width:820px;margin:0 auto;padding:20px 16px 48px}
nav h2{font-size:14px;margin:6px 0}
nav ol{margin:0 0 8px;padding-left:22px;font-size:13px;columns:2;column-gap:28px}
nav a{color:#2e6da4;text-decoration:none}
.fig svg{display:block;max-width:100%;height:auto}
.data{margin:6px 0 0;font-size:11px;line-height:1.5;color:#555;word-break:break-all;
      font-family:Consolas,Monaco,"Courier New",monospace}
.data-k{color:#1a3a5c;font-weight:600}
@media(max-width:600px){nav ol{columns:1}}
"""


def _report_page(title, heading, sub, css, body):
    return ('<!DOCTYPE html>\n<html lang="ja">\n<head>\n<meta charset="UTF-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
            '<title>%s - 公式データ集</title>\n<style>%s%s</style>\n</head>\n<body>\n'
            '<header><h1>%s</h1><p>%s</p></header>\n<main>\n%s\n</main>\n</body>\n</html>\n'
            % (_h(title), _REPORT_BASE_CSS, css, heading, sub, body))


def _report_site_name():
    return getattr(Config, 'SITE_NAME', None) or request.host


def _report_indicators_html():
    """指標の動き：全単一指標・全複合指標のグラフ（SVG）を見出しつきで並べたHTML。
    単一指標のクエリは1回ずつだけ実行し、複合指標はその結果を共有する。"""
    conn = mysql.connector.connect(**DatabaseConfig.default())
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT id, name, query, chart_type
            FROM official_data_archive_indicator_views
            ORDER BY sort_order, name
        """)
        views = cursor.fetchall()
        cursor.execute("""
            SELECT id, name FROM official_data_archive_composites
            ORDER BY sort_order, name
        """)
        composites = cursor.fetchall()
        cursor.execute("""
            SELECT cc.composite_id, cc.indicator_view_id, cc.color, cc.seq,
                   v.name AS indicator_name
            FROM official_data_archive_composite_components cc
            LEFT JOIN official_data_archive_indicator_views v
                   ON cc.indicator_view_id = v.id
            ORDER BY cc.composite_id, cc.seq
        """)
        comp_rows = cursor.fetchall()
        cursor.close()
    finally:
        conn.close()

    # 単一指標の実行（結果は複合指標でも使う）
    results = {}
    for v in views:
        r = {'name': v['name'], 'chart_type': v.get('chart_type') or 'bar'}
        query, err = _validate_select(v['query'])
        if err:
            r['error'] = err
        else:
            try:
                columns, rows, truncated = _run_indicator_query(
                    query, INDICATOR_MAX_ROWS)
                r['series'] = _extract_series(columns, rows)
                r['nrows'] = len(rows)
                r['truncated'] = truncated
            except Exception as qe:
                r['error'] = 'クエリ実行エラー：%s' % qe
        results[v['id']] = r

    toc1, toc2, sec1, sec2 = [], [], [], []
    for v in views:
        r = results[v['id']]
        anchor = 'iv-%d' % v['id']
        toc1.append('<li><a href="#%s">%s</a></li>' % (anchor, _h(v['name'])))
        if r.get('error'):
            body = '<p class="err">⚠️ %s</p>' % _h(r['error'])
        else:
            svg = (_svg_single(v['name'], r['series'],
                               'line' if r['chart_type'] == 'line' else 'bar')
                   if r.get('series') else None)
            body = ('<div class="fig">%s</div>' % svg if svg else
                    '<p class="err">⚠️ 数値列が見つからないためグラフ化できません</p>')
            if r.get('series'):
                body += _data_line(r['series'])
            if r.get('truncated'):
                body += ('<p class="note">結果が多いため先頭%d行のみで描いています</p>'
                         % r['nrows'])
        sec1.append('<section id="%s"><h3>%s</h3>%s</section>'
                    % (anchor, _h(v['name']), body))

    by_comp = {}
    for cr in comp_rows:
        by_comp.setdefault(cr['composite_id'], []).append(cr)
    for c in composites:
        anchor = 'cp-%d' % c['id']
        toc2.append('<li><a href="#%s">%s</a></li>' % (anchor, _h(c['name'])))
        series, errs = [], []
        for cr in by_comp.get(c['id'], []):
            nm = cr['indicator_name'] or '（削除された単一指標）'
            r = results.get(cr['indicator_view_id'])
            if not r:
                errs.append('%s：構成する単一指標が削除されています' % nm)
            elif r.get('error'):
                errs.append('%s：%s' % (nm, r['error']))
            elif not r.get('series'):
                errs.append('%s：数値列が見つかりません' % nm)
            else:
                s = r['series']
                series.append({'name': r['name'], 'color': cr['color'],
                               'labels': s['labels'], 'values': s['values'],
                               'xLabel': s['xLabel'], 'yLabel': s['yLabel']})
        body = ''
        if errs:
            body += '<p class="err">⚠️ %s</p>' % '<br>⚠️ '.join(_h(e) for e in errs)
        svg = _svg_multi(c['name'], series) if series else None
        body += ('<div class="fig">%s</div>' % svg if svg else
                 '<p class="err">表示できる系列がありません</p>')
        body += ''.join(_data_line(s, s['name']) for s in series)
        sec2.append('<section id="%s"><h3>%s</h3>%s</section>'
                    % (anchor, _h(c['name']), body))

    now = get_jst_now()
    sub = ('公式データ集　%s　生成日時 %s JST　単一指標 %d件／複合指標 %d件'
           % (_h(_report_site_name()), now.strftime('%Y-%m-%d %H:%M'),
              len(views), len(composites)))
    body = ('<nav><h2>単一指標</h2><ol>%s</ol><h2>複合指標</h2><ol>%s</ol></nav>\n'
            '<h2 class="part">単一指標</h2>\n%s\n'
            '<h2 class="part">複合指標</h2>\n%s'
            % (''.join(toc1), ''.join(toc2), '\n'.join(sec1), '\n'.join(sec2)))
    return _report_page('指標の動き', '📈 指標の動き', sub, _REPORT_IV_CSS, body)


_REPORT_TABLES_CSS = """
*{box-sizing:border-box}
body{margin:0;font-family:"Helvetica Neue",Arial,"Hiragino Kaku Gothic ProN","Yu Gothic",sans-serif;
     color:#222;background:#f4f6f8;line-height:1.6}
header{background:#1a3a5c;color:#fff;padding:22px 28px}
header h1{margin:0;font-size:22px}
header p{margin:6px 0 0;font-size:12px;opacity:.85}
main{max-width:1100px;margin:0 auto;padding:20px 16px 48px}
nav,section{background:#fff;border:1px solid #dde3e9;border-radius:8px;padding:14px 18px;margin-bottom:18px}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th,td{border-bottom:1px solid #e5e7eb;padding:5px 8px;text-align:left;vertical-align:top}
th{background:#1a3a5c;color:#fff;font-weight:600;white-space:nowrap}
tr:nth-child(even) td{background:#f9fafb}
nav table td a{color:#2e6da4;text-decoration:none}
.num{text-align:right;white-space:nowrap}
h2.part{font-size:18px;color:#1a3a5c;border-bottom:3px solid #2e6da4;padding-bottom:4px;margin:30px 0 14px}
section h3{margin:0;font-size:15px;color:#1a3a5c}
.meta{font-size:12px;color:#666;margin:2px 0 10px}
.mono{font-family:Consolas,Monaco,"Courier New",monospace}
details{margin-top:10px}
summary{cursor:pointer;font-size:12px;color:#2e6da4}
pre{background:#1e272e;color:#e8edf1;padding:10px 12px;border-radius:4px;font-size:11.5px;
    overflow:auto;white-space:pre}
.err{color:#991b1b;background:#fee2e2;border-radius:4px;padding:6px 10px;font-size:12px}
.wrap{overflow-x:auto}
@media print{body{background:#fff}header{background:#fff;color:#000;border-bottom:2px solid #000}
  section{break-inside:avoid;border:none;padding:0}details{display:block}}
"""


def _report_tables_html():
    """テーブル一覧：登録簿のテーブルごとに列構成（SHOW FULL COLUMNS）とDDLを並べたHTML。"""
    import html as _html
    h = _h
    regs = _registered_tables()
    now = get_jst_now()

    # DBごとに接続して列構成・DDL・行数を取る
    info = {}
    by_db = {}
    for r in regs:
        by_db.setdefault(r['database_name'], []).append(r)
    for db_key, items in by_db.items():
        if db_key not in DB_CHOICES:
            for r in items:
                info[r['id']] = {'error': '未対応のデータベース指定です'}
            continue
        try:
            conn = mysql.connector.connect(**DB_CHOICES[db_key]())
        except Exception as ce:
            for r in items:
                info[r['id']] = {'error': f'データベース接続エラー：{ce}'}
            continue
        try:
            cur = conn.cursor(dictionary=True)
            for r in items:
                tbl = r['table_name']
                try:
                    cur.execute("SHOW FULL COLUMNS FROM `%s`" % tbl)
                    cols = cur.fetchall()
                    cur.execute("SHOW CREATE TABLE `%s`" % tbl)
                    row = cur.fetchone() or {}
                    ddl = row.get('Create Table') or row.get('Create View')
                    cur.execute("SELECT COUNT(*) AS cnt FROM `%s`" % tbl)
                    cnt = (cur.fetchone() or {}).get('cnt')
                    info[r['id']] = {'cols': cols, 'ddl': ddl, 'count': cnt}
                except Exception as te:
                    info[r['id']] = {'error': f'取得エラー：{te}'}
            cur.close()
        finally:
            conn.close()

    # 並びは DB → テーブル名
    regs = sorted(regs, key=lambda r: (r['database_name'], r['table_name']))

    toc = []
    secs = []
    for i, r in enumerate(regs, 1):
        anchor = 't%d' % r['id']
        x = info.get(r['id'], {})
        cnt = x.get('count')
        toc.append(
            '<tr><td class="num">%d</td><td><a href="#%s" class="mono">%s</a></td>'
            '<td>%s</td><td class="mono">%s</td><td class="num">%s</td>'
            '<td class="num">%s</td></tr>' % (
                i, anchor, h(r['table_name']), h(r.get('display_name') or ''),
                h(r['database_name']),
                len(x['cols']) if x.get('cols') is not None else '?',
                cnt if cnt is not None else '?'))

        meta = ['所在DB：%s' % h(r['database_name'])]
        if r.get('display_name'):
            meta.append('表示名：%s' % h(r['display_name']))
        meta.append('管理グループ：%s' % h(r.get('manager_group_name')
                                          or '（全体管理者専管）'))
        if cnt is not None:
            meta.append('%s行' % cnt)
        body = ['<section id="%s"><h3 class="mono">%s</h3>' % (anchor, h(r['table_name'])),
                '<div class="meta">%s</div>' % '　'.join(meta)]
        if r.get('note'):
            body.append('<div class="meta">📝 %s</div>' % h(r['note']))
        if x.get('error'):
            body.append('<p class="err">⚠️ %s</p>' % h(x['error']))
        else:
            rows = []
            for k, c in enumerate(x['cols'], 1):
                dflt = c.get('Default')
                rows.append(
                    '<tr><td class="num">%d</td><td class="mono">%s</td>'
                    '<td class="mono">%s</td><td>%s</td><td>%s</td>'
                    '<td class="mono">%s</td><td class="mono">%s</td><td>%s</td></tr>' % (
                        k, h(c.get('Field')), h(c.get('Type')),
                        h(c.get('Null')), h(c.get('Key')),
                        'NULL' if dflt is None else h(dflt),
                        h(c.get('Extra')), h(c.get('Comment'))))
            body.append(
                '<div class="wrap"><table><thead><tr><th>#</th><th>列名</th><th>型</th>'
                '<th>NULL</th><th>キー</th><th>既定値</th><th>付加</th><th>コメント</th>'
                '</tr></thead><tbody>%s</tbody></table></div>' % ''.join(rows))
            if x.get('ddl'):
                body.append('<details><summary>DDL（SHOW CREATE TABLE）</summary>'
                            '<pre>%s;</pre></details>' % h(x['ddl']))
        body.append('</section>')
        secs.append('\n'.join(body))

    site = _report_site_name()
    return ('<!DOCTYPE html>\n<html lang="ja">\n<head>\n<meta charset="UTF-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
            '<title>テーブル一覧 - 公式データ集</title>\n<style>%s</style>\n</head>\n<body>\n'
            '<header><h1>🧱 テーブル一覧</h1><p>公式データ集　%s　生成日時 %s JST　%d テーブル</p></header>\n'
            '<main>\n<nav><div class="wrap"><table><thead><tr><th>#</th><th>テーブル名</th>'
            '<th>表示名</th><th>DB</th><th>列数</th><th>行数</th></tr></thead>'
            '<tbody>%s</tbody></table></div></nav>\n'
            '<h2 class="part">スキーマ</h2>\n%s\n</main>\n</body>\n</html>\n') % (
                _REPORT_TABLES_CSS, h(site), now.strftime('%Y-%m-%d %H:%M'),
                len(regs), ''.join(toc), '\n'.join(secs))



def _report_response(fn, basename):
    """一覧ドキュメントの共通応答：権限確認→生成→HTMLで返す（?dl=1 で保存）。"""
    from flask import Response
    user_id = session.get('user_id')
    if not can_view(user_id):
        return Response(_report_page('権限がありません', '公式データ集', '',
                                     '', '<p class="err">この文書を閲覧する権限がありません．</p>'),
                        status=403, mimetype='text/html; charset=utf-8')
    try:
        doc = fn()
    except Exception as e:
        logging.error("official_data_archive report %s error: %s", basename, e)
        return Response(_report_page('生成エラー', '公式データ集', '', '',
                                     '<p class="err">文書の生成に失敗しました：%s</p>'
                                     % _h(e)),
                        status=500, mimetype='text/html; charset=utf-8')
    resp = Response(doc, mimetype='text/html; charset=utf-8')
    resp.headers['Cache-Control'] = 'no-store'
    if request.args.get('dl'):
        fname = '%s_%s.html' % (basename, get_jst_now().strftime('%Y%m%d_%H%M%S'))
        resp.headers['Content-Disposition'] = 'attachment; filename="%s"' % fname
    return resp


@official_data_archive_bp.route('/report/indicators', methods=['GET'])
@login_required
def report_indicators():
    """Web文書「指標の動き」。単一指標・複合指標の全グラフ（SVG）を返す。"""
    return _report_response(_report_indicators_html, 'official_indicators')


@official_data_archive_bp.route('/report/tables', methods=['GET'])
@login_required
def report_tables():
    """Web文書「テーブル一覧」。公式テーブルの名前と列構成・DDLを返す。"""
    return _report_response(_report_tables_html, 'official_tables')


# ────────────────────────────────────────────
# ダッシュボードへ戻る
# ────────────────────────────────────────────

@official_data_archive_bp.route('/return_to_fujin')
@login_required
def return_to_fujin():
    """FUJIN-Pダッシュボードに戻る。"""
    return redirect_to_dashboard()
