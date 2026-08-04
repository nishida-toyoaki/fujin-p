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

# sql_saver/__init__.py
# SQL Saver - プラットフォーム全DBのバックアップ & リストア（簡易マイグレーション支援）
#
# 設計方針:
#   - 単一目的（バックアップ / リストア）に絞り、複雑化させない
#   - table_master / table_cycle と同じ Blueprint + ルート分割 + DatabaseConfig 流儀
#   - 書き出しは「CREATE TABLE 文 + 全行データ」を含む JSON（CSV/xlsxのセル制限・特殊文字問題を回避）
#   - リストアは常に完全置換 (DROP→CREATE→INSERT)。実行前に破壊対象を一括確認
#   - 対象DBは DatabaseConfig / Config から動的に列挙
#
# v1.1 (2026-07-27) の変更:
#   - 作業領域を static/ の外へ移した。zip が静的配信経路から素通しで取得される状態を解消
#   - before_request による admin 一括判定に変更（画面も含めてBlueprint全体を管理者限定）
#   - 日時をJST固定に統一（FUJIN-P 日時3層ルール準拠）
#   - SQL識別子（DB名・テーブル名・カラム名）の検査を追加
#   - リストアをバックアップと同じジョブ方式（start → step×N → finish）に統一し、進捗表示を追加
#   - テーブル単位の復元選択に対応
#   - 操作履歴を sql_saver_audit テーブルに記録し、画面から閲覧できるようにした

from flask import Blueprint

sql_saver_bp = Blueprint(
    'sql_saver',
    __name__,
    template_folder='templates',
    url_prefix='/sql_saver'
)

# ルートを読み込んで Blueprint に登録
from . import routes            # noqa: E402,F401  アクセス制御 + 画面 + DB一覧 + 共通ヘルパ
from . import routes_backup     # noqa: E402,F401  バックアップ生成 + zip
from . import routes_restore    # noqa: E402,F401  zip解析 + 復元