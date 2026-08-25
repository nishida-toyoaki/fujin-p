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

from flask import render_template, request, session, flash, redirect, url_for, jsonify
from decorators import admin_required
from auth import redirect_to_dashboard
from db import get_db_cursor
from datetime import datetime
from config import Config
from utils import generate_random_password, hash_password, create_password_reset_token, send_password_reset_email, send_approval_notification_email

from . import admin_bp


# ■ 手順1: ヘルパー関数を追加 ===========================

def _fmt_dt(val, fmt='%Y-%m-%d %H:%M:%S'):
    """datetime→文字列変換（実装ガイド準拠）
    - datetime型 → strftime で文字列化
    - str型      → そのまま返す
    - None       → None を返す
    """
    if val is None:
        return None
    if isinstance(val, str):
        return val
    try:
        return val.strftime(fmt)
    except (AttributeError, ValueError):
        return str(val)


def _fmt_rows(rows, date_fields):
    """dictリスト中の指定カラムをすべて文字列化"""
    for row in rows:
        for field in date_fields:
            if field in row and row[field] is not None:
                row[field] = _fmt_dt(row[field])
    return rows

@admin_bp.route('/dashboard')
@admin_required
def dashboard():
    """FUJIN-P メインダッシュボード（アプリのランチャ画面 admin_dashboard.html）。
    ※フィーチャー機能は廃止。features は空リストを渡す（DBは参照しない）。
    ※ログイン後・「FUJIN-Pダッシュボードに戻る」の遷移先はここ。"""
    statistics = get_registration_statistics()
    # アプリのランチャは正本（fujinp/app_registry.json）から組み立てる．
    # 管理者ダッシュボードでは表示条件（グループ・カテゴリ）を評価しない．
    from fujinp.registry import launcher_sections
    return render_template('admin/admin_dashboard.html',
                        user_name=session.get('user_name'),
                        user_email=session.get('user_email'),
                        site_url=Config.BASE_URL,
                        statistics=statistics,
                        launcher_sections=launcher_sections('admin', 'admin', []),
                        features=[])

@admin_bp.route('/users')
@admin_required
def users():
    """ユーザ一覧(削除済みを除く)。フィーチャー機能は廃止。"""
    search_email = request.args.get('email', '')
    search_name = request.args.get('name', '')
    search_category = request.args.get('category', '')

    with get_db_cursor() as (cursor, conn):
        query = """
            SELECT u.id, u.email, u.full_name, u.category, u.affiliation,
                   u.is_active, u.password_hash, u.created_at
            FROM users u
            WHERE u.deleted_at IS NULL
        """
        params = []

        if search_email:
            query += " AND u.email LIKE %s"
            params.append(f'%{search_email}%')
        if search_name:
            query += " AND u.full_name LIKE %s"
            params.append(f'%{search_name}%')
        if search_category:
            query += " AND u.category = %s"
            params.append(search_category)

        query += " ORDER BY u.created_at DESC"

        cursor.execute(query, params)
        users = cursor.fetchall()
        _fmt_rows(users, ['created_at'])

    return render_template('admin/users.html', users=users)
@admin_bp.route('/deleted_users')
@admin_required
def deleted_users():
    """削除済みユーザー一覧"""
    with get_db_cursor() as (cursor, conn):
        cursor.execute("""
            SELECT u.*, deleter.full_name as deleted_by_name
            FROM users u
            LEFT JOIN users deleter ON u.deleted_by = deleter.id
            WHERE u.deleted_at IS NOT NULL
            ORDER BY u.deleted_at DESC
        """)
        users = cursor.fetchall()

    return render_template('admin/deleted_users.html', users=users)

@admin_bp.route('/features')
@admin_required
def features():
    """（廃止）フィーチャー一覧"""
    flash('フィーチャー機能は廃止されました', 'info')
    return redirect(url_for('admin.unified_dashboard'))

