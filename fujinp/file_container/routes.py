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
ふぁいこん（file_container）

「箱」を単位に大型ファイルを受け渡す．
  渡す箱（download）: admin が箱にファイルを入れ，URLとパスワードを相手に伝える．
                     相手はパスワードを入れてダウンロードする．
  受け取る箱（upload）: admin が箱を作り，URLとパスワードを相手に伝える．
                     相手はパスワードを入れてファイルを送り込む．
用事がすめば admin がファイルの実体を消す．箱・ファイル・出来事の記録は残る．

管理画面は admin 専用（before_request で deny by default）．
受け渡し口（/s/<token>）だけはログイン不要で，箱のパスワードで開く．

テーブル（default DB．非公開データの置き場）: fc_boxes / fc_files / fc_events
日時はアプリが JST の naive datetime を書く（DB の CURRENT_TIMESTAMP は使わない）．
"""

import hashlib
import hmac
import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from flask import (abort, flash, redirect, render_template, request,
                   send_file, session, url_for)
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.security import check_password_hash, generate_password_hash

from auth import redirect_to_dashboard
from config import Config
from db import get_db_cursor

from . import file_container_bp as bp

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
# 定数
# ─────────────────────────────────────────────────────────────────

DB = 'default'
JST = timezone(timedelta(hours=9), 'JST')

# 1ファイルの上限（config.py の FILE_CONTAINER_MAX_BYTES で変えられる）
MAX_BYTES = int(getattr(Config, 'FILE_CONTAINER_MAX_BYTES', 0) or 100 * 1024 * 1024)

# 実体の置き場（アプリのディレクトリ配下が既定．config.py で変えられる）
STORAGE_DIR = os.path.realpath(
    getattr(Config, 'FILE_CONTAINER_STORAGE_DIR', None)
    or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'storage')
)

UNLOCK_MINUTES = 30          # パスワードを通したあと開いておく時間
FAIL_LIMIT = 10              # この回数だけ失敗すると
FAIL_WINDOW_MINUTES = 60     # この時間のあいだ受け付けない
CHUNK = 1024 * 1024

KIND_LABELS = {'download': '渡す箱', 'upload': '受け取る箱'}
STATUS_LABELS = {'open': '受付中', 'closed': '閉鎖'}
EVENT_LABELS = {
    'create': '箱を作成', 'edit': '箱を編集', 'password': 'パスワード再発行',
    'open': '受付を再開', 'close': '受付を停止',
    'upload': 'ファイルを入れた', 'download': 'ダウンロード',
    'auth_ok': 'パスワード認証', 'auth_fail': 'パスワード誤り', 'locked': '連続失敗で拒否',
    'purge': '実体を削除',
}

# パスワード生成に使う文字（見間違えやすい 0 O 1 l I を除く）
PW_ALPHABET = 'abcdefghjkmnpqrstuvwxyz23456789'

# admin でなくても通すエンドポイント（受け渡し口）
_PUBLIC_ENDPOINTS = {
    'file_container.public_entry',
    'file_container.public_download',
    'file_container.public_upload',
}


def now_jst():
    return datetime.now(JST).replace(tzinfo=None)


def fmt_bytes(n):
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f"{n:.0f}{unit}" if unit == 'B' else f"{n:.1f}{unit}"
        n /= 1024


def fmt_dt(v):
    if not v:
        return ''
    if isinstance(v, str):
        return v[:16]
    return v.strftime('%Y-%m-%d %H:%M')


def gen_password(n=12):
    return ''.join(secrets.choice(PW_ALPHABET) for _ in range(n))


def client_ip():
    fwd = request.headers.get('X-Forwarded-For', '')
    if fwd:
        return fwd.split(',')[0].strip()[:45]
    return (request.headers.get('X-Real-IP') or request.remote_addr or '')[:45]


def public_url(token):
    url = url_for('file_container.public_entry', token=token, _external=True)
    if url.startswith('http://') and request.headers.get('X-Forwarded-Proto', '') == 'https':
        url = 'https://' + url[len('http://'):]
    return url


def is_admin():
    return session.get('user_category') == 'admin'


def current_user_id():
    try:
        return int(session.get('user_id') or 0)
    except (TypeError, ValueError):
        return 0


# ─────────────────────────────────────────────────────────────────
# 認可（deny by default）と CSRF
# ─────────────────────────────────────────────────────────────────

CSRF_KEY = 'file_container_csrf_token'


def get_csrf_token():
    token = session.get(CSRF_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_KEY] = token
    return token


@bp.before_request
def guard():
    # 受け渡し口以外は admin 専用
    if request.endpoint not in _PUBLIC_ENDPOINTS:
        if not current_user_id():
            return redirect(url_for('auth.login', next=request.url))
        if not is_admin():
            abort(403)

    if request.method == 'POST':
        expected = session.get(CSRF_KEY)
        submitted = request.form.get('csrf_token') or request.headers.get('X-CSRF-Token')
        if not expected or not submitted or not hmac.compare_digest(str(expected), str(submitted)):
            logger.warning(f"[file_container] CSRF不一致 path={request.path}")
            flash('ページの有効期限が切れています．再読み込みしてからやり直してください', 'error')
            return redirect(request.referrer or url_for('file_container.index'))
    return None


@bp.context_processor
def inject_helpers():
    return {'csrf_token': get_csrf_token, 'fmt_bytes': fmt_bytes, 'fmt_dt': fmt_dt,
            'KIND_LABELS': KIND_LABELS, 'STATUS_LABELS': STATUS_LABELS,
            'EVENT_LABELS': EVENT_LABELS, 'max_label': fmt_bytes(MAX_BYTES),
            'max_bytes': MAX_BYTES}


@bp.errorhandler(RequestEntityTooLarge)
def too_large(e):
    flash(f'ファイルが大きすぎます．1件 {fmt_bytes(MAX_BYTES)} までです', 'error')
    return redirect(request.referrer or url_for('file_container.index'))


# ─────────────────────────────────────────────────────────────────
# DB ヘルパ
# ─────────────────────────────────────────────────────────────────

def log_event(cursor, box_id, event, file_id=None, detail=''):
    cursor.execute(
        """INSERT INTO fc_events (box_id, file_id, event, user_id, ip, user_agent, detail, created_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (box_id, file_id, event, current_user_id() or None, client_ip(),
         (request.headers.get('User-Agent') or '')[:255], (detail or '')[:500], now_jst()))


