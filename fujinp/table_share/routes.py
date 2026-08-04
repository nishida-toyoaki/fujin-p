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

# table_share/routes.py
# テーシャ (Table Share) - FUJIN-Pアライアンス間テーブル共有システム

import io
import json
import datetime
import logging
import hashlib
import base64
import re as _re
import requests
import mysql.connector
import pandas as pd
from flask import Blueprint, request, jsonify, session, render_template, current_app
from decorators import login_required
from auth import redirect_to_dashboard
from config import Config
from db import DatabaseConfig, Tables
from . import table_share_bp
from cryptography.fernet import Fernet

logging.basicConfig(level=logging.DEBUG)

# テーブル名定義
PUBLISHED_TABLES = "table_share_published"      # 公開テーブル管理
SUBSCRIPTIONS = "table_share_subscriptions"     # 購読設定
SYNC_HISTORY = "table_share_sync_history"       # 同期履歴
ALLIANCE_SITES = "table_share_alliance_sites"   # アライアンスサイト

# 時刻定義
JST = datetime.timezone(datetime.timedelta(hours=9))

def now_jst():
    """現在時刻をJST（tzなし）で返す（MySQL保存用）"""
    return datetime.datetime.now(JST).replace(tzinfo=None)

# 暗号化キー生成用のシークレット（実際の運用ではConfigから取得）
def get_encryption_key():
    """暗号化キーを取得（サイト固有のシークレットから生成）"""
    secret = getattr(Config, 'TABLE_SHARE_SECRET', Config.SECRET_KEY)
    key = hashlib.sha256(secret.encode()).digest()
    return base64.urlsafe_b64encode(key)


def encrypt_data(data: str) -> str:
    """データを暗号化"""
    f = Fernet(get_encryption_key())
    return f.encrypt(data.encode()).decode()


def decrypt_data(encrypted: str) -> str:
    """データを復号化"""
    f = Fernet(get_encryption_key())
    return f.decrypt(encrypted.encode()).decode()


def is_safe_identifier(name: str) -> bool:
    """テーブル名・DB名の妥当性チェック（SQLインジェクション防止）
       バックリスト方式：バックティック・NULL・セミコロン・引用符のみ遮断
       それ以外（日本語・ハイフン・ドットなど）は許可し、実際のDB存在確認で安全を担保"""
    if not name or not name.strip():
        return False
    dangerous = ('`', '\x00', ';', "'", '"')
    for ch in dangerous:
        if ch in name:
            logging.warning("is_safe_identifier: rejected name contains %s : %s", repr(ch), name)
            return False
    return True


# ============================================
# ダッシュボード
# ============================================

@table_share_bp.route('/')
@login_required
def dashboard():
    """テーシャ メイン画面"""
    return render_template('table_share_dashboard.html')


# ============================================
# データベース・テーブル一覧取得
# ============================================

