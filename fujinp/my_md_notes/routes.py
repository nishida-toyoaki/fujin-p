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

from flask import render_template, request, jsonify, session, url_for, redirect, flash
from decorators import login_required
from config import Config
from db import DatabaseConfig
from mysql.connector import Error
import mysql.connector
import os
from functools import wraps
from urllib.parse import urlparse
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

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'pdf'}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20MB

# 拡張子ごとのマジックナンバー（拡張子偽装の検出用）
MAGIC_NUMBERS = {
    'png': (b'\x89PNG\r\n\x1a\n',),
    'jpg': (b'\xff\xd8\xff',),
    'jpeg': (b'\xff\xd8\xff',),
    'pdf': (b'%PDF-',),
}

# 共有キーは以下の3値のみを取る（乱数トークンは廃止）
SHARE_KEYS = ('private', 'public', 'shared')

JST = pytz.timezone('Asia/Tokyo')


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
    """行dictに表示用の日時文字列を付与する"""
    for row in rows:
        row['作成日時表示'] = fmt_dt(row.get('作成日時'))
        row['更新日時表示'] = fmt_dt(row.get('更新日時'))
    return rows


def to_int(value, default=0):
    """フォーム値を整数に変換する。空欄・不正値は default"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def normalize_share_key(value, current='private'):
    """共有キーを3値のいずれかに正規化する"""
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


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def magic_ok(head_bytes, ext):
    signatures = MAGIC_NUMBERS.get(ext)
    if not signatures:
        return False
    return any(head_bytes.startswith(sig) for sig in signatures)


# =============================================================================
# データアクセス
# =============================================================================

def get_user_notes_flat(user_id, category=None):
    """更新日時の逆順で全ノートを取得する"""
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

                cursor.execute("""
                    SELECT DISTINCT ns.*, u.full_name AS オーナー名,
                        CASE
                            WHEN ns.オーナーID = %s THEN '所有者'
                            WHEN nsh.権限 IS NOT NULL THEN nsh.権限
                            ELSE NULL
                        END AS 権限
                    FROM my_md_notes_notes ns
                    LEFT JOIN users u ON ns.オーナーID = u.id
                    LEFT JOIN my_md_notes_shares nsh ON ns.id = nsh.ノートID AND nsh.共有先ユーザID = %s
                    WHERE ns.オーナーID = %s OR nsh.共有先ユーザID = %s
                    ORDER BY ns.更新日時 DESC
                """, (user_id, user_id, user_id, user_id))
                return add_display_dates(cursor.fetchall())
    except mysql.connector.Error as e:
        print(f"データベースエラー: {e}")
        return []


def create_note(user_id, name, sequence=0):
    """空のノートを1件作成して note_id を返す。共有キーの初期値は 'private'。"""
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
    if request.method == 'POST' and not origin_ok():
        flash('不正な要求元です。', 'error')
        return redirect(url_for('my_md_notes.index'))

    conn = mysql.connector.connect(**DatabaseConfig.default())
    cursor = conn.cursor(dictionary=True)
    try:
        user_category = session.get('user_category')
        if user_category == 'admin':
            cursor.execute("""
                SELECT ns.*, nc.内容, NULL AS 権限
                FROM my_md_notes_notes ns
                LEFT JOIN my_md_notes_contents nc ON ns.id = nc.ノートID
                WHERE ns.id = %s
            """, (note_id,))
        else:
            cursor.execute("""
                SELECT ns.*, nc.内容, nsh.権限
                FROM my_md_notes_notes ns
                LEFT JOIN my_md_notes_contents nc ON ns.id = nc.ノートID
                LEFT JOIN my_md_notes_shares nsh ON ns.id = nsh.ノートID AND nsh.共有先ユーザID = %s
                WHERE ns.id = %s AND (ns.オーナーID = %s OR (nsh.共有先ユーザID = %s AND nsh.権限 = '編集'))
            """, (session['user_id'], note_id, session['user_id'], session['user_id']))
        note = cursor.fetchone()

        if not note:
            flash('ノートが見つからないか、編集権限がありません。', 'error')
            return redirect(url_for('my_md_notes.index'))

        if request.method == 'POST':
            name = request.form.get('name', '').strip()
            if not name:
                flash('ノート名は必須です。', 'error')
                return redirect(url_for('my_md_notes.edit_note', note_id=note_id))

            content = request.form.get('content', '')
            sequence = to_int(request.form.get('sequence'), 0)
            current_share_key = note['共有キー']
            new_share_key = normalize_share_key(request.form.get('share_key'), current_share_key)

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

                cursor.execute("DELETE FROM my_md_notes_shares WHERE ノートID = %s", (note_id,))
                if new_share_key == 'shared':
                    for shared_user_id in request.form.getlist('shared_users'):
                        permission = request.form.get(f'user_permission_{shared_user_id}')
                        if permission not in ('閲覧', '編集'):
                            permission = '閲覧'
                        cursor.execute("""
                            INSERT INTO my_md_notes_shares (ノートID, 共有先ユーザID, 権限)
                            VALUES (%s, %s, %s)
                        """, (note_id, shared_user_id, permission))

                conn.commit()

                if current_share_key != new_share_key:
                    labels = {'private': '非公開', 'public': '公開', 'shared': '特定のユーザーと共有'}
                    flash(f'ノートを更新し、共有設定を「{labels[new_share_key]}」に変更しました。', 'success')
                else:
                    flash('ノートを更新しました。', 'success')

                return redirect(url_for('my_md_notes.view_note', note_id=note_id))

            except mysql.connector.Error as err:
                conn.rollback()
                flash(f'更新中にエラーが発生しました: {err}', 'error')
                return redirect(url_for('my_md_notes.edit_note', note_id=note_id))

        # GET
        cursor.execute("""
            SELECT id, full_name AS 氏名, category AS カテゴリー
            FROM users
            WHERE id != %s AND is_active = 1
            ORDER BY full_name
        """, (session['user_id'],))
        all_users = cursor.fetchall()

        cursor.execute("""
            SELECT 共有先ユーザID, 権限
            FROM my_md_notes_shares
            WHERE ノートID = %s
        """, (note_id,))
        shared_rows = cursor.fetchall()
        shared_user_ids = [row['共有先ユーザID'] for row in shared_rows]
        shared_permissions = {row['共有先ユーザID']: row['権限'] for row in shared_rows}

        return render_template('edit_note.html',
                               note=note,
                               all_users=all_users,
                               shared_user_ids=shared_user_ids,
                               shared_permissions=shared_permissions)

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
    """画像・PDFのアップロード（png / jpg / jpeg / pdf、20MBまで）"""
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'ファイルが選択されていません'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'ファイルが選択されていません'}), 400

    if not allowed_file(file.filename):
        return jsonify({'success': False, 'error': '許可されていない形式です（png / jpg / jpeg / pdf のみ）'}), 400

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

    # 内容検証（拡張子偽装の検出）
    head = file.stream.read(8)
    file.stream.seek(0)
    if not magic_ok(head, ext):
        return jsonify({'success': False, 'error': 'ファイルの内容が拡張子と一致しません'}), 400

    try:
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)

        timestamp = get_jst_now().strftime('%Y%m%d_%H%M%S')
        filename = secure_filename(file.filename)
        name, _ = os.path.splitext(filename)
        if not name:
            name = 'file'
        unique_filename = f"{name}_{timestamp}.{ext}"

        file.save(os.path.join(UPLOAD_FOLDER, unique_filename))

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
            SELECT ns.*, nc.内容, nsh.権限
            FROM my_md_notes_notes ns
            LEFT JOIN my_md_notes_contents nc ON ns.id = nc.ノートID
            LEFT JOIN my_md_notes_shares nsh ON ns.id = nsh.ノートID AND nsh.共有先ユーザID = %s
            WHERE ns.id = %s
        """, (session['user_id'], note_id))
        note = cursor.fetchone()

        if not note:
            flash('ノートが見つかりません。', 'error')
            return redirect(url_for('my_md_notes.index'))

        is_owner = note['オーナーID'] == session['user_id']
        is_public = note['共有キー'] == 'public'
        is_shared = (note['共有キー'] == 'shared' and note['権限'] is not None)
        if not (user_category == 'admin' or is_owner or is_public or is_shared):
            flash('このノートにアクセスする権限がありません。', 'error')
            return redirect(url_for('my_md_notes.index'))

        add_display_dates([note])
        html_content = process_markdown(note['内容'] or '', user_category)
        public_url = url_for('my_md_notes.public_view_by_id', note_id=note['id'], _external=True) if is_public else None

        return render_template('view_note.html',
                               note=note,
                               html_content=html_content,
                               public_url=public_url,
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
            SELECT ns.*, nc.内容, nsh.権限
            FROM my_md_notes_notes ns
            LEFT JOIN my_md_notes_contents nc ON ns.id = nc.ノートID
            LEFT JOIN my_md_notes_shares nsh ON ns.id = nsh.ノートID AND nsh.共有先ユーザID = %s
            WHERE ns.id = %s
        """, (session['user_id'], note_id))
        note = cursor.fetchone()

        if not note:
            flash('ノートが見つかりません。', 'error')
            return redirect(url_for('my_md_notes.index'))

        allowed = (user_category == 'admin'
                   or note['オーナーID'] == session['user_id']
                   or note['共有キー'] == 'public'
                   or (note['共有キー'] == 'shared' and note['権限'] is not None))
        if not allowed:
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

        cursor.execute("DELETE FROM my_md_notes_shares WHERE ノートID = %s", (note_id,))
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


@my_md_notes_bp.route('/public_view/<int:note_id>')
def public_view_by_id(note_id):
    """公開ノートの閲覧（ログイン不要、共有キー='public' のみ）"""
    conn = mysql.connector.connect(**DatabaseConfig.default())
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT ns.*, nc.内容
            FROM my_md_notes_notes ns
            LEFT JOIN my_md_notes_contents nc ON ns.id = nc.ノートID
            WHERE ns.id = %s AND ns.共有キー = 'public'
        """, (note_id,))
        note = cursor.fetchone()

        if not note:
            return render_template('public_not_found.html'), 404

        add_display_dates([note])
        html_content = process_markdown(note['内容'] or '')

        return render_template('public_view.html', note=note, html_content=html_content)
    except mysql.connector.Error as err:
        print(f"[my_md_notes] public_view error: {err}")
        return render_template('public_error.html'), 500
    finally:
        cursor.close()
        conn.close()


@my_md_notes_bp.route('/search')
@login_required
def search_notes():
    """ノート検索（ノート名の部分一致）"""
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
            base = f"""
                SELECT DISTINCT ns.*, u.full_name AS オーナー名,
                    CASE
                        WHEN ns.オーナーID = %s THEN '所有者'
                        WHEN nsh.権限 IS NOT NULL THEN nsh.権限
                        ELSE NULL
                    END AS 権限
                FROM my_md_notes_notes ns
                LEFT JOIN users u ON ns.オーナーID = u.id
                LEFT JOIN my_md_notes_shares nsh ON ns.id = nsh.ノートID AND nsh.共有先ユーザID = %s
                WHERE (ns.オーナーID = %s OR nsh.共有先ユーザID = %s){{keyword_clause}}
                {order_clause}
            """
            uid = session['user_id']
            if keyword:
                cursor.execute(base.format(keyword_clause=' AND ns.名前 LIKE %s'),
                               (uid, uid, uid, uid, f'%{keyword}%'))
            else:
                cursor.execute(base.format(keyword_clause=''), (uid, uid, uid, uid))

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
