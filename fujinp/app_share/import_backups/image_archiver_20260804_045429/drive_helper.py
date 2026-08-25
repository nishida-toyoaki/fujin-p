"""
drive_helper.py
いめくら用 Google Drive ユーティリティ
"""
import io
import logging

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

logger = logging.getLogger('image_archiver.drive')

DRIVE_FOLDER_NAME = 'image_archive_fujinpshowcase'
DRIVE_SCOPE = 'https://www.googleapis.com/auth/drive.file'


def get_credentials(config):
    """リフレッシュトークンからDrive認証情報を取得"""
    if not getattr(config, 'DRIVE_REFRESH_TOKEN', None):
        return None, 'Drive認証が完了していません'
    try:
        creds = Credentials(
            token=None,
            refresh_token=config.DRIVE_REFRESH_TOKEN,
            token_uri='https://oauth2.googleapis.com/token',
            client_id=config.DRIVE_CLIENT_ID,
            client_secret=config.DRIVE_CLIENT_SECRET,
            scopes=[DRIVE_SCOPE]
        )
        creds.refresh(Request())
        return creds, None
    except Exception as e:
        logger.error("Drive credentials error: %s", e)
        return None, f'Drive認証エラー: {str(e)}'


def get_drive_service(config):
    """Drive APIサービスを取得"""
    creds, error = get_credentials(config)
    if error:
        return None, error
    try:
        service = build('drive', 'v3', credentials=creds)
        return service, None
    except Exception as e:
        return None, str(e)


def get_or_create_folder(service):
    """image_archive_nishida フォルダを取得または作成"""
    try:
        results = service.files().list(
            q=f"name='{DRIVE_FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields='files(id, name)'
        ).execute()
        files = results.get('files', [])
        if files:
            return files[0]['id'], None

        folder = service.files().create(
            body={
                'name': DRIVE_FOLDER_NAME,
                'mimeType': 'application/vnd.google-apps.folder'
            },
            fields='id'
        ).execute()
        logger.info(f"フォルダ作成: {DRIVE_FOLDER_NAME} (id={folder['id']})")
        return folder['id'], None

    except Exception as e:
        logger.error("get_or_create_folder error: %s", e)
        return None, str(e)


def upload_file(config, file_bytes, filename, mimetype, folder_id=None):
    """
    ファイルをGoogle Driveにアップロードする
    戻り値: {'ok': True, 'file_id': ..., 'file_url': ..., 'filesize': ...}
         or {'ok': False, 'error': ...}
    """
    service, error = get_drive_service(config)
    if error:
        return {'ok': False, 'error': error}

    try:
        if not folder_id:
            folder_id, err = get_or_create_folder(service)
            if err:
                return {'ok': False, 'error': err}

        file_metadata = {
            'name': filename,
            'parents': [folder_id],
        }
        media = MediaIoBaseUpload(
            io.BytesIO(file_bytes),
            mimetype=mimetype,
            resumable=len(file_bytes) > 5 * 1024 * 1024
        )
        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id, name, webViewLink, size'
        ).execute()

        return {
            'ok': True,
            'file_id': file['id'],
            'file_url': file.get('webViewLink', ''),
            'filename': file['name'],
            'filesize': int(file.get('size', len(file_bytes))),
        }

    except Exception as e:
        logger.error("upload_file error: %s", e)
        return {'ok': False, 'error': str(e)}


def download_file(config, file_id):
    """
    Google DriveからファイルをDLしてbytesで返す
    戻り値: (bytes, mimetype) or (None, error_message)
    """
    service, error = get_drive_service(config)
    if error:
        return None, error

    try:
        # まずファイルのmimetypeを取得
        meta = service.files().get(
            fileId=file_id,
            fields='mimeType, name, size'
        ).execute()
        mimetype = meta.get('mimeType', 'application/octet-stream')

        # ダウンロード
        request = service.files().get_media(fileId=file_id)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

        buf.seek(0)
        return buf.read(), mimetype

    except Exception as e:
        logger.error("download_file error (file_id=%s): %s", file_id, e)
        return None, str(e)


def register_existing_file(config, drive_file_id):
    """
    既存DriveファイルのIDを登録する（アップロードなし）
    戻り値: {'ok': True, 'file_url': ..., 'mimetype': ..., 'filename': ..., 'filesize': ...}
         or {'ok': False, 'error': ...}
    """
    service, error = get_drive_service(config)
    if error:
        return {'ok': False, 'error': error}

    try:
        meta = service.files().get(
            fileId=drive_file_id,
            fields='id, name, mimeType, size, webViewLink'
        ).execute()

        return {
            'ok': True,
            'file_id': meta['id'],
            'file_url': meta.get('webViewLink', ''),
            'mimetype': meta.get('mimeType', 'application/octet-stream'),
            'filename': meta.get('name', ''),
            'filesize': int(meta.get('size', 0)),
        }

    except Exception as e:
        logger.error("register_existing_file error: %s", e)
        return {'ok': False, 'error': str(e)}