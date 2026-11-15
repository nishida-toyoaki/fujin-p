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
user_groups/ledger.py — ユーザとグループ（台帳の画面とユーザ管理）

画面
  /ledger  組織情報管理（admin・グループ管理統括）．ツリーの API は tree.py
  /users   ユーザ管理（admin）：名簿・名寄せ・登録申請とブラックリスト
  /system  管理（admin）：永続IDの起点とサイト移行（migrate.py）・対応付けの例外管理（tree.py）・統括の任命（stewards.py）

このモジュールの API
  ノードの編集（PUT /api/ledger/units/<id>），立場の追加，メンバーシップ（発令）の追加・変更・削除，
  年度の引き継ぎ，ユーザ検索，名寄せ，名簿，登録申請とブラックリスト

テーブル（すべて default DB）
  ug_units          ノード（組織・会議体の木と，種別 group のシステム用グループの単位）
  ug_roles          立場の語彙
  ug_appointments   メンバーシップ（ノード×ユーザ×立場×期間）
  ug_person_aliases 名寄せの対象外リスト（user_id NULL の行＝この氏名は誰にも紐づけない）
  ug_group_rules    グループの導出ルール（ノード×立場 → user_groups の構成員）
  ug_persons        アカウント名簿（氏名・メール・区分・所属・期間）．users に直ちに反映する
  （カーネル表 users / approved_users / registration_requests を読み書きする）
