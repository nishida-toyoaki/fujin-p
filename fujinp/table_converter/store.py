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

"""台帳 —— 対応式・標本・残余・実行の記録と，ふつうのSQLテーブルへの出し入れ。

テーコン自身が持つ表は tcv_ で始まる四つだけである。
分解した中身が入るのは，対応式が名指しした「ふつうのSQLテーブル」で，
それはテーコンの持ち物ではない。テーブルマスターからも，
ほかのアプリからも，何の断りもなく読める形にしておくのが眼目である。
"""

import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import db as _db_module

from . import spec as sp

JST = timezone(timedelta(hours=9))
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
SAMPLE_DIR = os.path.join(DATA_DIR, 'samples')
MAX_SAMPLE = 20 * 1024 * 1024


def now():
    """日時3層ルール：DB は JST の DATETIME。"""
    return datetime.now(JST).replace(tzinfo=None)


def ts():
    return now().strftime('%Y-%m-%d %H:%M:%S')


# ================================================================ 接続

_HELPERS = ('get_db_cursor', 'get_db_connection', 'get_connection', 'get_conn', 'connect')


def _call_helper(database=None):
    last = None
    for name in _HELPERS:
        f = getattr(_db_module, name, None)
        if not callable(f):
            continue
        try:
            return f(database=database) if database else f()
        except TypeError as e:
            last = e
            for arg in ((database,) if database else ('default', None)):
                try:
                    return f(arg)
                except TypeError as e2:
                    last = e2
    raise RuntimeError('db.py に使える接続ヘルパが見つかりません: %s' % last)


@contextmanager
def db(database=None):
    """(cursor, connection)。カーソルは辞書型。"""
    obj = _call_helper(database)
    ctx = None
    if hasattr(obj, '__enter__') and not hasattr(obj, 'cursor'):
        ctx = obj
        obj = ctx.__enter__()
    if isinstance(obj, (tuple, list)):
        conn, owns = obj[1], False
    else:
        conn, owns = obj, ctx is None
    cur = conn.cursor(dictionary=True)
    try:
        yield cur, conn
    finally:
        try:
            cur.close()
        except Exception:
            pass
        if ctx is not None:
            ctx.__exit__(None, None, None)
        elif owns:
            try:
                conn.close()
            except Exception:
                pass


def safe(name):
    """SQL に埋める名前は，必ずここを通す。"""
    if not sp.RE_IDENT.match(str(name or '')):
        raise ValueError('使えない名前です: %r' % (name,))
    return str(name)


# ================================================================ 対応式

def list_specs():
    with db() as (cur, _):
        cur.execute('SELECT id, name, title, sheet, status, note, updated_at '
                    'FROM tcv_specs ORDER BY updated_at DESC')
        return cur.fetchall()


def get_spec(spec_id):
    with db() as (cur, _):
        cur.execute('SELECT * FROM tcv_specs WHERE id=%s', (spec_id,))
        row = cur.fetchone()
    if row:
        try:
            row['spec'] = json.loads(row['spec_json'] or '{}')
        except ValueError:
            row['spec'] = {}
    return row


def get_spec_by_name(name):
    with db() as (cur, _):
        cur.execute('SELECT id FROM tcv_specs WHERE name=%s', (name,))
        row = cur.fetchone()
    return get_spec(row['id']) if row else None


def save_spec(spec_id, d, note='', status='draft', owner_id=None):
    """対応式を書き入れる。中身は必ず検めてから。"""
    sp.validate(d)
    body = json.dumps(d, ensure_ascii=False, indent=2)
    with db() as (cur, conn):
        if spec_id:
            cur.execute('UPDATE tcv_specs SET name=%s, title=%s, sheet=%s, spec_json=%s, '
                        'note=%s, status=%s, updated_at=%s WHERE id=%s',
                        (d['name'], d.get('title') or d['name'], d.get('sheet') or '',
                         body, note, status, ts(), spec_id))
        else:
            cur.execute('INSERT INTO tcv_specs (name, title, sheet, spec_json, note, status, '
                        'owner_id, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                        (d['name'], d.get('title') or d['name'], d.get('sheet') or '',
                         body, note, status, owner_id, ts(), ts()))
            spec_id = cur.lastrowid
        conn.commit()
    return spec_id


def delete_spec(spec_id):
    with db() as (cur, conn):
        cur.execute('DELETE FROM tcv_specs WHERE id=%s', (spec_id,))
        cur.execute('DELETE FROM tcv_residue WHERE spec_id=%s', (spec_id,))
        cur.execute('UPDATE tcv_samples SET spec_id=NULL WHERE spec_id=%s', (spec_id,))
        conn.commit()


# ================================================================ 標本

def list_samples():
    with db() as (cur, _):
        cur.execute('SELECT * FROM tcv_samples ORDER BY id DESC')
        return cur.fetchall()


