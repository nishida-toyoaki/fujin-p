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

# sql_saver/routes_restore.py
# SQL Saver リストア: zipアップロード → 中身解析（破壊対象の確認）→ 完全置換で復元
#   方式: バックアップと同じジョブ方式に統一。
#         inspect（展開・解析）→ start（計画作成）→ step(×N) → finish（後片付け）
#         step は最大 RESTORE_BATCH テーブルずつ処理するので、進捗が出せ、
#         大きなDBでも1リクエストのタイムアウトに当たらない。
#
#   アクセス制御は routes.require_admin（Blueprintの before_request）が一括で行う。

import os
import re
import json
import time
import shutil
import logging
import zipfile
import secrets
import decimal
import datetime
import mysql.connector
from werkzeug.utils import secure_filename
from flask import request, jsonify, session
from decorators import login_required
from . import sql_saver_bp
from .routes import (
    known_db_names, db_name_to_config, restore_work_root,
    safe_ident, stamp_jst, now_jst_str, audit,
)

RESTORE_BATCH = 5              # 1 step で復元するテーブル数
INSERT_BATCH = 500             # executemany の1回あたり行数
RESTORE_STALE_HOURS = 24       # これより古い展開データは inspect 時に掃除

# CREATE TABLE 文から対象テーブル名を取り出す（zip由来DDLの検証用）
# 名前は多バイト文字を含み得るので、バッククォート・空白・開き括弧の手前までを取る。
# 取り出した名前は復元先テーブル名と厳密一致で照合するので、これで十分。
_CREATE_RE = re.compile(
    r'^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?([^`\s(]+)`?', re.I)

# ============================================================
# 値のデコード（バックアップ時の encode_value の逆）
# ============================================================

def decode_value(v):
    if isinstance(v, dict) and '__type__' in v:
        t = v['__type__']; val = v['value']
        if t == 'datetime':
            return val            # MySQLは 'YYYY-MM-DD HH:MM:SS' 文字列を受け付ける
        if t == 'date':
            return val
        if t == 'timedelta':
            return str(datetime.timedelta(seconds=val))
        if t == 'decimal':
            return decimal.Decimal(val)
        if t == 'bytes':
            return bytes.fromhex(val)
        return val
    return v


# ============================================================
# 展開先とジョブ状態
# ============================================================

def _extract_dir(token):
    return os.path.join(restore_work_root(), token)


def _safe_token(raw):
    token = secure_filename(raw or '')
    if not token:
        raise ValueError('不正なtokenです')
    return token


def _state_path(token):
    return os.path.join(_extract_dir(token), 'restore_state.json')


def _load_state(token):
    p = _state_path(token)
    if not os.path.exists(p):
        return None
    with open(p, encoding='utf-8') as f:
        return json.load(f)


