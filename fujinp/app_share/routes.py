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
App Share (アプシャ) - FUJIN-Pアプリケーション共有システム
ルート定義

機能:
- 登録済みアプリの管理（レジストリ）
- ユーザマニュアル・技術仕様書の閲覧・編集
- App Info: 技術情報のフィールド編集 / JSONテキスト編集
- 全アプリ情報JSONエクスポート / インポート（インポートはadmin専用・管理タブ）
- アプリ単位のエクスポートパッケージ（JSON）作成（admin専用）
- アプリ単位パッケージの取り込み（admin専用・専用ダッシュボードで検証つき）
- アプリ説明のバージョン（更新日時）記録

※ アプリ説明のバージョン記録には app_share_registry に updated_at 列を追加:
   ALTER TABLE app_share_registry
       ADD COLUMN updated_at DATETIME NULL DEFAULT CURRENT_TIMESTAMP;
   （列が無い環境でも動作するが、その場合はドキュメント・ファイル更新時刻のみで判定）

※ 2026-07 改修: 公開(Publish)・取得・インストール(Subscribe)・
   バックアップ・復旧(Recovery)・履歴(History) 機能を廃止
"""

import os
import re
import sys
import json
import base64
import shutil
import secrets
import datetime
import logging
import importlib.util
import hashlib
from flask import (
    render_template, request, jsonify, session, Response, g, flash, redirect, url_for
)
import mysql.connector
from auth import redirect_to_dashboard
from . import app_share_bp
from config import Config
from db import DatabaseConfig
from decorators import login_required

# 定数
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# BASE_DIR の親（サイトのホーム）。auth.py / config.py / db.py / decorators.py などの
# プラットフォーム共通モジュールと、admin / guest / templates はこちら側に置かれる。
# （'/' 等へ暴走しないよう、親が BASE_DIR と異なる場合のみ採用）
_parent = os.path.dirname(BASE_DIR)
SITE_CODE_ROOT = _parent if (_parent and _parent != BASE_DIR and os.path.isdir(_parent)) else BASE_DIR

# 日時は FUJIN-P 日時3層ルールに従い JST で生成する（サーバのローカル時刻はUTC）
JST = datetime.timezone(datetime.timedelta(hours=9))


def _now_jst():
    return datetime.datetime.now(JST)


def check_admin_permission(user_id):
    """user_id が admin かどうか。
    同一リクエスト内では1回だけDBに問い合わせる（before_request と各ルートの
    二重判定を吸収する）。リクエストをまたいでは持ち越さない。"""
    try:
        cached = getattr(g, '_app_share_admin', None)
    except RuntimeError:          # リクエスト文脈の外
        cached = None
    if cached is not None and cached[0] == user_id:
        return cached[1]

    result = False
    conn = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        # buffered=Trueにして、データをメモリに「吸い切り」ます
        with conn.cursor(dictionary=True, buffered=True) as cursor:
            cursor.execute("SELECT category FROM users WHERE id = %s", (user_id,))
            user = cursor.fetchone()
            # 💡 これが重要：未読パケットを完全に掃除する
            while cursor.nextset(): pass
            result = bool(user and user['category'] == 'admin')
    finally:
        if conn: conn.close()

    try:
        g._app_share_admin = (user_id, result)
    except RuntimeError:
        pass
    return result

# ============================================
# 認可の一元化（deny by default）
#
#   アプシャの全ルートは既定で admin 必須とする。
#   admin 以外にも開放するものだけを _NON_ADMIN_ENDPOINTS に列挙する。
#   → 新しいルートを追加したとき、書き忘れても「admin必須」側に倒れる。
#   → 各ルートのインライン判定は残しておいてよい（二重でも実害はない）。
#
#   未ログイン時は decorators.login_required と同じ挙動（ログイン画面へ）に
#   合わせる。非GET（API呼び出し）だけは 401 JSON を返す。
# ============================================

# admin でなくてもアクセスしてよいエンドポイント（関数名で指定）
_NON_ADMIN_ENDPOINTS = frozenset({
    'dashboard',          # admin以外には app_share_public.html を返す
    'get_registry_apps',  # 公開画面のアプリ一覧
    'get_document',       # マニュアル本文（doc_type='note' は関数内で admin 判定）
    'manual_page',        # マニュアル単独ページ
    'return_to_fujin',    # FUJIN-Pダッシュボードへ戻る
})


@app_share_bp.before_request
def _app_share_authorize():
    """アプシャ全ルートの認可。既定は admin 必須。"""
    name = (request.endpoint or '').rsplit('.', 1)[-1]
    if name in _NON_ADMIN_ENDPOINTS:
        return None

    user_id = session.get('user_id')
    if not user_id:
        if request.method == 'GET':
            flash('ログインが必要です', 'error')
            return redirect(url_for('auth.login', next=request.url))
        return jsonify({'success': False, 'error': 'ログインが必要です'}), 401

    if not check_admin_permission(user_id):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403
    return None

# ============================================
# アプリ説明のバージョン（更新日時）ユーティリティ
# ============================================

def _parse_ts(value):
    """日時文字列を datetime に変換（失敗時 None）"""
    if not value:
        return None
    if isinstance(value, datetime.datetime):
        return value
    s = str(value).strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y/%m/%d %H:%M'):
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            pass
    try:
        return datetime.datetime.fromisoformat(s)
    except Exception:
        return None

# 表示用タイムゾーン（DBの naive datetime はサーバ＝UTCで記録されている）
# ※ JST 定数はファイル先頭で定義済み

def _fmt_jst(dt, fmt='%Y/%m/%d %H:%M'):
    """DBの日時（UTC）を日本時間（JST）の文字列にして返す（None は None）"""
    if not dt:
        return None
    if isinstance(dt, str):
        dt = _parse_ts(dt)
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(JST).strftime(fmt)

def touch_registry_timestamp(app_name, ts=None):
    """アプリ説明の更新日時（バージョン）を registry に記録する。
    app_share_registry.updated_at 列が未追加の環境では警告のみで続行。"""
    conn = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(buffered=True)
        cursor.execute("UPDATE app_share_registry SET updated_at=%s WHERE app_name=%s",
                       (ts or datetime.datetime.now(), app_name))
        conn.commit()
        cursor.close()
    except Exception as e:
        logging.warning(f"registry updated_at 記録スキップ({app_name}): {e}")
    finally:
        if conn and conn.is_connected():
            conn.close()


def _collect_local_timestamps(cursor):
    """registry / documents からアプリごとの更新日時情報を取得。
    戻り値: (reg_ts, doc_ts)  いずれも app_name -> datetime|None
    reg_ts のキー集合 = レジストリ登録済みアプリ"""
    reg_ts = {}
    try:
        cursor.execute("SELECT app_name, updated_at FROM app_share_registry")
        for r in cursor.fetchall():
            reg_ts[r['app_name']] = r['updated_at']
    except mysql.connector.Error:
        # updated_at 列が未追加の場合
        cursor.execute("SELECT app_name FROM app_share_registry")
        for r in cursor.fetchall():
            reg_ts[r['app_name']] = None
    doc_ts = {}
    # 管理ノート（doc_type='note'）はadmin用メモでありアプリ説明の版には含めない
    cursor.execute("""
        SELECT app_name, MAX(updated_at) AS mx
        FROM app_share_documents
        WHERE doc_type IN ('manual', 'spec')
        GROUP BY app_name
    """)
    for r in cursor.fetchall():
        doc_ts[r['app_name']] = r['mx']
    return reg_ts, doc_ts


def _local_app_updated_at(app_name, reg_ts, doc_ts):
    """アプリ説明（registry・ドキュメント・app_info.json・manifest.json）の
    最終更新日時を返す（情報がなければ None）"""
    cands = []
    if reg_ts.get(app_name):
        cands.append(reg_ts[app_name])
    if doc_ts.get(app_name):
        cands.append(doc_ts[app_name])
    app_path = os.path.join(BASE_DIR, app_name)
    for fn in ('app_info.json', 'manifest.json'):
        p = os.path.join(app_path, fn)
        if os.path.exists(p):
            try:
                cands.append(datetime.datetime.fromtimestamp(os.path.getmtime(p)))
            except Exception:
                pass
    return max(cands) if cands else None


# ============================================
# ダッシュボード
# ============================================

@app_share_bp.route('/')
@login_required
def dashboard():
    """アプシャ エントリポイント
    - admin: 従来どおりの管理ダッシュボード（app_share_dashboard.html）
    - admin以外: マニュアル閲覧専用画面（app_share_public.html）
    """
    is_admin = check_admin_permission(session.get('user_id'))
    if is_admin:
        return render_template('app_share_dashboard.html', is_admin=is_admin)
    # admin以外は公開向けマニュアル表示装置
    return render_template('app_share_public.html')



# ============================================
# ローカルアプリ一覧
# ============================================

@app_share_bp.route('/get_local_apps', methods=['GET'])
@login_required
def get_local_apps():
    """ローカルにインストール済みのアプリ一覧を取得"""
    try:
        apps = []

        # ベースディレクトリ内のサブディレクトリを走査
        for item in os.listdir(BASE_DIR):
            app_path = os.path.join(BASE_DIR, item)

            # ディレクトリかつ__init__.pyが存在する場合はアプリとみなす
            init_file = os.path.join(app_path, '__init__.py')
            if os.path.isdir(app_path) and os.path.exists(init_file):
                # 除外リスト
                if item in ['__pycache__', 'static', 'templates', 'app_share_backups']:
                    continue

                app_info = {
                    'name': item,
                    'path': app_path,
                    'has_routes': os.path.exists(os.path.join(app_path, 'routes.py')),
                    'has_templates': os.path.exists(os.path.join(app_path, 'templates')),
                    'has_schema': os.path.exists(os.path.join(app_path, 'schema.sql')),
                    'has_manifest': os.path.exists(os.path.join(app_path, 'manifest.json')),
                    'has_app_info': os.path.exists(os.path.join(app_path, 'app_info.json'))
                }

                # manifest.jsonがあれば読み込む
                manifest_path = os.path.join(app_path, 'manifest.json')
                if os.path.exists(manifest_path):
                    try:
                        with open(manifest_path, 'r', encoding='utf-8') as f:
                            manifest = json.load(f)
                            app_info['manifest'] = manifest
                    except:
                        pass

                apps.append(app_info)

        # 名前順でソート
        apps.sort(key=lambda x: x['name'])

        return jsonify({'success': True, 'apps': apps})

    except Exception as e:
        logging.error("get_local_apps error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================
# アプリ技術情報の閲覧・編集（手動編集仕様）
# ============================================

def get_initial_template(app_name, app_path):
    """初期テンプレートを生成（各項目に値・備考・ヒント付き）"""

    # schema.sqlがあれば読み込む
    schema_content = ''
    schema_path = os.path.join(app_path, 'schema.sql')
    if os.path.exists(schema_path):
        with open(schema_path, 'r', encoding='utf-8') as f:
            schema_content = f.read()

    # __init__.pyからBlueprint情報を推測
    blueprint_name = f'{app_name}_bp'
    url_prefix = f'/{app_name}'
    init_path = os.path.join(app_path, '__init__.py')
    if os.path.exists(init_path):
        with open(init_path, 'r', encoding='utf-8') as f:
            content = f.read()
            import re
            bp_match = re.search(r'(\w+_bp)\s*=\s*Blueprint', content)
            if bp_match:
                blueprint_name = bp_match.group(1)
            prefix_match = re.search(r"url_prefix\s*=\s*['\"]([^'\"]+)['\"]", content)
            if prefix_match:
                url_prefix = prefix_match.group(1)

    return {
        'general_notes': f'''# {app_name} 技術情報
# このファイルはアプリの技術ドキュメントです。
# 各項目を編集し、保存ボタンで app_info.json に保存されます。''',

        'fields': {
            'display_name': {
                'value': app_name,
                'note': '',
                'hint': '【取得方法】manifest.json または任意に命名\n【サンプル】マイノート、テーシャ'
            },
            'description': {
                'value': '',
                'note': '',
                'hint': '【取得方法】アプリの目的を1-2行で記述\n【サンプル】ユーザーがMarkdown形式でノートを管理・共有するためのWebアプリケーション'
            },
            'url': {
                'value': f'{url_prefix}/',
                'note': '',
                'hint': '【取得方法】__init__.py の url_prefix を確認\n【サンプル】/my_md_notes/'
            },
            'blueprint_name': {
                'value': blueprint_name,
                'note': '',
                'hint': '【取得方法】__init__.py で定義された Blueprint 変数名\n【サンプル】my_md_notes_bp'
            },
            'overview': {
                'value': '',
                'note': '',
                'hint': '''【取得方法】README.md や docstring から抜粋、または新規作成
【サンプル】
## 概要
ユーザーがMarkdown形式で情報を管理・共有するためのWebアプリケーション

## 主な機能
- リアルタイムプレビュー
- KaTeXによる数式表示'''
            },
            'directory_structure': {
                'value': '',
                'note': '',
                'hint': f'''【取得方法】ファイルマネージャ、ls -la、tree コマンド
【サンプル】
{app_name}/
├── __init__.py
├── routes.py
├── schema.sql
└── templates/
    └── index.html'''
            },
            'python_files': {
                'value': '',
                'note': '',
                'hint': '''【取得方法】find . -name "*.py" | grep -v __pycache__
【サンプル】
__init__.py - Blueprint定義
routes.py - ルート定義（主要ロジック）'''
            },
            'template_files': {
                'value': '',
                'note': '',
                'hint': '''【取得方法】ls templates/
【サンプル】
index.html - 一覧画面
edit.html - 編集画面'''
            },
            'endpoints': {
                'value': '',
                'note': '',
                'hint': '''【取得方法】routes.py の @xxx.route() を確認
【サンプル】
GET  /             - インデックス画面
GET  /edit/<id>    - 編集画面
POST /save/<id>    - 保存処理
POST /delete/<id>  - 削除処理'''
            },
            'mysql_schema': {
                'value': schema_content,
                'note': '',
                'hint': '''【取得方法】schema.sql の内容をコピー
【サンプル】
CREATE TABLE IF NOT EXISTS example_table (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;'''
            },
            'sql_tables_description': {
                'value': '',
                'note': '',
                'hint': '''【取得方法】schema.sql を読み、各カラムの意味を記述
【サンプル】
## example_table
- id: 主キー（自動採番）
- user_id: 所有ユーザーID
- created_at: 作成日時'''
            },
            'libraries': {
                'value': '',
                'note': '',
                'hint': '''【取得方法】routes.py の import 文を確認
【サンプル】
Flask - Webフレームワーク
mysql-connector-python - MySQL接続
pytz - タイムゾーン処理'''
            },
            'migration_guide': {
                'value': f'''1. DB構築
   - schema.sql をMySQLで実行
   - 対象DB: user_account$default

2. モジュール配置
   - {app_name}/ ディレクトリを配置

3. app.py への追記
   from {app_name} import {blueprint_name}
   app.register_blueprint({blueprint_name})

4. ダッシュボードへの追記
   admin_dashboard.html に追加

5. Webアプリをリロード''',
                'note': '',
                'hint': '【取得方法】インストール手順を順番に記述'
            },
            'config_notes': {
                'value': '',
                'note': '',
                'hint': '''【取得方法】config.py で必要な設定を確認
【サンプル】
# config.py に追記
MY_APP_API_KEY = 'your-api-key'
MY_APP_SECRET = 'your-secret'

# 設定不要の場合は空欄'''
            }
        }
    }


@app_share_bp.route('/get_app_info/<app_name>', methods=['GET'])
@login_required
def get_app_info(app_name):
    """アプリの技術情報を取得（手動編集用）"""

    app_path = os.path.join(BASE_DIR, app_name)

    # ディレクトリが存在しない場合も空テンプレートを返す（404にしない）
    if not os.path.isdir(app_path):
        empty = get_initial_template(app_name, app_path)
        return jsonify({'success': True, 'app_name': app_name, 'app_info': empty})

    try:
        info_path = os.path.join(app_path, 'app_info.json')

        if os.path.exists(info_path):
            with open(info_path, 'r', encoding='utf-8') as f:
                app_info = json.load(f)

            # 新形式（fields構造）かチェック、古い形式なら変換
            if 'fields' not in app_info:
                template = get_initial_template(app_name, app_path)
                old_info = app_info
                app_info = template

                # structured からの移行
                if 'structured' in old_info:
                    for key in ['directory_structure', 'python_files', 'template_files', 'mysql_schema', 'libraries']:
                        if key in old_info['structured']:
                            val = old_info['structured'][key]
                            if isinstance(val, list):
                                val = '\n'.join(val)
                            if key in app_info['fields']:
                                app_info['fields'][key]['value'] = str(val)

                # human_readable からの移行
                if 'human_readable' in old_info:
                    mapping = {
                        'display_name': 'display_name',
                        'description': 'description',
                        'url': 'url',
                        'overview': 'overview',
                        'endpoints': 'endpoints',
                        'sql_tables': 'sql_tables_description',
                        'migration_guide': 'migration_guide',
                        'config_notes': 'config_notes',
                        'blueprint_name': 'blueprint_name'
                    }
                    for old_key, new_key in mapping.items():
                        if old_key in old_info['human_readable']:
                            val = old_info['human_readable'][old_key]
                            if isinstance(val, list):
                                val = '\n'.join(str(v) for v in val)
                            if new_key in app_info['fields']:
                                app_info['fields'][new_key]['value'] = str(val)
        else:
            app_info = get_initial_template(app_name, app_path)

        return jsonify({'success': True, 'app_name': app_name, 'app_info': app_info})

    except Exception as e:
        logging.error("get_app_info error: %s", e)
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app_share_bp.route('/save_app_info/<app_name>', methods=['POST'])
@login_required
def save_app_info(app_name):
    """アプリの技術情報を保存"""
    if not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403

    app_path = os.path.join(BASE_DIR, app_name)

    # ★ 変更点: ディレクトリがなければ作成する（404にしない）
    if not os.path.isdir(app_path):
        try:
            os.makedirs(app_path, exist_ok=True)
            logging.info(f"app_info保存のため、ディレクトリを作成: {app_path}")
        except Exception as e:
            return jsonify({'success': False, 'error': f'ディレクトリ作成失敗: {str(e)}'}), 500

    try:
        data = request.json
        app_info = data.get('app_info', {})

        info_path = os.path.join(app_path, 'app_info.json')
        with open(info_path, 'w', encoding='utf-8') as f:
            json.dump(app_info, f, ensure_ascii=False, indent=2)

        # アプリ説明のバージョン（更新日時）を記録
        touch_registry_timestamp(app_name)

        return jsonify({'success': True, 'message': 'アプリ情報を保存しました'})

    except Exception as e:
        logging.error("save_app_info error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================
# JSONテキスト編集用エンドポイント
# ============================================

@app_share_bp.route('/get_app_info_json/<app_name>', methods=['GET'])
@login_required
def get_app_info_json(app_name):
    """アプリの技術情報をJSONテキストとして取得（コピー・エクスポート用）"""

    app_path = os.path.join(BASE_DIR, app_name)
    if not os.path.isdir(app_path):
        return jsonify({'success': False, 'error': f'アプリ "{app_name}" が見つかりません'}), 404

    try:
        info_path = os.path.join(app_path, 'app_info.json')

        if os.path.exists(info_path):
            with open(info_path, 'r', encoding='utf-8') as f:
                app_info = json.load(f)
        else:
            app_info = get_initial_template(app_name, app_path)

        # 整形済みJSONテキストを返す
        json_text = json.dumps(app_info, ensure_ascii=False, indent=2)
        return jsonify({
            'success': True,
            'app_name': app_name,
            'json_text': json_text
        })

    except Exception as e:
        logging.error("get_app_info_json error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@app_share_bp.route('/save_app_info_json/<app_name>', methods=['POST'])
@login_required
def save_app_info_json(app_name):
    """JSONテキストからアプリの技術情報を保存（インポート用）"""
    if not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403

    app_path = os.path.join(BASE_DIR, app_name)

    # ★ 変更点: ディレクトリがなければ作成する（404にしない）
    if not os.path.isdir(app_path):
        try:
            os.makedirs(app_path, exist_ok=True)
            logging.info(f"app_info_json保存のため、ディレクトリを作成: {app_path}")
        except Exception as e:
            return jsonify({'success': False, 'error': f'ディレクトリ作成失敗: {str(e)}'}), 500

    try:
        data = request.json
        json_text = data.get('json_text', '')
        if not json_text.strip():
            return jsonify({'success': False, 'error': 'JSONテキストが空です'}), 400

        # JSONとしてパース可能か検証
        try:
            app_info = json.loads(json_text)
        except json.JSONDecodeError as je:
            return jsonify({
                'success': False,
                'error': f'JSON構文エラー: {str(je)}'
            }), 400

        # 基本的な構造チェック（fieldsキーの存在）
        if not isinstance(app_info, dict):
            return jsonify({'success': False, 'error': 'JSONはオブジェクト形式である必要があります'}), 400

        # fields が無い場合、簡易形式として受け入れる
        # （キー名がフィールド名で値が文字列の場合、fields構造に変換）
        if 'fields' not in app_info:
            known_fields = [
                'display_name', 'description', 'url', 'blueprint_name',
                'overview', 'directory_structure', 'python_files', 'template_files',
                'endpoints', 'mysql_schema', 'sql_tables_description', 'libraries',
                'migration_guide', 'config_notes'
            ]
            # 簡易形式チェック: トップレベルのキーがフィールド名か確認
            is_simple = any(k in app_info for k in known_fields)

            if is_simple:
                # 簡易形式 → fields構造に変換
                template = get_initial_template(app_name, app_path)
                for key in known_fields:
                    if key in app_info:
                        val = app_info[key]
                        if isinstance(val, list):
                            val = '\n'.join(str(v) for v in val)
                        if key in template['fields']:
                            template['fields'][key]['value'] = str(val)
                # general_notes があれば取り込む
                if 'general_notes' in app_info:
                    template['general_notes'] = str(app_info['general_notes'])
                app_info = template

        info_path = os.path.join(app_path, 'app_info.json')
        with open(info_path, 'w', encoding='utf-8') as f:
            json.dump(app_info, f, ensure_ascii=False, indent=2)

        # アプリ説明のバージョン（更新日時）を記録
        touch_registry_timestamp(app_name)

        return jsonify({'success': True, 'message': 'JSONからアプリ情報を保存しました'})

    except Exception as e:
        logging.error("save_app_info_json error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@app_share_bp.route('/validate_json', methods=['POST'])
@login_required
def validate_json():
    """JSONテキストの構文を検証"""
    try:
        data = request.json
        json_text = data.get('json_text', '')

        if not json_text.strip():
            return jsonify({'valid': False, 'error': 'テキストが空です'})

        parsed = json.loads(json_text)

        if not isinstance(parsed, dict):
            return jsonify({'valid': False, 'error': 'オブジェクト形式ではありません'})

        # フィールド数をカウント
        if 'fields' in parsed:
            field_count = len(parsed['fields'])
            return jsonify({
                'valid': True,
                'format': 'standard',
                'field_count': field_count,
                'message': f'正規形式 ({field_count}フィールド)'
            })
        else:
            known_fields = [
                'display_name', 'description', 'url', 'blueprint_name',
                'overview', 'directory_structure', 'python_files', 'template_files',
                'endpoints', 'mysql_schema', 'sql_tables_description', 'libraries',
                'migration_guide', 'config_notes'
            ]
            found = [k for k in known_fields if k in parsed]
            if found:
                return jsonify({
                    'valid': True,
                    'format': 'simple',
                    'field_count': len(found),
                    'message': f'簡易形式 ({len(found)}フィールド検出) → 保存時に正規形式に変換されます'
                })
            else:
                return jsonify({
                    'valid': True,
                    'format': 'unknown',
                    'message': '有効なJSONですが、既知のフィールドが見つかりません'
                })

    except json.JSONDecodeError as je:
        # エラー位置を特定
        return jsonify({
            'valid': False,
            'error': f'行{je.lineno}, 列{je.colno}: {je.msg}'
        })
    except Exception as e:
        return jsonify({'valid': False, 'error': str(e)})


# ============================================
# アプリカード編集（アイコン・表示名）
# ============================================

@app_share_bp.route('/update_app_card/<app_name>', methods=['POST'])
@login_required
def update_app_card(app_name):
    """アプリカードのアイコンと表示名を更新（manifest.jsonに保存）"""
    data = request.json
    icon = data.get('icon', '📦')
    display_name = data.get('display_name', app_name)

    app_dir = os.path.join(BASE_DIR, app_name)
    if not os.path.isdir(app_dir):
        return jsonify({'success': False, 'error': 'アプリが見つかりません'}), 404

    manifest_path = os.path.join(app_dir, 'manifest.json')
    manifest = {}
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest = json.load(f)
        except:
            pass

    manifest['icon'] = icon
    manifest['display_name'] = display_name

    try:
        with open(manifest_path, 'w', encoding='utf-8') as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        # アプリ説明のバージョン（更新日時）を記録
        touch_registry_timestamp(app_name)
        return jsonify({'success': True, 'message': '更新しました'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500



# ============================================
# アプリレジストリ（guest閲覧対応）
# 閲覧: login_required のみ
# 編集: check_admin_permission 必須
# ============================================

# --- 閲覧（guest OK） ---

@app_share_bp.route('/get_registry_apps')
@login_required
def get_registry_apps():
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True, buffered=True)
        cursor.execute("""
            SELECT * FROM app_share_registry ORDER BY sort_order ASC, id ASC
        """)
        apps = cursor.fetchall()
        for a in apps:
            if a.get('updated_at'):
                a['updated_at'] = a['updated_at'].strftime('%Y-%m-%d %H:%M:%S')
            a['version_id'] = _get_version_id(a.get('app_name'))
            # sort_order は DOUBLE。列が DECIMAL のサイトでは Decimal が返り
            # jsonify が落ちるので float に正規化しておく。
            try:
                a['sort_order'] = float(a.get('sort_order') or 0)
            except (TypeError, ValueError):
                a['sort_order'] = 0.0
        return jsonify({'success': True, 'apps': apps})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'cursor' in locals(): cursor.close()
        if 'conn' in locals() and conn.is_connected(): conn.close()


# --- 以下すべて admin 限定 ---

@app_share_bp.route('/confirm_version/<app_name>', methods=['POST'])
@login_required
def confirm_version(app_name):
    """バージョン確定（admin専用）。ソースコード・技術仕様書・ユーザマニュアルの整合性を
    人手で確認したうえで、現時点のバージョン番号を刻む。押されたときだけ version.json を
    更新し、以後はエクスポートを繰り返しても同じバージョン番号が出る。
    実験版のため過去バージョンの履歴は保持しない（毎回上書き）。"""
    if not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403
    if not re.match(r'^[A-Za-z0-9_]+$', app_name or ''):
        return jsonify({'success': False, 'error': 'アプリ名が不正です'}), 400
    app_path = os.path.join(BASE_DIR, app_name)
    if not os.path.isdir(app_path):
        return jsonify({'success': False, 'error': f'アプリ "{app_name}" が見つかりません'}), 404

    now = datetime.datetime.now()
    version_id = 'v{}-{}'.format(now.strftime('%Y%m%d.%H%M%S'), _content_hash6(app_name))

    # 確定者名（任意・表示用）
    confirmed_by = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True, buffered=True)
        cursor.execute("SELECT full_name FROM users WHERE id = %s", (session.get('user_id'),))
        row = cursor.fetchone()
        if row:
            confirmed_by = row.get('full_name')
    except Exception:
        pass
    finally:
        if 'cursor' in locals(): cursor.close()
        if 'conn' in locals() and conn.is_connected(): conn.close()

    data = {'version_id': version_id,
            'confirmed_at': now.strftime('%Y-%m-%d %H:%M:%S'),
            'confirmed_by': confirmed_by}
    try:
        with open(_version_file_path(app_name), 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return jsonify({'success': False, 'error': f'保存に失敗しました: {e}'}), 500

    return jsonify({'success': True, **data})


@app_share_bp.route('/registry/add', methods=['POST'])
@login_required
def registry_add():
    if not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403
    data = request.get_json()
    app_name = data.get('app_name', '').strip()
    display_name = data.get('display_name', '').strip() or app_name
    icon = data.get('icon', '📦').strip()
    description = data.get('description', '').strip()
    if not app_name:
        return jsonify({'success': False, 'error': 'アプリ名は必須です'}), 400
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True, buffered=True)
        cursor.execute("SELECT COALESCE(MAX(sort_order),0)+10 AS n FROM app_share_registry")
        nxt = cursor.fetchone()['n']
        try:
            cursor.execute("INSERT INTO app_share_registry (app_name,display_name,icon,description,sort_order,updated_at) VALUES(%s,%s,%s,%s,%s,%s)",
                           (app_name, display_name, icon, description, nxt, datetime.datetime.now()))
        except mysql.connector.Error:
            # updated_at 列が未追加の場合（Duplicate entry もここを通り、外側で処理される）
            cursor.execute("INSERT INTO app_share_registry (app_name,display_name,icon,description,sort_order) VALUES(%s,%s,%s,%s,%s)",
                           (app_name, display_name, icon, description, nxt))
        conn.commit()
        return jsonify({'success': True, 'message': f'「{display_name}」を登録しました'})
    except Exception as e:
        if 'conn' in locals(): conn.rollback()
        if 'Duplicate entry' in str(e):
            return jsonify({'success': False, 'error': f'「{app_name}」は既に登録されています'}), 400
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'cursor' in locals(): cursor.close()
        if 'conn' in locals() and conn.is_connected(): conn.close()


@app_share_bp.route('/registry/delete/<int:reg_id>', methods=['DELETE'])
@login_required
def registry_delete(reg_id):
    if not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True, buffered=True)
        cursor.execute("SELECT app_name, display_name FROM app_share_registry WHERE id=%s", (reg_id,))
        app = cursor.fetchone()
        if not app:
            return jsonify({'success': False, 'error': '見つかりません'}), 404
        cursor.execute("DELETE FROM app_share_registry WHERE id=%s", (reg_id,))
        conn.commit()
        return jsonify({'success': True, 'message': f'「{app["display_name"]}」を削除しました（ファイルは残ります）'})
    except Exception as e:
        if 'conn' in locals(): conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'cursor' in locals(): cursor.close()
        if 'conn' in locals() and conn.is_connected(): conn.close()


@app_share_bp.route('/registry/update/<int:reg_id>', methods=['POST'])
@login_required
def registry_update(reg_id):
    """レジストリ1行の更新（admin専用）。
    表示名・アイコン・概要に加えて、
      sort_order : 表示順（任意の実数。小さい順に並ぶ）
      app_name   : アプリのディレクトリ名（手入力なので打ち間違いを直せるように）
    を変更できる。app_name を変えるときは実在するディレクトリであることを
    確認したうえで、app_share_documents の同名参照も同一トランザクションで
    付け替える（ex_engagement → ext_engagement の取りこぼしを繰り返さない）。
    """
    if not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403
    data = request.get_json() or {}
    display_name = (data.get('display_name') or '').strip()
    icon = (data.get('icon') or '📦').strip()
    description = (data.get('description') or '').strip()
    if not display_name:
        return jsonify({'success': False, 'error': '表示名は必須です'}), 400

    # 表示順：空欄なら現在値を変えない
    raw_sort = data.get('sort_order')
    sort_order = None
    if raw_sort not in (None, ''):
        try:
            sort_order = float(raw_sort)
        except (TypeError, ValueError):
            return jsonify({'success': False,
                            'error': '表示順は数値で入力してください（小数可）'}), 400

    new_app_name = (data.get('app_name') or '').strip()

    conn = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True, buffered=True)
        cursor.execute("SELECT app_name FROM app_share_registry WHERE id=%s", (reg_id,))
        row = cursor.fetchone()
        if not row:
            return jsonify({'success': False, 'error': '見つかりません'}), 404
        old_app_name = row['app_name']

        renamed = bool(new_app_name and new_app_name != old_app_name)
        if renamed:
            if not re.match(r'^[A-Za-z0-9_]+$', new_app_name):
                return jsonify({'success': False,
                                'error': 'ディレクトリ名は英数字とアンダースコアのみです'}), 400
            # 実在確認。打ち間違いを別の打ち間違いに置き換えても意味がない。
            if not os.path.exists(os.path.join(BASE_DIR, new_app_name, '__init__.py')):
                return jsonify({'success': False,
                                'error': f'{new_app_name}/__init__.py が見つかりません。'
                                         'ディレクトリ名を確認してください'}), 400
            cursor.execute("SELECT id FROM app_share_registry WHERE app_name=%s AND id<>%s",
                           (new_app_name, reg_id))
            if cursor.fetchone():
                return jsonify({'success': False,
                                'error': f'「{new_app_name}」は既に別の行で登録されています'}), 400
            # 付け替え先に既にマニュアル／仕様書があると
            # unique(app_name, doc_type) に衝突するので先に知らせる。
            cursor.execute("SELECT COUNT(*) AS c FROM app_share_documents WHERE app_name=%s",
                           (new_app_name,))
            if cursor.fetchone()['c']:
                return jsonify({'success': False,
                                'error': f'「{new_app_name}」名義のマニュアル／仕様書が'
                                         '既にあります。先にそちらを整理してください'}), 400

        sets = ['display_name=%s', 'icon=%s', 'description=%s']
        params = [display_name, icon, description]
        if sort_order is not None:
            sets.append('sort_order=%s'); params.append(sort_order)
        if renamed:
            sets.append('app_name=%s'); params.append(new_app_name)

        try:
            cursor.execute("UPDATE app_share_registry SET " + ','.join(sets) +
                           ", updated_at=%s WHERE id=%s",
                           params + [datetime.datetime.now(), reg_id])
        except mysql.connector.Error:
            # updated_at 列が未追加の場合
            cursor.execute("UPDATE app_share_registry SET " + ','.join(sets) +
                           " WHERE id=%s", params + [reg_id])

        moved_docs = 0
        if renamed:
            cursor.execute("UPDATE app_share_documents SET app_name=%s WHERE app_name=%s",
                           (new_app_name, old_app_name))
            moved_docs = cursor.rowcount
        conn.commit()

        msg = '更新しました'
        if renamed:
            msg = (f'ディレクトリ名を {old_app_name} → {new_app_name} に変更しました'
                   f'（マニュアル／仕様書 {moved_docs} 件も付け替え）')
        return jsonify({'success': True, 'message': msg})
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'cursor' in locals(): cursor.close()
        if conn and conn.is_connected(): conn.close()


@app_share_bp.route('/registry/move/<int:reg_id>/<direction>', methods=['POST'])
@login_required
def registry_move(reg_id, direction):
    if not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403
    if direction not in ('up', 'down'):
        return jsonify({'success': False, 'error': '不正な方向'}), 400
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True, buffered=True)
        cursor.execute("SELECT id, sort_order FROM app_share_registry WHERE id=%s", (reg_id,))
        cur = cursor.fetchone()
        if not cur:
            return jsonify({'success': False, 'error': '見つかりません'}), 404
        if direction == 'up':
            cursor.execute("""SELECT id, sort_order FROM app_share_registry
                WHERE sort_order < %s OR (sort_order=%s AND id < %s)
                ORDER BY sort_order DESC, id DESC LIMIT 1""",
                (cur['sort_order'], cur['sort_order'], cur['id']))
        else:
            cursor.execute("""SELECT id, sort_order FROM app_share_registry
                WHERE sort_order > %s OR (sort_order=%s AND id > %s)
                ORDER BY sort_order ASC, id ASC LIMIT 1""",
                (cur['sort_order'], cur['sort_order'], cur['id']))
        nb = cursor.fetchone()
        if not nb:
            return jsonify({'success': True, 'message': '端です'})
        cursor.execute("UPDATE app_share_registry SET sort_order=%s WHERE id=%s", (nb['sort_order'], cur['id']))
        cursor.execute("UPDATE app_share_registry SET sort_order=%s WHERE id=%s", (cur['sort_order'], nb['id']))
        conn.commit()
        return jsonify({'success': True, 'message': '移動しました'})
    except Exception as e:
        if 'conn' in locals(): conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'cursor' in locals(): cursor.close()
        if 'conn' in locals() and conn.is_connected(): conn.close()


@app_share_bp.route('/registry/migrate', methods=['POST'])
@login_required
def registry_migrate():
    if not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403
    try:
        found_apps = []
        for item in os.listdir(BASE_DIR):
            app_path = os.path.join(BASE_DIR, item)
            init_file = os.path.join(app_path, '__init__.py')
            if os.path.isdir(app_path) and os.path.exists(init_file):
                if item in ['__pycache__', 'static', 'templates', 'app_share_backups']:
                    continue
                icon = '📦'
                display_name = item
                manifest_path = os.path.join(app_path, 'manifest.json')
                if os.path.exists(manifest_path):
                    try:
                        with open(manifest_path, 'r', encoding='utf-8') as f:
                            manifest = json.load(f)
                            icon = manifest.get('icon', '📦')
                            display_name = manifest.get('display_name', item)
                    except: pass
                description = ''
                info_path = os.path.join(app_path, 'app_info.json')
                if os.path.exists(info_path):
                    try:
                        with open(info_path, 'r', encoding='utf-8') as f:
                            info = json.load(f)
                            if 'fields' in info and 'description' in info['fields']:
                                description = info['fields']['description'].get('value', '')[:200]
                            elif 'description' in info:
                                description = str(info['description'])[:200]
                    except: pass
                found_apps.append({'app_name': item, 'display_name': display_name, 'icon': icon, 'description': description})
        found_apps.sort(key=lambda x: x['app_name'])
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True, buffered=True)
        registered = skipped = 0
        for i, app in enumerate(found_apps):
            try:
                cursor.execute("INSERT INTO app_share_registry (app_name,display_name,icon,description,sort_order) VALUES(%s,%s,%s,%s,%s)",
                               (app['app_name'], app['display_name'], app['icon'], app['description'], (i+1)*10))
                registered += 1
            except mysql.connector.IntegrityError:
                skipped += 1
        conn.commit()
        return jsonify({'success': True, 'message': f'{registered}件登録, {skipped}件スキップ', 'registered': registered, 'skipped': skipped})
    except Exception as e:
        if 'conn' in locals(): conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'cursor' in locals(): cursor.close()
        if 'conn' in locals() and conn.is_connected(): conn.close()

# ============================================
# ドキュメント（guest閲覧対応）
# 閲覧: login_required のみ
# 編集: check_admin_permission 必須
# ============================================

# --- 閲覧（guest OK） ---

@app_share_bp.route('/doc/<app_name>/<doc_type>/get', methods=['GET'])
@login_required
def get_document(app_name, doc_type):
    if doc_type not in ('manual', 'spec', 'note'):
        return jsonify({'success': False, 'error': '不正なドキュメントタイプ'}), 400
    # 管理ノートは admin 専用
    if doc_type == 'note' and not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403

    conn = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        # 💡 buffered=True を指定して、結果をメモリに完全に吸い出す
        with conn.cursor(dictionary=True, buffered=True) as cursor:
            cursor.execute("""
                SELECT d.*, u.full_name as updated_by_name
                FROM app_share_documents d
                LEFT JOIN users u ON d.updated_by = u.id
                WHERE d.app_name = %s AND d.doc_type = %s
            """, (app_name, doc_type))
            doc = cursor.fetchone()
            # 💡 完全に結果を使い切るための魔法の1行
            while cursor.nextset(): pass

        if doc:
            doc['updated_at'] = _fmt_jst(doc['updated_at'])
            doc['created_at'] = _fmt_jst(doc['created_at'])
        return jsonify({'success': True, 'doc': doc})
    except Exception as e:
        logging.error(f"Error in get_document: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            conn.close()


# --- 以下 admin 限定 ---

@app_share_bp.route('/doc/<app_name>/<doc_type>/save', methods=['POST'])
@login_required
def save_document(app_name, doc_type):
    if not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403
    if doc_type not in ('manual', 'spec', 'note'):
        return jsonify({'success': False, 'error': '不正なドキュメントタイプ'}), 400
    data = request.get_json()
    title = data.get('title', '').strip()
    content = data.get('content', '')
    user_id = session.get('user_id')
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(buffered=True)
        cursor.execute("""
            INSERT INTO app_share_documents (app_name, doc_type, title, content, updated_by)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                title = VALUES(title), content = VALUES(content),
                updated_by = VALUES(updated_by), updated_at = CURRENT_TIMESTAMP
        """, (app_name, doc_type, title, content, user_id))
        conn.commit()
        # アプリ説明のバージョン（更新日時）を記録
        touch_registry_timestamp(app_name)
        return jsonify({'success': True, 'message': '保存しました'})
    except Exception as e:
        if 'conn' in locals(): conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'cursor' in locals(): cursor.close()
        if 'conn' in locals() and conn.is_connected(): conn.close()


@app_share_bp.route('/doc/preview', methods=['POST'])
@login_required
def preview_document_markdown():
    # セッション更新の副作用（MySQL衝突）を強制停止
    session.modified = False

    try:
        markdown_text = request.form.get('markdown', '')

        # 💡 もし markdown_converter が内部でDB接続を行っている場合、
        # ここで別のエラーが連鎖します。
        from markdown_converter import process_markdown
        html = process_markdown(markdown_text, 'admin')

        return Response(html, mimetype='text/html')
    except Exception as e:
        # 💡 エラー内容をログに詳細に出力して、どこで「未読」が起きたか特定できるようにします
        logging.error(f"PREVIEW ERROR: {str(e)}")
        # ユーザーには「エラーが発生しました」という文字だけ返し、DB接続をハングさせない
        return Response(f"Preview error: {str(e)}", mimetype='text/html')

@app_share_bp.route('/doc/<app_name>/<doc_type>/edit')
@login_required
def edit_document(app_name, doc_type):
    if not check_admin_permission(session.get('user_id')):
        return "管理者権限が必要です", 403
    if doc_type not in ('manual', 'spec', 'note'):
        return "不正なドキュメントタイプ", 400

    doc = None
    conn = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        # 💡 buffered=True を追加
        with conn.cursor(dictionary=True, buffered=True) as cursor:
            cursor.execute("SELECT * FROM app_share_documents WHERE app_name=%s AND doc_type=%s", (app_name, doc_type))
            doc = cursor.fetchone()
            # 💡 fetchoneの後、残留パケットを掃除する
            while cursor.nextset(): pass
    except Exception as e:
        logging.error(f"Error in edit_document: {e}")
        doc = None
    finally:
        if conn and conn.is_connected():
            conn.close()

    if not doc:
        doc = {'app_name': app_name, 'doc_type': doc_type, 'title': '', 'content': ''}

    title_label = {'manual': 'ユーザマニュアル', 'spec': '技術仕様書',
                   'note': '管理ノート'}.get(doc_type, doc_type)
    return render_template('app_share_edit_doc.html',
                           app_name=app_name, doc_type=doc_type,
                           title_label=title_label, doc=doc)

# ============================================
# マニュアル単独ページ（URL直アクセス用）
#   GET /app_share/manual/<app_name>
#   モーダルではなく独立したページとして表示するので、URLを
#   ブックマーク・共有・他アプリからのリンクに使える。
#   閲覧: login_required のみ（モーダル版 get_document と同じ）
#   admin には編集ボタンを表示する。
# ============================================

@app_share_bp.route('/manual/<app_name>')
@login_required
def manual_page(app_name):
    """ユーザーズマニュアルの単独ページ"""
    is_admin = check_admin_permission(session.get('user_id'))
    display_name = app_name
    icon = '📖'
    doc = None
    conn = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        with conn.cursor(dictionary=True, buffered=True) as cursor:
            cursor.execute(
                "SELECT display_name, icon FROM app_share_registry WHERE app_name = %s",
                (app_name,))
            reg = cursor.fetchone()
            while cursor.nextset(): pass
            if reg:
                display_name = reg['display_name'] or app_name
                icon = reg['icon'] or '📖'
        with conn.cursor(dictionary=True, buffered=True) as cursor:
            cursor.execute("""
                SELECT d.title, d.content, d.updated_at, u.full_name AS updated_by_name
                FROM app_share_documents d
                LEFT JOIN users u ON d.updated_by = u.id
                WHERE d.app_name = %s AND d.doc_type = 'manual'
            """, (app_name,))
            doc = cursor.fetchone()
            while cursor.nextset(): pass
    except Exception as e:
        logging.error(f"manual_page error: {e}")
    finally:
        if conn and conn.is_connected():
            conn.close()

    updated_at = ''
    updated_by_name = ''
    content = ''
    if doc:
        content = doc.get('content') or ''
        updated_by_name = doc.get('updated_by_name') or ''
        if doc.get('updated_at'):
            updated_at = _fmt_jst(doc['updated_at'])

    return render_template('app_share_manual_page.html',
                           app_name=app_name,
                           display_name=display_name,
                           icon=icon,
                           content=content,
                           updated_at=updated_at,
                           updated_by_name=updated_by_name,
                           is_admin=is_admin)


@app_share_bp.route('/return_to_fujin')
@login_required
def return_to_fujin():
    """FUJINダッシュボードに戻る"""
    return redirect_to_dashboard()

# ============================================
# 全アプリ情報JSONエクスポート
#
# 機能:
#   GET /app_share/export_site_overview
#   「登録済みアプリ」（app_share_registry）に登録された全アプリと、
#   登録されていないローカルアプリも含めて、
#     - 登録簿情報（表示名・アイコン・概要・表示順）
#     - 実ファイル構成（routes.py / templates / schema.sql 等の有無）
#     - manifest.json
#     - 技術仕様書 app_info.json（生成AI向けに value / note のみへ簡約）
#     - ユーザマニュアル（app_share_documents doc_type='manual'）
#     - 仕様書メモ（doc_type='spec'）
#   を1つのJSONに構造化し、ファイルとしてダウンロードさせる。
#
#   権限: admin専用（_app_share_authorize による）。
#   全アプリのソース一式と、このサイトの実DBから読んだスキーマを含むため、
#   閲覧系（マニュアル・app_info）とは別扱いにしている。
# ============================================

@app_share_bp.route('/export_site_overview', methods=['GET'])
@login_required
def export_site_overview():
    """登録済みアプリ（＋未登録ローカルアプリ）の全体像をJSONでダウンロード
    ※admin限定（_app_share_authorize による）"""
    conn = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True, buffered=True)

        # --- 1) アプリ登録簿（表示順） ---
        cursor.execute("""
            SELECT app_name, display_name, icon, description, sort_order
            FROM app_share_registry ORDER BY sort_order ASC, id ASC
        """)
        registry_rows = cursor.fetchall()
        registry = {r['app_name']: r for r in registry_rows}

        # --- 2) ローカルアプリ走査（未登録アプリの検出） ---
        local_apps = {}
        for item in os.listdir(BASE_DIR):
            app_path = os.path.join(BASE_DIR, item)
            init_file = os.path.join(app_path, '__init__.py')
            if os.path.isdir(app_path) and os.path.exists(init_file):
                if item in ['__pycache__', 'static', 'templates', 'app_share_backups']:
                    continue
                local_apps[item] = {
                    'exists': True,
                    'has_routes': os.path.exists(os.path.join(app_path, 'routes.py')),
                    'has_templates': os.path.exists(os.path.join(app_path, 'templates')),
                    'has_schema_sql': os.path.exists(os.path.join(app_path, 'schema.sql')),
                    'has_manifest': os.path.exists(os.path.join(app_path, 'manifest.json')),
                    'has_app_info': os.path.exists(os.path.join(app_path, 'app_info.json')),
                    'has_requirements': os.path.exists(os.path.join(app_path, 'requirements.txt'))
                }

        # --- 3) ドキュメント（マニュアル／仕様書メモ）一括取得 ---
        cursor.execute("""
            SELECT d.app_name, d.doc_type, d.title, d.content, d.updated_at,
                   u.full_name AS updated_by_name
            FROM app_share_documents d
            LEFT JOIN users u ON d.updated_by = u.id
        """)
        docs = {}
        for row in cursor.fetchall():
            docs.setdefault(row['app_name'], {})[row['doc_type']] = {
                'title': row['title'] or '',
                'content': row['content'] or '',
                'updated_at': row['updated_at'].strftime('%Y-%m-%d %H:%M') if row['updated_at'] else None,
                'updated_by': row['updated_by_name'] or None
            }

        # --- 4) アプリ説明の更新日時（バージョン）情報 ---
        reg_ts, doc_ts = _collect_local_timestamps(cursor)

        # フルパッケージ埋め込みの有無（?light=1 で従来の要約のみに）
        include_packages = request.args.get('light') not in ('1', 'true', 'yes')

        # 実行ユーザ名（パッケージの generated_by 用）
        generated_by = None
        try:
            cursor.execute("SELECT full_name FROM users WHERE id = %s",
                           (session.get('user_id'),))
            _u = cursor.fetchone()
            generated_by = _u['full_name'] if _u else None
        except Exception:
            pass
        site_url = request.host_url.rstrip('/') if request else ''

        # --- 5) 統合（登録簿の表示順 → 未登録は名前順で末尾） ---
        ordered = [r['app_name'] for r in registry_rows]
        extras = sorted((set(local_apps) | set(docs)) - set(ordered))

        apps_out = []
        for name in ordered + extras:
            reg = registry.get(name)
            entry = {
                'app_name': name,
                'registered_in_dashboard': bool(reg),
                'display_name': (reg.get('display_name') if reg else None) or name,
                'icon': (reg.get('icon') if reg else None) or '',
                'summary': (reg.get('description') if reg else None) or '',
                'local_files': local_apps.get(name, {'exists': False})
            }

            # アプリ説明の更新日時（バージョン・インポート時の新旧判定に使用）
            entry_ts = _local_app_updated_at(name, reg_ts, doc_ts)
            entry['updated_at'] = entry_ts.strftime('%Y-%m-%d %H:%M:%S') if entry_ts else None
            entry['version_id'] = _get_version_id(name)

            app_path = os.path.join(BASE_DIR, name)

            # manifest.json（アプリカード情報等）
            manifest_path = os.path.join(app_path, 'manifest.json')
            if os.path.exists(manifest_path):
                try:
                    with open(manifest_path, 'r', encoding='utf-8') as f:
                        entry['manifest'] = json.load(f)
                except Exception as e:
                    entry['manifest_error'] = str(e)

            # app_info.json（技術仕様書）→ 生成AI向けに value / note のみへ簡約
            #（hint はテンプレート共通の記入ガイドなので除外してサイズを削減）
            info_path = os.path.join(app_path, 'app_info.json')
            if os.path.exists(info_path):
                try:
                    with open(info_path, 'r', encoding='utf-8') as f:
                        raw = json.load(f)
                    if isinstance(raw, dict) and isinstance(raw.get('fields'), dict):
                        simplified = {}
                        if raw.get('general_notes'):
                            simplified['general_notes'] = raw['general_notes']
                        for key, fld in raw['fields'].items():
                            if isinstance(fld, dict):
                                val = fld.get('value')
                                if isinstance(val, str):
                                    val = val.strip()
                                if val:
                                    simplified[key] = val
                                note = fld.get('note')
                                if isinstance(note, str) and note.strip():
                                    simplified[key + '__note'] = note.strip()
                            else:
                                simplified[key] = fld
                        entry['tech_spec_app_info'] = simplified
                    else:
                        # 旧形式・不明形式はそのまま収録
                        entry['tech_spec_app_info'] = raw
                except Exception as e:
                    entry['tech_spec_error'] = str(e)

            # DB管理ドキュメント
            d = docs.get(name, {})
            if 'manual' in d:
                entry['user_manual'] = d['manual']
            if 'spec' in d:
                entry['spec_memo'] = d['spec']

            # フルパッケージ（ソース一式・SQLスキーマ・ライブラリ・ランチャ）を埋め込む
            # → 受け入れ側の「個別アプリ取り込み」を全アプリ一括で実行できる
            if include_packages and os.path.isdir(app_path):
                try:
                    entry['package'] = _build_app_package(
                        name, cursor, generated_by=generated_by, site_url=site_url)
                except Exception as e:
                    entry['package_error'] = str(e)

            apps_out.append(entry)

        # 共通（ホーム直下 admin.py/auth.py/guest 等）コードが参照する
        # 必須テーブルのDDL。受け入れ側で「最初にスキーマ宣言」するために同梱。
        try:
            core_schemas = _collect_core_schemas()
        except Exception as e:
            logging.warning("core schema collect skip: %s", e)
            core_schemas = []

        now = _now_jst()

        overview = {
            'export_type': 'fujinp_apps_overview',
            'format_version': 4,
            'includes_packages': include_packages,
            'description': (
                'FUJIN-P サイト「{}」の登録済みアプリの全体像。'
                'apps[] はアプシャ登録簿の表示順（未登録のローカルアプリは末尾・名前順）。'
                '各要素: registered_in_dashboard=登録簿掲載有無 / '
                'local_files=実ファイル構成 / manifest=manifest.json / '
                'tech_spec_app_info=技術仕様書(app_info.json、value・noteのみに簡約) / '
                'user_manual=ユーザマニュアル(Markdown) / spec_memo=仕様書メモ / '
                'updated_at=アプリ説明の更新日時（バージョン、インポート時の新旧判定に使用） / '
                'package=アプリ単位エクスポートパッケージ全体（ソース一式・SQLスキーマ・'
                'ライブラリ・ランチャ等。受け入れ側で個別アプリ取り込みに使用。?light=1 で省略可）。'
                'core_schemas=共通コード（admin.py/auth.py等）が参照する必須テーブルのDDL。'
                '受け入れ時に最初にスキーマ宣言（CREATE TABLE IF NOT EXISTS）する。'
                '生成AIへのコンテキスト提供／サイト間一括移設用。'
            ).format(getattr(Config, 'DB_ACCOUNT', '')),
            'site_name': getattr(Config, 'DB_ACCOUNT', ''),
            'site_url': site_url,
            'generated_at': now.strftime('%Y-%m-%d %H:%M:%S'),
            'app_count': len(apps_out),
            'registered_count': len(registry_rows),
            'core_schemas': core_schemas,
            'apps': apps_out
        }

        json_text = json.dumps(overview, ensure_ascii=False, indent=2, default=str)
        filename = "fujinp_apps_overview_{}.json".format(
            now.strftime('%Y%m%d_%H%M%S'))
        resp = Response(json_text, mimetype='application/json; charset=utf-8')
        resp.headers['Content-Disposition'] = 'attachment; filename="{}"'.format(filename)
        return resp

    except Exception as e:
        logging.error("export_site_overview error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'cursor' in locals():
            cursor.close()
        if conn and conn.is_connected():
            conn.close()


# ============================================
# 全アプリ情報JSONインポート（admin専用・管理タブ）
#
#   エクスポート機能 export_site_overview が出力したJSONを取り込み、
#   アプリ説明（レジストリ・manifest.json・app_info.json・
#   ユーザマニュアル・仕様書メモ）を更新する。
#
#   POST /app_share/import_preview
#     JSON内のアプリ一覧とローカルの更新日時を比較し、
#     new(新規)/newer(更新あり)/same(変更なし)/older(ローカルが新しい)/
#     unknown(日時なし) を返す。
#   POST /app_share/import_apps
#     mode='all'   : 全部上書き
#     mode='newer' : 新しいものだけ上書き（新規含む）
#   取り込んだアプリの updated_at はインポート元の日時を引き継ぐ。
# ============================================

def _app_info_from_simplified(app_name, app_path, spec):
    """エクスポートJSONの tech_spec_app_info（value/note簡約形式）を
    app_info.json の正規形式（fields構造）に変換する"""
    if isinstance(spec, dict) and isinstance(spec.get('fields'), dict):
        return spec  # 既に正規形式
    template = get_initial_template(app_name, app_path)
    if not isinstance(spec, dict):
        return template
    if spec.get('general_notes'):
        template['general_notes'] = str(spec['general_notes'])
    for key, val in spec.items():
        if key == 'general_notes':
            continue
        if key.endswith('__note'):
            base = key[:-len('__note')]
            fld = template['fields'].setdefault(base, {'value': '', 'note': '', 'hint': ''})
            fld['note'] = str(val)
        else:
            if isinstance(val, list):
                val = '\n'.join(str(v) for v in val)
            fld = template['fields'].setdefault(key, {'value': '', 'note': '', 'hint': ''})
            fld['value'] = str(val)
    return template


@app_share_bp.route('/import_preview', methods=['POST'])
@login_required
def import_preview():
    """インポートJSON内のアプリ一覧とローカルの状態を比較"""
    if not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403
    data = request.json or {}
    apps = data.get('apps', [])
    if not isinstance(apps, list):
        return jsonify({'success': False, 'error': 'apps はリストで指定してください'}), 400
    conn = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True, buffered=True)
        reg_ts, doc_ts = _collect_local_timestamps(cursor)
        results = []
        for a in apps:
            name = (a.get('app_name') or '').strip()
            if not name:
                continue
            imp_ts = _parse_ts(a.get('updated_at'))
            registered = name in reg_ts
            local_ts = _local_app_updated_at(name, reg_ts, doc_ts) if registered else None
            if not registered:
                status = 'new'
            elif imp_ts is None:
                status = 'unknown'
            elif local_ts is None or imp_ts > local_ts:
                status = 'newer'
            elif imp_ts < local_ts:
                status = 'older'
            else:
                status = 'same'
            results.append({
                'app_name': name,
                'status': status,
                'import_updated_at': a.get('updated_at') or None,
                'local_updated_at': local_ts.strftime('%Y-%m-%d %H:%M:%S') if local_ts else None
            })
        return jsonify({'success': True, 'results': results})
    except Exception as e:
        logging.error("import_preview error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'cursor' in locals(): cursor.close()
        if conn and conn.is_connected(): conn.close()


@app_share_bp.route('/import_apps', methods=['POST'])
@login_required
def import_apps():
    """全アプリ情報JSON（エクスポート形式）からアプリ説明を取り込む
    mode: 'all'=全部上書き / 'newer'=新しいものだけ上書き"""
    if not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403
    data = request.json or {}
    mode = data.get('mode', 'newer')
    apps = data.get('apps', [])
    if mode not in ('all', 'newer'):
        return jsonify({'success': False, 'error': '不正なモード'}), 400
    if not isinstance(apps, list) or not apps:
        return jsonify({'success': False, 'error': 'apps が空です'}), 400

    user_id = session.get('user_id')
    now = datetime.datetime.now()
    conn = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True, buffered=True)
        reg_ts, doc_ts = _collect_local_timestamps(cursor)

        results = []
        imported = skipped = errors = 0

        for entry in apps:
            name = (entry.get('app_name') or '').strip()
            if not name:
                continue
            imp_ts = _parse_ts(entry.get('updated_at'))
            registered = name in reg_ts

            # 「新しいものだけ」モードの判定（新規は常に取り込む）
            if mode == 'newer' and registered:
                local_ts = _local_app_updated_at(name, reg_ts, doc_ts)
                if imp_ts is None or (local_ts and imp_ts <= local_ts):
                    skipped += 1
                    results.append({'app_name': name, 'action': 'skipped',
                                    'reason': 'ローカルが同じか新しい'})
                    continue

            try:
                ver_ts = imp_ts or now
                display_name = entry.get('display_name') or name
                icon = entry.get('icon') or '📦'
                summary = entry.get('summary') or ''

                # --- 1) レジストリ upsert（updated_at 列が無い環境にも対応） ---
                if registered:
                    try:
                        cursor.execute("""UPDATE app_share_registry
                            SET display_name=%s, icon=%s, description=%s, updated_at=%s
                            WHERE app_name=%s""",
                            (display_name, icon, summary, ver_ts, name))
                    except mysql.connector.Error:
                        cursor.execute("""UPDATE app_share_registry
                            SET display_name=%s, icon=%s, description=%s
                            WHERE app_name=%s""",
                            (display_name, icon, summary, name))
                else:
                    cursor.execute("SELECT COALESCE(MAX(sort_order),0)+10 AS n FROM app_share_registry")
                    nxt = cursor.fetchone()['n']
                    try:
                        cursor.execute("""INSERT INTO app_share_registry
                            (app_name, display_name, icon, description, sort_order, updated_at)
                            VALUES (%s,%s,%s,%s,%s,%s)""",
                            (name, display_name, icon, summary, nxt, ver_ts))
                    except mysql.connector.Error:
                        cursor.execute("""INSERT INTO app_share_registry
                            (app_name, display_name, icon, description, sort_order)
                            VALUES (%s,%s,%s,%s,%s)""",
                            (name, display_name, icon, summary, nxt))
                    reg_ts[name] = ver_ts

                # --- 2) ファイル（manifest.json / app_info.json） ---
                app_path = os.path.join(BASE_DIR, name)
                os.makedirs(app_path, exist_ok=True)

                manifest = entry.get('manifest')
                if not isinstance(manifest, dict):
                    manifest = {}
                manifest.setdefault('display_name', display_name)
                manifest.setdefault('icon', icon)
                manifest_path = os.path.join(app_path, 'manifest.json')
                with open(manifest_path, 'w', encoding='utf-8') as f:
                    json.dump(manifest, f, ensure_ascii=False, indent=2)

                spec = entry.get('tech_spec_app_info')
                info_path = None
                if spec is not None:
                    app_info = _app_info_from_simplified(name, app_path, spec)
                    info_path = os.path.join(app_path, 'app_info.json')
                    with open(info_path, 'w', encoding='utf-8') as f:
                        json.dump(app_info, f, ensure_ascii=False, indent=2)

                # ファイルの更新時刻をインポート元のバージョン日時に合わせる
                # （再インポート時に「変更なし」と正しく判定させるため）
                if imp_ts:
                    ts_epoch = imp_ts.timestamp()
                    for p in (manifest_path, info_path):
                        if p and os.path.exists(p):
                            try:
                                os.utime(p, (ts_epoch, ts_epoch))
                            except Exception:
                                pass

                # --- 3) ドキュメント（マニュアル・仕様書メモ） ---
                # パッケージ（全アプリ情報JSON）は常に最新版とみなし、無条件で更新する。
                # フィールドが含まれていれば内容が空でも反映（＝ミラーリング）。
                # フィールド自体が無い（旧/外部形式）場合のみ既存を保持する。
                for doc_type, key in (('manual', 'user_manual'), ('spec', 'spec_memo')):
                    if key not in entry:
                        continue
                    d = entry.get(key)
                    if not isinstance(d, dict):
                        d = {}
                    dts = _parse_ts(d.get('updated_at')) or ver_ts
                    cursor.execute("""
                        INSERT INTO app_share_documents
                            (app_name, doc_type, title, content, updated_by, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            title = VALUES(title), content = VALUES(content),
                            updated_by = VALUES(updated_by), updated_at = VALUES(updated_at)
                    """, (name, doc_type, d.get('title') or '', d.get('content') or '', user_id, dts))

                imported += 1
                results.append({'app_name': name, 'action': 'imported',
                                'reason': f'v{ver_ts.strftime("%Y-%m-%d %H:%M:%S")} を取り込みました'})
            except Exception as e:
                errors += 1
                logging.error("import_apps error (%s): %s", name, e)
                results.append({'app_name': name, 'action': 'error', 'reason': str(e)})

        conn.commit()
        return jsonify({'success': True,
                        'message': f'{imported}件取込, {skipped}件スキップ' + (f', {errors}件エラー' if errors else ''),
                        'imported': imported, 'skipped': skipped, 'errors': errors,
                        'results': results})
    except Exception as e:
        if conn: conn.rollback()
        logging.error("import_apps fatal: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'cursor' in locals(): cursor.close()
        if conn and conn.is_connected(): conn.close()


# ============================================
# アプリ単位のエクスポートパッケージ（JSON）
#
#   GET /app_share/export_app_package/<app_name>
#   権限: admin専用（_app_share_authorize による）。
#   1つのアプリについて、
#     - アプリディレクトリ以下の全ファイル（static・__pycache__ を除く）
#     - ユーザマニュアル・技術仕様書（DB管理ドキュメント）
#     - アプリが使用しているSQLテーブルのスキーマ（CREATE TABLE文）
#     - importから抽出したライブラリ一覧（third_party/stdlib/local）
#     - メタ情報（日付・サイトURL・バージョン等）
#   を1つのJSONにパッケージ化してダウンロードさせる。
#   ※ 管理タブの「全アプリパッケージからのアプリインポート」の入力になる。
# ============================================

# パッケージに直接埋め込むファイルの上限サイズ（超過分はスキップして記録）
PACKAGE_MAX_FILE_SIZE = 5 * 1024 * 1024
# アプリパッケージ同梱の許可リスト（deny by default）。
# データは既定で配らない。配りたいデータは <app>/data_for_distribution/ に置く。
APP_PKG_ALWAYS_DIRS = ('templates', 'data_for_distribution')  # 以下すべて同梱
APP_PKG_ROOT_EXTS = ('.sql', '.json', '.md', '.txt')          # アプリ直下のみ同梱

# テーブル参照の照合対象にする拡張子。app_info.json やマニュアルの散文で
# 他アプリのテーブル名に言及しただけのものを拾わないよう、コードだけを見る。
CODE_FILE_EXTS = ('.py', '.html', '.sql', '.js')

# SQL文中のテーブル参照（FROM / JOIN / INTO / UPDATE / CREATE TABLE ...）
# ※ 文字クラスを ASCII 限定にすると、日本語を含むテーブル名
#   （例: T_06_04_受託共同研究事業費受入実績）が途中で切れて誤検出になる。
#   [^\W\d] = 「数字以外の語構成文字」。Python3 の \w は日本語を含む。
_TABLE_REF_RE = re.compile(
    r'\b(?:FROM|JOIN|INTO|UPDATE|TABLE(?:\s+IF\s+(?:NOT\s+)?EXISTS)?)\s+`?([^\W\d][\w$]*)`?',
    re.IGNORECASE)

# 定数経由のテーブル指定（定数名に TABLE を含む代入からテーブル名を取る）。
# f-string や % 書式でSQLに埋め込まれるテーブルは上の正規表現では拾えないため。
# ※ ここに実例を「定数 = 'テーブル名'」の形で書くと自分自身が誤検出されるので書かない。

_TABLE_CONST_RE = re.compile(
    r'\b[A-Za-z_]*TABLE[A-Za-z_]*\s*=\s*[\'"]([^\W\d][\w$]*)[\'"]',
    re.IGNORECASE)
# テーブル名として受け入れる文字。日本語を含む識別子を許すため \w を使う。
# バッククォート・引用符・空白・セミコロン・ハイフン等は \w に含まれないので、
# `{table}` へ埋め込む際の安全確認としては従来と同等（SQLインジェクション対策）。
_TABLE_NAME_RE = re.compile(r'^[\w$]+$')
# 参照名として拾ってしまうSQLキーワード等
_TABLE_REF_NOISE = {'select', 'if', 'exists', 'not', 'set', 'values', 'dual', 'outfile', 'table'}

# Python の import 行（from X import Y）は SQL の FROM と紛らわしいので除外する。
# 大文字小文字は区別する（SQLの FROM は除外しない）。
_IMPORT_LINE_RE = re.compile(r'^\s*(?:from|import)\s')


def _extract_imports(py_sources, app_name):
    """Pythonソースから import されているモジュールをトップレベル名で抽出し、
    third_party / stdlib / local に分類する"""
    mods = set()
    for src in py_sources:
        for line in src.splitlines():
            m = re.match(r'^\s*from\s+([A-Za-z_][\w\.]*)\s+import\b', line)
            if m:
                mods.add(m.group(1).split('.')[0])
                continue
            m = re.match(r'^\s*import\s+([^#]+)', line)
            if m:
                for part in m.group(1).split(','):
                    nm = part.strip().split(' as ')[0].strip()
                    if nm and re.match(r'^[A-Za-z_][\w\.]*$', nm):
                        mods.add(nm.split('.')[0])
    stdlib = set(getattr(sys, 'stdlib_module_names', ())) or {
        'os', 'sys', 'json', 're', 'datetime', 'logging', 'shutil', 'hashlib',
        'zipfile', 'tempfile', 'io', 'base64', 'time', 'math', 'collections',
        'functools', 'itertools', 'typing', 'pathlib', 'random', 'string',
        'subprocess', 'threading', 'traceback', 'urllib', 'uuid', 'csv', 'html',
        'http', 'email', 'unittest', 'copy', 'glob', 'decimal', 'enum', 'abc',
        'textwrap', 'secrets', 'socket', 'struct', 'warnings'}
    result = {'third_party': [], 'stdlib': [], 'local': []}

    # local 判定は BASE_DIR（fujinp/）だけでなく SITE_CODE_ROOT（ホーム）も見る。
    # auth / config / db / decorators / markdown_converter などのプラットフォーム
    # 共通モジュールはホーム側にあり、BASE_DIR だけでは third_party に誤分類される。
    roots = [BASE_DIR]
    if SITE_CODE_ROOT != BASE_DIR:
        roots.append(SITE_CODE_ROOT)

    def _is_local(name):
        if name == app_name:
            return True
        for r in roots:
            if (os.path.isfile(os.path.join(r, name + '.py')) or
                    os.path.isdir(os.path.join(r, name))):
                return True
        return False

    for name in sorted(mods):
        if _is_local(name):
            result['local'].append(name)
        elif name in stdlib:
            result['stdlib'].append(name)
        else:
            result['third_party'].append(name)
    return result


def _detect_used_tables(cursor, code_text):
    """DBの全テーブルのうち、アプリの**コード**中に名前が登場するものを抽出する。
    対象DBは <owner>$default と <owner>$fujinp の両方。アプリが他方のDBの
    テーブルを参照していても拾う（取り込み側の _compare_table_any は両DBを
    横断するので、出す側も揃える）。
    """
    schemas = {}
    schema_dbs = {}                        # {テーブル名: 実DB名} USE文ヒントの元
    existing = set()
    base, _dbs = _gc_target_databases()
    tables_map = _db_tables_map()          # {dbname: {テーブル名, ...}} 両DB

    if not tables_map:                     # 取得失敗時は従来どおり接続中のDBのみ
        cursor.execute("SHOW TABLES")
        tables_map = {None: set(list(r.values())[0] for r in cursor.fetchall())}
    for dbname, tset in tables_map.items():
        existing |= tset
        hits = [t for t in sorted(tset)
                if t not in schemas
                and re.search(r'\b' + re.escape(t) + r'\b', code_text)]
        if not hits:
            continue
        conn2 = None
        cu = None
        try:
            if dbname is None or dbname == base.get('database'):
                cu = cursor          # 既定DBは呼び出し元の接続を再利用（接続数を増やさない）
            else:
                d = dict(base); d['database'] = dbname
                conn2 = mysql.connector.connect(**d)
                cu = conn2.cursor(dictionary=True, buffered=True)
            for t in hits:
                schema_dbs[t] = dbname
                try:
                    cu.execute(f"SHOW CREATE TABLE `{t}`")
                    row = cu.fetchone()
                    schemas[t] = ((row.get('Create Table') or row.get('Create View') or '')
                                  if row else '')
                except Exception as e:
                    schemas[t] = f'-- スキーマ取得エラー: {e}'
        finally:
            if conn2:
                try: cu.close()
                except Exception: pass
                conn2.close()
    # 以降（scan_text / referenced / missing）は現行のまま
    # --- コードが参照しているのにDBに存在しないテーブル ---
    scan_text = '\n'.join('' if _IMPORT_LINE_RE.match(line) else line
                          for line in code_text.split('\n'))
    referenced = set(m.group(1) for m in _TABLE_REF_RE.finditer(scan_text))
    referenced |= set(m.group(1) for m in _TABLE_CONST_RE.finditer(scan_text))
    missing = sorted(n for n in referenced
                     if n not in existing
                     and '_' in n                      # 別名・キーワードの誤検出を落とす
                     and n.lower() not in _TABLE_REF_NOISE)
    return schemas, missing, schema_dbs

# --- ダッシュボードからのランチャ（起動リンク要素）抽出 ---

# ランチャの囲み要素候補（内側優先で最小の要素を採用）
LAUNCHER_CONTAINER_TAGS = ('a', 'button', 'li', 'article', 'section', 'div')
# 抽出したランチャ要素の最大サイズ（超える場合は周辺行の抜粋に切替）
LAUNCHER_MAX_SNIPPET = 2000


def _smallest_enclosing_element(html, pos):
    """html 内の位置 pos を含む最小の囲みHTML要素（LAUNCHER_CONTAINER_TAGS）を返す。
    要素が見つからなければ None。malformed HTML にもある程度耐える。"""
    tag_re = re.compile(r"<(/?)(" + "|".join(LAUNCHER_CONTAINER_TAGS) + r")\b([^>]*)>",
                        re.IGNORECASE)
    stack = []
    best = None  # (size, start, end)
    for m in tag_re.finditer(html):
        closing = (m.group(1) == '/')
        tagname = m.group(2).lower()
        attrs = m.group(3) or ''
        self_closing = attrs.rstrip().endswith('/')
        if closing:
            # 対応する開始タグを stack から探して閉じる
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == tagname:
                    open_start = stack[i][1]
                    elem_end = m.end()
                    del stack[i:]  # この要素と、内側の未閉じタグを破棄
                    if open_start <= pos < elem_end:
                        size = elem_end - open_start
                        if best is None or size < best[0]:
                            best = (size, open_start, elem_end)
                    break
        elif not self_closing:
            stack.append((tagname, m.start()))
    if best:
        return html[best[1]:best[2]]
    return None


def _line_context(html, pos, before=2, after=2):
    """位置 pos の前後数行を抜粋（囲み要素が取れない場合のフォールバック）"""
    lines = html.split('\n')
    idx = html.count('\n', 0, pos)
    lo = max(0, idx - before)
    hi = min(len(lines), idx + after + 1)
    return '\n'.join(lines[lo:hi])


def _launcher_snippets(html, app_name):
    """dashboard HTML から、当該アプリを参照するランチャ要素を抽出する。
    参照の目印:
      - url_for('<app>.xxx')
      - パスリテラル '/<app>/' '"/<app>"' '/<app>?...' '/<app>#...'
      - 先頭スラッシュ無しの相対パス 'href="<app>/"' 等
    戻り値: [{'snippet': str, 'method': 'element'|'context'}, ...]（重複除去）"""
    app = re.escape(app_name)
    patterns = [
        r"url_for\(\s*['\"]" + app + r"\.",              # url_for('app.endpoint')
        r"['\"]/?" + app + r"(?:/|['\"?#])",             # "/app/" "app/" "/app" "/app?" "/app#"
    ]
    positions = []
    for pat in patterns:
        for m in re.finditer(pat, html):
            positions.append(m.start())
    positions = sorted(set(positions))

    snippets = []
    seen = set()
    for pos in positions:
        elem = _smallest_enclosing_element(html, pos)
        if elem and len(elem) <= LAUNCHER_MAX_SNIPPET:
            snip, method = elem.strip(), 'element'
        else:
            snip, method = _line_context(html, pos).strip(), 'context'
        if snip and snip not in seen:
            seen.add(snip)
            snippets.append({'snippet': snip, 'method': method})
    return snippets


# ランチャ探索から除外するディレクトリ（バックアップ・退避・生成物）
LAUNCHER_SCAN_EXCLUDE_DIRS = {
    '__pycache__', 'static', 'node_modules',
    'app_share_backups', 'app_share_import_backups', 'app_share_import_staging',
    'import_staging', 'import_backups',
}
LAUNCHER_SCAN_MAX_DEPTH = 5

_DASHBOARD_CACHE = {}

def _find_dashboard_files(fname):
    """SITE_CODE_ROOT 以下から fname を探し、浅い順に返す。
    ダッシュボードHTMLの置き場所は配置により変わるため、'templates' という
    ディレクトリ名を決め打ちせず、深さを制限して走査する。
    結果はプロセス内でキャッシュする（HTMLを移動した場合はリロードが必要）。"""
    if fname in _DASHBOARD_CACHE:          # ← 追加
        return _DASHBOARD_CACHE[fname]     # ← 追加
    root = SITE_CODE_ROOT
    base_depth = root.rstrip(os.sep).count(os.sep)
    hits = []
    for cur, dirs, files in os.walk(root):
        if cur.rstrip(os.sep).count(os.sep) - base_depth >= LAUNCHER_SCAN_MAX_DEPTH:
            dirs[:] = []
        else:
            dirs[:] = [d for d in dirs
                       if not d.startswith('.') and d not in LAUNCHER_SCAN_EXCLUDE_DIRS]
        if fname in files:
            hits.append(os.path.join(cur, fname))
    hits.sort(key=lambda p: (p.count(os.sep), len(p)))
    _DASHBOARD_CACHE[fname] = hits         # ← 追加
    return hits

def _extract_launchers(app_name):
    """admin_dashboard.html（及び存在すれば guest_dashboard.html）から
    当該アプリのランチャを抽出してリストで返す。
    ダッシュボードHTMLの所在は SITE_CODE_ROOT 以下を走査して特定する。"""
    results = []
    for fname in ('admin_dashboard.html', 'guest_dashboard.html'):
        entry = {'source': fname, 'found': False, 'file_found': False}
        hits = _find_dashboard_files(fname)
        if not hits:
            entry['searched_root'] = SITE_CODE_ROOT      # 見つからない場合の切り分け用
        else:
            path = hits[0]
            entry['file_found'] = True
            entry['path'] = path
            if len(hits) > 1:
                entry['other_paths'] = hits[1:]          # OLD版・複製の検出用
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    html = f.read()
                snips = _launcher_snippets(html, app_name)
                if snips:
                    entry['found'] = True
                    entry['count'] = len(snips)
                    entry['method'] = snips[0]['method']
                    entry['snippet'] = '\n\n'.join(s['snippet'] for s in snips)
                else:
                    # ファイルは読めたが当該アプリの参照が無い（切り分け用）
                    entry['app_name_appears'] = (app_name in html)
            except Exception as e:
                entry['error'] = str(e)
        results.append(entry)
    return results


# --- app.py への Blueprint 登録コード生成（受け入れ結果・GC結果レポート用） ---

def _paren_span(text, open_idx):
    """text[open_idx]（'('）から対応する ')' までの部分文字列を返す"""
    depth = 0
    for j in range(open_idx, min(len(text), open_idx + 3000)):
        ch = text[j]
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                return text[open_idx:j + 1]
    return text[open_idx:open_idx + 3000]


def _analyze_blueprints(app_name, files):
    """アプリの .py ファイル群から Blueprint 定義を検出する。
    files: (相対パス, テキスト|None) のイテラブル。
    戻り値: [{'module':..., 'bp':..., 'url_prefix':...|None}, ...]（定義順・重複除去）
    module は app.py での import パス（例 fujinp.table_post / fujinp.<app>.routes）。"""
    pkg_prefix = os.path.basename(BASE_DIR.rstrip('/')) or 'fujinp'
    found, seen = [], set()
    for rel, text in files:
        if not rel or not rel.endswith('.py') or not text:
            continue
        parts = rel.replace('\\', '/')[:-3].split('/')
        if parts and parts[-1] == '__init__':
            parts = parts[:-1]
        module = '.'.join([pkg_prefix, app_name] + parts)
        for m in re.finditer(r'(\w+)\s*=\s*Blueprint\s*\(', text):
            bp = m.group(1)
            span = _paren_span(text, m.end() - 1)
            um = re.search(r"url_prefix\s*=\s*['\"]([^'\"]+)['\"]", span)
            key = (module, bp)
            if key in seen:
                continue
            seen.add(key)
            found.append({'module': module, 'bp': bp,
                          'url_prefix': um.group(1) if um else None})
    return found


def _blueprint_reg_lines(app_name, analyses):
    """(import行リスト, register行リスト) を返す（インデント無し）"""
    imports, registers = [], []
    for a in analyses:
        imports.append(f"from {a['module']} import {a['bp']}")
        if a.get('url_prefix'):
            registers.append(f"app.register_blueprint({a['bp']})")
        else:
            registers.append(f"app.register_blueprint({a['bp']}, url_prefix='/{app_name}')")
    return imports, registers


def _blueprint_snippet(pairs):
    """pairs: [(app_name, analyses), ...] → 貼り付け用スニペット文字列（import群→空行→register群）。
    Blueprint が見つからなければ None。"""
    all_imp, all_reg = [], []
    for app, an in pairs:
        imp, reg = _blueprint_reg_lines(app, an)
        all_imp += imp
        all_reg += reg
    if not all_imp:
        return None
    return '\n'.join(all_imp) + '\n\n' + '\n'.join(all_reg)


def _blueprint_snippet_from_package(pkg):
    """エクスポートパッケージ(files[])から Blueprint 登録スニペットを生成"""
    app_name = pkg.get('app_name') or ''
    files = [(f.get('path'), f.get('content') if f.get('encoding') != 'base64' else None)
             for f in (pkg.get('files') or [])]
    return _blueprint_snippet([(app_name, _analyze_blueprints(app_name, files))])


def _blueprint_snippet_from_dir(app_name):
    """ディスク上のアプリディレクトリから Blueprint 登録スニペットを生成（GC用）。
    作業用ディレクトリ（import_backups 等）内の他アプリ複製は解析に含めない。"""
    app_path = os.path.join(BASE_DIR, app_name)
    files = []
    if os.path.isdir(app_path):
        for root, dirs, filenames in os.walk(app_path):
            dirs[:] = [d for d in dirs
                       if not d.startswith('.') and d not in GC_SCAN_EXCLUDE_DIRS]
            for fn in filenames:
                if not fn.endswith('.py'):
                    continue
                rel = os.path.relpath(os.path.join(root, fn), app_path).replace(os.sep, '/')
                try:
                    with open(os.path.join(root, fn), 'r', encoding='utf-8', errors='ignore') as f:
                        files.append((rel, f.read()))
                except Exception:
                    pass
    return _blueprint_snippet([(app_name, _analyze_blueprints(app_name, files))])


def _content_hash6(app_name):
    """アプリディレクトリの内容から6桁の短いハッシュを作る（バージョン確定時の「アルファ」部）。
    各ファイルの相対パスとサイズを連結してSHA-1。static/__pycache__/import_* と *.pyc、
    および version.json（確定情報そのもの）は除外する。"""
    app_path = os.path.join(BASE_DIR, app_name)
    parts = []
    if os.path.isdir(app_path):
        for root, dirs, filenames in os.walk(app_path):
            dirs[:] = [d for d in dirs
                       if d not in ('static', '__pycache__', 'import_staging', 'import_backups')]
            for fn in sorted(filenames):
                if fn.endswith('.pyc') or fn == 'version.json':
                    continue
                fp = os.path.join(root, fn)
                rel = os.path.relpath(fp, app_path).replace(os.sep, '/')
                try:
                    parts.append('{}:{}'.format(rel, os.path.getsize(fp)))
                except Exception:
                    parts.append(rel)
    return hashlib.sha1('\n'.join(parts).encode('utf-8', 'replace')).hexdigest()[:6]


def _version_file_path(app_name):
    return os.path.join(BASE_DIR, app_name, 'version.json')


def _get_version_id(app_name):
    """確定済みのバージョンID（version.json に保存）を返す。未確定なら None。
    バージョンIDは「バージョン確定」ボタンでのみ更新され、エクスポートやファイル変更では変化しない。"""
    try:
        p = _version_file_path(app_name)
        if os.path.exists(p):
            with open(p, 'r', encoding='utf-8') as f:
                data = json.load(f)
            vid = (data or {}).get('version_id')
            if vid:
                return vid
    except Exception:
        pass
    return None


def _spec_from_app_info(files):
    """app_share_documents に技術仕様書が無いとき、アプリ同梱の app_info.json から
    代替の spec_memo を組み立てる。見つからなければ None。"""
    for f in files:
        if f.get('path') == 'app_info.json' and f.get('encoding') == 'text':
            try:
                info = json.loads(f['content'])
            except Exception:
                return None
            parts = []
            for key, val in (info.get('fields') or {}).items():
                body = ((val or {}).get('value') or '').rstrip()
                if body:
                    parts.append('## %s\n\n%s' % (key, body))
            notes = (info.get('general_notes') or '').rstrip()
            if notes:
                parts.append('## general_notes\n\n%s' % notes)
            if not parts:
                return None
            return {'title': '技術情報（app_info.json より生成）',
                    'content': '\n\n'.join(parts),
                    # mtime は JST なので使わない。受け入れ側は updated_at が None の場合
                    # パッケージの版日時（UTC基準）を採用する。DBのUTC前提とずらさないため。
                    'updated_at': None,
                    'updated_by': None,
                    'source': 'app_info.json'}
    return None

def _build_app_package(app_name, cursor, generated_by=None, site_url=None):
    """アプリ単位エクスポートパッケージ(dict)を構築する共通処理。
    export_app_package（単体DL）と export_site_overview（全アプリ埋め込み）で共用。
    cursor は dictionary=True, buffered=True のカーソル。"""
    app_path = os.path.join(BASE_DIR, app_name)

    # --- 1) ファイル収集（static・__pycache__・*.pyc を除く） ---
    files = []
    py_texts = []
    code_texts = []      # テーブル参照の照合用（コードのみ。散文を混ぜない）
    static_excluded = []
    excluded_data = {}   # 許可リスト外で落としたもの {トップレベル名: 件数}
    if os.path.isdir(app_path):
        for root, dirs, filenames in os.walk(app_path):
            # static は一切配らない（投稿データが混在しうるため）。
            # 落とした事実だけ件数で残す。ファイル名は載せない。
            for d in dirs:
                if d == 'static':
                    sp = os.path.join(root, d)
                    static_excluded.append({
                        'path': os.path.relpath(sp, app_path).replace(os.sep, '/'),
                        'file_count': sum(len(fs) for _r, _d, fs in os.walk(sp))})
            dirs[:] = [d for d in dirs if d not in ('static', '__pycache__', 'import_staging', 'import_backups')]
            for fn in sorted(filenames):
                if fn.endswith('.pyc'):
                    continue
                fpath = os.path.join(root, fn)
                rel = os.path.relpath(fpath, app_path).replace(os.sep, '/')
                # --- 同梱の許可リスト（deny by default）---
                #   *.py                     階層を問わずアプリ本体
                #   templates/ 以下          画面（CSS/JS内包分を含む）
                #   data_for_distribution/   配ると明示的に宣言したデータ
                #   アプリ直下の .sql .json .md .txt
                #                            schema.sql / app_info.json / manifest.json / README.md 等
                # それ以外（imgs/ cache/ 作業ファイル等）は配らない。
                top = rel.split('/', 1)[0] if '/' in rel else ''
                if fn.endswith('.py'):
                    pass
                elif top in APP_PKG_ALWAYS_DIRS:
                    pass
                elif not top and fn.endswith(APP_PKG_ROOT_EXTS):
                    pass
                else:
                    key = top or '(アプリ直下)'
                    excluded_data[key] = excluded_data.get(key, 0) + 1
                    continue
                try:
                    size = os.path.getsize(fpath)
                    mtime = datetime.datetime.fromtimestamp(
                        os.path.getmtime(fpath), JST).strftime('%Y-%m-%d %H:%M:%S')
                    if size > PACKAGE_MAX_FILE_SIZE:
                        files.append({'path': rel, 'size': size, 'mtime': mtime,
                                      'skipped': True,
                                      'reason': f'サイズ超過のためスキップ ({size} バイト)'})
                        continue
                    with open(fpath, 'rb') as f:
                        raw = f.read()
                    try:
                        text = raw.decode('utf-8')

                        files.append({'path': rel, 'size': size, 'mtime': mtime,
                                      'encoding': 'text', 'content': text})
                        if fn.endswith(CODE_FILE_EXTS):
                            code_texts.append(text)
                        if fn.endswith('.py'):
                            py_texts.append(text)

                    except UnicodeDecodeError:
                        files.append({'path': rel, 'size': size, 'mtime': mtime,
                                      'encoding': 'base64',
                                      'content': base64.b64encode(raw).decode('ascii')})
                except Exception as e:
                    files.append({'path': rel, 'skipped': True, 'reason': str(e)})

    # --- 2) レジストリ情報・アプリ説明バージョン ---
    cursor.execute("SELECT * FROM app_share_registry WHERE app_name = %s", (app_name,))
    reg = cursor.fetchone() or {}
    reg_ts, doc_ts = _collect_local_timestamps(cursor)
    ver_ts = _local_app_updated_at(app_name, reg_ts, doc_ts)

    # --- 3) ドキュメント（マニュアル・技術仕様書） ---
    cursor.execute("""
        SELECT d.doc_type, d.title, d.content, d.updated_at,
               u.full_name AS updated_by_name
        FROM app_share_documents d
        LEFT JOIN users u ON d.updated_by = u.id
        WHERE d.app_name = %s
    """, (app_name,))
    docs = {}
    for row in cursor.fetchall():
        docs[row['doc_type']] = {
            'title': row['title'] or '',
            'content': row['content'] or '',
            'updated_at': row['updated_at'].strftime('%Y-%m-%d %H:%M:%S') if row['updated_at'] else None,
            'updated_by': row['updated_by_name'] or None
        }

    # --- 4) 使用SQLテーブルのスキーマ ---

    sql_schemas, sql_tables_missing, _schema_dbs = _detect_used_tables(cursor, '\n'.join(code_texts))
    # テーブルごとの所属DB。実DB名はサイト名を含み移設先では意味がないので、
    # 接尾辞（default / fujinp）だけをヒントとして持たせる。
    sql_databases = {t: (db.rsplit('$', 1)[1] if db and '$' in db else 'default')
                     for t, db in _schema_dbs.items()}

    # --- 5) ライブラリ一覧（import解析） ---
    libraries = _extract_imports(py_texts, app_name)

    # --- 5b) ダッシュボードのランチャ ---
    launchers = _extract_launchers(app_name)

    now = _now_jst()
    return {
        'export_type': 'fujinp_app_package',
        'format_version': 2,
        'app_name': app_name,
        'display_name': reg.get('display_name') or app_name,
        'icon': reg.get('icon') or '📦',
        'description': reg.get('description') or '',
        'updated_at': ver_ts.strftime('%Y-%m-%d %H:%M:%S') if ver_ts else None,
        'version_id': _get_version_id(app_name),
        'site_name': getattr(Config, 'DB_ACCOUNT', ''),
        'site_url': site_url or (request.host_url.rstrip('/') if request else ''),
        'generated_at': now.strftime('%Y-%m-%d %H:%M:%S'),
        'generated_by': generated_by,
        'file_count': len(files),
        'files': files,
        'static_excluded': static_excluded,
        'excluded_data': sorted(
            [{'path': k, 'file_count': v} for k, v in excluded_data.items()],
            key=lambda x: x['path']),
        'user_manual': docs.get('manual'),
        'spec_memo': docs.get('spec') or _spec_from_app_info(files),
        'sql_schemas': sql_schemas,
        'sql_databases': sql_databases,
        'sql_tables_missing': sql_tables_missing,
        'libraries': libraries,
        'launchers': launchers
    }


@app_share_bp.route('/export_app_package/<app_name>', methods=['GET'])
@login_required
def export_app_package(app_name):
    """アプリ単位のエクスポートパッケージ(JSON)を生成してダウンロード"""
    app_path = os.path.join(BASE_DIR, app_name)
    if not os.path.isdir(app_path):
        return jsonify({'success': False, 'error': f'アプリ "{app_name}" が見つかりません'}), 404

    conn = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True, buffered=True)

        # 実行ユーザ名
        generated_by = None
        try:
            cursor.execute("SELECT full_name FROM users WHERE id = %s",
                           (session.get('user_id'),))
            u = cursor.fetchone()
            generated_by = u['full_name'] if u else None
        except Exception:
            pass

        package = _build_app_package(app_name, cursor, generated_by=generated_by)
        package['package_note'] = (
            'FUJIN-P アプリ単位エクスポートパッケージ。'
            'files[]=アプリ本体（*.py / templates/ / data_for_distribution/ / '
            'アプリ直下の .sql .json .md .txt。それ以外のデータは配らない）/ '
            'encoding=text|base64、サイズ超過はskipped=true）/ '
            'user_manual=ユーザマニュアル / '
            'spec_memo=技術仕様書（app_share_documents。無い場合はアプリ同梱の '
            'app_info.json から生成し source キーを付す）/ '
            'sql_schemas=アプリのコード(.py/.html/.sql/.js)が参照するテーブルのCREATE TABLE文 / '
            'sql_tables_missing=コードは参照しているがDBに存在しないテーブル名'
            '（schema.sql 未実行の疑い）/ '
            'libraries=import解析によるライブラリ一覧(third_party/stdlib/local。'
            'local はサイト内のモジュールでpip不要)/ '
            'launchers=admin/guest_dashboard.html から抽出したランチャ要素（受け入れ側で'
            'ダッシュボードに貼り付ける） / '
            'updated_at=アプリ説明のバージョン（版比較に使うためDB基準・UTC。'
            'generated_at と files[].mtime はJST）。'
            '他のFUJIN-Pサイトへの移設や生成AIへのコンテキスト提供に使用。'
            'excluded_data=許可リスト外で配布対象外にしたファイルの件数'
            '（配りたいデータは <app>/data_for_distribution/ に置くこと）。'
        )
        now = _now_jst()

        json_text = json.dumps(package, ensure_ascii=False, indent=2, default=str)
        filename = "app_package_{}_{}.json".format(
            app_name, now.strftime('%Y%m%d_%H%M%S'))
        resp = Response(json_text, mimetype='application/json; charset=utf-8')
        resp.headers['Content-Disposition'] = 'attachment; filename="{}"'.format(filename)
        return resp

    except Exception as e:
        logging.error("export_app_package error (%s): %s", app_name, e)
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'cursor' in locals():
            cursor.close()
        if conn and conn.is_connected():
            conn.close()


# ============================================
# アプリ単位パッケージの取り込み（admin専用・専用ダッシュボード）
#
#   GET  /app_share/import_package/<app_name>        取り込み専用ダッシュボード
#   POST /app_share/import_package_check/<app_name>  検証
#     - アプリ一致（そもそもこのアプリのパッケージか）・形式・バージョン比較
#     - 改修の概要（ファイル差分。テンプレート・バックエンドは置き換えのみ）
#     - テーブルスキーマ照合（カラム追加→ALTER文、テーブルなし→CREATE文を提示）
#     - ライブラリ照合（未インストールのものに pip install を提示）
#   POST /app_share/import_package_apply/<app_name>  実行
#     - 現在のファイル一式を app_share/import_backups/ にバックアップ
#     - パッケージ内ファイルで置き換え（ローカルのみのファイルは残す）
#     - アプリ説明（レジストリ）・マニュアル・仕様書メモを更新
#     ※ SQL・pip install は自動実行しない（画面で実行を促すのみ）
# ============================================

# import名 → pipパッケージ名の対応（既知のもののみ）
PIP_NAME_MAP = {
    'mysql': 'mysql-connector-python',
    'flask': 'Flask',
    'PIL': 'Pillow',
    'yaml': 'PyYAML',
    'bs4': 'beautifulsoup4',
    'dateutil': 'python-dateutil',
    'cv2': 'opencv-python',
    'sklearn': 'scikit-learn',
    'dotenv': 'python-dotenv',
    'jwt': 'PyJWT',
}

# 取り込み前バックアップの保存先
# 取り込み前バックアップの保存先。ステージング同様、アプシャ自身のディレクトリ内に持つ
# （旧: <BASE_DIR>/app_share_import_backups → 新: app_share/import_backups）
IMPORT_BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 'import_backups')

# パッケージ一時ステージングの保存先（モーダル→取り込み画面の受け渡し用）
# パッケージ一時ステージング・GCプレビュー状態などの作業用ディレクトリ。
# サイト共有の fujinp/ 直下ではなく、アプシャ自身のディレクトリ内に持つ
# （旧: <BASE_DIR>/app_share_import_staging → 新: app_share/import_staging）
IMPORT_STAGING_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  'import_staging')
STAGING_TTL_SECONDS = 3600  # 1時間で失効


# ============================================
# カーネル（プラットフォーム共通部）のパッケージ化
#
#   カーネル = SITE_CODE_ROOT 直下のコードと templates/。
#   アプリ（fujinp/ 配下）は「全アプリパッケージ」の担当なので含めない。
#   config.py は同梱しない。設定名だけを config_keys / config_keys_used に載せ、
#   受け入れ側で「不足している設定」を照合できるようにする。
#   （config_template.py の生成はさいまるの担当）
# ============================================


KERNEL_EXPORT_TYPE = 'fujinp_kernel_package'

# 収集対象：SITE_CODE_ROOT 直下のファイルのうち、この拡張子のもの
KERNEL_FILE_EXTS = ('.py', '.txt', '.md', '.json', '.sql', '.cfg', '.ini')

# 収集対象：SITE_CODE_ROOT 直下のディレクトリ（再帰）
# static_for_distribution = 配布する公開資産の原本（CSS・JS・アイコン・規約類）。
# 実行中のコードは書き込まないので、投稿データが紛れ込む経路がない。
# 一方 static/ は nginx が配信する実行時ディレクトリで投稿が混在するため、
# 引き続き対象外（件数だけ static_excluded に記録）。
KERNEL_INCLUDE_DIRS = ('templates', 'static_for_distribution')

# 除外するファイル名（SITE_CODE_ROOT 直下）
KERNEL_EXCLUDE_FILES = frozenset({
    'config.py',    # 秘密情報。値は出さず、設定名だけ config_keys に載せる
    'README.txt',   # PythonAnywhere の既定ファイル
})

# この接頭辞で始まるファイルは名前だけで除外（OAuth資格情報ファイル等）
KERNEL_EXCLUDE_PREFIXES = ('client_secret', 'credentials', 'service_account')
KERNEL_MAX_FILE_SIZE = 5 * 1024 * 1024
KERNEL_VERSION_FILE = os.path.join(SITE_CODE_ROOT, 'kernel_version.json')

# 秘密らしき記述の検出（検出した値そのものは記録しない）
# 資格情報らしき代入。名前には前後に語がつく（DB_PASSWORD / SECRET_KEY /
# LINE_CHANNEL_SECRET など）ので \b では捕まらない。前後の語を許す。
# 右辺がリテラル文字列のときだけ拾う（Config.X や os.environ.get(...) は対象外）。
_SECRET_PATTERNS = [
    # 値は「空白を含まない印字可能ASCII」に限る。日本語のメッセージ
    #（例: _ERROR_JA の 'token_revoked': 'トークンが失効しています'）は
    # 資格情報ではないので、これで誤検出が落ちる。
    (re.compile(r'(?i)[A-Za-z0-9_.]*'
                r'(?:password|passwd|secret|token|api_?key|credential|private_key)'
                r'[A-Za-z0-9_]*[\'"]?\s*[:=]\s*[\'"][\x21-\x7e]{4,}[\'"]'),
     '資格情報らしき代入'),
    (re.compile(r'mysql\.pythonanywhere-services\.com'), 'DBホスト名の直書き'),
    (re.compile(r'GOCSPX-'), 'Google クライアントシークレット'),
    (re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----'), '秘密鍵'),
]


def _scan_secrets(rel_path, text):
    """資格情報らしき記述を検出する。値は記録せず、位置と種別だけ返す。"""
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        for pat, label in _SECRET_PATTERNS:
            if pat.search(line):
                out.append({'path': rel_path, 'line': i, 'kind': label})
                break
    return out

_CONFIG_KEY_RE = re.compile(r'^\s*([A-Z][A-Z0-9_]*)\s*=', re.M)


def _config_keys(config_path):
    """config.py に定義されている設定名（大文字定数）だけを列挙する。
    値は読み取っても記録しない。カーネル更新時に「受け入れ側に不足している
    設定」を知らせるために使う。
    ※ config_template.py の生成はさいまるの担当（あちらには生成後の
      人によるレビュー段階があるため匿名化を試みてよい）。"""
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            src = f.read()
    except Exception:
        return []
    return sorted(set(_CONFIG_KEY_RE.findall(src)))

# カーネルのコードが実際に参照している設定名（Config.XXX / config['XXX']）
# Config.XXX / config.XXX（Jinja） / config['XXX'] / config.get('XXX') /
# getattr(Config, 'XXX', ...)（＝まだ値が決まっていない設定の受け取り口）
_CONFIG_USE_RE = re.compile(
    r'\b[Cc]onfig\.([A-Z][A-Z0-9_]*)'
    r'|\b[Cc]onfig\[[\'"]([A-Z][A-Z0-9_]*)[\'"]\]'
    r'|\b[Cc]onfig\.get\(\s*[\'"]([A-Z][A-Z0-9_]*)[\'"]'
    r'|\bgetattr\(\s*[Cc]onfig\s*,\s*[\'"]([A-Z][A-Z0-9_]*)[\'"]'
)


def _config_keys_used(files):
    """カーネルのコードが参照している設定名を集める。
    受け入れ側には「カーネルが本当に要求する設定」だけを知らせたいので、
    config.py に定義されているだけのアプリ固有の設定と区別する。"""
    used = set()
    for f in files:
        if f.get('encoding') != 'text':
            continue
        if not f['path'].endswith(('.py', '.html')):
            continue
        for m in _CONFIG_USE_RE.finditer(f.get('content') or ''):
            used.add(next(g for g in m.groups() if g))
    return sorted(used)

_BP_IMPORT_RE = re.compile(r'^\s*from\s+([A-Za-z_][\w.]*)\s+import\s+(.+)$', re.M)
_BP_REG_RE = re.compile(r'register_blueprint\(\s*([A-Za-z_]\w*)')
# 括弧で複数行に分けた import を1行に畳む（from X import ( a, b, c ) 形式）
_PAREN_IMPORT_RE = re.compile(r'from\s+([A-Za-z_][\w.]*)\s+import\s*\(([^)]*)\)')
_APP_PKG_NAME = os.path.basename(BASE_DIR.rstrip('/')) or 'fujinp'


def _strip_comment_lines(text):
    """行頭が # の行を空行に置き換える。
    コメントアウトされた import / register_blueprint を「生きている」と
    誤認しないため。行末コメント（コード # 説明）は壊さない。"""
    return '\n'.join('' if ln.lstrip().startswith('#') else ln
                     for ln in (text or '').split('\n'))


def _kernel_required_modules(app_py_text):
    """app.py が register_blueprint している Blueprint と、その由来アプリ名を返す。
    受け入れ側で「そのアプリがローカルに在るか」を照合するために使う。
    'fujinp.app_share' → 'app_share'、'admin'（ホーム直下）→ 'admin' に正規化する。
    コメントアウトされた行は無視する。"""
    src = _strip_comment_lines(app_py_text)
    # 括弧で複数行に分けた import を1行に畳む（from X import ( a, b, c ) 形式）
    flat = _PAREN_IMPORT_RE.sub(
        lambda m: 'from %s import %s' % (m.group(1), ' '.join(m.group(2).split())),
        src)
    bp_to_mod = {}
    for mod, names in _BP_IMPORT_RE.findall(flat):
        for n in re.split(r'[,\s]+', names.replace('(', ' ').replace(')', ' ')):
            n = n.strip().rstrip(',')
            if n and n != 'as':
                bp_to_mod[n] = mod

    seen, out = set(), []
    for bp in _BP_REG_RE.findall(src):
        mod = bp_to_mod.get(bp)
        top = None
        if mod:
            parts = mod.split('.')
            # 先頭が fujinp（アプリ用パッケージ）なら、その次がアプリ名
            top = parts[1] if (parts[0] == _APP_PKG_NAME and len(parts) > 1) else parts[0]
        key = (bp, top)
        if key in seen:
            continue
        seen.add(key)
        out.append({'blueprint': bp, 'module': mod, 'app': top})
    return out


def _build_kernel_package(generated_by=None, site_url=None):
    """カーネルのエクスポートパッケージを組み立てる。"""
    files = []
    warnings = []
    latest_mtime = None

    def _add(abs_path, rel_path, block_on_secret=True):
        """block_on_secret=False のときは、資格情報らしき記述を検出しても
        警告に記録するだけで内容は同梱する（ホーム直下の .py 用）。"""
        nonlocal latest_mtime
        try:
            size = os.path.getsize(abs_path)
            mt = os.path.getmtime(abs_path)
        except OSError:
            return
        if latest_mtime is None or mt > latest_mtime:
            latest_mtime = mt
        entry = {
            'path': rel_path,
            'size': size,
            'mtime': datetime.datetime.fromtimestamp(mt, JST).strftime('%Y-%m-%d %H:%M:%S'),
        }
        if size > KERNEL_MAX_FILE_SIZE:
            entry.update({'skipped': True, 'reason': 'サイズ超過'})
            files.append(entry)
            return
        try:
            with open(abs_path, 'r', encoding='utf-8') as f:
                text = f.read()
            hits = _scan_secrets(rel_path, text)
            if hits:
                for h in hits:
                    h['excluded'] = bool(block_on_secret)
                warnings.extend(hits)
                if block_on_secret:
                    # テンプレート・配布原本・直下の非.pyファイルは、疑わしければ
                    # 中身を出さない（アプシャのエクスポートに人のレビュー段階が
                    # 無いため）。誤検出でも「除外された」と受け入れ側に見える。
                    entry.update({'skipped': True,
                                  'reason': '資格情報らしき記述を含むため内容を除外',
                                  'secret_hits': len(hits)})
                    files.append(entry)
                    return
                # ホーム直下の .py はカーネル本体。落とすとサイトが動かなくなる
                # ので、警告を残して同梱する（config.py は名前で完全除外済み）。
                entry['secret_hits'] = len(hits)
            entry['encoding'] = 'text'
            entry['content'] = text
        except (UnicodeDecodeError, ValueError):
            with open(abs_path, 'rb') as f:
                entry['encoding'] = 'base64'
                entry['content'] = base64.b64encode(f.read()).decode('ascii')
        files.append(entry)

    # 1) ホーム直下のファイル
    for name in sorted(os.listdir(SITE_CODE_ROOT)):
        p = os.path.join(SITE_CODE_ROOT, name)
        if not os.path.isfile(p):
            continue
        if name.startswith('.') or name in KERNEL_EXCLUDE_FILES:
            continue
        if name.startswith(KERNEL_EXCLUDE_PREFIXES):
            continue
        if not name.endswith(KERNEL_FILE_EXTS):
            continue
        # ホーム直下の .py はカーネル本体。config.py だけ名前で完全除外し
        # （KERNEL_EXCLUDE_FILES）、残りは誤検出で落とさず警告のみとする。
        _add(p, name, block_on_secret=not name.endswith('.py'))

    # 2) 指定ディレクトリ（再帰）
    for d in KERNEL_INCLUDE_DIRS:
        root = os.path.join(SITE_CODE_ROOT, d)
        if not os.path.isdir(root):
            continue
        for cur, dirs, fnames in os.walk(root):
            dirs[:] = [x for x in dirs if x != '__pycache__' and not x.startswith('.')]
            for fn in sorted(fnames):
                if fn.startswith('.') or fn.endswith('.pyc'):
                    continue
                ap = os.path.join(cur, fn)
                _add(ap, os.path.relpath(ap, SITE_CODE_ROOT).replace('\\', '/'))

    # static は配布対象外（投稿データが混在するため）。必要なファイルは
    # 送り手の admin が個別に渡す。ここには件数だけ残す。
    static_excluded = []
    _sd = os.path.join(SITE_CODE_ROOT, 'static')
    if os.path.isdir(_sd):
        static_excluded.append(
            {'path': 'static',
             'file_count': sum(len(fs) for _r, _d, fs in os.walk(_sd))})

    # 3) 設定名（値は含めない）
    config_keys = _config_keys(os.path.join(SITE_CODE_ROOT, 'config.py'))
    config_keys_used = _config_keys_used(files)

    # 4) app.py が要求するアプリ（受け入れ側の事前チェック用）
    app_py = next((f for f in files if f['path'] == 'app.py'), None)
    required = _kernel_required_modules(app_py.get('content') or '') if app_py else []

    # 5) 版（内容ハッシュ＋最終更新時刻）
    parts = []
    for f in sorted(files, key=lambda x: x['path']):
        parts.append(f['path'])
        parts.append(str(f.get('content') or ''))
    content_hash = hashlib.sha1('\n'.join(parts).encode('utf-8', 'replace')).hexdigest()[:6]
    updated_at = (datetime.datetime.fromtimestamp(latest_mtime, JST).strftime('%Y-%m-%d %H:%M:%S')
                  if latest_mtime else None)

    version_id = None
    try:
        with open(KERNEL_VERSION_FILE, 'r', encoding='utf-8') as f:
            version_id = (json.load(f) or {}).get('version_id')
    except Exception:
        pass
    # 未確定なら None のまま。ここで時刻から作ると、内容が同じでも
    # エクスポートのたびに版が変わり、取り込み側の新旧判定が壊れる。
    # 同一性の判定は content_hash で行う。
    return {
        'export_type': KERNEL_EXPORT_TYPE,
        'format_version': 1,
        'display_name': 'FUJIN-P カーネル',
        'description': 'プラットフォーム共通部（ホーム直下のコードと templates/）。'
                       'アプリは含まない。config.py は同梱せず、設定名のみ config_keys に載せる。',
        'site_name': os.path.basename(SITE_CODE_ROOT),
        'site_url': site_url or '',
        'generated_at': _now_jst().strftime('%Y-%m-%d %H:%M:%S'),
        'generated_by': generated_by,
        'updated_at': updated_at,
        'content_hash': content_hash,
        'version_id': version_id,
        'file_count': len(files),
        'files': files,
        'static_excluded': static_excluded,
        'config_keys': config_keys,
        'config_keys_used': config_keys_used,
        'required_apps': required,
        'warnings': warnings,
        'package_note': (
            'FUJIN-P カーネルパッケージ。files[]=SITE_CODE_ROOT直下のコードと templates/ '
            '（config.py・static・fujinp・アップロード領域は除外）/ '
            'config_keys=config.py が定義している設定名の一覧（値は含まない）/ '
            'config_keys_used=カーネルのコードが実際に参照している設定名'
            '（受け入れ側の不足設定の判定にはこちらを使う）/ '
            'required_apps=app.py が register_blueprint しているアプリ'
            '（app キーがアプリ名。受け入れ側で実在を照合すること）/ '
            'warnings=秘密らしき記述の検出結果（値は含まない）。'
            'static_excluded=配布対象外とした static の件数（投稿データ混在のため一切配らない。'
            '必要な静的ファイルは admin が手作業で移すこと）。'
        ),
    }


@app_share_bp.route('/export_kernel_package', methods=['GET'])
@login_required
def export_kernel_package():
    """カーネルのエクスポートパッケージ(JSON)を生成してダウンロード
    ※admin限定（_app_share_authorize による）"""
    conn = None
    try:
        generated_by = None
        try:
            conn = mysql.connector.connect(**DatabaseConfig.default())
            cursor = conn.cursor(dictionary=True, buffered=True)
            cursor.execute("SELECT full_name FROM users WHERE id = %s",
                           (session.get('user_id'),))
            u = cursor.fetchone()
            generated_by = u['full_name'] if u else None
            cursor.close()
        except Exception:
            pass

        site_url = request.host_url.rstrip('/') if request else ''
        package = _build_kernel_package(generated_by=generated_by, site_url=site_url)

        json_text = json.dumps(package, ensure_ascii=False, indent=2, default=str)
        filename = "fujinp_kernel_{}.json".format(_now_jst().strftime('%Y%m%d_%H%M%S'))
        resp = Response(json_text, mimetype='application/json; charset=utf-8')
        resp.headers['Content-Disposition'] = 'attachment; filename="{}"'.format(filename)
        return resp
    except Exception as e:
        logging.error("export_kernel_package error: %s", e)
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            conn.close()

########

def _safe_rel_path(rel):
    """パッケージ内相対パスの安全性チェック（ディレクトリトラバーサル防止）"""
    if not rel or rel.startswith('/') or rel.startswith('\\'):
        return False
    parts = rel.replace('\\', '/').split('/')
    return '..' not in parts and '' not in parts


def _package_file_bytes(entry):
    """パッケージのファイルエントリからバイト列を復元（skipped等はNone）"""
    if entry.get('skipped'):
        return None
    content = entry.get('content')
    if content is None:
        return None
    if entry.get('encoding') == 'base64':
        try:
            return base64.b64decode(content)
        except Exception:
            return None
    return str(content).encode('utf-8')


def _parse_create_columns(ddl):
    """CREATE TABLE文からカラム定義 {カラム名: 定義} を抽出"""
    cols = {}
    for line in (ddl or '').splitlines():
        line = line.strip().rstrip(',')
        m = re.match(r'^`([^`]+)`\s+(.+)$', line)
        if m:
            cols[m.group(1)] = re.sub(r'\s+', ' ', m.group(2)).strip()
    return cols


def _normalize_col_def(d):
    """カラム定義の表示揺れを吸収して比較用に正規化する。
    MySQL の SHOW CREATE TABLE は、同じ定義でも作成時の書き方や
    バージョンにより「CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci」と
    「COLLATE utf8mb4_unicode_ci」を出し分けることがある。
    COLLATE 名が CHARACTER SET 名を接頭辞に持つ場合（＝charset が
    collation から一意に決まる場合）に限り CHARACTER SET 句を落とす。
    接頭辞が一致しない組み合わせは実差なので残す。"""
    s = re.sub(r'\s+', ' ', d or '').strip()
    s = re.sub(r'\bCHARACTER SET (\w+)\s+(?=COLLATE \1_)', '', s, flags=re.I)
    return s.lower()


def _db_tables_map():
    """対象DB（<owner>$default / <owner>$fujinp）ごとのテーブル集合 {dbname: set()}
    同一リクエスト内では1回だけ問い合わせる（全アプリ一括エクスポートで
    アプリ数ぶん接続を開かないため）。"""
    try:
        cached = getattr(g, '_app_share_tables_map', None)
    except RuntimeError:
        cached = None
    if cached is not None:
        return cached
    base, dbs = _gc_target_databases()
    out = {}
    for dbname in dbs:
        d = dict(base)
        d['database'] = dbname
        try:
            c = mysql.connector.connect(**d)
            cu = c.cursor()
            cu.execute("SHOW TABLES")
            out[dbname] = set(r[0] for r in cu.fetchall())
            cu.close()
            c.close()
        except Exception:
            pass
    try:
        g._app_share_tables_map = out
    except RuntimeError:
        pass
    return out


def _resolve_target_db(db_hint):
    """パッケージのDBヒント（'default'/'fujinp'）を受け入れ側の実DB名に解決する。
    ヒントが無い（旧形式パッケージ）場合は既定DBを返し、guessed=True を添える。"""
    base = DatabaseConfig.default()
    default_db = base.get('database') or ''
    prefix = default_db.rsplit('$', 1)[0] if '$' in default_db else ''
    if db_hint in ('default', 'fujinp') and prefix:
        return f'{prefix}${db_hint}', False
    return default_db, True


def _compare_table_any(table, pkg_ddl, tables_map=None, db_hint=None):
    """期待DDL(pkg_ddl)と受け入れ側の実テーブルを両DB横断で照合する。
    db_hint はパッケージの sql_databases 由来（'default'/'fujinp'）。
    戻り値: {'table','status'('invalid'|'missing'|'same'|'diff'),'database'?,
             'use_sql'(対象DBへ切り替える USE 文),'db_guessed'?,
             'added_columns','changed_columns','local_only_columns',
             'alter_sql','create_sql'(=期待DDL),'resolved'}
    resolved = 追加カラムなしで存在（定義差のみは resolved=True 扱い）
    database/use_sql は、実在すれば見つかった実DB、無ければヒントから解決。"""
    table = str(table)
    if not _TABLE_NAME_RE.match(table):
        return {'table': table, 'status': 'invalid', 'resolved': False,
                'create_sql': pkg_ddl}
    if tables_map is None:
        tables_map = _db_tables_map()
    base, _dbs = _gc_target_databases()
    found_db = None
    for dbname, tset in tables_map.items():
        if table in tset:
            found_db = dbname
            break
    if not found_db:
        use_db, guessed = _resolve_target_db(db_hint)
        return {'table': table, 'status': 'missing', 'create_sql': pkg_ddl,
                'database': use_db, 'db_guessed': guessed,
                'use_sql': f'USE `{use_db}`;',
                'resolved': False}
    local_ddl = ''
    d = dict(base)
    d['database'] = found_db
    try:
        c = mysql.connector.connect(**d)
        cu = c.cursor(dictionary=True, buffered=True)
        cu.execute(f"SHOW CREATE TABLE `{table}`")
        row = cu.fetchone()
        local_ddl = (row.get('Create Table') or row.get('Create View') or '') if row else ''
        cu.close()
        c.close()
    except Exception as e:
        return {'table': table, 'status': 'error', 'database': found_db,
                'use_sql': f'USE `{found_db}`;',
                'create_sql': pkg_ddl, 'resolved': False, 'error': str(e)}
    pkg_cols = _parse_create_columns(pkg_ddl)
    local_cols = _parse_create_columns(local_ddl)
    added = [{'name': cname, 'definition': pkg_cols[cname]}
             for cname in pkg_cols if cname not in local_cols]
    changed = [{'name': cname, 'package_def': pkg_cols[cname], 'local_def': local_cols[cname]}
               for cname in pkg_cols
               if cname in local_cols
               and _normalize_col_def(pkg_cols[cname]) != _normalize_col_def(local_cols[cname])]
    local_only_cols = [cname for cname in local_cols if cname not in pkg_cols]
    alter_sql = '\n'.join(
        f"ALTER TABLE `{table}` ADD COLUMN `{a['name']}` {a['definition']};"
        for a in added)
    # 新しい版に無いカラムは、古い版の残骸である可能性が高い。
    # 自動実行はせず、文面だけ提示する（他アプリが参照している場合があるため）。
    drop_sql = '\n'.join(
        f"ALTER TABLE `{table}` DROP COLUMN `{c}`;" for c in local_only_cols)
    status = 'same' if not added and not changed else 'diff'
    return {'table': table, 'status': status, 'database': found_db,
            'use_sql': f'USE `{found_db}`;',
            'added_columns': added, 'changed_columns': changed,
            'local_only_columns': local_only_cols,
            'alter_sql': alter_sql or None,
            'drop_sql': drop_sql or None,
            'create_sql': pkg_ddl,
            'resolved': not added}


def _check_package_core(cursor, app_name, pkg):
    """パッケージ検証の中核（単体検証・バッチ検証で共用）。
    identity/version/site/files/tables/libraries を含む dict を返す。"""
    # --- 1) 同一性・バージョン検証 ---
    identity = {
        'target_app': app_name,
        'package_app': pkg.get('app_name'),
        'match': pkg.get('app_name') == app_name,
        'export_type_ok': pkg.get('export_type') == 'fujinp_app_package',
        'format_version': pkg.get('format_version')
    }
    reg_ts, doc_ts = _collect_local_timestamps(cursor)
    local_ts = _local_app_updated_at(app_name, reg_ts, doc_ts)
    pkg_ts = _parse_ts(pkg.get('updated_at'))
    if pkg_ts is None:
        vstatus = 'unknown'
    elif local_ts is None or pkg_ts > local_ts:
        vstatus = 'newer'
    elif pkg_ts < local_ts:
        vstatus = 'older'
    else:
        vstatus = 'same'
    version = {
        'package_updated_at': pkg.get('updated_at'),
        'local_updated_at': local_ts.strftime('%Y-%m-%d %H:%M:%S') if local_ts else None,
        'status': vstatus,
        'package_version_id': pkg.get('version_id'),
        'local_version_id': _get_version_id(app_name),
    }

    # --- 2) 改修の概要（ファイル差分。置き換えのみ） ---
    app_path = os.path.join(BASE_DIR, app_name)
    details = []
    counts = {'new': 0, 'changed': 0, 'same': 0, 'skipped': 0}
    pkg_paths = set()
    for entry in pkg.get('files', []):
        rel = entry.get('path') or ''
        if not _safe_rel_path(rel):
            counts['skipped'] += 1
            details.append({'path': rel, 'status': 'skipped', 'reason': '不正なパス'})
            continue
        pkg_paths.add(rel)
        if entry.get('skipped'):
            counts['skipped'] += 1
            details.append({'path': rel, 'status': 'skipped',
                            'reason': entry.get('reason', 'パッケージ作成時にスキップ')})
            continue
        raw = _package_file_bytes(entry)
        lpath = os.path.join(app_path, rel)
        if not os.path.exists(lpath):
            st = 'new'
        else:
            try:
                with open(lpath, 'rb') as f:
                    st = 'same' if f.read() == raw else 'changed'
            except Exception:
                st = 'changed'
        counts[st] += 1
        details.append({'path': rel, 'status': st, 'size': entry.get('size')})
    local_only = []
    if os.path.isdir(app_path):
        for root, dirs, filenames in os.walk(app_path):
            dirs[:] = [d for d in dirs if d not in ('static', '__pycache__', 'import_staging', 'import_backups')]
            for fn in filenames:
                if fn.endswith('.pyc'):
                    continue
                rel = os.path.relpath(os.path.join(root, fn), app_path).replace(os.sep, '/')
                if rel not in pkg_paths:
                    local_only.append(rel)
    files_result = {'counts': counts, 'details': details,
                    'local_only': sorted(local_only),
                    'app_dir_exists': os.path.isdir(app_path)}

    # --- 3) テーブルスキーマ照合（両DB横断） ---
    tables_map = _db_tables_map()

    tables_result = []
    pkg_dbs = pkg.get('sql_databases') or {}
    for table, pkg_ddl in (pkg.get('sql_schemas') or {}).items():
        tables_result.append(_compare_table_any(table, pkg_ddl, tables_map,
                                                db_hint=pkg_dbs.get(table)))

    # --- 4) ライブラリ照合 ---
    libs_result = []
    for lib in (pkg.get('libraries') or {}).get('third_party', []):
        try:
            installed = importlib.util.find_spec(str(lib)) is not None
        except Exception:
            installed = False
        libs_result.append({'name': lib, 'installed': installed,
                            'pip_name': PIP_NAME_MAP.get(lib, lib)})

    site = {'site_name': pkg.get('site_name'), 'site_url': pkg.get('site_url'),
            'generated_at': pkg.get('generated_at'),
            'generated_by': pkg.get('generated_by')}

    return {'identity': identity, 'version': version, 'site': site,
            'files': files_result, 'tables': tables_result,
            'libraries': libs_result, 'launchers': pkg.get('launchers') or []}


def _apply_package_files(app_name, pkg, now, ver_ts):
    """パッケージのファイル群を配置（事前バックアップつき）。
    戻り値: (written, skipped, backup_name, errors)"""
    app_path = os.path.join(BASE_DIR, app_name)
    backup_name = None
    if os.path.isdir(app_path):
        os.makedirs(IMPORT_BACKUP_DIR, exist_ok=True)
        backup_name = f"{app_name}_{now.strftime('%Y%m%d_%H%M%S')}"
        # app_share 自身を取り込む場合、バックアップ先がソースの内側になるため
        # import_backups / import_staging / __pycache__ は複製から除外する（再帰防止）
        shutil.copytree(app_path, os.path.join(IMPORT_BACKUP_DIR, backup_name),
                        ignore=shutil.ignore_patterns(
                            'import_backups', 'import_staging', '__pycache__'))
    else:
        os.makedirs(app_path, exist_ok=True)

    written = 0
    skipped = 0
    errors = []
    for entry in pkg.get('files', []):
        rel = entry.get('path') or ''
        if not _safe_rel_path(rel):
            skipped += 1
            errors.append(f'{rel}: 不正なパスのためスキップ')
            continue
        raw = _package_file_bytes(entry)
        if raw is None:
            skipped += 1
            continue
        try:
            lpath = os.path.join(app_path, rel)
            parent = os.path.dirname(lpath)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(lpath, 'wb') as f:
                f.write(raw)
            written += 1
        except Exception as e:
            errors.append(f'{rel}: {e}')

    # バージョン判定用ファイルの更新時刻をパッケージの日時に合わせる
    ts_epoch = ver_ts.timestamp()
    for fn in ('app_info.json', 'manifest.json'):
        p = os.path.join(app_path, fn)
        if os.path.exists(p):
            try:
                os.utime(p, (ts_epoch, ts_epoch))
            except Exception:
                pass
    return written, skipped, backup_name, errors


def _apply_package_db(cursor, app_name, pkg, user_id, ver_ts):
    """レジストリ（アプリ説明）とドキュメント（マニュアル・仕様書）を更新（commitしない）。"""
    display_name = pkg.get('display_name') or app_name
    icon = pkg.get('icon') or '📦'
    description = pkg.get('description') or ''
    cursor.execute("SELECT id FROM app_share_registry WHERE app_name = %s", (app_name,))
    reg = cursor.fetchone()
    if reg:
        try:
            cursor.execute("""UPDATE app_share_registry
                SET display_name=%s, icon=%s, description=%s, updated_at=%s
                WHERE id=%s""",
                (display_name, icon, description, ver_ts, reg['id']))
        except mysql.connector.Error:
            cursor.execute("""UPDATE app_share_registry
                SET display_name=%s, icon=%s, description=%s
                WHERE id=%s""",
                (display_name, icon, description, reg['id']))
    else:
        cursor.execute("SELECT COALESCE(MAX(sort_order),0)+10 AS n FROM app_share_registry")
        nxt = cursor.fetchone()['n']
        try:
            cursor.execute("""INSERT INTO app_share_registry
                (app_name, display_name, icon, description, sort_order, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s)""",
                (app_name, display_name, icon, description, nxt, ver_ts))
        except mysql.connector.Error:
            cursor.execute("""INSERT INTO app_share_registry
                (app_name, display_name, icon, description, sort_order)
                VALUES (%s,%s,%s,%s,%s)""",
                (app_name, display_name, icon, description, nxt))

    # パッケージは常に最新版とみなし、マニュアル・仕様書メモを無条件で更新する。
    # フィールドが含まれていれば内容が空でも反映（＝ミラーリング）。
    # フィールド自体が無い（旧/外部形式）パッケージのみ既存を保持する。
    for doc_type, key in (('manual', 'user_manual'), ('spec', 'spec_memo')):
        if key not in pkg:
            continue
        d = pkg.get(key)
        if not isinstance(d, dict):
            d = {}
        dts = _parse_ts(d.get('updated_at')) or ver_ts
        cursor.execute("""
            INSERT INTO app_share_documents
                (app_name, doc_type, title, content, updated_by, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                title = VALUES(title), content = VALUES(content),
                updated_by = VALUES(updated_by), updated_at = VALUES(updated_at)
        """, (app_name, doc_type, d.get('title') or '', d.get('content') or '', user_id, dts))
    return display_name


@app_share_bp.route('/import_package/<app_name>')
@login_required
def import_package_page(app_name):
    """パッケージ取り込み専用ダッシュボード（admin専用）"""
    if not check_admin_permission(session.get('user_id')):
        return "管理者権限が必要です", 403
    display_name = app_name
    icon = '📦'
    conn = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        with conn.cursor(dictionary=True, buffered=True) as cursor:
            cursor.execute(
                "SELECT display_name, icon FROM app_share_registry WHERE app_name = %s",
                (app_name,))
            reg = cursor.fetchone()
            while cursor.nextset(): pass
            if reg:
                display_name = reg['display_name'] or app_name
                icon = reg['icon'] or '📦'
    except Exception as e:
        logging.error(f"import_package_page error: {e}")
    finally:
        if conn and conn.is_connected():
            conn.close()
    return render_template('app_share_import.html',
                           app_name=app_name, display_name=display_name, icon=icon)


@app_share_bp.route('/import_package_check/<app_name>', methods=['POST'])
@login_required
def import_package_check(app_name):
    """パッケージの検証（アプリ一致・バージョン・ファイル差分・スキーマ・ライブラリ）"""
    if not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403
    pkg = request.json or {}
    conn = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True, buffered=True)
        result = _check_package_core(cursor, app_name, pkg)
        result['success'] = True
        return jsonify(result)
    except Exception as e:
        logging.error("import_package_check error (%s): %s", app_name, e)
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'cursor' in locals(): cursor.close()
        if conn and conn.is_connected(): conn.close()


