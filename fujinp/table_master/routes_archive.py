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

# table_master/routes_archive.py
# アーカイブ装置 - クエリ結果のHTML保存と公開URL管理

import logging
import mysql.connector
from flask import request, jsonify, render_template, session
from decorators import login_required
from db import DatabaseConfig
from . import table_master_bp
from .routes import check_edit_permission, run_readonly_query

ARCHIVES_TABLE = "table_master_archives"
VIEWS_TABLE    = "table_master_views"


# ============================================================
# アーカイブ管理画面（admin専用）
# ============================================================

@table_master_bp.route('/archive/')
@login_required
def archive_manager():
    """アーカイブ管理画面"""
    return render_template('archive_manager.html')


# ============================================================
# 公開アーカイブ表示（ログイン不要）
# /table_master/archive/1  → テーブル第1号
# ============================================================

@table_master_bp.route('/archive/<int:archive_number>')
def archive_view(archive_number):
    """公開アーカイブを表示（レンダリング済みHTML）"""
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        # まず存在確認（公開・非公開を問わず取得）
        cursor.execute(f"""
            SELECT a.*, u.full_name AS created_by_name
            FROM `{ARCHIVES_TABLE}` a
            LEFT JOIN users u ON a.created_by = u.id
            WHERE a.archive_number = %s
        """, (archive_number,))
        archive = cursor.fetchone()

        if not archive:
            return render_template(
                'archive_view.html',
                archive=None,
                error='このテーブルは存在しません。',
                status_code=404,
                page_title='テーブルが見つかりません'
            ), 404

        if not archive.get('is_public'):
            return render_template(
                'archive_view.html',
                archive=None,
                error='このテーブルは存在しますが、現在は非公開に設定されています。',
                status_code=403,
                page_title='このテーブルは非公開です'
            ), 403

        if archive['created_at']:
            archive['created_at'] = archive['created_at'].isoformat()
        if archive['updated_at']:
            archive['updated_at'] = archive['updated_at'].isoformat()

        return render_template(
            'archive_view.html',
            archive=archive,
            error=None,
            status_code=200,
            page_title=f"テーブル第{archive['archive_number']}号 - {archive['title']}"
        )

    except Exception as e:
        logging.error("archive_view error: %s", e)
        return render_template(
            'archive_view.html',
            archive=None,
            error='エラーが発生しました。',
            status_code=500,
            page_title='エラー'
        ), 500

    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


# ============================================================
# アーカイブ一覧取得
# ============================================================

