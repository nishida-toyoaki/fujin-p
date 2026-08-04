#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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
文書アーカイブ閲覧アプリケーション

アクセス権管理、JST対応、ファイルアップロード機能、制作会社向けエクスポート機能を含みます。

アクセスポリシー（詳細は permissions.py の ACCESS_POLICIES を参照）:
  public         - 一般公開: 未ログインを含む全ユーザ
  domestic       - 学内公開: user_category が 'regular' のユーザ、作成者本人、admin
  private        - 非公開  : 作成者本人および admin のみ
  group          - グループ: 指定グループ所属者（カテゴリ不問）、作成者本人、admin
  domestic_group - 学内公開＋指定グループ: regular または 指定グループ所属者（和集合）、作成者本人、admin

データベース構成:
- users, user_groups, user_group_memberships, features, user_features → default データベース
- public_documents, document_access_groups                            → fujinp データベース

改修履歴:
  2026-07-26  P0/P1/P2 改修
    P0-1 エクスポートの任意ファイル読み取り・SSRF 対策（URLはサーバ側で解決）
    P0-2 POST /save に作成・編集権限チェックを追加
    P0-3 ファイル孤児化・file_type/file_path の NULL 上書きを修正
    P0-4 user_id の型を permissions.current_user_id() に統一
    P0-5 権限 WHERE 句を完全プレースホルダ化
    P0-6 /debug_session を削除
    P1   検索結果の権限表示・作成者氏名・公開範囲バッジ・next付きログイン誘導・権限モジュール分離
    P2   デッドコード削除・N+1解消・一時ファイル掃除・ログ整理・ZIP名の日本語対応