@app_share_bp.route('/import_package_apply/<app_name>', methods=['POST'])
@login_required
def import_package_apply(app_name):
    """検証済みパッケージの取り込み実行（バックアップ→ファイル置き換え→説明・ドキュメント更新）"""
    if not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403
    pkg = request.json or {}
    if pkg.get('export_type') != 'fujinp_app_package':
        return jsonify({'success': False, 'error': 'パッケージ形式が不正です'}), 400
    if pkg.get('app_name') != app_name:
        return jsonify({'success': False,
                        'error': f'アプリが一致しません（対象: {app_name} / パッケージ: {pkg.get("app_name")}）'}), 400

    user_id = session.get('user_id')
    now = datetime.datetime.now()
    ver_ts = _parse_ts(pkg.get('updated_at')) or now
    conn = None
    try:
        # --- 1-2) バックアップ＋ファイル置き換え ---
        written, skipped, backup_name, errors = _apply_package_files(app_name, pkg, now, ver_ts)

        # --- 3) アプリ説明（レジストリ）・ドキュメントの更新 ---
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True, buffered=True)
        display_name = _apply_package_db(cursor, app_name, pkg, user_id, ver_ts)
        conn.commit()

        return jsonify({'success': True,
                        'message': f'「{display_name}」を取り込みました（{written}ファイル置き換え）',
                        'written': written, 'skipped': skipped,
                        'backup': backup_name, 'errors': errors,
                        'blueprint_registration': _blueprint_snippet_from_package(pkg)})

    except Exception as e:
        if conn: conn.rollback()
        logging.error("import_package_apply error (%s): %s", app_name, e)
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'cursor' in locals(): cursor.close()
        if conn and conn.is_connected(): conn.close()


