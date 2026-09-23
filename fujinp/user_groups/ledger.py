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
user_groups/ledger.py — ユーザとグループ（発令台帳）

「単位 × 役割 × 人 × 期間」の台帳と，その大表ビュー，Excel 入出力を提供する．
既存のグループ機能（routes.py / utils.py）には触れない．

テーブル（すべて default DB）
  ug_units         単位（組織・会議体・分担事務の木）
  ug_roles         役割の語彙（表の列）
  ug_appointments  発令（表のセル1個＝1行）
  ug_person_aliases 名寄せの対象外リスト（user_id NULL の行＝この氏名は誰にも紐づけない）
                    ※ user_id ありの行（表記の別名）は 2026-09-08 に廃止．残っていれば読むだけ
  ug_group_rules   グループの導出ルール（単位×役割 → user_groups の構成員）
  ug_group_snapshot ルール導入前の構成員集合（回帰の照合用）
  ug_persons       アカウント名簿（氏名・メール・区分・所属・期間）．台帳が正で，users に直ちに反映する
  （カーネル表 users / approved_users / registration_requests を読み書きする：名簿の発行・申請の承認・ブラックリスト）

単位の種別 kind は構造の骨格としてコードに持つ（org / body / duty / group）．
group は管理者が手で作るグループ（まいぐるの user_groups に対応．役割は 所有者／メンバー）．
単位名・役割名・人・期間はすべてデータ側．
"""
import io
import re
import logging
from functools import wraps
from datetime import datetime, date, timedelta, timezone

from flask import render_template, jsonify, request, session, send_file

from decorators import login_required
from . import user_groups_bp
from .routes import get_db, check_is_total_admin, parse_input

JST = timezone(timedelta(hours=9), 'JST')

# 単位の種別（骨格）．表示名は kind ラベル．
KINDS = [('org', '組織'), ('body', '会議体'), ('duty', '分担事務'), ('group', 'グループ')]
KIND_LABEL = dict(KINDS)
LABEL_KIND = {v: k for k, v in KINDS}

MAX_DEPTH = 5           # Excel の階層列の本数
DEFAULT_RANK = 99       # 未登録の役割を自動作成するときの序列

SHEET_UNITS = '単位'
SHEET_ROLES = '役割'
SHEET_APPT = '発令'
SHEET_PERSONS = '人'

UNIT_HEADERS = ['種別'] + [f'階層{i}' for i in range(1, MAX_DEPTH + 1)] + ['序列', '管理グループ', '開始', '終了', '備考']
ROLE_HEADERS = ['種別', '役割名', '序列', '備考']
APPT_HEADERS = ['種別'] + [f'階層{i}' for i in range(1, MAX_DEPTH + 1)] + ['役割', '氏名', '開始', '終了', '備考', '出典']
PERSON_HEADERS = ['氏名', 'メール', '区分', '所属', '開始', '終了', '備考']
PERSON_CATEGORIES = ('admin', 'regular', 'guest')

# 氏名欄の区切り（複数人を1セルに書いたとき）
_NAME_SPLIT_RE = re.compile(r'[、，,／/・;；]\s*|\s{2,}')


# ────────────────────────────────────────────
# 小道具
# ────────────────────────────────────────────

def _now():
    return datetime.now(JST).replace(tzinfo=None)


def _s(v):
    """セル値を空白除去した文字列に（None は ''）"""
    if v is None:
        return ''
    return str(v).strip()


def _d(v):
    """セル値を datetime に（空なら None）．Excel の日付セルと文字列の両方を受ける"""
    if v is None or v == '':
        return None
    if isinstance(v, datetime):
        return v.replace(tzinfo=None)
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    s = str(v).strip().replace('/', '-')
    return parse_input(s)


def _dstr(dt):
    if not dt:
        return ''
    if isinstance(dt, datetime):
        return dt.strftime('%Y-%m-%d') if (dt.hour, dt.minute) == (0, 0) else dt.strftime('%Y-%m-%d %H:%M')
    return str(dt)


def _as_of_from_request():
    """?as_of=YYYY-MM-DD．空なら None（全期間）．'today' は今日"""
    v = _s(request.args.get('as_of'))
    if v == '' or v.lower() == 'all':
        return None
    if v.lower() == 'today':
        return _now().replace(hour=0, minute=0, second=0, microsecond=0)
    dt = parse_input(v)
    return dt


def _fiscal_year_bounds(at):
    """at を含む年度（4月1日始まり）の始点・終点"""
    y = at.year if at.month >= 4 else at.year - 1
    return date(y, 4, 1), date(y + 1, 3, 31)


def _partial(valid_from, valid_until, at):
    """時点の年度の途中で始まる／終わる発令か（年度いっぱいのものは False）"""
    if at is None:
        return bool(valid_from or valid_until)
    fs, fe = _fiscal_year_bounds(at)
    return bool((valid_from and valid_from.date() > fs) or (valid_until and valid_until.date() < fe))


def _valid_at(valid_from, valid_until, at):
    if at is None:
        return True
    if valid_from and valid_from > at:
        return False
    if valid_until and valid_until < at:
        return False
    return True


def _norm_name(s):
    """氏名照合用の正規化（空白・全角空白・括弧を除去．「佐藤（充）」→「佐藤充」）"""
    return re.sub(r'[\s\u3000（）()]+', '', s or '')


def _is_admin():
    return check_is_total_admin(session.get('user_id'))


def _deny():
    return jsonify({'success': False, 'error': '権限がありません（台帳の操作は総管理者のみ）'}), 403


def admin_only(view):
    """総管理者以外を弾く．@user_groups_bp.route と @login_required の下に置く"""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not _is_admin():
            return _deny()
        return view(*args, **kwargs)
    return wrapped


DENY_PAGE = (
    '<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    '<title>ユーザとグループ</title></head>'
    '<body style="font-family:-apple-system,\'Hiragino Kaku Gothic ProN\',sans-serif;'
    'margin:40px;color:#222">'
    '<h2 style="color:#54367C">ユーザとグループ</h2>'
    '<p>この画面は総管理者だけが使えます．</p>'
    '<p><a href="{back}">FUJINダッシュボードに戻る</a></p>'
    '</body></html>'
)


# ────────────────────────────────────────────
# 単位の木
# ────────────────────────────────────────────

def _load_units(cursor, kind):
    cursor.execute("""
        SELECT id, kind, name, parent_id, owner_group_id, sort_order,
               valid_from, valid_until, note
        FROM ug_units WHERE kind = %s
        ORDER BY sort_order, id
    """, (kind,))
    return cursor.fetchall()


def _tree_order(units):
    """親→子の順に並べ，depth と path を付けて返す"""
    by_parent = {}
    for u in units:
        by_parent.setdefault(u['parent_id'], []).append(u)
    out = []

    def walk(pid, depth, path):
        for u in by_parent.get(pid, []):
            u = dict(u)
            u['depth'] = depth
            u['path'] = path + [u['name']]
            out.append(u)
            walk(u['id'], depth + 1, u['path'])

    walk(None, 0, [])
    # 親が消えて宙に浮いた単位も末尾に出す
    seen = {u['id'] for u in out}
    for u in units:
        if u['id'] not in seen:
            u = dict(u)
            u['depth'] = 0
            u['path'] = ['?', u['name']]
            out.append(u)
    return out


def _find_unit_by_path(cursor, kind, path):
    """階層パス（名前のリスト）で単位を探す．なければ None"""
    pid = None
    unit = None
    for name in path:
        if pid is None:
            cursor.execute("SELECT id FROM ug_units WHERE kind=%s AND parent_id IS NULL AND name=%s",
                           (kind, name))
        else:
            cursor.execute("SELECT id FROM ug_units WHERE kind=%s AND parent_id=%s AND name=%s",
                           (kind, pid, name))
        row = cursor.fetchone()
        if not row:
            return None
        pid = row['id']
        unit = row
    return unit['id'] if unit else None


def _ensure_unit_path(cursor, kind, path, created):
    """階層パスの単位を（なければ作りながら）辿り，末端の id を返す"""
    pid = None
    for depth, name in enumerate(path):
        if pid is None:
            cursor.execute("SELECT id FROM ug_units WHERE kind=%s AND parent_id IS NULL AND name=%s",
                           (kind, name))
        else:
            cursor.execute("SELECT id FROM ug_units WHERE kind=%s AND parent_id=%s AND name=%s",
                           (kind, pid, name))
        row = cursor.fetchone()
        if row:
            pid = row['id']
        else:
            cursor.execute("SELECT COALESCE(MAX(sort_order),0)+10 AS so FROM ug_units WHERE kind=%s AND parent_id <=> %s",
                           (kind, pid))
            so = cursor.fetchone()['so']
            cursor.execute("INSERT INTO ug_units (kind, name, parent_id, sort_order) VALUES (%s,%s,%s,%s)",
                           (kind, name, pid, so))
            pid = cursor.lastrowid
            created.append('／'.join(path[:depth + 1]))
    return pid


# ────────────────────────────────────────────
# 画面
# ────────────────────────────────────────────

@user_groups_bp.route('/ledger')
@login_required
def ledger_page():
    if not _is_admin():
        from flask import url_for
        return DENY_PAGE.format(back=url_for('user_groups.return_to_fujin')), 403
    return render_template('ledger.html',
                           kinds=KINDS,
                           is_admin=_is_admin(),
                           today=_now().strftime('%Y-%m-%d'))


# ────────────────────────────────────────────
# 大表（グリッド）
# ────────────────────────────────────────────

@user_groups_bp.route('/api/ledger/grid')
@login_required
@admin_only
def ledger_grid():
    kind = request.args.get('kind', 'org')
    if kind not in KIND_LABEL:
        return jsonify({'success': False, 'error': 'kind が不正です'}), 400
    at = _as_of_from_request()

    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id, name, `rank`, sort_order, note FROM ug_roles WHERE kind=%s ORDER BY `rank`, sort_order, id",
                       (kind,))
        roles = cursor.fetchall()

        units = _tree_order(_load_units(cursor, kind))
        unit_ids = [u['id'] for u in units]
        appts = []
        if unit_ids:
            fmt = ','.join(['%s'] * len(unit_ids))
            cursor.execute(f"""
                SELECT a.id, a.unit_id, a.role_id, a.user_id, a.person_name,
                       a.valid_from, a.valid_until, a.note, a.source,
                       u.full_name, u.email
                FROM ug_appointments a
                LEFT JOIN users u ON u.id = a.user_id
                WHERE a.unit_id IN ({fmt})
                ORDER BY a.valid_from, a.id
            """, tuple(unit_ids))
            appts = cursor.fetchall()

        cell = {}
        for a in appts:
            if not _valid_at(a['valid_from'], a['valid_until'], at):
                continue
            cell.setdefault(a['unit_id'], {}).setdefault(a['role_id'], []).append({
                'id': a['id'],
                'name': a['person_name'] or a['full_name'] or '',
                'user_name': a['full_name'] or '',
                'user_email': a['email'] or '',
                'user_id': a['user_id'],
                'resolved': a['user_id'] is not None,
                'vacant': not (a['person_name'] or a['full_name']),
                'from': _dstr(a['valid_from']),
                'until': _dstr(a['valid_until']),
                'partial': _partial(a['valid_from'], a['valid_until'], at),
                'note': a['note'] or '',
                'source': a['source'] or '',
            })

        rows = []
        for u in units:
            if not _valid_at(u['valid_from'], u['valid_until'], at):
                continue
            rows.append({
                'id': u['id'],
                'name': u['name'],
                'depth': u['depth'],
                'path': u['path'],
                'parent_id': u['parent_id'],
                'sort_order': u['sort_order'],
                'owner_group_id': u['owner_group_id'],
                'from': _dstr(u['valid_from']),
                'until': _dstr(u['valid_until']),
                'note': u['note'] or '',
                'cells': {str(rid): items for rid, items in cell.get(u['id'], {}).items()},
            })

        return jsonify({'success': True, 'kind': kind, 'kind_label': KIND_LABEL[kind],
                        'as_of': _dstr(at) if at else '',
                        'roles': [{'id': r['id'], 'name': r['name'], 'rank': r['rank'], 'note': r['note'] or ''} for r in roles],
                        'rows': rows,
                        'appointment_count': sum(len(v) for c in cell.values() for v in c.values())})
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/api/ledger/summary')
@login_required
@admin_only
def ledger_summary():
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        out = {}
        for kind, label in KINDS:
            cursor.execute("SELECT COUNT(*) AS n FROM ug_units WHERE kind=%s", (kind,))
            nu = cursor.fetchone()['n']
            cursor.execute("SELECT COUNT(*) AS n FROM ug_roles WHERE kind=%s", (kind,))
            nr = cursor.fetchone()['n']
            cursor.execute("SELECT COUNT(*) AS n FROM ug_appointments a JOIN ug_units u ON u.id=a.unit_id WHERE u.kind=%s", (kind,))
            na = cursor.fetchone()['n']
            out[kind] = {'label': label, 'units': nu, 'roles': nr, 'appointments': na}
        cursor.execute("SELECT source, COUNT(*) AS n, MIN(valid_from) AS f, MAX(valid_until) AS t FROM ug_appointments GROUP BY source ORDER BY source")
        sources = [{'source': r['source'] or '', 'n': r['n'], 'from': _dstr(r['f']), 'until': _dstr(r['t'])} for r in cursor.fetchall()]
        cursor.execute("SELECT COUNT(*) AS n FROM ug_appointments WHERE user_id IS NULL AND COALESCE(person_name,'')<>''")
        unresolved = cursor.fetchone()['n']
        cursor.execute("SELECT COUNT(*) AS n FROM ug_appointments WHERE user_id IS NULL AND COALESCE(person_name,'')=''")
        vacant = cursor.fetchone()['n']
        return jsonify({'success': True, 'kinds': out, 'sources': sources,
                        'unresolved': unresolved, 'vacant': vacant})
    finally:
        cursor.close()
        conn.close()


# ────────────────────────────────────────────
# 単位・役割・発令の CRUD（総管理者）
# ────────────────────────────────────────────

@user_groups_bp.route('/api/ledger/units', methods=['POST'])
@login_required
def ledger_unit_create():
    if not _is_admin():
        return _deny()
    data = request.get_json(silent=True) or {}
    kind = data.get('kind')
    name = _s(data.get('name'))
    if kind not in KIND_LABEL or not name:
        return jsonify({'success': False, 'error': '種別と名称は必須です'}), 400
    parent_id = data.get('parent_id') or None
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT COALESCE(MAX(sort_order),0)+10 AS so FROM ug_units WHERE kind=%s AND parent_id <=> %s",
                       (kind, parent_id))
        so = data.get('sort_order') or cursor.fetchone()['so']
        cursor.execute("""INSERT INTO ug_units (kind, name, parent_id, owner_group_id, sort_order, valid_from, valid_until, note)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                       (kind, name, parent_id, data.get('owner_group_id') or None, so,
                        parse_input(_s(data.get('valid_from'))), parse_input(_s(data.get('valid_until'))),
                        _s(data.get('note')) or None))
        conn.commit()
        return jsonify({'success': True, 'id': cursor.lastrowid})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/api/ledger/units/<int:unit_id>', methods=['PUT'])
