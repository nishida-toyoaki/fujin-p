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
アプシャ — 台帳点検（2026-10-10）

サイトの全アプリについて，コード（.py・.sql）が参照するテーブルと，台帳（app_share_tables）と，
DB の実物を突き合わせ，台帳のずれを一覧にして，その場で直す．

方針（2026-10-10 決定）：
- アプリの台帳には，そのアプリが持つテーブルだけを載せる．
- 共通テーブル（users・user_groups 群・ug_*・approved_users など）は _platform 行の台帳に一本化し，
  各アプリの台帳からは外す．
- 台帳の DDL は実物（SHOW CREATE TABLE）と一致させる．

判定の種類（kind）：
  add_own        コードが参照し，DB に実在し，どの台帳にも無く，名前がアプリ名_ で始まる → このアプリの台帳に取り込む（実物の DDL）
  foreign        コードが参照し，DB に実在し，どの台帳にも無いが，名前がアプリ名_ で始まらない → 判断（このアプリの表なら取り込む．他アプリの表なら持ち主の台帳に）
  add_platform   共通テーブルがコードに出るが _platform 台帳に無い → _platform の台帳に取り込む
  remove_common  共通テーブルがこのアプリの台帳に載っている → 外す（持ち主のアプリ＝user_groups は除く）
  remove_other   他アプリの表（接頭辞がそのアプリ名）がこのアプリの台帳に載っている → 外す
  dup_ledger     このアプリの台帳にある表が他アプリの台帳にもあり，接頭辞では持ち主が決まらない → 判断
  recapture      台帳の DDL が実物と違う（実物の方が列が多い等）→ 実物から取り直す
  apply_cols     台帳にだけある列・索引がある → 管理画面の「適用」で実物に足す（取り直すと宣言が失われる）
  no_table       コードが参照するが DB に無く，台帳にも無い → 判断（未作成か，死んだコードか）
  unreferenced   台帳にあるがコードに出ない → 判断（外すか，残すか）
  not_in_db      台帳にあるが DB に無い → 情報（管理画面の「適用」で作られる）
  other_app      コードが参照するが他アプリの台帳にある → 情報（閲覧している）
"""
import os
import re

from flask import render_template, request

from . import app_share_bp
from . import manage as _m
from . import package as _p
from decorators import login_required

PLATFORM_ROW = _m.PLATFORM_ROW
# 共通テーブル：どのアプリも閲覧するだけ．_platform の台帳に目録として載せる
COMMON_RE = re.compile(r'^(users|user_groups|user_group_memberships|user_groups_.*|ug_.*|approved_users|'
                       r'registration_requests|login_history|password_resets|password_reset_tokens)$')
# 共通テーブルのうち，更新する（持ち主の）アプリが決まっているもの．持ち主の台帳にも載せる
# （持ち主のアプリパッケージが別サイトでその表を作れるようにするため）
COMMON_OWNERS = (
    (re.compile(r'^(user_groups|user_group_memberships|user_groups_.*|ug_.*)$'), 'user_groups'),
)
# 接頭辞では持ち主が決まらない表の持ち主（明示）
EXTRA_OWNERS = {
    'public_documents': 'document_archive',
    'document_access_groups': 'document_archive',
}
LITERAL_RE = re.compile(r"""['"]([^\W\d][\w$]*)['"]""")


def _code_texts_on_disk(app_name):
    app_dir = _m._app_path(app_name)
    if not os.path.isdir(app_dir):
        return []
    out = []
    for path in _m._walk_code(app_dir):
        if not path.endswith(('.py', '.sql')):
            continue
        rel = os.path.relpath(path, app_dir).replace(os.sep, '/')
        out.append((rel, _m._read(path)))
    return out


def _literal_refs(texts, known):
    """文字列リテラル（定数に入れたテーブル名）で参照しているもの．known（実在か台帳にある名前）に限る"""
    refs = {}
    for rel, text in texts:
        for m in LITERAL_RE.finditer(text):
            name = m.group(1)
            if name in known:
                files = refs.setdefault(name, [])
                tag = rel + "（文字列）"
                if tag not in files:
                    files.append(tag)
    return refs


_TABLES_ATTR_RE = re.compile(r'''^\s*([A-Z][A-Z0-9_]*)\s*=\s*['"]([^\W\d][\w$]*)['"]''', re.M)
_TABLES_USE_RE = re.compile(r'\bTables\.([A-Z][A-Z0-9_]*)\b')
_TABLES_FSTR_RE = re.compile(r"""^\s*([A-Z][A-Z0-9_]*)\s*=\s*f?['"]\{[^}]*\}\.([^\W\d][\w$]*)['"]""", re.M)


