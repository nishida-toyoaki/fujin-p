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


# ════════════════════════════════════════════════════════════
# カーネルでの一括防御（脆弱性点検報告 §5，2026-10-26）
#
# 入口（before_request）
#   1. 書き込み（GET・HEAD・OPTIONS 以外）の Origin 照合（CSRF 対策）．
#      Origin（なければ Referer）が自サイトでなければ拒否する．
#      両方ないとき（curl・Scheduled Task などブラウザ以外）は通す．
#   2. 未ログインの書き込みの既定拒否．
#      許可リストの口と，カーネルの auth・Blueprint なしの経路は対象外．
# 出口（after_request）
#   nosniff・Referrer-Policy・X-Frame-Options を付け，
#   send_file で返す HTML・SVG・XML には CSP sandbox を付ける．
#   500 番台の応答は本文を定型文に差し替え，元の本文はログに書く（2026-10-31 追加）．
# セッション Cookie には Secure・HttpOnly・SameSite=Lax を付ける．
#
# 設定（config.py）
#   SECURITY_GUARD_MODE = 'report'   記録だけ（既定）．~/fujinp.log に
#                                    [security_guard] WOULD-BLOCK と出る
#                       = 'enforce'  拒否する
#   SECURITY_PUBLIC_WRITE_ENDPOINTS  未ログインの書き込みを許す endpoint 名の追加
#   SECURITY_CROSS_ORIGIN_ENDPOINTS  Origin 照合を免除する endpoint 名の追加
#   SECURITY_TRUSTED_ORIGINS         自サイト以外に信頼するオリジン
#   SECURITY_FRAME_OPTIONS           既定 'SAMEORIGIN'．None で付けない
#   SECURITY_COOKIE_SECURE           既定 True．http のローカル開発では False
#   SECURITY_SESSION_RECHECK_SEC     既定 300．ログイン中の人を users 表で確かめ直す間隔（秒）
#   SECURITY_SESSION_MAX_DAYS        既定 30．ログインからこの日数でセッションを切る
#   SECURITY_MASK_SERVER_ERRORS      既定 True．500 番台の本文を定型文にする．False で元のまま返す
#
# セッションの確かめ直し（2026-10-26 追加）
#   Flask のセッションは署名つきクッキーで，サーバ側で消せない．このため，削除・停止した人や
#   区分を下げた人も，古いクッキーで以前の権限のまま入れてしまう．一定間隔で users 表と照らし，
#   削除・停止されていればセッションを消し，区分と名前は表の値に合わせる．
#
# 500 番台の定型化（2026-10-31 追加，点検報告 4.2 の Low 項目）
#   多くのアプリが except 節で str(e)（MySQL のエラー文など）を 500 応答の本文に入れて返す．
#   表名・列名・SQL の断片が利用者に見えるので，出口でまとめて定型文に差し替える．
#   元の本文はエラー番号つきで ~/fujinp.log に書き，画面にはエラー番号だけを出す．
#   admin には元の本文をそのまま返す（デバッグのため）．
#   JSON は {"success": false, "error": ...} の形を保ち，HTML は短い定型ページにする．
#   send_file・ストリーミングの応答は本文を読まずにそのまま通す．
#   400 番台はアプリが意図して返す案内文が多いので対象にしない．
# ════════════════════════════════════════════════════════════
import re as _re
import secrets as _secrets
from html import escape as _html_escape
from urllib.parse import urlsplit as _urlsplit

_SAFE_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS'})

# 未ログインでも書き込みを受ける正規の口（各アプリが自分でトークン等を検査する）
_PUBLIC_WRITE_ENDPOINTS = frozenset({
    'all_portal.mcp',                          # オール：MCP
    'all_portal.oauth_register',               # オール：OAuth
    'all_portal.oauth_token',
    'all_portal.oauth_revoke',
    'our_meeting.api_vote',                    # あわみ：投票トークン
    'file_container.public_entry',             # ふぁいこん：受け渡し窓口
    'file_container.public_upload',
    'kitare_deliberation.api_deadline_batch',  # キターレ：締切バッチ
})

