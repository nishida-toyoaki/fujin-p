# -*- coding: utf-8 -*-
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

"""Tag Chase - ルート定義

純ビューアアプリのためデータベースは使用しない。
ゲームロジック・描画はすべてフロントエンド（Three.js / WebGL）で行う。

【公開設定】
イベント期間中はログイン不要で公開（PUBLIC_ACCESS = True）。
終了後は PUBLIC_ACCESS = False に書き換えてリロードすると、
従来どおり @login_required 相当の保護がかかる。
"""
import logging

from flask import render_template, redirect, session

# FUJIN-P共通モジュール（常に存在する前提）
from auth import redirect_to_dashboard
from decorators import login_required

# Blueprint読み込み
from . import tag_chase_bp

# ────────────────────────────────────────────
# 公開スイッチ（イベント終了後は False に戻す）
# False にすると、ログイン保護がかかると同時に
# ログインページのカードも自動的に非表示になる。
# ────────────────────────────────────────────
PUBLIC_ACCESS = True


@tag_chase_bp.record_once
def _register_public_flag(state):
    """公開状態を Flask config に載せる（テンプレートから参照可能にする）。"""
    state.app.config['TAG_CHASE_PUBLIC'] = PUBLIC_ACCESS


def _index():
    """鬼ごっこ3D画面"""
    return render_template('tag_chase/index.html')


# PUBLIC_ACCESS に応じてログイン保護を切り替えて登録する。
# endpoint は常に 'tag_chase.index'（url_for の参照名は変わらない）。
if PUBLIC_ACCESS:
    tag_chase_bp.route('/', endpoint='index')(_index)
else:
    tag_chase_bp.route('/', endpoint='index')(login_required(_index))


# ────────────────────────────────────────────
# FUJIN-Pへ戻る
# ────────────────────────────────────────────

@tag_chase_bp.route('/return_to_fujin')
def return_to_fujin():
    """ログイン済みならダッシュボードへ、未ログインならトップ（ログインページ）へ戻る。"""
    if session.get('user_id'):
        return redirect_to_dashboard()
    return redirect('/')