@login_required
def ledger_unit_update(unit_id):
    if not _is_admin():
        return _deny()
    data = request.get_json(silent=True) or {}
    fields, vals = [], []
    for col in ('name', 'note'):
        if col in data:
            fields.append(f"{col}=%s")
            vals.append(_s(data[col]) or None)
    for col in ('parent_id', 'owner_group_id', 'sort_order'):
        if col in data:
            fields.append(f"{col}=%s")
            vals.append(data[col] if data[col] not in ('', None) else None)
    for col in ('valid_from', 'valid_until'):
        if col in data:
            fields.append(f"{col}=%s")
            vals.append(parse_input(_s(data[col])))
    if not fields:
        return jsonify({'success': False, 'error': '変更がありません'}), 400
    if 'parent_id' in data and data['parent_id'] == unit_id:
        return jsonify({'success': False, 'error': '自分自身を親にはできません'}), 400
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(f"UPDATE ug_units SET {', '.join(fields)} WHERE id=%s", tuple(vals) + (unit_id,))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/api/ledger/units/<int:unit_id>', methods=['DELETE'])
@login_required
def ledger_unit_delete(unit_id):
    """単位を削除（配下の単位と発令も一緒に消す）"""
    if not _is_admin():
        return _deny()
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        ids = [unit_id]
        queue = [unit_id]
        while queue:
            pid = queue.pop()
            cursor.execute("SELECT id FROM ug_units WHERE parent_id=%s", (pid,))
            for r in cursor.fetchall():
                ids.append(r['id'])
                queue.append(r['id'])
        fmt = ','.join(['%s'] * len(ids))
        cursor.execute(f"DELETE FROM ug_appointments WHERE unit_id IN ({fmt})", tuple(ids))
        na = cursor.rowcount
        cursor.execute(f"DELETE FROM ug_units WHERE id IN ({fmt})", tuple(ids))
        conn.commit()
        return jsonify({'success': True, 'deleted_units': len(ids), 'deleted_appointments': na})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/api/ledger/roles', methods=['POST'])
@login_required
def ledger_role_create():
    if not _is_admin():
        return _deny()
    data = request.get_json(silent=True) or {}
    kind = data.get('kind')
    name = _s(data.get('name'))
    if kind not in KIND_LABEL or not name:
        return jsonify({'success': False, 'error': '種別と役割名は必須です'}), 400
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO ug_roles (kind, name, `rank`, sort_order, note) VALUES (%s,%s,%s,%s,%s)",
                       (kind, name, data.get('rank') or DEFAULT_RANK, data.get('sort_order') or 0, _s(data.get('note')) or None))
        conn.commit()
        return jsonify({'success': True, 'id': cursor.lastrowid})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/api/ledger/roles/<int:role_id>', methods=['PUT'])
@login_required
def ledger_role_update(role_id):
    if not _is_admin():
        return _deny()
    data = request.get_json(silent=True) or {}
    fields, vals = [], []
    if 'name' in data:
        fields.append("name=%s"); vals.append(_s(data['name']))
    if 'rank' in data:
        fields.append("`rank`=%s"); vals.append(data['rank'] or DEFAULT_RANK)
    if 'sort_order' in data:
        fields.append("sort_order=%s"); vals.append(data['sort_order'] or 0)
    if 'note' in data:
        fields.append("note=%s"); vals.append(_s(data['note']) or None)
    if not fields:
        return jsonify({'success': False, 'error': '変更がありません'}), 400
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(f"UPDATE ug_roles SET {', '.join(fields)} WHERE id=%s", tuple(vals) + (role_id,))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/api/ledger/roles/<int:role_id>', methods=['DELETE'])
@login_required
def ledger_role_delete(role_id):
    if not _is_admin():
        return _deny()
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT COUNT(*) AS n FROM ug_appointments WHERE role_id=%s", (role_id,))
        if cursor.fetchone()['n']:
            return jsonify({'success': False, 'error': 'この役割を使う発令が残っています．先に発令を消してください'}), 400
        cursor.execute("DELETE FROM ug_roles WHERE id=%s", (role_id,))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


class NameResolver:
    """
    氏名 → users.id の名寄せ．順に試す（2026-09-08：姓だけの先頭一致は廃止）：
      exact  … users.full_name と完全一致（空白・括弧は無視）
      ignore … 対象外リストにある氏名（紐づけない）
      post   … 役職名そのもの（充て職）
      person … 名簿（氏名→メール→users）で一意に決まる
    決まらないものは未解決とし，名寄せ画面で人を選ぶ
    """
    def __init__(self, cursor):
        cursor.execute("SELECT id, full_name, category FROM users WHERE deleted_at IS NULL")
        rows = cursor.fetchall()
        self.users = [(r['id'], r['full_name'] or '', _norm_name(r['full_name'])) for r in rows]
        self.guest = {r['id'] for r in rows if (r['category'] or '') == 'guest'}
        self.exact = {}
        for uid, _, key in self.users:
            self.exact.setdefault(key, []).append(uid)
        # 役職名そのもの（学部長・事務局長・委員長…）は充て職として「紐づけ不要」扱い
        self.posts = set()
        try:
            cursor.execute("SELECT name FROM ug_roles")
            self.posts = {_norm_name(r['name']) for r in cursor.fetchall()}
        except Exception as e:
            logging.warning("ug_roles unavailable: %s", e)
        self.alias = {}
        try:
            cursor.execute("SELECT alias_name, user_id FROM ug_person_aliases")
            self.alias = {_norm_name(r['alias_name']): r['user_id'] for r in cursor.fetchall()}
        except Exception as e:      # 別名表が未作成でも名寄せ以外は動かす
            logging.warning("ug_person_aliases unavailable: %s", e)
        # 名簿（氏名→メール）．メールが users にあればその id へ
        self.by_email = {}
        cursor.execute("SELECT id, email FROM users WHERE deleted_at IS NULL")
        for r in cursor.fetchall():
            if r['email']:
                self.by_email[r['email'].strip().lower()] = r['id']
        self.persons = []           # [(norm_name, email)]
        try:
            cursor.execute("SELECT full_name, email FROM ug_persons WHERE email IS NOT NULL")
            self.add_persons([(r['full_name'], r['email']) for r in cursor.fetchall()])
        except Exception as e:
            logging.warning("ug_persons unavailable: %s", e)
        self.cache = {}

    def add_persons(self, pairs):
        """名簿の (氏名, メール) を追加（取込の検証で，シート上の名簿を先読みするため）"""
        for fn, em in pairs:
            k, e = _norm_name(fn), (em or '').strip().lower()
            if k and e and (k, e) not in self.persons:
                self.persons.append((k, e))
        self.cache = {}

    def _via_persons(self, key):
        """名簿で氏名→メール→users.id（完全一致で一意のときだけ）"""
        exact = {e for k, e in self.persons if k == key}
        if len(exact) == 1:
            return self.by_email.get(next(iter(exact)))
        return None

    def _pick(self, ids):
        """候補が複数のとき，guest でない人が1人だけならその人"""
        if len(ids) == 1:
            return ids[0]
        non_guest = [u for u in ids if u not in self.guest]
        return non_guest[0] if len(non_guest) == 1 else None

    def resolve(self, name):
        """(user_id or None, method)．method は exact/alias/ignore/person/post/None"""
        key = _norm_name(name)
        if not key:
            return None, None
        if key in self.cache:
            return self.cache[key]
        res = (None, None)
        if key in self.exact and self._pick(self.exact[key]):
            res = (self._pick(self.exact[key]), 'exact')
        elif key in self.alias:
            uid = self.alias[key]
            res = (uid, 'alias') if uid else (None, 'ignore')
        elif key in self.posts:
            res = (None, 'post')
        elif self._via_persons(key):
            res = (self._via_persons(key), 'person')
        self.cache[key] = res
        return res

    def candidates(self, name, limit=8):
        """名寄せ画面用の候補：先頭一致 → 部分一致 の順"""
        key = _norm_name(name)
        if not key:
            return []
        head = [(uid, fn) for uid, fn, k in self.users if k.startswith(key)]
        part = [(uid, fn) for uid, fn, k in self.users if key in k and not k.startswith(key)]
        # 括弧付き（佐藤（充））は括弧前の姓でも探す
        base = re.sub(r'[（(].*$', '', key)
        more = [(uid, fn) for uid, fn, k in self.users if base and base != key and k.startswith(base)]
        seen, out = set(), []
        for uid, fn in head + part + more:
            if uid not in seen:
                seen.add(uid)
                out.append({'id': uid, 'full_name': fn})
        return out[:limit]


def _resolve_user_id(cursor, name, cache):
    """後方互換の薄い包み（1件だけ引くとき用）"""
    return NameResolver(cursor).resolve(name)[0]


