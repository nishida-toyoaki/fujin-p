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
まいあし (MaiAshi) 教材と履修記録のエクスポート／インポート（サイト間移設用）

エクスポート
    GET  /migration_assistant/admin/export_content
         ?course=1,3   教材IDで絞る（省略時は全教材）
         ?images=0     画像を同梱しない（省略時は同梱）
    教材（Phase / Stage / Step を含む），受講登録，進捗，師匠候補の記録を，
    関係するユーザの ID・氏名・メールアドレスとセットにした JSON をダウンロードさせる．

インポート
    POST /migration_assistant/admin/import_content   (multipart/form-data)
         file    エクスポートした JSON
         commit  1 で書き込み．それ以外は試行（全処理を行ったうえでロールバック）
         force   1 で同じ作成者・同じタイトルの既存教材があっても新しく作る
         map     メールアドレスの読み替え．1行に「旧アドレス=新アドレス」
    ユーザはメールアドレスで受け入れ側の users に対応付け，ID を付け替える．
    教材IDは新しく採番し，phase_id / stage_id / step_id はそのまま使う．

付け替えの規則
    - メールアドレスは前後の空白を除き，大文字小文字を区別せずに照合する
    - 作成者を対応付けられない教材は，配下（Phase/Stage/Step/受講/進捗）ごと取り込まない
    - 対応付けられない弟子の受講登録と進捗は取り込まない
    - 受講登録の mentor_user_id が対応付けられないときは，新しい教材の作成者IDを入れる
    - 師匠候補の approved_by が対応付けられないときは，本人のIDを入れる（自動昇格と同じ形）
    - 受け入れ側に既にある師匠候補の記録と受講登録は上書きしない．進捗は上書きする
    - 画像は受け入れ側の UPLOAD_FOLDER に無いものだけ書く（同名のファイルは上書きしない）

