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
user_groups/stewards.py — グループの分散管理（2026-10-20）

権限は三段で，どれも台帳（ug_appointments）に載る．任期は設けない（valid_until は空のまま，
上位者が終了させるまで続く）．
  admin              … 区分（users.category）でシステムが固定的に決める
  グループ管理統括   … admin が任命する．予約単位「グループ管理統括」（種別グループ）の「メンバー」
  グループ管理者     … 統括が任命する．種別グループの各単位の「管理者」

  統括   … グループの改廃（作成・改名・廃止・再開）と管理者の任命・解任，台帳から導くグループの定義
  管理者 … 自分が管理者であるグループのメンバーの追加・期間の変更・終了

閲覧は admin・regular の全員に開く（組織の透明性のため．guest には開かない）．グループの構成と表示名は誰でも見られ，メールアドレスは admin にだけ出す．
権限のない操作のボタンや画面の入口は，そもそも表示しない．

グループの実体は次のとおり．
  台帳の単位（種別グループ）＋発令（管理者・メンバー）が正本．
  user_groups の行は単位ごとに1つで，ルール（ug_group_rules：単位×全役割）で構成員を導く．
  行の番号は単位に対して不変（改名しても同じ行の名前を変える．廃止しても行は消さない）．
  メンバーの増減は発令の増減だけで済み，所属判定（utils）は即座に変わる．

システムが保つ2つのグループ（ランチャの出し分けに使う）
  グループ管理統括 … 予約単位のメンバー
  グループ管理者   … すべてのグループ単位の管理者
