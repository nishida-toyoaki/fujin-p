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

import logging
import mysql.connector
from flask import Blueprint, request, jsonify, render_template, Response
from flask import Response
from auth import login_required
from auth import redirect_to_dashboard
import pytz
import datetime
from pytz import timezone
# from db import base_db_config, default_db_config
from config import Config
from db import DatabaseConfig, Tables
from markdown_converter import process_markdown_for_preview
import markdown
import re


# 定数定義
# COLREP_PROJECTS_TABLE = "nishida4fujinp$fujinp.colrep_projects"
# TARGET_DATABASE = "nishida4fujinp$fujinp"
COLREP_PROJECTS_TABLE = Tables.COLREP_PROJECTS
TARGET_DATABASE = Tables.DB_FUJINP

# タイムゾーン設定
JST = timezone('Asia/Tokyo')

def get_jst_now():
    """現在の日時をJSTで取得（naive datetime）"""
    return datetime.datetime.now(JST).replace(tzinfo=None)

def serialize_for_json(obj):
    """datetime およびその他のオブジェクトを JSON シリアライズ可能な形式に変換"""
    from jinja2 import Undefined

    if isinstance(obj, Undefined):
        return None
    elif isinstance(obj, dict):
        return {k: serialize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [serialize_for_json(item) for item in obj]
    elif isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.strftime('%Y-%m-%d %H:%M:%S')
    elif isinstance(obj, datetime.time):
        return obj.strftime('%H:%M:%S')
    elif obj is None:
        return None
    return obj

colrep_public_bp = Blueprint('colrep_public', __name__,
                            url_prefix='/colrep_public',
                            template_folder='../templates')

logging.basicConfig(level=logging.DEBUG)


@colrep_public_bp.route('/')
def public_projects_list():
    """公開プロジェクト一覧ページ"""
    return render_template('colrep_public_list.html')

@colrep_public_bp.route('/api/projects')
def get_public_projects():
    """公開プロジェクト一覧取得API"""
    try:
        # conn = mysql.connector.connect(**base_db_config, database=TARGET_DATABASE)
        conn = mysql.connector.connect(**DatabaseConfig.fujinp())
        cursor = conn.cursor(dictionary=True)

        # 公開プロジェクトのみ取得
        cursor.execute(f"""
            SELECT cp.id, cp.プロジェクト名, cp.更新日時, cp.責任者,
                   u.full_name as 責任者名, u.full_name as 責任者氏名
            FROM {COLREP_PROJECTS_TABLE} cp
            LEFT JOIN {Tables.USERS} u ON cp.責任者 = u.id
            WHERE cp.is_public = TRUE
            ORDER BY cp.更新日時 DESC
        """)

        projects = cursor.fetchall()

        return jsonify({
            'success': True,
            'projects': serialize_for_json(projects)
        })

    except Exception as e:
        logging.error(f"Error in get_public_projects: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()

@colrep_public_bp.route('/<int:project_id>/detail')
def project_detail(project_id):
    """プロジェクト詳細ページ（Composer + 部品一覧）"""
    try:
        # conn = mysql.connector.connect(**base_db_config, database=TARGET_DATABASE)
        conn = mysql.connector.connect(**DatabaseConfig.fujinp())
        cursor = conn.cursor(dictionary=True)

        # プロジェクト情報を取得（公開プロジェクトのみ）
        cursor.execute(f"""
            SELECT cp.*, u.full_name as 責任者名, u.full_name as 責任者氏名
            FROM {COLREP_PROJECTS_TABLE} cp
            LEFT JOIN {Tables.USERS} u ON cp.責任者 = u.id
            WHERE cp.id = %s AND cp.is_public = TRUE
        """, (project_id,))

        project = cursor.fetchone()

        if not project:
            return render_template('error.html', error='公開プロジェクトが見つかりません。'), 404

        # テーブル情報を取得
        table_name = project['テーブル名']
        cursor.execute(f"SHOW TABLES LIKE '{table_name}'")
        table_exists = cursor.fetchone() is not None

        components = []
        if table_exists:
            # 部品（タスク）一覧を取得
            cursor.execute(f"""
                SELECT t.id, t.カラム名, t.説明, t.status, t.更新日時, t.担当者アカウント,
                       u.full_name, u.full_name
                FROM `{table_name}` t
                LEFT JOIN {Tables.USERS} u ON t.担当者アカウント = u.id
                ORDER BY t.id ASC
            """)
            components = cursor.fetchall()

        return render_template('colrep_public_project_detail.html',
                              project=project,
                              table_exists=table_exists,
                              components=components)

    except Exception as e:
        logging.error(f"Error in project_detail: {str(e)}")
        return render_template('error.html', error=str(e)), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()

@colrep_public_bp.route('/<int:project_id>/api/composer')
def get_composer_info(project_id):
    """Composer情報取得API"""
    try:
        # conn = mysql.connector.connect(**base_db_config, database=TARGET_DATABASE)
        conn = mysql.connector.connect(**DatabaseConfig.fujinp())
        cursor = conn.cursor(dictionary=True)

        cursor.execute(f"""
            SELECT プロジェクト名, Composer
            FROM {COLREP_PROJECTS_TABLE}
            WHERE id = %s AND is_public = TRUE
        """, (project_id,))

        project = cursor.fetchone()
        if not project:
            return jsonify({'success': False, 'error': 'プロジェクトが見つかりません。'}), 404

        # ✅ 修正：markdown.markdown() を使用
        html_content = ""
        if project['Composer']:
            try:
                html_content = markdown.markdown(project['Composer'], extensions=['extra', 'nl2br', 'sane_lists', 'fenced_code'])
            except Exception as e:
                logging.warning(f"Markdown conversion failed: {e}")
                import html
                html_content = f"<pre>{html.escape(project['Composer'])}</pre>"
        else:
            html_content = "<p class='text-muted'>Composerがまだ作成されていません</p>"

        return jsonify({
            'success': True,
            'project_name': project['プロジェクト名'],
            'composer_md': project['Composer'] or '',
            'composer_html': html_content
        })

    except Exception as e:
        logging.error(f"Error in get_composer_info: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@colrep_public_bp.route('/<int:project_id>/api/components')
def get_components(project_id):
    """部品一覧取得API（JSON）"""
    try:
        # conn = mysql.connector.connect(**base_db_config, database=TARGET_DATABASE)
        conn = mysql.connector.connect(**DatabaseConfig.fujinp())
        cursor = conn.cursor(dictionary=True)

        # プロジェクト情報を取得（公開プロジェクトのみ）
        cursor.execute(f"""
            SELECT テーブル名
            FROM {COLREP_PROJECTS_TABLE}
            WHERE id = %s AND is_public = TRUE
        """, (project_id,))

        project = cursor.fetchone()

        if not project:
            return jsonify({'success': False, 'error': 'プロジェクトが見つかりません。'}), 404

        table_name = project['テーブル名']

        # テーブルが存在するかチェック
        cursor.execute(f"SHOW TABLES LIKE '{table_name}'")
        if not cursor.fetchone():
            return jsonify({
                'success': True,
                'components': [],
                'message': 'テーブルがまだ作成されていません'
            })

        # ✅ 修正：テーブル名を正しく修正
        cursor.execute(f"""
            SELECT t.id, t.カラム名, t.説明, t.status, t.更新日時, t.担当者アカウント,
                   t.content,
                   u.full_name
            FROM `{table_name}` t
            LEFT JOIN {Tables.USERS} u ON t.担当者アカウント = u.id
            ORDER BY t.id ASC
        """)

        components = cursor.fetchall()

        # contentの長さを計算
        for comp in components:
            comp['content_length'] = len(comp.get('content', '') or '')
            comp['has_content'] = comp['content_length'] > 0

        return jsonify({
            'success': True,
            'components': serialize_for_json(components)
        })

    except Exception as e:
        logging.error(f"Error in get_components: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()

@colrep_public_bp.route('/<int:project_id>/api/component/<int:component_id>')
def get_component_source(project_id, component_id):
    """部品のソースコンテンツ取得API"""
    try:
        # conn = mysql.connector.connect(**base_db_config, database=TARGET_DATABASE)
        conn = mysql.connector.connect(**DatabaseConfig.fujinp())
        cursor = conn.cursor(dictionary=True)

        # プロジェクト情報を取得（公開プロジェクトのみ）
        cursor.execute(f"""
            SELECT テーブル名
            FROM {COLREP_PROJECTS_TABLE}
            WHERE id = %s AND is_public = TRUE
        """, (project_id,))

        project = cursor.fetchone()

        if not project:
            return jsonify({'success': False, 'error': 'プロジェクトが見つかりません。'}), 404

        table_name = project['テーブル名']

        # 部品情報を取得
        cursor.execute(f"""
            SELECT t.id, t.カラム名, t.説明, t.content, t.status, t.更新日時, t.担当者アカウント,
                   u.full_name
            FROM `{table_name}` t
            LEFT JOIN {Tables.USERS} u ON t.担当者アカウント = u.id
            WHERE t.id = %s
        """, (component_id,))

        component = cursor.fetchone()

        if not component:
            return jsonify({'success': False, 'error': '部品が見つかりません。'}), 404

        # ✅ 修正：markdown.markdown() を使用
        html_content = ""
        if component['content']:
            try:
                html_content = markdown.markdown(component['content'], extensions=['extra', 'nl2br', 'sane_lists', 'fenced_code'])
            except Exception as e:
                logging.warning(f"Markdown conversion failed: {e}")
                import html
                html_content = f"<pre style='white-space: pre-wrap;'>{html.escape(component['content'])}</pre>"
        else:
            html_content = "<p class='text-muted'>まだコンテンツが入力されていません</p>"

        # ✅ 修正：テンプレートが期待するフィールド名に統一
        return jsonify({
            'success': True,
            'component': {
                'id': component['id'],
                'カラム名': component['カラム名'],
                '説明': component['説明'],
                'status': component['status'],
                '更新日時': serialize_for_json(component['更新日時']),
                'ユーザ名': component['full_name'] or '-',
                'content_md': component['content'] or '',
                'content_html': html_content
            }
        })

    except Exception as e:
        logging.error(f"Error in get_component_source: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()

@colrep_public_bp.route('/<int:project_id>/preview')
def get_integrated_preview(project_id):
    """統合プレビューをプレーン HTML で返す（完全なHTMLドキュメント）"""
    try:
        # conn = mysql.connector.connect(**base_db_config, database=TARGET_DATABASE)
        conn = mysql.connector.connect(**DatabaseConfig.fujinp())
        cursor = conn.cursor(dictionary=True)

        cursor.execute(f"""
            SELECT p.id, p.プロジェクト名, p.テーブル名, p.Composer, p.is_public
            FROM {COLREP_PROJECTS_TABLE} p
            WHERE p.id = %s AND p.is_public = TRUE
        """, (project_id,))

        project = cursor.fetchone()
        if not project:
            return Response("<p>プロジェクトが見つかりません。</p>", mimetype='text/html'), 404

        table_name = project['テーブル名']
        cursor.execute(f"SHOW TABLES LIKE '{table_name}'")
        if not cursor.fetchone():
            return Response("<p>プロジェクトテーブルが見つかりません。</p>", mimetype='text/html'), 404

        if not project.get('Composer'):
            return Response("<p>統合指示書（Composer）がまだ作成されていません。</p>", mimetype='text/html'), 400

        cursor.execute(f"""
            SELECT t.*,
               (SELECT full_name FROM {Tables.USERS} WHERE id = t.担当者アカウント) as full_name
            FROM `{table_name}` t
            ORDER BY t.id ASC
        """)
        raw_data = cursor.fetchall()

        integrated_markdown = _process_composer_integration(
            project['Composer'],
            raw_data,
            cursor,
            table_name
        )

        # ✅ markdown.markdown() を使用
        try:
            integrated_html = markdown.markdown(integrated_markdown, extensions=['extra', 'nl2br', 'sane_lists', 'fenced_code'])
        except Exception as e:
            logging.error(f"Markdown変換エラー: {str(e)}")
            import html as html_module
            escaped_content = html_module.escape(integrated_markdown)
            integrated_html = f"<pre style='white-space: pre-wrap; font-family: inherit; background: #f5f5f5; padding: 15px; border-radius: 4px;'>{escaped_content}</pre>"

        # ✅ 修正：colrep.pyと同じシンプルな完全HTMLドキュメントを返す
        complete_html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{project['プロジェクト名']} - 統合プレビュー</title>
    {_math_diagram_assets()}
</head>
<body>
    <div id="integrated-content">
        {integrated_html}
    </div>
</body>
</html>"""

        return Response(complete_html, mimetype='text/html')

    except Exception as e:
        logging.error(f"Error in get_integrated_preview: {str(e)}")
        error_html = f"<p style='color: red;'>エラーが発生しました: {str(e)}</p>"
        return Response(error_html, mimetype='text/html'), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()



def _math_diagram_assets():
    """[2026-07-25 改修]
    サーバ側で組み立てるプレビューHTML用の KaTeX / Mermaid 読み込み断片。
    従来これらを読み込んでいなかったため、数式（$...$）や mermaid 図が
    エディタ内プレビューでしか描画されなかった。外部エディタと同じCDN・同じ設定を使う。"""
    return """
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css">
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Hiragino Sans",
               "Noto Sans JP", sans-serif; line-height: 1.7; margin: 0; }
        /* 上部の固定バー（共有プレビュー）を全幅のまま保つため、余白は本文側に付ける */
        #integrated-content { max-width: 960px; margin: 0 auto; padding: 24px; }
        #integrated-content img, #integrated-content svg { max-width: 100%; height: auto; }
        .katex { font-size: 1.1em !important; }
        .mermaid { text-align: center; }
        table { border-collapse: collapse; }
        table, th, td { border: 1px solid #ccc; }
        th, td { padding: 6px 10px; }
        pre { background: #f5f5f5; padding: 12px; border-radius: 4px; overflow-x: auto; }
    </style>
    <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"></script>
    <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/mermaid@10.9.0/dist/mermaid.min.js"></script>
    <script>
    window.addEventListener('load', function () {
        var root = document.getElementById('integrated-content') || document.body;
        // ```mermaid のコードブロックを .mermaid 要素に変換（Markdown変換系の違いを吸収）
        root.querySelectorAll('pre > code.language-mermaid, pre > code.mermaid').forEach(function (code) {
            var div = document.createElement('div');
            div.className = 'mermaid';
            div.textContent = code.textContent;
            code.parentNode.replaceWith(div);
        });
        function renderMath() {
            if (!window.renderMathInElement) return;
            renderMathInElement(root, {
                delimiters: [
                    { left: '$$', right: '$$', display: true },
                    { left: '$', right: '$', display: false }
                ],
                throwOnError: false
            });
        }
        // Mermaid の描画完了後に KaTeX を走らせる（図の定義中の $ を誤変換しないため）
        if (window.mermaid) {
            try {
                mermaid.initialize({ startOnLoad: false });
                var p = mermaid.run({ nodes: root.querySelectorAll('.mermaid') });
                if (p && typeof p.then === 'function') { p.then(renderMath, renderMath); }
                else { renderMath(); }
            } catch (e) { console.error('mermaid error', e); renderMath(); }
        } else {
            renderMath();
        }
    });
    </script>"""


def _process_composer_integration(composer_content, raw_data, cursor, table_name):
    """Composer内のマクロを実際のコンテンツで置換"""
    result = composer_content

    # パターン 1: [[カラム名]] 形式のマクロ
    macro_pattern = r'\[\[([^\]]+)\]\]'

    def replace_macro(match):
        column_name = match.group(1).strip()
        matching_tasks = [task for task in raw_data if task['カラム名'] == column_name]
        if matching_tasks:
            content = matching_tasks[0].get('content')
            return content if content else ''
        return ''

    result = re.sub(macro_pattern, replace_macro, result)

    # パターン 2: 既存の SQL クエリパターン（互換性のため）
    sql_pattern = r'```sql_md_texts\s+select\s+content\s+from\s+' + re.escape(table_name) + r'\s+where\s+カラム名\s*=\s*[\'"]([^\'\"]+)[\'\"]\s*;\s*```'

    def replace_sql(match):
        column_name = match.group(1)
        matching_tasks = [task for task in raw_data if task['カラム名'] == column_name]
        if matching_tasks:
            return matching_tasks[0].get('content', '')
        return ''

    result = re.sub(sql_pattern, replace_sql, result, flags=re.IGNORECASE | re.MULTILINE | re.DOTALL)

    return result

@colrep_public_bp.route('/return_to_fujin')
@login_required
def return_to_fujin():
    """FUJIN-Pダッシュボードに戻る"""
    return redirect_to_dashboard()