"""

import hmac
import io
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import socket
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import (Response, flash, jsonify, redirect, render_template,
                   request, send_file, session, url_for)
from werkzeug.utils import secure_filename

from auth import redirect_to_dashboard
from config import Config
from db import get_db_cursor
from decorators import login_required
from markdown_converter import process_markdown

from . import document_archive_bp
from .permissions import (GROUP_POLICIES, VALID_ACCESS_POLICIES,
                          build_permission_clause, can_create_document,
                          can_import_corepo, can_modify_document,
                          check_view_permission, current_user_id,
                          get_group_name_map, get_group_names_by_ids,
                          is_admin_user)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
# 定数
# ─────────────────────────────────────────────────────────────────

# アップロード許可拡張子
TEXT_EXTENSIONS = {'html', 'htm'}                       # DBの content 列へ保存
BINARY_EXTENSIONS = {                                   # ファイルシステムへ保存
    'pdf', 'svg',
    'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'ico',
    'mp4',
    'txt',
}
ALLOWED_EXTENSIONS = TEXT_EXTENSIONS | BINARY_EXTENSIONS

MIME_TYPES = {
    'pdf': 'application/pdf',
    'svg': 'image/svg+xml',
    'png': 'image/png',
    'jpg': 'image/jpeg',
    'jpeg': 'image/jpeg',
    'gif': 'image/gif',
    'webp': 'image/webp',
    'bmp': 'image/bmp',
    'ico': 'image/x-icon',
    'mp4': 'video/mp4',
    'txt': 'text/plain; charset=utf-8',
    'html': 'text/html; charset=utf-8',
    'htm': 'text/html; charset=utf-8',
}

# アップロードファイルの永続保存先
FILE_STORAGE_DIR = os.path.realpath(
    getattr(Config, 'ARCHIVE_FILE_STORAGE_DIR', None)
    or os.path.expanduser('~/fujinp_file_uploads')
)
os.makedirs(FILE_STORAGE_DIR, exist_ok=True)

# 制作用エクスポートの作業領域
EXPORT_SESSION_DIR = tempfile.gettempdir()
EXPORT_SESSION_PREFIX = 'fujin_export_'
EXPORT_IMAGE_PREFIX = 'fujin_imgs_'
EXPORT_SESSION_TTL = 24 * 60 * 60          # 24時間で期限切れ
EXPORT_IMAGE_MAX_BYTES = 50 * 1024 * 1024  # 1画像あたりの上限
MAX_REDIRECTS = 3                          # 画像取得時に追従するリダイレクト数

# JST（DBは naive な JST で日時を保持）
JST = timezone(timedelta(hours=9), 'JST')


def now_jst():
    """DB格納用の naive な JST 現在時刻"""
    return datetime.now(JST).replace(tzinfo=None)


# ─────────────────────────────────────────────────────────────────
# CSRF 対策
#
# 書き込み系（POST）のエンドポイントを、セッションに紐づくトークンで保護する。
# FUJIN-P 本体に共通の CSRF 機構が導入された場合は、そちらへ移行してよい。
# ─────────────────────────────────────────────────────────────────

CSRF_SESSION_KEY = 'document_archive_csrf_token'
CSRF_FORM_FIELD = 'csrf_token'
CSRF_HEADER = 'X-CSRF-Token'


def get_csrf_token():
    """セッションのCSRFトークンを取得（無ければ生成）"""
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def _submitted_csrf_token():
    """リクエストから提示されたトークンを取り出す"""
    token = request.headers.get(CSRF_HEADER)
    if token:
        return token
    if request.form:
        token = request.form.get(CSRF_FORM_FIELD)
        if token:
            return token
    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        return payload.get(CSRF_FORM_FIELD)
    return None


@document_archive_bp.before_request
def verify_csrf_token():
    """POST リクエストのCSRFトークンを検証する"""
    if request.method != 'POST':
        return None

    expected = session.get(CSRF_SESSION_KEY)
    submitted = _submitted_csrf_token()

    if not expected or not submitted or not hmac.compare_digest(str(expected), str(submitted)):
        logger.warning(
            f"[security] CSRFトークンが一致しません: path={request.path} "
            f"user={session.get('user_id')}")
        if request.is_json:
            return jsonify({'success': False,
                            'error': 'セッションの有効期限が切れています。'
                                     'ページを再読み込みしてください。'}), 400
        flash('セッションの有効期限が切れています。操作をやり直してください。', 'error')
        return redirect(url_for('document_archive.dashboard'))
    return None


@document_archive_bp.context_processor
def inject_csrf_token():
    """このBlueprintのテンプレートから {{ csrf_token() }} を使えるようにする"""
    return {'csrf_token': get_csrf_token}


# ─────────────────────────────────────────────────────────────────
# ファイル種別ヘルパ
# ─────────────────────────────────────────────────────────────────

def file_extension(filename):
    """拡張子を小文字で返す（無ければ空文字）"""
    if not filename or '.' not in filename:
        return ''
    return filename.rsplit('.', 1)[1].lower()


def allowed_file(filename):
    return file_extension(filename) in ALLOWED_EXTENSIONS


def is_binary_file(filename):
    """HTML ではなくバイナリとして保存する対象か"""
    return file_extension(filename) in BINARY_EXTENSIONS


# file_path の各要素に許可する文字（英数字と . _ -）
_PATH_SEGMENT_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')

# DBカラムの上限（public_documents.file_path は varchar(500)）
FILE_PATH_MAX_LENGTH = 500
TITLE_MAX_LENGTH = 255          # public_documents.title は varchar(255)


def storage_path(file_path):
    """
    DB の file_path から実ファイルの絶対パスを求める。

    現行の保存処理は <uuid4hex>.<ext> という単一のファイル名を書き込むが、
    カラムの定義は varchar(500)「ストレージ上の相対パス」であり、
    過去のデータにサブディレクトリ付きの値が入っている可能性がある。
    そのため相対パス自体は許容しつつ、

      - 絶対パス、'..'、先頭がドットの要素、区切り以外の記号を拒否
      - realpath で正規化して FILE_STORAGE_DIR 配下であることを確認

    の二段で外部への脱出を塞ぐ（basename で黙って読み替えると
    異常なデータに気づけないため、拒否してログに残す方針）。
    """
    if not file_path:
        return None
    raw = str(file_path).strip().replace('\\', '/')

    if (not raw or len(raw) > FILE_PATH_MAX_LENGTH
            or raw.startswith('/') or ':' in raw):
        logger.warning(f"[security] 不正な file_path を拒否しました: {file_path!r}")
        return None

    segments = [seg for seg in raw.split('/') if seg not in ('', '.')]
    if not segments or any(not _PATH_SEGMENT_RE.match(seg) for seg in segments):
        logger.warning(f"[security] 不正な file_path を拒否しました: {file_path!r}")
        return None

    candidate = os.path.realpath(os.path.join(FILE_STORAGE_DIR, *segments))
    if not candidate.startswith(FILE_STORAGE_DIR + os.sep):
        logger.warning(f"[security] FILE_STORAGE_DIR 外を指す file_path: {file_path!r}")
        return None
    return candidate


def delete_stored_file(file_path):
    """保存済みファイルを削除する（存在しなくてもエラーにしない）"""
    abs_path = storage_path(file_path)
    if abs_path and os.path.exists(abs_path):
        try:
            os.remove(abs_path)
            logger.info(f"[file] 削除しました: {file_path}")
        except OSError as e:
            logger.error(f"[file] 削除に失敗: {file_path} ({e})")


def upload_provided(upload):
    """ファイルが実際にアップロードされたか"""
    return bool(upload and upload.filename)


# ─────────────────────────────────────────────────────────────────
# ユーザ名・グループ名の解決（P1-2 / P2-4：まとめて引いて N+1 を避ける）
# ─────────────────────────────────────────────────────────────────

def get_user_names(user_ids):
    """ユーザIDのリストから {id: 氏名} を1クエリで取得（default DB）"""
    ids = set()
    for u in user_ids:
        try:
            value = int(u or 0)
        except (TypeError, ValueError):
            continue
        if value:
            ids.add(value)
    if not ids:
        return {}
    try:
        with get_db_cursor(database='default') as (cursor, conn):
            placeholders = ','.join(['%s'] * len(ids))
            cursor.execute(
                f"SELECT id, full_name FROM users WHERE id IN ({placeholders})",
                tuple(sorted(ids)),
            )
            return {row['id']: row['full_name'] for row in cursor.fetchall()}
    except Exception as e:
        logger.error(f"ユーザ名取得エラー: {e}")
        return {}


def get_user_name(user_id):
    """ユーザIDから氏名を取得（単体版）"""
    if not user_id:
        return "不明"
    try:
        key = int(user_id)
    except (TypeError, ValueError):
        return "不明"
    return get_user_names([key]).get(key, "不明")


def attach_user_names(documents):
    """文書リストに created_by_name をまとめて付与する"""
    names = get_user_names([d.get('created_by') for d in documents])
    for doc in documents:
        try:
            key = int(doc.get('created_by') or 0)
        except (TypeError, ValueError):
            key = 0
        doc['created_by_name'] = names.get(key, "不明")
    return documents


def attach_group_names(documents, cursor):
    """グループ限定文書に allowed_group_ids / allowed_group_names をまとめて付与する"""
    targets = [d for d in documents if d.get('access_policy') in GROUP_POLICIES]
    if not targets:
        return documents

    doc_ids = [d['id'] for d in targets]
    placeholders = ','.join(['%s'] * len(doc_ids))
    cursor.execute(
        f"SELECT doc_id, group_id FROM document_access_groups "
        f"WHERE doc_id IN ({placeholders})",
        tuple(doc_ids),
    )
    mapping = {}
    for row in cursor.fetchall():
        mapping.setdefault(row['doc_id'], []).append(row['group_id'])

    name_by_id = get_group_name_map({g for ids in mapping.values() for g in ids})

    for doc in targets:
        ids = mapping.get(doc['id'], [])
        doc['allowed_group_ids'] = ids
        doc['allowed_group_names'] = [name_by_id.get(g, f"#{g}") for g in ids]
    return documents


def get_all_groups():
    """全グループのIDと名前を取得（フォーム選択用）- default DB"""
    try:
        with get_db_cursor(database='default') as (cursor, conn):
            cursor.execute("SELECT id, name FROM user_groups ORDER BY id DESC")
            return cursor.fetchall()
    except Exception as e:
        logger.error(f"全グループ取得エラー: {e}")
        return []


# ─────────────────────────────────────────────────────────────────
# 文書の取得
# ─────────────────────────────────────────────────────────────────

def decorate_documents(documents):
    """一覧・検索の結果に表示用の付加情報を付ける"""
    attach_user_names(documents)
    for doc in documents:
        doc['can_edit'] = can_modify_document(doc)
    return documents


def get_all_documents():
    """閲覧権限のある文書のみを取得 - fujinp DB"""
    try:
        where_clause, where_params = build_permission_clause()

        with get_db_cursor(database='fujinp') as (cursor, conn):
            query = f"""
                SELECT d.id, d.title, d.public_description, d.owner_memo,
                       d.created_at, d.updated_at, d.created_by, d.access_policy,
                       d.file_type, d.file_path,
                       (d.corepo_source_json IS NOT NULL) AS has_corepo_source
                FROM public_documents d
                WHERE {where_clause}
                ORDER BY d.created_at DESC
            """
            cursor.execute(query, where_params)
            documents = cursor.fetchall()
            logger.debug(f"[get_all_documents] {len(documents)} 件")
            attach_group_names(documents, cursor)

        return decorate_documents(documents)

    except Exception as e:
        logger.error(f"文書取得エラー: {e}", exc_info=True)
        return []


def get_document_by_id(doc_id):
    """文書を1件取得（allowed_group_ids 付き）- fujinp DB"""
    try:
        with get_db_cursor(database='fujinp') as (cursor, conn):
            cursor.execute(
                """
                SELECT id, title, public_description, owner_memo, content,
                       created_at, updated_at, created_by, access_policy,
                       file_type, file_path
                FROM public_documents
                WHERE id = %s
                """,
                (doc_id,),
            )
            document = cursor.fetchone()
            if document:
                cursor.execute(
                    "SELECT group_id FROM document_access_groups WHERE doc_id = %s",
                    (doc_id,),
                )
                document['allowed_group_ids'] = [r['group_id'] for r in cursor.fetchall()]
            return document
    except Exception as e:
        logger.error(f"文書取得エラー: {e}")
        return None


def search_documents(query_str):
    """権限考慮版の検索 - fujinp DB"""
    try:
        perm_clause, perm_params = build_permission_clause()
        search_term = f"%{query_str}%"

        with get_db_cursor(database='fujinp') as (cursor, conn):
            sql = f"""
                SELECT d.id, d.title, d.public_description, d.owner_memo,
                       d.created_at, d.updated_at, d.created_by, d.access_policy,
                       d.file_type, d.file_path
                FROM public_documents d
                WHERE {perm_clause}
                  AND (d.title LIKE %s OR d.public_description LIKE %s
                       OR d.owner_memo LIKE %s OR d.content LIKE %s)
                ORDER BY d.created_at DESC
            """
            # 権限句のパラメータが SQL 中で先に現れるため、必ず先に連結する
            params = tuple(perm_params) + (search_term,) * 4
            cursor.execute(sql, params)
            documents = cursor.fetchall()
            attach_group_names(documents, cursor)

        return decorate_documents(documents)
    except Exception as e:
        logger.error(f"検索エラー: {e}", exc_info=True)
        return []


# ─────────────────────────────────────────────────────────────────
# 文書の保存・削除
# ─────────────────────────────────────────────────────────────────

def delete_document(doc_id):
    """文書を削除 - fujinp DB（P0-3：実ファイルも削除する）"""
    document = get_document_by_id(doc_id)
    if not document:
        return False, "文書が見つかりません"
    try:
        with get_db_cursor(database='fujinp') as (cursor, conn):
            cursor.execute("DELETE FROM document_access_groups WHERE doc_id = %s", (doc_id,))
            cursor.execute("DELETE FROM public_documents WHERE id = %s", (doc_id,))
            conn.commit()
    except Exception as e:
        logger.error(f"削除エラー: {e}")
        return False, str(e)

    # DBの削除が成功してからファイルを消す（順序を逆にすると実体だけ失う）
    delete_stored_file(document.get('file_path'))
    return True, "削除しました"


def save_document_to_db(title, public_description, owner_memo, content,
                        access_policy, group_ids, doc_id=None,
                        file_type=None, file_path=None):
    """文書を保存（新規/更新）- fujinp DB"""
    try:
        if access_policy not in VALID_ACCESS_POLICIES:
            logger.warning(f"[save] 未知のポリシー {access_policy!r} → private にフォールバック")
            access_policy = 'private'

        stamp = now_jst()

        with get_db_cursor(database='fujinp') as (cursor, conn):
            if doc_id:  # 更新
                cursor.execute(
                    """
                    UPDATE public_documents
                    SET title=%s, public_description=%s, owner_memo=%s,
                        content=%s, access_policy=%s, updated_at=%s,
                        file_type=%s, file_path=%s
                    WHERE id=%s
                    """,
                    (title, public_description, owner_memo, content,
                     access_policy, stamp, file_type, file_path, doc_id),
                )
                cursor.execute("DELETE FROM document_access_groups WHERE doc_id=%s", (doc_id,))
                message = "文書を更新しました"
            else:      # 新規
                cursor.execute(
                    """
                    INSERT INTO public_documents
                    (title, public_description, owner_memo, content,
                     created_by, access_policy, created_at, updated_at,
                     file_type, file_path)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (title, public_description, owner_memo, content,
                     current_user_id(), access_policy, stamp, stamp,
                     file_type, file_path),
                )
                doc_id = cursor.lastrowid
                message = "文書を作成しました"

            if access_policy in GROUP_POLICIES and group_ids:
                # PRIMARY KEY(doc_id, group_id) のため重複は事前に除去する
                # （重複を投げると IntegrityError で保存全体が失敗する）
                unique_ids = []
                for gid in group_ids:
                    try:
                        value = int(gid)
                    except (TypeError, ValueError):
                        logger.warning(f"[save] 不正なグループID を無視: {gid!r}")
                        continue
                    if value not in unique_ids:
                        unique_ids.append(value)
                for value in unique_ids:
                    cursor.execute(
                        "INSERT INTO document_access_groups (doc_id, group_id) VALUES (%s,%s)",
                        (doc_id, value),
                    )
            conn.commit()
            return True, message, doc_id

    except Exception as e:
        logger.error(f"保存エラー: {e}", exc_info=True)
        return False, str(e), None


