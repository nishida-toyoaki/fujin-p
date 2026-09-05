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

# 保護領域（添付の原本。アプリディレクトリ配下・実行時に自動生成・配布対象外）★4.0
#   添付は既定でここに置き、/my_md_notes/file/<id> で配信する。配信のたびに
#   ノートの公開範囲を判定するので、ノートの公開範囲を変えれば添付のアクセス権も
#   自動で追随する。本文にはリンク [名前](/my_md_notes/file/<id>) だけを書く。
#   画像として表示したいとき、PDFなどをノートの外へ渡したいときは、ユーザが
#   「公開」操作で static/mdimgs/ に複製を作る（えふえふ＝fujin_forum と同じ規則）。
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
FILES_DIR = os.path.join(DATA_DIR, 'files')
PROTECTED_URL_PREFIX = '/my_md_notes/file'

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'svg', 'pdf'}
# 公開したときの本文での書き方を分ける（画像は <img>、それ以外は公開リンク）
IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'svg'}
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
#   4.0 で添付が保護領域に移ったため、形式は 2 になった（1 も取り込める）。
EXPORT_FORMAT_VERSION = 2
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
        .content-area a.mn-att { display: inline-block; background: #f3f4f6; border: 1px solid #e2e8f0;
            border-radius: 6px; padding: 2px 8px; margin: 2px 0; text-decoration: none; color: #2d3748; }
        .content-area a.mn-pub { display: inline-block; background: #ecfdf5; border: 1px solid #a7f3d0;
            border-radius: 6px; padding: 2px 8px; margin: 2px 0; text-decoration: none; color: #065f46; }

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


def can_edit_note(note, user_id, user_category):
    """ノートを編集できるのは所有者とadminだけ（添付の公開／非公開もこれに従う）"""
    return user_category == 'admin' or note['オーナーID'] == user_id


# =============================================================================
# 添付ファイル（保護領域と公開複製）★4.0
# =============================================================================

PROTECTED_IMG_RE = re.compile(
    r'<img\b[^>]*?\bsrc\s*=\s*["\']([^"\']*?' + re.escape(PROTECTED_URL_PREFIX) + r'/(\d+)[^"\']*)["\'][^>]*>',
    re.IGNORECASE)

PROTECTED_REF_RE = re.compile(
    r'(?:https?://[^/\s"\')]+)?' + re.escape(PROTECTED_URL_PREFIX) + r'/(\d+)')

DATA_ATT_RE = re.compile(r'\bdata-att\s*=\s*["\'](\d+)["\']')


def attachment_kind(name, mimetype=None):
    """公開したときの本文での書き方（画像は <img>、それ以外は公開リンク）"""
    name = name or ''
    ext = name.rsplit('.', 1)[1].lower() if '.' in name else ''
    if ext in IMAGE_EXTENSIONS or (mimetype or '').startswith('image/'):
        return 'image'
    return 'file'


def guard_protected_images(html_text):
    """保護添付（/my_md_notes/file/…）を指す <img> はリンクに変える。

    保護領域のファイルは画像として表示せず、必ずリンクで渡す（えふえふと同じ規則）。
    画像として見せたいときは、編集画面の「添付を公開」で公開複製を作る。
    """
    def repl(m):
        src = m.group(1)
        alt = re.search(r'\balt\s*=\s*["\']([^"\']*)["\']', m.group(0))
        label = (alt.group(1) if alt and alt.group(1) else f'添付 {m.group(2)}')
        return f'<a href="{html.escape(src)}" class="mn-att">📎 {html.escape(label)}</a>'
    return PROTECTED_IMG_RE.sub(repl, html_text)


def render_note_html(markdown_text, user_category):
    """本文のHTML化（保護添付のガードまで込み）"""
    return guard_protected_images(process_markdown(markdown_text or '', user_category))


def load_attachment(cursor, aid):
    cursor.execute("SELECT * FROM my_md_notes_attachments WHERE id = %s", (aid,))
    return cursor.fetchone()


def load_note_row(cursor, note_id):
    cursor.execute("SELECT * FROM my_md_notes_notes WHERE id = %s", (note_id,))
    return cursor.fetchone()


def unique_stored_name(original_name):
    """保存名（乱数8桁を先頭に付けてURLの総当たり推測を防ぐ）"""
    ext = original_name.rsplit('.', 1)[1].lower() if '.' in original_name else ''
    base, _ = os.path.splitext(secure_filename(original_name))
    return f"{uuid.uuid4().hex[:8]}_{base or 'file'}" + (f'.{ext}' if ext else '')


def store_protected(cursor, note_id, original_name, data, mimetype, uploaded_by):
    """保護領域にファイルを置き、台帳（my_md_notes_attachments）に1行作る"""
    rel = os.path.join(str(note_id), unique_stored_name(original_name))
    abs_path = os.path.join(FILES_DIR, rel)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, 'wb') as f:
        f.write(data)
    cursor.execute("""
        INSERT INTO my_md_notes_attachments
            (ノートID, name, mimetype, size, local_path, public_path, uploaded_by, 作成日時)
        VALUES (%s, %s, %s, %s, %s, NULL, %s, %s)
    """, (note_id, (original_name or 'file')[:500], (mimetype or '')[:100], len(data),
          rel.replace('\\', '/'), uploaded_by, get_jst_now()))
    aid = cursor.lastrowid
    return {'id': aid, 'name': original_name,
            'url': url_for('my_md_notes.serve_file', aid=aid)}


def protected_abs_path(local_path):
    """台帳の local_path を絶対パスにする（保護領域の外を指していたら None）"""
    if not local_path:
        return None
    path = os.path.normpath(os.path.join(FILES_DIR, local_path))
    if not path.startswith(os.path.normpath(FILES_DIR)):
        return None
    return path


def public_abs_path(public_path):
    """公開複製のURLから実ファイルの絶対パスを求める（公開領域の外なら None）"""
    if not public_path or not public_path.startswith(UPLOAD_URL_PREFIX + '/'):
        return None
    path = os.path.normpath(os.path.join(UPLOAD_FOLDER, public_path[len(UPLOAD_URL_PREFIX) + 1:]))
    if not path.startswith(os.path.normpath(UPLOAD_FOLDER)):
        return None
    return path


def make_public_copy(src_path, original_name, fixed_name=None):
    """公開領域に複製を作り、その URL を返す"""
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    unique = fixed_name or unique_stored_name(original_name or 'file')
    with open(src_path, 'rb') as rf, open(os.path.join(UPLOAD_FOLDER, unique), 'wb') as wf:
        wf.write(rf.read())
    return f"{UPLOAD_URL_PREFIX}/{unique}"


def remove_public_copy(public_path):
    path = public_abs_path(public_path)
    if path and os.path.isfile(path):
        try:
            os.remove(path)
        except OSError as e:
            print(f"[my_md_notes] 公開複製の削除に失敗: {e}")


def absolutize_protected_refs(text):
    """保護添付への参照をフルURLにする（ダウンロードした先でも開けるように）"""
    base = request.host_url.rstrip('/')
    return PROTECTED_REF_RE.sub(
        lambda m: f"{base}{PROTECTED_URL_PREFIX}/{m.group(1)}", text)


def publish_note_attachments(conn, cursor, note, markdown_text):
    """本文が参照する保護添付をすべて公開し、本文の参照を公開URLに置き換える。★4.0

    アーカイブ（＝インターネット公開の経路）へ出すときに使う。戻り値は
    （置き換えた本文, 公開したファイル名のリスト）。
    """
    ids = {int(x) for x in PROTECTED_REF_RE.findall(markdown_text or '')}
    ids |= {int(x) for x in DATA_ATT_RE.findall(markdown_text or '')}
    if not ids:
        return markdown_text, []

    base = request.host_url.rstrip('/')
    published = []
    url_by_id = {}
    for aid in sorted(ids):
        a = load_attachment(cursor, aid)
        if not a or a.get('ノートID') != note['id']:
            continue
        pub = a.get('public_path')
        if not pub:
            src = protected_abs_path(a.get('local_path'))
            if not src or not os.path.isfile(src):
                continue
            pub = make_public_copy(src, a.get('name'))
            cursor.execute("UPDATE my_md_notes_attachments SET public_path = %s WHERE id = %s",
                           (pub, aid))
            published.append(a.get('name') or f'添付 {aid}')
        url_by_id[aid] = base + pub
    if published:
        conn.commit()

    def repl(m):
        return url_by_id.get(int(m.group(1)), m.group(0))

    return PROTECTED_REF_RE.sub(repl, markdown_text or ''), published


def delete_note_attachments(cursor, note_id):
    """ノートに属する添付の実体（原本と公開複製）と台帳の行を消す"""
    cursor.execute("SELECT * FROM my_md_notes_attachments WHERE ノートID = %s", (note_id,))
    rows = cursor.fetchall()
    for a in rows:
        path = protected_abs_path(a.get('local_path'))
        if path and os.path.isfile(path):
            try:
                os.remove(path)
            except OSError as e:
                print(f"[my_md_notes] 添付原本の削除に失敗: {e}")
        remove_public_copy(a.get('public_path'))
    cursor.execute("DELETE FROM my_md_notes_attachments WHERE ノートID = %s", (note_id,))
    folder = os.path.join(FILES_DIR, str(note_id))
    if os.path.isdir(folder):
        try:
            os.rmdir(folder)
        except OSError:
            pass
    return len(rows)


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
        return jsonify({'html': render_note_html(markdown_text, user_category)})
    except Exception as e:
        print(f"[my_md_notes] preview error: {e}")
        return jsonify({'html': '<p style="color: red;">プレビューの生成に失敗しました。</p>'})


@my_md_notes_bp.route('/upload_image', methods=['POST'])
@login_required
@same_origin_required
def upload_image():
    """添付のアップロード（png / jpg / jpeg / svg / pdf、20MBまで）。★4.0

    保護領域に置き、本文に書くリンク [名前](/my_md_notes/file/<id>) を返す。
    query / form: note=<ノートID>（編集できるノート）
    """
    note_id = request.args.get('note', type=int) or request.form.get('note', type=int)
    if not note_id:
        return jsonify({'success': False, 'error': 'ノートが指定されていません'}), 400

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

    data = file.read()
    if ext == 'svg':
        # SVG はサニタイズしてから保存
        data = _sanitize_svg(data)

    conn = mysql.connector.connect(**DatabaseConfig.default())
    cursor = conn.cursor(dictionary=True)
    try:
        note = load_note_row(cursor, note_id)
        if not note:
            return jsonify({'success': False, 'error': 'ノートが見つかりません'}), 404
        if not can_edit_note(note, session['user_id'], session.get('user_category')):
            return jsonify({'success': False, 'error': 'このノートに添付する権限がありません'}), 403

        r = store_protected(cursor, note_id, file.filename, data, file.mimetype,
                            session['user_id'])
        conn.commit()
        return jsonify({
            'success': True,
            'id': r['id'],
            'filename': r['name'],
            'url': r['url'],
            'kind': 'pdf' if ext == 'pdf' else 'image',
        })

    except Exception as e:
        conn.rollback()
        print(f"[my_md_notes] upload error: {e}")
        return jsonify({'success': False, 'error': 'アップロードに失敗しました'}), 500
    finally:
        cursor.close()
        conn.close()


@my_md_notes_bp.route('/file/<int:aid>')
@login_required
def serve_file(aid):
    """保護添付の配信。配信のたびにノートの公開範囲を判定する。★4.0"""
    from flask import send_file, abort
    conn = mysql.connector.connect(**DatabaseConfig.default())
    cursor = conn.cursor(dictionary=True)
    try:
        a = load_attachment(cursor, aid)
        if not a or not a.get('local_path'):
            abort(404)
        note = load_note_row(cursor, a['ノートID']) if a.get('ノートID') else None
        if not note:
            abort(404)
        if not can_view_note(note, session['user_id'], session.get('user_category')):
            abort(403)
    finally:
        cursor.close()
        conn.close()

    path = protected_abs_path(a['local_path'])
    if not path or not os.path.isfile(path):
        abort(404)
    mt = a.get('mimetype') or 'application/octet-stream'
    inline = (mt.startswith('image/') and 'svg' not in mt) or mt == 'application/pdf'
    return send_file(path, mimetype=mt, as_attachment=not inline,
                     download_name=a.get('name') or f'file{aid}')


@my_md_notes_bp.route('/api/attachments/<int:aid>/publish', methods=['POST'])
@login_required
@same_origin_required
def api_att_publish(aid):
    """添付を公開領域（static/mdimgs/）に複製し、公開URLを返す。★4.0

    画像／それ以外の別は kind（image / file）で返し、編集画面は画像なら <img>、
    それ以外なら公開リンク <a> を本文に書く。
    """
    conn = mysql.connector.connect(**DatabaseConfig.default())
    cursor = conn.cursor(dictionary=True)
    try:
        a = load_attachment(cursor, aid)
        if not a:
            return jsonify({'success': False, 'error': '添付がありません'}), 404
        note = load_note_row(cursor, a.get('ノートID')) if a.get('ノートID') else None
        if not note or not can_edit_note(note, session['user_id'], session.get('user_category')):
            return jsonify({'success': False,
                            'error': '公開できるのはノートの所有者と管理者です'}), 403

        kind = attachment_kind(a.get('name'), a.get('mimetype'))
        if a.get('public_path'):
            return jsonify({'success': True, 'url': a['public_path'], 'name': a.get('name'),
                            'kind': kind, 'already': True})

        src = protected_abs_path(a.get('local_path'))
        if not src or not os.path.isfile(src):
            return jsonify({'success': False, 'error': '原本が見つかりません'}), 404

        pub = make_public_copy(src, a.get('name'))
        cursor.execute("UPDATE my_md_notes_attachments SET public_path = %s WHERE id = %s",
                       (pub, aid))
        conn.commit()
        return jsonify({'success': True, 'url': pub, 'name': a.get('name'), 'kind': kind})
    except Exception as e:
        conn.rollback()
        print(f"[my_md_notes] publish error: {e}")
        return jsonify({'success': False, 'error': '公開に失敗しました'}), 500
    finally:
        cursor.close()
        conn.close()


@my_md_notes_bp.route('/api/attachments/unpublish', methods=['POST'])
@login_required
@same_origin_required
def api_att_unpublish():
    """公開複製を消して、保護領域のリンクに戻す。★4.0

    JSON の引数は次のどちらか。
      id   - 台帳にある添付のID（本文の data-att から取る）
      url  - 公開領域(static/mdimgs/)のURL。4.0より前に貼った添付はまだ台帳に
             載っていないので、その場合はここで台帳に登録し、実体を保護領域へ
             移してから公開複製を消す（note＝そのノートIDも要る）
    """
    payload = request.get_json(silent=True) or {}
    aid = payload.get('id')
    url = (payload.get('url') or '').strip()
    note_id = payload.get('note')

    conn = mysql.connector.connect(**DatabaseConfig.default())
    cursor = conn.cursor(dictionary=True)
    try:
        a = None
        if aid:
            a = load_attachment(cursor, int(aid))
        elif url:
            path_only = url.split('://', 1)[-1]
            path_only = path_only[path_only.index(UPLOAD_URL_PREFIX):] \
                if UPLOAD_URL_PREFIX in path_only else ''
            if not path_only:
                return jsonify({'success': False, 'error': '公開領域のURLではありません'}), 400
            cursor.execute("SELECT * FROM my_md_notes_attachments WHERE public_path = %s",
                           (path_only,))
            a = cursor.fetchone()
            if not a:
                # 台帳に無い＝4.0より前に貼ったファイル。ここで取り込む
                return adopt_legacy_attachment(conn, cursor, path_only, note_id)

        if not a:
            return jsonify({'success': False, 'error': '添付がありません'}), 404

        note = load_note_row(cursor, a.get('ノートID')) if a.get('ノートID') else None
        if not note or not can_edit_note(note, session['user_id'], session.get('user_category')):
            return jsonify({'success': False,
                            'error': '非公開に戻せるのはノートの所有者と管理者です'}), 403

        remove_public_copy(a.get('public_path'))
        cursor.execute("UPDATE my_md_notes_attachments SET public_path = NULL WHERE id = %s",
                       (a['id'],))
        conn.commit()
        return jsonify({'success': True, 'id': a['id'], 'name': a.get('name'),
                        'url': url_for('my_md_notes.serve_file', aid=a['id'])})
    except Exception as e:
        conn.rollback()
        print(f"[my_md_notes] unpublish error: {e}")
        return jsonify({'success': False, 'error': '非公開に戻せませんでした'}), 500
    finally:
        cursor.close()
        conn.close()


def adopt_legacy_attachment(conn, cursor, public_path, note_id):
    """4.0より前に static/mdimgs/ へ直接置かれたファイルを台帳に取り込む。★4.0

    実体を保護領域へ移し、公開複製を消す。ほかのノートも同じファイルを参照して
    いるときは、そのノートの本文が壊れるので断る。
    """
    if not note_id:
        return jsonify({'success': False, 'error': 'ノートが指定されていません'}), 400
    note = load_note_row(cursor, int(note_id))
    if not note or not can_edit_note(note, session['user_id'], session.get('user_category')):
        return jsonify({'success': False,
                        'error': '非公開に戻せるのはノートの所有者と管理者です'}), 403

    src = public_abs_path(public_path)
    if not src or not os.path.isfile(src):
        return jsonify({'success': False, 'error': '公開領域にファイルがありません'}), 404

    cursor.execute("""
        SELECT ノートID FROM my_md_notes_contents
        WHERE 内容 LIKE %s AND ノートID <> %s
    """, ('%' + public_path + '%', int(note_id)))
    others = [r['ノートID'] for r in cursor.fetchall()]
    if others:
        return jsonify({'success': False,
                        'error': 'ほかのノート（ID: '
                                 + '、'.join(str(x) for x in others[:5])
                                 + '）も同じファイルを参照しているので非公開にできません'}), 409

    with open(src, 'rb') as f:
        data = f.read()
    original = os.path.basename(public_path)
    # 先頭の乱数8桁は保存時に付けたものなので、表示名からは外す
    display_name = original.split('_', 1)[1] if '_' in original[:9] else original
    mimetype = ('image/svg+xml' if original.lower().endswith('.svg') else
                'application/pdf' if original.lower().endswith('.pdf') else
                'image/png' if original.lower().endswith('.png') else
                'image/jpeg' if original.lower().endswith(('.jpg', '.jpeg')) else
                'application/octet-stream')
    r = store_protected(cursor, int(note_id), display_name, data, mimetype, session['user_id'])
    conn.commit()
    try:
        os.remove(src)
    except OSError as e:
        print(f"[my_md_notes] 旧添付の公開複製を消せませんでした: {e}")
    return jsonify({'success': True, 'id': r['id'], 'name': r['name'], 'url': r['url'],
                    'adopted': True})


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
        html_content = render_note_html(note['内容'], user_category)

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
        # 保護添付はリンクのまま（開くにはログインが要る）。相対URLだと
        # ダウンロードしたファイルからは辿れないので、フルURLにしておく。
        body_html = absolutize_protected_refs(render_note_html(note['内容'], user_category))

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

        # アーカイブは文書をインターネットへ出すための経路なので、保護添付は
        # そのままでは読み手が開けない。既定では本文が参照する保護添付を公開し、
        # 参照を公開URLに書き換えてから保存する（チェックを外すと保護のまま）。
        publish_attachments = request.form.get('publish_attachments', '1') == '1'
        content_md = note['内容'] or ''
        published = []
        if publish_attachments:
            content_md, published = publish_note_attachments(conn, cursor, note, content_md)

        body_html = absolutize_protected_refs(render_note_html(content_md, user_category))

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
            lines = [f'ノート「{note["名前"]}」をアーカイブに保存しました（{message}）。'
                     'インターネットへの公開は文書アーカイブで公開範囲を設定してください。']
            if published:
                lines.append(f'本文が参照する添付 {len(published)} 件を公開しました：'
                             + '、'.join(published[:10])
                             + ('…' if len(published) > 10 else ''))
            elif not publish_attachments:
                lines.append('保護されたままの添付は、アーカイブの読み手には開けません。')
            flash('\n'.join(lines), 'success')
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

        att_n = delete_note_attachments(cursor, note_id)
        cursor.execute("DELETE FROM my_md_notes_access_groups WHERE ノートID = %s", (note_id,))
        cursor.execute("DELETE FROM my_md_notes_contents WHERE ノートID = %s", (note_id,))
        cursor.execute("DELETE FROM my_md_notes_notes WHERE id = %s", (note_id,))

        conn.commit()

        message = f'ノート「{note["名前"]}」を削除しました'
        if att_n:
            message += f'（添付 {att_n} 件も削除）'
        return jsonify({'success': True, 'message': message})

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
            # (a) 台帳にある添付（4.0以降）。原本を保護領域から読み、公開複製が
            #     あればその名前も持たせる（移行先で同じ名前の公開複製を作る）
            cursor.execute("SELECT * FROM my_md_notes_attachments ORDER BY id")
            public_names = set()
            for a in cursor.fetchall():
                path = protected_abs_path(a.get('local_path'))
                if not path or not os.path.isfile(path):
                    missing.append(a.get('name') or f'添付 {a["id"]}')
                    continue
                with open(path, 'rb') as f:
                    data = f.read()
                public_name = os.path.basename(a['public_path']) if a.get('public_path') else None
                if public_name:
                    public_names.add(public_name)
                attachments.append({
                    'ref': a['id'],
                    'note_ref': a.get('ノートID'),
                    'filename': a.get('name') or 'file',
                    'public_name': public_name,
                    'mimetype': a.get('mimetype') or '',
                    'size': len(data),
                    'data_base64': base64.b64encode(data).decode('ascii'),
                })

            # (b) 4.0より前に static/mdimgs/ へ直接置いた添付（台帳に無いもの）
            for name in find_attachment_names(n['内容'] for n in notes):
                if name in public_names:
                    continue
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

            # 本文の保護添付への参照は移行先でIDが変わるので目印に置き換える
            for n in notes:
                n['内容'] = PROTECTED_REF_RE.sub(
                    lambda m: '{{att:%s}}' % m.group(1), n['内容'])

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
    note_id_map = {}        # 移行元のノートID → 当サイトのノートID
    written = []            # (ノートID, 取り込んだ本文) …添付の目印を直すために控える
    att_map = {}            # 移行元の添付ID → 当サイトの添付ID
    att_ledger_restored = 0
    att_ledger_rejected = []
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

            if n.get('source_id') is not None:
                note_id_map[str(n['source_id'])] = note_id
            written.append((note_id, content))

        # 台帳にある添付（形式2）の復元。原本を保護領域に置き、公開複製が
        # あった添付は同じ名前で公開領域にも複製する（本文のURLがそのまま生きる）
        for a in payload.get('attachments') or []:
            if not isinstance(a, dict) or a.get('ref') is None:
                continue                      # 旧形式（mdimgs 直置き）は後段で復元
            note_id = note_id_map.get(str(a.get('note_ref')))
            if note_id is None:
                continue                      # 取り込まなかったノートの添付は捨てる
            fname = a.get('filename') or 'file'
            if not allowed_file(fname):
                att_ledger_rejected.append(fname)
                continue
            try:
                data = base64.b64decode(a.get('data_base64') or '')
            except (ValueError, TypeError):
                att_ledger_rejected.append(fname)
                continue
            ext = fname.rsplit('.', 1)[1].lower()
            if ext == 'svg':
                if not svg_head_ok(data[:2048]):
                    att_ledger_rejected.append(fname)
                    continue
                data = _sanitize_svg(data)
            elif not magic_ok(data[:8], ext):
                att_ledger_rejected.append(fname)
                continue

            r = store_protected(cursor, note_id, fname, data, a.get('mimetype'), me_id)
            att_map[str(a['ref'])] = r['id']
            att_ledger_restored += 1

            public_name = a.get('public_name') or ''
            if public_name and safe_attachment_name(public_name):
                os.makedirs(UPLOAD_FOLDER, exist_ok=True)
                public_path = os.path.join(UPLOAD_FOLDER, public_name)
                if not os.path.exists(public_path):
                    with open(public_path, 'wb') as f:
                        f.write(data)
                cursor.execute(
                    "UPDATE my_md_notes_attachments SET public_path = %s WHERE id = %s",
                    (f"{UPLOAD_URL_PREFIX}/{public_name}", r['id']))

        # 本文の目印（{{att:N}} と data-att）を当サイトの添付IDに直す
        for note_id, content in written:
            fixed = re.sub(
                r'\{\{att:(\d+)\}\}',
                lambda m: (url_for('my_md_notes.serve_file', aid=att_map[m.group(1)])
                           if m.group(1) in att_map else '#添付なし'),
                content)
            fixed = re.sub(
                r'(\bdata-att\s*=\s*")(\d+)(")',
                lambda m: m.group(1) + str(att_map.get(m.group(2), m.group(2))) + m.group(3),
                fixed)
            if fixed != content:
                cursor.execute(
                    "UPDATE my_md_notes_contents SET 内容 = %s WHERE ノートID = %s",
                    (fixed, note_id))

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
            if isinstance(a, dict) and a.get('ref') is not None:
                continue                      # 台帳ぶんはトランザクション内で復元済み
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
    if att_ledger_restored or att_ledger_rejected:
        lines.append(f'添付（保護領域）：復元 {att_ledger_restored} 件。')
        if att_ledger_rejected:
            lines.append('保護領域の添付のうち不正なため復元しなかったもの：'
                         + '、'.join(att_ledger_rejected[:10])
                         + ('…' if len(att_ledger_rejected) > 10 else ''))
    if restore_attachments:
        lines.append(f'添付（公開領域・旧形式）：復元 {att_restored} 件、'
                     f'既存のため温存 {att_kept} 件。')
        if att_rejected:
            lines.append('公開領域の添付のうち不正なため復元しなかったもの：' + '、'.join(att_rejected[:10])
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