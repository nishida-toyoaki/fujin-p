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

from flask import render_template, request, jsonify, session, url_for, redirect, flash, Response
from decorators import login_required
from config import Config
from db import DatabaseConfig
from mysql.connector import Error
import mysql.connector
import os
import re
import uuid
import html
import json
import base64
from functools import wraps
from urllib.parse import urlparse, quote
from werkzeug.utils import secure_filename
from markdown_converter import process_markdown
from datetime import datetime
import pytz

from . import my_md_notes_bp

# =============================================================================
# 定数
# =============================================================================

# 画像／ファイルアップロード設定
#   保存先は Config.UPLOAD_BASE_DIR を起点に組み立てる（プラットフォーム共通の作法）。
#   UPLOAD_BASE_DIR は「配下の static/ が URL /static/ で配信されるディレクトリ」を指す。
#   未定義の環境ではホームディレクトリにフォールバックする（従来と同じ場所）。
UPLOAD_BASE_DIR = getattr(Config, 'UPLOAD_BASE_DIR', None) or os.path.expanduser('~')
UPLOAD_SUBDIR = 'mdimgs'
UPLOAD_FOLDER = os.path.join(UPLOAD_BASE_DIR, 'static', UPLOAD_SUBDIR)
UPLOAD_URL_PREFIX = f'/static/{UPLOAD_SUBDIR}'

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'svg', 'pdf'}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20MB

# 拡張子ごとのマジックナンバー（拡張子偽装の検出用）
# SVGはテキスト形式のため別扱い（svg_head_ok で検査し、保存前にサニタイズする）
MAGIC_NUMBERS = {
    'png': (b'\x89PNG\r\n\x1a\n',),
    'jpg': (b'\xff\xd8\xff',),
    'jpeg': (b'\xff\xd8\xff',),
    'pdf': (b'%PDF-',),
}

# 公開範囲（コレポ／文書アーカイブ／あわならと同じ区分）
#   private        - 所有者とadminのみ
#   public         - ログイン済みの全ユーザに開示（＝コレポの「ゲストにも」と同義）
#   domestic       - 構成員（regular）だけ
#   group          - 指定グループの有効所属者だけ
#   domestic_group - 構成員または指定グループの有効所属者（和集合）
#
# 未ログイン公開（旧・公開URL）は廃止。マイノートは執筆用アプリであり、
# 執筆物をそのままインターネットへ出さない方針（コレポと同じ）。
# インターネット公開は「アーカイブに保存」（archive_note）で
# 文書アーカイブ（document_archive）へ移してから行う。
SHARE_KEYS = ('private', 'public', 'domestic', 'group', 'domestic_group')

SHARE_LABELS = {
    'private': '非公開',
    'public': 'ゲストにも',
    'domestic': '構成員だけ',
    'group': 'グループ',
    'domestic_group': '構成員＋グループ',
}

JST = pytz.timezone('Asia/Tokyo')

# コンテンツ移行（JSONエクスポート／インポート）
#   サイト間でノート本体・本文・公開範囲・許可グループ・添付ファイルを移すための形式。
#   users.id や user_groups.id はサイトごとに異なるので、所有者は email、
#   許可グループは name で持ち運び、取り込み先で引き当てる。
EXPORT_TYPE = 'fujinp_my_md_notes_content'
EXPORT_FORMAT_VERSION = 1
DT_FORMAT = '%Y-%m-%d %H:%M:%S'
IMPORT_MODES = ('skip', 'overwrite', 'add')

# HTMLダウンロード（download_html）が生成する文書のスタイル。
# 閲覧画面（view_note.html）の .content-area 用CSSと同じ規則を持たせ、
# ダウンロードしたファイル単体でも画面と同じ見え方になるようにする。
# ★ここを変えたときは view_note.html 側も合わせること。
DOWNLOAD_HTML_CSS = """
        * { box-sizing: border-box; }

        body {
            margin: 0;
            padding: 0;
            font-family: 'Segoe UI', 'Helvetica Neue', sans-serif;
            background-color: #f5f7fa;
            color: #2d3748;
        }

        .content-container {
            max-width: 1200px;
            margin: 30px auto;
            padding: 0 20px;
        }

        .content-area {
            background-color: white;
            padding: 40px;
            border-radius: 8px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            line-height: 1.7;
        }

        /* Markdown コンテンツスタイル */
        .content-area h1, .content-area h2, .content-area h3,
        .content-area h4, .content-area h5, .content-area h6 {
            margin-top: 1.5em;
            margin-bottom: 0.5em;
            font-weight: 600;
            line-height: 1.25;
            color: #2d3748;
        }

        .content-area h1 {
            font-size: 2em;
            border-bottom: 2px solid #e2e8f0;
            padding-bottom: 0.3em;
        }

        .content-area h2 {
            font-size: 1.5em;
            border-bottom: 1px solid #e2e8f0;
            padding-bottom: 0.3em;
        }

        .content-area h3 { font-size: 1.25em; }

        .content-area p { margin-bottom: 1em; }

        .content-area a { color: #4299e1; text-decoration: none; }
        .content-area a:hover { text-decoration: underline; }

        .content-area ul, .content-area ol {
            padding-left: 2em;
            margin-bottom: 1em;
        }

        .content-area li { margin-bottom: 0.25em; }

        .content-area blockquote {
            border-left: 4px solid #e2e8f0;
            padding-left: 1em;
            margin-left: 0;
            color: #718096;
            font-style: italic;
        }

        .content-area code {
            background-color: #f7fafc;
            padding: 2px 6px;
            border-radius: 3px;
            font-family: 'Courier New', monospace;
            font-size: 0.9em;
            color: #e53e3e;
        }

        .content-area pre {
            background-color: #2d3748;
            color: #e2e8f0;
            padding: 16px;
            border-radius: 6px;
            overflow-x: auto;
            margin-bottom: 1em;
        }

        .content-area pre code {
            background-color: transparent;
            padding: 0;
            color: inherit;
            font-size: 0.9em;
        }

        .content-area table {
            border-collapse: collapse;
            width: 100%;
            margin-bottom: 1em;
            overflow-x: auto;
            display: block;
        }

        .content-area th, .content-area td {
            border: 1px solid #e2e8f0;
            padding: 8px 12px;
            text-align: left;
        }

        .content-area th {
            background-color: #f7fafc;
            font-weight: 600;
        }

        .content-area tbody tr:nth-child(even) { background-color: #f7fafc; }

        .content-area img {
            max-width: 100%;
            height: auto;
            border-radius: 6px;
            margin: 1em 0;
        }

        .content-area hr {
            border: none;
            border-top: 2px solid #e2e8f0;
            margin: 2em 0;
        }

        /* SQL結果テーブル */
        .sql-result-table { font-size: 0.85em; }
        .table-container { overflow-x: auto; margin-bottom: 1em; }

        /* KaTeX数式 */
        .katex-display { margin: 1em 0; }

        @media (max-width: 768px) {
            .content-area { padding: 20px; }
        }
"""