# ─────────────────────────────────────────────────────────────────
# エクスポート用セッション（ファイルベース）
# ─────────────────────────────────────────────────────────────────

def _safe_token(token):
    """トークンから英数字とハイフンのみを取り出す"""
    return ''.join(c for c in str(token) if c.isalnum() or c == '-')[:64]


def _session_path(token):
    return os.path.join(EXPORT_SESSION_DIR,
                        f"{EXPORT_SESSION_PREFIX}{_safe_token(token)}.json")


def _session_tmp_dir(token):
    return os.path.join(EXPORT_SESSION_DIR,
                        f"{EXPORT_IMAGE_PREFIX}{_safe_token(token)}")


def _save_session(token, data):
    with open(_session_path(token), 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, default=str)


def _iter_image_entries(sess):
    """
    セッションの image_urls を (url, fig_num) で安全に列挙する。
    壊れたエントリは警告を出して読み飛ばす（500 にしない）。
    """
    for entry in sess.get('image_urls') or ():
        try:
            url, fig_num = entry[0], int(entry[1])
        except (TypeError, ValueError, IndexError, KeyError):
            logger.warning(f"[export] 壊れた画像エントリを無視します: {entry!r}")
            continue
        yield url, fig_num


def _load_session(token):
    """
    セッション情報を読み込む。存在しない・期限切れ・所有者が異なる場合は None。
    （P0-1：トークンだけを知っている第三者が操作できないようにする）
    """
    if not _safe_token(token):
        return None
    path = _session_path(token)
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return None

    try:
        created_ts = float(data.get('created_ts', 0))
    except (TypeError, ValueError):
        created_ts = 0
    if time.time() - created_ts > EXPORT_SESSION_TTL:
        logger.info("[export] 期限切れセッションを破棄します")
        _delete_session(token)
        return None

    try:
        owner_id = int(data.get('owner_id') or 0)
    except (TypeError, ValueError):
        owner_id = 0
    if owner_id != current_user_id() or owner_id == 0:
        logger.warning("[security] エクスポートセッションの所有者が一致しません")
        return None

    return data


