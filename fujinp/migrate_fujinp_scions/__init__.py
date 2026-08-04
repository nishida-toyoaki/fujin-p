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
migrate_fujinp_scions （さいまる）- Blueprint 定義

FUJIN-P サイトのマイグレーション支援ツール。
エクスポーター（パッケージ作成・レビュー・登録）と
インポーター（登録済みパッケージの一覧・ダウンロード）の
2つの機能を提供する。

■ セキュリティ上の重要な注意（2026-07-25 修正）
    このアプリは static_folder を持たない（static_folder=None）。

    以前は static_folder='static' が設定されており、パッケージ ZIP の
    保存先が <module>/static/migration_packages/ だったため、

        /migrate_fujinp_scions/static/migration_packages/<filename>

    という URL で、@login_required を経由せずに（＝未ログインでも）
    サイト全体のソースコードを含む ZIP を取得できる状態だった。

    現在はパッケージの保存先を <module>/migration_packages/ に移し、
    配信は必ず api_import_download（権限チェックあり）を通す。
    この Blueprint に static_folder を復活させてはならない。
"""
from flask import Blueprint

migrate_fujinp_scions_bp = Blueprint(
    'migrate_fujinp_scions',
    __name__,
    url_prefix='/migrate_fujinp_scions',
    template_folder='templates',
    static_folder=None,          # ← 静的配信を無効化（ZIP の未認証露出を防ぐ）
)

# ルート定義を読み込む（循環 import 回避のため末尾で）
from . import routes  # noqa: E402,F401