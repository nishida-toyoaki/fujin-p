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

# table_master/routes.py
# テーブルマスター - データベーステーブル閲覧・編集・管理システム

import json
import datetime
import logging
import mysql.connector
from flask import Blueprint, request, jsonify, session, render_template
from decorators import login_required
from auth import redirect_to_dashboard
from config import Config
from db import DatabaseConfig
from . import table_master_bp

logging.basicConfig(level=logging.DEBUG)

# テーブル名定義
VIEWS_TABLE = "table_master_views"
RENAME_HISTORY = "table_master_rename_history"
EDIT_HISTORY = "table_master_edit_history"
EXPORT_TEMPLATES = "table_master_export_templates"
DELETION_HISTORY = "table_master_deletion_history"


def is_safe_identifier(name: str) -> bool:
    """テーブル名・DB名の妥当性チェック（SQLインジェクション防止）"""
    if not name or not name.strip():
        return False
    dangerous = ('`', '\x00', ';', "'", '"')
    for ch in dangerous:
        if ch in name:
            logging.warning("is_safe_identifier: rejected name contains %s : %s", repr(ch), name)
            return False
    return True


def check_edit_permission(user_id):
    """テーブル編集権限チェック（admin または承認ユーザ）"""
    try:
        if not user_id:
            return False

        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        cursor.execute("SELECT id, category FROM users WHERE id = %s", (user_id,))
        user = cursor.fetchone()

        if not user:
            return False

        # 管理者は許可
        if user['category'] in ['admin']:
            return True

        # テーブル編集権限を持つユーザをチェック
        cursor.execute("""
            SELECT COUNT(*) as count
            FROM user_features uf
            JOIN features f ON uf.feature_id = f.id
            WHERE uf.user_id = %s
              AND f.feature_name = 'テーブル編集権限'
        """, (user_id,))
        result = cursor.fetchone()

        return result['count'] > 0

    except Exception as e:
        logging.error("check_edit_permission error: %s", e)
        return False
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


# ============================================
# ダッシュボード
# ============================================

@table_master_bp.route('/')
@login_required
def dashboard():
    """テーブルマスター メイン画面"""
    return render_template('table_master_dashboard.html')


# ============================================
# テーブル一覧取得
# ============================================