def _delete_session(token):
    try:
        os.remove(_session_path(token))
    except OSError:
        pass
    shutil.rmtree(_session_tmp_dir(token), ignore_errors=True)


def cleanup_stale_export_sessions(ttl=EXPORT_SESSION_TTL):
    """
    期限切れのエクスポート作業ファイルを掃除する（P2-5）。
    ダウンロードもキャンセルもせずに離脱した分の残骸を回収する。
    """
    removed = 0
    now = time.time()
    try:
        for name in os.listdir(EXPORT_SESSION_DIR):
            if not name.startswith((EXPORT_SESSION_PREFIX, EXPORT_IMAGE_PREFIX)):
                continue
            path = os.path.join(EXPORT_SESSION_DIR, name)
            try:
                if now - os.path.getmtime(path) <= ttl:
                    continue
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.remove(path)
                removed += 1
            except OSError:
                continue
    except OSError as e:
        logger.error(f"[export] 作業ファイルの掃除に失敗: {e}")
    if removed:
        logger.info(f"[export] 期限切れ作業ファイルを {removed} 件削除しました")
    return removed


# ─────────────────────────────────────────────────────────────────
# 制作会社向けエクスポート
# ─────────────────────────────────────────────────────────────────

def extract_images_and_create_plain_text(html_content):
    """本文HTMLから画像を抜き出し、図マーカー入りのプレーンテキストを作る"""
    soup = BeautifulSoup(html_content or '', 'html.parser')
    image_urls = []
    figure_counter = 1

    for img in soup.find_all('img'):
        src = img.get('src', '')
        if src:
            image_urls.append((src, figure_counter))
            marker = soup.new_tag('span')
            marker.string = f'**図{figure_counter:02d}**'
            img.replace_with(marker)
            figure_counter += 1

    for table in soup.find_all('table'):
        table.decompose()
    for tag in soup.find_all(['script', 'style']):
        tag.decompose()

    plain_text = soup.get_text(separator='\n')
    plain_text = re.sub(r'\n{3,}', '\n\n', plain_text).strip()
    return plain_text, image_urls


def _is_public_ip(addr):
    """内部ネットワークを指すアドレスでないか"""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _is_public_host(host):
    """名前解決した結果が全てグローバルIPかどうか（P0-1：SSRF対策）"""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    if not infos:
        return False
    for info in infos:
        addr = info[4][0]
        if not _is_public_ip(addr):
            logger.warning(f"[security] 内部アドレスへの取得を拒否: {host} -> {addr}")
            return False
    return True


def _peer_is_public(response):
    """
    実際に接続した相手のIPを再検証する（DNS rebinding 対策）。

    getaddrinfo での事前チェックと実接続の間に名前解決結果が
    差し替えられる可能性があるため、本文を読む前にもう一度確認する。
    ソケットが取得できない環境では事前チェックを信頼する。
    """
    try:
        sock = response.raw._connection.sock          # urllib3 の内部構造に依存
        peer = sock.getpeername()[0]
    except Exception:
        return True
    if not _is_public_ip(peer):
        logger.warning(f"[security] 接続先が内部アドレスでした: {peer}")
        return False
    return True


