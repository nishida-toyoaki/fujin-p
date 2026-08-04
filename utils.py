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

import secrets
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date, time, timedelta
from config import Config
from db import get_db_cursor
from flask_mail import Message
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

### 日時関係

def timedelta_to_time(td):
    """
    timedeltaを 'HH:MM:SS' 形式の文字列に変換
    24時間を超える場合も合計時間として計算します
    """
    if not isinstance(td, timedelta):
        return str(td)
    total_seconds = int(td.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

def format_date(value, format_str='%Y-%m-%d %H:%M:%S'):
    """
    日時をフォーマットし、JSTの文字列として返すことを保証する関数
    データベースの値はすでにJST（時差変換不要）という前提に基づきます
    """
    # 1. Noneチェック（テンプレートでエラーを出さないよう空文字を返すのが安全です）
    if value is None:
        return ""

    # 2. datetime型の場合
    if isinstance(value, datetime):
        return value.strftime(format_str)

    # 3. date型の場合（時刻情報がないためフォーマットに注意）
    if isinstance(value, date):
        return value.strftime(format_str)

    # 4. timedelta型の場合（MySQLのTIME型など）
    if isinstance(value, timedelta):
        # フォーマットが標準的な時分秒指定を含んでいる場合は専用関数を使用
        if '%H:%M:%S' in format_str or format_str == '%Y-%m-%d %H:%M:%S':
            return timedelta_to_time(value)
        # それ以外は単純な文字列化
        return str(value)

    # 5. すでに文字列の場合
    if isinstance(value, str):
        return value

    # 6. その他（数値など）
    return str(value)

###

def is_gmail_address(email):
    """
    Gmailアドレスかどうかを判定

    Args:
        email (str): メールアドレス

    Returns:
        bool: Gmailアドレスの場合True
    """
    return email.lower().endswith('@gmail.com')

def is_google_auth_domain(email):
    """
    Google認証を使用するドメインかどうかを判定
    (@gmail.com)

    Args:
        email (str): メールアドレス

    Returns:
        bool: Google認証ドメインの場合True
    """
    if not email:
        return False
    email_lower = email.lower()
    return email_lower.endswith('@gmail.com')

def generate_random_password(length=12):
    """ランダムパスワード生成"""
    import string
    chars = string.ascii_letters + string.digits + "!@#$%"
    return ''.join(secrets.choice(chars) for _ in range(length))

def hash_password(password):
    """パスワードをハッシュ化（werkzeug: ソルト付き・反復付き）"""
    return generate_password_hash(password)

def verify_password(password, password_hash):
    """パスワードを保存済みハッシュと照合する．
    werkzeug形式のハッシュのみ受け付ける．
    旧方式（ソルト無しSHA-256）のハッシュは形式不正として常に不一致になる．
    """
    if not password_hash:
        return False
    try:
        return check_password_hash(password_hash, password)
    except Exception:
        return False

def generate_reset_token():
    """パスワードリセットトークン生成"""
    return secrets.token_urlsafe(32)

def create_password_reset_token(user_id):
    """パスワードリセットトークンをDBに保存"""
    token = generate_reset_token()
    jst_now = datetime.now(Config.JST)
    expires_at = (jst_now + timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S')
    created_at = jst_now.strftime('%Y-%m-%d %H:%M:%S')

    with get_db_cursor() as (cursor, conn):
        cursor.execute("""
            INSERT INTO password_reset_tokens (user_id, token, expires_at, created_at)
            VALUES (%s, %s, %s, %s)
        """, (user_id, token, expires_at, created_at))
        conn.commit()

    return token

def send_password_reset_email(email, token):
    """パスワードリセットメールを送信"""
    print(f"=== メール送信開始 ===")
    print(f"送信先: {email}")
    print(f"トークン: {token}")

    reset_url = f"{Config.BASE_URL}/reset_password/{token}"

    subject = 'FUJIN-P パスワード設定'

    body = f'''
FUJIN-Pのアカウントが作成されました。

以下のリンクからパスワードを設定してください（24時間有効）：

{reset_url}

このメールに心当たりがない場合は、このメールを無視してください。

---
FUJIN-P管理者
    '''

    try:
        print("メール送信中...")

        # メッセージ作成
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['Subject'] = subject
        msg['From'] = Config.MAIL_USERNAME
        msg['To'] = email

        # メール送信
        with smtplib.SMTP(Config.MAIL_SERVER, Config.MAIL_PORT) as server:
            server.starttls()
            server.login(Config.MAIL_USERNAME, Config.MAIL_PASSWORD)
            server.send_message(msg)

        print(f"メール送信成功: {email}")
        return True
    except Exception as e:
        print(f"メール送信エラー: {str(e)}")
        import traceback
        traceback.print_exc()
        raise

def jst_datetime_filter(value, format='%Y-%m-%d %H:%M:%S'):
    """JSTのdatetimeを文字列に変換するフィルター"""
    if isinstance(value, str):
        return value
    if value is None:
        return ''
    if isinstance(value, datetime):
        return value.strftime(format)
    return str(value)

def jst_date_filter(value, format='%Y-%m-%d'):
    """JSTのdateを文字列に変換するフィルター"""
    return jst_datetime_filter(value, format)

def get_jst_now():
    """現在のJST時刻を取得"""
    return datetime.now(Config.JST)

def get_jst_now_str(format='%Y-%m-%d %H:%M:%S'):
    """現在のJST時刻を文字列で取得"""
    return get_jst_now().strftime(format)

def send_approval_notification_email(email, full_name):
    """
    承認通知メールを送信（Google認証向け）

    Args:
        email (str): 送信先メールアドレス
        full_name (str): ユーザーの氏名

    Returns:
        bool: 送信成功時True
    """
    subject = '【FUJIN-P】アカウント登録申請が承認されました'

    # メール本文（HTML）
    html_body = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <style>
            body {{ font-family: sans-serif; line-height: 1.6; color: #333; }}
            .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
            .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                       color: white; padding: 30px; text-align: center; border-radius: 8px; }}
            .content {{ background: white; padding: 30px; margin-top: 20px;
                       border: 1px solid #e0e0e0; border-radius: 8px; }}
            .button {{ display: inline-block; background: #667eea; color: white;
                      padding: 12px 30px; text-decoration: none; border-radius: 6px;
                      margin: 20px 0; }}
            .notice {{ background: #e8f4fd; padding: 15px; border-left: 4px solid #2196f3;
                      margin: 20px 0; border-radius: 4px; }}
            .footer {{ text-align: center; color: #999; margin-top: 30px; font-size: 0.9em; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="content">
                <h2>{full_name} 様</h2>

                <p>FUJIN-Pへのアカウント登録申請が承認されました。</p>

                <div class="notice">
                    <strong>✓ アカウントの準備が完了しました</strong><br>
                    パスワード設定は不要です。Googleアカウントでそのままログインできます。
                </div>

                <p>以下のリンクからログイン画面にアクセスし、<strong>「Googleでログイン」</strong>ボタンをクリックしてください：</p>

                <p style="text-align: center;">
                    <a href="{Config.BASE_URL}/login" class="button">ログイン画面へ</a>
                </p>

                <p style="background: #f9f9f9; padding: 15px; border-radius: 4px;">
                    <strong>ログイン方法：</strong><br>
                    1. 上記ボタンをクリックしてログイン画面を開く<br>
                    2. 「Googleでログイン」ボタンをクリック<br>
                    3. 登録したGoogleアカウント（{email}）でログイン
                </p>

                <hr style="margin: 30px 0; border: none; border-top: 1px solid #e0e0e0;">

                <p style="color: #666; font-size: 0.9em;">
                    このメールに心当たりがない場合は、お手数ですが管理者までお問い合わせください。
                </p>
            </div>

            <div class="footer">
                <p>FUJIN-P 管理者</p>
            </div>
        </div>
    </body>
    </html>
    """

    # テキスト版（HTMLメール非対応の場合）
    text_body = f"""
{full_name} 様

FUJIN-Pへのアカウント登録申請が承認されました。

【重要】パスワード設定は不要です
アカウントの準備が完了しました。Googleアカウントでそのままログインできます。

■ ログイン方法

1. 以下のURLからログイン画面にアクセス
   {Config.BASE_URL}/login

2. 「Googleでログイン」ボタンをクリック

3. 登録したGoogleアカウント（{email}）でログイン

---
このメールに心当たりがない場合は、お手数ですが管理者までお問い合わせください。

FUJIN-P 管理者
    """

    try:
        # メッセージ作成
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = Config.MAIL_USERNAME
        msg['To'] = email

        # テキスト版とHTML版を添付
        part1 = MIMEText(text_body, 'plain', 'utf-8')
        part2 = MIMEText(html_body, 'html', 'utf-8')
        msg.attach(part1)
        msg.attach(part2)

        # メール送信
        with smtplib.SMTP(Config.MAIL_SERVER, Config.MAIL_PORT) as server:
            server.starttls()
            server.login(Config.MAIL_USERNAME, Config.MAIL_PASSWORD)
            server.send_message(msg)

        print(f"承認通知メールを送信しました: {email}")
        return True

    except Exception as e:
        print(f"承認通知メール送信エラー: {str(e)}")
        import traceback
        traceback.print_exc()
        raise

def send_rejection_notification_email(email, full_name, reason=''):
    """
    不承認通知メールを送信（オプション）

    Args:
        email (str): 送信先メールアドレス
        full_name (str): ユーザーの氏名
        reason (str): 不承認理由（省略可）

    Returns:
        bool: 送信成功時True
    """
    subject = '【FUJIN-P】アカウント登録申請について'

    reason_text = f"\n不承認理由: {reason}\n" if reason else ""

    html_body = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <style>
            body {{ font-family: sans-serif; line-height: 1.6; color: #333; }}
            .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
            .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                       color: white; padding: 30px; text-align: center; border-radius: 8px; }}
            .content {{ background: white; padding: 30px; margin-top: 20px;
                       border: 1px solid #e0e0e0; border-radius: 8px; }}
            .footer {{ text-align: center; color: #999; margin-top: 30px; font-size: 0.9em; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="content">
                <h2>{full_name} 様</h2>

                <p>FUJIN-Pへのアカウント登録申請につきまして、
                   誠に申し訳ございませんが、今回は承認を見送らせていただくこととなりました。</p>

                {f'<p style="background: #f5f5f5; padding: 15px; border-left: 4px solid #999;">{reason}</p>' if reason else ''}

                <p>ご不明な点がございましたら、管理者までお問い合わせください。</p>
            </div>

            <div class="footer">
                <p>FUJIN-P 管理者</p>
            </div>
        </div>
    </body>
    </html>
    """

    text_body = f"""
{full_name} 様

FUJIN-Pへのアカウント登録申請につきまして、
誠に申し訳ございませんが、今回は承認を見送らせていただくこととなりました。

{reason_text}

ご不明な点がございましたら、管理者までお問い合わせください。

FUJIN-P 管理者
    """

    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = Config.MAIL_USERNAME
        msg['To'] = email

        part1 = MIMEText(text_body, 'plain', 'utf-8')
        part2 = MIMEText(html_body, 'html', 'utf-8')
        msg.attach(part1)
        msg.attach(part2)

        with smtplib.SMTP(Config.MAIL_SERVER, Config.MAIL_PORT) as server:
            server.starttls()
            server.login(Config.MAIL_USERNAME, Config.MAIL_PASSWORD)
            server.send_message(msg)

        print(f"不承認通知メールを送信しました: {email}")
        return True

    except Exception as e:
        print(f"不承認通知メール送信エラー: {str(e)}")
        import traceback
        traceback.print_exc()
        raise