@table_master_bp.route('/archive/list', methods=['GET'])
@login_required
def archive_list():
    """アーカイブ一覧を返す（管理用 = 非公開含む）"""
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(f"""
            SELECT a.id, a.archive_number, a.title, a.description,
                   a.source_database, a.source_view_id, a.is_public,
                   a.created_by, a.created_at, a.updated_at,
                   u.full_name AS created_by_name,
                   v.view_name AS source_view_name,
                   CHAR_LENGTH(a.html_content) AS html_length
            FROM `{ARCHIVES_TABLE}` a
            LEFT JOIN users u ON a.created_by = u.id
            LEFT JOIN `{VIEWS_TABLE}` v ON a.source_view_id = v.id
            ORDER BY a.archive_number ASC
        """)
        rows = cursor.fetchall()
        for r in rows:
            for k in ('created_at', 'updated_at'):
                if r[k]: r[k] = r[k].isoformat()
        return jsonify({'success': True, 'archives': rows})
    except Exception as e:
        logging.error("archive_list error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ============================================================
# アーカイブ取得（1件）
# ============================================================

@table_master_bp.route('/archive/get', methods=['GET'])
@login_required
def archive_get():
    """アーカイブ1件の詳細（html_content含む）を返す"""
    archive_id = request.args.get('id')
    if not archive_id:
        return jsonify({'success': False, 'error': 'IDが指定されていません'}), 400
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(f"""
            SELECT * FROM `{ARCHIVES_TABLE}` WHERE id = %s
        """, (archive_id,))
        row = cursor.fetchone()
        if not row:
            return jsonify({'success': False, 'error': 'アーカイブが見つかりません'}), 404
        for k in ('created_at', 'updated_at'):
            if row[k]: row[k] = row[k].isoformat()
        return jsonify({'success': True, 'archive': row})
    except Exception as e:
        logging.error("archive_get error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ============================================================
# アーカイブ保存（新規 / 更新）
# ============================================================

@table_master_bp.route('/archive/save', methods=['POST'])
@login_required
def archive_save():
    """アーカイブを保存（新規 or 更新）"""
    user_id = session.get('user_id')
    if not check_edit_permission(user_id):
        return jsonify({'success': False, 'error': '編集権限がありません'}), 403

    data = request.json
    archive_id     = data.get('id')            # None = 新規
    title          = (data.get('title') or '').strip()
    description    = (data.get('description') or '').strip()
    source_view_id = data.get('source_view_id')  # None = アドホック
    source_db      = (data.get('source_database') or '').strip()
    source_query   = (data.get('source_query') or '').strip()
    html_content   = (data.get('html_content') or '').strip()
    is_public      = 1 if data.get('is_public', True) else 0

    if not title or not html_content:
        return jsonify({'success': False, 'error': 'タイトルとHTMLコンテンツは必須です'}), 400

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        if archive_id:
            # 更新
            cursor.execute(f"""
                UPDATE `{ARCHIVES_TABLE}`
                SET title=%s, description=%s,
                    source_view_id=%s, source_database=%s, source_query=%s,
                    html_content=%s, is_public=%s
                WHERE id=%s
            """, (title, description, source_view_id, source_db, source_query,
                  html_content, is_public, archive_id))
            conn.commit()

            cursor.execute(f"SELECT archive_number FROM `{ARCHIVES_TABLE}` WHERE id=%s", (archive_id,))
            row = cursor.fetchone()
            number = row['archive_number'] if row else None
            return jsonify({'success': True, 'message': 'アーカイブを更新しました',
                            'id': archive_id, 'archive_number': number})
        else:
            # 新規: archive_number は MAX+1
            cursor.execute(f"SELECT COALESCE(MAX(archive_number), 0)+1 AS next_num FROM `{ARCHIVES_TABLE}`")
            next_num = cursor.fetchone()['next_num']

            cursor.execute(f"""
                INSERT INTO `{ARCHIVES_TABLE}`
                    (archive_number, title, description,
                     source_view_id, source_database, source_query,
                     html_content, is_public, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (next_num, title, description,
                  source_view_id, source_db, source_query,
                  html_content, is_public, user_id))
            conn.commit()
            new_id = cursor.lastrowid
            return jsonify({'success': True, 'message': f'テーブル第{next_num}号として保存しました',
                            'id': new_id, 'archive_number': next_num})

    except Exception as e:
        logging.error("archive_save error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ============================================================
# アーカイブ削除
# ============================================================

@table_master_bp.route('/archive/delete', methods=['POST'])
@login_required
def archive_delete():
    """アーカイブを削除"""
    user_id = session.get('user_id')
    if not check_edit_permission(user_id):
        return jsonify({'success': False, 'error': '編集権限がありません'}), 403

    archive_id = request.json.get('id')
    if not archive_id:
        return jsonify({'success': False, 'error': 'IDが指定されていません'}), 400

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute(f"DELETE FROM `{ARCHIVES_TABLE}` WHERE id=%s", (archive_id,))
        conn.commit()
        return jsonify({'success': True, 'message': 'アーカイブを削除しました'})
    except Exception as e:
        logging.error("archive_delete error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ============================================================
# 公開/非公開切替
# ============================================================

@table_master_bp.route('/archive/toggle_public', methods=['POST'])
@login_required
def archive_toggle_public():
    """アーカイブの公開/非公開を切り替える"""
    user_id = session.get('user_id')
    if not check_edit_permission(user_id):
        return jsonify({'success': False, 'error': '編集権限がありません'}), 403

    data = request.json
    archive_id = data.get('id')
    is_public  = 1 if data.get('is_public') else 0

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute(f"UPDATE `{ARCHIVES_TABLE}` SET is_public=%s WHERE id=%s", (is_public, archive_id))
        conn.commit()
        status = '公開' if is_public else '非公開'
        return jsonify({'success': True, 'message': f'{status}に変更しました'})
    except Exception as e:
        logging.error("archive_toggle_public error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ============================================================
# ビュー再実行（アーカイブ更新用）
# ============================================================

@table_master_bp.route('/archive/rerun', methods=['POST'])
@login_required
def archive_rerun():
    """保存済みクエリを再実行してHTMLデータを取得"""
    data = request.json
    source_db    = (data.get('source_database') or '').strip()
    source_query = (data.get('source_query') or '').strip()

    if not source_db or not source_query:
        return jsonify({'success': False, 'error': 'パラメータ不足'}), 400

    return run_readonly_query(source_db, source_query, user_id=session.get('user_id'), record_history=False)