# =============================================================================
# ユーティリティ
# =============================================================================

def get_jst_now():
    """現在日時をJSTのnaive datetimeで返す（DBのDATETIME列にJSTの値として格納する）"""
    return datetime.now(JST).replace(tzinfo=None)


def fmt_dt(value):
    """DATETIME(JST) を表示用の文字列にする。フロントエンドへは常に文字列で渡す"""
    if not value:
        return ''
    if isinstance(value, str):
        # 既に文字列で返ってきた場合（ドライバや列型の違い）は先頭16文字を使う
        return value[:16]
    try:
        return value.strftime('%Y-%m-%d %H:%M')
    except AttributeError:
        return str(value)


def add_display_dates(rows):
    """行dictに表示用の日時文字列と公開範囲ラベルを付与する"""
    for row in rows:
        row['作成日時表示'] = fmt_dt(row.get('作成日時'))
        row['更新日時表示'] = fmt_dt(row.get('更新日時'))
        row['共有表示'] = SHARE_LABELS.get(row.get('共有キー'), '非公開')
    return rows


def to_int(value, default=0):
    """フォーム値を整数に変換する。空欄・不正値は default"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def normalize_share_key(value, current='private'):
    """公開範囲キーを SHARE_KEYS のいずれかに正規化する"""
    if value in SHARE_KEYS:
        return value
    return current if current in SHARE_KEYS else 'private'


def origin_ok():
    """要求元が同一オリジンかどうかを判定する（簡易CSRF対策）。

    Origin / Referer のいずれも付かない要求は真を返す（判定できないため）。
    プラットフォーム側にCSRFトークン機構を導入する場合は、この仕組みを置き換えること。
    """
    for header in ('Origin', 'Referer'):
        value = request.headers.get(header)
        if value:
            return urlparse(value).netloc == request.host
    return True


def same_origin_required(view):
    """状態を変更するPOST（JSON応答）に対する簡易CSRF対策"""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not origin_ok():
            return jsonify({'success': False, 'error': '不正な要求元です'}), 403
        return view(*args, **kwargs)
    return wrapper


def admin_only(view):
    """管理者（session の user_category == 'admin'）だけに許すルート用のガード。
    それ以外はフラッシュして一覧へ戻す。@login_required の内側で使う。"""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if session.get('user_category') != 'admin':
            flash('この操作は管理者のみ実行できます。', 'error')
            return redirect(url_for('my_md_notes.index'))
        return view(*args, **kwargs)
    return wrapper


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def magic_ok(head_bytes, ext):
    signatures = MAGIC_NUMBERS.get(ext)
    if not signatures:
        return False
    return any(head_bytes.startswith(sig) for sig in signatures)


def svg_head_ok(head_bytes):
    """SVGらしさの簡易検査（先頭付近に <svg タグがあるか）"""
    try:
        text = head_bytes.decode('utf-8', errors='replace')
    except Exception:
        return False
    return '<svg' in text.lower()


def _sanitize_svg(data):
    """アップロードされた SVG から script / on* / javascript: を除去する簡易サニタイズ
    （コレポと同じ規則）"""
    try:
        text = data.decode('utf-8', errors='replace')
    except Exception:
        return data
    # <script>...</script> 除去
    text = re.sub(r'<script\b[^>]*>.*?</script\s*>', '', text,
                  flags=re.IGNORECASE | re.DOTALL)
    # 自己終端の <script .../> 除去
    text = re.sub(r'<script\b[^>]*/\s*>', '', text, flags=re.IGNORECASE)
    # on*="..." イベントハンドラ除去
    text = re.sub(r'\son\w+\s*=\s*"[^"]*"', '', text, flags=re.IGNORECASE)
    text = re.sub(r"\son\w+\s*=\s*'[^']*'", '', text, flags=re.IGNORECASE)
    # javascript: URI 除去
    text = re.sub(r'javascript\s*:', '', text, flags=re.IGNORECASE)
    return text.encode('utf-8')


# =============================================================================
# ユーザーグループ（公開範囲の判定用。コレポと同じ参照規則）
# =============================================================================

def get_user_active_group_ids(user_id):
    """ユーザーが現在有効に所属しているグループIDのリスト
    （user_groups / user_group_memberships を参照、有効期間チェック付き）"""
    if not user_id:
        return []
    try:
        now = get_jst_now()
        with mysql.connector.connect(**DatabaseConfig.default()) as conn:
            with conn.cursor(dictionary=True) as cursor:
                cursor.execute("""
                    SELECT group_id FROM user_group_memberships
                    WHERE user_id = %s
                      AND (valid_from IS NULL OR valid_from <= %s)
                      AND (valid_until IS NULL OR valid_until >= %s)
                """, (user_id, now, now))
                return [r['group_id'] for r in cursor.fetchall()]
    except mysql.connector.Error as e:
        print(f"[my_md_notes] get_user_active_group_ids error: {e}")
        return []


def get_all_user_groups():
    """全ユーザーグループの一覧（共有設定の選択肢用）"""
    try:
        with mysql.connector.connect(**DatabaseConfig.default()) as conn:
            with conn.cursor(dictionary=True) as cursor:
                cursor.execute("SELECT id, name FROM user_groups ORDER BY id DESC")
                return cursor.fetchall()
    except mysql.connector.Error as e:
        print(f"[my_md_notes] get_all_user_groups error: {e}")
        return []


def get_note_access_group_ids(note_id):
    """ノートのグループ公開で許可されているグループIDのリスト"""
    try:
        with mysql.connector.connect(**DatabaseConfig.default()) as conn:
            with conn.cursor(dictionary=True) as cursor:
                cursor.execute("""
                    SELECT group_id FROM my_md_notes_access_groups WHERE ノートID = %s
                """, (note_id,))
                return [r['group_id'] for r in cursor.fetchall()]
    except mysql.connector.Error as e:
        print(f"[my_md_notes] get_note_access_group_ids error: {e}")
        return []


def can_view_note(note, user_id, user_category):
    """ノートの閲覧可否を公開範囲（共有キー）に基づいて判定する。

    呼び出し元はすべて @login_required 配下にある。したがって

      admin／所有者     - 常に可
      public            - ログイン済みの全ユーザ（ゲスト含む）
      domestic          - 構成員（regular）のみ
      group             - 指定グループの有効所属者のみ
      domestic_group    - 構成員または指定グループの有効所属者
      private           - 所有者とadminのみ
    """
    if user_category == 'admin' or note['オーナーID'] == user_id:
        return True

    policy = note.get('共有キー') or 'private'

    if policy == 'public':
        return True

    if policy == 'domestic':
        return user_category == 'regular'

    if policy in ('group', 'domestic_group'):
        if policy == 'domestic_group' and user_category == 'regular':
            return True
        allowed = set(get_note_access_group_ids(note['id']))
        mine = set(get_user_active_group_ids(user_id))
        return bool(allowed & mine)

    return False


# =============================================================================
# データアクセス
# =============================================================================

def get_user_notes_flat(user_id, category=None):
    """更新日時の逆順でノートを取得する。

    admin   - 全ノート
    その他  - 自分のノート ＋ グループ公開で自分が対象のノート
              （public / domestic のノートは一覧には出さず、閲覧URLで到達する）
    """
    try:
        with mysql.connector.connect(**DatabaseConfig.default()) as conn:
            with conn.cursor(dictionary=True) as cursor:
                if category == 'admin':
                    cursor.execute("""
                        SELECT ns.*, u.full_name AS オーナー名, 'admin' AS 権限
                        FROM my_md_notes_notes ns
                        LEFT JOIN users u ON ns.オーナーID = u.id
                        ORDER BY ns.更新日時 DESC
                    """)
                    return add_display_dates(cursor.fetchall())

                group_ids = get_user_active_group_ids(user_id)
                if group_ids:
                    placeholders = ', '.join(['%s'] * len(group_ids))
                    cursor.execute(f"""
                        SELECT DISTINCT ns.*, u.full_name AS オーナー名,
                            CASE WHEN ns.オーナーID = %s THEN '所有者' ELSE '閲覧' END AS 権限
                        FROM my_md_notes_notes ns
                        LEFT JOIN users u ON ns.オーナーID = u.id
                        WHERE ns.オーナーID = %s
                           OR (ns.共有キー IN ('group', 'domestic_group')
                               AND ns.id IN (SELECT ノートID FROM my_md_notes_access_groups
                                             WHERE group_id IN ({placeholders})))
                        ORDER BY ns.更新日時 DESC
                    """, tuple([user_id, user_id] + group_ids))
                else:
                    cursor.execute("""
                        SELECT ns.*, u.full_name AS オーナー名, '所有者' AS 権限
                        FROM my_md_notes_notes ns
                        LEFT JOIN users u ON ns.オーナーID = u.id
                        WHERE ns.オーナーID = %s
                        ORDER BY ns.更新日時 DESC
                    """, (user_id,))
                return add_display_dates(cursor.fetchall())
    except mysql.connector.Error as e:
        print(f"データベースエラー: {e}")
        return []


def create_note(user_id, name, sequence=0):
    """空のノートを1件作成して note_id を返す。公開範囲の初期値は 'private'。"""
    conn = mysql.connector.connect(**DatabaseConfig.default())
    cursor = conn.cursor()
    try:
        current_time = get_jst_now()

        cursor.execute("""
            INSERT INTO my_md_notes_notes (オーナーID, 名前, 共有キー, 序列, 作成日時, 更新日時)
            VALUES (%s, %s, 'private', %s, %s, %s)
        """, (user_id, name, sequence, current_time, current_time))
        note_id = cursor.lastrowid

        cursor.execute("""
            INSERT INTO my_md_notes_contents (ノートID, 内容, 作成日時, 更新日時)
            VALUES (%s, '', %s, %s)
        """, (note_id, current_time, current_time))

        conn.commit()
        return note_id
    except Error as e:
        print(f"Error: {e}")
        conn.rollback()
        return None
    finally:
        cursor.close()
        conn.close()


# =============================================================================
# ルート
# =============================================================================

@my_md_notes_bp.route('/')
@login_required
def index():
    user_category = session.get('user_category')
    notes = get_user_notes_flat(session['user_id'], category=user_category)
    return_to = request.args.get('return_to') or url_for('auth.redirect_to_dashboard')

    return render_template('my_notes.html', notes=notes, return_to=return_to,
                           user_category=user_category, session=session)


@my_md_notes_bp.route('/create_note')
@login_required
def create_note_route():
    """新規ノート作成 - 空のノートを作成して編集画面へ"""
    note_id = create_note(session['user_id'], '新規ノート', 0)
    if note_id:
        return redirect(url_for('my_md_notes.edit_note', note_id=note_id))

    flash('ノートの作成に失敗しました。', 'error')
    return redirect(url_for('my_md_notes.index'))


@my_md_notes_bp.route('/edit_note/<int:note_id>', methods=['GET', 'POST'])
@login_required
def edit_note(note_id):
    """ノート編集（所有者とadminのみ）"""
    if request.method == 'POST' and not origin_ok():
        flash('不正な要求元です。', 'error')
        return redirect(url_for('my_md_notes.index'))

    conn = mysql.connector.connect(**DatabaseConfig.default())
    cursor = conn.cursor(dictionary=True)
    try:
        user_category = session.get('user_category')
        if user_category == 'admin':
            cursor.execute("""
                SELECT ns.*, nc.内容
                FROM my_md_notes_notes ns
                LEFT JOIN my_md_notes_contents nc ON ns.id = nc.ノートID
                WHERE ns.id = %s
            """, (note_id,))
        else:
            cursor.execute("""
                SELECT ns.*, nc.内容
                FROM my_md_notes_notes ns
                LEFT JOIN my_md_notes_contents nc ON ns.id = nc.ノートID
                WHERE ns.id = %s AND ns.オーナーID = %s
            """, (note_id, session['user_id']))
        note = cursor.fetchone()

        if not note:
            flash('ノートが見つからないか、編集権限がありません。', 'error')
            return redirect(url_for('my_md_notes.index'))

        if request.method == 'POST':
            # stay=1 は編集画面にとどまったままの「保存」ボタンからの要求。
            # 画面遷移しないので、リダイレクトやフラッシュではなくJSONで結果を返す。
            stay = request.form.get('stay') == '1'

            def fail(message):
                if stay:
                    return jsonify({'success': False, 'error': message}), 400
                flash(message, 'error')
                return redirect(url_for('my_md_notes.edit_note', note_id=note_id))

            name = request.form.get('name', '').strip()
            if not name:
                return fail('ノート名は必須です。')

            content = request.form.get('content', '')
            sequence = to_int(request.form.get('sequence'), 0)
            current_share_key = note['共有キー']
            new_share_key = normalize_share_key(request.form.get('share_key'), current_share_key)

            group_ids = [to_int(g) for g in request.form.getlist('access_groups')]
            group_ids = [g for g in group_ids if g > 0]
            if new_share_key in ('group', 'domestic_group') and not group_ids:
                return fail('グループ公開では、許可するグループを1つ以上選んでください。')

            try:
                current_time = get_jst_now()

                cursor.execute("""
                    UPDATE my_md_notes_notes
                    SET 名前 = %s, 序列 = %s, 共有キー = %s, 更新日時 = %s
                    WHERE id = %s
                """, (name, sequence, new_share_key, current_time, note_id))

                cursor.execute("""
                    UPDATE my_md_notes_contents
                    SET 内容 = %s, 更新日時 = %s
                    WHERE ノートID = %s
                """, (content, current_time, note_id))

                # 許可グループはコレポと同じ保存規則（全削除→再挿入）
                cursor.execute("DELETE FROM my_md_notes_access_groups WHERE ノートID = %s",
                               (note_id,))
                if new_share_key in ('group', 'domestic_group'):
                    for gid in group_ids:
                        cursor.execute("""
                            INSERT INTO my_md_notes_access_groups (ノートID, group_id)
                            VALUES (%s, %s)
                        """, (note_id, gid))

                conn.commit()

                if current_share_key != new_share_key:
                    message = (f'ノートを更新し、公開範囲を「{SHARE_LABELS[new_share_key]}」に'
                               '変更しました。')
                else:
                    message = 'ノートを更新しました。'

                if stay:
                    return jsonify({'success': True,
                                    'message': message,
                                    'updated_at': fmt_dt(current_time)})

                flash(message, 'success')
                return redirect(url_for('my_md_notes.view_note', note_id=note_id))

            except mysql.connector.Error as err:
                conn.rollback()
                return fail(f'更新中にエラーが発生しました: {err}')

        # GET
        cursor.execute("""
            SELECT group_id FROM my_md_notes_access_groups WHERE ノートID = %s
        """, (note_id,))
        note_group_ids = [row['group_id'] for row in cursor.fetchall()]

        return render_template('edit_note.html',
                               note=note,
                               all_groups=get_all_user_groups(),
                               note_group_ids=note_group_ids)

    except mysql.connector.Error as err:
        conn.rollback()
        flash(f'データベースエラーが発生しました: {err}', 'error')
        return redirect(url_for('my_md_notes.index'))
    finally:
        cursor.close()
        conn.close()


@my_md_notes_bp.route('/preview', methods=['POST'])
@login_required
@same_origin_required
def preview_markdown():
    """Markdownプレビュー"""
    data = request.get_json(silent=True) or {}
    markdown_text = data.get('markdown', '')
    user_category = session.get('user_category')

    try:
        html = process_markdown(markdown_text, user_category)
        return jsonify({'html': html})
    except Exception as e:
        print(f"[my_md_notes] preview error: {e}")
        return jsonify({'html': '<p style="color: red;">プレビューの生成に失敗しました。</p>'})


@my_md_notes_bp.route('/upload_image', methods=['POST'])
@login_required
@same_origin_required
def upload_image():
    """画像・PDFのアップロード（png / jpg / jpeg / svg / pdf、20MBまで）"""
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'ファイルが選択されていません'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'ファイルが選択されていません'}), 400

    if not allowed_file(file.filename):
        return jsonify({'success': False,
                        'error': '許可されていない形式です（png / jpg / jpeg / svg / pdf のみ）'}), 400

    ext = file.filename.rsplit('.', 1)[1].lower()

    # サイズ検証
    file.stream.seek(0, os.SEEK_END)
    size = file.stream.tell()
    file.stream.seek(0)
    if size == 0:
        return jsonify({'success': False, 'error': 'ファイルが空です'}), 400
    if size > MAX_UPLOAD_BYTES:
        limit_mb = MAX_UPLOAD_BYTES // (1024 * 1024)
        return jsonify({'success': False, 'error': f'ファイルサイズが上限（{limit_mb}MB）を超えています'}), 400

    # 内容検証（拡張子偽装の検出。SVGはテキスト形式なので先頭付近の <svg タグを見る）
    if ext == 'svg':
        head = file.stream.read(2048)
        file.stream.seek(0)
        if not svg_head_ok(head):
            return jsonify({'success': False, 'error': 'ファイルの内容が拡張子と一致しません'}), 400
    else:
        head = file.stream.read(8)
        file.stream.seek(0)
        if not magic_ok(head, ext):
            return jsonify({'success': False, 'error': 'ファイルの内容が拡張子と一致しません'}), 400

    try:
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)

        # 乱数8桁を先頭に付け、URLの総当たり推測を防ぐ（コレポと同じ規則）
        timestamp = get_jst_now().strftime('%Y%m%d_%H%M%S')
        filename = secure_filename(file.filename)
        name, _ = os.path.splitext(filename)
        if not name:
            name = 'file'
        unique_filename = f"{uuid.uuid4().hex[:8]}_{name}_{timestamp}.{ext}"

        filepath = os.path.join(UPLOAD_FOLDER, unique_filename)
        if ext == 'svg':
            # SVG はサニタイズしてから保存
            svg_data = _sanitize_svg(file.read())
            with open(filepath, 'wb') as f:
                f.write(svg_data)
        else:
            file.save(filepath)

        return jsonify({
            'success': True,
            'filename': unique_filename,
            'url': f"{UPLOAD_URL_PREFIX}/{unique_filename}",
            'kind': 'pdf' if ext == 'pdf' else 'image',
        })

    except Exception as e:
        print(f"[my_md_notes] upload error: {e}")
        return jsonify({'success': False, 'error': 'アップロードに失敗しました'}), 500


@my_md_notes_bp.route('/view_note/<int:note_id>')
@login_required
def view_note(note_id):
    user_category = session.get('user_category')
    conn = mysql.connector.connect(**DatabaseConfig.default())
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT ns.*, nc.内容
            FROM my_md_notes_notes ns
            LEFT JOIN my_md_notes_contents nc ON ns.id = nc.ノートID
            WHERE ns.id = %s
        """, (note_id,))
        note = cursor.fetchone()

        if not note:
            flash('ノートが見つかりません。', 'error')
            return redirect(url_for('my_md_notes.index'))

        if not can_view_note(note, session['user_id'], user_category):
            flash('このノートにアクセスする権限がありません。', 'error')
            return redirect(url_for('my_md_notes.index'))

        add_display_dates([note])
        html_content = process_markdown(note['内容'] or '', user_category)

        return render_template('view_note.html',
                               note=note,
                               html_content=html_content,
                               session=session,
                               user_category=user_category)
    except mysql.connector.Error as err:
        flash(f'データベースエラーが発生しました: {err}', 'error')
        return redirect(url_for('my_md_notes.index'))
    finally:
        cursor.close()
        conn.close()


