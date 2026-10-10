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
# Source: https://github.com/u-fukuchiyama/fujin-p

"""
app_share.source — ソースとテーブルの閲覧（2026-10-10）

アプリのソースとテーブルのスキーマを，JavaScript を使わずサーバ側で組み立てた HTML で返す．
オールの URL 文書として登録すれば，Claude も admin が画面で見るのと同じ中身を MCP で読める．
すべて admin 専用（アプシャの before_request の既定どおり）．

  GET /app_share/source/                         アプリ索引（カーネルと全アプリ．ファイル数・テーブル数・文書へのリンク）
  GET /app_share/source/<app_name>               アプリの中身の一覧（正本の要点・ファイル・テーブル・文書・不具合）
  GET /app_share/source/<app_name>/registry      正本の JSON（パッケージから files の本文を除いたもの全部．2026-10-10）
  GET /app_share/source/<app_name>/file/<path>   ファイル1本の本文（資格情報らしき値は伏せる．UTF-8 でないものは base64）
  GET /app_share/tables/                         テーブルカタログ（全DB．持ち主・ソースに名前が出るアプリ・行数・説明）
  GET /app_share/tables/<db>/<table>             テーブル1本のスキーマ（列・索引・CREATE 文・関係するアプリ）

ファイルはパッケージと同じ許可リスト（gitsync._site_app_files）で集める．カーネルは版のハッシュと同じ
対象（routes._kernel_hash_files）で，config.py と資格情報ファイルは含まない．
正本の JSON とファイルの本文を合わせれば，アプシャが書き出すアプリパッケージと同じものを組み立てられる
（Claude がパッケージの添付なしに改修パッケージを作るため）．ファイルには sha256 を添えるので，組み立てた結果を照合できる．
伏せるのは値だけ（代入の右辺の文字列・クライアントシークレット・DB ホスト名・秘密鍵の見出し）で，行は残す．
「ソースに名前が出るアプリ」は，各アプリのファイルの語にテーブル名がそのまま現れるものを拾う．
名前を組み立てて参照している場合（f 文字列や登録簿経由）は拾えない．
"""

import os
import re
import json
import base64
import hashlib
import datetime

from flask import render_template, abort, url_for, session

from . import app_share_bp
from . import routes as _routes
from . import manage as _m
from . import gitsync as _g
from decorators import login_required