def get_box(box_id=None, token=None):
    with get_db_cursor(database=DB) as (cursor, conn):
        if token is not None:
            cursor.execute("SELECT * FROM fc_boxes WHERE token=%s", (token,))
        else:
            cursor.execute("SELECT * FROM fc_boxes WHERE id=%s", (box_id,))
        return cursor.fetchone()


def get_files(box_id, include_purged=True):
    with get_db_cursor(database=DB) as (cursor, conn):
        sql = "SELECT * FROM fc_files WHERE box_id=%s"
        if not include_purged:
            sql += " AND purged_at IS NULL"
        cursor.execute(sql + " ORDER BY id", (box_id,))
        return cursor.fetchall()


def get_file(box_id, file_id):
    with get_db_cursor(database=DB) as (cursor, conn):
        cursor.execute("SELECT * FROM fc_files WHERE id=%s AND box_id=%s", (file_id, box_id))
        return cursor.fetchone()


def user_names(ids):
    ids = sorted({int(i) for i in ids if i})
    if not ids:
        return {}
    try:
        with get_db_cursor(database='default') as (cursor, conn):
            ph = ','.join(['%s'] * len(ids))
            cursor.execute(f"SELECT id, full_name FROM users WHERE id IN ({ph})", tuple(ids))
            return {r['id']: r['full_name'] for r in cursor.fetchall()}
    except Exception as e:
        logger.error(f"[file_container] 氏名取得エラー: {e}")
        return {}


def box_is_live(box):
    """受け渡し口として開いているか（受付中かつ期限内）"""
    if not box or box['status'] != 'open':
        return False
    if box.get('expires_at') and box['expires_at'] < now_jst():
        return False
    return True