@my_md_notes_bp.route('/view_markdown_note/<int:note_id>')
@login_required
def view_markdown_note(note_id):
    user_category = session.get('user_category')
    conn = mysql.connector.connect(**DatabaseConfig.default())
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT ns.*, nc.内容
            FROM my_md_notes_notes ns
            LEFT JOIN my_md_notes_contents nc ON ns.id = nc.ノートID
            WHERE ns.id = %s
        """, (note_id,))
        note = cursor.fetchone()

        if not note:
            flash('ノートが見つかりません。', 'error')
            return redirect(url_for('my_md_notes.index'))

        if not can_view_note(note, session['user_id'], user_category):
            flash('ノートが見つからないか、アクセス権限がありません。', 'error')
            return redirect(url_for('my_md_notes.index'))

        add_display_dates([note])
        return render_template('view_markdown_note.html', note=note)
    except mysql.connector.Error as err:
        flash(f'データベースエラーが発生しました: {err}', 'error')
        return redirect(url_for('my_md_notes.index'))
    finally:
        cursor.close()
        conn.close()


@my_md_notes_bp.route('/download_md/<int:note_id>')
@login_required
def download_md(note_id):
    """ソースMDファイルのダウンロード（閲覧できる人はダウンロードもできる）"""
    user_category = session.get('user_category')
    conn = mysql.connector.connect(**DatabaseConfig.default())
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT ns.*, nc.内容
            FROM my_md_notes_notes ns
            LEFT JOIN my_md_notes_contents nc ON ns.id = nc.ノートID
            WHERE ns.id = %s
        """, (note_id,))
        note = cursor.fetchone()

        if not note:
            flash('ノートが見つかりません。', 'error')
            return redirect(url_for('my_md_notes.index'))

        if not can_view_note(note, session['user_id'], user_category):
            flash('このノートにアクセスする権限がありません。', 'error')
            return redirect(url_for('my_md_notes.index'))

        # ノート名からファイル名を作る（パスに使えない文字は _ に）
        safe_name = re.sub(r'[\\/:*?"<>|\r\n]+', '_', note['名前'] or '').strip()
        if not safe_name:
            safe_name = f'note_{note_id}'
        download_name = f'{safe_name}.md'

        response = Response(note['内容'] or '', mimetype='text/markdown; charset=utf-8')
        # 日本語ファイル名は RFC 5987 の filename* で渡す（filename はASCIIの控え）
        response.headers['Content-Disposition'] = (
            f'attachment; filename="note_{note_id}.md"; '
            f"filename*=UTF-8''{quote(download_name)}"
        )
        return response
    except mysql.connector.Error as err:
        flash(f'データベースエラーが発生しました: {err}', 'error')
        return redirect(url_for('my_md_notes.index'))
    finally:
        cursor.close()
        conn.close()


