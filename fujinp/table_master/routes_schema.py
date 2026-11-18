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
テーブルマスター：スキーマ一覧（2026-10-10，admin 専用）

このサイトの全データベース（<アカウント>$ で始まるもの）の全テーブルについて，
列・索引・説明・行数（概数）を，データベース別に1枚の HTML に並べる．
JavaScript を使わずサーバ側で組み立てるので，オールの URL 文書として登録すれば
Claude も admin が画面で見るのと同じスキーマを MCP で読める．中身のデータは載せない．

  GET /table_master/schemas                全データベース
  GET /table_master/schemas?db=<名前>      1つのデータベースだけ（名前は fujinp$default でも default でもよい）
  GET /table_master/schemas?ddl=1          CREATE 文も添える（重くなる）

information_schema を1データベースあたり3回引くだけで組み立てる（ddl=1 のときだけ SHOW CREATE を各表に）．
"""

import logging
import datetime
import mysql.connector
from flask import request, render_template, session, abort

from decorators import login_required
from config import Config
from db import DatabaseConfig
from . import table_master_bp
from .routes import is_safe_identifier

JST = datetime.timezone(datetime.timedelta(hours=9))


def _is_admin_user(user_id):
    """users.category で admin かどうかを確かめる（セッションの値には頼らない）"""
    if not user_id:
        return False
    conn = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur = conn.cursor(dictionary=True, buffered=True)
        cur.execute("SELECT category FROM users WHERE id=%s", (user_id,))
        row = cur.fetchone()
        cur.close()
        return bool(row and row.get('category') == 'admin')
    except Exception as e:
        logging.error("schemas: admin check error: %s", e)
        return False
    finally:
        if conn and conn.is_connected():
            conn.close()


def _fmt(dt):
    if not dt:
        return ''
    return dt.strftime('%Y-%m-%d %H:%M') if hasattr(dt, 'strftime') else str(dt)


def _collect(dbs, with_ddl=False):
    """[{name, short, tables: [{name, comment, rows, engine, collation, updated, view, columns, indexes, ddl}]}]"""
    out = {d: {'name': d, 'short': d.split('$', 1)[-1], 'tables': {}, 'error': ''} for d in dbs}
    if not dbs:
        return []
    marks = ','.join(['%s'] * len(dbs))
    conn = mysql.connector.connect(**DatabaseConfig.base())
    try:
        cur = conn.cursor(dictionary=True, buffered=True)
        cur.execute(f"""SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE, ENGINE, TABLE_ROWS, TABLE_COLLATION,
                               TABLE_COMMENT, CREATE_TIME, UPDATE_TIME
                        FROM information_schema.TABLES WHERE TABLE_SCHEMA IN ({marks})
                        ORDER BY TABLE_SCHEMA, TABLE_NAME""", tuple(dbs))
        for r in cur.fetchall():
            if r['TABLE_SCHEMA'] not in out:
                continue
            out[r['TABLE_SCHEMA']]['tables'][r['TABLE_NAME']] = {
                'name': r['TABLE_NAME'], 'view': r['TABLE_TYPE'] == 'VIEW', 'engine': r['ENGINE'] or '',
                'rows': r['TABLE_ROWS'], 'collation': r['TABLE_COLLATION'] or '',
                'comment': r['TABLE_COMMENT'] or '',
                'updated': _fmt(r['UPDATE_TIME'] or r['CREATE_TIME']),
                'columns': [], 'indexes': {}, 'ddl': ''}
        cur.execute(f"""SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY,
                               COLUMN_DEFAULT, EXTRA, COLUMN_COMMENT
                        FROM information_schema.COLUMNS WHERE TABLE_SCHEMA IN ({marks})
                        ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION""", tuple(dbs))
        for r in cur.fetchall():
            t = out.get(r['TABLE_SCHEMA'], {}).get('tables', {}).get(r['TABLE_NAME'])
            if t is not None:
                t['columns'].append(r)
        cur.execute(f"""SELECT TABLE_SCHEMA, TABLE_NAME, INDEX_NAME, NON_UNIQUE, COLUMN_NAME, SEQ_IN_INDEX
                        FROM information_schema.STATISTICS WHERE TABLE_SCHEMA IN ({marks})
                        ORDER BY TABLE_SCHEMA, TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX""", tuple(dbs))
        for r in cur.fetchall():
            t = out.get(r['TABLE_SCHEMA'], {}).get('tables', {}).get(r['TABLE_NAME'])
            if t is None:
                continue
            ix = t['indexes'].setdefault(r['INDEX_NAME'], {'name': r['INDEX_NAME'],
                                                           'unique': not int(r['NON_UNIQUE'] or 0),
                                                           'columns': []})
            ix['columns'].append(r['COLUMN_NAME'])
        cur.close()
    finally:
        conn.close()

    if with_ddl:
        for d in dbs:
            try:
                c2 = mysql.connector.connect(**DatabaseConfig.get_config(d))
            except Exception as e:
                out[d]['error'] = str(e)
                continue
            try:
                cu = c2.cursor()
                for name, t in out[d]['tables'].items():
                    if not is_safe_identifier(name):
                        continue
                    try:
                        cu.execute(f"SHOW CREATE TABLE `{name}`")
                        row = cu.fetchone()
                        t['ddl'] = row[1] if row else ''
                    except Exception as e:
                        t['ddl'] = f'（CREATE 文を取得できません：{e}）'
                cu.close()
            finally:
                c2.close()

    result = []
    for d in dbs:
        x = out[d]
        tables = list(x['tables'].values())
        for t in tables:
            t['indexes'] = sorted(t['indexes'].values(), key=lambda i: (i['name'] != 'PRIMARY', i['name']))
        x['tables'] = tables
        x['n_columns'] = sum(len(t['columns']) for t in tables)
        result.append(x)
    return result


def _databases():
    """このサイトのデータベース名の一覧（<アカウント>$ で始まるもの）"""
    conn = mysql.connector.connect(**DatabaseConfig.base())
    try:
        cur = conn.cursor()
        cur.execute("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME LIKE %s "
                    "ORDER BY SCHEMA_NAME", (Config.DB_ACCOUNT + '$%',))
        names = [r[0] for r in cur.fetchall()]
        cur.close()
        return names
    finally:
        conn.close()


@table_master_bp.route('/schemas')
@login_required
def schema_list():
    """全テーブルのスキーマ一覧（データベース別）．admin 専用"""
    if not _is_admin_user(session.get('user_id')):
        abort(403)
    try:
        all_dbs = _databases()
    except Exception as e:
        logging.error("schemas: database list error: %s", e)
        return f"データベースの一覧を取得できません：{e}", 500
    want = (request.args.get('db') or '').strip()
    dbs = all_dbs
    if want:
        dbs = [d for d in all_dbs if d == want or d.split('$', 1)[-1] == want]
        if not dbs:
            abort(404)
    with_ddl = request.args.get('ddl') in ('1', 'true', 'yes')
    try:
        data = _collect(dbs, with_ddl=with_ddl)
    except Exception as e:
        logging.error("schemas: collect error: %s", e)
        return f"スキーマを取得できません：{e}", 500
    summary = [{'name': d, 'short': d.split('$', 1)[-1]} for d in all_dbs]
    counts = {x['name']: (len(x['tables']), x['n_columns']) for x in data}
    return render_template('tm_schemas.html', data=data, summary=summary, counts=counts,
                           selected=want, with_ddl=with_ddl,
                           n_tables=sum(len(x['tables']) for x in data),
                           generated_at=datetime.datetime.now(JST).strftime('%Y-%m-%d %H:%M'))
