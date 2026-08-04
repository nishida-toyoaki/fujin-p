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
strm - シンプル時空資源予約
部屋・公用車などの時空資源に対するブロック予約（作成・閲覧・取り下げ）を提供する。
資源ごとに承認設定（なし／管理者／まいぐるグループ）を持ち、
「なし」は即確定、それ以外は該当承認者の誰か1人の承認で確定となる。
"""
from flask import Blueprint

strm_bp = Blueprint(
    'strm',
    __name__,
    template_folder='templates',
    url_prefix='/strm'
)

from . import routes
