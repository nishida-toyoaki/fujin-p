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
まいあし (MaiAshi) Blueprint

2026-10-20 第1段：入口を1つにし，図書館（文書アーカイブの文書への目次）と
配布ファイル（導入スクリプトと組み立て道具）だけを持つ形に作り直した．
教材（かたりべ作品の並び）は第2段，教室は第3段で足す．
"""
from flask import Blueprint

migration_assistant = Blueprint(
    'migration_assistant',
    __name__,
    url_prefix='/migration_assistant',
    template_folder='templates',
    static_folder=None,          # 2026-10-05 データ流出を防ぐため静的配信を無効化．復活させないこと
)

from . import migration_assistant_routes
