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
slack_minutes - ルート定義（v1.2）

【エンドポイント一覧】
  GET  /                    メイン画面（チャンネル選択 + 取得履歴）
  GET  /return_to_fujin     FUJIN-Pダッシュボードに戻る（標準仕様）★v1.2新設
  GET  /api/workspace       連携先ワークスペース名・Bot名        ★v1.2新設
  GET  /api/channels        Slackチャンネル一覧取得（is_member付き）
  GET  /api/probe/<cid>     Bot のチャンネル参加確認（probe）    ★v1.2新設
  POST /api/fetch           指定チャンネルのメッセージを取得・保存
  GET  /api/sessions        取得セッション一覧
  GET  /api/messages/<sid>  セッション内メッセージ一覧
  POST /api/delete/<sid>    セッション削除

【v1.2 変更点】
  - /api/workspace 新設：連携先ワークスペース名と Bot 名を返す。
    Config.SLACK_WORKSPACE_NAMES（list または str）・Config.SLACK_BOT_NAME が
    定義されていればそれを優先し、無ければ auth.test で自動取得する
    （追加スコープ不要）。プロセス内キャッシュ付き。
  - /api/channels の各チャンネルに is_member（Bot参加済みか）を追加。
  - /api/probe/<channel_id> 新設：conversations.info で Bot の参加状態を
    その場で再確認する（招待作業の完了確認用）。
  - /return_to_fujin 新設：「FUJIN-Pダッシュボードへの戻り方（技術仕様書）」
    準拠。戻り先の解決は auth.redirect_to_dashboard() に一元的に委ねる。
  - admin 専用化：全エンドポイントを login_required から admin_required に
    差し替え。Bot が参加しているチャンネル（= admin が招待したチャンネル）の
    内容・取得履歴を regular / guest に開示しないため。
    （/return_to_fujin のみ標準仕様に従いデコレータなし）
"""
import datetime
import logging
import time

from pytz import timezone

from flask import render_template, request, jsonify, session
import mysql.connector

try:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
    _SLACK_SDK_OK = True
except ImportError:
    _SLACK_SDK_OK = False
    logging.warning("slack_minutes: slack_sdk が未インストールです")

from config import Config
from db import DatabaseConfig
from decorators import admin_required
from auth import redirect_to_dashboard
from . import slack_minutes_bp

# ── 日時ヘルパー ────────────────────────────────────────────────
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
    dt = ts_to_jst(ts_str)
    return fmt_datetime(dt)


# ── Slack クライアント ──────────────────────────────────────────

def _slack_client():
    token = getattr(Config, 'SLACK_BOT_TOKEN', None)
    if not token:
        raise RuntimeError('SLACK_BOT_TOKEN が config.py に設定されていません')
    return WebClient(token=token)


# ── ワークスペース情報 ──────────────────────────────────────────
#
# 連携先ワークスペース名と Bot 名。
#   優先1: Config.SLACK_WORKSPACE_NAMES（list または str）
#          Config.SLACK_BOT_NAME（str）
#   優先2: auth.test の team / user フィールド（追加スコープ不要）
# auth.test の結果はプロセス内にキャッシュする（ワークスペース名は
# 運用中に変わらないため）。

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


# ── チャンネル一覧取得 ──────────────────────────────────────────

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
                time.sleep(retry)
                continue
            raise
        for ch in resp.get('channels', []):
            channels.append({
                'id':          ch['id'],
                'name':        ch.get('name', ''),
                'is_private':  ch.get('is_private', False),
                'is_member':   ch.get('is_member', False),   # ★v1.2
                'num_members': ch.get('num_members', 0),
            })
        cursor = resp.get('response_metadata', {}).get('next_cursor')
        if not cursor:
            break
        # sleepなし（レートリミット時のみ上記で待つ）
    channels.sort(key=lambda c: c['name'])
    return channels


# ── ユーザー名キャッシュ ────────────────────────────────────────

_user_cache: dict[str, str] = {}


def _resolve_username(client, user_id: str) -> str:
    """Slack ユーザーID → 表示名（キャッシュ付き）"""
    if not user_id:
        return '（不明）'
    if user_id in _user_cache:
        return _user_cache[user_id]
    try:
        resp = client.users_info(user=user_id)
        profile = resp.get('user', {}).get('profile', {})
        name = (profile.get('display_name')
                or profile.get('real_name')
                or user_id)
        _user_cache[user_id] = name
        return name
    except Exception:
        _user_cache[user_id] = user_id
        return user_id


# ── メッセージ取得・保存 ────────────────────────────────────────

def _fetch_and_save(channel_id: str, channel_name: str,
                    oldest_ts: str | None, user_id: int) -> dict:
    """
    指定チャンネルのメッセージを取得し DB へ保存する．
    oldest_ts: これより新しいメッセージのみ取得（None = 全件）
    戻り値: {'session_id', 'fetched', 'saved', 'skipped'}
    """
    client = _slack_client()
    conn = mysql.connector.connect(**DatabaseConfig.default())
    cur = conn.cursor(dictionary=True)

    try:
        # セッション登録
        now = get_jst_now()
        cur.execute("""
            INSERT INTO slack_minutes_sessions
                (user_id, channel_id, channel_name, fetched_at, status)
            VALUES (%s, %s, %s, %s, 'running')
        """, (user_id, channel_id, channel_name, now))
        conn.commit()
        session_id = cur.lastrowid

        # メッセージ取得（ページネーション）
        messages_raw = []
        cursor = None
        kwargs_base = dict(channel=channel_id, limit=200)
        if oldest_ts:
            kwargs_base['oldest'] = oldest_ts
        while True:
            kw = dict(kwargs_base)
            if cursor:
                kw['cursor'] = cursor
            resp = client.conversations_history(**kw)
            messages_raw.extend(resp.get('messages', []))
            cursor = resp.get('response_metadata', {}).get('next_cursor')
            if not resp.get('has_more') or not cursor:
                break
            time.sleep(0.5)

        fetched = len(messages_raw)
        saved = skipped = 0

        for msg in messages_raw:
            # bot_message や channel_join など通常メッセージ以外はスキップ
            if msg.get('subtype') in ('channel_join', 'channel_leave',
                                       'bot_message', 'channel_archive'):
                skipped += 1
                continue

            ts        = msg.get('ts', '')
            text      = msg.get('text', '')
            sender_id = msg.get('user', '')
            sender_name = _resolve_username(client, sender_id)
            posted_at   = ts_to_jst(ts)
            thread_ts   = msg.get('thread_ts')

            # ts の重複チェック
            cur.execute("""
                SELECT id FROM slack_minutes_messages
                WHERE channel_id = %s AND slack_ts = %s
                LIMIT 1
            """, (channel_id, ts))
            if cur.fetchone():
                skipped += 1
                continue

            cur.execute("""
                INSERT INTO slack_minutes_messages
                    (session_id, channel_id, channel_name, slack_ts,
                     sender_id, sender_name, text, posted_at,
                     thread_ts, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (session_id, channel_id, channel_name, ts,
                  sender_id, sender_name, text, posted_at,
                  thread_ts, now))
            saved += 1

        conn.commit()

        # セッション完了更新
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
        if 'session_id' in dir():
            try:
                cur.execute("""
                    UPDATE slack_minutes_sessions
                    SET status = 'error' WHERE id = %s
                """, (session_id,))
                conn.commit()
            except Exception:
                pass
        raise
    finally:
        if conn.is_connected():
            cur.close()
            conn.close()


