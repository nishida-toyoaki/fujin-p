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
User_groups Routes (権限委譲対応版 + サブグループ新規作成機能)
【修正版】グループ管理者の権限を適切に制限
"""
from flask import Blueprint, render_template, jsonify, request, session
import mysql.connector
from datetime import datetime, timedelta, timezone
# from db import default_db_config
from config import Config
from db import DatabaseConfig, Tables
from auth import redirect_to_dashboard  # 冒頭のimportに
from decorators import login_required
from . import user_groups_bp

JST = timezone(timedelta(hours=9), 'JST')

# def get_db():
#     return mysql.connector.connect(
#         host=default_db_config['host'],
#         user=default_db_config['user'],
#         password=default_db_config['password'],
#         database=default_db_config['database'],
#         charset='utf8mb4',
#         use_pure=True
#     )
def get_db():
    """データベース接続を取得"""
    return mysql.connector.connect(**DatabaseConfig.default())

# --- Time Helpers (JST) ---
def get_now_jst():
    return datetime.now(JST)

def to_str(dt):
    if dt is None: return None
    return dt.strftime('%Y/%m/%d %H:%M')

def parse_input(date_str):
    if not date_str: return None
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
    if valid_from and valid_from > now_jst_naive: return False
    if valid_until and valid_until < now_jst_naive: return False
    return True

# --- 権限チェック関数 ---

def check_is_total_admin(user_id):
    """総管理者判定：admin カテゴリのユーザが総管理者を兼ねる（フィーチャー非依存）"""
    if not user_id: return False
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT category FROM users WHERE id = %s AND deleted_at IS NULL",
            (user_id,)
        )
        user = cursor.fetchone()
        cursor.close()
        conn.close()
        return bool(user) and user['category'] == 'admin'
    except Exception as e:
        print(f"Error checking total admin: {e}")
        return False

def check_is_global_manager(user_id):
    if not user_id: return False
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        query = "SELECT valid_from, valid_until FROM user_group_global_managers WHERE user_id = %s"
        cursor.execute(query, (user_id,))
        records = cursor.fetchall()
        cursor.close()
        conn.close()
        for r in records:
            if is_valid_now(r['valid_from'], r['valid_until']):
                return True
        return False
    except Exception:
        return False

def check_group_permission(group_id, user_id):
    """
    グループに対する編集権限をチェック
    - 総管理者: すべてのグループを編集可能
    - グループ所有者: 自分が管理者のグループのみ編集可能
    - グループ管理者: 編集権限なし（作成・削除のみ）
    """
    is_total = check_is_total_admin(user_id)
    if is_total:
        return True

    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT manager_user_id FROM user_groups WHERE id = %s", (group_id,))
    group = cursor.fetchone()
    cursor.close()
    conn.close()

    if group and group['manager_user_id'] == user_id:
        return True
    return False

# --- Routes ---

@user_groups_bp.route('/groups')
@login_required
def index():
    return render_template('groups_manager.html')

@user_groups_bp.route('/api/admin/global_managers', methods=['GET'])
@login_required
def list_global_managers():
    current_user_id = session.get('user_id')
    if not check_is_total_admin(current_user_id):
        return jsonify({'success': False, 'error': 'Permission Denied'}), 403

    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT gm.id, gm.user_id, u.full_name, gm.valid_from, gm.valid_until, gm.created_at
        FROM user_group_global_managers gm
        JOIN users u ON gm.user_id = u.id
        ORDER BY gm.created_at DESC
    """)
    managers = cursor.fetchall()
    cursor.close()
    conn.close()

    for m in managers:
        m['is_active'] = is_valid_now(m['valid_from'], m['valid_until'])
        m['valid_from'] = to_str(m['valid_from'])
        m['valid_until'] = to_str(m['valid_until'])
        m['created_at'] = to_str(m['created_at'])

    return jsonify({'success': True, 'managers': managers})