いずれも管理者（users.category='admin'）のみ．JSON にメールアドレスを含むため．
形式は export_type=maiashi_content, format_version=2（tables のキーは改名後の表名）．
旧表名で書き出した format_version=1 の JSON も，キーを読み替えて取り込める．
"""
import base64
import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal

from flask import Response, jsonify, request, session

from config import Config
from db import Tables

from . import migration_assistant
from .migration_assistant_routes import (
    check_is_admin, get_db_connection,
    COURSES_TABLE, PHASES_TABLE, STAGES_TABLE, STEPS_TABLE,
    ENROLLMENTS_TABLE, PROGRESS_TABLE, MENTORS_TABLE,
)

JST = timezone(timedelta(hours=9), 'JST')

# 書き出すテーブル（取り込み順）
TRANSFER_TABLES = [
    COURSES_TABLE,
    PHASES_TABLE,
    STAGES_TABLE,
    STEPS_TABLE,
    ENROLLMENTS_TABLE,
    PROGRESS_TABLE,
    MENTORS_TABLE,
]

# 教材IDで絞り込む配下のテーブル
CHILD_TABLES = [PHASES_TABLE, STAGES_TABLE, STEPS_TABLE, ENROLLMENTS_TABLE, PROGRESS_TABLE]

# ユーザIDを持つ列（受け入れ側で付け替える列）
TRANSFER_USER_COLUMNS = {
    COURSES_TABLE: ['creator_user_id'],
    ENROLLMENTS_TABLE: ['student_user_id', 'mentor_user_id'],
    PROGRESS_TABLE: ['student_user_id'],
    MENTORS_TABLE: ['user_id', 'approved_by'],
}

# 教材IDを持つ列（受け入れ側で付け替える列）
TRANSFER_COURSE_COLUMNS = {
    COURSES_TABLE: ['id'],
    PHASES_TABLE: ['course_id'],
    STAGES_TABLE: ['course_id'],
    STEPS_TABLE: ['course_id'],
    ENROLLMENTS_TABLE: ['course_id'],
    PROGRESS_TABLE: ['course_id'],
}

# 取り込み件数の集計キー（画面の表示名と対応）
STAT_KEYS = {PHASES_TABLE: 'phases', STAGES_TABLE: 'stages', STEPS_TABLE: 'steps'}

# format_version=1（2026-10-09 の改名前）の表名 → 現在の表名
LEGACY_TABLE_NAMES = {
    'courses': COURSES_TABLE,
    'course_phase_contents': PHASES_TABLE,
    'course_stage_contents': STAGES_TABLE,
    'course_step_contents': STEPS_TABLE,
    'course_enrollments': ENROLLMENTS_TABLE,
    'course_progress': PROGRESS_TABLE,
    'migration_assistant_mentors': MENTORS_TABLE,
}

EMAIL_CANDIDATES = ['email', 'mail', 'email_address', 'mail_address', 'e_mail', 'user_email']
IMG_RE = re.compile(r'/static/mdimgs/([^\s)"\'<>?#]+)')


# =============================================================================
# 共通
# =============================================================================

def _require_admin():
    """管理者でなければ (レスポンス, ステータス) を返す"""
    user_id = session.get('user_id')
    if not user_id:
        return None, (jsonify({'success': False, 'error': 'Unauthorized'}), 401)
    if not check_is_admin(user_id):
        return None, (jsonify({'success': False, 'error': '管理者権限が必要です'}), 403)
    return user_id, None


def _norm_email(e):
    return (e or '').strip().lower()


def _to_json_value(v):
    if isinstance(v, datetime):
        return v.strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode('utf-8', errors='replace')
    return v


def _fetch(cursor, sql, params=()):
    cursor.execute(sql, params)
    return [{k: _to_json_value(v) for k, v in r.items()} for r in cursor.fetchall()]


def _table_columns(cursor, table):
    cursor.execute(f"SHOW COLUMNS FROM `{table}`")
    return [r['Field'] for r in cursor.fetchall()]


def _detect_email_column(cursor):
    cols = _table_columns(cursor, Tables.USERS)
    for c in EMAIL_CANDIDATES:
        if c in cols:
            return c
    raise RuntimeError(f"{Tables.USERS} にメールアドレスの列が見つかりません（列: {', '.join(cols)}）")


def _upload_folder():
    return getattr(Config, 'UPLOAD_FOLDER', None)


# =============================================================================
# エクスポート
# =============================================================================

def build_transfer_package(course_ids=None, with_images=True):
    """書き出し用の辞書を組み立てる"""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        email_col = _detect_email_column(cursor)

        # ---- 教材と配下のデータ ----
        data = {}
        if course_ids:
            ph = ','.join(['%s'] * len(course_ids))
            ids = tuple(course_ids)
            data[COURSES_TABLE] = _fetch(
                cursor, f"SELECT * FROM `{COURSES_TABLE}` WHERE id IN ({ph}) ORDER BY id", ids)
            for t in CHILD_TABLES:
                data[t] = _fetch(cursor, f"SELECT * FROM `{t}` WHERE course_id IN ({ph}) ORDER BY id", ids)
            creators = sorted({c['creator_user_id'] for c in data[COURSES_TABLE]})
            if creators:
                ph2 = ','.join(['%s'] * len(creators))
                data[MENTORS_TABLE] = _fetch(
                    cursor,
                    f"SELECT * FROM `{MENTORS_TABLE}` WHERE user_id IN ({ph2}) ORDER BY id",
                    tuple(creators))
            else:
                data[MENTORS_TABLE] = []
        else:
            for t in TRANSFER_TABLES:
                data[t] = _fetch(cursor, f"SELECT * FROM `{t}` ORDER BY id")

        # ---- 関係するユーザ ----
        user_ids = set()
        for t, cols in TRANSFER_USER_COLUMNS.items():
            for r in data.get(t, []):
                for c in cols:
                    if r.get(c) is not None:
                        user_ids.add(r[c])
        users = []
        if user_ids:
            ph = ','.join(['%s'] * len(user_ids))
            users = _fetch(cursor,
                           f"SELECT id, full_name, `{email_col}` AS email FROM `{Tables.USERS}` "
                           f"WHERE id IN ({ph}) ORDER BY id", tuple(sorted(user_ids)))
        found = {u['id'] for u in users}
        for uid in sorted(user_ids - found):
            users.append({'id': uid, 'full_name': None, 'email': None,
                          'note': 'users テーブルに存在しない'})
        cursor.close()
    finally:
        if conn and conn.is_connected():
            conn.close()

    # ---- 本文が参照する画像 ----
    names = set()
    for r in data[COURSES_TABLE]:
        names.update(IMG_RE.findall(r.get('course_description') or ''))
    for r in data[PHASES_TABLE]:
        names.update(IMG_RE.findall(r.get('phase_description') or ''))
    for r in data[STEPS_TABLE]:
        names.update(IMG_RE.findall(r.get('step_detail') or ''))
    images = []
    folder = _upload_folder()
    for name in sorted(names):
        item = {'filename': name, 'url': f'/static/mdimgs/{name}'}
        if with_images:
            path = os.path.join(folder or '', os.path.basename(name))
            if folder and os.path.isfile(path):
                with open(path, 'rb') as f:
                    item['content_base64'] = base64.b64encode(f.read()).decode('ascii')
                item['size'] = os.path.getsize(path)
            else:
                item['note'] = 'ファイルが見つからない'
        images.append(item)

    now = datetime.now(JST)
    return {
        'export_type': 'maiashi_content',
        'format_version': 2,
        'app_name': 'migration_assistant',
        'source_site': request.host.split('.')[0],
        'source_url': request.host_url,
        'generated_at': now.strftime('%Y-%m-%d %H:%M:%S'),
        'timezone_note': '日時はすべてDBに格納されているJSTのnaive値をそのまま文字列化したもの',
        'users_table': Tables.USERS,
        'email_column': email_col,
        'note': ('users はこのファイル内で参照されるユーザ全員．受け入れ側では email を手掛かりに '
                 'user_columns の列を新しいユーザIDに付け替え，course_columns の列を新しい教材IDに付け替える．'
                 'phase_id / stage_id / step_id は教材内で一意な文字列なのでそのまま使える．'),
        'user_columns': TRANSFER_USER_COLUMNS,
        'course_columns': TRANSFER_COURSE_COLUMNS,
        'users': users,
        'tables': data,
        'images': images,
        'counts': {t: len(data[t]) for t in TRANSFER_TABLES},
        'users_without_email': [u['id'] for u in users if not (u.get('email') or '').strip()],
    }


@migration_assistant.route('/admin/export_content', methods=['GET'])
def export_content():
    """教材と履修記録エクスポート（管理者のみ）．JSONファイルとしてダウンロードさせる"""
    user_id, err = _require_admin()
    if err:
        return err

    course_ids = None
    raw = (request.args.get('course') or '').strip()
    if raw:
        try:
            course_ids = sorted({int(x) for x in raw.split(',') if x.strip()})
        except ValueError:
            return jsonify({'success': False, 'error': 'course は教材IDのカンマ区切りで指定してください'}), 400
    with_images = request.args.get('images', '1') != '0'

    try:
        pkg = build_transfer_package(course_ids, with_images)
    except Exception as e:
        logging.error(f"export_content error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

    logging.info(f"[教材エクスポート] user_id={user_id} courses={course_ids or 'all'} "
                 f"counts={pkg['counts']} images={len(pkg['images'])}")

    fname = (f"maiashi_content_{pkg['source_site']}_"
             f"{datetime.now(JST).strftime('%Y%m%d_%H%M%S')}.json")
    body = json.dumps(pkg, ensure_ascii=False, indent=2)
    return Response(body, mimetype='application/json; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="{fname}"'})


# =============================================================================
# インポート
# =============================================================================

class _Inserter:
    """受け入れ側に存在する列だけを使って INSERT する（id は採番に任せる）"""

    def __init__(self, cursor):
        self.cursor = cursor
        self.cols = {}

    def insert(self, table, row, verb='INSERT', tail=''):
        if table not in self.cols:
            self.cols[table] = set(_table_columns(self.cursor, table))
        keys = [k for k in row if k != 'id' and k in self.cols[table]]
        sql = (f"{verb} INTO `{table}` ({', '.join('`' + k + '`' for k in keys)}) "
               f"VALUES ({', '.join(['%s'] * len(keys))}) {tail}")
        self.cursor.execute(sql, tuple(row[k] for k in keys))
        return self.cursor.lastrowid, self.cursor.rowcount


def _parse_email_map(text):
    remap = {}
    for line in (text or '').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            raise ValueError(f'読み替えの形式が不正です（旧アドレス=新アドレス）: {line}')
        a, b = line.split('=', 1)
        remap[_norm_email(a)] = _norm_email(b)
    return remap


def import_transfer_package(pkg, commit=False, force=False, remap=None):
    """JSON（辞書）を取り込む．commit=False なら最後にロールバックする"""
    if pkg.get('export_type') != 'maiashi_content':
        raise ValueError('まいあしの教材エクスポート（maiashi_content）ではありません')
    version = pkg.get('format_version')
    if version not in (1, 2):
        raise ValueError(f"対応していない形式の版です: {version}")
    T = dict(pkg.get('tables') or {})
    if version == 1:
        # 改名前の表名で書き出されたもの．キーを現在の表名に読み替える
        T = {LEGACY_TABLE_NAMES.get(k, k): v for k, v in T.items()}
    for t in TRANSFER_TABLES:
        T.setdefault(t, [])
    remap = remap or {}

    stats = defaultdict(int)
    warnings = []
    user_report = []
    course_report = []

    conn = get_db_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        email_col = _detect_email_column(cursor)

        # ---- ユーザの対応付け ----
        src_email = {}
        for u in pkg.get('users', []):
            e = _norm_email(u.get('email'))
            src_email[u['id']] = remap.get(e, e)
        wanted = sorted({e for e in src_email.values() if e})
        by_email = defaultdict(list)
        if wanted:
            ph = ','.join(['%s'] * len(wanted))
            cursor.execute(f"SELECT id, full_name, `{email_col}` AS email FROM `{Tables.USERS}` "
                           f"WHERE LOWER(TRIM(`{email_col}`)) IN ({ph})", tuple(wanted))
            for r in cursor.fetchall():
                by_email[_norm_email(r['email'])].append(r)

        uid_map = {}
        for u in pkg.get('users', []):
            e = src_email[u['id']]
            hits = by_email.get(e, [])
            rep = {'old_id': u['id'], 'old_name': u.get('full_name'), 'email': e or None,
                   'new_id': None, 'new_name': None, 'reason': None}
            if len(hits) == 1:
                uid_map[u['id']] = hits[0]['id']
                rep['new_id'] = hits[0]['id']
                rep['new_name'] = hits[0]['full_name']
            elif not e:
                rep['reason'] = 'メールアドレスなし'
            elif not hits:
                rep['reason'] = '受け入れ側に該当なし'
            else:
                rep['reason'] = f'受け入れ側に {len(hits)} 人該当（曖昧）'
            user_report.append(rep)

        ins = _Inserter(cursor)
        course_map = {}       # 旧教材ID → 新教材ID
        course_creator = {}   # 新教材ID → 新作成者ID

        # ---- 教材 ----
        for c in T[COURSES_TABLE]:
            rep = {'old_id': c['id'], 'title': c.get('course_title'), 'new_id': None, 'reason': None}
            new_creator = uid_map.get(c['creator_user_id'])
            if new_creator is None:
                rep['reason'] = '作成者を対応付けられない'
                stats['courses_skipped'] += 1
                course_report.append(rep)
                continue
            cursor.execute(f"SELECT id FROM `{COURSES_TABLE}` WHERE creator_user_id = %s AND course_title = %s",
                           (new_creator, c['course_title']))
            exist = cursor.fetchone()
            cursor.fetchall()  # 未読の行を捨てる（同名が複数ある場合）
            if exist and not force:
                rep['reason'] = f"同じ作成者・同じタイトルの教材（id={exist['id']}）が既にある"
                stats['courses_skipped'] += 1
                course_report.append(rep)
                continue
            new_id, _ = ins.insert(COURSES_TABLE, dict(c, creator_user_id=new_creator))
            course_map[c['id']] = new_id
            course_creator[new_id] = new_creator
            rep['new_id'] = new_id
            stats['courses'] += 1
            course_report.append(rep)

        # ---- Phase / Stage / Step ----
        for t in (PHASES_TABLE, STAGES_TABLE, STEPS_TABLE):
            for r in T[t]:
                if r['course_id'] not in course_map:
                    continue
                ins.insert(t, dict(r, course_id=course_map[r['course_id']]))
                stats[STAT_KEYS[t]] += 1

        # ---- 受講登録 ----
        for r in T[ENROLLMENTS_TABLE]:
            if r['course_id'] not in course_map:
                continue
            new_course = course_map[r['course_id']]
            student = uid_map.get(r['student_user_id'])
            if student is None:
                warnings.append(f"受講 {r['id']}（弟子 ID {r['student_user_id']}）: "
                                f"弟子を対応付けられないため取り込みません")
                stats['enrollments_skipped'] += 1
                continue
            mentor = uid_map.get(r['mentor_user_id'], course_creator[new_course])
            _, n = ins.insert(ENROLLMENTS_TABLE,
                              dict(r, course_id=new_course, student_user_id=student, mentor_user_id=mentor),
                              verb='INSERT IGNORE')
            stats['enrollments' if n == 1 else 'enrollments_existing'] += 1

        # ---- 進捗 ----
        for r in T[PROGRESS_TABLE]:
            if r['course_id'] not in course_map:
                continue
            student = uid_map.get(r['student_user_id'])
            if student is None:
                stats['progress_skipped'] += 1
                continue
            ins.insert(PROGRESS_TABLE,
                       dict(r, course_id=course_map[r['course_id']], student_user_id=student),
                       tail='ON DUPLICATE KEY UPDATE status = VALUES(status), updated_at = VALUES(updated_at)')
            stats['progress'] += 1

        # ---- 師匠候補の記録 ----
        for r in T[MENTORS_TABLE]:
            user = uid_map.get(r['user_id'])
            if user is None:
                stats['mentors_skipped'] += 1
                continue
            approver = uid_map.get(r['approved_by'], user)
            _, n = ins.insert(MENTORS_TABLE,
                              dict(r, user_id=user, approved_by=approver), verb='INSERT IGNORE')
            stats['mentors' if n == 1 else 'mentors_existing'] += 1

        if commit:
            conn.commit()
        else:
            conn.rollback()
        cursor.close()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        if conn.is_connected():
            conn.close()

    # ---- 画像（同梱分のうち，受け入れ側に無いものだけ）----
    img = {'written': 0, 'existing': 0, 'not_included': 0}
    folder = _upload_folder()
    for im in pkg.get('images', []):
        name = os.path.basename(im.get('filename') or '')
        if not name or 'content_base64' not in im or not folder:
            img['not_included'] += 1
            continue
        path = os.path.join(folder, name)
        if os.path.exists(path):
            img['existing'] += 1
            continue
        if commit:
            os.makedirs(folder, exist_ok=True)
            with open(path, 'wb') as f:
                f.write(base64.b64decode(im['content_base64']))
        img['written'] += 1

    return {
        'committed': commit,
        'source_site': pkg.get('source_site'),
        'generated_at': pkg.get('generated_at'),
        'users': user_report,
        'courses': course_report,
        'stats': dict(stats),
        'images': img,
        'warnings': warnings,
    }


@migration_assistant.route('/admin/import_content', methods=['POST'])
def import_content():
    """教材と履修記録インポート（管理者のみ）．commit=1 以外は試行してロールバック"""
    user_id, err = _require_admin()
    if err:
        return err

    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'success': False, 'error': 'JSONファイルを選んでください'}), 400
    try:
        pkg = json.loads(f.read().decode('utf-8-sig'))
    except Exception as e:
        return jsonify({'success': False, 'error': f'JSONとして読めません: {e}'}), 400

    commit = request.form.get('commit') == '1'
    force = request.form.get('force') == '1'
    try:
        remap = _parse_email_map(request.form.get('map'))
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400

    try:
        result = import_transfer_package(pkg, commit=commit, force=force, remap=remap)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logging.error(f"import_content error: {e}")
        return jsonify({'success': False, 'error': f'取り込みを中止しロールバックしました: {e}'}), 500

    logging.info(f"[教材インポート] user_id={user_id} commit={commit} force={force} "
                 f"source={result['source_site']} stats={result['stats']}")
    return jsonify(dict(result, success=True))