# エクスポートで取り込める画像の拡張子（これ以外はディレクトリ配下でも読まない）
IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp', 'ico', 'tif', 'tiff'}


def resolve_local_image(url):
    """
    相対URLを UPLOAD_BASE_DIR 配下に限定して解決する（P0-1：パストラバーサル対策）。

    - realpath で正規化し、基準ディレクトリ配下でなければ None
      （シンボリックリンク経由の脱出も realpath で塞がる）
    - 画像拡張子以外は None（配下の任意ファイルを吸い出せないようにする）
    """
    base_raw = getattr(Config, 'UPLOAD_BASE_DIR', '') or ''
    if not base_raw:
        return None
    base = os.path.realpath(base_raw)
    clean = str(url).split('?', 1)[0].split('#', 1)[0]

    if file_extension(clean) not in IMAGE_EXTENSIONS:
        logger.warning(f"[security] 画像以外のローカルファイル参照を拒否: {url!r}")
        return None

    path = os.path.realpath(os.path.join(base, clean.lstrip('/')))
    if not path.startswith(base + os.sep):
        logger.warning(f"[security] パストラバーサルを遮断しました: {url!r}")
        return None
    return path if os.path.isfile(path) else None


def download_image(url, timeout=10):
    """
    画像を取得して (バイト列, 拡張子) を返す。取得できなければ (None, None)。

    P0-1:
      - 相対パスは UPLOAD_BASE_DIR 配下に限定（realpath で検証）
      - 絶対URLは http/https のみ、かつ内部アドレスを拒否
      - サイズ上限を設ける
    """
    url = str(url or '').strip()
    if not url:
        return None, None

    # ── 相対パス（自サーバの static 配信）はローカルから読む ──
    if not url.startswith(('http://', 'https://')):
        if '://' in url or url.startswith('data:'):   # file:// data: などは拒否
            logger.warning(f"[security] 未対応のスキームを拒否: {url!r}")
            return None, None
        local_path = resolve_local_image(url)
        if not local_path:
            logger.warning(f"ローカル画像が見つかりません: {url}")
            return None, None
        try:
            if os.path.getsize(local_path) > EXPORT_IMAGE_MAX_BYTES:
                logger.warning(f"画像が大きすぎます: {url}")
                return None, None
            with open(local_path, 'rb') as f:
                data = f.read()
        except OSError as e:
            logger.error(f"ローカル画像の読み込みに失敗: {url} ({e})")
            return None, None
        ext = os.path.splitext(local_path)[1][:5] or '.jpg'
        return data, ext

    # ── 絶対URLは HTTP 取得 ──
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; FUJIN-P document_archive exporter)'}

    try:
        response = None
        current = url
        # リダイレクトは自動追従させず、1ホップごとに宛先を検証する
        for _ in range(MAX_REDIRECTS + 1):
            parsed = urlparse(current)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname:
                logger.warning(f"[security] 未対応のスキームを拒否: {current!r}")
                return None, None
            if not _is_public_host(parsed.hostname):
                return None, None

            response = requests.get(current, timeout=timeout, stream=True,
                                    headers=headers, allow_redirects=False)
            if not _peer_is_public(response):
                response.close()
                return None, None

            if response.is_redirect or response.is_permanent_redirect:
                location = response.headers.get('location')
                response.close()
                if not location:
                    return None, None
                current = urljoin(current, location)
                continue
            break
        else:
            logger.warning(f"リダイレクトが多すぎます: {url}")
            return None, None

        response.raise_for_status()
        parsed = urlparse(current)

        length = response.headers.get('content-length')
        if length and length.isdigit() and int(length) > EXPORT_IMAGE_MAX_BYTES:
            logger.warning(f"画像が大きすぎます: {url}")
            return None, None

        chunks, total = [], 0
        for chunk in response.iter_content(8192):
            total += len(chunk)
            if total > EXPORT_IMAGE_MAX_BYTES:
                logger.warning(f"画像が大きすぎます: {url}")
                return None, None
            chunks.append(chunk)
        content = b''.join(chunks)

        content_type = response.headers.get('content-type', '').lower()
        extension = None
        for key, ext in (('png', '.png'), ('gif', '.gif'),
                         ('svg', '.svg'), ('webp', '.webp')):
            if key in content_type:
                extension = ext
                break
        if extension is None:
            if '.' in parsed.path:
                extension = os.path.splitext(parsed.path)[1][:5] or '.jpg'
            else:
                extension = '.jpg'
        return content, extension

    except requests.exceptions.Timeout:
        logger.error(f"画像取得がタイムアウトしました: {url}")
    except requests.exceptions.RequestException as e:
        logger.error(f"画像取得に失敗しました: {url} ({e})")
    except Exception as e:
        logger.error(f"画像取得中の予期しないエラー: {url} ({e})")
    return None, None


def zip_safe_name(title, suffix, extension):
    """
    ZIP名・内部ファイル名を組み立てる（P2-7）。
    secure_filename は非ASCIIを全て落として名前が消えてしまうため、
    パス区切りや制御文字だけを除去して日本語タイトルを保持する。
    """
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', str(title or ''))
    base = re.sub(r'\s+', '_', base).strip()[:80].strip('._')
    if not base:
        base = secure_filename(str(title or '')) or 'document'
    return f"{base}{suffix}{extension}"


# ─────────────────────────────────────────────────────────────────
# ルート：一覧・表示
# ─────────────────────────────────────────────────────────────────

