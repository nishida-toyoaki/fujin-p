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
user_groups/migrate.py — サイト設定（永続IDの起点）とサイト移行用の書き出し（JSON）

  ug_settings      サイトごとの設定（キーと値）．いまは pid_issuer と pid_prefix
  /system/export.json
                   ユーザ・グループ・組織台帳・永続IDを1本の JSON に書き出す（admin）．
                   起点サイトでは，書き出す前にまだ永続IDの無いユーザとグループに発番する．
                   行はテーブルごとに全列をそのまま書く（ローカルIDつき）．
                   移行先では ug_pids を介して人とグループを突き合わせる．
"""
import re
import json
import logging
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal

from flask import jsonify, request, session, Response

from decorators import login_required
from . import user_groups_bp
from .routes import get_db, check_is_total_admin

JST = timezone(timedelta(hours=9), 'JST')

FORMAT = 'fujinp-user-groups'
FORMAT_VERSION = 1

SETTINGS_DDL = """CREATE TABLE IF NOT EXISTS `ug_settings` (
  `k` varchar(64) NOT NULL,
  `v` text,
  `updated_at` datetime DEFAULT NULL,
  `updated_by` varchar(255) DEFAULT NULL,
  PRIMARY KEY (`k`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""

# 書き出すテーブル（順序は取り込みの順序でもある）
EXPORT_TABLES = [
    ('users',                  'カーネル：アカウント'),
    ('approved_users',         'カーネル：ログインを認めるメールの一覧'),
    ('registration_requests',  'カーネル：登録申請とブラックリスト'),
    ('user_groups',            'システムグループ'),
    ('user_group_memberships', 'システムグループの直接メンバー'),
    ('ug_roles',               '立場'),
    ('ug_units',               'ノード（組織・会議体・グループの単位）'),
    ('ug_unit_links',          'ノードの親子（共有子・期間つき）'),
    ('ug_appointments',        'メンバーシップ（ノード×ユーザ×立場×期間）'),
    ('ug_group_rules',         'グループの導出ルール（ノード×立場→システムグループ）'),
    ('ug_persons',             'アカウント名簿'),
    ('ug_person_aliases',      '名寄せの対象外リスト'),
    ('ug_pids',                '永続ID（ローカルIDとの対応）'),
    ('ug_settings',            'サイト設定'),
]
# 書き出さないテーブル（理由つき．JSON の meta にも載せる）
SKIPPED_TABLES = {
    'user_group_global_managers': 'まいぐる廃止により未使用',
    'user_group_subgroups':       'まいぐる廃止により未使用',
    'ug_group_snapshot':          'ルール導入前の照合用の控え',
    'ug_pid_imports':             'このサイトでの取り込み記録（サイト固有）',
}
SECRET_COLUMNS = ('password_hash',)


def _now():
    return datetime.now(JST).replace(tzinfo=None)


def _is_admin():
    return check_is_total_admin(session.get('user_id'))


def _deny():
    return jsonify({'success': False, 'error': 'admin のみ操作できます'}), 403


def _me_label():
    return f"{session.get('full_name') or ''}({session.get('user_id')})".strip()


# ────────────────────────────────────────────
# 設定
# ────────────────────────────────────────────

def ensure_settings(cursor):
    cursor.execute(SETTINGS_DDL)


def get_setting(key, default=None):
    """設定を読む．表が無い・行が無いときは default（None なら「未設定」）"""
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT v FROM ug_settings WHERE k=%s", (key,))
        row = cursor.fetchone()
        cursor.close()
        return row['v'] if row else default
    except Exception:
        return default
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _put(cursor, key, value):
    cursor.execute("""INSERT INTO ug_settings (k, v, updated_at, updated_by) VALUES (%s,%s,%s,%s)
                      ON DUPLICATE KEY UPDATE v=VALUES(v), updated_at=VALUES(updated_at), updated_by=VALUES(updated_by)""",
                   (key, value, _now(), _me_label()))


def _pid_counts(cursor):
    out = {}
    cursor.execute("""SELECT COUNT(*) AS n FROM users u
                      LEFT JOIN ug_pids p ON p.kind='person' AND p.local_id=u.id
                      WHERE u.deleted_at IS NULL AND p.id IS NULL""")
    out['users_without_pid'] = cursor.fetchone()['n']
    cursor.execute("""SELECT COUNT(*) AS n FROM user_groups g
                      LEFT JOIN ug_pids p ON p.kind='group' AND p.local_id=g.id
                      WHERE p.id IS NULL""")
    out['groups_without_pid'] = cursor.fetchone()['n']
    cursor.execute("SELECT COUNT(*) AS n FROM ug_pids WHERE kind='person' AND local_id IS NOT NULL")
    out['users_with_pid'] = cursor.fetchone()['n']
    cursor.execute("SELECT COUNT(*) AS n FROM ug_pids WHERE kind='group' AND local_id IS NOT NULL")
    out['groups_with_pid'] = cursor.fetchone()['n']
    return out


@user_groups_bp.route('/api/migrate/settings')
@login_required
def migrate_settings_get():
    if not _is_admin():
        return _deny()
    from .pids import is_issuer, prefix
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        ensure_settings(cursor)
        conn.commit()
        counts = _pid_counts(cursor)
    finally:
        cursor.close()
        conn.close()
    return jsonify({'success': True, 'issuer': is_issuer(), 'prefix': prefix(), 'counts': counts})


@user_groups_bp.route('/api/migrate/settings', methods=['POST'])
@login_required
def migrate_settings_put():
    """
    {issuer: bool, prefix: 'FUC'}
    起点にした瞬間に，まだ永続IDの無いユーザとグループに発番する．
    すでにこの接頭辞で発番した永続IDがあるときは，接頭辞を変えられない．
    """
    if not _is_admin():
        return _deny()
    from .pids import prefix as cur_prefix, issue_missing
    d = request.get_json(silent=True) or {}
    issuer = bool(d.get('issuer'))
    pre = str(d.get('prefix') or '').strip().upper()
    if pre and not re.match(r'^[A-Z0-9]{2,8}$', pre):
        return jsonify({'success': False, 'error': '接頭辞は英大文字と数字の2〜8文字です'}), 400
    if issuer and not pre:
        return jsonify({'success': False, 'error': '起点にするには接頭辞が要ります'}), 400

    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        ensure_settings(cursor)
        old = cur_prefix()
        if old and pre != old:
            cursor.execute("SELECT COUNT(*) AS n FROM ug_pids WHERE pid LIKE %s AND issued_at IS NOT NULL",
                           (old + '-%',))
            if cursor.fetchone()['n']:
                conn.rollback()
                return jsonify({'success': False,
                                'error': f'接頭辞 {old} で発番した永続IDがあるので，接頭辞は変えられません'}), 400
        _put(cursor, 'pid_issuer', '1' if issuer else '0')
        _put(cursor, 'pid_prefix', pre)
        made = {'person': 0, 'group': 0}
        if issuer:
            made = issue_missing(cursor, pre)
        conn.commit()
        counts = _pid_counts(cursor)
    except Exception as e:
        conn.rollback()
        logging.exception("migrate_settings_put failed")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()
    return jsonify({'success': True, 'issuer': issuer, 'prefix': pre, 'issued': made, 'counts': counts})


# ────────────────────────────────────────────
# サイト移行用の書き出し（JSON）
# ────────────────────────────────────────────

def _val(v):
    if isinstance(v, datetime):
        return v.strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(v, date):
        return v.strftime('%Y-%m-%d')
    if isinstance(v, (time, timedelta)):
        return str(v)
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, (bytes, bytearray)):
        try:
            return v.decode('utf-8')
        except Exception:
            return None
    return v


def _table_exists(cursor, name):
    cursor.execute("""SELECT COUNT(*) AS n FROM information_schema.TABLES
                      WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s""", (name,))
    return cursor.fetchone()['n'] > 0


def _dump_table(cursor, name, with_secrets):
    cursor.execute(f"SELECT * FROM `{name}` ORDER BY 1")
    rows = cursor.fetchall()
    cols = list(cursor.column_names)
    drop = set() if with_secrets else {c for c in cols if c in SECRET_COLUMNS}
    cols_out = [c for c in cols if c not in drop]
    return {'columns': cols_out,
            'omitted_columns': sorted(drop),
            'rows': [{c: _val(r[c]) for c in cols_out} for r in rows]}


def build_export(cursor, with_secrets=False):
    from .pids import is_issuer, prefix, issue_missing
    from .tree import ensure_links
    warnings = []
    issued = {'person': 0, 'group': 0}
    ensure_settings(cursor)
    try:
        ensure_links(cursor)
    except Exception as e:
        logging.warning("ensure_links failed: %s", e)
    issuer, pre = is_issuer(), prefix()
    if issuer and pre:
        issued = issue_missing(cursor, pre)
    counts = _pid_counts(cursor)
    if counts['users_without_pid'] or counts['groups_without_pid']:
        warnings.append(f"永続IDの無いユーザ {counts['users_without_pid']} 人，グループ {counts['groups_without_pid']} 件があります"
                        "（このサイトは永続IDの起点ではないため発番していません）")

    tables, missing = {}, []
    for name, label in EXPORT_TABLES:
        if not _table_exists(cursor, name):
            missing.append(name)
            continue
        t = _dump_table(cursor, name, with_secrets)
        t['label'] = label
        tables[name] = t
    if missing:
        warnings.append('このサイトに無いテーブル：' + '，'.join(missing))

    try:
        host = request.host
    except Exception:
        host = ''
    meta = {
        'format': FORMAT,
        'format_version': FORMAT_VERSION,
        'exported_at': _now().strftime('%Y-%m-%d %H:%M:%S'),
        'exported_by': _me_label(),
        'site': host,
        'pid_issuer': issuer,
        'pid_prefix': pre,
        'issued_now': issued,
        'with_secrets': bool(with_secrets),
        'counts': {k: len(v['rows']) for k, v in tables.items()},
        'pid_counts': counts,
        'skipped_tables': SKIPPED_TABLES,
        'missing_tables': missing,
        'warnings': warnings,
        'notes': [
            '各テーブルの行は全列をそのまま書いている（id はこのサイトのローカルID）．',
            '人とグループは ug_pids（kind, pid, local_id）で永続IDに対応づく．移行先ではローカルIDを付け替えて取り込む．',
            'ノードは ug_units.id，立場は ug_roles.id で参照される（ug_unit_links，ug_appointments，ug_group_rules）．',
            'ノードとシステムグループの対応付けは ug_group_rules の role_id が NULL・mode が include の行（そのノードと配下の全員→グループ）．',
        ],
    }
    return {'meta': meta, 'tables': tables}


@user_groups_bp.route('/system/export.json')
@login_required
def migrate_export():
    """サイト移行用の書き出し（admin）．?secrets=1 でパスワードのハッシュも含める"""
    if not _is_admin():
        return _deny()
    with_secrets = request.args.get('secrets') == '1'
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        data = build_export(cursor, with_secrets)
        conn.commit()          # 発番した分を確定
    except Exception as e:
        conn.rollback()
        logging.exception("migrate_export failed")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()
    body = json.dumps(data, ensure_ascii=False, indent=1)
    pre = data['meta']['pid_prefix'] or 'site'
    fname = f"user_groups_{pre}_{_now().strftime('%Y%m%d_%H%M%S')}.json"
    return Response(body, mimetype='application/json; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="{fname}"'})


