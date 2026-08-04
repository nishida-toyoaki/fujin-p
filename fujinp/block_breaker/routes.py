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

"""Block Breaker - ルート定義"""
# @login_required   は外しています．


import datetime
import logging
from pytz import timezone

from flask import render_template, request, jsonify, session
import mysql.connector

from auth import redirect_to_dashboard
from config import Config
from db import DatabaseConfig, Tables
from decorators import login_required

from . import block_breaker_bp

# タイムゾーン設定
JST = timezone('Asia/Tokyo')

# ランキング表示件数
RANKING_LIMIT = 10


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


@block_breaker_bp.route('/')
# @login_required
def index():
    """ゲーム画面"""
    return render_template('block_breaker/index.html')


@block_breaker_bp.route('/return_to_fujin')
# @login_required
def return_to_fujin():
    """FUJIN-Pダッシュボードに戻る。"""
    return redirect_to_dashboard()


@block_breaker_bp.route('/api/save_score', methods=['POST'])
# @login_required
def api_save_score():
    """スコア保存API（ゲームオーバー／クリア時に呼ばれる）"""
    try:
        data = request.json or {}
        user_id = session.get('user_id')

        score = data.get('score')
        cleared = 1 if data.get('cleared') else 0

        # 入力検証
        if not isinstance(score, int) or score < 0 or score > 1000000:
            return jsonify({'success': False, 'error': '不正なスコアです'}), 400

        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO block_breaker_scores (user_id, score, cleared, played_at)
            VALUES (%s, %s, %s, %s)
        """, (user_id, score, cleared, get_jst_now()))
        conn.commit()

        return jsonify({'success': True, 'id': cursor.lastrowid})

    except Exception as e:
        logging.error("block_breaker api_save_score error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@block_breaker_bp.route('/api/ranking', methods=['GET'])
# @login_required
def api_ranking():
    """ハイスコアランキング取得API（上位N件＋自己ベスト）"""
    try:
        user_id = session.get('user_id')
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        # 上位ランキング（ユーザーごとのベストスコアと、その初達成日時）
        cursor.execute("""
            SELECT u.full_name, b.best_score, MIN(s.played_at) AS best_at
            FROM (
                SELECT user_id, MAX(score) AS best_score
                FROM block_breaker_scores
                GROUP BY user_id
            ) b
            JOIN block_breaker_scores s
                 ON s.user_id = b.user_id AND s.score = b.best_score
            JOIN users u ON u.id = b.user_id
            GROUP BY b.user_id, b.best_score, u.full_name
            ORDER BY b.best_score DESC, best_at ASC
            LIMIT %s
        """, (RANKING_LIMIT,))
        ranking = cursor.fetchall()

        # 日時はバックエンドで文字列化してから返す
        for row in ranking:
            row['best_at'] = fmt_datetime(row.get('best_at'))

        # 自己ベスト
        cursor.execute("""
            SELECT MAX(score) AS my_best, COUNT(*) AS my_plays
            FROM block_breaker_scores
            WHERE user_id = %s
        """, (user_id,))
        mine = cursor.fetchone() or {}

        return jsonify({
            'success': True,
            'ranking': ranking,
            'my_best': mine.get('my_best') or 0,
            'my_plays': mine.get('my_plays') or 0
        })

    except Exception as e:
        logging.error("block_breaker api_ranking error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()
