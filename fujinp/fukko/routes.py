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

"""ふっこ (fukko) - ルート定義

Claude API（Anthropic Messages API）への接続デモ。
- マルチターン：同一会話内の直近履歴をClaudeに送る
- ログ：ユーザ入力・Claudeレスポンスをすべて日時（JST）とともにDB記録
- アクセス：admin のみ
"""
import datetime
import logging
import uuid

import requests
import mysql.connector
from pytz import timezone
from flask import render_template, request, jsonify, session

from config import Config
from db import DatabaseConfig
from decorators import login_required

from . import fukko_bp

# ===== タイムゾーン設定 =====
JST = timezone('Asia/Tokyo')


def get_jst_now():
    """現在の日時をJSTで取得（naive datetime）。INSERT/UPDATEに使う。"""
    return datetime.datetime.now(JST).replace(tzinfo=None)


def fmt_datetime(d):
    """datetime → 'YYYY-MM-DD HH:MM' 文字列。None は空文字。"""
    if d is None:
        return ''
    if isinstance(d, (datetime.datetime, datetime.date)):
        return d.strftime('%Y-%m-%d %H:%M')
    return str(d)


def fmt_datetime_sec(d):
    """datetime → 'YYYY-MM-DD HH:MM:SS' 文字列（デモ用に秒まで表示）。"""
    if d is None:
        return ''
    if isinstance(d, (datetime.datetime, datetime.date)):
        return d.strftime('%Y-%m-%d %H:%M:%S')
    return str(d)


# ===== アプリ内定数（プラットフォーム非依存） =====
ANTHROPIC_API_URL = 'https://api.anthropic.com/v1/messages'
ANTHROPIC_VERSION = '2023-06-01'
HISTORY_MAX_MESSAGES = 20   # マルチターンでClaudeに送る直近メッセージ数
API_TIMEOUT_SEC = 60
SYSTEM_PROMPT = (
    'あなたは「ふっこ」という名前のアシスタントです。'
    'FUJIN-P上のClaude APIデモとして動いています。'
    '日本語で、わかりやすく簡潔に答えてください。'
)


# ===== 権限判定 =====
def get_user_category(user_id):
    """users.category を返す。"""
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT category FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()
        return row['category'] if row else None
    except Exception as e:
        logging.error("fukko get_user_category error: %s", e)
        return None
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


def is_admin():
    """現在のセッションユーザが admin かどうか。"""
    user_id = session.get('user_id')
    return user_id is not None and get_user_category(user_id) == 'admin'