def _save_state(state):
    with open(_state_path(state['token']), 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def _progress(state):
    total = len(state['plan'])
    done = state['pos']
    per_db = {}
    for item in state['plan']:
        per_db.setdefault(item['db'], {'db': item['db'], 'total': 0, 'done': 0})
        per_db[item['db']]['total'] += 1
    for item in state['plan'][:done]:
        per_db[item['db']]['done'] += 1
    return {'total': total, 'total_done': done, 'per_db': list(per_db.values()),
            'ok_count': len(state['done']), 'error_count': len(state['errors'])}


def _cleanup_stale_restores():
    """古い展開データを掃除"""
    root = restore_work_root()
    cutoff = time.time() - RESTORE_STALE_HOURS * 3600
    try:
        for name in os.listdir(root):
            path = os.path.join(root, name)
            if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
    except Exception as e:
        logging.error("cleanup_stale_restores error: %s", e)


def _read_manifest_or_scan(extract_dir):
    """展開ディレクトリから manifest を読む。無ければディレクトリ構造から推定。

    返り値: (base, [{'db': <dbname>, 'tables': [...], 'path': <dir>}, ...])
    （zip内ルートは backup_<stamp>_<rand>/<dbname>/<table>.json 構造）
    """
    entries = [d for d in os.listdir(extract_dir)
               if os.path.isdir(os.path.join(extract_dir, d))]
    # トップが1つならその中を見る、それ以外はextract_dir直下を見る
    base = os.path.join(extract_dir, entries[0]) if len(entries) == 1 else extract_dir

    manifest_path = os.path.join(base, 'manifest.json')
    if os.path.exists(manifest_path):
        with open(manifest_path, encoding='utf-8') as f:
            man = json.load(f)
        dbs = []
        for d in man.get('databases', []):
            dbs.append({'db': d['db'],
                        'tables': d.get('tables', []),
                        'path': os.path.join(base, d['db'])})
        return base, dbs

    # manifest が無い場合: <base>/<dbname>/*.json をスキャン
    dbs = []
    for d in os.listdir(base):
        dpath = os.path.join(base, d)
        if os.path.isdir(dpath):
            tables = [fn[:-5] for fn in os.listdir(dpath) if fn.endswith('.json')]
            if tables:
                dbs.append({'db': d, 'tables': tables, 'path': dpath})
    return base, dbs


# ============================================================
# restore/inspect : アップロード + 解析
#   multipart/form-data で zip を受け取り展開、中身と破壊対象を返す。
#   実際の復元はまだ行わない（確認のためトークンを返す）。
# ============================================================

@sql_saver_bp.route('/restore/inspect', methods=['POST'])
@login_required
def restore_inspect():
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'ファイルがありません'}), 400
    f = request.files['file']
    if not f.filename or not f.filename.lower().endswith('.zip'):
        return jsonify({'success': False, 'error': '.zip ファイルを指定してください'}), 400

    _cleanup_stale_restores()

    # 一意な展開先（日時はJST）
    token = "%s_%s" % (stamp_jst(), secrets.token_hex(4))
    extract_dir = _extract_dir(token)
    os.makedirs(extract_dir, exist_ok=True)

    zip_save_path = os.path.join(extract_dir, secure_filename(f.filename))
    f.save(zip_save_path)

    try:
        with zipfile.ZipFile(zip_save_path) as zf:
            # zipスリップ対策: 各メンバーの展開先がextract_dir内に収まるか確認
            for member in zf.namelist():
                dest = os.path.realpath(os.path.join(extract_dir, member))
                if not dest.startswith(os.path.realpath(extract_dir) + os.sep):
                    raise ValueError('不正なzipエントリ: %s' % member)
            zf.extractall(extract_dir)
        os.remove(zip_save_path)

        base, dbs_in_zip = _read_manifest_or_scan(extract_dir)
        if not dbs_in_zip:
            shutil.rmtree(extract_dir, ignore_errors=True)
            return jsonify({'success': False, 'error': 'バックアップ内容が見つかりません'}), 400

        # 既知の対象DB名の集合（復元先として許可するDB）
        known = known_db_names()

        report = []
        for d in dbs_in_zip:
            db_name = d['db']
            allowed = db_name in known

            existing = set()
            reachable = False
            if allowed:
                conn = None
                cursor = None
                try:
                    conn = mysql.connector.connect(**db_name_to_config(db_name))
                    cursor = conn.cursor()
                    cursor.execute("SHOW TABLES")
                    existing = {r[0] for r in cursor.fetchall()}
                    reachable = True
                except Exception as e:
                    logging.error("inspect connect %s error: %s", db_name, e)
                finally:
                    if cursor is not None:
                        try:
                            cursor.close()
                        except Exception:
                            pass
                    if conn is not None and conn.is_connected():
                        conn.close()

            tbl_report = []
            for tbl in d['tables']:
                try:
                    safe_ident(tbl, 'テーブル名')
                    bad = False
                except ValueError:
                    bad = True
                tbl_report.append({
                    'table': tbl,
                    'will_overwrite': tbl in existing,   # 既存 → DROPして再作成（破壊）
                    'invalid': bad,                      # 名前が不正 → 復元対象にできない
                })
            report.append({
                'db': db_name,
                'allowed': allowed,          # 復元先として許可されたDBか
                'reachable': reachable,
                'tables': tbl_report,
                'overwrite_count': sum(1 for x in tbl_report if x['will_overwrite']),
                'new_count': sum(1 for x in tbl_report
                                 if not x['will_overwrite'] and not x['invalid']),
            })

        return jsonify({
            'success': True,
            'token': token,           # start 時にこのtokenを渡す
            'report': report,
        })

    except Exception as e:
        logging.error("restore_inspect error: %s", e)
        shutil.rmtree(extract_dir, ignore_errors=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================
# restore/start : 復元計画を作る
#   POST { token, databases: [<dbname>,...], tables: { <dbname>: [<table>,...] } }
#   tables 省略時は該当DBの全テーブル。
# ============================================================

@sql_saver_bp.route('/restore/start', methods=['POST'])
@login_required
def restore_start():
    data = request.json or {}
    try:
        token = _safe_token(data.get('token'))
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400

    selected_dbs = data.get('databases') or []
    table_filter = data.get('tables') or {}
    if not selected_dbs:
        return jsonify({'success': False, 'error': '復元先が選択されていません'}), 400

    extract_dir = _extract_dir(token)
    if not os.path.isdir(extract_dir):
        return jsonify({'success': False,
                        'error': '展開データが見つかりません（再アップロードしてください）'}), 404

    known = known_db_names()
    try:
        _base, dbs_in_zip = _read_manifest_or_scan(extract_dir)
        dbmap = {d['db']: d for d in dbs_in_zip}

        plan = []
        skipped = []
        for db_name in selected_dbs:
            if db_name not in known:
                skipped.append({'db': db_name, 'reason': '許可されていないDB'})
                continue
            if db_name not in dbmap:
                skipped.append({'db': db_name, 'reason': 'zip内に存在しません'})
                continue

            d = dbmap[db_name]
            wanted = table_filter.get(db_name)      # None なら全テーブル
            for tbl in d['tables']:
                if wanted is not None and tbl not in wanted:
                    continue
                try:
                    safe_ident(tbl, 'テーブル名')
                except ValueError:
                    skipped.append({'db': db_name, 'reason': '不正なテーブル名: %s' % tbl})
                    continue
                plan.append({'db': db_name, 'table': tbl, 'path': d['path']})

        if not plan:
            return jsonify({'success': False,
                            'error': '復元対象のテーブルがありません', 'skipped': skipped}), 400

        state = {
            'token': token,
            'created_at': now_jst_str(),
            'created_by': session.get('user_id'),
            'plan': plan,
            'pos': 0,
            'done': [],
            'errors': [],
            'skipped': skipped,
        }
        _save_state(state)
        return jsonify({'success': True, 'token': token,
                        'skipped': skipped, 'progress': _progress(state)})

    except Exception as e:
        logging.error("restore_start error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================
# restore/step : 最大 RESTORE_BATCH テーブルを完全置換で復元
#   POST { token }
# ============================================================

def _restore_one_table(cursor, item):
    """1テーブルを DROP → CREATE → INSERT で置換し、行数を返す"""
    tbl = safe_ident(item['table'], 'テーブル名')
    jpath = os.path.join(item['path'], "%s.json" % tbl)
    if not os.path.exists(jpath):
        raise FileNotFoundError('JSONなし')

    with open(jpath, encoding='utf-8') as jf:
        dumped = json.load(jf)

    create_sql = dumped.get('create_sql')
    columns = dumped.get('columns') or []
    rows = dumped.get('rows') or []

    # DDLが本当にこのテーブルのものかを確認（zip由来の内容を信用しない）
    if not create_sql:
        raise ValueError('CREATE TABLE 文がありません')
    m = _CREATE_RE.match(create_sql)
    if not m:
        raise ValueError('CREATE TABLE 文を解釈できません')
    if m.group(1) != tbl:
        raise ValueError('CREATE TABLE 文のテーブル名が一致しません（%s）' % m.group(1))
    for c in columns:
        safe_ident(c, 'カラム名')

    cursor.execute("DROP TABLE IF EXISTS `%s`" % tbl)
    cursor.execute(create_sql)

    if rows and columns:
        col_list = ', '.join("`%s`" % c for c in columns)
        placeholders = ', '.join(['%s'] * len(columns))
        sql = "INSERT INTO `%s` (%s) VALUES (%s)" % (tbl, col_list, placeholders)
        decoded = [tuple(decode_value(v) for v in row) for row in rows]
        for i in range(0, len(decoded), INSERT_BATCH):
            cursor.executemany(sql, decoded[i:i + INSERT_BATCH])

    return len(rows)


@sql_saver_bp.route('/restore/step', methods=['POST'])
@login_required
def restore_step():
    try:
        token = _safe_token((request.json or {}).get('token'))
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400

    state = _load_state(token)
    if state is None:
        return jsonify({'success': False, 'error': '復元ジョブが見つかりません'}), 404

    processed = []
    conn = None
    cursor = None
    cur_db = None
    try:
        count = 0
        while state['pos'] < len(state['plan']) and count < RESTORE_BATCH:
            item = state['plan'][state['pos']]

            # DBが変わったら接続を張り替える
            if item['db'] != cur_db:
                if conn is not None:
                    try:
                        cursor.execute("SET FOREIGN_KEY_CHECKS=1")
                        cursor.close()
                    except Exception:
                        pass
                    if conn.is_connected():
                        conn.close()
                conn = mysql.connector.connect(**db_name_to_config(item['db']))
                cursor = conn.cursor()
                # 外部キー制約があってもDROP/再作成できるよう一時的に無効化
                cursor.execute("SET FOREIGN_KEY_CHECKS=0")
                cur_db = item['db']

            try:
                n = _restore_one_table(cursor, item)
                conn.commit()
                state['done'].append({'db': item['db'], 'table': item['table'], 'rows': n})
                processed.append({'db': item['db'], 'table': item['table'],
                                  'rows': n, 'ok': True})
            except Exception as te:
                try:
                    conn.rollback()
                except Exception:
                    pass
                logging.error("restore table %s.%s error: %s",
                              item['db'], item['table'], te)
                state['errors'].append({'db': item['db'], 'table': item['table'],
                                        'error': str(te)})
                processed.append({'db': item['db'], 'table': item['table'],
                                  'ok': False, 'error': str(te)})

            state['pos'] += 1
            count += 1

        _save_state(state)
        return jsonify({'success': True, 'token': token, 'processed': processed,
                        'complete': state['pos'] >= len(state['plan']),
                        'progress': _progress(state)})

    except Exception as e:
        logging.error("restore_step error (%s): %s", token, e)
        try:
            _save_state(state)
        except Exception:
            pass
        return jsonify({'success': False, 'error': str(e),
                        'token': token, 'progress': _progress(state)}), 500
    finally:
        if cursor is not None:
            try:
                cursor.execute("SET FOREIGN_KEY_CHECKS=1")
                cursor.close()
            except Exception:
                pass
        if conn is not None and conn.is_connected():
            conn.close()


# ============================================================
# restore/finish : 結果をまとめて展開データを掃除
#   POST { token }
# ============================================================

@sql_saver_bp.route('/restore/finish', methods=['POST'])
@login_required
def restore_finish():
    try:
        token = _safe_token((request.json or {}).get('token'))
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400

    state = _load_state(token)
    if state is None:
        return jsonify({'success': False, 'error': '復元ジョブが見つかりません'}), 404
    if state['pos'] < len(state['plan']):
        return jsonify({'success': False, 'error': 'まだ未処理のテーブルがあります',
                        'progress': _progress(state)}), 400

    results = {}
    for item in state['done']:
        r = results.setdefault(item['db'], {'db': item['db'], 'restored': [], 'errors': []})
        r['restored'].append({'table': item['table'], 'rows': item['rows']})
    for item in state['errors']:
        r = results.setdefault(item['db'], {'db': item['db'], 'restored': [], 'errors': []})
        r['errors'].append({'table': item['table'], 'error': item['error']})

    ok = len(state['errors']) == 0
    audit('restore', {
        'token': token,
        'databases': [{'db': r['db'],
                       'restored': len(r['restored']),
                       'errors': len(r['errors']),
                       'row_total': sum(t['rows'] for t in r['restored'])}
                      for r in results.values()],
        'skipped': state.get('skipped', []),
    }, ok=ok)

    shutil.rmtree(_extract_dir(token), ignore_errors=True)

    message = 'リストアが完了しました' if ok else 'リストアが完了しました（エラーあり）'
    return jsonify({'success': True, 'message': message,
                    'results': list(results.values()),
                    'skipped': state.get('skipped', [])})


# ============================================================
# restore/cancel : 中断・キャンセル時の展開データ掃除
# ============================================================

@sql_saver_bp.route('/restore/cancel', methods=['POST'])
@login_required
def restore_cancel():
    token = (request.json or {}).get('token')
    if token:
        try:
            safe = _safe_token(token)
        except ValueError:
            return jsonify({'success': True})
        extract_dir = _extract_dir(safe)
        if os.path.isdir(extract_dir):
            shutil.rmtree(extract_dir, ignore_errors=True)
    return jsonify({'success': True})