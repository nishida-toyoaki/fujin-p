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
user_groups/pids.py — 永続ID（サイトをまたぐアカウント・グループの識別子）

FUJIN-P のサイトをまたいでアカウントとグループを突合するための層．
各サイトのローカルID（users.id / user_groups.id）はそのまま残し，その上に
永続IDを被せる．サイトAのローカルID 1 とサイトBのローカルID 3 が同じ永続IDを
持てば同一のアカウント，という形にする．

  ・永続IDが同定するのはアカウントであって人ではない（同じ人の複数アカウントは別ID）
  ・同一サイト内で複数行が同じ永続IDを指すことはない
  ・発番するのは起点サイトだけ（管理画面の「永続IDの起点」で切り替える．ug_settings に保存）
  ・配布はサイト移行用の JSON（migrate.py）．受け取った側は写し取るだけで，常時同期はしない
  ・相手がまだ無い永続IDは控えておき，名簿の発行で users ができたら付け直す（link_pending）

テーブル（default DB）
  ug_pids         永続IDとローカルIDの対応（源泉サイトでは発行台帳を兼ねる）
  ug_pid_imports  サイト移行の取り込みの記録

設定（ug_settings．未設定なら config.py の PID_ISSUER／PID_PREFIX を読む）
  pid_issuer = '1'/'0'      起点サイトなら '1'
  pid_prefix = 'FUC'        発番元の識別子．永続IDの接頭辞になる
