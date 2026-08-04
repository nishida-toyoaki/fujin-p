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

from flask import Blueprint, request, render_template, jsonify, session, redirect, url_for, flash
from auth import login_required
import mysql.connector
import markdown
import re
import os
import logging
# from db import base_db_config, default_db_config
from config import Config
from db import DatabaseConfig, Tables


markdown_converter_bp = Blueprint('markdown_converter', __name__)


def escape_dollar_signs(text):
    # 既にエスケープされているドル記号はそのままにする
    text = re.sub(r'(?<!\\)\$\$', r'\\$\\$', text)
    # 単一のドル記号をエスケープする（ただし、既にエスケープされているものは除く）
    text = re.sub(r'(?<!\\)\$', r'\\$', text)
    return text

def sanitize_sql_blocks_for_guest(markdown_content):
    """
    guestユーザー用：```sqlブロックを無効化してセキュリティリスクを軽減
    """
    if not markdown_content:
        return markdown_content

    def replace_sql_block(match):
        sql_code = match.group(1)
        # 警告文をコードブロックの外（前）に配置
        return f"⚠️ **セキュリティ保護**: 元のコード記法 `\u0060\u0060\u0060sql` は管理者のみ表示可能です（このコードは実行できません）\n\n```\n{sql_code}```"

    pattern = r'```(?i:sql)\s*[\r\n]+(.*?)```'
    result = re.sub(pattern, replace_sql_block, markdown_content, flags=re.DOTALL)

    return result

#  内部リンク拡張は使用しない
#
#def process_internal_links(content):
#    def replacement(match):
#        text = match.group(1)
#        note_id = match.group(2)
#        return f'<a href="{url_for("trial_user.view_note", note_id=note_id)}">{text}</a>'
#
#    # 新しい形式 [テキスト]{ID} に対応する正規表現
#    pattern = r'\[([^\]]+)\]\{(\d+)\}'
#    return re.sub(pattern, replacement, content)

def process_sql_md_texts(md_content, connection):
    """
    sql_md_textsブロックを処理する新しい関数
    SQLクエリを実行し、結果をMarkdownとして処理してHTMLに変換
    """
    sql_md_pattern = re.compile(r'```sql_md_texts\s*\n(.*?)\n\s*```', re.DOTALL)
    cursor = connection.cursor(dictionary=True)

    def replace_sql_md_block(match):
        sql_query = match.group(1).strip()
        try:
            cursor.execute(sql_query)
            result = cursor.fetchall()

            # 結果を結合（複数行の場合は改行で繋ぐ）
            if result:
                # 最初のカラムの値を取得して結合
                first_column_name = list(result[0].keys())[0] if result[0] else None
                if first_column_name:
                    md_texts = []
                    for row in result:
                        value = row[first_column_name]
                        if value is not None:
                            md_texts.append(str(value))

                    combined_md = '\n'.join(md_texts)

                    # 再帰的にMarkdown処理を行う（sql_md_textsブロックの無限ループを防ぐため、この部分では通常のMarkdown処理のみ）
                    processed_html = markdown.markdown(combined_md, extensions=[
                        'extra', 'nl2br', 'sane_lists', 'fenced_code'    #,'pymdownx.tasklist'
                    ])

                    return processed_html
                else:
                    return "<p>No columns found in result</p>"
            else:
                return "<p>No results found</p>"

        except mysql.connector.Error as e:
            return f"<p class='text-danger'>Error executing SQL query: {str(e)}</p>"
        except Exception as e:
            return f"<p class='text-danger'>Error processing sql_md_texts: {str(e)}</p>"

    # sql_md_textsブロックを処理
    processed_content = sql_md_pattern.sub(replace_sql_md_block, md_content)
    cursor.close()

    return processed_content

def process_mermaid(md_content):
    """
    Mermaidブロックを処理してHTMLに変換する
    """
    import re

    def create_mermaid_html(mermaid_content, diagram_id):
        return f"""
        <div class="mermaid-diagram" id="mermaid-{diagram_id}">
            <div class="mermaid">
                {mermaid_content}
            </div>
        </div>
        """

    # Mermaidブロックを検出して処理
    mermaid_pattern = re.compile(r'```mermaid\n(.*?)\n```', re.DOTALL)
    diagram_counter = 0

    def replace_mermaid(match):
        nonlocal diagram_counter
        diagram_counter += 1
        mermaid_content = match.group(1).strip()
        return create_mermaid_html(mermaid_content, diagram_counter)

    # Mermaidブロックを処理
    processed_content = mermaid_pattern.sub(replace_mermaid, md_content)

    # Mermaidスクリプトを追加（ダイアグラムが存在する場合のみ）
    if diagram_counter > 0:
        mermaid_script = """
        <script src="https://cdnjs.cloudflare.com/ajax/libs/mermaid/10.8.0/mermaid.min.js"></script>
        <script>
            mermaid.initialize({
                startOnLoad: true,
                theme: 'default',
                securityLevel: 'loose',
                htmlLabels: true,
                flowchart: { useMaxWidth: true, htmlLabels: true },
                clickable: true
            });
        </script>
        """
        # スクリプトタグを</body>の直前に挿入
        processed_content = re.sub(
            r'</body>',
            f'{mermaid_script}</body>',
            processed_content
        )

    return processed_content


