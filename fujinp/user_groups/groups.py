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
グループ管理（2026-09-24，まいぐるの画面から「ユーザとグループ」へ移設）

アドホックなグループの入口．user_groups と user_group_memberships を直接扱う．
台帳（組織・会議体・分担事務）から導かれるグループはルールで構成員が決まるので，
ここでは改名・削除を受け付けず，直接メンバーの追加だけを許す．

権限
  総管理者（users.category='admin'）……すべて
  作成権限者（user_group_global_managers，期間内）……グループの新規作成
  グループの管理者（user_groups.manager_user_id）……そのグループの改名・説明・管理者交代・構成員・削除
  ログインユーザ……一覧と構成員の閲覧
グループの同定は名前で行う（サイト間の移行を名前で突き合わせるため）．名前の重複は作成・改名時に弾く．
"""
from datetime import datetime

from flask import jsonify, request, session

from decorators import login_required
from . import user_groups_bp
from .routes import get_db, check_is_total_admin, check_is_global_manager, parse_input, get_now_jst

API = '/api/gm'


def _now():
    return get_now_jst().replace(tzinfo=None)


def _dstr(dt):
    if not dt:
        return ''
    if isinstance(dt, datetime) and (dt.hour or dt.minute or dt.second):
        return dt.strftime('%Y-%m-%d %H:%M')
    return dt.strftime('%Y-%m-%d')


def _valid_at(vf, vu, at):
    if vf and vf > at:
        return False
    if vu and vu < at:
        return False
    return True


def _state(vf, vu, at):
    if vf and vf > at:
        return 'future'
    if vu and vu < at:
        return 'past'
    return 'current'


def _as_of():
    s = (request.args.get('as_of') or '').strip()
    if s and s != 'today':
        d = parse_input(s)
        if d:
            return d          # 台帳と同じく日付だけなら 0:00 で判定する
    return _now()


def _err(msg, code=400):
    return jsonify({'success': False, 'error': msg}), code


def _me():
    return session.get('user_id')


def _can_edit(group, me, is_admin):
    return bool(group) and (is_admin or group['manager_user_id'] == me)


def _has_table(cursor, name):
    cursor.execute("SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = %s", (name,))
    return cursor.fetchone()['n'] > 0


def _rule_group_ids(cursor):
    """台帳から導かれるグループ（ルールを持つ）の id"""
    if not _has_table(cursor, 'ug_group_rules'):
        return set()
    cursor.execute("SELECT DISTINCT group_id FROM ug_group_rules")
    return {r['group_id'] for r in cursor.fetchall()}


def _reserved_names(cursor):
    """台帳が使う（使う予定の）グループ名．アドホックなグループには付けさせない"""
    try:
        from .ledger import _proposals
        return {p['group_name'] for p in _proposals(cursor)}
    except Exception:
        return set()


def _name_problem(cursor, name, self_id=None):
    if not name:
        return 'グループ名は必須です'
    cursor.execute("SELECT id FROM user_groups WHERE name = %s", (name,))
    if any(r['id'] != self_id for r in cursor.fetchall()):
        return f'「{name}」は既に使われています'
    if name in _reserved_names(cursor):
        return f'「{name}」は台帳から導かれるグループの名前です．別の名前にしてください'
    return None


def _members(cursor, gid, at):
    from .utils import _group_member_ids
    return _group_member_ids(cursor, gid, at)


def _user_names(cursor, ids):
    ids = [i for i in ids if i]
    if not ids:
        return {}
    fmt = ','.join(['%s'] * len(ids))
    cursor.execute(f"SELECT id, full_name, deleted_at, is_active FROM users WHERE id IN ({fmt})", tuple(ids))
    return {r['id']: (r['full_name'] or f"#{r['id']}") + ('（削除済）' if r['deleted_at'] else '（停止中）' if not r['is_active'] else '')
            for r in cursor.fetchall()}


# ────────────────────────────────────────────
# 一覧・詳細
# ────────────────────────────────────────────

@user_groups_bp.route(f'{API}/groups')
@login_required
def gm_groups():
    me = _me()
    is_admin = check_is_total_admin(me)
    at = _as_of()
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        ruled = _rule_group_ids(cursor)
        cursor.execute("SELECT id, name, description, manager_user_id FROM user_groups ORDER BY name")
        groups = cursor.fetchall()
        cursor.execute("SELECT group_id, user_id, valid_from, valid_until FROM user_group_memberships")
        direct = {}
        for r in cursor.fetchall():
            if _valid_at(r['valid_from'], r['valid_until'], at):
                direct.setdefault(r['group_id'], set()).add(r['user_id'])
        names = _user_names(cursor, list({g['manager_user_id'] for g in groups}))
        items = []
        for g in groups:
            mem = _members(cursor, g['id'], at) if g['id'] in ruled else direct.get(g['id'], set())
            items.append({
                'id': g['id'], 'name': g['name'], 'description': g['description'] or '',
                'manager_id': g['manager_user_id'], 'manager': names.get(g['manager_user_id'], ''),
                'ledger': g['id'] in ruled, 'count': len(mem),
                'mine': g['manager_user_id'] == me, 'member': me in mem,
                'can_edit': _can_edit(g, me, is_admin),
            })
        return jsonify({'success': True, 'items': items, 'is_admin': is_admin,
                        'can_create': is_admin or check_is_global_manager(me), 'me': me})
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route(f'{API}/groups/<int:gid>')
@login_required
def gm_group(gid):
    me = _me()
    is_admin = check_is_total_admin(me)
    at = _as_of()
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id, name, description, manager_user_id FROM user_groups WHERE id = %s", (gid,))
        g = cursor.fetchone()
        if not g:
            return _err('グループが見つかりません', 404)
        cursor.execute("""SELECT id, user_id, valid_from, valid_until FROM user_group_memberships
                          WHERE group_id = %s ORDER BY valid_from IS NULL DESC, valid_from, id""", (gid,))
        rows = cursor.fetchall()
        effective = _members(cursor, gid, at)
        direct_now = {r['user_id'] for r in rows if _valid_at(r['valid_from'], r['valid_until'], at)}
        rules = []
        if gid in _rule_group_ids(cursor):
            from .ledger import _rules_of, _unit_path, KIND_LABEL
            rules = [{'unit': '／'.join(_unit_path(cursor, r['unit_id'])), 'kind': KIND_LABEL.get(r['kind'], r['kind']),
                      'role': r['role_name'] or '（全役割）', 'recurse': bool(r['recurse']), 'mode': r['mode']}
                     for r in _rules_of(cursor, gid)]
        derived = effective - direct_now
        dropped = direct_now - effective
        names = _user_names(cursor, [r['user_id'] for r in rows] + list(derived) + [g['manager_user_id']])
        return jsonify({'success': True, 'as_of': _dstr(at),
                        'group': {'id': g['id'], 'name': g['name'], 'description': g['description'] or '',
                                  'manager_id': g['manager_user_id'], 'manager': names.get(g['manager_user_id'], ''),
                                  'ledger': bool(rules)},
                        'can_edit': _can_edit(g, me, is_admin), 'is_admin': is_admin,
                        'members': [{'id': r['id'], 'user_id': r['user_id'], 'name': names.get(r['user_id'], f"#{r['user_id']}"),
                                     'from': _dstr(r['valid_from']), 'until': _dstr(r['valid_until']),
                                     'state': _state(r['valid_from'], r['valid_until'], at),
                                     'dropped': r['user_id'] in dropped} for r in rows],
                        'rules': rules,
                        'derived': sorted(names.get(u, f'#{u}') for u in derived),
                        'count': len(effective)})
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route(f'{API}/users')
@login_required
def gm_users():
    """構成員・管理者を選ぶための検索（ID完全一致・氏名部分一致．メールは総管理者だけ）"""
    q = (request.args.get('q') or '').strip()
    if not q:
        return jsonify({'success': True, 'items': []})
    is_admin = check_is_total_admin(_me())
    from .ledger import _norm_name
    key = _norm_name(q).lower()
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id, full_name, email, is_active FROM users WHERE deleted_at IS NULL")
        items = []
        for r in cursor.fetchall():
            if not is_admin and not r['is_active']:
                continue
            hit = (q.isdigit() and r['id'] == int(q)) or (key and key in _norm_name(r['full_name']).lower()) \
                or (is_admin and key and key in (r['email'] or '').lower())
            if hit:
                items.append({'id': r['id'], 'full_name': r['full_name'] or '', 'email': (r['email'] or '') if is_admin else '',
                              'inactive': not r['is_active']})
        items.sort(key=lambda x: (not (q.isdigit() and x['id'] == int(q)), x['inactive'], x['full_name']))
        return jsonify({'success': True, 'items': items[:12], 'more': max(0, len(items) - 12)})
    finally:
        cursor.close()
        conn.close()


# ────────────────────────────────────────────
# グループの作成・変更・削除
# ────────────────────────────────────────────

@user_groups_bp.route(f'{API}/groups', methods=['POST'])
@login_required
def gm_group_create():
    me = _me()
    if not (check_is_total_admin(me) or check_is_global_manager(me)):
        return _err('グループを作る権限がありません', 403)
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        p = _name_problem(cursor, name)
        if p:
            return _err(p)
        manager = int(data.get('manager_user_id') or me)
        now = _now()
        cursor.execute("""INSERT INTO user_groups (name, description, manager_user_id, created_at, updated_at)
                          VALUES (%s,%s,%s,%s,%s)""", (name, (data.get('description') or '').strip() or None, manager, now, now))
        gid = cursor.lastrowid
        if data.get('manager_is_member'):
            cursor.execute("INSERT INTO user_group_memberships (group_id, user_id, valid_from) VALUES (%s,%s,%s)",
                           (gid, manager, now.replace(hour=0, minute=0, second=0)))
        conn.commit()
        return jsonify({'success': True, 'id': gid})
    except Exception as e:
        conn.rollback()
        return _err(str(e), 500)
    finally:
        cursor.close()
        conn.close()


def _load_for_edit(cursor, gid):
    cursor.execute("SELECT id, name, description, manager_user_id FROM user_groups WHERE id = %s", (gid,))
    g = cursor.fetchone()
    me = _me()
    if not g:
        return None, _err('グループが見つかりません', 404)
    if not _can_edit(g, me, check_is_total_admin(me)):
        return None, _err('このグループを編集する権限がありません（グループの管理者か総管理者のみ）', 403)
    return g, None


@user_groups_bp.route(f'{API}/groups/<int:gid>', methods=['PUT'])
@login_required
def gm_group_update(gid):
    data = request.get_json(silent=True) or {}
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        g, err = _load_for_edit(cursor, gid)
        if err:
            return err
        sets, params = [], []
        if 'name' in data:
            name = (data.get('name') or '').strip()
            if name != g['name']:
                if gid in _rule_group_ids(cursor):
                    return _err('台帳から導かれるグループは改名できません（台帳の単位名を変えてください）')
                p = _name_problem(cursor, name, self_id=gid)
                if p:
                    return _err(p)
                sets.append('name = %s'); params.append(name)
        if 'description' in data:
            sets.append('description = %s'); params.append((data.get('description') or '').strip() or None)
        if data.get('manager_user_id'):
            cursor.execute("SELECT id FROM users WHERE id = %s AND deleted_at IS NULL", (int(data['manager_user_id']),))
            if not cursor.fetchone():
                return _err('管理者に指定したユーザが見つかりません')
            sets.append('manager_user_id = %s'); params.append(int(data['manager_user_id']))
        if not sets:
            return jsonify({'success': True})
        sets.append('updated_at = %s'); params.append(_now())
        cursor.execute(f"UPDATE user_groups SET {', '.join(sets)} WHERE id = %s", tuple(params) + (gid,))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return _err(str(e), 500)
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route(f'{API}/groups/<int:gid>', methods=['DELETE'])
@login_required
def gm_group_delete(gid):
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        g, err = _load_for_edit(cursor, gid)
        if err:
            return err
        if gid in _rule_group_ids(cursor):
            return _err('台帳から導かれるグループは削除できません（台帳の単位を閉じてください）')
        cursor.execute("DELETE FROM user_group_memberships WHERE group_id = %s", (gid,))
        if _has_table(cursor, 'user_group_subgroups'):
            cursor.execute("DELETE FROM user_group_subgroups WHERE parent_group_id = %s OR child_group_id = %s", (gid, gid))
        cursor.execute("DELETE FROM user_groups WHERE id = %s", (gid,))
        conn.commit()
        return jsonify({'success': True, 'deleted': g['name']})
    except Exception as e:
        conn.rollback()
        return _err(str(e), 500)
    finally:
        cursor.close()
        conn.close()


# ────────────────────────────────────────────
# 構成員（直接メンバー）
# ────────────────────────────────────────────

def _overlap(a_from, a_until, b_from, b_until):
    lo = max([d for d in (a_from, b_from) if d], default=None)
    hi = min([d for d in (a_until, b_until) if d], default=None)
    return lo is None or hi is None or lo <= hi


@user_groups_bp.route(f'{API}/groups/<int:gid>/members', methods=['POST'])
@login_required
def gm_member_add(gid):
    data = request.get_json(silent=True) or {}
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        g, err = _load_for_edit(cursor, gid)
        if err:
            return err
        try:
            uids = [int(u) for u in (data.get('user_ids') or [data.get('user_id')]) if u]
        except (TypeError, ValueError):
            return _err('ユーザの指定が不正です')
        if not uids:
            return _err('ユーザを選んでください')
        vf, vu = parse_input(data.get('valid_from')), parse_input(data.get('valid_until'))
        if vf and vu and vf > vu:
            return _err('開始が終了より後になっています')
        added, skipped = 0, []
        for uid in uids:
            cursor.execute("SELECT valid_from, valid_until FROM user_group_memberships WHERE group_id = %s AND user_id = %s", (gid, uid))
            if any(_overlap(vf, vu, r['valid_from'], r['valid_until']) for r in cursor.fetchall()):
                skipped.append(uid)
                continue
            cursor.execute("INSERT INTO user_group_memberships (group_id, user_id, valid_from, valid_until) VALUES (%s,%s,%s,%s)",
                           (gid, uid, vf, vu))
            added += 1
        conn.commit()
        res = {'success': True, 'added': added}
        if skipped:
            res['skipped'] = list(_user_names(cursor, skipped).values())
        return jsonify(res)
    except Exception as e:
        conn.rollback()
        return _err(str(e), 500)
    finally:
        cursor.close()
        conn.close()


def _membership_for_edit(cursor, mid):
    cursor.execute("SELECT id, group_id, user_id, valid_from, valid_until FROM user_group_memberships WHERE id = %s", (mid,))
    m = cursor.fetchone()
    if not m:
        return None, _err('構成員の行が見つかりません', 404)
    _g, err = _load_for_edit(cursor, m['group_id'])
    return (None, err) if err else (m, None)


@user_groups_bp.route(f'{API}/members/<int:mid>', methods=['PUT'])
@login_required
def gm_member_update(mid):
    data = request.get_json(silent=True) or {}
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        m, err = _membership_for_edit(cursor, mid)
        if err:
            return err
        if data.get('end_now'):
            vf, vu = m['valid_from'], _now()
        else:
            vf, vu = parse_input(data.get('valid_from')), parse_input(data.get('valid_until'))
        if vf and vu and vf > vu:
            return _err('開始が終了より後になっています')
        cursor.execute("SELECT id, valid_from, valid_until FROM user_group_memberships WHERE group_id = %s AND user_id = %s AND id <> %s",
                       (m['group_id'], m['user_id'], mid))
        if any(_overlap(vf, vu, r['valid_from'], r['valid_until']) for r in cursor.fetchall()):
            return _err('同じ人の別の行と期間が重なります')
        cursor.execute("UPDATE user_group_memberships SET valid_from = %s, valid_until = %s WHERE id = %s", (vf, vu, mid))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return _err(str(e), 500)
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route(f'{API}/members/<int:mid>', methods=['DELETE'])
@login_required
def gm_member_delete(mid):
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        m, err = _membership_for_edit(cursor, mid)
        if err:
            return err
        cursor.execute("DELETE FROM user_group_memberships WHERE id = %s", (mid,))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return _err(str(e), 500)
    finally:
        cursor.close()
        conn.close()


# ────────────────────────────────────────────
# 作成権限者（総管理者のみ）
# ────────────────────────────────────────────

@user_groups_bp.route(f'{API}/creators')
@login_required
def gm_creators():
    if not check_is_total_admin(_me()):
        return _err('総管理者のみ', 403)
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""SELECT gm.id, gm.user_id, u.full_name, gm.valid_from, gm.valid_until
                          FROM user_group_global_managers gm LEFT JOIN users u ON u.id = gm.user_id ORDER BY u.full_name""")
        at = _now()
        items = [{'id': r['id'], 'user_id': r['user_id'], 'name': r['full_name'] or f"#{r['user_id']}",
                  'from': _dstr(r['valid_from']), 'until': _dstr(r['valid_until']),
                  'state': _state(r['valid_from'], r['valid_until'], at)} for r in cursor.fetchall()]
        return jsonify({'success': True, 'items': items})
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route(f'{API}/creators', methods=['POST'])
@login_required
def gm_creator_set():
    """作成権限を与える．既に行があれば期間を書き換える（user_id が UNIQUE のため）"""
    if not check_is_total_admin(_me()):
        return _err('総管理者のみ', 403)
    data = request.get_json(silent=True) or {}
    if not data.get('user_id'):
        return _err('ユーザを選んでください')
    uid = int(data['user_id'])
    vf, vu = parse_input(data.get('valid_from')), parse_input(data.get('valid_until'))
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id FROM user_group_global_managers WHERE user_id = %s", (uid,))
        row = cursor.fetchone()
        if row:
            cursor.execute("UPDATE user_group_global_managers SET valid_from = %s, valid_until = %s WHERE id = %s", (vf, vu, row['id']))
        else:
            cursor.execute("INSERT INTO user_group_global_managers (user_id, valid_from, valid_until) VALUES (%s,%s,%s)", (uid, vf, vu))
        conn.commit()
        return jsonify({'success': True, 'updated': bool(row)})
    except Exception as e:
        conn.rollback()
        return _err(str(e), 500)
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route(f'{API}/creators/<int:cid>', methods=['DELETE'])
@login_required
def gm_creator_delete(cid):
    if not check_is_total_admin(_me()):
        return _err('総管理者のみ', 403)
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM user_group_global_managers WHERE id = %s", (cid,))
        conn.commit()
        return jsonify({'success': True})
    finally:
        cursor.close()
        conn.close()


