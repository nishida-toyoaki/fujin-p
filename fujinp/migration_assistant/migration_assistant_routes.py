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

"""
まいあし (MaiAshi) Routes v2 - マルチユーザー・師弟制度対応
FUJIN-Pスタブの段階的構築をガイドするアプリケーション
"""
from flask import render_template, jsonify, request, session
from datetime import datetime, timedelta, timezone
import mysql.connector
# from db import default_db_config
from config import Config
from db import DatabaseConfig, Tables
from auth import redirect_to_dashboard  # ← FUJIN-Pダッシュボードへ戻る用
import os
import logging  # ← 追加
from werkzeug.utils import secure_filename
from markdown_converter import process_markdown

from . import migration_assistant

JST = timezone(timedelta(hours=9), 'JST')

#def get_db_connection():
#    """データベース接続を取得"""
#    return mysql.connector.connect(
#        host=default_db_config['host'],
#        user=default_db_config['user'],
#        password=default_db_config['password'],
#        database=default_db_config['database'],
#        charset='utf8mb4',
#        use_pure=True
#    )
def get_db_connection():
    return mysql.connector.connect(**DatabaseConfig.default())

# UPLOAD_FOLDER = '/home/nishida4fujinp/static/mdimgs'
UPLOAD_FOLDER = Config.UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

# 画像アップロードのサイズ上限（フロントの 10MB チェックと同値。
# フロント側チェックはAPIを直接叩かれると迂回できるため、サーバー側でも見る）
MAX_IMAGE_BYTES = 10 * 1024 * 1024

# course_progress.status の許可値（DBのENUM定義と一致させる）
PROGRESS_STATUSES = ('未着手', '取り組み中', '苦戦', '完了', '放棄')

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# =============================================================================
# ユーティリティ関数
# =============================================================================

def get_now_jst():
    """現在時刻（JST）を取得"""
    return datetime.now(JST)

def is_valid_now(valid_from, valid_until):
    """現在時刻が有効期間内かチェック"""
    now_jst_naive = get_now_jst().replace(tzinfo=None)
    if valid_from and valid_from > now_jst_naive: return False
    if valid_until and valid_until < now_jst_naive: return False
    return True

def check_is_admin(user_id):
    """ユーザーがadminかチェック（簡易版）"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(f"SELECT category FROM {Tables.USERS} WHERE id = %s", (user_id,))
        user = cursor.fetchone()
        cursor.close()
        conn.close()
        return user and user['category'] == 'admin'
    except:
        return False

def require_course_editor(course_id):
    """
    教材編集APIの共通認可チェック。

    ポリシーは mentor_content_editor 画面と同じ「作成者本人、または管理者」。
    画面側だけで権限を見ていると、APIを直接叩かれた場合に他人の教材を
    書き換えられてしまうため、各APIの入口でこの関数を必ず通す。

    戻り値:
        (user_id, None)                 … 認可OK
        (None, (レスポンス, ステータス)) … 認可NG。呼び出し側はそのまま return する
    """
    user_id = session.get('user_id')
    if not user_id:
        return None, (jsonify({'success': False, 'error': 'Unauthorized'}), 401)

    if not course_id:
        return None, (jsonify({'success': False, 'error': 'course_id は必須です'}), 400)

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT creator_user_id FROM courses WHERE id = %s", (course_id,))
        course = cursor.fetchone()
        cursor.close()
    except Exception as e:
        logging.error(f"require_course_editor error: course_id={course_id}, {e}")
        return None, (jsonify({'success': False,
                               'error': '権限の確認中にエラーが発生しました'}), 500)
    finally:
        if conn and conn.is_connected():
            conn.close()

    if not course:
        return None, (jsonify({'success': False,
                               'error': '指定された教材が見つかりません'}), 404)

    if course['creator_user_id'] != user_id and not check_is_admin(user_id):
        logging.warning(
            f"[認可拒否] user_id={user_id} が course_id={course_id} "
            f"(作成者 {course['creator_user_id']}) の編集APIを呼び出しました"
        )
        return None, (jsonify({'success': False,
                               'error': 'この教材の編集権限がありません'}), 403)

    return user_id, None


def require_enrollment_owner(enrollment_id):
    """
    学習者APIの共通認可チェック。

    受講ID（enrollment_id ＝ /api/systems が返す id）が
    ログイン中のユーザー本人のものであることを確認し、対応する course_id を返す。
    これを通さないと、受講IDを差し替えるだけで他人の進捗や
    非公開教材の本文にアクセスできてしまう。

    戻り値:
        (user_id, course_id, None)            … 認可OK
        (None, None, (レスポンス, ステータス)) … 認可NG。呼び出し側はそのまま return する
    """
    user_id = session.get('user_id')
    if not user_id:
        return None, None, (jsonify({'success': False, 'error': 'Unauthorized'}), 401)

    if not enrollment_id:
        return None, None, (jsonify({'success': False,
                                     'error': 'system_id は必須です'}), 400)

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT course_id FROM course_enrollments "
            "WHERE id = %s AND student_user_id = %s",
            (enrollment_id, user_id)
        )
        enrollment = cursor.fetchone()
        cursor.close()
    except Exception as e:
        logging.error(f"require_enrollment_owner error: "
                      f"enrollment_id={enrollment_id}, {e}")
        return None, None, (jsonify({'success': False,
                                     'error': '権限の確認中にエラーが発生しました'}), 500)
    finally:
        if conn and conn.is_connected():
            conn.close()

    if not enrollment:
        # 「他人の受講ID」と「存在しないID」を区別せず404で返す（存在推測を防ぐ）
        logging.warning(
            f"[認可拒否] user_id={user_id} が enrollment_id={enrollment_id} "
            f"にアクセスしました（本人の受講ではありません）"
        )
        return None, None, (jsonify({'success': False,
                                     'error': '受講データが見つかりません'}), 404)

    return user_id, enrollment['course_id'], None


# =============================================================================
# 画面表示（ページ遷移）
# =============================================================================

@migration_assistant.route('/')
@migration_assistant.route('/index')
def index():
    """まいあしメインページ（コース未選択）"""
    user_id = session.get('user_id')
    if not user_id:
        return render_template('error.html', error='ログインが必要です')
    return render_template('migration_assistant_index.html', initial_system_id=None)


@migration_assistant.route('/course/<int:course_id>')
def course_view(course_id):
    """
    学習者用: 特定のコース（教材）を選択した状態で開く。
    あわならの canvas_view と同様、教材ごとに固有URL（course_id基準）を与える。
    URLの course_id から本人の受講（enrollment）を引き当て、その enrollment_id を
    テンプレートへ渡してフロント側で自動選択させる。
    本人が受講していない／存在しないコースの場合はメインページへ戻す。
    """
    user_id = session.get('user_id')
    if not user_id:
        return render_template('error.html', error='ログインが必要です')

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        # URLの course_id に対する、このユーザー本人の受講レコードを取得
        cursor.execute(
            "SELECT id FROM course_enrollments "
            "WHERE course_id = %s AND student_user_id = %s",
            (course_id, user_id)
        )
        enrollment = cursor.fetchone()
        cursor.close()

        if not enrollment:
            # 未受講、または存在しないコース → メインページへ
            return render_template('migration_assistant_index.html', initial_system_id=None)

        # フロントは enrollment_id（＝/api/systems の id）で照合するため、それを渡す
        return render_template('migration_assistant_index.html',
                               initial_system_id=enrollment['id'])
    except Exception as e:
        logging.error(f"course_view error: {e}")
        return render_template('migration_assistant_index.html', initial_system_id=None)
    finally:
        if conn and conn.is_connected():
            conn.close()

@migration_assistant.route('/mentor_dashboard')
def mentor_dashboard():
    """師匠ダッシュボード"""
    user_id = session.get('user_id')
    if not user_id:
        return render_template('error.html', error='ログインが必要です')
    return render_template('migration_assistant_mentor_dashboard.html')

@migration_assistant.route('/mentor_content_editor/<int:course_id>')
def mentor_content_editor(course_id):
    """
    師匠用コンテンツ編集ページ
    ポリシー: 自分が制作したコンテンツであれば、誰でも師匠として編集可能。
    """
    user_id = session.get('user_id')
    if not user_id:
        return render_template('error.html', error='ログインが必要です')

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # 1. 教材の所有者（作成者）を特定する
        cursor.execute("SELECT creator_user_id, course_title FROM courses WHERE id = %s", (course_id,))
        course = cursor.fetchone()

        if not course:
            return render_template('error.html', error='指定された教材が見つかりません')

        # 2. 所有権のチェック
        # 自分が作成したコースであれば、師匠テーブルの有無に関わらず編集を許可する
        is_creator = (course['creator_user_id'] == user_id)

        # (オプション) 管理者であれば他人のコンテンツも編集可能とする場合
        is_admin = check_is_admin(user_id)

        if not is_creator and not is_admin:
            return render_template('error.html', error='この教材の編集権限がありません（作成者本人のみ編集可能です）')

        # 3. 師匠としての記録を（未登録なら）自動で作成、または更新する
        # これにより「コンテンツを書いた＝師匠になった」という事実をDBに刻みます
        now_jst = get_now_jst().replace(tzinfo=None)  # JSTで記録（DBはUTCサーバのためNOW()不可）
        cursor.execute("""
            INSERT INTO migration_assistant_mentors (user_id, approved_by, valid_from, notes)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE valid_from = IFNULL(valid_from, VALUES(valid_from))
        """, (user_id, user_id, now_jst, f"Course ID {course_id} の作成により自動昇格"))

        conn.commit()

        return render_template('migration_assistant_mentor_content_editor.html',
                               course_id=course_id,
                               course_title=course['course_title'])

    except Exception as e:
        logging.error(f"Editor Access Error: {e}")
        return render_template('error.html', error='アクセス権限の確認中にエラーが発生しました')
    finally:
        if conn:
            conn.close()

@migration_assistant.route('/admin_panel')
def admin_panel():
    """管理者パネル（師匠承認）"""
    user_id = session.get('user_id')
    if not user_id or not check_is_admin(user_id):
        return render_template('error.html', error='管理者権限が必要です')
    return render_template('migration_assistant_admin_panel.html')

@migration_assistant.route('/return_to_fujin')
def return_to_fujin():
    """FUJIN-Pダッシュボードに戻る"""
    return redirect_to_dashboard()

# =============================================================================
# 師匠用：コンテンツ管理API (V2 - コースIDベース)
# =============================================================================

def get_course_content_v2(course_id):
    """新テーブル (course_...) から教材の構造を取得 (内部用ヘルパー)"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Phase取得
        cursor.execute("""
            SELECT phase_id, phase_number, phase_title, phase_description
            FROM course_phase_contents WHERE course_id = %s ORDER BY phase_number
        """, (course_id,))
        phases = cursor.fetchall()

        # 【変更】辞書からリストに変更
        content = []
        for phase in phases:
            p_id = phase['phase_id']
            # 各Phaseのデータを構築
            phase_data = {
                'id': p_id,
                'number': phase['phase_number'],
                'title': phase['phase_title'],
                'description': phase['phase_description'],
                'stages': []
            }

            # Stage取得
            cursor.execute("""
                SELECT stage_id, stage_number, stage_title
                FROM course_stage_contents
                WHERE course_id = %s AND phase_id = %s ORDER BY stage_number
            """, (course_id, p_id))
            stages = cursor.fetchall()

            for stage in stages:
                s_id = stage['stage_id']
                stage_data_item = {
                    'id': s_id,
                    'number': stage['stage_number'],
                    'title': stage['stage_title'],
                    'steps': []
                }

                # Step取得
                cursor.execute("""
                    SELECT step_id, step_number, step_title, step_detail
                    FROM course_step_contents
                    WHERE course_id = %s AND phase_id = %s AND stage_id = %s ORDER BY step_number
                """, (course_id, p_id, s_id))
                steps = cursor.fetchall()

                for step in steps:
                    stage_data_item['steps'].append({
                        'id': step['step_id'],
                        'number': step['step_number'],
                        'title': step['step_title'],
                        'detail': step['step_detail'],
                        'display_id': f"{phase['phase_number']}-{stage['stage_number']}-{step['step_number']}"
                    })
                phase_data['stages'].append(stage_data_item)

            # 【重要】リストに追加
            content.append(phase_data)

        cursor.close()
        conn.close()
        return content # リストを返す
    except Exception as e:
        print(f"Error getting course content v2: {e}")
        return []