def _tables_class_map():
    """カーネル db.py の class Tables（属性名 → テーブル名）．
    値は "DB名.テーブル名" の形（f 文字列で Config の値を埋め込む）なので，実行時に読んで最後の '.' の後ろを取る．
    import できないときは db.py の本文から拾う．"""
    out = {}
    try:
        import importlib
        dbmod = importlib.import_module('db')
        T = getattr(dbmod, 'Tables', None)
        if T is not None:
            for attr in dir(T):
                if not re.match(r'^[A-Z][A-Z0-9_]*$', attr) or attr.startswith('DB_'):
                    continue                    # DB_DEFAULT などは DB 名であって表ではない
                v = getattr(T, attr, None)
                if isinstance(v, str) and v:
                    name = v.rsplit('.', 1)[-1].strip('`')
                    if re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', name):
                        out[attr] = name
            return out
    except Exception:
        pass
    path = os.path.join(_m.SITE_CODE_ROOT, 'db.py')
    text = _m._read(path)
    i = text.find('class Tables')
    if i < 0:
        return out
    body = text[i:]
    j = re.search(r'^class\s', body[12:], re.M)
    if j:
        body = body[:12 + j.start()]
    for m in _TABLES_ATTR_RE.finditer(body):
        if not m.group(1).startswith('DB_') and '$' not in m.group(2):
            out[m.group(1)] = m.group(2)
    for m in _TABLES_FSTR_RE.finditer(body):
        out.setdefault(m.group(1), m.group(2))
    return out


def _tables_class_refs(texts, tmap):
    """Tables.X（db.py の定数表）経由の参照"""
    refs = {}
    if not tmap:
        return refs
    for rel, text in texts:
        for m in _TABLES_USE_RE.finditer(text):
            name = tmap.get(m.group(1))
            if not name:
                continue
            files = refs.setdefault(name, [])
            tag = rel + "（Tables." + m.group(1) + "）"
            if tag not in files:
                files.append(tag)
    return refs


def _common_owner(name):
    for rx, app in COMMON_OWNERS:
        if rx.match(name):
            return app
    return None


def _is_common(name, platform_tables):
    return name in platform_tables or bool(COMMON_RE.match(name))


def _live_tables():
    """{テーブル名: db_target}（最初に見つかった DB）"""
    live = {}
    for suf, dbname in _m._db_names().items():
        try:
            for t in _m._list_tables(dbname):
                live.setdefault(t, suf)
        except Exception:
            pass
    return live


def _mine(app, name):
    return name.startswith(app + '_') or name == app or _common_owner(name) == app or EXTRA_OWNERS.get(name) == app


