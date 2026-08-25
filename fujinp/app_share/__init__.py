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
App Share (アプシェア) - FUJIN-Pアプリケーション共有システム

アプリケーションを他のFUJIN-Pサイトと共有・配布するためのシステム。
テーシャ（Table Share）と連携してアライアンスサイト情報を共有。
"""
from flask import Blueprint

app_share_bp = Blueprint(
    'app_share',
    __name__,
    template_folder='templates',
    url_prefix='/app_share'
)

from . import routes
from . import manage   # 段階6a：正本の管理画面・発行・診断
from . import gitsync  # 段階6b：関所への写し・commit・push
from . import package  # 段階6c：パッケージ輸出入（v3）