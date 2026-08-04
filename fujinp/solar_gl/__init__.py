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
Solar GL - 太陽系3Dシミュレータ
太陽と8惑星（＋月）を Three.js（WebGL）でブラウザ描画するビューアアプリ。
各惑星は実際の公転周期の比率で公転し、視点回転・ズーム・時間制御ができる。
"""
from flask import Blueprint

# Blueprint定義
solar_gl_bp = Blueprint(
    'solar_gl',            # Blueprint名（エンドポイント名に使用）
    __name__,
    template_folder='templates',
    url_prefix='/solar_gl'  # URLプレフィックス
)

from . import routes