@admin_bp.route('/user/add', methods=['GET', 'POST'])
@admin_required
def add_user():
    """ユーザー追加"""
    if request.method == 'POST':
        email = request.form['email']
        full_name = request.form['full_name']
        category = request.form['category']
        invitation_mode = request.form.get('invitation_mode', 'send_now')
        jst_now = datetime.now(Config.JST).strftime('%Y-%m-%d %H:%M:%S')

        with get_db_cursor() as (cursor, conn):
            try:
                # 事前チェック: メールアドレスの重複確認（削除済みを含む）
                cursor.execute("""
                    SELECT id, deleted_at FROM users WHERE email = %s
                """, (email,))
                existing_user = cursor.fetchone()

                if existing_user:
                    if existing_user['deleted_at']:
                        flash(f'このメールアドレス ({email}) は削除済みユーザーに使用されています。別のメールアドレスを使用してください。', 'error')
                    else:
                        flash(f'このメールアドレス ({email}) は既に登録されています。', 'error')
                    return redirect(url_for('admin.add_user'))

                # Google認証ドメインの判定（Gmail）
                from utils import is_google_auth_domain
                is_google_auth = is_google_auth_domain(email)

                # ランダムパスワードを生成
                random_password = generate_random_password()
                password_hash = hash_password(random_password)

                # Case 2: 後で招待メールを送る場合は、特殊なハッシュ値を設定
                # ただし、Google認証ドメインの場合は通常のハッシュでOK（メール送信不要）
                if invitation_mode == 'send_later' and not is_google_auth:
                    password_hash = 'PENDING_INVITATION'

                cursor.execute("""
                    INSERT INTO users (email, full_name, category, password_hash, created_at, is_active)
                    VALUES (%s, %s, %s, %s, %s, TRUE)
                """, (email, full_name, category, password_hash, jst_now))
                conn.commit()

                # ユーザーIDを取得
                user_id = cursor.lastrowid

                # 招待モードに応じた処理
                if invitation_mode == 'send_now':
                    if is_google_auth:
                        # Google認証: メール送信不要
                        flash(f'ユーザーを追加しました。{email} はGoogleアカウントでログインできます。', 'success')
                    else:
                        # その他: パスワード設定メールを送信
                        try:
                            token = create_password_reset_token(user_id)
                            send_password_reset_email(email, token)
                            flash(f'ユーザーを追加し、パスワード設定メールを送信しました', 'success')
                        except Exception as mail_error:
                            print(f"メール送信エラー: {str(mail_error)}")
                            import traceback
                            traceback.print_exc()
                            flash(f'ユーザーは追加されましたが、メール送信に失敗しました: {str(mail_error)}', 'error')

                elif invitation_mode == 'send_later':
                    if is_google_auth:
                        # Google認証: 後で招待する必要なし
                        flash(f'ユーザーを追加しました。{email} はGoogleアカウントでログインできます。', 'success')
                    else:
                        # その他: 後で招待メールを送る
                        flash(f'ユーザーを追加しました。後で招待メールを送信してください。', 'success')

                else:  # show_password
                    if is_google_auth:
                        # Google認証: パスワード不要
                        flash(f'ユーザーを追加しました。{email} はGoogleアカウントでログインできます。', 'success')
                    else:
                        # その他: ランダムパスワードを表示
                        flash(f'ユーザーを追加しました（初期パスワード: {random_password}）', 'success')

                return redirect(url_for('admin.users'))

            except Exception as e:
                conn.rollback()
                # MySQLのDuplicate entryエラーを判定
                error_msg = str(e)
                if 'Duplicate entry' in error_msg or '1062' in error_msg:
                    flash(f'このメールアドレス ({email}) は既に登録されています。', 'error')
                else:
                    flash(f'ユーザー追加中にエラーが発生しました。管理者に連絡してください。', 'error')
                    print(f"ユーザー追加エラー: {error_msg}")
                    import traceback
                    traceback.print_exc()
                return redirect(url_for('admin.add_user'))

    return render_template('admin/add_user.html')