@document_archive_bp.route('/')
def dashboard():
    """ダッシュボード（未ログイン時は一般公開文書のみの簡易一覧）"""
    if not current_user_id():
        try:
            with get_db_cursor(database='fujinp') as (cursor, conn):
                cursor.execute(
                    """
                    SELECT id, title, updated_at
                    FROM public_documents
                    WHERE access_policy = 'public'
                    ORDER BY updated_at DESC
                    """
                )
                public_docs = cursor.fetchall()
        except Exception as e:
            logger.error(f"公開文書一覧の取得に失敗: {e}")
            public_docs = []
        return render_template('view_public.html', documents=public_docs)

    return render_template('document_archive_dashboard.html',
                           documents=get_all_documents(),
                           can_create=can_create_document(),
                           can_import=can_import_corepo())


@document_archive_bp.route('/view/<int:doc_id>')
def view_document(doc_id):
    """文書詳細"""
    document = get_document_by_id(doc_id)

    if not document:
        flash('文書が見つかりません', 'error')
        return redirect(url_for('document_archive.dashboard'))

    if not check_view_permission(document):
        if not current_user_id():
            # P1-4: ログイン後に元のページへ戻す
            flash('このページを閲覧するにはログインが必要です', 'info')
            return redirect(url_for('auth.login', next=request.url))
        flash('閲覧権限がありません', 'error')
        return redirect(url_for('document_archive.dashboard'))

    try:
        public_html = process_markdown(document['public_description']) if document.get('public_description') else ""
        owner_html = process_markdown(document['owner_memo']) if document.get('owner_memo') else ""
    except Exception as e:
        logger.error(f"Markdown変換エラー: {e}")
        public_html, owner_html = "", ""
    content_html = document.get('content') or ""

    # P1-2 / P1-3: 作成者は氏名、公開範囲も表示する
    document['created_by_name'] = get_user_name(document.get('created_by'))
    document['allowed_group_names'] = get_group_names_by_ids(
        document.get('allowed_group_ids') or []
    )

    return render_template('document_archive_view.html',
                           document=document,
                           public_html=public_html,
                           owner_html=owner_html,
                           content_html=content_html,
                           can_edit=can_modify_document(document))


@document_archive_bp.route('/plain/<int:doc_id>')
def plain_view(doc_id):
    """プレーン表示（バイナリ文書はファイルをそのまま配信）"""
    document = get_document_by_id(doc_id)

    if not document:
        return "文書が見つかりません", 404

    if not check_view_permission(document):
        if not current_user_id():
            flash('このページを閲覧するにはログインが必要です', 'info')
            return redirect(url_for('auth.login', next=request.url))
        return "権限がありません", 403

    if document.get('file_path') and document.get('file_type'):
        abs_path = storage_path(document['file_path'])
        if not abs_path or not os.path.exists(abs_path):
            logger.error(f"実ファイルが見つかりません: doc_id={doc_id} path={document['file_path']!r}")
            return "ファイルが見つかりません", 404
        return send_file(abs_path, mimetype=document['file_type'])

    if not document.get('content'):
        return "No Content", 404
    return Response(document['content'], mimetype='text/html; charset=utf-8')


@document_archive_bp.route('/search')
@login_required
def search():
    """検索"""
    query = request.args.get('q', '').strip()
    if not query:
        return redirect(url_for('document_archive.dashboard'))
    return render_template('document_archive_search.html',
                           documents=search_documents(query), query=query)


# ─────────────────────────────────────────────────────────────────
# ルート：作成・編集・削除
# ─────────────────────────────────────────────────────────────────

@document_archive_bp.route('/new')
@login_required
def new_document():
    """新規作成画面"""
    if not can_create_document():
        flash('文書作成権限がありません', 'error')
        return redirect(url_for('document_archive.dashboard'))
    return render_template('document_archive_form.html', groups=get_all_groups())


@document_archive_bp.route('/edit/<int:doc_id>')
@login_required
def edit_document(doc_id):
    """編集画面"""
    document = get_document_by_id(doc_id)

    if not document:
        flash('文書が見つかりません', 'error')
        return redirect(url_for('document_archive.dashboard'))

    if not can_modify_document(document):
        flash('編集権限がありません', 'error')
        return redirect(url_for('document_archive.dashboard'))

    return render_template('document_archive_form.html',
                           document=document, groups=get_all_groups())


