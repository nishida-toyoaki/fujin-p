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
まいあし (MaiAshi) ルート（2026-10-20 第1段）

  組み込みの手引き … data_for_distribution/ の .md（導入・移行の手引き）を画面で読み，.md で受け取る．
                     一覧はコード内の固定表 GUIDES で，DB にも他のアプリにも依存しない．
                     まいあしのパッケージに入って一緒に移るので，移設先でもそのまま読める（2026-10-31）
  図書館      … 文書アーカイブなどに置いたその他の文書への目次．行は題名・説明・URL・並び順だけ
  配布ファイル … 立ち上げスクリプト fujinp_bootstrap.py（このサイトのホーム直下にある本物を配る．2026-10-31）

閲覧はログインした人全員．目次の編集は admin と「まいあしインストラクター」グループの所属者．
"""
import datetime
import logging
import os

import mysql.connector
from flask import abort, jsonify, render_template, request, send_from_directory, session

from auth import redirect_to_dashboard
from db import DatabaseConfig

from . import migration_assistant

# まいあしが所有するテーブル（SQL では必ずこの定数を使う．アプシャは *_TABLE 定数から見つける）
LIBRARY_TABLE = 'migration_assistant_library'

# 教材を作り，図書館の目次を編集できる人のグループ（「ユーザとグループ」で登録する）
INSTRUCTOR_GROUP = 'まいあしインストラクター'

HERE = os.path.dirname(os.path.abspath(__file__))
DIST_DIR = os.path.join(HERE, 'data_for_distribution')
# 立ち上げスクリプトはカーネルの一部としてホーム直下に置かれている（全アプリパッケージにも入る）．
# まいあしは写しを持たず，ホーム直下の本物を配る（2026-10-31．旧 fujinp_kernel_install.py と build_installer.py は廃止）
SITE_ROOT = os.path.dirname(os.path.dirname(HERE))
BOOTSTRAP_NAME = 'fujinp_bootstrap.py'

JST = datetime.timezone(datetime.timedelta(hours=9), 'JST')


def get_jst_now():
    """JST の naive datetime（DBがUTCのため，NOW() に頼らず明示的に書く）"""
    return datetime.datetime.now(JST).replace(tzinfo=None)


def get_db_connection():
    return mysql.connector.connect(**DatabaseConfig.default())


# =============================================================================
# 権限
# =============================================================================

def _ug(name):
    """「ユーザとグループ」の公開API を遅延で取り出す（無ければ None）"""
    try:
        from fujinp.user_groups import utils
        return getattr(utils, name, None)
    except Exception:
        return None


def _in_group_by_sql(user_id, group_name):
    """公開API が無いサイト向けの代替：直接メンバーを有効期間つきで見る"""
    now = get_jst_now()
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT 1 FROM user_group_memberships m
            JOIN user_groups g ON g.id = m.group_id
            WHERE m.user_id = %s AND g.name = %s
              AND (m.valid_from IS NULL OR m.valid_from <= %s)
              AND (m.valid_until IS NULL OR m.valid_until >= %s)
            LIMIT 1
        """, (user_id, group_name, now, now))
        hit = cur.fetchone() is not None
        cur.close()
        return hit
    except Exception as e:
        logging.error("migration_assistant _in_group_by_sql: %s", e)
        return False
    finally:
        if conn is not None and conn.is_connected():
            conn.close()


def is_instructor(user_id=None):
    """admin，または「まいあしインストラクター」の所属者か"""
    user_id = user_id or session.get('user_id')
    if not user_id:
        return False
    if session.get('user_category') == 'admin':
        return True
    names_fn = _ug('get_user_group_names')
    if names_fn is not None:
        try:
            return INSTRUCTOR_GROUP in (names_fn(user_id) or [])
        except Exception as e:
            logging.warning("migration_assistant get_user_group_names: %s", e)
    return _in_group_by_sql(user_id, INSTRUCTOR_GROUP)