# ===== ログ記録・取得 =====
def save_log(user_id, conversation_id, role, content,
             model=None, input_tokens=None, output_tokens=None, logged_at=None):
    """やりとり1件を fukko_logs に記録する。"""
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO fukko_logs
                (user_id, conversation_id, role, content,
                 model, input_tokens, output_tokens, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (user_id, conversation_id, role, content,
              model, input_tokens, output_tokens,
              logged_at or get_jst_now()))
        conn.commit()
        return cursor.lastrowid
    except Exception as e:
        logging.error("fukko save_log error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return None
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


def get_conversation_messages(conversation_id, limit=None):
    """会話のメッセージを古い順に取得。limit指定時は直近limit件のみ。"""
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT role, content, model, input_tokens, output_tokens, created_at
            FROM fukko_logs
            WHERE conversation_id = %s
            ORDER BY id ASC
        """, (conversation_id,))
        rows = cursor.fetchall()
        if limit:
            rows = rows[-limit:]
        return rows
    except Exception as e:
        logging.error("fukko get_conversation_messages error: %s", e)
        return []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


# ===== Claude API呼び出し =====
def call_claude(messages):
    """Anthropic Messages APIを呼ぶ。

    戻り値: (reply_text, usage_dict, error_message)
    成功時は error_message が None。
    """
    headers = {
        'x-api-key': Config.ANTHROPIC_API_KEY,
        'anthropic-version': ANTHROPIC_VERSION,
        'content-type': 'application/json',
    }
    payload = {
        'model': Config.FUKKO_MODEL,
        'max_tokens': Config.FUKKO_MAX_TOKENS,
        'system': SYSTEM_PROMPT,
        'messages': messages,
    }
    try:
        resp = requests.post(ANTHROPIC_API_URL, headers=headers,
                             json=payload, timeout=API_TIMEOUT_SEC)
    except requests.exceptions.Timeout:
        return None, None, 'Claude APIがタイムアウトしました（%d秒）' % API_TIMEOUT_SEC
    except Exception as e:
        logging.error("fukko call_claude request error: %s", e)
        return None, None, 'Claude APIへの接続に失敗しました'

    if resp.status_code != 200:
        try:
            err = resp.json().get('error', {}).get('message', resp.text[:200])
        except Exception:
            err = resp.text[:200]
        logging.error("fukko call_claude API error %s: %s", resp.status_code, err)
        return None, None, 'Claude APIエラー (%d): %s' % (resp.status_code, err)

    body = resp.json()
    reply = ''.join(
        block.get('text', '')
        for block in body.get('content', [])
        if block.get('type') == 'text'
    )
    usage = body.get('usage', {})
    return reply, usage, None


# ===== ルート =====
@fukko_bp.route('/')
@login_required
def index():
    """メイン画面（チャット）"""
    if not is_admin():
        return render_template('fukko/forbidden.html'), 403
    # 会話IDがなければ新規発行
    if not session.get('fukko_conversation_id'):
        session['fukko_conversation_id'] = str(uuid.uuid4())
    return render_template('fukko/index.html', model=Config.FUKKO_MODEL)


@fukko_bp.route('/api/chat', methods=['POST'])
@login_required
def api_chat():
    """ユーザ入力をClaudeに送り、レスポンスを返す（両方をログ記録）"""
    if not is_admin():
        return jsonify({'success': False, 'error': '管理者専用です'}), 403
    try:
        data = request.json or {}
        message = (data.get('message') or '').strip()
        if not message:
            return jsonify({'success': False, 'error': 'メッセージが空です'}), 400

        user_id = session.get('user_id')
        conversation_id = session.get('fukko_conversation_id')
        if not conversation_id:
            conversation_id = str(uuid.uuid4())
            session['fukko_conversation_id'] = conversation_id

        # マルチターン：これまでの履歴を組み立てる（直近 HISTORY_MAX_MESSAGES 件）
        history = get_conversation_messages(conversation_id, limit=HISTORY_MAX_MESSAGES)
        api_messages = [{'role': r['role'], 'content': r['content']} for r in history]
        api_messages.append({'role': 'user', 'content': message})

        # ユーザ入力をログ記録（API失敗時も記録は残す）
        sent_at = get_jst_now()
        save_log(user_id, conversation_id, 'user', message, logged_at=sent_at)

        # Claude API呼び出し
        reply, usage, error = call_claude(api_messages)
        if error:
            return jsonify({'success': False, 'error': error}), 502

        # レスポンスをログ記録
        replied_at = get_jst_now()
        save_log(user_id, conversation_id, 'assistant', reply,
                 model=Config.FUKKO_MODEL,
                 input_tokens=usage.get('input_tokens'),
                 output_tokens=usage.get('output_tokens'),
                 logged_at=replied_at)

        return jsonify({
            'success': True,
            'reply': reply,
            'sent_at': fmt_datetime_sec(sent_at),
            'replied_at': fmt_datetime_sec(replied_at),
            'model': Config.FUKKO_MODEL,
            'input_tokens': usage.get('input_tokens'),
            'output_tokens': usage.get('output_tokens'),
        })
    except Exception as e:
        logging.error("fukko api_chat error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@fukko_bp.route('/api/history', methods=['GET'])
@login_required
def api_history():
    """現在の会話の履歴を返す（画面再読み込み用）"""
    if not is_admin():
        return jsonify({'success': False, 'error': '管理者専用です'}), 403
    conversation_id = session.get('fukko_conversation_id')
    if not conversation_id:
        return jsonify({'success': True, 'messages': []})
    rows = get_conversation_messages(conversation_id)
    messages = []
    for r in rows:
        messages.append({
            'role': r['role'],
            'content': r['content'],
            'model': r.get('model') or '',
            'input_tokens': r.get('input_tokens'),
            'output_tokens': r.get('output_tokens'),
            'created_at': fmt_datetime_sec(r.get('created_at')),
        })
    return jsonify({'success': True, 'messages': messages})


@fukko_bp.route('/api/new', methods=['POST'])
@login_required
def api_new():
    """新しい会話を開始する（履歴はDBに残る）"""
    if not is_admin():
        return jsonify({'success': False, 'error': '管理者専用です'}), 403
    session['fukko_conversation_id'] = str(uuid.uuid4())
    return jsonify({'success': True})