"""
import re
import logging
from functools import wraps
from datetime import datetime, timedelta, timezone

from flask import render_template, jsonify, request, session

from decorators import login_required
from . import user_groups_bp
from .routes import get_db, check_is_total_admin, parse_input

JST = timezone(timedelta(hours=9), 'JST')

# 単位の種別（骨格）．表示名は kind ラベル．
KINDS = [('org', '組織'), ('body', '会議体'), ('duty', '分担事務'), ('group', 'グループ')]
KIND_LABEL = dict(KINDS)

DEFAULT_RANK = 99       # 未登録の役割を自動作成するときの序列

PERSON_CATEGORIES = ('admin', 'regular', 'guest')

# 氏名欄の区切り（複数人を1セルに書いたとき）


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


def _valid_at(valid_from, valid_until, at):
    if at is None:
        return True
    if valid_from and valid_from > at:
        return False
    if valid_until and valid_until < at:
        return False
    return True


def _has_order(cursor):
    from .stewards import ensure_order_column
    return ensure_order_column(cursor)


def _norm_name(s):
    """氏名照合用の正規化（空白・全角空白・括弧を除去．「佐藤（充）」→「佐藤充」）"""
    return re.sub(r'[\s\u3000（）()]+', '', s or '')


def _is_admin():
    return check_is_total_admin(session.get('user_id'))


def _deny():
    return jsonify({'success': False, 'error': '権限がありません（台帳の操作は総管理者のみ）'}), 403


def _can_define_groups():
    """台帳から導くグループの定義（グループ定義）は admin とグループ管理統括"""
    if _is_admin():
        return True
    try:
        from .stewards import who
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        try:
            return who(cursor, session.get('user_id'))['can_steward']
        finally:
            cursor.close()
            conn.close()
    except Exception as e:
        logging.warning("steward check failed: %s", e)
        return False


def org_only(view):
    """組織情報管理（台帳）の操作：admin とグループ管理統括"""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not _can_define_groups():
            return _deny()
        return view(*args, **kwargs)
    return wrapped


def _reserved_unit_guard(cursor, unit_ids):
    """予約単位（グループ管理統括）の単位・発令は admin だけが触れる．触れてはいけなければエラー文を返す"""
    if _is_admin() or not unit_ids:
        return None
    from .stewards import SUPERVISOR_NAME
    marks = ','.join(['%s'] * len(unit_ids))
    cursor.execute(f"SELECT COUNT(*) AS n FROM ug_units WHERE id IN ({marks}) AND kind='group' AND name=%s",
                   tuple(unit_ids) + (SUPERVISOR_NAME,))
    if cursor.fetchone()['n']:
        return 'グループ管理統括の任命・解任は admin が「管理」で行います'
    return None


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

# ────────────────────────────────────────────
# 画面
# ────────────────────────────────────────────

@user_groups_bp.route('/ledger')
@login_required
def ledger_page():
    if not _can_define_groups():
        from flask import url_for
        return DENY_PAGE.format(back=url_for('user_groups.return_to_fujin')), 403
    from .stewards import nav_context
    return render_template('ledger.html', page_title='組織情報管理', nav=nav_context('org'),
                           kinds=KINDS,
                           is_admin=True,          # 台帳の編集ボタンを出す（admin と統括）
                           real_admin=_is_admin(),
                           today=_now().strftime('%Y-%m-%d'))


@user_groups_bp.route('/users')
@login_required
def users_page():
    """ユーザ管理（admin）：名簿・名寄せ・登録申請とブラックリスト"""
    if not _is_admin():
        from flask import url_for
        return DENY_PAGE.format(back=url_for('user_groups.return_to_fujin')), 403
    from .stewards import nav_context
    return render_template('users.html', page_title='ユーザ管理', is_admin=True, nav=nav_context('users'),
                           today=_now().strftime('%Y-%m-%d'))


@user_groups_bp.route('/system')
@login_required
def system_page():
    """管理（admin）：永続IDの起点とサイト移行・ノードとシステムグループの対応付けの例外管理・グループ管理統括の任命"""
    if not _is_admin():
        from flask import url_for
        return DENY_PAGE.format(back=url_for('user_groups.return_to_fujin')), 403
    from .stewards import nav_context
    return render_template('system.html', page_title='管理', is_admin=True, nav=nav_context('system'),
                           today=_now().strftime('%Y-%m-%d'))


# ────────────────────────────────────────────
# 大表（グリッド）
# ────────────────────────────────────────────

# ────────────────────────────────────────────
# 単位・役割・発令の CRUD（総管理者）
# ────────────────────────────────────────────

@user_groups_bp.route('/api/ledger/units/<int:unit_id>', methods=['PUT'])
@login_required
def ledger_unit_update(unit_id):
    if not _can_define_groups():
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
    cursor = conn.cursor(dictionary=True)
    try:
        err = _reserved_unit_guard(cursor, [unit_id])
        if err:
            return jsonify({'success': False, 'error': err}), 403
        cursor.execute(f"UPDATE ug_units SET {', '.join(fields)} WHERE id=%s", tuple(vals) + (unit_id,))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/api/ledger/roles', methods=['POST'])
@login_required
def ledger_role_create():
    if not _can_define_groups():
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
@org_only
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
        _is_admin_cached = _is_admin()
        items = []
        for r in cursor.fetchall():
            adm = _is_admin_cached
            hit = (q.isdigit() and r['id'] == int(q)) or (key and key in _norm_name(r['full_name']).lower()) or (adm and key and key in (r['email'] or '').lower())
            if hit:
                items.append({'id': r['id'], 'full_name': r['full_name'] or '', 'email': (r['email'] or '') if adm else '',
                              'category': r['category'], 'inactive': not r['is_active']})
        items.sort(key=lambda x: (not (q.isdigit() and x['id'] == int(q)), x['inactive'], x['full_name']))
        return jsonify({'success': True, 'items': items[:12], 'more': max(0, len(items) - 12)})
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/api/ledger/appointments', methods=['POST'])
@login_required
def ledger_appt_create():
    if not _can_define_groups():
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
        err = _reserved_unit_guard(cursor, [unit_id])
        if err:
            return jsonify({'success': False, 'error': err}), 403
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
    if not _can_define_groups():
        return _deny()
    data = request.get_json(silent=True) or {}
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT unit_id FROM ug_appointments WHERE id=%s", (appt_id,))
        row = cursor.fetchone()
        err = _reserved_unit_guard(cursor, [row['unit_id']] + ([data['unit_id']] if data.get('unit_id') else [])) if row else None
        if err:
            return jsonify({'success': False, 'error': err}), 403
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
    if not _can_define_groups():
        return _deny()
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT unit_id FROM ug_appointments WHERE id=%s", (appt_id,))
        row = cursor.fetchone()
        err = _reserved_unit_guard(cursor, [row['unit_id']]) if row else None
        if err:
            return jsonify({'success': False, 'error': err}), 403
        cursor.execute("DELETE FROM ug_appointments WHERE id=%s", (appt_id,))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


# ────────────────────────────────────────────
# 年度の引き継ぎ（年度ごとに区切った発令を翌年度へ写す）
# ────────────────────────────────────────────

@user_groups_bp.route('/api/ledger/carryover', methods=['POST'])
@login_required
@org_only
def ledger_carryover():
    """
    {kind, from_fy, to_fy, mode: check|apply}
    from_fy 年度の末日（3月31日）で終わる発令を，to_fy 年度（4月1日〜3月31日）の発令として写す．
    期限のない発令はそのまま続くので写さない．写し先の年度に同じ単位・役割・人の発令が既にあれば飛ばす．
    単位がその年度より前に閉じていれば飛ばす．
    """
    data = request.get_json(silent=True) or {}
    kind = data.get('kind')
    try:
        fy0, fy1 = int(data.get('from_fy')), int(data.get('to_fy'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': '年度を数で指定してください'}), 400
    kinds = ['org', 'body'] if kind == 'tree' else [kind]
    if any(k not in KIND_LABEL for k in kinds) or fy1 <= fy0:
        return jsonify({'success': False, 'error': '種別と年度（写し先は写し元より後）を指定してください'}), 400
    apply_ = data.get('mode') == 'apply'
    src_end = datetime(fy0 + 1, 3, 31)
    t0, t1 = datetime(fy1, 4, 1), datetime(fy1 + 1, 3, 31)
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        has_order = _has_order(cursor)
        cursor.execute(f"""
            SELECT a.id, a.unit_id, a.role_id, a.user_id, a.person_name, a.valid_from, a.valid_until, a.note,
                   {'a.sort_order' if has_order else 'NULL AS sort_order'},
                   u.name AS unit_name, u.valid_until AS unit_until, r.name AS role_name
            FROM ug_appointments a JOIN ug_units u ON u.id=a.unit_id JOIN ug_roles r ON r.id=a.role_id
            WHERE u.kind IN ({','.join(['%s'] * len(kinds))})""", tuple(kinds))
        rows = cursor.fetchall()
        def key(a):
            return (a['unit_id'], a['role_id'], a['user_id'] or ('n:' + _norm_name(a['person_name'])))
        in_target = {key(a) for a in rows
                     if (a['valid_from'] is None or a['valid_from'] <= t1) and (a['valid_until'] is None or a['valid_until'] >= t0)}
        open_ended = sum(1 for a in rows if a['valid_until'] is None and (a['valid_from'] is None or a['valid_from'] <= t1))
        copy, skipped_exist, skipped_closed = [], 0, 0
        for a in rows:
            if not a['valid_until'] or a['valid_until'].date() != src_end.date():
                continue
            if a['unit_until'] and a['unit_until'] < t0:
                skipped_closed += 1
                continue
            if key(a) in in_target:
                skipped_exist += 1
                continue
            copy.append(a)
        source = f"{fy1}年度（{fy0}年度から引継ぎ）"
        if apply_:
            me = session.get('user_id')
            for a in copy:
                cursor.execute("""INSERT INTO ug_appointments
                                  (unit_id, role_id, user_id, person_name, valid_from, valid_until, note, source, created_by)
                                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                               (a['unit_id'], a['role_id'], a['user_id'], a['person_name'], t0, t1, a['note'], source, me))
                if has_order and a['sort_order'] is not None:
                    cursor.execute("UPDATE ug_appointments SET sort_order=%s WHERE id=%s", (a['sort_order'], cursor.lastrowid))
            conn.commit()
        sample = [f"{a['unit_name']}／{a['role_name']}：{a['person_name'] or ''}" for a in copy[:15]]
        return jsonify({'success': True, 'mode': 'apply' if apply_ else 'check', 'copied': len(copy),
                        'skipped_existing': skipped_exist, 'skipped_closed': skipped_closed,
                        'open_ended': open_ended, 'source': source, 'sample': sample})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


# ────────────────────────────────────────────
# グループの導出ルール
# ────────────────────────────────────────────

# 会議体で「委員の側」に当たる役割（残りは事務局側とみなす）
BODY_STAFF_ROLES = ('事務職員',)
# 組織で「教員」に当たる役割


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

    # 種別グループの単位はグループ管理（stewards.py）が改廃のたびに user_groups を揃える

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
        # 先に取り込んで控えてあった永続IDを，できた users に付ける（複製：ユーザ→永続ID→台帳の順でも，逆でもよい）
        res['pids_linked'] = {'person': 0, 'group': 0}
        try:
            from .pids import link_pending
            cursor.execute("SAVEPOINT before_pids")
            res['pids_linked'] = link_pending(cursor)
        except Exception as e:
            logging.warning("link_pending skipped: %s", e)
            try:
                cursor.execute("ROLLBACK TO SAVEPOINT before_pids")
            except Exception:
                pass
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
