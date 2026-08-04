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
import secrets
import json
from datetime import datetime
from config import Config
from db import get_db_cursor
from decorators import login_required
from app import oauth
from utils import create_password_reset_token, send_password_reset_email, hash_password, verify_password


auth_bp = Blueprint('auth', __name__)

def record_user_event(user_id, event_type, event_data=None):
    """ユーザイベントを記録"""
    jst_now = datetime.now(Config.JST).strftime('%Y-%m-%d %H:%M:%S')
    ip_address = request.remote_addr

    if event_data is not None:
        event_data_json = json.dumps(event_data)
    else:
        event_data_json = None

    with get_db_cursor() as (cursor, conn):
        cursor.execute("""
            INSERT INTO user_events (user_id, event_type, event_data, occurred_at, ip_address)
            VALUES (%s, %s, %s, %s, %s)
        """, (user_id, event_type, event_data_json, jst_now, ip_address))
        conn.commit()

def set_user_session(user):
    """ユーザーセッションを設定"""
    session['user_id'] = user['id']
    session['user_email'] = user['email']
    session['user_name'] = user['full_name']
    session['user_category'] = user['category']


def is_blacklisted(email):
    """ブラックリストチェック"""
    with get_db_cursor() as (cursor, conn):
        cursor.execute("""
            SELECT id FROM registration_requests
            WHERE email = %s AND status = 'blacklisted'
        """, (email,))
        return cursor.fetchone() is not None

def create_registration_request(email, full_name, category, affiliation=None):
    """登録申請を作成"""
    jst_now = datetime.now(Config.JST).strftime('%Y-%m-%d %H:%M:%S')
    ip_address = request.remote_addr

    with get_db_cursor() as (cursor, conn):
        cursor.execute("""
            INSERT INTO registration_requests
            (email, full_name, category, affiliation, status, requested_at, ip_address)
            VALUES (%s, %s, %s, %s, 'pending', %s, %s)
        """, (email, full_name, category, affiliation, jst_now, ip_address))
        conn.commit()
        return cursor.lastrowid

def create_user_from_approved(email, full_name, category, affiliation=None):
    """
    承認済み情報からユーザーを作成（パスワードは未設定）
    削除済みユーザーが存在する場合は復活させる
    """
    jst_now = datetime.now(Config.JST).strftime('%Y-%m-%d %H:%M:%S')

    # categoryがNoneまたは空文字列の場合はguestをデフォルトに
    if not category:
        category = 'guest'

    with get_db_cursor() as (cursor, conn):
        # まず、削除済みユーザーが存在するかチェック
        cursor.execute("""
            SELECT id FROM users
            WHERE email = %s AND deleted_at IS NOT NULL
        """, (email,))
        deleted_user = cursor.fetchone()

        if deleted_user:
            # 削除済みユーザーが存在する場合：復活させる
            user_id = deleted_user['id']
            cursor.execute("""
                UPDATE users
                SET full_name = %s,
                    category = %s,
                    affiliation = %s,
                    is_active = TRUE,
                    deleted_at = NULL,
                    deleted_by = NULL,
                    updated_at = %s
                WHERE id = %s
            """, (full_name, category, affiliation, jst_now, user_id))
            conn.commit()
            print(f"削除済みユーザーを復活させました: {email} (ID: {user_id})")
            return user_id
        else:
            # 削除済みユーザーが存在しない場合：新規作成
            cursor.execute("""
                INSERT INTO users (email, full_name, category, affiliation, is_active, created_at, updated_at)
                VALUES (%s, %s, %s, %s, TRUE, %s, %s)
            """, (email, full_name, category, affiliation, jst_now, jst_now))
            conn.commit()
            user_id = cursor.lastrowid
            print(f"新規ユーザーを作成しました: {email} (ID: {user_id})")
            return user_id

###### routes

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    """ログインページ（ユーザー名/パスワード + Google認証）"""
    if request.method == 'POST':
        email = request.form.get('username')
        password = request.form.get('password')

        if not email or not password:
            flash('メールアドレスとパスワードを入力してください', 'error')
            return render_template('login.html')

        with get_db_cursor() as (cursor, conn):
            # 削除されていないアクティブなユーザーのみ
            cursor.execute("""
                SELECT * FROM users
                WHERE email = %s AND is_active = TRUE AND deleted_at IS NULL
            """, (email,))
            user = cursor.fetchone()

            if not user:
                flash('メールアドレスまたはパスワードが正しくありません', 'error')
                return render_template('login.html')

            if not user['password_hash']:
                flash('このアカウントはGoogle認証専用です', 'error')
                return render_template('login.html')

            if not verify_password(password, user['password_hash']):
                flash('メールアドレスまたはパスワードが正しくありません', 'error')
                return render_template('login.html')

            # ログイン成功
            set_user_session(user)
            record_user_event(user['id'], 'login', {'method': 'password'})

            next_url = request.args.get('next') or request.form.get('next')
            if next_url:
                # オープンリダイレクト対策：自ドメインのみ許可
                from urllib.parse import urlparse
                parsed = urlparse(next_url)
                if not parsed.netloc or parsed.netloc == request.host:
                    return redirect(next_url)

            return redirect_to_dashboard()

    return render_template('login.html')