"""
import logging
from datetime import datetime, timedelta, timezone

from flask import render_template, jsonify, request, session, url_for

from decorators import login_required
from . import user_groups_bp
from .routes import get_db, parse_input

JST = timezone(timedelta(hours=9), 'JST')

SUPERVISOR_NAME = 'グループ管理統括'      # 予約単位の名前＝システムグループの名前
MANAGER_GROUP_NAME = 'グループ管理者'     # システムグループの名前
RESERVED_NAMES = (SUPERVISOR_NAME, MANAGER_GROUP_NAME)
ROLE_MANAGER = ('管理者', '所有者')        # 先にあるものを使う（初期の台帳は「所有者」）
ROLE_MEMBER = ('メンバー',)
SOURCE = 'グループ管理'                   # この画面で作った発令の出典


# ────────────────────────────────────────────
# 小道具
# ────────────────────────────────────────────

def _now():
    return datetime.now(JST).replace(tzinfo=None)


def _s(v):
    return '' if v is None else str(v).strip()


def _dstr(dt):
    if not dt:
        return ''
    return dt.strftime('%Y-%m-%d') if (dt.hour, dt.minute) == (0, 0) else dt.strftime('%Y-%m-%d %H:%M')


def _valid_at(valid_from, valid_until, at):
    if valid_from and valid_from > at:
        return False
    if valid_until and valid_until < at:
        return False
    return True


_ORDER_COL = {}


def ensure_order_column(cursor):
    """
    ug_appointments.sort_order（表示順）があるか．無ければ足してみる（tables[] の DDL に合わせる）．
    足せなかったときは False を返し，呼び出し側は表示順なしで動く
    """
    if _ORDER_COL.get('ok'):
        return True
    try:
        cursor.execute("SHOW COLUMNS FROM ug_appointments LIKE 'sort_order'")
        if cursor.fetchall():
            _ORDER_COL['ok'] = True
            return True
        cursor.execute("ALTER TABLE ug_appointments ADD COLUMN sort_order int DEFAULT NULL "
                       "COMMENT '表示順（同じ単位・役割の中．NULL は末尾）' AFTER valid_until")
        _ORDER_COL['ok'] = True
        return True
    except Exception as e:
        logging.warning("ug_appointments.sort_order unavailable: %s", e)
        return False


_GROUP_ORDER = {}


def ensure_group_order_column(cursor):
    """user_groups.sort_order（グループの表示順．admin が決める）があるか．無ければ足してみる"""
    if _GROUP_ORDER.get('ok'):
        return True
    try:
        cursor.execute("SHOW COLUMNS FROM user_groups LIKE 'sort_order'")
        if cursor.fetchall():
            _GROUP_ORDER['ok'] = True
            return True
        cursor.execute("ALTER TABLE user_groups ADD COLUMN sort_order int DEFAULT NULL "
                       "COMMENT '表示順（admin が決める．NULL は末尾）' AFTER manager_user_id")
        _GROUP_ORDER['ok'] = True
        return True
    except Exception as e:
        logging.warning("user_groups.sort_order unavailable: %s", e)
        return False


def group_order_map(cursor):
    """user_groups.id → 表示順（列が無ければ空）"""
    if not ensure_group_order_column(cursor):
        return {}
    cursor.execute("SELECT id, sort_order FROM user_groups WHERE sort_order IS NOT NULL")
    return {r['id']: r['sort_order'] for r in cursor.fetchall()}


def nav_context(active):
    """
    全画面の上のタブ．権限のないタブは出さない．
      ユーザ管理（admin）／グループ管理（regular 以上）／組織情報管理（admin・統括）／管理（admin）
    """
    def run(c):
        w = who(c, session.get('user_id'))
        return {'active': active, 'users': w['admin'], 'groups': w['view'],
                'org': w['can_steward'], 'system': w['admin']}
    return _with_cursor(run)


def order_sql(cursor, alias='a'):
    """表示順の ORDER BY 句の頭（列が無ければ空）"""
    return f"({alias}.sort_order IS NULL), {alias}.sort_order, " if ensure_order_column(cursor) else ''


def _err(msg, code=400):
    return jsonify({'success': False, 'error': msg}), code


def _role_id(cursor, names, rank):
    """種別グループの役割 id（無ければ先頭の名前で作る）"""
    for n in names:
        cursor.execute("SELECT id FROM ug_roles WHERE kind='group' AND name=%s", (n,))
        r = cursor.fetchone()
        if r:
            return r['id']
    cursor.execute("INSERT INTO ug_roles (kind, name, `rank`) VALUES ('group', %s, %s)", (names[0], rank))
    return cursor.lastrowid


def manager_role_id(cursor):
    return _role_id(cursor, ROLE_MANAGER, 1)


def member_role_id(cursor):
    return _role_id(cursor, ROLE_MEMBER, 2)


def _supervisor_unit_id(cursor, create=False):
    cursor.execute("SELECT id FROM ug_units WHERE kind='group' AND parent_id IS NULL AND name=%s",
                   (SUPERVISOR_NAME,))
    r = cursor.fetchone()
    if r:
        return r['id']
    if not create:
        return None
    cursor.execute("""INSERT INTO ug_units (kind, name, parent_id, sort_order, note)
                      VALUES ('group', %s, NULL, 0, %s)""",
                   (SUPERVISOR_NAME, '予約単位：メンバーがグループ管理統括（admin が任命）'))
    return cursor.lastrowid


def _is_admin(cursor, uid):
    if not uid:
        return False
    cursor.execute("SELECT category FROM users WHERE id=%s AND deleted_at IS NULL", (uid,))
    r = cursor.fetchone()
    return bool(r) and r['category'] == 'admin'


def _valid_appt_users(cursor, unit_id, role_id, at):
    cursor.execute("""SELECT user_id, valid_from, valid_until FROM ug_appointments
                      WHERE unit_id=%s AND role_id=%s AND user_id IS NOT NULL""", (unit_id, role_id))
    return {r['user_id'] for r in cursor.fetchall() if _valid_at(r['valid_from'], r['valid_until'], at)}


def is_supervisor(cursor, uid, at=None):
    if not uid:
        return False
    sid = _supervisor_unit_id(cursor)
    if not sid:
        return False
    return uid in _valid_appt_users(cursor, sid, member_role_id(cursor), at or _now())


def managed_unit_ids(cursor, uid, at=None):
    """uid が管理者になっている（開いている）グループ単位の id"""
    if not uid:
        return set()
    at = at or _now()
    rid = manager_role_id(cursor)
    cursor.execute("""SELECT a.unit_id, a.valid_from, a.valid_until, u.valid_from AS uf, u.valid_until AS uu, u.name
                      FROM ug_appointments a JOIN ug_units u ON u.id=a.unit_id
                      WHERE a.user_id=%s AND a.role_id=%s AND u.kind='group'""", (uid, rid))
    return {r['unit_id'] for r in cursor.fetchall()
            if r['name'] != SUPERVISOR_NAME
            and _valid_at(r['valid_from'], r['valid_until'], at) and _valid_at(r['uf'], r['uu'], at)}


VIEW_CATEGORIES = ('admin', 'regular')     # 閲覧できる区分（guest と未登録者は閲覧できない）


def can_view_category(cursor, uid):
    """閲覧してよい区分か（admin・regular で，有効なアカウント）"""
    if not uid:
        return False
    cursor.execute("SELECT category, is_active FROM users WHERE id=%s AND deleted_at IS NULL", (uid,))
    r = cursor.fetchone()
    return bool(r) and bool(r['is_active']) and (r['category'] or '') in VIEW_CATEGORIES


def who(cursor, uid):
    """
    画面とAPIが使う権限の一式．
    閲覧は admin・regular に限る．guest は統括・管理者に任命されていても何もできない
    （任命は regular 以上に対して行う前提）
    """
    view = can_view_category(cursor, uid)
    admin = view and _is_admin(cursor, uid)
    sup = view and is_supervisor(cursor, uid)
    managed = managed_unit_ids(cursor, uid) if view else set()
    return {'view': view, 'admin': admin, 'supervisor': sup, 'managed': managed,
            'can_steward': admin or sup, 'can_manage_any': admin or sup or bool(managed)}


def _hide_emails(obj, w):
    """メールアドレスは admin にだけ見せる（表示名だけは全員に開く）"""
    if w['admin']:
        return obj
    if isinstance(obj, dict):
        return {k: ('' if k == 'email' else _hide_emails(v, w)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_hide_emails(x, w) for x in obj]
    return obj


def can_edit_group_id(group_id, user_id):
    """旧API互換（routes.check_group_permission）：user_groups.id の単位の管理者か"""
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT unit_id FROM ug_group_rules WHERE group_id=%s AND role_id IS NULL AND mode='include'",
                       (group_id,))
        units = {r['unit_id'] for r in cursor.fetchall()}
        return bool(units & managed_unit_ids(cursor, user_id))
    finally:
        cursor.close()
        conn.close()


# ────────────────────────────────────────────
# 台帳の単位 ↔ user_groups（番号を不変に保つ）
# ────────────────────────────────────────────

def _group_id_of_unit(cursor, unit_id, role_id=None):
    """この単位を（全役割で）取り込むルールを持つグループ．予約単位は役割つき"""
    cursor.execute("""SELECT group_id FROM ug_group_rules
                      WHERE unit_id=%s AND role_id <=> %s AND mode='include' ORDER BY id LIMIT 1""",
                   (unit_id, role_id))
    r = cursor.fetchone()
    return r['group_id'] if r else None


def _ensure_rule(cursor, gid, unit_id, role_id, me, note):
    cursor.execute("""SELECT id FROM ug_group_rules
                      WHERE group_id=%s AND unit_id=%s AND role_id <=> %s AND mode='include'""",
                   (gid, unit_id, role_id))
    if cursor.fetchone():
        return 0
    cursor.execute("""INSERT INTO ug_group_rules (group_id, unit_id, role_id, recurse, mode, note, created_by)
                      VALUES (%s,%s,%s,0,'include',%s,%s)""", (gid, unit_id, role_id, note, me))
    return 1


def _ensure_group_row(cursor, name, description, me, unit_id=None, role_id=None):
    """
    単位に対応する user_groups の行を返す（無ければ作る）．
    まずルールで単位から探し（改名されていても同じ行），なければ同名の行，それもなければ作る．
    ルールで見つかった行の名前が単位と違えば，行の名前を単位に合わせる．
    """
    now = _now()
    gid = _group_id_of_unit(cursor, unit_id, role_id) if unit_id else None
    created = renamed = 0
    if gid:
        cursor.execute("SELECT name FROM user_groups WHERE id=%s", (gid,))
        r = cursor.fetchone()
        if r is None:
            gid = None
        elif r['name'] != name:
            cursor.execute("SELECT id FROM user_groups WHERE name=%s AND id<>%s", (name, gid))
            if cursor.fetchone():
                logging.warning("group rename skipped: %s already exists", name)
            else:
                cursor.execute("UPDATE user_groups SET name=%s, updated_at=%s WHERE id=%s", (name, now, gid))
                renamed = 1
    if not gid:
        cursor.execute("SELECT id FROM user_groups WHERE name=%s ORDER BY id LIMIT 1", (name,))
        r = cursor.fetchone()
        gid = r['id'] if r else None
    if not gid:
        cursor.execute("""INSERT INTO user_groups (name, description, manager_user_id, created_at, updated_at)
                          VALUES (%s,%s,%s,%s,%s)""", (name, description, me or 0, now, now))
        gid = cursor.lastrowid
        created = 1
    rules = 0
    if unit_id:
        rules = _ensure_rule(cursor, gid, unit_id, role_id, me, '台帳から生成')
    return gid, created, renamed, rules


def _first_manager(cursor, unit_id, at):
    rid = manager_role_id(cursor)
    cursor.execute("""SELECT user_id, valid_from, valid_until FROM ug_appointments
                      WHERE unit_id=%s AND role_id=%s AND user_id IS NOT NULL ORDER BY valid_from, id""",
                   (unit_id, rid))
    for r in cursor.fetchall():
        if _valid_at(r['valid_from'], r['valid_until'], at):
            return r['user_id']
    return None


def refresh_group_unit(cursor, unit_id, me):
    """1つのグループ単位について user_groups の行・ルール・管理者欄・「グループ管理者」の規則を揃える"""
    cursor.execute("SELECT id, name FROM ug_units WHERE id=%s AND kind='group'", (unit_id,))
    u = cursor.fetchone()
    if not u:
        return None
    if u['name'] == SUPERVISOR_NAME:
        gid, *_ = _ensure_group_row(cursor, SUPERVISOR_NAME, 'システム：グループ管理統括（admin が任命）',
                                    me, unit_id, member_role_id(cursor))
        return gid
    gid, *_ = _ensure_group_row(cursor, u['name'], f"台帳から生成（グループ：{u['name']}）", me, unit_id)
    mgr = _first_manager(cursor, unit_id, _now())
    if mgr:
        cursor.execute("UPDATE user_groups SET manager_user_id=%s WHERE id=%s", (mgr, gid))
    mg, *_ = _ensure_group_row(cursor, MANAGER_GROUP_NAME, 'システム：グループ管理者（全グループの管理者）', me)
    _ensure_rule(cursor, mg, unit_id, manager_role_id(cursor), me, 'グループ管理者の導出')
    return gid


def _name_problem(cursor, name, unit_id=None):
    """グループ名に使えない理由（使えるなら None）"""
    if not name:
        return 'グループ名を入れてください'
    if name in RESERVED_NAMES:
        return f'「{name}」はシステムが使う名前です'
    if len(name) > 255:
        return 'グループ名が長すぎます'
    cursor.execute("SELECT id FROM ug_units WHERE kind='group' AND parent_id IS NULL AND name=%s AND id<>%s",
                   (name, unit_id or 0))
    if cursor.fetchone():
        return f'「{name}」というグループは既にあります（廃止済を含む）'
    own_gid = _group_id_of_unit(cursor, unit_id) if unit_id else None
    cursor.execute("SELECT id FROM user_groups WHERE name=%s AND id<>%s", (name, own_gid or 0))
    if cursor.fetchone():
        return f'「{name}」という user_groups の行が既にあります（組織・会議体から導いたグループなど）'
    try:
        from .ledger import _proposals
        if any(p['group_name'] == name for p in _proposals(cursor)):
            return f'「{name}」は組織・会議体から導くグループの名前と重なります'
    except Exception as e:
        logging.warning("proposal check skipped: %s", e)
    return None


def _user_label(cursor, uid):
    cursor.execute("SELECT full_name FROM users WHERE id=%s AND deleted_at IS NULL", (uid,))
    r = cursor.fetchone()
    return r['full_name'] if r else None


def _appoint(cursor, unit_id, role_id, uid, me, valid_from=None, valid_until=None, note=None):
    name = _user_label(cursor, uid)
    if name is None:
        raise ValueError('そのユーザは見つかりません')
    if role_id != member_role_id(cursor) and not can_view_category(cursor, uid):
        raise ValueError(f'{name} は regular 以上ではないので，統括・管理者には任命できません')
    at = _now()
    if uid in _valid_appt_users(cursor, unit_id, role_id, at):
        raise ValueError(f'{name} は既に入っています')
    cursor.execute("""INSERT INTO ug_appointments
                      (unit_id, role_id, user_id, person_name, valid_from, valid_until, note, source, created_by)
                      VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                   (unit_id, role_id, uid, name, valid_from or at.replace(hour=0, minute=0, second=0, microsecond=0),
                    valid_until, note or None, SOURCE, me))
    aid = cursor.lastrowid
    if ensure_order_column(cursor):
        # 順位がついているグループでは末尾に足す（順位が無いグループは無いままにする）
        cursor.execute("SELECT MAX(sort_order) AS m FROM ug_appointments WHERE unit_id=%s AND role_id=%s",
                       (unit_id, role_id))
        m = cursor.fetchone()['m']
        if m is not None:
            cursor.execute("UPDATE ug_appointments SET sort_order=%s WHERE id=%s", (m + 1, aid))
    return aid


