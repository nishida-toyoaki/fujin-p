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
fujin_forum（えふえふ）- ルート定義 v1.2

【画面】
  GET  /fujin_forum/                     3列画面（チャンネル／記事一覧／記事とスレッド）
  GET  /fujin_forum/compose              記事の新規・返信・編集（MD エディタ，マイノートと同じ）
  POST /fujin_forum/compose              保存
  GET  /fujin_forum/import               すらくみのアーカイブ取込（admin）
  GET  /fujin_forum/return_to_fujin      ダッシュボードへ

【API】（すべて login_required．状態を変える POST は同一オリジン検査つき）
  GET  /api/channels                     見えるチャンネル一覧（未読数つき）
  POST /api/channels                     チャンネル作成（regular / admin）
  POST /api/channels/<id>                チャンネル設定（作成者 / admin）
  GET  /api/groups                       まいぐるのグループ一覧
  GET  /api/channels/<id>/posts          記事一覧（新しい順，before= でページング）
  GET  /api/posts/<id>                   記事とスレッド（HTML 描画済み）．既読を更新
  POST /api/posts/<id>/react             リアクションの付け外し
  POST /api/posts/<id>/delete            記事の削除（投稿者 / admin）
  POST /preview                          Markdown プレビュー
  POST /upload_image?channel=<id>        添付のアップロード（保護領域へ．リンクを返す）★v1.1
  GET  /file/<id>                        保護添付の配信（チャンネルの閲覧権で判定）★v1.1
  POST /api/attachments/<id>/publish     添付を公開領域に複製し画像表示できるようにする ★v1.1
  POST /api/attachments/<id>/unpublish   公開複製を消す ★v1.1
  GET  /api/import/sources               すらくみのアーカイブ済みチャンネル（admin）
  POST /api/import                       取込の実行（admin）
  GET  /export/<id>.json                 チャンネルの全記事を JSON で出力（admin）★v1.2
  POST /api/import_json                  その JSON の取込（単純追加．admin）★v1.2
  POST /api/channels/<id>/delete         チャンネルの完全削除（admin）★v1.2

【添付ファイルの扱い】★v1.1
  添付は保護領域 data/files/<channel_id>/ に置き，/file/<id> で配信する．配信の
  たびにチャンネルの閲覧権を判定するので，チャンネルの公開範囲を変えれば
  リンク先のアクセス権も自動で追随する．本文にはリンク [📎 名前](/fujin_forum/file/<id>)
  だけを書く．画像として表示したいときは，ユーザが「公開」操作で ~/static/ffimgs/
  に乱数名の複製を作り（ホスティングの規則上，static は権限で隠せない），本文の
  リンクを <img> に置き換える．描画時に /fujin_forum/file/… を指す <img> や ![]() は
  リンクに変換するので，保護ファイルが画像表示されることはない．
  取込（すらくみ）の添付もすべて保護領域に置き，リンクだけを書く．

【権限】
  見える人＝投稿できる人（アーカイブ済みチャンネルは読むだけ）．
  公開範囲はマイノートと同じ5区分＋まいぐるのグループ．private は作成者と admin．
  チャンネル作成は regular と admin，設定変更は作成者と admin，
  記事の編集・削除は投稿者と admin．