@user_groups_bp.route('/api/ledger/users')
@login_required
@admin_only
def ledger_users_search():
    """発令の編集で人を選ぶための検索：users ID・メール・氏名の一部（空白無視）"""
    q = _s(request.args.get('q'))
    if not q:
        return jsonify({'success': True, 'items': []})
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id, full_name, email, category, is_active FROM users WHERE deleted_at IS NULL")
        key = _norm_name(q).lower()
        items = []
        for r in cursor.fetchall():
            hit = (q.isdigit() and r['id'] == int(q)) or (key and key in _norm_name(r['full_name']).lower()) or (key and key in (r['email'] or '').lower())
            if hit:
                items.append({'id': r['id'], 'full_name': r['full_name'] or '', 'email': r['email'] or '',
                              'category': r['category'], 'inactive': not r['is_active']})
        items.sort(key=lambda x: (not (q.isdigit() and x['id'] == int(q)), x['inactive'], x['full_name']))
        return jsonify({'success': True, 'items': items[:12], 'more': max(0, len(items) - 12)})
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/api/ledger/appointments', methods=['POST'])
@login_required
def ledger_appt_create():
    if not _is_admin():
        return _deny()
    data = request.get_json(silent=True) or {}
    unit_id = data.get('unit_id')
    role_id = data.get('role_id')
    if not unit_id or not role_id:
        return jsonify({'success': False, 'error': '単位と役割は必須です'}), 400
    person = _s(data.get('person_name'))
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        user_id = data.get('user_id') if 'user_id' in data else _resolve_user_id(cursor, person, {})
        cursor.execute("""INSERT INTO ug_appointments
                          (unit_id, role_id, user_id, person_name, valid_from, valid_until, note, source, created_by)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                       (unit_id, role_id, user_id, person or None,
                        parse_input(_s(data.get('valid_from'))), parse_input(_s(data.get('valid_until'))),
                        _s(data.get('note')) or None, _s(data.get('source')) or None, session.get('user_id')))
        conn.commit()
        return jsonify({'success': True, 'id': cursor.lastrowid, 'user_id': user_id})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/api/ledger/appointments/<int:appt_id>', methods=['PUT'])
@login_required
def ledger_appt_update(appt_id):
    if not _is_admin():
        return _deny()
    data = request.get_json(silent=True) or {}
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        fields, vals = [], []
        if 'person_name' in data:
            person = _s(data['person_name'])
            fields.append("person_name=%s"); vals.append(person or None)
            uid = data.get('user_id') if 'user_id' in data else _resolve_user_id(cursor, person, {})
            fields.append("user_id=%s"); vals.append(uid or None)
        elif 'user_id' in data:
            fields.append("user_id=%s"); vals.append(data['user_id'] or None)
        for col in ('unit_id', 'role_id'):
            if col in data and data[col]:
                fields.append(f"{col}=%s"); vals.append(data[col])
        for col in ('valid_from', 'valid_until'):
            if col in data:
                fields.append(f"{col}=%s"); vals.append(parse_input(_s(data[col])))
        for col in ('note', 'source'):
            if col in data:
                fields.append(f"{col}=%s"); vals.append(_s(data[col]) or None)
        if not fields:
            return jsonify({'success': False, 'error': '変更がありません'}), 400
        cursor.execute(f"UPDATE ug_appointments SET {', '.join(fields)} WHERE id=%s", tuple(vals) + (appt_id,))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/api/ledger/appointments/<int:appt_id>', methods=['DELETE'])
@login_required
def ledger_appt_delete(appt_id):
    if not _is_admin():
        return _deny()
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM ug_appointments WHERE id=%s", (appt_id,))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/api/ledger/persons')
@login_required
@admin_only
def ledger_persons():
    """人から引く：氏名の部分一致で発令を横断検索（全期間）"""
    q = _s(request.args.get('q'))
    if not q:
        return jsonify({'success': True, 'items': []})
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT a.id, a.person_name, a.user_id, a.valid_from, a.valid_until, a.note,
                   u.kind, u.id AS unit_id, r.name AS role_name, us.full_name
            FROM ug_appointments a
            JOIN ug_units u ON u.id = a.unit_id
            JOIN ug_roles r ON r.id = a.role_id
            LEFT JOIN users us ON us.id = a.user_id
            WHERE a.person_name LIKE %s OR us.full_name LIKE %s
            ORDER BY u.kind, u.id, r.`rank`
        """, (f'%{q}%', f'%{q}%'))
        hits = cursor.fetchall()
        # 単位のパスを解決
        paths = {}
        for kind, _ in KINDS:
            for u in _tree_order(_load_units(cursor, kind)):
                paths[u['id']] = u['path']
        items = [{'id': h['id'], 'name': h['person_name'] or h['full_name'] or '',
                  'kind': h['kind'], 'kind_label': KIND_LABEL[h['kind']],
                  'path': paths.get(h['unit_id'], []), 'role': h['role_name'],
                  'from': _dstr(h['valid_from']), 'until': _dstr(h['valid_until']),
                  'note': h['note'] or ''} for h in hits]
        return jsonify({'success': True, 'items': items})
    finally:
        cursor.close()
        conn.close()


# ────────────────────────────────────────────
# グループの導出ルール
# ────────────────────────────────────────────

# 会議体で「委員の側」に当たる役割（残りは事務局側とみなす）
BODY_MEMBER_ROLES = ('理事長', '理事', '監事', '議長', '担当副学長', '委員長', '副委員長',
                     '室長', '副室長', '部局長', '副学部長等', '委員', '構成員')
BODY_STAFF_ROLES = ('事務職員',)
# 組織で「教員」に当たる役割
FACULTY_ROLES = ('学長', '副学長', '学部長', '副学部長', '基盤教育院長', '研究科長',
                 '教授', '准教授', '講師', '助教', '特命教授')   # 参考（いまは単位ごと全役割で足りる）


def _rules_of(cursor, group_id):
    cursor.execute("""
        SELECT r.id, r.unit_id, r.role_id, r.recurse, r.mode, r.valid_from, r.valid_until, r.note,
               u.kind, u.name AS unit_name, ro.name AS role_name
        FROM ug_group_rules r
        JOIN ug_units u ON u.id = r.unit_id
        LEFT JOIN ug_roles ro ON ro.id = r.role_id
        WHERE r.group_id = %s ORDER BY r.id
    """, (group_id,))
    return cursor.fetchall()


def _unit_path(cursor, unit_id):
    names, cur = [], unit_id
    for _ in range(MAX_DEPTH + 2):
        cursor.execute("SELECT name, parent_id FROM ug_units WHERE id=%s", (cur,))
        r = cursor.fetchone()
        if not r:
            break
        names.append(r['name'])
        if not r['parent_id']:
            break
        cur = r['parent_id']
    return list(reversed(names))


@user_groups_bp.route('/api/ledger/groups')
@login_required
@admin_only
def ledger_groups():
    """グループの一覧：直接メンバー・ルール・いまの構成員（台帳が導く分を含む）"""
    at = _as_of_from_request() or _now()
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        from .utils import _group_member_ids
        cursor.execute("SELECT id, name, description, manager_user_id FROM user_groups ORDER BY name")
        groups = cursor.fetchall()
        cursor.execute("SELECT id, full_name FROM users WHERE deleted_at IS NULL")
        uname = {r['id']: r['full_name'] or '' for r in cursor.fetchall()}
        items = []
        for g in groups:
            cursor.execute("""SELECT user_id, valid_from, valid_until FROM user_group_memberships WHERE group_id=%s""", (g['id'],))
            direct = {r['user_id'] for r in cursor.fetchall() if _valid_at(r['valid_from'], r['valid_until'], at)}
            rules = _rules_of(cursor, g['id'])
            members = _group_member_ids(cursor, g['id'], at)
            items.append({
                'id': g['id'], 'name': g['name'], 'description': g['description'] or '',
                'manager': uname.get(g['manager_user_id'], ''),
                'direct': sorted(direct), 'direct_names': [uname.get(u, f'#{u}') for u in sorted(direct)],
                'rules': [{'id': r['id'], 'unit_id': r['unit_id'], 'unit': '／'.join(_unit_path(cursor, r['unit_id'])),
                           'kind': KIND_LABEL.get(r['kind'], r['kind']), 'role': r['role_name'] or '（全役割）',
                           'recurse': bool(r['recurse']), 'mode': r['mode'], 'note': r['note'] or ''} for r in rules],
                'members': sorted(members), 'member_names': [uname.get(u, f'#{u}') for u in sorted(members)],
            })
        return jsonify({'success': True, 'items': items, 'as_of': _dstr(at)})
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/api/ledger/groups/rules', methods=['POST'])
@login_required
def ledger_group_rule_add():
    """グループにルールを1本足す"""
    if not _is_admin():
        return _deny()
    data = request.get_json(silent=True) or {}
    if not data.get('group_id') or not data.get('unit_id'):
        return jsonify({'success': False, 'error': 'グループと単位は必須です'}), 400
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""INSERT INTO ug_group_rules (group_id, unit_id, role_id, recurse, mode, valid_from, valid_until, note, created_by)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                       (data['group_id'], data['unit_id'], data.get('role_id') or None,
                        1 if data.get('recurse') else 0, data.get('mode') if data.get('mode') in ('include', 'exclude') else 'include',
                        parse_input(_s(data.get('valid_from'))), parse_input(_s(data.get('valid_until'))),
                        _s(data.get('note')) or None, session.get('user_id')))
        conn.commit()
        return jsonify({'success': True, 'id': cursor.lastrowid})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/api/ledger/groups/rules/<int:rule_id>', methods=['DELETE'])
@login_required
def ledger_group_rule_delete(rule_id):
    if not _is_admin():
        return _deny()
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM ug_group_rules WHERE id=%s", (rule_id,))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


def _proposals(cursor):
    """台帳から作れるグループの候補（組織・会議体）を並べる"""
    out = []
    cursor.execute("SELECT id, kind, name FROM ug_roles")
    role_id = {(r['kind'], r['name']): r['id'] for r in cursor.fetchall()}

    # 組織：大学の下の学部等 → 「<単位名>教員」，事務局の課・係 → 「<単位名>」
    cursor.execute("SELECT id, name, parent_id FROM ug_units WHERE kind='org' ORDER BY sort_order, id")
    org = cursor.fetchall()
    by_id = {u['id']: u for u in org}
    for u in org:
        parent = by_id.get(u['parent_id'])
        if not parent:
            continue
        top = parent
        while by_id.get(top['parent_id']):
            top = by_id[top['parent_id']]
        if top['name'] == '大学' and parent['name'] == '大学':
            out.append({'group_name': f"{u['name']}教員", 'unit_id': u['id'], 'unit': u['name'],
                        'rules': [(None, 'include')], 'role_label': '所属する教員', 'recurse': True, 'kind': '組織'})
        elif top['name'] == '事務局':
            out.append({'group_name': u['name'], 'unit_id': u['id'], 'unit': u['name'],
                        'rules': [(None, 'include')], 'role_label': '所属する職員', 'recurse': True, 'kind': '組織'})
    cursor.execute("SELECT id, name FROM ug_units WHERE kind='org' AND name='事務局'")
    r = cursor.fetchone()
    if r:
        out.append({'group_name': '事務局', 'unit_id': r['id'], 'unit': '事務局', 'rules': [(None, 'include')],
                    'role_label': '所属する職員', 'recurse': True, 'kind': '組織'})

    # グループ種別：台帳のグループ単位 → 同名のグループ（管理者・メンバーとも構成員）
    cursor.execute("SELECT id, name FROM ug_units WHERE kind='group' ORDER BY sort_order, id")
    for u in cursor.fetchall():
        out.append({'group_name': u['name'], 'unit_id': u['id'], 'unit': u['name'],
                    'rules': [(None, 'include')], 'role_label': '管理者・メンバー', 'recurse': False, 'kind': 'グループ'})

    # 会議体：末端の単位ごとに「<名称>」（委員側）と「<名称>_事務」（事務職員）
    cursor.execute("SELECT id, name, parent_id FROM ug_units WHERE kind='body' ORDER BY sort_order, id")
    body = cursor.fetchall()
    has_child = {u['parent_id'] for u in body if u['parent_id']}
    for u in body:
        if u['id'] in has_child:
            continue
        staff = [role_id[('body', r)] for r in BODY_STAFF_ROLES if ('body', r) in role_id]
        out.append({'group_name': u['name'], 'unit_id': u['id'], 'unit': u['name'],
                    'rules': [(None, 'include')] + [(rid, 'exclude') for rid in staff],
                    'role_label': '委員（事務職員を除く）', 'recurse': False, 'kind': '会議体'})
        if staff:
            out.append({'group_name': f"{u['name']}_事務", 'unit_id': u['id'], 'unit': u['name'],
                        'rules': [(rid, 'include') for rid in staff],
                        'role_label': '事務職員', 'recurse': False, 'kind': '会議体'})
    return out