# ────────────────────────────────────────────
# 一覧の材料
# ────────────────────────────────────────────

def _group_units(cursor):
    cursor.execute("""SELECT id, name, note, valid_from, valid_until, sort_order
                      FROM ug_units WHERE kind='group' AND name<>%s ORDER BY sort_order, id""", (SUPERVISOR_NAME,))
    return cursor.fetchall()


def _appts_of(cursor, unit_ids):
    if not unit_ids:
        return []
    marks = ','.join(['%s'] * len(unit_ids))
    has = ensure_order_column(cursor)
    cursor.execute(f"""SELECT a.id, a.unit_id, a.role_id, a.user_id, a.person_name, a.valid_from, a.valid_until,
                              a.note, a.source, {'a.sort_order' if has else 'NULL AS sort_order'}, u.full_name, u.email
                       FROM ug_appointments a LEFT JOIN users u ON u.id=a.user_id
                       WHERE a.unit_id IN ({marks}) ORDER BY {order_sql(cursor)}a.valid_from, a.id""", tuple(unit_ids))
    return cursor.fetchall()


def _appt_view(a, at):
    return {'id': a['id'], 'user_id': a['user_id'], 'order': a.get('sort_order'),
            'name': a['full_name'] or a['person_name'] or '',
            'email': a['email'] or '', 'resolved': a['user_id'] is not None,
            'from': _dstr(a['valid_from']), 'until': _dstr(a['valid_until']),
            'note': a['note'] or '', 'source': a['source'] or '',
            'now': _valid_at(a['valid_from'], a['valid_until'], at),
            'future': bool(a['valid_from'] and a['valid_from'] > at)}


