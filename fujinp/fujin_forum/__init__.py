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
fujin_forum（えふえふ）- FUJIN-P のフォーラムアプリ  v1.0（2026-08-27）

ミニ Slack 型の3列構成（チャンネル一覧／記事一覧／記事とスレッド）で，
Markdown の記事を投稿・返信・リアクションできる．すらくみ（slack_minutes）の
アーカイブをチャンネルごとに取り込める（admin）．

依存：
  - decorators.login_required，db.DatabaseConfig，config.Config（UPLOAD_BASE_DIR）
  - markdown_converter.process_markdown（サイト共通の MD → HTML）
  - まいぐる（user_groups / user_group_memberships）：公開範囲のグループ判定
  - fujinp.slack_minutes.mrkdwn（取込時の Slack mrkdwn → MD 復元．無ければ生テキスト）

テーブル：fujin_forum_channels / _access_groups / _posts / _reactions / _attachments / _reads
"""
from flask import Blueprint

fujin_forum_bp = Blueprint(
    'fujin_forum', __name__,
    url_prefix='/fujin_forum',
    template_folder='templates',
)

from . import routes  # noqa: E402,F401