"""
import os
import re
import uuid
import json
import shutil
import logging
from functools import wraps
from urllib.parse import urlparse

import mysql.connector
import pytz
from datetime import datetime, timedelta
from flask import (render_template, request, jsonify, session, url_for,
                   redirect, flash, abort)
from werkzeug.utils import secure_filename

from decorators import login_required
from config import Config
from db import DatabaseConfig
from markdown_converter import process_markdown
from auth import redirect_to_dashboard

from . import fujin_forum_bp

# ── 定数 ───────────────────────────────────────────────────
JST = pytz.timezone('Asia/Tokyo')

SHARE_KEYS = ('private', 'public', 'domestic', 'group', 'domestic_group')
SHARE_LABELS = {
    'private': '非公開', 'public': 'ゲストにも', 'domestic': '構成員だけ',
    'group': 'グループ', 'domestic_group': '構成員＋グループ',
}

# 公開領域（画像表示用の複製．マイノートの mdimgs と同じ規則．乱数プレフィクス付き）
UPLOAD_BASE_DIR = getattr(Config, 'UPLOAD_BASE_DIR', None) or os.path.expanduser('~')
UPLOAD_SUBDIR = 'ffimgs'
UPLOAD_FOLDER = os.path.join(UPLOAD_BASE_DIR, 'static', UPLOAD_SUBDIR)
UPLOAD_URL_PREFIX = f'/static/{UPLOAD_SUBDIR}'
# 保護領域（添付の原本．アプリディレクトリ配下・実行時に自動生成・配布対象外）★v1.1
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
FILES_DIR = os.path.join(DATA_DIR, 'files')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'svg', 'pdf'}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAGIC_NUMBERS = {
    'png': (b'\x89PNG\r\n\x1a\n',), 'jpg': (b'\xff\xd8\xff',),
    'jpeg': (b'\xff\xd8\xff',), 'pdf': (b'%PDF-',),
}

# 取込で添付の実体を許す拡張子（それ以外はリンクだけ残す）
IMPORT_COPY_EXTENSIONS = ALLOWED_EXTENSIONS | {'gif', 'webp', 'txt', 'md', 'csv',
                                               'docx', 'xlsx', 'pptx', 'zip', 'json'}

REACTION_CHOICES = ['👍', '❤️', '🎉', '😄', '👀', '🙏', '✅', '💡']
PAGE_SIZE = 50


# ── 日時 ───────────────────────────────────────────────────

def get_jst_now():
    return datetime.now(JST).replace(tzinfo=None)


def fmt_dt(d):
    if not d:
        return ''
    if isinstance(d, datetime):
        return d.strftime('%Y-%m-%d %H:%M')
    return str(d)


# ── DB ────────────────────────────────────────────────────

def _db():
    return mysql.connector.connect(**DatabaseConfig.default())


# ── 同一オリジン検査（マイノートと同じ簡易 CSRF 対策）─────────

def origin_ok():
    for header in ('Origin', 'Referer'):
        value = request.headers.get(header)
        if value:
            return urlparse(value).netloc == request.host
    return True


def same_origin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not origin_ok():
            return jsonify({'success': False, 'error': '不正な要求元です'}), 403
        return view(*args, **kwargs)
    return wrapper


def admin_only_json(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if session.get('user_category') != 'admin':
            return jsonify({'success': False, 'error': 'この操作は管理者のみ実行できます'}), 403
        return view(*args, **kwargs)
    return wrapper


def _me():
    return (session.get('user_id'), session.get('user_category'),
            session.get('user_name') or session.get('user_email') or '')


def _is_admin():
    return session.get('user_category') == 'admin'


def _ok(**kw):
    kw['success'] = True
    return jsonify(kw)


def _err(msg, code=400):
    return jsonify({'success': False, 'error': msg}), code


# ── グループ（まいぐる．マイノートと同じ規則）──────────────────

def _user_active_group_ids(cur, user_id) -> set:
    if not user_id:
        return set()
    try:
        now = get_jst_now()
        cur.execute("""
            SELECT group_id FROM user_group_memberships
            WHERE user_id = %s
              AND (valid_from IS NULL OR valid_from <= %s)
              AND (valid_until IS NULL OR valid_until >= %s)
        """, (user_id, now, now))
        return {r['group_id'] for r in cur.fetchall()}
    except mysql.connector.Error as e:
        logging.warning("fujin_forum: user_group_memberships: %s", e)
        return set()


def _all_groups(cur) -> list:
    try:
        cur.execute("SELECT id, name FROM user_groups ORDER BY name")
        return cur.fetchall()
    except mysql.connector.Error as e:
        logging.warning("fujin_forum: user_groups: %s", e)
        return []


def _channel_group_ids(cur, channel_id) -> set:
    cur.execute("SELECT group_id FROM fujin_forum_access_groups WHERE channel_id=%s",
                (channel_id,))
    return {r['group_id'] for r in cur.fetchall()}


def _all_channel_groups(cur) -> dict:
    cur.execute("SELECT channel_id, group_id FROM fujin_forum_access_groups")
    out = {}
    for r in cur.fetchall():
        out.setdefault(r['channel_id'], set()).add(r['group_id'])
    return out


def _can_view(ch, user_id, category, ugids: set, allowed: set) -> bool:
    if category == 'admin':
        return True
    if ch.get('created_by') and ch['created_by'] == user_id:
        return True
    key = ch.get('share_key') or 'private'
    if key == 'public':
        return True
    if key == 'domestic':
        return category == 'regular'
    if key == 'group':
        return bool(ugids & allowed)
    if key == 'domestic_group':
        return category == 'regular' or bool(ugids & allowed)
    return False


def _can_manage(ch, user_id, category) -> bool:
    return category == 'admin' or (bool(ch.get('created_by')) and ch['created_by'] == user_id)


def _load_channel(cur, channel_id):
    cur.execute("SELECT * FROM fujin_forum_channels WHERE id=%s", (channel_id,))
    return cur.fetchone()


def _check_view(cur, channel_id):
    """閲覧できるチャンネル行を返す．できなければ 403，無ければ 404"""
    ch = _load_channel(cur, channel_id)
    if not ch:
        abort(404)
    uid, cat, _ = _me()
    if cat == 'admin':
        return ch
    allowed = _channel_group_ids(cur, channel_id) \
        if ch['share_key'] in ('group', 'domestic_group') else set()
    ugids = _user_active_group_ids(cur, uid) if allowed else set()
    if not _can_view(ch, uid, cat, ugids, allowed):
        abort(403)
    return ch


# ── Markdown ───────────────────────────────────────────────

_PROTECTED_IMG_RE = re.compile(
    r'<img\b[^>]*?\bsrc\s*=\s*["\']([^"\']*?/fujin_forum/file/(\d+)[^"\']*)["\'][^>]*>',
    re.IGNORECASE)


def _guard_protected_images(html_text: str) -> str:
    """保護添付（/fujin_forum/file/…）を指す <img> はリンクに変える（★v1.1 一本の規則）"""
    def repl(m):
        src = m.group(1)
        alt = re.search(r'\balt\s*=\s*["\']([^"\']*)["\']', m.group(0))
        label = (alt.group(1) if alt and alt.group(1) else f'添付 {m.group(2)}')
        import html as _h
        return f'<a href="{_h.escape(src)}" class="ff-att">📎 {_h.escape(label)}</a>'
    return _PROTECTED_IMG_RE.sub(repl, html_text)


def _md_html(md: str) -> str:
    try:
        out = process_markdown(md or '', session.get('user_category'))
    except Exception as e:
        logging.warning("fujin_forum: process_markdown: %s", e)
        import html as _h
        out = '<pre style="white-space:pre-wrap">' + _h.escape(md or '') + '</pre>'
    return _guard_protected_images(out)


_MD_STRIP_RE = re.compile(r'(!\[[^\]]*\]\([^)]*\)|<img[^>]*>|```[\s\S]*?```|`|\*\*|__|~~|^#{1,6}\s*|^>\s*|^\s*[-*+]\s+)',
                          re.M)


def _summary(md: str, n=120) -> str:
    """記事一覧に出す冒頭（Markdown 記号を落として1行に）"""
    text = _MD_STRIP_RE.sub('', md or '')
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = ' '.join(text.split())
    return text[:n] + ('…' if len(text) > n else '')


def _first_heading(md: str) -> str:
    m = re.search(r'^\s*#{1,3}\s+(.+?)\s*$', md or '', re.M)
    return m.group(1).strip() if m else ''


# ── リアクション ────────────────────────────────────────────

def _reactions_for(cur, post_ids: list, me_uid) -> dict:
    """{post_id: [{emoji, count, names, mine}]}"""
    if not post_ids:
        return {}
    ph = ','.join(['%s'] * len(post_ids))
    cur.execute(f"""
        SELECT post_id, user_id, reactor_name, emoji
        FROM fujin_forum_reactions WHERE post_id IN ({ph})
        ORDER BY id
    """, tuple(post_ids))
    agg = {}
    for r in cur.fetchall():
        d = agg.setdefault(r['post_id'], {}).setdefault(r['emoji'], {'emoji': r['emoji'], 'count': 0, 'names': [], 'mine': False})
        d['count'] += 1
        if r['reactor_name']:
            d['names'].append(r['reactor_name'])
        if me_uid and r['user_id'] == me_uid:
            d['mine'] = True
    return {pid: list(v.values()) for pid, v in agg.items()}


# ══════════════════════════════════════════════════════════════
# 画面
# ══════════════════════════════════════════════════════════════

@fujin_forum_bp.route('/return_to_fujin')
def return_to_fujin():
    return redirect_to_dashboard()


@fujin_forum_bp.route('/')
@login_required
def index():
    uid, cat, name = _me()
    return render_template('fujin_forum/index.html',
                           is_admin=(cat == 'admin'),
                           can_create=(cat in ('admin', 'regular')),
                           user_name=name, user_id=uid,
                           share_labels=SHARE_LABELS,
                           reaction_choices=REACTION_CHOICES)


@fujin_forum_bp.route('/compose', methods=['GET', 'POST'])
@login_required
def compose():
    """記事の新規（channel=）・返信（parent=）・編集（post=）"""
    uid, cat, name = _me()
    channel_id = request.values.get('channel', type=int)
    parent_id = request.values.get('parent', type=int)
    post_id = request.values.get('post', type=int)
    conn = _db()
    cur = conn.cursor(dictionary=True)
    try:
        post = None
        parent = None
        if post_id:
            cur.execute("SELECT * FROM fujin_forum_posts WHERE id=%s AND deleted_at IS NULL", (post_id,))
            post = cur.fetchone()
            if not post:
                abort(404)
            if not (cat == 'admin' or post['user_id'] == uid):
                abort(403)
            channel_id = post['channel_id']
            parent_id = post['parent_id']
        if parent_id:
            cur.execute("SELECT * FROM fujin_forum_posts WHERE id=%s AND deleted_at IS NULL", (parent_id,))
            parent = cur.fetchone()
            if not parent:
                abort(404)
            if parent['parent_id']:
                parent_id = parent['parent_id']     # 返信への返信は親スレッドにぶら下げる
                cur.execute("SELECT * FROM fujin_forum_posts WHERE id=%s", (parent_id,))
                parent = cur.fetchone()
            channel_id = parent['channel_id']
        if not channel_id:
            abort(400)
        ch = _check_view(cur, channel_id)
        if ch['is_archived'] and cat != 'admin':
            flash('このチャンネルはアーカイブされているため投稿できません', 'error')
            return redirect(url_for('fujin_forum.index') + f'#c={channel_id}')

        if request.method == 'POST':
            body = (request.form.get('content') or '').replace('\r\n', '\n').strip()
            if not body:
                flash('本文が空です', 'error')
                return redirect(request.url)
            now = get_jst_now()
            if post:
                cur.execute("""
                    UPDATE fujin_forum_posts SET body_md=%s, updated_at=%s, edited_at=%s
                    WHERE id=%s
                """, (body, now, now, post['id']))
                new_id = post['id']
                anchor = post['parent_id'] or post['id']
            else:
                cur.execute("""
                    INSERT INTO fujin_forum_posts
                        (channel_id, parent_id, user_id, author_name, body_md,
                         created_at, updated_at, source)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,'user')
                """, (channel_id, parent_id, uid, name, body, now, now))
                new_id = cur.lastrowid
                if parent_id:
                    cur.execute("""
                        UPDATE fujin_forum_posts
                        SET reply_count = reply_count + 1, last_reply_at=%s
                        WHERE id=%s
                    """, (now, parent_id))
                anchor = parent_id or new_id
            # 本文が参照する添付に post_id を付ける（★v1.1）
            _bind_attachments(cur, channel_id, new_id, body)
            # 自分の投稿は既読にする
            cur.execute("""
                INSERT INTO fujin_forum_reads (user_id, channel_id, last_read_at)
                VALUES (%s,%s,%s) ON DUPLICATE KEY UPDATE last_read_at=VALUES(last_read_at)
            """, (uid, channel_id, now))
            conn.commit()
            return redirect(url_for('fujin_forum.index') + f'#c={channel_id}&p={anchor}')

        parent_html = _md_html(parent['body_md']) if parent else ''
        return render_template('fujin_forum/compose.html',
                               ch=ch, post=post, parent=parent, parent_html=parent_html,
                               parent_at=fmt_dt(parent['created_at']) if parent else '',
                               content=(post['body_md'] if post else ''),
                               mode=('edit' if post else ('reply' if parent else 'new')),
                               back_url=url_for('fujin_forum.index') + f'#c={channel_id}' +
                                        (f'&p={parent_id}' if parent_id else ''))
    finally:
        cur.close(); conn.close()


@fujin_forum_bp.route('/import')
@login_required
def import_page():
    if not _is_admin():
        abort(403)
    return render_template('fujin_forum/import.html')


# ══════════════════════════════════════════════════════════════
# API：チャンネル
# ══════════════════════════════════════════════════════════════

def _channel_dict(ch, gids, can_manage):
    return {
        'id': ch['id'], 'name': ch['name'], 'description': ch.get('description') or '',
        'share_key': ch['share_key'], 'share_label': SHARE_LABELS.get(ch['share_key'], ch['share_key']),
        'group_ids': sorted(gids), 'is_archived': bool(ch['is_archived']),
        'created_by': ch.get('created_by'), 'created_at': fmt_dt(ch.get('created_at')),
        'slack_channel_id': ch.get('slack_channel_id') or '',
        'can_manage': can_manage,
    }


@fujin_forum_bp.route('/api/channels')
@login_required
def api_channels():
    uid, cat, _ = _me()
    conn = _db()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT * FROM fujin_forum_channels ORDER BY is_archived, sort_order, name")
        rows = cur.fetchall()
        allowed_all = _all_channel_groups(cur)
        ugids = _user_active_group_ids(cur, uid) if cat != 'admin' else set()
        # 未読数：last_read_at 以降の記事・返信（自分のは除く）
        cur.execute("SELECT channel_id, last_read_at FROM fujin_forum_reads WHERE user_id=%s", (uid,))
        reads = {r['channel_id']: r['last_read_at'] for r in cur.fetchall()}
        cur.execute("""
            SELECT channel_id, created_at, user_id FROM fujin_forum_posts
            WHERE deleted_at IS NULL AND created_at > %s
        """, (get_jst_now() - timedelta(days=60),))
        unread = {}
        for p in cur.fetchall():
            if p['user_id'] == uid:
                continue
            lr = reads.get(p['channel_id'])
            if lr is None or p['created_at'] > lr:
                unread[p['channel_id']] = unread.get(p['channel_id'], 0) + 1
        cur.execute("""
            SELECT channel_id, COUNT(*) AS n, MAX(COALESCE(last_reply_at, created_at)) AS last_at
            FROM fujin_forum_posts WHERE deleted_at IS NULL AND parent_id IS NULL
            GROUP BY channel_id
        """)
        stats = {r['channel_id']: r for r in cur.fetchall()}
        out = []
        for ch in rows:
            gids = allowed_all.get(ch['id'], set())
            if not _can_view(ch, uid, cat, ugids, gids):
                continue
            d = _channel_dict(ch, gids, _can_manage(ch, uid, cat))
            d['unread'] = unread.get(ch['id'], 0)
            st = stats.get(ch['id'])
            d['post_count'] = int(st['n']) if st else 0
            d['last_at'] = fmt_dt(st['last_at']) if st else ''
            out.append(d)
        return _ok(channels=out)
    finally:
        cur.close(); conn.close()


def _parse_channel_form(d, cur):
    name = (d.get('name') or '').strip().lstrip('#')
    if not name or len(name) > 100:
        return None, '名前は1〜100文字です'
    if re.search(r'[\s/\\?&=#]', name):
        return None, '名前に空白や記号（/ \\ ? & = #）は使えません'
    key = d.get('share_key') or 'private'
    if key not in SHARE_KEYS:
        return None, '公開範囲が不正です'
    try:
        gids = sorted({int(g) for g in (d.get('group_ids') or [])})
    except (TypeError, ValueError):
        return None, 'グループIDが不正です'
    if key in ('group', 'domestic_group'):
        if not gids:
            return None, 'グループを1つ以上選んでください'
    else:
        gids = []
    return {'name': name, 'description': (d.get('description') or '').strip()[:2000],
            'share_key': key, 'group_ids': gids,
            'is_archived': 1 if d.get('is_archived') else 0}, None


@fujin_forum_bp.route('/api/channels', methods=['POST'])
@login_required
@same_origin_required
def api_channel_create():
    uid, cat, _ = _me()
    if cat not in ('admin', 'regular'):
        return _err('チャンネルを作成できるのは構成員と管理者です', 403)
    conn = _db()
    cur = conn.cursor(dictionary=True)
    try:
        f, err = _parse_channel_form(request.get_json(silent=True) or {}, cur)
        if err:
            return _err(err)
        cur.execute("SELECT id FROM fujin_forum_channels WHERE name=%s", (f['name'],))
        if cur.fetchone():
            return _err('同じ名前のチャンネルがあります')
        now = get_jst_now()
        cur.execute("""
            INSERT INTO fujin_forum_channels
                (name, description, share_key, created_by, created_at, updated_at, is_archived, sort_order)
            VALUES (%s,%s,%s,%s,%s,%s,0,0)
        """, (f['name'], f['description'], f['share_key'], uid, now, now))
        cid = cur.lastrowid
        for g in f['group_ids']:
            cur.execute("INSERT INTO fujin_forum_access_groups (channel_id, group_id) VALUES (%s,%s)", (cid, g))
        conn.commit()
        return _ok(id=cid)
    except mysql.connector.Error as e:
        conn.rollback()
        return _err(f'データベースエラー: {e}', 500)
    finally:
        cur.close(); conn.close()


@fujin_forum_bp.route('/api/channels/<int:channel_id>', methods=['POST'])
@login_required
@same_origin_required
def api_channel_update(channel_id):
    uid, cat, _ = _me()
    conn = _db()
    cur = conn.cursor(dictionary=True)
    try:
        ch = _load_channel(cur, channel_id)
        if not ch:
            return _err('チャンネルがありません', 404)
        if not _can_manage(ch, uid, cat):
            return _err('設定を変更できるのは作成者と管理者です', 403)
        f, err = _parse_channel_form(request.get_json(silent=True) or {}, cur)
        if err:
            return _err(err)
        cur.execute("SELECT id FROM fujin_forum_channels WHERE name=%s AND id<>%s", (f['name'], channel_id))
        if cur.fetchone():
            return _err('同じ名前のチャンネルがあります')
        cur.execute("""
            UPDATE fujin_forum_channels
            SET name=%s, description=%s, share_key=%s, is_archived=%s, updated_at=%s
            WHERE id=%s
        """, (f['name'], f['description'], f['share_key'], f['is_archived'], get_jst_now(), channel_id))
        cur.execute("DELETE FROM fujin_forum_access_groups WHERE channel_id=%s", (channel_id,))
        for g in f['group_ids']:
            cur.execute("INSERT INTO fujin_forum_access_groups (channel_id, group_id) VALUES (%s,%s)", (channel_id, g))
        conn.commit()
        return _ok()
    except mysql.connector.Error as e:
        conn.rollback()
        return _err(f'データベースエラー: {e}', 500)
    finally:
        cur.close(); conn.close()


@fujin_forum_bp.route('/api/groups')
@login_required
def api_groups():
    conn = _db()
    cur = conn.cursor(dictionary=True)
    try:
        return _ok(groups=_all_groups(cur))
    finally:
        cur.close(); conn.close()


# ══════════════════════════════════════════════════════════════
# API：記事
# ══════════════════════════════════════════════════════════════

def _post_brief(p, reactions, me_uid, cat):
    return {
        'id': p['id'], 'author': p['author_name'] or '（不明）', 'user_id': p.get('user_id'),
        'created_at': fmt_dt(p['created_at']), 'edited': bool(p.get('edited_at')),
        'reply_count': int(p.get('reply_count') or 0), 'last_reply_at': fmt_dt(p.get('last_reply_at')),
        'title': _first_heading(p.get('body_md')), 'summary': _summary(p.get('body_md')),
        'source': p.get('source') or 'user',
        'reactions': reactions.get(p['id'], []),
        'can_edit': cat == 'admin' or (me_uid is not None and p.get('user_id') == me_uid),
    }


@fujin_forum_bp.route('/api/channels/<int:channel_id>/posts')
@login_required
def api_posts(channel_id):
    uid, cat, _ = _me()
    before = request.args.get('before', type=int)     # この id より小さい（古い）記事
    q = (request.args.get('q') or '').strip()
    conn = _db()
    cur = conn.cursor(dictionary=True)
    try:
        ch = _check_view(cur, channel_id)
        sql = """
            SELECT id, channel_id, user_id, author_name, body_md, created_at, edited_at,
                   reply_count, last_reply_at, source
            FROM fujin_forum_posts
            WHERE channel_id=%s AND parent_id IS NULL AND deleted_at IS NULL
        """
        args = [channel_id]
        if before:
            sql += " AND id < %s"; args.append(before)
        if q:
            sql += " AND (body_md LIKE %s OR author_name LIKE %s)"
            args += [f'%{q}%', f'%{q}%']
        sql += " ORDER BY created_at DESC, id DESC LIMIT %s"
        args.append(PAGE_SIZE + 1)
        cur.execute(sql, tuple(args))
        rows = cur.fetchall()
        more = len(rows) > PAGE_SIZE
        rows = rows[:PAGE_SIZE]
        reactions = _reactions_for(cur, [r['id'] for r in rows], uid)
        return _ok(posts=[_post_brief(r, reactions, uid, cat) for r in rows], more=more,
                   channel=_channel_dict(ch, _channel_group_ids(cur, channel_id), _can_manage(ch, uid, cat)),
                   can_post=(not ch['is_archived'] or cat == 'admin'))
    finally:
        cur.close(); conn.close()


@fujin_forum_bp.route('/api/posts/<int:post_id>')
@login_required
def api_post(post_id):
    uid, cat, _ = _me()
    conn = _db()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT * FROM fujin_forum_posts WHERE id=%s AND deleted_at IS NULL", (post_id,))
        p = cur.fetchone()
        if not p:
            return _err('記事がありません', 404)
        if p['parent_id']:
            post_id = p['parent_id']
            cur.execute("SELECT * FROM fujin_forum_posts WHERE id=%s", (post_id,))
            p = cur.fetchone()
        ch = _check_view(cur, p['channel_id'])
        cur.execute("""
            SELECT * FROM fujin_forum_posts
            WHERE parent_id=%s AND deleted_at IS NULL ORDER BY created_at, id
        """, (post_id,))
        replies = cur.fetchall()
        allp = [p] + replies
        reactions = _reactions_for(cur, [x['id'] for x in allp], uid)

        def full(x):
            d = _post_brief(x, reactions, uid, cat)
            d['html'] = _md_html(x['body_md'])
            d['md'] = x['body_md'] or ''
            return d

        # 既読（このチャンネルの最新まで）
        now = get_jst_now()
        cur.execute("""
            INSERT INTO fujin_forum_reads (user_id, channel_id, last_read_at)
            VALUES (%s,%s,%s) ON DUPLICATE KEY UPDATE last_read_at=VALUES(last_read_at)
        """, (uid, p['channel_id'], now))
        conn.commit()
        return _ok(post=full(p), replies=[full(r) for r in replies],
                   can_post=(not ch['is_archived'] or cat == 'admin'),
                   channel_id=p['channel_id'])
    finally:
        cur.close(); conn.close()


@fujin_forum_bp.route('/api/posts/<int:post_id>/react', methods=['POST'])
@login_required
@same_origin_required
def api_react(post_id):
    uid, cat, name = _me()
    emoji = ((request.get_json(silent=True) or {}).get('emoji') or '').strip()
    if not emoji or len(emoji) > 16:
        return _err('絵文字が不正です')
    conn = _db()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT channel_id FROM fujin_forum_posts WHERE id=%s AND deleted_at IS NULL", (post_id,))
        p = cur.fetchone()
        if not p:
            return _err('記事がありません', 404)
        _check_view(cur, p['channel_id'])
        cur.execute("SELECT id FROM fujin_forum_reactions WHERE post_id=%s AND user_id=%s AND emoji=%s",
                    (post_id, uid, emoji))
        r = cur.fetchone()
        if r:
            cur.execute("DELETE FROM fujin_forum_reactions WHERE id=%s", (r['id'],))
            state = 'removed'
        else:
            cur.execute("""
                INSERT INTO fujin_forum_reactions (post_id, user_id, reactor_name, emoji, created_at)
                VALUES (%s,%s,%s,%s,%s)
            """, (post_id, uid, name, emoji, get_jst_now()))
            state = 'added'
        conn.commit()
        return _ok(state=state, reactions=_reactions_for(cur, [post_id], uid).get(post_id, []))
    finally:
        cur.close(); conn.close()


@fujin_forum_bp.route('/api/posts/<int:post_id>/delete', methods=['POST'])
@login_required
@same_origin_required
def api_post_delete(post_id):
    uid, cat, _ = _me()
    conn = _db()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT * FROM fujin_forum_posts WHERE id=%s AND deleted_at IS NULL", (post_id,))
        p = cur.fetchone()
        if not p:
            return _err('記事がありません', 404)
        if not (cat == 'admin' or p['user_id'] == uid):
            return _err('削除できるのは投稿者と管理者です', 403)
        now = get_jst_now()
        cur.execute("UPDATE fujin_forum_posts SET deleted_at=%s WHERE id=%s", (now, post_id))
        if p['parent_id']:
            cur.execute("""
                UPDATE fujin_forum_posts SET reply_count = GREATEST(reply_count - 1, 0) WHERE id=%s
            """, (p['parent_id'],))
        else:
            cur.execute("UPDATE fujin_forum_posts SET deleted_at=%s WHERE parent_id=%s AND deleted_at IS NULL",
                        (now, post_id))
        conn.commit()
        return _ok(parent_id=p['parent_id'])
    finally:
        cur.close(); conn.close()


# ══════════════════════════════════════════════════════════════
# API：エディタ支援（マイノートと同じ）
# ══════════════════════════════════════════════════════════════

@fujin_forum_bp.route('/preview', methods=['POST'])
@login_required
@same_origin_required
def preview_markdown():
    data = request.get_json(silent=True) or {}
    return jsonify({'html': _md_html(data.get('markdown', ''))})


def _allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def _magic_ok(head, ext):
    sigs = MAGIC_NUMBERS.get(ext)
    return bool(sigs) and any(head.startswith(s) for s in sigs)


def _svg_head_ok(head):
    try:
        return '<svg' in head.decode('utf-8', errors='replace').lower()
    except Exception:
        return False


def _sanitize_svg(data):
    try:
        text = data.decode('utf-8', errors='replace')
    except Exception:
        return data
    text = re.sub(r'<script\b[^>]*>.*?</script\s*>', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'<script\b[^>]*/\s*>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\son\w+\s*=\s*"[^"]*"', '', text, flags=re.IGNORECASE)
    text = re.sub(r"\son\w+\s*=\s*'[^']*'", '', text, flags=re.IGNORECASE)
    text = re.sub(r'javascript\s*:', '', text, flags=re.IGNORECASE)
    return text.encode('utf-8')


_ATT_REF_RE = re.compile(r'/fujin_forum/file/(\d+)')


def _bind_attachments(cur, channel_id, post_id, body_md):
    ids = sorted({int(x) for x in _ATT_REF_RE.findall(body_md or '')})
    if not ids:
        return
    ph = ','.join(['%s'] * len(ids))
    cur.execute(f"UPDATE fujin_forum_attachments SET post_id=%s "
                f"WHERE channel_id=%s AND post_id IS NULL AND id IN ({ph})",
                (post_id, channel_id, *ids))


def _store_protected(channel_id, name, data: bytes, mimetype, uid, source, cur, post_id=None) -> dict:
    """保護領域にファイルを置き，attachments 行を作る．戻り値 {'id','url','name'}"""
    ext = name.rsplit('.', 1)[1].lower() if '.' in name else ''
    base, _ = os.path.splitext(secure_filename(name))
    unique = f"{uuid.uuid4().hex[:8]}_{base or 'file'}" + (f'.{ext}' if ext else '')
    rel = os.path.join(str(channel_id), unique)
    abs_path = os.path.join(FILES_DIR, rel)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, 'wb') as f:
        f.write(data)
    cur.execute("""
        INSERT INTO fujin_forum_attachments
            (post_id, channel_id, name, mimetype, size, local_path, public_path,
             uploaded_by, source, created_at)
        VALUES (%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s)
    """, (post_id, channel_id, name[:500], (mimetype or '')[:100], len(data), rel,
          uid, source, get_jst_now()))
    aid = cur.lastrowid
    return {'id': aid, 'url': url_for('fujin_forum.serve_file', aid=aid), 'name': name}


@fujin_forum_bp.route('/upload_image', methods=['POST'])
@login_required
@same_origin_required
def upload_image():
    """添付のアップロード（png / jpg / jpeg / svg / pdf，20MB まで）．
    ★v1.1 保護領域に置き，本文に書くリンク [📎 名前](/fujin_forum/file/<id>) を返す．
    query: channel=<channel_id>（投稿できるチャンネル）"""
    uid, cat, _ = _me()
    channel_id = request.args.get('channel', type=int) or request.form.get('channel', type=int)
    if not channel_id:
        return _err('チャンネルが指定されていません')
    if 'file' not in request.files:
        return _err('ファイルが選択されていません')
    file = request.files['file']
    if file.filename == '':
        return _err('ファイルが選択されていません')
    if not _allowed_file(file.filename):
        return _err('許可されていない形式です（png / jpg / jpeg / svg / pdf のみ）')
    ext = file.filename.rsplit('.', 1)[1].lower()
    data = file.read()
    if not data:
        return _err('ファイルが空です')
    if len(data) > MAX_UPLOAD_BYTES:
        return _err(f'ファイルサイズが上限（{MAX_UPLOAD_BYTES // (1024 * 1024)}MB）を超えています')
    if ext == 'svg':
        if not _svg_head_ok(data[:2048]):
            return _err('ファイルの内容が拡張子と一致しません')
        data = _sanitize_svg(data)
    elif not _magic_ok(data[:8], ext):
        return _err('ファイルの内容が拡張子と一致しません')
    conn = _db()
    cur = conn.cursor(dictionary=True)
    try:
        ch = _check_view(cur, channel_id)
        if ch['is_archived'] and cat != 'admin':
            return _err('アーカイブされたチャンネルには添付できません', 403)
        r = _store_protected(channel_id, file.filename, data, file.mimetype, uid, 'user', cur)
        conn.commit()
        return _ok(id=r['id'], url=r['url'], filename=r['name'],
                   kind='pdf' if ext == 'pdf' else 'image')
    except Exception as e:
        conn.rollback()
        logging.error("fujin_forum upload error: %s", e)
        return _err('アップロードに失敗しました', 500)
    finally:
        cur.close(); conn.close()


def _load_attachment(cur, aid):
    cur.execute("SELECT * FROM fujin_forum_attachments WHERE id=%s", (aid,))
    return cur.fetchone()


@fujin_forum_bp.route('/file/<int:aid>')
@login_required
def serve_file(aid):
    """保護添付の配信．配信のたびにチャンネルの閲覧権を判定する（★v1.1）"""
    conn = _db()
    cur = conn.cursor(dictionary=True)
    try:
        a = _load_attachment(cur, aid)
        if not a or not a.get('local_path'):
            abort(404)
        if a.get('post_id'):
            cur.execute("SELECT deleted_at FROM fujin_forum_posts WHERE id=%s", (a['post_id'],))
            p = cur.fetchone()
            if p and p.get('deleted_at'):
                abort(404)
        _check_view(cur, a['channel_id'])
    finally:
        cur.close(); conn.close()
    path = os.path.normpath(os.path.join(FILES_DIR, a['local_path']))
    if not path.startswith(os.path.normpath(FILES_DIR)) or not os.path.isfile(path):
        abort(404)
    from flask import send_file
    mt = a.get('mimetype') or 'application/octet-stream'
    inline = (mt.startswith('image/') and 'svg' not in mt) or mt == 'application/pdf'
    return send_file(path, mimetype=mt, as_attachment=not inline,
                     download_name=a.get('name') or f'file{aid}')


def _can_publish(a, uid, cat):
    """公開／非公開の操作は，添付した本人か admin（Slack 由来は admin だけ）"""
    return cat == 'admin' or (a.get('uploaded_by') is not None and a['uploaded_by'] == uid)


@fujin_forum_bp.route('/api/attachments/<int:aid>/publish', methods=['POST'])
@login_required
@same_origin_required
def api_att_publish(aid):
    """添付を公開領域（~/static/ffimgs/）に複製し，画像表示に使える URL を返す（★v1.1）"""
    uid, cat, _ = _me()
    conn = _db()
    cur = conn.cursor(dictionary=True)
    try:
        a = _load_attachment(cur, aid)
        if not a:
            return _err('添付がありません', 404)
        _check_view(cur, a['channel_id'])
        if not _can_publish(a, uid, cat):
            return _err('公開できるのは添付した本人と管理者です', 403)
        if a.get('public_path'):
            return _ok(url=a['public_path'], already=True)
        src = os.path.normpath(os.path.join(FILES_DIR, a['local_path'] or ''))
        if not src.startswith(os.path.normpath(FILES_DIR)) or not os.path.isfile(src):
            return _err('原本が見つかりません', 404)
        ext = (a.get('name') or '').rsplit('.', 1)[-1].lower() if '.' in (a.get('name') or '') else ''
        base, _ = os.path.splitext(secure_filename(a.get('name') or 'file'))
        unique = f"{uuid.uuid4().hex[:8]}_{base or 'file'}" + (f'.{ext}' if ext else '')
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        shutil.copyfile(src, os.path.join(UPLOAD_FOLDER, unique))
        pub = f"{UPLOAD_URL_PREFIX}/{unique}"
        cur.execute("UPDATE fujin_forum_attachments SET public_path=%s WHERE id=%s", (pub, aid))
        conn.commit()
        return _ok(url=pub, name=a.get('name'))
    finally:
        cur.close(); conn.close()


@fujin_forum_bp.route('/api/attachments/<int:aid>/unpublish', methods=['POST'])
@login_required
@same_origin_required
def api_att_unpublish(aid):
    """公開複製を消す（★v1.1）．本文の <img> をリンクに戻すのはエディタ側"""
    uid, cat, _ = _me()
    conn = _db()
    cur = conn.cursor(dictionary=True)
    try:
        a = _load_attachment(cur, aid)
        if not a:
            return _err('添付がありません', 404)
        _check_view(cur, a['channel_id'])
        if not _can_publish(a, uid, cat):
            return _err('非公開に戻せるのは添付した本人と管理者です', 403)
        pub = a.get('public_path')
        if pub and pub.startswith(UPLOAD_URL_PREFIX + '/'):
            p = os.path.normpath(os.path.join(UPLOAD_FOLDER, pub[len(UPLOAD_URL_PREFIX) + 1:]))
            if p.startswith(os.path.normpath(UPLOAD_FOLDER)) and os.path.isfile(p):
                try:
                    os.remove(p)
                except OSError as e:
                    logging.warning("fujin_forum unpublish remove: %s", e)
        cur.execute("UPDATE fujin_forum_attachments SET public_path=NULL WHERE id=%s", (aid,))
        conn.commit()
        return _ok(url=url_for('fujin_forum.serve_file', aid=aid), name=a.get('name'))
    finally:
        cur.close(); conn.close()


# ══════════════════════════════════════════════════════════════
# すらくみ（slack_minutes）アーカイブの取込（admin）
# ══════════════════════════════════════════════════════════════
#
# slack_minutes_* テーブルを直接読む．本文は Slack mrkdwn を MD に復元し
# （fujinp.slack_minutes.mrkdwn），添付は実体を ~/static/ffimgs/ にコピーして
# 本文末尾に画像／リンクとして付ける．返信はスレッドに，リアクションは絵文字と
# 人名で引き継ぐ．(channel_id, slack_ts) で照合するので何度実行しても重複しない．

try:
    from fujinp.slack_minutes.mrkdwn import mrkdwn_to_md as _mrkdwn_to_md, emoji as _slack_emoji
except Exception:                                   # すらくみが無いサイト
    _mrkdwn_to_md = None
    _slack_emoji = None

SLACK_FILES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               'slack_minutes', 'data', 'files')
SKIP_SLACK_SUBTYPES = ('channel_join', 'channel_leave', 'bot_message', 'channel_archive')


def _slack_md(text, users: dict, channels: dict) -> str:
    if _mrkdwn_to_md:
        return _mrkdwn_to_md(text or '', lambda u: users.get(u, u), lambda c: channels.get(c, c))
    import html as _h
    return _h.unescape(text or '')


def _copy_slack_file(f, cid, cur, post_id) -> str:
    """すらくみの保存済み添付を保護領域へコピーして attachments 行を作る（★v1.1）．
    戻り値: 本文に書くリンク先 URL（/fujin_forum/file/<id>）．取れなければ None"""
    if f.get('status') != 'done' or not f.get('local_path'):
        return None
    src = os.path.normpath(os.path.join(SLACK_FILES_DIR, f['local_path']))
    if not src.startswith(os.path.normpath(SLACK_FILES_DIR)) or not os.path.isfile(src):
        return None
    name = f.get('name') or os.path.basename(src)
    ext = name.rsplit('.', 1)[1].lower() if '.' in name else ''
    if ext not in IMPORT_COPY_EXTENSIONS:
        return None
    try:
        with open(src, 'rb') as fi:
            data = fi.read()
        if ext == 'svg':
            data = _sanitize_svg(data)
        r = _store_protected(cid, name, data, f.get('mimetype'), None, 'slack', cur, post_id=post_id)
        return r['url']
    except Exception as e:
        logging.warning("fujin_forum import: copy failed %s: %s", src, e)
        return None


@fujin_forum_bp.route('/api/import/sources')
@login_required
@admin_only_json
def api_import_sources():
    """すらくみのアーカイブ済みチャンネル一覧と，えふえふ側の取込先候補"""
    conn = _db()
    cur = conn.cursor(dictionary=True)
    try:
        try:
            cur.execute("""
                SELECT m.channel_id, COALESCE(MAX(c.name), MAX(m.channel_name)) AS name,
                       COUNT(*) AS total,
                       SUM(CASE WHEN m.thread_ts IS NULL OR m.thread_ts = m.slack_ts THEN 1 ELSE 0 END) AS parents,
                       MIN(m.posted_at) AS first_at, MAX(m.posted_at) AS last_at,
                       MAX(c.last_archived_at) AS last_archived_at,
                       (SELECT COUNT(*) FROM slack_minutes_files f
                         WHERE f.channel_id = m.channel_id AND f.status='done') AS files_done
                FROM slack_minutes_messages m
                LEFT JOIN slack_minutes_channels c ON c.channel_id = m.channel_id
                GROUP BY m.channel_id ORDER BY last_at DESC
            """)
            srcs = cur.fetchall()
        except mysql.connector.Error as e:
            return _err(f'すらくみのテーブルを読めません: {e}', 500)
        for s in srcs:
            for k in ('first_at', 'last_at', 'last_archived_at'):
                s[k] = fmt_dt(s.get(k))
            for k in ('total', 'parents', 'files_done'):
                s[k] = int(s.get(k) or 0)
        cur.execute("""
            SELECT c.id, c.name, c.slack_channel_id, c.is_archived,
                   (SELECT COUNT(*) FROM fujin_forum_posts p WHERE p.channel_id=c.id AND p.source='slack') AS imported
            FROM fujin_forum_channels c ORDER BY c.name
        """)
        targets = cur.fetchall()
        for t in targets:
            t['imported'] = int(t['imported'] or 0)
            t['is_archived'] = bool(t['is_archived'])
        return _ok(sources=srcs, targets=targets, mrkdwn=bool(_mrkdwn_to_md))
    finally:
        cur.close(); conn.close()


@fujin_forum_bp.route('/api/import', methods=['POST'])
@login_required
@same_origin_required
@admin_only_json
def api_import():
    """取込の実行．Body: { slack_channel_id, target: 'new'|'existing', channel_id, name, share_key }"""
    d = request.get_json(silent=True) or {}
    slack_cid = (d.get('slack_channel_id') or '').strip()
    if not slack_cid:
        return _err('取込元を選んでください')
    uid, cat, me_name = _me()
    conn = _db()
    cur = conn.cursor(dictionary=True)
    try:
        now = get_jst_now()
        # ── 取込先チャンネル ──
        if d.get('target') == 'existing':
            ch = _load_channel(cur, d.get('channel_id') or 0)
            if not ch:
                return _err('取込先のチャンネルがありません', 404)
            cid = ch['id']
        else:
            cur.execute("SELECT name FROM slack_minutes_channels WHERE channel_id=%s", (slack_cid,))
            r = cur.fetchone()
            if not r:
                cur.execute("SELECT channel_name AS name FROM slack_minutes_messages WHERE channel_id=%s LIMIT 1",
                            (slack_cid,))
                r = cur.fetchone()
            name = (d.get('name') or (r or {}).get('name') or slack_cid).strip().lstrip('#')
            name = re.sub(r'[\s/\\?&=#]', '-', name)[:100]
            share_key = d.get('share_key') if d.get('share_key') in SHARE_KEYS else 'private'
            cur.execute("SELECT id FROM fujin_forum_channels WHERE name=%s", (name,))
            if cur.fetchone():
                return _err(f'チャンネル名「{name}」は既にあります．既存への取込を選ぶか名前を変えてください')
            cur.execute("""
                INSERT INTO fujin_forum_channels
                    (name, description, share_key, created_by, created_at, updated_at,
                     is_archived, sort_order, slack_channel_id)
                VALUES (%s,%s,%s,%s,%s,%s,0,0,%s)
            """, (name, f'Slack #{name} のアーカイブから取込（{fmt_dt(now)}）', share_key,
                  uid, now, now, slack_cid))
            cid = cur.lastrowid
        cur.execute("UPDATE fujin_forum_channels SET slack_channel_id=%s, updated_at=%s WHERE id=%s",
                    (slack_cid, now, cid))

        # ── 名前解決の材料 ──
        users = {}
        try:
            cur.execute("SELECT user_id, name, display_name, real_name FROM slack_minutes_users")
            for u in cur.fetchall():
                users[u['user_id']] = u['display_name'] or u['real_name'] or u['name'] or u['user_id']
        except mysql.connector.Error:
            pass
        channels = {}
        try:
            cur.execute("SELECT channel_id, name FROM slack_minutes_channels")
            channels = {r['channel_id']: r['name'] for r in cur.fetchall()}
        except mysql.connector.Error:
            pass
        cur.execute("SELECT id, full_name FROM users WHERE deleted_at IS NULL AND full_name IS NOT NULL")
        by_name = {}
        for u in cur.fetchall():
            by_name.setdefault((u['full_name'] or '').strip(), u['id'])

        # ── 既に取り込んだ ts ──
        cur.execute("SELECT id, slack_ts, parent_id FROM fujin_forum_posts WHERE channel_id=%s AND slack_ts IS NOT NULL",
                    (cid,))
        existing = {r['slack_ts']: r['id'] for r in cur.fetchall()}

        # ── 元データ ──
        cur.execute("""
            SELECT slack_ts, sender_id, sender_name, text, posted_at, thread_ts, subtype,
                   edited_at, reactions_json
            FROM slack_minutes_messages WHERE channel_id=%s
            ORDER BY posted_at, slack_ts
        """, (slack_cid,))
        msgs = [m for m in cur.fetchall() if m.get('subtype') not in SKIP_SLACK_SUBTYPES]
        cur.execute("""
            SELECT slack_ts, name, mimetype, size, local_path, status
            FROM slack_minutes_files WHERE channel_id=%s ORDER BY id
        """, (slack_cid,))
        files = {}
        for f in cur.fetchall():
            files.setdefault(f['slack_ts'], []).append(f)

        have = {m['slack_ts'] for m in msgs}
        parents = [m for m in msgs if not m['thread_ts'] or m['thread_ts'] == m['slack_ts'] or m['thread_ts'] not in have]
        parent_ts = {m['slack_ts'] for m in parents}
        replies = [m for m in msgs if m['slack_ts'] not in parent_ts]

        added = skipped = files_n = reactions_n = 0

        def insert(m, parent_pid):
            nonlocal added, files_n, reactions_n
            md = _slack_md(m['text'], users, channels)
            author = (m['sender_name'] or users.get(m['sender_id']) or m['sender_id'] or '（不明）').strip()
            user_id = by_name.get(author)
            cur.execute("""
                INSERT INTO fujin_forum_posts
                    (channel_id, parent_id, user_id, author_name, body_md, created_at, updated_at,
                     edited_at, source, slack_ts)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'slack',%s)
            """, (cid, parent_pid, user_id, author, md, m['posted_at'] or now, now,
                  m['edited_at'], m['slack_ts']))
            pid = cur.lastrowid
            added += 1
            # 添付：保護領域にコピーし，本文末尾にリンクだけを付ける（画像も ![]() にしない）
            extra = []
            for f in files.get(m['slack_ts'], []):
                url = _copy_slack_file(f, cid, cur, pid)
                label = f.get('name') or 'file'
                if url:
                    extra.append(f'📎 [{label}]({url})')
                    files_n += 1
                else:
                    extra.append(f'📎 {label}（添付は取得できませんでした）')
            if extra:
                md = (md + '\n\n' if md else '') + '\n'.join(extra)
                cur.execute("UPDATE fujin_forum_posts SET body_md=%s WHERE id=%s", (md, pid))
            try:
                rs = json.loads(m['reactions_json']) if m.get('reactions_json') else []
            except Exception:
                rs = []
            for r in rs:
                em = _slack_emoji(r.get('name')) if _slack_emoji else f":{r.get('name')}:"
                names = list(r.get('users') or [])
                cnt = int(r.get('count') or len(names))
                while len(names) < cnt:
                    names.append('')
                for nm in names:
                    cur.execute("""
                        INSERT INTO fujin_forum_reactions (post_id, user_id, reactor_name, emoji, created_at)
                        VALUES (%s,NULL,%s,%s,%s)
                    """, (pid, nm, em, m['posted_at'] or now))
                    reactions_n += 1
            return pid

        pid_of = dict(existing)
        for m in parents:
            if m['slack_ts'] in existing:
                skipped += 1
                continue
            pid_of[m['slack_ts']] = insert(m, None)
        for m in replies:
            if m['slack_ts'] in existing:
                skipped += 1
                continue
            parent_pid = pid_of.get(m['thread_ts'])
            pid_of[m['slack_ts']] = insert(m, parent_pid)

        # 返信数・最終返信日時を再計算
        cur.execute("""
            SELECT parent_id, COUNT(*) AS n, MAX(created_at) AS la
            FROM fujin_forum_posts
            WHERE channel_id=%s AND parent_id IS NOT NULL AND deleted_at IS NULL
            GROUP BY parent_id
        """, (cid,))
        counts = {r['parent_id']: (r['n'], r['la']) for r in cur.fetchall()}
        cur.execute("UPDATE fujin_forum_posts SET reply_count=0, last_reply_at=NULL "
                    "WHERE channel_id=%s AND parent_id IS NULL", (cid,))
        for pid_, (n, la) in counts.items():
            cur.execute("UPDATE fujin_forum_posts SET reply_count=%s, last_reply_at=%s WHERE id=%s",
                        (n, la, pid_))
        conn.commit()
        return _ok(channel_id=cid, added=added, skipped=skipped, files=files_n, reactions=reactions_n,
                   url=url_for('fujin_forum.index') + f'#c={cid}')
    except mysql.connector.Error as e:
        conn.rollback()
        logging.error("fujin_forum import error: %s", e)
        return _err(f'データベースエラー: {e}', 500)
    finally:
        cur.close(); conn.close()


# ══════════════════════════════════════════════════════════════
# ★v1.2 チャンネル単位の JSON エクスポート／インポート／削除（admin）
# ══════════════════════════════════════════════════════════════
#
# エクスポート形式（export_type='fujinp_fujin_forum_channel'，format_version=1）
#   channel     : name, description, share_key, group_names, is_archived, slack_channel_id
#   posts       : ref（元 id）, parent_ref, author_name, author_email, body_md（添付参照は
#                 {{att:ref}}／公開複製は {{pub:ref}} に置換）, created_at, edited_at,
#                 source, slack_ts, reactions[{emoji, reactor_name, reactor_email}]
#   attachments : ref, name, mimetype, size, source, public（公開複製の有無）, data（base64）
# インポートは単純追加（重複排除なし）．新規チャンネルか既存チャンネルへ．

import base64

EXPORT_TYPE = 'fujinp_fujin_forum_channel'
EXPORT_FORMAT = 1
_PUB_URL_RE = re.compile(r'(?:https?://[^/\s"\')]+)?(/static/' + UPLOAD_SUBDIR + r'/[^\s"\')]+)')
_ATT_URL_RE = re.compile(r'(?:https?://[^/\s"\')]+)?/fujin_forum/file/(\d+)')


def _admin_page_or_403():
    if not _is_admin():
        abort(403)


@fujin_forum_bp.route('/export/<int:channel_id>.json')
@login_required
def export_channel_json(channel_id):
    """チャンネルの全記事・返信・リアクション・添付（base64）を JSON で出力（admin）"""
    _admin_page_or_403()
    conn = _db()
    cur = conn.cursor(dictionary=True)
    try:
        ch = _load_channel(cur, channel_id)
        if not ch:
            abort(404)
        gids = _channel_group_ids(cur, channel_id)
        gnames = []
        if gids:
            ph = ','.join(['%s'] * len(gids))
            cur.execute(f"SELECT name FROM user_groups WHERE id IN ({ph})", tuple(gids))
            gnames = [r['name'] for r in cur.fetchall()]
        cur.execute("""
            SELECT p.*, u.email AS author_email FROM fujin_forum_posts p
            LEFT JOIN users u ON u.id = p.user_id
            WHERE p.channel_id=%s AND p.deleted_at IS NULL ORDER BY p.created_at, p.id
        """, (channel_id,))
        posts = cur.fetchall()
        cur.execute("""
            SELECT r.*, u.email AS reactor_email FROM fujin_forum_reactions r
            LEFT JOIN users u ON u.id = r.user_id
            WHERE r.post_id IN (SELECT id FROM fujin_forum_posts WHERE channel_id=%s AND deleted_at IS NULL)
            ORDER BY r.id
        """, (channel_id,))
        rx = {}
        for r in cur.fetchall():
            rx.setdefault(r['post_id'], []).append({'emoji': r['emoji'], 'reactor_name': r['reactor_name'] or '',
                                                   'reactor_email': r.get('reactor_email')})
        cur.execute("SELECT * FROM fujin_forum_attachments WHERE channel_id=%s ORDER BY id", (channel_id,))
        atts = cur.fetchall()
        pub_map = {a['public_path']: a['id'] for a in atts if a.get('public_path')}

        def rewrite(body):
            body = _ATT_URL_RE.sub(lambda m: '{{att:%s}}' % m.group(1), body or '')
            body = _PUB_URL_RE.sub(lambda m: ('{{pub:%s}}' % pub_map[m.group(1)]) if m.group(1) in pub_map else m.group(0), body)
            return body

        out_posts = []
        for p in posts:
            out_posts.append({
                'ref': p['id'], 'parent_ref': p.get('parent_id'),
                'author_name': p.get('author_name') or '', 'author_email': p.get('author_email'),
                'body_md': rewrite(p.get('body_md')),
                'created_at': (p['created_at'].strftime('%Y-%m-%d %H:%M:%S')
                               if isinstance(p['created_at'], datetime) else str(p['created_at'] or '')),
                'edited_at': fmt_dt(p.get('edited_at')) or None,
                'source': p.get('source') or 'user', 'slack_ts': p.get('slack_ts'),
                'reactions': rx.get(p['id'], []),
            })
        out_atts = []
        for a in atts:
            data = None
            if a.get('local_path'):
                path = os.path.normpath(os.path.join(FILES_DIR, a['local_path']))
                if path.startswith(os.path.normpath(FILES_DIR)) and os.path.isfile(path):
                    with open(path, 'rb') as f:
                        data = base64.b64encode(f.read()).decode('ascii')
            out_atts.append({'ref': a['id'], 'post_ref': a.get('post_id'), 'name': a['name'],
                             'mimetype': a.get('mimetype'), 'size': a.get('size'),
                             'source': a.get('source') or 'user', 'public': bool(a.get('public_path')),
                             'data': data})
        doc = {
            'export_type': EXPORT_TYPE, 'format_version': EXPORT_FORMAT,
            'site_url': request.host_url.rstrip('/'), 'exported_at': fmt_dt(get_jst_now()),
            'exported_by': _me()[2],
            'channel': {'name': ch['name'], 'description': ch.get('description') or '',
                        'share_key': ch['share_key'], 'group_names': gnames,
                        'is_archived': bool(ch['is_archived']), 'slack_channel_id': ch.get('slack_channel_id')},
            'post_count': len(out_posts), 'attachment_count': len(out_atts),
            'posts': out_posts, 'attachments': out_atts,
        }
        from flask import Response
        from urllib.parse import quote
        body = json.dumps(doc, ensure_ascii=False, indent=1)
        fname = f"fujin_forum_{ch['name']}_{get_jst_now().strftime('%Y%m%d_%H%M')}.json"
        resp = Response(body, mimetype='application/json; charset=utf-8')
        resp.headers['Content-Disposition'] = (f'attachment; filename="fujin_forum_{channel_id}.json"; '
                                               f"filename*=UTF-8''{quote(fname)}")
        return resp
    finally:
        cur.close(); conn.close()


@fujin_forum_bp.route('/api/import_json', methods=['POST'])
@login_required
@same_origin_required
@admin_only_json
def api_import_json():
    """JSON からの取込（単純追加）．multipart: file, target=new|existing, channel_id, name"""
    if 'file' not in request.files:
        return _err('JSON ファイルを選んでください')
    try:
        doc = json.loads(request.files['file'].read().decode('utf-8'))
    except Exception as e:
        return _err(f'JSON を読めません: {e}')
    if doc.get('export_type') != EXPORT_TYPE:
        return _err('えふえふのチャンネル JSON ではありません（export_type）')
    uid, cat, _ = _me()
    target = request.form.get('target') or 'new'
    conn = _db()
    cur = conn.cursor(dictionary=True)
    try:
        now = get_jst_now()
        chd = doc.get('channel') or {}
        if target == 'existing':
            ch = _load_channel(cur, request.form.get('channel_id', type=int) or 0)
            if not ch:
                return _err('取込先のチャンネルがありません', 404)
            cid = ch['id']
        else:
            name = (request.form.get('name') or chd.get('name') or 'imported').strip().lstrip('#')
            name = re.sub(r'[\s/\\?&=#]', '-', name)[:100]
            cur.execute("SELECT id FROM fujin_forum_channels WHERE name=%s", (name,))
            if cur.fetchone():
                return _err(f'チャンネル名「{name}」は既にあります．名前を変えるか既存への追加を選んでください')
            share_key = chd.get('share_key') if chd.get('share_key') in SHARE_KEYS else 'private'
            gids = []
            if chd.get('group_names'):
                ph = ','.join(['%s'] * len(chd['group_names']))
                try:
                    cur.execute(f"SELECT id FROM user_groups WHERE name IN ({ph})", tuple(chd['group_names']))
                    gids = [r['id'] for r in cur.fetchall()]
                except mysql.connector.Error:
                    gids = []
            if share_key in ('group', 'domestic_group') and not gids:
                share_key = 'private'          # グループを引き当てられなければ安全側
            cur.execute("""
                INSERT INTO fujin_forum_channels
                    (name, description, share_key, created_by, created_at, updated_at,
                     is_archived, sort_order, slack_channel_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,0,%s)
            """, (name, chd.get('description') or '', share_key, uid, now, now,
                  1 if chd.get('is_archived') else 0, chd.get('slack_channel_id')))
            cid = cur.lastrowid
            for g in gids:
                cur.execute("INSERT INTO fujin_forum_access_groups (channel_id, group_id) VALUES (%s,%s)", (cid, g))

        # ユーザ引き当て（email → id，次に氏名）
        cur.execute("SELECT id, email, full_name FROM users WHERE deleted_at IS NULL")
        by_email, by_name = {}, {}
        for u in cur.fetchall():
            if u.get('email'):
                by_email.setdefault(u['email'].lower(), u['id'])
            if u.get('full_name'):
                by_name.setdefault(u['full_name'].strip(), u['id'])

        def resolve(email, name):
            if email and email.lower() in by_email:
                return by_email[email.lower()]
            return by_name.get((name or '').strip())

        # 既存チャンネルへ追加するとき，slack_ts が衝突するものは NULL にする（単純追加）
        cur.execute("SELECT slack_ts FROM fujin_forum_posts WHERE channel_id=%s AND slack_ts IS NOT NULL", (cid,))
        have_ts = {r['slack_ts'] for r in cur.fetchall()}

        # 添付を先に置く（ref → 新 id，公開複製の URL）
        att_map, pub_map = {}, {}
        atts_n = 0
        for a in doc.get('attachments') or []:
            if not a.get('data'):
                continue
            try:
                data = base64.b64decode(a['data'])
            except Exception:
                continue
            name = a.get('name') or 'file'
            ext = name.rsplit('.', 1)[1].lower() if '.' in name else ''
            if ext == 'svg':
                data = _sanitize_svg(data)
            r = _store_protected(cid, name, data, a.get('mimetype'), None if a.get('source') == 'slack' else uid,
                                 'slack' if a.get('source') == 'slack' else 'user', cur)
            att_map[str(a['ref'])] = r['id']
            atts_n += 1
            if a.get('public'):
                base_, _ = os.path.splitext(secure_filename(name))
                unique = f"{uuid.uuid4().hex[:8]}_{base_ or 'file'}" + (f'.{ext}' if ext else '')
                os.makedirs(UPLOAD_FOLDER, exist_ok=True)
                with open(os.path.join(UPLOAD_FOLDER, unique), 'wb') as f:
                    f.write(data)
                pub = f"{UPLOAD_URL_PREFIX}/{unique}"
                cur.execute("UPDATE fujin_forum_attachments SET public_path=%s WHERE id=%s", (pub, r['id']))
                pub_map[str(a['ref'])] = pub

        def rewrite(body):
            body = re.sub(r'\{\{att:(\d+)\}\}',
                          lambda m: url_for('fujin_forum.serve_file', aid=att_map[m.group(1)]) if m.group(1) in att_map else '#添付なし',
                          body or '')
            body = re.sub(r'\{\{pub:(\d+)\}\}',
                          lambda m: pub_map.get(m.group(1), url_for('fujin_forum.serve_file', aid=att_map[m.group(1)]) if m.group(1) in att_map else '#'),
                          body)
            body = re.sub(r'data-att="(\d+)"', lambda m: f'data-att="{att_map.get(m.group(1), m.group(1))}"', body)
            return body

        def parse_dt(s):
            for f in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
                try:
                    return datetime.strptime(s, f)
                except Exception:
                    pass
            return now

        posts = doc.get('posts') or []
        id_map = {}
        added = reactions_n = 0
        # 親を先に，返信を後に
        for pass_reply in (False, True):
            for p in posts:
                is_reply = p.get('parent_ref') is not None
                if is_reply != pass_reply:
                    continue
                parent_id = id_map.get(str(p['parent_ref'])) if is_reply else None
                if is_reply and parent_id is None:
                    parent_id = None                 # 親が無ければ親記事として入れる
                ts = p.get('slack_ts')
                if ts and ts in have_ts:
                    ts = None
                if ts:
                    have_ts.add(ts)
                body = rewrite(p.get('body_md'))
                cur.execute("""
                    INSERT INTO fujin_forum_posts
                        (channel_id, parent_id, user_id, author_name, body_md, created_at, updated_at,
                         edited_at, source, slack_ts)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (cid, parent_id, resolve(p.get('author_email'), p.get('author_name')),
                      (p.get('author_name') or '')[:200], body, parse_dt(p.get('created_at') or ''), now,
                      parse_dt(p['edited_at']) if p.get('edited_at') else None,
                      'slack' if p.get('source') == 'slack' else 'user', ts))
                pid = cur.lastrowid
                id_map[str(p['ref'])] = pid
                added += 1
                _bind_attachments(cur, cid, pid, body)
                for r in p.get('reactions') or []:
                    if not r.get('emoji'):
                        continue
                    cur.execute("""
                        INSERT INTO fujin_forum_reactions (post_id, user_id, reactor_name, emoji, created_at)
                        VALUES (%s,%s,%s,%s,%s)
                    """, (pid, resolve(r.get('reactor_email'), r.get('reactor_name')),
                          (r.get('reactor_name') or '')[:200], r['emoji'][:32], now))
                    reactions_n += 1
        # 返信数の再計算
        cur.execute("""
            SELECT parent_id, COUNT(*) AS n, MAX(created_at) AS la FROM fujin_forum_posts
            WHERE channel_id=%s AND parent_id IS NOT NULL AND deleted_at IS NULL GROUP BY parent_id
        """, (cid,))
        counts = {r['parent_id']: (r['n'], r['la']) for r in cur.fetchall()}
        cur.execute("UPDATE fujin_forum_posts SET reply_count=0, last_reply_at=NULL WHERE channel_id=%s AND parent_id IS NULL", (cid,))
        for pid_, (n, la) in counts.items():
            cur.execute("UPDATE fujin_forum_posts SET reply_count=%s, last_reply_at=%s WHERE id=%s", (n, la, pid_))
        conn.commit()
        return _ok(channel_id=cid, added=added, attachments=atts_n, reactions=reactions_n,
                   url=url_for('fujin_forum.index') + f'#c={cid}')
    except mysql.connector.Error as e:
        conn.rollback()
        logging.error("fujin_forum import_json error: %s", e)
        return _err(f'データベースエラー: {e}', 500)
    finally:
        cur.close(); conn.close()