def _groups_payload(cursor, unit_rows, with_members):
    at = _now()
    mrid, brid = manager_role_id(cursor), member_role_id(cursor)
    ids = [u['id'] for u in unit_rows]
    by_unit = {}
    for a in _appts_of(cursor, ids):
        by_unit.setdefault(a['unit_id'], []).append(a)
    cursor.execute("""SELECT unit_id, group_id FROM ug_group_rules WHERE role_id IS NULL AND mode='include'""")
    gid_of = {}
    for r in cursor.fetchall():
        gid_of.setdefault(r['unit_id'], r['group_id'])
    out = []
    for u in unit_rows:
        appts = by_unit.get(u['id'], [])
        mgrs = [_appt_view(a, at) for a in appts if a['role_id'] == mrid]
        mems = [_appt_view(a, at) for a in appts if a['role_id'] == brid]
        g = {'unit_id': u['id'], 'name': u['name'], 'note': u['note'] or '',
             'from': _dstr(u['valid_from']), 'until': _dstr(u['valid_until']),
             'closed': not _valid_at(u['valid_from'], u['valid_until'], at),
             'group_id': gid_of.get(u['id']),
             'managers': [m for m in mgrs if m['now']],
             'managers_all': mgrs,
             'member_count': sum(1 for m in mems if m['now'])}
        if with_members:
            g['members'] = mems
        out.append(g)
    return out


# ────────────────────────────────────────────
# 画面
# ────────────────────────────────────────────

