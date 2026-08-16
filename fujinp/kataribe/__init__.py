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
かたりべ (kataribe) - 人×AIコラボのプレゼン語り編集アプリ
発想をストーリー化する：ブロックの登場・退場で語りを組み立てるページレス・プレゼンエディタ．
Claudeとの協働は「依頼文の生成→手渡し→回答スペックの総替え取り込み」の手動往復方式（v1）．
"""
from flask import Blueprint

kataribe_bp = Blueprint(
    'kataribe',
    __name__,
    template_folder='templates',
    url_prefix='/kataribe'
)

from . import routes