def audit(cur):
    cur.execute("SELECT app_name, kind FROM app_share_registry ORDER BY app_name")
    apps = [r for r in cur.fetchall()]
    app_names = {a['app_name'] for a in apps}
    cur.execute("SELECT app_name, table_name, db_target, ddl, status FROM app_share_tables")
    ledger = {}
    for r in cur.fetchall():
        ledger.setdefault(r['app_name'], {})[r['table_name']] = r
    platform_tables = set(ledger.get(PLATFORM_ROW, {}).keys())
    platform_add = {}       # 共通テーブルで _platform 台帳に無いもの（アプリをまたいで1回だけ）
    owners = {}
    for a, rows in ledger.items():
        for t in rows:
            owners.setdefault(t, []).append(a)
    live = _live_tables()
    known = set(live) | set(owners)
    tmap = _tables_class_map()
    ddl_cache = {}

    def live_ddl(suf, table):
        key = (suf, table)
        if key not in ddl_cache:
            dbname = _m._db_names().get(suf)
            try:
                ddl_cache[key] = _m._show_create(dbname, table) if dbname else None
            except Exception:
                ddl_cache[key] = None
        return ddl_cache[key]

    def prefix_owner(name):
        """名前の接頭辞（または明示の対応表）から持ち主と分かるアプリ（最長一致）"""
        if name in EXTRA_OWNERS and EXTRA_OWNERS[name] in app_names:
            return EXTRA_OWNERS[name]
        best = None
        for b in app_names:
            if b == PLATFORM_ROW:
                continue
            if name == b or name.startswith(b + '_'):
                if best is None or len(b) > len(best):
                    best = b
        return best

    report = []
    totals = {}
    for a in apps:
        app = a['app_name']
        if app == PLATFORM_ROW:
            continue
        texts = _code_texts_on_disk(app)
        sql_refs = _p.referenced_tables_in(texts, known)
        lit_refs = _literal_refs(texts, known)
        cls_refs = _tables_class_refs(texts, tmap)
        refs = {}
        for name, files in sql_refs.items():
            refs.setdefault(name, []).extend(files)
        for name, files in cls_refs.items():
            refs.setdefault(name, []).extend(files)
        for name, files in lit_refs.items():
            refs.setdefault(name, []).extend(files)
        own = ledger.get(app, {})
        items = []
        for name, files in sorted(refs.items()):
            in_own = name in own
            mine = _mine(app, name)
            common = _is_common(name, platform_tables) and not mine
            others = [o for o in owners.get(name, []) if o not in (app, PLATFORM_ROW)]
            suf = live.get(name)
            strong = name in sql_refs or name in cls_refs
            if common:
                if in_own:
                    items.append({'kind': 'remove_common', 'table_name': name, 'files': files, 'db_target': own[name]['db_target']})
                if name not in platform_tables and suf:
                    platform_add.setdefault(name, {'kind': 'add_platform', 'table_name': name, 'files': [], 'db_target': suf, 'apps': []})
                    platform_add[name]['apps'].append(app)
                continue
            if in_own:
                row = own[name]
                if not suf:
                    items.append({'kind': 'not_in_db', 'table_name': name, 'files': files, 'db_target': row['db_target']})
                else:
                    ld = live_ddl(row['db_target'], name)
                    if ld is None:
                        ld = live_ddl(suf, name)
                    if ld is not None:
                        cmp = _m._compare(row['ddl'], ld)
                        if cmp.get('status') != 'same' or cmp.get('live_only_columns'):
                            needs_apply = bool(cmp.get('added_columns') or cmp.get('added_indexes'))
                            items.append({'kind': 'apply_cols' if needs_apply else 'recapture', 'table_name': name, 'files': files,
                                          'db_target': row['db_target'], 'diff': {
                                              'ledger_only_columns': [c['name'] for c in cmp.get('added_columns', [])],
                                              'live_only_columns': list(cmp.get('live_only_columns', [])),
                                              'changed_columns': [c['name'] for c in cmp.get('changed_columns', [])],
                                              'ledger_only_indexes': [i.get('name') for i in cmp.get('added_indexes', [])],
                                          }})
                    if others and not mine:
                        po = prefix_owner(name)
                        if po and po != app:
                            items.append({'kind': 'remove_other', 'table_name': name, 'files': files,
                                          'db_target': row['db_target'], 'owners': [po]})
                        else:
                            items.append({'kind': 'dup_ledger', 'table_name': name, 'files': files,
                                          'db_target': row['db_target'], 'owners': others})
                continue
            if mine and suf:
                items.append({'kind': 'add_own', 'table_name': name, 'files': files, 'db_target': suf})
            elif not strong:
                continue                        # 文字列にだけ出る名前は，自分の表でなければ扱わない
            elif others:
                items.append({'kind': 'other_app', 'table_name': name, 'files': files, 'owners': others})
            elif suf:
                items.append({'kind': 'foreign', 'table_name': name, 'files': files, 'db_target': suf})
            else:
                items.append({'kind': 'no_table', 'table_name': name, 'files': files})
        for name, row in sorted(own.items()):
            if name in refs:
                continue
            if _is_common(name, platform_tables) and not _mine(app, name):
                items.append({'kind': 'remove_common', 'table_name': name, 'files': [], 'db_target': row['db_target']})
                continue
            po = prefix_owner(name)
            if po and po != app and not _mine(app, name):
                items.append({'kind': 'remove_other', 'table_name': name, 'files': [], 'db_target': row['db_target'],
                              'owners': [po]})
            else:
                items.append({'kind': 'unreferenced', 'table_name': name, 'files': [],
                              'db_target': row['db_target'], 'exists': name in live})
        for it in items:
            totals[it['kind']] = totals.get(it['kind'], 0) + 1
        report.append({'app_name': app, 'kind': a['kind'], 'has_code': bool(texts) or os.path.isdir(_m._app_path(app)),
                       'ledger': sorted(own.keys()), 'items': items})
    # _platform 自身：共通テーブルの取り込みと，台帳にあるが DB に無い表
    plat_items = list(platform_add.values())
    for it in plat_items:
        totals['add_platform'] = totals.get('add_platform', 0) + 1
    for name, row in sorted(ledger.get(PLATFORM_ROW, {}).items()):
        if name not in live:
            plat_items.append({'kind': 'not_in_db', 'table_name': name, 'files': [], 'db_target': row['db_target']})
            totals['not_in_db'] = totals.get('not_in_db', 0) + 1
    return {'apps': report, 'platform': {'ledger': sorted(platform_tables), 'items': plat_items},
            'totals': totals, 'databases': _m._db_names()}