DENY_PAGE = (
    '<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    '<title>{title}</title></head>'
    '<body style="font-family:-apple-system,\'Hiragino Kaku Gothic ProN\',sans-serif;margin:40px;color:#222">'
    '<h2 style="color:#54367C">{title}</h2><p>{msg}</p>'
    '<p><a href="{back}">FUJINダッシュボードに戻る</a></p></body></html>'
)


def _with_cursor(fn):
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        out = fn(cursor)
        conn.commit()
        return out
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/steward')
@login_required
def steward_page():
    """旧：グループ管理統括の画面．グループ管理の画面に統合した"""
    from flask import redirect
    return redirect(url_for('user_groups.my_groups_page'))


@user_groups_bp.route('/manage')
@login_required
def my_groups_page():
    """
    グループ管理の画面（regular 以上に開く．閲覧は全員，操作は役割で決まる）．
      管理者 … 自分のグループのメンバー（追加・期間・順位・退出）
      統括   … ＋ 全グループのメンバー，グループの改廃と管理者の任命
      admin  … ＋ グループの表示順
    """
    w = _with_cursor(lambda c: who(c, session.get('user_id')))
    if not w['view']:
        return DENY_PAGE.format(title='グループ管理', msg='この画面は教職員（regular 以上）が使えます．',
                                back=url_for('user_groups.return_to_fujin')), 403
    return render_template('my_groups.html', me=w, nav=nav_context('groups'), today=_now().strftime('%Y-%m-%d'),
                           focus=request.args.get('unit', ''))


# ────────────────────────────────────────────
# API：共通
# ────────────────────────────────────────────

def _api(fn, need):
    """need: 'view'（ログインした全員）／'steward'（統括・admin）／'manage'（管理者・admin）／'admin'"""
    uid = session.get('user_id')
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        w = who(cursor, uid)
        ok = {'search': w['admin'] or w['supervisor'] or bool(w['managed']), 'view': w['view'], 'admin': w['admin'], 'steward': w['can_steward'], 'manage': w['can_manage_any']}[need]
        if not ok:
            return _err('権限がありません', 403)
        res = fn(cursor, uid, w)
        conn.commit()
        return res
    except ValueError as e:
        conn.rollback()
        return _err(str(e))
    except Exception as e:
        conn.rollback()
        logging.exception("stewards api failed")
        return _err(str(e), 500)
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/api/steward/users')
@login_required
def steward_users():
    """人を選ぶための検索（users ID・メール・氏名の一部）"""
    q = _s(request.args.get('q'))

    def run(cursor, uid, w):
        if not q:
            return jsonify({'success': True, 'items': []})
        import re
        key = re.sub(r'[\s　]+', '', q).lower()
        cursor.execute("""SELECT id, full_name, email, category, is_active FROM users
                          WHERE deleted_at IS NULL""")
        items = []
        for r in cursor.fetchall():
            nm = re.sub(r'[\s　]+', '', r['full_name'] or '').lower()
            # メールで引けるのは admin だけ（admin 以外にはメールアドレスを出さない）
            hit = (q.isdigit() and r['id'] == int(q)) or (key in nm) or \
                  (w['admin'] and key in (r['email'] or '').lower())
            if hit and r['is_active']:
                items.append({'id': r['id'], 'full_name': r['full_name'] or '',
                              'email': (r['email'] or '') if w['admin'] else '',
                              'category': r['category'] if w['admin'] else ''})
        items.sort(key=lambda x: x['full_name'])
        return jsonify({'success': True, 'items': items[:12], 'more': max(0, len(items) - 12)})
    return _api(run, 'search')


# ────────────────────────────────────────────
# API：グループ管理統括（グループの改廃・管理者の任命）
# ────────────────────────────────────────────

@user_groups_bp.route('/api/steward/groups')
@login_required
def steward_groups():
    def run(cursor, uid, w):
        return jsonify({'success': True, 'items': _hide_emails(_groups_payload(cursor, _group_units(cursor), False), w)})
    return _api(run, 'steward')


@user_groups_bp.route('/api/steward/groups', methods=['POST'])
@login_required
def steward_group_create():
    data = request.get_json(silent=True) or {}
    name = _s(data.get('name'))
    managers = [int(x) for x in (data.get('manager_user_ids') or []) if str(x).isdigit()]

    def run(cursor, uid, w):
        prob = _name_problem(cursor, name)
        if prob:
            raise ValueError(prob)
        if not managers:
            raise ValueError('管理者を1人以上選んでください')
        cursor.execute("SELECT COALESCE(MAX(sort_order),0)+10 AS so FROM ug_units WHERE kind='group' AND parent_id IS NULL")
        so = cursor.fetchone()['so']
        now = _now()
        cursor.execute("""INSERT INTO ug_units (kind, name, parent_id, sort_order, valid_from, note)
                          VALUES ('group', %s, NULL, %s, %s, %s)""",
                       (name, so, now.replace(hour=0, minute=0, second=0, microsecond=0), _s(data.get('note')) or None))
        unit_id = cursor.lastrowid
        rid = manager_role_id(cursor)
        member_role_id(cursor)
        for m in managers:
            _appoint(cursor, unit_id, rid, m, uid)
        gid = refresh_group_unit(cursor, unit_id, uid)
        return jsonify({'success': True, 'unit_id': unit_id, 'group_id': gid})
    return _api(run, 'steward')