@user_groups_bp.route('/api/admin/global_managers', methods=['POST'])
@login_required
def add_global_manager():
    current_user_id = session.get('user_id')
    if not check_is_total_admin(current_user_id):
        return jsonify({'success': False, 'error': 'Permission Denied'}), 403

    data = request.json
    valid_from = parse_input(data.get('valid_from'))
    valid_until = parse_input(data.get('valid_until'))

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO user_group_global_managers (user_id, valid_from, valid_until)
        VALUES (%s, %s, %s)
    """, (data.get('user_id'), valid_from, valid_until))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@user_groups_bp.route('/api/admin/global_managers/<int:manager_id>', methods=['DELETE'])
@login_required
def delete_global_manager(manager_id):
    current_user_id = session.get('user_id')
    if not check_is_total_admin(current_user_id):
        return jsonify({'success': False, 'error': 'Permission Denied'}), 403

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM user_group_global_managers WHERE id = %s", (manager_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@user_groups_bp.route('/api/groups', methods=['GET'])
@login_required
def list_groups():
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT g.id, g.name, g.description, g.manager_user_id, u.full_name as manager_name, g.created_at
        FROM user_groups g
        LEFT JOIN users u ON g.manager_user_id = u.id
        ORDER BY g.created_at DESC
    """)
    groups = cursor.fetchall()

    current_user_id = session.get('user_id')
    is_total = check_is_total_admin(current_user_id)
    is_global = check_is_global_manager(current_user_id)
    can_create = is_total or is_global

    for g in groups:
        # 【修正】グループのメンバー編集権限は、総管理者またはそのグループの管理者のみ
        g['can_edit'] = is_total or (g['manager_user_id'] == current_user_id)
        g['created_at'] = to_str(g.get('created_at'))

    cursor.close()
    conn.close()
    return jsonify({'success': True, 'groups': groups, 'is_total_admin': is_total, 'can_create': can_create})

