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
App Share (アプシャ) - FUJIN-Pアプリケーション共有システム
ルート定義

機能:
- 登録済みアプリの管理（レジストリ）
- ユーザマニュアル・技術仕様書の閲覧・編集
- App Info: 技術情報のフィールド編集 / JSONテキスト編集
- 全アプリ情報JSONエクスポート / インポート（インポートはadmin専用・管理タブ）
- アプリ単位のエクスポートパッケージ（JSON）作成（admin専用）
- アプリ単位パッケージの取り込み（admin専用・専用ダッシュボードで検証つき）
- アプリ説明のバージョン（更新日時）記録

※ アプリ説明のバージョン記録には app_share_registry に updated_at 列を追加:
   ALTER TABLE app_share_registry
       ADD COLUMN updated_at DATETIME NULL DEFAULT CURRENT_TIMESTAMP;
   （列が無い環境でも動作するが、その場合はドキュメント・ファイル更新時刻のみで判定）

※ 2026-07 改修: 公開(Publish)・取得・インストール(Subscribe)・
   バックアップ・復旧(Recovery)・履歴(History) 機能を廃止
"""

import os
import re
import sys
import json
import base64
import shutil
import secrets
import datetime
import logging
import importlib.util
import hashlib
from flask import (
    render_template, request, jsonify, session, Response, g, flash, redirect, url_for
)
import mysql.connector
from auth import redirect_to_dashboard
from . import app_share_bp
from config import Config
from db import DatabaseConfig
from decorators import login_required

# 定数
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# BASE_DIR の親（サイトのホーム）。auth.py / config.py / db.py / decorators.py などの
# プラットフォーム共通モジュールと、admin / guest / templates はこちら側に置かれる。
# （'/' 等へ暴走しないよう、親が BASE_DIR と異なる場合のみ採用）
_parent = os.path.dirname(BASE_DIR)
SITE_CODE_ROOT = _parent if (_parent and _parent != BASE_DIR and os.path.isdir(_parent)) else BASE_DIR

# 日時は FUJIN-P 日時3層ルールに従い JST で生成する（サーバのローカル時刻はUTC）
JST = datetime.timezone(datetime.timedelta(hours=9))


# ============================================
# 共通ユーティリティ・認可
# ============================================

def _now_jst():
    return datetime.datetime.now(JST)

def check_admin_permission(user_id):
    """user_id が admin かどうか。
    同一リクエスト内では1回だけDBに問い合わせる（before_request と各ルートの
    二重判定を吸収する）。リクエストをまたいでは持ち越さない。"""
    try:
        cached = getattr(g, '_app_share_admin', None)
    except RuntimeError:          # リクエスト文脈の外
        cached = None
    if cached is not None and cached[0] == user_id:
        return cached[1]

    result = False
    conn = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        # buffered=Trueにして、データをメモリに「吸い切り」ます
        with conn.cursor(dictionary=True, buffered=True) as cursor:
            cursor.execute("SELECT category FROM users WHERE id = %s", (user_id,))
            user = cursor.fetchone()
            # 💡 これが重要：未読パケットを完全に掃除する
            while cursor.nextset(): pass
            result = bool(user and user['category'] == 'admin')
    finally:
        if conn: conn.close()

    try:
        g._app_share_admin = (user_id, result)
    except RuntimeError:
        pass
    return result

# admin でなくてもアクセスしてよいエンドポイント（関数名で指定）
_NON_ADMIN_ENDPOINTS = frozenset({
    'dashboard',          # admin以外には app_share_public.html を返す
    'get_registry_apps',  # 公開画面のアプリ一覧
    'get_document',       # マニュアル本文（doc_type='note' は関数内で admin 判定）
    'manual_page',        # マニュアル単独ページ
    'return_to_fujin',    # FUJIN-Pダッシュボードへ戻る
})

@app_share_bp.before_request
def _app_share_authorize():
    """アプシャ全ルートの認可。既定は admin 必須。"""
    name = (request.endpoint or '').rsplit('.', 1)[-1]
    if name in _NON_ADMIN_ENDPOINTS:
        return None

    user_id = session.get('user_id')
    if not user_id:
        if request.method == 'GET':
            flash('ログインが必要です', 'error')
            return redirect(url_for('auth.login', next=request.url))
        return jsonify({'success': False, 'error': 'ログインが必要です'}), 401

    if not check_admin_permission(user_id):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403
    return None

def _parse_ts(value):
    """日時文字列を datetime に変換（失敗時 None）"""
    if not value:
        return None
    if isinstance(value, datetime.datetime):
        return value
    s = str(value).strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y/%m/%d %H:%M'):
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            pass
    try:
        return datetime.datetime.fromisoformat(s)
    except Exception:
        return None

def _fmt_jst(dt, fmt='%Y/%m/%d %H:%M'):
    """DBの日時（UTC）を日本時間（JST）の文字列にして返す（None は None）"""
    if not dt:
        return None
    if isinstance(dt, str):
        dt = _parse_ts(dt)
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(JST).strftime(fmt)

def touch_registry_timestamp(app_name, ts=None):
    """アプリ説明の更新日時（バージョン）を registry に記録する。
    app_share_registry.updated_at 列が未追加の環境では警告のみで続行。"""
    conn = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(buffered=True)
        cursor.execute("UPDATE app_share_registry SET updated_at=%s WHERE app_name=%s",
                       (ts or datetime.datetime.now(), app_name))
        conn.commit()
        cursor.close()
    except Exception as e:
        logging.warning(f"registry updated_at 記録スキップ({app_name}): {e}")
    finally:
        if conn and conn.is_connected():
            conn.close()


# ============================================
# ダッシュボード・レジストリ（一覧の基本操作）
# ============================================

@app_share_bp.route('/')
@login_required
def dashboard():
    """アプシャ エントリポイント
    - admin: 従来どおりの管理ダッシュボード（app_share_dashboard.html）
    - admin以外: マニュアル閲覧専用画面（app_share_public.html）
    """
    is_admin = check_admin_permission(session.get('user_id'))
    if is_admin:
        return render_template('app_share_dashboard.html', is_admin=is_admin)
    # admin以外は公開向けマニュアル表示装置
    return render_template('app_share_public.html')

@app_share_bp.route('/get_registry_apps')
@login_required
def get_registry_apps():
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True, buffered=True)
        cursor.execute("""
            SELECT * FROM app_share_registry ORDER BY sort_order ASC, id ASC
        """)
        apps = cursor.fetchall()
        for a in apps:
            if a.get('updated_at'):
                a['updated_at'] = a['updated_at'].strftime('%Y-%m-%d %H:%M:%S')
            # 版は正本（DB）の version_id をそのまま使う（version.json は廃止）
            # sort_order は DOUBLE。列が DECIMAL のサイトでは Decimal が返り
            # jsonify が落ちるので float に正規化しておく。
            try:
                a['sort_order'] = float(a.get('sort_order') or 0)
            except (TypeError, ValueError):
                a['sort_order'] = 0.0
        return jsonify({'success': True, 'apps': apps})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'cursor' in locals(): cursor.close()
        if 'conn' in locals() and conn.is_connected(): conn.close()

@app_share_bp.route('/registry/add', methods=['POST'])
@login_required
def registry_add():
    if not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403
    data = request.get_json()
    app_name = data.get('app_name', '').strip()
    display_name = data.get('display_name', '').strip() or app_name
    icon = data.get('icon', '📦').strip()
    description = data.get('description', '').strip()
    if not app_name:
        return jsonify({'success': False, 'error': 'アプリ名は必須です'}), 400
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True, buffered=True)
        cursor.execute("SELECT COALESCE(MAX(sort_order),0)+10 AS n FROM app_share_registry")
        nxt = cursor.fetchone()['n']
        try:
            cursor.execute("INSERT INTO app_share_registry (app_name,display_name,icon,description,sort_order,updated_at) VALUES(%s,%s,%s,%s,%s,%s)",
                           (app_name, display_name, icon, description, nxt, datetime.datetime.now()))
        except mysql.connector.Error:
            # updated_at 列が未追加の場合（Duplicate entry もここを通り、外側で処理される）
            cursor.execute("INSERT INTO app_share_registry (app_name,display_name,icon,description,sort_order) VALUES(%s,%s,%s,%s,%s)",
                           (app_name, display_name, icon, description, nxt))
        conn.commit()
        return jsonify({'success': True, 'message': f'「{display_name}」を登録しました'})
    except Exception as e:
        if 'conn' in locals(): conn.rollback()
        if 'Duplicate entry' in str(e):
            return jsonify({'success': False, 'error': f'「{app_name}」は既に登録されています'}), 400
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'cursor' in locals(): cursor.close()
        if 'conn' in locals() and conn.is_connected(): conn.close()

@app_share_bp.route('/registry/update/<int:reg_id>', methods=['POST'])
@login_required
def registry_update(reg_id):
    """レジストリ1行の更新（admin専用）。
    表示名・アイコン・概要に加えて、
      sort_order : 表示順（任意の実数。小さい順に並ぶ）
      app_name   : アプリのディレクトリ名（手入力なので打ち間違いを直せるように）
    を変更できる。app_name を変えるときは実在するディレクトリであることを
    確認したうえで、app_share_documents の同名参照も同一トランザクションで
    付け替える（ex_engagement → ext_engagement の取りこぼしを繰り返さない）。
    """
    if not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403
    data = request.get_json() or {}
    display_name = (data.get('display_name') or '').strip()
    icon = (data.get('icon') or '📦').strip()
    description = (data.get('description') or '').strip()
    if not display_name:
        return jsonify({'success': False, 'error': '表示名は必須です'}), 400

    # 表示順：空欄なら現在値を変えない
    raw_sort = data.get('sort_order')
    sort_order = None
    if raw_sort not in (None, ''):
        try:
            sort_order = float(raw_sort)
        except (TypeError, ValueError):
            return jsonify({'success': False,
                            'error': '表示順は数値で入力してください（小数可）'}), 400

    new_app_name = (data.get('app_name') or '').strip()

    conn = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True, buffered=True)
        cursor.execute("SELECT app_name FROM app_share_registry WHERE id=%s", (reg_id,))
        row = cursor.fetchone()
        if not row:
            return jsonify({'success': False, 'error': '見つかりません'}), 404
        old_app_name = row['app_name']

        renamed = bool(new_app_name and new_app_name != old_app_name)
        if renamed:
            if not re.match(r'^[A-Za-z0-9_]+$', new_app_name):
                return jsonify({'success': False,
                                'error': 'ディレクトリ名は英数字とアンダースコアのみです'}), 400
            # 実在確認。打ち間違いを別の打ち間違いに置き換えても意味がない。
            if not os.path.exists(os.path.join(BASE_DIR, new_app_name, '__init__.py')):
                return jsonify({'success': False,
                                'error': f'{new_app_name}/__init__.py が見つかりません。'
                                         'ディレクトリ名を確認してください'}), 400
            cursor.execute("SELECT id FROM app_share_registry WHERE app_name=%s AND id<>%s",
                           (new_app_name, reg_id))
            if cursor.fetchone():
                return jsonify({'success': False,
                                'error': f'「{new_app_name}」は既に別の行で登録されています'}), 400
            # 付け替え先に既にマニュアル／仕様書があると
            # unique(app_name, doc_type) に衝突するので先に知らせる。
            cursor.execute("SELECT COUNT(*) AS c FROM app_share_documents WHERE app_name=%s",
                           (new_app_name,))
            if cursor.fetchone()['c']:
                return jsonify({'success': False,
                                'error': f'「{new_app_name}」名義のマニュアル／仕様書が'
                                         '既にあります。先にそちらを整理してください'}), 400

        sets = ['display_name=%s', 'icon=%s', 'description=%s']
        params = [display_name, icon, description]
        if sort_order is not None:
            sets.append('sort_order=%s'); params.append(sort_order)
        if renamed:
            sets.append('app_name=%s'); params.append(new_app_name)

        try:
            cursor.execute("UPDATE app_share_registry SET " + ','.join(sets) +
                           ", updated_at=%s WHERE id=%s",
                           params + [datetime.datetime.now(), reg_id])
        except mysql.connector.Error:
            # updated_at 列が未追加の場合
            cursor.execute("UPDATE app_share_registry SET " + ','.join(sets) +
                           " WHERE id=%s", params + [reg_id])

        moved_docs = 0
        if renamed:
            cursor.execute("UPDATE app_share_documents SET app_name=%s WHERE app_name=%s",
                           (new_app_name, old_app_name))
            moved_docs = cursor.rowcount
        conn.commit()

        msg = '更新しました'
        if renamed:
            msg = (f'ディレクトリ名を {old_app_name} → {new_app_name} に変更しました'
                   f'（マニュアル／仕様書 {moved_docs} 件も付け替え）')
        return jsonify({'success': True, 'message': msg})
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'cursor' in locals(): cursor.close()
        if conn and conn.is_connected(): conn.close()

@app_share_bp.route('/registry/move/<int:reg_id>/<direction>', methods=['POST'])
@login_required
def registry_move(reg_id, direction):
    if not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403
    if direction not in ('up', 'down'):
        return jsonify({'success': False, 'error': '不正な方向'}), 400
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True, buffered=True)
        cursor.execute("SELECT id, sort_order FROM app_share_registry WHERE id=%s", (reg_id,))
        cur = cursor.fetchone()
        if not cur:
            return jsonify({'success': False, 'error': '見つかりません'}), 404
        if direction == 'up':
            cursor.execute("""SELECT id, sort_order FROM app_share_registry
                WHERE sort_order < %s OR (sort_order=%s AND id < %s)
                ORDER BY sort_order DESC, id DESC LIMIT 1""",
                (cur['sort_order'], cur['sort_order'], cur['id']))
        else:
            cursor.execute("""SELECT id, sort_order FROM app_share_registry
                WHERE sort_order > %s OR (sort_order=%s AND id > %s)
                ORDER BY sort_order ASC, id ASC LIMIT 1""",
                (cur['sort_order'], cur['sort_order'], cur['id']))
        nb = cursor.fetchone()
        if not nb:
            return jsonify({'success': True, 'message': '端です'})
        cursor.execute("UPDATE app_share_registry SET sort_order=%s WHERE id=%s", (nb['sort_order'], cur['id']))
        cursor.execute("UPDATE app_share_registry SET sort_order=%s WHERE id=%s", (cur['sort_order'], nb['id']))
        conn.commit()
        return jsonify({'success': True, 'message': '移動しました'})
    except Exception as e:
        if 'conn' in locals(): conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'cursor' in locals(): cursor.close()
        if 'conn' in locals() and conn.is_connected(): conn.close()


# ============================================
# 文書（マニュアル・仕様書の叙述・管理ノート）
# ============================================

@app_share_bp.route('/doc/<app_name>/<doc_type>/get', methods=['GET'])
@login_required
def get_document(app_name, doc_type):
    if doc_type not in ('manual', 'spec', 'note'):
        return jsonify({'success': False, 'error': '不正なドキュメントタイプ'}), 400
    # 管理ノートは admin 専用
    if doc_type == 'note' and not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403

    conn = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        # 💡 buffered=True を指定して、結果をメモリに完全に吸い出す
        with conn.cursor(dictionary=True, buffered=True) as cursor:
            cursor.execute("""
                SELECT d.*, u.full_name as updated_by_name
                FROM app_share_documents d
                LEFT JOIN users u ON d.updated_by = u.id
                WHERE d.app_name = %s AND d.doc_type = %s
            """, (app_name, doc_type))
            doc = cursor.fetchone()
            # 💡 完全に結果を使い切るための魔法の1行
            while cursor.nextset(): pass

        if doc:
            doc['updated_at'] = _fmt_jst(doc['updated_at'])
            doc['created_at'] = _fmt_jst(doc['created_at'])
        return jsonify({'success': True, 'doc': doc})
    except Exception as e:
        logging.error(f"Error in get_document: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            conn.close()

@app_share_bp.route('/doc/<app_name>/<doc_type>/save', methods=['POST'])
@login_required
def save_document(app_name, doc_type):
    if not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403
    if doc_type not in ('manual', 'spec', 'note'):
        return jsonify({'success': False, 'error': '不正なドキュメントタイプ'}), 400
    data = request.get_json()
    title = data.get('title', '').strip()
    content = data.get('content', '')
    user_id = session.get('user_id')
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(buffered=True)
        cursor.execute("""
            INSERT INTO app_share_documents (app_name, doc_type, title, content, updated_by)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                title = VALUES(title), content = VALUES(content),
                updated_by = VALUES(updated_by), updated_at = CURRENT_TIMESTAMP
        """, (app_name, doc_type, title, content, user_id))
        conn.commit()
        # アプリ説明のバージョン（更新日時）を記録
        touch_registry_timestamp(app_name)
        return jsonify({'success': True, 'message': '保存しました'})
    except Exception as e:
        if 'conn' in locals(): conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'cursor' in locals(): cursor.close()
        if 'conn' in locals() and conn.is_connected(): conn.close()

@app_share_bp.route('/doc/preview', methods=['POST'])
@login_required
def preview_document_markdown():
    # セッション更新の副作用（MySQL衝突）を強制停止
    session.modified = False

    try:
        markdown_text = request.form.get('markdown', '')

        # 💡 もし markdown_converter が内部でDB接続を行っている場合、
        # ここで別のエラーが連鎖します。
        from markdown_converter import process_markdown
        html = process_markdown(markdown_text, 'admin')

        return Response(html, mimetype='text/html')
    except Exception as e:
        # 💡 エラー内容をログに詳細に出力して、どこで「未読」が起きたか特定できるようにします
        logging.error(f"PREVIEW ERROR: {str(e)}")
        # ユーザーには「エラーが発生しました」という文字だけ返し、DB接続をハングさせない
        return Response(f"Preview error: {str(e)}", mimetype='text/html')

@app_share_bp.route('/doc/<app_name>/<doc_type>/edit')
@login_required
def edit_document(app_name, doc_type):
    if not check_admin_permission(session.get('user_id')):
        return "管理者権限が必要です", 403
    if doc_type not in ('manual', 'spec', 'note'):
        return "不正なドキュメントタイプ", 400

    doc = None
    conn = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        # 💡 buffered=True を追加
        with conn.cursor(dictionary=True, buffered=True) as cursor:
            cursor.execute("SELECT * FROM app_share_documents WHERE app_name=%s AND doc_type=%s", (app_name, doc_type))
            doc = cursor.fetchone()
            # 💡 fetchoneの後、残留パケットを掃除する
            while cursor.nextset(): pass
    except Exception as e:
        logging.error(f"Error in edit_document: {e}")
        doc = None
    finally:
        if conn and conn.is_connected():
            conn.close()

    if not doc:
        doc = {'app_name': app_name, 'doc_type': doc_type, 'title': '', 'content': ''}

    title_label = {'manual': 'ユーザマニュアル', 'spec': '技術仕様書',
                   'note': '管理ノート'}.get(doc_type, doc_type)
    return render_template('app_share_edit_doc.html',
                           app_name=app_name, doc_type=doc_type,
                           title_label=title_label, doc=doc)

@app_share_bp.route('/manual/<app_name>')
@login_required
def manual_page(app_name):
    """ユーザーズマニュアルの単独ページ"""
    is_admin = check_admin_permission(session.get('user_id'))
    display_name = app_name
    icon = '📖'
    doc = None
    conn = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        with conn.cursor(dictionary=True, buffered=True) as cursor:
            cursor.execute(
                "SELECT display_name, icon FROM app_share_registry WHERE app_name = %s",
                (app_name,))
            reg = cursor.fetchone()
            while cursor.nextset(): pass
            if reg:
                display_name = reg['display_name'] or app_name
                icon = reg['icon'] or '📖'
        with conn.cursor(dictionary=True, buffered=True) as cursor:
            cursor.execute("""
                SELECT d.title, d.content, d.updated_at, u.full_name AS updated_by_name
                FROM app_share_documents d
                LEFT JOIN users u ON d.updated_by = u.id
                WHERE d.app_name = %s AND d.doc_type = 'manual'
            """, (app_name,))
            doc = cursor.fetchone()
            while cursor.nextset(): pass
    except Exception as e:
        logging.error(f"manual_page error: {e}")
    finally:
        if conn and conn.is_connected():
            conn.close()

    updated_at = ''
    updated_by_name = ''
    content = ''
    if doc:
        content = doc.get('content') or ''
        updated_by_name = doc.get('updated_by_name') or ''
        if doc.get('updated_at'):
            updated_at = _fmt_jst(doc['updated_at'])

    return render_template('app_share_manual_page.html',
                           app_name=app_name,
                           display_name=display_name,
                           icon=icon,
                           content=content,
                           updated_at=updated_at,
                           updated_by_name=updated_by_name,
                           is_admin=is_admin)

@app_share_bp.route('/return_to_fujin')
@login_required
def return_to_fujin():
    """FUJINダッシュボードに戻る"""
    return redirect_to_dashboard()


# ============================================
# カーネルパッケージ（config.py を除くカーネル一式の輸出．さいまる・dist/ 用）
# ============================================

KERNEL_EXPORT_TYPE = 'fujinp_kernel_package'

# 収集対象：SITE_CODE_ROOT 直下のファイルのうち、この拡張子のもの
KERNEL_FILE_EXTS = ('.py', '.txt', '.md', '.json', '.sql', '.cfg', '.ini')

# 収集対象：SITE_CODE_ROOT 直下のディレクトリ（再帰）
# static_for_distribution = 配布する公開資産の原本（CSS・JS・アイコン・規約類）。
# 実行中のコードは書き込まないので、投稿データが紛れ込む経路がない。
# 一方 static/ は nginx が配信する実行時ディレクトリで投稿が混在するため、
# 引き続き対象外（件数だけ static_excluded に記録）。
KERNEL_INCLUDE_DIRS = ('templates', 'static_for_distribution')

# 除外するファイル名（SITE_CODE_ROOT 直下）
KERNEL_EXCLUDE_FILES = frozenset({
    'config.py',    # 秘密情報。値は出さず、設定名だけ config_keys に載せる
    'README.txt',   # PythonAnywhere の既定ファイル
})

# この接頭辞で始まるファイルは名前だけで除外（OAuth資格情報ファイル等）
KERNEL_EXCLUDE_PREFIXES = ('client_secret', 'credentials', 'service_account')

KERNEL_MAX_FILE_SIZE = 5 * 1024 * 1024

KERNEL_VERSION_FILE = os.path.join(SITE_CODE_ROOT, 'kernel_version.json')

# 秘密らしき記述の検出（検出した値そのものは記録しない）
# 資格情報らしき代入。名前には前後に語がつく（DB_PASSWORD / SECRET_KEY /
# LINE_CHANNEL_SECRET など）ので \b では捕まらない。前後の語を許す。
# 右辺がリテラル文字列のときだけ拾う（Config.X や os.environ.get(...) は対象外）。
_SECRET_PATTERNS = [
    # 値は「空白を含まない印字可能ASCII」に限る。日本語のメッセージ
    #（例: _ERROR_JA の 'token_revoked': 'トークンが失効しています'）は
    # 資格情報ではないので、これで誤検出が落ちる。
    (re.compile(r'(?i)[A-Za-z0-9_.]*'
                r'(?:password|passwd|secret|token|api_?key|credential|private_key)'
                r'[A-Za-z0-9_]*[\'"]?\s*[:=]\s*[\'"][\x21-\x7e]{4,}[\'"]'),
     '資格情報らしき代入'),
    (re.compile(r'mysql\.pythonanywhere-services\.com'), 'DBホスト名の直書き'),
    (re.compile(r'GOCSPX-'), 'Google クライアントシークレット'),
    (re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----'), '秘密鍵'),
]

def _scan_secrets(rel_path, text):
    """資格情報らしき記述を検出する。値は記録せず、位置と種別だけ返す。"""
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        for pat, label in _SECRET_PATTERNS:
            if pat.search(line):
                out.append({'path': rel_path, 'line': i, 'kind': label})
                break
    return out

_CONFIG_KEY_RE = re.compile(r'^\s*([A-Z][A-Z0-9_]*)\s*=', re.M)

def _config_keys(config_path):
    """config.py に定義されている設定名（大文字定数）だけを列挙する。
    値は読み取っても記録しない。カーネル更新時に「受け入れ側に不足している
    設定」を知らせるために使う。
    ※ config_template.py の生成はさいまるの担当（あちらには生成後の
      人によるレビュー段階があるため匿名化を試みてよい）。"""
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            src = f.read()
    except Exception:
        return []
    return sorted(set(_CONFIG_KEY_RE.findall(src)))

# カーネルのコードが実際に参照している設定名（Config.XXX / config['XXX']）
# Config.XXX / config.XXX（Jinja） / config['XXX'] / config.get('XXX') /
# getattr(Config, 'XXX', ...)（＝まだ値が決まっていない設定の受け取り口）
_CONFIG_USE_RE = re.compile(
    r'\b[Cc]onfig\.([A-Z][A-Z0-9_]*)'
    r'|\b[Cc]onfig\[[\'"]([A-Z][A-Z0-9_]*)[\'"]\]'
    r'|\b[Cc]onfig\.get\(\s*[\'"]([A-Z][A-Z0-9_]*)[\'"]'
    r'|\bgetattr\(\s*[Cc]onfig\s*,\s*[\'"]([A-Z][A-Z0-9_]*)[\'"]'
)

def _config_keys_used(files):
    """カーネルのコードが参照している設定名を集める。
    受け入れ側には「カーネルが本当に要求する設定」だけを知らせたいので、
    config.py に定義されているだけのアプリ固有の設定と区別する。"""
    used = set()
    for f in files:
        if f.get('encoding') != 'text':
            continue
        if not f['path'].endswith(('.py', '.html')):
            continue
        for m in _CONFIG_USE_RE.finditer(f.get('content') or ''):
            used.add(next(g for g in m.groups() if g))
    return sorted(used)

_BP_IMPORT_RE = re.compile(r'^\s*from\s+([A-Za-z_][\w.]*)\s+import\s+(.+)$', re.M)

_BP_REG_RE = re.compile(r'register_blueprint\(\s*([A-Za-z_]\w*)')

# 括弧で複数行に分けた import を1行に畳む（from X import ( a, b, c ) 形式）
_PAREN_IMPORT_RE = re.compile(r'from\s+([A-Za-z_][\w.]*)\s+import\s*\(([^)]*)\)')

_APP_PKG_NAME = os.path.basename(BASE_DIR.rstrip('/')) or 'fujinp'

def _strip_comment_lines(text):
    """行頭が # の行を空行に置き換える。
    コメントアウトされた import / register_blueprint を「生きている」と
    誤認しないため。行末コメント（コード # 説明）は壊さない。"""
    return '\n'.join('' if ln.lstrip().startswith('#') else ln
                     for ln in (text or '').split('\n'))