@admin_bp.route('/user/<int:user_id>/send_invitation', methods=['POST'])
@admin_required
def send_invitation(user_id):
    """招待メールを送信（Case 2用）"""
    with get_db_cursor() as (cursor, conn):
        try:
            # ユーザー情報を取得
            cursor.execute("""
                SELECT * FROM users
                WHERE id = %s AND deleted_at IS NULL
            """, (user_id,))
            user = cursor.fetchone()

            if not user:
                flash('ユーザーが見つかりません', 'error')
                return redirect(url_for('admin.users'))

            # Google認証ドメインの判定
            from utils import is_google_auth_domain
            is_google_auth = is_google_auth_domain(user['email'])

            if is_google_auth:
                # Google認証: メール送信不要
                flash(f'{user["email"]} はGoogleアカウントでログインできます。招待メールは不要です。', 'success')

                # PENDING_INVITATIONの場合は通常のハッシュに更新
                if user['password_hash'] == 'PENDING_INVITATION':
                    random_password = generate_random_password()
                    password_hash = hash_password(random_password)
                    cursor.execute("""
                        UPDATE users
                        SET password_hash = %s
                        WHERE id = %s
                    """, (password_hash, user_id))
                    conn.commit()

                return redirect(url_for('admin.users'))

            # 招待待ちでない場合は送信しない
            if user['password_hash'] != 'PENDING_INVITATION':
                flash('このユーザーは既に招待メールが送信されているか、パスワードが設定されています', 'error')
                return redirect(url_for('admin.users'))

            # 招待メールを送信
            try:
                token = create_password_reset_token(user_id)
                send_password_reset_email(user['email'], token)

                # パスワードハッシュを一時的なものに更新（招待メール送信済みフラグ）
                random_password = generate_random_password()
                password_hash = hash_password(random_password)

                cursor.execute("""
                    UPDATE users
                    SET password_hash = %s
                    WHERE id = %s
                """, (password_hash, user_id))
                conn.commit()

                flash(f'{user["email"]} に招待メールを送信しました', 'success')
            except Exception as mail_error:
                print(f"メール送信エラー: {str(mail_error)}")
                import traceback
                traceback.print_exc()
                flash(f'招待メール送信に失敗しました: {str(mail_error)}', 'error')

        except Exception as e:
            conn.rollback()
            flash(f'エラーが発生しました: {str(e)}', 'error')
            print(f"招待メール送信エラー: {str(e)}")
            import traceback
            traceback.print_exc()

    return redirect(url_for('admin.users'))

@admin_bp.route('/user/edit/<int:user_id>', methods=['GET', 'POST'])
@admin_required
def edit_user(user_id):
    """ユーザー編集"""
    if request.method == 'POST':
        email = request.form['email']
        full_name = request.form['full_name']
        category = request.form['category']
        is_active = request.form.get('is_active', 'off') == 'on'

        with get_db_cursor() as (cursor, conn):
            try:
                cursor.execute("""
                    UPDATE users SET
                    email = %s, full_name = %s, category = %s, is_active = %s
                    WHERE id = %s AND deleted_at IS NULL
                """, (email, full_name, category, is_active, user_id))

                conn.commit()
                flash('ユーザー情報を更新しました', 'success')
                return redirect(url_for('admin.users'))
            except Exception as e:
                conn.rollback()
                flash(f'エラー: {str(e)}', 'error')

    # ユーザー情報取得
    with get_db_cursor() as (cursor, conn):
        cursor.execute("SELECT * FROM users WHERE id = %s AND deleted_at IS NULL", (user_id,))
        user = cursor.fetchone()

        if not user:
            flash('ユーザーが見つかりません', 'error')
            return redirect(url_for('admin.users'))

    return render_template('admin/edit_user.html', user=user)

@admin_bp.route('/user/delete/<int:user_id>', methods=['POST'])
@admin_required
def delete_user(user_id):
    """ユーザー削除（論理削除）"""
    # 自分自身は削除できない
    if user_id == session.get('user_id'):
        flash('自分自身は削除できません', 'error')
        return redirect(url_for('admin.users'))

    admin_id = session.get('user_id')
    jst_now = datetime.now(Config.JST).strftime('%Y-%m-%d %H:%M:%S')

    with get_db_cursor() as (cursor, conn):
        try:
            # 論理削除：deleted_at と deleted_by を設定
            cursor.execute("""
                UPDATE users
                SET deleted_at = %s, deleted_by = %s, is_active = FALSE
                WHERE id = %s AND deleted_at IS NULL
            """, (jst_now, admin_id, user_id))

            if cursor.rowcount > 0:
                conn.commit()
                flash('ユーザーを削除しました', 'success')
            else:
                flash('ユーザーが見つからないか、既に削除されています', 'error')
        except Exception as e:
            conn.rollback()
            flash(f'エラー: {str(e)}', 'error')

    return redirect(url_for('admin.users'))

