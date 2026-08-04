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
official_data_archive - 公式データ集
大学の公式データ（SQLテーブル）を登録し、読み取り専用で
閲覧・検索・XLSXダウンロードできるアーカイブ。
data_center の「元データ閲覧」機能を独立・単純化したアプリ。

  - 公式テーブルの一覧・閲覧・絞り込み・XLSXダウンロード（regular以上）
  - 公式テーブルへのアイテム（SQLテーブル）の追加・削除（管理者）
  - 削除は「登録簿からの除外」のみ。SQLテーブル自体は絶対に DROP しない
"""
from flask import Blueprint

official_data_archive_bp = Blueprint(
    'official_data_archive',
    __name__,
    template_folder='templates',
    url_prefix='/official_data_archive'
)

from . import routes