@app_share_bp.route('/ledger')
@login_required
def ledger_page():
    return render_template('app_share_ledger.html')


@app_share_bp.route('/api/ledger/audit')
@login_required
def api_ledger_audit():
    with _m._db() as (cur, conn):
        res = audit(cur)
    return _m._ok(result=res)


def _do_action(cur, conn, app_name, kind, table, db_target):
    """1件の処置．戻り値 (ok, message)"""
    dbs = _m._db_names()
    if not _m._valid_app(app_name) or not re.match(r'^[\w$]+$', table or ''):
        return False, '名前が不正です'
    if kind in ('add_own', 'add_platform', 'recapture'):
        target = PLATFORM_ROW if kind == 'add_platform' else app_name
        suf = db_target or 'default'
        if suf not in dbs:
            return False, f'DB {suf} は設定されていません'
        ddl = _m._show_create(dbs[suf], table)
        if ddl is None:
            return False, f'{dbs[suf]} に {table} がありません'
        cur.execute("SELECT COALESCE(MAX(sort_order),0)+10 AS n FROM app_share_tables WHERE app_name=%s", (target,))
        nxt = cur.fetchone()['n']
        cur.execute("""INSERT INTO app_share_tables
            (app_name, table_name, db_target, ddl, captured_at, status, sort_order)
            VALUES (%s,%s,%s,%s,%s,'confirmed',%s)
            ON DUPLICATE KEY UPDATE ddl=VALUES(ddl), captured_at=VALUES(captured_at),
                db_target=VALUES(db_target), status='confirmed'""",
            (target, table, suf, ddl, _m._now(), nxt))
        _m._touch(cur, target)
        return True, f'{target} の台帳に {table} を{"取り直しました" if kind == "recapture" else "取り込みました"}'
    if kind == 'apply_cols':
        plan = _m._compare_app_tables(cur, app_name)
        import mysql.connector
        from db import DatabaseConfig
        done = []
        for item in plan:
            if item['table_name'] != table:
                continue
            sqls = item.get('alter_sqls') or ([] if item.get('status') != 'missing' else [item['create_sql']])
            if not sqls:
                return False, '足す列・索引がありません'
            conn2 = mysql.connector.connect(**DatabaseConfig.get_config(item['database']))
            try:
                cu = conn2.cursor()
                for q in sqls:
                    cu.execute(q)
                    done.append(q)
                conn2.commit()
                cu.close()
            finally:
                conn2.close()
        return True, f'{table} に適用：' + '；'.join(done)
    if kind in ('remove_common', 'remove_other', 'remove'):
        cur.execute("DELETE FROM app_share_tables WHERE app_name=%s AND table_name=%s", (app_name, table))
        n = cur.rowcount
        _m._touch(cur, app_name)
        return n > 0, f'{app_name} の台帳から {table} を外しました' if n else '台帳にありませんでした'
    return False, f'処置 {kind} は知りません'


@app_share_bp.route('/api/ledger/act', methods=['POST'])
@login_required
def api_ledger_act():
    """処置を実行．{actions:[{app_name, kind, table_name, db_target}]}"""
    d = request.get_json(silent=True) or {}
    acts = d.get('actions') or []
    results = []
    with _m._db() as (cur, conn):
        for a in acts:
            try:
                ok, msg = _do_action(cur, conn, a.get('app_name'), a.get('kind'), a.get('table_name'), a.get('db_target'))
            except Exception as e:
                ok, msg = False, str(e)
            results.append({'app_name': a.get('app_name'), 'table_name': a.get('table_name'), 'kind': a.get('kind'),
                            'ok': ok, 'message': msg})
        conn.commit()
    return _m._ok(results=results)