# 未ログインの書き込み判定の対象外（None はカーネル直下の経路）
_KERNEL_OPEN_BLUEPRINTS = frozenset({None, 'auth'})

# Origin 照合を免除する口（他オリジンから正規に呼ばれる API）
_CROSS_ORIGIN_ENDPOINTS = frozenset({
    'all_portal.mcp',
    'all_portal.oauth_register',
    'all_portal.oauth_token',
    'all_portal.oauth_revoke',
})
# endpoint 名がわからない MCP 窓口などはパスで免除する
_CROSS_ORIGIN_PATH_RE = _re.compile(r'(/mcp/?$|/oauth/(register|token|revoke)$)')

# send_file で返すときに sandbox を付ける型
_ACTIVE_MIMETYPES = frozenset({
    'text/html', 'application/xhtml+xml', 'image/svg+xml',
    'text/xml', 'application/xml',
})

# 500 番台の定型文
_SERVER_ERROR_MESSAGE = '内部エラーが発生しました'
_SERVER_ERROR_LOG_LIMIT = 4000   # ログに書く元の本文の上限（文字）
_SERVER_ERROR_PAGE = '''<!DOCTYPE html>
<html lang="ja">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>エラー</title>
<style>
body{{font-family:'Noto Sans JP',sans-serif;background:#f5f7fa;color:#2d3748;margin:0;padding:48px 16px;}}
.box{{max-width:520px;margin:0 auto;background:#fff;border-radius:10px;padding:28px 32px;box-shadow:0 1px 3px rgba(0,0,0,.1);line-height:1.8;}}
h1{{font-size:1.2rem;margin:0 0 12px;}}
.code{{color:#718096;font-size:.85rem;}}
a{{color:#4f46e5;}}
</style></head>
<body><div class="box">
<h1>{status} {message}</h1>
<p>処理の途中で問題が起きました．時間をおいてもう一度お試しください．</p>
<p>続くときは，次のエラー番号を管理者にお知らせください．</p>
<p class="code">エラー番号：{error_id}</p>
<p><a href="javascript:history.back()">前の画面に戻る</a></p>
</div></body></html>'''