@my_md_notes_bp.route('/download_html/<int:note_id>')
@login_required
def download_html(note_id):
    """HTMLファイルのダウンロード（閲覧できる人はダウンロードもできる）。

    本文を process_markdown でHTML化し、KaTeXの数式描画スクリプトを同梱した
    自己完結のHTML文書として返す。
    """
    user_category = session.get('user_category')
    conn = mysql.connector.connect(**DatabaseConfig.default())
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT ns.*, nc.内容
            FROM my_md_notes_notes ns
            LEFT JOIN my_md_notes_contents nc ON ns.id = nc.ノートID
            WHERE ns.id = %s
        """, (note_id,))
        note = cursor.fetchone()

        if not note:
            flash('ノートが見つかりません。', 'error')
            return redirect(url_for('my_md_notes.index'))

        if not can_view_note(note, session['user_id'], user_category):
            flash('このノートにアクセスする権限がありません。', 'error')
            return redirect(url_for('my_md_notes.index'))

        title = note['名前'] or f'note_{note_id}'
        body_html = process_markdown(note['内容'] or '', user_category)

        # 閲覧画面と同じ枠組み（.content-container > .content-area）に収めて返す。
        # 枠を合わせないと、表・引用・コードブロックに素のブラウザ既定スタイルが
        # 当たって崩れる。
        #
        # KaTeX の区切り指定は、生成されるJavaScript側で '\\[' の2文字になる必要がある。
        # Python の通常文字列で '\\[' と書くと出力が '\[' になり、JavaScript では
        # 単なる '[' として解釈される。すると本文中の [ ... ] が数式と見なされて
        # 置換され、mermaid のソースが壊れて "Syntax error in text" になる。
        # そのため、この部分はraw文字列で組み立てる。
        # 併せて ignoredClasses で mermaid のソースを走査対象から外す。
        final_html = ("""<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>""" + html.escape(title) + """</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.9/katex.min.css">
    <style>""" + DOWNLOAD_HTML_CSS + """</style>