def _need_login_json():
    return jsonify({'success': False, 'error': 'ログインが切れています．再ログインしてください'}), 401


def _need_instructor_json():
    return jsonify({'success': False,
                    'error': 'この操作はまいあしインストラクターだけが行えます'}), 403


# =============================================================================
# 組み込みの手引き（data_for_distribution/ の .md）
# =============================================================================

# key: (ファイル名, 表題, 一行の説明)．並びは画面の表示順
GUIDES = [
    ('overview', 'install_overview.md', '全体マニュアル',
     '新品の PythonAnywhere に FUJIN-P を立ち上げるまでの見取り図．全アプリパッケージ1つと config.py，2つの段階，手で行う設定，アプシャが開けなくなったときの立て直し．最初にこれを読む．'),
    ('stage1', 'install_stage1.md', '第1段階マニュアル',
     '立ち上げスクリプト fujinp_bootstrap.py で，全アプリパッケージからカーネルとアプシャまでを立ち上げ，admin でログインできるところまで．'),
    ('stage2', 'install_stage2.md', '第2段階マニュアル',
     '立ち上がったサイトのアプシャで，同じ全アプリパッケージから一般のアプリをまとめて取り込む．'),
    ('external', 'install_external_services.md', '外部サービス設定（Google認証・Slack）',
     '新しいサイトで Google ログインのコールバック URL を登録し直す手順と，Slack の二重投稿・予約投稿を避ける切替の順序．なぜ必要かの説明つき．'),
    ('legacy', 'legacy_docs_202607.md', 'まいあし 旧文書（2026年7月版）',
     '正本化より前のユーザマニュアルと技術仕様書．控えとして残してある．現行のコードとは食い違う．'),
]
GUIDE_MAP = {k: (name, title, desc) for k, name, title, desc in GUIDES}


def _guide_info():
    """画面に並べる手引きの一覧（実在するものに exists=True）"""
    rows = []
    for key, name, title, desc in GUIDES:
        path = os.path.join(DIST_DIR, name)
        ok = os.path.isfile(path)
        rows.append({
            'key': key, 'name': name, 'title': title, 'desc': desc, 'exists': ok,
            'mtime': (datetime.datetime.fromtimestamp(os.stat(path).st_mtime, JST).strftime('%Y-%m-%d %H:%M')
                      if ok else ''),
        })
    return rows


def _guide_text(key):
    """(ファイル名, 表題, 本文) を返す．表に無い key は None，ファイルが無ければ本文 None"""
    if key not in GUIDE_MAP:
        return None
    name, title, _desc = GUIDE_MAP[key]
    path = os.path.join(DIST_DIR, name)
    if not os.path.isfile(path):
        return name, title, None
    with open(path, encoding='utf-8') as f:
        return name, title, f.read()


def _md_to_html(md_text):
    """Markdown を HTML に．カーネルの markdown_converter（無害化つき）を使い，無ければ素のテキストで出す"""
    try:
        from markdown_converter import process_markdown_for_preview
        return process_markdown_for_preview(md_text)
    except Exception as e:
        logging.warning("migration_assistant _md_to_html: %s", e)
        import html as _h
        return '<pre style="white-space:pre-wrap">' + _h.escape(md_text) + '</pre>'


# =============================================================================
# 配布ファイル
# =============================================================================

def _dist_files():
    """配布するファイルの一覧（名前 → 実パス）．手引きの .md は「組み込みの手引き」が配る"""
    out = {}
    boot = os.path.join(SITE_ROOT, BOOTSTRAP_NAME)
    if os.path.isfile(boot):
        out[BOOTSTRAP_NAME] = boot
    return out


DIST_NOTES = {
    BOOTSTRAP_NAME: '立ち上げスクリプト．全アプリパッケージの JSON と一緒に，新しいサイトのホーム直下（/home/<アカウント名>/）に置いて使います．'
                    'ふつうは JSON から取り出すので，ダウンロードは要りません',
}