def install_security(app):
    from flask import request, session, jsonify, Response

    def cfg(key, default):
        return app.config.get(key, default)

    # ── セッション Cookie ──
    if cfg('SECURITY_COOKIE_SECURE', True):
        app.config['SESSION_COOKIE_SECURE'] = True
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    if not app.config.get('SESSION_COOKIE_SAMESITE'):
        app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

    def client_ip():
        xff = request.headers.get('X-Forwarded-For', '')
        if xff:
            return xff.split(',')[0].strip()
        return request.headers.get('X-Real-IP') or request.remote_addr or '-'

    def origin_of(url):
        try:
            p = _urlsplit(url)
        except ValueError:
            return None
        if not p.scheme or not p.netloc:
            return None
        return f'{p.scheme}://{p.netloc}'.lower()

    def allowed_origins():
        host = (request.host or '').lower()
        allowed = {f'https://{host}', f'http://{host}'}
        for o in cfg('SECURITY_TRUSTED_ORIGINS', ()) or ():
            allowed.add(o.rstrip('/').lower())
        return allowed

    def path_exempt():
        return bool(_CROSS_ORIGIN_PATH_RE.search(request.path or ''))

    def cross_origin_exempt():
        ep = request.endpoint or ''
        return (ep in _CROSS_ORIGIN_ENDPOINTS
                or ep in set(cfg('SECURITY_CROSS_ORIGIN_ENDPOINTS', ()) or ())
                or path_exempt())

    def public_write():
        ep = request.endpoint or ''
        return (ep in _PUBLIC_WRITE_ENDPOINTS
                or ep in set(cfg('SECURITY_PUBLIC_WRITE_ENDPOINTS', ()) or ())
                or path_exempt())

    def check_origin():
        """自サイト以外からの書き込みなら理由を返す．問題なければ None．"""
        origin = request.headers.get('Origin')
        if origin is not None:
            o = origin.strip().lower()
            if o == 'null' or o not in allowed_origins():
                return f'origin mismatch ({origin})'
            return None
        referer = request.headers.get('Referer')
        if referer:
            ro = origin_of(referer)
            if ro is None or ro not in allowed_origins():
                return f'referer mismatch ({ro})'
        return None   # どちらもない：ブラウザ以外とみなして通す

    def reject(status, message):
        wants_json = (request.is_json
                      or '/api/' in (request.path or '')
                      or 'application/json' in (request.headers.get('Accept') or ''))
        if wants_json:
            return jsonify({'success': False, 'error': message}), status
        return Response(message, status=status, mimetype='text/plain; charset=utf-8')

    @app.before_request
    def _security_session_recheck():
        uid = session.get('user_id')
        if not uid or request.endpoint == 'static':
            return None
        import time
        from datetime import datetime, timedelta
        now = time.time()
        # ログインからの日数の上限
        max_days = cfg('SECURITY_SESSION_MAX_DAYS', 30)
        login_at = session.get('login_at')
        if not login_at:
            session['login_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')   # 改修前からのセッション
        elif max_days:
            try:
                t0 = datetime.strptime(login_at, '%Y-%m-%d %H:%M:%S')
                if datetime.now() - t0 > timedelta(days=max_days + 1):   # JST と UTC の差を見込んで 1 日足す
                    app.logger.info('[security_guard] session expired user=%s', uid)
                    session.clear()
                    return None
            except ValueError:
                pass
        # users 表との照合（一定間隔）
        if now - float(session.get('auth_checked_at') or 0) < cfg('SECURITY_SESSION_RECHECK_SEC', 300):
            return None
        try:
            from db import get_db_cursor
            with get_db_cursor() as (cur, conn):
                cur.execute("SELECT category, full_name, is_active, deleted_at FROM users WHERE id = %s", (uid,))
                u = cur.fetchone()
        except Exception as e:
            app.logger.warning('[security_guard] session recheck failed: %s', e)
            return None
        if not u or not u.get('is_active') or u.get('deleted_at') is not None:
            app.logger.warning('[security_guard] session cleared (inactive or deleted) user=%s', uid)
            session.clear()
            return None
        if session.get('user_category') != u.get('category'):
            app.logger.warning('[security_guard] category synced user=%s %s -> %s',
                               uid, session.get('user_category'), u.get('category'))
            session['user_category'] = u.get('category')
        if u.get('full_name') and session.get('user_name') != u.get('full_name'):
            session['user_name'] = u.get('full_name')
        session['auth_checked_at'] = now
        return None

    @app.before_request
    def _security_guard_entry():
        if request.method in _SAFE_METHODS:
            return None
        if request.endpoint is None:
            return None   # 経路なし（404/405）は Flask に任せる
        if request.headers.get('Authorization'):
            return None   # トークン認証の API（ブラウザが勝手に付けないヘッダ）

        mode = cfg('SECURITY_GUARD_MODE', 'report')
        reason, status = None, 403

        if not cross_origin_exempt():
            reason = check_origin()

        if reason is None and not session.get('user_id'):
            if request.blueprint not in _KERNEL_OPEN_BLUEPRINTS and not public_write():
                reason, status = 'unauthenticated write', 401

        if reason is None:
            return None

        app.logger.warning(
            '[security_guard] %s %s %s endpoint=%s user=%s ip=%s reason=%s',
            'BLOCK' if mode == 'enforce' else 'WOULD-BLOCK',
            request.method, request.path, request.endpoint,
            session.get('user_id'), client_ip(), reason)
        if mode != 'enforce':
            return None
        if status == 401:
            return reject(401, 'ログインが必要です')
        return reject(403, '他のサイトからの送信は受け付けません．画面を開き直してから操作してください')

    def mask_server_error(resp):
        """500 番台の本文を定型文に差し替える．元の本文はログに書く．"""
        if not cfg('SECURITY_MASK_SERVER_ERRORS', True):
            return resp
        if resp.status_code < 500:
            return resp
        if getattr(resp, 'direct_passthrough', False) or resp.is_streamed:
            return resp   # send_file・ストリーミングは本文を読まない
        try:
            if session.get('user_category') == 'admin':
                return resp   # admin には元の本文を返す
        except Exception:
            pass

        error_id = _secrets.token_hex(4)
        try:
            original = resp.get_data(as_text=True)
        except Exception:
            original = '(本文を読めませんでした)'
        try:
            uid = session.get('user_id')
        except Exception:
            uid = None
        app.logger.error(
            '[security_guard] SERVER-ERROR id=%s status=%s %s %s endpoint=%s user=%s ip=%s body=%s',
            error_id, resp.status_code, request.method, request.path, request.endpoint,
            uid, client_ip(), original[:_SERVER_ERROR_LOG_LIMIT].replace('\n', '\\n'))

        mimetype = (resp.mimetype or '').lower()
        if mimetype == 'application/json' or mimetype.endswith('+json'):
            import json
            body = json.dumps({'success': False,
                               'error': f'{_SERVER_ERROR_MESSAGE}（エラー番号 {error_id}）',
                               'error_id': error_id}, ensure_ascii=False)
            resp.set_data(body)
            resp.mimetype = 'application/json'
        elif mimetype in ('text/html', 'application/xhtml+xml'):
            resp.set_data(_SERVER_ERROR_PAGE.format(
                status=resp.status_code,
                message=_html_escape(_SERVER_ERROR_MESSAGE),
                error_id=error_id))
            resp.mimetype = 'text/html'
            resp.headers['Content-Type'] = 'text/html; charset=utf-8'
        else:
            resp.set_data(f'{_SERVER_ERROR_MESSAGE}（エラー番号 {error_id}）')
            resp.headers['Content-Type'] = 'text/plain; charset=utf-8'
        return resp

    @app.after_request
    def _security_guard_exit(resp):
        h = resp.headers
        h.setdefault('X-Content-Type-Options', 'nosniff')
        h.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')

        frame = cfg('SECURITY_FRAME_OPTIONS', 'SAMEORIGIN')
        csp = h.get('Content-Security-Policy', '')
        if frame and 'X-Frame-Options' not in h and 'frame-ancestors' not in csp:
            h['X-Frame-Options'] = frame

        # send_file で返すアップロード物（HTML・SVG・XML）は sandbox で開かせる
        if (getattr(resp, 'direct_passthrough', False)
                and (resp.mimetype or '').lower() in _ACTIVE_MIMETYPES
                and 'sandbox' not in csp):
            h['Content-Security-Policy'] = (csp + '; ' if csp else '') + 'sandbox'

        # 500 番台の本文を定型文にする（2026-10-31）
        try:
            resp = mask_server_error(resp)
        except Exception as e:
            app.logger.warning('[security_guard] mask_server_error failed: %s', e)
        return resp

    app.logger.info('[security_guard] installed (mode=%s, mask_5xx=%s)',
                    cfg('SECURITY_GUARD_MODE', 'report'),
                    cfg('SECURITY_MASK_SERVER_ERRORS', True))


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
        # ログはサイトのホーム直下（サイトごとに違うので直書きしない．2026-09-23）
        handler = RotatingFileHandler(os.path.join(os.path.expanduser('~'), 'fujinp.log'),
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

    # ---- カーネルでの一括防御（Blueprint をすべて登録したあとに取り付ける）----
    install_security(app)

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
