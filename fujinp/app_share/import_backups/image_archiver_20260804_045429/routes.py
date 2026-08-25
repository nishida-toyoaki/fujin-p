"""
routes.py - いめくら（image_archiver）
"""
import datetime
import logging
import secrets
import mimetypes

from flask import (
    render_template, request, jsonify, session,
    redirect, url_for, flash, Response, abort
)
import mysql.connector
from pytz import timezone

from config import Config
from db import DatabaseConfig
from decorators import login_required
from auth import redirect_to_dashboard
from .drive_helper import (
    upload_file, download_file, register_existing_file,
    get_or_create_folder, get_drive_service
)

from . import image_archiver_bp

logger = logging.getLogger('image_archiver')
JST = timezone('Asia/Tokyo')


# ─────────────────────────────────────────
# ユーティリティ
# ─────────────────────────────────────────

def get_jst_now():
    return datetime.datetime.now(JST).replace(tzinfo=None)


def generate_label():
    """タイムスタンプ+ランダム8文字のラベルを生成"""
    ts = datetime.datetime.now(JST).strftime('%Y%m%d-%H%M%S')
    rand = secrets.token_hex(4)  # 8文字のランダム16進数
    return f"{ts}-{rand}"


def get_db():
    return mysql.connector.connect(**DatabaseConfig.default())


def get_item_by_label(label):
    """ラベルからアイテムを取得"""
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT * FROM image_archive WHERE label = %s", (label,)
        )
        item = cursor.fetchone()
        if item:
            for col in ('created_at',):
                if item.get(col) and hasattr(item[col], 'strftime'):
                    item[col] = item[col].strftime('%Y-%m-%d %H:%M:%S')
        return item
    except Exception as e:
        logger.error("get_item_by_label error: %s", e)
        return None
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


def get_all_items(search=None, mimetype_filter=None, page=1, per_page=100):
    """アイテム一覧取得"""
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)

        conditions = []
        params = []

        if search:
            conditions.append(
                "(label LIKE %s OR title LIKE %s OR original_filename LIKE %s OR memo LIKE %s)"
            )
            like = f"%{search}%"
            params.extend([like, like, like, like])

        if mimetype_filter:
            conditions.append("mimetype LIKE %s")
            params.append(f"{mimetype_filter}%")

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        cursor.execute(
            f"SELECT COUNT(*) as cnt FROM image_archive {where}", params
        )
        total = cursor.fetchone()['cnt']

        offset = (page - 1) * per_page
        cursor.execute(
            f"""SELECT id, label, drive_file_id, drive_url, mimetype,
                       original_filename, filesize, title, memo, source_app, created_at
                FROM image_archive {where}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s""",
            params + [per_page, offset]
        )
        items = cursor.fetchall()
        for item in items:
            if item.get('created_at') and hasattr(item['created_at'], 'strftime'):
                item['created_at'] = item['created_at'].strftime('%Y-%m-%d %H:%M')

        return items, total

    except Exception as e:
        logger.error("get_all_items error: %s", e)
        return [], 0
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


# ─────────────────────────────────────────
# ルート定義
# ─────────────────────────────────────────

