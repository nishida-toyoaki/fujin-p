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

"""こんか（CQM / conversation quanta） — 会話量子の倉庫と，親子のあいだの二方向のやり取り。

倉庫は種別と作法しか知らない。様式の語彙（項目名・欄の名前）は
すべてデータの側にあり，このパッケージのコードには現れない。
"""

from flask import Blueprint

cqm_bp = Blueprint(
    'cqm', __name__,
    url_prefix='/cqm',
    template_folder='templates',
)

from . import routes  # noqa: E402,F401
from . import ids  # noqa: E402,F401