@user_groups_bp.route('/api/groups/create', methods=['POST'])
@login_required
def create_group():
    current_user_id = session.get('user_id')
    is_total = check_is_total_admin(current_user_id)
    is_global = check_is_global_manager(current_user_id)

    if not (is_total or is_global):
        return jsonify({'success': False, 'error': 'Permission Denied'}), 403

    data = request.json
    name = data.get('name')
    manager_id = data.get('manager_user_id') or current_user_id
    now_jst = get_now_jst().replace(tzinfo=None)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO user_groups (name, description, manager_user_id, created_at)
        VALUES (%s, %s, %s, %s)
    """, (name, data.get('description'), manager_id, now_jst))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@user_groups_bp.route('/api/groups/<int:group_id>/details', methods=['GET'])
@login_required
def get_group_details(group_id):
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT manager_user_id FROM user_groups WHERE id = %s", (group_id,))
    group = cursor.fetchone()

    current_user_id = session.get('user_id')
    is_total = check_is_total_admin(current_user_id)

    # 【修正】グループのメンバー編集権限は、総管理者またはそのグループの管理者のみ
    can_edit = False
    if group:
        can_edit = is_total or (group['manager_user_id'] == current_user_id)

    cursor.execute("""
        SELECT m.id, m.user_id, u.full_name, m.valid_from, m.valid_until
        FROM user_group_memberships m
        JOIN users u ON m.user_id = u.id
        WHERE m.group_id = %s ORDER BY m.valid_from DESC
    """, (group_id,))
    members = cursor.fetchall()
    for m in members:
        m['is_active'] = is_valid_now(m['valid_from'], m['valid_until'])
        m['valid_from'] = to_str(m['valid_from'])
        m['valid_until'] = to_str(m['valid_until'])

    cursor.execute("""
        SELECT s.id, s.child_group_id, g.name as child_group_name, s.valid_from, s.valid_until
        FROM user_group_subgroups s
        JOIN user_groups g ON s.child_group_id = g.id
        WHERE s.parent_group_id = %s ORDER BY s.valid_from DESC
    """, (group_id,))
    subgroups = cursor.fetchall()
    for s in subgroups:
        s['is_active'] = is_valid_now(s['valid_from'], s['valid_until'])
        s['valid_from'] = to_str(s['valid_from'])
        s['valid_until'] = to_str(s['valid_until'])

    cursor.close()
    conn.close()
    return jsonify({'success': True, 'members': members, 'subgroups': subgroups, 'can_edit': can_edit})

@user_groups_bp.route('/api/groups/<int:group_id>/members', methods=['POST'])
@login_required
def add_member(group_id):
    current_user_id = session.get('user_id')
    if not check_group_permission(group_id, current_user_id):
        return jsonify({'success': False, 'error': 'Permission Denied'}), 403

    data = request.json
    valid_from = parse_input(data.get('valid_from'))
    valid_until = parse_input(data.get('valid_until'))

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO user_group_memberships (group_id, user_id, valid_from, valid_until) VALUES (%s, %s, %s, %s)",
                   (group_id, data['user_id'], valid_from, valid_until))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@user_groups_bp.route('/api/groups/<int:group_id>/members', methods=['PUT'])
@login_required
def sync_members(group_id):
    """メンバー一括同期: チェックON→追加or任期更新, チェックOFF→レコード削除"""
    current_user_id = session.get('user_id')
    if not check_group_permission(group_id, current_user_id):
        return jsonify({'success': False, 'error': 'Permission Denied'}), 403

    data = request.json  # [{user_id, checked, valid_from, valid_until, membership_id?}, ...]
    if not isinstance(data, list):
        return jsonify({'success': False, 'error': 'Invalid payload'}), 400

    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        # 現在のメンバーシップを取得（user_id → membership id のマップ）
        cursor.execute(
            "SELECT id, user_id FROM user_group_memberships WHERE group_id = %s",
            (group_id,)
        )
        existing = {r['user_id']: r['id'] for r in cursor.fetchall()}

        for item in data:
            uid = int(item['user_id'])
            checked = bool(item.get('checked'))
            vf = parse_input(item.get('valid_from'))
            vu = parse_input(item.get('valid_until'))

            if checked:
                if uid in existing:
                    # 任期更新
                    cursor.execute(
                        "UPDATE user_group_memberships SET valid_from=%s, valid_until=%s WHERE id=%s",
                        (vf, vu, existing[uid])
                    )
                else:
                    # 新規追加
                    cursor.execute(
                        "INSERT INTO user_group_memberships (group_id, user_id, valid_from, valid_until) VALUES (%s,%s,%s,%s)",
                        (group_id, uid, vf, vu)
                    )
            else:
                if uid in existing:
                    # レコード削除
                    cursor.execute(
                        "DELETE FROM user_group_memberships WHERE id=%s",
                        (existing[uid],)
                    )

        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@user_groups_bp.route('/api/groups/<int:group_id>/subgroups', methods=['POST'])
@login_required
def add_subgroup(group_id):
    current_user_id = session.get('user_id')
    if not check_group_permission(group_id, current_user_id):
        return jsonify({'success': False, 'error': 'Permission Denied'}), 403

    data = request.json
    valid_from = parse_input(data.get('valid_from'))
    valid_until = parse_input(data.get('valid_until'))

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO user_group_subgroups (parent_group_id, child_group_id, valid_from, valid_until) VALUES (%s, %s, %s, %s)",
                   (group_id, data['child_group_id'], valid_from, valid_until))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@user_groups_bp.route('/api/groups/<int:group_id>/members/<int:item_id>', methods=['DELETE'])
@login_required
def delete_member(group_id, item_id):
    current_user_id = session.get('user_id')
    if not check_group_permission(group_id, current_user_id): return jsonify({'error': 'Denied'}), 403
    conn = get_db(); cursor = conn.cursor()
    cursor.execute("DELETE FROM user_group_memberships WHERE id=%s AND group_id=%s", (item_id, group_id))
    conn.commit(); conn.close()
    return jsonify({'success': True})

@user_groups_bp.route('/api/groups/<int:group_id>/members/<int:item_id>', methods=['PUT'])
@login_required
def update_member(group_id, item_id):
    current_user_id = session.get('user_id')
    if not check_group_permission(group_id, current_user_id): return jsonify({'error': 'Denied'}), 403
    data = request.json
    vf, vu = parse_input(data.get('valid_from')), parse_input(data.get('valid_until'))
    conn = get_db(); cursor = conn.cursor()
    cursor.execute("UPDATE user_group_memberships SET valid_from=%s, valid_until=%s WHERE id=%s AND group_id=%s", (vf, vu, item_id, group_id))
    conn.commit(); conn.close()
    return jsonify({'success': True})

@user_groups_bp.route('/api/groups/<int:group_id>/subgroups/<int:item_id>', methods=['DELETE'])
@login_required
def delete_subgroup(group_id, item_id):
    current_user_id = session.get('user_id')
    if not check_group_permission(group_id, current_user_id): return jsonify({'error': 'Denied'}), 403
    conn = get_db(); cursor = conn.cursor()
    cursor.execute("DELETE FROM user_group_subgroups WHERE id=%s AND parent_group_id=%s", (item_id, group_id))
    conn.commit(); conn.close()
    return jsonify({'success': True})

@user_groups_bp.route('/api/groups/<int:group_id>/subgroups/<int:item_id>', methods=['PUT'])
@login_required
def update_subgroup(group_id, item_id):
    current_user_id = session.get('user_id')
    if not check_group_permission(group_id, current_user_id): return jsonify({'error': 'Denied'}), 403
    data = request.json
    vf, vu = parse_input(data.get('valid_from')), parse_input(data.get('valid_until'))
    conn = get_db(); cursor = conn.cursor()
    cursor.execute("UPDATE user_group_subgroups SET valid_from=%s, valid_until=%s WHERE id=%s AND parent_group_id=%s", (vf, vu, item_id, group_id))
    conn.commit(); conn.close()
    return jsonify({'success': True})

@user_groups_bp.route('/api/users/candidates', methods=['GET'])
@login_required
def list_user_candidates():
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, full_name FROM users ORDER BY id ASC")
    users = cursor.fetchall()
    cursor.close()
    conn.close()
    return jsonify({'success': True, 'users': users})

# ★★★ ここに追加しました: サブグループ新規作成＆コピーAPI ★★★
@user_groups_bp.route('/api/groups/<int:group_id>/subgroups/create_new', methods=['POST'])
@login_required
def create_and_link_subgroup(group_id):
    current_user_id = session.get('user_id')
    if not check_group_permission(group_id, current_user_id):
        return jsonify({'success': False, 'error': 'Permission Denied'}), 403

    data = request.json
    new_name = data.get('name')
    description = data.get('description', '')
    link_valid_from = parse_input(data.get('valid_from'))
    link_valid_until = parse_input(data.get('valid_until'))

    if not new_name:
        return jsonify({'success': False, 'error': 'Group name is required'}), 400

    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("SELECT id FROM user_groups WHERE name = %s", (new_name,))
        if cursor.fetchone():
            return jsonify({'success': False, 'error': 'そのグループ名は既に使用されています'}), 400

        cursor.execute("SELECT manager_user_id FROM user_groups WHERE id = %s", (group_id,))
        parent_group = cursor.fetchone()
        manager_id = parent_group['manager_user_id'] if parent_group else current_user_id

        now_jst = get_now_jst().replace(tzinfo=None)
        cursor.execute("INSERT INTO user_groups (name, description, manager_user_id, created_at) VALUES (%s, %s, %s, %s)",
                       (new_name, description, manager_id, now_jst))
        new_group_id = cursor.lastrowid

        cursor.execute("INSERT INTO user_group_subgroups (parent_group_id, child_group_id, valid_from, valid_until, created_at) VALUES (%s, %s, %s, %s, %s)",
                       (group_id, new_group_id, link_valid_from, link_valid_until, now_jst))

        cursor.execute("SELECT user_id, valid_from, valid_until FROM user_group_memberships WHERE group_id = %s", (group_id,))
        members = cursor.fetchall()

        for m in members:
            # 有効期限切れもコピーする仕様であれば、if is_valid_now... は外してください
            if is_valid_now(m['valid_from'], m['valid_until']):
                cursor.execute("INSERT INTO user_group_memberships (group_id, user_id, valid_from, valid_until, created_at) VALUES (%s, %s, %s, %s, %s)",
                               (new_group_id, m['user_id'], m['valid_from'], m['valid_until'], now_jst))

        conn.commit()
        return jsonify({'success': True})

    except Exception as e:
        conn.rollback()
        print(f"Error creating subgroup: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

# user_groups/routes.py に追加、または utils.py として切り出して import

def get_user_effective_group_ids(user_id):
    """
    ユーザーが現在有効なメンバーとして所属している全てのグループIDを取得する
    （直接所属 + サブグループの親所属による継承）
    """
    if not user_id: return []

    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    try:
        # 1. 直接所属している有効なメンバーシップを取得
        cursor.execute("""
            SELECT group_id
            FROM user_group_memberships
            WHERE user_id = %s
        """, (user_id,))
        rows = cursor.fetchall()

        # 期間チェック (Python側で行うかSQLで行うかですが、提示コードの helper を活用)
        # ここではSQLで取得した候補に対して Python helper で厳密にチェック
        active_group_ids = set()

        # もう一度詳細を取得して期間判定（またはSQLで NOW() を使う方が高速ですが、統一感を優先）
        cursor.execute("""
            SELECT group_id, valid_from, valid_until
            FROM user_group_memberships
            WHERE user_id = %s
        """, (user_id,))
        memberships = cursor.fetchall()

        for m in memberships:
            if is_valid_now(m['valid_from'], m['valid_until']):
                active_group_ids.add(m['group_id'])

        # 2. サブグループ展開 (親グループに所属していれば、その子グループにも所属とみなす仕様の場合)
        # ※ もし「サブグループのメンバーは親グループのメンバーでもある」ではなく、
        #    「親グループのメンバーはサブグループの権限も持つ」という仕様であれば、
        #    以下のようにトップダウンで探索します。

        # 探索用キュー
        queue = list(active_group_ids)
        visited = set(active_group_ids)

        while queue:
            parent_id = queue.pop(0)

            # この親グループに紐付いている有効なサブグループを取得
            cursor.execute("""
                SELECT child_group_id, valid_from, valid_until
                FROM user_group_subgroups
                WHERE parent_group_id = %s
            """, (parent_id,))
            links = cursor.fetchall()

            for link in links:
                if is_valid_now(link['valid_from'], link['valid_until']):
                    child_id = link['child_group_id']
                    if child_id not in visited:
                        visited.add(child_id)
                        queue.append(child_id)
                        active_group_ids.add(child_id)

        return list(active_group_ids)

    finally:
        cursor.close()
        conn.close()

@user_groups_bp.route('/api/groups/<int:group_id>', methods=['DELETE'])
@login_required
def delete_group(group_id):
    """グループ削除：総管理者またはそのグループの管理者のみ。
    メンバーシップとサブグループ関係（親側・子側とも）を同時に削除する。
    サブグループとしてぶら下がっていた子グループ自体は削除せず、独立したグループとして残る。
    """
    current_user_id = session.get('user_id')
    if not check_group_permission(group_id, current_user_id):
        return jsonify({'success': False, 'error': 'Permission Denied'}), 403

    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id, name FROM user_groups WHERE id = %s", (group_id,))
        group = cursor.fetchone()
        if not group:
            return jsonify({'success': False, 'error': 'Group not found'}), 404

        # 関連レコードの削除
        cursor.execute(
            "DELETE FROM user_group_memberships WHERE group_id = %s",
            (group_id,)
        )
        cursor.execute(
            "DELETE FROM user_group_subgroups WHERE parent_group_id = %s OR child_group_id = %s",
            (group_id, group_id)
        )
        # グループ本体の削除
        cursor.execute("DELETE FROM user_groups WHERE id = %s", (group_id,))

        conn.commit()
        return jsonify({'success': True, 'deleted': group['name']})
    except Exception as e:
        conn.rollback()
        print(f"Error deleting group: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@user_groups_bp.route('/api/groups/<int:group_id>/rename', methods=['PUT'])
@login_required
def rename_group(group_id):
    current_user_id = session.get('user_id')
    if not check_group_permission(group_id, current_user_id):
        return jsonify({'success': False, 'error': 'Permission Denied'}), 403

    data = request.json
    new_name = (data.get('name') or '').strip()
    if not new_name:
        return jsonify({'success': False, 'error': 'グループ名は必須です'}), 400

    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id FROM user_groups WHERE name = %s AND id != %s", (new_name, group_id))
        if cursor.fetchone():
            return jsonify({'success': False, 'error': 'そのグループ名は既に使用されています'}), 400

        cursor.execute("UPDATE user_groups SET name = %s WHERE id = %s", (new_name, group_id))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@user_groups_bp.route('/return_to_fujin')
@login_required
def return_to_fujin():
    """FUJINダッシュボードに戻る"""
    return redirect_to_dashboard()