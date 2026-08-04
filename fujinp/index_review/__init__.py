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
index_review - 大学指標ビジュアル分析
nishida$fujinp の T_XX テーブルから数値指標を取り出し、
2016〜2025 年度の推移を折れ線・棒グラフで可視化するアプリ。
基礎指標（プリセット）と合成指標（ユーザー定義 SQL）を重ねて表示できる。
"""
from flask import Blueprint

index_review_bp = Blueprint(
    'index_review',
    __name__,
    template_folder='templates',
    url_prefix='/index_review'
)

from . import routes