def _group_unit(cursor, unit_id):
    cursor.execute("SELECT id, name, valid_from, valid_until FROM ug_units WHERE id=%s AND kind='group'", (unit_id,))
    u = cursor.fetchone()
    if not u or u['name'] == SUPERVISOR_NAME:
        raise ValueError('そのグループは見つかりません')
    return u


@user_groups_bp.route('/api/steward/groups/<int:unit_id>', methods=['PUT'])
@login_required
def steward_group_update(unit_id):
    """改名・説明の変更．user_groups の行は同じ番号のまま名前が変わる"""
    data = request.get_json(silent=True) or {}

    def run(cursor, uid, w):
        u = _group_unit(cursor, unit_id)
        if 'name' in data:
            name = _s(data['name'])
            if name != u['name']:
                prob = _name_problem(cursor, name, unit_id)
                if prob:
                    raise ValueError(prob)
                cursor.execute("UPDATE ug_units SET name=%s WHERE id=%s", (name, unit_id))
        if 'note' in data:
            cursor.execute("UPDATE ug_units SET note=%s WHERE id=%s", (_s(data['note']) or None, unit_id))
        refresh_group_unit(cursor, unit_id, uid)
        return jsonify({'success': True})
    return _api(run, 'steward')


@user_groups_bp.route('/api/steward/groups/<int:unit_id>/close', methods=['POST'])
@login_required
def steward_group_close(unit_id):
    """廃止：単位を閉じ，開いている発令（管理者・メンバー）をいま終える．user_groups の行は残す"""
    def run(cursor, uid, w):
        _group_unit(cursor, unit_id)
        now = _now()
        cursor.execute("UPDATE ug_units SET valid_until=%s WHERE id=%s", (now, unit_id))
        cursor.execute("""UPDATE ug_appointments SET valid_until=%s
                          WHERE unit_id=%s AND (valid_until IS NULL OR valid_until > %s)""", (now, unit_id, now))
        return jsonify({'success': True, 'ended': cursor.rowcount})
    return _api(run, 'steward')


@user_groups_bp.route('/api/steward/groups/<int:unit_id>/reopen', methods=['POST'])
@login_required
def steward_group_reopen(unit_id):
    """再開：単位の終了を外す（管理者・メンバーは改めて任命・追加する）"""
    def run(cursor, uid, w):
        _group_unit(cursor, unit_id)
        cursor.execute("UPDATE ug_units SET valid_until=NULL WHERE id=%s", (unit_id,))
        refresh_group_unit(cursor, unit_id, uid)
        return jsonify({'success': True})
    return _api(run, 'steward')


@user_groups_bp.route('/api/steward/groups/<int:unit_id>/managers', methods=['POST'])
@login_required
def steward_manager_add(unit_id):
    data = request.get_json(silent=True) or {}

    def run(cursor, uid, w):
        u = _group_unit(cursor, unit_id)
        if not _valid_at(u['valid_from'], u['valid_until'], _now()):
            raise ValueError('廃止したグループです．先に再開してください')
        aid = _appoint(cursor, unit_id, manager_role_id(cursor), int(data.get('user_id') or 0), uid)
        refresh_group_unit(cursor, unit_id, uid)
        return jsonify({'success': True, 'id': aid})
    return _api(run, 'steward')


# ────────────────────────────────────────────
# API：グループ管理者（メンバーの出入り）
# ────────────────────────────────────────────

@user_groups_bp.route('/api/steward/my')
@login_required
def steward_my_groups():
    """
    閲覧画面の材料（全員に開く）．
      items   … 種別グループ（台帳の単位）．管理者・メンバー・期間つき．mine＝自分が管理者，can_edit＝編集してよい
      derived … それ以外の user_groups（組織・会議体から導くグループ，システムのグループ）．いまの構成員の表示名
    メールアドレスは admin にだけ返す．?closed=1 で廃止済の種別グループも含める
    """
    with_closed = request.args.get('closed') == '1'

    def run(cursor, uid, w):
        items = _groups_payload(cursor, _group_units(cursor), True)
        if not with_closed:
            items = [g for g in items if not g['closed']]
        order = group_order_map(cursor)
        for g in items:
            g['mine'] = g['unit_id'] in w['managed']
            g['can_edit'] = (g['mine'] or w['admin'] or w['supervisor']) and not g['closed']
            g['order'] = order.get(g['group_id'])
        derived = _derived_groups(cursor, {g['group_id'] for g in items} | _group_unit_gids(cursor))
        for g in derived:
            g['order'] = order.get(g['group_id'])
        key = lambda g: (g['order'] is None, g['order'] or 0)
        items.sort(key=key)          # 同じ順位なら元の並び（台帳の序列）を保つ
        derived.sort(key=lambda g: (g['order'] is None, g['order'] or 0, g['name']))
        return jsonify({'success': True, 'items': _hide_emails(items, w), 'derived': derived,
                        'admin': w['admin'], 'supervisor': w['supervisor'], 'managed': sorted(w['managed'])})
    return _api(run, 'view')