def _kernel_required_modules(app_py_text):
    """app.py が register_blueprint している Blueprint と、その由来アプリ名を返す。
    受け入れ側で「そのアプリがローカルに在るか」を照合するために使う。
    'fujinp.app_share' → 'app_share'、'admin'（ホーム直下）→ 'admin' に正規化する。
    コメントアウトされた行は無視する。"""
    src = _strip_comment_lines(app_py_text)
    # 括弧で複数行に分けた import を1行に畳む（from X import ( a, b, c ) 形式）
    flat = _PAREN_IMPORT_RE.sub(
        lambda m: 'from %s import %s' % (m.group(1), ' '.join(m.group(2).split())),
        src)
    bp_to_mod = {}
    for mod, names in _BP_IMPORT_RE.findall(flat):
        for n in re.split(r'[,\s]+', names.replace('(', ' ').replace(')', ' ')):
            n = n.strip().rstrip(',')
            if n and n != 'as':
                bp_to_mod[n] = mod

    seen, out = set(), []
    for bp in _BP_REG_RE.findall(src):
        mod = bp_to_mod.get(bp)
        top = None
        if mod:
            parts = mod.split('.')
            # 先頭が fujinp（アプリ用パッケージ）なら、その次がアプリ名
            top = parts[1] if (parts[0] == _APP_PKG_NAME and len(parts) > 1) else parts[0]
        key = (bp, top)
        if key in seen:
            continue
        seen.add(key)
        out.append({'blueprint': bp, 'module': mod, 'app': top})
    return out

