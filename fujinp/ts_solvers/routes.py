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

from flask import Blueprint, render_template, session, redirect
from auth import login_required
import os

ts_solvers_bp = Blueprint('ts_solvers', __name__, template_folder='templates')

@ts_solvers_bp.route('/')
# @login_required
def index():
    return render_template('ts_solver_index.html')

@ts_solvers_bp.route('/plain')
# @login_required
def plain():
    template_dir = os.path.join(os.path.dirname(__file__), 'templates')
    with open(os.path.join(template_dir, 'plain_ts_solver.html'), 'r', encoding='utf-8') as f:
        return f.read()

@ts_solvers_bp.route('/improved')
# @login_required
def improved():
    template_dir = os.path.join(os.path.dirname(__file__), 'templates')
    with open(os.path.join(template_dir, 'improved_ts_solver.html'), 'r', encoding='utf-8') as f:
        return f.read()

@ts_solvers_bp.route('/further_improved')
# @login_required
def further_improved():
    template_dir = os.path.join(os.path.dirname(__file__), 'templates')
    with open(os.path.join(template_dir, 'further_improved_ts_solved.html'), 'r', encoding='utf-8') as f:
        return f.read()

@ts_solvers_bp.route('/finish')
# @login_required
def finish():
    user_category = session.get('user_category')
    if user_category == 'admin':
        return redirect('/admin/dashboard')
    else:
        return redirect('/guest/dashboard')