@document_archive_bp.route('/save', methods=['POST'])
@login_required
def save_document():
    """保存（新規・更新共用）"""
    try:
        raw_id = request.form.get('id')
        doc_id = None
        existing = None

        # ── P0-2: サーバ側で権限を検証する ──
        if raw_id:
            try:
                doc_id = int(raw_id)
            except (TypeError, ValueError):
                flash('不正なリクエストです', 'error')
                return redirect(url_for('document_archive.dashboard'))
            existing = get_document_by_id(doc_id)
            if not existing:
                flash('文書が見つかりません', 'error')
                return redirect(url_for('document_archive.dashboard'))
            if not can_modify_document(existing):
                logger.warning(
                    f"[security] 権限のない更新を拒否: user={current_user_id()} doc={doc_id}")
                flash('編集権限がありません', 'error')
                return redirect(url_for('document_archive.dashboard'))
        else:
            if not can_create_document():
                logger.warning(
                    f"[security] 権限のない新規作成を拒否: user={current_user_id()}")
                flash('文書作成権限がありません', 'error')
                return redirect(url_for('document_archive.dashboard'))

        title = request.form.get('title', '').strip()
        public_description = request.form.get('public_description', '').strip()
        owner_memo = request.form.get('owner_memo', '').strip()
        manual_content = request.form.get('content', '').strip()
        access_policy = request.form.get('access_policy', 'private')
        group_ids = request.form.getlist('group_ids')

        if not title:
            flash('タイトルは必須です', 'error')
            return redirect(request.referrer or url_for('document_archive.dashboard'))

        if len(title) > TITLE_MAX_LENGTH:
            # public_documents.title は varchar(255)
            flash(f'タイトルは{TITLE_MAX_LENGTH}文字以内で入力してください', 'error')
            return redirect(request.referrer or url_for('document_archive.dashboard'))

        if access_policy not in VALID_ACCESS_POLICIES:
            flash('公開範囲の指定が不正です', 'error')
            return redirect(request.referrer or url_for('document_archive.dashboard'))

        content = manual_content or None
        file_type = None
        file_path = None
        replaced_file_path = None
        upload_applied = False        # アップロードが実際に内容を差し替えたか

        upload = request.files.get('html_file')
        if upload_provided(upload):
            if not allowed_file(upload.filename):
                flash('許可されていないファイル形式です', 'error')
                return redirect(request.referrer or url_for('document_archive.dashboard'))

            if is_binary_file(upload.filename):
                # バイナリ系は admin のみ
                if not is_admin_user():
                    flash('このファイル形式のアップロードはadminのみ可能です', 'error')
                    return redirect(request.referrer or url_for('document_archive.dashboard'))

                ext = file_extension(upload.filename)
                unique_name = f"{uuid.uuid4().hex}.{ext}"
                save_path = storage_path(unique_name)
                upload.save(save_path)

                file_type = MIME_TYPES.get(ext, 'application/octet-stream')
                file_path = unique_name
                content = None
                upload_applied = True
                # 旧ファイルは保存成功後に削除する（先に消すと失敗時に実体を失う）
                if existing:
                    replaced_file_path = existing.get('file_path')
            else:
                try:
                    text = upload.read().decode('utf-8')
                except UnicodeDecodeError:
                    flash('ファイルの文字コードが UTF-8 ではありません', 'error')
                    return redirect(request.referrer or url_for('document_archive.dashboard'))
                except Exception as e:
                    flash(f"ファイル読み込みエラー: {e}", 'error')
                    return redirect(request.referrer or url_for('document_archive.dashboard'))
                if not text.strip():
                    # 空のHTMLで既存の内容を消してしまわないよう明示的に弾く
                    flash('アップロードされたHTMLファイルが空です', 'error')
                    return redirect(request.referrer or url_for('document_archive.dashboard'))
                content = text
                file_type = None
                file_path = None
                upload_applied = True
                if existing and existing.get('file_path'):
                    # HTMLで置き換えるので旧バイナリは不要になる
                    replaced_file_path = existing.get('file_path')

        # ── P0-3: ファイルを差し替えない更新では既存の内容を引き継ぐ ──
        if existing and not upload_applied:
            if content is None:
                content = existing.get('content')
            if file_path is None:
                file_type = existing.get('file_type')
                file_path = existing.get('file_path')

        if not content and not file_path:
            flash('内容を入力するかファイルをアップロードしてください', 'error')
            return redirect(request.referrer or url_for('document_archive.dashboard'))

        success, msg, new_id = save_document_to_db(
            title, public_description, owner_memo, content,
            access_policy, group_ids, doc_id,
            file_type=file_type, file_path=file_path,
        )

        if success and replaced_file_path and replaced_file_path != file_path:
            delete_stored_file(replaced_file_path)

        flash(msg, 'success' if success else 'error')
        return redirect(url_for('document_archive.dashboard'))

    except Exception as e:
        logger.error(f"保存処理でエラー: {e}", exc_info=True)
        flash("保存中にエラーが発生しました。管理者にお問い合わせください。", 'error')
        return redirect(url_for('document_archive.dashboard'))


@document_archive_bp.route('/delete_confirm/<int:doc_id>')
@login_required
def delete_confirm(doc_id):
    """削除確認画面"""
    document = get_document_by_id(doc_id)
    if not document:
        flash('文書が見つかりません', 'error')
        return redirect(url_for('document_archive.dashboard'))
    if not can_modify_document(document):
        flash('削除権限がありません', 'error')
        return redirect(url_for('document_archive.dashboard'))

    document['created_by_name'] = get_user_name(document.get('created_by'))
    document['allowed_group_names'] = get_group_names_by_ids(
        document.get('allowed_group_ids') or []
    )
    return render_template('document_archive_delete_confirm.html', document=document)


@document_archive_bp.route('/delete/<int:doc_id>', methods=['POST'])
@login_required
def delete_archive(doc_id):
    """削除実行"""
    document = get_document_by_id(doc_id)
    if not document:
        flash('文書が見つかりません', 'error')
        return redirect(url_for('document_archive.dashboard'))
    if not can_modify_document(document):
        flash('削除権限がありません', 'error')
        return redirect(url_for('document_archive.dashboard'))

    if request.form.get('confirmation') != 'delete':
        flash('削除の確認にチェックを入れてください', 'warning')
        return redirect(url_for('document_archive.delete_confirm', doc_id=doc_id))

    success, msg = delete_document(doc_id)
    flash(msg, 'success' if success else 'error')
    return redirect(url_for('document_archive.dashboard'))


@document_archive_bp.route('/preview_markdown', methods=['POST'])
@login_required
def preview_markdown():
    """Markdownプレビュー"""
    try:
        payload = request.get_json(silent=True) or {}
        content = payload.get('content', '') or ''
        if not content.strip():
            return jsonify({'success': True, 'html': '<p class="text-muted">内容がありません</p>'})
        return jsonify({'success': True, 'html': process_markdown(content)})
    except Exception as e:
        logger.error(f"Markdownプレビューエラー: {e}")
        return jsonify({'success': False, 'error': 'プレビューの生成に失敗しました'})


# ─────────────────────────────────────────────────────────────────
# ルート：制作会社向けエクスポート
# ─────────────────────────────────────────────────────────────────

