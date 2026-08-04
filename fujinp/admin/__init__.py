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
admin - FUJIN-P 管理者機能
ユーザ管理（追加・編集・論理削除・復元・物理削除・招待メール送信）と，
登録申請管理（承認・不承認・ブラックリスト・承認済み一覧）を提供する．
※フィーチャー機能・アプリ管理／アプリ権限制御機能は廃止済み．
  既存リンクからの url_for エラーを避けるためルート名だけ残し，
  DBにはアクセスせず flash + redirect のみを行う．
"""
from flask import Blueprint

# Blueprint定義
admin_bp = Blueprint(
    'admin',
    __name__,
    template_folder='templates'
)

from . import routes  # noqa: E402  ルート登録（admin_bp 定義後でないと循環する）

# 他モジュールから `from admin import ...` されている関数の再エクスポート
from .routes import (  # noqa: E402,F401
    get_registration_statistics,
    admin_get_user_feature_labels,
)