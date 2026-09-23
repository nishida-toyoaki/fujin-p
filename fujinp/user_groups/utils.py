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
user_groups/utils.py
他のBlueprintからimportして使うグループ判定ユーティリティ関数。

使い方:
    from fujinp.user_groups.utils import user_is_in_group, get_group_member_ids

2026-09-08 拡張:
    グループの構成員 = 直接メンバー（user_group_memberships）
                     ∪ ルール由来（ug_group_rules → 台帳の発令）
                     − 除外ルール
    ルールが0件のときは従来と完全に同じ結果になる。
    公開API 3本の名前・引数・戻り値は据え置き（as_of はキーワード引数の追加のみ）。
"""
import logging
from datetime import datetime, timedelta, timezone

import mysql.connector
from db import DatabaseConfig

JST = timezone(timedelta(hours=9), 'JST')


def _get_db():
    return mysql.connector.connect(**DatabaseConfig.default())


def _get_now_jst_naive():
    return datetime.now(JST).replace(tzinfo=None)


def _valid_at(valid_from, valid_until, at):
    if valid_from and valid_from > at:
        return False
    if valid_until and valid_until < at:
        return False
    return True


def _is_valid_now(valid_from, valid_until):
    return _valid_at(valid_from, valid_until, _get_now_jst_naive())


def _get_group_id_by_name(cursor, group_name):
    """グループ名 → id（なければ None）"""
    cursor.execute("SELECT id FROM user_groups WHERE name = %s", (group_name,))
    row = cursor.fetchone()
    return row['id'] if row else None


# ────────────────────────────────────────────
# ルール展開（台帳の発令から構成員を導く）
# ────────────────────────────────────────────

def _load_rules(cursor, group_id, at):
    """そのグループの有効なルール。台帳が未導入なら空（従来動作）"""
    try:
        cursor.execute("""
            SELECT unit_id, role_id, recurse, mode, valid_from, valid_until
            FROM ug_group_rules WHERE group_id = %s
        """, (group_id,))
    except Exception:
        return []
    return [r for r in cursor.fetchall() if _valid_at(r['valid_from'], r['valid_until'], at)]


def _descendant_unit_ids(cursor, unit_id):
    """その単位と配下すべての単位 id"""
    ids, queue = [unit_id], [unit_id]
    while queue:
        pid = queue.pop()
        cursor.execute("SELECT id FROM ug_units WHERE parent_id = %s", (pid,))
        for r in cursor.fetchall():
            ids.append(r['id'])
            queue.append(r['id'])
    return ids


def _rule_member_ids(cursor, rule, at):
    """1本のルールが指す user_id の集合"""
    unit_ids = _descendant_unit_ids(cursor, rule['unit_id']) if rule['recurse'] else [rule['unit_id']]
    if not unit_ids:
        return set()
    fmt = ','.join(['%s'] * len(unit_ids))
    sql = f"""SELECT user_id, valid_from, valid_until FROM ug_appointments
              WHERE user_id IS NOT NULL AND unit_id IN ({fmt})"""
    params = list(unit_ids)
    if rule['role_id']:
        sql += " AND role_id = %s"
        params.append(rule['role_id'])
    cursor.execute(sql, tuple(params))
    return {r['user_id'] for r in cursor.fetchall() if _valid_at(r['valid_from'], r['valid_until'], at)}


def _group_member_ids(cursor, group_id, at):
    """直接メンバー ∪ ルール由来 − 除外"""
    cursor.execute("""
        SELECT user_id, valid_from, valid_until
        FROM user_group_memberships WHERE group_id = %s
    """, (group_id,))
    members = {r['user_id'] for r in cursor.fetchall() if _valid_at(r['valid_from'], r['valid_until'], at)}

    rules = _load_rules(cursor, group_id, at)
    if not rules:
        return members                      # ルール0件＝従来と同一

    excluded = set()
    for rule in rules:
        ids = _rule_member_ids(cursor, rule, at)
        if rule['mode'] == 'exclude':
            excluded |= ids
        else:
            members |= ids
    return members - excluded


def _user_group_ids(cursor, user_id, at):
    """そのユーザが属するグループの id 集合（直接メンバー ∪ ルール由来 − 除外）"""
    cursor.execute("""
        SELECT group_id, valid_from, valid_until
        FROM user_group_memberships WHERE user_id = %s
    """, (user_id,))
    direct = {r['group_id'] for r in cursor.fetchall()
              if _valid_at(r['valid_from'], r['valid_until'], at)}

    # ルールを持つグループは直接メンバーだけでは決まらないので改めて判定する
    # （除外ルールがあると直接メンバーでも構成員から外れる）。
    # ルール0件のときは従来と完全に同じ結果になる。
    try:
        cursor.execute("SELECT DISTINCT group_id FROM ug_group_rules")
        rule_group_ids = {r['group_id'] for r in cursor.fetchall()}
    except Exception:
        rule_group_ids = set()

    ids = direct - rule_group_ids
    for gid in rule_group_ids:
        if user_id in _group_member_ids(cursor, gid, at):
            ids.add(gid)
    return ids


# ────────────────────────────────────────────
# 公開API
# ────────────────────────────────────────────

def user_is_in_group(user_id, group_name, as_of=None):
    """
    指定ユーザが指定グループのメンバーか（既定は現時点）。

    Args:
        user_id (int): ユーザID
        group_name (str): グループ名（user_groups.name）
        as_of (datetime): 判定する時点。省略時は現在（JST）

    Returns:
        bool
    """
    if not user_id or not group_name:
        return False
    conn = None
    try:
        at = as_of or _get_now_jst_naive()
        conn = _get_db()
        cursor = conn.cursor(dictionary=True)

        group_id = _get_group_id_by_name(cursor, group_name)
        if group_id is None:
            return False

        return user_id in _group_member_ids(cursor, group_id, at)

    except Exception as e:
        logging.error("user_is_in_group error (group=%s): %s", group_name, e)
        return False
    finally:
        if conn is not None and conn.is_connected():
            cursor.close()
            conn.close()


def get_group_member_ids(group_name, as_of=None):
    """
    指定グループのメンバーのユーザIDのリスト（既定は現時点）。

    Args:
        group_name (str): グループ名
        as_of (datetime): 判定する時点。省略時は現在（JST）

    Returns:
        list[int]
    """
    if not group_name:
        return []
    conn = None
    try:
        at = as_of or _get_now_jst_naive()
        conn = _get_db()
        cursor = conn.cursor(dictionary=True)

        group_id = _get_group_id_by_name(cursor, group_name)
        if group_id is None:
            return []

        return sorted(_group_member_ids(cursor, group_id, at))

    except Exception as e:
        logging.error("get_group_member_ids error (group=%s): %s", group_name, e)
        return []
    finally:
        if conn is not None and conn.is_connected():
            cursor.close()
            conn.close()


def get_user_group_ids(user_id, as_of=None):
    """
    指定ユーザが所属しているグループIDのリスト（既定は現時点）。

    グループIDで閲覧権を管理しているアプリ（マイノート等）の受け渡し用。
    user_group_memberships を直接引くと、台帳のルールから作られたグループ
    （総務課など、直接メンバーの行を持たないグループ）が漏れる。

    Args:
        user_id (int): ユーザID
        as_of (datetime): 判定する時点。省略時は現在（JST）

    Returns:
        list[int]
    """
    if not user_id:
        return []
    conn = None
    try:
        at = as_of or _get_now_jst_naive()
        conn = _get_db()
        cursor = conn.cursor(dictionary=True)
        return sorted(_user_group_ids(cursor, user_id, at))

    except Exception as e:
        logging.error("get_user_group_ids error: %s", e)
        return []
    finally:
        if conn is not None and conn.is_connected():
            cursor.close()
            conn.close()


def get_user_group_names(user_id, as_of=None):
    """
    指定ユーザが所属しているグループ名のリスト（既定は現時点）。
    guest.py のダッシュボードへの受け渡し用。
    判定は get_user_group_ids と同じ（直接メンバー ∪ ルール由来 − 除外）。

    Args:
        user_id (int): ユーザID
        as_of (datetime): 判定する時点。省略時は現在（JST）

    Returns:
        list[str]
    """
    if not user_id:
        return []
    conn = None
    try:
        at = as_of or _get_now_jst_naive()
        conn = _get_db()
        cursor = conn.cursor(dictionary=True)
        ids = _user_group_ids(cursor, user_id, at)
        if not ids:
            return []
        marks = ','.join(['%s'] * len(ids))
        cursor.execute(
            f"SELECT name FROM user_groups WHERE id IN ({marks}) ORDER BY name",
            tuple(sorted(ids)))
        return [r['name'] for r in cursor.fetchall()]

    except Exception as e:
        logging.error("get_user_group_names error: %s", e)
        return []
    finally:
        if conn is not None and conn.is_connected():
            cursor.close()
            conn.close()