@auth_bp.route('/google_login')
def google_login():
    """Google認証開始"""
    nonce = secrets.token_urlsafe(16)
    session['google_auth_nonce'] = nonce

    # ★ next を保存（OAuthリダイレクト中にURLパラメータが消えるため）
    next_url = request.args.get('next')
    if next_url:
        session['login_next_url'] = next_url

    redirect_uri = url_for('auth.google_callback', _external=True)
    return oauth.google.authorize_redirect(redirect_uri, nonce=nonce)

@auth_bp.route('/google_callback')
def google_callback():
    """Google認証コールバック（改修版：ドメインパターン対応）"""
    try:
        token = oauth.google.authorize_access_token()
        nonce = session.pop('google_auth_nonce', None)
        userinfo = oauth.google.parse_id_token(token, nonce=nonce)

        if not userinfo or not userinfo.get('email'):
            flash('Google認証に失敗しました', 'error')
            return redirect(url_for('auth.login'))

        email = userinfo['email']
        google_name = userinfo.get('name', '')  # Googleアカウントの表示名

        # ブラックリストチェック
        if is_blacklisted(email):
            flash('申し訳ありませんが、このメールアドレスではご利用いただけません。', 'error')
            return redirect(url_for('auth.login'))

        with get_db_cursor() as (cursor, conn):
            # 1. 既存ユーザーチェック（削除されていないユーザーのみ）
            cursor.execute("""
                SELECT * FROM users
                WHERE email = %s AND is_active = TRUE AND deleted_at IS NULL
            """, (email,))
            user = cursor.fetchone()

            if user:
                # 既存ユーザー：ログイン成功
                set_user_session(user)
                record_user_event(user['id'], 'login', {'method': 'google'})

                # ★ next_url があればそちらへ
                next_url = session.pop('login_next_url', None)
                if next_url:
                    from urllib.parse import urlparse
                    parsed = urlparse(next_url)
                    if not parsed.netloc or parsed.netloc == request.host:
                        return redirect(next_url)

                return redirect_to_dashboard()

            # 2. 承認済みユーザーテーブルチェック
            cursor.execute("SELECT * FROM approved_users WHERE email = %s", (email,))
            approved_user = cursor.fetchone()

            if approved_user:
                # 承認済みユーザー：ユーザー作成してログイン画面に誘導（パスワード不要）
                try:
                    user_id = create_user_from_approved(
                        email,
                        approved_user['full_name'],
                        approved_user['category'],
                        approved_user.get('affiliation')
                    )

                    cursor.execute("DELETE FROM approved_users WHERE email = %s", (email,))
                    conn.commit()

                    flash('アカウントが作成されました。Googleアカウントで再度ログインしてください。', 'success')
                    return redirect(url_for('auth.login'))

                except Exception as e:
                    print(f"承認済みユーザー処理エラー: {str(e)}")
                    import traceback
                    traceback.print_exc()
                    flash(f'エラーが発生しました: {str(e)}', 'error')
                    return redirect(url_for('auth.login'))

            # 3. 新規ユーザー：外部登録申請（承認制）へ一本化
            session['registration_email'] = email
            session['registration_google_name'] = google_name  # Google名を保存
            session['registration_method'] = 'google'  # 登録方法を記録

            flash('外部登録申請を行ってください', 'info')
            return redirect(url_for('auth.register_external'))

    except Exception as e:
        print(f"Google認証エラー: {str(e)}")
        import traceback
        traceback.print_exc()
        flash(f'認証エラー: {str(e)}', 'error')
        return redirect(url_for('auth.login'))

@auth_bp.route('/logout', methods=['POST'])
def logout():
    """ログアウト"""
    user_id = session.get('user_id')
    if user_id:
        record_user_event(user_id, 'logout')

    session.clear()
    flash('ログアウトしました', 'success')
    return redirect(url_for('auth.login'))