@admin_bp.route('/user/restore/<int:user_id>', methods=['POST'])
@admin_required
def restore_user(user_id):
    """削除済みユーザーを復元"""
    with get_db_cursor() as (cursor, conn):
        try:
            cursor.execute("""
                UPDATE users
                SET deleted_at = NULL, deleted_by = NULL, is_active = TRUE
                WHERE id = %s AND deleted_at IS NOT NULL
            """, (user_id,))

            if cursor.rowcount > 0:
                conn.commit()
                flash('ユーザーを復元しました', 'success')
            else:
                flash('ユーザーが見つかりません', 'error')
        except Exception as e:
            conn.rollback()
            flash(f'エラー: {str(e)}', 'error')

    return redirect(url_for('admin.deleted_users'))

@admin_bp.route('/user/permanent_delete/<int:user_id>', methods=['POST'])
@admin_required
def permanent_delete_user(user_id):
    """ユーザーを物理削除（データベースから完全削除）"""
    # 自分自身は削除できない
    if user_id == session.get('user_id'):
        flash('自分自身は削除できません', 'error')
        return redirect(url_for('admin.deleted_users'))

    with get_db_cursor() as (cursor, conn):
        try:
            # 削除済みユーザーであることを確認
            cursor.execute("""
                SELECT email, full_name FROM users
                WHERE id = %s AND deleted_at IS NOT NULL
            """, (user_id,))
            user = cursor.fetchone()

            if not user:
                flash('削除済みユーザーが見つかりません', 'error')
                return redirect(url_for('admin.deleted_users'))

            # ★追加：approved_users からも削除
            cursor.execute("""
                DELETE FROM approved_users WHERE email = %s
            """, (user['email'],))

            # 物理削除（CASCADE設定により関連データも削除される）
            cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))

            if cursor.rowcount > 0:
                conn.commit()
                flash(f'{user["full_name"]} ({user["email"]}) をデータベースから完全に削除しました', 'success')
            else:
                flash('削除に失敗しました', 'error')

        except Exception as e:
            conn.rollback()
            flash(f'エラー: {str(e)}', 'error')
            print(f"物理削除エラー: {str(e)}")
            import traceback
            traceback.print_exc()

    return redirect(url_for('admin.deleted_users'))

@admin_bp.route('/user/<int:user_id>/features', methods=['GET', 'POST'])
@admin_required
def manage_user_features(user_id):
    """（廃止）ユーザフィーチャー管理。DBには一切アクセスしない。"""
    flash('フィーチャー機能は廃止されました', 'info')
    return redirect(url_for('admin.unified_dashboard'))

# フィーチャー機能（features / user_features）は廃止。
# 既存リンクからの url_for エラーを避けるためルート名だけ残し、DBには一切アクセスしない。

@admin_bp.route('/feature/add', methods=['GET', 'POST'])
@admin_required
def add_feature():
    """（廃止）フィーチャー追加"""
    flash('フィーチャー機能は廃止されました', 'info')
    return redirect(url_for('admin.unified_dashboard'))

@admin_bp.route('/feature/delete/<int:feature_id>', methods=['POST'])
@admin_required
def delete_feature(feature_id):
    """（廃止）フィーチャー削除"""
    flash('フィーチャー機能は廃止されました', 'info')
    return redirect(url_for('admin.unified_dashboard'))

@admin_bp.route('/feature/toggle/<int:feature_id>', methods=['POST'])
@admin_required
def toggle_feature(feature_id):
    """（廃止）フィーチャー有効/無効切替"""
    flash('フィーチャー機能は廃止されました', 'info')
    return redirect(url_for('admin.unified_dashboard'))

