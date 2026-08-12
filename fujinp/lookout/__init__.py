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
lookout（みはらし） - 地形パノラマ展望アプリ

北近畿の地図上でクリックした地点から見た360度の地形パノラマ
（可視稜線）を地理院標高タイル（DEM）から計算して表示する。
視点高は地上0〜100mで調整でき、0mは当該地点の標高を
10m解像度DEMから正確に求める。
"""
from flask import Blueprint

lookout_bp = Blueprint(
    'lookout',
    __name__,
    template_folder='templates',
    url_prefix='/lookout'
)

from . import routes
