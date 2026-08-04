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

# table_master/routes_project.py
# プロジェクト機能

import logging
from datetime import datetime, timedelta, timezone
import mysql.connector
from flask import request, jsonify, render_template, session
from decorators import login_required
from db import DatabaseConfig
from . import table_master_bp
from .routes import check_edit_permission, is_safe_identifier

PROJECTS_TABLE  = "table_master_projects"
PROJECT_TABLES  = "table_master_project_tables"
PROJECT_VIEWS   = "table_master_project_views"
VIEWS_TABLE     = "table_master_views"
PROJECT_ACCESS  = "table_master_project_access_groups"


# ============================================================
# 権限ヘルパー
# ============================================================

def _is_admin():
    return session.get('user_category', '') == 'admin'


def _get_user_group_ids(user_id):
    """ユーザーの有効グループIDリストを返す (JST対応)"""
    if not user_id:
        return []
    try:
        JST     = timezone(timedelta(hours=9), 'JST')
        now_jst = datetime.now(JST).replace(tzinfo=None)
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT group_id FROM user_group_memberships
            WHERE user_id = %s
              AND (valid_from  IS NULL OR valid_from  <= %s)
              AND (valid_until IS NULL OR valid_until >= %s)
        """, (user_id, now_jst, now_jst))
        rows = cursor.fetchall()
        return [r['group_id'] for r in rows]
    except Exception as e:
        logging.error("_get_user_group_ids error: %s", e)
        return []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


def _build_project_permission_clause(user_id):
    """
    projects_list の WHERE 句フィルタを生成。
    access_policy / PROJECT_ACCESS テーブルが存在することを前提とする。
    """
    if _is_admin():
        return "1=1"

    user_category = session.get('user_category', '')
    group_ids     = _get_user_group_ids(user_id)
    uid           = int(user_id) if user_id else 0

    domestic_ok = "p.access_policy = 'domestic'" if user_category == 'regular' else "1=0"

    if group_ids:
        g_str       = ", ".join(map(str, group_ids))
        group_check = (
            f"EXISTS ("
            f"SELECT 1 FROM `{PROJECT_ACCESS}` pag "
            f"WHERE pag.project_id = p.id AND pag.group_id IN ({g_str})"
            f")"
        )
    else:
        group_check = "1=0"

    return (
        f"(p.access_policy = 'public' "
        f"OR p.created_by = {uid} "
        f"OR ({domestic_ok}) "
        f"OR (p.access_policy = 'group' AND {group_check}))"
    )


def _can_view_project(project, user_id):
    """プロジェクトの閲覧権限チェック"""
    if _is_admin():
        return True
    policy = project.get('access_policy') or 'public'
    uid    = int(user_id) if user_id else 0
    if project.get('created_by') == uid:
        return True
    if policy == 'public':
        return True
    if policy == 'domestic':
        return session.get('user_category') in ('regular', 'admin')
    if policy == 'private':
        return False
    if policy == 'group':
        user_groups    = set(_get_user_group_ids(user_id))
        allowed_groups = set(project.get('allowed_group_ids', []))
        return bool(user_groups & allowed_groups)
    return False


def _check_project_access(project_id, user_id, cursor):
    """project_id のアクセス権確認。閲覧可なら project dict、不可なら None。"""
    cursor.execute(f"""
        SELECT p.*, COALESCE(p.access_policy,'public') AS access_policy
        FROM `{PROJECTS_TABLE}` p WHERE p.id = %s
    """, (project_id,))
    project = cursor.fetchone()
    if not project:
        return None
    cursor.execute(
        f"SELECT group_id FROM `{PROJECT_ACCESS}` WHERE project_id=%s", (project_id,)
    )
    project['allowed_group_ids'] = [r['group_id'] for r in cursor.fetchall()]
    return project if _can_view_project(project, user_id) else None


# ============================================================
# ページルート
# ============================================================

@table_master_bp.route('/projects/')
@login_required
def project_list_page():
    return render_template('tm_project_list.html')


@table_master_bp.route('/projects/public')
@login_required
def project_list_public():
    return render_template('tm_project_list_public.html')


@table_master_bp.route('/projects/<int:project_id>')
@login_required
def project_detail_page(project_id):
    return render_template('tm_project_detail.html', project_id=project_id)


# ============================================================
# プロジェクト一覧 API
# ============================================================

@table_master_bp.route('/projects/list', methods=['GET'])
@login_required
def projects_list():
    user_id = session.get('user_id')
    try:
        perm_clause = _build_project_permission_clause(user_id)
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(f"""
            SELECT p.id, p.name, p.description, p.created_by,
                   p.created_at, p.updated_at,
                   p.access_policy,
                   u.full_name AS created_by_name,
                   COUNT(DISTINCT pt.id) AS table_count,
                   COUNT(DISTINCT pv.id) AS view_count
            FROM `{PROJECTS_TABLE}` p
            LEFT JOIN users u ON p.created_by = u.id
            LEFT JOIN `{PROJECT_TABLES}` pt ON pt.project_id = p.id
            LEFT JOIN `{PROJECT_VIEWS}` pv ON pv.project_id = p.id
            WHERE {perm_clause}
            GROUP BY p.id
            ORDER BY p.updated_at DESC
        """)
        rows = cursor.fetchall()
        for r in rows:
            for k in ('created_at', 'updated_at'):
                if r[k]: r[k] = r[k].isoformat()
        return jsonify({'success': True, 'projects': rows})
    except Exception as e:
        logging.error("projects_list error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ============================================================
# プロジェクト保存・削除
# ============================================================

@table_master_bp.route('/projects/save', methods=['POST'])
@login_required
def projects_save():
    user_id = session.get('user_id')
    if not check_edit_permission(user_id):
        return jsonify({'success': False, 'error': '編集権限がありません'}), 403
    data        = request.json
    project_id  = data.get('id')
    name        = (data.get('name') or '').strip()
    description = (data.get('description') or '').strip()
    if not name:
        return jsonify({'success': False, 'error': 'プロジェクト名は必須です'}), 400
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        if project_id:
            cursor.execute(
                f"UPDATE `{PROJECTS_TABLE}` SET name=%s, description=%s WHERE id=%s",
                (name, description, project_id))
            msg = 'プロジェクトを更新しました'
        else:
            cursor.execute(
                f"INSERT INTO `{PROJECTS_TABLE}` (name, description, created_by) VALUES (%s,%s,%s)",
                (name, description, user_id))
            project_id = cursor.lastrowid
            msg = 'プロジェクトを作成しました'
        conn.commit()
        return jsonify({'success': True, 'message': msg, 'id': project_id})
    except mysql.connector.IntegrityError:
        return jsonify({'success': False, 'error': f'「{name}」は既に使われています'}), 409
    except Exception as e:
        logging.error("projects_save error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@table_master_bp.route('/projects/delete', methods=['POST'])
@login_required
def projects_delete():
    user_id = session.get('user_id')
    if not check_edit_permission(user_id):
        return jsonify({'success': False, 'error': '編集権限がありません'}), 403
    project_id = request.json.get('id')
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute(f"DELETE FROM `{PROJECTS_TABLE}` WHERE id=%s", (project_id,))
        conn.commit()
        return jsonify({'success': True, 'message': 'プロジェクトを削除しました'})
    except Exception as e:
        logging.error("projects_delete error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ============================================================
# プロジェクト詳細取得
# ============================================================

@table_master_bp.route('/projects/<int:project_id>/detail', methods=['GET'])
@login_required
def project_detail(project_id):
    user_id = session.get('user_id')
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        # プロジェクト取得
        cursor.execute(f"""
            SELECT p.*, u.full_name AS created_by_name
            FROM `{PROJECTS_TABLE}` p
            LEFT JOIN users u ON p.created_by = u.id
            WHERE p.id = %s
        """, (project_id,))
        project = cursor.fetchone()
        if not project:
            return jsonify({'success': False, 'error': 'プロジェクトが見つかりません'}), 404

        # グループ情報付きでアクセス権チェック
        cursor.execute(
            f"SELECT group_id FROM `{PROJECT_ACCESS}` WHERE project_id=%s", (project_id,)
        )
        project['allowed_group_ids'] = [r['group_id'] for r in cursor.fetchall()]
        if not _can_view_project(project, user_id):
            return jsonify({'success': False, 'error': '閲覧権限がありません'}), 403

        for k in ('created_at', 'updated_at'):
            if project.get(k): project[k] = project[k].isoformat()

        # ビュー一覧（sort_order 付き）
        cursor.execute(f"""
            SELECT pv.id AS link_id, v.id AS view_id,
                   v.view_name, v.database_name AS source_database,
                   v.sql_query AS query_text, v.description,
                   (pv.html_content IS NOT NULL AND pv.html_content != '') AS has_html,
                   COALESCE(pv.sort_order, 0.500000) AS sort_order
            FROM `{PROJECT_VIEWS}` pv
            JOIN `{VIEWS_TABLE}` v ON pv.view_id = v.id
            WHERE pv.project_id = %s
            ORDER BY COALESCE(pv.sort_order, 0.500000), v.view_name
        """, (project_id,))
        views = cursor.fetchall()

        # テーブル一覧
        cursor.execute(f"""
            SELECT id, database_name, table_name, added_at
            FROM `{PROJECT_TABLES}`
            WHERE project_id = %s
            ORDER BY database_name, table_name
        """, (project_id,))
        tables = cursor.fetchall()
        for t in tables:
            if t.get('added_at'): t['added_at'] = t['added_at'].isoformat()

        return jsonify({'success': True, 'project': project, 'views': views, 'tables': tables})
    except Exception as e:
        logging.error("project_detail error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ============================================================
# 利用可能ビュー・ビュー追加/削除
# ============================================================

@table_master_bp.route('/projects/<int:project_id>/available_views', methods=['GET'])
@login_required
def project_available_views(project_id):
    user_id = session.get('user_id')
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        if _check_project_access(project_id, user_id, cursor) is None:
            return jsonify({'success': False, 'error': '閲覧権限がありません'}), 403
        cursor.execute(f"""
            SELECT v.id, v.view_name, v.database_name AS source_database,
                   v.sql_query AS query_text, v.description
            FROM `{VIEWS_TABLE}` v
            WHERE v.id NOT IN (
                SELECT view_id FROM `{PROJECT_VIEWS}` WHERE project_id = %s
            )
            ORDER BY v.view_name
        """, (project_id,))
        return jsonify({'success': True, 'views': cursor.fetchall()})
    except Exception as e:
        logging.error("project_available_views error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@table_master_bp.route('/projects/<int:project_id>/add_view', methods=['POST'])
@login_required
def project_add_view(project_id):
    user_id = session.get('user_id')
    if not check_edit_permission(user_id):
        return jsonify({'success': False, 'error': '編集権限がありません'}), 403
    view_id = request.json.get('view_id')
    if not view_id:
        return jsonify({'success': False, 'error': 'view_idが必要です'}), 400
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute(
            f"INSERT IGNORE INTO `{PROJECT_VIEWS}` (project_id, view_id) VALUES (%s,%s)",
            (project_id, view_id))
        conn.commit()
        return jsonify({'success': True, 'message': 'ビューを追加しました'})
    except Exception as e:
        logging.error("project_add_view error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@table_master_bp.route('/projects/<int:project_id>/remove_view', methods=['POST'])
@login_required
def project_remove_view(project_id):
    user_id = session.get('user_id')
    if not check_edit_permission(user_id):
        return jsonify({'success': False, 'error': '編集権限がありません'}), 403
    link_id = request.json.get('link_id')
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute(
            f"DELETE FROM `{PROJECT_VIEWS}` WHERE id=%s AND project_id=%s",
            (link_id, project_id))
        conn.commit()
        return jsonify({'success': True, 'message': 'ビューを外しました'})
    except Exception as e:
        logging.error("project_remove_view error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ============================================================
# スキーマ取得
# ============================================================

@table_master_bp.route('/projects/<int:project_id>/schema', methods=['GET'])
@login_required
def project_schema(project_id):
    user_id = session.get('user_id')
    try:
        conn_default   = mysql.connector.connect(**DatabaseConfig.default())
        cursor_default = conn_default.cursor(dictionary=True)
        if _check_project_access(project_id, user_id, cursor_default) is None:
            return jsonify({'success': False, 'error': '閲覧権限がありません'}), 403
        cursor_default.execute(f"""
            SELECT database_name, table_name FROM `{PROJECT_TABLES}`
            WHERE project_id = %s ORDER BY database_name, table_name
        """, (project_id,))
        tables = cursor_default.fetchall()
        if not tables:
            return jsonify({'success': True, 'schema': '（テーブルが登録されていません）', 'table_count': 0})

        schema_parts = []
        current_db   = None
        conn_target  = None
        for t in tables:
            db_name  = t['database_name']
            tbl_name = t['table_name']
            if not is_safe_identifier(db_name) or not is_safe_identifier(tbl_name):
                continue
            if db_name != current_db:
                if conn_target and conn_target.is_connected(): conn_target.close()
                conn_target = mysql.connector.connect(**DatabaseConfig.get_config(db_name))
                current_db  = db_name
            try:
                cur = conn_target.cursor()
                cur.execute(f"SHOW CREATE TABLE `{tbl_name}`")
                row = cur.fetchone()
                if row:
                    schema_parts.append(
                        f"-- ============================================================\n"
                        f"-- {db_name}.{tbl_name}\n"
                        f"-- ============================================================\n"
                        f"{row[1]};\n"
                    )
            except Exception as e:
                schema_parts.append(f"-- ERROR: {db_name}.{tbl_name}: {e}\n")
        if conn_target and conn_target.is_connected(): conn_target.close()
        return jsonify({'success': True, 'schema': "\n".join(schema_parts),
                        'table_count': len(schema_parts)})
    except Exception as e:
        logging.error("project_schema error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn_default' in locals() and conn_default.is_connected():
            cursor_default.close(); conn_default.close()


# ============================================================
# テーブル追加・削除
# ============================================================

@table_master_bp.route('/projects/<int:project_id>/add_table', methods=['POST'])
@login_required
def project_add_table(project_id):
    user_id = session.get('user_id')
    if not check_edit_permission(user_id):
        return jsonify({'success': False, 'error': '編集権限がありません'}), 403
    data    = request.json
    db_name = (data.get('database_name') or '').strip()
    tbl     = (data.get('table_name') or '').strip()
    if not db_name or not tbl:
        return jsonify({'success': False, 'error': 'パラメータ不足'}), 400
    if not is_safe_identifier(db_name) or not is_safe_identifier(tbl):
        return jsonify({'success': False, 'error': '不正な識別子'}), 400
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute(f"""
            INSERT IGNORE INTO `{PROJECT_TABLES}` (project_id, database_name, table_name)
            VALUES (%s, %s, %s)
        """, (project_id, db_name, tbl))
        conn.commit()
        return jsonify({'success': True, 'message': f'{tbl} を追加しました'})
    except Exception as e:
        logging.error("project_add_table error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@table_master_bp.route('/projects/<int:project_id>/remove_table', methods=['POST'])
@login_required
def project_remove_table(project_id):
    user_id = session.get('user_id')
    if not check_edit_permission(user_id):
        return jsonify({'success': False, 'error': '編集権限がありません'}), 403
    entry_id = request.json.get('id')
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute(
            f"DELETE FROM `{PROJECT_TABLES}` WHERE id=%s AND project_id=%s",
            (entry_id, project_id))
        conn.commit()
        return jsonify({'success': True, 'message': 'テーブルを外しました'})
    except Exception as e:
        logging.error("project_remove_table error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ============================================================
# ビューHTML取得・保存
# ============================================================

@table_master_bp.route('/projects/<int:project_id>/views/<int:link_id>/html', methods=['GET'])
@login_required
def project_view_get_html(project_id, link_id):
    user_id = session.get('user_id')
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        if _check_project_access(project_id, user_id, cursor) is None:
            return jsonify({'success': False, 'error': '閲覧権限がありません'}), 403
        cursor.execute(
            f"SELECT html_content FROM `{PROJECT_VIEWS}` WHERE id=%s AND project_id=%s",
            (link_id, project_id))
        row = cursor.fetchone()
        if not row:
            return jsonify({'success': False, 'error': '見つかりません'}), 404
        html = row['html_content']
        if isinstance(html, (bytes, bytearray)):
            html = html.decode('utf-8', errors='replace')
        return jsonify({'success': True, 'html_content': html})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@table_master_bp.route('/projects/<int:project_id>/views/<int:link_id>/html', methods=['POST'])
@login_required
def project_view_save_html(project_id, link_id):
    user_id = session.get('user_id')
    if not check_edit_permission(user_id):
        return jsonify({'success': False, 'error': '編集権限がありません'}), 403
    html_content = request.json.get('html_content', '')
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        if _check_project_access(project_id, user_id, cursor) is None:
            return jsonify({'success': False, 'error': '閲覧権限がありません'}), 403
        cursor = conn.cursor()
        cursor.execute(f"""
            UPDATE `{PROJECT_VIEWS}` SET html_content=%s
            WHERE id=%s AND project_id=%s
        """, (html_content, link_id, project_id))
        conn.commit()
        return jsonify({'success': True, 'message': 'HTMLを保存しました'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ============================================================
# 全ビューHTML一括取得
# ============================================================

@table_master_bp.route('/projects/<int:project_id>/all_html', methods=['GET'])
@login_required
def project_all_html(project_id):
    user_id = session.get('user_id')
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        project = _check_project_access(project_id, user_id, cursor)
        if project is None:
            return jsonify({'success': False, 'error': '閲覧権限がありません'}), 403
        cursor.execute(f"""
            SELECT pv.id AS link_id, v.view_name, v.description,
                   pv.html_content,
                   (pv.html_content IS NOT NULL AND pv.html_content != '') AS has_html
            FROM `{PROJECT_VIEWS}` pv
            JOIN `{VIEWS_TABLE}` v ON pv.view_id = v.id
            WHERE pv.project_id = %s ORDER BY COALESCE(pv.sort_order, 0.5), v.view_name
        """, (project_id,))
        views = cursor.fetchall()
        for v in views:
            if isinstance(v.get('html_content'), (bytes, bytearray)):
                v['html_content'] = v['html_content'].decode('utf-8', errors='replace')
        return jsonify({'success': True, 'project_name': project['name'], 'views': views})
    except Exception as e:
        logging.error("project_all_html error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ============================================================
# アクセス権設定
# ============================================================

@table_master_bp.route('/projects/<int:project_id>/access', methods=['GET'])
@login_required
def project_get_access(project_id):
    user_id = session.get('user_id')
    if not check_edit_permission(user_id):
        return jsonify({'success': False, 'error': '編集権限がありません'}), 403
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            f"SELECT access_policy FROM `{PROJECTS_TABLE}` WHERE id=%s", (project_id,))
        row = cursor.fetchone()
        if not row:
            return jsonify({'success': False, 'error': 'プロジェクトが見つかりません'}), 404
        cursor.execute(f"SELECT group_id FROM `{PROJECT_ACCESS}` WHERE project_id=%s", (project_id,))
        group_ids = [r['group_id'] for r in cursor.fetchall()]
        cursor.execute("SELECT id, name FROM user_groups ORDER BY id")
        all_groups = cursor.fetchall()
        return jsonify({'success': True, 'policy': row['access_policy'] or 'public',
                        'group_ids': group_ids, 'all_groups': all_groups})
    except Exception as e:
        logging.error("project_get_access error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@table_master_bp.route('/projects/<int:project_id>/access', methods=['POST'])
@login_required
def project_save_access(project_id):
    user_id = session.get('user_id')
    if not check_edit_permission(user_id):
        return jsonify({'success': False, 'error': '編集権限がありません'}), 403
    data      = request.json
    policy    = data.get('policy', 'public')
    group_ids = [int(g) for g in data.get('group_ids', []) if str(g).isdigit()]
    if policy not in {'public', 'domestic', 'private', 'group'}:
        policy = 'public'
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE `{PROJECTS_TABLE}` SET access_policy=%s WHERE id=%s", (policy, project_id))
        cursor.execute(f"DELETE FROM `{PROJECT_ACCESS}` WHERE project_id=%s", (project_id,))
        if policy == 'group' and group_ids:
            cursor.executemany(
                f"INSERT IGNORE INTO `{PROJECT_ACCESS}` (project_id, group_id) VALUES (%s,%s)",
                [(project_id, gid) for gid in group_ids])
        conn.commit()
        return jsonify({'success': True, 'message': 'アクセス権を保存しました'})
    except Exception as e:
        logging.error("project_save_access error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ============================================================
# sort_order 更新・正規化
# ============================================================

@table_master_bp.route('/projects/<int:project_id>/views/<int:link_id>/sort', methods=['POST'])
@login_required
def project_view_set_sort(project_id, link_id):
    user_id = session.get('user_id')
    if not check_edit_permission(user_id):
        return jsonify({'success': False, 'error': '編集権限がありません'}), 403
    try:
        v = float(request.json.get('sort_order'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': '数値を入力してください'}), 400
    v = max(0.0, min(0.999999, v))
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE `{PROJECT_VIEWS}` SET sort_order=%s WHERE id=%s AND project_id=%s",
            (round(v, 6), link_id, project_id))
        conn.commit()
        return jsonify({'success': True, 'sort_order': round(v, 6)})
    except Exception as e:
        logging.error("project_view_set_sort error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@table_master_bp.route('/projects/<int:project_id>/views/normalize_sort', methods=['POST'])
@login_required
def project_views_normalize_sort(project_id):
    user_id = session.get('user_id')
    if not check_edit_permission(user_id):
        return jsonify({'success': False, 'error': '編集権限がありません'}), 403
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(f"""
            SELECT pv.id FROM `{PROJECT_VIEWS}` pv
            JOIN `{VIEWS_TABLE}` v ON pv.view_id = v.id
            WHERE pv.project_id = %s
            ORDER BY COALESCE(pv.sort_order, 0.500000), v.view_name
        """, (project_id,))
        rows = cursor.fetchall()
        n = len(rows)
        if n == 0:
            return jsonify({'success': True, 'message': 'ビューがありません'})
        step = round(1.0 / (n + 1), 6)
        for i, row in enumerate(rows):
            cursor.execute(
                f"UPDATE `{PROJECT_VIEWS}` SET sort_order=%s WHERE id=%s",
                (round(step * (i + 1), 6), row['id']))
        conn.commit()
        return jsonify({'success': True, 'message': f'{n}件を等間隔に正規化しました', 'step': step})
    except Exception as e:
        logging.error("project_views_normalize_sort error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()