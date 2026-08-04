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

# sql_saver/routes_backup.py
# SQL Saver バックアップ（レベル2: 中断再開対応）
#   方式: ジョブ単位で状態を job_state.json に保持し、フロントが
#         start -> step(×N) -> finalize と進める。
#         step は「未完了テーブルだけ」を最大BATCH件処理するので、
#         途中で止めても続きから再開でき、必ず全テーブルが揃うまで完了しない。
#
#   アクセス制御は routes.require_admin（Blueprintの before_request）が一括で行うため、
#   各ハンドラでの admin 判定は持たない。

import os
import re
import json
import shutil
import logging
import zipfile
import secrets
import decimal
import datetime
import time
import mysql.connector
from flask import request, jsonify, session, send_file
from decorators import login_required
from . import sql_saver_bp
from .routes import (
    get_target_databases, db_name_to_config, backups_root, restore_work_root,
    legacy_backups_root, safe_ident, stamp_jst, now_jst_str, audit,
)

BATCH = 10                       # 1 step で処理するテーブル数
JOB_PREFIX = 'job_'              # 作業ディレクトリ名の接頭辞
JOB_STALE_HOURS = 24             # これより古い未完了ジョブは start 時に掃除


# ============================================================
# 値エンコード（型保持）
# ============================================================

def encode_value(v):
    if v is None:
        return None
    if isinstance(v, datetime.datetime):
        return {'__type__': 'datetime', 'value': v.isoformat(sep=' ')}
    if isinstance(v, datetime.date):
        return {'__type__': 'date', 'value': v.isoformat()}
    if isinstance(v, datetime.timedelta):
        return {'__type__': 'timedelta', 'value': v.total_seconds()}
    if isinstance(v, decimal.Decimal):
        return {'__type__': 'decimal', 'value': str(v)}
    if isinstance(v, (bytes, bytearray)):
        return {'__type__': 'bytes', 'value': v.hex()}
    return v


# ============================================================
# ジョブ状態の読み書き
#   job_state.json:
#   {
#     "job_id": "job_YYYYmmdd_HHMMSS",          # 日時はJST
#     "created_at": "...", "created_by": <id>,
#     "format_version": 1,
#     "databases": [{"key","db"}, ...],
#     "tables": {"<db_name>": ["t1","t2",...], ...},   # 対象テーブル(確定)
#     "done":   {"<db_name>": ["t1",...], ...},        # 完了テーブル
#     "row_counts": {"<db_name>": {"t1": 75, ...}, ...}
#   }
# ============================================================

def _job_dir(job_id):
    return os.path.join(backups_root(), job_id)


def _state_path(job_id):
    return os.path.join(_job_dir(job_id), 'job_state.json')


def _load_state(job_id):
    p = _state_path(job_id)
    if not os.path.exists(p):
        return None
    with open(p, encoding='utf-8') as f:
        return json.load(f)


def _save_state(state):
    with open(_state_path(state['job_id']), 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=1)

# job_id は必ず 'job_YYYYmmdd_HHMMSS' の形。safe_ident はテーブル名向けに
# 多バイトを許すようになったため、こちらは専用の厳格な検査を持つ。
_JOB_ID_RE = re.compile(r'^job_\d{8}_\d{6}$')


def _safe_job_id(raw):
    """外部から来た job_id を検証して返す。不正なら ValueError。"""
    job_id = os.path.basename(raw or '')
    if not _JOB_ID_RE.match(job_id):
        raise ValueError('不正なjob_idです')
    return job_id



def _progress(state):
    """進捗サマリ（画面表示用）を作る"""
    per_db = []
    total = 0
    total_done = 0
    for d in state['databases']:
        db = d['db']
        tot = len(state['tables'].get(db, []))
        done = len(state['done'].get(db, []))
        total += tot
        total_done += done
        per_db.append({'key': d['key'], 'db': db, 'total': tot, 'done': done})
    return {'total': total, 'total_done': total_done, 'per_db': per_db}


def _is_complete(state):
    for d in state['databases']:
        db = d['db']
        if len(state['done'].get(db, [])) < len(state['tables'].get(db, [])):
            return False
    return True