# =============================================================================
# コンテンツ管理API (新テーブル course_... 専用)
# =============================================================================

# 師匠がエディタを開いた時のデータ取得
@migration_assistant.route('/api/v2/courses/<int:course_id>/content', methods=['GET'])
def get_course_content_api_v2(course_id):
    """新テーブルから教材構造を返す（師匠用。エディタとダッシュボードが使用）"""
    # 認可チェック（作成者本人または管理者のみ。
    # 非公開教材の全ステップ本文を返すため、編集系と同じ基準で保護する）
    _uid, err = require_course_editor(course_id)
    if err:
        return err

    # 既存の get_course_content_v2(course_id) 関数を呼び出し
    content = get_course_content_v2(course_id)
    return jsonify({'success': True, 'content': content})




# =============================================================================
# システム管理API
# =============================================================================

@migration_assistant.route('/api/systems', methods=['GET'])
def get_systems():
    """ユーザーのシステム一覧を取得（新テーブルのみを参照・単純Step集計）"""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # 1. 完了したステップ数：course_progress テーブルで '完了' になっているレコード
        # 2. 全ステップ数：course_step_contents テーブルに登録されているマスターデータ数
        cursor.execute(f"""
            SELECT
                e.id,
                c.course_title as system_name,
                e.enrolled_at as created_at,
                'course' as source_type,
                e.course_id,
                e.mentor_user_id,
                u.full_name as mentor_name,
                -- 完了したステップ数をカウント
                (SELECT COUNT(*) FROM course_progress cp
                 WHERE cp.student_user_id = e.student_user_id
                 AND cp.course_id = e.course_id
                 AND cp.status = '完了') as completed_steps,
                -- 師匠が用意した全ステップ数をカウント
                (SELECT COUNT(*) FROM course_step_contents csc
                 WHERE csc.course_id = e.course_id) as total_steps
            FROM course_enrollments e
            JOIN courses c ON e.course_id = c.id
            LEFT JOIN {Tables.USERS} u ON e.mentor_user_id = u.id
            WHERE e.student_user_id = %s
            ORDER BY e.enrolled_at DESC
        """, (user_id,))

        course_systems = cursor.fetchall()

        for sys in course_systems:
            if sys['created_at']:
                sys['created_at'] = sys['created_at'].strftime('%Y-%m-%d %H:%M')
            if not sys['mentor_name']:
                sys['mentor_name'] = "未設定"

        cursor.close()
        conn.close()
        return jsonify({'success': True, 'systems': course_systems})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500









# =============================================================================
# ユーティリティAPI
# =============================================================================

@migration_assistant.route('/api/check_admin', methods=['GET'])
def check_admin_api():
    """管理者権限チェックAPI"""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'is_admin': False})

    try:
        is_admin = check_is_admin(user_id)
        return jsonify({'is_admin': is_admin})
    except Exception as e:
        print(f"Error checking admin: {e}")
        return jsonify({'is_admin': False})



# =============================================================================
# 師匠ダッシュボードAPI
# =============================================================================

