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
user_groups/utils.py
他のBlueprintからimportして使うグループ判定ユーティリティ関数。

使い方:
    from fujinp.user_groups.utils import user_is_in_group, get_group_member_ids
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


def _is_valid_now(valid_from, valid_until):
    now = _get_now_jst_naive()
    if valid_from and valid_from > now:
        return False
    if valid_until and valid_until < now:
        return False
    return True


def _get_group_id_by_name(cursor, group_name):
    """グループ名 → id（なければ None）"""
    cursor.execute("SELECT id FROM user_groups WHERE name = %s", (group_name,))
    row = cursor.fetchone()
    return row['id'] if row else None


# ────────────────────────────────────────────
# 公開API
# ────────────────────────────────────────────

def user_is_in_group(user_id, group_name):
    """
    指定ユーザが指定グループに現時点で有効なメンバーとして所属しているか。

    Args:
        user_id (int): ユーザID
        group_name (str): グループ名（user_groups.name）

    Returns:
        bool
    """
    if not user_id or not group_name:
        return False
    try:
        conn   = _get_db()
        cursor = conn.cursor(dictionary=True)

        group_id = _get_group_id_by_name(cursor, group_name)
        if group_id is None:
            return False

        cursor.execute("""
            SELECT valid_from, valid_until
            FROM user_group_memberships
            WHERE user_id = %s AND group_id = %s
        """, (user_id, group_id))
        rows = cursor.fetchall()

        return any(_is_valid_now(r['valid_from'], r['valid_until']) for r in rows)

    except Exception as e:
        logging.error("user_is_in_group error (group=%s): %s", group_name, e)
        return False
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


def get_group_member_ids(group_name):
    """
    指定グループに現時点で有効なメンバーとして所属しているユーザIDのリスト。

    Args:
        group_name (str): グループ名

    Returns:
        list[int]
    """
    if not group_name:
        return []
    try:
        conn   = _get_db()
        cursor = conn.cursor(dictionary=True)

        group_id = _get_group_id_by_name(cursor, group_name)
        if group_id is None:
            return []

        cursor.execute("""
            SELECT user_id, valid_from, valid_until
            FROM user_group_memberships
            WHERE group_id = %s
        """, (group_id,))
        rows = cursor.fetchall()

        return [r['user_id'] for r in rows if _is_valid_now(r['valid_from'], r['valid_until'])]

    except Exception as e:
        logging.error("get_group_member_ids error (group=%s): %s", group_name, e)
        return []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


def get_user_group_names(user_id):
    """
    指定ユーザが現時点で有効なメンバーとして所属しているグループ名のリスト。
    guest.py のダッシュボードへの受け渡し用。

    Args:
        user_id (int): ユーザID

    Returns:
        list[str]
    """
    if not user_id:
        return []
    try:
        now = _get_now_jst_naive()
        conn   = _get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT g.name
            FROM user_group_memberships m
            JOIN user_groups g ON m.group_id = g.id
            WHERE m.user_id = %s
              AND (m.valid_from  IS NULL OR m.valid_from  <= %s)
              AND (m.valid_until IS NULL OR m.valid_until >= %s)
            ORDER BY g.name
        """, (user_id, now, now))
        return [r['name'] for r in cursor.fetchall()]

    except Exception as e:
        logging.error("get_user_group_names error: %s", e)
        return []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()