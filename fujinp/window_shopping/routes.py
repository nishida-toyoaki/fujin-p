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

"""Window Shopping - ルート定義
# @login_requiredはコメントアウト中

純ビューアアプリのためデータベースは使用しない。
描画・アニメーションはすべてフロントエンド（Three.js / WebGL）で行う。
"""
import logging

from flask import render_template

# FUJIN-P共通モジュール（常に存在する前提）
from auth import redirect_to_dashboard
from decorators import login_required

# Blueprint読み込み
from . import window_shopping_bp


@window_shopping_bp.route('/')
# @login_required
def index():
    """ウィンドウショッピング3D画面"""
    return render_template('window_shopping/index.html')


# ────────────────────────────────────────────
# ダッシュボードへ戻る
# ────────────────────────────────────────────

@window_shopping_bp.route('/return_to_fujin')
# @login_required
def return_to_fujin():
    """FUJIN-Pダッシュボードに戻る。"""
    return redirect_to_dashboard()