def _cleanup_stale_jobs():
    """古い未完了ジョブ作業ディレクトリを掃除"""
    root = backups_root()
    cutoff = time.time() - JOB_STALE_HOURS * 3600
    try:
        for name in os.listdir(root):
            if not name.startswith(JOB_PREFIX):
                continue
            path = os.path.join(root, name)
            if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
    except Exception as e:
        logging.error("cleanup_stale_jobs error: %s", e)


def active_jobs():
    """未完了のバックアップジョブID一覧（clear_all の保護に使う）"""
    result = []
    try:
        root = backups_root()
        for name in os.listdir(root):
            if not name.startswith(JOB_PREFIX):
                continue
            if not os.path.isdir(os.path.join(root, name)):
                continue
            state = _load_state(name)
            if state and not _is_complete(state):
                result.append(name)
    except Exception as e:
        logging.error("active_jobs error: %s", e)
    return result


def active_restores():
    """展開中のリストア作業ディレクトリ一覧（clear_all の保護に使う）"""
    try:
        root = restore_work_root()
        return [n for n in os.listdir(root)
                if os.path.isdir(os.path.join(root, n))]
    except Exception as e:
        logging.error("active_restores error: %s", e)
        return []


# ============================================================
# 1テーブル dump（JSON書き出し）
# ============================================================

def _dump_one_table(db_name, table_name, out_dir):
    safe_ident(table_name, 'テーブル名')
    conn = mysql.connector.connect(**db_name_to_config(db_name))
    try:
        cursor = conn.cursor()
        cursor.execute("SHOW CREATE TABLE `%s`" % table_name)
        create_row = cursor.fetchone()
        create_sql = create_row[1] if create_row else None

        cursor.execute("SELECT * FROM `%s`" % table_name)
        columns = [d[0] for d in cursor.description]
        rows = [[encode_value(v) for v in row] for row in cursor.fetchall()]
        cursor.close()

        dumped = {
            'table': table_name,
            'create_sql': create_sql,
            'columns': columns,
            'row_count': len(rows),
            'rows': rows,
        }
        with open(os.path.join(out_dir, "%s.json" % table_name), 'w', encoding='utf-8') as f:
            json.dump(dumped, f, ensure_ascii=False, indent=1)
        return len(rows)
    finally:
        if conn.is_connected():
            conn.close()


# ============================================================
# backup/start : ジョブ作成（または既存未完了ジョブの再開候補を返す）
#   POST { databases: ["default","fujinp",...], resume_job_id?: "job_..." }
# ============================================================

@sql_saver_bp.route('/backup/start', methods=['POST'])
@login_required
def backup_start():
    user_id = session.get('user_id')

    data = request.json or {}
    selected_keys = data.get('databases') or []
    if not selected_keys:
        return jsonify({'success': False, 'error': '対象データベースが選択されていません'}), 400

    targets = [t for t in get_target_databases() if t['key'] in selected_keys]
    if not targets:
        return jsonify({'success': False, 'error': '有効な対象データベースがありません'}), 400

    _cleanup_stale_jobs()

    # 再開指定があれば、その未完了ジョブを返す
    resume = data.get('resume_job_id')
    if resume:
        try:
            resume = _safe_job_id(resume)
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        state = _load_state(resume)
        if state and not _is_complete(state):
            return jsonify({'success': True, 'job_id': state['job_id'],
                            'resumed': True, 'progress': _progress(state)})

    # 新規ジョブ作成（日時はJST）
    job_id = "%s%s" % (JOB_PREFIX, stamp_jst())
    job_dir = _job_dir(job_id)
    if os.path.exists(job_dir):
        shutil.rmtree(job_dir)
    os.makedirs(job_dir, exist_ok=True)

    tables = {}
    try:
        for t in targets:
            db = t['db']
            os.makedirs(os.path.join(job_dir, db), exist_ok=True)
            conn = mysql.connector.connect(**db_name_to_config(db))
            cursor = conn.cursor()
            cursor.execute("SHOW TABLES")
            names = []
            for n in [r[0] for r in cursor.fetchall()]:
                try:
                    safe_ident(n, 'テーブル名')
                except ValueError:
                    # 1つの異常なテーブル名でバックアップ全体を止めない。
                    # 対象から外してログに残す（zipには含まれない）。
                    logging.warning("backup_start: テーブル名を除外 %r (%s)", n, db)
                    continue
                names.append(n)
            tables[db] = names

            cursor.close(); conn.close()
    except Exception as e:
        logging.error("backup_start enumerate error: %s", e)
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify({'success': False, 'error': str(e)}), 500

    state = {
        'job_id': job_id,
        'created_at': now_jst_str(),
        'created_by': user_id,
        'format_version': 1,
        'databases': [{'key': t['key'], 'db': t['db']} for t in targets],
        'tables': tables,
        'done': {t['db']: [] for t in targets},
        'row_counts': {t['db']: {} for t in targets},
    }
    _save_state(state)

    return jsonify({'success': True, 'job_id': job_id, 'resumed': False,
                    'progress': _progress(state)})