@table_share_bp.route('/get_databases', methods=['GET'])
@login_required
def get_databases():
    """DB一覧を返す"""
    try:
        conn = mysql.connector.connect(**DatabaseConfig.base())
        cursor = conn.cursor()

        user_prefix = Config.DB_ACCOUNT + "$"
        query = f"SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME LIKE '{user_prefix}%'"
        cursor.execute(query)
        rows = cursor.fetchall()
        dbs = [r[0] for r in rows]

        return jsonify({'success': True, 'databases': dbs})
    except mysql.connector.Error as e:
        logging.error("get_databases error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@table_share_bp.route('/get_tables', methods=['POST'])
@login_required
def get_tables():
    """テーブル一覧を返す"""
    data = request.json
    database = data.get('database')
    if not database:
        return jsonify({'success': False, 'error': 'database not specified'}), 400

    try:
        conn = mysql.connector.connect(**DatabaseConfig.get_config(database))
        cursor = conn.cursor()
        cursor.execute("SHOW TABLES")
        rows = cursor.fetchall()
        tables = [r[0] for r in rows]

        return jsonify({'success': True, 'tables': tables})
    except mysql.connector.Error as e:
        logging.error("get_tables error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


# ============================================
# テーブル一覧・プレビュー（Overview）
# ============================================

@table_share_bp.route('/get_all_tables_overview', methods=['GET'])
@login_required
def get_all_tables_overview():
    """
    ローカル全DB・全テーブル＋リモート公開テーブル一覧を返す
    フロント側のテーブル一覧タブで使用
    """
    result = {'local_tables': [], 'remote_tables': []}

    try:
        # ─── ローカル全テーブル ───
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
                    result['local_tables'].append({
                        'database': db,
                        'table_name': tbl,
                        'row_count': cnt
                    })
                db_cursor.close()
                db_conn.close()
            except Exception as e:
                logging.warning("overview: DB %s error: %s", db, e)

        # ─── リモート公開テーブル（全アライアンスサイト） ───
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(f"""
            SELECT id, site_name, site_url, api_key
            FROM {ALLIANCE_SITES} WHERE is_active = 1
        """)
        sites = cursor.fetchall()
        cursor.close()
        conn.close()

        for site in sites:
            try:
                remote_url = f"{site['site_url']}/table_share/api/list_published"
                resp = requests.post(remote_url, json={'api_key': site['api_key']}, timeout=10)
                if resp.ok:
                    remote_data = resp.json()
                    if remote_data.get('success'):
                        for t in remote_data['tables']:
                            result['remote_tables'].append({
                                'site_id': site['id'],
                                'site_name': site['site_name'],
                                'database': t['database_name'],
                                'table_name': t['table_name'],
                                'version': t.get('version'),
                                'row_count': t.get('row_count')
                            })
            except Exception as e:
                logging.warning("overview: site %s fetch error: %s", site['site_name'], e)

        return jsonify({'success': True, **result})

    except Exception as e:
        logging.error("get_all_tables_overview error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@table_share_bp.route('/preview_local_table', methods=['POST'])
@login_required
def preview_local_table():
    """
    ローカルテーブルの先頭N行＋カラム情報を返す（プレビュー用）
    デフォルト100行、最大500行
    """
    data = request.json
    database = data.get('database', '')
    table_name = data.get('table_name', '')
    limit = min(int(data.get('limit', 100)), 500)

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

        # カラム情報
        cursor.execute(f"SHOW COLUMNS FROM `{table_name}`")
        columns = [c['Field'] for c in cursor.fetchall()]

        # 全件数
        cursor.execute(f"SELECT COUNT(*) as cnt FROM `{table_name}`")
        total_count = cursor.fetchone()['cnt']

        # データ取得（LIMIT付き）
        cursor.execute(f"SELECT * FROM `{table_name}` LIMIT %s", (limit,))
        rows = cursor.fetchall()

        # シリアライズ可能にする
        def safe_val(v):
            if v is None:
                return None
            if isinstance(v, (datetime.datetime, datetime.date)):
                return v.isoformat()
            if isinstance(v, bytes):
                return v.decode('utf-8', errors='replace')
            return v

        serialized = [{k: safe_val(v) for k, v in row.items()} for row in rows]

        return jsonify({
            'success': True,
            'columns': columns,
            'rows': serialized,
            'total_count': total_count,
            'fetched_count': len(rows)
        })

    except Exception as e:
        logging.error("preview_local_table error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@table_share_bp.route('/preview_remote_table', methods=['POST'])
@login_required
def preview_remote_table():
    """
    リモートサイトの公開テーブルデータを取得し先頭100行を返す（プレビュー用）
    既存 api/fetch_table エンドポイントを再利用
    """
    data = request.json
    alliance_site_id = data.get('alliance_site_id')
    database = data.get('database', '')
    table_name = data.get('table_name', '')

    if not all([alliance_site_id, database, table_name]):
        return jsonify({'success': False, 'error': 'パラメータ不足'}), 400

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(f"""
            SELECT site_url, api_key FROM {ALLIANCE_SITES}
            WHERE id = %s AND is_active = 1
        """, (alliance_site_id,))
        site = cursor.fetchone()
        cursor.close()
        conn.close()

        if not site:
            return jsonify({'success': False, 'error': 'サイトが見つかりません'}), 404

        # リモートからデータ取得
        remote_url = f"{site['site_url']}/table_share/api/fetch_table"
        resp = requests.post(remote_url, json={
            'api_key': site['api_key'],
            'database': database,
            'table_name': table_name
        }, timeout=30)

        if not resp.ok:
            return jsonify({'success': False, 'error': f'リモート接続エラー: {resp.status_code}'}), 500

        remote_data = resp.json()
        if not remote_data.get('success'):
            return jsonify({'success': False, 'error': remote_data.get('error', '不明なエラー')}), 500

        table_data = json.loads(remote_data['encrypted_content'])
        all_rows = table_data.get('rows', [])
        columns = list(all_rows[0].keys()) if all_rows else []
        preview_rows = all_rows[:100]

        return jsonify({
            'success': True,
            'columns': columns,
            'rows': preview_rows,
            'total_count': len(all_rows),
            'fetched_count': len(preview_rows),
            'version': remote_data.get('version')
        })

    except Exception as e:
        logging.error("preview_remote_table error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================
# Publish機能（サーバ側）
# ============================================

@table_share_bp.route('/publish_table', methods=['POST'])
@login_required
def publish_table():
    """
    テーブルを公開（Publish）する
    - 公開中のテーブルとしてマークされる
    - 他サイトが依存している可能性があることを示す
    - バージョン番号で管理（更新時にバージョンアップ）
    """
    if not check_table_share_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '権限がありません'}), 403

    data = request.json
    database = data.get('database')
    table_name = data.get('table_name')
    description = data.get('description', '')

    if not database or not table_name:
        return jsonify({'success': False, 'error': 'パラメータ不足'}), 400

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        # 既存の公開を確認
        cursor.execute(f"""
            SELECT * FROM {PUBLISHED_TABLES}
            WHERE database_name = %s AND table_name = %s
        """, (database, table_name))
        existing = cursor.fetchone()

        # テーブルデータを取得
        table_conn = mysql.connector.connect(**DatabaseConfig.get_config(database))
        table_cursor = table_conn.cursor(dictionary=True)
        table_cursor.execute(f"SELECT * FROM `{table_name}`")
        rows = table_cursor.fetchall()

        # スキーマ情報も取得
        table_cursor.execute(f"SHOW CREATE TABLE `{table_name}`")
        schema_result = table_cursor.fetchone()
        schema_ddl = schema_result.get('Create Table', '')

        table_cursor.close()
        table_conn.close()

        # データをJSON化（暗号化なし - HTTPS通信で保護）
        table_data = {
            'schema': schema_ddl,
            'rows': rows,
            'row_count': len(rows)
        }
        content_json = json.dumps(table_data, default=str)
        content_hash = hashlib.sha256(content_json.encode()).hexdigest()

        now = now_jst()
        user_id = session.get('user_id')
        site_name = Config.DB_ACCOUNT  # サイト識別子

        if existing:
            # 更新（バージョンアップ）
            new_version = existing['version'] + 1
            cursor.execute(f"""
                UPDATE {PUBLISHED_TABLES}
                SET version = %s, content_hash = %s, encrypted_content = %s,
                    row_count = %s, updated_at = %s, updated_by = %s, description = %s
                WHERE id = %s
            """, (new_version, content_hash, content_json,
                  len(rows), now, user_id, description, existing['id']))
            message = f"テーブルを更新しました (v{new_version})"
        else:
            # 新規公開
            cursor.execute(f"""
                INSERT INTO {PUBLISHED_TABLES}
                (site_name, database_name, table_name, version, content_hash,
                 encrypted_content, row_count, description, published_at, published_by, updated_at, updated_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (site_name, database, table_name, 1, content_hash,
                  content_json, len(rows), description, now, user_id, now, user_id))
            message = "テーブルを公開しました (v1)"

        conn.commit()
        return jsonify({'success': True, 'message': message})

    except Exception as e:
        logging.error("publish_table error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()

### 公開取り消し

@table_share_bp.route('/unpublish_table', methods=['POST'])
@login_required
def unpublish_table():
    """
    テーブルの公開を取り消す
    - 公開テーブル管理から削除
    - 他サイトは次回同期時にテーブルが見つからなくなる
    """
    if not check_table_share_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '権限がありません'}), 403

    data = request.json
    database = data.get('database')
    table_name = data.get('table_name')

    if not database or not table_name:
        return jsonify({'success': False, 'error': 'パラメータ不足'}), 400

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        # 公開テーブルの存在確認
        cursor.execute(f"""
            SELECT * FROM {PUBLISHED_TABLES}
            WHERE database_name = %s AND table_name = %s
        """, (database, table_name))
        existing = cursor.fetchone()

        if not existing:
            return jsonify({'success': False, 'error': 'このテーブルは公開されていません'}), 404

        # 公開情報を削除
        cursor.execute(f"""
            DELETE FROM {PUBLISHED_TABLES}
            WHERE database_name = %s AND table_name = %s
        """, (database, table_name))

        conn.commit()

        message = f"テーブル「{database}.{table_name}」を非公開にしました"
        logging.info(f"unpublish_table: {message} by user {session.get('user_id')}")

        return jsonify({'success': True, 'message': message})

    except Exception as e:
        logging.error("unpublish_table error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()

@table_share_bp.route('/get_published_tables', methods=['GET'])
@login_required
def get_published_tables():
    """公開済みテーブル一覧を取得"""
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        cursor.execute(f"""
            SELECT id, site_name, database_name, table_name, version,
                   content_hash, row_count, description, published_at, updated_at
            FROM {PUBLISHED_TABLES}
            ORDER BY updated_at DESC
        """)
        tables = cursor.fetchall()

        # datetime を文字列に変換
        for t in tables:
            if t.get('published_at'):
                t['published_at'] = t['published_at'].strftime('%Y-%m-%d %H:%M:%S')
            if t.get('updated_at'):
                t['updated_at'] = t['updated_at'].strftime('%Y-%m-%d %H:%M:%S')

        return jsonify({'success': True, 'tables': tables})
    except Exception as e:
        logging.error("get_published_tables error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@table_share_bp.route('/api/site_info', methods=['GET'])
def api_site_info():
    """
    サイト情報を公開（認証不要）
    - FUJIN-Pアライアンス対応の証明
    - サイト名、説明などを返す
    """
    try:
        site_name = Config.DB_ACCOUNT  # サイト識別子
        site_display_name = getattr(Config, 'SITE_DISPLAY_NAME', site_name)
        site_description = getattr(Config, 'SITE_DESCRIPTION', 'FUJIN-P Alliance Site')

        return jsonify({
            'success': True,
            'fujin_p_alliance': True,  # FUJIN-P対応フラグ
            'site_id': site_name,
            'site_name': site_display_name,
            'description': site_description,
            'version': '1.0',
            'endpoints': {
                'list_published': '/table_share/api/list_published',
                'fetch_table': '/table_share/api/fetch_table'
            }
        })
    except Exception as e:
        logging.error("api_site_info error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@table_share_bp.route('/api/list_published', methods=['POST'])
def api_list_published():
    """
    外部サイトからの公開テーブル一覧リクエストに応答（API）
    - APIキーによる認証
    - 公開中のテーブル一覧を返す
    """
    data = request.json
    api_key = data.get('api_key')

    # APIキー検証
    if not verify_api_key(api_key):
        return jsonify({'success': False, 'error': '認証失敗'}), 401

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        cursor.execute(f"""
            SELECT database_name, table_name, version, row_count, description, updated_at
            FROM {PUBLISHED_TABLES}
            ORDER BY database_name, table_name
        """)
        tables = cursor.fetchall()

        # datetime を文字列に変換
        for t in tables:
            if t.get('updated_at'):
                t['updated_at'] = t['updated_at'].strftime('%Y-%m-%d %H:%M:%S')

        return jsonify({'success': True, 'tables': tables})

    except Exception as e:
        logging.error("api_list_published error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@table_share_bp.route('/api/fetch_table', methods=['POST'])
def api_fetch_table():
    """
    外部サイトからのテーブル取得リクエストに応答（API）
    - APIキーによる認証
    - 暗号化されたテーブルデータを返す
    """
    data = request.json
    api_key = data.get('api_key')
    database = data.get('database')
    table_name = data.get('table_name')

    # APIキー検証（簡易実装、実際はより堅牢な認証を）
    if not verify_api_key(api_key):
        return jsonify({'success': False, 'error': '認証失敗'}), 401

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        cursor.execute(f"""
            SELECT encrypted_content, version, content_hash, row_count, updated_at
            FROM {PUBLISHED_TABLES}
            WHERE database_name = %s AND table_name = %s
        """, (database, table_name))
        published = cursor.fetchone()

        if not published:
            return jsonify({'success': False, 'error': 'テーブルが見つかりません'}), 404

        return jsonify({
            'success': True,
            'encrypted_content': published['encrypted_content'],
            'version': published['version'],
            'content_hash': published['content_hash'],
            'row_count': published['row_count'],
            'updated_at': published['updated_at'].strftime('%Y-%m-%d %H:%M:%S') if published['updated_at'] else None
        })

    except Exception as e:
        logging.error("api_fetch_table error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


# ============================================
# Subscribe機能（クライアント側）
# ============================================

@table_share_bp.route('/get_subscriptions', methods=['GET'])
@login_required
def get_subscriptions():
    """購読設定一覧を取得"""
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        cursor.execute(f"""
            SELECT s.*, a.site_url, a.site_name as remote_site_name
            FROM {SUBSCRIPTIONS} s
            LEFT JOIN {ALLIANCE_SITES} a ON s.alliance_site_id = a.id
            ORDER BY s.created_at DESC
        """)
        subs = cursor.fetchall()

        for s in subs:
            if s.get('created_at'):
                s['created_at'] = s['created_at'].strftime('%Y-%m-%d %H:%M:%S')
            if s.get('last_synced_at'):
                s['last_synced_at'] = s['last_synced_at'].strftime('%Y-%m-%d %H:%M:%S')

        return jsonify({'success': True, 'subscriptions': subs})
    except Exception as e:
        logging.error("get_subscriptions error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@table_share_bp.route('/fetch_remote_tables', methods=['POST'])
@login_required
def fetch_remote_tables():
    """アライアンスサイトの公開テーブル一覧を取得"""
    data = request.json
    alliance_site_id = data.get('alliance_site_id')

    if not alliance_site_id:
        return jsonify({'success': False, 'error': 'alliance_site_id が必要です'}), 400

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        # アライアンスサイト情報を取得
        cursor.execute(f"""
            SELECT site_url, api_key, site_name
            FROM {ALLIANCE_SITES}
            WHERE id = %s AND is_active = 1
        """, (alliance_site_id,))
        site = cursor.fetchone()

        if not site:
            return jsonify({'success': False, 'error': 'サイトが見つかりません'}), 404

        # リモートサイトから公開テーブル一覧を取得
        remote_url = f"{site['site_url']}/table_share/api/list_published"
        response = requests.post(remote_url, json={
            'api_key': site['api_key']
        }, timeout=30)

        if not response.ok:
            return jsonify({'success': False, 'error': f'リモート接続エラー: {response.status_code}'}), 500

        remote_data = response.json()
        if not remote_data.get('success'):
            return jsonify({'success': False, 'error': remote_data.get('error', '不明なエラー')}), 500

        return jsonify({
            'success': True,
            'site_name': site['site_name'],
            'tables': remote_data['tables']
        })

    except requests.RequestException as e:
        logging.error("fetch_remote_tables network error: %s", e)
        return jsonify({'success': False, 'error': f'ネットワークエラー: {str(e)}'}), 500
    except Exception as e:
        logging.error("fetch_remote_tables error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@table_share_bp.route('/add_subscription', methods=['POST'])
@login_required
def add_subscription():
    """購読設定を追加"""
    if not check_table_share_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '権限がありません'}), 403

    data = request.json
    alliance_site_id = data.get('alliance_site_id')
    remote_database = data.get('remote_database')
    remote_table = data.get('remote_table')
    local_database = data.get('local_database')
    local_table = data.get('local_table') or remote_table  # 空文字列の場合もremote_tableを使用

    if not all([alliance_site_id, remote_database, remote_table, local_database]):
        return jsonify({'success': False, 'error': 'パラメータ不足'}), 400

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        now = now_jst()
        user_id = session.get('user_id')

        cursor.execute(f"""
            INSERT INTO {SUBSCRIPTIONS}
            (alliance_site_id, remote_database, remote_table, local_database, local_table,
             auto_sync, created_at, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (alliance_site_id, remote_database, remote_table, local_database, local_table,
              False, now, user_id))

        conn.commit()
        return jsonify({'success': True, 'message': '購読設定を追加しました'})

    except Exception as e:
        logging.error("add_subscription error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@table_share_bp.route('/delete_subscription', methods=['POST'])
@login_required
def delete_subscription():
    """購読設定を解除"""
    if not check_table_share_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '権限がありません'}), 403

    data = request.json
    subscription_id = data.get('subscription_id')

    if not subscription_id:
        return jsonify({'success': False, 'error': 'subscription_id が必要です'}), 400

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        # 購読設定が存在するか確認
        cursor.execute(f"SELECT * FROM {SUBSCRIPTIONS} WHERE id = %s", (subscription_id,))
        sub = cursor.fetchone()

        if not sub:
            return jsonify({'success': False, 'error': '購読設定が見つかりません'}), 404

        # 同期履歴も削除（オプション：履歴を残したい場合はコメントアウト）
        cursor.execute(f"DELETE FROM {SYNC_HISTORY} WHERE subscription_id = %s", (subscription_id,))

        # 購読設定を削除
        cursor.execute(f"DELETE FROM {SUBSCRIPTIONS} WHERE id = %s", (subscription_id,))

        conn.commit()
        return jsonify({'success': True, 'message': '購読設定を解除しました'})

    except Exception as e:
        logging.error("delete_subscription error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@table_share_bp.route('/sync_table', methods=['POST'])
@login_required
def sync_table():
    """
    テーブルを同期（リモートからローカルへ）
    1. リモートサイトからテーブルデータを取得
    2. ローカルのバックアップを作成
    3. ローカルテーブルを更新
    """
    import traceback
    import sys

    def log(msg):
        print(f"[SYNC] {msg}", file=sys.stderr, flush=True)

    log("="*50)
    log("sync_table called")

    conn = None
    try:
        user_id = session.get('user_id')
        log(f"user_id: {user_id}")

        # === デバッグ情報追加 ===
        user_category = session.get('user_category', 'unknown')
        log(f"user_category: {user_category}")
        log(f"session keys: {list(session.keys())}")
        log(f"session contents: {dict(session)}")

        # 権限チェック前のログ
        log("Checking table_share_permission...")
        has_permission = check_table_share_permission(user_id)
        log(f"Permission check result: {has_permission}")

        if not has_permission:
            log("Permission denied")
            log(f"Returning 403 error for user_id={user_id}, category={user_category}")
            return jsonify({'success': False, 'error': '権限がありません'}), 403

        log("Permission OK")
        # === デバッグ情報追加ここまで ===

        data = request.json
        log(f"request.json: {data}")
        subscription_id = data.get('subscription_id')

        if not subscription_id:
            return jsonify({'success': False, 'error': 'subscription_id が必要です'}), 400

        log(f"Connecting to database...")
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        log("Database connected")

        # 購読設定を取得
        log("Fetching subscription settings...")
        cursor.execute(f"""
            SELECT s.*, a.site_url, a.api_key
            FROM {SUBSCRIPTIONS} s
            JOIN {ALLIANCE_SITES} a ON s.alliance_site_id = a.id
            WHERE s.id = %s
        """, (subscription_id,))
        sub = cursor.fetchone()

        if not sub:
            log(f"Subscription not found: {subscription_id}")
            return jsonify({'success': False, 'error': '購読設定が見つかりません'}), 404

        log(f"Subscription found: site_url={sub['site_url']}")
        log(f"  remote: {sub['remote_database']}.{sub['remote_table']}")

        # ローカルテーブル名が空の場合はリモートテーブル名を使用
        local_table = sub['local_table'] or sub['remote_table']
        log(f"  local: {sub['local_database']}.{local_table}")

        # リモートサイトからテーブルを取得
        remote_url = f"{sub['site_url']}/table_share/api/fetch_table"
        log(f"Fetching from remote: {remote_url}")

        try:
            response = requests.post(remote_url, json={
                'api_key': sub['api_key'],
                'database': sub['remote_database'],
                'table_name': sub['remote_table']
            }, timeout=60)
            log(f"Remote response status: {response.status_code}")
        except requests.RequestException as req_err:
            log(f"Request failed: {type(req_err).__name__}: {req_err}")
            return jsonify({'success': False, 'error': f'リモート接続失敗: {type(req_err).__name__}: {str(req_err)}'}), 500

        if not response.ok:
            log(f"Remote returned error: {response.status_code}")
            log(f"Response body: {response.text[:1000]}")
            return jsonify({'success': False, 'error': f'リモート接続エラー: {response.status_code}'}), 500

        remote_data = response.json()
        log(f"Remote response success: {remote_data.get('success')}")

        if not remote_data.get('success'):
            error_msg = remote_data.get('error', '不明なエラー')
            log(f"Remote error: {error_msg}")
            return jsonify({'success': False, 'error': f"リモートエラー: {error_msg}"}), 500

        log(f"Remote data received: version={remote_data.get('version')}, rows={remote_data.get('row_count')}")

        # データを取得（暗号化なし - HTTPS通信で保護）
        log("Parsing table data...")
        try:
            table_data = json.loads(remote_data['encrypted_content'])
            log(f"Parse successful: {len(table_data.get('rows', []))} rows")
        except Exception as parse_err:
            log(f"Parse failed: {type(parse_err).__name__}: {parse_err}")
            log(traceback.format_exc())
            return jsonify({'success': False, 'error': f'データ解析エラー: {type(parse_err).__name__}: {str(parse_err)}'}), 500

        # ローカルテーブルのバックアップを作成
        log(f"Creating backup: {sub['local_database']}.{local_table}")
        backup_result = create_table_backup(sub['local_database'], local_table)
        if not backup_result['success']:
            log(f"Backup failed: {backup_result['error']}")
            return jsonify({'success': False, 'error': f"バックアップ失敗: {backup_result['error']}"}), 500

        log(f"Backup created: {backup_result['backup_table']}")

        # ローカルテーブルを更新
        log("Updating local table...")
        update_result = update_local_table(
            sub['local_database'],
            local_table,
            table_data['rows'],
            table_data.get('schema')
        )

        if not update_result['success']:
            log(f"Update failed: {update_result['error']}")
            return jsonify({'success': False, 'error': f"更新失敗: {update_result['error']}"}), 500

        log("Local table updated successfully")

        # 同期履歴を記録
        now = now_jst()
        cursor.execute(f"""
            INSERT INTO {SYNC_HISTORY}
            (subscription_id, remote_version, content_hash, row_count, synced_at, synced_by, status, backup_table)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (subscription_id, remote_data['version'], remote_data['content_hash'],
              remote_data['row_count'], now, user_id, 'success', backup_result['backup_table']))

        # 購読設定の最終同期時刻を更新
        cursor.execute(f"""
            UPDATE {SUBSCRIPTIONS}
            SET last_synced_at = %s, last_synced_version = %s
            WHERE id = %s
        """, (now, remote_data['version'], subscription_id))

        conn.commit()
        log("sync_table completed successfully")
        log("="*50)

        return jsonify({
            'success': True,
            'message': f"同期完了（{remote_data['row_count']}行、v{remote_data['version']}）",
            'backup_table': backup_result['backup_table']
        })

    except Exception as e:
        error_detail = traceback.format_exc()
        log(f"EXCEPTION: {type(e).__name__}: {e}")
        log(error_detail)
        if conn:
            conn.rollback()
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {str(e)}'}), 500
    finally:
        try:
            if conn and conn.is_connected():
                cursor.close()
                conn.close()
        except:
            pass


def create_table_backup(database, table_name):
    """テーブルのバックアップを作成（テーブルが存在しない場合はスキップ）"""
    import sys
    try:
        conn = mysql.connector.connect(**DatabaseConfig.get_config(database))
        cursor = conn.cursor()

        # テーブルが存在するか確認
        cursor.execute(f"SHOW TABLES LIKE '{table_name}'")
        exists = cursor.fetchone()

        if not exists:
            print(f"[BACKUP] Table {table_name} does not exist, skipping backup", file=sys.stderr, flush=True)
            return {'success': True, 'backup_table': None, 'skipped': True}

        # バックアップテーブル名（タイムスタンプ付き）
        timestamp = now_jst().strftime('%Y%m%d_%H%M%S')
        backup_table = f"{table_name}_backup_{timestamp}"

        # テーブルをコピー
        cursor.execute(f"CREATE TABLE `{backup_table}` LIKE `{table_name}`")
        cursor.execute(f"INSERT INTO `{backup_table}` SELECT * FROM `{table_name}`")

        conn.commit()
        print(f"[BACKUP] Backup created: {backup_table}", file=sys.stderr, flush=True)
        return {'success': True, 'backup_table': backup_table}

    except Exception as e:
        logging.error("create_table_backup error: %s", e)
        import traceback
        print(f"[BACKUP] Error: {e}", file=sys.stderr, flush=True)
        print(traceback.format_exc(), file=sys.stderr, flush=True)
        return {'success': False, 'error': str(e)}
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


def update_local_table(database, table_name, rows, schema=None):
    import sys, re, traceback

    def log(msg):
        print(f"[UPDATE] {msg}", file=sys.stderr, flush=True)

    conn = None
    try:
        # ★ DDL用：autocommit=True で接続
        conn = mysql.connector.connect(
            **DatabaseConfig.get_config(database),
            autocommit=True
        )
        cursor = conn.cursor()

        cursor.execute("SET FOREIGN_KEY_CHECKS = 0")

        cursor.execute("SHOW TABLES LIKE %s", (table_name,))
        exists = cursor.fetchone()

        if exists:
            log(f"Dropping existing table: {table_name}")
            cursor.execute(f"DROP TABLE `{table_name}`")
            log("Dropped")

        if schema:
            create_stmt = re.sub(
                r'CREATE TABLE `[^`]+`',
                f'CREATE TABLE `{table_name}`',
                schema
            )
            log("Creating table with remote schema")
            cursor.execute(create_stmt)
        elif rows:
            columns = list(rows[0].keys())
            col_defs = ', '.join([f'`{col}` TEXT' for col in columns])
            cursor.execute(
                f"CREATE TABLE `{table_name}` ({col_defs}) "
                f"ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
        else:
            return {'success': False, 'error': 'スキーマ情報もデータもありません'}

        log("Table created")

        # ★ INSERT だけ手動トランザクション
        if rows:
            conn.autocommit = False   # ← DDL完了後に切り替え
            try:
                columns = list(rows[0].keys())
                placeholders = ', '.join(['%s'] * len(columns))
                columns_str = '`, `'.join(columns)
                insert_query = (
                    f"INSERT INTO `{table_name}` (`{columns_str}`) "
                    f"VALUES ({placeholders})"
                )
                batch_size = 1000
                total = 0
                for i in range(0, len(rows), batch_size):
                    batch = [tuple(r.get(c) for c in columns) for r in rows[i:i+batch_size]]
                    cursor.executemany(insert_query, batch)
                    total += len(batch)
                    log(f"Inserted batch: {total} rows so far")

                conn.commit()   # ← INSERT をまとめて確定
                log(f"Committed {total} rows")

            except Exception:
                conn.rollback()
                raise

        cursor.execute("SET FOREIGN_KEY_CHECKS = 1")
        log("Done")
        return {'success': True, 'row_count': len(rows) if rows else 0}

    except Exception as e:
        log(f"Error: {type(e).__name__}: {e}")
        log(traceback.format_exc())
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return {'success': False, 'error': f'{type(e).__name__}: {str(e)}'}

    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()

# ============================================
# アライアンスサイト管理
# ============================================

@table_share_bp.route('/get_alliance_sites', methods=['GET'])
@login_required
def get_alliance_sites():
    """アライアンスサイト一覧を取得"""
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        cursor.execute(f"""
            SELECT id, site_name, site_url, description, is_active, created_at
            FROM {ALLIANCE_SITES}
            WHERE is_active = 1
            ORDER BY site_name
        """)
        sites = cursor.fetchall()

        for s in sites:
            if s.get('created_at'):
                s['created_at'] = s['created_at'].strftime('%Y-%m-%d %H:%M:%S')

        return jsonify({'success': True, 'sites': sites})
    except Exception as e:
        logging.error("get_alliance_sites error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@table_share_bp.route('/add_alliance_site', methods=['POST'])
@login_required
def add_alliance_site():
    """アライアンスサイトを追加"""
    if not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403

    data = request.json
    site_name = data.get('site_name')
    site_url = data.get('site_url', '').strip().rstrip('/')
    api_key = data.get('api_key')
    description = data.get('description', '')

    if not all([site_name, site_url, api_key]):
        return jsonify({'success': False, 'error': 'パラメータ不足'}), 400

    # URLスキームがない場合は自動補完
    if not site_url.startswith('http://') and not site_url.startswith('https://'):
        site_url = 'https://' + site_url

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        # 既存の登録を確認
        cursor.execute(f"SELECT * FROM {ALLIANCE_SITES} WHERE site_url = %s", (site_url,))
        existing = cursor.fetchone()

        if existing:
            return jsonify({'success': False, 'error': f'このサイトは既に登録されています（{existing["site_name"]}）'}), 400

        now = now_jst()
        user_id = session.get('user_id')

        cursor.execute(f"""
            INSERT INTO {ALLIANCE_SITES}
            (site_name, site_url, api_key, description, is_active, created_at, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (site_name, site_url, api_key, description, True, now, user_id))

        conn.commit()
        return jsonify({'success': True, 'message': 'アライアンスサイトを追加しました'})

    except Exception as e:
        logging.error("add_alliance_site error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@table_share_bp.route('/delete_alliance_site', methods=['POST'])
@login_required
def delete_alliance_site():
    """アライアンスサイトを削除"""
    if not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403

    data = request.json
    site_id = data.get('site_id')

    if not site_id:
        return jsonify({'success': False, 'error': 'site_id が必要です'}), 400

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        # サイトが存在するか確認
        cursor.execute(f"SELECT * FROM {ALLIANCE_SITES} WHERE id = %s", (site_id,))
        site = cursor.fetchone()

        if not site:
            return jsonify({'success': False, 'error': 'サイトが見つかりません'}), 404

        # このサイトを参照している購読設定があるか確認
        cursor.execute(f"SELECT COUNT(*) as cnt FROM {SUBSCRIPTIONS} WHERE alliance_site_id = %s", (site_id,))
        sub_count = cursor.fetchone()['cnt']

        if sub_count > 0:
            return jsonify({
                'success': False,
                'error': f'このサイトには {sub_count} 件の購読設定があります。先に購読設定を解除してください。'
            }), 400

        # 削除
        cursor.execute(f"DELETE FROM {ALLIANCE_SITES} WHERE id = %s", (site_id,))

        conn.commit()
        return jsonify({'success': True, 'message': f'サイト「{site["site_name"]}」を削除しました'})

    except Exception as e:
        logging.error("delete_alliance_site error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@table_share_bp.route('/check_site', methods=['POST'])
@login_required
def check_site():
    """
    URLを指定してFUJIN-P対応サイトかどうかを確認
    対応していればサイト情報を返す
    """
    data = request.json
    site_url = data.get('site_url', '').strip().rstrip('/')

    if not site_url:
        return jsonify({'success': False, 'error': 'URLを入力してください'}), 400

    # URLスキームがない場合は自動補完
    if not site_url.startswith('http://') and not site_url.startswith('https://'):
        site_url = 'https://' + site_url

    try:
        # サイト情報を取得
        info_url = f"{site_url}/table_share/api/site_info"
        response = requests.get(info_url, timeout=10)

        if not response.ok:
            return jsonify({
                'success': False,
                'error': f'接続できません（HTTP {response.status_code}）'
            }), 200

        site_info = response.json()

        if not site_info.get('fujin_p_alliance'):
            return jsonify({
                'success': False,
                'error': 'FUJIN-Pアライアンス対応サイトではありません'
            }), 200

        return jsonify({
            'success': True,
            'site_info': site_info
        })

    except requests.RequestException as e:
        logging.error("check_site network error: %s", e)
        return jsonify({
            'success': False,
            'error': f'接続エラー: {str(e)}'
        }), 200
    except Exception as e:
        logging.error("check_site error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@table_share_bp.route('/get_sync_history', methods=['GET'])
@login_required
def get_sync_history():
    """同期履歴を取得"""
    subscription_id = request.args.get('subscription_id')

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        query = f"""
            SELECT h.*, s.remote_table, s.local_table
            FROM {SYNC_HISTORY} h
            JOIN {SUBSCRIPTIONS} s ON h.subscription_id = s.id
        """
        params = []

        if subscription_id:
            query += " WHERE h.subscription_id = %s"
            params.append(subscription_id)

        query += " ORDER BY h.synced_at DESC LIMIT 100"

        cursor.execute(query, params)
        history = cursor.fetchall()

        for h in history:
            if h.get('synced_at'):
                h['synced_at'] = h['synced_at'].strftime('%Y-%m-%d %H:%M:%S')

        return jsonify({'success': True, 'history': history})
    except Exception as e:
        logging.error("get_sync_history error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


# ============================================
# 権限チェック
# ============================================

def check_table_share_permission(user_id):
    """テーブル共有権限チェック（admin または承認ユーザ）"""
    import sys
    try:
        print(f"[PERMISSION] Checking for user_id={user_id}", file=sys.stderr, flush=True)

        if not user_id:
            print(f"[PERMISSION] user_id is None or empty", file=sys.stderr, flush=True)
            return False

        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        cursor.execute("SELECT id, category FROM users WHERE id = %s", (user_id,))
        user = cursor.fetchone()

        print(f"[PERMISSION] User query result: {user}", file=sys.stderr, flush=True)

        if not user:
            print(f"[PERMISSION] User not found in database", file=sys.stderr, flush=True)
            return False

        # 管理者は許可
        if user['category'] in ['admin']:
            print(f"[PERMISSION] Admin access granted", file=sys.stderr, flush=True)
            return True

        # テーブル共有権限を持つユーザをチェック
        print(f"[PERMISSION] Checking feature permissions for user_id={user_id}", file=sys.stderr, flush=True)
        cursor.execute("""
            SELECT COUNT(*) as count
            FROM user_features uf
            JOIN features f ON uf.feature_id = f.id
            WHERE uf.user_id = %s
              AND f.feature_name = 'テーブル共有権限'
        """, (user_id,))
        result = cursor.fetchone()

        has_feature = result['count'] > 0
        print(f"[PERMISSION] Feature check result: {has_feature} (count={result['count']})", file=sys.stderr, flush=True)

        return has_feature

    except Exception as e:
        print(f"[PERMISSION] Exception: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        import traceback
        print(traceback.format_exc(), file=sys.stderr, flush=True)
        logging.error("check_table_share_permission error: %s", e)
        return False
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


def check_admin_permission(user_id):
    """管理者権限チェック"""
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        cursor.execute("SELECT category FROM users WHERE id = %s", (user_id,))
        user = cursor.fetchone()

        return user and user['category'] == 'admin'

    except Exception as e:
        logging.error("check_admin_permission error: %s", e)
        return False
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


def verify_api_key(api_key):
    """APIキーの検証（簡易実装）"""
    if not api_key:
        return False

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        # API キーがアライアンスサイトに登録されているか確認
        # （自サイトの場合はConfig.TABLE_SHARE_API_KEYと比較）
        expected_key = getattr(Config, 'TABLE_SHARE_API_KEY', None)
        if expected_key and api_key == expected_key:
            return True

        # 登録されたアライアンスサイトのキーを確認
        cursor.execute(f"""
            SELECT COUNT(*) as count FROM {ALLIANCE_SITES}
            WHERE api_key = %s AND is_active = 1
        """, (api_key,))
        result = cursor.fetchone()

        return result['count'] > 0

    except Exception as e:
        logging.error("verify_api_key error: %s", e)
        return False
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()

@table_share_bp.route('/return_to_fujin')
@login_required
def return_to_fujin():
    """FUJIN-Pダッシュボードに戻る"""
    return redirect_to_dashboard()
