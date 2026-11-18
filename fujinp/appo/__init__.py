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
appo - あぽる
オフィスアワー（対面／オンライン）を公開し，招待と請求でミーティングを成立させる会員制の面会調整アプリ．
排他的（1対1）と非排他的（懇談）を区別し，直前の予約・キャンセルとサブチャンネルでの連絡を扱う．
"""
from flask import Blueprint

appo_bp = Blueprint(
    'appo',
    __name__,
    template_folder='templates',
    url_prefix='/appo'
)

from . import routes
