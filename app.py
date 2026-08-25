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

    # ---- kernel の Blueprint（app.py が固定で登録する）----
    #   auth / profile / admin / guest / app_share の5つだけ．
    #   アプリはすべて正本（fujinp/app_registry.json）から登録する．
    from auth import auth_bp
    from profile import profile_bp

    from fujinp.admin import admin_bp
    from fujinp.admin.guest import guest_bp

    from fujinp.app_share import app_share_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(profile_bp, url_prefix='/user')

    app.register_blueprint(admin_bp, url_prefix='/admin')
    app.register_blueprint(guest_bp, url_prefix='/guest')

    app.register_blueprint(app_share_bp)

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

    # ---- アプリの Blueprint は正本（fujinp/app_registry.json）から登録する ----
    #   正本はアプシャの「発行」で書き出される．app.py にアプリを追記する運用は終了．
    #   1アプリの失敗はログに残して続行し，サイト全体は止めない．
    from fujinp.registry import register_blueprints
    register_blueprints(app)

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