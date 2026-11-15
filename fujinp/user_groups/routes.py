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
user_groups/routes.py — 共通の小道具と，まいぐる（旧）の後始末

2026-10-20：まいぐる（旧グループ管理画面と /api/groups 系の書き込み API）を廃止した．
user_groups への書き込みは「ユーザとグループ」の台帳（ledger.py）とグループ管理（stewards.py）
だけが行う．ここに残すのは
  ・他モジュールが使う小道具（get_db・parse_input・check_is_total_admin など）
  ・他アプリの画面が読むかもしれない読み取り専用 API（GET のみ）
  ・旧URL /groups の転送
所属判定は utils.py（直接メンバー ∪ ルール由来 − 除外）に一本化してある．
"""
from flask import jsonify, session, redirect, url_for
import mysql.connector
from datetime import datetime, timedelta, timezone
from db import DatabaseConfig
from auth import redirect_to_dashboard
from decorators import login_required
from . import user_groups_bp

JST = timezone(timedelta(hours=9), 'JST')


def get_db():
    """データベース接続を取得"""
    return mysql.connector.connect(**DatabaseConfig.default())


# --- Time Helpers (JST) ---
def get_now_jst():
    return datetime.now(JST)


def to_str(dt):
    if dt is None:
        return None
    return dt.strftime('%Y/%m/%d %H:%M')


def parse_input(date_str):
    if not date_str:
        return None
    try:
        clean_str = date_str.strip().replace('T', ' ')
        if len(clean_str) == 10:  # YYYY-MM-DD のみ
            return datetime.strptime(clean_str, '%Y-%m-%d')
        elif len(clean_str) <= 16:
            return datetime.strptime(clean_str, '%Y-%m-%d %H:%M')
        else:
            return datetime.strptime(clean_str, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None


def is_valid_now(valid_from, valid_until):
    now_jst_naive = get_now_jst().replace(tzinfo=None)
    if valid_from and valid_from > now_jst_naive:
        return False
    if valid_until and valid_until < now_jst_naive:
        return False
    return True


# --- 権限チェック関数 ---

def check_is_total_admin(user_id):
    """総管理者判定：admin カテゴリのユーザが総管理者を兼ねる"""
    if not user_id:
        return False
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT category FROM users WHERE id = %s AND deleted_at IS NULL", (user_id,))
        user = cursor.fetchone()
        cursor.close()
        conn.close()
        return bool(user) and user['category'] == 'admin'
    except Exception as e:
        print(f"Error checking total admin: {e}")
        return False


def check_group_permission(group_id, user_id):
    """
    旧API互換：グループの編集権限．
    いまの権限は台帳で決まる（stewards.py）．admin か，その台帳グループの管理者なら True．
    """
    if check_is_total_admin(user_id):
        return True
    try:
        from .stewards import can_edit_group_id
        return can_edit_group_id(group_id, user_id)
    except Exception:
        return False


def get_user_effective_group_ids(user_id):
    """旧API互換：いま所属しているグループ id のリスト（判定は utils に一本化）"""
    from .utils import get_user_group_ids
    return get_user_group_ids(user_id)


# --- 旧URL ---

@user_groups_bp.route('/groups')
@login_required
def index():
    """まいぐる（旧）は廃止．グループ管理の画面へ転送する"""
    return redirect(url_for('user_groups.my_groups_page'))


# --- 読み取り専用 API（他アプリの画面が使っている場合のため残す．admin・regular に限る） ---

def _viewer_or_403():
    """閲覧してよい区分でなければ 403 の応答を返す（よければ None）"""
    from .stewards import can_view_category
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        if can_view_category(cursor, session.get('user_id')):
            return None
    finally:
        cursor.close()
        conn.close()
    return jsonify({'success': False, 'error': '権限がありません'}), 403


@user_groups_bp.route('/api/groups', methods=['GET'])
@login_required
def list_groups():
    deny = _viewer_or_403()
    if deny:
        return deny
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT g.id, g.name, g.description, g.manager_user_id, u.full_name AS manager_name, g.created_at
            FROM user_groups g
            LEFT JOIN users u ON g.manager_user_id = u.id
            ORDER BY g.name
        """)
        groups = cursor.fetchall()
        for g in groups:
            g['can_edit'] = False          # 編集はグループ管理の画面で行う
            g['created_at'] = to_str(g.get('created_at'))
        return jsonify({'success': True, 'groups': groups,
                        'is_total_admin': check_is_total_admin(session.get('user_id')),
                        'can_create': False})
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/api/groups/<int:group_id>/details', methods=['GET'])
@login_required
def get_group_details(group_id):
    """いまの構成員（判定は utils と同じ）．編集はグループ管理の画面で行う"""
    deny = _viewer_or_403()
    if deny:
        return deny
    from .utils import _group_member_ids, _get_now_jst_naive
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        ids = sorted(_group_member_ids(cursor, group_id, _get_now_jst_naive()))
        members = []
        if ids:
            marks = ','.join(['%s'] * len(ids))
            cursor.execute(f"SELECT id AS user_id, full_name FROM users WHERE id IN ({marks}) ORDER BY full_name",
                           tuple(ids))
            members = [dict(r, is_active=True) for r in cursor.fetchall()]
        return jsonify({'success': True, 'members': members, 'subgroups': [], 'can_edit': False})
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/api/users/candidates', methods=['GET'])
@login_required
def list_user_candidates():
    deny = _viewer_or_403()
    if deny:
        return deny
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id, full_name FROM users WHERE deleted_at IS NULL ORDER BY id ASC")
        return jsonify({'success': True, 'users': cursor.fetchall()})
    finally:
        cursor.close()
        conn.close()


@user_groups_bp.route('/return_to_fujin')
@login_required
def return_to_fujin():
    """FUJINダッシュボードに戻る"""
    return redirect_to_dashboard()