# ============================================================
# backup/step : 未完了テーブルを最大BATCH件処理
#   POST { job_id }
# ============================================================

@sql_saver_bp.route('/backup/step', methods=['POST'])
@login_required
def backup_step():
    try:
        job_id = _safe_job_id((request.json or {}).get('job_id'))
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400

    state = _load_state(job_id)
    if state is None:
        return jsonify({'success': False, 'error': 'ジョブが見つかりません'}), 404

    processed = []
    count = 0
    try:
        for d in state['databases']:
            if count >= BATCH:
                break
            db = d['db']
            done_set = set(state['done'].get(db, []))
            for tbl in state['tables'].get(db, []):
                if count >= BATCH:
                    break
                if tbl in done_set:
                    continue
                out_dir = os.path.join(_job_dir(job_id), db)
                n = _dump_one_table(db, tbl, out_dir)
                state['done'][db].append(tbl)
                state['row_counts'][db][tbl] = n
                done_set.add(tbl)
                processed.append({'db': db, 'table': tbl, 'rows': n})
                count += 1

        _save_state(state)
        complete = _is_complete(state)
        return jsonify({'success': True, 'job_id': job_id,
                        'processed': processed, 'complete': complete,
                        'progress': _progress(state)})
    except Exception as e:
        logging.error("backup_step error (%s): %s", job_id, e)
        # 途中まで完了したぶんは保存して残す（再開可能）
        try:
            _save_state(state)
        except Exception:
            pass
        return jsonify({'success': False, 'error': str(e),
                        'job_id': job_id, 'progress': _progress(state)}), 500


# ============================================================
# backup/finalize : 全完了を確認してzip化
#   POST { job_id }
# ============================================================

