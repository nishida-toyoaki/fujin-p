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
# Source: https://github.com/u-fukuchiyama/fujin-p

# guest.py
from flask import Blueprint, render_template, session, redirect, url_for, flash, request, current_app, abort
from decorators import login_required
from config import Config
from db import get_db_cursor
from datetime import datetime, timezone, timedelta

guest_bp = Blueprint('guest', __name__, template_folder='templates')


# ── ゲスト公開アプリ（ログイン不要） ─────────────────────────────────────
# ★2026-08-27 固定の一覧（GUEST_APPS）を廃止．正本のランチャで使用区分が
#   「公開（ログイン不要）」のカードを fujinp.registry.public_cards() から得る．
#   ログイン画面（login.html）も同じ一覧を描く．/go/<app_name> はアクセス記録用．


# ── グループ取得ヘルパー ──────────────────────────────────────────────────

# 構成員の判定はまいぐる（user_groups）の公開APIに任せる。台帳のルールから
# 作られたグループ（総務課など）は user_group_memberships に行を持たないため、
# このテーブルを直接引くと構成員が0人になる。取り込みは初回の呼び出し時に行う
# （起動時の読み込み順に左右されないようにするため）。
_UG_UTILS = None


def _ug(name):
    """まいぐるの utils から関数を取り出す。無ければ None（呼び出し元が従来処理に落ちる）"""
    global _UG_UTILS
    if _UG_UTILS is None:
        try:
            from fujinp.user_groups import utils as _u
        except Exception:
            _u = False
        _UG_UTILS = _u
    return getattr(_UG_UTILS, name, None) if _UG_UTILS else None


def get_user_group_names(user_id):
    """
    ユーザが現在有効なメンバーとして所属しているグループ名のリストを返す。
    まいぐるに委ねる（直接メンバー ∪ 台帳のルール由来）。
    まいぐるが使えないときは、従来どおり user_group_memberships を直接引く。
    テンプレート側で  {% if 'グループ名' in user_group_names %}  の形で使う。
    """
    if not user_id:
        return []
    _fn = _ug('get_user_group_names')
    if _fn is not None:
        try:
            return list(_fn(user_id))
        except Exception as e:
            current_app.logger.error(f'user_groups.get_user_group_names error: {e}')
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

    # アプリのランチャは正本（fujinp/app_registry.json）から組み立てる．
    # 区画・カードの表示条件（require_groups / require_categories）はここで評価される．
    from fujinp.registry import launcher_sections
    sections = launcher_sections('guest', user_category, user_group_names)
    try:  # 旧いカーネル（registry.py）には無い．その場合は CSS の既定の背景
        from fujinp.registry import dashboard_background
        dashboard_bg = dashboard_background('guest')
    except ImportError:
        dashboard_bg = None

    return render_template('admin/guest_dashboard.html',
                            user_name=user_name,
                            # features=features,
                            # feature_codes=feature_codes,
                            user_group_names=user_group_names,
                            launcher_sections=sections,
                            dashboard_bg=dashboard_bg,
                            site_url=Config.BASE_URL,
                            user_category=user_category)


# ── 既存ルート（変更なし）────────────────────────────────────────────────


@guest_bp.route('/go/<app_id>')
def go(app_id):
    """ログイン不要の公開アプリへ（アクセス記録付き）．app_id はアプリ名"""
    from fujinp.registry import find_public_card
    card = find_public_card(app_id)
    if not card:
        abort(404)
    app_info      = {'label': card['label'], 'href': card['href']}
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
    return redirect(app_info['href'])
