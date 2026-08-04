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

# sql_saver/routes.py
# SQL Saver 共通: アクセス制御 / 画面表示 / 対象DB列挙 / 作業領域 / 監査記録 / 共通ヘルパ

import os
import re
import json
import logging
import datetime
import mysql.connector
from flask import (render_template, jsonify, session, request,
                   redirect, url_for, current_app)
from decorators import login_required
from auth import redirect_to_dashboard
from db import DatabaseConfig
from config import Config
from . import sql_saver_bp


# ============================================================
# 日時（FUJIN-P 日時3層ルール: バックエンドでJSTの文字列にする）
#   サーバのローカル時刻はUTCなので datetime.now() は使わない。
# ============================================================

JST = datetime.timezone(datetime.timedelta(hours=9))


def now_jst():
    """JSTの現在時刻（tz付き）"""
    return datetime.datetime.now(JST)


def now_jst_str():
    """JSTの現在時刻を 'YYYY-MM-DD HH:MM:SS' で返す"""
    return now_jst().strftime('%Y-%m-%d %H:%M:%S')


def stamp_jst():
    """ジョブID・ファイル名に使う 'YYYYmmdd_HHMMSS'（JST）"""
    return now_jst().strftime('%Y%m%d_%H%M%S')


# ============================================================
# アクセス制御（Blueprint全体を admin 限定）
#   バックアップ・リストアとも破壊力があるため、画面表示も含めて admin のみ。
#   画面が全面Ajaxのため、拒否時の応答を出し分ける:
#     Ajax        → 401/403 のJSON
#     ページ遷移  → auth.login / guest.dashboard へリダイレクト
# ============================================================

def _is_ajax():
    """Ajax由来のリクエストか（POST全般 + XMLHttpRequestヘッダ）"""
    if request.method != 'GET':
        return True
    return request.headers.get('X-Requested-With') == 'XMLHttpRequest'


@sql_saver_bp.before_request
def require_admin():
    user_id = session.get('user_id')
    if not user_id:
        if _is_ajax():
            return jsonify({'success': False, 'error': 'ログインが必要です'}), 401
        return redirect(url_for('auth.login'))

    if get_user_category(user_id) != 'admin':
        if _is_ajax():
            return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403
        return redirect(url_for('guest.dashboard'))


def get_user_category(user_id):
    """users.category を返す。

    session['user_category'] があればそれを使い、無ければDBに1度だけ問い合わせて
    セッションへ載せる。バックアップは step を繰り返すため、リクエストごとの
    問い合わせを避ける。
    """
    cat = session.get('user_category')
    if cat:
        return cat
    cat = _fetch_user_category(user_id)
    if cat:
        session['user_category'] = cat
    return cat


def _fetch_user_category(user_id):
    """users.category をDBから取得（フォールバック経路）"""
    if not user_id:
        return None
    conn = None
    cursor = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT category FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()
        return row['category'] if row else None
    except Exception as e:
        logging.error("sql_saver get_user_category error: %s", e)
        return None
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if conn is not None and conn.is_connected():
            conn.close()


def is_admin(user_id):
    """管理者かどうか（before_request で判定済みだが、個別確認用に残す）"""
    return get_user_category(user_id) == 'admin'


# ============================================================
# SQL識別子の検査
#   DB名・テーブル名・カラム名は f-string でSQLへ埋め込むため、
#   埋め込む直前に必ずここを通す。zip由来の名前にも適用する。
# ============================================================

# MySQLはバッククォートで囲めば日本語を含む多くの文字を識別子に使える
# 本アプリは識別子を必ずバッククォートで
# 囲んで埋め込むので、弾くべきなのは次だけ:
#   ・バッククォート … 囲みを抜け出せる
#   ・制御文字 / NUL
#   ・パス区切り … テーブル名は <テーブル名>.json というファイル名にもなる
# 長さはMySQLの上限に合わせて64文字。
_IDENT_BAD_RE = re.compile(r'[`\x00-\x1f/\\]')


def safe_ident(name, kind='識別子'):
    """MySQL識別子として安全な文字列だけを通す。違反は ValueError。"""
    if (not isinstance(name, str) or not name or len(name) > 64
            or _IDENT_BAD_RE.search(name) or name in ('.', '..')):
        raise ValueError('不正な%s: %r' % (kind, name))
    return name


# ============================================================
# 対象データベースの列挙
#   Config の3定数を基準に列挙。ここを編集すればDBを増減できる。
#   key   = 画面・zip内フォルダで使う短い識別子
#   db    = 実際のMySQLデータベース名（接続に使う）
# ============================================================

def get_target_databases():
    """バックアップ/リストア対象のDB一覧を返す。

    Config に定義された既知のDB名から、重複を除いて列挙する。
    DBを増やしたい場合は Config に定数を足し、ここに1行追加するだけ。
    """
    candidates = [
        ('default', getattr(Config, 'DB_DEFAULT', None)),
        ('fujinp',  getattr(Config, 'DB_FUJINP',  None)),
        ('public',  getattr(Config, 'DB_PUBLIC',  None)),
    ]
    seen = set()
    result = []
    for key, dbname in candidates:
        if dbname and dbname not in seen:
            seen.add(dbname)
            result.append({'key': key, 'db': dbname})
    return result


def known_db_names():
    """復元先として許可するDB名の集合"""
    return {t['db'] for t in get_target_databases()}


def db_name_to_config(db_name):
    """MySQLデータベース名から接続configを得る（識別子検査つき）"""
    if db_name not in known_db_names():
        raise ValueError('対象外のデータベースです: %r' % (db_name,))
    return DatabaseConfig.get_config(db_name)