# ────────────────────────────────────────────
# サイト移行用の取り込み（JSON）
# ────────────────────────────────────────────
#
# 突き合わせの規則
#   ユーザ   … 永続IDが一致 → メールが一致（削除済みを除く）→ どちらも無ければ作る．
#              一致したユーザはこのサイトの行をそのまま使う（区分・氏名は書き換えない）．
#              作るユーザのうち区分 admin は regular にする．
#   グループ … 永続IDが一致 → 名前が一致（admin が「統合」か「別名で残す」を選ぶ．既定は統合）→ 作る．
#   立場     … 種別と名前が一致すれば同じもの．
#   ノード   … 種別・名前・（突き合わせ後の）親が一致すれば同じもの．
#   その他   … 参照先を付け替え，同じ内容の行が無ければ足す．
#   永続ID   … 付け替えたローカルIDに写す．このサイトの行に別の永続IDが付いていれば見送る．
#   ug_settings は取り込まない（サイト固有）．
# 「確認」は同じ処理をすべて行ってから取り消すので，件数は「取り込む」と一致する．

USER_COLS = ('manager_user_id', 'user_id', 'created_by', 'approved_by')
RENAME_SUFFIX = '（移入）'


class _Importer:
    def __init__(self, cursor, data, group_actions, with_secrets):
        self.c = cursor
        self.t = data.get('tables') or {}
        self.meta = data.get('meta') or {}
        self.group_actions = group_actions or {}
        self.with_secrets = with_secrets
        self.umap, self.gmap, self.rmap, self.nmap = {}, {}, {}, {}
        self.stats = {}
        self.report = {'users_matched': [], 'admin_demoted': [], 'groups_same_name': [],
                       'groups_by_pid': 0, 'pid_conflicts': [], 'warnings': []}
        self._cols = {}

    # ── 小道具 ──
    def rows(self, name):
        return (self.t.get(name) or {}).get('rows') or []

    def cols(self, table):
        if table not in self._cols:
            self.c.execute("""SELECT COLUMN_NAME FROM information_schema.COLUMNS
                              WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s""", (table,))
            self._cols[table] = {r['COLUMN_NAME'] for r in self.c.fetchall()}
        return self._cols[table]

    def count(self, table, key):
        s = self.stats.setdefault(table, {'added': 0, 'matched': 0, 'skipped': 0})
        s[key] += 1

    def uid(self, v):
        if v is None or isinstance(v, str):
            return v
        return self.umap.get(v)

    def insert(self, table, row, override=None):
        data = {k: v for k, v in row.items() if k != 'id' and k in self.cols(table)}
        for k in USER_COLS:
            if k in data:
                data[k] = self.uid(data[k])
        data.update(override or {})
        data = {k: v for k, v in data.items() if k in self.cols(table)}
        keys = list(data)
        self.c.execute(f"INSERT INTO `{table}` ({', '.join('`%s`' % k for k in keys)}) "
                       f"VALUES ({', '.join(['%s'] * len(keys))})", tuple(data[k] for k in keys))
        return self.c.lastrowid

    def find(self, table, cond):
        cond = {k: v for k, v in cond.items() if k in self.cols(table)}
        where = ' AND '.join(f"`{k}` <=> %s" for k in cond)
        self.c.execute(f"SELECT id FROM `{table}` WHERE {where} ORDER BY id LIMIT 1", tuple(cond.values()))
        r = self.c.fetchone()
        return r['id'] if r else None

    def local_pid(self, kind, pid):
        if not pid:
            return None
        self.c.execute("SELECT local_id FROM ug_pids WHERE kind=%s AND pid=%s", (kind, pid))
        r = self.c.fetchone()
        return r['local_id'] if r else None

    def src_pids(self, kind):
        return {r['local_id']: r['pid'] for r in self.rows('ug_pids')
                if r.get('kind') == kind and r.get('local_id') is not None}

    # ── 各段 ──
    def users(self):
        pids = self.src_pids('person')
        self.c.execute("SELECT id, email, category FROM users WHERE deleted_at IS NULL")
        by_mail = {}
        for r in self.c.fetchall():
            if r['email']:
                by_mail.setdefault(r['email'].strip().lower(), r['id'])
        for u in self.rows('users'):
            if u.get('deleted_at'):
                self.count('users', 'skipped')
                continue
            mail = (u.get('email') or '').strip().lower()
            local = self.local_pid('person', pids.get(u['id'])) or (by_mail.get(mail) if mail else None)
            if local:
                self.umap[u['id']] = local
                self.count('users', 'matched')
                if len(self.report['users_matched']) < 200:
                    self.report['users_matched'].append({'name': u.get('full_name'), 'email': mail, 'local_id': local})
                if self.with_secrets and u.get('password_hash') and 'password_hash' in self.cols('users'):
                    self.c.execute("UPDATE users SET password_hash=%s WHERE id=%s AND password_hash IS NULL",
                                   (u['password_hash'], local))
                continue
            over = {'email': mail or None}
            if u.get('category') == 'admin':
                over['category'] = 'regular'
                self.report['admin_demoted'].append(f"{u.get('full_name') or ''}（{mail}）")
            if not self.with_secrets:
                over['password_hash'] = None
            new = self.insert('users', u, over)
            self.umap[u['id']] = new
            if mail:
                by_mail[mail] = new
            self.count('users', 'added')

    def kernel_lists(self):
        for r in self.rows('approved_users'):
            mail = (r.get('email') or '').strip().lower()
            if not mail or self.find('approved_users', {'email': mail}):
                self.count('approved_users', 'matched')
                continue
            if r.get('category') == 'admin':
                r = dict(r, category='regular')
            self.insert('approved_users', r, {'email': mail})
            self.count('approved_users', 'added')
        for r in self.rows('registration_requests'):
            mail = (r.get('email') or '').strip().lower()
            if self.find('registration_requests', {'email': mail, 'status': r.get('status')}):
                self.count('registration_requests', 'matched')
                continue
            self.insert('registration_requests', r, {'email': mail})
            self.count('registration_requests', 'added')

    def groups(self):
        pids = self.src_pids('group')
        for g in self.rows('user_groups'):
            pid = pids.get(g['id'])
            local = self.local_pid('group', pid)
            if local:
                self.gmap[g['id']] = local
                self.report['groups_by_pid'] += 1
                self.count('user_groups', 'matched')
                continue
            same = self.find('user_groups', {'name': g.get('name')})
            if same:
                act = self.group_actions.get(str(g['id']), 'merge')
                self.report['groups_same_name'].append({'src_id': g['id'], 'name': g.get('name'), 'pid': pid,
                                                        'local_id': same, 'action': act})
                if act != 'rename':
                    self.gmap[g['id']] = same
                    self.count('user_groups', 'matched')
                    continue
                name, n = g.get('name') + RENAME_SUFFIX, 2
                while self.find('user_groups', {'name': name}):
                    name, n = f"{g.get('name')}{RENAME_SUFFIX[:-1]}{n}）", n + 1
                self.gmap[g['id']] = self.insert('user_groups', g, {'name': name})
            else:
                self.gmap[g['id']] = self.insert('user_groups', g)
            self.count('user_groups', 'added')
        for m in self.rows('user_group_memberships'):
            gid, uid = self.gmap.get(m.get('group_id')), self.umap.get(m.get('user_id'))
            if not gid or not uid:
                self.count('user_group_memberships', 'skipped')
                continue
            if self.find('user_group_memberships', {'group_id': gid, 'user_id': uid, 'valid_from': m.get('valid_from')}):
                self.count('user_group_memberships', 'matched')
                continue
            self.insert('user_group_memberships', m, {'group_id': gid, 'user_id': uid})
            self.count('user_group_memberships', 'added')

    def roles(self):
        for r in self.rows('ug_roles'):
            local = self.find('ug_roles', {'kind': r.get('kind'), 'name': r.get('name')})
            if local:
                self.rmap[r['id']] = local
                self.count('ug_roles', 'matched')
            else:
                self.rmap[r['id']] = self.insert('ug_roles', r)
                self.count('ug_roles', 'added')

    def units(self):
        rows = self.rows('ug_units')
        ids = {u['id'] for u in rows}
        pending = list(rows)
        while pending:
            rest = []
            for u in pending:
                p = u.get('parent_id')
                if p is not None and p in ids and p not in self.nmap:
                    rest.append(u)
                    continue
                parent = self.nmap.get(p) if p is not None else None
                over = {'parent_id': parent, 'owner_group_id': self.gmap.get(u.get('owner_group_id'))}
                local = self.find('ug_units', {'kind': u.get('kind'), 'name': u.get('name'), 'parent_id': parent})
                if local:
                    self.nmap[u['id']] = local
                    self.count('ug_units', 'matched')
                else:
                    self.nmap[u['id']] = self.insert('ug_units', u, over)
                    self.count('ug_units', 'added')
            if len(rest) == len(pending):       # 親が循環している行は親なしで入れる
                for u in rest:
                    self.nmap[u['id']] = self.insert('ug_units', u, {'parent_id': None})
                    self.count('ug_units', 'added')
                self.report['warnings'].append(f'親をたどれないノード {len(rest)} 件を，親なしで取り込みました')
                break
            pending = rest

    def links_and_memberships(self):
        for l in self.rows('ug_unit_links'):
            p, ch = self.nmap.get(l.get('parent_id')), self.nmap.get(l.get('child_id'))
            if not p or not ch:
                self.count('ug_unit_links', 'skipped')
                continue
            if self.find('ug_unit_links', {'parent_id': p, 'child_id': ch, 'valid_from': l.get('valid_from')}):
                self.count('ug_unit_links', 'matched')
                continue
            self.insert('ug_unit_links', l, {'parent_id': p, 'child_id': ch})
            self.count('ug_unit_links', 'added')
        for a in self.rows('ug_appointments'):
            n, r = self.nmap.get(a.get('unit_id')), self.rmap.get(a.get('role_id'))
            if not n or not r:
                self.count('ug_appointments', 'skipped')
                continue
            uid = self.umap.get(a.get('user_id')) if a.get('user_id') is not None else None
            if self.find('ug_appointments', {'unit_id': n, 'role_id': r, 'user_id': uid,
                                             'person_name': a.get('person_name'), 'valid_from': a.get('valid_from')}):
                self.count('ug_appointments', 'matched')
                continue
            self.insert('ug_appointments', a, {'unit_id': n, 'role_id': r, 'user_id': uid})
            self.count('ug_appointments', 'added')
        for g in self.rows('ug_group_rules'):
            gid, n = self.gmap.get(g.get('group_id')), self.nmap.get(g.get('unit_id'))
            r = self.rmap.get(g.get('role_id')) if g.get('role_id') is not None else None
            if not gid or not n or (g.get('role_id') is not None and not r):
                self.count('ug_group_rules', 'skipped')
                continue
            if self.find('ug_group_rules', {'group_id': gid, 'unit_id': n, 'role_id': r, 'mode': g.get('mode')}):
                self.count('ug_group_rules', 'matched')
                continue
            self.insert('ug_group_rules', g, {'group_id': gid, 'unit_id': n, 'role_id': r})
            self.count('ug_group_rules', 'added')

    def roster(self):
        for p in self.rows('ug_persons'):
            mail = (p.get('email') or '').strip().lower()
            if mail and self.find('ug_persons', {'email': mail}):
                self.count('ug_persons', 'matched')
                continue
            self.insert('ug_persons', p, {'email': mail or None})
            self.count('ug_persons', 'added')
        for a in self.rows('ug_person_aliases'):
            if self.find('ug_person_aliases', {'alias_name': a.get('alias_name')}):
                self.count('ug_person_aliases', 'matched')
                continue
            self.insert('ug_person_aliases', a)
            self.count('ug_person_aliases', 'added')

    def pids(self):
        for p in self.rows('ug_pids'):
            kind = p.get('kind')
            src = p.get('local_id')
            local = (self.umap if kind == 'person' else self.gmap).get(src) if src is not None else None
            self.c.execute("SELECT id, local_id FROM ug_pids WHERE pid=%s", (p.get('pid'),))
            have = self.c.fetchone()
            if have and (have['local_id'] or not local):
                self.count('ug_pids', 'matched')
                continue
            if local:
                self.c.execute("SELECT pid FROM ug_pids WHERE kind=%s AND local_id=%s", (kind, local))
                other = self.c.fetchone()
                if other:
                    self.report['pid_conflicts'].append(
                        f"{p.get('pid')}（{p.get('display_name') or p.get('email') or ''}）：このサイトの行には既に {other['pid']} が付いています")
                    self.count('ug_pids', 'skipped')
                    continue
            if have:
                self.c.execute("UPDATE ug_pids SET local_id=%s WHERE id=%s", (local, have['id']))
            else:
                self.insert('ug_pids', p, {'local_id': local})
            self.count('ug_pids', 'added')

    def run(self):
        self.users()
        self.kernel_lists()
        self.groups()
        self.roles()
        self.units()
        self.links_and_memberships()
        self.roster()
        self.pids()