@document_archive_bp.route('/export_production/<int:doc_id>')
@login_required
def export_for_production(doc_id):
    """エクスポート画面（作業用トークンを発行）"""
    document = get_document_by_id(doc_id)

    if not document:
        flash('文書が見つかりません', 'error')
        return redirect(url_for('document_archive.dashboard'))

    if not check_view_permission(document):
        flash('閲覧権限がありません', 'error')
        return redirect(url_for('document_archive.dashboard'))

    if document.get('file_path'):
        flash('この文書はHTML以外のファイルのため、制作用エクスポートは利用できません', 'warning')
        return redirect(url_for('document_archive.dashboard'))

    cleanup_stale_export_sessions()

    plain_text, image_urls = extract_images_and_create_plain_text(document.get('content'))

    token = str(uuid.uuid4())
    os.makedirs(_session_tmp_dir(token), exist_ok=True)

    _save_session(token, {
        'owner_id': current_user_id(),          # P0-1: 所有者を紐付ける
        'created_ts': time.time(),
        'doc_id': doc_id,
        'title': document['title'],
        'public_description': document.get('public_description', ''),
        'created_at': str(document.get('created_at') or ''),
        'updated_at': str(document.get('updated_at') or ''),
        'plain_text': plain_text,
        'image_urls': image_urls,               # [[url, fig_num], ...]
    })

    return render_template('document_archive_export.html',
                           document=document,
                           image_count=len(image_urls),
                           image_urls=image_urls,
                           token=token)


@document_archive_bp.route('/export_download_one', methods=['POST'])
@login_required
def export_download_one():
    """
    画像を1件取得する。

    リクエスト (JSON): { "token": "...", "fig_num": 3 }
    レスポンス (JSON): { "success": true|false, "fig_num": 3, "error": "..." }

    P0-1: URL はクライアントから受け取らず、サーバ側のセッションに保存された
          画像一覧から fig_num で引く。これにより任意パス・任意URLの指定を封じる。
    """
    data = request.get_json(silent=True) or {}
    token = data.get('token', '')
    try:
        fig_num = int(data.get('fig_num', 0))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'fig_num': 0, 'error': '不正なリクエストです'}), 400

    sess = _load_session(token)
    if not sess:
        return jsonify({'success': False, 'fig_num': fig_num,
                        'error': 'セッションが無効です。ページを再読み込みしてください。'}), 403

    url = next((u for u, n in _iter_image_entries(sess) if n == fig_num), None)
    if url is None:
        logger.warning(f"[security] 不正な図番号: user={current_user_id()} fig_num={fig_num}")
        return jsonify({'success': False, 'fig_num': fig_num, 'error': '不正な図番号です'}), 400

    tmp_dir = _session_tmp_dir(token)
    os.makedirs(tmp_dir, exist_ok=True)

    image_data, extension = download_image(url)
    if not image_data:
        return jsonify({'success': False, 'fig_num': fig_num, 'error': 'ダウンロード失敗'})

    ext = re.sub(r'[^A-Za-z0-9.]', '', extension or '.jpg')[:5] or '.jpg'
    with open(os.path.join(tmp_dir, f"fig_{fig_num:03d}{ext}"), 'wb') as f:
        f.write(image_data)
    return jsonify({'success': True, 'fig_num': fig_num})


@document_archive_bp.route('/export_finalize/<token>')
@login_required
def export_finalize(token):
    """ZIPを生成してダウンロードさせる"""
    sess = _load_session(token)
    if not sess:
        flash('セッションが無効です。再度エクスポートしてください。', 'error')
        return redirect(url_for('document_archive.dashboard'))

    tmp_dir = _session_tmp_dir(token)

    try:
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(zip_safe_name(sess['title'], '_本文', '.txt'),
                        sess['plain_text'].encode('utf-8'))

            meta = (
                f"文書情報\n{'=' * 40}\n"
                f"タイトル: {sess['title']}\n"
                f"作成日時: {sess.get('created_at') or '不明'}\n"
                f"更新日時: {sess.get('updated_at') or '不明'}\n\n"
                f"説明:\n{sess.get('public_description', '')}\n\n"
                f"図の総数: {len(list(_iter_image_entries(sess)))}\n{'=' * 40}\n"
                "- 本文テキストには図の位置に「**図XX**」マーカーが入っています。\n"
                "- 画像は images/ フォルダに保存されています。\n"
                "- テーブル等の複雑な要素は削除されています。\n"
            )
            zf.writestr('README.txt', meta.encode('utf-8'))

            existing_files = os.listdir(tmp_dir) if os.path.isdir(tmp_dir) else []
            failed_list = []
            for url, fig_num in _iter_image_entries(sess):
                matches = sorted(f for f in existing_files
                                 if f.startswith(f"fig_{fig_num:03d}"))
                if matches:
                    fpath = os.path.join(tmp_dir, matches[0])
                    ext = os.path.splitext(matches[0])[1]
                    with open(fpath, 'rb') as f:
                        zf.writestr(f"images/図{fig_num:02d}{ext}", f.read())
                else:
                    failed_list.append((fig_num, url))

            if failed_list:
                err_log = "画像ダウンロードエラーログ\n" + "=" * 50 + "\n\n"
                err_log += f"失敗: {len(failed_list)}件\n\n失敗した画像:\n"
                for fig_num, u in failed_list:
                    err_log += f"  図{fig_num:02d}: {u}\n"
                zf.writestr('ダウンロードエラー.txt', err_log.encode('utf-8'))

        zip_buffer.seek(0)
        timestamp = datetime.now(JST).strftime('%Y%m%d_%H%M%S')
        download_name = zip_safe_name(sess['title'], f'_制作用_{timestamp}', '.zip')

        return send_file(zip_buffer, mimetype='application/zip',
                         as_attachment=True, download_name=download_name)

    finally:
        _delete_session(token)


@document_archive_bp.route('/export_cleanup/<token>', methods=['POST'])
@login_required
def export_cleanup(token):
    """中断時のセッション破棄"""
    if _load_session(token) is not None:
        _delete_session(token)
    return jsonify({'success': True})


# ─────────────────────────────────────────────────────────────────
# ルート：その他
# ─────────────────────────────────────────────────────────────────

@document_archive_bp.route('/return_to_fujin')
@login_required
def return_to_fujin():
    """FUJINダッシュボードに戻る"""
    return redirect_to_dashboard()
