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
app_share.manage — アプリ正本の管理画面（段階6a）

正本（app_share_registry / app_share_tables / app_share_sections / app_share_issues）を
1アプリ1ページで編集し，「発行」で fujinp/app_registry.json へ写す．

ページ
  GET  /app_share/app/<app_name>          管理画面（タブ：基本／起動／ランチャ／テーブル／
                                          ライブラリ／定数／文書／版／不具合）
  GET  /app_share/spec/<app_name>         仕様書ビュー（正本＋叙述．ログインユーザ全員）
  GET  /app_share/sections                区画の編集

API（すべて admin．routes.py の before_request が既定で admin 必須にする）
  GET  /app_share/api/app/<app_name>                     管理画面の全データ
  POST /app_share/api/app/<app_name>/basic               基本の保存
  POST /app_share/api/app/<app_name>/blueprints          起動情報の保存
  GET  /app_share/api/app/<app_name>/blueprints/detect   ツリーから Blueprint 定義を検出
  POST /app_share/api/app/<app_name>/launchers           ランチャの保存
  GET  /app_share/api/app/<app_name>/launchers/check     endpoint の解決確認
  GET  /app_share/api/user_groups                        まいぐるのグループ名一覧（使用区分の選択肢）★2026-08-27
  GET  /app_share/api/app/<app_name>/tables/candidates   DB のテーブル一覧（帰属つき）
  POST /app_share/api/app/<app_name>/tables/add          台帳へ追加（DDL を実物から取込）
  POST /app_share/api/app/<app_name>/tables/remove       台帳から外す
  POST /app_share/api/app/<app_name>/tables/confirm      candidate → confirmed
  POST /app_share/api/app/<app_name>/tables/recapture    DDL を実物から取り直す
  GET  /app_share/api/app/<app_name>/tables/compare      台帳の DDL と実物の比較
  POST /app_share/api/app/<app_name>/tables/apply        宣言（CREATE IF NOT EXISTS）と
                                                          改訂（列・索引の追加のみ）を実行
  POST /app_share/api/app/<app_name>/libraries           目録の保存
  GET  /app_share/api/app/<app_name>/libraries/check     import 可否
  GET  /app_share/api/app/<app_name>/libraries/detect    ツリーから候補検出
  POST /app_share/api/app/<app_name>/config_keys         目録の保存
  GET  /app_share/api/app/<app_name>/config_keys/check   Config の有無・プレースホルダ
  GET  /app_share/api/app/<app_name>/config_keys/detect  ツリーから候補検出
  POST /app_share/api/app/<app_name>/issues/add          不具合の登録
  POST /app_share/api/app/<app_name>/issues/<int:id>     不具合の更新
  POST /app_share/api/app/<app_name>/version/confirm     版確定
  GET  /app_share/api/diag/<app_name>                    診断 JSON（アプリ）
  GET  /app_share/api/diag_site                          診断 JSON（サイト）
  GET  /app_share/api/publish/status                     未発行の変更があるか
  POST /app_share/api/publish                            発行（＋任意で Reload）
  GET  /app_share/api/summary                            一覧用の要約（open件数など）
  GET  /app_share/api/sections / POST /app_share/api/sections