def _build_kernel_package(generated_by=None, site_url=None):
    """カーネルのエクスポートパッケージを組み立てる。"""
    files = []
    warnings = []
    latest_mtime = None

    def _add(abs_path, rel_path, block_on_secret=True):
        """block_on_secret=False のときは、資格情報らしき記述を検出しても
        警告に記録するだけで内容は同梱する（ホーム直下の .py 用）。"""
        nonlocal latest_mtime
        try:
            size = os.path.getsize(abs_path)
            mt = os.path.getmtime(abs_path)
        except OSError:
            return
        if latest_mtime is None or mt > latest_mtime:
            latest_mtime = mt
        entry = {
            'path': rel_path,
            'size': size,
            'mtime': datetime.datetime.fromtimestamp(mt, JST).strftime('%Y-%m-%d %H:%M:%S'),
        }
        if size > KERNEL_MAX_FILE_SIZE:
            entry.update({'skipped': True, 'reason': 'サイズ超過'})
            files.append(entry)
            return
        try:
            with open(abs_path, 'r', encoding='utf-8') as f:
                text = f.read()
            hits = _scan_secrets(rel_path, text)
            if hits:
                for h in hits:
                    h['excluded'] = bool(block_on_secret)
                warnings.extend(hits)
                if block_on_secret:
                    # テンプレート・配布原本・直下の非.pyファイルは、疑わしければ
                    # 中身を出さない（アプシャのエクスポートに人のレビュー段階が
                    # 無いため）。誤検出でも「除外された」と受け入れ側に見える。
                    entry.update({'skipped': True,
                                  'reason': '資格情報らしき記述を含むため内容を除外',
                                  'secret_hits': len(hits)})
                    files.append(entry)
                    return
                # ホーム直下の .py はカーネル本体。落とすとサイトが動かなくなる
                # ので、警告を残して同梱する（config.py は名前で完全除外済み）。
                entry['secret_hits'] = len(hits)
            entry['encoding'] = 'text'
            entry['content'] = text
        except (UnicodeDecodeError, ValueError):
            with open(abs_path, 'rb') as f:
                entry['encoding'] = 'base64'
                entry['content'] = base64.b64encode(f.read()).decode('ascii')
        files.append(entry)

    # 1) ホーム直下のファイル
    for name in sorted(os.listdir(SITE_CODE_ROOT)):
        p = os.path.join(SITE_CODE_ROOT, name)
        if not os.path.isfile(p):
            continue
        if name.startswith('.') or name in KERNEL_EXCLUDE_FILES:
            continue
        if name.startswith(KERNEL_EXCLUDE_PREFIXES):
            continue
        if not name.endswith(KERNEL_FILE_EXTS):
            continue
        # ホーム直下の .py はカーネル本体。config.py だけ名前で完全除外し
        # （KERNEL_EXCLUDE_FILES）、残りは誤検出で落とさず警告のみとする。
        _add(p, name, block_on_secret=not name.endswith('.py'))

    # 2) 指定ディレクトリ（再帰）
    for d in KERNEL_INCLUDE_DIRS:
        root = os.path.join(SITE_CODE_ROOT, d)
        if not os.path.isdir(root):
            continue
        for cur, dirs, fnames in os.walk(root):
            dirs[:] = [x for x in dirs if x != '__pycache__' and not x.startswith('.')]
            for fn in sorted(fnames):
                if fn.startswith('.') or fn.endswith('.pyc'):
                    continue
                ap = os.path.join(cur, fn)
                _add(ap, os.path.relpath(ap, SITE_CODE_ROOT).replace('\\', '/'))

    # 2b) fujinp/ 配下のカーネル側ファイル（正本の読み手と写し．これが無いと起動しない）
    for kf in ('__init__.py', 'registry.py', 'app_registry.json'):
        ap = os.path.join(BASE_DIR, kf)
        if os.path.isfile(ap):
            _add(ap, 'fujinp/' + kf, block_on_secret=False)

    # static は配布対象外（投稿データが混在するため）。必要なファイルは
    # 送り手の admin が個別に渡す。ここには件数だけ残す。
    static_excluded = []
    _sd = os.path.join(SITE_CODE_ROOT, 'static')
    if os.path.isdir(_sd):
        static_excluded.append(
            {'path': 'static',
             'file_count': sum(len(fs) for _r, _d, fs in os.walk(_sd))})

    # 3) 設定名（値は含めない）
    config_keys = _config_keys(os.path.join(SITE_CODE_ROOT, 'config.py'))
    config_keys_used = _config_keys_used(files)

    # 4) app.py が要求するアプリ（受け入れ側の事前チェック用）
    app_py = next((f for f in files if f['path'] == 'app.py'), None)
    required = _kernel_required_modules(app_py.get('content') or '') if app_py else []

    # 5) 版（内容ハッシュ＋最終更新時刻）
    parts = []
    for f in sorted(files, key=lambda x: x['path']):
        parts.append(f['path'])
        parts.append(str(f.get('content') or ''))
    content_hash = hashlib.sha1('\n'.join(parts).encode('utf-8', 'replace')).hexdigest()[:6]
    updated_at = (datetime.datetime.fromtimestamp(latest_mtime, JST).strftime('%Y-%m-%d %H:%M:%S')
                  if latest_mtime else None)

    version_id = None
    try:
        with open(KERNEL_VERSION_FILE, 'r', encoding='utf-8') as f:
            version_id = (json.load(f) or {}).get('version_id')
    except Exception:
        pass
    # 未確定なら None のまま。ここで時刻から作ると、内容が同じでも
    # エクスポートのたびに版が変わり、取り込み側の新旧判定が壊れる。
    # 同一性の判定は content_hash で行う。
    return {
        'export_type': KERNEL_EXPORT_TYPE,
        'format_version': 1,
        'display_name': 'FUJIN-P カーネル',
        'description': 'プラットフォーム共通部（ホーム直下のコードと templates/）。'
                       'アプリは含まない。config.py は同梱せず、設定名のみ config_keys に載せる。',
        'site_name': os.path.basename(SITE_CODE_ROOT),
        'site_url': site_url or '',
        'generated_at': _now_jst().strftime('%Y-%m-%d %H:%M:%S'),
        'generated_by': generated_by,
        'updated_at': updated_at,
        'content_hash': content_hash,
        'version_id': version_id,
        'file_count': len(files),
        'files': files,
        'static_excluded': static_excluded,
        'config_keys': config_keys,
        'config_keys_used': config_keys_used,
        'required_apps': required,
        'warnings': warnings,
        'package_note': (
            'FUJIN-P カーネルパッケージ。files[]=SITE_CODE_ROOT直下のコードと templates/ '
            '（config.py・static・fujinp・アップロード領域は除外）/ '
            'config_keys=config.py が定義している設定名の一覧（値は含まない）/ '
            'config_keys_used=カーネルのコードが実際に参照している設定名'
            '（受け入れ側の不足設定の判定にはこちらを使う）/ '
            'required_apps=app.py が register_blueprint しているアプリ'
            '（app キーがアプリ名。受け入れ側で実在を照合すること）/ '
            'warnings=秘密らしき記述の検出結果（値は含まない）。'
            'static_excluded=配布対象外とした static の件数（投稿データ混在のため一切配らない。'
            '必要な静的ファイルは admin が手作業で移すこと）。'
        ),
    }

@app_share_bp.route('/export_kernel_package', methods=['GET'])
@login_required
def export_kernel_package():
    """カーネルのエクスポートパッケージ(JSON)を生成してダウンロード
    ※admin限定（_app_share_authorize による）"""
    conn = None
    try:
        generated_by = None
        try:
            conn = mysql.connector.connect(**DatabaseConfig.default())
            cursor = conn.cursor(dictionary=True, buffered=True)
            cursor.execute("SELECT full_name FROM users WHERE id = %s",
                           (session.get('user_id'),))
            u = cursor.fetchone()
            generated_by = u['full_name'] if u else None
            cursor.close()
        except Exception:
            pass

        site_url = request.host_url.rstrip('/') if request else ''
        package = _build_kernel_package(generated_by=generated_by, site_url=site_url)

        json_text = json.dumps(package, ensure_ascii=False, indent=2, default=str)
        filename = "fujinp_kernel_{}.json".format(_now_jst().strftime('%Y%m%d_%H%M%S'))
        resp = Response(json_text, mimetype='application/json; charset=utf-8')
        resp.headers['Content-Disposition'] = 'attachment; filename="{}"'.format(filename)
        return resp
    except Exception as e:
        logging.error("export_kernel_package error: %s", e)
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            conn.close()