@user_groups_bp.route('/api/ledger/groups/proposals')
@login_required
def ledger_group_proposals():
    """台帳から作れるグループの候補と，その構成員（作る前に確かめる）"""
    if not _is_admin():
        return _deny()
    at = _now()
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        from .utils import _rule_member_ids
        cursor.execute("SELECT id, name FROM user_groups")
        exists = {r['name']: r['id'] for r in cursor.fetchall()}
        cursor.execute("SELECT id, full_name FROM users WHERE deleted_at IS NULL")
        uname = {r['id']: r['full_name'] or '' for r in cursor.fetchall()}
        cursor.execute("SELECT group_id, unit_id, role_id FROM ug_group_rules")
        have = {(r['group_id'], r['unit_id'], r['role_id']) for r in cursor.fetchall()}
        items = []
        for p in _proposals(cursor):
            inc, exc = set(), set()
            for rid, mode in p['rules']:
                got = _rule_member_ids(cursor, {'unit_id': p['unit_id'], 'role_id': rid, 'recurse': p['recurse']}, at)
                (exc if mode == 'exclude' else inc).update(got)
            ids = inc - exc
            gid = exists.get(p['group_name'])
            done = bool(gid) and all((gid, p['unit_id'], rid) in have for rid, _m in p['rules'])
            items.append({k: v for k, v in p.items() if k != 'rules'} |
                         {'rule_count': len(p['rules']), 'members': sorted(ids),
                          'member_names': [uname.get(u, f'#{u}') for u in sorted(ids)],
                          'exists': bool(gid), 'group_id': gid, 'done': done})
        return jsonify({'success': True, 'items': items})
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/api/ledger/groups/generate', methods=['POST'])
@login_required
def ledger_group_generate():
    """選んだ候補について，グループを作り（無ければ）ルールを足す"""
    if not _is_admin():
        return _deny()
    data = request.get_json(silent=True) or {}
    want = set(data.get('names') or [])
    if not want:
        return jsonify({'success': False, 'error': '作るグループを選んでください'}), 400
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        me = session.get('user_id')
        cursor.execute("SELECT id, name FROM user_groups")
        exists = {r['name']: r['id'] for r in cursor.fetchall()}
        cursor.execute("SELECT group_id, unit_id, role_id FROM ug_group_rules")
        have = {(r['group_id'], r['unit_id'], r['role_id']) for r in cursor.fetchall()}
        created_groups = added_rules = 0
        for p in _proposals(cursor):
            if p['group_name'] not in want:
                continue
            gid = exists.get(p['group_name'])
            if not gid:
                cursor.execute("""INSERT INTO user_groups (name, description, manager_user_id, created_at, updated_at)
                                  VALUES (%s,%s,%s,%s,%s)""",
                               (p['group_name'], f"台帳から生成（{p['kind']}：{p['unit']}／{p['role_label']}）", me, _now(), _now()))
                gid = cursor.lastrowid
                exists[p['group_name']] = gid
                created_groups += 1
            for rid, mode in p['rules']:
                if (gid, p['unit_id'], rid) in have:
                    continue
                cursor.execute("""INSERT INTO ug_group_rules (group_id, unit_id, role_id, recurse, mode, note, created_by)
                                  VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                               (gid, p['unit_id'], rid, 1 if p['recurse'] else 0, mode, '台帳から生成', me))
                have.add((gid, p['unit_id'], rid))
                added_rules += 1
        conn.commit()
        return jsonify({'success': True, 'groups_created': created_groups, 'rules_added': added_rules})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/api/ledger/groups/sync', methods=['POST'])
@login_required
def ledger_group_sync():
    """
    台帳を users・user_groups 等に反映する（何度実行しても同じ結果になる）．
      1. 名簿 → users（無ければ作る，あれば氏名・区分・所属を合わせる）と approved_users
      2. 名寄せの掛け直し
      3. 台帳の全単位 → user_groups とルール（無ければ作り，足りないルールを足す）
      台帳に対応の無いグループは報告するだけで消さない．
    """
    if not _is_admin():
        return _deny()
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        me = session.get('user_id')
        now = _now()
        data = request.get_json(silent=True) or {}
        roster = _publish_roster(cursor, me, now, create_users=True,
                                 deactivate=bool(data.get('deactivate_expired')))
        cursor.execute("SELECT id, name FROM user_groups")
        exists = {r['name']: r['id'] for r in cursor.fetchall()}
        cursor.execute("SELECT group_id, unit_id, role_id FROM ug_group_rules")
        have = {(r['group_id'], r['unit_id'], r['role_id']) for r in cursor.fetchall()}
        created = added = 0
        from_ledger = set()
        for p in _proposals(cursor):
            from_ledger.add(p['group_name'])
            gid = exists.get(p['group_name'])
            if not gid:
                cursor.execute("""INSERT INTO user_groups (name, description, manager_user_id, created_at, updated_at)
                                  VALUES (%s,%s,%s,%s,%s)""",
                               (p['group_name'], f"台帳から生成（{p['kind']}：{p['unit']}／{p['role_label']}）", me, now, now))
                gid = cursor.lastrowid
                exists[p['group_name']] = gid
                created += 1
            for rid, mode in p['rules']:
                if (gid, p['unit_id'], rid) in have:
                    continue
                cursor.execute("""INSERT INTO ug_group_rules (group_id, unit_id, role_id, recurse, mode, note, created_by)
                                  VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                               (gid, p['unit_id'], rid, 1 if p['recurse'] else 0, mode, '台帳から生成', me))
                have.add((gid, p['unit_id'], rid))
                added += 1
        # 台帳に対応の無いグループ
        extras = []
        for name, gid in exists.items():
            if name in from_ledger:
                continue
            cursor.execute("SELECT COUNT(*) AS n FROM ug_group_rules WHERE group_id=%s", (gid,))
            nr = cursor.fetchone()['n']
            cursor.execute("SELECT COUNT(*) AS n FROM user_group_memberships WHERE group_id=%s", (gid,))
            nm = cursor.fetchone()['n']
            extras.append({'name': name, 'rules': nr, 'direct': nm})
        # 控えてある永続IDを，できあがった users／user_groups に付け直す（永続IDは元帳に従う）
        pids_linked = {'person': 0, 'group': 0}
        try:
            from .pids import link_pending
            cursor.execute("SAVEPOINT before_pids")
            pids_linked = link_pending(cursor)
        except Exception as e:
            logging.warning("link_pending skipped: %s", e)
            try:
                cursor.execute("ROLLBACK TO SAVEPOINT before_pids")
            except Exception:
                pass
        conn.commit()
        return jsonify({'success': True, 'groups_created': created, 'rules_added': added,
                        'from_ledger': len(from_ledger), 'extras': sorted(extras, key=lambda x: x['name']),
                        'roster': roster, 'pids_linked': pids_linked})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/api/ledger/groups/snapshot', methods=['POST'])
@login_required
def ledger_group_snapshot():
    """いまの全グループの構成員を控える（ルール導入前の姿を記録する）"""
    if not _is_admin():
        return _deny()
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        from .utils import _group_member_ids
        at = _now()
        cursor.execute("DELETE FROM ug_group_snapshot")
        cursor.execute("SELECT id, name FROM user_groups")
        n = 0
        for g in cursor.fetchall():
            for uid in _group_member_ids(cursor, g['id'], at):
                cursor.execute("INSERT INTO ug_group_snapshot (taken_at, group_id, group_name, user_id) VALUES (%s,%s,%s,%s)",
                               (at, g['id'], g['name'], uid))
                n += 1
        conn.commit()
        return jsonify({'success': True, 'rows': n, 'taken_at': _dstr(at)})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/api/ledger/groups/compare')
@login_required
def ledger_group_compare():
    """控えた構成員といまの構成員を突き合わせる（ルール0件なら完全一致するはず）"""
    if not _is_admin():
        return _deny()
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        from .utils import _group_member_ids
        at = _now()
        cursor.execute("SELECT MIN(taken_at) AS t, COUNT(*) AS n FROM ug_group_snapshot")
        head = cursor.fetchone()
        if not head['n']:
            return jsonify({'success': False, 'error': 'まだ控えがありません'}), 400
        cursor.execute("SELECT group_id, group_name, user_id FROM ug_group_snapshot")
        snap = {}
        for r in cursor.fetchall():
            snap.setdefault((r['group_id'], r['group_name']), set()).add(r['user_id'])
        cursor.execute("SELECT id, full_name FROM users WHERE deleted_at IS NULL")
        uname = {r['id']: r['full_name'] or '' for r in cursor.fetchall()}
        cursor.execute("SELECT id, name FROM user_groups")
        now_groups = cursor.fetchall()
        diffs = []
        for g in now_groups:
            before = snap.get((g['id'], g['name']), set())
            after = _group_member_ids(cursor, g['id'], at)
            add, rem = after - before, before - after
            if add or rem:
                diffs.append({'group': g['name'],
                              'added': [uname.get(u, f'#{u}') for u in sorted(add)],
                              'removed': [uname.get(u, f'#{u}') for u in sorted(rem)]})
        gone = [name for (gid, name) in snap if not any(g['id'] == gid for g in now_groups)]
        return jsonify({'success': True, 'taken_at': _dstr(head['t']), 'groups': len(now_groups),
                        'identical': not diffs and not gone, 'diffs': diffs, 'groups_gone': gone})
    finally:
        cursor.close()
        conn.close()


# ────────────────────────────────────────────
# 名寄せ（対象外リスト）
# ────────────────────────────────────────────

def _appt_ids_for_name(cursor, name):
    """person_name が name と同じ（空白無視）で user_id 未設定の発令 id"""
    key = _norm_name(name)
    cursor.execute("SELECT id, person_name FROM ug_appointments WHERE user_id IS NULL AND COALESCE(person_name,'')<>''")
    return [r['id'] for r in cursor.fetchall() if _norm_name(r['person_name']) == key]


@user_groups_bp.route('/api/ledger/names')
@login_required
@admin_only
def ledger_names():
    """未解決の氏名（件数・候補）と別名表"""
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        rs = NameResolver(cursor)
        cursor.execute("""
            SELECT a.person_name, u.kind, COUNT(*) AS n
            FROM ug_appointments a JOIN ug_units u ON u.id = a.unit_id
            WHERE a.user_id IS NULL AND COALESCE(a.person_name,'')<>''
            GROUP BY a.person_name, u.kind
        """)
        agg = {}
        for r in cursor.fetchall():
            d = agg.setdefault(r['person_name'], {'name': r['person_name'], 'count': 0, 'kinds': set()})
            d['count'] += r['n']
            d['kinds'].add(KIND_LABEL[r['kind']])
        show_ignored = request.args.get('show_ignored') == '1'
        items = []
        ignored = 0
        for d in agg.values():
            uid, how = rs.resolve(d['name'])
            if how == 'post':
                continue
            if how == 'ignore' and not show_ignored:
                ignored += 1
                continue
            items.append({'name': d['name'], 'count': d['count'], 'kinds': sorted(d['kinds']),
                          'auto': how,                       # いま自動解決できるなら方式（exact/prefix/alias/ignore）
                          'auto_user': next((fn for u, fn, _ in rs.users if u == uid), '') if uid else '',
                          'candidates': rs.candidates(d['name'])})
        items.sort(key=lambda x: (x['auto'] is not None, -x['count'], x['name']))
        cursor.execute("""SELECT id, alias_name, note FROM ug_person_aliases
                          WHERE user_id IS NULL ORDER BY alias_name""")
        excluded = [{'id': r['id'], 'name': r['alias_name'], 'note': r['note'] or ''} for r in cursor.fetchall()]
        cursor.execute("SELECT id, full_name FROM users WHERE deleted_at IS NULL ORDER BY full_name")
        users = [{'id': r['id'], 'full_name': r['full_name'] or ''} for r in cursor.fetchall()]
        return jsonify({'success': True, 'unresolved': items, 'excluded': excluded, 'users': users, 'ignored_hidden': ignored})
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/api/ledger/names/auto', methods=['POST'])
@login_required
def ledger_names_auto():
    """user_id 未設定の発令に名寄せを掛け直す（exact／alias／prefix）"""
    if not _is_admin():
        return _deny()
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        rs = NameResolver(cursor)
        cursor.execute("SELECT id, person_name FROM ug_appointments WHERE user_id IS NULL AND COALESCE(person_name,'')<>''")
        rows = cursor.fetchall()
        by = {'exact': 0, 'alias': 0, 'person': 0}
        updates = []
        for r in rows:
            uid, how = rs.resolve(r['person_name'])
            if uid:
                updates.append((uid, r['id']))
                by[how] += 1
        for uid, aid in updates:
            cursor.execute("UPDATE ug_appointments SET user_id=%s WHERE id=%s", (uid, aid))
        conn.commit()
        return jsonify({'success': True, 'checked': len(rows), 'resolved': len(updates), 'by': by})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/api/ledger/exclusions', methods=['POST'])
@login_required
def ledger_exclusion_add():
    """氏名を対象外にする（誰にも紐づけない）"""
    if not _is_admin():
        return _deny()
    data = request.get_json(silent=True) or {}
    name = _s(data.get('name'))
    if not name:
        return jsonify({'success': False, 'error': '氏名が空です'}), 400
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        key = _norm_name(name)
        cursor.execute("SELECT id, alias_name FROM ug_person_aliases")
        hit = next((r['id'] for r in cursor.fetchall() if _norm_name(r['alias_name']) == key), None)
        if hit:
            cursor.execute("UPDATE ug_person_aliases SET user_id=NULL, note=%s WHERE id=%s",
                           (_s(data.get('note')) or '対象外', hit))
            new_id = hit
        else:
            cursor.execute("""INSERT INTO ug_person_aliases (alias_name, user_id, note, created_by)
                              VALUES (%s, NULL, %s, %s)""",
                           (name, _s(data.get('note')) or '対象外', session.get('user_id')))
            new_id = cursor.lastrowid
        conn.commit()
        return jsonify({'success': True, 'id': new_id})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/api/ledger/exclusions/<int:excl_id>', methods=['DELETE'])