@fujin_forum_bp.route('/api/channels/<int:channel_id>/delete', methods=['POST'])
@login_required
@same_origin_required
@admin_only_json
def api_channel_delete(channel_id):
    """チャンネルの完全削除（admin）．記事・返信・リアクション・添付（原本と公開複製）・既読・許可グループ"""
    d = request.get_json(silent=True) or {}
    conn = _db()
    cur = conn.cursor(dictionary=True)
    try:
        ch = _load_channel(cur, channel_id)
        if not ch:
            return _err('チャンネルがありません', 404)
        if (d.get('confirm_name') or '').strip().lstrip('#') != ch['name']:
            return _err('確認のためチャンネル名を正しく入力してください')
        cur.execute("SELECT * FROM fujin_forum_attachments WHERE channel_id=%s", (channel_id,))
        atts = cur.fetchall()
        cur.execute("DELETE FROM fujin_forum_reactions WHERE post_id IN "
                    "(SELECT id FROM fujin_forum_posts WHERE channel_id=%s)", (channel_id,))
        cur.execute("DELETE FROM fujin_forum_attachments WHERE channel_id=%s", (channel_id,))
        cur.execute("DELETE FROM fujin_forum_posts WHERE channel_id=%s", (channel_id,))
        cur.execute("DELETE FROM fujin_forum_reads WHERE channel_id=%s", (channel_id,))
        cur.execute("DELETE FROM fujin_forum_access_groups WHERE channel_id=%s", (channel_id,))
        cur.execute("DELETE FROM fujin_forum_channels WHERE id=%s", (channel_id,))
        conn.commit()
        # ファイルの掃除（DB 確定後．失敗しても記録だけ）
        removed = 0
        for a in atts:
            pub = a.get('public_path')
            if pub and pub.startswith(UPLOAD_URL_PREFIX + '/'):
                p = os.path.normpath(os.path.join(UPLOAD_FOLDER, pub[len(UPLOAD_URL_PREFIX) + 1:]))
                if p.startswith(os.path.normpath(UPLOAD_FOLDER)) and os.path.isfile(p):
                    try:
                        os.remove(p); removed += 1
                    except OSError:
                        pass
        d_dir = os.path.normpath(os.path.join(FILES_DIR, str(channel_id)))
        if d_dir.startswith(os.path.normpath(FILES_DIR)) and os.path.isdir(d_dir):
            try:
                shutil.rmtree(d_dir); removed += len(atts)
            except OSError as e:
                logging.warning("fujin_forum delete channel files: %s", e)
        return _ok(deleted=True, files_removed=removed)
    except mysql.connector.Error as e:
        conn.rollback()
        return _err(f'データベースエラー: {e}', 500)
    finally:
        cur.close(); conn.close()
