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
FUJIN-Pスタブの段階的構築をガイドするマイグレーションアシスタント
"""
from flask import Blueprint

migration_assistant = Blueprint(
    'migration_assistant',
    __name__,
    url_prefix='/migration_assistant',
    template_folder='migration_assistant_templates',
    static_folder='static',
    static_url_path='static'         # ← 追加
)

from . import migration_assistant_routes