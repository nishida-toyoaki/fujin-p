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

"""テーコン（table_converter） —— お絵描きエクセルと，ふつうのSQLテーブル群を往復する。

事務の現場でエクセルは表計算ではなく，お絵描きの道具として使われている。
そこで使われる仕掛けは，つきつめると「セルの合併」ひとつである。

テーコンは，人と生成AIが標本を見ながらこしらえた「対応式」にしたがって，
絵を〈ふつうのSQLテーブル群の積〉と〈残余〉に分解し，また組み立て直す。
絵でやりとりしている現場と，データでやりとりする世界をつなぐための道具である。
"""

from flask import Blueprint

table_converter_bp = Blueprint(
    'table_converter', __name__,
    url_prefix='/table_converter',
    template_folder='templates',
)

from . import routes  # noqa: E402,F401
