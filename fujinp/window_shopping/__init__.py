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
Window Shopping - ウィンドウショッピング3D
棒人形が歩き・走り・ジャンプしながら広場を行き来する3Dアニメーション。
マウスクリックで店を配置すると、棒人形たちが店のショーウィンドウを見て回る。
純ビューアアプリ（データベース不使用）。
"""
from flask import Blueprint

window_shopping_bp = Blueprint(
    'window_shopping',
    __name__,
    template_folder='templates',
    url_prefix='/window_shopping'
)

from . import routes