@auth_bp.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    """パスワードリセット申請（改修版：ドメインパターン対応）"""
    if request.method == 'POST':
        email = request.form.get('email')

        # ブラックリストチェック
        if is_blacklisted(email):
            flash('申し訳ありませんが、このメールアドレスではご利用いただけません。', 'error')
            return render_template('forgot_password.html')

        with get_db_cursor() as (cursor, conn):
            # 1. 既存ユーザーチェック（削除されていないユーザーのみ）
            cursor.execute("""
                SELECT id, password_hash FROM users
                WHERE email = %s AND is_active = TRUE AND deleted_at IS NULL
            """, (email,))
            user = cursor.fetchone()

            if user:
                # 既存ユーザー：通常のパスワードリセット
                try:
                    token = create_password_reset_token(user['id'])
                    send_password_reset_email(email, token)
                    flash('パスワード設定メールを送信しました', 'success')
                    return redirect(url_for('auth.login'))
                except Exception as e:
                    print(f"メール送信エラー: {str(e)}")
                    import traceback
                    traceback.print_exc()
                    flash(f'メール送信エラー: {str(e)}', 'error')
                    return render_template('forgot_password.html')

            # 2. 承認済みユーザーテーブルチェック
            cursor.execute("SELECT * FROM approved_users WHERE email = %s", (email,))
            approved_user = cursor.fetchone()

            if approved_user:
                try:
                    user_id = create_user_from_approved(
                        email,
                        approved_user['full_name'],
                        approved_user['category'],
                        approved_user.get('affiliation')
                    )
                    token = create_password_reset_token(user_id)
                    send_password_reset_email(email, token)

                    cursor.execute("DELETE FROM approved_users WHERE email = %s", (email,))
                    conn.commit()

                    flash('パスワード設定メールを送信しました', 'success')
                    return redirect(url_for('auth.login'))
                except Exception as e:
                    print(f"承認済みユーザー処理エラー: {str(e)}")
                    import traceback
                    traceback.print_exc()
                    flash(f'エラーが発生しました: {str(e)}', 'error')
                    return render_template('forgot_password.html')

            # 3. 新規ユーザー：外部登録申請（承認制）へ一本化
            session['registration_email'] = email
            return redirect(url_for('auth.register_external'))

    # ★ 修正点: GET リクエスト時のreturnが抜けていたため追加
    return render_template('forgot_password.html')

##

@auth_bp.route('/redirect_to_dashboard')
def redirect_to_dashboard():
    """カテゴリに応じたダッシュボードにリダイレクト"""
    category = session.get('user_category')
    if category == 'admin':
        return redirect(url_for('admin.dashboard'))
    else:
        return redirect(url_for('guest.dashboard'))

##

@auth_bp.route('/register_external', methods=['GET', 'POST'])
def register_external():
    """外部申請者登録フォーム（承認待ち）"""
    email = session.get('registration_email')

    if not email:
        flash('不正なアクセスです', 'error')
        return redirect(url_for('auth.forgot_password'))

    if request.method == 'POST':
        full_name = request.form.get('full_name')
        affiliation = request.form.get('affiliation')

        if not full_name or not affiliation:
            flash('すべての項目を入力してください', 'error')
            return render_template('register_external.html', email=email)

        try:
            request_id = create_registration_request(
                email,
                full_name,
                '承認待ち_登録希望者',
                affiliation
            )

            session.pop('registration_email', None)
            session.pop('registration_pattern', None)
            session.pop('registration_user_type', None)
            session.pop('registration_google_name', None)
            session.pop('registration_method', None)

            return render_template('registration_pending.html', email=email)

        except Exception as e:
            print(f"外部登録申請エラー: {str(e)}")
            import traceback
            traceback.print_exc()
            flash(f'エラーが発生しました: {str(e)}', 'error')
            return render_template('register_external.html', email=email)

    return render_template('register_external.html', email=email)

@auth_bp.route('/reset_password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    """パスワードリセット実行"""
    jst_now = datetime.now(Config.JST).strftime('%Y-%m-%d %H:%M:%S')

    with get_db_cursor() as (cursor, conn):
        cursor.execute("""
            SELECT user_id FROM password_reset_tokens
            WHERE token = %s AND expires_at > %s AND used = FALSE
        """, (token, jst_now))
        token_data = cursor.fetchone()

        if not token_data:
            flash('無効または期限切れのリンクです', 'error')
            return redirect(url_for('auth.login'))

        # ユーザーが削除されていないか確認
        cursor.execute("""
            SELECT id FROM users
            WHERE id = %s AND deleted_at IS NULL
        """, (token_data['user_id'],))

        if not cursor.fetchone():
            flash('このユーザーは削除されています', 'error')
            return redirect(url_for('auth.login'))

        if request.method == 'POST':
            password = request.form.get('password')
            password_confirm = request.form.get('password_confirm')

            if password != password_confirm:
                flash('パスワードが一致しません', 'error')
                return render_template('reset_password.html', token=token)

            if len(password) < 8:
                flash('パスワードは8文字以上にしてください', 'error')
                return render_template('reset_password.html', token=token)

            password_hash = hash_password(password)
            cursor.execute("""
                UPDATE users SET password_hash = %s WHERE id = %s
            """, (password_hash, token_data['user_id']))

            cursor.execute("""
                UPDATE password_reset_tokens SET used = TRUE WHERE token = %s
            """, (token,))

            conn.commit()

            flash('パスワードを設定しました。ログインしてください', 'success')
            return redirect(url_for('auth.login'))

    return render_template('reset_password.html', token=token)