def process_markdown_for_preview(md_content, user_category='guest'):
    """
    プレビュー用に、HTMLの本体部分のみを生成する
    """
    if user_category != 'admin':
        md_content = sanitize_sql_blocks_for_guest(md_content)

    # 既存のprocess_markdownのロジックを再利用し、最後のHTMLテンプレート部分だけを省略する
    try:
        # 1. 基本的な前処理
        md_content = md_content.replace('\r', '')
        md_content = escape_dollar_signs(md_content)
        # md_content = process_internal_links(md_content)  #  内部リンク拡張は使用しない

        # データベース接続を作成
        # connection = mysql.connector.connect(**fujinp_db_config)
        connection = mysql.connector.connect(**DatabaseConfig.fujinp())


        # 2. sql_md_textsクエリの処理（新機能）
        md_content = process_sql_md_texts(md_content, connection)

        # 3. 通常のSQLクエリの処理
        sql_pattern = re.compile(r'```sql\n(.*?)(?:\n---\nColumn widths: ([\d%, ]+))?\n```', re.DOTALL)
        cursor = connection.cursor(dictionary=True)
        sql_matches = sql_pattern.finditer(md_content)
        for sql_match in sql_matches:
            sql_query = sql_match.group(1).strip()
            column_widths = sql_match.group(2)
            try:
                cursor.execute(sql_query)
                result = cursor.fetchall()
                table_content = process_sql_result(result, column_widths)
                md_content = md_content.replace(sql_match.group(0), table_content)
            except mysql.connector.Error as e:
                md_content = md_content.replace(sql_match.group(0), f"<p class='text-danger'>Error executing SQL query: {str(e)}</p>")
        cursor.close()
        connection.close()

        # 4. Mermaid, SVGブロックをプレースホルダーに置き換え
        mermaid_blocks = {}
        mermaid_counter = 0
        mermaid_pattern = re.compile(r'```mermaid\s*\n(.*?)\n\s*```', re.DOTALL)
        def mermaid_replace(match):
            nonlocal mermaid_counter
            mermaid_counter += 1
            content = match.group(1).strip()
            placeholder = f"MERMAID_PLACEHOLDER_{mermaid_counter}"
            mermaid_blocks[placeholder] = f'<div class="mermaid">{content}</div>'
            return placeholder
        md_content = mermaid_pattern.sub(mermaid_replace, md_content)

        svg_blocks = {}
        svg_counter = 0
        svg_pattern = re.compile(r'```svg\s*\n(.*?)\n\s*```', re.DOTALL)
        def svg_replace(match):
            nonlocal svg_counter
            svg_counter += 1
            content = match.group(1).strip()
            placeholder = f"SVG_PLACEHOLDER_{svg_counter}"
            svg_blocks[placeholder] = f'<div class="svg-container">{content}</div>'
            return placeholder
        md_content = svg_pattern.sub(svg_replace, md_content)

        # 5. 標準Markdownの変換
        html_content = markdown.markdown(md_content, extensions=[
            'extra', 'nl2br', 'sane_lists', 'fenced_code' # , 'pymdownx.tasklist'
        ])

        # 6. プレースホルダーをHTMLに置き換え
        for placeholder, block_html in mermaid_blocks.items():
            html_content = html_content.replace(f'<p>{placeholder}</p>', block_html)
        for placeholder, block_html in svg_blocks.items():
            html_content = html_content.replace(f'<p>{placeholder}</p>', block_html)

        # 7. 数式の後処理
        return html_content

    except Exception as e:
        return f"<p class='text-danger'>Markdown処理中にエラーが発生しました: {str(e)}</p>"

