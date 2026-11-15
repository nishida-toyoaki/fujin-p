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
User_groups Blueprint
ユーザとグループ：ユーザ管理（admin）・グループ管理統括・グループ管理（管理者）
"""
import os

from flask import Blueprint
from jinja2 import ChoiceLoader, FileSystemLoader

user_groups_bp = Blueprint(
    'user_groups',
    __name__,
    template_folder='templates'
)

# テンプレートは templates/ を正とする．アプシャのエクスポータが templates/ しか
# 拾わないため，この位置に置いたものだけがパッケージに載る．
# 移行の途中で user_groups_templates/ に残っていても描画できるよう，両方を見る．
_HERE = os.path.dirname(os.path.abspath(__file__))
user_groups_bp.jinja_loader = ChoiceLoader([
    FileSystemLoader(os.path.join(_HERE, 'templates')),
    FileSystemLoader(os.path.join(_HERE, 'user_groups_templates')),
])

# routes.pyをインポート（循環インポート回避のため最後に）
from . import routes
from . import ledger   # 発令台帳（段階1，2026-09-07）
from . import pids     # 永続ID（サイト間の突合，2026-09-14）
from . import stewards # グループの分散管理（統括・管理者，2026-10-20）
from . import tree     # 組織情報管理のツリー（親子は共有可・期間つき，2026-10-20）
from . import migrate  # 永続IDの起点の設定とサイト移行用の書き出し（JSON，2026-10-20）
