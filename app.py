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

from flask import Flask
from authlib.integrations.flask_client import OAuth
from flask_mail import Mail
import os
import sys
from datetime import datetime
from config import config

oauth = OAuth()
mail = Mail()  # 追加

def create_app():
    app = Flask(__name__)

    # 設定読み込み
    config_name = os.getenv('FLASK_ENV', 'default')
    app.config.from_object(config[config_name])
    # OAuth初期化
    oauth.init_app(app)
    oauth.register(
        name='google',
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={'scope': 'openid email profile'}
    )
    # Mail初期化
    mail.init_app(app)

    # FUJIN-P のBlueprint
    from auth import auth_bp
    from profile import profile_bp

    from fujinp.admin import admin_bp
    from fujinp.admin.guest import guest_bp
    from fujinp.table_cycle import table_cycle_bp
    from fujinp.table_master import table_master_bp
    from fujinp.sql_saver import sql_saver_bp
    from fujinp.slack_minutes import slack_minutes_bp
    from fujinp.fukko import fukko_bp

    from fujinp.migration_assistant import migration_assistant
    from fujinp.table_share import table_share_bp
    from fujinp.user_migration import user_migration_bp
    from fujinp.migrate_fujinp_scions import migrate_fujinp_scions_bp
    from fujinp.my_md_notes import my_md_notes_bp

    from fujinp.user_groups import user_groups_bp
    from fujinp.colrep import colrep_bp
    from fujinp.colrep import colrep_public_bp
    from fujinp.colrep.scripts.excel_helper import excel_helper_bp
    from fujinp.table_post import table_post_bp

    from fujinp.app_share import app_share_bp
    from fujinp.official_data_archive import official_data_archive_bp
    from fujinp.data_center import data_center_bp
    from fujinp.index_review import index_review_bp
    from fujinp.stats.stats import stats_bp

    from fujinp.awami import our_meeting_bp
    from fujinp.document_archive import document_archive_bp
    from fujinp.free_hand_curve.routes import free_hand_curve_bp
    from fujinp.ts_solvers.routes import ts_solvers_bp
    from fujinp.solar_gl import solar_gl_bp
    from fujinp.sorakara import sorakara_bp
    from fujinp.block_breaker import block_breaker_bp
    from fujinp.window_shopping import window_shopping_bp
    from fujinp.tag_chase import tag_chase_bp

    from fujinp.strm import strm_bp

    app.register_blueprint(auth_bp)   ## ここはauthに関係するところなのでこのまま
    app.register_blueprint(profile_bp, url_prefix='/user')

    app.register_blueprint(admin_bp, url_prefix='/admin')
    app.register_blueprint(guest_bp, url_prefix='/guest')
    app.register_blueprint(table_cycle_bp, url_prefix='/table_cycle')
    app.register_blueprint(table_master_bp)
    app.register_blueprint(sql_saver_bp)
    app.register_blueprint(slack_minutes_bp)
    app.register_blueprint(fukko_bp)

    app.register_blueprint(migration_assistant, url_prefix='/migration_assistant')
    app.register_blueprint(table_share_bp)
    app.register_blueprint(user_migration_bp)
    app.register_blueprint(migrate_fujinp_scions_bp)
    app.register_blueprint(my_md_notes_bp)

    app.register_blueprint(user_groups_bp, url_prefix='/user_groups')
    app.register_blueprint(colrep_bp, url_prefix='/colrep')
    app.register_blueprint(colrep_public_bp, url_prefix='/colrep_public')
    app.register_blueprint(table_post_bp)

    app.register_blueprint(app_share_bp)
    app.register_blueprint(official_data_archive_bp)
    app.register_blueprint(data_center_bp)
    app.register_blueprint(index_review_bp)
    app.register_blueprint(stats_bp, url_prefix='/stats')


    app.register_blueprint(our_meeting_bp)
    app.register_blueprint(document_archive_bp, url_prefix='/document_archive')
    app.register_blueprint(free_hand_curve_bp, url_prefix='/free_hand_curve')
    app.register_blueprint(ts_solvers_bp, url_prefix='/ts_solvers')
    app.register_blueprint(solar_gl_bp)
    app.register_blueprint(sorakara_bp)
    app.register_blueprint(block_breaker_bp)

    app.register_blueprint(window_shopping_bp)
    app.register_blueprint(tag_chase_bp)

    app.register_blueprint(strm_bp)


    # ログ設定
    if not app.debug:
        import logging
        from logging.handlers import RotatingFileHandler
        handler = RotatingFileHandler('/home/nishida4fujinp/fujinp.log',
                                     maxBytes=10485760, backupCount=10)
        handler.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'
        ))
        handler.setLevel(logging.INFO)
        app.logger.addHandler(handler)
        app.logger.setLevel(logging.INFO)
        app.logger.info('FUJIN-P startup')

    @app.template_filter('format_date')
    def format_date_filter(value, format='%Y-%m-%d'):
        if value is None:
            return ''
        if isinstance(value, str):
            return value
        return value.strftime(format)

    return app

app = create_app()
google = oauth.google

@app.route('/')
def index():
    from flask import redirect, url_for
    return redirect(url_for('auth.login'))