@sql_saver_bp.route('/backup/finalize', methods=['POST'])
@login_required
def backup_finalize():
    try:
        job_id = _safe_job_id((request.json or {}).get('job_id'))
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400

    state = _load_state(job_id)
    if state is None:
        return jsonify({'success': False, 'error': 'ジョブが見つかりません'}), 404
    if not _is_complete(state):
        return jsonify({'success': False, 'error': 'まだ未完了のテーブルがあります',
                        'progress': _progress(state)}), 400

    job_dir = _job_dir(job_id)
    try:
        manifest = {
            'created_at': state['created_at'],
            'created_by': state['created_by'],
            'format_version': state['format_version'],
            'databases': [{'key': d['key'], 'db': d['db'],
                           'tables': state['tables'][d['db']]}
                          for d in state['databases']],
        }
        with open(os.path.join(job_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1)

        # ファイル名に乱数を混ぜる（保存先は static 外だが、名前の推測を難しくしておく）
        stamp = job_id[len(JOB_PREFIX):]
        zip_basename = "backup_%s_%s" % (stamp, secrets.token_hex(4))
        root = backups_root()
        zip_path = os.path.join(root, zip_basename + '.zip')
        if os.path.exists(zip_path):
            os.remove(zip_path)

        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for dirpath, _dirs, files in os.walk(job_dir):
                for fn in files:
                    if fn == 'job_state.json':
                        continue
                    full = os.path.join(dirpath, fn)
                    rel = os.path.relpath(full, job_dir)
                    arc = os.path.join(zip_basename, rel)
                    zf.write(full, arc)

        shutil.rmtree(job_dir, ignore_errors=True)

        summary = []
        for d in state['databases']:
            db = d['db']
            tbls = [{'table': t, 'rows': state['row_counts'][db].get(t, 0)}
                    for t in state['tables'][db]]
            summary.append({'key': d['key'], 'db': db, 'tables': tbls})

        audit('backup', {
            'zip_name': zip_basename + '.zip',
            'zip_size': os.path.getsize(zip_path),
            'databases': [{'db': s['db'], 'table_count': len(s['tables']),
                           'row_total': sum(t['rows'] for t in s['tables'])}
                          for s in summary],
        })

        return jsonify({'success': True, 'message': 'バックアップを作成しました',
                        'zip_name': zip_basename + '.zip',
                        'zip_size': os.path.getsize(zip_path),
                        'summary': summary})
    except Exception as e:
        logging.error("backup_finalize error (%s): %s", job_id, e)
        audit('backup', {'job_id': job_id, 'error': str(e)}, ok=False)
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================
# zip ダウンロード
#   作業領域は static の外にあるので、この経路（admin限定）以外から取得できない。
# ============================================================

@sql_saver_bp.route('/download/<path:zip_name>', methods=['GET'])
@login_required
def download(zip_name):
    safe_name = os.path.basename(zip_name)
    if not safe_name.endswith('.zip') or not safe_name.startswith('backup_'):
        return jsonify({'success': False, 'error': '不正なファイル名です'}), 400

    root = backups_root()
    zip_path = os.path.realpath(os.path.join(root, safe_name))
    if not zip_path.startswith(os.path.realpath(root) + os.sep):
        return jsonify({'success': False, 'error': '不正なファイル名です'}), 400
    if not os.path.exists(zip_path):
        return jsonify({'success': False, 'error': 'ファイルが見つかりません'}), 404

    return send_file(zip_path, as_attachment=True, download_name=safe_name)


# ============================================================
# 作業ファイル全クリア
#   作業ルートの中身（job_*, _restore_work, backup_*.zip 等）をすべて削除する。
#   v1.0 の保存先（static/sql_saver_backups/）が残っていれば併せて削除する。
#   進行中のジョブ・リストアがある場合は force を要求する。
# ============================================================

@sql_saver_bp.route('/clear_all', methods=['POST'])
@login_required
def clear_all():
    data = request.json or {}
    force = bool(data.get('force'))

    jobs = active_jobs()
    restores = active_restores()
    if (jobs or restores) and not force:
        parts = []
        if jobs:
            parts.append('未完了のバックアップ %d件' % len(jobs))
        if restores:
            parts.append('展開中のリストア %d件' % len(restores))
        return jsonify({'success': False, 'need_force': True,
                        'error': '進行中の作業があります（%s）。削除すると再開できなくなります。'
                                 % '，'.join(parts)}), 409

    removed = 0
    freed = 0
    targets = [backups_root()]
    legacy = legacy_backups_root()
    if legacy:
        targets.append(legacy)

    try:
        for root in targets:
            for name in os.listdir(root):
                path = os.path.join(root, name)
                try:
                    if os.path.isdir(path):
                        # ディレクトリ内の合計サイズを集計してから削除
                        for dp, _d, files in os.walk(path):
                            for fn in files:
                                try:
                                    freed += os.path.getsize(os.path.join(dp, fn))
                                except OSError:
                                    pass
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        try:
                            freed += os.path.getsize(path)
                        except OSError:
                            pass
                        os.remove(path)
                    removed += 1
                except Exception as ie:
                    logging.error("clear_all remove %s error: %s", path, ie)

        message = '作業ファイルを全て削除しました（%d項目）' % removed
        if legacy:
            message += '／旧保存先 static/sql_saver_backups も削除しました'
        audit('clear_all', {'removed': removed, 'freed': freed,
                            'forced': force, 'legacy_cleaned': bool(legacy)})
        return jsonify({'success': True, 'message': message,
                        'removed': removed, 'freed': freed})
    except Exception as e:
        logging.error("clear_all error: %s", e)
        audit('clear_all', {'error': str(e)}, ok=False)
        return jsonify({'success': False, 'error': str(e)}), 500