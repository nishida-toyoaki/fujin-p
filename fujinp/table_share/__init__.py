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
テーシャ (Table Share) - FUJIN-Pアライアンス間テーブル共有システム

FUJIN-Pサイト間でMySQLテーブルのネットワーク状の緩やかな同期を実現します。
- Publish: テーブルを公開（バージョン管理付き）
- Subscribe: 他サイトからテーブルを取得（バックアップ付き）
"""
from flask import Blueprint


table_share_bp = Blueprint('table_share', __name__,
                           url_prefix='/table_share',
                           template_folder='templates')

from . import routes