# --- パッケージ一時ステージング（モーダル→取り込み画面の受け渡し。二重アップロード回避） ---

def _cleanup_staging():
    """失効した（TTL超過の）ステージングファイルを削除"""
    try:
        if not os.path.isdir(IMPORT_STAGING_DIR):
            return
        cutoff = datetime.datetime.now().timestamp() - STAGING_TTL_SECONDS
        for fn in os.listdir(IMPORT_STAGING_DIR):
            p = os.path.join(IMPORT_STAGING_DIR, fn)
            try:
                if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except Exception:
                pass
    except Exception as e:
        logging.warning("staging cleanup skip: %s", e)


@app_share_bp.route('/stage_package', methods=['POST'])
@login_required
def stage_package():
    """パッケージJSONをサーバに一時保存し、取り出し用トークンを返す（admin専用）"""
    if not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403
    pkg = request.json or {}
    if pkg.get('export_type') != 'fujinp_app_package':
        return jsonify({'success': False, 'error': 'アプリ単位のパッケージではありません'}), 400
    app_name = pkg.get('app_name')
    if not app_name or not re.match(r'^[A-Za-z0-9_]+$', str(app_name)):
        return jsonify({'success': False, 'error': 'app_name が不正です'}), 400
    try:
        os.makedirs(IMPORT_STAGING_DIR, exist_ok=True)
        _cleanup_staging()
        token = secrets.token_hex(16)
        path = os.path.join(IMPORT_STAGING_DIR, token + '.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(pkg, f, ensure_ascii=False)
        return jsonify({'success': True, 'token': token, 'app_name': app_name})
    except Exception as e:
        logging.error("stage_package error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@app_share_bp.route('/staged_package/<token>', methods=['GET'])
@login_required
def staged_package(token):
    """ステージング済みパッケージを取り出す（admin専用）"""
    if not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403
    if not re.match(r'^[a-f0-9]{32}$', token or ''):
        return jsonify({'success': False, 'error': '不正なトークン'}), 400
    path = os.path.join(IMPORT_STAGING_DIR, token + '.json')
    if not os.path.exists(path):
        return jsonify({'success': False, 'error': 'ステージングが見つかりません（期限切れの可能性）'}), 404
    try:
        with open(path, 'r', encoding='utf-8') as f:
            pkg = json.load(f)
        return jsonify({'success': True, 'package': pkg})
    except Exception as e:
        logging.error("staged_package error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================
# 全アプリ情報JSON（パッケージ入り）の一括受け入れ（admin専用・管理タブ）
#
#   export_site_overview（format_version>=3、includes_packages=true）が出力した
#   各アプリ package を使って、選択したアプリごとに個別アプリ取り込みを実行する。
#
#   POST /import_site_preview  … アプリ一覧＋ローカル状況を返す
#       body: {apps: [{app_name, updated_at, has_package}]}
#       default_selected = 受け入れ側に既にディレクトリがあるアプリ（オプトイン）
#   POST /import_site_apply    … 選択アプリのパッケージを一括取り込み
#       body: {packages: [<fujinp_app_package>, ...]}
#       各アプリ: 事前バックアップ→ファイル置換→説明/ドキュメント更新、
#                 及び スキーマ/ライブラリ/ランチャの案内（自動実行しない）
# ============================================

@app_share_bp.route('/import_site_preview', methods=['POST'])
@login_required
def import_site_preview():
    """全アプリ情報JSON（パッケージ入り）のアプリ一覧を、受け入れ側の状況付きで返す"""
    if not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403
    data = request.json or {}
    apps = data.get('apps', [])
    if not isinstance(apps, list):
        return jsonify({'success': False, 'error': 'apps はリストで指定してください'}), 400
    conn = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True, buffered=True)
        reg_ts, doc_ts = _collect_local_timestamps(cursor)
        results = []
        for a in apps:
            name = (a.get('app_name') or '').strip()
            if not name:
                continue
            has_pkg = bool(a.get('has_package'))
            app_path = os.path.join(BASE_DIR, name)
            local_dir_exists = os.path.isdir(app_path)
            registered = name in reg_ts
            imp_ts = _parse_ts(a.get('updated_at'))
            local_ts = _local_app_updated_at(name, reg_ts, doc_ts) if (registered or local_dir_exists) else None
            if not local_dir_exists and not registered:
                vstatus = 'new'
            elif imp_ts is None:
                vstatus = 'unknown'
            elif local_ts is None or imp_ts > local_ts:
                vstatus = 'newer'
            elif imp_ts < local_ts:
                vstatus = 'older'
            else:
                vstatus = 'same'
            results.append({
                'app_name': name,
                'has_package': has_pkg,
                'local_dir_exists': local_dir_exists,
                'registered': registered,
                'version_status': vstatus,
                'import_updated_at': a.get('updated_at') or None,
                'local_updated_at': local_ts.strftime('%Y-%m-%d %H:%M:%S') if local_ts else None,
                # デフォルト選択（オプトイン）= 受け入れ側に既にディレクトリがある & パッケージあり
                'default_selected': bool(has_pkg and local_dir_exists)
            })
        return jsonify({'success': True, 'results': results})
    except Exception as e:
        logging.error("import_site_preview error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'cursor' in locals(): cursor.close()
        if conn and conn.is_connected(): conn.close()


@app_share_bp.route('/import_site_apply', methods=['POST'])
@login_required
def import_site_apply():
    """選択された各アプリのパッケージを一括で個別取り込みする"""
    if not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403
    data = request.json or {}
    packages = data.get('packages', [])
    if not isinstance(packages, list) or not packages:
        return jsonify({'success': False, 'error': 'packages が空です'}), 400

    user_id = session.get('user_id')
    conn = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True, buffered=True)

        results = []
        imported = failed = 0
        for pkg in packages:
            if not isinstance(pkg, dict):
                continue
            name = (pkg.get('app_name') or '').strip()
            entry = {'app_name': name}
            # 形式・アプリ名の妥当性
            if pkg.get('export_type') != 'fujinp_app_package':
                entry.update({'ok': False, 'error': 'パッケージ形式が不正です'})
                results.append(entry); failed += 1; continue
            if not name or not re.match(r'^[A-Za-z0-9_]+$', name):
                entry.update({'ok': False, 'error': 'app_name が不正です'})
                results.append(entry); failed += 1; continue
            try:
                # 取り込み前に案内情報（スキーマ・ライブラリ・ランチャ・バージョン）を収集
                chk = _check_package_core(cursor, name, pkg)
                now = datetime.datetime.now()
                ver_ts = _parse_ts(pkg.get('updated_at')) or now
                written, skipped, backup_name, errors = _apply_package_files(name, pkg, now, ver_ts)
                display_name = _apply_package_db(cursor, name, pkg, user_id, ver_ts)
                conn.commit()

                # 案内: 実行すべきSQL / pip / 貼り付けるランチャ
                sql_stmts = []
                for t in chk['tables']:
                    if t.get('status') == 'missing' and t.get('create_sql'):
                        sql_stmts.append(t['create_sql'].rstrip(';') + ';')
                    elif t.get('status') == 'diff' and t.get('alter_sql'):
                        sql_stmts.append(t['alter_sql'])
                missing_libs = [l for l in chk['libraries'] if not l.get('installed')]
                pip_cmd = ('pip install --user ' + ' '.join(l['pip_name'] for l in missing_libs)) if missing_libs else None
                launchers = [l for l in (chk.get('launchers') or []) if l.get('found') and l.get('snippet')]

                entry.update({
                    'ok': True,
                    'display_name': display_name,
                    'version_status': chk['version']['status'],
                    'written': written, 'skipped': skipped,
                    'backup': backup_name, 'file_errors': errors,
                    'guidance': {
                        'sql': '\n'.join(sql_stmts) if sql_stmts else None,
                        'pip': pip_cmd,
                        'launchers': launchers,
                        'blueprint': _blueprint_snippet_from_package(pkg)
                    }
                })
                imported += 1
            except Exception as e:
                if conn: conn.rollback()
                logging.error("import_site_apply error (%s): %s", name, e)
                entry.update({'ok': False, 'error': str(e)})
                failed += 1
            results.append(entry)

        return jsonify({'success': True,
                        'message': f'{imported}件取込' + (f', {failed}件失敗' if failed else ''),
                        'imported': imported, 'failed': failed, 'results': results})
    except Exception as e:
        logging.error("import_site_apply fatal: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'cursor' in locals(): cursor.close()
        if conn and conn.is_connected(): conn.close()


@app_share_bp.route('/import_core_schema', methods=['POST'])
@login_required
def import_core_schema():
    """全アプリ情報JSONの core_schemas（必須テーブルDDL）を受け入れ側で宣言する。
    各テーブルを CREATE TABLE IF NOT EXISTS で作成（既存は変更しない）。
    database suffix に応じて <owner>$<suffix> のDBに作成する。"""
    if not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403
    data = request.json or {}
    schemas = data.get('schemas', [])
    if not isinstance(schemas, list) or not schemas:
        return jsonify({'success': False, 'error': 'schemas が空です'}), 400

    base, prefix = _gc_prefix_and_base()
    default_db = base.get('database')

    def target_db(suffix):
        if prefix and suffix:
            return f'{prefix}${suffix}'
        return default_db

    # DBごとに接続をまとめる
    conns = {}
    results = []
    created = existed = errors = 0
    try:
        for item in schemas:
            if not isinstance(item, dict):
                continue
            table = (item.get('table') or '').strip()
            ddl = item.get('create_sql') or ''
            suffix = (item.get('database') or 'default').strip()
            if not table or not _TABLE_NAME_RE.match(table):
                errors += 1
                results.append({'table': table, 'status': 'error', 'error': '不正なテーブル名'})
                continue
            if not ddl.strip():
                errors += 1
                results.append({'table': table, 'status': 'error', 'error': 'DDLが空'})
                continue
            dbname = target_db(suffix)
            # 接続確保
            if dbname not in conns:
                d = dict(base)
                d['database'] = dbname
                try:
                    c = mysql.connector.connect(**d)
                    cu = c.cursor()
                    try:
                        cu.execute("SET FOREIGN_KEY_CHECKS=0")
                    except Exception:
                        pass
                    conns[dbname] = (c, cu)
                except Exception as e:
                    errors += 1
                    results.append({'table': table, 'database': dbname,
                                    'status': 'error', 'error': '接続失敗: ' + str(e)})
                    continue
            c, cu = conns[dbname]
            try:
                cu.execute("SHOW TABLES LIKE %s", (table,))
                already = cu.fetchone() is not None
                # CREATE TABLE → CREATE TABLE IF NOT EXISTS（冪等化）
                ddl2 = re.sub(r'^\s*CREATE\s+TABLE\s+', 'CREATE TABLE IF NOT EXISTS ',
                              ddl, count=1, flags=re.IGNORECASE)
                cu.execute(ddl2)
                if already:
                    existed += 1
                    results.append({'table': table, 'database': dbname, 'status': 'exists'})
                else:
                    created += 1
                    results.append({'table': table, 'database': dbname, 'status': 'created'})
            except Exception as e:
                errors += 1
                results.append({'table': table, 'database': dbname,
                                'status': 'error', 'error': str(e)})
        # commit + FKチェック戻す
        for dbname, (c, cu) in conns.items():
            try:
                cu.execute("SET FOREIGN_KEY_CHECKS=1")
                c.commit()
            except Exception:
                pass
        return jsonify({'success': True,
                        'message': f'{created}件作成, {existed}件既存' + (f', {errors}件エラー' if errors else ''),
                        'created': created, 'existed': existed, 'errors': errors,
                        'results': results})
    except Exception as e:
        logging.error("import_core_schema fatal: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        for dbname, (c, cu) in conns.items():
            try:
                cu.close()
                c.close()
            except Exception:
                pass


# ============================================
# ガーベージコレクション（admin専用・管理タブ）※破壊的操作
#
#   /home/<owner>/fujinp（BASE_DIR）配下のディレクトリのうち、アプシャの
#   レジストリに載っていないアプリ（Blueprintパッケージ）を削除し、
#   <owner>$default / <owner>$fujinp データベースのテーブルのうち、
#   「残すコード（削除対象アプリを除いた全 .py/.sql）」から一切参照されない
#   ものを削除する。
#
#   POST /app_share/gc_preview   … 削除候補を算出（トークン発行、削除しない）
#   POST /app_share/gc_execute   … token + confirm='DELETE' で実際に削除
#
#   安全策:
#     - __init__.py を持つ「アプリ」ディレクトリのみ削除対象（一般ディレクトリ除外）
#     - 保護ディレクトリ（app_share 本体・templates・static・各バックアップ等）
#     - 保護テーブル（users・app_share_registry・app_share_documents）
#     - プレビュー結果をトークンで保存し、実行時に再計算して一致確認（変化時は中断）
# ============================================

# テーブル参照スキャンの対象コードルートは SITE_CODE_ROOT（ファイル先頭で定義）を使う。
# アプリ本体（fujinp/）だけでなく、その上位（ホーム）にある auth / admin / guest /
# profile / apps 等や app.py も参照対象に含める必要があるため。

GC_PROTECTED_DIRS = {
    '__pycache__', 'static', 'templates', 'app_share',
    'app_share_backups', 'app_share_import_backups', 'app_share_import_staging',
}
GC_PROTECTED_TABLES = {'users', 'app_share_registry', 'app_share_documents'}
# 参照スキャン・進捗対象から常に除外（バックアップ等はアプリの複製を含むため
# テーブル参照判定を汚染する。ここを除外しないと削除候補が誤って残る）
GC_SCAN_EXCLUDE_DIRS = {
    '__pycache__', 'app_share_backups', 'app_share_import_backups', 'app_share_import_staging',
    'import_staging', 'import_backups', 'node_modules',
}


def _gc_target_databases():
    """(base_kwargs, [db名...]) を返す。<owner>$default と <owner>$fujinp を対象。"""
    base = DatabaseConfig.default()
    default_db = base.get('database')
    names = []
    if default_db:
        names.append(default_db)
        if '$' in default_db:
            prefix = default_db.rsplit('$', 1)[0]
            for suf in ('default', 'fujinp'):
                cand = f'{prefix}${suf}'
                if cand not in names:
                    names.append(cand)
    out, seen = [], set()
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return base, out


def _gc_walk_texts(abs_root, garbage_abs=frozenset()):
    """abs_root 配下の .py/.sql テキストを列挙。除外: 隠しディレクトリ・
    __pycache__・バックアップ/ステージング・node_modules・削除対象アプリ。"""
    texts = []
    if not os.path.isdir(abs_root):
        return texts
    for root, dirs, files in os.walk(abs_root):
        pruned = []
        for d in dirs:
            full = os.path.join(root, d)
            if d.startswith('.') or d in GC_SCAN_EXCLUDE_DIRS or full in garbage_abs:
                continue
            pruned.append(d)
        dirs[:] = pruned
        for fn in files:
            if fn.endswith(('.py', '.sql')):
                try:
                    with open(os.path.join(root, fn), 'r', encoding='utf-8', errors='ignore') as f:
                        texts.append(f.read())
                except Exception:
                    pass
    return texts


def _gc_root_texts_at(abs_root):
    """abs_root 直下（サブディレクトリを除く）の .py/.sql テキストを返す。"""
    texts = []
    try:
        for fn in os.listdir(abs_root):
            p = os.path.join(abs_root, fn)
            if os.path.isfile(p) and fn.endswith(('.py', '.sql')):
                try:
                    with open(p, 'r', encoding='utf-8', errors='ignore') as f:
                        texts.append(f.read())
                except Exception:
                    pass
    except Exception:
        pass
    return texts


def _gc_collect_kept_code(garbage_names):
    """サイトのコードルート（SITE_CODE_ROOT＝ホーム）配下の全 .py/.sql を連結して返す。
    削除対象アプリ(garbage_names, BASE_DIR配下)とバックアップ・隠しディレクトリ等は除外。
    ここに出現するテーブル名は「参照あり（残す）」とみなす。
    ※ fujinp/ の外にある auth/admin/guest/profile/apps や app.py も対象に含める。"""
    garbage_abs = {os.path.join(BASE_DIR, g) for g in garbage_names}
    return '\n'.join(_gc_walk_texts(SITE_CODE_ROOT, garbage_abs))


def _collect_core_text():
    """共通（アプリ非依存）コードのテキストを連結して返す。
    対象: ホーム直下ファイル（admin.py/auth.py/app.py 等）＋ fujinp 直下ファイル＋
    fujinp 以外のホーム直下ディレクトリ（auth/admin/guest/profile/apps 等）。
    fujinp 配下の各アプリは per-app パッケージが持つため、ここには含めない。"""
    texts = _gc_root_texts_at(SITE_CODE_ROOT)
    if SITE_CODE_ROOT != BASE_DIR:
        texts += _gc_root_texts_at(BASE_DIR)
        base_name = os.path.basename(BASE_DIR)
        for item in sorted(os.listdir(SITE_CODE_ROOT)):
            p = os.path.join(SITE_CODE_ROOT, item)
            if not os.path.isdir(p):
                continue
            if item == base_name or item.startswith('.') or item in GC_SCAN_EXCLUDE_DIRS:
                continue
            texts += _gc_walk_texts(p)
    return '\n'.join(texts)


def _collect_core_schemas():
    """共通コードが参照する「必須テーブル」のDDLを両DBから収集。
    戻り値: [{'database': suffix, 'table': name, 'create_sql': ddl}, ...]
    （database suffix は 'default' / 'fujinp' 等。受け入れ側でこの suffix の DB に作成する）"""
    core_text = _collect_core_text()
    base, db_names = _gc_target_databases()
    out = []
    for dbname in db_names:
        suffix = dbname.rsplit('$', 1)[1] if '$' in dbname else dbname
        d = dict(base)
        d['database'] = dbname
        try:
            c = mysql.connector.connect(**d)
            cu = c.cursor()
            cu.execute("SHOW TABLES")
            tbls = [row[0] for row in cu.fetchall()]
            for t in tbls:
                if not re.search(r'\b' + re.escape(t) + r'\b', core_text):
                    continue
                try:
                    cu.execute(f"SHOW CREATE TABLE `{t}`")
                    row = cu.fetchone()
                    ddl = row[1] if row and len(row) > 1 else ''
                    out.append({'database': suffix, 'table': t, 'create_sql': ddl})
                except Exception as e:
                    out.append({'database': suffix, 'table': t, 'error': str(e)})
            cu.close()
            c.close()
        except Exception as e:
            logging.warning("core schema scan skip %s: %s", dbname, e)
    return out


def _gc_prefix_and_base():
    """(db接続kwargs, '<owner>' プレフィックス or None) を返す。"""
    base = DatabaseConfig.default()
    default_db = base.get('database') or ''
    prefix = default_db.rsplit('$', 1)[0] if '$' in default_db else None
    return base, prefix


def _gc_read_dir_files(dirname, exts=('.py', '.sql')):
    """BASE_DIR/<dirname> 配下の対象拡張子ファイルを (相対パス, テキスト) で返す。
    作業用ディレクトリ（import_backups / import_staging 等）と隠しディレクトリは除外
    （バックアップ内の他アプリ複製を解析・参照判定に混入させないため）。"""
    out = []
    base = os.path.join(BASE_DIR, dirname)
    if not os.path.isdir(base):
        return out
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs
                   if not d.startswith('.') and d not in GC_SCAN_EXCLUDE_DIRS]
        for fn in files:
            if fn.endswith(exts):
                rel = os.path.relpath(os.path.join(root, fn), base).replace(os.sep, '/')
                try:
                    with open(os.path.join(root, fn), 'r', encoding='utf-8', errors='ignore') as f:
                        out.append((rel, f.read()))
                except Exception:
                    pass
    return out


def _gc_read_root_files(exts=('.py', '.sql')):
    """BASE_DIR 直下（サブディレクトリを除く）の対象ファイルテキストを返す。"""
    texts = []
    try:
        for fn in os.listdir(BASE_DIR):
            p = os.path.join(BASE_DIR, fn)
            if os.path.isfile(p) and fn.endswith(exts):
                try:
                    with open(p, 'r', encoding='utf-8', errors='ignore') as f:
                        texts.append(f.read())
                except Exception:
                    pass
    except Exception:
        pass
    return texts


def _gc_classify_dirs():
    """BASE_DIR直下のディレクトリを分類。
    戻り値: (registered_ordered, garbage_dirs, survivor_app_dirs, non_app, protected_kept)"""
    conn = mysql.connector.connect(**DatabaseConfig.default())
    cur = conn.cursor(dictionary=True, buffered=True)
    cur.execute("SELECT app_name FROM app_share_registry ORDER BY sort_order ASC, id ASC")
    registered_ordered = [r['app_name'] for r in cur.fetchall()]
    cur.close()
    conn.close()
    registered = set(registered_ordered)

    garbage_dirs, survivor_app_dirs, non_app, protected_kept = [], [], [], []
    for item in sorted(os.listdir(BASE_DIR)):
        p = os.path.join(BASE_DIR, item)
        if not os.path.isdir(p):
            continue
        is_app = os.path.exists(os.path.join(p, '__init__.py'))
        if item in GC_PROTECTED_DIRS:
            protected_kept.append(item)
            if is_app and item not in survivor_app_dirs:
                survivor_app_dirs.append(item)  # 例: app_share 本体
            continue
        if not is_app:
            non_app.append(item)
            continue
        if item in registered:
            survivor_app_dirs.append(item)
        else:
            garbage_dirs.append(item)
    return registered_ordered, garbage_dirs, survivor_app_dirs, non_app, protected_kept


def _gc_ordered_survivors(survivor_app_dirs, registered_ordered):
    """生き残るアプリを「登録簿の並び順→その他は名前順」で並べる。"""
    surv = set(survivor_app_dirs)
    ordered = [a for a in registered_ordered if a in surv]
    extras = sorted(surv - set(ordered))
    return ordered + extras


def _gc_blueprint_pairs(dir_names):
    """[(app名, analyses), ...] を返す（各ディレクトリの .py を解析）。"""
    pairs = []
    for name in dir_names:
        pairs.append((name, _analyze_blueprints(name, _gc_read_dir_files(name, ('.py',)))))
    return pairs


def _gc_compute_plan(compute_bp=True):
    """削除候補を算出（同期版）。戻り値 (plan, details)。
    plan = {'dirs': [name...], 'tables': {db: [name...]}}（実行はこの plan に従う）。
    compute_bp=False の場合は Blueprint スニペット生成を省略（実行時の一致確認用に高速化）。"""
    (registered_ordered, garbage_dirs, survivor_app_dirs,
     non_app, protected_kept) = _gc_classify_dirs()
    garbage_set = set(garbage_dirs)
    kept_code = _gc_collect_kept_code(garbage_set)

    blueprint_keep = blueprint_unregister = None
    if compute_bp:
        ordered_surv = _gc_ordered_survivors(survivor_app_dirs, registered_ordered)
        blueprint_keep = _blueprint_snippet(_gc_blueprint_pairs(ordered_surv))
        blueprint_unregister = _blueprint_snippet(_gc_blueprint_pairs(sorted(garbage_dirs)))

    # --- テーブル判定（DBごと） ---
    base, db_names = _gc_target_databases()
    tables_plan = {}
    db_details = []
    for dbname in db_names:
        d = dict(base)
        d['database'] = dbname
        entry = {'database': dbname}
        try:
            c = mysql.connector.connect(**d)
            cu = c.cursor()
            cu.execute("SHOW TABLES")
            tbls = [row[0] for row in cu.fetchall()]
            cu.close()
            c.close()
        except Exception as e:
            entry['error'] = str(e)
            db_details.append(entry)
            continue
        drop, keep = [], []
        for t in tbls:
            if t in GC_PROTECTED_TABLES:
                keep.append(t)
            elif re.search(r'\b' + re.escape(t) + r'\b', kept_code):
                keep.append(t)
            else:
                drop.append(t)
        tables_plan[dbname] = sorted(drop)
        entry['tables_to_drop'] = sorted(drop)
        entry['tables_kept'] = sorted(keep)
        db_details.append(entry)

    plan = {'dirs': sorted(garbage_dirs), 'tables': tables_plan}
    details = {
        'base_dir': BASE_DIR,
        'registered_apps': sorted(registered_ordered),
        'kept_dirs': sorted(set(survivor_app_dirs) | set(protected_kept)),
        'non_app_dirs_skipped': sorted(non_app),
        'protected_tables': sorted(GC_PROTECTED_TABLES),
        'databases': db_details,
        'blueprint_keep': blueprint_keep,
        'blueprint_unregister': blueprint_unregister,
        'match_note': ('テーブルは「残すコード中に名前が出現するか」で判定（コメント内の言及も'
                       '参照とみなす保守的方式）。削除対象アプリ専用のテーブルは削除されます。'),
    }
    return plan, details


def _gc_plan_key(plan):
    """plan を正規化して比較用のキー（tuple）に変換"""
    dirs = tuple(sorted(plan.get('dirs', [])))
    tables = tuple(sorted((db, tuple(sorted(v)))
                          for db, v in (plan.get('tables', {}) or {}).items()))
    return (dirs, tables)


def _gcp_path(token):
    return os.path.join(IMPORT_STAGING_DIR, 'gcp_' + token + '.json')


def _gcp_load(token):
    with open(_gcp_path(token), 'r', encoding='utf-8') as f:
        return json.load(f)


def _gcp_save(token, state):
    with open(_gcp_path(token), 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False)


@app_share_bp.route('/gc_preview_init', methods=['POST'])
@login_required
def gc_preview_init():
    """進捗つきプレビューの初期化。ディレクトリ分類とテーブル一覧を取得し、
    以降 1件ずつ処理する作業リストとトークンを返す。"""
    if not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403
    try:
        (registered_ordered, garbage_dirs, survivor_app_dirs,
         non_app, protected_kept) = _gc_classify_dirs()
        garbage_set = set(garbage_dirs)
        survivor_app_set = set(survivor_app_dirs)

        # DBごとのテーブル一覧
        base, db_names = _gc_target_databases()
        db_tables, db_errors = {}, {}
        candidates = set()
        for dbname in db_names:
            d = dict(base)
            d['database'] = dbname
            try:
                c = mysql.connector.connect(**d)
                cu = c.cursor()
                cu.execute("SHOW TABLES")
                tbls = [row[0] for row in cu.fetchall()]
                cu.close()
                c.close()
                db_tables[dbname] = tbls
                for t in tbls:
                    if t not in GC_PROTECTED_TABLES:
                        candidates.add(t)
            except Exception as e:
                db_errors[dbname] = str(e)

        # 作業アイテム:
        #  1) ルート直下ファイル（ホーム直下＋fujinp直下の app.py 等）
        #  2) fujinp の外（ホーム直下）の各ディレクトリ = auth/admin/guest/profile/apps 等 → 参照のみ
        #  3) fujinp 配下の各ディレクトリ → 削除判定＋Blueprint解析
        items = [{'kind': 'root', 'label': '(共通コード直下のファイル)'}]
        base_name = os.path.basename(BASE_DIR)
        if SITE_CODE_ROOT != BASE_DIR:
            for item in sorted(os.listdir(SITE_CODE_ROOT)):
                p = os.path.join(SITE_CODE_ROOT, item)
                if not os.path.isdir(p):
                    continue
                if item == base_name or item.startswith('.') or item in GC_SCAN_EXCLUDE_DIRS:
                    continue
                items.append({'kind': 'homedir', 'name': item, 'label': item})
        for item in sorted(os.listdir(BASE_DIR)):
            p = os.path.join(BASE_DIR, item)
            if not os.path.isdir(p) or item in GC_SCAN_EXCLUDE_DIRS:
                continue
            items.append({'kind': 'basedir', 'name': item,
                          'garbage': item in garbage_set,
                          'is_app': item in survivor_app_set or item in garbage_set
                                    or os.path.exists(os.path.join(p, '__init__.py')),
                          'label': base_name + '/' + item})

        os.makedirs(IMPORT_STAGING_DIR, exist_ok=True)
        _cleanup_staging()
        token = secrets.token_hex(16)
        state = {
            'registered_ordered': registered_ordered,
            'garbage_dirs': sorted(garbage_dirs),
            'survivor_app_dirs': survivor_app_dirs,
            'non_app_dirs': sorted(non_app),
            'protected_kept': sorted(protected_kept),
            'db_tables': db_tables, 'db_errors': db_errors,
            'candidates': sorted(candidates),
            'items': items,
            'referenced': [], 'keep_bp': [], 'remove_bp': [],
            'processed': 0,
        }
        _gcp_save(token, state)
        return jsonify({'success': True, 'token': token, 'total': len(items),
                        'garbage_count': len(garbage_dirs)})
    except Exception as e:
        logging.error("gc_preview_init error: %s", e)
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app_share_bp.route('/gc_preview_step', methods=['POST'])
@login_required
def gc_preview_step():
    """作業アイテムを1件処理し、進捗を返す。"""
    if not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403
    data = request.json or {}
    token = data.get('token', '')
    index = data.get('index')
    if not re.match(r'^[a-f0-9]{32}$', token or '') or not os.path.exists(_gcp_path(token)):
        return jsonify({'success': False, 'error': 'セッションが見つかりません（再度プレビュー）'}), 404
    try:
        state = _gcp_load(token)
        items = state['items']
        if not isinstance(index, int) or index < 0 or index >= len(items):
            return jsonify({'success': False, 'error': 'index 範囲外'}), 400
        it = items[index]
        referenced = set(state['referenced'])
        remaining = [c for c in state['candidates'] if c not in referenced]

        def scan_refs(texts):
            joined = '\n'.join(texts)
            for c in remaining:
                if re.search(r'\b' + re.escape(c) + r'\b', joined):
                    referenced.add(c)

        kind = it['kind']
        if kind == 'root':
            # ホーム直下＋fujinp直下の app.py 等
            texts = _gc_root_texts_at(SITE_CODE_ROOT)
            if SITE_CODE_ROOT != BASE_DIR:
                texts += _gc_root_texts_at(BASE_DIR)
            scan_refs(texts)
        elif kind == 'homedir':
            # fujinp の外（auth/admin/guest/profile/apps 等）: 参照のみ
            scan_refs(_gc_walk_texts(os.path.join(SITE_CODE_ROOT, it['name'])))
        else:  # basedir（fujinp配下）
            name = it['name']
            if it.get('garbage'):
                # 削除対象: 参照には数えない。remove-bp のみ解析
                files = _gc_read_dir_files(name, ('.py',))
                state['remove_bp'].append({'app': name,
                                           'an': _analyze_blueprints(name, files)})
            else:
                files = _gc_read_dir_files(name, ('.py', '.sql'))
                scan_refs([t for _, t in files])
                if it.get('is_app'):
                    py = [(r, t) for r, t in files if r.endswith('.py')]
                    state['keep_bp'].append({'app': name,
                                             'an': _analyze_blueprints(name, py)})
        state['referenced'] = sorted(referenced)
        state['processed'] = max(state['processed'], index + 1)
        _gcp_save(token, state)
        return jsonify({'success': True, 'processed': index + 1,
                        'total': len(items), 'label': it.get('label', '')})
    except Exception as e:
        logging.error("gc_preview_step error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@app_share_bp.route('/gc_preview_finish', methods=['POST'])
@login_required
def gc_preview_finish():
    """全ステップ完了後、プレビュー結果（削除計画・保持Blueprint等）を組み立てて返す。"""
    if not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403
    data = request.json or {}
    token = data.get('token', '')
    if not re.match(r'^[a-f0-9]{32}$', token or '') or not os.path.exists(_gcp_path(token)):
        return jsonify({'success': False, 'error': 'セッションが見つかりません（再度プレビュー）'}), 404
    try:
        state = _gcp_load(token)
        referenced = set(state['referenced'])

        db_details, tables_plan = [], {}
        for dbname, tbls in state['db_tables'].items():
            drop, keep = [], []
            for t in tbls:
                if t in GC_PROTECTED_TABLES or t in referenced:
                    keep.append(t)
                else:
                    drop.append(t)
            tables_plan[dbname] = sorted(drop)
            db_details.append({'database': dbname, 'tables_to_drop': sorted(drop),
                               'tables_kept': sorted(keep)})
        for dbname, err in state.get('db_errors', {}).items():
            db_details.append({'database': dbname, 'error': err})

        # Blueprint スニペット（残す＝保持アプリ / 外す＝削除アプリ）
        surv_order = _gc_ordered_survivors(state['survivor_app_dirs'], state['registered_ordered'])
        keep_map = {e['app']: e['an'] for e in state['keep_bp']}
        keep_pairs = [(a, keep_map.get(a, [])) for a in surv_order]
        blueprint_keep = _blueprint_snippet(keep_pairs)
        remove_pairs = [(e['app'], e['an']) for e in sorted(state['remove_bp'], key=lambda x: x['app'])]
        blueprint_unregister = _blueprint_snippet(remove_pairs)

        plan = {'dirs': sorted(state['garbage_dirs']), 'tables': tables_plan}
        # 実行用に確定プランを保存（gc_execute が読む）
        with open(os.path.join(IMPORT_STAGING_DIR, 'gc_' + token + '.json'),
                  'w', encoding='utf-8') as f:
            json.dump(plan, f, ensure_ascii=False)
        try:
            os.remove(_gcp_path(token))
        except Exception:
            pass

        details = {
            'base_dir': BASE_DIR,
            'registered_apps': sorted(state['registered_ordered']),
            'kept_dirs': sorted(set(state['survivor_app_dirs']) | set(state['protected_kept'])),
            'non_app_dirs_skipped': state.get('non_app_dirs', []),
            'protected_tables': sorted(GC_PROTECTED_TABLES),
            'databases': db_details,
            'blueprint_keep': blueprint_keep,
            'blueprint_unregister': blueprint_unregister,
            'match_note': ('テーブルは「残すコード中に名前が出現するか」で判定（コメント内の言及も'
                           '参照とみなす保守的方式）。削除対象アプリ専用のテーブルは削除されます。'),
        }
        table_count = sum(len(v) for v in tables_plan.values())
        return jsonify({'success': True, 'token': token, 'plan': plan, 'details': details,
                        'dir_count': len(plan['dirs']), 'table_count': table_count})
    except Exception as e:
        logging.error("gc_preview_finish error: %s", e)
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app_share_bp.route('/gc_execute', methods=['POST'])
@login_required
def gc_execute():
    """プレビュー済みトークン＋confirm='DELETE' で実際に削除し、結果レポートを返す"""
    if not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403
    data = request.json or {}
    token = data.get('token', '')
    confirm = data.get('confirm', '')
    if confirm != 'DELETE':
        return jsonify({'success': False, 'error': '確認文字列が一致しません（DELETE と入力してください）'}), 400
    if not re.match(r'^[a-f0-9]{32}$', token or ''):
        return jsonify({'success': False, 'error': '不正なトークン'}), 400
    ppath = os.path.join(IMPORT_STAGING_DIR, 'gc_' + token + '.json')
    if not os.path.exists(ppath):
        return jsonify({'success': False, 'error': 'プレビュー情報が見つかりません（期限切れ）。もう一度プレビューしてください'}), 404
    try:
        with open(ppath, 'r', encoding='utf-8') as f:
            stored = json.load(f)
        # 実行直前に再計算し、プレビューと一致するか確認（変化していたら中断）
        # 一致確認は高速化のため Blueprint 生成を省略
        fresh, _fd = _gc_compute_plan(compute_bp=False)
        if _gc_plan_key(stored) != _gc_plan_key(fresh):
            return jsonify({'success': False, 'changed': True,
                            'error': '前回プレビューから状況が変化しました。もう一度プレビューしてください'}), 409

        # レポート用 Blueprint スニペット（削除前に収集）
        (reg_ordered, garbage_now, survivor_now, _na, _pk) = _gc_classify_dirs()
        surv_order = _gc_ordered_survivors(survivor_now, reg_ordered)
        blueprint_keep = _blueprint_snippet(_gc_blueprint_pairs(surv_order))
        blueprint_unregister = _blueprint_snippet(_gc_blueprint_pairs(sorted(garbage_now)))

        # --- ディレクトリ削除 ---
        deleted_dirs, dir_errors = [], []
        base_abs = os.path.abspath(BASE_DIR)
        for name in fresh['dirs']:
            if name in GC_PROTECTED_DIRS:
                dir_errors.append({'name': name, 'error': '保護対象のためスキップ'})
                continue
            p = os.path.join(BASE_DIR, name)
            # 安全確認: BASE_DIR直下・実ディレクトリ・シンボリックリンクでない
            if (os.path.dirname(os.path.abspath(p)) != base_abs
                    or not os.path.isdir(p) or os.path.islink(p)):
                dir_errors.append({'name': name, 'error': '不正なパスのためスキップ'})
                continue
            try:
                shutil.rmtree(p)
                deleted_dirs.append(name)
            except Exception as e:
                dir_errors.append({'name': name, 'error': str(e)})

        # --- テーブル削除 ---
        dropped_tables, table_errors = [], []
        base, _dbs = _gc_target_databases()
        for dbname, tbls in fresh['tables'].items():
            if not tbls:
                continue
            d = dict(base)
            d['database'] = dbname
            try:
                c = mysql.connector.connect(**d)
                cu = c.cursor()
            except Exception as e:
                table_errors.append({'database': dbname, 'error': '接続失敗: ' + str(e)})
                continue
            try:
                cu.execute("SET FOREIGN_KEY_CHECKS=0")
            except Exception:
                pass
            for t in tbls:
                if t in GC_PROTECTED_TABLES or not _TABLE_NAME_RE.match(t):
                    table_errors.append({'database': dbname, 'table': t, 'error': '保護/不正名のためスキップ'})
                    continue
                try:
                    cu.execute(f"DROP TABLE IF EXISTS `{t}`")
                    dropped_tables.append({'database': dbname, 'table': t})
                except Exception as e:
                    table_errors.append({'database': dbname, 'table': t, 'error': str(e)})
            try:
                cu.execute("SET FOREIGN_KEY_CHECKS=1")
                c.commit()
            except Exception:
                pass
            cu.close()
            c.close()

        try:
            os.remove(ppath)
        except Exception:
            pass

        return jsonify({
            'success': True,
            'base_dir': BASE_DIR,
            'generated_at': _now_jst().strftime('%Y-%m-%d %H:%M:%S'),
            'deleted_dirs': deleted_dirs,
            'dir_errors': dir_errors,
            'dropped_tables': dropped_tables,
            'table_errors': table_errors,
            'deleted_dir_count': len(deleted_dirs),
            'dropped_table_count': len(dropped_tables),
            'blueprint_keep': blueprint_keep,
            'blueprint_unregister': blueprint_unregister,
        })
    except Exception as e:
        logging.error("gc_execute error: %s", e)
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================
# アプリ管理（admin専用）: テーブル確認・診断・管理ノート
#
#   POST /import_table_check            期待DDLと実テーブルの照合（実行後確認ボタン用）
#   GET  /app_diagnosis/<app_name>      取り込み（インストール）状態の診断
#   POST /app_note_append/<app_name>    管理ノート（doc_type='note'）へ日付見出しつきで追記
# ============================================

@app_share_bp.route('/import_table_check', methods=['POST'])
@login_required
def import_table_check():
    """期待DDL(create_sql)と受け入れ側の実テーブルを照合する（実行後確認）。
    resolved=True なら完了（追加カラムなしで存在。定義差のみは完了扱い）。"""
    if not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403
    data = request.json or {}
    table = (data.get('table') or '').strip()
    create_sql = data.get('create_sql') or ''
    db_hint = data.get('database_hint') or None
    if not table:
        return jsonify({'success': False, 'error': 'table が必要です'}), 400
    try:
        res = _compare_table_any(table, create_sql, db_hint=db_hint)
        res['success'] = True
        return jsonify(res)
    except Exception as e:
        logging.error("import_table_check error (%s): %s", table, e)
        return jsonify({'success': False, 'error': str(e)}), 500


def _extract_create_statements(sql_text):
    """SQLテキストから CREATE TABLE 文を {'table','create_sql'} で列挙"""
    out = []
    if not sql_text:
        return out
    for m in re.finditer(
            r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?([\w$]+)`?',
            sql_text, re.IGNORECASE):
        name = m.group(1)
        end = sql_text.find(';', m.end())
        stmt = sql_text[m.start(): end + 1 if end != -1 else len(sql_text)]
        out.append({'table': name, 'create_sql': stmt.strip()})
    return out


@app_share_bp.route('/app_diagnosis/<app_name>', methods=['GET'])
@login_required
def app_diagnosis(app_name):
    """アプリの取り込み（インストール）状態を診断する（admin専用）。
    ファイル構成・レジストリ・ドキュメント・Blueprint登録・ランチャ・
    SQLテーブル・ライブラリを点検し、未完作業の遂行材料（スニペット等）を返す。"""
    if not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403
    if not re.match(r'^[A-Za-z0-9_]+$', app_name or ''):
        return jsonify({'success': False, 'error': 'アプリ名が不正です'}), 400
    app_path = os.path.join(BASE_DIR, app_name)
    conn = None
    try:
        diag = {'app_name': app_name}

        # --- 1) ファイル構成 ---
        diag['files'] = {
            'dir_exists': os.path.isdir(app_path),
            'has_init': os.path.exists(os.path.join(app_path, '__init__.py')),
            'has_routes': os.path.exists(os.path.join(app_path, 'routes.py')),
            'has_templates': os.path.isdir(os.path.join(app_path, 'templates')),
            'has_manifest': os.path.exists(os.path.join(app_path, 'manifest.json')),
            'has_app_info': os.path.exists(os.path.join(app_path, 'app_info.json')),
        }

        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True, buffered=True)

        # --- 2) レジストリ（カード） ---
        cursor.execute("SELECT * FROM app_share_registry WHERE app_name = %s", (app_name,))
        reg = cursor.fetchone()
        upd = reg.get('updated_at') if reg else None
        diag['registry'] = {
            'registered': bool(reg),
            'display_name': (reg.get('display_name') if reg else None) or app_name,
            'icon': (reg.get('icon') if reg else None) or '📦',
            'updated_at': upd.strftime('%Y-%m-%d %H:%M:%S') if upd else None,
            'version_id': _get_version_id(app_name),
        }

        # --- 3) ドキュメント（manual / spec / note。noteは内容も返す） ---
        cursor.execute("""
            SELECT doc_type, title, content, updated_at
            FROM app_share_documents WHERE app_name = %s
        """, (app_name,))
        docs = {}
        for row in cursor.fetchall():
            docs[row['doc_type']] = {
                'exists': True,
                'title': row['title'] or '',
                'updated_at': row['updated_at'].strftime('%Y-%m-%d %H:%M') if row['updated_at'] else None,
                'content': row['content'] if row['doc_type'] == 'note' else None,
            }
        for t in ('manual', 'spec', 'note'):
            docs.setdefault(t, {'exists': False})
        diag['docs'] = docs

        # --- 4) Blueprint登録（app.py） ---
        analyses = _analyze_blueprints(app_name, _gc_read_dir_files(app_name, ('.py',)))
        snippet = _blueprint_snippet([(app_name, analyses)])
        app_py_text = ''
        for root in (SITE_CODE_ROOT, BASE_DIR):
            p = os.path.join(root, 'app.py')
            if os.path.exists(p):
                try:
                    with open(p, 'r', encoding='utf-8', errors='ignore') as f:
                        app_py_text += f.read() + '\n'
                except Exception:
                    pass
        bp_names = [a['bp'] for a in analyses]
        app_py_live = _strip_comment_lines(app_py_text)   # コメントアウトを除く
        registered_in_app = (any(re.search(r'\b' + re.escape(b) + r'\b', app_py_live)
                                 for b in bp_names) if bp_names else None)

        diag['blueprint'] = {'defined': bool(bp_names), 'bp_names': bp_names,
                             'registered_in_app_py': registered_in_app,
                             'snippet': snippet}

        # --- 5) ランチャ ---
        diag['launchers'] = _extract_launchers(app_name)

        # --- 6) SQLテーブル（app_info.json の mysql_schema ＋ schema.sql から期待DDLを抽出） ---
        sql_text = ''
        info_path = os.path.join(app_path, 'app_info.json')
        if os.path.exists(info_path):
            try:
                with open(info_path, 'r', encoding='utf-8') as f:
                    info = json.load(f)
                fld = (info.get('fields') or {}).get('mysql_schema') or {}
                sql_text += (fld.get('value') or '') + '\n'
            except Exception:
                pass
        schema_path = os.path.join(app_path, 'schema.sql')
        if os.path.exists(schema_path):
            try:
                with open(schema_path, 'r', encoding='utf-8', errors='ignore') as f:
                    sql_text += f.read()
            except Exception:
                pass
        stmts = _extract_create_statements(sql_text)
        tables = []
        seen = set()
        tables_map = _db_tables_map() if stmts else {}
        for s in stmts:
            if s['table'] in seen:
                continue
            seen.add(s['table'])
            tables.append(_compare_table_any(s['table'], s['create_sql'], tables_map))
        diag['tables'] = tables

        # --- 7) ライブラリ ---
        libs = _extract_imports([t for _, t in _gc_read_dir_files(app_name, ('.py',))], app_name)
        lib_list = []
        for lib in libs.get('third_party', []):
            try:
                installed = importlib.util.find_spec(str(lib)) is not None
            except Exception:
                installed = False
            lib_list.append({'name': lib, 'installed': installed,
                             'pip_name': PIP_NAME_MAP.get(lib, lib)})
        diag['libraries'] = lib_list

        diag['generated_at'] = _now_jst().strftime('%Y-%m-%d %H:%M:%S')
        return jsonify({'success': True, 'diagnosis': diag})

    except Exception as e:
        logging.error("app_diagnosis error (%s): %s", app_name, e)
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'cursor' in locals(): cursor.close()
        if conn and conn.is_connected(): conn.close()


@app_share_bp.route('/app_note_append/<app_name>', methods=['POST'])
@login_required
def app_note_append(app_name):
    """管理ノート（doc_type='note'）の末尾に日付見出しつきで追記する（admin専用）"""
    if not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403
    text = ((request.json or {}).get('text') or '').strip()
    if not text:
        return jsonify({'success': False, 'error': '追記テキストが空です'}), 400
    user_id = session.get('user_id')
    now = datetime.datetime.now()
    stamp = now.strftime('%Y-%m-%d %H:%M')
    conn = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True, buffered=True)
        cursor.execute("""
            SELECT id, content FROM app_share_documents
            WHERE app_name = %s AND doc_type = 'note'
        """, (app_name,))
        row = cursor.fetchone()
        if row:
            content = (row['content'] or '').rstrip() + f"\n\n## {stamp}\n{text}\n"
            cursor.execute("""
                UPDATE app_share_documents
                SET content = %s, updated_by = %s, updated_at = %s
                WHERE id = %s
            """, (content, user_id, now, row['id']))
        else:
            content = f"## {stamp}\n{text}\n"
            cursor.execute("""
                INSERT INTO app_share_documents
                    (app_name, doc_type, title, content, updated_by, updated_at)
                VALUES (%s, 'note', %s, %s, %s, %s)
            """, (app_name, '管理ノート', content, user_id, now))
        conn.commit()
        return jsonify({'success': True, 'message': 'ノートに追記しました', 'content': content})
    except Exception as e:
        if conn: conn.rollback()
        logging.error("app_note_append error (%s): %s", app_name, e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'cursor' in locals(): cursor.close()
        if conn and conn.is_connected(): conn.close()