def process_markdown(md_content, user_category='guest'):
    """
    Markdownコンテンツを処理してHTMLに変換する
    """
    # 基本的な前処理
    md_content = md_content.replace('\r', '')
    if user_category != 'admin':
        md_content = sanitize_sql_blocks_for_guest(md_content)
    # md_content = escape_dollar_signs(md_content) # $記号はエスケープせずそのままにする
    # md_content = process_internal_links(md_content)  #  内部リンク拡張は使用しない

    # 基本的なCSSスタイルを定義（既存のスタイルを維持）
    css_style = """
    <style>
        .sql-result-table {
            border-collapse: collapse;
            margin: 0 auto;
            table-layout: fixed;
            width: 100%;
            font-size: 12px;
        }
        .sql-result-table th, .sql-result-table td {
            border: 1px solid #ddd;
            padding: 4px;
            text-align: left;
            vertical-align: top;
            word-wrap: break-word;
            overflow-wrap: break-word;
            hyphens: auto;
        }
        .sql-result-table th {
            background-color: #f2f2f2;
            font-size: 13px;
        }
        .table-container {
            width: 100%;
            overflow-x: auto;
        }
        ul, ol {
            padding-left: 20px;
            margin: 10px 0;
        }
        li {
            margin-bottom: 5px;
        }
        ol {
            list-style-type: decimal;
        }
        .task-list-item {
            list-style-type: none;
        }
        .task-list-item-checkbox {
            margin-right: 0.5em;
        }
        /* Mermaid図用のスタイル */
        .mermaid-diagram {
            margin: 20px 0;
            padding: 10px;
            background: #f8f9fa;
            border-radius: 4px;
            overflow-x: auto;
        }
        .mermaid {
            display: flex;
            justify-content: center;
        }
        .mermaid svg {
            max-width: 100%;
        }
        /* クリッカブル要素のスタイル */
        .mermaid [id*="node"] a {
            cursor: pointer !important;
        }
        .mermaid [id*="node"]:hover {
            opacity: 0.7;
            transition: opacity 0.2s;
        }
        /* クリッカブルノードのスタイル */
        .mermaid g[class*="clickable"] {
            cursor: pointer !important;
            transition: opacity 0.2s;
        }
        .mermaid g[class*="clickable"]:hover {
            opacity: 0.7;
        }
        /* SVG表示用のスタイル追加 */
        .svg-container {
            display: flex;
            justify-content: center;
            margin: 20px 0;
            max-width: 100%;
            overflow-x: auto;
        }
        .svg-container svg {
            max-width: 100%;
            height: auto;
        }
        /* sql_md_texts結果用のスタイル */
        .sql-md-content {
            margin: 10px 0;
        }
    </style>
    """

    try:
        # データベース接続を作成
        # connection = mysql.connector.connect(**fujinp_db_config)
        connection = mysql.connector.connect(**DatabaseConfig.fujinp())


        # 1. sql_md_textsクエリの処理（新機能）
        md_content = process_sql_md_texts(md_content, connection)

        # 2. 通常のSQLクエリの処理
        sql_pattern = re.compile(r'```sql\n(.*?)(?:\n---\nColumn widths: ([\d%,]+))?\n```', re.DOTALL)
        sql_matches = sql_pattern.finditer(md_content)

        cursor = connection.cursor(dictionary=True)

        for sql_match in sql_matches:
            sql_query = sql_match.group(1).strip()
            column_widths = sql_match.group(2)

            try:
                cursor.execute(sql_query)
                result = cursor.fetchall()
                table_content = process_sql_result(result, column_widths)
                md_content = md_content.replace(sql_match.group(0), table_content)
            except mysql.connector.Error as e:
                md_content = md_content.replace(sql_match.group(0), f"<p>Error executing SQL query: {str(e)}</p>")

        cursor.close()
        connection.close()

        # 3. Mermaid図の処理部分を修正
        mermaid_blocks = {}  # Mermaidブロックを一時的に保存
        diagram_counter = 0

        # Mermaidブロックを一時的なプレースホルダーに置き換え
        mermaid_pattern = re.compile(
            r'```mermaid\s*\n'  # 開始行
            r'(.*?)'            # 図の定義内容（最小一致）
            r'\n\s*```',        # 終了行
            re.DOTALL
        )

        def mermaid_replace(match):
            nonlocal diagram_counter
            diagram_counter += 1
            content = match.group(1).strip()
            placeholder = f"MERMAID_PLACEHOLDER_{diagram_counter}"
            mermaid_blocks[placeholder] = f"""
            <div class="mermaid" id="mermaid-{diagram_counter}">
        {content}
            </div>
            """

            return placeholder

        md_content = mermaid_pattern.sub(mermaid_replace, md_content)

        katex_script = """
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.9/katex.min.css">
        <script defer src="https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.9/katex.min.js"></script>
        <script defer src="https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.9/contrib/auto-render.min.js"
            onload="renderMathInElement(document.body, {
                delimiters: [
                    {left: '$$', right: '$$', display: true},
                    {left: '$', right: '$', display: false}
                ]
            });">
        </script>
        <style>
            .katex { font-size: 1em !important; }
            .katex-display { overflow: auto hidden; }
        </style>
        """

        # SVGブロックの処理を追加
        svg_blocks = {}
        svg_counter = 0
        svg_pattern = re.compile(r'```svg\s*\n(.*?)\n\s*```', re.DOTALL)

        def svg_replace(match):
            nonlocal svg_counter
            svg_counter += 1
            content = match.group(1).strip()
            placeholder = f"SVG_PLACEHOLDER_{svg_counter}"
            svg_blocks[placeholder] = f"""
            <div class="svg-container">
                {content}
            </div>
            """
            return placeholder

        md_content = svg_pattern.sub(svg_replace, md_content)

        # 4. Markdownの変換（fenced_code拡張を含める）
        html_content = markdown.markdown(md_content, extensions=[
            'extra', 'nl2br', 'sane_lists',
            ##'pymdownx.tasklist',
            'fenced_code'  # 明示的にfenced_code拡張を追加
        ])

        # 5. プレースホルダーをMermaid HTMLに置き換え
        for placeholder, mermaid_html in mermaid_blocks.items():
            html_content = html_content.replace(placeholder, mermaid_html)

        # SVGの置き換え（新規追加）
        for placeholder, svg_html in svg_blocks.items():
            html_content = html_content.replace(placeholder, svg_html)

        # 6. 数式の処理（既存の処理）
        html_content = re.sub(r'\\∖\((.*?)\\∖\)', r'$\1$', html_content)
        html_content = re.sub(r'\\∖\[(.*?)\\∖\]', r'$$\1$$', html_content)

        mermaid_script = """
        <script src="https://cdn.jsdelivr.net/npm/mermaid@10.8.0/dist/mermaid.min.js"></script>
        <script>
            document.addEventListener('DOMContentLoaded', function() {
                mermaid.initialize({
                    startOnLoad: true,
                    theme: 'default',
                    securityLevel: 'loose',
                    htmlLabels: true,
                    flowchart: { useMaxWidth: true },
                    mindmap: { padding: 10 }
                });

                // クリックイベントの設定
                mermaid.parseError = function(err, hash) {
                    console.error('Mermaid error:', err);
                };
            });
        </script>
        """

        # 7. 最終的なHTML生成
        full_html_content = f"""
        <!DOCTYPE html>
        <html lang="ja">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Markdown Converted Content</title>
            {css_style}
            {katex_script}
            {mermaid_script if diagram_counter > 0 else ''}
        </head>
        <body>
            <div class="content">
                {html_content}
            </div>
        </body>
        </html>
        """
        return full_html_content

    except Exception as e:
        return f"<p>An error occurred: {str(e)}</p>"