@admin_bp.route('/feature/edit/<int:feature_id>', methods=['POST'])
@admin_required
def edit_feature(feature_id):
    """（廃止）フィーチャー編集"""
    flash('フィーチャー機能は廃止されました', 'info')
    return redirect(url_for('admin.unified_dashboard'))

@admin_bp.route('/feature/<int:feature_id>/update_priority', methods=['POST'])
@admin_required
def update_priority(feature_id):
    """（廃止）フィーチャーpriority更新"""
    flash('フィーチャー機能は廃止されました', 'info')
    return redirect(url_for('admin.unified_dashboard'))

@admin_bp.route('/feature/<int:feature_id>/assign', methods=['GET', 'POST'])
@admin_required
def assign_feature(feature_id):
    """（廃止）フィーチャー一括付与"""
    flash('フィーチャー機能は廃止されました', 'info')
    return redirect(url_for('admin.unified_dashboard'))

@admin_bp.route('/feature/<int:feature_id>/users', methods=['GET'])
@admin_required
def feature_users(feature_id):
    """（廃止）フィーチャー保有ユーザ一覧"""
    flash('フィーチャー機能は廃止されました', 'info')
    return redirect(url_for('admin.unified_dashboard'))

@admin_bp.route('/feature/<int:feature_id>/revoke/<int:user_id>', methods=['POST'])
@admin_required
def revoke_feature(feature_id, user_id):
    """（廃止）フィーチャー剥奪"""
    flash('フィーチャー機能は廃止されました', 'info')
    return redirect(url_for('admin.unified_dashboard'))


# ============================================
# 新規追加：登録申請管理機能
# ============================================

def get_registration_statistics():
    """登録申請の統計情報を取得"""
    with get_db_cursor() as (cursor, conn):
        # 承認待ちの件数
        cursor.execute("SELECT COUNT(*) as count FROM registration_requests WHERE status = 'pending'")
        pending_count = cursor.fetchone()['count']

        # ブラックリストの件数
        cursor.execute("SELECT COUNT(*) as count FROM registration_requests WHERE status = 'blacklisted'")
        blacklisted_count = cursor.fetchone()['count']

        # 承認済み（パスワード未設定）の件数
        cursor.execute("SELECT COUNT(*) as count FROM approved_users")
        approved_count = cursor.fetchone()['count']

        return {
            'pending_count': pending_count,
            'blacklisted_count': blacklisted_count,
            'approved_count': approved_count
        }

# ============================================
# 承認待ちユーザー管理
# ============================================

@admin_bp.route('/pending_requests')
@admin_required
def pending_requests():
    """承認待ちユーザー一覧"""
    with get_db_cursor() as (cursor, conn):
        cursor.execute("""
            SELECT * FROM registration_requests
            WHERE status = 'pending'
            ORDER BY requested_at DESC
        """)
        requests = cursor.fetchall()

    return render_template('admin/pending_requests.html', requests=requests)