def get_sample(sample_id):
    with db() as (cur, _):
        cur.execute('SELECT * FROM tcv_samples WHERE id=%s', (sample_id,))
        return cur.fetchone()


def save_sample(filename, data, title='', owner_id=None):
    if len(data) > MAX_SAMPLE:
        raise ValueError('標本が大きすぎます（20MB まで）')
    os.makedirs(SAMPLE_DIR, exist_ok=True)
    with db() as (cur, conn):
        cur.execute('INSERT INTO tcv_samples (title, filename, note, owner_id, created_at) '
                    'VALUES (%s,%s,%s,%s,%s)',
                    (title or filename, filename, '', owner_id, ts()))
        sid = cur.lastrowid
        conn.commit()
    with open(os.path.join(SAMPLE_DIR, '%d.xlsx' % sid), 'wb') as f:
        f.write(data)
    return sid


def sample_bytes(sample_id):
    path = os.path.join(SAMPLE_DIR, '%d.xlsx' % int(sample_id))
    if not os.path.exists(path):
        return None
    with open(path, 'rb') as f:
        return f.read()


def link_sample(sample_id, spec_id, sheet=''):
    with db() as (cur, conn):
        cur.execute('UPDATE tcv_samples SET spec_id=%s, sheet=%s WHERE id=%s',
                    (spec_id or None, sheet or '', sample_id))
        conn.commit()


def delete_sample(sample_id):
    with db() as (cur, conn):
        cur.execute('DELETE FROM tcv_samples WHERE id=%s', (sample_id,))
        cur.execute('DELETE FROM tcv_paint WHERE sample_id=%s', (sample_id,))
        conn.commit()
    path = os.path.join(SAMPLE_DIR, '%d.xlsx' % int(sample_id))
    if os.path.exists(path):
        os.remove(path)


# ================================================================ セル区分（塗り分け）

def get_paint(sample_id, sheet):
    with db() as (cur, _):
        cur.execute('SELECT * FROM tcv_paint WHERE sample_id=%s AND sheet=%s',
                    (sample_id, sheet or ''))
        row = cur.fetchone()
    if row:
        try:
            row['marks'] = (json.loads(row['marks_json'] or '{}') or {}).get('marks') or {}
        except ValueError:
            row['marks'] = {}
    return row