def _dist_info():
    rows = []
    for name, path in _dist_files().items():
        st = os.stat(path)
        rows.append({
            'name': name,
            'size': st.st_size,
            'mtime': datetime.datetime.fromtimestamp(st.st_mtime, JST).strftime('%Y-%m-%d %H:%M'),
            'note': DIST_NOTES.get(name, ''),
        })
    return rows


# =============================================================================
# 画面
# =============================================================================

@migration_assistant.route('/')
@migration_assistant.route('/index')
def index():
    if not session.get('user_id'):
        return render_template('migration_assistant/error.html', error='ログインが必要です'), 401
    return render_template('migration_assistant/migration_assistant_index.html',
                           can_edit=is_instructor(),
                           guides=_guide_info(),
                           dist_files=_dist_info(),
                           instructor_group=INSTRUCTOR_GROUP)


@migration_assistant.route('/guide/<key>')
def guide_view(key):
    """組み込みの手引きを画面で読む"""
    if not session.get('user_id'):
        return render_template('migration_assistant/error.html',
                               error='手引きを読むにはログインが必要です'), 401
    got = _guide_text(key)
    if got is None:
        abort(404)
    name, title, text = got
    if text is None:
        return render_template('migration_assistant/error.html',
                               error=f'{name} がアプリの data_for_distribution/ にありません'), 404
    return render_template('migration_assistant/migration_assistant_guide.html',
                           key=key, name=name, title=title, body=_md_to_html(text),
                           guides=_guide_info())


@migration_assistant.route('/guide/<key>/download')
def guide_download(key):
    """組み込みの手引きを .md で受け取る"""
    if not session.get('user_id'):
        return render_template('migration_assistant/error.html',
                               error='手引きの受け取りにはログインが必要です'), 401
    if key not in GUIDE_MAP:
        abort(404)
    name = GUIDE_MAP[key][0]
    if not os.path.isfile(os.path.join(DIST_DIR, name)):
        abort(404)
    return send_from_directory(DIST_DIR, name, as_attachment=True, download_name=name,
                               mimetype='text/markdown')


@migration_assistant.route('/dist/<path:name>')
def dist_download(name):
    """配布ファイルを受け取る（ガイドの文書からもここへリンクする）"""
    if not session.get('user_id'):
        return render_template('migration_assistant/error.html',
                               error='配布ファイルの受け取りにはログインが必要です'), 401
    files = _dist_files()
    if name not in files:
        abort(404)
    path = files[name]
    return send_from_directory(os.path.dirname(path), os.path.basename(path),
                               as_attachment=True, download_name=name)


@migration_assistant.route('/return_to_fujin')
def return_to_fujin():
    return redirect_to_dashboard()


# =============================================================================
# 図書館 API
# =============================================================================

def _row_out(r):
    return {
        'id': r['id'],
        'title': r['title'],
        'description': r.get('description') or '',
        'url': r['url'],
        'sort_order': r['sort_order'],
        'updated_at': r['updated_at'].strftime('%Y-%m-%d %H:%M') if r.get('updated_at') else '',
    }


def _clean_entry(data):
    """入力を検証して (title, description, url) を返す．不正なら ValueError"""
    title = str(data.get('title') or '').strip()
    url = str(data.get('url') or '').strip()
    desc = str(data.get('description') or '').strip()
    if not title:
        raise ValueError('題名は必須です')
    if len(title) > 255:
        raise ValueError('題名は255文字までです')
    if not url:
        raise ValueError('URLは必須です')
    if len(url) > 1000:
        raise ValueError('URLは1000文字までです')
    if not (url.startswith('https://') or url.startswith('http://') or url.startswith('/')):
        raise ValueError('URLは https:// または / で始めてください')
    if len(desc) > 2000:
        raise ValueError('説明は2000文字までです')
    return title, desc, url