# ============================================================
# 作業領域
#   ★ static/ の外に置く。static配下に置くと、PythonAnywhere の静的マッピング
#     経由で Flask を通らずに zip が配信され得るため（zipには users テーブルの
#     メールアドレス・パスワードハッシュが含まれる）。
#
#   <root>/                         … 既定は 本アプリ配下 fujinp/sql_saver/sql_saver_work
#     ├── job_<stamp>/              … バックアップ作業中
#     ├── backup_<stamp>_<rand>.zip … 完成品
#     └── _restore_work/<token>/    … アップロードzipの展開先
# ============================================================

RESTORE_SUBDIR = '_restore_work'
LEGACY_DIRNAME = 'sql_saver_backups'   # v1.0 までの保存先（static配下）


def backups_root():
    """作業用ルートディレクトリ（static配下ではない・本アプリのディレクトリ配下）"""
    root = getattr(Config, 'SQL_SAVER_WORK_DIR', None)
    if not root:
        # ポリシー: アプリが使う作業領域はそのアプリのディレクトリ配下に置く
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sql_saver_work')
    os.makedirs(root, exist_ok=True)
    return root


def restore_work_root():
    """リストア用zipの展開先ルート"""
    root = os.path.join(backups_root(), RESTORE_SUBDIR)
    os.makedirs(root, exist_ok=True)
    return root


def legacy_backups_root():
    """v1.0 までの static 配下の保存先。存在すればパスを返す（後片付け専用）。"""
    static_dir = current_app.static_folder
    if not static_dir:
        return None
    path = os.path.join(static_dir, LEGACY_DIRNAME)
    return path if os.path.isdir(path) else None


# ============================================================
# 操作履歴（監査）
#   専用テーブル sql_saver_audit（<DB_ACCOUNT>$default）に記録する。
#   記録の失敗が本体の操作を壊さないよう、例外は握りつぶしてログに出す。
# ============================================================

AUDIT_TABLE = 'sql_saver_audit'
AUDIT_DETAIL_LIMIT = 60000     # text 型に収める上限（超えたら切り詰める）


def audit(action, detail=None, ok=True):
    """操作を1行記録する。action: backup / restore / clear_all など。"""
    payload = None
    if detail is not None:
        payload = json.dumps(detail, ensure_ascii=False)
        if len(payload) > AUDIT_DETAIL_LIMIT:
            payload = payload[:AUDIT_DETAIL_LIMIT] + '…(truncated)'

    logging.info("sql_saver audit: user=%s action=%s ok=%s",
                 session.get('user_id'), action, ok)

    conn = None
    cursor = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO `%s` (user_id, action, succeeded, detail, created_at) "
            "VALUES (%%s, %%s, %%s, %%s, %%s)" % AUDIT_TABLE,
            (session.get('user_id'), action, 1 if ok else 0, payload, now_jst_str()))
        conn.commit()
    except Exception as e:
        logging.error("sql_saver audit write error (%s): %s", action, e)
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if conn is not None and conn.is_connected():
            conn.close()


# ============================================================
# 画面
# ============================================================

@sql_saver_bp.route('/')
@login_required
def index():
    """SQL Saver メイン画面（バックアップ / リストア / 履歴）"""
    return render_template('sql_saver/sql_saver.html')


# ============================================================
# 対象DB一覧（画面のチェックボックス用）
# ============================================================

@sql_saver_bp.route('/databases', methods=['GET'])
@login_required
def databases():
    """バックアップ/リストア対象のDB一覧を返す。各DBのテーブル数も付ける。"""
    result = []
    for entry in get_target_databases():
        info = {'key': entry['key'], 'db': entry['db'],
                'table_count': None, 'reachable': False, 'error': None}
        conn = None
        cursor = None
        try:
            conn = mysql.connector.connect(**db_name_to_config(entry['db']))
            cursor = conn.cursor()
            cursor.execute("SHOW TABLES")
            info['table_count'] = len(cursor.fetchall())
            info['reachable'] = True
        except Exception as e:
            info['error'] = str(e)
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn is not None and conn.is_connected():
                conn.close()
        result.append(info)

    return jsonify({'success': True, 'databases': result})


# ============================================================
# 操作履歴の閲覧
# ============================================================

@sql_saver_bp.route('/audit', methods=['GET'])
@login_required
def audit_list():
    """直近の操作履歴を返す（既定100件）"""
    try:
        limit = min(int(request.args.get('limit', 100)), 500)
    except (TypeError, ValueError):
        limit = 100

    conn = None
    cursor = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT a.id, a.user_id, a.action, a.succeeded, a.detail, a.created_at, "
            "       u.full_name "
            "FROM `%s` a LEFT JOIN users u ON u.id = a.user_id "
            "ORDER BY a.id DESC LIMIT %%s" % AUDIT_TABLE, (limit,))
        rows = []
        for r in cursor.fetchall():
            rows.append({
                'id': r['id'],
                'user_id': r['user_id'],
                'user_name': r['full_name'],
                'action': r['action'],
                'succeeded': bool(r['succeeded']),
                'detail': r['detail'],
                # DBには既にJSTの文字列相当が入っている。ここで文字列化して返す。
                'created_at': r['created_at'].strftime('%Y-%m-%d %H:%M:%S')
                              if r['created_at'] else None,
            })
        return jsonify({'success': True, 'rows': rows})
    except Exception as e:
        logging.error("sql_saver audit_list error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if conn is not None and conn.is_connected():
            conn.close()


@sql_saver_bp.route('/return_to_fujin')
@login_required
def return_to_fujin():
    """FUJIN-Pダッシュボードに戻る"""
    return redirect_to_dashboard()