@login_required
def ledger_exclusion_delete(excl_id):
    """対象外を解除する（未解決一覧に戻る）"""
    if not _is_admin():
        return _deny()
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM ug_person_aliases WHERE id=%s AND user_id IS NULL", (excl_id,))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/api/ledger/names/link', methods=['POST'])
@login_required
def ledger_name_link():
    """未解決の氏名を users に結びつける（発令の user_id を直接更新．別名は作らない）"""
    if not _is_admin():
        return _deny()
    data = request.get_json(silent=True) or {}
    name = _s(data.get('name'))
    user_id = data.get('user_id')
    if not name or not user_id:
        return jsonify({'success': False, 'error': '氏名とユーザは必須です'}), 400
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id FROM users WHERE id=%s AND deleted_at IS NULL", (user_id,))
        if not cursor.fetchone():
            return jsonify({'success': False, 'error': 'そのユーザは見つかりません'}), 400
        ids = _appt_ids_for_name(cursor, name)
        for aid in ids:
            cursor.execute("UPDATE ug_appointments SET user_id=%s WHERE id=%s", (user_id, aid))
        conn.commit()
        return jsonify({'success': True, 'linked': len(ids)})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/api/ledger/aliases', methods=['POST'])
@login_required
def ledger_alias_upsert():
    """
    {alias_name, user_id|null, note}
    user_id あり … 別名を登録し，同じ氏名の未解決発令をその人に紐づける
    user_id なし … 「紐づけ不要」（外部の方など）として登録し，未解決一覧から外す
    """
    if not _is_admin():
        return _deny()
    data = request.get_json(silent=True) or {}
    alias = _s(data.get('alias_name'))
    if not alias:
        return jsonify({'success': False, 'error': '別名が空です'}), 400
    user_id = data.get('user_id') or None
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        key = _norm_name(alias)
        cursor.execute("SELECT id, alias_name FROM ug_person_aliases")
        hit = next((r['id'] for r in cursor.fetchall() if _norm_name(r['alias_name']) == key), None)
        if hit:
            cursor.execute("UPDATE ug_person_aliases SET alias_name=%s, user_id=%s, note=%s WHERE id=%s",
                           (alias, user_id, _s(data.get('note')) or None, hit))
            alias_id = hit
        else:
            cursor.execute("INSERT INTO ug_person_aliases (alias_name, user_id, note, created_by) VALUES (%s,%s,%s,%s)",
                           (alias, user_id, _s(data.get('note')) or None, session.get('user_id')))
            alias_id = cursor.lastrowid
        linked = 0
        if user_id:
            ids = _appt_ids_for_name(cursor, alias)
            for aid in ids:
                cursor.execute("UPDATE ug_appointments SET user_id=%s WHERE id=%s", (user_id, aid))
            linked = len(ids)
        conn.commit()
        return jsonify({'success': True, 'id': alias_id, 'linked': linked})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/api/ledger/aliases/<int:alias_id>', methods=['DELETE'])
@login_required
def ledger_alias_delete(alias_id):
    """別名を消す．?unlink=1 なら，その別名で紐づいた発令の user_id も外す"""
    if not _is_admin():
        return _deny()
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT alias_name, user_id FROM ug_person_aliases WHERE id=%s", (alias_id,))
        row = cursor.fetchone()
        if not row:
            return jsonify({'success': False, 'error': '見つかりません'}), 404
        unlinked = 0
        if request.args.get('unlink') == '1' and row['user_id']:
            key = _norm_name(row['alias_name'])
            cursor.execute("SELECT id, person_name FROM ug_appointments WHERE user_id=%s", (row['user_id'],))
            ids = [r['id'] for r in cursor.fetchall() if _norm_name(r['person_name']) == key]
            for aid in ids:
                cursor.execute("UPDATE ug_appointments SET user_id=NULL WHERE id=%s", (aid,))
            unlinked = len(ids)
        cursor.execute("DELETE FROM ug_person_aliases WHERE id=%s", (alias_id,))
        conn.commit()
        return jsonify({'success': True, 'unlinked': unlinked})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


# ────────────────────────────────────────────
# アカウント名簿（ug_persons → approved_users）
# ────────────────────────────────────────────

@user_groups_bp.route('/api/ledger/roster')
@login_required
@admin_only
def ledger_roster():
    """名簿の一覧と，users／approved_users との突合状態"""
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""SELECT id, full_name, email, category, affiliation, valid_from, valid_until, note
                          FROM ug_persons ORDER BY affiliation, full_name""")
        persons = cursor.fetchall()
        cursor.execute("SELECT id, email, is_active, deleted_at, password_hash FROM users")
        users = {r['email'].strip().lower(): r for r in cursor.fetchall() if r['email']}
        cursor.execute("SELECT email, approved_at FROM approved_users")
        approved = {r['email'].strip().lower(): r['approved_at'] for r in cursor.fetchall()}
        now = _now()
        items = []
        for p in persons:
            em = (p['email'] or '').strip().lower()
            u = users.get(em) if em else None
            active = _valid_at(p['valid_from'], p['valid_until'], now)
            items.append({'id': p['id'], 'full_name': p['full_name'], 'email': p['email'] or '', 'category': p['category'],
                          'affiliation': p['affiliation'] or '', 'from': _dstr(p['valid_from']), 'until': _dstr(p['valid_until']),
                          'note': p['note'] or '', 'active': active,
                          'user_id': (u['id'] if u and not u['deleted_at'] else None),
                          'user_inactive': bool(u and not u['deleted_at'] and not u['is_active']),
                          'has_password': bool(u and u.get('password_hash')),
                          'approved': bool(em) and em in approved})
        # 台帳未登録の users（名簿にメールが無いアカウント．立ち上げ時に手で作った admin など）
        listed = {(p['email'] or '').strip().lower() for p in persons if p['email']}
        cursor.execute("""SELECT id, full_name, email, category, affiliation, is_active
                          FROM users WHERE deleted_at IS NULL ORDER BY id""")
        unregistered = [{'user_id': r['id'], 'full_name': r['full_name'] or '', 'email': r['email'] or '',
                         'category': r['category'] or '', 'affiliation': r['affiliation'] or '',
                         'inactive': not r['is_active']}
                        for r in cursor.fetchall()
                        if (r['email'] or '').strip().lower() not in listed]
        n_users = sum(1 for i in items if i['user_id'])
        n_appr = sum(1 for i in items if i['approved'])
        n_pending = sum(1 for i in items if i['active'] and i['email'] and not i['user_id'] and not i['approved'])
        n_noemail = sum(1 for i in items if not i['email'])
        return jsonify({'success': True, 'items': items, 'unregistered': unregistered,
                        'counts': {'total': len(items), 'active': sum(1 for i in items if i['active']),
                                   'users': n_users, 'approved': n_appr, 'pending': n_pending, 'no_email': n_noemail,
                                   'unregistered': len(unregistered)}})
    finally:
        cursor.close()
        conn.close()


def _publish_roster(cursor, me, now, create_users=False, deactivate=False):
    """
    名簿を users / approved_users に反映する（台帳が正）．
      有効期間内 … users にあれば 氏名・区分・所属 を合わせ，無ければ作る（create_users）か approved_users に写す
      期限切れ   … approved_users から外す．deactivate なら users も無効化
    最後に名寄せを掛け直す．
    """
    cursor.execute("SELECT full_name, email, category, affiliation, valid_from, valid_until FROM ug_persons")
    persons = cursor.fetchall()
    cursor.execute("SELECT email FROM users WHERE deleted_at IS NULL")
    has_user = {r['email'].strip().lower() for r in cursor.fetchall() if r['email']}
    added = updated = removed = skipped = created = deactivated = updated_users = 0
    warnings = []
    for p in persons:
        if not p['email']:
            continue
        em = p['email'].strip().lower()
        if _valid_at(p['valid_from'], p['valid_until'], now):
            if em in has_user:
                uid, action, w = _sync_user_from_person(cursor, p, me, now)
                if action == 'updated':
                    updated_users += 1
                if w:
                    warnings.append(w)
                skipped += 1
                continue
            if create_users:
                uid, action, w = _sync_user_from_person(cursor, p, me, now)
                if w:
                    warnings.append(w)
                if action in ('created', 'revived'):
                    created += 1
                    has_user.add(em)
                    continue
            cursor.execute("SELECT id FROM approved_users WHERE email=%s", (em,))
            if cursor.fetchone():
                cursor.execute("""UPDATE approved_users SET full_name=%s, category=%s, affiliation=%s, notes=%s
                                  WHERE email=%s""",
                               (p['full_name'], p['category'], p['affiliation'], '台帳の名簿から発行', em))
                updated += 1
            else:
                cursor.execute("""INSERT INTO approved_users (email, full_name, category, affiliation, approved_by, approved_at, notes)
                                  VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                               (em, p['full_name'], p['category'], p['affiliation'], me, now, '台帳の名簿から発行'))
                added += 1
        else:
            cursor.execute("DELETE FROM approved_users WHERE email=%s", (em,))
            removed += cursor.rowcount
            if deactivate and em in has_user:
                u = _user_by_email(cursor, em)
                if u and u['is_active'] and u['id'] != me and not (u['category'] == 'admin' and _admin_count(cursor, exclude_user_id=u['id']) == 0):
                    cursor.execute("UPDATE users SET is_active=FALSE, updated_at=%s WHERE id=%s", (now, u['id']))
                    deactivated += 1
    rs = NameResolver(cursor)
    cursor.execute("SELECT id, person_name FROM ug_appointments WHERE user_id IS NULL AND COALESCE(person_name,'')<>''")
    linked = 0
    for r in cursor.fetchall():
        uid, how = rs.resolve(r['person_name'])
        if uid:
            cursor.execute("UPDATE ug_appointments SET user_id=%s WHERE id=%s", (uid, r['id']))
            linked += 1
    return {'added': added, 'updated': updated, 'removed': removed, 'already_users': skipped,
            'users_created': created, 'users_updated': updated_users, 'users_deactivated': deactivated,
            'deleted_users_skipped': 0, 'linked': linked, 'warnings': warnings}


@user_groups_bp.route('/api/ledger/roster/publish', methods=['POST'])
@login_required
def ledger_roster_publish():
    """名簿を users / approved_users に反映する（名簿画面の「発行」）"""
    if not _is_admin():
        return _deny()
    data = request.get_json(silent=True) or {}
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        res = _publish_roster(cursor, session.get('user_id'), _now(),
                              create_users=bool(data.get('create_users')),
                              deactivate=bool(data.get('deactivate_expired')))
        conn.commit()
        return jsonify({'success': True, **res})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


def _admin_count(cursor, exclude_user_id=None):
    cursor.execute("SELECT COUNT(*) AS n FROM users WHERE category='admin' AND is_active=TRUE AND deleted_at IS NULL")
    n = cursor.fetchone()['n']
    if exclude_user_id:
        cursor.execute("SELECT category, is_active, deleted_at FROM users WHERE id=%s", (exclude_user_id,))
        r = cursor.fetchone()
        if r and r['category'] == 'admin' and r['is_active'] and not r['deleted_at']:
            n -= 1
    return n


def _user_by_email(cursor, email, include_deleted=False):
    if not email:
        return None
    cursor.execute("SELECT id, email, full_name, category, affiliation, is_active, deleted_at FROM users WHERE email=%s", (email,))
    u = cursor.fetchone()
    if u and u['deleted_at'] and not include_deleted:
        return None
    return u


def _sync_user_from_person(cursor, person, me, now):
    """
    名簿の1行を users に反映する（台帳が正）．
      users に無い       … 作る（削除済みなら復活）
      users にある       … full_name / category / affiliation を上書き
    安全弁：自分自身と，最後の admin は降格しない．
    戻り値 (user_id, action, warning)
    """
    em = (person['email'] or '').strip().lower()
    if not em:
        return None, 'skip', None
    u = _user_by_email(cursor, em, include_deleted=True)
    if not u:
        cursor.execute("""INSERT INTO users (email, full_name, category, affiliation, is_active, created_at, updated_at)
                          VALUES (%s,%s,%s,%s,TRUE,%s,%s)""",
                       (em, person['full_name'], person['category'], person['affiliation'], now, now))
        return cursor.lastrowid, 'created', None
    if u['deleted_at']:
        cursor.execute("""UPDATE users SET full_name=%s, category=%s, affiliation=%s, is_active=TRUE,
                          deleted_at=NULL, deleted_by=NULL, updated_at=%s WHERE id=%s""",
                       (person['full_name'], person['category'], person['affiliation'], now, u['id']))
        return u['id'], 'revived', None
    new_cat = person['category']
    warning = None
    if u['category'] == 'admin' and new_cat != 'admin':
        if u['id'] == me:
            new_cat, warning = 'admin', f"{em}: 自分自身は admin から降格しません"
        elif _admin_count(cursor, exclude_user_id=u['id']) == 0:
            new_cat, warning = 'admin', f"{em}: 最後の admin なので降格しません"
    if (u['full_name'], u['category'], u['affiliation']) != (person['full_name'], new_cat, person['affiliation']):
        cursor.execute("UPDATE users SET full_name=%s, category=%s, affiliation=%s, updated_at=%s WHERE id=%s",
                       (person['full_name'], new_cat, person['affiliation'], now, u['id']))
        return u['id'], 'updated', warning
    return u['id'], 'same', warning