@migration_assistant.route('/api/library', methods=['GET'])
def api_library_list():
    if not session.get('user_id'):
        return _need_login_json()
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute(f"""
            SELECT id, title, description, url, sort_order, updated_at
            FROM {LIBRARY_TABLE} ORDER BY sort_order, id
        """)
        rows = [_row_out(r) for r in cur.fetchall()]
        cur.close()
        return jsonify({'success': True, 'entries': rows, 'can_edit': is_instructor()})
    except Exception as e:
        logging.error("migration_assistant api_library_list: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn is not None and conn.is_connected():
            conn.close()


@migration_assistant.route('/api/library', methods=['POST'])
def api_library_add():
    if not session.get('user_id'):
        return _need_login_json()
    if not is_instructor():
        return _need_instructor_json()
    try:
        title, desc, url = _clean_entry(request.get_json(silent=True) or {})
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(f"SELECT COALESCE(MAX(sort_order), 0) FROM {LIBRARY_TABLE}")
        nxt = (cur.fetchone()[0] or 0) + 10
        now = get_jst_now()
        cur.execute(f"""
            INSERT INTO {LIBRARY_TABLE}
                (title, description, url, sort_order, created_by, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (title, desc, url, nxt, session.get('user_id'), now, now))
        new_id = cur.lastrowid
        conn.commit()
        cur.close()
        return jsonify({'success': True, 'id': new_id})
    except Exception as e:
        logging.error("migration_assistant api_library_add: %s", e)
        if conn is not None:
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn is not None and conn.is_connected():
            conn.close()


@migration_assistant.route('/api/library/<int:entry_id>', methods=['PUT'])
def api_library_update(entry_id):
    if not session.get('user_id'):
        return _need_login_json()
    if not is_instructor():
        return _need_instructor_json()
    try:
        title, desc, url = _clean_entry(request.get_json(silent=True) or {})
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(f"SELECT id FROM {LIBRARY_TABLE} WHERE id = %s", (entry_id,))
        if cur.fetchone() is None:
            cur.close()
            return jsonify({'success': False, 'error': '見つかりません'}), 404
        cur.execute(f"""
            UPDATE {LIBRARY_TABLE}
            SET title = %s, description = %s, url = %s, updated_at = %s
            WHERE id = %s
        """, (title, desc, url, get_jst_now(), entry_id))
        conn.commit()
        cur.close()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("migration_assistant api_library_update: %s", e)
        if conn is not None:
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn is not None and conn.is_connected():
            conn.close()


@migration_assistant.route('/api/library/<int:entry_id>', methods=['DELETE'])
def api_library_delete(entry_id):
    if not session.get('user_id'):
        return _need_login_json()
    if not is_instructor():
        return _need_instructor_json()
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(f"DELETE FROM {LIBRARY_TABLE} WHERE id = %s", (entry_id,))
        n = cur.rowcount
        conn.commit()
        cur.close()
        if not n:
            return jsonify({'success': False, 'error': '見つかりません'}), 404
        return jsonify({'success': True})
    except Exception as e:
        logging.error("migration_assistant api_library_delete: %s", e)
        if conn is not None:
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn is not None and conn.is_connected():
            conn.close()


@migration_assistant.route('/api/library/order', methods=['POST'])
def api_library_order():
    """並び順を ids の順に振り直す（10, 20, 30…）"""
    if not session.get('user_id'):
        return _need_login_json()
    if not is_instructor():
        return _need_instructor_json()
    ids = (request.get_json(silent=True) or {}).get('ids')
    if not isinstance(ids, list):
        return jsonify({'success': False, 'error': 'ids は配列で渡してください'}), 400
    try:
        ids = [int(x) for x in ids]
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'ids が不正です'}), 400
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        now = get_jst_now()
        for i, eid in enumerate(ids, 1):
            cur.execute(f"UPDATE {LIBRARY_TABLE} SET sort_order = %s, updated_at = %s WHERE id = %s",
                        (i * 10, now, eid))
        conn.commit()
        cur.close()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("migration_assistant api_library_order: %s", e)
        if conn is not None:
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn is not None and conn.is_connected():
            conn.close()
