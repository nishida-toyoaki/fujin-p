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
user_groups/tree.py — 組織情報管理のツリー（2026-10-20）

組織構造とメンバーシップを分けて持つ．どちらも期間つき．
  組織構造   … ノード（ug_units：種別は 組織・会議体）と親子（ug_unit_links：親・子・期間・並び）
               一つの子を複数の親が共有してよい（厳密な木ではない）．矛盾はチェックしない
  メンバーシップ … 発令（ug_appointments：ノード・ユーザ・立場・期間）．多対多
  システムグループとの対応 … ノード1つに user_groups 1つ（ug_group_rules：単位×全役割×配下も）
               グループの構成員は，そのノードと配下（その時点で有効な親子でたどる）の発令の人

ug_units.parent_id は旧来の親（1つだけ）．ug_unit_links ができたあとは読まない．
初めて使うときに parent_id から親子を写す（ug_unit_links が空のときだけ）．
"""
import logging

from flask import jsonify, request, session

from decorators import login_required
from . import user_groups_bp
from .routes import get_db, parse_input
from .ledger import (org_only, admin_only, _s, _dstr, _now, _valid_at, _as_of_from_request, _reserved_unit_guard,
                     _norm_name, KIND_LABEL)

TREE_KINDS = ('org', 'body')          # ツリーに出す種別（分担事務は別に管理，グループはグループ管理で扱う）

LINKS_DDL = """CREATE TABLE IF NOT EXISTS `ug_unit_links` (
  `id` int NOT NULL AUTO_INCREMENT,
  `parent_id` int NOT NULL COMMENT '親ノード ug_units.id',
  `child_id` int NOT NULL COMMENT '子ノード ug_units.id',
  `valid_from` datetime DEFAULT NULL,
  `valid_until` datetime DEFAULT NULL,
  `sort_order` int NOT NULL DEFAULT '0' COMMENT '同じ親の中の並び',
  `note` text,
  `created_by` int DEFAULT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_ug_links_parent` (`parent_id`),
  KEY `idx_ug_links_child` (`child_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""

_STATE = {}


def ensure_links(cursor):
    """ug_unit_links を用意する（無ければ作る）．空なら ug_units.parent_id から写す"""
    if not _STATE.get('table'):
        cursor.execute("SHOW TABLES LIKE 'ug_unit_links'")
        if not cursor.fetchall():
            cursor.execute(LINKS_DDL)
        _STATE['table'] = True
    cursor.execute("SELECT COUNT(*) AS n FROM ug_unit_links")
    if cursor.fetchone()['n'] == 0:
        cursor.execute("""INSERT INTO ug_unit_links (parent_id, child_id, sort_order, note)
                          SELECT parent_id, id, sort_order, '旧来の親（parent_id）から写した'
                          FROM ug_units WHERE parent_id IS NOT NULL AND kind <> 'group'""")
        return cursor.rowcount
    return 0


def _with_cursor(fn):
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        ensure_links(cursor)
        out = fn(cursor)
        conn.commit()
        return out
    except ValueError as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        conn.rollback()
        logging.exception("tree api failed")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


def _descendants(cursor, unit_id, at):
    """その時点で有効な親子でたどった配下（自分を含む）．同じノードは二度通らない"""
    seen, queue = {unit_id}, [unit_id]
    while queue:
        pid = queue.pop()
        cursor.execute("SELECT child_id, valid_from, valid_until FROM ug_unit_links WHERE parent_id=%s", (pid,))
        for r in cursor.fetchall():
            if at is not None and not _valid_at(r['valid_from'], r['valid_until'], at):
                continue
            if r['child_id'] not in seen:
                seen.add(r['child_id'])
                queue.append(r['child_id'])
    return seen


def _mapped_groups(cursor, unit_ids=None):
    """ノード → 対応する user_groups（単位×全役割の取り込みルール）"""
    cursor.execute("""SELECT r.unit_id, g.id, g.name FROM ug_group_rules r JOIN user_groups g ON g.id=r.group_id
                      WHERE r.role_id IS NULL AND r.mode='include' ORDER BY r.id""")
    out = {}
    for r in cursor.fetchall():
        if unit_ids is None or r['unit_id'] in unit_ids:
            out.setdefault(r['unit_id'], []).append({'id': r['id'], 'name': r['name']})
    return out


# ────────────────────────────────────────────
# 読む
# ────────────────────────────────────────────

@user_groups_bp.route('/api/ledger/tree')
@login_required
@org_only
def ledger_tree():
    """ツリーの材料：ノード・親子・役割．?as_of=YYYY-MM-DD（空＝全期間）"""
    at = _as_of_from_request()

    def run(cursor):
        marks = ','.join(['%s'] * len(TREE_KINDS))
        cursor.execute(f"""SELECT id, kind, name, sort_order, valid_from, valid_until, note
                           FROM ug_units WHERE kind IN ({marks}) ORDER BY sort_order, id""", TREE_KINDS)
        units = cursor.fetchall()
        ids = {u['id'] for u in units}
        cursor.execute("SELECT id, parent_id, child_id, valid_from, valid_until, sort_order, note FROM ug_unit_links ORDER BY sort_order, id")
        links = [l for l in cursor.fetchall() if l['parent_id'] in ids and l['child_id'] in ids]
        cursor.execute(f"""SELECT a.unit_id, a.valid_from, a.valid_until, a.user_id, a.person_name, u.full_name
                           FROM ug_appointments a JOIN ug_units n ON n.id=a.unit_id
                           LEFT JOIN users u ON u.id=a.user_id WHERE n.kind IN ({marks})""", TREE_KINDS)
        cnt, names = {}, {}
        for a in cursor.fetchall():
            if at is not None and not _valid_at(a['valid_from'], a['valid_until'], at):
                continue
            cnt[a['unit_id']] = cnt.get(a['unit_id'], 0) + 1
            nm = a['full_name'] or a['person_name']
            if nm:
                names.setdefault(a['unit_id'], []).append(nm)
        groups = _mapped_groups(cursor, ids)
        cursor.execute(f"SELECT id, kind, name, `rank` FROM ug_roles WHERE kind IN ({marks}) ORDER BY `rank`, sort_order, id", TREE_KINDS)
        roles_all = cursor.fetchall()
        # 立場の一覧は種別を問わず一つにまとめる（同名は序列の小さいものを代表にする．既存の発令の立場はそのまま）
        roles, seen = [], set()
        for r in roles_all:
            if r['name'] not in seen:
                seen.add(r['name'])
                roles.append(r)
        return jsonify({'success': True, 'as_of': _dstr(at) if at else '',
                        'nodes': [{'id': u['id'], 'kind': u['kind'], 'kind_label': KIND_LABEL[u['kind']], 'name': u['name'],
                                   'from': _dstr(u['valid_from']), 'until': _dstr(u['valid_until']),
                                   'valid': at is None or _valid_at(u['valid_from'], u['valid_until'], at),
                                   'note': u['note'] or '', 'count': cnt.get(u['id'], 0),
                                   'names': names.get(u['id'], []), 'groups': groups.get(u['id'], [])} for u in units],
                        'links': [{'id': l['id'], 'parent': l['parent_id'], 'child': l['child_id'],
                                   'from': _dstr(l['valid_from']), 'until': _dstr(l['valid_until']),
                                   'valid': at is None or _valid_at(l['valid_from'], l['valid_until'], at),
                                   'order': l['sort_order']} for l in links],
                        'roles': roles, 'roles_all': roles_all})
    return _with_cursor(run)


@user_groups_bp.route('/api/ledger/nodes/<int:unit_id>/members')
@login_required
@org_only
def ledger_node_members(unit_id):
    """ノードのメンバーシップ（全期間．画面で時点を当てる）と，配下を含めたその時点の構成員数"""
    at = _as_of_from_request()
    show_mail = False
    try:
        from .ledger import _is_admin
        show_mail = _is_admin()
    except Exception:
        pass

    def run(cursor):
        from .stewards import ensure_order_column
        has = ensure_order_column(cursor)
        cursor.execute(f"""SELECT a.id, a.role_id, a.user_id, a.person_name, a.valid_from, a.valid_until, a.note, a.source,
                                  {'a.sort_order' if has else 'NULL AS sort_order'},
                                  r.name AS role_name, r.`rank`, u.full_name, u.email
                           FROM ug_appointments a JOIN ug_roles r ON r.id=a.role_id
                           LEFT JOIN users u ON u.id=a.user_id
                           WHERE a.unit_id=%s ORDER BY r.`rank`, r.id, (a.sort_order IS NULL), a.sort_order, a.valid_from, a.id""",
                       (unit_id,))
        now = at or _now()
        rows = [{'id': a['id'], 'role_id': a['role_id'], 'role': a['role_name'], 'user_id': a['user_id'],
                 'name': a['full_name'] or a['person_name'] or '', 'ledger_name': a['person_name'] or '',
                 'email': (a['email'] or '') if show_mail else '',
                 'resolved': a['user_id'] is not None, 'from': _dstr(a['valid_from']), 'until': _dstr(a['valid_until']),
                 'note': a['note'] or '', 'source': a['source'] or '', 'order': a['sort_order'],
                 'now': _valid_at(a['valid_from'], a['valid_until'], now),
                 'future': bool(a['valid_from'] and a['valid_from'] > now)} for a in cursor.fetchall()]
        sub = _descendants(cursor, unit_id, now)
        marks = ','.join(['%s'] * len(sub))
        # 構成員（自動計算）：このノードと配下の，その時点のメンバー．人ごとに「どこで（立場）」をまとめる
        cursor.execute(f"""SELECT a.unit_id, a.user_id, a.person_name, a.valid_from, a.valid_until,
                                  n.name AS unit_name, r.name AS role_name, u.full_name
                           FROM ug_appointments a JOIN ug_units n ON n.id=a.unit_id JOIN ug_roles r ON r.id=a.role_id
                           LEFT JOIN users u ON u.id=a.user_id
                           WHERE a.unit_id IN ({marks})""", tuple(sub))
        people = {}
        for r in cursor.fetchall():
            if not _valid_at(r['valid_from'], r['valid_until'], now):
                continue
            nm = r['full_name'] or r['person_name']
            if not nm:
                continue
            key = r['user_id'] or ('n:' + _norm_name(nm))
            p = people.setdefault(key, {'user_id': r['user_id'], 'name': nm, 'where': []})
            w = f"{r['unit_name']}（{r['role_name']}）" if r['unit_id'] != unit_id else f"（{r['role_name']}）"
            if w not in p['where']:
                p['where'].append(w)
        users = {k for k in people if not str(k).startswith('n:')}
        subtree_members = sorted(people.values(), key=lambda x: (x['user_id'] is None, x['name']))
        # 下位ノード（子．全期間）
        cursor.execute("""SELECT l.id, l.child_id, l.valid_from, l.valid_until, l.sort_order, u.name
                          FROM ug_unit_links l JOIN ug_units u ON u.id=l.child_id
                          WHERE l.parent_id=%s ORDER BY l.sort_order, l.id""", (unit_id,))
        children = [{'link_id': c['id'], 'id': c['child_id'], 'name': c['name'],
                     'from': _dstr(c['valid_from']), 'until': _dstr(c['valid_until']),
                     'valid': _valid_at(c['valid_from'], c['valid_until'], now)} for c in cursor.fetchall()]
        cursor.execute("SELECT id, parent_id, valid_from, valid_until FROM ug_unit_links WHERE child_id=%s ORDER BY id", (unit_id,))
        parents = []
        for l in cursor.fetchall():
            cursor.execute("SELECT name FROM ug_units WHERE id=%s", (l['parent_id'],))
            p = cursor.fetchone()
            parents.append({'link_id': l['id'], 'id': l['parent_id'], 'name': p['name'] if p else f"#{l['parent_id']}",
                            'from': _dstr(l['valid_from']), 'until': _dstr(l['valid_until']),
                            'valid': _valid_at(l['valid_from'], l['valid_until'], now)})
        groups = _mapped_groups(cursor, {unit_id}).get(unit_id, [])
        # 多対1：同じグループに対応しているほかのノード
        for g in groups:
            cursor.execute("""SELECT u.id, u.name FROM ug_group_rules r JOIN ug_units u ON u.id=r.unit_id
                              WHERE r.group_id=%s AND r.role_id IS NULL AND r.mode='include' AND r.unit_id<>%s""",
                           (g['id'], unit_id))
            g['others'] = [x['name'] for x in cursor.fetchall()]
        # このノードを立場で絞って使っている旧来のグループ（「◯◯_事務」など）
        cursor.execute("""SELECT DISTINCT g.id, g.name FROM ug_group_rules r JOIN user_groups g ON g.id=r.group_id
                          WHERE r.unit_id=%s AND (r.role_id IS NOT NULL OR r.mode='exclude')""", (unit_id,))
        mapped = {g['id'] for g in groups}
        related = [{'id': r['id'], 'name': r['name']} for r in cursor.fetchall() if r['id'] not in mapped]
        return jsonify({'success': True, 'items': rows, 'subtree_nodes': len(sub), 'subtree_users': len(users),
                        'subtree_members': subtree_members, 'children': children,
                        'parents': parents, 'groups': groups, 'related': related})
    return _with_cursor(run)


# ────────────────────────────────────────────
# 組織構造を書く（admin・統括）
# ────────────────────────────────────────────

@user_groups_bp.route('/api/ledger/nodes', methods=['POST'])
@login_required
@org_only
def ledger_node_create():
    """ノードを作る．parent_id があればその子として親子を張る"""
    data = request.get_json(silent=True) or {}
    name = _s(data.get('name'))
    kind = data.get('kind') if data.get('kind') in TREE_KINDS else 'org'     # 種別は使わない（旧来の値として 'org' を入れる）
    if not name:
        return jsonify({'success': False, 'error': '名称を入れてください'}), 400

    def run(cursor):
        vf, vu = parse_input(_s(data.get('valid_from'))), parse_input(_s(data.get('valid_until')))
        parent = data.get('parent_id') or None
        cursor.execute("SELECT COALESCE(MAX(sort_order),0)+10 AS so FROM ug_units WHERE kind=%s", (kind,))
        so = cursor.fetchone()['so']
        cursor.execute("""INSERT INTO ug_units (kind, name, parent_id, sort_order, valid_from, valid_until, note)
                          VALUES (%s,%s,%s,%s,%s,%s,%s)""", (kind, name, parent, so, vf, vu, _s(data.get('note')) or None))
        nid = cursor.lastrowid
        if parent:
            _add_link(cursor, int(parent), nid, vf, vu)
        # ノードを作るときに，同名のシステムグループを1対1で作って対応させる（同名があればそれに対応させる）
        gid = _map_group(cursor, nid, name, None)
        return jsonify({'success': True, 'id': nid, 'group_id': gid})
    return _with_cursor(run)


@user_groups_bp.route('/api/ledger/nodes/<int:unit_id>', methods=['DELETE'])
@login_required
@org_only
def ledger_node_delete(unit_id):
    """ノードを消す（そのノードの発令・親子・対応ルールも）．配下のノードは消さない（親を失うだけ）"""
    def run(cursor):
        err = _reserved_unit_guard(cursor, [unit_id])
        if err:
            raise ValueError(err)
        cursor.execute("DELETE FROM ug_appointments WHERE unit_id=%s", (unit_id,))
        na = cursor.rowcount
        cursor.execute("DELETE FROM ug_unit_links WHERE parent_id=%s OR child_id=%s", (unit_id, unit_id))
        cursor.execute("DELETE FROM ug_group_rules WHERE unit_id=%s", (unit_id,))
        cursor.execute("UPDATE ug_units SET parent_id=NULL WHERE parent_id=%s", (unit_id,))
        cursor.execute("DELETE FROM ug_units WHERE id=%s", (unit_id,))
        return jsonify({'success': True, 'deleted_appointments': na})
    return _with_cursor(run)


def _add_link(cursor, parent, child, vf=None, vu=None):
    cursor.execute("SELECT COALESCE(MAX(sort_order),0)+10 AS so FROM ug_unit_links WHERE parent_id=%s", (parent,))
    so = cursor.fetchone()['so']
    cursor.execute("""INSERT INTO ug_unit_links (parent_id, child_id, valid_from, valid_until, sort_order, created_by)
                      VALUES (%s,%s,%s,%s,%s,%s)""", (parent, child, vf, vu, so, session.get('user_id')))
    return cursor.lastrowid


@user_groups_bp.route('/api/ledger/links', methods=['POST'])
@login_required
@org_only
def ledger_link_create():
    """親子を張る（共有も可）．矛盾（循環・重複）はチェックしない"""
    data = request.get_json(silent=True) or {}

    def run(cursor):
        p, c = int(data.get('parent_id') or 0), int(data.get('child_id') or 0)
        if not p or not c or p == c:
            raise ValueError('親と子を選んでください')
        lid = _add_link(cursor, p, c, parse_input(_s(data.get('valid_from'))), parse_input(_s(data.get('valid_until'))))
        return jsonify({'success': True, 'id': lid})
    return _with_cursor(run)


@user_groups_bp.route('/api/ledger/links/<int:link_id>', methods=['PUT'])
@login_required
@org_only
def ledger_link_update(link_id):
    data = request.get_json(silent=True) or {}

    def run(cursor):
        fields, vals = [], []
        for col in ('valid_from', 'valid_until'):
            if col in data:
                fields.append(f"{col}=%s"); vals.append(parse_input(_s(data[col])))
        if 'parent_id' in data and data['parent_id']:
            fields.append("parent_id=%s"); vals.append(int(data['parent_id']))
        if not fields:
            raise ValueError('変更がありません')
        cursor.execute(f"UPDATE ug_unit_links SET {', '.join(fields)} WHERE id=%s", tuple(vals) + (link_id,))
        return jsonify({'success': True})
    return _with_cursor(run)


@user_groups_bp.route('/api/ledger/links/<int:link_id>', methods=['DELETE'])
@login_required
@org_only
def ledger_link_delete(link_id):
    """親子を外す（記録を消す）．時期を区切って外すときは終了日を入れる（PUT）"""
    def run(cursor):
        cursor.execute("DELETE FROM ug_unit_links WHERE id=%s", (link_id,))
        return jsonify({'success': True})
    return _with_cursor(run)


@user_groups_bp.route('/api/ledger/order', methods=['POST'])
@login_required
@org_only
def ledger_tree_order():
    """
    並びを順位で決める．
      {parent_id, ids}   … ids＝その親の親子（ug_unit_links.id）を表示したい順に
      {parent_id: null, ids} … ids＝トップレベルのノード（ug_units.id）を表示したい順に
    先頭から 10,20,30… を振る
    """
    data = request.get_json(silent=True) or {}
    ids = [int(x) for x in (data.get('ids') or []) if str(x).isdigit()]

    def run(cursor):
        table = 'ug_unit_links' if data.get('parent_id') else 'ug_units'
        for n, i in enumerate(ids, start=1):
            cursor.execute(f"UPDATE {table} SET sort_order=%s WHERE id=%s", (n * 10, i))
        return jsonify({'success': True, 'count': len(ids)})
    return _with_cursor(run)


@user_groups_bp.route('/api/ledger/nodes/<int:unit_id>/order', methods=['POST'])
@login_required
@org_only
def ledger_member_order(unit_id):
    """ノードのメンバーの並び．ids＝発令 id を表示したい順に（立場ごとの並びは立場の序列が先）"""
    data = request.get_json(silent=True) or {}
    ids = [int(x) for x in (data.get('ids') or []) if str(x).isdigit()]

    def run(cursor):
        from .stewards import ensure_order_column
        if not ensure_order_column(cursor):
            raise ValueError('表示順の列（ug_appointments.sort_order）がありません')
        for n, i in enumerate(ids, start=1):
            cursor.execute("UPDATE ug_appointments SET sort_order=%s WHERE id=%s AND unit_id=%s", (n, i, unit_id))
        return jsonify({'success': True})
    return _with_cursor(run)


# ────────────────────────────────────────────
# システムグループとの対応（1対1）
# ────────────────────────────────────────────

def _map_group(cursor, unit_id, name, gid):
    """ノードをグループに対応させる（このノードの既存の対応は置き換える）．gid が None なら同名のグループ（無ければ作る）"""
    me = session.get('user_id')
    if gid is None:
        cursor.execute("SELECT id FROM user_groups WHERE name=%s ORDER BY id LIMIT 1", (name,))
        r = cursor.fetchone()
        if r:
            gid = r['id']
        else:
            now = _now()
            cursor.execute("""INSERT INTO user_groups (name, description, manager_user_id, created_at, updated_at)
                              VALUES (%s,%s,%s,%s,%s)""", (name, f"組織ツリーのノード「{name}」に対応", me or 0, now, now))
            gid = cursor.lastrowid
    cursor.execute("DELETE FROM ug_group_rules WHERE unit_id=%s AND role_id IS NULL AND mode='include'", (unit_id,))
    cursor.execute("""INSERT INTO ug_group_rules (group_id, unit_id, role_id, recurse, mode, note, created_by)
                      VALUES (%s,%s,NULL,1,'include',%s,%s)""", (gid, unit_id, '組織ツリーの対応（配下も）', me))
    return gid


@user_groups_bp.route('/api/ledger/nodes/<int:unit_id>/group', methods=['POST'])
@login_required
@admin_only
def ledger_node_group(unit_id):
    """
    admin 専用（対応付けの例外管理．ふだんはノードの作成時に同名のグループが1対1で付く）
    action=create … ノードと同名のグループに対応させる（無ければ作る）
    action=link   … 既存のグループ group_id に対応させる
    action=unlink … 対応を外す（グループの行は残す）
    対応＝ルール（このノード×全役割×配下も）．このノードの既存の対応は置き換える
    """
    data = request.get_json(silent=True) or {}
    action = data.get('action')

    def run(cursor):
        cursor.execute("SELECT id, name FROM ug_units WHERE id=%s", (unit_id,))
        u = cursor.fetchone()
        if not u:
            raise ValueError('そのノードは見つかりません')
        if action == 'unlink':
            cursor.execute("DELETE FROM ug_group_rules WHERE unit_id=%s AND role_id IS NULL AND mode='include'", (unit_id,))
            return jsonify({'success': True})
        if action == 'link':
            gid = int(data.get('group_id') or 0)
            cursor.execute("SELECT id FROM user_groups WHERE id=%s", (gid,))
            if not cursor.fetchone():
                raise ValueError('そのグループは見つかりません')
            return jsonify({'success': True, 'group_id': _map_group(cursor, unit_id, u['name'], gid)})
        if action == 'create':
            return jsonify({'success': True, 'group_id': _map_group(cursor, unit_id, u['name'], None)})
        raise ValueError('action が不正です')
    return _with_cursor(run)


@user_groups_bp.route('/api/ledger/mapping')
@login_required
@admin_only
def ledger_mapping():
    """
    ノードとシステムグループの対応付けの一覧（admin の例外管理）．
    status: ok＝同名のグループ1つ／renamed＝名前の違うグループ1つ／none＝対応なし／multi＝2つ以上
    shared: そのグループにほかのノードも対応している
    """
    def run(cursor):
        marks = ','.join(['%s'] * len(TREE_KINDS))
        cursor.execute(f"SELECT id, name, valid_from, valid_until FROM ug_units WHERE kind IN ({marks}) ORDER BY sort_order, id", TREE_KINDS)
        units = cursor.fetchall()
        ids = {u['id'] for u in units}
        mapped = _mapped_groups(cursor, ids)
        by_group = {}
        for uid, gs in mapped.items():
            for g in gs:
                by_group.setdefault(g['id'], []).append(uid)
        cursor.execute("SELECT parent_id, child_id FROM ug_unit_links")
        parent = {}
        for l in cursor.fetchall():
            parent.setdefault(l['child_id'], l['parent_id'])
        name = {u['id']: u['name'] for u in units}

        def path(uid):
            out, cur, seen = [], parent.get(uid), set()
            while cur and cur not in seen and cur in name:
                seen.add(cur); out.append(name[cur]); cur = parent.get(cur)
            return '／'.join(reversed(out))
        now = _now()
        items = []
        for u in units:
            gs = mapped.get(u['id'], [])
            st = 'none' if not gs else 'multi' if len(gs) > 1 else ('ok' if gs[0]['name'] == u['name'] else 'renamed')
            items.append({'id': u['id'], 'name': u['name'], 'path': path(u['id']),
                          'valid': _valid_at(u['valid_from'], u['valid_until'], now), 'status': st,
                          'groups': [{'id': g['id'], 'name': g['name'],
                                      'shared': [name[x] for x in by_group.get(g['id'], []) if x != u['id']]} for g in gs]})
        cursor.execute("SELECT id, name FROM user_groups ORDER BY name")
        return jsonify({'success': True, 'items': items, 'groups': cursor.fetchall()})
    return _with_cursor(run)


@user_groups_bp.route('/api/ledger/nodes/<int:unit_id>/move', methods=['POST'])
@login_required
@org_only
def ledger_node_move(unit_id):
    """
    ノードを別の親の下へ移す（parent_id が空ならトップレベルへ）．
      from_link_id … 移す元の親子（共有されているノードで，どの親から外すか）．空なら，いまの親すべて
      date         … 空なら記録を書き換える（はじめからその親の下にあったことにする）
                     日付があれば，その日から新しい親の下へ（元の親子は前日で終える）
    """
    data = request.get_json(silent=True) or {}

    def run(cursor):
        new_parent = int(data.get('parent_id') or 0) or None
        if new_parent == unit_id:
            raise ValueError('自分自身の下へは移せません')
        d = parse_input(_s(data.get('date')))
        now = _now()
        cursor.execute("SELECT id, parent_id, valid_from, valid_until FROM ug_unit_links WHERE child_id=%s", (unit_id,))
        links = cursor.fetchall()
        fl = int(data.get('from_link_id') or 0)
        if fl:
            links = [l for l in links if l['id'] == fl]
        else:
            at = d or now
            links = [l for l in links if _valid_at(l['valid_from'], l['valid_until'], at)]
        if d is None:
            for l in links:
                cursor.execute("DELETE FROM ug_unit_links WHERE id=%s", (l['id'],))
            if new_parent:
                _add_link(cursor, new_parent, unit_id, None, None)
        else:
            from datetime import timedelta
            end = d - timedelta(days=1)
            for l in links:
                if l['valid_from'] and l['valid_from'] >= d:
                    cursor.execute("DELETE FROM ug_unit_links WHERE id=%s", (l['id'],))
                else:
                    cursor.execute("UPDATE ug_unit_links SET valid_until=%s WHERE id=%s", (end, l['id']))
            if new_parent:
                _add_link(cursor, new_parent, unit_id, d, None)
        cursor.execute("UPDATE ug_units SET parent_id=%s WHERE id=%s", (new_parent, unit_id))
        return jsonify({'success': True, 'moved_from': len(links)})
    return _with_cursor(run)

