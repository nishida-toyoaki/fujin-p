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
slack_minutes - ルート定義（v2.3）

【エンドポイント一覧】
  GET  /                          admin：メイン画面（取得＋履歴＋アーカイブ一覧）
                                  regular/guest：公開アーカイブの一覧のみ  ★v2.1
  GET  /return_to_fujin           FUJIN-Pダッシュボードに戻る（標準仕様）
  GET  /api/workspace             連携先ワークスペース名・Bot名
  GET  /api/channels              Slackチャンネル一覧取得（is_member付き）
  GET  /api/probe/<cid>           Bot のチャンネル参加確認（probe）
  POST /api/fetch                 差分取得（v1.x互換：本文のみ・新規だけ保存）
  GET  /api/sessions              取得セッション一覧
  GET  /api/messages/<sid>        セッション内メッセージ一覧
  POST /api/delete/<sid>          セッション（取得記録）の削除    ★v2.0 挙動変更
  POST /api/archive/start         完全アーカイブ取得の開始         ★v2.0新設
  POST /api/archive/step/<sid>    完全アーカイブ取得を1区切り進める ★v2.0新設
  GET  /api/archives              アーカイブ済みチャンネル一覧     ★v2.0新設
  GET  /api/archives/<cid>/access 公開範囲と許可グループの取得（admin） ★v2.2
  POST /api/archives/<cid>/access 公開範囲と許可グループの設定（admin） ★v2.2
  GET  /channel/<cid>             チャンネル閲覧画面（目録）       ★v2.0新設
  GET  /api/catalog/<cid>         チャンネルの目録（親メッセージ一覧） ★v2.0新設
  GET  /api/thread/<cid>/<ts>     スレッド（親＋返信）の Markdown/HTML ★v2.0新設
  GET  /export/<cid>.md           チャンネル全体の Markdown 出力   ★v2.0新設
  GET  /file/<int:fid>            添付ファイルの配信（admin専用）  ★v2.0新設

【v2.3 変更点】
  - 添付の表示を，画像も含めてすべてリンク（📎 [名前](/slack_minutes/file/<id>)）にした．
    添付はチャンネルの閲覧権で配信するファイルであり，画像として本文中に表示するのは
    公開領域に置いたものだけ，というサイト共通の規則に揃えるため．

【v2.2 変更点：公開範囲をマイノート／コレポと同じ5区分に】
  - slack_minutes_channels.visibility を 5 値にした（SHARE_KEYS）．
      private        - admin のみ
      public         - ログイン済みの全ユーザ（ゲストにも）
      domestic       - 構成員（regular）だけ
      group          - 指定グループの有効所属者だけ
      domestic_group - 構成員または指定グループの有効所属者
    許可グループは slack_minutes_access_groups（channel_id, group_id）に持ち，
    まいぐる（user_groups / user_group_memberships）を有効期間チェック付きで
    参照する（マイノートの get_user_active_group_ids と同じ規則）．
  - 設定は admin だけ．アーカイブ一覧の「公開範囲」ボタンからモーダルで
    区分とグループを選ぶ（/api/archives/<cid>/access）．
  - 既定は private（新規アーカイブも既存行の移行も）．公開は admin が
    画面で設定する．再アーカイブしても設定は保持する．

