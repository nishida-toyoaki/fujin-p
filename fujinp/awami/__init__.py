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
awami - あわみ（our_meeting）
ナラティブ・ネットワーク共有アプリ．

ナラティブ素（意味づけられた出来事や状態．実体は既存のMD文書等のURL）をノード，
ナラティブ結合子（2以上のノードを特定の意味で結ぶハイパーエッジ）をエッジとして，
無限キャンバス上に配置・共有する．ノード内容の編集・閲覧は既存の
MDエディタ／プレビュアーに委ね，本アプリは「網」の管理のみを行う．

第1版の権限モデル：
- キャンバス作成者（owner）のみが編集できる．他者は read only．
- 表示制御はノード単位（カテゴリ＋まいぐる）．ACL未設定のノードはキャンバスの
  アクセス権に従う．不可視ノードを端点から失ったエッジは，主ノードが不可視か
  可視端点が2未満になった時点でエッジごと非表示になる．
"""
from flask import Blueprint

# Blueprint定義
our_meeting_bp = Blueprint(
    'our_meeting',
    __name__,
    template_folder='templates',
    url_prefix='/awami'
)

from . import routes