def _drop_ignore_alias(cursor, name):
    """氏名にメールが付いたら，同名の「紐づけ不要」別名（user_id NULL）を外す"""
    key = _norm_name(name)
    if not key:
        return 0
    cursor.execute("SELECT id, alias_name FROM ug_person_aliases WHERE user_id IS NULL")
    ids = [r['id'] for r in cursor.fetchall() if _norm_name(r['alias_name']) == key]
    for i in ids:
        cursor.execute("DELETE FROM ug_person_aliases WHERE id=%s", (i,))
    return len(ids)


def _relink_name(cursor, name):
    """その氏名の未解決発令を名寄せし直す"""
    _drop_ignore_alias(cursor, name)
    rs = NameResolver(cursor)
    uid, how = rs.resolve(name)
    if not uid:
        return 0
    ids = _appt_ids_for_name(cursor, name)
    for aid in ids:
        cursor.execute("UPDATE ug_appointments SET user_id=%s WHERE id=%s", (uid, aid))
    return len(ids)


@user_groups_bp.route('/api/ledger/roster/adopt', methods=['POST'])
@login_required
def ledger_roster_adopt():
    """
    台帳未登録の users を名簿に載せる（台帳登録）．
    body: {user_ids: [..]}．users の氏名・メール・区分・所属をそのまま名簿に写す．users は触らない．
    """
    if not _is_admin():
        return _deny()
    data = request.get_json(silent=True) or {}
    try:
        ids = [int(x) for x in (data.get('user_ids') or [])]
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'user_ids が不正です'}), 400
    if not ids:
        return jsonify({'success': False, 'error': '登録するユーザがありません'}), 400
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        adopted, skipped, linked = [], [], 0
        for uid in ids:
            cursor.execute("""SELECT id, full_name, email, category, affiliation FROM users
                              WHERE id=%s AND deleted_at IS NULL""", (uid,))
            u = cursor.fetchone()
            em = ((u or {}).get('email') or '').strip().lower()
            if not u or not em:
                skipped.append({'user_id': uid, 'reason': 'users に無いかメールが空'})
                continue
            cursor.execute("SELECT id FROM ug_persons WHERE email=%s", (em,))
            if cursor.fetchone():
                skipped.append({'user_id': uid, 'reason': '名簿に既にある'})
                continue
            fn = _s(u['full_name']) or em
            cat = u['category'] if u['category'] in PERSON_CATEGORIES else 'regular'
            cursor.execute("""INSERT INTO ug_persons (full_name, email, category, affiliation, note)
                              VALUES (%s,%s,%s,%s,%s)""",
                           (fn, em, cat, u['affiliation'] or None, '台帳未登録のアカウントを台帳登録'))
            _drop_ignore_alias(cursor, fn)
            linked += _relink_name(cursor, fn)
            adopted.append(uid)
        conn.commit()
        return jsonify({'success': True, 'adopted': adopted, 'skipped': skipped, 'linked': linked})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/api/ledger/roster', methods=['POST'])