</head>
<body>
    <div class="content-container">
        <div class="content-area">
""" + body_html + """
        </div>
    </div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.9/katex.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.9/contrib/auto-render.min.js"></script>
<script>
document.addEventListener("DOMContentLoaded", function() {
    renderMathInElement(document.querySelector('.content-area'), {
        delimiters: [
            {left: '$$', right: '$$', display: true},
            {left: '$', right: '$', display: false},
            {left: '\\\\[', right: '\\\\]', display: true},
            {left: '\\\\(', right: '\\\\)', display: false}
        ],
        ignoredTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code', 'option'],
        ignoredClasses: ['mermaid'],
        throwOnError: false
    });
});
</script>
</body>
</html>""")

        # ノート名からファイル名を作る（パスに使えない文字は _ に）
        safe_name = re.sub(r'[\\/:*?"<>|\r\n]+', '_', note['名前'] or '').strip()
        if not safe_name:
            safe_name = f'note_{note_id}'
        download_name = f'{safe_name}.html'

        response = Response(final_html, mimetype='text/html; charset=utf-8')
        response.headers['Content-Disposition'] = (
            f'attachment; filename="note_{note_id}.html"; '
            f"filename*=UTF-8''{quote(download_name)}"
        )
        return response
    except mysql.connector.Error as err:
        flash(f'データベースエラーが発生しました: {err}', 'error')
        return redirect(url_for('my_md_notes.index'))
    finally:
        cursor.close()
        conn.close()


@my_md_notes_bp.route('/archive_note/<int:note_id>', methods=['POST'])
@login_required
def archive_note(note_id):
    """ノートを文書アーカイブ（document_archive）に保存する（所有者とadminのみ）。

    コレポの archive_project と同じ規則：
      - HTMLに変換した完成稿を public_documents に登録する
      - 登録時の access_policy は 'private'。インターネットへの一般公開は
        文書アーカイブ側で公開範囲を変更して行う
    """
    if not origin_ok():
        flash('不正な要求元です。', 'error')
        return redirect(url_for('my_md_notes.index'))

    user_category = session.get('user_category')
    conn = mysql.connector.connect(**DatabaseConfig.default())
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT ns.*, nc.内容
            FROM my_md_notes_notes ns
            LEFT JOIN my_md_notes_contents nc ON ns.id = nc.ノートID
            WHERE ns.id = %s
        """, (note_id,))
        note = cursor.fetchone()

        if not note:
            flash('ノートが見つかりません。', 'error')
            return redirect(url_for('my_md_notes.index'))

        if not (user_category == 'admin' or note['オーナーID'] == session['user_id']):
            flash('このノートをアーカイブする権限がありません。', 'error')
            return redirect(url_for('my_md_notes.index'))

        title = request.form.get('archive_title', '').strip()
        public_description = request.form.get('public_description', '')
        owner_memo = request.form.get('owner_memo', '')

        if not title:
            flash('アーカイブタイトルは必須です。', 'error')
            return redirect(url_for('my_md_notes.index'))

        body_html = process_markdown(note['内容'] or '', user_category)

        # 最小限のHTMLドキュメント構造に整形（コレポの generate_complete_archive_html と同形）
        final_html = """<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>""" + title + """</title>
</head>
<body>
    """ + body_html + """
</body>
</html>"""

        success, message = save_to_archive(
            title=title,
            public_description=public_description,
            owner_memo=owner_memo,
            html_content=final_html,
        )

        if success:
            flash(f'ノート「{note["名前"]}」をアーカイブに保存しました（{message}）。'
                  'インターネットへの公開は文書アーカイブで公開範囲を設定してください。', 'success')
        else:
            flash(f'アーカイブ保存中にエラーが発生しました: {message}', 'error')

        return redirect(url_for('my_md_notes.index'))
    except mysql.connector.Error as err:
        flash(f'データベースエラーが発生しました: {err}', 'error')
        return redirect(url_for('my_md_notes.index'))
    finally:
        cursor.close()
        conn.close()