@migration_assistant.route('/api/mentor/students', methods=['GET'])
def get_mentor_students():
    """師匠用: 新テーブルから自分が担当する弟子の一覧と進捗概要を取得"""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # 新テーブル (course_enrollments) を主軸に、コース名と学生名、進捗を集計
        cursor.execute(f"""
            SELECT
                e.id as enrollment_id,
                e.student_user_id,
                u.full_name as student_name,
                e.course_id,
                c.course_title as system_name,
                e.enrolled_at as assigned_at,
                -- 完了したステップ数
                (SELECT COUNT(*) FROM course_progress cp
                 WHERE cp.student_user_id = e.student_user_id
                   AND cp.course_id = e.course_id
                   AND cp.status = '完了') as completed_steps,
                -- コース内の全ステップ数
                (SELECT COUNT(*) FROM course_step_contents csc
                 WHERE csc.course_id = e.course_id) as total_steps,
                -- 最終活動日時
                (SELECT MAX(updated_at) FROM course_progress cp
                 WHERE cp.student_user_id = e.student_user_id
                   AND cp.course_id = e.course_id) as last_activity
            FROM course_enrollments e
            JOIN {Tables.USERS} u ON e.student_user_id = u.id
            JOIN courses c ON e.course_id = c.id
            WHERE e.mentor_user_id = %s
            ORDER BY last_activity DESC, e.enrolled_at DESC
        """, (user_id,))

        students = cursor.fetchall()

        # 日時フォーマットの調整
        for student in students:
            student['assigned_at'] = student['assigned_at'].strftime('%Y-%m-%d %H:%M') if student['assigned_at'] else None
            # 活動がない場合は登録日を表示
            if not student['last_activity']:
                student['last_activity'] = student['assigned_at']
            else:
                student['last_activity'] = student['last_activity'].strftime('%Y-%m-%d %H:%M')

        cursor.close()
        conn.close()

        return jsonify({'success': True, 'students': students})
    except Exception as e:
        print(f"Error getting mentor students: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@migration_assistant.route('/api/mentor/student/<int:student_id>/system/<int:enrollment_id>', methods=['GET'])
def get_student_progress(student_id, enrollment_id):
    """師匠用: 特定の弟子の詳細な進捗マップを取得（新テーブル版）"""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # 権限チェック
        cursor.execute("""
            SELECT course_id FROM course_enrollments
            WHERE id = %s AND mentor_user_id = %s AND student_user_id = %s
        """, (enrollment_id, user_id, student_id))

        enrollment = cursor.fetchone()
        if not enrollment:
            cursor.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Permission denied'}), 403

        # 進捗取得
        cursor.execute("""
            SELECT phase_id, stage_id, step_id, status
            FROM course_progress
            WHERE student_user_id = %s AND course_id = %s
        """, (student_id, enrollment['course_id']))

        progress_records = cursor.fetchall()
        cursor.close()
        conn.close()

        # 【修正】NULL値を扱わず、全て実際の値として処理
        progress = {f"{p['phase_id']}_{p['stage_id']}_{p['step_id']}": p['status']
                    for p in progress_records}

        return jsonify({'success': True, 'progress': progress})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# =============================================================================
# 管理者API（師匠承認）
# =============================================================================

@migration_assistant.route('/api/admin/mentors', methods=['GET'])
def get_admin_mentors():
    """師匠候補一覧を取得（管理者専用）"""
    user_id = session.get('user_id')
    if not user_id or not check_is_admin(user_id):
        return jsonify({'success': False, 'error': 'Permission denied'}), 403

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute(f"""
            SELECT
                m.id,
                m.user_id,
                u.full_name,
                m.approved_by,
                approver.full_name as approved_by_name,
                m.valid_from,
                m.valid_until,
                m.notes,
                m.created_at
            FROM migration_assistant_mentors m
            JOIN {Tables.USERS} u ON m.user_id = u.id
            LEFT JOIN {Tables.USERS} approver ON m.approved_by = approver.id
            ORDER BY m.created_at DESC
        """)

        mentors = cursor.fetchall()

        # 有効期間チェック
        for m in mentors:
            m['is_active'] = is_valid_now(m['valid_from'], m['valid_until'])
            m['valid_from'] = m['valid_from'].strftime('%Y-%m-%d %H:%M') if m['valid_from'] else None
            m['valid_until'] = m['valid_until'].strftime('%Y-%m-%d %H:%M') if m['valid_until'] else None
            m['created_at'] = m['created_at'].strftime('%Y-%m-%d %H:%M') if m['created_at'] else None

        cursor.close()
        conn.close()

        return jsonify({'success': True, 'mentors': mentors})
    except Exception as e:
        print(f"Error getting admin mentors: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@migration_assistant.route('/api/admin/mentors', methods=['POST'])
def approve_mentor():
    """師匠候補を承認（管理者専用）"""
    user_id = session.get('user_id')
    if not user_id or not check_is_admin(user_id):
        return jsonify({'success': False, 'error': 'Permission denied'}), 403

    try:
        data = request.get_json()
        mentor_user_id = data.get('user_id')
        valid_from = data.get('valid_from')
        valid_until = data.get('valid_until')
        notes = data.get('notes', '')

        if not mentor_user_id:
            return jsonify({'success': False, 'error': 'ユーザーIDは必須です'}), 400

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO migration_assistant_mentors (user_id, approved_by, valid_from, valid_until, notes)
            VALUES (%s, %s, %s, %s, %s)
        """, (mentor_user_id, user_id, valid_from, valid_until, notes))

        conn.commit()
        cursor.close()
        conn.close()

        return jsonify({'success': True})
    except mysql.connector.IntegrityError:
        return jsonify({'success': False, 'error': 'このユーザーは既に師匠候補として登録されています'}), 400
    except Exception as e:
        print(f"Error approving mentor: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@migration_assistant.route('/api/admin/mentors/<int:mentor_id>', methods=['DELETE'])
def revoke_mentor(mentor_id):
    """師匠承認を取り消し（管理者専用）"""
    user_id = session.get('user_id')
    if not user_id or not check_is_admin(user_id):
        return jsonify({'success': False, 'error': 'Permission denied'}), 403

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("DELETE FROM migration_assistant_mentors WHERE id = %s", (mentor_id,))

        conn.commit()
        cursor.close()
        conn.close()

        return jsonify({'success': True})
    except Exception as e:
        print(f"Error revoking mentor: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# =============================================================================
# 師匠用コンテンツ管理API（v3.0）
# =============================================================================



@migration_assistant.route('/api/preview', methods=['POST'])
def preview_markdown():
    """Markdownプレビュー"""
    if not session.get('user_id'):
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    data = request.get_json(silent=True) or {}
    markdown_text = data.get('markdown', '')

    if not markdown_text:
        return jsonify({'html': '<p class="text-muted">プレビューがここに表示されます</p>'})

    try:
        # MarkdownをHTMLに変換（シンプルな変換）
        import markdown as md
        html = md.markdown(
            markdown_text,
            extensions=['extra', 'nl2br', 'sane_lists', 'fenced_code', 'tables']
        )
        return jsonify({'html': html})
    except Exception as e:
        print(f"Preview error: {e}")
        import traceback
        traceback.print_exc()
        # エラー時はエスケープして表示
        import html as html_module
        escaped = html_module.escape(markdown_text)
        return jsonify({
            'html': f'<pre style="white-space: pre-wrap; background: #f5f5f5; padding: 15px; border-radius: 4px;">{escaped}</pre>',
            'error': str(e)
        })

# =============================================================================
# 【V2】師匠用コンテンツ管理API（新テーブル専用）
# =============================================================================

@migration_assistant.route('/api/v2/mentor/content/phase', methods=['POST'])
def update_phase_content_v2():
    """Phaseタイトルの更新（新テーブル）"""
    data = request.get_json(silent=True) or {}
    c_id = data.get('course_id')
    p_id = data.get('phase_id')
    title = data.get('phase_title')
    desc = data.get('phase_description', '')

    # 認可チェック（作成者本人または管理者のみ）
    _uid, err = require_course_editor(c_id)
    if err:
        return err

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE course_phase_contents
        SET phase_title = %s, phase_description = %s
        WHERE course_id = %s AND phase_id = %s
    """, (title, desc, c_id, p_id))
    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({'success': True})

@migration_assistant.route('/api/v2/mentor/content/stage', methods=['POST'])
def update_stage_content_v2():
    """Stageタイトルの更新（新テーブル）"""
    data = request.get_json(silent=True) or {}
    c_id = data.get('course_id')
    p_id = data.get('phase_id')
    s_id = data.get('stage_id')
    title = data.get('stage_title')

    # 認可チェック（作成者本人または管理者のみ）
    _uid, err = require_course_editor(c_id)
    if err:
        return err

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE course_stage_contents
        SET stage_title = %s
        WHERE course_id = %s AND phase_id = %s AND stage_id = %s
    """, (title, c_id, p_id, s_id))
    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({'success': True})

@migration_assistant.route('/api/v2/mentor/content/step', methods=['POST'])
def update_step_content_v2():
    data = request.get_json(silent=True) or {}
    c_id = data.get('course_id')
    p_id = data.get('phase_id')
    s_id = data.get('stage_id')
    st_id = data.get('step_id')
    title = data.get('step_title')
    detail = data.get('step_detail')

    # 認可チェック（作成者本人または管理者のみ）
    _uid, err = require_course_editor(c_id)
    if err:
        return err

    logging.info(f"[Step保存開始] course_id={c_id}, phase={p_id}, stage={s_id}, step={st_id}")
    logging.info(f"[Step保存開始] title={title}, detail length={len(detail) if detail else 0}")

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # ★まずレコードが存在するか確認
        cursor.execute("""
            SELECT step_id FROM course_step_contents
            WHERE course_id = %s AND phase_id = %s AND stage_id = %s AND step_id = %s
        """, (c_id, p_id, s_id, st_id))

        existing = cursor.fetchone()

        if not existing:
            logging.error(f"[Step保存エラー] レコードが存在しません")
            cursor.close()
            conn.close()
            return jsonify({'success': False, 'error': 'ステップが見つかりません'}), 404

        # ★UPDATE実行
        if detail is not None:
            cursor.execute("""
                UPDATE course_step_contents
                SET step_title = %s, step_detail = %s
                WHERE course_id = %s AND phase_id = %s AND stage_id = %s AND step_id = %s
            """, (title, detail, c_id, p_id, s_id, st_id))
        else:
            cursor.execute("""
                UPDATE course_step_contents
                SET step_title = %s
                WHERE course_id = %s AND phase_id = %s AND stage_id = %s AND step_id = %s
            """, (title, c_id, p_id, s_id, st_id))

        affected_rows = cursor.rowcount
        logging.info(f"[Step保存] 更新行数: {affected_rows}")

        # ★重要な修正: affected_rows が 0 でも成功とする
        # （レコードが存在することは既に確認済み。値が同じ場合は更新不要）
        if affected_rows == 0:
            logging.info(f"[Step保存] 値に変更がないためスキップしました")

        conn.commit()
        logging.info(f"[Step保存] ✓ コミット完了")

        cursor.close()
        conn.close()

        return jsonify({'success': True})

    except Exception as e:
        logging.error(f"[Step保存エラー] {str(e)}")
        import traceback
        traceback.print_exc()
        if conn:
            try:
                conn.rollback()
            except:
                pass
            try:
                conn.close()
            except:
                pass
        return jsonify({'success': False, 'error': str(e)}), 500

# =============================================================================
# 【V2】コンテンツ構造管理API (新テーブル course_... 専用)
# =============================================================================

# --- Phase追加 (V2) ---
@migration_assistant.route('/api/v2/mentor/content/phase/add', methods=['POST'])
def add_phase_v2():
    data = request.get_json(silent=True) or {}
    c_id = data.get('course_id')
    position = data.get('position', 'after')
    after_num = data.get('after_number', 0)

    # 認可チェック（作成者本人または管理者のみ）
    _uid, err = require_course_editor(c_id)
    if err:
        return err

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # 1. 挿入位置の決定と既存番号の押し上げ
        insert_number = 1 if position == 'first' else after_num + 1
        cursor.execute("""
            UPDATE course_phase_contents
            SET phase_number = phase_number + 1
            WHERE course_id = %s AND phase_number >= %s
        """, (c_id, insert_number))

        # 2. 新規挿入
        new_p_id = f"p_{int(datetime.now().timestamp() * 1000)}"
        cursor.execute("""
            INSERT INTO course_phase_contents (course_id, phase_id, phase_number, phase_title)
            VALUES (%s, %s, %s, %s)
        """, (c_id, new_p_id, insert_number, "新しいPhase"))

        # 3. 連番の強制再整理（念のため）
        cursor.execute("SELECT phase_id FROM course_phase_contents WHERE course_id = %s ORDER BY phase_number", (c_id,))
        phases = cursor.fetchall()
        for idx, p in enumerate(phases, start=1):
            cursor.execute("UPDATE course_phase_contents SET phase_number = %s WHERE course_id = %s AND phase_id = %s", (idx, c_id, p[0]))

        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()

# --- Phase削除 (V2) ---
@migration_assistant.route('/api/v2/mentor/content/phase/delete', methods=['POST'])
def delete_phase_v2():
    data = request.get_json(silent=True) or {}
    c_id, p_id = data.get('course_id'), data.get('phase_id')

    # 認可チェック（作成者本人または管理者のみ）
    _uid, err = require_course_editor(c_id)
    if err:
        return err

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # 1. 下位階層をすべて削除（Step -> Stage -> Phase）
        cursor.execute("DELETE FROM course_step_contents WHERE course_id = %s AND phase_id = %s", (c_id, p_id))
        cursor.execute("DELETE FROM course_stage_contents WHERE course_id = %s AND phase_id = %s", (c_id, p_id))
        cursor.execute("DELETE FROM course_phase_contents WHERE course_id = %s AND phase_id = %s", (c_id, p_id))

        # 2. 残ったPhaseの番号を詰め直す
        cursor.execute("SELECT phase_id FROM course_phase_contents WHERE course_id = %s ORDER BY phase_number", (c_id,))
        phases = cursor.fetchall()
        for idx, p in enumerate(phases, start=1):
            cursor.execute("UPDATE course_phase_contents SET phase_number = %s WHERE course_id = %s AND phase_id = %s", (idx, c_id, p[0]))

        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()

@migration_assistant.route('/api/v2/mentor/content/stage/add', methods=['POST'])
def add_stage_v2():
    """新テーブルへのStage追加（挿入位置指定・連番整理）"""
    data = request.get_json(silent=True) or {}
    c_id = data.get('course_id')
    p_id = data.get('phase_id')
    position = data.get('position', 'after')
    after_number = data.get('after_number', 0)

    # 認可チェック（作成者本人または管理者のみ）
    _uid, err = require_course_editor(c_id)
    if err:
        return err

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # 1. 挿入スペースを確保（指定番号より後ろを+1）
        if position == 'first':
            insert_number = 1
            cursor.execute("UPDATE course_stage_contents SET stage_number = stage_number + 1 WHERE course_id = %s AND phase_id = %s", (c_id, p_id))
        else: # after
            insert_number = after_number + 1
            cursor.execute("UPDATE course_stage_contents SET stage_number = stage_number + 1 WHERE course_id = %s AND phase_id = %s AND stage_number >= %s", (c_id, p_id, insert_number))

        # 2. 新規挿入
        new_s_id = f"s_{int(datetime.now().timestamp() * 1000)}"
        cursor.execute("""
            INSERT INTO course_stage_contents (course_id, phase_id, stage_id, stage_number, stage_title)
            VALUES (%s, %s, %s, %s, %s)
        """, (c_id, p_id, new_s_id, insert_number, "新しいStage"))

        # 3. 番号を綺麗に振り直し（データの整合性を保証）
        cursor.execute("SELECT stage_id FROM course_stage_contents WHERE course_id = %s AND phase_id = %s ORDER BY stage_number", (c_id, p_id))
        stages = cursor.fetchall()
        for idx, stage in enumerate(stages, start=1):
            cursor.execute("UPDATE course_stage_contents SET stage_number = %s WHERE course_id = %s AND stage_id = %s", (idx, c_id, stage[0]))

        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@migration_assistant.route('/api/v2/mentor/content/stage/delete', methods=['POST'])
def delete_stage_v2():
    """新テーブルからのStage削除（関連Step削除・連番整理）"""
    data = request.get_json(silent=True) or {}
    c_id = data.get('course_id')
    p_id = data.get('phase_id')
    s_id = data.get('stage_id')

    # 認可チェック（作成者本人または管理者のみ）
    _uid, err = require_course_editor(c_id)
    if err:
        return err

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # 1. 関連するStepを削除
        cursor.execute("DELETE FROM course_step_contents WHERE course_id = %s AND phase_id = %s AND stage_id = %s", (c_id, p_id, s_id))

        # 2. Stage本体を削除
        cursor.execute("DELETE FROM course_stage_contents WHERE course_id = %s AND phase_id = %s AND stage_id = %s", (c_id, p_id, s_id))

        # 3. 削除後の番号詰め
        cursor.execute("SELECT stage_id FROM course_stage_contents WHERE course_id = %s AND phase_id = %s ORDER BY stage_number", (c_id, p_id))
        stages = cursor.fetchall()
        for idx, stage in enumerate(stages, start=1):
            cursor.execute("UPDATE course_stage_contents SET stage_number = %s WHERE course_id = %s AND stage_id = %s", (idx, c_id, stage[0]))

        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# --- Step追加 (V2) ---
@migration_assistant.route('/api/v2/mentor/content/step/add', methods=['POST'])
def add_step_v2():
    data = request.get_json(silent=True) or {}
    c_id, p_id, s_id = data.get('course_id'), data.get('phase_id'), data.get('stage_id')
    position = data.get('position', 'after')
    after_num = data.get('after_number', 0)

    # 認可チェック（作成者本人または管理者のみ）
    _uid, err = require_course_editor(c_id)
    if err:
        return err

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # 1. 挿入スペース確保
        insert_number = 1 if position == 'first' else after_num + 1
        cursor.execute("""
            UPDATE course_step_contents
            SET step_number = step_number + 1
            WHERE course_id = %s AND phase_id = %s AND stage_id = %s AND step_number >= %s
        """, (c_id, p_id, s_id, insert_number))

        # 2. 新規挿入
        new_st_id = f"st_{int(datetime.now().timestamp() * 1000)}"
        cursor.execute("""
            INSERT INTO course_step_contents (course_id, phase_id, stage_id, step_id, step_number, step_title, step_detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (c_id, p_id, s_id, new_st_id, insert_number, "新しいStep", ""))

        # 3. 番号振り直し
        cursor.execute("""
            SELECT step_id FROM course_step_contents
            WHERE course_id = %s AND phase_id = %s AND stage_id = %s
            ORDER BY step_number
        """, (c_id, p_id, s_id))
        steps = cursor.fetchall()
        for idx, st in enumerate(steps, start=1):
            cursor.execute("UPDATE course_step_contents SET step_number = %s WHERE course_id = %s AND step_id = %s", (idx, c_id, st[0]))

        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()

# --- Step削除 (V2) ---
# ※以前の回答で作成済みですが、番号振り直しをより確実にした版です
@migration_assistant.route('/api/v2/mentor/content/step/delete', methods=['POST'])
def delete_step_v2():
    data = request.get_json(silent=True) or {}
    c_id, p_id, s_id, st_id = (data.get('course_id'), data.get('phase_id'),
                               data.get('stage_id'), data.get('step_id'))

    # 認可チェック（作成者本人または管理者のみ）
    _uid, err = require_course_editor(c_id)
    if err:
        return err

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # 1. 削除実行
        cursor.execute("DELETE FROM course_step_contents WHERE course_id = %s AND step_id = %s", (c_id, st_id))

        # 2. 番号を詰め直す
        cursor.execute("""
            SELECT step_id FROM course_step_contents
            WHERE course_id = %s AND phase_id = %s AND stage_id = %s
            ORDER BY step_number
        """, (c_id, p_id, s_id))
        steps = cursor.fetchall()
        for idx, st in enumerate(steps, start=1):
            cursor.execute("UPDATE course_step_contents SET step_number = %s WHERE course_id = %s AND step_id = %s", (idx, c_id, st[0]))

        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()


# =============================================================================
# 【V2】弟子用：コンテンツ・詳細取得（新テーブル専用）
# =============================================================================

@migration_assistant.route('/api/student/content', methods=['GET'])
def get_student_content():
    """弟子用: 受講ID(system_id)からコースIDを特定し、新テーブルから構造を返す"""
    user_id = session.get('user_id')
    enrollment_id = request.args.get('system_id', type=int)

    if not user_id or not enrollment_id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # 1. 受講情報から course_id と mentor_user_id を取得
        # 所有者（student_user_id）のチェックも同時に行う
        cursor.execute("""
            SELECT course_id, mentor_user_id
            FROM course_enrollments
            WHERE id = %s AND student_user_id = %s
        """, (enrollment_id, user_id))
        enrollment = cursor.fetchone()

        if not enrollment:
            return jsonify({'success': False, 'error': 'not_found', 'message': '受講データが見つかりません'})

        # 2. 師匠が未指名の場合、フロントエンドに 'no_mentor' を通知する
        # これにより、JS側の showNoMentorMessage() が発火します
        if enrollment['mentor_user_id'] is None:
            return jsonify({
                'success': False,
                'error': 'no_mentor',
                'message': 'この教材を進めるには師匠の指名が必要です'
            })

        # 3. 師匠が指名されていれば、新テーブル(course_...)から構造を取得
        content = get_course_content_v2(enrollment['course_id'])

        cursor.close()
        return jsonify({'success': True, 'content': content})

    except Exception as e:
        logging.error(f"Error in get_student_content: {str(e)}")
        return jsonify({'success': False, 'error': 'server_error', 'message': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            conn.close()

@migration_assistant.route('/api/student/step_detail/<phase_id>/<stage_id>/<step_id>', methods=['GET'])
def get_student_step_detail(phase_id, stage_id, step_id):
    """弟子用: 受講IDからコースIDを特定し、新テーブルからステップ詳細(HTML)を取得"""
    enrollment_id = request.args.get('system_id', type=int)

    # 認可チェック（本人の受講のみ。他人の受講IDでは非公開教材の本文が読めてしまう）
    _uid, course_id, err = require_enrollment_owner(enrollment_id)
    if err:
        return err

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # 新テーブル (course_step_contents) からデータを取得
        cursor.execute("""
            SELECT step_title, step_detail FROM course_step_contents
            WHERE course_id = %s AND phase_id = %s AND stage_id = %s AND step_id = %s
        """, (course_id, phase_id, stage_id, step_id))

        step = cursor.fetchone()
        cursor.close()

        detail_html = (process_markdown(step['step_detail'])
                       if step and step['step_detail'] else '詳細情報がありません。')
        return jsonify({'success': True,
                        'step_title': step['step_title'] if step else "無題",
                        'step_detail': detail_html})
    except Exception as e:
        logging.error(f"Error getting step detail: enrollment_id={enrollment_id}, {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            conn.close()

# =============================================================================
# 進捗管理API
# =============================================================================

@migration_assistant.route('/api/student/progress', methods=['GET'])
def get_student_progress_v2():
    """弟子用: 新テーブル(course_progress)から進捗を取得"""
    enrollment_id = request.args.get('system_id', type=int)

    # 認可チェック（本人の受講のみ）
    user_id, course_id, err = require_enrollment_owner(enrollment_id)
    if err:
        return err

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # 新テーブルから進捗をロード
        cursor.execute(
            "SELECT phase_id, stage_id, step_id, status FROM course_progress "
            "WHERE student_user_id = %s AND course_id = %s",
            (user_id, course_id)
        )

        progress_list = cursor.fetchall()
        cursor.close()

        progress = {f"{p['phase_id']}_{p['stage_id']}_{p['step_id']}": p['status']
                    for p in progress_list}
        return jsonify({'success': True, 'progress': progress})
    except Exception as e:
        logging.error(f"Error getting student progress: enrollment_id={enrollment_id}, {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            conn.close()

@migration_assistant.route('/api/student/progress', methods=['POST'])
def update_student_progress_v2():
    """弟子用: 新テーブル(course_progress)へ進捗をUPSERT"""
    data = request.get_json(silent=True) or {}
    enrollment_id = data.get('system_id')

    # 認可チェック（本人の受講のみ。これが無いと他人の進捗を書き換えられる）
    user_id, c_id, err = require_enrollment_owner(enrollment_id)
    if err:
        return err

    phase_id = data.get('phase_id')
    stage_id = data.get('stage_id')
    step_id = data.get('step_id')
    status = data.get('status')

    if not all([phase_id, stage_id, step_id, status]):
        return jsonify({'success': False,
                        'error': 'phase_id / stage_id / step_id / status は必須です'}), 400

    if status not in PROGRESS_STATUSES:
        return jsonify({'success': False,
                        'error': f"status は {'/'.join(PROGRESS_STATUSES)} のいずれかです"}), 400

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # UPSERT (なければ挿入、あれば更新)
        now_jst = get_now_jst().replace(tzinfo=None)  # JSTで記録（DBはUTCサーバのためNOW()不可）
        cursor.execute("""
            INSERT INTO course_progress (student_user_id, course_id, phase_id, stage_id, step_id, status, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE status = VALUES(status), updated_at = VALUES(updated_at)
        """, (user_id, c_id, phase_id, stage_id, step_id, status, now_jst))

        conn.commit()
        cursor.close()
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"Error updating student progress: enrollment_id={enrollment_id}, {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            conn.close()


@migration_assistant.route('/api/v2/mentor/content/step/detail', methods=['GET'])
def get_mentor_step_detail_v2():
    """師匠用: エディタモーダルを開く際に、生のMarkdownを取得する"""
    course_id = request.args.get('course_id', type=int)
    phase_id = request.args.get('phase_id')
    stage_id = request.args.get('stage_id')
    step_id = request.args.get('step_id')

    if not all([course_id, phase_id, stage_id, step_id]):
        return jsonify({'success': False, 'error': 'Missing parameters'}), 400

    # 認可チェック（生Markdownを返すため、編集系と同じ基準で保護する）
    _uid, err = require_course_editor(course_id)
    if err:
        return err

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # 【重要】course_step_contents (V2) から生の Markdown を取得
    cursor.execute("""
        SELECT step_title, step_detail
        FROM course_step_contents
        WHERE course_id = %s AND phase_id = %s AND stage_id = %s AND step_id = %s
    """, (course_id, phase_id, stage_id, step_id))

    step = cursor.fetchone()
    conn.close()

    if step:
        return jsonify({
            'success': True,
            'step_title': step['step_title'],
            'step_detail': step['step_detail'] # 生のMarkdownを返す
        })
    else:
        return jsonify({'success': False, 'error': 'Step not found'})


# =============================================================================
# 【V2】師匠用：進捗集計（新テーブル専用）
# =============================================================================

@migration_assistant.route('/api/v2/courses/<int:course_id>/progress_by_step', methods=['GET'])
def get_mentor_progress_by_step_v2(course_id):
    """師匠用: 特定コースのステップごとの進捗を新テーブルから集計"""
    # 認可チェック（作成者本人または管理者のみ。
    # 他人の教材の受講者の進捗分布が読めてしまうため）
    _uid, err = require_course_editor(course_id)
    if err:
        return err

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT phase_id, stage_id, step_id, status, COUNT(*) as count
            FROM course_progress WHERE course_id = %s
            GROUP BY phase_id, stage_id, step_id, status
        """, (course_id,))
        results = cursor.fetchall()
        cursor.close()

        by_step = {}
        for r in results:
            key = f"{r['phase_id']}_{r['stage_id']}_{r['step_id']}"
            if key not in by_step:
                by_step[key] = {s: 0 for s in PROGRESS_STATUSES}
            # 想定外のstatus（NULL等）でKeyErrorにならないようにする
            if r['status'] in by_step[key]:
                by_step[key][r['status']] = r['count']
        return jsonify({'success': True, 'by_step': by_step})
    except Exception as e:
        logging.error(f"Error getting progress by step: course_id={course_id}, {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            conn.close()

# =============================================================================
# 画像アップロードAPI
# =============================================================================

@migration_assistant.route('/api/upload_image', methods=['POST'])  # ← migration_assistant_bp → migration_assistant に修正
def upload_image():
    """画像アップロード"""
    # 認可チェック（未ログインでの無制限アップロードを防ぐ）
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'ファイルが選択されていません'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'ファイルが選択されていません'}), 400

    if not allowed_file(file.filename):
        return jsonify({'success': False, 'error': '許可されていないファイル形式です'}), 400

    # サイズ上限（フロントの10MBチェックはAPI直叩きで迂回できるためサーバー側でも確認）
    file.stream.seek(0, os.SEEK_END)
    size = file.stream.tell()
    file.stream.seek(0)
    if size > MAX_IMAGE_BYTES:
        logging.warning(f"[アップロード拒否] user_id={user_id} size={size} "
                        f"> {MAX_IMAGE_BYTES}")
        return jsonify({
            'success': False,
            'error': f'ファイルサイズは{MAX_IMAGE_BYTES // (1024 * 1024)}MB以下にしてください'
        }), 400
    if size == 0:
        return jsonify({'success': False, 'error': '空のファイルです'}), 400

    try:
        # ディレクトリ作成
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)

        # ファイル名生成（タイムスタンプ付き）
        timestamp = datetime.now(JST).strftime('%Y%m%d_%H%M%S')  # ← JSTタイムゾーンを使用
        filename = secure_filename(file.filename)
        name, ext = os.path.splitext(filename)
        unique_filename = f"{name}_{timestamp}{ext}"

        filepath = os.path.join(UPLOAD_FOLDER, unique_filename)
        file.save(filepath)

        # URL生成
        url = f"/static/mdimgs/{unique_filename}"

        return jsonify({'success': True, 'filename': unique_filename, 'url': url})

    except Exception as e:
        logging.error(f"画像アップロードエラー: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@migration_assistant.route('/admin_migrationNG')
def admin_migrationNG():
    """管理者用: データマイグレーション管理ページ"""
    user_id = session.get('user_id')
    if not user_id or not check_is_admin(user_id):
        return "❌ 管理者権限が必要です", 403
    return render_template('migration_assistant_admin_migration.html')


@migration_assistant.route('/api/admin/migrate_to_coursesNG', methods=['POST'])
def migrate_to_coursesNG():
    """旧システムのデータを新テーブルに転写（完全にリセットしてやり直す）"""
    now_jst = get_now_jst()
    user_id = session.get('user_id')
    if not user_id or not check_is_admin(user_id):
        return jsonify({'success': False, 'error': 'Permission denied'}), 403

    log = []
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        log.append('[INFO] データマイグレーション開始（リセットモード）')

        # --- ここから TRUNCATE (リセット処理) ---
        cursor.execute("SET FOREIGN_KEY_CHECKS = 0") # 外部キー制約を一時的に無効化

        tables_to_reset = [
            "course_progress",
            "course_enrollments",
            "course_step_contents",
            "course_stage_contents",
            "course_phase_contents",
            "courses"
        ]

        for table in tables_to_reset:
            cursor.execute(f"TRUNCATE TABLE {table}")
            log.append(f'  → {table} をリセットしました')

        cursor.execute("SET FOREIGN_KEY_CHECKS = 1") # 制約を元に戻す
        # ---------------------------------------

        # 1. 既存の全師匠を取得
        cursor.execute("SELECT DISTINCT mentor_user_id FROM migration_assistant_phase_contents")
        mentors = cursor.fetchall()

        for mentor in mentors:
            mentor_user_id = mentor['mentor_user_id']
            # ここからは新規作成のみを行う（TRUNCATEしたので既存チェックは不要）
            cursor.execute("""
                INSERT INTO courses (creator_user_id, course_title, course_description, is_public, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (mentor_user_id, f"デフォルトコース（師匠ID: {mentor_user_id}）", "旧システムから移行されたコース", True, now_jst, now_jst))

            course_id = cursor.lastrowid
            log.append(f'[SUCCESS] 師匠ID {mentor_user_id} 用にコースID {course_id} を新規作成しました')

            # 3. Phase データを転写
            cursor.execute("""
                SELECT * FROM migration_assistant_phase_contents
                WHERE mentor_user_id = %s
                ORDER BY phase_number
            """, (mentor_user_id,))
            phases = cursor.fetchall()

            for phase in phases:
                cursor.execute("""
                    INSERT INTO course_phase_contents
                    (course_id, phase_id, phase_number, phase_title, phase_description)
                    VALUES (%s, %s, %s, %s, %s)
                """, (
                    course_id,
                    phase['phase_id'],
                    phase['phase_number'],
                    phase['phase_title'],
                    phase['phase_description']
                ))

            log.append(f'  → Phase: {len(phases)}件')

            # 4. Stage データを転写
            cursor.execute("""
                SELECT * FROM migration_assistant_stage_contents
                WHERE mentor_user_id = %s
                ORDER BY phase_id, stage_number
            """, (mentor_user_id,))
            stages = cursor.fetchall()

            for stage in stages:
                cursor.execute("""
                    INSERT INTO course_stage_contents
                    (course_id, phase_id, stage_id, stage_number, stage_title)
                    VALUES (%s, %s, %s, %s, %s)
                """, (
                    course_id,
                    stage['phase_id'],
                    stage['stage_id'],
                    stage['stage_number'],
                    stage['stage_title']
                ))

            log.append(f'  → Stage: {len(stages)}件')

            # 5. Step データを転写
            cursor.execute("""
                SELECT * FROM migration_assistant_step_contents
                WHERE mentor_user_id = %s
                ORDER BY phase_id, stage_id, step_number
            """, (mentor_user_id,))
            steps = cursor.fetchall()

            for step in steps:
                cursor.execute("""
                    INSERT INTO course_step_contents
                    (course_id, phase_id, stage_id, step_id, step_number, step_title, step_detail)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (
                    course_id,
                    step['phase_id'],
                    step['stage_id'],
                    step['step_id'],
                    step['step_number'],
                    step['step_title'],
                    step['step_detail']
                ))

            log.append(f'  → Step: {len(steps)}件')

            # 6. 受講関係を転写
            cursor.execute("""
                SELECT * FROM migration_assistant_mentor_assignments
                WHERE mentor_user_id = %s
            """, (mentor_user_id,))
            enrollments = cursor.fetchall()

            for enrollment in enrollments:
                cursor.execute("""
                    INSERT INTO course_enrollments
                    (student_user_id, course_id, mentor_user_id)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE course_id = course_id
                """, (
                    enrollment['student_user_id'],
                    course_id,
                    mentor_user_id
                ))

            log.append(f'  → 受講関係: {len(enrollments)}件')

            # 7. 進捗データを転写
            cursor.execute("""
                SELECT * FROM migration_assistant_progress
                WHERE mentor_user_id = %s
            """, (mentor_user_id,))
            progress_records = cursor.fetchall()

            for progress in progress_records:
                # ★修正箇所: updated_at に NOW() ではなく now_jst を使う
                cursor.execute("""
                    INSERT INTO course_progress
                    (student_user_id, course_id, phase_id, stage_id, step_id, status, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE status = VALUES(status), updated_at = VALUES(updated_at)
                """, (
                    progress['student_user_id'],
                    course_id,
                    progress['phase_id'],
                    progress['stage_id'],
                    progress['step_id'],
                    progress['status'],
                    now_jst # updated_at
                ))

            log.append(f'  → 進捗: {len(progress_records)}件')

        conn.commit()
        log.append('[SUCCESS] 全データの転写が完了しました')

        cursor.close()
        conn.close()

        return jsonify({'success': True, 'log': log})

    except Exception as e:
        logging.error(f"Migration error: {e}")
        import traceback
        traceback.print_exc()
        log.append(f'[ERROR] {str(e)}')
        return jsonify({'success': False, 'error': str(e), 'log': log}), 500

# =============================================================================
# コース閲覧・受講申し込みAPI（新システム）
# =============================================================================

@migration_assistant.route('/api/v2/courses/public', methods=['GET'])
def get_public_courses_v2():
    """公開されているコース一覧を取得（新テーブル版）"""
    if not session.get('user_id'):
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute(f"""
            SELECT
                c.id,
                c.course_title,
                c.course_description,
                c.creator_user_id,
                u.full_name as creator_name,
                c.created_at,
                c.updated_at,
                (SELECT COUNT(*) FROM course_phase_contents WHERE course_id = c.id) as phase_count
            FROM courses c
            JOIN {Tables.USERS} u ON c.creator_user_id = u.id
            WHERE c.is_public = TRUE
            ORDER BY c.updated_at DESC
        """)

        courses = cursor.fetchall()

        # 日時フォーマット
        for course in courses:
            if course['created_at']:
                course['created_at'] = course['created_at'].isoformat()
            if course['updated_at']:
                course['updated_at'] = course['updated_at'].isoformat()

        cursor.close()
        conn.close()

        return jsonify({'success': True, 'courses': courses})

    except Exception as e:
        logging.error(f"Error getting public courses: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@migration_assistant.route('/api/v2/enrollments/enroll', methods=['POST'])
def enroll_in_course():
    """
    受講登録。教材の作成者（creator_user_id）を師匠（mentor_user_id）として記録する。

    エラー文字列は index.html 側の日本語化テーブルに合わせている:
      'Unauthorized'    → ログインが必要です
      'Course not found'→ 指定された教材が見つかりません
      'Already enrolled'→ 現在受講中科目の重複履修はできません
    """
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    data = request.get_json(silent=True) or {}
    course_id = data.get('course_id')
    if not course_id:
        return jsonify({'success': False, 'error': 'course_id は必須です'}), 400

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # 1. 教材の作成者を取得
        cursor.execute("SELECT creator_user_id FROM courses WHERE id = %s", (course_id,))
        course = cursor.fetchone()

        if not course:
            cursor.close()
            return jsonify({'success': False, 'error': 'Course not found'}), 404

        # 2. 重複受講の防止
        #    course_enrollments には UNIQUE(student_user_id, course_id) が無いため、
        #    INSERT の一意制約違反には頼れない。ここで明示的に既存受講を確認する。
        #    （DB側にも一意キーを追加することを推奨。add_unique_enrollment.sql 参照）
        cursor.execute("""
            SELECT id FROM course_enrollments
            WHERE student_user_id = %s AND course_id = %s
        """, (user_id, course_id))
        if cursor.fetchone():
            cursor.close()
            return jsonify({'success': False, 'error': 'Already enrolled'}), 400

        # 3. 受講登録時に著者（creator_user_id）を師匠（mentor_user_id）として保存
        now_jst = get_now_jst().replace(tzinfo=None)  # JSTで記録（DBはUTCサーバのためデフォルト不可）
        cursor.execute("""
            INSERT INTO course_enrollments (student_user_id, course_id, mentor_user_id, enrolled_at)
            VALUES (%s, %s, %s, %s)
        """, (user_id, course_id, course['creator_user_id'], now_jst))

        conn.commit()
        cursor.close()
        return jsonify({'success': True})

    except mysql.connector.IntegrityError:
        # DB側に一意キーを追加した場合の競合（上記チェックとINSERTの間に
        # 別リクエストが割り込んだケース）を拾う保険
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return jsonify({'success': False, 'error': 'Already enrolled'}), 400
    except Exception as e:
        logging.error(f"Error enrolling in course: course_id={course_id}, {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            conn.close()

@migration_assistant.route('/api/v2/enrollments/<int:enrollment_id>', methods=['DELETE'])
def delete_enrollment(enrollment_id):
    """受講を取り消す（新テーブル版）"""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # 自分の受講かチェック
        cursor.execute("""
            SELECT id, course_id FROM course_enrollments
            WHERE id = %s AND student_user_id = %s
        """, (enrollment_id, user_id))

        enrollment = cursor.fetchone()
        if not enrollment:
            cursor.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Enrollment not found'}), 404

        course_id = enrollment['course_id']

        # 進捗データを削除
        cursor.execute("""
            DELETE FROM course_progress
            WHERE student_user_id = %s AND course_id = %s
        """, (user_id, course_id))

        # 受講登録を削除
        cursor.execute("""
            DELETE FROM course_enrollments
            WHERE id = %s
        """, (enrollment_id,))

        conn.commit()
        cursor.close()
        conn.close()

        return jsonify({'success': True})

    except Exception as e:
        logging.error(f"Error deleting enrollment: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

# =============================================================================
# 師匠用: コース管理API（新システム）
# =============================================================================

@migration_assistant.route('/api/v2/mentor/my_courses', methods=['GET'])
def get_my_courses():
    """自分が作成したコース一覧を取得"""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT
                c.id,
                c.course_title,
                c.course_description,
                c.is_public,
                c.created_at,
                c.updated_at,
                (SELECT COUNT(*) FROM course_phase_contents WHERE course_id = c.id) as phase_count,
                (SELECT COUNT(*) FROM course_enrollments WHERE course_id = c.id) as student_count
            FROM courses c
            WHERE c.creator_user_id = %s
            ORDER BY c.updated_at DESC
        """, (user_id,))

        courses = cursor.fetchall()

        # 日時フォーマット
        for course in courses:
            if course['created_at']:
                course['created_at'] = course['created_at'].strftime('%Y-%m-%d %H:%M')
            if course['updated_at']:
                course['updated_at'] = course['updated_at'].strftime('%Y-%m-%d %H:%M')

        cursor.close()
        conn.close()

        return jsonify({'success': True, 'courses': courses})

    except Exception as e:
        logging.error(f"Error getting my courses: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@migration_assistant.route('/api/v2/courses/create', methods=['POST'])
def create_course():
    """新規コースを作成"""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    try:
        data = request.get_json()
        course_title = data.get('course_title', '').strip()
        course_description = data.get('course_description', '').strip()
        is_public = data.get('is_public', True)

        if not course_title:
            return jsonify({'success': False, 'error': 'コースタイトルは必須です'}), 400

        conn = get_db_connection()
        cursor = conn.cursor()

        now_jst = get_now_jst().replace(tzinfo=None)  # JSTで記録（DBはUTCサーバのためデフォルト不可）
        cursor.execute("""
            INSERT INTO courses (creator_user_id, course_title, course_description, is_public, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (user_id, course_title, course_description, is_public, now_jst, now_jst))

        course_id = cursor.lastrowid

        conn.commit()
        cursor.close()
        conn.close()

        return jsonify({'success': True, 'course_id': course_id})

    except Exception as e:
        logging.error(f"Error creating course: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@migration_assistant.route('/api/v2/courses/<int:course_id>', methods=['DELETE'])
def delete_course(course_id):
    """コースを削除（弟子がいない場合のみ）"""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # 自分のコースかチェック
        cursor.execute("""
            SELECT id, course_title FROM courses
            WHERE id = %s AND creator_user_id = %s
        """, (course_id, user_id))

        course = cursor.fetchone()
        if not course:
            cursor.close()
            conn.close()
            return jsonify({'success': False, 'error': 'コースが見つかりません'}), 404

        # 弟子がいないかチェック
        cursor.execute("""
            SELECT COUNT(*) as count FROM course_enrollments
            WHERE course_id = %s
        """, (course_id,))

        result = cursor.fetchone()
        if result['count'] > 0:
            cursor.close()
            conn.close()
            return jsonify({'success': False, 'error': '弟子が受講中のため削除できません'}), 400

        # コンテンツを削除
        cursor.execute("DELETE FROM course_step_contents WHERE course_id = %s", (course_id,))
        cursor.execute("DELETE FROM course_stage_contents WHERE course_id = %s", (course_id,))
        cursor.execute("DELETE FROM course_phase_contents WHERE course_id = %s", (course_id,))

        # コースを削除
        cursor.execute("DELETE FROM courses WHERE id = %s", (course_id,))

        conn.commit()
        cursor.close()
        conn.close()

        return jsonify({'success': True})

    except Exception as e:
        logging.error(f"Error deleting course: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@migration_assistant.route('/api/v2/courses/<int:course_id>/students', methods=['GET'])
def get_course_students(course_id):
    """師匠用: 特定コースの弟子一覧と進捗を取得（SQLを修正）"""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # 弟子一覧を取得 (total_stepsの数え方をCSCに変更)
        cursor.execute(f"""
            SELECT
                e.id as enrollment_id,
                e.student_user_id,
                u.full_name,
                e.enrolled_at,
                -- 完了したステップ数
                (SELECT COUNT(*) FROM course_progress cp
                 WHERE cp.student_user_id = e.student_user_id
                 AND cp.course_id = e.course_id
                 AND cp.status = '完了') as completed_steps,
                -- 【重要】コース内の全ステップ数（マスターから数える）
                (SELECT COUNT(*) FROM course_step_contents csc
                 WHERE csc.course_id = e.course_id) as total_steps
            FROM course_enrollments e
            JOIN {Tables.USERS} u ON e.student_user_id = u.id
            WHERE e.course_id = %s AND e.mentor_user_id = %s
            ORDER BY e.enrolled_at DESC
        """, (course_id, user_id))

        students = cursor.fetchall()

        for student in students:
            if student['enrolled_at']:
                student['enrolled_at'] = student['enrolled_at'].strftime('%Y-%m-%d %H:%M')

        cursor.close()
        conn.close()
        return jsonify({'success': True, 'students': students})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@migration_assistant.route('/api/v2/courses/<int:course_id>/students/<int:student_id>/progress', methods=['GET'])
def get_student_course_progress(course_id, student_id):
    """特定の弟子の詳細進捗を取得"""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # 自分のコースかチェック
        cursor.execute("""
            SELECT id FROM courses
            WHERE id = %s AND creator_user_id = %s
        """, (course_id, user_id))

        if not cursor.fetchone():
            cursor.close()
            conn.close()
            return jsonify({'success': False, 'error': 'コースが見つかりません'}), 404

        # 進捗詳細を取得
        cursor.execute("""
            SELECT
                phase_id,
                stage_id,
                step_id,
                status,
                updated_at
            FROM course_progress
            WHERE student_user_id = %s AND course_id = %s
            ORDER BY updated_at DESC
        """, (student_id, course_id))

        progress = cursor.fetchall()

        # 日時フォーマット
        for p in progress:
            if p['updated_at']:
                p['updated_at'] = p['updated_at'].strftime('%Y-%m-%d %H:%M')

        cursor.close()
        conn.close()

        return jsonify({'success': True, 'progress': progress})

    except Exception as e:
        logging.error(f"Error getting student progress: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@migration_assistant.route('/api/v2/courses/<int:course_id>', methods=['PUT'])
def update_course_settings_api(course_id):
    """コースのタイトル、説明、公開設定を更新"""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    try:
        data = request.get_json()
        title = data.get('course_title')
        description = data.get('course_description')
        is_public = data.get('is_public', True)

        conn = get_db_connection()
        cursor = conn.cursor()

        now_jst = get_now_jst().replace(tzinfo=None)  # JSTで記録（DBはUTCサーバのためNOW()不可）
        # 権限チェック：自分が作成したコースのみ更新可能
        cursor.execute("""
            UPDATE courses
            SET course_title = %s, course_description = %s, is_public = %s, updated_at = %s
            WHERE id = %s AND creator_user_id = %s
        """, (title, description, is_public, now_jst, course_id, user_id))

        conn.commit()
        cursor.close()
        conn.close()

        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"Error updating course settings: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# =============================================================================
# 教材一括結合（検証用）: 全 Phase/Stage/Step の Markdown を1本につなぐ
#   - 既存の get_course_content_v2(course_id) を再利用
#   - つないだ生Markdown と、HTMLレンダリング結果の両方を返す
# このブロックを migration_assistant_routes.py の末尾付近に貼り付けてください。
# =============================================================================
@migration_assistant.route('/api/v2/courses/<int:course_id>/aggregate', methods=['GET'])
def aggregate_course_markdown(course_id):
    """教材の全ステップ本文を結合して返す（検証用）。

    返却:
      success    : bool
      title      : コースタイトル
      markdown   : 全ステップを連結した生Markdown
      html       : それをHTMLレンダリングした文字列
    """
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'ログインが必要です'}), 401

    conn = None
    try:
        # コースタイトルと所有者を取得（自分の教材か管理者のみ許可）
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT creator_user_id, course_title FROM courses WHERE id = %s",
            (course_id,)
        )
        course = cursor.fetchone()
        cursor.close()
        conn.close()
        conn = None

        if not course:
            return jsonify({'success': False, 'error': '指定された教材が見つかりません'}), 404

        if course['creator_user_id'] != user_id and not check_is_admin(user_id):
            return jsonify({'success': False, 'error': 'この教材を閲覧する権限がありません'}), 403

        # 全 Phase/Stage/Step を取得（既存ヘルパーを再利用）
        content = get_course_content_v2(course_id)

        # Markdown を組み立てる
        parts = []
        course_title = course['course_title'] or '（無題の教材）'
        parts.append(f"# {course_title}\n")

        for phase in content:
            parts.append(
                f"\n# Phase {phase['number']}: {phase['title']}\n"
            )
            if phase.get('description'):
                parts.append(f"\n{phase['description']}\n")

            for stage in phase['stages']:
                parts.append(
                    f"\n## Stage {phase['number']}-{stage['number']}: {stage['title']}\n"
                )

                for step in stage['steps']:
                    parts.append(
                        f"\n### Step {step['display_id']}: {step['title']}\n"
                    )
                    detail = step.get('detail') or ''
                    if detail.strip():
                        parts.append(f"\n{detail}\n")
                    else:
                        parts.append("\n*（本文未記入）*\n")

        merged_markdown = "\n".join(parts)

        # HTML レンダリング（preview と同じ拡張で統一）
        try:
            import markdown as md
            merged_html = md.markdown(
                merged_markdown,
                extensions=['extra', 'nl2br', 'sane_lists', 'fenced_code', 'tables']
            )
        except Exception as e:
            print(f"Aggregate render error: {e}")
            import html as html_module
            merged_html = (
                '<pre style="white-space:pre-wrap;">'
                + html_module.escape(merged_markdown)
                + '</pre>'
            )

        return jsonify({
            'success': True,
            'title': course_title,
            'markdown': merged_markdown,
            'html': merged_html
        })

    except Exception as e:
        print(f"Aggregate error: {e}")
        import traceback
        traceback.print_exc()
        if conn:
            conn.close()
        return jsonify({'success': False, 'error': str(e)}), 500