def _group_unit_gids(cursor):
    """種別グループの単位に対応する user_groups.id（廃止済を含む．予約単位は除く）"""
    cursor.execute("""SELECT r.group_id FROM ug_group_rules r JOIN ug_units u ON u.id=r.unit_id
                      WHERE u.kind='group' AND u.name<>%s AND r.role_id IS NULL AND r.mode='include'""",
                   (SUPERVISOR_NAME,))
    return {r['group_id'] for r in cursor.fetchall()}


def _derived_groups(cursor, exclude):
    """種別グループ以外の user_groups といまの構成員（表示名のみ）"""
    from .utils import _group_member_ids
    at = _now()
    cursor.execute("SELECT id, full_name FROM users WHERE deleted_at IS NULL")
    uname = {r['id']: r['full_name'] or '' for r in cursor.fetchall()}
    cursor.execute("SELECT id, name, description FROM user_groups ORDER BY name")
    out = []
    for g in cursor.fetchall():
        if g['id'] in exclude:
            continue
        ids = _group_member_ids(cursor, g['id'], at)
        mem = sorted(({'user_id': u, 'name': uname[u]} for u in ids if u in uname), key=lambda x: x['name'])
        cursor.execute("""SELECT r.role_id, r.recurse, r.mode, u.kind, u.name AS unit, ro.name AS role
                          FROM ug_group_rules r JOIN ug_units u ON u.id=r.unit_id
                          LEFT JOIN ug_roles ro ON ro.id=r.role_id WHERE r.group_id=%s ORDER BY r.id""", (g['id'],))
        kl = {'org': '組織', 'body': '会議体', 'duty': '分担事務', 'group': 'グループ'}
        rules = [f"{'除外：' if r['mode'] == 'exclude' else ''}{kl.get(r['kind'], r['kind'])}「{r['unit']}」"
                 f"{'の' + r['role'] if r['role'] else ''}{'（配下も）' if r['recurse'] else ''}" for r in cursor.fetchall()]
        out.append({'group_id': g['id'], 'name': g['name'], 'description': g['description'] or '',
                    'system': g['name'] in RESERVED_NAMES, 'rules': rules,
                    'member_count': len(mem), 'member_names': [m['name'] for m in mem], 'members': mem})
    return out


def _can_edit_member_unit(cursor, w, unit_id):
    """メンバーの追加・変更・順位：admin・統括・そのグループの管理者"""
    if w['admin'] or w['supervisor']:
        return True
    return unit_id in w['managed']


@user_groups_bp.route('/api/steward/groups/<int:unit_id>/members', methods=['POST'])
@login_required
def steward_member_add(unit_id):
    data = request.get_json(silent=True) or {}

    def run(cursor, uid, w):
        u = _group_unit(cursor, unit_id)
        if not _can_edit_member_unit(cursor, w, unit_id):
            raise ValueError('このグループのメンバーを変えられるのは，その管理者・統括・admin です')
        if not _valid_at(u['valid_from'], u['valid_until'], _now()):
            raise ValueError('廃止したグループです')
        vf = parse_input(_s(data.get('valid_from')))
        vu = parse_input(_s(data.get('valid_until')))
        if vf and vu and vu < vf:
            raise ValueError('終了が開始より前です')
        aid = _appoint(cursor, unit_id, member_role_id(cursor), int(data.get('user_id') or 0), uid,
                       vf, vu, _s(data.get('note')))
        return jsonify({'success': True, 'id': aid})
    return _api(run, 'manage')


@user_groups_bp.route('/api/steward/groups/<int:unit_id>/order', methods=['POST'])
@login_required
def steward_member_order(unit_id):
    """
    メンバーの表示順．ids＝メンバーの発令 id を表示したい順に並べたもの．先頭から 1,2,3… を振る．
    ids に無いメンバー（画面に出していない過去の人など）の順位はそのまま
    """
    data = request.get_json(silent=True) or {}
    ids = [int(x) for x in (data.get('ids') or []) if str(x).isdigit()]

    def run(cursor, uid, w):
        _group_unit(cursor, unit_id)
        if not _can_edit_member_unit(cursor, w, unit_id):
            raise ValueError('このグループの順位を変えられるのは，その管理者・統括・admin です')
        if not ensure_order_column(cursor):
            raise ValueError('表示順の列（ug_appointments.sort_order）がありません．アプリのテーブルを更新してください')
        rid = member_role_id(cursor)
        cursor.execute("SELECT id FROM ug_appointments WHERE unit_id=%s AND role_id=%s", (unit_id, rid))
        mine = {r['id'] for r in cursor.fetchall()}
        if any(i not in mine for i in ids):
            raise ValueError('このグループのメンバーでない行が含まれています')
        for n, aid in enumerate(ids, start=1):
            cursor.execute("UPDATE ug_appointments SET sort_order=%s WHERE id=%s", (n, aid))
        return jsonify({'success': True, 'count': len(ids)})
    return _api(run, 'manage')


@user_groups_bp.route('/api/steward/group_order', methods=['POST'])
@login_required
def steward_group_order():
    """グループの表示順（admin）．ids＝user_groups.id を表示したい順に．先頭から 1,2,3… を振り，残りは末尾（NULL）"""
    data = request.get_json(silent=True) or {}
    ids = [int(x) for x in (data.get('ids') or []) if str(x).isdigit()]

    def run(cursor, uid, w):
        if not ensure_group_order_column(cursor):
            raise ValueError('表示順の列（user_groups.sort_order）がありません．アプリのテーブルを更新してください')
        cursor.execute("UPDATE user_groups SET sort_order=NULL")
        for n, gid in enumerate(ids, start=1):
            cursor.execute("UPDATE user_groups SET sort_order=%s WHERE id=%s", (n, gid))
        return jsonify({'success': True, 'count': len(ids)})
    return _api(run, 'admin')