"""
import re
from datetime import datetime, timedelta, timezone

from flask import request, session

from .routes import check_is_total_admin

JST = timezone(timedelta(hours=9), 'JST')

KIND_TAG = {'person': 'P', 'group': 'G'}

# ────────────────────────────────────────────
# 小道具
# ────────────────────────────────────────────

def _now():
    return datetime.now(JST).replace(tzinfo=None)


def _s(v):
    return '' if v is None else str(v).strip()


def _is_admin():
    return check_is_total_admin(session.get('user_id'))


def _cfg(name, default):
    """config.py の定数を読む（未定義のサイトでも動くように getattr で拾う）"""
    try:
        from config import Config
        return getattr(Config, name, default)
    except Exception:
        return default


def is_issuer():
    from .migrate import get_setting
    v = get_setting('pid_issuer')
    if v is not None:
        return v == '1'
    return bool(_cfg('PID_ISSUER', False))


def prefix():
    from .migrate import get_setting
    v = get_setting('pid_prefix')
    p = _s(v if v is not None else _cfg('PID_PREFIX', '')).upper()
    return p if re.match(r'^[A-Z0-9]{2,8}$', p) else ''


def _site():
    try:
        return request.host
    except Exception:
        return ''


def _me_label():
    return f"{_s(session.get('full_name'))}({session.get('user_id')})".strip()


# ────────────────────────────────────────────
# 発番
# ────────────────────────────────────────────

def _next_seq(cursor, pre, kind):
    tag = KIND_TAG[kind]
    cursor.execute("SELECT pid FROM ug_pids WHERE pid LIKE %s", (f"{pre}-{tag}-%",))
    pat = re.compile(rf'^{re.escape(pre)}-{re.escape(tag)}-(\d+)$')
    mx = 0
    for r in cursor.fetchall():
        m = pat.match(r['pid'] or '')
        if m:
            mx = max(mx, int(m.group(1)))
    return mx + 1


def _mint(cursor, pre, kind, seq):
    return f"{pre}-{KIND_TAG[kind]}-{seq:06d}"


def issue_missing(cursor, pre):
    """まだ永続IDの無い users（削除済みを除く）と user_groups に発番する．commit は呼び出し側"""
    now = _now()
    me = _me_label()
    made = {'person': 0, 'group': 0}
    cursor.execute("""
        SELECT u.id, u.email, u.full_name FROM users u
        LEFT JOIN ug_pids p ON p.kind='person' AND p.local_id = u.id
        WHERE u.deleted_at IS NULL AND p.id IS NULL
        ORDER BY u.id
    """)
    targets = cursor.fetchall()          # 先に取り切る（同じカーソルを次の問い合わせで使うため）
    seq = _next_seq(cursor, pre, 'person')
    for u in targets:
        cursor.execute("""INSERT INTO ug_pids (kind, pid, local_id, email, display_name,
                                               issued_at, issued_by, source_site)
                          VALUES ('person',%s,%s,%s,%s,%s,%s,%s)""",
                       (_mint(cursor, pre, 'person', seq), u['id'], (u['email'] or '').lower(),
                        u['full_name'], now, me, _site()))
        seq += 1
        made['person'] += 1

    cursor.execute("""
        SELECT g.id, g.name FROM user_groups g
        LEFT JOIN ug_pids p ON p.kind='group' AND p.local_id = g.id
        WHERE p.id IS NULL ORDER BY g.id
    """)
    targets = cursor.fetchall()
    seq = _next_seq(cursor, pre, 'group')
    for g in targets:
        cursor.execute("""INSERT INTO ug_pids (kind, pid, local_id, display_name,
                                               issued_at, issued_by, source_site)
                          VALUES ('group',%s,%s,%s,%s,%s,%s)""",
                       (_mint(cursor, pre, 'group', seq), g['id'], g['name'], now, me, _site()))
        seq += 1
        made['group'] += 1
    return made


# ────────────────────────────────────────────
# 概要
# ────────────────────────────────────────────

# ────────────────────────────────────────────
# エクスポート（XLSX）
# ────────────────────────────────────────────

# ────────────────────────────────────────────
# インポート（XLSX）
# ────────────────────────────────────────────

def _find_local(cursor, kind, email, name):
    """
    永続IDを付ける相手をローカル（元帳）から探す．
      人     … users のメール完全一致（削除済みを除く）
      グループ … user_groups の名前完全一致．同名が複数あれば決めない
    戻り値 (local_id or None, 理由)
    """
    if kind == 'person':
        if not email:
            return None, 'メールが空'
        cursor.execute("SELECT id FROM users WHERE email=%s AND deleted_at IS NULL", (email,))
        rows = cursor.fetchall()
    else:
        if not name:
            return None, '名前が空'
        cursor.execute("SELECT id FROM user_groups WHERE name=%s", (name,))
        rows = cursor.fetchall()
    if not rows:
        return None, 'このサイトに無い'
    if len(rows) > 1:
        return None, f'同じ{"メール" if kind == "person" else "名前"}が {len(rows)} 件ある'
    return rows[0]['id'], ''


def _link_one(cursor, kind, pid, email, name, now, meta, apply_):
    """
    永続IDを1件，ローカルの行に付ける（元帳優先：ローカルの行は作らない・書き換えない）．
    result: known（付与済み）／linked（今回付与）／pending（相手がまだ無いので控えるだけ）／
            conflict（相手の行に別の永続IDが付いている，または相手が一意に決まらない）
    """
    label = 'person' if kind == 'person' else 'group'
    cursor.execute("SELECT local_id FROM ug_pids WHERE pid=%s", (pid,))
    row = cursor.fetchone()
    if row and row['local_id']:
        return {'result': 'known'}
    local, why = _find_local(cursor, kind, email, name)
    shown = email or name
    if not local:
        if why.startswith('同じ'):
            res = {'result': 'conflict', 'note': f"{pid}（{shown}）：{why}ので付与を見送り，控えだけ残します"}
        else:
            res = {'result': 'pending'}
        if apply_:
            _upsert_pid(cursor, label, pid, None, email, name, now, meta)
        return res
    cursor.execute("SELECT pid FROM ug_pids WHERE kind=%s AND local_id=%s AND pid<>%s", (label, local, pid))
    other = cursor.fetchone()
    if other:
        return {'result': 'conflict',
                'note': f"{pid}（{shown}）：このサイトの行には既に {other['pid']} が付いているので付与しません"}
    if apply_:
        _upsert_pid(cursor, label, pid, local, email, name, now, meta)
    return {'result': 'linked'}


def link_pending(cursor):
    """
    控えてある（ローカル未対応の）永続IDを，いまのローカルの行に付け直す．
    台帳の反映で users／user_groups ができたあとに呼ぶ．戻り値 {'person': n, 'group': n}
    """
    now = _now()
    out = {'person': 0, 'group': 0}
    cursor.execute("SELECT pid, kind, email, display_name FROM ug_pids WHERE local_id IS NULL")
    for r in cursor.fetchall():
        k = 'person' if r['kind'] == 'person' else 'group'
        res = _link_one(cursor, k, r['pid'], (r['email'] or '').lower() if k == 'person' else '',
                        r['display_name'] or '', now, {}, True)
        if res['result'] == 'linked':
            out[k] += 1
    return out


def _upsert_pid(cursor, kind, pid, local_id, email, name, now, meta):
    """永続IDの対応を控える．同じ永続IDの行があれば local_id を埋める"""
    cursor.execute("SELECT id FROM ug_pids WHERE pid=%s", (pid,))
    row = cursor.fetchone()
    if row:
        cursor.execute("""UPDATE ug_pids SET local_id=%s, email=COALESCE(NULLIF(%s,''), email),
                          display_name=COALESCE(NULLIF(%s,''), display_name) WHERE id=%s""",
                       (local_id, email, name, row['id']))
        return
    cursor.execute("""INSERT INTO ug_pids (kind, pid, local_id, email, display_name,
                                           issued_at, issued_by, source_site, note)
                      VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                   (kind, pid, local_id, email or None, name or None, now,
                    '取り込み', meta.get('発行元サイト', ''), '配布物から取り込み'))


# ────────────────────────────────────────────
# 画面
# ────────────────────────────────────────────