# ── ルート定義 ─────────────────────────────────────────────────

@slack_minutes_bp.route('/return_to_fujin')
def return_to_fujin():
    """FUJIN-Pダッシュボードに戻る（ユーザカテゴリに応じた戻り先へ）"""
    return redirect_to_dashboard()


@slack_minutes_bp.route('/')
@admin_required
def index():
    """メイン画面"""
    return render_template('slack_minutes/index.html')


@slack_minutes_bp.route('/api/workspace', methods=['GET'])
@admin_required
def api_workspace():
    """連携先ワークスペース名と Bot 名を返す（★v1.2新設）"""
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
    """
    Bot が指定チャンネルに参加済みかどうかをその場で確認する（★v1.2新設）。
    招待作業（/invite）の完了確認に使う。

    戻り値: {'success': True, 'is_member': bool, 'is_private': bool,
             'name': 'チャンネル名'}
    ※ Bot が参加していないプライベートチャンネルは conversations.info が
      channel_not_found を返すため、is_member=False として扱う。
    """
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


@slack_minutes_bp.route('/api/fetch', methods=['POST'])
@admin_required
def api_fetch():
    """
    指定チャンネルのメッセージを取得・保存する
    Body: { channel_id, channel_name, oldest_ts（省略可） }
    """
    if not _SLACK_SDK_OK:
        return jsonify({'success': False,
                        'error': 'slack_sdk が未インストールです'}), 500
    try:
        data        = request.json or {}
        channel_id  = data.get('channel_id', '').strip()
        channel_name = data.get('channel_name', '').strip()
        oldest_ts   = data.get('oldest_ts') or None
        user_id     = session.get('user_id')

        if not channel_id:
            return jsonify({'success': False,
                            'error': 'チャンネルIDが未指定です'}), 400

        result = _fetch_and_save(channel_id, channel_name,
                                 oldest_ts, user_id)
        return jsonify({'success': True, **result})

    except SlackApiError as e:
        err = e.response.get('error', str(e))
        logging.error("api_fetch SlackApiError: %s", err)
        if err == 'not_in_channel':
            return jsonify({'success': False,
                            'error': 'Bot がこのチャンネルに参加していません'
                                     '（/invite で招待してください）'}), 500
        return jsonify({'success': False,
                        'error': f'Slack APIエラー: {err}'}), 500
    except Exception as e:
        logging.error("api_fetch error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@slack_minutes_bp.route('/api/sessions', methods=['GET'])
@admin_required
def api_sessions():
    """取得セッション一覧（新しい順）"""
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT id, channel_name, fetched_at, status,
                   fetched_count, saved_count
            FROM slack_minutes_sessions
            ORDER BY fetched_at DESC
            LIMIT 100
        """)
        rows = cur.fetchall()
        for r in rows:
            r['fetched_at'] = fmt_datetime(r.get('fetched_at'))
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
    """セッション内のメッセージ一覧（投稿日時の昇順）"""
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT id, sender_name, text, posted_at, thread_ts, slack_ts
            FROM slack_minutes_messages
            WHERE session_id = %s
            ORDER BY posted_at ASC
        """, (sid,))
        rows = cur.fetchall()
        for r in rows:
            r['posted_at'] = fmt_datetime(r.get('posted_at'))
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
    """セッションとそのメッセージを削除"""
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute(
            "DELETE FROM slack_minutes_messages WHERE session_id = %s", (sid,))
        cur.execute(
            "DELETE FROM slack_minutes_sessions WHERE id = %s", (sid,))
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