"""

import os
import re
import sys
import json
import hashlib
import datetime
import importlib
import importlib.util
import platform
from contextlib import contextmanager

from flask import (render_template, request, jsonify, session, url_for,
                   current_app, Response)
import mysql.connector

from . import app_share_bp
from . import routes as _routes
from config import Config
from db import DatabaseConfig
from decorators import login_required
from fujinp import registry as _reg

BASE_DIR = _routes.BASE_DIR                # fujinp/
SITE_CODE_ROOT = _routes.SITE_CODE_ROOT    # ホーム
JST = datetime.timezone(datetime.timedelta(hours=9))

# 仕様書ビューはログインユーザ全員に見せる（before_request の admin 既定から外す）
_routes._NON_ADMIN_ENDPOINTS = frozenset(set(_routes._NON_ADMIN_ENDPOINTS) | {'spec_view'})

APP_NAME_RE = re.compile(r'^[A-Za-z0-9_]+$')
SCAN_EXCLUDE_DIRS = {'static', '__pycache__', 'import_staging', 'import_backups',
                     'data_for_distribution', '.git'}
CODE_EXTS = ('.py', '.html', '.sql', '.js')
PLATFORM_ROW = '_platform'

PIP_NAME_MAP = {
    'mysql': 'mysql-connector-python', 'MySQLdb': 'mysqlclient', 'flask': 'Flask',
    'flask_mail': 'Flask-Mail', 'authlib': 'Authlib', 'markdown': 'Markdown',
    'pymdownx': 'pymdown-extensions', 'pymysql': 'PyMySQL', 'dotenv': 'python-dotenv',
    'werkzeug': 'Werkzeug', 'jinja2': 'Jinja2', 'yaml': 'PyYAML', 'PIL': 'Pillow',
    'bs4': 'beautifulsoup4', 'dateutil': 'python-dateutil', 'googleapiclient':
    'google-api-python-client', 'google_auth_oauthlib': 'google-auth-oauthlib',
}
_CONFIG_USE_RE = re.compile(
    r'\b[Cc]onfig\.([A-Z][A-Z0-9_]*)'
    r'|\b[Cc]onfig\[[\'"]([A-Z][A-Z0-9_]*)[\'"]\]'
    r'|\b[Cc]onfig\.get\(\s*[\'"]([A-Z][A-Z0-9_]*)[\'"]'
    r'|\bgetattr\(\s*[Cc]onfig\s*,\s*[\'"]([A-Z][A-Z0-9_]*)[\'"]'
)
_BP_DEF_RE = re.compile(r'(\w+)\s*=\s*Blueprint\s*\(\s*[\'"]([\w]+)[\'"]')
_URL_PREFIX_RE = re.compile(r"url_prefix\s*=\s*['\"]([^'\"]+)['\"]")


# ============================================================
# 共通
# ============================================================

def _now():
    return datetime.datetime.now(JST).replace(tzinfo=None)


def _fmt(dt):
    if not dt:
        return None
    if isinstance(dt, str):
        return dt
    return dt.strftime('%Y-%m-%d %H:%M:%S')


@contextmanager
def _db(database=None):
    cfg = DatabaseConfig.default() if database is None else DatabaseConfig.get_config(database)
    conn = mysql.connector.connect(**cfg)
    cur = conn.cursor(dictionary=True, buffered=True)
    try:
        yield cur, conn
    finally:
        cur.close()
        conn.close()


def _jload(v, default):
    if v is None or v == '':
        return default
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return default


def _jdump(v):
    return json.dumps(v, ensure_ascii=False)


def _ok(**kw):
    kw.setdefault('success', True)
    return jsonify(kw)


def _err(msg, code=400):
    return jsonify({'success': False, 'error': msg}), code


def _valid_app(app_name):
    return bool(app_name and APP_NAME_RE.match(app_name))


def _db_names():
    """{'default': 実DB名, 'fujinp': 実DB名, 'public': 実DB名}（存在するものだけ）"""
    out = {}
    for suf, fn in (('default', DatabaseConfig.default),
                    ('fujinp', DatabaseConfig.fujinp),
                    ('public', DatabaseConfig.public)):
        try:
            n = fn().get('database')
            if n and n not in out.values():
                out[suf] = n
        except Exception:
            pass
    return out


def _app_path(app_name):
    return os.path.join(BASE_DIR, app_name)


def _walk_code(app_dir):
    for root, dirs, files in os.walk(app_dir):
        dirs[:] = [d for d in dirs if d not in SCAN_EXCLUDE_DIRS and not d.startswith('.')]
        for fn in sorted(files):
            if fn.endswith('.pyc'):
                continue
            yield os.path.join(root, fn)


def _read(p):
    try:
        with open(p, 'r', encoding='utf-8', errors='ignore') as f:
            return f.read()
    except Exception:
        return ''


def _load_registry_row(cur, app_name):
    cur.execute("SELECT * FROM app_share_registry WHERE app_name=%s", (app_name,))
    return cur.fetchone()


def _touch(cur, app_name):
    """updated_at を進める（ON UPDATE が効かない JSON 列だけの更新にも版を刻む）"""
    cur.execute("UPDATE app_share_registry SET updated_at=UTC_TIMESTAMP() WHERE app_name=%s", (app_name,))


# ============================================================
# ページ
# ============================================================

@app_share_bp.route('/app/<app_name>')
@login_required
def manage_page(app_name):
    if not _valid_app(app_name):
        return "アプリ名が不正です", 400
    with _db() as (cur, conn):
        row = _load_registry_row(cur, app_name)
    if not row:
        return f"アプリ「{app_name}」はレジストリにありません", 404
    return render_template('app_share_manage.html',
                           app_name=app_name,
                           display_name=row.get('display_name') or app_name,
                           icon=row.get('icon') or '📦')


@app_share_bp.route('/sections')
@login_required
def sections_page():
    return render_template('app_share_sections.html')


@app_share_bp.route('/spec/<app_name>')
@login_required
def spec_view(app_name):
    """仕様書ビュー：正本（起動・ランチャ・テーブル・ライブラリ・定数）＋叙述（DBの仕様書メモ）"""
    if not _valid_app(app_name):
        return "アプリ名が不正です", 400
    with _db() as (cur, conn):
        row = _load_registry_row(cur, app_name)
        if not row:
            return f"アプリ「{app_name}」はレジストリにありません", 404
        cur.execute("""SELECT table_name, db_target, ddl, status, captured_at, note
                       FROM app_share_tables WHERE app_name=%s ORDER BY sort_order, table_name""",
                    (app_name,))
        tables = cur.fetchall()
        cur.execute("""SELECT d.title, d.content, d.updated_at, u.full_name AS updated_by_name
                       FROM app_share_documents d LEFT JOIN users u ON d.updated_by=u.id
                       WHERE d.app_name=%s AND d.doc_type='spec'""", (app_name,))
        spec = cur.fetchone() or {}
        cur.execute("""SELECT id, title, status, reported_at FROM app_share_issues
                       WHERE app_name=%s AND status='open' ORDER BY reported_at DESC""", (app_name,))
        issues = cur.fetchall()
    is_admin = _routes.check_admin_permission(session.get('user_id'))
    for t in tables:
        t['captured_at'] = _fmt(t.get('captured_at'))
    for i in issues:
        i['reported_at'] = _fmt(i.get('reported_at'))
    return render_template('app_share_spec_view.html',
                           app_name=app_name,
                           row=row,
                           blueprints=_jload(row.get('blueprints'), []),
                           launchers=_jload(row.get('launchers'), []),
                           libraries=_jload(row.get('libraries'), []),
                           config_keys=_jload(row.get('config_keys'), []),
                           tables=tables,
                           spec_content=spec.get('content') or '',
                           spec_updated_at=_routes._fmt_jst(spec.get('updated_at')) if spec else None,
                           spec_updated_by=spec.get('updated_by_name') if spec else None,
                           issues=issues,
                           version_id=row.get('version_id'),
                           is_admin=is_admin)


# ============================================================
# 管理画面の全データ
# ============================================================

def _runtime_blueprints():
    """起動中の Flask に登録されている Blueprint 名の集合"""
    try:
        return set(current_app.blueprints.keys())
    except Exception:
        return set()


def _docs_status(cur, app_name):
    cur.execute("""SELECT d.doc_type, d.title, d.updated_at, LENGTH(d.content) AS len,
                          u.full_name AS updated_by_name
                   FROM app_share_documents d LEFT JOIN users u ON d.updated_by=u.id
                   WHERE d.app_name=%s""", (app_name,))
    out = {}
    for r in cur.fetchall():
        out[r['doc_type']] = {'title': r['title'] or '', 'length': r['len'] or 0,
                              'updated_at': _routes._fmt_jst(r['updated_at']),
                              'updated_by': r['updated_by_name']}
    return out


def _issues(cur, app_name, status=None):
    if status:
        cur.execute("""SELECT * FROM app_share_issues WHERE app_name=%s AND status=%s
                       ORDER BY reported_at DESC""", (app_name, status))
    else:
        cur.execute("""SELECT * FROM app_share_issues WHERE app_name=%s
                       ORDER BY FIELD(status,'open','fixed','wontfix'), reported_at DESC""", (app_name,))
    rows = cur.fetchall()
    for r in rows:
        r['reported_at'] = _fmt(r.get('reported_at'))
        r['fixed_at'] = _fmt(r.get('fixed_at'))
    return rows


@app_share_bp.route('/api/app/<app_name>')
@login_required
def api_app(app_name):
    if not _valid_app(app_name):
        return _err('アプリ名が不正です')
    with _db() as (cur, conn):
        row = _load_registry_row(cur, app_name)
        if not row:
            return _err('レジストリにありません', 404)
        cur.execute("""SELECT id, table_name, db_target, status, captured_at, note, sort_order,
                              LENGTH(ddl) AS ddl_len
                       FROM app_share_tables WHERE app_name=%s ORDER BY sort_order, table_name""",
                    (app_name,))
        tables = cur.fetchall()
        for t in tables:
            t['captured_at'] = _fmt(t['captured_at'])
        docs = _docs_status(cur, app_name)
        issues = _issues(cur, app_name)
    rt = _runtime_blueprints()
    bps = _jload(row.get('blueprints'), [])
    for b in bps:
        b['registered'] = bool(b.get('name') and b['name'] in rt)
    app_dir = _app_path(app_name)
    return _ok(app={
        'app_name': app_name,
        'display_name': row.get('display_name') or '',
        'icon': row.get('icon') or '📦',
        'description': row.get('description') or '',
        'kind': row.get('kind') or 'app',
        'enabled': bool(row.get('enabled', 1)),
        'sort_order': float(row.get('sort_order') or 0),
        'updated_at': _routes._fmt_jst(row.get('updated_at')),
        'published_at': _fmt(row.get('published_at')),
        'version_id': row.get('version_id'),
        'version_confirmed_at': _fmt(row.get('version_confirmed_at')),
        'version_confirmed_by': row.get('version_confirmed_by'),
        'git_commit': row.get('git_commit'),
        'git_committed_at': _fmt(row.get('git_committed_at')),
        'dir_exists': os.path.isdir(app_dir),
        'blueprints': bps,
        'launchers': _jload(row.get('launchers'), []),
        'libraries': _jload(row.get('libraries'), []),
        'config_keys': _jload(row.get('config_keys'), []),
        'tables': tables,
        'docs': docs,
        'issues': issues,
    })


# ============================================================
# 基本
# ============================================================

@app_share_bp.route('/api/app/<app_name>/basic', methods=['POST'])
@login_required
def api_basic(app_name):
    if not _valid_app(app_name):
        return _err('アプリ名が不正です')
    d = request.get_json(silent=True) or {}
    kind = d.get('kind') if d.get('kind') in ('app', 'kernel') else 'app'
    try:
        sort_order = float(d.get('sort_order') or 0)
    except (TypeError, ValueError):
        sort_order = 0
    with _db() as (cur, conn):
        cur.execute("""UPDATE app_share_registry
                       SET display_name=%s, icon=%s, description=%s, kind=%s, enabled=%s, sort_order=%s
                       WHERE app_name=%s""",
                    ((d.get('display_name') or app_name).strip(), (d.get('icon') or '📦').strip(),
                     d.get('description') or '', kind, 1 if d.get('enabled', True) else 0,
                     sort_order, app_name))
        conn.commit()
    return _ok()


# ============================================================
# 起動（Blueprint）
# ============================================================

def _detect_blueprints(app_name):
    app_dir = _app_path(app_name)
    found = []
    if not os.path.isdir(app_dir):
        return found
    for p in _walk_code(app_dir):
        if not p.endswith('.py'):
            continue
        rel = os.path.relpath(p, app_dir).replace(os.sep, '/')
        parts = rel[:-3].split('/')
        if parts[-1] == '__init__':
            parts = parts[:-1]
        module = '.'.join(['fujinp', app_name] + parts)
        text = _read(p)
        for m in _BP_DEF_RE.finditer(text):
            tail = text[m.end(): m.end() + 600]
            seg = tail.split(')')[0] if ')' in tail else tail
            um = _URL_PREFIX_RE.search(seg)
            found.append({'module': module, 'attr': m.group(1), 'name': m.group(2),
                          'url_prefix': None,
                          'defined_prefix': um.group(1) if um else None,
                          'file': rel})
    return found


@app_share_bp.route('/api/app/<app_name>/blueprints/detect')
@login_required
def api_bp_detect(app_name):
    if not _valid_app(app_name):
        return _err('アプリ名が不正です')
    return _ok(found=_detect_blueprints(app_name))


@app_share_bp.route('/api/app/<app_name>/blueprints', methods=['POST'])
@login_required
def api_bp_save(app_name):
    if not _valid_app(app_name):
        return _err('アプリ名が不正です')
    d = request.get_json(silent=True) or {}
    items = []
    for b in d.get('blueprints') or []:
        module = (b.get('module') or '').strip()
        attr = (b.get('attr') or '').strip()
        if not module or not attr:
            continue
        if not re.match(r'^[A-Za-z_][\w.]*$', module) or not re.match(r'^\w+$', attr):
            return _err(f'module / attr が不正です: {module} {attr}')
        up = (b.get('url_prefix') or '').strip() or None
        if up and not up.startswith('/'):
            return _err(f'url_prefix は / で始めてください: {up}')
        items.append({'module': module, 'attr': attr,
                      'name': (b.get('name') or '').strip() or None, 'url_prefix': up})
    with _db() as (cur, conn):
        cur.execute("UPDATE app_share_registry SET blueprints=%s WHERE app_name=%s",
                    (_jdump(items), app_name))
        _touch(cur, app_name)
        conn.commit()
    return _ok(blueprints=items)


# ============================================================
# ランチャ
# ============================================================

@app_share_bp.route('/api/app/<app_name>/launchers', methods=['POST'])
@login_required
def api_launchers_save(app_name):
    """ランチャの保存．★2026-08-27 使用コントローラー：
    visibility（使用区分）・groups（グループ名）・params（url_for の引数 'k=v&k=v'）を持ち，
    require_groups / require_categories は廃止．dashboards は配置（admin/guest）だけを表す．"""
    if not _valid_app(app_name):
        return _err('アプリ名が不正です')
    d = request.get_json(silent=True) or {}
    items = []
    for i, c in enumerate(d.get('launchers') or []):
        ep = (c.get('endpoint') or '').strip()
        # 'static' だけは Blueprint なしを許す（静的ファイルを開くカード．params に filename=…）
        if not ep or not (re.match(r'^[\w]+\.[\w]+$', ep) or ep == 'static'):
            return _err(f'endpoint は "blueprint名.関数名" の形です（静的ファイルは static）: {ep!r}')
        dashboards = [x for x in (c.get('dashboards') or []) if x in ('admin', 'guest')]
        vis = (c.get('visibility') or 'private').strip()
        if vis not in _reg.VISIBILITY_KEYS:
            return _err(f'カード「{c.get("label")}」の使用区分が不正です: {vis!r}')
        if not dashboards and vis != 'open':
            return _err(f'カード「{c.get("label")}」の配置（admin／guest）を1つ以上選んでください'
                        f'（配置なしでよいのは「公開（ログイン不要）」だけです）')
        groups = [g.strip() for g in (c.get('groups') or []) if g and g.strip()]
        if vis in ('group', 'domestic_group') and not groups:
            return _err(f'カード「{c.get("label")}」はグループを1つ以上選んでください')
        if vis not in ('group', 'domestic_group'):
            groups = []
        params = (c.get('params') or '').strip()
        if params and not re.match(r'^[\w]+=[^&]*(&[\w]+=[^&]*)*$', params):
            return _err(f'カード「{c.get("label")}」の params は k=v&k=v の形です: {params!r}')
        if ep == 'static' and 'filename=' not in params:
            return _err(f'カード「{c.get("label")}」は static なので params に filename=… が必要です')
        try:
            so = int(c.get('sort_order') if c.get('sort_order') not in (None, '') else (i + 1) * 10)
        except (TypeError, ValueError):
            so = (i + 1) * 10
        items.append({
            'dashboards': dashboards,
            'section': (c.get('section') or 'guest').strip(),
            'endpoint': ep,
            'params': params,
            'label': (c.get('label') or '').strip(),
            'icon': (c.get('icon') or '').strip(),
            'description': (c.get('description') or '').strip(),
            'sort_order': so,
            'visibility': vis,
            'groups': groups,
            'extra_class': (c.get('extra_class') or '').strip(),
        })
    with _db() as (cur, conn):
        cur.execute("UPDATE app_share_registry SET launchers=%s WHERE app_name=%s",
                    (_jdump(items), app_name))
        _touch(cur, app_name)
        conn.commit()
    return _ok(launchers=items)


@app_share_bp.route('/api/user_groups')
@login_required
def api_user_groups():
    """まいぐるのグループ名一覧（使用区分 group / domestic_group の選択肢）"""
    try:
        with _db() as (cur, conn):
            cur.execute("SELECT id, name FROM user_groups ORDER BY name")
            rows = cur.fetchall()
    except Exception as e:
        return _ok(groups=[], note=f'user_groups を読めません: {e}')
    return _ok(groups=[{'id': r['id'], 'name': r['name']} for r in rows],
               visibility_keys=list(_reg.VISIBILITY_KEYS),
               visibility_labels=_reg.VISIBILITY_LABELS)


@app_share_bp.route('/api/app/<app_name>/launchers/check')
@login_required
def api_launchers_check(app_name):
    if not _valid_app(app_name):
        return _err('アプリ名が不正です')
    with _db() as (cur, conn):
        row = _load_registry_row(cur, app_name)
    out = []
    for c in _jload((row or {}).get('launchers'), []):
        try:
            href = url_for(c['endpoint'], **_reg._parse_params(c.get('params')))
            out.append({'endpoint': c['endpoint'], 'ok': True, 'href': href})
        except Exception as e:
            out.append({'endpoint': c.get('endpoint'), 'ok': False, 'error': str(e)})
    return _ok(results=out)


@app_share_bp.route('/api/sections', methods=['GET'])
@login_required
def api_sections_get():
    with _db() as (cur, conn):
        cur.execute("SELECT * FROM app_share_sections ORDER BY sort_order")
        rows = cur.fetchall()
    for r in rows:
        r['require_groups'] = _jload(r.get('require_groups'), [])
        r['require_categories'] = _jload(r.get('require_categories'), [])
        r['sort_order'] = float(r.get('sort_order') or 0)
    return _ok(sections=rows)


@app_share_bp.route('/api/sections', methods=['POST'])
@login_required
def api_sections_save():
    d = request.get_json(silent=True) or {}
    secs = d.get('sections') or []
    with _db() as (cur, conn):
        keep = []
        for s in secs:
            key = (s.get('section_key') or '').strip()
            if not re.match(r'^[a-z0-9_]+$', key):
                return _err(f'section_key は英小文字・数字・_ のみ: {key!r}')
            keep.append(key)
            # ★2026-08-27 区画側の表示条件は廃止（見出し・色・順だけ）．
            #   列は互換のため残し，常に「制限なし」で書く
            cur.execute("""INSERT INTO app_share_sections
                (section_key, title, css_class, sort_order, show_admin, show_guest,
                 require_groups, require_categories)
                VALUES (%s,%s,%s,%s,1,1,'[]','[]')
                ON DUPLICATE KEY UPDATE title=VALUES(title), css_class=VALUES(css_class),
                    sort_order=VALUES(sort_order), show_admin=1, show_guest=1,
                    require_groups='[]', require_categories='[]'""",
                (key, (s.get('title') or key).strip(), (s.get('css_class') or '').strip(),
                 float(s.get('sort_order') or 0)))
        if keep:
            fmt = ','.join(['%s'] * len(keep))
            cur.execute(f"DELETE FROM app_share_sections WHERE section_key NOT IN ({fmt})", tuple(keep))
        conn.commit()
    return _ok()


# ============================================================
# テーブル
# ============================================================

def _show_create(dbname, table):
    cfg = DatabaseConfig.get_config(dbname)
    conn = mysql.connector.connect(**cfg)
    try:
        cu = conn.cursor(dictionary=True, buffered=True)
        cu.execute(f"SHOW CREATE TABLE `{table}`")
        row = cu.fetchone()
        cu.close()
        if not row:
            return None
        return row.get('Create Table') or row.get('Create View') or ''
    finally:
        conn.close()


def _list_tables(dbname):
    cfg = DatabaseConfig.get_config(dbname)
    conn = mysql.connector.connect(**cfg)
    try:
        cu = conn.cursor()
        cu.execute("SHOW TABLES")
        return [r[0] for r in cu.fetchall()]
    finally:
        conn.close()


def _parse_columns(ddl):
    cols = {}
    order = []
    for line in (ddl or '').splitlines():
        s = line.strip().rstrip(',')
        m = re.match(r'^`([^`]+)`\s+(.+)$', s)
        if m:
            cols[m.group(1)] = re.sub(r'\s+', ' ', m.group(2)).strip()
            order.append(m.group(1))
    return cols, order


def _parse_indexes(ddl):
    """{索引名: 定義行}．PRIMARY KEY は 'PRIMARY'，CONSTRAINT は 'FK:名前'"""
    idx = {}
    for line in (ddl or '').splitlines():
        s = line.strip().rstrip(',')
        if s.startswith('PRIMARY KEY'):
            idx['PRIMARY'] = s
        elif re.match(r'^(UNIQUE KEY|KEY|INDEX|FULLTEXT KEY|SPATIAL KEY)\s+`', s):
            m = re.search(r'`([^`]+)`', s)
            if m:
                idx[m.group(1)] = s
        elif s.startswith('CONSTRAINT'):
            m = re.search(r'`([^`]+)`', s)
            if m:
                idx['FK:' + m.group(1)] = s
    return idx


def _norm(d):
    s = re.sub(r'\s+', ' ', d or '').strip()
    s = re.sub(r'\bCHARACTER SET (\w+)\s+(?=COLLATE \1_)', '', s, flags=re.I)
    # 整数型の表示幅（MariaDB や MySQL 5.7 は int(11) と出す．MySQL 8 は int）
    s = re.sub(r'\b((?:tiny|small|medium|big)?int)\(\d+\)', r'\1', s, flags=re.I)
    return s.lower()


def _compare(ddl, live_ddl):
    """台帳の DDL と実物の差．戻り値 dict（status: same|diff）"""
    pc, porder = _parse_columns(ddl)
    lc, _ = _parse_columns(live_ddl)
    pi = _parse_indexes(ddl)
    li = _parse_indexes(live_ddl)
    added = []
    for i, name in enumerate(porder):
        if name not in lc:
            after = None
            for prev in reversed(porder[:i]):
                if prev in lc or any(a['name'] == prev for a in added):
                    after = prev
                    break
            added.append({'name': name, 'definition': pc[name], 'after': after})
    changed = [{'name': n, 'declared': pc[n], 'live': lc[n]}
               for n in porder if n in lc and _norm(pc[n]) != _norm(lc[n])]
    live_only = [n for n in lc if n not in pc]
    idx_added = [{'name': n, 'definition': pi[n]} for n in pi
                 if n not in li and not n.startswith('FK:') and n != 'PRIMARY']
    fk_added = [{'name': n, 'definition': pi[n]} for n in pi if n.startswith('FK:') and n not in li]
    status = 'same' if not (added or changed or idx_added) else 'diff'
    return {'status': status, 'added_columns': added, 'changed_columns': changed,
            'live_only_columns': live_only, 'added_indexes': idx_added,
            'fk_only_in_declared': fk_added}


def _create_sql(ddl):
    s = re.sub(r'\s+AUTO_INCREMENT=\d+', '', ddl or '')
    return re.sub(r'^\s*CREATE TABLE\s+(?!IF NOT EXISTS)', 'CREATE TABLE IF NOT EXISTS ', s, count=1)


def _alter_sqls(table, cmp):
    out = []
    for a in cmp['added_columns']:
        pos = f" AFTER `{a['after']}`" if a.get('after') else ''
        out.append(f"ALTER TABLE `{table}` ADD COLUMN `{a['name']}` {a['definition']}{pos};")
    for i in cmp['added_indexes']:
        out.append(f"ALTER TABLE `{table}` ADD {i['definition']};")
    return out


def _table_ownership(cur):
    """{テーブル名: [app_name, ...]}（台帳上の帰属）"""
    cur.execute("SELECT app_name, table_name, status FROM app_share_tables")
    own = {}
    for r in cur.fetchall():
        own.setdefault(r['table_name'], []).append({'app_name': r['app_name'], 'status': r['status']})
    return own


@app_share_bp.route('/api/app/<app_name>/tables/candidates')
@login_required
def api_tables_candidates(app_name):
    if not _valid_app(app_name):
        return _err('アプリ名が不正です')
    dbs = _db_names()
    with _db() as (cur, conn):
        own = _table_ownership(cur)
    out = []
    for suf, dbname in dbs.items():
        try:
            names = _list_tables(dbname)
        except Exception as e:
            out.append({'db_target': suf, 'error': str(e)})
            continue
        for t in names:
            owners = own.get(t, [])
            out.append({'db_target': suf, 'table_name': t,
                        'prefix_match': t.startswith(app_name + '_') or t == app_name,
                        'owned_by_me': any(o['app_name'] == app_name for o in owners),
                        'owners': [o['app_name'] for o in owners if o['app_name'] != app_name]})
    out.sort(key=lambda x: (not x.get('prefix_match', False), x.get('db_target', ''), x.get('table_name', '')))
    return _ok(candidates=out, databases=dbs)


@app_share_bp.route('/api/app/<app_name>/tables/add', methods=['POST'])
@login_required
def api_tables_add(app_name):
    if not _valid_app(app_name):
        return _err('アプリ名が不正です')
    d = request.get_json(silent=True) or {}
    table = (d.get('table_name') or '').strip()
    suf = d.get('db_target') or 'default'
    if not re.match(r'^[\w$]+$', table):
        return _err('テーブル名が不正です')
    dbs = _db_names()
    if suf not in dbs:
        return _err(f'DB {suf} は設定されていません')
    ddl = _show_create(dbs[suf], table)
    if ddl is None:
        return _err(f'{dbs[suf]} に {table} がありません', 404)
    with _db() as (cur, conn):
        cur.execute("SELECT COALESCE(MAX(sort_order),0)+10 AS n FROM app_share_tables WHERE app_name=%s",
                    (app_name,))
        nxt = cur.fetchone()['n']
        cur.execute("""INSERT INTO app_share_tables
            (app_name, table_name, db_target, ddl, captured_at, status, sort_order)
            VALUES (%s,%s,%s,%s,%s,'confirmed',%s)
            ON DUPLICATE KEY UPDATE ddl=VALUES(ddl), captured_at=VALUES(captured_at),
                db_target=VALUES(db_target), status='confirmed'""",
            (app_name, table, suf, ddl, _now(), nxt))
        _touch(cur, app_name)
        conn.commit()
    return _ok()


@app_share_bp.route('/api/app/<app_name>/tables/remove', methods=['POST'])
@login_required
def api_tables_remove(app_name):
    if not _valid_app(app_name):
        return _err('アプリ名が不正です')
    d = request.get_json(silent=True) or {}
    with _db() as (cur, conn):
        cur.execute("DELETE FROM app_share_tables WHERE app_name=%s AND table_name=%s",
                    (app_name, d.get('table_name') or ''))
        n = cur.rowcount
        _touch(cur, app_name)
        conn.commit()
    return _ok(removed=n)


@app_share_bp.route('/api/app/<app_name>/tables/confirm', methods=['POST'])
@login_required
def api_tables_confirm(app_name):
    if not _valid_app(app_name):
        return _err('アプリ名が不正です')
    d = request.get_json(silent=True) or {}
    with _db() as (cur, conn):
        cur.execute("UPDATE app_share_tables SET status='confirmed' WHERE app_name=%s AND table_name=%s",
                    (app_name, d.get('table_name') or ''))
        conn.commit()
    return _ok()


@app_share_bp.route('/api/app/<app_name>/tables/recapture', methods=['POST'])
@login_required
def api_tables_recapture(app_name):
    """台帳の DDL を実物から取り直す（table_name 指定なら1件，無ければ全部）"""
    if not _valid_app(app_name):
        return _err('アプリ名が不正です')
    d = request.get_json(silent=True) or {}
    only = (d.get('table_name') or '').strip() or None
    dbs = _db_names()
    results = []
    with _db() as (cur, conn):
        cur.execute("SELECT table_name, db_target FROM app_share_tables WHERE app_name=%s", (app_name,))
        rows = [r for r in cur.fetchall() if not only or r['table_name'] == only]
        for r in rows:
            dbname = dbs.get(r['db_target'])
            ddl = _show_create(dbname, r['table_name']) if dbname else None
            if ddl is None:
                results.append({'table_name': r['table_name'], 'ok': False, 'error': '実物が無い'})
                continue
            cur.execute("""UPDATE app_share_tables SET ddl=%s, captured_at=%s
                           WHERE app_name=%s AND table_name=%s""",
                        (ddl, _now(), app_name, r['table_name']))
            results.append({'table_name': r['table_name'], 'ok': True})
        _touch(cur, app_name)
        conn.commit()
    return _ok(results=results)


def _compare_app_tables(cur, app_name):
    dbs = _db_names()
    cur.execute("""SELECT table_name, db_target, ddl, status FROM app_share_tables
                   WHERE app_name=%s ORDER BY sort_order, table_name""", (app_name,))
    out = []
    for r in cur.fetchall():
        dbname = dbs.get(r['db_target'])
        item = {'table_name': r['table_name'], 'db_target': r['db_target'],
                'database': dbname, 'ledger_status': r['status']}
        if not dbname:
            item.update({'status': 'nodb'})
        else:
            try:
                live = _show_create(dbname, r['table_name'])
            except Exception as e:
                live = None
                item['error'] = str(e)
            if live is None:
                item.update({'status': 'missing', 'create_sql': _create_sql(r['ddl'])})
            else:
                cmp = _compare(r['ddl'], live)
                item.update(cmp)
                item['alter_sqls'] = _alter_sqls(r['table_name'], cmp)
        out.append(item)
    return out


@app_share_bp.route('/api/app/<app_name>/tables/compare')
@login_required
def api_tables_compare(app_name):
    if not _valid_app(app_name):
        return _err('アプリ名が不正です')
    with _db() as (cur, conn):
        out = _compare_app_tables(cur, app_name)
    return _ok(tables=out)


@app_share_bp.route('/api/app/<app_name>/tables/apply', methods=['POST'])
@login_required
def api_tables_apply(app_name):
    """宣言と改訂の実行．足すだけ（CREATE IF NOT EXISTS／ADD COLUMN／ADD INDEX）．
    型変更・削除・改名・外部キーは実行しない（比較結果に載せるだけ）．"""
    if not _valid_app(app_name):
        return _err('アプリ名が不正です')
    d = request.get_json(silent=True) or {}
    only = set(d.get('tables') or [])
    with _db() as (cur, conn):
        plan = _compare_app_tables(cur, app_name)
    executed = []
    for item in plan:
        if only and item['table_name'] not in only:
            continue
        sqls = []
        if item.get('status') == 'missing':
            sqls = [item['create_sql']]
        elif item.get('status') == 'diff':
            sqls = item.get('alter_sqls') or []
        if not sqls:
            continue
        cfg = DatabaseConfig.get_config(item['database'])
        conn2 = mysql.connector.connect(**cfg)
        try:
            cu = conn2.cursor()
            for s in sqls:
                try:
                    cu.execute(s)
                    executed.append({'table_name': item['table_name'], 'sql': s, 'ok': True})
                except Exception as e:
                    executed.append({'table_name': item['table_name'], 'sql': s, 'ok': False,
                                     'error': str(e)})
            conn2.commit()
            cu.close()
        finally:
            conn2.close()
    return _ok(executed=executed)


# ============================================================
# ライブラリ・定数
# ============================================================

def _detect_libraries(app_name):
    app_dir = _app_path(app_name)
    mods = set()
    for p in _walk_code(app_dir) if os.path.isdir(app_dir) else []:
        if not p.endswith('.py'):
            continue
        for line in _read(p).splitlines():
            m = re.match(r'^\s*from\s+([A-Za-z_][\w\.]*)\s+import\b', line)
            if m:
                mods.add(m.group(1).split('.')[0]); continue
            m = re.match(r'^\s*import\s+([^#]+)', line)
            if m:
                for part in m.group(1).split(','):
                    nm = part.strip().split(' as ')[0].strip()
                    if nm and re.match(r'^[A-Za-z_][\w\.]*$', nm):
                        mods.add(nm.split('.')[0])
    stdlib = set(getattr(sys, 'stdlib_module_names', ()))
    libs = []
    for name in sorted(mods):
        if name in stdlib or name in ('fujinp', app_name):
            continue
        local = (os.path.isfile(os.path.join(SITE_CODE_ROOT, name + '.py')) or
                 os.path.isdir(os.path.join(SITE_CODE_ROOT, name)) or
                 os.path.isfile(os.path.join(BASE_DIR, name + '.py')) or
                 os.path.isdir(os.path.join(BASE_DIR, name)))
        libs.append({'name': name if local else PIP_NAME_MAP.get(name, name), 'import': name,
                     'kind': 'local' if local else 'pip', 'status': 'candidate', 'note': ''})
    return libs


def _check_libraries(libs):
    out = []
    for l in libs:
        imp = l.get('import') or l.get('name')
        try:
            ok = importlib.util.find_spec(str(imp)) is not None
        except Exception:
            ok = False
        out.append({'name': l.get('name'), 'import': imp, 'kind': l.get('kind'), 'installed': ok})
    return out


def _detect_config_keys(app_name):
    app_dir = _app_path(app_name)
    keys = set()
    for p in _walk_code(app_dir) if os.path.isdir(app_dir) else []:
        if not p.endswith(('.py', '.html')):
            continue
        for m in _CONFIG_USE_RE.finditer(_read(p)):
            keys.add(next(g for g in m.groups() if g))
    return [{'name': k, 'required': True, 'status': 'candidate', 'note': ''} for k in sorted(keys)]


def _check_config_keys(keys):
    out = []
    for k in keys:
        name = k.get('name')
        exists = hasattr(Config, name) if name else False
        placeholder = False
        empty = False
        if exists:
            v = getattr(Config, name)
            if isinstance(v, str):
                placeholder = v.startswith('<YOUR_') or '<YOUR_' in v
                empty = (v.strip() == '')
            elif v is None:
                empty = True
        out.append({'name': name, 'exists': exists, 'placeholder': placeholder, 'empty': empty,
                    'required': bool(k.get('required', True))})
    return out


def _save_json_col(app_name, col, items):
    with _db() as (cur, conn):
        cur.execute(f"UPDATE app_share_registry SET {col}=%s WHERE app_name=%s",
                    (_jdump(items), app_name))
        _touch(cur, app_name)
        conn.commit()


@app_share_bp.route('/api/app/<app_name>/libraries', methods=['POST'])
@login_required
def api_libs_save(app_name):
    if not _valid_app(app_name):
        return _err('アプリ名が不正です')
    d = request.get_json(silent=True) or {}
    items = []
    for l in d.get('libraries') or []:
        name = (l.get('name') or '').strip()
        if not name:
            continue
        items.append({'name': name, 'import': (l.get('import') or name).strip(),
                      'kind': 'local' if l.get('kind') == 'local' else 'pip',
                      'status': 'confirmed' if l.get('status') == 'confirmed' else 'candidate',
                      'note': (l.get('note') or '').strip()})
    _save_json_col(app_name, 'libraries', items)
    return _ok(libraries=items)


@app_share_bp.route('/api/app/<app_name>/libraries/detect')
@login_required
def api_libs_detect(app_name):
    if not _valid_app(app_name):
        return _err('アプリ名が不正です')
    return _ok(found=_detect_libraries(app_name))


@app_share_bp.route('/api/app/<app_name>/libraries/check')
@login_required
def api_libs_check(app_name):
    if not _valid_app(app_name):
        return _err('アプリ名が不正です')
    with _db() as (cur, conn):
        row = _load_registry_row(cur, app_name)
    return _ok(results=_check_libraries(_jload((row or {}).get('libraries'), [])))


@app_share_bp.route('/api/app/<app_name>/config_keys', methods=['POST'])
@login_required
def api_cfg_save(app_name):
    if not _valid_app(app_name):
        return _err('アプリ名が不正です')
    d = request.get_json(silent=True) or {}
    items = []
    for k in d.get('config_keys') or []:
        name = (k.get('name') or '').strip()
        if not re.match(r'^[A-Z][A-Z0-9_]*$', name):
            return _err(f'定数名が不正です: {name!r}')
        items.append({'name': name, 'required': bool(k.get('required', True)),
                      'status': 'confirmed' if k.get('status') == 'confirmed' else 'candidate',
                      'note': (k.get('note') or '').strip()})
    _save_json_col(app_name, 'config_keys', items)
    return _ok(config_keys=items)


@app_share_bp.route('/api/app/<app_name>/config_keys/detect')
@login_required
def api_cfg_detect(app_name):
    if not _valid_app(app_name):
        return _err('アプリ名が不正です')
    return _ok(found=_detect_config_keys(app_name))


@app_share_bp.route('/api/app/<app_name>/config_keys/check')
@login_required
def api_cfg_check(app_name):
    if not _valid_app(app_name):
        return _err('アプリ名が不正です')
    with _db() as (cur, conn):
        row = _load_registry_row(cur, app_name)
    return _ok(results=_check_config_keys(_jload((row or {}).get('config_keys'), [])))


# ============================================================
# 不具合
# ============================================================

@app_share_bp.route('/api/app/<app_name>/issues/add', methods=['POST'])
@login_required
def api_issue_add(app_name):
    if not _valid_app(app_name):
        return _err('アプリ名が不正です')
    d = request.get_json(silent=True) or {}
    title = (d.get('title') or '').strip()
    if not title:
        return _err('件名を入れてください')
    with _db() as (cur, conn):
        cur.execute("""INSERT INTO app_share_issues
            (app_name, title, detail, status, reported_at, reported_by, note)
            VALUES (%s,%s,%s,'open',%s,%s,%s)""",
            (app_name, title[:500], d.get('detail') or '', _now(), session.get('user_id'),
             (d.get('note') or '')[:500]))
        new_id = cur.lastrowid
        conn.commit()
    return _ok(id=new_id)


@app_share_bp.route('/api/app/<app_name>/issues/<int:issue_id>', methods=['POST'])
@login_required
def api_issue_update(app_name, issue_id):
    if not _valid_app(app_name):
        return _err('アプリ名が不正です')
    d = request.get_json(silent=True) or {}
    sets, vals = [], []
    if 'title' in d:
        sets.append('title=%s'); vals.append((d['title'] or '').strip()[:500])
    if 'detail' in d:
        sets.append('detail=%s'); vals.append(d['detail'] or '')
    if 'note' in d:
        sets.append('note=%s'); vals.append((d['note'] or '')[:500])
    if 'status' in d:
        st = d['status']
        if st not in ('open', 'fixed', 'wontfix'):
            return _err('status は open / fixed / wontfix')
        sets.append('status=%s'); vals.append(st)
        if st == 'fixed':
            with _db() as (cur, conn):
                row = _load_registry_row(cur, app_name)
            sets.append('fixed_at=%s'); vals.append(_now())
            sets.append('fixed_version_id=%s'); vals.append((row or {}).get('version_id'))
        elif st == 'open':
            sets.append('fixed_at=NULL'); sets.append('fixed_version_id=NULL')
    if not sets:
        return _err('変更がありません')
    vals += [issue_id, app_name]
    with _db() as (cur, conn):
        cur.execute(f"UPDATE app_share_issues SET {', '.join(sets)} WHERE id=%s AND app_name=%s", tuple(vals))
        n = cur.rowcount
        conn.commit()
    return _ok(updated=n)


@app_share_bp.route('/api/app/<app_name>/issues/<int:issue_id>/delete', methods=['POST'])
@login_required
def api_issue_delete(app_name, issue_id):
    if not _valid_app(app_name):
        return _err('アプリ名が不正です')
    with _db() as (cur, conn):
        cur.execute("DELETE FROM app_share_issues WHERE id=%s AND app_name=%s", (issue_id, app_name))
        n = cur.rowcount
        conn.commit()
    return _ok(deleted=n)


# ============================================================
# 版
# ============================================================

def _content_hash6(app_name):
    app_path = _app_path(app_name)
    parts = []
    if os.path.isdir(app_path):
        for root, dirs, filenames in os.walk(app_path):
            dirs[:] = [d for d in dirs if d not in ('static', '__pycache__', 'import_staging', 'import_backups')]
            for fn in sorted(filenames):
                if fn.endswith('.pyc') or fn == 'version.json':
                    continue
                fp = os.path.join(root, fn)
                rel = os.path.relpath(fp, app_path).replace(os.sep, '/')
                try:
                    parts.append('{}:{}'.format(rel, os.path.getsize(fp)))
                except Exception:
                    parts.append(rel)
    return hashlib.sha1('\n'.join(parts).encode('utf-8', 'replace')).hexdigest()[:6]


@app_share_bp.route('/api/app/<app_name>/version/confirm', methods=['POST'])
@login_required
def api_version_confirm(app_name):
    """版確定．正本（DB）に刻む．6d までは旧コード（エクスポータ等）が version.json を読むので
    同じ内容をファイルにも書く．"""
    if not _valid_app(app_name):
        return _err('アプリ名が不正です')
    now = _now()
    version_id = 'v{}-{}'.format(now.strftime('%Y%m%d.%H%M%S'), _content_hash6(app_name))
    with _db() as (cur, conn):
        cur.execute("SELECT full_name FROM users WHERE id=%s", (session.get('user_id'),))
        u = cur.fetchone()
        by = (u or {}).get('full_name')
        cur.execute("""UPDATE app_share_registry
                       SET version_id=%s, version_confirmed_at=%s, version_confirmed_by=%s
                       WHERE app_name=%s""", (version_id, now, by, app_name))
        conn.commit()
    return _ok(version_id=version_id, confirmed_at=_fmt(now), confirmed_by=by)


# ============================================================
# 発行
# ============================================================

def _registry_stale(cur):
    """DB から組んだ正本と app_registry.json の中身が違うか（generated_at を除いて比較）"""
    data = _reg.build_registry_from_db(cur)
    data.pop('generated_at', None)
    current = _reg.load_registry(force=True)
    cur_copy = dict(current)
    cur_copy.pop('generated_at', None)
    return json.dumps(data, sort_keys=True, ensure_ascii=False) != json.dumps(cur_copy, sort_keys=True, ensure_ascii=False)


@app_share_bp.route('/api/publish/status')
@login_required
def api_publish_status():
    with _db() as (cur, conn):
        stale = _registry_stale(cur)
        cur.execute("SELECT MAX(published_at) AS p FROM app_share_registry")
        p = cur.fetchone()['p']
    reg = _reg.load_registry()
    return _ok(stale=stale, published_at=_fmt(p), generated_at=reg.get('generated_at'),
               file=_reg.REGISTRY_FILE, exists=os.path.exists(_reg.REGISTRY_FILE))


@app_share_bp.route('/api/publish', methods=['POST'])
@login_required
def api_publish():
    d = request.get_json(silent=True) or {}
    do_reload = bool(d.get('reload'))
    rt = _runtime_blueprints()
    with _db() as (cur, conn):
        data = _reg.publish(cur)
        conn.commit()
    # 起動中と正本の Blueprint 集合の差（Reload が要るか）
    want = set()
    for a in data['apps']:
        if a.get('kind') == 'kernel' or not a.get('enabled'):
            continue
        for b in a.get('blueprints') or []:
            if b.get('name'):
                want.add(b['name'])
    kernel = {'auth', 'profile', 'admin', 'guest', 'app_share'}
    running_apps = rt - kernel
    need_reload = (want != running_apps)
    reloaded = False
    if do_reload:
        reloaded = _reg.reload_site()
    return _ok(apps=len(data['apps']), sections=len(data['sections']),
               blueprints=len(want), need_reload=need_reload,
               not_running=sorted(want - running_apps), extra_running=sorted(running_apps - want),
               reloaded=reloaded, generated_at=data.get('generated_at'))


@app_share_bp.route('/api/reload', methods=['POST'])
@login_required
def api_reload():
    return _ok(reloaded=_reg.reload_site(), wsgi=_reg.wsgi_path())


# ============================================================
# 一覧用の要約
# ============================================================

@app_share_bp.route('/api/summary')
@login_required
def api_summary():
    with _db() as (cur, conn):
        cur.execute("""SELECT app_name, COUNT(*) AS n FROM app_share_issues
                       WHERE status='open' GROUP BY app_name""")
        issues = {r['app_name']: r['n'] for r in cur.fetchall()}
        cur.execute("""SELECT app_name, kind, enabled, git_commit, git_committed_at, version_id
                       FROM app_share_registry""")
        rows = cur.fetchall()
        stale = _registry_stale(cur)
    rt = _runtime_blueprints()
    reg = {a['app_name']: a for a in _reg.load_registry().get('apps', [])}
    from . import gitsync
    git = gitsync.git_summary_all([r['app_name'] for r in rows])
    apps = {}
    for r in rows:
        a = reg.get(r['app_name'], {})
        names = [b.get('name') for b in (a.get('blueprints') or []) if b.get('name')]
        apps[r['app_name']] = {
            'open_issues': issues.get(r['app_name'], 0),
            'kind': r['kind'], 'enabled': bool(r['enabled']),
            'running': (all(n in rt for n in names) if names else None),
            'git_commit': r['git_commit'],
            'git_committed_at': _fmt(r['git_committed_at']),
            'git': git.get(r['app_name']),
        }
    return _ok(apps=apps, stale=stale, repo_ok=gitsync._repo_ok())


# ============================================================
# 診断
# ============================================================

_MASK_EMAIL = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')
_MASK_TOKEN = re.compile(r'\b(?:ghp_|xox[abp]-|sk-|AIza|ya29\.)[A-Za-z0-9_\-\.]{8,}')


def _mask(s):
    s = _MASK_EMAIL.sub('<email>', s)
    return _MASK_TOKEN.sub('<token>', s)


def _tail_lines(path, n=400):
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 300000))
            data = f.read().decode('utf-8', 'replace')
        return data.splitlines()[-n:]
    except Exception:
        return []


def _log_paths():
    user = os.path.basename(os.path.expanduser('~').rstrip('/'))
    base = f'/var/log/{user}.pythonanywhere.com'
    return {'server': base + '.server.log', 'error': base + '.error.log', 'access': base + '.access.log'}


def _log_excerpt(app_name=None, n=400):
    out = {}
    for kind, p in _log_paths().items():
        if kind == 'access':
            continue
        lines = _tail_lines(p, n)
        if app_name:
            keep = []
            for i, ln in enumerate(lines):
                if app_name in ln or 'Traceback' in ln or 'Error' in ln or 'registry' in ln:
                    keep.append(ln)
            lines = keep[-150:]
        out[kind] = [_mask(l) for l in lines]
    return out


def _file_inventory(app_name):
    app_dir = _app_path(app_name)
    out = []
    if not os.path.isdir(app_dir):
        return out
    for p in _walk_code(app_dir):
        rel = os.path.relpath(p, app_dir).replace(os.sep, '/')
        try:
            st = os.stat(p)
            h = hashlib.sha1(open(p, 'rb').read()).hexdigest()[:8]
            out.append({'path': rel, 'size': st.st_size,
                        'mtime': datetime.datetime.fromtimestamp(st.st_mtime, JST).strftime('%Y-%m-%d %H:%M:%S'),
                        'sha1': h})
        except Exception as e:
            out.append({'path': rel, 'error': str(e)})
    return out


def _lib_versions():
    out = {'python': sys.version.split()[0], 'platform': platform.platform()}
    for name in ('flask', 'mysql.connector', 'authlib', 'werkzeug', 'markdown'):
        try:
            m = importlib.import_module(name)
            out[name] = getattr(m, '__version__', None) or getattr(m, 'version', None) or 'installed'
        except Exception:
            out[name] = None
    return out


def _diag_app(cur, app_name):
    row = _load_registry_row(cur, app_name)
    if not row:
        return {'app_name': app_name, 'error': 'レジストリにありません'}
    rt = _runtime_blueprints()
    bps = _jload(row.get('blueprints'), [])
    for b in bps:
        b['registered'] = bool(b.get('name') and b['name'] in rt)
    launch = []
    for c in _jload(row.get('launchers'), []):
        try:
            launch.append({'endpoint': c['endpoint'], 'ok': True,
                           'href': url_for(c['endpoint'], **_reg._parse_params(c.get('params'))),
                           'section': c.get('section'), 'dashboards': c.get('dashboards'),
                           'visibility': c.get('visibility'), 'groups': c.get('groups')})
        except Exception as e:
            launch.append({'endpoint': c.get('endpoint'), 'ok': False, 'error': str(e)})
    d = {
        'app_name': app_name,
        'registry': {k: (_fmt(v) if isinstance(v, datetime.datetime) else v)
                     for k, v in row.items()
                     if k not in ('blueprints', 'launchers', 'libraries', 'config_keys')},
        'blueprints': bps,
        'launchers': launch,
        'tables': _compare_app_tables(cur, app_name),
        'libraries': _check_libraries(_jload(row.get('libraries'), [])),
        'config_keys': _check_config_keys(_jload(row.get('config_keys'), [])),
        'docs': _docs_status(cur, app_name),
        'issues_open': _issues(cur, app_name, 'open'),
        'files': _file_inventory(app_name),
        'detected': {
            'blueprints': _detect_blueprints(app_name),
            'libraries': _detect_libraries(app_name),
            'config_keys': [k['name'] for k in _detect_config_keys(app_name)],
        },
    }
    return d


@app_share_bp.route('/api/diag/<app_name>')
@login_required
def api_diag(app_name):
    if not _valid_app(app_name):
        return _err('アプリ名が不正です')
    with _db() as (cur, conn):
        d = _diag_app(cur, app_name)
        stale = _registry_stale(cur)
    out = {
        'diag_type': 'fujinp_app_diagnosis', 'format_version': 1,
        'generated_at': _fmt(_now()), 'site': getattr(Config, 'DB_ACCOUNT', ''),
        'environment': _lib_versions(),
        'registry_file': {'path': _reg.REGISTRY_FILE, 'exists': os.path.exists(_reg.REGISTRY_FILE),
                          'generated_at': _reg.load_registry().get('generated_at'), 'stale': stale},
        'app': d,
        'logs': _log_excerpt(app_name),
        'note': 'Claude へ渡す診断情報．config の値・メールアドレス・トークンは含まない／伏せ字．',
    }
    fn = f'diag_{app_name}_{_now().strftime("%Y%m%d_%H%M%S")}.json'
    return Response(json.dumps(out, ensure_ascii=False, indent=2),
                    mimetype='application/json',
                    headers={'Content-Disposition': f'attachment; filename="{fn}"'})


@app_share_bp.route('/api/diag_site')
@login_required
def api_diag_site():
    with _db() as (cur, conn):
        cur.execute("SELECT app_name, kind, enabled FROM app_share_registry ORDER BY sort_order")
        rows = cur.fetchall()
        stale = _registry_stale(cur)
        apps = []
        for r in rows:
            d = _diag_app(cur, r['app_name'])
            d.pop('files', None)
            d.pop('detected', None)
            apps.append(d)
    reg = _reg.load_registry()
    rt = sorted(_runtime_blueprints())
    out = {
        'diag_type': 'fujinp_site_diagnosis', 'format_version': 1,
        'generated_at': _fmt(_now()), 'site': getattr(Config, 'DB_ACCOUNT', ''),
        'environment': _lib_versions(),
        'registry_file': {'path': _reg.REGISTRY_FILE, 'exists': os.path.exists(_reg.REGISTRY_FILE),
                          'generated_at': reg.get('generated_at'), 'stale': stale,
                          'apps': len(reg.get('apps', [])), 'sections': reg.get('sections', [])},
        'running_blueprints': rt,
        'databases': _db_names(),
        'apps': apps,
        'logs': _log_excerpt(None, 300),
    }
    fn = f'diag_site_{_now().strftime("%Y%m%d_%H%M%S")}.json'
    return Response(json.dumps(out, ensure_ascii=False, indent=2),
                    mimetype='application/json',
                    headers={'Content-Disposition': f'attachment; filename="{fn}"'})