def process_sql_result(result, column_widths):
    """SQLクエリ結果をHTML表に変換する補助関数"""
    if not result:
        return "<div class='table-container'><table class='sql-result-table'><tr><td>No results found</td></tr></table></div>"

    columns = result[0].keys()
    widths = []
    if column_widths:
        widths = [float(w.strip('%').strip()) for w in column_widths.split(',') if w.strip()]
    else:
        widths = [100 / len(columns)] * len(columns)

    table_html = "<div class='table-container'><table class='sql-result-table'>"

    # ヘッダー行
    table_html += "<tr>"
    for i, col_name in enumerate(columns):
        width_style = f"width: {widths[i]}%;"
        table_html += f"<th style='{width_style}'>{col_name}</th>"
    table_html += "</tr>"

    # データ行
    for row in result:
        table_html += "<tr>"
        for i, col_name in enumerate(columns):
            width_style = f"width: {widths[i]}%;"
            display_value = row[col_name] if row[col_name] is not None else ''
            table_html += f"<td style='{width_style}'>{display_value}</td>"
        table_html += "</tr>"

    table_html += "</table></div>"
    return table_html


@markdown_converter_bp.route('/')
@login_required
def index():
    print('markdown converter index')
    return_to = request.args.get('return_to')
    if not return_to:
        print('return to not given at markdown converter')
        return_to = url_for('auth.dashboard')
    return render_template('markdown_converter.html', return_to = return_to)

@markdown_converter_bp.route('/convert', methods=['POST'])
@login_required
def convert_markdown():
    md_content = request.form.get('markdown_content')

    if md_content:
        # ブラウザからの入力を処理
        output_html = process_markdown(md_content)
        return jsonify({
            'success': True,
            'html_content': output_html
        })
    else:
        # 元の機能を保持（ファイルからの読み込み）
        md_file_path = '/home/nishida/markdowns/DatabookPublic.md'
        output_file = '/home/nishida/markdowns/DatabookPublic.html'

        output_html = process_markdown_file(md_file_path, output_file)
        return jsonify({
            'success': True,
            'message': f"HTML file has been written to {output_file}",
            'output_path': output_file
        })


def process_markdown_file(md_file_path, output_file):
    try:
        # Markdownファイルの読み込み
        with open(md_file_path, 'r', encoding='utf-8') as md_file:
            md_content = md_file.read()

        # Markdownの処理
        html_content = process_markdown(md_content)

        # HTMLファイルを書き出し
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(html_content)

        return html_content

    except Exception as e:
        return f"<p>An error occurred: {str(e)}</p>"