def _prepare(cursor):
    """取り込みの前に表と列を揃える（DDL は暗黙に commit するので，取り込みの前に済ませる）"""
    ensure_settings(cursor)
    for mod, fn in (('tree', 'ensure_links'), ('stewards', 'ensure_order_column'),
                    ('stewards', 'ensure_group_order_column')):
        try:
            m = __import__(f'{__package__}.{mod}', fromlist=[fn])
            getattr(m, fn)(cursor)
        except Exception as e:
            logging.warning("%s.%s failed: %s", mod, fn, e)


@user_groups_bp.route('/api/migrate/import', methods=['POST'])
@login_required
def migrate_import():
    """
    multipart: file=json, mode=check|apply,
               group_actions={"<書き出し元のグループID>": "merge"|"rename"}（同名グループの扱い．既定は merge），
               secrets=0|1（パスワードのハッシュが入っていれば使う）
    """
    if not _is_admin():
        return _deny()
    f = request.files.get('file')
    if not f:
        return jsonify({'success': False, 'error': 'ファイルがありません'}), 400
    try:
        data = json.loads(f.read().decode('utf-8-sig'))
    except Exception as e:
        return jsonify({'success': False, 'error': f'JSON として読めません：{e}'}), 400
    meta = data.get('meta') or {}
    if meta.get('format') != FORMAT:
        return jsonify({'success': False, 'error': 'サイト移行用の書き出し（fujinp-user-groups）ではありません'}), 400
    if int(meta.get('format_version') or 0) > FORMAT_VERSION:
        return jsonify({'success': False, 'error': 'このアプリより新しい形式です．アプリを更新してください'}), 400
    apply_ = request.form.get('mode') == 'apply'
    try:
        actions = json.loads(request.form.get('group_actions') or '{}')
    except Exception:
        actions = {}
    with_secrets = request.form.get('secrets') == '1'

    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        _prepare(cursor)
        conn.commit()
        imp = _Importer(cursor, data, actions, with_secrets)
        imp.run()
        if apply_:
            try:
                st = imp.stats
                cursor.execute("""INSERT INTO ug_pid_imports
                                  (version, source_site, file_name, persons_linked, persons_created,
                                   groups_linked, groups_created, memberships_added, imported_by, imported_at, note)
                                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                               (meta.get('exported_at', ''), meta.get('site', ''), f.filename or '',
                                st.get('users', {}).get('matched', 0), st.get('users', {}).get('added', 0),
                                st.get('user_groups', {}).get('matched', 0), st.get('user_groups', {}).get('added', 0),
                                st.get('ug_appointments', {}).get('added', 0), _me_label(), _now(),
                                'サイト移行（JSON）'))
            except Exception as e:
                logging.warning("ug_pid_imports unavailable: %s", e)
            conn.commit()
        else:
            conn.rollback()
    except Exception as e:
        conn.rollback()
        logging.exception("migrate_import failed")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()
    return jsonify({'success': True, 'mode': 'apply' if apply_ else 'check',
                    'source': {k: meta.get(k) for k in ('site', 'exported_at', 'pid_prefix', 'with_secrets')},
                    'stats': imp.stats, 'report': imp.report})
