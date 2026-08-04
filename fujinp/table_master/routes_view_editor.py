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

# table_master/routes_view_editor.py
# ビューエディタ - 保存済みSQLクエリ（ビュー定義）の管理

import logging
import mysql.connector
from flask import request, jsonify, render_template, session
from decorators import login_required
from db import DatabaseConfig
from . import table_master_bp
from .routes import check_edit_permission, is_safe_identifier, run_readonly_query

VIEWS_TABLE = "table_master_views"


# ============================================================
# ビューエディタ 画面
# ============================================================

@table_master_bp.route('/view_editor/')
@login_required
def view_editor():
    """ビューエディタ メイン画面"""
    return render_template('view_editor.html')


# ============================================================
# ビュー一覧取得
# ============================================================

@table_master_bp.route('/view_editor/list', methods=['GET'])
@login_required
def view_editor_list():
    """保存済みビュー一覧を返す"""
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(f"""
            SELECT v.id, v.view_name, v.view_name AS display_name,
                   v.description, v.database_name AS source_database,
                   v.sql_query AS query_text,
                   v.created_by, v.created_at, v.updated_at,
                   u.full_name AS created_by_name
            FROM `{VIEWS_TABLE}` v
            LEFT JOIN users u ON v.created_by = u.id
            ORDER BY v.updated_at DESC
        """)
        views = cursor.fetchall()
        for v in views:
            for k in ('created_at', 'updated_at'):
                if v[k]:
                    v[k] = v[k].isoformat()
        return jsonify({'success': True, 'views': views})
    except Exception as e:
        logging.error("view_editor_list error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ============================================================
# ビュー保存（新規 / 更新）
# ============================================================

@table_master_bp.route('/view_editor/save', methods=['POST'])
@login_required
def view_editor_save():
    """ビュー定義を保存（INSERT or UPDATE）"""
    user_id = session.get('user_id')
    if not check_edit_permission(user_id):
        return jsonify({'success': False, 'error': '編集権限がありません'}), 403

    data = request.json
    view_id       = data.get('id')           # None = 新規
    view_name     = (data.get('view_name') or '').strip()
    display_name  = (data.get('display_name') or '').strip()
    description   = (data.get('description') or '').strip()
    source_db     = (data.get('source_database') or '').strip()
    query_text    = (data.get('query_text') or '').strip()

    if not view_name or not source_db or not query_text:
        return jsonify({'success': False, 'error': '必須項目が未入力です'}), 400
    if not is_safe_identifier(view_name) or not is_safe_identifier(source_db):
        return jsonify({'success': False, 'error': '不正な識別子が含まれています'}), 400

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        if view_id:
            cursor.execute(f"""
                UPDATE `{VIEWS_TABLE}`
                SET view_name=%s, description=%s,
                    database_name=%s, sql_query=%s
                WHERE id=%s
            """, (view_name, description, source_db, query_text, view_id))
            msg = 'ビューを更新しました'
        else:
            cursor.execute(f"""
                INSERT INTO `{VIEWS_TABLE}`
                    (view_name, description, database_name, sql_query, created_by)
                VALUES (%s, %s, %s, %s, %s)
            """, (view_name, description, source_db, query_text, user_id))
            view_id = cursor.lastrowid
            msg = 'ビューを保存しました'

        conn.commit()
        return jsonify({'success': True, 'message': msg, 'id': view_id})

    except mysql.connector.IntegrityError:
        return jsonify({'success': False, 'error': f'ビュー名「{view_name}」は既に使われています'}), 409
    except Exception as e:
        logging.error("view_editor_save error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ============================================================
# ビュー削除
# ============================================================

@table_master_bp.route('/view_editor/delete', methods=['POST'])
@login_required
def view_editor_delete():
    """ビュー定義を削除"""
    user_id = session.get('user_id')
    if not check_edit_permission(user_id):
        return jsonify({'success': False, 'error': '編集権限がありません'}), 403

    view_id = request.json.get('id')
    if not view_id:
        return jsonify({'success': False, 'error': 'IDが指定されていません'}), 400

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute(f"DELETE FROM `{VIEWS_TABLE}` WHERE id=%s", (view_id,))
        conn.commit()
        return jsonify({'success': True, 'message': 'ビューを削除しました'})
    except Exception as e:
        logging.error("view_editor_delete error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ============================================================
# ビュープレビュー（SQLを実行して結果を返す）
# ============================================================

@table_master_bp.route('/view_editor/preview', methods=['POST'])
@login_required
def view_editor_preview():
    """ビュー定義のSQLを実行してプレビュー結果を返す"""
    data = request.json
    source_db  = (data.get('source_database') or '').strip()
    query_text = (data.get('query_text') or '').strip()

    if not source_db or not query_text:
        return jsonify({'success': False, 'error': 'パラメータ不足'}), 400

    # 共通クエリ実行ヘルパーに委譲
    return run_readonly_query(source_db, query_text, user_id=session.get('user_id'), record_history=False)