@login_required
def ledger_roster_create():
    """名簿に人を足す．メールがあれば users も直ちに作る（台帳が正）"""
    if not _is_admin():
        return _deny()
    data = request.get_json(silent=True) or {}
    fn = _s(data.get('full_name'))
    em = _s(data.get('email')).lower()
    if not fn:
        return jsonify({'success': False, 'error': '氏名は必須です'}), 400
    if em and '@' not in em:
        return jsonify({'success': False, 'error': 'メールの形式が不正です'}), 400
    cat = data.get('category') if data.get('category') in PERSON_CATEGORIES else 'regular'
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        if em:
            cursor.execute("SELECT id FROM ug_persons WHERE email=%s", (em,))
            if cursor.fetchone():
                return jsonify({'success': False, 'error': 'そのメールは名簿に既にあります'}), 400
        now = _now()
        person = {'full_name': fn, 'email': em or None, 'category': cat, 'affiliation': _s(data.get('affiliation')) or None}
        cursor.execute("""INSERT INTO ug_persons (full_name, email, category, affiliation, valid_from, valid_until, note)
                          VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                       (fn, em or None, cat, person['affiliation'],
                        parse_input(_s(data.get('valid_from'))), parse_input(_s(data.get('valid_until'))),
                        _s(data.get('note')) or None))
        pid = cursor.lastrowid
        uid, action, warning = _sync_user_from_person(cursor, person, session.get('user_id'), now)
        linked = _relink_name(cursor, fn) if uid else 0
        conn.commit()
        return jsonify({'success': True, 'id': pid, 'user_id': uid, 'user_action': action, 'linked': linked, 'warning': warning})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/api/ledger/roster/<int:person_id>', methods=['PUT'])
@login_required
def ledger_roster_update(person_id):
    """名簿の1行を直す（メール・区分・所属・期間・備考）．メールは他の行と重複不可"""
    if not _is_admin():
        return _deny()
    data = request.get_json(silent=True) or {}
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        fields, vals = [], []
        if 'email' in data:
            em = _s(data['email']).lower()
            if em and '@' not in em:
                return jsonify({'success': False, 'error': 'メールの形式が不正です'}), 400
            if em:
                cursor.execute("SELECT id FROM ug_persons WHERE email=%s AND id<>%s", (em, person_id))
                if cursor.fetchone():
                    return jsonify({'success': False, 'error': 'そのメールは名簿の別の行にあります'}), 400
            fields.append("email=%s"); vals.append(em or None)
        if 'full_name' in data and _s(data['full_name']):
            fields.append("full_name=%s"); vals.append(_s(data['full_name']))
        if 'category' in data and data['category'] in PERSON_CATEGORIES:
            fields.append("category=%s"); vals.append(data['category'])
        for col in ('affiliation', 'note'):
            if col in data:
                fields.append(f"{col}=%s"); vals.append(_s(data[col]) or None)
        for col in ('valid_from', 'valid_until'):
            if col in data:
                fields.append(f"{col}=%s"); vals.append(parse_input(_s(data[col])))
        if not fields:
            return jsonify({'success': False, 'error': '変更がありません'}), 400
        cursor.execute("SELECT email FROM ug_persons WHERE id=%s", (person_id,))
        before = cursor.fetchone()
        if not before:
            return jsonify({'success': False, 'error': '名簿にありません'}), 404
        old_email = (before['email'] or '').lower()
        cursor.execute(f"UPDATE ug_persons SET {', '.join(fields)} WHERE id=%s", tuple(vals) + (person_id,))
        cursor.execute("SELECT full_name, email, category, affiliation FROM ug_persons WHERE id=%s", (person_id,))
        person = cursor.fetchone()
        now = _now()
        me = session.get('user_id')
        warning = None
        uid = None
        action = 'skip'
        new_email = (person['email'] or '').lower()
        if old_email and new_email and old_email != new_email:
            # メール変更：既存 users のメールを付け替える（新メールが users に無いとき）
            u_old = _user_by_email(cursor, old_email)
            if u_old and not _user_by_email(cursor, new_email, include_deleted=True):
                cursor.execute("UPDATE users SET email=%s, updated_at=%s WHERE id=%s", (new_email, now, u_old['id']))
                cursor.execute("UPDATE approved_users SET email=%s WHERE email=%s", (new_email, old_email))
        if new_email:
            uid, action, warning = _sync_user_from_person(cursor, person, me, now)
            if uid:
                _relink_name(cursor, person['full_name'])
        conn.commit()
        return jsonify({'success': True, 'user_id': uid, 'user_action': action, 'warning': warning})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/api/ledger/roster/<int:person_id>/password_mail', methods=['POST'])
@login_required
def ledger_roster_password_mail(person_id):
    """名簿の人（users あり）にパスワード設定メールを送る．Google を使わない人の初回設定・再設定用"""
    if not _is_admin():
        return _deny()
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT email, full_name FROM ug_persons WHERE id=%s", (person_id,))
        p = cursor.fetchone()
        if not p:
            return jsonify({'success': False, 'error': '名簿にありません'}), 404
        if not p['email']:
            return jsonify({'success': False, 'error': 'メール未記入です'}), 400
        cursor.execute("SELECT id FROM users WHERE email=%s AND deleted_at IS NULL AND is_active=TRUE", (p['email'],))
        u = cursor.fetchone()
        if not u:
            return jsonify({'success': False, 'error': 'users にまだありません（先に「users まで作る」で発行してください）'}), 400
    finally:
        cursor.close()
        conn.close()
    try:
        from utils import create_password_reset_token, send_password_reset_email
        token = create_password_reset_token(u['id'])
        send_password_reset_email(p['email'], token)
        return jsonify({'success': True})
    except Exception as e:
        logging.exception("password mail failed")
        return jsonify({'success': False, 'error': f'メール送信に失敗: {e}'}), 500


@user_groups_bp.route('/api/ledger/roster/<int:person_id>', methods=['DELETE'])
@login_required
def ledger_roster_delete(person_id):
    """名簿から消す．?with_user=1 なら users も論理削除し approved_users からも外す（台帳が正）．
    自分自身と最後の admin は消さない．発令の user_id は履歴として残す"""
    if not _is_admin():
        return _deny()
    with_user = request.args.get('with_user') == '1'
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT email FROM ug_persons WHERE id=%s", (person_id,))
        p = cursor.fetchone()
        if not p:
            return jsonify({'success': False, 'error': '名簿にありません'}), 404
        em = (p['email'] or '').lower()
        me = session.get('user_id')
        user_action = 'skip'
        if with_user and em:
            u = _user_by_email(cursor, em)
            if u:
                if u['id'] == me:
                    return jsonify({'success': False, 'error': '自分自身は削除できません'}), 400
                if u['category'] == 'admin' and _admin_count(cursor, exclude_user_id=u['id']) == 0:
                    return jsonify({'success': False, 'error': '最後の admin は削除できません'}), 400
                now = _now()
                cursor.execute("UPDATE users SET is_active=FALSE, deleted_at=%s, deleted_by=%s, updated_at=%s WHERE id=%s",
                               (now, me, now, u['id']))
                user_action = 'deleted'
            cursor.execute("DELETE FROM approved_users WHERE email=%s", (em,))
        cursor.execute("DELETE FROM ug_persons WHERE id=%s", (person_id,))
        conn.commit()
        return jsonify({'success': True, 'user_action': user_action})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()



# ────────────────────────────────────────────
# 登録申請（registration_requests）とブラックリスト
# ────────────────────────────────────────────

REQ_STATUSES = ('pending', 'approved', 'rejected', 'blacklisted')


def _req_columns(cursor):
    cursor.execute("""SELECT COLUMN_NAME FROM information_schema.COLUMNS
                      WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'registration_requests'""")
    return {r['COLUMN_NAME'] for r in cursor.fetchall()}


def _req_set_status(cursor, req_id, status, me, now, note=None):
    """status を更新．処理者・処理日時・メモの列があれば一緒に書く（列名は環境差があるので存在するものだけ）"""
    cols = _req_columns(cursor)
    sets, vals = ["status=%s"], [status]
    for c in ('processed_at', 'reviewed_at', 'approved_at', 'updated_at'):
        if c in cols:
            sets.append(f"{c}=%s"); vals.append(now)
    for c in ('processed_by', 'reviewed_by', 'approved_by'):
        if c in cols:
            sets.append(f"{c}=%s"); vals.append(me)
    if note is not None:
        for c in ('notes', 'note', 'admin_note', 'reason'):
            if c in cols:
                sets.append(f"{c}=%s"); vals.append(note)
                break
    cursor.execute(f"UPDATE registration_requests SET {', '.join(sets)} WHERE id=%s", tuple(vals) + (req_id,))


@user_groups_bp.route('/api/ledger/requests')
@login_required
def ledger_requests():
    """登録申請の一覧（status ごと）"""
    if not _is_admin():
        return _deny()
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cols = _req_columns(cursor)
        cursor.execute("SELECT * FROM registration_requests ORDER BY id DESC")
        rows = cursor.fetchall()
        cursor.execute("SELECT email FROM users WHERE deleted_at IS NULL")
        has_user = {r['email'].strip().lower() for r in cursor.fetchall() if r['email']}
        cursor.execute("SELECT email FROM ug_persons WHERE email IS NOT NULL")
        in_roster = {r['email'].strip().lower() for r in cursor.fetchall()}
        items = []
        for r in rows:
            em = (r.get('email') or '').strip().lower()
            items.append({'id': r['id'], 'email': r.get('email') or '', 'full_name': r.get('full_name') or '',
                          'category': r.get('category') or '', 'affiliation': r.get('affiliation') or '',
                          'status': r.get('status') or '', 'requested_at': _dstr(r.get('requested_at')),
                          'ip': r.get('ip_address') or '', 'note': next((r[c] for c in ('notes', 'note', 'admin_note', 'reason') if c in cols and r.get(c)), ''),
                          'in_users': em in has_user, 'in_roster': em in in_roster})
        counts = {st: sum(1 for i in items if i['status'] == st) for st in REQ_STATUSES}
        return jsonify({'success': True, 'items': items, 'counts': counts})
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/api/ledger/requests/<int:req_id>/<action>', methods=['POST'])
@login_required
def ledger_request_action(req_id, action):
    """
    approve   … 名簿に載せ，users を直ちに作る（台帳が正），status=approved
    reject    … status=rejected
    blacklist … status=blacklisted（users があれば無効化）
    unblock   … ブラックリスト解除（status=rejected）
    """
    if not _is_admin():
        return _deny()
    if action not in ('approve', 'reject', 'blacklist', 'unblock'):
        return jsonify({'success': False, 'error': '不正な操作'}), 400
    data = request.get_json(silent=True) or {}
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT * FROM registration_requests WHERE id=%s", (req_id,))
        r = cursor.fetchone()
        if not r:
            return jsonify({'success': False, 'error': '申請がありません'}), 404
        em = (r['email'] or '').strip().lower()
        me = session.get('user_id')
        now = _now()
        result = {}
        if action == 'approve':
            cat = data.get('category') if data.get('category') in PERSON_CATEGORIES else (r.get('category') if r.get('category') in PERSON_CATEGORIES else 'regular')
            fn = _s(data.get('full_name')) or r.get('full_name') or em
            aff = _s(data.get('affiliation')) or r.get('affiliation') or None
            cursor.execute("SELECT id FROM ug_persons WHERE email=%s", (em,))
            p = cursor.fetchone()
            if p:
                cursor.execute("UPDATE ug_persons SET full_name=%s, category=%s, affiliation=%s WHERE id=%s", (fn, cat, aff, p['id']))
            else:
                cursor.execute("INSERT INTO ug_persons (full_name, email, category, affiliation, note) VALUES (%s,%s,%s,%s,%s)",
                               (fn, em, cat, aff, f'登録申請 #{req_id} を承認'))
            uid, ua, w = _sync_user_from_person(cursor, {'full_name': fn, 'email': em, 'category': cat, 'affiliation': aff}, me, now)
            _relink_name(cursor, fn)
            _req_set_status(cursor, req_id, 'approved', me, now, _s(data.get('note')) or None)
            result = {'user_id': uid, 'user_action': ua, 'warning': w}
        elif action == 'reject':
            _req_set_status(cursor, req_id, 'rejected', me, now, _s(data.get('note')) or None)
        elif action == 'blacklist':
            _req_set_status(cursor, req_id, 'blacklisted', me, now, _s(data.get('note')) or None)
            u = _user_by_email(cursor, em)
            if u and u['id'] != me and not (u['category'] == 'admin' and _admin_count(cursor, exclude_user_id=u['id']) == 0):
                cursor.execute("UPDATE users SET is_active=FALSE, updated_at=%s WHERE id=%s", (now, u['id']))
                result['user_deactivated'] = True
            cursor.execute("DELETE FROM approved_users WHERE email=%s", (em,))
        elif action == 'unblock':
            _req_set_status(cursor, req_id, 'rejected', me, now, _s(data.get('note')) or None)
        conn.commit()
        return jsonify({'success': True, **result})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/api/ledger/requests/blacklist', methods=['POST'])
@login_required
def ledger_blacklist_add():
    """申請が無いメールを手でブラックリストに載せる（registration_requests に blacklisted 行を作る）"""
    if not _is_admin():
        return _deny()
    data = request.get_json(silent=True) or {}
    em = _s(data.get('email')).lower()
    if not em or '@' not in em:
        return jsonify({'success': False, 'error': 'メールの形式が不正です'}), 400
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id FROM registration_requests WHERE email=%s AND status='blacklisted'", (em,))
        if cursor.fetchone():
            return jsonify({'success': False, 'error': '既にブラックリストにあります'}), 400
        cols = _req_columns(cursor)
        now = _now()
        fields, vals = ['email', 'full_name', 'status'], [em, _s(data.get('full_name')) or '（手動登録）', 'blacklisted']
        if 'category' in cols:
            fields.append('category'); vals.append('guest')
        if 'requested_at' in cols:
            fields.append('requested_at'); vals.append(now)
        for c in ('notes', 'note', 'admin_note', 'reason'):
            if c in cols:
                fields.append(c); vals.append(_s(data.get('note')) or '手動でブラックリスト登録')
                break
        cursor.execute(f"INSERT INTO registration_requests ({', '.join(fields)}) VALUES ({', '.join(['%s']*len(fields))})", tuple(vals))
        new_id = cursor.lastrowid
        u = _user_by_email(cursor, em)
        if u and u['id'] != session.get('user_id') and not (u['category'] == 'admin' and _admin_count(cursor, exclude_user_id=u['id']) == 0):
            cursor.execute("UPDATE users SET is_active=FALSE, updated_at=%s WHERE id=%s", (now, u['id']))
        cursor.execute("DELETE FROM approved_users WHERE email=%s", (em,))
        conn.commit()
        return jsonify({'success': True, 'id': new_id})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


# ────────────────────────────────────────────
# Excel 入出力
# ────────────────────────────────────────────

def _wb_from_rows(units_rows, roles_rows, appt_rows, person_rows=()):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    head_font = Font(bold=True)
    head_fill = PatternFill('solid', fgColor='DDEBF7')

    def sheet(title, headers, rows, widths):
        ws = wb.active if wb.active.title == 'Sheet' else wb.create_sheet()
        ws.title = title
        ws.append(headers)
        for c in ws[1]:
            c.font = head_font
            c.fill = head_fill
            c.alignment = Alignment(vertical='center')
        for r in rows:
            ws.append(r)
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = 'A2'
        return ws

    sheet(SHEET_UNITS, UNIT_HEADERS, units_rows, [10] + [18] * MAX_DEPTH + [6, 16, 12, 12, 30])
    sheet(SHEET_ROLES, ROLE_HEADERS, roles_rows, [10, 22, 6, 30])
    sheet(SHEET_APPT, APPT_HEADERS, appt_rows, [10] + [18] * MAX_DEPTH + [14, 16, 12, 12, 30, 16])
    sheet(SHEET_PERSONS, PERSON_HEADERS, person_rows, [18, 36, 9, 14, 12, 12, 30])
    return wb


def _dump_rows(cursor):
    """DB の台帳を Excel の3シート分の行に展開"""
    cursor.execute("SELECT id, name FROM user_groups")
    gname = {r['id']: r['name'] for r in cursor.fetchall()}

    unit_rows, appt_rows = [], []
    unit_path = {}
    for kind, label in KINDS:
        for u in _tree_order(_load_units(cursor, kind)):
            p = (u['path'] + [''] * MAX_DEPTH)[:MAX_DEPTH]
            unit_path[u['id']] = (label, p)
            unit_rows.append([label] + p + [u['sort_order'], gname.get(u['owner_group_id'], ''),
                                            _dstr(u['valid_from']), _dstr(u['valid_until']), u['note'] or ''])

    cursor.execute("SELECT kind, name, `rank`, note FROM ug_roles ORDER BY kind, `rank`, sort_order, id")
    role_rows = [[KIND_LABEL.get(r['kind'], r['kind']), r['name'], r['rank'], r['note'] or ''] for r in cursor.fetchall()]

    cursor.execute("""
        SELECT a.unit_id, a.person_name, a.valid_from, a.valid_until, a.note, a.source,
               r.name AS role_name, us.full_name
        FROM ug_appointments a
        JOIN ug_roles r ON r.id = a.role_id
        LEFT JOIN users us ON us.id = a.user_id
        ORDER BY a.unit_id, r.`rank`, a.valid_from, a.id
    """)
    for a in cursor.fetchall():
        label, p = unit_path.get(a['unit_id'], ('?', [''] * MAX_DEPTH))
        appt_rows.append([label] + p + [a['role_name'], a['person_name'] or a['full_name'] or '',
                                        _dstr(a['valid_from']), _dstr(a['valid_until']),
                                        a['note'] or '', a['source'] or ''])
    person_rows = []
    try:
        cursor.execute("SELECT full_name, email, category, affiliation, valid_from, valid_until, note FROM ug_persons ORDER BY affiliation, full_name")
        person_rows = [[r['full_name'], r['email'], r['category'], r['affiliation'] or '', _dstr(r['valid_from']), _dstr(r['valid_until']), r['note'] or '']
                       for r in cursor.fetchall()]
    except Exception as e:
        logging.warning("ug_persons unavailable: %s", e)
    return unit_rows, role_rows, appt_rows, person_rows


@user_groups_bp.route('/ledger/export.xlsx')
@login_required
@admin_only
def ledger_export():
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        unit_rows, role_rows, appt_rows, person_rows = _dump_rows(cursor)
    finally:
        cursor.close()
        conn.close()
    wb = _wb_from_rows(unit_rows, role_rows, appt_rows, person_rows)
    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    fname = f"maiguru_ledger_{_now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return send_file(bio, as_attachment=True, download_name=fname,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@user_groups_bp.route('/ledger/template.xlsx')
@login_required
@admin_only
def ledger_template():
    """空の雛形（見本として汎用の行を数行入れてある）"""
    units = [
        ['組織', '事務局', '', '', '', '', 10, '', '', '', ''],
        ['組織', '事務局', '総務課', '', '', '', 10, '', '', '', ''],
        ['組織', '事務局', '総務課', '総務係', '', '', 10, '', '', '', ''],
        ['会議体', '全学委員会', '', '', '', '', 10, '', '', '', ''],
        ['会議体', '全学委員会', '情報委員会', '', '', '', 10, '', '', '', ''],
        ['分担事務', '総務課', '総務係', '文書の管理に関すること', '', '', 10, '', '', '', ''],
    ]
    roles = [
        ['組織', '課長', 1, ''], ['組織', '係長', 2, ''], ['組織', '主事', 3, ''],
        ['会議体', '委員長', 1, ''], ['会議体', '委員', 2, ''], ['会議体', '事務職員', 3, ''],
        ['分担事務', '主担当', 1, ''], ['分担事務', '副担当', 2, ''],
    ]
    appts = [
        ['組織', '事務局', '総務課', '', '', '', '課長', '山田 太郎', '2026-04-01', '', '', '見本'],
        ['組織', '事務局', '総務課', '総務係', '', '', '係長', '佐藤 花子', '2026-04-01', '2027-03-31', '', '見本'],
        ['会議体', '全学委員会', '情報委員会', '', '', '', '委員長', '鈴木 一郎', '', '', '', '見本'],
        ['会議体', '全学委員会', '情報委員会', '', '', '', '事務職員', '佐藤', '', '', '氏名は姓だけでも可（users との照合は完全一致のみ）', '見本'],
        ['分担事務', '総務課', '総務係', '文書の管理に関すること', '', '', '主担当', '佐藤 花子', '', '', '', '見本'],
        ['分担事務', '総務課', '総務係', '文書の管理に関すること', '', '', '副担当', '', '', '', '氏名が空なら空席', '見本'],
    ]
    persons = [
        ['山田 太郎', 'yamada@example.ac.jp', 'regular', '事務職員', '', '', '見本'],
        ['鈴木 一郎', 'suzuki@example.ac.jp', 'regular', '教員', '', '2027-03-31', '見本．終了を過ぎると発行対象から外れる'],
    ]
    wb = _wb_from_rows(units, roles, appts, persons)
    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return send_file(bio, as_attachment=True, download_name='maiguru_ledger_template.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


def _read_sheet(ws, headers, key='種別'):
    """ヘッダ名で列を割り当てて行を dict で返す（列順は問わない）．key は必須列"""
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return [], []
    head = [_s(h) for h in rows[0]]
    idx = {h: head.index(h) for h in headers if h in head}
    missing = [h for h in headers if h not in idx and not h.startswith('階層')]
    if key not in idx:
        raise ValueError(f"シート「{ws.title}」に「{key}」列がありません")
    out = []
    for n, r in enumerate(rows[1:], start=2):
        if r is None or all(_s(v) == '' for v in r):
            continue
        d = {h: (r[i] if i < len(r) else None) for h, i in idx.items()}
        d['_row'] = n
        out.append(d)
    return out, missing


def _path_of(d):
    return [_s(d.get(f'階層{i}')) for i in range(1, MAX_DEPTH + 1) if _s(d.get(f'階層{i}'))]


def parse_workbook(file_obj):
    """アップロードされた xlsx を読み，正規化した3種の行と警告を返す（DBには触れない）"""
    from openpyxl import load_workbook
    wb = load_workbook(file_obj, data_only=True)
    warnings = []
    units, roles, appts = [], [], []

    if SHEET_ROLES in wb.sheetnames:
        rows, _ = _read_sheet(wb[SHEET_ROLES], ROLE_HEADERS)
        for d in rows:
            kind = LABEL_KIND.get(_s(d.get('種別')))
            name = _s(d.get('役割名'))
            if not kind:
                warnings.append(f"役割 {d['_row']}行: 種別「{_s(d.get('種別'))}」が不明（{'／'.join(KIND_LABEL.values())}）")
                continue
            if not name:
                warnings.append(f"役割 {d['_row']}行: 役割名が空")
                continue
            try:
                rank = int(d.get('序列')) if _s(d.get('序列')) else DEFAULT_RANK
            except (TypeError, ValueError):
                rank = DEFAULT_RANK
            roles.append({'kind': kind, 'name': name, 'rank': rank, 'note': _s(d.get('備考'))})

    if SHEET_UNITS in wb.sheetnames:
        rows, _ = _read_sheet(wb[SHEET_UNITS], UNIT_HEADERS)
        for d in rows:
            kind = LABEL_KIND.get(_s(d.get('種別')))
            path = _path_of(d)
            if not kind:
                warnings.append(f"単位 {d['_row']}行: 種別「{_s(d.get('種別'))}」が不明")
                continue
            if not path:
                warnings.append(f"単位 {d['_row']}行: 階層が空")
                continue
            try:
                so = int(d.get('序列')) if _s(d.get('序列')) else None
            except (TypeError, ValueError):
                so = None
            units.append({'kind': kind, 'path': path, 'sort_order': so,
                          'owner_group': _s(d.get('管理グループ')),
                          'valid_from': _d(d.get('開始')), 'valid_until': _d(d.get('終了')),
                          'note': _s(d.get('備考'))})

    persons = []
    if SHEET_PERSONS in wb.sheetnames:
        rows, _ = _read_sheet(wb[SHEET_PERSONS], PERSON_HEADERS, key='氏名')
        seen = set()
        for d in rows:
            fn, em = _s(d.get('氏名')), _s(d.get('メール')).lower()
            if not fn:
                warnings.append(f"人 {d['_row']}行: 氏名が空")
                continue
            if em and '@' not in em:
                warnings.append(f"人 {d['_row']}行: メール「{em}」の形式が不正")
                continue
            if em and em in seen:
                warnings.append(f"人 {d['_row']}行: メール「{em}」が重複（後の行を無視）")
                continue
            if em:
                seen.add(em)
            else:
                warnings.append(f"人 {d['_row']}行: {fn} はメール未記入（名簿には入るが発行されない）")
            cat = _s(d.get('区分')).lower() or 'regular'
            if cat not in PERSON_CATEGORIES:
                warnings.append(f"人 {d['_row']}行: 区分「{cat}」は admin/regular/guest のいずれかにしてください（regular として扱う）")
                cat = 'regular'
            persons.append({'full_name': fn, 'email': em, 'category': cat, 'affiliation': _s(d.get('所属')),
                            'valid_from': _d(d.get('開始')), 'valid_until': _d(d.get('終了')), 'note': _s(d.get('備考'))})

    if SHEET_APPT in wb.sheetnames:
        rows, _ = _read_sheet(wb[SHEET_APPT], APPT_HEADERS)
        for d in rows:
            kind = LABEL_KIND.get(_s(d.get('種別')))
            path = _path_of(d)
            role = _s(d.get('役割'))
            if not kind:
                warnings.append(f"発令 {d['_row']}行: 種別「{_s(d.get('種別'))}」が不明")
                continue
            if not path:
                warnings.append(f"発令 {d['_row']}行: 階層が空")
                continue
            if not role:
                warnings.append(f"発令 {d['_row']}行: 役割が空")
                continue
            names = [n for n in _NAME_SPLIT_RE.split(_s(d.get('氏名'))) if n] or ['']
            for name in names:
                appts.append({'kind': kind, 'path': path, 'role': role, 'person_name': name,
                              'valid_from': _d(d.get('開始')), 'valid_until': _d(d.get('終了')),
                              'note': _s(d.get('備考')), 'source': _s(d.get('出典')), '_row': d['_row']})
    else:
        warnings.append(f"シート「{SHEET_APPT}」がありません")

    return {'units': units, 'roles': roles, 'appts': appts, 'persons': persons, 'warnings': warnings}


@user_groups_bp.route('/api/ledger/import', methods=['POST'])
@login_required
def ledger_import():
    """
    multipart: file=xlsx, mode=check|apply, replace_source=0|1
    check  … DB に触れず，何が起きるかを数えて返す
    apply  … 単位・役割は upsert，発令は追加（replace_source=1 なら同じ出典の既存発令を先に消す）
    """
    if not _is_admin():
        return _deny()
    f = request.files.get('file')
    mode = request.form.get('mode', 'check')
    replace_source = request.form.get('replace_source', '1') == '1'
    if not f:
        return jsonify({'success': False, 'error': 'ファイルがありません'}), 400
    try:
        parsed = parse_workbook(f.stream)
    except Exception as e:
        return jsonify({'success': False, 'error': f'読み取り失敗: {e}'}), 400

    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        # admin の扱い：外から来た admin は，このサイトの現 admin とメールが一致しない限り regular にする
        cursor.execute("SELECT email FROM users WHERE category='admin' AND is_active=TRUE AND deleted_at IS NULL")
        local_admins = {(r['email'] or '').strip().lower() for r in cursor.fetchall() if r['email']}
        admin_kept, admin_demoted = [], []
        for x in parsed['persons']:
            if x['category'] != 'admin':
                continue
            label = f"{x['full_name']}（{x['email'] or 'メール未記入'}）"
            if x['email'] and x['email'] in local_admins:
                admin_kept.append(label)
            else:
                x['category'] = 'regular'
                admin_demoted.append(label)
        if admin_demoted:
            parsed['warnings'].append(
                f"区分 admin の {len(admin_demoted)} 人は，このサイトの admin と一致しないので regular として取り込みます："
                + '，'.join(admin_demoted))

        # 役割の突合
        cursor.execute("SELECT id, kind, name FROM ug_roles")
        role_db = {(r['kind'], r['name']): r['id'] for r in cursor.fetchall()}
        role_sheet = {(r['kind'], r['name']) for r in parsed['roles']}
        role_auto = sorted({(a['kind'], a['role']) for a in parsed['appts']
                            if (a['kind'], a['role']) not in role_db and (a['kind'], a['role']) not in role_sheet})

        # 出典ごとの既存件数
        sources = sorted({a['source'] for a in parsed['appts']})
        src_existing = {}
        for s in sources:
            cursor.execute("SELECT COUNT(*) AS n FROM ug_appointments WHERE source <=> %s", (s or None,))
            src_existing[s] = cursor.fetchone()['n']

        # 人の解決
        rs = NameResolver(cursor)
        resolved = unresolved = vacant = 0
        rs.add_persons([(x['full_name'], x['email']) for x in parsed['persons']])
        by = {'exact': 0, 'alias': 0, 'person': 0, 'ignore': 0, 'post': 0}
        for a in parsed['appts']:
            if not a['person_name']:
                vacant += 1
                a['user_id'] = None
                continue
            uid, how = rs.resolve(a['person_name'])
            a['user_id'] = uid
            if how:
                by[how] += 1
            if uid:
                resolved += 1
            elif how not in ('ignore', 'post'):
                unresolved += 1

        summary = {
            'units': len(parsed['units']),
            'roles': len(parsed['roles']),
            'persons': len(parsed['persons']),
            'appointments': len(parsed['appts']),
            'roles_auto_created': [f"{KIND_LABEL[k]}：{n}" for k, n in role_auto],
            'sources': [{'source': s, 'new': sum(1 for a in parsed['appts'] if a['source'] == s),
                         'existing': src_existing[s]} for s in sources],
            'resolved': resolved, 'unresolved': unresolved, 'vacant': vacant, 'resolved_by': by,
            'warnings': parsed['warnings'],
            'unresolved_names': sorted({a['person_name'] for a in parsed['appts']
                                        if a['person_name'] and not a['user_id']
                                        and rs.resolve(a['person_name'])[1] not in ('ignore', 'post')})[:30],
            'replace_source': replace_source,
            'admin_kept': admin_kept, 'admin_demoted': admin_demoted,
        }
        if mode != 'apply':
            return jsonify({'success': True, 'mode': 'check', 'summary': summary})

        # ── 適用 ──
        cursor.execute("SELECT id, full_name, email FROM ug_persons")
        _rows = cursor.fetchall()
        blank_by_name = {}                   # 正規化した氏名 → メール未記入行の id（重複があれば全部）
        names_any = set()
        for r in _rows:
            names_any.add(_norm_name(r['full_name']))
            if not r['email']:
                blank_by_name.setdefault(_norm_name(r['full_name']), []).append(r['id'])
        for x in parsed['persons']:
            if x['email']:
                # メールが付いた人：同名でメール未記入の行があればそれを埋める（未記入→記入への昇格）
                for bid in blank_by_name.pop(_norm_name(x['full_name']), []):
                    cursor.execute("DELETE FROM ug_persons WHERE id=%s", (bid,))
                cursor.execute("""INSERT INTO ug_persons (full_name, email, category, affiliation, valid_from, valid_until, note)
                                  VALUES (%s,%s,%s,%s,%s,%s,%s)
                                  ON DUPLICATE KEY UPDATE full_name=VALUES(full_name), category=VALUES(category),
                                    affiliation=VALUES(affiliation), valid_from=VALUES(valid_from),
                                    valid_until=VALUES(valid_until), note=VALUES(note)""",
                               (x['full_name'], x['email'], x['category'], x['affiliation'] or None,
                                x['valid_from'], x['valid_until'], x['note'] or None))
            else:
                k = _norm_name(x['full_name'])
                if k in names_any:
                    continue            # 同名の人が既にいる（メール付き／未記入とも）なら触らない
                names_any.add(k)
                cursor.execute("""INSERT INTO ug_persons (full_name, email, category, affiliation, valid_from, valid_until, note)
                                  VALUES (%s,NULL,%s,%s,%s,%s,%s)""",
                               (x['full_name'], x['category'], x['affiliation'] or None,
                                x['valid_from'], x['valid_until'], x['note'] or None))
        for x in parsed['persons']:
            if x['email']:
                _drop_ignore_alias(cursor, x['full_name'])
        if parsed['persons']:
            rs = NameResolver(cursor)           # 名簿を反映した解決器で引き直す
            for a in parsed['appts']:
                if a['person_name'] and not a['user_id']:
                    a['user_id'] = rs.resolve(a['person_name'])[0]
        created_units = []
        for r in parsed['roles']:
            key = (r['kind'], r['name'])
            if key in role_db:
                cursor.execute("UPDATE ug_roles SET `rank`=%s, note=%s WHERE id=%s",
                               (r['rank'], r['note'] or None, role_db[key]))
            else:
                cursor.execute("INSERT INTO ug_roles (kind, name, `rank`, note) VALUES (%s,%s,%s,%s)",
                               (r['kind'], r['name'], r['rank'], r['note'] or None))
                role_db[key] = cursor.lastrowid
        for k, n in role_auto:
            cursor.execute("INSERT INTO ug_roles (kind, name, `rank`) VALUES (%s,%s,%s)", (k, n, DEFAULT_RANK))
            role_db[(k, n)] = cursor.lastrowid

        cursor.execute("SELECT id, name FROM user_groups")
        gid_by_name = {r['name']: r['id'] for r in cursor.fetchall()}
        for u in parsed['units']:
            uid = _ensure_unit_path(cursor, u['kind'], u['path'], created_units)
            sets, vals = [], []
            if u['sort_order'] is not None:
                sets.append("sort_order=%s"); vals.append(u['sort_order'])
            if u['owner_group']:
                if u['owner_group'] in gid_by_name:
                    sets.append("owner_group_id=%s"); vals.append(gid_by_name[u['owner_group']])
                else:
                    summary['warnings'].append(f"単位「{'／'.join(u['path'])}」の管理グループ「{u['owner_group']}」は未登録のため無視")
            for col in ('valid_from', 'valid_until'):
                sets.append(f"{col}=%s"); vals.append(u[col])
            sets.append("note=%s"); vals.append(u['note'] or None)
            cursor.execute(f"UPDATE ug_units SET {', '.join(sets)} WHERE id=%s", tuple(vals) + (uid,))

        deleted = 0
        if replace_source:
            for s in sources:
                cursor.execute("DELETE FROM ug_appointments WHERE source <=> %s", (s or None,))
                deleted += cursor.rowcount

        me = session.get('user_id')
        for a in parsed['appts']:
            uid = _ensure_unit_path(cursor, a['kind'], a['path'], created_units)
            rid = role_db[(a['kind'], a['role'])]
            cursor.execute("""INSERT INTO ug_appointments
                              (unit_id, role_id, user_id, person_name, valid_from, valid_until, note, source, created_by)
                              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                           (uid, rid, a['user_id'], a['person_name'] or None,
                            a['valid_from'], a['valid_until'], a['note'] or None, a['source'] or None, me))
        conn.commit()
        summary['units_created'] = created_units
        summary['appointments_deleted'] = deleted
        return jsonify({'success': True, 'mode': 'apply', 'summary': summary})
    except Exception as e:
        conn.rollback()
        logging.exception("ledger import failed")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()