@table_master_bp.route('/get_all_tables', methods=['GET'])
@login_required
def get_all_tables():
    """
    ローカル全DB・全テーブル一覧を返す
    テーシャと同様の構造
    """
    result = {'tables': []}

    try:
        conn = mysql.connector.connect(**DatabaseConfig.base())
        cursor = conn.cursor()
        user_prefix = Config.DB_ACCOUNT + "$"
        cursor.execute(
            f"SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA "
            f"WHERE SCHEMA_NAME LIKE '{user_prefix}%'"
        )
        databases = [r[0] for r in cursor.fetchall()]
        cursor.close()
        conn.close()

        for db in databases:
            try:
                db_conn = mysql.connector.connect(**DatabaseConfig.get_config(db))
                db_cursor = db_conn.cursor()
                db_cursor.execute("SHOW TABLES")
                for (tbl,) in db_cursor.fetchall():
                    db_cursor.execute(f"SELECT COUNT(*) FROM `{tbl}`")
                    cnt = db_cursor.fetchone()[0]
                    result['tables'].append({
                        'database': db,
                        'table_name': tbl,
                        'row_count': cnt
                    })
                db_cursor.close()
                db_conn.close()
            except Exception as e:
                logging.warning("get_all_tables: DB %s error: %s", db, e)

        return jsonify({'success': True, **result})

    except Exception as e:
        logging.error("get_all_tables error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================
# テーブルデータ取得（編集用）
# ============================================

@table_master_bp.route('/get_table_data', methods=['POST'])
@login_required
def get_table_data():
    """
    テーブルの全データ＋メタデータを取得
    編集用に主キー情報も含める
    """
    data = request.json
    database = data.get('database', '')
    table_name = data.get('table_name', '')
    limit = min(int(data.get('limit', 1000)), 5000)  # 最大5000行

    if not database or not table_name:
        return jsonify({'success': False, 'error': 'パラメータ不足'}), 400
    if not is_safe_identifier(database) or not is_safe_identifier(table_name):
        return jsonify({'success': False, 'error': '不正な識別子'}), 400

    try:
        conn = mysql.connector.connect(**DatabaseConfig.get_config(database))
        cursor = conn.cursor(dictionary=True)

        # テーブル存在確認
        cursor.execute("SHOW TABLES LIKE %s", (table_name,))
        if not cursor.fetchone():
            return jsonify({'success': False, 'error': 'テーブルが見つかりません'}), 404

        # カラム情報取得
        cursor.execute(f"SHOW COLUMNS FROM `{table_name}`")
        columns_info = cursor.fetchall()
        columns = [c['Field'] for c in columns_info]

        # 主キー情報
        primary_keys = [c['Field'] for c in columns_info if c['Key'] == 'PRI']

        # 全件数
        cursor.execute(f"SELECT COUNT(*) as cnt FROM `{table_name}`")
        total_count = cursor.fetchone()['cnt']

        # データ取得
        cursor.execute(f"SELECT * FROM `{table_name}` LIMIT %s", (limit,))
        rows = cursor.fetchall()

        # シリアライズ
        def safe_val(v):
            if v is None:
                return None
            if isinstance(v, (datetime.datetime, datetime.date)):
                return v.isoformat()
            if isinstance(v, datetime.timedelta):  # ← 追加
                total_seconds = int(v.total_seconds())
                hours = total_seconds // 3600
                minutes = (total_seconds % 3600) // 60
                return f"{hours:02d}:{minutes:02d}"
            if isinstance(v, bytes):
                return v.decode('utf-8', errors='replace')
            return v

        serialized = [{k: safe_val(v) for k, v in row.items()} for row in rows]

        return jsonify({
            'success': True,
            'columns': columns,
            'columns_info': columns_info,
            'primary_keys': primary_keys,
            'rows': serialized,
            'total_count': total_count,
            'fetched_count': len(rows)
        })

    except Exception as e:
        logging.error("get_table_data error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


# ============================================
# データCRUD操作
# ============================================

@table_master_bp.route('/insert_row', methods=['POST'])
@login_required
def insert_row():
    """行を挿入"""
    if not check_edit_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '編集権限がありません'}), 403

    data = request.json
    database = data.get('database')
    table_name = data.get('table_name')
    row_data = data.get('row_data', {})

    if not database or not table_name or not row_data:
        return jsonify({'success': False, 'error': 'パラメータ不足'}), 400
    if not is_safe_identifier(database) or not is_safe_identifier(table_name):
        return jsonify({'success': False, 'error': '不正な識別子'}), 400

    try:
        conn = mysql.connector.connect(**DatabaseConfig.get_config(database))
        cursor = conn.cursor()

        # カラム名とプレースホルダー
        columns = list(row_data.keys())
        values = [row_data[col] for col in columns]

        columns_str = '`, `'.join(columns)
        placeholders = ', '.join(['%s'] * len(columns))

        query = f"INSERT INTO `{table_name}` (`{columns_str}`) VALUES ({placeholders})"
        cursor.execute(query, values)

        conn.commit()
        inserted_id = cursor.lastrowid

        # 履歴記録
        log_edit_history(database, table_name, 'INSERT', str(inserted_id), row_data)

        return jsonify({'success': True, 'message': '行を追加しました', 'inserted_id': inserted_id})

    except Exception as e:
        logging.error("insert_row error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@table_master_bp.route('/update_row', methods=['POST'])
@login_required
def update_row():
    """行を更新"""
    if not check_edit_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '編集権限がありません'}), 403

    data = request.json
    database = data.get('database')
    table_name = data.get('table_name')
    where_clause = data.get('where_clause')  # 例: {"id": 123}
    updates = data.get('updates', {})  # 例: {"name": "New Name", "age": 30}

    if not all([database, table_name, where_clause, updates]):
        return jsonify({'success': False, 'error': 'パラメータ不足'}), 400
    if not is_safe_identifier(database) or not is_safe_identifier(table_name):
        return jsonify({'success': False, 'error': '不正な識別子'}), 400

    try:
        conn = mysql.connector.connect(**DatabaseConfig.get_config(database))
        cursor = conn.cursor()

        # UPDATE文構築
        set_clause = ', '.join([f"`{k}` = %s" for k in updates.keys()])
        where_parts = ' AND '.join([f"`{k}` = %s" for k in where_clause.keys()])

        values = list(updates.values()) + list(where_clause.values())

        query = f"UPDATE `{table_name}` SET {set_clause} WHERE {where_parts}"
        cursor.execute(query, values)

        conn.commit()
        affected = cursor.rowcount

        # 履歴記録
        log_edit_history(database, table_name, 'UPDATE', str(where_clause), updates)

        return jsonify({'success': True, 'message': f'{affected}行を更新しました', 'affected_rows': affected})

    except Exception as e:
        logging.error("update_row error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@table_master_bp.route('/delete_row', methods=['POST'])
@login_required
def delete_row():
    """行を削除"""
    if not check_edit_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '編集権限がありません'}), 403

    data = request.json
    database = data.get('database')
    table_name = data.get('table_name')
    where_clause = data.get('where_clause')  # 例: {"id": 123}

    if not all([database, table_name, where_clause]):
        return jsonify({'success': False, 'error': 'パラメータ不足'}), 400
    if not is_safe_identifier(database) or not is_safe_identifier(table_name):
        return jsonify({'success': False, 'error': '不正な識別子'}), 400

    try:
        conn = mysql.connector.connect(**DatabaseConfig.get_config(database))
        cursor = conn.cursor()

        where_parts = ' AND '.join([f"`{k}` = %s" for k in where_clause.keys()])
        values = list(where_clause.values())

        query = f"DELETE FROM `{table_name}` WHERE {where_parts}"
        cursor.execute(query, values)

        conn.commit()
        affected = cursor.rowcount

        # 履歴記録
        log_edit_history(database, table_name, 'DELETE', str(where_clause), {})

        return jsonify({'success': True, 'message': f'{affected}行を削除しました', 'affected_rows': affected})

    except Exception as e:
        logging.error("delete_row error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


# ============================================
# テーブル管理
# ============================================

@table_master_bp.route('/rename_table', methods=['POST'])
@login_required
def rename_table():
    """テーブル名を変更"""
    if not check_edit_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '編集権限がありません'}), 403

    data = request.json
    database = data.get('database')
    old_name = data.get('old_table_name')
    new_name = data.get('new_table_name')
    reason = data.get('reason', '')

    if not all([database, old_name, new_name]):
        return jsonify({'success': False, 'error': 'パラメータ不足'}), 400
    if not is_safe_identifier(database) or not is_safe_identifier(old_name) or not is_safe_identifier(new_name):
        return jsonify({'success': False, 'error': '不正な識別子'}), 400

    try:
        conn = mysql.connector.connect(**DatabaseConfig.get_config(database))
        cursor = conn.cursor()

        # テーブル名変更
        cursor.execute(f"RENAME TABLE `{old_name}` TO `{new_name}`")
        conn.commit()

        # 履歴記録
        log_rename_history(database, old_name, new_name, reason, session.get('user_id'))

        return jsonify({'success': True, 'message': f'テーブル名を「{old_name}」→「{new_name}」に変更しました'})

    except Exception as e:
        logging.error("rename_table error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@table_master_bp.route('/delete_table', methods=['POST'])
@login_required
def delete_table():
    """テーブルを削除"""
    if not check_edit_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '編集権限がありません'}), 403

    data = request.json
    database = data.get('database')
    table_name = data.get('table_name')
    reason = data.get('reason', '')

    if not all([database, table_name]):
        return jsonify({'success': False, 'error': 'パラメータ不足'}), 400
    if not is_safe_identifier(database) or not is_safe_identifier(table_name):
        return jsonify({'success': False, 'error': '不正な識別子'}), 400
    if not reason:
        return jsonify({'success': False, 'error': '削除理由を入力してください'}), 400

    try:
        conn = mysql.connector.connect(**DatabaseConfig.get_config(database))
        cursor = conn.cursor()

        # テーブル存在確認
        cursor.execute("SHOW TABLES LIKE %s", (table_name,))
        if not cursor.fetchone():
            return jsonify({'success': False, 'error': 'テーブルが見つかりません'}), 404

        # 行数を取得（ログ用）
        cursor.execute(f"SELECT COUNT(*) as cnt FROM `{table_name}`")
        row_count = cursor.fetchone()[0]

        # テーブル削除
        cursor.execute(f"DROP TABLE `{table_name}`")
        conn.commit()

        # 削除履歴記録
        log_table_deletion(database, table_name, row_count, reason, session.get('user_id'))

        return jsonify({
            'success': True,
            'message': f'テーブル「{database}.{table_name}」({row_count}行)を削除しました'
        })

    except Exception as e:
        logging.error("delete_table error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@table_master_bp.route('/get_rename_history', methods=['GET'])
@login_required
def get_rename_history():
    """テーブル名変更履歴を取得"""
    database = request.args.get('database')

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        query = f"SELECT * FROM {RENAME_HISTORY}"
        params = []

        if database:
            query += " WHERE database_name = %s"
            params.append(database)

        query += " ORDER BY renamed_at DESC LIMIT 100"

        cursor.execute(query, params)
        history = cursor.fetchall()

        for h in history:
            if h.get('renamed_at'):
                h['renamed_at'] = h['renamed_at'].strftime('%Y-%m-%d %H:%M:%S')

        return jsonify({'success': True, 'history': history})

    except Exception as e:
        logging.error("get_rename_history error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@table_master_bp.route('/get_table_deletion_history', methods=['GET'])
@login_required
def get_table_deletion_history():
    """テーブル削除履歴を取得"""
    database = request.args.get('database')

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        # テーブル削除履歴テーブルが存在するか確認（初回は存在しない可能性がある）
        cursor.execute("""
            SELECT COUNT(*) as cnt
            FROM information_schema.tables
            WHERE table_schema = DATABASE()
            AND table_name = %s
        """, (DELETION_HISTORY,))

        if cursor.fetchone()['cnt'] == 0:
            return jsonify({'success': True, 'history': []})

        query = f"SELECT * FROM {DELETION_HISTORY}"
        params = []

        if database:
            query += " WHERE database_name = %s"
            params.append(database)

        query += " ORDER BY deleted_at DESC LIMIT 100"

        cursor.execute(query, params)
        history = cursor.fetchall()

        for h in history:
            if h.get('deleted_at'):
                h['deleted_at'] = h['deleted_at'].strftime('%Y-%m-%d %H:%M:%S')

        return jsonify({'success': True, 'history': history})

    except Exception as e:
        logging.error("get_table_deletion_history error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@table_master_bp.route('/execute_query', methods=['POST'])
@login_required
def execute_query():
    """SQLクエリを実行（読み取り専用クエリのみ）"""
    data = request.json
    database = data.get('database')
    query = data.get('query', '').strip()

    if not database or not query:
        return jsonify({'success': False, 'error': 'パラメータ不足'}), 400
    if not is_safe_identifier(database):
        return jsonify({'success': False, 'error': '不正な識別子'}), 400

    # コメント除去（--と#）
    lines = query.split('\n')
    cleaned_lines = []
    for line in lines:
        # --コメント除去
        if '--' in line:
            line = line[:line.index('--')]
        # #コメント除去
        if '#' in line:
            line = line[:line.index('#')]
        cleaned_lines.append(line)
    query = '\n'.join(cleaned_lines).strip()

    # セミコロンで分割して複数クエリ対応
    queries = [q.strip() for q in query.split(';') if q.strip()]

    if not queries:
        return jsonify({'success': False, 'error': 'クエリが空です'}), 400

    # 各クエリの検証
    allowed_commands = ['select', 'show', 'describe', 'desc', 'explain', 'use']
    dangerous_keywords = ['insert', 'update', 'delete', 'drop', 'create', 'alter', 'truncate', 'grant', 'revoke', 'replace', 'load']

    for q in queries:
        q_lower = q.lower().strip()

        # 許可コマンドチェック
        is_allowed = False
        for cmd in allowed_commands:
            if q_lower.startswith(cmd):
                is_allowed = True
                break

        if not is_allowed:
            return jsonify({'success': False, 'error': f'許可されていないコマンド: {q[:50]}...'}), 400

        # 危険なキーワードチェック
        for keyword in dangerous_keywords:
            if f' {keyword} ' in f' {q_lower} ' or q_lower.endswith(f' {keyword}'):
                return jsonify({'success': False, 'error': f'危険なキーワード「{keyword}」が含まれています'}), 400

    try:
        import time
        start_time = time.time()

        conn = mysql.connector.connect(**DatabaseConfig.get_config(database))
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SET SESSION max_execution_time=30000")

        # 複数クエリを順次実行
        last_result = None
        current_database = database

        for q in queries:
            q_lower = q.lower().strip()

            # USE文の場合はデータベースを切り替え
            if q_lower.startswith('use'):
                db_name = q.split()[1].strip().strip(';').strip('`')
                # 安全性チェック
                if not is_safe_identifier(db_name):
                    return jsonify({'success': False, 'error': f'不正なデータベース名: {db_name}'}), 400
                cursor.execute(f"USE `{db_name}`")
                current_database = db_name
                continue

            # クエリ実行
            cursor.execute(q)

            # 結果を保存（最後のSELECT/SHOW/DESCRIBE文の結果のみ）
            if q_lower.startswith(('select', 'show', 'describe', 'desc', 'explain')):
                last_result = {
                    'rows': cursor.fetchall(),
                    'columns': [desc[0] for desc in cursor.description] if cursor.description else [],
                    'query': q
                }

        execution_time = round(time.time() - start_time, 3)

        # 結果がない場合（USEのみなど）
        if not last_result:
            return jsonify({
                'success': True,
                'columns': ['Result'],
                'rows': [{'Result': 'クエリが正常に実行されました'}],
                'row_count': 1,
                'execution_time': execution_time
            })

        # シリアライズ
        def safe_val(v):
            if v is None:
                return None
            if isinstance(v, (datetime.datetime, datetime.date)):
                return v.isoformat()
            if isinstance(v, datetime.timedelta):  # ← 追加
                total_seconds = int(v.total_seconds())
                hours = total_seconds // 3600
                minutes = (total_seconds % 3600) // 60
                return f"{hours:02d}:{minutes:02d}"
            if isinstance(v, bytes):
                return v.decode('utf-8', errors='replace')
            return v

        serialized = [{k: safe_val(v) for k, v in row.items()} for row in last_result['rows']]

        # クエリ履歴記録（元のクエリ全体を記録）
        log_query_history(database, query, len(serialized), execution_time, session.get('user_id'))

        return jsonify({
            'success': True,
            'columns': last_result['columns'],
            'rows': serialized,
            'row_count': len(serialized),
            'execution_time': execution_time
        })

    except mysql.connector.Error as e:
        logging.error("execute_query MySQL error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logging.error("execute_query error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@table_master_bp.route('/get_query_history', methods=['GET'])
@login_required
def get_query_history():
    """クエリ実行履歴を取得"""
    database = request.args.get('database')
    limit = min(int(request.args.get('limit', 50)), 200)

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        # クエリ履歴テーブルが存在するか確認
        cursor.execute("""
            SELECT COUNT(*) as cnt
            FROM information_schema.tables
            WHERE table_schema = DATABASE()
            AND table_name = 'table_master_query_history'
        """)

        if cursor.fetchone()['cnt'] == 0:
            return jsonify({'success': True, 'history': []})

        query = "SELECT * FROM table_master_query_history"
        params = []

        if database:
            query += " WHERE database_name = %s"
            params.append(database)

        query += " ORDER BY executed_at DESC LIMIT %s"
        params.append(limit)

        cursor.execute(query, params)
        history = cursor.fetchall()

        for h in history:
            if h.get('executed_at'):
                h['executed_at'] = h['executed_at'].strftime('%Y-%m-%d %H:%M:%S')

        return jsonify({'success': True, 'history': history})

    except Exception as e:
        logging.error("get_query_history error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


# ============================================
# 履歴記録ヘルパー
# ============================================

def log_edit_history(database, table_name, operation, row_id, changes):
    """編集履歴を記録"""
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()

        cursor.execute(f"""
            INSERT INTO {EDIT_HISTORY}
            (database_name, table_name, operation, row_identifier, changes_json, edited_by, edited_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (database, table_name, operation, row_id, json.dumps(changes, default=str),
              session.get('user_id'), datetime.datetime.now()))

        conn.commit()

    except Exception as e:
        logging.error("log_edit_history error: %s", e)
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


def log_rename_history(database, old_name, new_name, reason, user_id):
    """テーブル名変更履歴を記録"""
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()

        cursor.execute(f"""
            INSERT INTO {RENAME_HISTORY}
            (database_name, old_table_name, new_table_name, reason, renamed_by, renamed_at)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (database, old_name, new_name, reason, user_id, datetime.datetime.now()))

        conn.commit()

    except Exception as e:
        logging.error("log_rename_history error: %s", e)
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


def log_table_deletion(database, table_name, row_count, reason, user_id):
    """テーブル削除履歴を記録"""
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()

        # 削除履歴テーブルが存在しない場合は作成
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {DELETION_HISTORY} (
                id INT AUTO_INCREMENT PRIMARY KEY,
                database_name VARCHAR(100) NOT NULL,
                table_name VARCHAR(100) NOT NULL,
                row_count INT DEFAULT 0,
                reason TEXT,
                deleted_by INT,
                deleted_at DATETIME NOT NULL,
                INDEX idx_database (database_name),
                INDEX idx_table (table_name),
                INDEX idx_deleted_at (deleted_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)

        cursor.execute(f"""
            INSERT INTO {DELETION_HISTORY}
            (database_name, table_name, row_count, reason, deleted_by, deleted_at)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (database, table_name, row_count, reason, user_id, datetime.datetime.now()))

        conn.commit()

    except Exception as e:
        logging.error("log_table_deletion error: %s", e)
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()

# ============================================================
# routes.py への追記パッチ
# execute_query の内部ロジックを共通ヘルパーとして切り出す
# 既存の execute_query 関数の「try:」ブロック直前に追加する
# ============================================================

def run_readonly_query(database, query, user_id=None, record_history=True):
    """
    読み取り専用SQLクエリを実行し jsonify 結果を返す共通ヘルパー。
    view_editor/preview と archive/rerun から呼び出される。
    """
    import time as _time
    import re

    if not database or not query:
        return jsonify({'success': False, 'error': 'パラメータ不足'}), 400
    if not is_safe_identifier(database):
        return jsonify({'success': False, 'error': '不正な識別子'}), 400

    # コメント除去
    lines = query.split('\n')
    cleaned = []
    for line in lines:
        if '--' in line: line = line[:line.index('--')]
        if '#'  in line: line = line[:line.index('#')]
        cleaned.append(line)
    query_clean = '\n'.join(cleaned).strip()

    queries = [q.strip() for q in query_clean.split(';') if q.strip()]
    if not queries:
        return jsonify({'success': False, 'error': 'クエリが空です'}), 400

    allowed_commands  = ['select', 'show', 'describe', 'desc', 'explain', 'use']
    dangerous_keywords = ['insert', 'update', 'delete', 'drop', 'create', 'alter',
                          'truncate', 'grant', 'revoke', 'replace', 'load']

    for q in queries:
        q_lower = q.lower().strip()
        if not any(q_lower.startswith(cmd) for cmd in allowed_commands):
            return jsonify({'success': False, 'error': f'許可されていないコマンド: {q[:50]}'}), 400
        for kw in dangerous_keywords:
            if f' {kw} ' in f' {q_lower} ' or q_lower.endswith(f' {kw}'):
                return jsonify({'success': False, 'error': f'危険なキーワード「{kw}」が含まれています'}), 400

    try:
        start_time = _time.time()
        conn = mysql.connector.connect(**DatabaseConfig.get_config(database))
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SET SESSION max_execution_time=30000")

        last_result = None
        for q in queries:
            q_lower = q.lower().strip()
            if q_lower.startswith('use'):
                db_name = q.split()[1].strip().strip(';').strip('`')
                if not is_safe_identifier(db_name):
                    return jsonify({'success': False, 'error': f'不正なDB名: {db_name}'}), 400
                cursor.execute(f"USE `{db_name}`")
                continue
            cursor.execute(q)
            if q_lower.startswith(('select', 'show', 'describe', 'desc', 'explain')):
                last_result = {
                    'rows':    cursor.fetchall(),
                    'columns': [d[0] for d in cursor.description] if cursor.description else [],
                    'query':   q
                }

        execution_time = round(_time.time() - start_time, 3)

        if not last_result:
            return jsonify({'success': True, 'columns': ['Result'],
                            'rows': [{'Result': 'クエリが実行されました'}],
                            'row_count': 1, 'execution_time': execution_time})

        def safe_val(v):
            import datetime as _dt
            if v is None: return None
            if isinstance(v, (_dt.datetime, _dt.date)): return v.isoformat()
            if isinstance(v, bytes): return v.decode('utf-8', errors='replace')
            return v

        serialized = [{k: safe_val(v) for k, v in row.items()} for row in last_result['rows']]

        if record_history and user_id:
            log_query_history(database, query, len(serialized), execution_time, user_id)

        return jsonify({'success': True,
                        'columns': last_result['columns'],
                        'rows': serialized[:1000],
                        'row_count': len(serialized),
                        'execution_time': execution_time})

    except mysql.connector.Error as e:
        logging.error("run_readonly_query MySQL error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logging.error("run_readonly_query error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()



def log_query_history(database, query, row_count, execution_time, user_id):
    """クエリ実行履歴を記録"""
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()

        # クエリ履歴テーブルが存在しない場合は作成
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS table_master_query_history (
                id INT AUTO_INCREMENT PRIMARY KEY,
                database_name VARCHAR(100) NOT NULL,
                query_text TEXT NOT NULL,
                row_count INT DEFAULT 0,
                execution_time FLOAT DEFAULT 0,
                executed_by INT,
                executed_at DATETIME NOT NULL,
                INDEX idx_database (database_name),
                INDEX idx_executed_at (executed_at),
                INDEX idx_user (executed_by)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)

        cursor.execute("""
            INSERT INTO table_master_query_history
            (database_name, query_text, row_count, execution_time, executed_by, executed_at)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (database, query, row_count, execution_time, user_id, datetime.datetime.now()))

        conn.commit()

    except Exception as e:
        logging.error("log_query_history error: %s", e)
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()

@table_master_bp.route('/return_to_fujin')
@login_required
def return_to_fujin():
    """FUJIN-Pダッシュボードに戻る"""
    return redirect_to_dashboard()