@image_archiver_bp.route('/')
@login_required
def index():
    """一覧画面"""
    search          = request.args.get('q', '').strip()
    mimetype_filter = request.args.get('mt', '').strip()
    page            = int(request.args.get('page', 1))
    per_page        = 50

    items, total = get_all_items(
        search=search or None,
        mimetype_filter=mimetype_filter or None,
        page=page,
        per_page=per_page
    )
    total_pages = max(1, (total + per_page - 1) // per_page)

    return render_template('image_archiver/index.html',
        items=items, search=search, mimetype_filter=mimetype_filter,
        page=page, total=total, total_pages=total_pages
    )


@image_archiver_bp.route('/upload', methods=['GET'])
@login_required
def upload_form():
    """アップロードフォーム"""
    return render_template('image_archiver/upload.html')


@image_archiver_bp.route('/upload', methods=['POST'])
@login_required
def upload():
    """ファイルアップロードAPI"""
    try:
        user_id = session.get('user_id')
        now = get_jst_now()

        # ファイルアップロードかDrive ID登録かを判定
        drive_file_id = request.form.get('drive_file_id', '').strip()
        title         = request.form.get('title', '').strip() or None
        memo          = request.form.get('memo', '').strip() or None
        source_app    = request.form.get('source_app', '').strip() or None

        if drive_file_id:
            # 既存DriveファイルのID登録
            result = register_existing_file(Config, drive_file_id)
            if not result['ok']:
                flash(f'Drive登録エラー: {result["error"]}', 'error')
                return redirect(url_for('image_archiver.upload_form'))

            label    = generate_label()
            mimetype = result['mimetype']
            filename = result['filename']
            filesize = result['filesize']
            file_url = result['file_url']

        elif 'file' in request.files and request.files['file'].filename:
            # ファイルアップロード
            f = request.files['file']
            file_bytes = f.read()
            filename   = f.filename
            mimetype   = f.mimetype or mimetypes.guess_type(filename)[0] or 'application/octet-stream'
            filesize   = len(file_bytes)

            # Driveにアップロード
            service, err = get_drive_service(Config)
            if err:
                flash(f'Drive認証エラー: {err}', 'error')
                return redirect(url_for('image_archiver.upload_form'))

            folder_id, err = get_or_create_folder(service)
            if err:
                flash(f'フォルダエラー: {err}', 'error')
                return redirect(url_for('image_archiver.upload_form'))

            result = upload_file(Config, file_bytes, filename, mimetype, folder_id=folder_id)
            if not result['ok']:
                flash(f'アップロードエラー: {result["error"]}', 'error')
                return redirect(url_for('image_archiver.upload_form'))

            label        = generate_label()
            drive_file_id = result['file_id']
            file_url     = result['file_url']

        else:
            flash('ファイルまたはDrive IDを指定してください', 'error')
            return redirect(url_for('image_archiver.upload_form'))

        # DBに保存
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO image_archive
               (label, drive_file_id, drive_url, mimetype, original_filename,
                filesize, title, memo, source_app, created_by, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (label, drive_file_id, file_url, mimetype, filename,
             filesize, title, memo, source_app, user_id, now)
        )
        conn.commit()

        flash(f'登録完了: {label}', 'success')
        return redirect(url_for('image_archiver.view_item', label=label))

    except Exception as e:
        logger.error("upload error: %s", e)
        flash(f'エラー: {str(e)}', 'error')
        return redirect(url_for('image_archiver.upload_form'))
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@image_archiver_bp.route('/view/<label>')
@login_required
def view_item(label):
    """アイテム詳細ページ"""
    item = get_item_by_label(label)
    if not item:
        abort(404)
    serve_url = url_for('image_archiver.serve', label=label, _external=True)
    return render_template('image_archiver/view.html', item=item, serve_url=serve_url)


@image_archiver_bp.route('/delete/<label>', methods=['POST'])
@login_required
def delete_item(label):
    """削除API（DBからのみ削除、Driveファイルは残す）"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM image_archive WHERE label = %s", (label,))
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        logger.error("delete_item error: %s", e)
        return jsonify({'ok': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


# ─────────────────────────────────────────
# ファイル配信エンドポイント（アクセス制限なし）
# ─────────────────────────────────────────

@image_archiver_bp.route('/f/<label>')
def serve(label):
    """
    ラベルからファイルを取得してそのままレスポンスとして返す
    アクセス制限なし（公開エンドポイント）
    URL例: /image_archiver/f/20260504-153042-a3f8c2d1
    """
    item = get_item_by_label(label)
    if not item:
        abort(404)

    drive_file_id = item.get('drive_file_id')
    if not drive_file_id:
        abort(404)

    file_bytes, mimetype_or_error = download_file(Config, drive_file_id)
    if file_bytes is None:
        logger.error("serve error (label=%s): %s", label, mimetype_or_error)
        abort(502)

    # DBのmimetypeを優先
    mimetype = item.get('mimetype') or mimetype_or_error

    return Response(
        file_bytes,
        mimetype=mimetype,
        headers={
            'Content-Disposition': 'inline',
            'Cache-Control': 'public, max-age=86400',  # 1日キャッシュ
        }
    )


# ─────────────────────────────────────────
# API（他アプリからの登録用）
# ─────────────────────────────────────────

@image_archiver_bp.route('/api/register', methods=['POST'])
@login_required
def api_register():
    """
    他のFUJIN-Pアプリからファイルを登録するAPI
    POST JSON: {
        drive_file_id: str,
        mimetype: str (optional),
        filename: str (optional),
        filesize: int (optional),
        title: str (optional),
        memo: str (optional),
        source_app: str (optional)
    }
    戻り値: { ok: true, label: str, serve_url: str }
    """
    try:
        data = request.json or {}
        user_id = session.get('user_id')
        now = get_jst_now()

        drive_file_id = data.get('drive_file_id', '').strip()
        if not drive_file_id:
            return jsonify({'ok': False, 'error': 'drive_file_idは必須です'}), 400

        # Drive側からメタ情報取得（省略可）
        mimetype = data.get('mimetype', '')
        filename = data.get('filename', '')
        filesize = data.get('filesize', 0)
        file_url = data.get('drive_url', '')

        if not mimetype or not filename:
            result = register_existing_file(Config, drive_file_id)
            if not result['ok']:
                return jsonify({'ok': False, 'error': result['error']}), 500
            mimetype = mimetype or result['mimetype']
            filename = filename or result['filename']
            filesize = filesize or result['filesize']
            file_url = file_url or result['file_url']

        label = generate_label()

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO image_archive
               (label, drive_file_id, drive_url, mimetype, original_filename,
                filesize, title, memo, source_app, created_by, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (label, drive_file_id, file_url, mimetype, filename,
             filesize, data.get('title', ''), data.get('memo', ''),
             data.get('source_app', ''), user_id, now)
        )
        conn.commit()

        serve_url = url_for('image_archiver.serve', label=label, _external=True)
        return jsonify({
            'ok': True,
            'label': label,
            'serve_url': serve_url,
            'drive_file_id': drive_file_id,
        })

    except Exception as e:
        logger.error("api_register error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()

@image_archiver_bp.route('/return_to_fujin')
@login_required
def return_to_fujin():
    """FUJIN-Pダッシュボードに戻る"""
    return redirect_to_dashboard()