def save_paint(sample_id, sheet, marks_doc, note='', owner_id=None):
    body = json.dumps(marks_doc, ensure_ascii=False)
    with db() as (cur, conn):
        cur.execute('SELECT id FROM tcv_paint WHERE sample_id=%s AND sheet=%s',
                    (sample_id, sheet or ''))
        row = cur.fetchone()
        if row:
            cur.execute('UPDATE tcv_paint SET marks_json=%s, note=%s, updated_at=%s '
                        'WHERE id=%s', (body, note, ts(), row['id']))
            pid = row['id']
        else:
            cur.execute('INSERT INTO tcv_paint (sample_id, sheet, marks_json, note, '
                        'owner_id, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                        (sample_id, sheet or '', body, note, owner_id, ts(), ts()))
            pid = cur.lastrowid
        conn.commit()
    return pid


def paints_of(sample_id):
    """その標本の，シート名 → セル区分。塗っていないシートは入らない。"""
    out = {}
    with db() as (cur, _):
        cur.execute('SELECT sheet, marks_json FROM tcv_paint WHERE sample_id=%s',
                    (sample_id,))
        for r in cur.fetchall():
            try:
                out[r['sheet']] = (json.loads(r['marks_json'] or '{}') or {}).get('marks') or {}
            except ValueError:
                pass
    return out


def list_paints(sample_id=None):
    with db() as (cur, _):
        if sample_id:
            cur.execute('SELECT id, sample_id, sheet, note, updated_at FROM tcv_paint '
                        'WHERE sample_id=%s ORDER BY sheet', (sample_id,))
        else:
            cur.execute('SELECT id, sample_id, sheet, note, updated_at FROM tcv_paint '
                        'ORDER BY updated_at DESC LIMIT 50')
        return cur.fetchall()


# ================================================================ 分解したものの出し入れ

def table_exists(name):
    with db() as (cur, _):
        cur.execute('SHOW TABLES LIKE %s', (safe(name),))
        return bool(cur.fetchone())


def missing_tables(d):
    return [t for _, t in sp.tables_of(d) if not table_exists(t)]


def create_tables(d):
    """対応式の求めるテーブルを作る（無いものだけ）。"""
    made = []
    with db() as (cur, conn):
        for stmt in sp.ddl(d).split(';'):
            s = stmt.strip()
            if not s or s.startswith('--') and 'CREATE' not in s:
                continue
            body = '\n'.join(ln for ln in s.split('\n') if not ln.strip().startswith('--'))
            if not body.strip():
                continue
            cur.execute(body)
        conn.commit()
    for a, t in sp.tables_of(d):
        if table_exists(t):
            made.append(t)
    return made


def write_data(d, dataset, data):
    """分解した中身を，ふつうのSQLテーブルへ入れ替える（同じ版は先に消す）。"""
    ds_col = d.get('dataset_col', sp.DEFAULT_DATASET_COL)
    counts = {}
    with db() as (cur, conn):
        for alias, t in sp.tables_of(d):
            name = safe(t)
            cols = list(d['tables'][alias]['columns'].keys())
            if ds_col:
                cur.execute('DELETE FROM `%s` WHERE `%s`=%%s' % (name, safe(ds_col)), (dataset,))
            else:
                cur.execute('DELETE FROM `%s`' % name)
            rows = data.get(alias) or []
            if not rows:
                counts[t] = 0
                continue
            fields = (([ds_col] if ds_col else []) + ['_ord'] + cols)
            ph = ','.join(['%s'] * len(fields))
            sql = ('INSERT INTO `%s` (%s) VALUES (%s)'
                   % (name, ','.join('`%s`' % safe(c) for c in fields), ph))
            vals = []
            for r in rows:
                v = ([dataset] if ds_col else []) + [int(r.get('_ord') or 0)]
                v += [r.get(c) for c in cols]
                vals.append(tuple(v))
            cur.executemany(sql, vals)
            counts[t] = len(vals)
        conn.commit()
    return counts


def read_data(d, dataset):
    ds_col = d.get('dataset_col', sp.DEFAULT_DATASET_COL)
    out = {}
    with db() as (cur, _):
        for alias, t in sp.tables_of(d):
            name = safe(t)
            if ds_col:
                cur.execute('SELECT * FROM `%s` WHERE `%s`=%%s ORDER BY `_ord`, `id`'
                            % (name, safe(ds_col)), (dataset,))
            else:
                cur.execute('SELECT * FROM `%s` ORDER BY `_ord`, `id`' % name)
            out[alias] = cur.fetchall()
    return out


def datasets(d):
    ds_col = d.get('dataset_col', sp.DEFAULT_DATASET_COL)
    if not ds_col:
        return ['']
    found = []
    with db() as (cur, _):
        for _, t in sp.tables_of(d):
            if not table_exists(t):
                continue
            cur.execute('SELECT DISTINCT `%s` AS d FROM `%s`' % (safe(ds_col), safe(t)))
            for r in cur.fetchall():
                if r['d'] not in found:
                    found.append(r['d'])
    return sorted(found)


# ================================================================ 残余

def write_residue(spec_id, dataset, residue):
    with db() as (cur, conn):
        cur.execute('DELETE FROM tcv_residue WHERE spec_id=%s AND dataset=%s',
                    (spec_id, dataset))
        if residue:
            cur.executemany(
                'INSERT INTO tcv_residue (spec_id, dataset, mode, band, ord_no, dr, r_no, '
                'c_no, rs, cs, v) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                [(spec_id, dataset, e.get('mode'), e.get('band') or '', int(e.get('ord') or 0),
                  int(e.get('dr') or 0), int(e.get('r') or 0), int(e.get('c') or 0),
                  int(e.get('rs') or 1), int(e.get('cs') or 1), e.get('v') or '')
                 for e in residue])
        conn.commit()
    return len(residue or [])


def read_residue(spec_id, dataset):
    with db() as (cur, _):
        cur.execute('SELECT * FROM tcv_residue WHERE spec_id=%s AND dataset=%s '
                    'ORDER BY id', (spec_id, dataset))
        return [{'mode': r['mode'], 'band': r['band'], 'ord': r['ord_no'], 'dr': r['dr'],
                 'r': r['r_no'], 'c': r['c_no'], 'rs': r['rs'], 'cs': r['cs'], 'v': r['v']}
                for r in cur.fetchall()]


# ================================================================ 実行の記録

def log_run(spec_id, dataset, direction, summary, actor_id):
    with db() as (cur, conn):
        cur.execute('INSERT INTO tcv_runs (spec_id, dataset, direction, summary, actor_id, '
                    'created_at) VALUES (%s,%s,%s,%s,%s,%s)',
                    (spec_id, dataset, direction,
                     json.dumps(summary, ensure_ascii=False)[:4000], actor_id, ts()))
        conn.commit()


def list_runs(spec_id=None, limit=30):
    with db() as (cur, _):
        if spec_id:
            cur.execute('SELECT * FROM tcv_runs WHERE spec_id=%s ORDER BY id DESC LIMIT %s',
                        (spec_id, int(limit)))
        else:
            cur.execute('SELECT * FROM tcv_runs ORDER BY id DESC LIMIT %s', (int(limit),))
        return cur.fetchall()