【v2.1 変更点：閲覧の開放】
  - / を login_required にし，admin には従来のメイン画面，regular/guest には
    閲覧できるアーカイブの一覧（browse.html）だけを表示する．
  - チャンネル閲覧系（/channel, /api/catalog, /api/thread, /export, /file,
    /api/archives）を login_required にし，公開範囲で制御する．
  - 取得系（/api/channels, /api/fetch, /api/archive/*, /api/sessions,
    /api/messages, /api/delete, /api/workspace）は admin_required のまま．

【v2.0 変更点：完全アーカイブ化】
  - 完全アーカイブ取得（/api/archive/*）を新設．チャンネルの全メッセージを
    上書き同期（INSERT … ON DUPLICATE KEY UPDATE）し，スレッド返信
    （conversations.replies），リアクション，添付ファイル（実体をサーバに
    保存）を取り込む．Slack の生データは raw_json に保持する．
  - 取得はブラウザ主導の区切り実行（start → step を繰り返す）．1回の
    step は時間・件数で上限を切り，進捗を返す．Slack の rate limit
    （Retry-After）や PythonAnywhere のリクエスト時間制限に耐える．
    中断しても sessions.state_json から再開できる．
  - 本文は Slack mrkdwn のまま保存し，表示・出力時に mrkdwn.py で
    Markdown に復元する（メンション・リンク・書式・箇条書き）．
  - チャンネル閲覧画面（/channel/<cid>）を新設．セッション境界に関係なく
    チャンネル単位の目録（親メッセージ一覧）を表示し，行を開くと
    スレッド全体を Markdown レンダリングで閲覧できる．Markdown 一括出力付き．
  - ユーザー名・チャンネル名は DB（slack_minutes_users / _channels）に
    保持し，Slack 側が消えても復元できるようにした．
  - セッション削除は「取得記録の削除」に改め，メッセージは消さない．
    （v1.x はセッション配下のメッセージも物理削除していた．アーカイブでは
    メッセージがチャンネルの資産なので，記録と本文を切り離す）
  - bot_message / channel_join / channel_leave / channel_archive は
    v1.x と同じく取り込まない．

【必要スコープ】（v1.2 からの追加）
  files:read   - 添付ファイルのダウンロード（url_private_download）
  ※ reactions は conversations.history の応答に含まれるため追加不要
  ※ conversations.replies は channels:history / groups:history で動作
"""
import datetime
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request

from pytz import timezone

from flask import (render_template, request, jsonify, session,
                   send_file, Response, abort, url_for)
from werkzeug.exceptions import HTTPException
import mysql.connector

try:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
    _SLACK_SDK_OK = True
except ImportError:
    _SLACK_SDK_OK = False
    logging.warning("slack_minutes: slack_sdk が未インストールです")

    class SlackApiError(Exception):   # 名前だけ用意（except 節で参照するため）
        pass

# Markdown → HTML はサイト共通モジュールを優先し，無ければ markdown ライブラリ
try:
    from markdown_converter import process_markdown as _site_md
except Exception:
    _site_md = None
try:
    import markdown as _md_lib
except Exception:
    _md_lib = None

from config import Config
from db import DatabaseConfig
from decorators import admin_required, login_required
from auth import redirect_to_dashboard
from . import slack_minutes_bp
from .mrkdwn import mrkdwn_to_md, mrkdwn_to_plain, emoji, format_size

# ── 定数 ───────────────────────────────────────────────────
SKIP_SUBTYPES = ('channel_join', 'channel_leave', 'bot_message',
                 'channel_archive')

# 完全アーカイブ取得の 1 step あたりの上限
STEP_TIME_BUDGET   = 20      # 秒（PythonAnywhere の時間制限に余裕を残す）
HISTORY_PAGE_LIMIT = 200     # conversations.history の limit
HISTORY_PAGES_PER_STEP = 3
THREADS_PER_STEP   = 12
FILES_PER_STEP     = 6
FILE_SIZE_LIMIT    = 200 * 1024 * 1024   # 200MB を超える添付は保存しない

# 添付ファイルの保存先（アプリディレクトリ配下・実行時に自動生成・配布対象外）
DATA_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
FILES_DIR = os.path.join(DATA_DIR, 'files')

# ── 日時ヘルパー ────────────────────────────────────────────
JST = timezone('Asia/Tokyo')


def get_jst_now():
    """現在の日時をJSTで取得（naive datetime）"""
    return datetime.datetime.now(JST).replace(tzinfo=None)


def fmt_datetime(d):
    """datetime → 'YYYY-MM-DD HH:MM' 文字列"""
    if d is None:
        return ''
    if isinstance(d, (datetime.datetime, datetime.date)):
        return d.strftime('%Y-%m-%d %H:%M')
    return str(d)


def ts_to_jst(ts_str):
    """Slack の ts（UNIX秒文字列）→ JST naive datetime"""
    try:
        return datetime.datetime.fromtimestamp(
            float(ts_str), JST
        ).replace(tzinfo=None)
    except Exception:
        return None


def ts_to_str(ts_str):
    """Slack の ts → 'YYYY-MM-DD HH:MM' 文字列"""
    return fmt_datetime(ts_to_jst(ts_str))


# ── DB ────────────────────────────────────────────────────

def _db():
    return mysql.connector.connect(**DatabaseConfig.default())


# ── ★v2.2 公開範囲（マイノート／コレポ／文書アーカイブと同じ区分）──

SHARE_KEYS = ('private', 'public', 'domestic', 'group', 'domestic_group')
SHARE_LABELS = {
    'private':        '非公開',
    'public':         'ゲストにも',
    'domestic':       '構成員だけ',
    'group':          'グループ',
    'domestic_group': '構成員＋グループ',
}
SHARE_DESCRIPTIONS = {
    'private':        'admin だけが閲覧できます',
    'public':         'ログイン済みの全ユーザ（ゲストを含む）が閲覧できます',
    'domestic':       'ログイン済みの構成員だけが閲覧できます',
    'group':          '指定グループの所属者だけが閲覧できます',
    'domestic_group': '構成員と指定グループの所属者が閲覧できます',
}


def _is_admin() -> bool:
    return session.get('user_category') == 'admin'


def _user_active_group_ids(cur, user_id) -> set:
    """ユーザが現在有効に所属しているグループID（まいぐる．有効期間チェック付き）"""
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
        logging.warning("slack_minutes: user_group_memberships: %s", e)
        return set()


def _all_user_groups(cur) -> list:
    """全ユーザグループ（設定モーダルの選択肢）"""
    try:
        cur.execute("SELECT id, name FROM user_groups ORDER BY id DESC")
        return cur.fetchall()
    except mysql.connector.Error as e:
        logging.warning("slack_minutes: user_groups: %s", e)
        return []


def _channel_access_group_ids(cur, channel_id) -> set:
    cur.execute("SELECT group_id FROM slack_minutes_access_groups "
                "WHERE channel_id=%s", (channel_id,))
    return {r['group_id'] for r in cur.fetchall()}


def _can_view(visibility, user_category, user_group_ids: set,
              allowed_group_ids: set) -> bool:
    """公開範囲の判定（呼び出し元はすべて login_required 配下）"""
    if user_category == 'admin':
        return True
    if visibility == 'public':
        return True
    if visibility == 'domestic':
        return user_category == 'regular'
    if visibility == 'group':
        return bool(user_group_ids & allowed_group_ids)
    if visibility == 'domestic_group':
        return user_category == 'regular' or bool(user_group_ids & allowed_group_ids)
    return False          # private / 未設定


def _check_channel_access(cur, channel_id, conn=None):   # conn は互換のため残置
    """閲覧不可なら 403．戻り値はチャンネル行（無い場合は None）"""
    cur.execute("SELECT * FROM slack_minutes_channels WHERE channel_id=%s",
                (channel_id,))
    ch = cur.fetchone()
    if _is_admin():
        return ch
    vis = (ch or {}).get('visibility') or 'private'
    allowed = _channel_access_group_ids(cur, channel_id) \
        if vis in ('group', 'domestic_group') else set()
    ugids = _user_active_group_ids(cur, session.get('user_id')) \
        if vis in ('group', 'domestic_group') else set()
    if not _can_view(vis, session.get('user_category'), ugids, allowed):
        abort(403)
    return ch


# ── Slack クライアント ──────────────────────────────────────

def _slack_client():
    token = getattr(Config, 'SLACK_BOT_TOKEN', None)
    if not token:
        raise RuntimeError('SLACK_BOT_TOKEN が config.py に設定されていません')
    return WebClient(token=token)


class RateLimited(Exception):
    """Slack の ratelimited 応答（Retry-After 秒を保持）"""
    def __init__(self, retry_after):
        super().__init__(f'ratelimited ({retry_after}s)')
        self.retry_after = retry_after


def _call(client, method, **kw):
    """Slack API 呼び出し．ratelimited は RateLimited に変換して上位で扱う"""
    try:
        return getattr(client, method)(**kw)
    except SlackApiError as e:
        if e.response.get('error') == 'ratelimited':
            retry = int(e.response.headers.get('Retry-After', 30))
            raise RateLimited(retry)
        raise


# ── ワークスペース情報 ──────────────────────────────────────

_workspace_cache: dict = {}


def _workspace_info() -> dict:
    """{'workspaces': [名前, ...], 'bot_name': 'Bot名'} を返す"""
    if _workspace_cache:
        return _workspace_cache

    names = getattr(Config, 'SLACK_WORKSPACE_NAMES', None)
    if isinstance(names, str):
        names = [names]
    bot_name = getattr(Config, 'SLACK_BOT_NAME', None)

    if not names or not bot_name:
        try:
            resp = _slack_client().auth_test()
            if not names:
                names = [resp.get('team', '')]
            if not bot_name:
                bot_name = resp.get('user', '')
        except Exception as e:
            logging.warning("slack_minutes._workspace_info: %s", e)
            names = names or []
            bot_name = bot_name or ''

    _workspace_cache['workspaces'] = [n for n in names if n]
    _workspace_cache['bot_name'] = bot_name or ''
    return _workspace_cache


# ── チャンネル一覧取得 ──────────────────────────────────────

def _fetch_channels():
    client = _slack_client()
    channels = []
    cursor = None
    while True:
        kwargs = dict(
            types='public_channel,private_channel',
            exclude_archived=True,
            limit=200,
        )
        if cursor:
            kwargs['cursor'] = cursor
        try:
            resp = client.conversations_list(**kwargs)
        except SlackApiError as e:
            if e.response.get('error') == 'ratelimited':
                retry = int(e.response.headers.get('Retry-After', 30))
                time.sleep(min(retry, 30))
                continue
            raise
        for ch in resp.get('channels', []):
            channels.append({
                'id':          ch['id'],
                'name':        ch.get('name', ''),
                'is_private':  ch.get('is_private', False),
                'is_member':   ch.get('is_member', False),
                'num_members': ch.get('num_members', 0),
            })
        cursor = resp.get('response_metadata', {}).get('next_cursor')
        if not cursor:
            break
    channels.sort(key=lambda c: c['name'])
    return channels


# ── ユーザー名（DB キャッシュ付き）★v2.0 ─────────────────────
#
# v1.x はプロセス内キャッシュのみだったが，v2.0 では slack_minutes_users に
# 永続化する．Slack 側でアカウントが消えても，本文中のメンションを
# 表示名に復元できるようにするため．

_user_cache: dict = {}


def _user_display(row) -> str:
    return (row.get('display_name') or row.get('real_name')
            or row.get('name') or row.get('user_id') or '')


def _load_users_from_db(conn):
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT user_id, name, display_name, real_name "
                "FROM slack_minutes_users")
    for r in cur.fetchall():
        _user_cache[r['user_id']] = _user_display(r)
    cur.close()


def _resolve_username(client, user_id: str, conn=None) -> str:
    """Slack ユーザーID → 表示名（プロセス内＋DB キャッシュ）"""
    if not user_id:
        return '（不明）'
    if not _user_cache and conn is not None:
        try:
            _load_users_from_db(conn)
        except Exception as e:
            logging.warning("slack_minutes: users cache load failed: %s", e)
    if user_id in _user_cache:
        return _user_cache[user_id]
    if client is None:
        return user_id
    try:
        resp = client.users_info(user=user_id)
        u = resp.get('user', {}) or {}
        profile = u.get('profile', {}) or {}
        row = {'user_id': user_id,
               'name': u.get('name') or '',
               'display_name': profile.get('display_name') or '',
               'real_name': profile.get('real_name') or u.get('real_name') or '',
               'is_bot': 1 if u.get('is_bot') else 0,
               'deleted': 1 if u.get('deleted') else 0}
        name = _user_display(row) or user_id
        _user_cache[user_id] = name
        if conn is not None:
            try:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO slack_minutes_users
                        (user_id, name, display_name, real_name,
                         is_bot, deleted, fetched_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                        name=VALUES(name), display_name=VALUES(display_name),
                        real_name=VALUES(real_name), is_bot=VALUES(is_bot),
                        deleted=VALUES(deleted), fetched_at=VALUES(fetched_at)
                """, (user_id, row['name'], row['display_name'],
                      row['real_name'], row['is_bot'], row['deleted'],
                      get_jst_now()))
                conn.commit()
                cur.close()
            except Exception as e:
                logging.warning("slack_minutes: users upsert failed: %s", e)
        return name
    except Exception:
        _user_cache[user_id] = user_id
        return user_id


def _channel_name_resolver(conn):
    """<#C123> の解決関数（DB の slack_minutes_channels と messages から）"""
    names = {}
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT channel_id, name FROM slack_minutes_channels")
        for r in cur.fetchall():
            names[r['channel_id']] = r['name']
        cur.close()
    except Exception:
        pass
    return lambda cid: names.get(cid, cid)


# ── メッセージ保存（差分取得 v1.x 互換）──────────────────────

def _fetch_and_save(channel_id: str, channel_name: str,
                    oldest_ts, user_id: int) -> dict:
    """
    v1.x の差分取得．本文・送信者・投稿日時のみ，新規だけ保存する．
    oldest_ts: これより新しいメッセージのみ取得（None = 全件）
    """
    client = _slack_client()
    conn = _db()
    cur = conn.cursor(dictionary=True)
    session_id = None
    try:
        now = get_jst_now()
        cur.execute("""
            INSERT INTO slack_minutes_sessions
                (user_id, channel_id, channel_name, fetched_at, status, mode)
            VALUES (%s, %s, %s, %s, 'running', 'diff')
        """, (user_id, channel_id, channel_name, now))
        conn.commit()
        session_id = cur.lastrowid

        messages_raw = []
        cursor = None
        kwargs_base = dict(channel=channel_id, limit=HISTORY_PAGE_LIMIT)
        if oldest_ts:
            kwargs_base['oldest'] = oldest_ts
        while True:
            kw = dict(kwargs_base)
            if cursor:
                kw['cursor'] = cursor
            try:
                resp = _call(client, 'conversations_history', **kw)
            except RateLimited as rl:
                time.sleep(min(rl.retry_after, 30))
                continue
            messages_raw.extend(resp.get('messages', []))
            cursor = resp.get('response_metadata', {}).get('next_cursor')
            if not resp.get('has_more') or not cursor:
                break
            time.sleep(0.3)

        fetched = len(messages_raw)
        saved = skipped = 0
        for msg in messages_raw:
            if msg.get('subtype') in SKIP_SUBTYPES:
                skipped += 1
                continue
            ts = msg.get('ts', '')
            cur.execute("""
                SELECT id FROM slack_minutes_messages
                WHERE channel_id = %s AND slack_ts = %s LIMIT 1
            """, (channel_id, ts))
            if cur.fetchone():
                skipped += 1
                continue
            sender_id = msg.get('user', '')
            cur.execute("""
                INSERT INTO slack_minutes_messages
                    (session_id, channel_id, channel_name, slack_ts,
                     sender_id, sender_name, text, posted_at,
                     thread_ts, created_at, subtype, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (session_id, channel_id, channel_name, ts,
                  sender_id, _resolve_username(client, sender_id, conn),
                  msg.get('text', ''), ts_to_jst(ts),
                  msg.get('thread_ts'), now, msg.get('subtype'), now))
            saved += 1
        conn.commit()

        cur.execute("""
            UPDATE slack_minutes_sessions
            SET status = 'done', fetched_count = %s, saved_count = %s
            WHERE id = %s
        """, (fetched, saved, session_id))
        conn.commit()
        return {'session_id': session_id, 'fetched': fetched,
                'saved': saved, 'skipped': skipped}

    except Exception as e:
        conn.rollback()
        logging.error("slack_minutes._fetch_and_save error: %s", e)
        if session_id:
            try:
                cur.execute("UPDATE slack_minutes_sessions SET status='error' "
                            "WHERE id=%s", (session_id,))
                conn.commit()
            except Exception:
                pass
        raise
    finally:
        if conn.is_connected():
            cur.close()
            conn.close()


# ══════════════════════════════════════════════════════════════
# ★v2.0 完全アーカイブ取得
# ══════════════════════════════════════════════════════════════
#
# 状態（sessions.state_json）:
#   {
#     'phase':      'history' | 'threads' | 'files' | 'done',
#     'cursor':     conversations.history の次カーソル（history 中）,
#     'pages':      取得済みページ数,
#     'threads':    返信のある親 ts の一覧,
#     'thread_idx': 処理済み件数,
#     'file_total': 添付ファイル数（files フェーズ開始時に確定）,
#     'file_done':  処理済み件数,
#     'counts':     {'fetched','saved','updated','replies','skipped'}
#   }

def _upsert_message(cur, session_id, channel_id, channel_name, msg, client,
                    conn, now) -> str:
    """
    メッセージを INSERT … ON DUPLICATE KEY UPDATE で保存する．
    戻り値: 'saved' | 'updated' | 'same'
    """
    ts        = msg.get('ts', '')
    sender_id = msg.get('user', '') or msg.get('bot_id', '') or ''
    sender_name = _resolve_username(client, sender_id, conn) \
        if msg.get('user') else (msg.get('username') or sender_id or '（不明）')
    thread_ts = msg.get('thread_ts')
    edited    = (msg.get('edited') or {}).get('ts')
    reactions = []
    for r in msg.get('reactions', []) or []:
        reactions.append({
            'name':  r.get('name', ''),
            'count': r.get('count', 0),
            'users': [_resolve_username(client, u, conn)
                      for u in (r.get('users') or [])],
        })
    cur.execute("""
        INSERT INTO slack_minutes_messages
            (session_id, channel_id, channel_name, slack_ts,
             sender_id, sender_name, text, posted_at, thread_ts,
             created_at, subtype, reply_count, edited_at,
             reactions_json, raw_json, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            channel_name   = VALUES(channel_name),
            sender_id      = VALUES(sender_id),
            sender_name    = VALUES(sender_name),
            text           = VALUES(text),
            posted_at      = VALUES(posted_at),
            thread_ts      = VALUES(thread_ts),
            subtype        = VALUES(subtype),
            reply_count    = VALUES(reply_count),
            edited_at      = VALUES(edited_at),
            reactions_json = VALUES(reactions_json),
            raw_json       = VALUES(raw_json),
            updated_at     = VALUES(updated_at)
    """, (session_id, channel_id, channel_name, ts,
          sender_id, sender_name, msg.get('text', ''), ts_to_jst(ts),
          thread_ts, now, msg.get('subtype'),
          int(msg.get('reply_count') or 0),
          ts_to_jst(edited) if edited else None,
          json.dumps(reactions, ensure_ascii=False) if reactions else None,
          json.dumps(msg, ensure_ascii=False), now))
    rc = cur.rowcount          # 1=INSERT, 2=UPDATE(変更あり), 0=変更なし
    if rc == 1:
        return 'saved'
    return 'updated' if rc == 2 else 'same'


def _register_files(cur, channel_id, msg, now):
    """メッセージ内の添付ファイルを slack_minutes_files に登録（pending）"""
    ts = msg.get('ts', '')
    for f in msg.get('files', []) or []:
        fid = f.get('id')
        if not fid:
            continue
        mode = f.get('mode', '')
        # 削除済み・アクセス不可のファイルは Slack が実体を返さない
        status = 'pending'
        if mode in ('tombstone', 'hidden_by_limit') or f.get('file_access') in (
                'access_denied', 'file_not_found', 'check_file_info'):
            status = 'expired'
        url = f.get('url_private_download') or f.get('url_private') or ''
        cur.execute("""
            INSERT INTO slack_minutes_files
                (file_id, channel_id, slack_ts, name, title, mimetype,
                 filetype, size, url_private, status, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
                name=VALUES(name), title=VALUES(title),
                mimetype=VALUES(mimetype), filetype=VALUES(filetype),
                size=VALUES(size), url_private=VALUES(url_private),
                status = IF(status='done', 'done', VALUES(status))
        """, (fid, channel_id, ts,
              (f.get('name') or f.get('title') or fid)[:500],
              (f.get('title') or '')[:500],
              (f.get('mimetype') or '')[:100],
              (f.get('filetype') or '')[:32],
              f.get('size'), url, status, now))


_SAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _local_path_for(channel_id, file_id, name):
    safe = _SAFE_NAME_RE.sub('_', name or '')[:120] or 'file'
    return os.path.join(channel_id, f'{file_id}_{safe}')


def _download_file(token, row) -> tuple:
    """
    url_private_download から実体を取得して保存する．
    戻り値: (status, local_path or None, error or None)
    """
    url = row.get('url_private') or ''
    if not url:
        return ('expired', None, 'URLなし')
    if row.get('size') and int(row['size']) > FILE_SIZE_LIMIT:
        return ('error', None, f'サイズ上限超過（{format_size(row["size"])}）')
    rel = _local_path_for(row['channel_id'], row['file_id'], row['name'])
    abs_path = os.path.join(FILES_DIR, rel)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    req = urllib.request.Request(url, headers={
        'Authorization': f'Bearer {token}',
        'User-Agent': 'FUJIN-P slack_minutes/2.0',
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            ctype = resp.headers.get('Content-Type', '')
            data = resp.read(FILE_SIZE_LIMIT + 1)
        if len(data) > FILE_SIZE_LIMIT:
            return ('error', None, 'サイズ上限超過')
        # 認証失敗時は HTML のログインページが返ることがある
        if 'text/html' in ctype and not (row.get('mimetype') or '').startswith('text/html'):
            head = data[:300].lower()
            if b'<html' in head or b'<!doctype' in head:
                return ('error', None,
                        'Slack が HTML を返しました（files:read スコープ未付与の可能性）')
        with open(abs_path, 'wb') as fp:
            fp.write(data)
        return ('done', rel, None)
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            return ('expired', None, f'HTTP {e.code}')
        return ('error', None, f'HTTP {e.code}')
    except Exception as e:
        return ('error', None, str(e)[:500])


def _sync_channel_info(conn, client, channel_id, channel_name):
    """slack_minutes_channels を更新（名前・種別・topic・purpose）"""
    now = get_jst_now()
    info = {}
    try:
        resp = client.conversations_info(channel=channel_id)
        info = resp.get('channel', {}) or {}
    except Exception as e:
        logging.warning("slack_minutes: conversations_info failed: %s", e)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO slack_minutes_channels
            (channel_id, name, is_private, topic, purpose,
             slack_created_at, last_archived_at, updated_at, visibility)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            name=VALUES(name), is_private=VALUES(is_private),
            topic=VALUES(topic), purpose=VALUES(purpose),
            slack_created_at=VALUES(slack_created_at),
            last_archived_at=VALUES(last_archived_at),
            updated_at=VALUES(updated_at)
    """, (channel_id, info.get('name') or channel_name,
          1 if info.get('is_private') else 0,
          ((info.get('topic') or {}).get('value') or '')[:2000],
          ((info.get('purpose') or {}).get('value') or '')[:2000],
          ts_to_jst(info['created']) if info.get('created') else None,
          now, now, 'private'))    # 新規は非公開．公開は admin が設定する
    conn.commit()
    cur.close()


def _archive_start(channel_id, channel_name, user_id) -> dict:
    client = _slack_client()
    conn = _db()
    try:
        _sync_channel_info(conn, client, channel_id, channel_name)
        state = {'phase': 'history', 'cursor': None, 'pages': 0,
                 'threads': [], 'thread_idx': 0,
                 'file_total': 0, 'file_done': 0,
                 'counts': {'fetched': 0, 'saved': 0, 'updated': 0,
                            'replies': 0, 'skipped': 0}}
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO slack_minutes_sessions
                (user_id, channel_id, channel_name, fetched_at, status,
                 mode, phase, state_json)
            VALUES (%s,%s,%s,%s,'running','archive','history',%s)
        """, (user_id, channel_id, channel_name, get_jst_now(),
              json.dumps(state, ensure_ascii=False)))
        conn.commit()
        sid = cur.lastrowid
        cur.close()
        return {'session_id': sid, 'state': state}
    finally:
        if conn.is_connected():
            conn.close()


def _progress(state) -> dict:
    """フロントへ返す進捗情報"""
    c = state['counts']
    ph = state['phase']
    if ph == 'history':
        msg = f'履歴を取得中… {state["pages"]}ページ／{c["fetched"]}件'
    elif ph == 'threads':
        msg = f'スレッド返信を取得中… {state["thread_idx"]}／{len(state["threads"])}件'
    elif ph == 'files':
        msg = f'添付ファイルを保存中… {state["file_done"]}／{state["file_total"]}件'
    else:
        msg = '完了'
    return {'phase': ph, 'message': msg, 'counts': c,
            'threads_total': len(state['threads']),
            'thread_idx': state['thread_idx'],
            'file_total': state['file_total'],
            'file_done': state['file_done'],
            'done': ph == 'done'}


def _archive_step(sid: int) -> dict:
    """完全アーカイブ取得を 1 区切り進める（時間・件数で上限）"""
    client = _slack_client()
    token = getattr(Config, 'SLACK_BOT_TOKEN', '')
    conn = _db()
    cur = conn.cursor(dictionary=True)
    started = time.time()

    def budget_left():
        return (time.time() - started) < STEP_TIME_BUDGET

    try:
        cur.execute("SELECT * FROM slack_minutes_sessions WHERE id=%s", (sid,))
        srow = cur.fetchone()
        if not srow:
            raise RuntimeError('セッションがありません')
        if srow.get('mode') != 'archive':
            raise RuntimeError('完全アーカイブのセッションではありません')
        state = json.loads(srow.get('state_json') or '{}')
        if not state:
            raise RuntimeError('セッション状態がありません（開始からやり直してください）')
        if state.get('phase') == 'done':
            return {**_progress(state), 'session_id': sid}

        channel_id   = srow['channel_id']
        channel_name = srow['channel_name']
        counts = state['counts']
        now = get_jst_now()
        wait = 0    # rate limit で待つ秒数（フロントに知らせる）

        try:
            # ── phase 1: history ──────────────────────────
            if state['phase'] == 'history':
                pages = 0
                while budget_left() and pages < HISTORY_PAGES_PER_STEP:
                    kw = dict(channel=channel_id, limit=HISTORY_PAGE_LIMIT)
                    if state.get('cursor'):
                        kw['cursor'] = state['cursor']
                    resp = _call(client, 'conversations_history', **kw)
                    msgs = resp.get('messages', []) or []
                    pages += 1
                    state['pages'] += 1
                    counts['fetched'] += len(msgs)
                    for msg in msgs:
                        if msg.get('subtype') in SKIP_SUBTYPES:
                            counts['skipped'] += 1
                            continue
                        r = _upsert_message(cur, sid, channel_id, channel_name,
                                            msg, client, conn, now)
                        if r == 'saved':
                            counts['saved'] += 1
                        elif r == 'updated':
                            counts['updated'] += 1
                        _register_files(cur, channel_id, msg, now)
                        if int(msg.get('reply_count') or 0) > 0 and \
                                msg.get('ts') not in state['threads']:
                            state['threads'].append(msg['ts'])
                    conn.commit()
                    nxt = (resp.get('response_metadata') or {}).get('next_cursor')
                    if resp.get('has_more') and nxt:
                        state['cursor'] = nxt
                    else:
                        state['cursor'] = None
                        state['phase'] = 'threads'
                        break

            # ── phase 2: threads ──────────────────────────
            if state['phase'] == 'threads':
                n = 0
                while budget_left() and n < THREADS_PER_STEP and \
                        state['thread_idx'] < len(state['threads']):
                    parent_ts = state['threads'][state['thread_idx']]
                    cursor = None
                    while True:
                        kw = dict(channel=channel_id, ts=parent_ts, limit=200)
                        if cursor:
                            kw['cursor'] = cursor
                        try:
                            resp = _call(client, 'conversations_replies', **kw)
                        except SlackApiError as e:
                            err = e.response.get('error', '')
                            if err == 'thread_not_found':
                                break
                            raise
                        for msg in resp.get('messages', []) or []:
                            if msg.get('ts') == parent_ts:
                                # 親は history で保存済み（reply_count 等はそちらが正）
                                continue
                            if msg.get('subtype') in SKIP_SUBTYPES:
                                counts['skipped'] += 1
                                continue
                            r = _upsert_message(cur, sid, channel_id,
                                                channel_name, msg, client,
                                                conn, now)
                            counts['replies'] += 1
                            if r == 'saved':
                                counts['saved'] += 1
                            elif r == 'updated':
                                counts['updated'] += 1
                            _register_files(cur, channel_id, msg, now)
                        conn.commit()
                        nxt = (resp.get('response_metadata') or {}).get('next_cursor')
                        if resp.get('has_more') and nxt:
                            cursor = nxt
                        else:
                            break
                    state['thread_idx'] += 1
                    n += 1
                if state['thread_idx'] >= len(state['threads']):
                    cur.execute("""
                        SELECT COUNT(*) AS c FROM slack_minutes_files
                        WHERE channel_id=%s AND status='pending'
                    """, (channel_id,))
                    state['file_total'] = cur.fetchone()['c']
                    state['file_done'] = 0
                    state['phase'] = 'files'

            # ── phase 3: files ────────────────────────────
            if state['phase'] == 'files':
                n = 0
                while budget_left() and n < FILES_PER_STEP:
                    cur.execute("""
                        SELECT * FROM slack_minutes_files
                        WHERE channel_id=%s AND status='pending'
                        ORDER BY id LIMIT 1
                    """, (channel_id,))
                    frow = cur.fetchone()
                    if not frow:
                        break
                    status, rel, err = _download_file(token, frow)
                    cur.execute("""
                        UPDATE slack_minutes_files
                        SET status=%s, local_path=%s, error=%s,
                            downloaded_at=%s
                        WHERE id=%s
                    """, (status, rel, err,
                          get_jst_now() if status == 'done' else None,
                          frow['id']))
                    conn.commit()
                    state['file_done'] += 1
                    n += 1
                cur.execute("""
                    SELECT COUNT(*) AS c FROM slack_minutes_files
                    WHERE channel_id=%s AND status='pending'
                """, (channel_id,))
                if cur.fetchone()['c'] == 0:
                    state['phase'] = 'done'

        except RateLimited as rl:
            wait = min(rl.retry_after, 60)

        # ── 状態保存 ──────────────────────────────────
        done = state['phase'] == 'done'
        cur.execute("""
            UPDATE slack_minutes_sessions
            SET phase=%s, state_json=%s, status=%s,
                fetched_count=%s, saved_count=%s, updated_count=%s,
                reply_count=%s, file_count=%s
            WHERE id=%s
        """, (state['phase'], json.dumps(state, ensure_ascii=False),
              'done' if done else 'running',
              counts['fetched'], counts['saved'], counts['updated'],
              counts['replies'], state['file_total'], sid))
        if done:
            cur.execute("""
                UPDATE slack_minutes_channels SET last_archived_at=%s
                WHERE channel_id=%s
            """, (get_jst_now(), channel_id))
        conn.commit()
        return {**_progress(state), 'session_id': sid, 'wait': wait}

    except Exception as e:
        logging.error("slack_minutes._archive_step error: %s", e)
        try:
            conn.rollback()
            cur.execute("UPDATE slack_minutes_sessions SET status='error' "
                        "WHERE id=%s", (sid,))
            conn.commit()
        except Exception:
            pass
        raise
    finally:
        if conn.is_connected():
            cur.close()
            conn.close()


# ══════════════════════════════════════════════════════════════
# ★v2.0 閲覧・出力（Markdown 復元）
# ══════════════════════════════════════════════════════════════

def _files_for_channel(cur, channel_id) -> dict:
    """{slack_ts: [file行, …]}"""
    cur.execute("""
        SELECT id, file_id, slack_ts, name, title, mimetype, size,
               local_path, status
        FROM slack_minutes_files WHERE channel_id=%s ORDER BY id
    """, (channel_id,))
    out = {}
    for f in cur.fetchall():
        out.setdefault(f['slack_ts'], []).append(f)
    return out


def _file_url(f, absolute=False):
    return url_for('slack_minutes.serve_file', fid=f['id'], _external=absolute)


def _files_md(files, absolute=False) -> str:
    """添付ファイルの Markdown 断片（画像は埋め込み，他はリンク）"""
    lines = []
    for f in files or []:
        name = f.get('name') or f.get('title') or f.get('file_id')
        size = format_size(f.get('size'))
        if f.get('status') == 'done' and f.get('local_path'):
            url = _file_url(f, absolute)
            # ★v2.3 画像も埋め込み（![]()）にせずリンクにする．添付はチャンネルの
            #   閲覧権で配信され，画像表示は公開領域に置いたものだけ，という規則に揃える
            lines.append(f'📎 [{name}]({url}){"（" + size + "）" if size else ""}')
        else:
            note = {'expired': '期限切れ・取得不可', 'error': '取得失敗',
                    'pending': '未取得'}.get(f.get('status'), '')
            lines.append(f'📎 {name}{"（" + size + "）" if size else ""} ※{note}')
    return '\n'.join(lines)


def _reactions_text(reactions_json) -> str:
    try:
        rs = json.loads(reactions_json) if reactions_json else []
    except Exception:
        rs = []
    parts = []
    for r in rs:
        users = '、'.join(r.get('users') or [])
        parts.append(f'{emoji(r.get("name"))} {r.get("count", 0)}'
                     + (f'（{users}）' if users else ''))
    return '　'.join(parts)


def _message_md(row, files, resolve_user, resolve_channel, absolute=False,
                with_meta=True, level=3) -> str:
    """1メッセージの Markdown（見出し＋本文＋添付＋リアクション）"""
    body = mrkdwn_to_md(row.get('text') or '', resolve_user, resolve_channel)
    fm = _files_md(files, absolute)
    if fm:
        body = (body + '\n\n' + fm) if body else fm
    rx = _reactions_text(row.get('reactions_json'))
    if rx:
        body += '\n\n' + rx
    if not with_meta:
        return body
    edited = '（編集済み）' if row.get('edited_at') else ''
    head = f'{"#" * level} {fmt_datetime(row.get("posted_at"))}　{row.get("sender_name") or "（不明）"}{edited}'
    return head + '\n\n' + body


def _md_to_html(md: str) -> str:
    """Markdown → HTML（サイト共通 markdown_converter を優先）"""
    if _site_md is not None:
        try:
            out = _site_md(md)
            if isinstance(out, tuple):
                out = out[0]
            return out
        except Exception as e:
            logging.warning("slack_minutes: process_markdown failed: %s", e)
    if _md_lib is not None:
        try:
            return _md_lib.markdown(md, extensions=['fenced_code', 'tables',
                                                    'nl2br'])
        except Exception as e:
            logging.warning("slack_minutes: markdown failed: %s", e)
    import html as _h
    return '<pre style="white-space:pre-wrap">' + _h.escape(md) + '</pre>'


def _load_channel_tree(cur, channel_id):
    """チャンネルの全メッセージを親→返信の木に組む．(parents, replies_by_ts)"""
    cur.execute("""
        SELECT id, slack_ts, sender_id, sender_name, text, posted_at,
               thread_ts, subtype, reply_count, edited_at, reactions_json
        FROM slack_minutes_messages
        WHERE channel_id=%s
        ORDER BY posted_at ASC, slack_ts ASC
    """, (channel_id,))
    rows = cur.fetchall()
    have = {r['slack_ts'] for r in rows}
    parents, replies = [], {}
    for r in rows:
        tts = r.get('thread_ts')
        if tts and tts != r['slack_ts'] and tts in have:
            replies.setdefault(tts, []).append(r)
        else:
            parents.append(r)
    for lst in replies.values():
        lst.sort(key=lambda x: float(x['slack_ts']))
    return parents, replies


def _channel_row(cur, channel_id):
    cur.execute("SELECT * FROM slack_minutes_channels WHERE channel_id=%s",
                (channel_id,))
    ch = cur.fetchone()
    if ch:
        return ch
    cur.execute("""
        SELECT channel_name FROM slack_minutes_messages
        WHERE channel_id=%s ORDER BY id DESC LIMIT 1
    """, (channel_id,))
    r = cur.fetchone()
    return {'channel_id': channel_id, 'name': r['channel_name'] if r else channel_id,
            'is_private': 0, 'topic': '', 'purpose': '',
            'slack_created_at': None, 'last_archived_at': None}


def _build_channel_md(conn, channel_id, absolute=True) -> tuple:
    """チャンネル全体の Markdown．戻り値 (channel_name, md)"""
    cur = conn.cursor(dictionary=True)
    ch = _channel_row(cur, channel_id)
    parents, replies = _load_channel_tree(cur, channel_id)
    files = _files_for_channel(cur, channel_id)
    _load_users_from_db(conn)
    ru = lambda uid: _user_cache.get(uid, uid)
    rc = _channel_name_resolver(conn)
    cur.close()

    lines = [f'# #{ch["name"]}', '']
    if ch.get('purpose'):
        lines += [f'目的：{ch["purpose"]}', '']
    if ch.get('topic'):
        lines += [f'トピック：{ch["topic"]}', '']
    if parents:
        lines += [f'期間：{fmt_datetime(parents[0]["posted_at"])} 〜 '
                  f'{fmt_datetime(parents[-1]["posted_at"])}　'
                  f'メッセージ {len(parents)} 件・返信 '
                  f'{sum(len(v) for v in replies.values())} 件', '']
    lines += [f'（FUJIN-P すらくみ による Slack アーカイブ．出力日時 '
              f'{fmt_datetime(get_jst_now())}）', '', '---', '']
    for p in parents:
        lines.append(_message_md(p, files.get(p['slack_ts']), ru, rc,
                                 absolute=absolute, level=3))
        lines.append('')
        for r in replies.get(p['slack_ts'], []):
            md = _message_md(r, files.get(r['slack_ts']), ru, rc,
                             absolute=absolute, level=4)
            lines.append('\n'.join('> ' + l if l else '>' for l in md.split('\n')))
            lines.append('')
        lines += ['---', '']
    return ch['name'], '\n'.join(lines)


# ══════════════════════════════════════════════════════════════
# ルート定義
# ══════════════════════════════════════════════════════════════

@slack_minutes_bp.route('/return_to_fujin')
def return_to_fujin():
    """FUJIN-Pダッシュボードに戻る（ユーザカテゴリに応じた戻り先へ）"""
    return redirect_to_dashboard()


@slack_minutes_bp.route('/')
@login_required
def index():
    """admin：メイン画面／それ以外：公開アーカイブの一覧のみ（★v2.1）"""
    if _is_admin():
        return render_template('slack_minutes/index.html')
    conn = _db()
    try:
        channels = _list_archives(conn, include_hidden=False)
    finally:
        conn.close()
    return render_template('slack_minutes/browse.html', channels=channels)


@slack_minutes_bp.route('/api/workspace', methods=['GET'])
@admin_required
def api_workspace():
    """連携先ワークスペース名と Bot 名を返す"""
    if not _SLACK_SDK_OK:
        return jsonify({'success': False,
                        'error': 'slack_sdk が未インストールです'}), 500
    try:
        info = _workspace_info()
        return jsonify({'success': True, **info})
    except Exception as e:
        logging.error("api_workspace error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@slack_minutes_bp.route('/api/channels', methods=['GET'])
@admin_required
def api_channels():
    """Slackチャンネル一覧を返す"""
    if not _SLACK_SDK_OK:
        return jsonify({'success': False,
                        'error': 'slack_sdk が未インストールです'}), 500
    try:
        channels = _fetch_channels()
        return jsonify({'success': True, 'channels': channels})
    except Exception as e:
        logging.error("api_channels error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@slack_minutes_bp.route('/api/probe/<channel_id>', methods=['GET'])
@admin_required
def api_probe(channel_id):
    """Bot が指定チャンネルに参加済みかどうかをその場で確認する"""
    if not _SLACK_SDK_OK:
        return jsonify({'success': False,
                        'error': 'slack_sdk が未インストールです'}), 500
    try:
        client = _slack_client()
        resp = client.conversations_info(channel=channel_id)
        ch = resp.get('channel', {}) or {}
        return jsonify({'success': True,
                        'is_member': bool(ch.get('is_member', False)),
                        'is_private': bool(ch.get('is_private', False)),
                        'name': ch.get('name', '')})
    except SlackApiError as e:
        err = e.response.get('error', str(e))
        if err == 'channel_not_found':
            return jsonify({'success': True, 'is_member': False,
                            'not_found': True})
        logging.error("api_probe SlackApiError: %s", err)
        return jsonify({'success': False,
                        'error': f'Slack APIエラー: {err}'}), 500
    except Exception as e:
        logging.error("api_probe error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


def _slack_error_json(e):
    err = e.response.get('error', str(e))
    logging.error("slack_minutes SlackApiError: %s", err)
    if err == 'not_in_channel':
        return jsonify({'success': False,
                        'error': 'Bot がこのチャンネルに参加していません'
                                 '（/invite で招待してください）'}), 500
    if err == 'missing_scope':
        need = e.response.get('needed', '')
        return jsonify({'success': False,
                        'error': f'Slack App のスコープが不足しています'
                                 f'（needed: {need}）．OAuth & Permissions で'
                                 f'追加して Reinstall してください'}), 500
    return jsonify({'success': False, 'error': f'Slack APIエラー: {err}'}), 500


@slack_minutes_bp.route('/api/fetch', methods=['POST'])
@admin_required
def api_fetch():
    """差分取得（v1.x 互換）．Body: { channel_id, channel_name, oldest_ts }"""
    if not _SLACK_SDK_OK:
        return jsonify({'success': False,
                        'error': 'slack_sdk が未インストールです'}), 500
    try:
        data         = request.json or {}
        channel_id   = (data.get('channel_id') or '').strip()
        channel_name = (data.get('channel_name') or '').strip()
        oldest_ts    = data.get('oldest_ts') or None
        user_id      = session.get('user_id')
        if not channel_id:
            return jsonify({'success': False,
                            'error': 'チャンネルIDが未指定です'}), 400
        result = _fetch_and_save(channel_id, channel_name, oldest_ts, user_id)
        return jsonify({'success': True, **result})
    except SlackApiError as e:
        return _slack_error_json(e)
    except Exception as e:
        logging.error("api_fetch error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


# ── ★v2.0 完全アーカイブ ─────────────────────────────────────

@slack_minutes_bp.route('/api/archive/start', methods=['POST'])
@admin_required
def api_archive_start():
    """完全アーカイブ取得の開始．Body: { channel_id, channel_name }"""
    if not _SLACK_SDK_OK:
        return jsonify({'success': False,
                        'error': 'slack_sdk が未インストールです'}), 500
    try:
        data         = request.json or {}
        channel_id   = (data.get('channel_id') or '').strip()
        channel_name = (data.get('channel_name') or '').strip()
        if not channel_id:
            return jsonify({'success': False,
                            'error': 'チャンネルIDが未指定です'}), 400
        res = _archive_start(channel_id, channel_name, session.get('user_id'))
        return jsonify({'success': True, 'session_id': res['session_id'],
                        **_progress(res['state'])})
    except SlackApiError as e:
        return _slack_error_json(e)
    except Exception as e:
        logging.error("api_archive_start error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@slack_minutes_bp.route('/api/archive/step/<int:sid>', methods=['POST'])
@admin_required
def api_archive_step(sid):
    """完全アーカイブ取得を 1 区切り進める（done になるまで繰り返し呼ぶ）"""
    if not _SLACK_SDK_OK:
        return jsonify({'success': False,
                        'error': 'slack_sdk が未インストールです'}), 500
    try:
        res = _archive_step(sid)
        return jsonify({'success': True, **res})
    except SlackApiError as e:
        return _slack_error_json(e)
    except Exception as e:
        logging.error("api_archive_step error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


def _list_archives(conn, include_hidden: bool) -> list:
    """アーカイブ済み（＝メッセージが保存されている）チャンネルの一覧"""
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT m.channel_id,
               COALESCE(MAX(c.name), MAX(m.channel_name)) AS name,
               MAX(c.is_private) AS is_private,
               MAX(c.visibility) AS visibility,
               MAX(c.purpose) AS purpose,
               COUNT(*) AS total,
               SUM(CASE WHEN m.thread_ts IS NULL OR m.thread_ts = m.slack_ts
                        THEN 1 ELSE 0 END) AS parents,
               SUM(CASE WHEN m.thread_ts IS NOT NULL AND m.thread_ts <> m.slack_ts
                        THEN 1 ELSE 0 END) AS replies,
               MIN(m.posted_at) AS first_at,
               MAX(m.posted_at) AS last_at,
               MAX(c.last_archived_at) AS last_archived_at,
               (SELECT COUNT(*) FROM slack_minutes_files f
                 WHERE f.channel_id = m.channel_id AND f.status='done') AS files_done,
               (SELECT COUNT(*) FROM slack_minutes_files f
                 WHERE f.channel_id = m.channel_id) AS files_total
        FROM slack_minutes_messages m
        LEFT JOIN slack_minutes_channels c ON c.channel_id = m.channel_id
        GROUP BY m.channel_id
        ORDER BY last_at DESC
    """)
    rows = cur.fetchall()
    # 許可グループ（チャンネル別）
    cur.execute("SELECT channel_id, group_id FROM slack_minutes_access_groups")
    allowed = {}
    for a in cur.fetchall():
        allowed.setdefault(a['channel_id'], set()).add(a['group_id'])
    ugids = set() if include_hidden else \
        _user_active_group_ids(cur, session.get('user_id'))
    cur.close()
    cat = session.get('user_category')
    out = []
    for r in rows:
        r['visibility'] = r.get('visibility') or 'private'
        if r['visibility'] not in SHARE_KEYS:
            r['visibility'] = 'private'
        if not include_hidden and not _can_view(
                r['visibility'], cat, ugids, allowed.get(r['channel_id'], set())):
            continue
        r['visibility_label'] = SHARE_LABELS[r['visibility']]
        r['group_ids'] = sorted(allowed.get(r['channel_id'], set()))
        for k in ('first_at', 'last_at', 'last_archived_at'):
            r[k] = fmt_datetime(r.get(k))
        for k in ('total', 'parents', 'replies', 'files_done',
                  'files_total', 'is_private'):
            r[k] = int(r.get(k) or 0)
        r['purpose'] = r.get('purpose') or ''
        r['url'] = url_for('slack_minutes.channel_view',
                           channel_id=r['channel_id'])
        r['export_url'] = url_for('slack_minutes.export_md',
                                  channel_id=r['channel_id'])
        out.append(r)
    return out


@slack_minutes_bp.route('/api/archives', methods=['GET'])
@login_required
def api_archives():
    """アーカイブ済みチャンネルの一覧（admin は非公開分も含む）"""
    try:
        conn = _db()
        rows = _list_archives(conn, include_hidden=_is_admin())
        return jsonify({'success': True, 'channels': rows,
                        'is_admin': _is_admin()})
    except Exception as e:
        logging.error("api_archives error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            conn.close()


@slack_minutes_bp.route('/api/archives/<channel_id>/access', methods=['GET'])
@admin_required
def api_get_access(channel_id):
    """公開範囲と許可グループの取得（★v2.2）"""
    try:
        conn = _db()
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT visibility FROM slack_minutes_channels WHERE channel_id=%s",
                    (channel_id,))
        r = cur.fetchone()
        vis = (r or {}).get('visibility') or 'private'
        gids = sorted(_channel_access_group_ids(cur, channel_id))
        groups = _all_user_groups(cur)
        cur.close()
        return jsonify({'success': True, 'visibility': vis, 'group_ids': gids,
                        'all_groups': groups,
                        'labels': SHARE_LABELS,
                        'descriptions': SHARE_DESCRIPTIONS})
    except Exception as e:
        logging.error("api_get_access error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            conn.close()


@slack_minutes_bp.route('/api/archives/<channel_id>/access', methods=['POST'])
@admin_required
def api_set_access(channel_id):
    """
    公開範囲と許可グループの設定（★v2.2．admin のみ）
    Body: { visibility: SHARE_KEYS のいずれか, group_ids: [int, …] }
    許可グループは全削除→再挿入（マイノートと同じ）
    """
    data = request.json or {}
    vis = data.get('visibility')
    if vis not in SHARE_KEYS:
        return jsonify({'success': False, 'error': '公開範囲の値が不正です'}), 400
    try:
        group_ids = sorted({int(g) for g in (data.get('group_ids') or [])})
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'グループIDが不正です'}), 400
    if vis not in ('group', 'domestic_group'):
        group_ids = []
    elif not group_ids:
        return jsonify({'success': False,
                        'error': 'グループを1つ以上選んでください'}), 400
    try:
        conn = _db()
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT channel_id FROM slack_minutes_channels WHERE channel_id=%s",
                    (channel_id,))
        if cur.fetchone():
            cur.execute("""
                UPDATE slack_minutes_channels SET visibility=%s, updated_at=%s
                WHERE channel_id=%s
            """, (vis, get_jst_now(), channel_id))
        else:
            # 差分取得しかしていないチャンネル：messages から名前を拾って行を作る
            cur.execute("""
                SELECT channel_name FROM slack_minutes_messages
                WHERE channel_id=%s ORDER BY id DESC LIMIT 1
            """, (channel_id,))
            r = cur.fetchone()
            if not r:
                return jsonify({'success': False,
                                'error': 'チャンネルがありません'}), 404
            cur.execute("""
                INSERT INTO slack_minutes_channels
                    (channel_id, name, is_private, updated_at, visibility)
                VALUES (%s,%s,0,%s,%s)
            """, (channel_id, r['channel_name'], get_jst_now(), vis))
        cur.execute("DELETE FROM slack_minutes_access_groups WHERE channel_id=%s",
                    (channel_id,))
        for gid in group_ids:
            cur.execute("""
                INSERT INTO slack_minutes_access_groups (channel_id, group_id)
                VALUES (%s, %s)
            """, (channel_id, gid))
        conn.commit()
        return jsonify({'success': True, 'visibility': vis,
                        'group_ids': group_ids, 'label': SHARE_LABELS[vis]})
    except Exception as e:
        logging.error("api_set_access error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()


@slack_minutes_bp.route('/channel/<channel_id>')
@login_required
def channel_view(channel_id):
    """チャンネル閲覧画面（目録）"""
    conn = _db()
    try:
        cur = conn.cursor(dictionary=True)
        _check_channel_access(cur, channel_id, conn)
        ch = _channel_row(cur, channel_id)
        cur.close()
    finally:
        conn.close()
    ch['last_archived_at'] = fmt_datetime(ch.get('last_archived_at'))
    ch['slack_created_at'] = fmt_datetime(ch.get('slack_created_at'))
    return render_template('slack_minutes/channel.html', ch=ch,
                           is_admin=_is_admin())


@slack_minutes_bp.route('/api/catalog/<channel_id>', methods=['GET'])
@login_required
def api_catalog(channel_id):
    """目録：親メッセージの一覧（返信数・添付数・リアクション・要約付き）"""
    try:
        conn = _db()
        cur = conn.cursor(dictionary=True)
        _check_channel_access(cur, channel_id, conn)
        parents, replies = _load_channel_tree(cur, channel_id)
        files = _files_for_channel(cur, channel_id)
        _load_users_from_db(conn)
        ru = lambda uid: _user_cache.get(uid, uid)
        rc = _channel_name_resolver(conn)
        items = []
        for p in parents:
            rs = replies.get(p['slack_ts'], [])
            plain = mrkdwn_to_plain(p.get('text') or '', ru, rc)
            fl = files.get(p['slack_ts'], [])
            nfiles = len(fl) + sum(len(files.get(r['slack_ts'], [])) for r in rs)
            items.append({
                'ts':        p['slack_ts'],
                'posted_at': fmt_datetime(p.get('posted_at')),
                'date':      fmt_datetime(p.get('posted_at'))[:10],
                'sender':    p.get('sender_name') or '（不明）',
                'summary':   plain[:140].replace('\n', ' '),
                'plain':     plain + ' ' + ' '.join(
                    mrkdwn_to_plain(r.get('text') or '', ru, rc) for r in rs),
                'replies':   len(rs),
                'files':     nfiles,
                'reactions': _reactions_text(p.get('reactions_json')),
                'edited':    bool(p.get('edited_at')),
                'repliers':  sorted({r.get('sender_name') or '' for r in rs}),
            })
        senders = sorted({p.get('sender_name') or '（不明）' for p in parents})
        return jsonify({'success': True, 'items': items, 'senders': senders,
                        'total': len(parents),
                        'reply_total': sum(len(v) for v in replies.values())})
    except HTTPException:
        raise
    except Exception as e:
        logging.error("api_catalog error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()


@slack_minutes_bp.route('/api/thread/<channel_id>/<ts>', methods=['GET'])
@login_required
def api_thread(channel_id, ts):
    """スレッド（親＋返信）を Markdown と HTML で返す"""
    try:
        conn = _db()
        cur = conn.cursor(dictionary=True)
        _check_channel_access(cur, channel_id, conn)
        cur.execute("""
            SELECT id, slack_ts, sender_id, sender_name, text, posted_at,
                   thread_ts, subtype, reply_count, edited_at, reactions_json
            FROM slack_minutes_messages
            WHERE channel_id=%s AND (slack_ts=%s OR thread_ts=%s)
            ORDER BY slack_ts ASC
        """, (channel_id, ts, ts))
        rows = cur.fetchall()
        if not rows:
            return jsonify({'success': False, 'error': 'メッセージがありません'}), 404
        cur.execute("""
            SELECT id, file_id, slack_ts, name, title, mimetype, size,
                   local_path, status
            FROM slack_minutes_files
            WHERE channel_id=%s AND (slack_ts=%s OR slack_ts IN (
                SELECT slack_ts FROM slack_minutes_messages
                WHERE channel_id=%s AND thread_ts=%s))
            ORDER BY id
        """, (channel_id, ts, channel_id, ts))
        files = {}
        for f in cur.fetchall():
            files.setdefault(f['slack_ts'], []).append(f)
        _load_users_from_db(conn)
        ru = lambda uid: _user_cache.get(uid, uid)
        rc = _channel_name_resolver(conn)
        out = []
        for r in rows:
            md = _message_md(r, files.get(r['slack_ts']), ru, rc,
                             with_meta=False)
            out.append({
                'ts': r['slack_ts'],
                'is_reply': r['slack_ts'] != ts,
                'sender': r.get('sender_name') or '（不明）',
                'posted_at': fmt_datetime(r.get('posted_at')),
                'edited': bool(r.get('edited_at')),
                'md': md,
                'html': _md_to_html(md),
            })
        return jsonify({'success': True, 'messages': out})
    except HTTPException:
        raise
    except Exception as e:
        logging.error("api_thread error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()


@slack_minutes_bp.route('/export/<channel_id>.md', methods=['GET'])
@login_required
def export_md(channel_id):
    """チャンネル全体を Markdown ファイルとして出力"""
    conn = _db()
    try:
        cur = conn.cursor(dictionary=True)
        _check_channel_access(cur, channel_id, conn)
        cur.close()
        name, md = _build_channel_md(conn, channel_id, absolute=True)
    finally:
        conn.close()
    from urllib.parse import quote
    fname = f'slack_{name}_{get_jst_now().strftime("%Y%m%d_%H%M")}.md'
    ascii_name = f'slack_{channel_id}.md'
    resp = Response(md, mimetype='text/markdown; charset=utf-8')
    resp.headers['Content-Disposition'] = (
        f"attachment; filename=\"{ascii_name}\"; "
        f"filename*=UTF-8''{quote(fname)}")
    return resp


@slack_minutes_bp.route('/file/<int:fid>', methods=['GET'])
@login_required
def serve_file(fid):
    """保存済み添付ファイルの配信（チャンネルの公開範囲に従う）"""
    conn = _db()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM slack_minutes_files WHERE id=%s", (fid,))
        f = cur.fetchone()
        if f:
            _check_channel_access(cur, f['channel_id'], conn)
        cur.close()
    finally:
        conn.close()
    if not f or f.get('status') != 'done' or not f.get('local_path'):
        abort(404)
    path = os.path.normpath(os.path.join(FILES_DIR, f['local_path']))
    if not path.startswith(os.path.normpath(FILES_DIR)) or not os.path.isfile(path):
        abort(404)
    mimetype = f.get('mimetype') or 'application/octet-stream'
    # HTML/SVG をそのまま描画させない（同一オリジンでのスクリプト実行を避ける）
    inline = (mimetype.startswith('image/') and 'svg' not in mimetype) \
        or mimetype == 'application/pdf'
    return send_file(path, mimetype=mimetype, as_attachment=not inline,
                     download_name=f.get('name') or f['file_id'])


# ── セッション（取得記録）──────────────────────────────────

@slack_minutes_bp.route('/api/sessions', methods=['GET'])
@admin_required
def api_sessions():
    """取得セッション一覧（新しい順）"""
    try:
        conn = _db()
        cur  = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT id, channel_id, channel_name, fetched_at, status,
                   fetched_count, saved_count, mode, phase,
                   updated_count, reply_count, file_count
            FROM slack_minutes_sessions
            ORDER BY fetched_at DESC
            LIMIT 100
        """)
        rows = cur.fetchall()
        for r in rows:
            r['fetched_at'] = fmt_datetime(r.get('fetched_at'))
            r['mode'] = r.get('mode') or 'diff'
        return jsonify({'success': True, 'sessions': rows})
    except Exception as e:
        logging.error("api_sessions error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()


@slack_minutes_bp.route('/api/messages/<int:sid>', methods=['GET'])
@admin_required
def api_messages(sid):
    """セッションで初めて保存されたメッセージの一覧（投稿日時の昇順）"""
    try:
        conn = _db()
        cur  = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT id, sender_name, text, posted_at, thread_ts, slack_ts
            FROM slack_minutes_messages
            WHERE session_id = %s
            ORDER BY posted_at ASC
        """, (sid,))
        rows = cur.fetchall()
        _load_users_from_db(conn)
        ru = lambda uid: _user_cache.get(uid, uid)
        rc = _channel_name_resolver(conn)
        for r in rows:
            r['posted_at'] = fmt_datetime(r.get('posted_at'))
            r['text'] = mrkdwn_to_plain(r.get('text') or '', ru, rc)
        return jsonify({'success': True, 'messages': rows})
    except Exception as e:
        logging.error("api_messages error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()


@slack_minutes_bp.route('/api/delete/<int:sid>', methods=['POST'])
@admin_required
def api_delete(sid):
    """
    セッション（取得記録）を削除する．★v2.0 挙動変更
    メッセージは削除しない（session_id は残るが参照先が無いだけで支障なし）．
    """
    try:
        conn = _db()
        cur  = conn.cursor()
        cur.execute("DELETE FROM slack_minutes_sessions WHERE id = %s", (sid,))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("api_delete error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