@admin_bp.route('/approve_request/<int:request_id>', methods=['POST'])
@admin_required
def approve_request(request_id):
    """登録申請を承認"""
    jst_now = datetime.now(Config.JST).strftime('%Y-%m-%d %H:%M:%S')
    admin_id = session.get('user_id')

    with get_db_cursor() as (cursor, conn):
        cursor.execute("""
            SELECT * FROM registration_requests
            WHERE id = %s AND status = 'pending'
        """, (request_id,))
        req = cursor.fetchone()

        if not req:
            flash('申請が見つかりません', 'error')
            return redirect(url_for('admin.pending_requests'))

        try:
            cursor.execute("""
                INSERT INTO approved_users (email, full_name, category, affiliation, approved_by, approved_at)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (req['email'], req['full_name'], 'guest', req['affiliation'], admin_id, jst_now))

            cursor.execute("""
                UPDATE registration_requests
                SET status = 'approved', processed_at = %s, processed_by = %s
                WHERE id = %s
            """, (jst_now, admin_id, request_id))

            conn.commit()

            # 通知はアドレス種別で分岐する：
            #   Google認証ドメイン → 「Googleでログイン」案内（ユーザは初回Googleログイン時に作成）
            #   それ以外           → ここでユーザを作成し，パスワード設定メールを送る
            from utils import is_google_auth_domain
            if is_google_auth_domain(req['email']):
                send_approval_notification_email(req['email'], req['full_name'])
                flash(f'{req["full_name"]}さんの申請を承認しました', 'success')
            else:
                cursor.execute("""
                    SELECT id, deleted_at FROM users WHERE email = %s
                """, (req['email'],))
                existing = cursor.fetchone()
                if existing and not existing['deleted_at']:
                    user_id = existing['id']
                elif existing and existing['deleted_at']:
                    flash(f'このメールアドレス ({req["email"]}) は削除済みユーザーに使用されています。復元または完全削除を先に行ってください。', 'error')
                    return redirect(url_for('admin.pending_requests'))
                else:
                    password_hash = hash_password(generate_random_password())
                    cursor.execute("""
                        INSERT INTO users (email, full_name, category, affiliation, password_hash, created_at, is_active)
                        VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                    """, (req['email'], req['full_name'], 'guest', req['affiliation'], password_hash, jst_now))
                    conn.commit()
                    user_id = cursor.lastrowid
                try:
                    token = create_password_reset_token(user_id)
                    send_password_reset_email(req['email'], token)
                    flash(f'{req["full_name"]}さんの申請を承認し、パスワード設定メールを送信しました', 'success')
                except Exception as mail_error:
                    print(f"メール送信エラー: {str(mail_error)}")
                    import traceback
                    traceback.print_exc()
                    flash(f'承認しましたが、メール送信に失敗しました: {str(mail_error)}', 'error')

        except Exception as e:
            print(f"承認処理エラー: {str(e)}")
            import traceback
            traceback.print_exc()
            flash(f'エラーが発生しました: {str(e)}', 'error')

    return redirect(url_for('admin.pending_requests'))

@admin_bp.route('/reject_request/<int:request_id>', methods=['POST'])
@admin_required
def reject_request(request_id):
    """登録申請を不承認"""
    jst_now = datetime.now(Config.JST).strftime('%Y-%m-%d %H:%M:%S')
    admin_id = session.get('user_id')
    rejection_reason = request.form.get('rejection_reason', '')

    with get_db_cursor() as (cursor, conn):
        cursor.execute("""
            UPDATE registration_requests
            SET status = 'rejected', processed_at = %s, processed_by = %s, rejection_reason = %s
            WHERE id = %s AND status = 'pending'
        """, (jst_now, admin_id, rejection_reason, request_id))
        conn.commit()

        if cursor.rowcount > 0:
            flash('申請を不承認にしました', 'success')
        else:
            flash('申請が見つかりません', 'error')

    return redirect(url_for('admin.pending_requests'))

@admin_bp.route('/blacklist_request/<int:request_id>', methods=['POST'])
@admin_required
def blacklist_request(request_id):
    """登録申請をブラックリストに登録"""
    jst_now = datetime.now(Config.JST).strftime('%Y-%m-%d %H:%M:%S')
    admin_id = session.get('user_id')

    with get_db_cursor() as (cursor, conn):
        cursor.execute("""
            UPDATE registration_requests
            SET status = 'blacklisted', processed_at = %s, processed_by = %s
            WHERE id = %s
        """, (jst_now, admin_id, request_id))
        conn.commit()

        if cursor.rowcount > 0:
            flash('ブラックリストに登録しました', 'success')
        else:
            flash('申請が見つかりません', 'error')

    return redirect(url_for('admin.pending_requests'))

@admin_bp.route('/blacklisted_users')
@admin_required
def blacklisted_users():
    """ブラックリストユーザー一覧"""
    with get_db_cursor() as (cursor, conn):
        cursor.execute("""
            SELECT rr.*, u.full_name as processor_name
            FROM registration_requests rr
            LEFT JOIN users u ON rr.processed_by = u.id
            WHERE rr.status = 'blacklisted'
            ORDER BY rr.processed_at DESC
        """)
        blacklisted = cursor.fetchall()

    return render_template('admin/blacklisted_users.html', blacklisted=blacklisted)

@admin_bp.route('/remove_from_blacklist/<int:request_id>', methods=['POST'])
@admin_required
def remove_from_blacklist(request_id):
    """ブラックリストから解除"""
    with get_db_cursor() as (cursor, conn):
        cursor.execute("""
            UPDATE registration_requests
            SET status = 'rejected', processed_at = NULL, processed_by = NULL
            WHERE id = %s AND status = 'blacklisted'
        """, (request_id,))
        conn.commit()

        if cursor.rowcount > 0:
            flash('ブラックリストから解除しました', 'success')
        else:
            flash('該当するユーザーが見つかりません', 'error')

    return redirect(url_for('admin.blacklisted_users'))

@admin_bp.route('/approved_users_list')
@admin_required
def approved_users_list():
    """承認済み（パスワード未設定）ユーザー一覧"""
    with get_db_cursor() as (cursor, conn):
        cursor.execute("""
            SELECT au.*, u.full_name as approver_name
            FROM approved_users au
            LEFT JOIN users u ON au.approved_by = u.id
            ORDER BY au.approved_at DESC
        """)
        approved = cursor.fetchall()

    return render_template('admin/approved_users.html', approved=approved)

@admin_bp.route('/delete_approved_user/<int:approved_id>', methods=['POST'])
@admin_required
def delete_approved_user(approved_id):
    """承認済みユーザーを削除"""
    with get_db_cursor() as (cursor, conn):
        cursor.execute("""
            DELETE FROM approved_users WHERE id = %s
        """, (approved_id,))
        conn.commit()

        if cursor.rowcount > 0:
            flash('承認済みユーザーを削除しました', 'success')
        else:
            flash('該当するユーザーが見つかりません', 'error')

    return redirect(url_for('admin.approved_users_list'))

@admin_bp.route('/resend_approval_email/<int:approved_id>', methods=['POST'])
@admin_required
def resend_approval_email(approved_id):
    """承認メールを再送信"""
    with get_db_cursor() as (cursor, conn):
        cursor.execute("""
            SELECT * FROM approved_users WHERE id = %s
        """, (approved_id,))
        approved = cursor.fetchone()

        if not approved:
            flash('該当するユーザーが見つかりません', 'error')
            return redirect(url_for('admin.approved_users_list'))

        try:
            send_approval_notification_email(approved['email'], approved['full_name'])
            flash(f'{approved["email"]} に承認通知メールを再送信しました', 'success')
        except Exception as e:
            print(f"メール再送信エラー: {str(e)}")
            import traceback
            traceback.print_exc()
            flash(f'メール送信エラー: {str(e)}', 'error')

    return redirect(url_for('admin.approved_users_list'))

# フィーチャーラベル取得（廃止）。他モジュールが呼んでも空リストを返す。DBはアクセスしない。
def admin_get_user_feature_labels(user_id):
    """（廃止）フィーチャーラベル。常に空リストを返す。"""
    return []


@admin_bp.route('/api/user/<int:user_id>/feature_labels', methods=['GET'])
@admin_required
def get_user_feature_labels(user_id):
    """（廃止）フィーチャーラベルAPI。常に空を返す。"""
    return jsonify({'success': True, 'user_id': user_id, 'labels': [],
                    'note': 'フィーチャー機能は廃止されました'})


@admin_bp.route('/unified_dashboard')
@admin_required
def unified_dashboard():
    """管理者ダッシュボード（統合版）
    ※フィーチャー機能・アプリ権限機能は廃止。ユーザ管理と申請管理のみ。"""
    tab = request.args.get('tab', 'users')
    if tab in ('features', 'apps'):
        tab = 'users'
    statistics = get_registration_statistics()

    with get_db_cursor() as (cursor, conn):
        # --- ユーザタブ用データ ---
        users_data = _get_users_data(cursor, request.args)

    return render_template('admin/dashboard_unified.html',
                           user_name=session.get('user_name'),
                           user_email=session.get('user_email'),
                           site_url=Config.BASE_URL,
                           statistics=statistics,
                           tab=tab,
                           users=users_data['users'],
                           user_stats=users_data['stats'])


def _get_users_data(cursor, args):
    """ユーザ一覧データを取得（フィーチャー機能は廃止。user_features/features は参照しない）"""
    search_email = args.get('email', '')
    search_name = args.get('name', '')
    search_category = args.get('category', '')

    query = """
        SELECT u.id, u.email, u.full_name, u.category, u.affiliation,
               u.is_active, u.password_hash, u.created_at
        FROM users u
        WHERE u.deleted_at IS NULL
    """
    params = []
    if search_email:
        query += " AND u.email LIKE %s"
        params.append(f'%{search_email}%')
    if search_name:
        query += " AND u.full_name LIKE %s"
        params.append(f'%{search_name}%')
    if search_category:
        query += " AND u.category = %s"
        params.append(search_category)

    query += " ORDER BY u.created_at DESC"
    cursor.execute(query, params)
    users = cursor.fetchall()
    _fmt_rows(users, ['created_at'])

    cursor.execute("SELECT COUNT(*) as total FROM users WHERE deleted_at IS NULL")
    total = cursor.fetchone()['total']

    return {'users': users, 'stats': {'total': total}}

# ============================================
# アプリ管理（新規）
# ============================================

# アプリ管理（apps テーブル）・アプリ権限制御は廃止。
# 既存リンクからの url_for エラーを避けるためルート名だけ残し、DBには一切アクセスしない。

@admin_bp.route('/app/add', methods=['GET', 'POST'])
@admin_required
def add_app():
    """（廃止）アプリ追加"""
    flash('アプリ管理機能は廃止されました', 'info')
    return redirect(url_for('admin.unified_dashboard'))


@admin_bp.route('/app/edit/<int:app_id>', methods=['GET', 'POST'])
@admin_required
def edit_app(app_id):
    """（廃止）アプリ編集"""
    flash('アプリ管理機能は廃止されました', 'info')
    return redirect(url_for('admin.unified_dashboard'))


@admin_bp.route('/app/delete/<int:app_id>', methods=['POST'])
@admin_required
def delete_app(app_id):
    """（廃止）アプリ削除"""
    flash('アプリ管理機能は廃止されました', 'info')
    return redirect(url_for('admin.unified_dashboard'))


@admin_bp.route('/app/<int:app_id>/permissions', methods=['GET', 'POST'])
@admin_required
def manage_app_permissions(app_id):
    """（廃止）アプリ権限マトリクス管理。DBには一切アクセスしない。"""
    flash('アプリ権限制御機能は廃止されました（アプシャからの使用制限は行いません）', 'info')
    return redirect(url_for('admin.unified_dashboard'))


@admin_bp.route('/app/<int:app_id>/add_permission', methods=['POST'])
@admin_required
def add_app_permission_label(app_id):
    """（廃止）アプリ権限ラベル追加。DBには一切アクセスしない。"""
    flash('アプリ権限制御機能は廃止されました', 'info')
    return redirect(url_for('admin.unified_dashboard'))


@admin_bp.route('/app/<int:app_id>/delete_permission/<int:perm_id>', methods=['POST'])
@admin_required
def delete_app_permission_label(app_id, perm_id):
    """（廃止）アプリ権限ラベル削除。DBには一切アクセスしない。"""
    flash('アプリ権限制御機能は廃止されました', 'info')
    return redirect(url_for('admin.unified_dashboard'))


# ============================================
# API: ユーザ権限取得（削除済み）
#   アプリ権限制御の廃止に伴い、2本のAPIを削除した（2026-07-28）。
#   デコレータが無く未認証でアクセスでき、user_id の存在（404/200）と
#   category を返すため、adminのuser_id特定に使えた。
#     - /api/user/<user_id>/app/<app_id>/permissions
#     - /api/user/<user_id>/all_permissions
# ============================================


@admin_bp.route('/return_to_fujin')
@admin_required
def return_to_fujin():
    """FUJIN-Pダッシュボードに戻る"""
    return redirect_to_dashboard()