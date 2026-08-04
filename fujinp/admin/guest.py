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

# guest.py
from flask import Blueprint, render_template, session, redirect, url_for, flash, request, current_app, abort
from decorators import login_required
from config import Config
from db import get_db_cursor
from datetime import datetime, timezone, timedelta

guest_bp = Blueprint('guest', __name__, template_folder='templates')


# ── ゲスト公開アプリの定義（ログイン不要） ───────────────────────────────
GUEST_APPS = {
    'ts_solvers': {
        'label': '巡回セールスマン問題ソルバー',
        'url_func': 'ts_solvers.index',
    },
    'free_hand_curve': {
        'label': 'フリーハンド曲線',
        'url_func': 'free_hand_curve.index',
    },
    'tag_chase': {
        'label': '鬼ごっこ3D',
        'url_func': 'tag_chase.index',
    },
    'sorakara': {
        'label': 'そらから',
        'url_func': 'sorakara.index',
    }
}


# ── グループ取得ヘルパー ──────────────────────────────────────────────────

def get_user_group_names(user_id):
    """
    ユーザが現在有効なメンバーとして所属しているグループ名のリストを返す。
    valid_from / valid_until の期間チェック（JSTベース）を行う。
    テンプレート側で  {% if 'グループ名' in user_group_names %}  の形で使う。
    """
    if not user_id:
        return []
    JST = timezone(timedelta(hours=9), 'JST')
    now_jst = datetime.now(JST).replace(tzinfo=None)
    try:
        with get_db_cursor() as (cursor, conn):
            cursor.execute("""
                SELECT g.name
                FROM user_group_memberships m
                JOIN user_groups g ON m.group_id = g.id
                WHERE m.user_id = %s
                  AND (m.valid_from  IS NULL OR m.valid_from  <= %s)
                  AND (m.valid_until IS NULL OR m.valid_until >= %s)
                ORDER BY g.name
            """, (user_id, now_jst, now_jst))
            return [r['name'] for r in cursor.fetchall()]
    except Exception as e:
        current_app.logger.error(f'get_user_group_names error: {e}')
        return []


# ── ダッシュボード ────────────────────────────────────────────────────────

@guest_bp.route('/dashboard')
@login_required
def dashboard():
    user_id       = session.get('user_id')
    user_name     = session.get('user_name')
    user_category = session.get('user_category')

    # features（後方互換・段階的廃止予定）
    # with get_db_cursor() as (cursor, conn):
    #    cursor.execute("""
    #        SELECT f.feature_code, f.feature_name, f.description
    #        FROM user_features uf
    #        JOIN features f ON uf.feature_id = f.id
    #        WHERE uf.user_id = %s AND f.is_active = TRUE
    #        ORDER BY f.priority ASC, f.id ASC
    #    """, (user_id,))
    #    features = cursor.fetchall()
    # feature_codes = [f['feature_code'] for f in features]

    # テンプレート側で in 演算子で判定する。
    user_group_names = get_user_group_names(user_id)

    return render_template('admin/guest_dashboard.html',
                            user_name=user_name,
                            # features=features,
                            # feature_codes=feature_codes,
                            user_group_names=user_group_names,
                            site_url=Config.BASE_URL,
                            user_category=user_category)


# ── 既存ルート（変更なし）────────────────────────────────────────────────

@guest_bp.route('/launch/toi_no_mori')
@login_required
def launch_toi_no_mori():
    user_id = session.get('user_id')
    with get_db_cursor() as (cursor, conn):
        cursor.execute("""
            SELECT 1 FROM user_features uf
            JOIN features f ON uf.feature_id = f.id
            WHERE uf.user_id = %s AND f.feature_code = 'test_user' AND f.is_active = TRUE
        """, (user_id,))
        if not cursor.fetchone():
            flash('このアプリにアクセスする権限がありません', 'error')
            return redirect(url_for('guest.dashboard'))
    return redirect('/welcome')


@guest_bp.route('/go/<app_id>')
def go(app_id):
    if app_id not in GUEST_APPS:
        abort(404)
    app_info      = GUEST_APPS[app_id]
    ip            = request.remote_addr
    forwarded_for = request.headers.get('X-Forwarded-For', '-')
    ua            = request.headers.get('User-Agent', '-')
    referer       = request.headers.get('Referer', '-')
    lang          = request.headers.get('Accept-Language', '-')[:40]
    screen        = request.args.get('screen', '-')
    current_app.logger.info(
        'GUEST_ACCESS app_id=%s app_label="%s" ip=%s forwarded_for=%s ua="%s" referer="%s" lang=%s screen=%s',
        app_id, app_info['label'], ip, forwarded_for, ua, referer, lang, screen
    )
    return redirect(url_for(app_info['url_func']))