# ────────────────────────────────────────────
# 移行：台帳のグループ種別を直接メンバーに戻す（総管理者のみ，1回きりの想定・冪等）
# ────────────────────────────────────────────

@user_groups_bp.route(f'{API}/retire_group_kind', methods=['POST'])
@login_required
def gm_retire_group_kind():
    """
    台帳のグループ種別（ug_units.kind='group'）の単位を，同名の user_groups の直接メンバーに移す．
      ・発令（user_id あり）→ user_group_memberships（期間をそのまま写す．同じ行があれば足さない）
      ・グループ種別で序列が最小の役割（管理者）の発令 → user_groups.manager_user_id
      ・移し終えた単位について，その単位を指すルール・発令・単位そのものを消す
      ・氏名だけ（users 未解決）の発令がある単位は移さずに残して報告する
    dry=true なら何も書かずに結果（構成員の増減の照合を含む）だけ返す．
    """
    me = _me()
    if not check_is_total_admin(me):
        return _err('総管理者のみ', 403)
    dry = bool((request.get_json(silent=True) or {}).get('dry', True))
    at = _now()
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id, name, `rank` FROM ug_roles WHERE kind = 'group' ORDER BY `rank`, sort_order, id")
        roles = cursor.fetchall()
        mgr_role = roles[0]['id'] if roles else None
        cursor.execute("SELECT id, name FROM ug_units WHERE kind = 'group' ORDER BY sort_order, id")
        units = cursor.fetchall()
        unit_ids = [u['id'] for u in units]
        if not units:
            return jsonify({'success': True, 'dry': dry, 'units': 0, 'moved': [], 'kept': [], 'diffs': []})

        # 影響を受けるグループ（同名グループと，これらの単位を指すルールを持つグループ）の，いまの構成員
        fmt = ','.join(['%s'] * len(unit_ids))
        cursor.execute(f"SELECT DISTINCT group_id FROM ug_group_rules WHERE unit_id IN ({fmt})", tuple(unit_ids))
        affected = {r['group_id'] for r in cursor.fetchall()}
        cursor.execute(f"SELECT id FROM user_groups WHERE name IN ({','.join(['%s'] * len(units))})", tuple(u['name'] for u in units))
        affected |= {r['id'] for r in cursor.fetchall()}
        before = {gid: _members(cursor, gid, at) for gid in affected}

        moved, kept = [], []
        added_rows = managers_set = groups_created = 0
        for u in units:
            cursor.execute("SELECT id, manager_user_id FROM user_groups WHERE name = %s", (u['name'],))
            gs = cursor.fetchall()
            if len(gs) > 1:
                kept.append({'name': u['name'], 'reason': f'同名のグループが {len(gs)} 件あります'})
                continue
            cursor.execute("""SELECT role_id, user_id, person_name, valid_from, valid_until FROM ug_appointments
                              WHERE unit_id = %s ORDER BY id""", (u['id'],))
            appts = cursor.fetchall()
            unresolved = sorted({a['person_name'] for a in appts if not a['user_id'] and (a['person_name'] or '').strip()})
            if unresolved:
                kept.append({'name': u['name'], 'reason': 'users に紐づかない氏名：' + '，'.join(unresolved)})
                continue
            if gs:
                gid, cur_mgr = gs[0]['id'], gs[0]['manager_user_id']
            else:
                cursor.execute("""INSERT INTO user_groups (name, description, manager_user_id, created_at, updated_at)
                                  VALUES (%s,%s,%s,%s,%s)""", (u['name'], '台帳のグループ種別から移行', me, at, at))
                gid, cur_mgr = cursor.lastrowid, me
                groups_created += 1
                affected.add(gid)
                before.setdefault(gid, set())
            cursor.execute("SELECT user_id, valid_from, valid_until FROM user_group_memberships WHERE group_id = %s", (gid,))
            have = {(r['user_id'], r['valid_from'], r['valid_until']) for r in cursor.fetchall()}
            n = 0
            for a in appts:
                if not a['user_id']:
                    continue
                key = (a['user_id'], a['valid_from'], a['valid_until'])
                if key in have:
                    continue
                cursor.execute("INSERT INTO user_group_memberships (group_id, user_id, valid_from, valid_until) VALUES (%s,%s,%s,%s)",
                               (gid, *key))
                have.add(key)
                n += 1
            added_rows += n
            mgrs = [a for a in appts if a['user_id'] and a['role_id'] == mgr_role]
            mgrs.sort(key=lambda a: not _valid_at(a['valid_from'], a['valid_until'], at))   # いま有効な人を先に
            new_mgr = mgrs[0]['user_id'] if mgrs else None
            cursor.execute("UPDATE user_groups SET description = NULL WHERE id = %s AND description LIKE %s", (gid, '台帳から生成%'))
            if new_mgr and new_mgr != cur_mgr:
                cursor.execute("UPDATE user_groups SET manager_user_id = %s, updated_at = %s WHERE id = %s", (new_mgr, at, gid))
                managers_set += 1
            cursor.execute("DELETE FROM ug_group_rules WHERE unit_id = %s", (u['id'],))
            cursor.execute("DELETE FROM ug_appointments WHERE unit_id = %s", (u['id'],))
            cursor.execute("UPDATE ug_units SET parent_id = NULL WHERE parent_id = %s", (u['id'],))
            cursor.execute("DELETE FROM ug_units WHERE id = %s", (u['id'],))
            moved.append({'name': u['name'], 'rows': n, 'manager_changed': bool(new_mgr and new_mgr != cur_mgr)})

        diffs = []
        for gid in sorted(affected):
            after = _members(cursor, gid, at)
            b = before.get(gid, set())
            if after != b:
                cursor.execute("SELECT name FROM user_groups WHERE id = %s", (gid,))
                row = cursor.fetchone()
                nm = _user_names(cursor, list(after ^ b))
                diffs.append({'group': row['name'] if row else f'#{gid}',
                              'added': [nm.get(x, f'#{x}') for x in after - b],
                              'removed': [nm.get(x, f'#{x}') for x in b - after]})
        if dry:
            conn.rollback()
        else:
            conn.commit()
        return jsonify({'success': True, 'dry': dry, 'units': len(units), 'moved': moved, 'kept': kept,
                        'rows_added': added_rows, 'managers_set': managers_set, 'groups_created': groups_created,
                        'diffs': diffs})
    except Exception as e:
        conn.rollback()
        return _err(str(e), 500)
    finally:
        cursor.close()
        conn.close()