def _appt_for_edit(cursor, w, appt_id):
    """編集してよい発令か（役割で決まる）．戻り値は発令の行"""
    cursor.execute("""SELECT a.id, a.unit_id, a.role_id, a.valid_from, a.valid_until, u.kind, u.name AS unit_name
                      FROM ug_appointments a JOIN ug_units u ON u.id=a.unit_id WHERE a.id=%s""", (appt_id,))
    a = cursor.fetchone()
    if not a or a['kind'] != 'group':
        raise ValueError('その発令は見つかりません')
    if a['unit_name'] == SUPERVISOR_NAME:
        if not w['admin']:
            raise ValueError('グループ管理統括の任命・解任は admin が行います')
    elif a['role_id'] == manager_role_id(cursor):
        if not w['can_steward']:
            raise ValueError('管理者の任命・解任はグループ管理統括が行います')
    elif not _can_edit_member_unit(cursor, w, a['unit_id']):
        raise ValueError('このグループのメンバーを変えられるのは，その管理者・統括・admin です')
    return a


@user_groups_bp.route('/api/steward/appointments/<int:appt_id>', methods=['PUT'])
@login_required
def steward_appt_update(appt_id):
    data = request.get_json(silent=True) or {}

    def run(cursor, uid, w):
        a = _appt_for_edit(cursor, w, appt_id)
        fields, vals = [], []
        for col in ('valid_from', 'valid_until'):
            if col in data:
                fields.append(f"{col}=%s"); vals.append(parse_input(_s(data[col])))
        if 'note' in data:
            fields.append("note=%s"); vals.append(_s(data['note']) or None)
        if not fields:
            raise ValueError('変更がありません')
        vf = vals[fields.index('valid_from=%s')] if 'valid_from=%s' in fields else a['valid_from']
        vu = vals[fields.index('valid_until=%s')] if 'valid_until=%s' in fields else a['valid_until']
        if vf and vu and vu < vf:
            raise ValueError('終了が開始より前です')
        cursor.execute(f"UPDATE ug_appointments SET {', '.join(fields)} WHERE id=%s", tuple(vals) + (appt_id,))
        if a['kind'] == 'group':
            refresh_group_unit(cursor, a['unit_id'], uid)
        return jsonify({'success': True})
    return _api(run, 'view')     # 権限は _appt_for_edit が役割ごとに判定する


@user_groups_bp.route('/api/steward/appointments/<int:appt_id>/end', methods=['POST'])
@login_required
def steward_appt_end(appt_id):
    """いま終える（記録は残る）．メンバーの退出・管理者の解任・統括の解任に使う"""
    def run(cursor, uid, w):
        a = _appt_for_edit(cursor, w, appt_id)
        now = _now()
        if a['valid_from'] and a['valid_from'] > now:
            cursor.execute("DELETE FROM ug_appointments WHERE id=%s", (appt_id,))   # まだ始まっていないものは取り消す
        else:
            cursor.execute("UPDATE ug_appointments SET valid_until=%s WHERE id=%s", (now, appt_id))
        refresh_group_unit(cursor, a['unit_id'], uid)
        return jsonify({'success': True})
    return _api(run, 'view')     # 権限は _appt_for_edit が役割ごとに判定する


@user_groups_bp.route('/api/steward/appointments/<int:appt_id>', methods=['DELETE'])
@login_required
def steward_appt_delete(appt_id):
    """誤って入れた発令を消す（記録も残らない）．ふつうは「終了」を使う"""
    def run(cursor, uid, w):
        a = _appt_for_edit(cursor, w, appt_id)
        cursor.execute("DELETE FROM ug_appointments WHERE id=%s", (appt_id,))
        refresh_group_unit(cursor, a['unit_id'], uid)
        return jsonify({'success': True})
    return _api(run, 'view')     # 権限は _appt_for_edit が役割ごとに判定する


# ────────────────────────────────────────────
# API：admin（グループ管理統括の任命）
# ────────────────────────────────────────────

@user_groups_bp.route('/api/steward/supervisors')
@login_required
def steward_supervisors():
    def run(cursor, uid, w):
        sid = _supervisor_unit_id(cursor)
        items = []
        if sid:
            at = _now()
            items = [_appt_view(a, at) for a in _appts_of(cursor, [sid]) if a['role_id'] == member_role_id(cursor)]
        return jsonify({'success': True, 'items': items})
    return _api(run, 'admin')


@user_groups_bp.route('/api/steward/supervisors', methods=['POST'])
@login_required
def steward_supervisor_add():
    data = request.get_json(silent=True) or {}

    def run(cursor, uid, w):
        sid = _supervisor_unit_id(cursor, create=True)
        aid = _appoint(cursor, sid, member_role_id(cursor), int(data.get('user_id') or 0), uid)
        refresh_group_unit(cursor, sid, uid)
        return jsonify({'success': True, 'id': aid})
    return _api(run, 'admin')