def save_to_archive(title, public_description, owner_memo, html_content):
    """文書アーカイブ（public_documents）に保存する。
    コレポの save_to_colrep_archive と同じテーブル・同じ既定値（access_policy='private'）。"""
    try:
        connection = mysql.connector.connect(**DatabaseConfig.fujinp())
        cursor = connection.cursor()

        formatted_now = get_jst_now().strftime('%Y-%m-%d %H:%M:%S')

        cursor.execute("""
            INSERT INTO public_documents
            (title, public_description, owner_memo, content,
             created_by, created_at, updated_at, access_policy)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (title, public_description, owner_memo, html_content,
              session.get('user_id'), formatted_now, formatted_now, 'private'))
        connection.commit()
        doc_id = cursor.lastrowid

        cursor.close()
        connection.close()

        print(f"[my_md_notes] ノートをアーカイブに保存しました (ID: {doc_id}): {title}")
        return True, f"ID:{doc_id} として保存されました"
    except Exception as e:
        print(f"[my_md_notes] アーカイブ保存エラー: {e}")
        return False, str(e)


@my_md_notes_bp.route('/delete_note/<int:note_id>', methods=['POST'])
@login_required
@same_origin_required
def delete_note(note_id):
    """ノート削除（管理者と所有者のみ）"""
    user_category = session.get('user_category')
    conn = None

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        if user_category == 'admin':
            cursor.execute("SELECT id, 名前 FROM my_md_notes_notes WHERE id = %s", (note_id,))
        else:
            cursor.execute("""
                SELECT id, 名前 FROM my_md_notes_notes
                WHERE id = %s AND オーナーID = %s
            """, (note_id, session['user_id']))

        note = cursor.fetchone()
        if not note:
            return jsonify({'success': False, 'error': '権限がありません'}), 403

        cursor.execute("DELETE FROM my_md_notes_access_groups WHERE ノートID = %s", (note_id,))
        cursor.execute("DELETE FROM my_md_notes_contents WHERE ノートID = %s", (note_id,))
        cursor.execute("DELETE FROM my_md_notes_notes WHERE id = %s", (note_id,))

        conn.commit()

        return jsonify({'success': True, 'message': f'ノート「{note["名前"]}」を削除しました'})

    except mysql.connector.Error as err:
        if conn:
            conn.rollback()
        return jsonify({'success': False, 'error': str(err)}), 500
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()


# =============================================================================
# コンテンツ移行（JSONエクスポート／インポート。管理者のみ）
# =============================================================================

ATTACHMENT_REF_RE = re.compile(re.escape(UPLOAD_URL_PREFIX) + r'/([A-Za-z0-9._-]+)')


def dt_to_str(value):
    """DATETIME(JST) を移行用の文字列（秒まで）にする"""
    if not value:
        return ''
    if isinstance(value, str):
        return value[:19]
    try:
        return value.strftime(DT_FORMAT)
    except AttributeError:
        return str(value)[:19]


def str_to_dt(value, default):
    """移行用の日時文字列を datetime に戻す。不正・空欄は default"""
    if not value:
        return default
    for fmt in (DT_FORMAT, '%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(str(value)[:19], fmt)
        except ValueError:
            continue
    return default


def find_attachment_names(contents):
    """本文群から /static/mdimgs/ 配下のファイル名を集める（重複除去・出現順）"""
    seen = []
    for text in contents:
        for m in ATTACHMENT_REF_RE.finditer(text or ''):
            name = m.group(1)
            if name not in seen:
                seen.append(name)
    return seen


def safe_attachment_name(name):
    """添付ファイル名として受け入れてよいか（パス要素なし・許可拡張子）"""
    if not name or name != secure_filename(name):
        return False
    return allowed_file(name)


@my_md_notes_bp.route('/export_json')
@login_required
@admin_only
def export_json():
    """全ノートをJSONにエクスポートする（管理者のみ）。

    含めるもの：ノート本体（名前・公開範囲・序列・作成／更新日時）、本文、
    所有者（email／full_name）、許可グループ（name）、
    attachments=1 のとき本文が参照する添付ファイル（base64）。
    """
    include_attachments = request.args.get('attachments', '1') != '0'
    site_url = request.host_url.rstrip('/')

    conn = mysql.connector.connect(**DatabaseConfig.default())
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT ns.id, ns.オーナーID, ns.名前, ns.共有キー, ns.序列,
                   ns.作成日時, ns.更新日時, nc.内容,
                   u.email AS owner_email, u.full_name AS owner_name
            FROM my_md_notes_notes ns
            LEFT JOIN my_md_notes_contents nc ON ns.id = nc.ノートID
            LEFT JOIN users u ON ns.オーナーID = u.id
            ORDER BY ns.id
        """)
        rows = cursor.fetchall()

        cursor.execute("""
            SELECT ag.ノートID, ag.group_id, g.name
            FROM my_md_notes_access_groups ag
            LEFT JOIN user_groups g ON ag.group_id = g.id
            ORDER BY ag.ノートID, ag.group_id
        """)
        groups_by_note = {}
        for r in cursor.fetchall():
            groups_by_note.setdefault(r['ノートID'], []).append(
                {'source_id': r['group_id'], 'name': r['name'] or ''})

        cursor.execute("SELECT email, full_name FROM users WHERE id = %s", (session['user_id'],))
        me = cursor.fetchone() or {}

        notes = []
        for r in rows:
            notes.append({
                'source_id': r['id'],
                '名前': r['名前'],
                '共有キー': r['共有キー'],
                '序列': r['序列'],
                '作成日時': dt_to_str(r['作成日時']),
                '更新日時': dt_to_str(r['更新日時']),
                'owner': {
                    'source_id': r['オーナーID'],
                    'email': r['owner_email'] or '',
                    'full_name': r['owner_name'] or '',
                },
                'access_groups': groups_by_note.get(r['id'], []),
                '内容': r['内容'] or '',
            })

        attachments = []
        missing = []
        if include_attachments:
            for name in find_attachment_names(n['内容'] for n in notes):
                path = os.path.join(UPLOAD_FOLDER, name)
                if not safe_attachment_name(name) or not os.path.isfile(path):
                    missing.append(name)
                    continue
                with open(path, 'rb') as f:
                    data = f.read()
                attachments.append({
                    'filename': name,
                    'size': len(data),
                    'data_base64': base64.b64encode(data).decode('ascii'),
                })

        payload = {
            'export_type': EXPORT_TYPE,
            'format_version': EXPORT_FORMAT_VERSION,
            'app_name': 'my_md_notes',
            'site_url': site_url,
            'exported_at': get_jst_now().strftime(DT_FORMAT),
            'exported_by': {'email': me.get('email', ''), 'full_name': me.get('full_name', '')},
            'note_count': len(notes),
            'attachment_count': len(attachments),
            'attachments_missing': missing,
            'notes': notes,
            'attachments': attachments,
        }

        body = json.dumps(payload, ensure_ascii=False, indent=1)
        stamp = get_jst_now().strftime('%Y%m%d_%H%M%S')
        download_name = f'my_md_notes_content_{stamp}.json'
        response = Response(body, mimetype='application/json; charset=utf-8')
        response.headers['Content-Disposition'] = f'attachment; filename="{download_name}"'
        return response
    except mysql.connector.Error as err:
        flash(f'データベースエラーが発生しました: {err}', 'error')
        return redirect(url_for('my_md_notes.index'))
    finally:
        cursor.close()
        conn.close()


