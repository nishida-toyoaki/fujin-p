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

from flask import Blueprint, render_template, request, session, flash, redirect, url_for
from decorators import login_required
from db import get_db_cursor
from datetime import datetime
from config import Config
from utils import hash_password, verify_password

profile_bp = Blueprint('profile', __name__)

@profile_bp.route('/profile', methods=['GET', 'POST'])
@login_required
def edit_profile():
    user_id = session.get('user_id')
    if request.method == 'POST':
        full_name = request.form.get('full_name')
        affiliation = request.form.get('affiliation', '').strip()  # ← 追加

        if not full_name or len(full_name.strip()) == 0:
            flash('氏名を入力してください', 'error')
            return redirect(url_for('profile.edit_profile'))

        with get_db_cursor() as (cursor, conn):
            try:
                cursor.execute("""
                    UPDATE users SET full_name = %s, affiliation = %s WHERE id = %s
                """, (full_name, affiliation or None, user_id))  # ← affiliation追加
                conn.commit()
                session['user_name'] = full_name
                flash('プロフィールを更新しました', 'success')
                if session.get('user_category') == 'admin':
                    return redirect(url_for('admin.dashboard'))
                else:
                    return redirect(url_for('guest.dashboard'))
            except Exception as e:
                conn.rollback()
                flash(f'エラー: {str(e)}', 'error')

    with get_db_cursor() as (cursor, conn):
        cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        user = cursor.fetchone()

    return render_template('edit_profile.html', user=user)

@profile_bp.route('/profile/password', methods=['GET', 'POST'])
@login_required
def change_password():
    user_id = session.get('user_id')

    # Google認証ユーザーはパスワード変更不可
    with get_db_cursor() as (cursor, conn):
        cursor.execute("SELECT password_hash FROM users WHERE id = %s", (user_id,))
        user = cursor.fetchone()

    is_google_user = not user or not user['password_hash']
    if is_google_user:
        flash('Googleアカウントのユーザーはパスワード変更できません', 'info')
        return redirect(url_for('profile.edit_profile'))

    if request.method == 'POST':
        current_password = request.form.get('current_password')
        new_password = request.form.get('new_password')
        new_password_confirm = request.form.get('new_password_confirm')

        # バリデーション
        if not all([current_password, new_password, new_password_confirm]):
            flash('すべての項目を入力してください', 'error')
            return redirect(url_for('profile.change_password'))

        if new_password != new_password_confirm:
            flash('新しいパスワードが一致しません', 'error')
            return redirect(url_for('profile.change_password'))

        if len(new_password) < 8:
            flash('パスワードは8文字以上にしてください', 'error')
            return redirect(url_for('profile.change_password'))

        with get_db_cursor() as (cursor, conn):
            # 現在のパスワードを確認
            cursor.execute("SELECT password_hash FROM users WHERE id = %s", (user_id,))
            user = cursor.fetchone()

            if not user or not verify_password(current_password, user['password_hash']):
                flash('現在のパスワードが正しくありません', 'error')
                return redirect(url_for('profile.change_password'))

            try:
                # パスワードを更新
                new_password_hash = hash_password(new_password)
                cursor.execute("""
                    UPDATE users SET password_hash = %s WHERE id = %s
                """, (new_password_hash, user_id))
                conn.commit()

                flash('パスワードを変更しました', 'success')

                # カテゴリに応じたダッシュボードにリダイレクト
                if session.get('user_category') == 'admin':
                    return redirect(url_for('admin.dashboard'))
                else:
                    return redirect(url_for('guest.dashboard'))
            except Exception as e:
                conn.rollback()
                flash(f'エラー: {str(e)}', 'error')

    return render_template('change_password.html')