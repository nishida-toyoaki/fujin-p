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

from functools import wraps
from flask import session, redirect, url_for, flash, request

def login_required(f):
    """ログイン必須デコレータ"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('ログインが必要です', 'error')
            return redirect(url_for('auth.login', next=request.url))  # ← next を付加
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    """管理者必須デコレータ"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('ログインが必要です', 'error')
            return redirect(url_for('auth.login', next=request.url))  # ← 同様に
        if session.get('user_category') != 'admin':
            flash('管理者権限が必要です', 'error')
            return redirect(url_for('guest.dashboard'))
        return f(*args, **kwargs)
    return decorated_function

def feature_required(feature_code):
    """フィーチャー必須デコレータ"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user_id' not in session:
                flash('ログインが必要です', 'error')
                return redirect(url_for('auth.login', next=request.url))

            from db import get_db_cursor
            user_id = session['user_id']

            with get_db_cursor() as (cursor, conn):
                cursor.execute("""
                    SELECT COUNT(*) as count FROM user_features uf
                    JOIN features f ON uf.feature_id = f.id
                    WHERE uf.user_id = %s AND f.feature_code = %s AND f.is_active = TRUE
                """, (user_id, feature_code))
                result = cursor.fetchone()

                if result['count'] == 0:
                    flash('この機能へのアクセス権限がありません', 'error')
                    return redirect(url_for('guest.dashboard'))

            return f(*args, **kwargs)
        return decorated_function
    return decorator