@my_md_notes_bp.route('/import_json', methods=['POST'])
@login_required
@admin_only
def import_json():
    """export_json 形式のJSONからノートを取り込む（管理者のみ）。

    フォーム項目：
      file          - JSONファイル
      mode          - skip      : 同じ所有者・同名のノートがあれば取り込まない（既定・再実行しても増えない）
                      overwrite : 同じ所有者・同名のノートがあれば本文などを上書きする
                      add       : 既存を気にせず常に新規追加する
      rewrite_urls  - '1' のとき、本文中の添付URLのホストを移行元(site_url)から当サイトへ書き換える
      attachments   - '1' のとき、同梱の添付ファイルを当サイトの mdimgs/ に復元する（同名があれば残す）

    引き当て規則：
      所有者   - email で users を引く。見つからなければ実行した管理者を所有者にする
      グループ - name で user_groups を引く。1つも引けなかった group／domestic_group は private に落とす
    DBへの反映は1トランザクションで、途中で失敗したら何も残さない。
    """
    if not origin_ok():
        flash('不正な要求元です。', 'error')
        return redirect(url_for('my_md_notes.index'))

    file = request.files.get('file')
    if not file or file.filename == '':
        flash('JSONファイルが選択されていません。', 'error')
        return redirect(url_for('my_md_notes.index'))

    mode = request.form.get('mode', 'skip')
    if mode not in IMPORT_MODES:
        mode = 'skip'
    rewrite_urls = request.form.get('rewrite_urls') == '1'
    restore_attachments = request.form.get('attachments') == '1'

    try:
        payload = json.loads(file.read().decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as e:
        flash(f'JSONを読み取れませんでした: {e}', 'error')
        return redirect(url_for('my_md_notes.index'))

    if not isinstance(payload, dict) or payload.get('export_type') != EXPORT_TYPE:
        flash('マイノートのエクスポート形式ではありません（export_type が一致しません）。', 'error')
        return redirect(url_for('my_md_notes.index'))
    if payload.get('format_version', 0) > EXPORT_FORMAT_VERSION:
        flash('このファイルはより新しい形式です。アプリを更新してから取り込んでください。', 'error')
        return redirect(url_for('my_md_notes.index'))

    notes = payload.get('notes') or []
    src_site_url = (payload.get('site_url') or '').rstrip('/')
    dst_site_url = request.host_url.rstrip('/')
    me_id = session['user_id']

    counts = {'added': 0, 'overwritten': 0, 'skipped': 0}
    owner_fallback = []     # (ノート名, email)
    group_downgraded = []   # ノート名
    group_partial = []      # (ノート名, [未一致グループ名])

    conn = mysql.connector.connect(**DatabaseConfig.default())
    cursor = conn.cursor(dictionary=True)
    try:
        # 引き当て表
        cursor.execute("SELECT id, email FROM users WHERE deleted_at IS NULL")
        user_by_email = {r['email']: r['id'] for r in cursor.fetchall() if r['email']}
        cursor.execute("SELECT id, name FROM user_groups")
        group_by_name = {r['name']: r['id'] for r in cursor.fetchall() if r['name']}

        now = get_jst_now()

        for n in notes:
            if not isinstance(n, dict):
                continue
            name = (n.get('名前') or '').strip() or '無題'
            owner = n.get('owner') or {}
            email = owner.get('email') or ''
            owner_id = user_by_email.get(email)
            owner_missing = owner_id is None
            if owner_missing:
                owner_id = me_id

            share_key = normalize_share_key(n.get('共有キー'), 'private')
            group_ids = []
            unmatched = []
            downgraded = False
            if share_key in ('group', 'domestic_group'):
                for g in n.get('access_groups') or []:
                    gname = (g or {}).get('name') or ''
                    gid = group_by_name.get(gname)
                    if gid is not None and gid not in group_ids:
                        group_ids.append(gid)
                    else:
                        unmatched.append(gname or '(名称なし)')
                if not group_ids:
                    share_key = 'private'
                    downgraded = True

            sequence = to_int(n.get('序列'), 0)
            created_at = str_to_dt(n.get('作成日時'), now)
            updated_at = str_to_dt(n.get('更新日時'), created_at)
            content = n.get('内容') or ''
            if rewrite_urls and src_site_url and src_site_url != dst_site_url:
                content = content.replace(src_site_url + UPLOAD_URL_PREFIX + '/',
                                          dst_site_url + UPLOAD_URL_PREFIX + '/')

            existing_id = None
            if mode in ('skip', 'overwrite'):
                cursor.execute("""
                    SELECT id FROM my_md_notes_notes
                    WHERE オーナーID = %s AND 名前 = %s
                    ORDER BY id LIMIT 1
                """, (owner_id, name))
                row = cursor.fetchone()
                existing_id = row['id'] if row else None

            if existing_id is not None and mode == 'skip':
                counts['skipped'] += 1
                continue

            # ここから先は実際に書き込むノート。引き当ての結果を報告用に記録する
            if owner_missing:
                owner_fallback.append((name, email))
            if downgraded:
                group_downgraded.append(name)
            elif unmatched:
                group_partial.append((name, unmatched))

            if existing_id is not None and mode == 'overwrite':
                note_id = existing_id
                cursor.execute("""
                    UPDATE my_md_notes_notes
                    SET 共有キー = %s, 序列 = %s, 作成日時 = %s, 更新日時 = %s
                    WHERE id = %s
                """, (share_key, sequence, created_at, updated_at, note_id))
                cursor.execute("""
                    UPDATE my_md_notes_contents
                    SET 内容 = %s, 作成日時 = %s, 更新日時 = %s
                    WHERE ノートID = %s
                """, (content, created_at, updated_at, note_id))
                if cursor.rowcount == 0:
                    cursor.execute("""
                        INSERT INTO my_md_notes_contents (ノートID, 内容, 作成日時, 更新日時)
                        VALUES (%s, %s, %s, %s)
                    """, (note_id, content, created_at, updated_at))
                cursor.execute("DELETE FROM my_md_notes_access_groups WHERE ノートID = %s", (note_id,))
                counts['overwritten'] += 1
            else:
                cursor.execute("""
                    INSERT INTO my_md_notes_notes (オーナーID, 名前, 共有キー, 序列, 作成日時, 更新日時)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (owner_id, name, share_key, sequence, created_at, updated_at))
                note_id = cursor.lastrowid
                cursor.execute("""
                    INSERT INTO my_md_notes_contents (ノートID, 内容, 作成日時, 更新日時)
                    VALUES (%s, %s, %s, %s)
                """, (note_id, content, created_at, updated_at))
                counts['added'] += 1

            for gid in group_ids:
                cursor.execute("""
                    INSERT INTO my_md_notes_access_groups (ノートID, group_id) VALUES (%s, %s)
                """, (note_id, gid))

        conn.commit()
    except Exception as err:
        conn.rollback()
        flash(f'取り込み中にエラーが発生したため、何も反映していません: {err}', 'error')
        return redirect(url_for('my_md_notes.index'))
    finally:
        cursor.close()
        conn.close()

    # 添付ファイルの復元（DB反映後。既存の同名ファイルは残す）
    att_restored = 0
    att_kept = 0
    att_rejected = []
    if restore_attachments:
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        for a in payload.get('attachments') or []:
            fname = (a or {}).get('filename') or ''
            if not safe_attachment_name(fname):
                att_rejected.append(fname or '(名称なし)')
                continue
            path = os.path.join(UPLOAD_FOLDER, fname)
            if os.path.exists(path):
                att_kept += 1
                continue
            try:
                data = base64.b64decode(a.get('data_base64') or '')
            except (ValueError, TypeError):
                att_rejected.append(fname)
                continue
            ext = fname.rsplit('.', 1)[1].lower()
            if ext == 'svg':
                if not svg_head_ok(data[:2048]):
                    att_rejected.append(fname)
                    continue
                data = _sanitize_svg(data)
            elif not magic_ok(data[:8], ext):
                att_rejected.append(fname)
                continue
            with open(path, 'wb') as f:
                f.write(data)
            att_restored += 1

    # 結果報告
    lines = [f'JSONの取り込みが完了しました（対象 {len(notes)} 件）。',
             f'追加 {counts["added"]} 件、上書き {counts["overwritten"]} 件、'
             f'スキップ {counts["skipped"]} 件。']
    if restore_attachments:
        lines.append(f'添付ファイル：復元 {att_restored} 件、既存のため温存 {att_kept} 件。')
        if att_rejected:
            lines.append('添付ファイルのうち不正なため復元しなかったもの：' + '、'.join(att_rejected[:10])
                         + ('…' if len(att_rejected) > 10 else ''))
    if rewrite_urls and src_site_url and src_site_url != dst_site_url:
        lines.append(f'本文中の添付URLのホストを {src_site_url} → {dst_site_url} に書き換えました。')
    if owner_fallback:
        shown = '、'.join(f'「{n}」({e or "email不明"})' for n, e in owner_fallback[:10])
        lines.append(f'所有者が当サイトに見つからないため実行者を所有者にしたノート {len(owner_fallback)} 件：'
                     + shown + ('…' if len(owner_fallback) > 10 else ''))
    if group_downgraded:
        lines.append(f'許可グループが1つも引き当てられず非公開にしたノート {len(group_downgraded)} 件：'
                     + '、'.join(f'「{n}」' for n in group_downgraded[:10])
                     + ('…' if len(group_downgraded) > 10 else ''))
    if group_partial:
        shown = '、'.join(f'「{n}」({"/".join(g)})' for n, g in group_partial[:10])
        lines.append(f'一部の許可グループが引き当てられなかったノート {len(group_partial)} 件：' + shown
                     + ('…' if len(group_partial) > 10 else ''))
    flash('\n'.join(lines), 'success')
    return redirect(url_for('my_md_notes.index'))


@my_md_notes_bp.route('/search')
@login_required
def search_notes():
    """ノート検索（ノート名の部分一致。可視範囲は一覧と同じ）"""
    user_category = session.get('user_category')
    keyword = request.args.get('keyword', '').strip()
    sort_by = request.args.get('sort_by', 'updated_desc')
    return_to = request.args.get('return_to') or url_for('auth.redirect_to_dashboard')

    order_clauses = {
        'updated_asc': 'ORDER BY ns.更新日時 ASC',
        'sequence_asc': 'ORDER BY ns.序列 ASC, ns.更新日時 DESC',
        'sequence_desc': 'ORDER BY ns.序列 DESC, ns.更新日時 DESC',
        'updated_desc': 'ORDER BY ns.更新日時 DESC',
    }
    if sort_by not in order_clauses:
        sort_by = 'updated_desc'
    order_clause = order_clauses[sort_by]

    conn = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        if user_category == 'admin':
            base = f"""
                SELECT ns.*, u.full_name AS オーナー名, 'admin' AS 権限
                FROM my_md_notes_notes ns
                LEFT JOIN users u ON ns.オーナーID = u.id
                {{where}}
                {order_clause}
            """
            if keyword:
                cursor.execute(base.format(where='WHERE ns.名前 LIKE %s'), (f'%{keyword}%',))
            else:
                cursor.execute(base.format(where=''))
        else:
            uid = session['user_id']
            group_ids = get_user_active_group_ids(uid)
            if group_ids:
                placeholders = ', '.join(['%s'] * len(group_ids))
                visible_clause = f"""(ns.オーナーID = %s
                       OR (ns.共有キー IN ('group', 'domestic_group')
                           AND ns.id IN (SELECT ノートID FROM my_md_notes_access_groups
                                         WHERE group_id IN ({placeholders}))))"""
                params = [uid, uid] + group_ids
            else:
                visible_clause = "ns.オーナーID = %s"
                params = [uid, uid]
            base = f"""
                SELECT DISTINCT ns.*, u.full_name AS オーナー名,
                    CASE WHEN ns.オーナーID = %s THEN '所有者' ELSE '閲覧' END AS 権限
                FROM my_md_notes_notes ns
                LEFT JOIN users u ON ns.オーナーID = u.id
                WHERE {visible_clause}{{keyword_clause}}
                {order_clause}
            """
            if keyword:
                cursor.execute(base.format(keyword_clause=' AND ns.名前 LIKE %s'),
                               tuple(params + [f'%{keyword}%']))
            else:
                cursor.execute(base.format(keyword_clause=''), tuple(params))

        notes = add_display_dates(cursor.fetchall())

        return render_template('search_notes.html',
                               notes=notes,
                               keyword=keyword,
                               sort_by=sort_by,
                               return_to=return_to,
                               user_category=user_category,
                               session=session)

    except mysql.connector.Error as err:
        flash(f'データベースエラーが発生しました: {err}', 'error')
        return redirect(url_for('my_md_notes.index'))
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()