# ─────────────────────────────────────────────────────────────────
# ファイルの実体
# ─────────────────────────────────────────────────────────────────

def stored_path(stored_name):
    """stored_name（32桁の16進）から実体の絶対パスを求める．異常値は None"""
    name = str(stored_name or '')
    if len(name) != 32 or any(c not in '0123456789abcdef' for c in name):
        logger.warning(f"[file_container] 不正な stored_name: {stored_name!r}")
        return None
    path = os.path.realpath(os.path.join(STORAGE_DIR, name))
    if not path.startswith(STORAGE_DIR + os.sep):
        return None
    return path


def clean_original_name(name):
    name = os.path.basename(str(name or '').replace('\\', '/')).strip()
    name = ''.join(c for c in name if c >= ' ' and c not in '<>:"|?*')
    return (name or 'file')[:255]


def receive_upload(upload):
    """アップロードを上限つきで書き込み，(stored_name, size, sha256) を返す．
    上限を超えたら書きかけを消して ValueError．"""
    os.makedirs(STORAGE_DIR, exist_ok=True)
    stored_name = uuid.uuid4().hex
    path = stored_path(stored_name)
    digest = hashlib.sha256()
    size = 0
    try:
        with open(path, 'wb') as out:
            while True:
                chunk = upload.stream.read(CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError(f'ファイルが大きすぎます．1件 {fmt_bytes(MAX_BYTES)} までです')
                digest.update(chunk)
                out.write(chunk)
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    if size == 0:
        os.remove(path)
        raise ValueError('空のファイルは受け付けません')
    return stored_name, size, digest.hexdigest()


def store_files(box, uploads, via, uploader_label=''):
    """複数のアップロードを箱に入れる．(成功件数, エラーメッセージのリスト)"""
    saved, errors = 0, []
    for up in uploads:
        if not up or not up.filename:
            continue
        original = clean_original_name(up.filename)
        try:
            stored_name, size, sha = receive_upload(up)
        except ValueError as e:
            errors.append(f'{original}: {e}')
            continue
        except Exception as e:
            logger.error(f"[file_container] 受信エラー: {e}", exc_info=True)
            errors.append(f'{original}: 保存できませんでした')
            continue
        try:
            with get_db_cursor(database=DB) as (cursor, conn):
                cursor.execute(
                    """INSERT INTO fc_files (box_id, original_name, stored_name, size_bytes, sha256,
                                             mime_type, uploaded_via, uploader_label, uploaded_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (box['id'], original, stored_name, size, sha,
                     (up.mimetype or 'application/octet-stream')[:100], via,
                     (uploader_label or '')[:100], now_jst()))
                fid = cursor.lastrowid
                log_event(cursor, box['id'], 'upload', fid, f'{original}（{fmt_bytes(size)}）')
                conn.commit()
            saved += 1
        except Exception as e:
            logger.error(f"[file_container] 記録エラー: {e}", exc_info=True)
            p = stored_path(stored_name)
            if p and os.path.exists(p):
                os.remove(p)
            errors.append(f'{original}: 記録できませんでした')
    return saved, errors


def purge_file(cursor, f, detail=''):
    """実体を消し，記録に purged_at を付ける"""
    p = stored_path(f['stored_name'])
    if p and os.path.exists(p):
        try:
            os.remove(p)
        except OSError as e:
            logger.error(f"[file_container] 実体の削除に失敗: {f['stored_name']} ({e})")
            return False
    cursor.execute("UPDATE fc_files SET purged_at=%s, purged_by=%s WHERE id=%s",
                   (now_jst(), current_user_id() or None, f['id']))
    log_event(cursor, f['box_id'], 'purge', f['id'], detail or f['original_name'])
    return True


def send_stored(f):
    p = stored_path(f['stored_name'])
    if not p or not os.path.exists(p):
        return None
    return send_file(p, mimetype=f.get('mime_type') or 'application/octet-stream',
                     as_attachment=True, download_name=f['original_name'],
                     max_age=0)


# ─────────────────────────────────────────────────────────────────
# 管理画面（admin）
# ─────────────────────────────────────────────────────────────────

@bp.route('/')
def index():
    # 渡す箱と受け取る箱はタブで分けて管理する（最後に開いたタブを覚えておく）
    tab = request.args.get('tab') or session.get('fc_tab') or 'download'
    if tab not in KIND_LABELS:
        tab = 'download'
    session['fc_tab'] = tab
    show = request.args.get('show', 'active')
    with get_db_cursor(database=DB) as (cursor, conn):
        cursor.execute(
            """SELECT b.*,
                      COUNT(f.id) AS file_total,
                      SUM(f.id IS NOT NULL AND f.purged_at IS NULL) AS file_live,
                      COALESCE(SUM(CASE WHEN f.purged_at IS NULL THEN f.size_bytes END), 0) AS live_bytes,
                      (SELECT MAX(e.created_at) FROM fc_events e WHERE e.box_id=b.id) AS last_event_at,
                      (SELECT COUNT(*) FROM fc_events e WHERE e.box_id=b.id AND e.event='download') AS download_count
               FROM fc_boxes b LEFT JOIN fc_files f ON f.box_id=b.id
               GROUP BY b.id
               ORDER BY b.id DESC""")
        boxes = cursor.fetchall()
    now = now_jst()
    for b in boxes:
        b['file_live'] = int(b['file_live'] or 0)
        b['expired'] = bool(b.get('expires_at') and b['expires_at'] < now)
        b['live'] = box_is_live(b)
    total_live = sum(int(b['live_bytes'] or 0) for b in boxes)
    # タブごとの件数（使用中の箱）
    counts = {k: sum(1 for b in boxes if b['kind'] == k and (b['status'] == 'open' or b['file_live']))
              for k in KIND_LABELS}
    tab_bytes = sum(int(b['live_bytes'] or 0) for b in boxes if b['kind'] == tab)
    boxes = [b for b in boxes if b['kind'] == tab]
    if show == 'active':
        boxes = [b for b in boxes if b['status'] == 'open' or b['file_live']]
    return render_template('file_container/index.html', boxes=boxes, show=show, tab=tab,
                           counts=counts, tab_bytes=tab_bytes, total_live=total_live,
                           home_tab=tab)


@bp.route('/new', methods=['GET', 'POST'])
def new_box():
    if request.method == 'GET':
        kind = request.args.get('kind') or session.get('fc_tab') or 'download'
        return render_template('file_container/new.html', password=gen_password(),
                               kind=kind, home_tab=kind)

    kind = request.form.get('kind')
    title = (request.form.get('title') or '').strip()
    note = (request.form.get('note') or '').strip()
    password = (request.form.get('password') or '').strip()
    try:
        days = int(request.form.get('expires_days') or 0)
    except ValueError:
        days = 0
    if kind not in KIND_LABELS or not title or len(title) > 200:
        flash('種類と題名（200字以内）を入れてください', 'error')
        return redirect(url_for('file_container.new_box'))
    if len(password) < 8:
        flash('パスワードは8文字以上にしてください', 'error')
        return redirect(url_for('file_container.new_box'))

    now = now_jst()
    token = secrets.token_urlsafe(16)
    expires = now + timedelta(days=days) if days > 0 else None
    with get_db_cursor(database=DB) as (cursor, conn):
        cursor.execute(
            """INSERT INTO fc_boxes (token, kind, title, note, password_hash, status,
                                     expires_at, created_by, created_at, updated_at)
               VALUES (%s,%s,%s,%s,%s,'open',%s,%s,%s,%s)""",
            (token, kind, title, note, generate_password_hash(password), expires,
             current_user_id() or None, now, now))
        box_id = cursor.lastrowid
        log_event(cursor, box_id, 'create', None, KIND_LABELS[kind])
        conn.commit()

    session['fc_tab'] = kind
    box = get_box(box_id)
    # 最初のファイルを同時に入れた場合（渡す箱）
    uploads = request.files.getlist('files')
    if any(u and u.filename for u in uploads):
        saved, errors = store_files(box, uploads, 'admin')
        for msg in errors:
            flash(msg, 'error')
    return render_template('file_container/credentials.html', box=get_box(box_id),
                           url=public_url(token), password=password, fresh=True,
                           home_tab=kind)


@bp.route('/box/<int:box_id>')
def box_detail(box_id):
    box = get_box(box_id)
    if not box:
        flash('箱が見つかりません', 'error')
        return redirect(url_for('file_container.index'))
    files = get_files(box_id)
    with get_db_cursor(database=DB) as (cursor, conn):
        cursor.execute("SELECT * FROM fc_events WHERE box_id=%s ORDER BY id DESC LIMIT 300", (box_id,))
        events = cursor.fetchall()
        cursor.execute(
            "SELECT file_id, COUNT(*) AS n FROM fc_events WHERE box_id=%s AND event='download' GROUP BY file_id",
            (box_id,))
        dl = {r['file_id']: r['n'] for r in cursor.fetchall()}
    for f in files:
        f['download_count'] = dl.get(f['id'], 0)
    names = user_names([box.get('created_by')] + [e.get('user_id') for e in events])
    box['live'] = box_is_live(box)
    box['expired'] = bool(box.get('expires_at') and box['expires_at'] < now_jst())
    return render_template('file_container/box.html', box=box, files=files, events=events,
                           names=names, url=public_url(box['token']),
                           home_tab=box['kind'])


@bp.route('/box/<int:box_id>/upload', methods=['POST'])
def admin_upload(box_id):
    box = get_box(box_id)
    if not box:
        abort(404)
    if request.content_length and request.content_length > MAX_BYTES * 10 + CHUNK:
        flash('一度に送る量が大きすぎます．分けて送ってください', 'error')
        return redirect(url_for('file_container.box_detail', box_id=box_id))
    saved, errors = store_files(box, request.files.getlist('files'), 'admin')
    if saved:
        flash(f'{saved}件のファイルを入れました', 'success')
    for msg in errors:
        flash(msg, 'error')
    if not saved and not errors:
        flash('ファイルを選んでください', 'error')
    return redirect(url_for('file_container.box_detail', box_id=box_id))


@bp.route('/box/<int:box_id>/file/<int:file_id>')
def admin_download(box_id, file_id):
    f = get_file(box_id, file_id)
    if not f or f['purged_at']:
        flash('ファイルの実体がありません', 'error')
        return redirect(url_for('file_container.box_detail', box_id=box_id))
    resp = send_stored(f)
    if resp is None:
        flash('ファイルの実体が見つかりません（サーバ上で失われています）', 'error')
        return redirect(url_for('file_container.box_detail', box_id=box_id))
    with get_db_cursor(database=DB) as (cursor, conn):
        log_event(cursor, box_id, 'download', file_id, f"管理画面から：{f['original_name']}")
        conn.commit()
    return resp


@bp.route('/box/<int:box_id>/file/<int:file_id>/purge', methods=['POST'])
def admin_purge(box_id, file_id):
    f = get_file(box_id, file_id)
    if not f or f['purged_at']:
        flash('すでに実体はありません', 'info')
    else:
        with get_db_cursor(database=DB) as (cursor, conn):
            ok = purge_file(cursor, f)
            conn.commit()
        flash(f"「{f['original_name']}」の実体を削除しました（記録は残ります）" if ok
              else '実体を削除できませんでした', 'success' if ok else 'error')
    return redirect(url_for('file_container.box_detail', box_id=box_id))


@bp.route('/box/<int:box_id>/finish', methods=['POST'])
def finish_box(box_id):
    """用事がすんだ：受付を止め，実体をすべて削除する（記録は残す）"""
    box = get_box(box_id)
    if not box:
        abort(404)
    n = 0
    with get_db_cursor(database=DB) as (cursor, conn):
        for f in get_files(box_id, include_purged=False):
            if purge_file(cursor, f, f"終了処理：{f['original_name']}"):
                n += 1
        if box['status'] != 'closed':
            cursor.execute("UPDATE fc_boxes SET status='closed', closed_at=%s, updated_at=%s WHERE id=%s",
                           (now_jst(), now_jst(), box_id))
            log_event(cursor, box_id, 'close', None, '終了処理')
        conn.commit()
    flash(f'受付を止め，実体を{n}件削除しました．記録は残っています', 'success')
    return redirect(url_for('file_container.box_detail', box_id=box_id))


@bp.route('/box/<int:box_id>/status', methods=['POST'])
def set_status(box_id):
    box = get_box(box_id)
    if not box:
        abort(404)
    to = request.form.get('to')
    if to not in STATUS_LABELS or to == box['status']:
        return redirect(url_for('file_container.box_detail', box_id=box_id))
    now = now_jst()
    with get_db_cursor(database=DB) as (cursor, conn):
        cursor.execute("UPDATE fc_boxes SET status=%s, closed_at=%s, updated_at=%s WHERE id=%s",
                       (to, now if to == 'closed' else None, now, box_id))
        log_event(cursor, box_id, 'close' if to == 'closed' else 'open')
        conn.commit()
    flash('受付を停止しました' if to == 'closed' else '受付を再開しました', 'success')
    return redirect(url_for('file_container.box_detail', box_id=box_id))


@bp.route('/box/<int:box_id>/edit', methods=['POST'])
def edit_box(box_id):
    box = get_box(box_id)
    if not box:
        abort(404)
    title = (request.form.get('title') or '').strip()
    note = (request.form.get('note') or '').strip()
    raw_exp = (request.form.get('expires_at') or '').strip()
    if not title or len(title) > 200:
        flash('題名（200字以内）を入れてください', 'error')
        return redirect(url_for('file_container.box_detail', box_id=box_id))
    expires = None
    if raw_exp:
        try:
            expires = datetime.strptime(raw_exp, '%Y-%m-%dT%H:%M')
        except ValueError:
            flash('期限の形式が正しくありません', 'error')
            return redirect(url_for('file_container.box_detail', box_id=box_id))
    with get_db_cursor(database=DB) as (cursor, conn):
        cursor.execute("UPDATE fc_boxes SET title=%s, note=%s, expires_at=%s, updated_at=%s WHERE id=%s",
                       (title, note, expires, now_jst(), box_id))
        log_event(cursor, box_id, 'edit')
        conn.commit()
    flash('箱の情報を更新しました', 'success')
    return redirect(url_for('file_container.box_detail', box_id=box_id))


@bp.route('/box/<int:box_id>/password', methods=['POST'])
def reset_password(box_id):
    """パスワードを作り直す（URL も作り直すかを選べる）"""
    box = get_box(box_id)
    if not box:
        abort(404)
    password = (request.form.get('password') or '').strip() or gen_password()
    if len(password) < 8:
        flash('パスワードは8文字以上にしてください', 'error')
        return redirect(url_for('file_container.box_detail', box_id=box_id))
    new_token = request.form.get('new_url') == '1'
    token = secrets.token_urlsafe(16) if new_token else box['token']
    with get_db_cursor(database=DB) as (cursor, conn):
        cursor.execute("UPDATE fc_boxes SET password_hash=%s, token=%s, updated_at=%s WHERE id=%s",
                       (generate_password_hash(password), token, now_jst(), box_id))
        log_event(cursor, box_id, 'password', None, 'URLも作り直し' if new_token else '')
        conn.commit()
    return render_template('file_container/credentials.html', box=get_box(box_id),
                           url=public_url(token), password=password, fresh=False,
                           home_tab=box['kind'])


@bp.route('/return_to_fujin')
def return_to_fujin():
    return redirect_to_dashboard()


# ─────────────────────────────────────────────────────────────────
# 受け渡し口（ログイン不要．箱のパスワードで開く）
# ─────────────────────────────────────────────────────────────────

def _unlock_key(box):
    # パスワードを変えると古い解錠は無効になる
    return f"fc_unlock_{box['id']}_{box['password_hash'][-12:]}"


def is_unlocked(box):
    until = session.get(_unlock_key(box))
    try:
        return bool(until) and float(until) > now_jst().timestamp()
    except (TypeError, ValueError):
        return False


def recent_failures(box):
    since = now_jst() - timedelta(minutes=FAIL_WINDOW_MINUTES)
    with get_db_cursor(database=DB) as (cursor, conn):
        cursor.execute("SELECT COUNT(*) AS n FROM fc_events WHERE box_id=%s AND event='auth_fail' AND created_at>=%s",
                       (box['id'], since))
        return int(cursor.fetchone()['n'])


def _closed_page():
    return render_template('file_container/public_msg.html',
                           message='この受け渡し口は閉じています．送り主にお問い合わせください．'), 404


@bp.route('/s/<token>', methods=['GET', 'POST'])
def public_entry(token):
    box = get_box(token=token[:64])
    if not box_is_live(box):
        return _closed_page()

    if request.method == 'POST' and request.form.get('action') == 'unlock':
        if recent_failures(box) >= FAIL_LIMIT:
            with get_db_cursor(database=DB) as (cursor, conn):
                log_event(cursor, box['id'], 'locked')
                conn.commit()
            flash(f'パスワードの誤りが続いたため，しばらく受け付けません（{FAIL_WINDOW_MINUTES}分ほどおいてください）', 'error')
            return redirect(url_for('file_container.public_entry', token=token))
        ok = check_password_hash(box['password_hash'], request.form.get('password') or '')
        with get_db_cursor(database=DB) as (cursor, conn):
            log_event(cursor, box['id'], 'auth_ok' if ok else 'auth_fail')
            conn.commit()
        if ok:
            session[_unlock_key(box)] = (now_jst() + timedelta(minutes=UNLOCK_MINUTES)).timestamp()
        else:
            flash('パスワードが違います', 'error')
        return redirect(url_for('file_container.public_entry', token=token))

    if not is_unlocked(box):
        return render_template('file_container/public_login.html', box=box)

    files = get_files(box['id'], include_purged=False)
    # 受け取る箱では，送ってきた人に他人の送ったファイル名を見せない（この解錠で送った分だけ）
    if box['kind'] == 'upload':
        mine = set(session.get(f"fc_sent_{box['id']}") or [])
        files = [f for f in files if f['id'] in mine]
    return render_template('file_container/public_box.html', box=box, files=files)


@bp.route('/s/<token>/f/<int:file_id>')
def public_download(token, file_id):
    box = get_box(token=token[:64])
    if not box_is_live(box) or box['kind'] != 'download':
        return _closed_page()
    if not is_unlocked(box):
        return redirect(url_for('file_container.public_entry', token=token))
    f = get_file(box['id'], file_id)
    if not f or f['purged_at']:
        return render_template('file_container/public_msg.html', message='このファイルはもうありません．'), 404
    resp = send_stored(f)
    if resp is None:
        return render_template('file_container/public_msg.html',
                               message='ファイルを読み出せませんでした．送り主にお問い合わせください．'), 404
    with get_db_cursor(database=DB) as (cursor, conn):
        log_event(cursor, box['id'], 'download', file_id, f['original_name'])
        conn.commit()
    return resp


@bp.route('/s/<token>/upload', methods=['POST'])
def public_upload(token):
    box = get_box(token=token[:64])
    if not box_is_live(box) or box['kind'] != 'upload':
        return _closed_page()
    if not is_unlocked(box):
        return redirect(url_for('file_container.public_entry', token=token))
    if request.content_length and request.content_length > MAX_BYTES * 10 + CHUNK:
        flash('一度に送る量が大きすぎます．分けて送ってください', 'error')
        return redirect(url_for('file_container.public_entry', token=token))
    label = (request.form.get('uploader_label') or '').strip()
    before = {f['id'] for f in get_files(box['id'])}
    saved, errors = store_files(box, request.files.getlist('files'), 'link', label)
    new_ids = [f['id'] for f in get_files(box['id']) if f['id'] not in before]
    key = f"fc_sent_{box['id']}"
    session[key] = list(session.get(key) or []) + new_ids
    if saved:
        flash(f'{saved}件のファイルを受け付けました．ありがとうございました', 'success')
    for msg in errors:
        flash(msg, 'error')
    if not saved and not errors:
        flash('ファイルを選んでください', 'error')
    return redirect(url_for('file_container.public_entry', token=token))