PLATFORM_ROW = _m.PLATFORM_ROW
SHOW_EXTS = ('.py', '.html', '.htm', '.js', '.css', '.sql', '.md', '.txt', '.json', '.cfg', '.ini', '.toml', '.yaml', '.yml')
SCAN_EXTS = ('.py', '.html', '.htm', '.js', '.sql')
MASK = '＊＊伏せ字＊＊'
# 伏せる値（行は残す）．(パターン, 置き換え関数)
_VALUE_PATTERNS = [
    (re.compile(r'(?i)([A-Za-z0-9_.]*(?:password|passwd|secret|token|api_?key|credential|private_key)'
                r'[A-Za-z0-9_]*[\'"]?\s*[:=]\s*)([\'"])([\x21-\x7e]{4,}?)(\2)'),
     lambda m: m.group(1) + m.group(2) + MASK + m.group(4)),
    (re.compile(r'GOCSPX-[A-Za-z0-9_\-]{10,}'), lambda m: MASK),
    (re.compile(r'(?<![>\\])\b[A-Za-z0-9_\-]+\.mysql\.pythonanywhere-services\.com'), lambda m: MASK),
    (re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----'), lambda m: MASK),
]
_TOKEN_RE = re.compile(r'[\w・（）～]+')


# ============================================================
# 集める
# ============================================================

def _files_of(app_name):
    """{相対パス: 絶対パス}．カーネルは版のハッシュと同じ対象から config と資格情報を除いたもの"""
    if app_name == PLATFORM_ROW:
        out = {}
        for rel, p in _routes._kernel_hash_files().items():
            fn = rel.rsplit('/', 1)[-1]
            if fn in _routes.KERNEL_EXCLUDE_FILES or fn.startswith(_routes.KERNEL_EXCLUDE_PREFIXES):
                continue
            out[rel] = p
        return out
    if not _m._valid_app(app_name):
        return {}
    return _g._site_app_files(app_name)


def _read_text(p):
    try:
        raw = open(p, 'rb').read()
    except Exception:
        return None
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        return None


def _mask(rel, text):
    """資格情報らしき値を伏せる（行は残す）．戻り値 (本文, 伏せた行の番号のリスト)"""
    lines = text.split('\n')
    hit = []
    for i, ln in enumerate(lines):
        new = ln
        for pat, fn in _VALUE_PATTERNS:
            new = pat.sub(fn, new)
        if new != ln:
            lines[i] = new
            hit.append(i + 1)
    return '\n'.join(lines), hit


def _file_entry(rel, p):
    """(本文, encoding, 伏せた行)．UTF-8 なら text（伏せ字つき），そうでなければ base64"""
    try:
        raw = open(p, 'rb').read()
    except Exception:
        return None, None, []
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        return base64.b64encode(raw).decode('ascii'), 'base64', []
    text, hit = _mask(rel, text)
    return text, 'text', hit


def _mtime(p):
    try:
        return datetime.datetime.fromtimestamp(os.path.getmtime(p), _m.JST).strftime('%Y-%m-%d %H:%M')
    except Exception:
        return ''


def _registry(cur):
    cur.execute("SELECT * FROM app_share_registry ORDER BY sort_order ASC, id ASC")
    return cur.fetchall()


def _owned_tables(cur):
    """{app_name: [台帳の行, ...]}"""
    cur.execute("""SELECT app_name, table_name, db_target, status, note, sort_order
                   FROM app_share_tables ORDER BY app_name, sort_order, table_name""")
    out = {}
    for r in cur.fetchall():
        out.setdefault(r['app_name'], []).append(r)
    return out


def _live_tables(cur):
    """[{db, schema, table, rows, comment, updated}]（default・fujinp・public の実在するもの）"""
    dbs = _m._db_names()
    out = []
    for suf, schema in dbs.items():
        try:
            cur.execute("""SELECT TABLE_NAME, TABLE_TYPE, TABLE_ROWS, TABLE_COMMENT, UPDATE_TIME, CREATE_TIME
                           FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s ORDER BY TABLE_NAME""", (schema,))
            for r in cur.fetchall():
                out.append({'db': suf, 'schema': schema, 'table': r['TABLE_NAME'],
                            'view': r['TABLE_TYPE'] == 'VIEW',
                            'rows': r['TABLE_ROWS'], 'comment': r['TABLE_COMMENT'] or '',
                            'updated': _m._fmt(r['UPDATE_TIME'] or r['CREATE_TIME'])})
        except Exception as e:
            out.append({'db': suf, 'schema': schema, 'table': None, 'error': str(e)})
    return out


def _app_tokens(app_names):
    """{app_name: そのアプリのコードに現れる語の集合}"""
    out = {}
    for a in app_names:
        toks = set()
        for rel, p in _files_of(a).items():
            if not rel.lower().endswith(SCAN_EXTS):
                continue
            t = _read_text(p)
            if t:
                toks.update(_TOKEN_RE.findall(t))
        out[a] = toks
    return out


def _relations(cur, regs=None):
    """テーブル名 → {'owners': [...], 'users': [...]}．users は持ち主以外でソースに名前が出るアプリ"""
    regs = regs if regs is not None else _registry(cur)
    names = [r['app_name'] for r in regs]
    owned = _owned_tables(cur)
    own = {}
    for a, rows in owned.items():
        for t in rows:
            own.setdefault(t['table_name'], []).append(a)
    toks = _app_tokens(names)
    return own, toks


def _users_of(table, own, toks):
    owners = own.get(table, [])
    return [a for a, ts in toks.items() if table in ts and a not in owners]


def _display(regs):
    return {r['app_name']: (r.get('icon') or '📦') + ' ' + (r.get('display_name') or r['app_name']) for r in regs}


# ============================================================
# ソース
# ============================================================

@app_share_bp.route('/source/')
@login_required
def source_index():
    with _m._db() as (cur, conn):
        regs = _registry(cur)
        owned = _owned_tables(cur)
    apps = []
    for r in regs:
        name = r['app_name']
        files = _files_of(name)
        apps.append({'app_name': name,
                     'display_name': 'カーネル（ホーム直下と fujinp/ の共通部分）' if name == PLATFORM_ROW
                     else (r.get('display_name') or name),
                     'icon': '🧠' if name == PLATFORM_ROW else (r.get('icon') or '📦'),
                     'description': r.get('description') or '',
                     'kind': r.get('kind') or 'app',
                     'enabled': bool(r.get('enabled', 1)),
                     'version_id': r.get('version_id'),
                     'n_files': len(files),
                     'n_bytes': sum(os.path.getsize(p) for p in files.values() if os.path.exists(p)),
                     'n_tables': len(owned.get(name, []))})
    apps.sort(key=lambda a: a['app_name'] != PLATFORM_ROW)
    return render_template('app_share_source.html', mode='index', apps=apps,
                           generated_at=_m._now().strftime('%Y-%m-%d %H:%M'))


@app_share_bp.route('/source/<app_name>')
@login_required
def source_app(app_name):
    if app_name != PLATFORM_ROW and not _m._valid_app(app_name):
        abort(404)
    with _m._db() as (cur, conn):
        row = _m._load_registry_row(cur, app_name)
        if not row:
            abort(404)
        owned = _owned_tables(cur).get(app_name, [])
        cur.execute("""SELECT doc_type, CHAR_LENGTH(content) AS clen, updated_at FROM app_share_documents
                       WHERE app_name=%s AND doc_type IN ('manual','spec')""", (app_name,))
        docs = {d['doc_type']: d for d in cur.fetchall()}
        cur.execute("""SELECT title, detail, reported_at FROM app_share_issues
                       WHERE app_name=%s AND status='open' ORDER BY id""", (app_name,))
        issues = cur.fetchall()
        regs = _registry(cur)
        live = {(t['db'], t['table']): t for t in _live_tables(cur) if t.get('table')}
        own, toks = _relations(cur, regs)
    files = []
    for rel, p in sorted(_files_of(app_name).items()):
        t = _read_text(p)
        files.append({'path': rel, 'size': os.path.getsize(p) if os.path.exists(p) else 0,
                      'lines': (t.count('\n') + 1) if t else None, 'mtime': _mtime(p),
                      'text': True, 'binary': t is None})
    names = _display(regs)
    tables = []
    for t in owned:
        lv = live.get((t['db_target'], t['table_name']))
        tables.append({'table': t['table_name'], 'db': t['db_target'], 'status': t['status'],
                       'note': t.get('note') or '', 'rows': lv['rows'] if lv else None, 'exists': bool(lv),
                       'users': [names.get(a, a) for a in _users_of(t['table_name'], own, toks)]})
    # 他のアプリが持つテーブルのうち，このアプリのソースに名前が出るもの
    mine = toks.get(app_name, set())
    borrowed = []
    for tname, owners in sorted(own.items()):
        if app_name in owners or tname not in mine:
            continue
        db = next((k[0] for k in live if k[1] == tname), None)
        borrowed.append({'table': tname, 'db': db, 'owners': [names.get(a, a) for a in owners]})
    # 台帳にないが実在し，このアプリのソースに名前が出るテーブル（kernel の users など）
    stray = sorted({(k[0], k[1]) for k in live if k[1] in mine and k[1] not in own})
    return render_template('app_share_source.html', mode='app', app_name=app_name,
                           is_kernel=app_name == PLATFORM_ROW,
                           row=row, blueprints=_m._jload(row.get('blueprints'), []),
                           launchers=_m._jload(row.get('launchers'), []),
                           libraries=_m._jload(row.get('libraries'), []),
                           config_keys=_m._jload(row.get('config_keys'), []),
                           files=files, tables=tables, borrowed=borrowed, stray=stray,
                           docs=docs, issues=issues,
                           generated_at=_m._now().strftime('%Y-%m-%d %H:%M'))


@app_share_bp.route('/source/<app_name>/file/<path:rel>')
@login_required
def source_file(app_name, rel):
    files = _files_of(app_name)
    p = files.get(rel)
    if not p:
        abort(404)
    text, enc, hit = _file_entry(rel, p)
    raw = open(p, 'rb').read() if os.path.exists(p) else b''
    return render_template('app_share_source.html', mode='file', app_name=app_name, rel=rel,
                           text=text, encoding=enc, masked=len(hit), masked_lines=hit,
                           sha256=hashlib.sha256(raw).hexdigest(),
                           size=os.path.getsize(p), mtime=_mtime(p),
                           lines=(text.count('\n') + 1) if (text and enc == 'text') else 0)


@app_share_bp.route('/source/<app_name>/registry')
@login_required
def source_registry(app_name):
    """正本の JSON．パッケージ（build_package）から files の本文を除き，各ファイルの目録（sha256・伏せた行）を付ける"""
    if app_name != PLATFORM_ROW and not _m._valid_app(app_name):
        abort(404)
    from . import package as _p
    err = ''
    pkg = None
    with _m._db() as (cur, conn):
        cur.execute("SELECT full_name FROM users WHERE id=%s", (session.get('user_id'),))
        u = cur.fetchone() or {}
        try:
            pkg = _p.build_package(cur, app_name, generated_by=u.get('full_name'), docs_choice='later')
        except Exception as e:
            err = str(e)
    if pkg is None and not err:
        abort(404)
    files = []
    if pkg:
        paths = _files_of(app_name)
        for f in pkg.get('files') or []:
            ap = paths.get(f['path'])
            raw = open(ap, 'rb').read() if ap and os.path.exists(ap) else b''
            _, enc, hit = _file_entry(f['path'], ap) if ap else (None, f.get('encoding'), [])
            files.append({'path': f['path'], 'size': f.get('size'), 'mtime': f.get('mtime'),
                          'encoding': enc or f.get('encoding'), 'sha256': hashlib.sha256(raw).hexdigest(),
                          'masked_lines': hit,
                          'url': url_for('app_share.source_file', app_name=app_name, rel=f['path'])})
        pkg['files'] = files
        pkg['file_count'] = len(files)
    body = json.dumps(pkg, ensure_ascii=False, indent=1, default=str) if pkg else ''
    return render_template('app_share_source.html', mode='registry', app_name=app_name, body=body,
                           err=err, files=files, n_masked=sum(len(f['masked_lines']) for f in files),
                           generated_at=_m._now().strftime('%Y-%m-%d %H:%M'))


# ============================================================
# テーブル
# ============================================================

@app_share_bp.route('/tables/')
@login_required
def table_catalog():
    with _m._db() as (cur, conn):
        regs = _registry(cur)
        live = _live_tables(cur)
        own, toks = _relations(cur, regs)
        owned = _owned_tables(cur)
    names = _display(regs)
    status = {}
    for a, rows in owned.items():
        for t in rows:
            status[(t['db_target'], t['table_name'])] = t.get('note') or ''
    errors = [t for t in live if t.get('error')]
    tables = []
    for t in live:
        if not t.get('table'):
            continue
        tables.append(dict(t, owners=[names.get(a, a) for a in own.get(t['table'], [])],
                           users=[names.get(a, a) for a in _users_of(t['table'], own, toks)],
                           note=status.get((t['db'], t['table']), '')))
    dbs = []
    for d in _m._db_names():
        rows = [t for t in tables if t['db'] == d]
        dbs.append({'db': d, 'tables': rows,
                    'shared': sum(1 for t in rows if len(t['owners']) + len(t['users']) > 1),
                    'orphan': sum(1 for t in rows if not t['owners'] and not t['users'])})
    return render_template('app_share_source.html', mode='tables', dbs=dbs, errors=errors,
                           n_tables=len(tables), generated_at=_m._now().strftime('%Y-%m-%d %H:%M'))


@app_share_bp.route('/tables/<db>/<path:table>')
@login_required
def table_schema(db, table):
    dbs = _m._db_names()
    schema = dbs.get(db)
    if not schema:
        abort(404)
    with _m._db() as (cur, conn):
        cur.execute("""SELECT TABLE_TYPE, TABLE_ROWS, TABLE_COMMENT, UPDATE_TIME, CREATE_TIME
                       FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s""", (schema, table))
        info = cur.fetchone()
        if not info:
            abort(404)
        cur.execute("""SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY, COLUMN_DEFAULT, EXTRA, COLUMN_COMMENT
                       FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s
                       ORDER BY ORDINAL_POSITION""", (schema, table))
        cols = cur.fetchall()
        regs = _registry(cur)
        own, toks = _relations(cur, regs)
        cur.execute("""SELECT app_name, status, note, captured_at FROM app_share_tables
                       WHERE table_name=%s AND db_target=%s""", (table, db))
        ledger = cur.fetchall()
    try:
        ddl = _m._show_create(schema, table) or ''
    except Exception as e:
        ddl = f'（CREATE 文を取得できません：{e}）'
    names = _display(regs)
    return render_template('app_share_source.html', mode='table', db=db, table=table,
                           info={'view': info['TABLE_TYPE'] == 'VIEW', 'rows': info['TABLE_ROWS'],
                                 'comment': info['TABLE_COMMENT'] or '',
                                 'updated': _m._fmt(info['UPDATE_TIME'] or info['CREATE_TIME'])},
                           cols=cols, ddl=ddl,
                           owners=[(a, names.get(a, a)) for a in own.get(table, [])],
                           users=[(a, names.get(a, a)) for a in _users_of(table, own, toks)],
                           ledger=[dict(l, captured_at=_m._fmt(l.get('captured_at'))) for l in ledger])
