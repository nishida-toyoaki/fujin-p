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
slack_minutes - Slack チャンネル議事録取得アプリ

指定したSlackチャンネルのメッセージを取得・保存し，
議事録として閲覧できるようにするアプリケーション．

依存：
  - Config.SLACK_BOT_TOKEN（notifiers.py と共用）
  - 追加Slackスコープ：channels:read / channels:history /
                       groups:read / groups:history / users:read
"""
from flask import Blueprint

slack_minutes_bp = Blueprint(
    'slack_minutes',
    __name__,
    template_folder='templates',
    url_prefix='/slack_minutes'
)

from . import routes
