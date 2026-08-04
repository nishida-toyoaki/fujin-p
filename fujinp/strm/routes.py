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

"""strm - シンプル時空資源予約 ルート定義（承認モデルv2）"""
import calendar
import datetime
import logging

from flask import render_template, request, jsonify, session, redirect, url_for
import mysql.connector
from pytz import timezone

from config import Config
from db import DatabaseConfig
from decorators import login_required
from auth import redirect_to_dashboard

from . import strm_bp

# ---------------------------------------------------------------
# 定数・日時ヘルパー（実装ガイド 5.2）
# ---------------------------------------------------------------
JST = timezone('Asia/Tokyo')

GAP_MINUTES = 10  # 同一資源で前後の予約と必要な間隔（分）

# 資源管理・特権・承認の最上位グループ（まいぐる上のグループ名）
GROUP_SUPER_ADMIN = '時空資源総管理者'

WEEKDAYS_JA = ['月', '火', '水', '木', '金', '土', '日']  # date.weekday() 順

STATUS_LABELS = {
    'pending': '承認待ち',
    'active': '確定',
    'canceled': '取り下げ',
    'rejected': '却下',
}

# 予約枠をブロックする状態（承認待ちも枠を押さえる）
BLOCKING_STATUSES = ('pending', 'active')

GROUP_ROLES = ('applicant', 'approver', 'privileged')


def get_jst_now():
    """現在の日時をJSTで取得（naive datetime）。INSERT/UPDATEに使う。"""
    return datetime.datetime.now(JST).replace(tzinfo=None)


def fmt_datetime(d):
    """datetime → 'YYYY-MM-DD HH:MM' 文字列。None は空文字。"""
    if d is None:
        return ''
    if isinstance(d, (datetime.datetime, datetime.date)):
        return d.strftime('%Y-%m-%d %H:%M')
    return str(d)


def fmt_date(d):
    """date/datetime → 'YYYY-MM-DD' 文字列。None は空文字。"""
    if d is None:
        return ''
    if isinstance(d, (datetime.datetime, datetime.date)):
        return d.strftime('%Y-%m-%d')
    return str(d)


def fmt_time(d):
    """datetime → 'HH:MM' 文字列。None は空文字。"""
    if d is None:
        return ''
    if isinstance(d, datetime.datetime):
        return d.strftime('%H:%M')
    return str(d)


def parse_date(s):
    """'YYYY-MM-DD' 文字列 → date。失敗時は None。"""
    if not s:
        return None
    try:
        return datetime.datetime.strptime(s[:10], '%Y-%m-%d').date()
    except Exception:
        return None


# ---------------------------------------------------------------
# 共通ヘルパー
# ---------------------------------------------------------------
def _connect():
    return mysql.connector.connect(**DatabaseConfig.default())


def get_user_category(user_id):
    """共通 users テーブルの category（admin/regular/guest）を返す。"""
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT category FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()
        return row['category'] if row else None
    except Exception as e:
        logging.error("strm.get_user_category error: %s", e)
        return None
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


_GROUP_FN_CACHE = {'fn': None, 'resolved': False}


def _resolve_group_fn():
    """
    まいぐる（user_groups アプリ）公開の get_user_effective_group_ids() を探して返す
    （実装ガイド 6.2「判定ロジックを再発明しない」）。
    配置環境によりインポートパスが異なるため複数候補を試し、結果をキャッシュする。
    """
    if _GROUP_FN_CACHE['resolved']:
        return _GROUP_FN_CACHE['fn']
    import importlib
    candidates = (
        'user_groups.routes',
        'user_groups',
        'fujinp.user_groups.routes',
        'fujinp.user_groups',
    )
    for mod_name in candidates:
        try:
            mod = importlib.import_module(mod_name)
            fn = getattr(mod, 'get_user_effective_group_ids', None)
            if fn is not None:
                logging.info("strm: get_user_effective_group_ids を %s から読み込みました", mod_name)
                _GROUP_FN_CACHE['fn'] = fn
                break
        except Exception:
            continue
    if _GROUP_FN_CACHE['fn'] is None:
        logging.error("strm: まいぐる(user_groups)の get_user_effective_group_ids が"
                      "どのインポートパスでも見つかりません（グループ判定は無効）: %s",
                      ', '.join(candidates))
    _GROUP_FN_CACHE['resolved'] = True
    return _GROUP_FN_CACHE['fn']


def get_effective_group_ids(user_id):
    """
    ユーザーの有効なグループID集合を返す。
    まいぐるの関数が取得できない環境では空集合を返す（グループ関連機能は動作しない）。
    """
    fn = _resolve_group_fn()
    if fn is None:
        return set()
    try:
        return set(int(g) for g in fn(user_id))
    except Exception as e:
        logging.error("strm.get_effective_group_ids error: %s", e)
        return set()


def get_super_admin_group_id():
    """「時空資源総管理者」グループのIDを返す（未作成なら None）。"""
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id FROM user_groups WHERE name = %s", (GROUP_SUPER_ADMIN,))
        row = cursor.fetchone()
        return row['id'] if row else None
    except Exception as e:
        logging.error("strm.get_super_admin_group_id error: %s", e)
        return None
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


def get_user_context(user_id):
    """権限判定用コンテキスト。"""
    category = get_user_category(user_id)
    group_ids = get_effective_group_ids(user_id)
    super_gid = get_super_admin_group_id()
    return {
        'user_id': user_id,
        'category': category,
        'is_admin': category == 'admin',
        'group_ids': group_ids,
        'is_super_admin': super_gid is not None and super_gid in group_ids,
    }


# ---------------------------------------------------------------
# 権限判定（承認モデルv2）
#   - 承認申請不要: ログインユーザ全員が予約可・即確定
#   - 要承認:
#       申請資格   = カテゴリ（regular/guest）または申請グループ所属
#       承認       = 承認グループ（1つ以上）の誰か1人。総管理者は常に承認可
#       特権       = 特権グループ所属者は承認を経ずに即確定。総管理者は常に特権
#   - 資源管理    = 総管理者グループ所属者 + users.category='admin'
# ---------------------------------------------------------------
def can_manage(ctx):
    """資源管理（登録・編集・停止/再開）の権限。"""
    return ctx['is_super_admin'] or ctx['is_admin']


def is_privileged(ctx, resource):
    """要承認資源で承認を経ずに即確定できるか。"""
    if ctx['is_super_admin']:
        return True
    return any(g in ctx['group_ids'] for g in resource['groups']['privileged'])


def can_apply(ctx, resource):
    """この資源に予約（申請）できるか。"""
    if not resource['approval_required']:
        return True  # 承認不要資源はログインユーザ全員
    if ctx['is_super_admin'] or ctx['is_admin']:
        return True
    if is_privileged(ctx, resource):
        return True
    if ctx['category'] == 'regular' and resource['allow_regular']:
        return True
    if ctx['category'] == 'guest' and resource['allow_guest']:
        return True
    return any(g in ctx['group_ids'] for g in resource['groups']['applicant'])


def can_approve(ctx, resource):
    """この資源の承認待ち予約を承認・却下できるか。"""
    if not resource['approval_required']:
        return False
    if ctx['is_super_admin']:
        return True
    return any(g in ctx['group_ids'] for g in resource['groups']['approver'])


# ---------------------------------------------------------------
# 資源の取得（ロール別グループ付き）
# ---------------------------------------------------------------
def _empty_groups():
    return {'applicant': [], 'approver': [], 'privileged': []}


def _attach_groups(cursor, resources):
    """resources（dictのlist）に groups = {applicant/approver/privileged: [group_id,...]} を付与。"""
    for r in resources:
        r['groups'] = _empty_groups()
    ids = [r['id'] for r in resources]
    if not ids:
        return resources
    index = {r['id']: r for r in resources}
    placeholders = ','.join(['%s'] * len(ids))
    cursor.execute("""
        SELECT resource_id, group_id, role
        FROM strm_resource_groups
        WHERE resource_id IN (%s)
    """ % placeholders, ids)
    for row in cursor.fetchall():
        r = index.get(row['resource_id'])
        if r is not None and row['role'] in r['groups']:
            r['groups'][row['role']].append(row['group_id'])
    return resources


RESOURCE_COLUMNS = """
    id, name, category, description,
    approval_required, allow_regular, allow_guest, is_active
"""


def _fetch_resources(cursor, only_active=True):
    sql = "SELECT %s FROM strm_resources" % RESOURCE_COLUMNS
    if only_active:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY category, name"
    cursor.execute(sql)
    return _attach_groups(cursor, cursor.fetchall())


def _fetch_resource(cursor, resource_id, only_active=True):
    sql = "SELECT %s FROM strm_resources WHERE id = %%s" % RESOURCE_COLUMNS
    if only_active:
        sql += " AND is_active = 1"
    cursor.execute(sql, (resource_id,))
    row = cursor.fetchone()
    if not row:
        return None
    return _attach_groups(cursor, [row])[0]


def _approval_summary(ctx):
    """(このユーザーが承認者か, 承認可能な承認待ち件数) を返す。"""
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        resources = _fetch_resources(cursor, only_active=False)
        approvable_ids = [r['id'] for r in resources if can_approve(ctx, r)]
        if not approvable_ids:
            return False, 0
        placeholders = ','.join(['%s'] * len(approvable_ids))
        cursor.execute("""
            SELECT COUNT(*) AS cnt
            FROM strm_reservations
            WHERE status = 'pending' AND resource_id IN (%s)
        """ % placeholders, approvable_ids)
        return True, cursor.fetchone()['cnt']
    except Exception as e:
        logging.error("strm._approval_summary error: %s", e)
        return False, 0
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


def _template_context(user_id):
    ctx = get_user_context(user_id)
    has_rights, pending_count = _approval_summary(ctx)
    return {
        'is_admin': can_manage(ctx),
        'show_pending_link': has_rights,
        'pending_count': pending_count,
    }


# ---------------------------------------------------------------
# 画面
# ---------------------------------------------------------------
@strm_bp.route('/return_to_fujin')
def return_to_fujin():
    """FUJIN-Pダッシュボードに戻る（ユーザカテゴリに応じた戻り先へ）"""
    return redirect_to_dashboard()



@strm_bp.route('/')
@login_required
def index():
    """週次予約カレンダー画面"""
    return render_template('strm/index.html', **_template_context(session.get('user_id')))


@strm_bp.route('/overview')
@login_required
def overview():
    """年間予約状況カレンダー画面"""
    return render_template('strm/overview.html', **_template_context(session.get('user_id')))


@strm_bp.route('/resources')
@login_required
def resources():
    """資源管理画面（時空資源総管理者・admin専用）"""
    tc = _template_context(session.get('user_id'))
    if not tc['is_admin']:
        return redirect(url_for('strm.index'))
    return render_template('strm/resources.html', **tc)


@strm_bp.route('/pending')
@login_required
def pending():
    """承認待ち一覧画面（承認者専用）"""
    tc = _template_context(session.get('user_id'))
    if not tc['show_pending_link']:
        return redirect(url_for('strm.index'))
    return render_template('strm/pending.html', **tc)


# ---------------------------------------------------------------
# API: 週次カレンダー
# ---------------------------------------------------------------
@strm_bp.route('/api/week', methods=['GET'])
@login_required
def api_week():
    """指定週の資源一覧と予約一覧を返す。日時・状態・権限判定はすべてサーバ側で行い文字列で返す。"""
    user_id = session.get('user_id')
    ctx = get_user_context(user_id)
    now = get_jst_now()
    today = now.date()

    base = parse_date(request.args.get('start', '')) or today
    week_start = base - datetime.timedelta(days=(base.weekday() + 1) % 7)  # 日曜始まり
    days = [week_start + datetime.timedelta(days=i) for i in range(7)]

    week_dates = [{
        'date': fmt_date(d),
        'label': '%d/%d' % (d.month, d.day),
        'weekday': WEEKDAYS_JA[d.weekday()],
        'is_today': d == today,
    } for d in days]

    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)

        resource_rows = _fetch_resources(cursor, only_active=True)
        resources_out = []
        approvable = {}
        for r in resource_rows:
            approvable[r['id']] = can_approve(ctx, r)
            resources_out.append({
                'id': r['id'],
                'name': r['name'],
                'category': r['category'],
                'requires_approval': bool(r['approval_required']),
                'can_apply': can_apply(ctx, r),
                # このユーザーが予約したとき承認待ちになるか（特権なら False）
                'needs_approval': bool(r['approval_required']) and not is_privileged(ctx, r),
            })

        cursor.execute("""
            SELECT r.id, r.resource_id, r.user_id, r.start_at, r.end_at, r.summary, r.status,
                   u.full_name AS reserver_name
            FROM strm_reservations r
            JOIN users u ON r.user_id = u.id
            WHERE r.status IN ('pending', 'active')
              AND DATE(r.start_at) BETWEEN %s AND %s
            ORDER BY r.start_at
        """, (fmt_date(days[0]), fmt_date(days[6])))

        reservations = {}
        for row in cursor.fetchall():
            d = fmt_date(row['start_at'])
            if row['end_at'] < now:
                time_state = 'past'
            elif row['start_at'] <= now:
                time_state = 'current'
            else:
                time_state = 'future'
            is_mine = (row['user_id'] == user_id)
            item = {
                'id': row['id'],
                'date': d,
                'start_time': fmt_time(row['start_at']),
                'end_time': fmt_time(row['end_at']),
                'summary': row['summary'] or '',
                'reserver_name': row['reserver_name'],
                'status': row['status'],
                'status_label': STATUS_LABELS.get(row['status'], row['status']),
                'time_state': time_state,
                'is_mine': is_mine,
                'can_cancel': is_mine and row['end_at'] >= now,
                'can_approve': row['status'] == 'pending' and approvable.get(row['resource_id'], False),
            }
            reservations.setdefault(str(row['resource_id']), {}).setdefault(d, []).append(item)

        return jsonify({
            'success': True,
            'resources': resources_out,
            'reservations': reservations,
            'week_dates': week_dates,
            'week_label': '%s 〜 %s' % (days[0].strftime('%Y年%m月%d日'),
                                        days[6].strftime('%Y年%m月%d日')),
            'prev_start': fmt_date(week_start - datetime.timedelta(days=7)),
            'next_start': fmt_date(week_start + datetime.timedelta(days=7)),
        })

    except Exception as e:
        logging.error("strm.api_week error: %s", e)
        return jsonify({'success': False, 'error': 'データの取得中にエラーが発生しました。'}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


# ---------------------------------------------------------------
# API: 予約の作成・取り下げ・承認・却下
# ---------------------------------------------------------------
@strm_bp.route('/api/reservations', methods=['POST'])
@login_required
def api_create_reservation():
    """予約作成。承認不要または特権なら即「確定」、それ以外の要承認資源は「承認待ち」。"""
    data = request.json or {}
    user_id = session.get('user_id')
    resource_id = data.get('resource_id')
    date_s = (data.get('date') or '').strip()
    start_s = (data.get('start_time') or '').strip()
    end_s = (data.get('end_time') or '').strip()
    summary = (data.get('summary') or '').strip()[:200]

    if not all([resource_id, date_s, start_s, end_s]):
        return jsonify({'success': False, 'error': '必要な日時情報が不足しています。'}), 400

    try:
        start_at = datetime.datetime.strptime('%s %s' % (date_s, start_s), '%Y-%m-%d %H:%M')
        end_at = datetime.datetime.strptime('%s %s' % (date_s, end_s), '%Y-%m-%d %H:%M')
    except ValueError:
        return jsonify({'success': False, 'error': '日時の形式が正しくありません。'}), 400

    now = get_jst_now()
    if end_at <= start_at:
        return jsonify({'success': False, 'error': '終了時刻は開始時刻より後である必要があります。'}), 400
    if start_at < now:
        return jsonify({'success': False, 'error': '過去の日時は予約できません。'}), 400

    ctx = get_user_context(user_id)

    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)

        resource = _fetch_resource(cursor, resource_id, only_active=True)
        if not resource:
            return jsonify({'success': False, 'error': '資源が見つかりません。'}), 404

        if not can_apply(ctx, resource):
            return jsonify({'success': False,
                            'error': 'この資源の予約申請資格がありません。'}), 403

        # 10分間隔ルール（承認待ちも枠をブロックする）
        cursor.execute("""
            SELECT COUNT(*) AS cnt
            FROM strm_reservations
            WHERE resource_id = %s
              AND status IN ('pending', 'active')
              AND start_at < DATE_ADD(%s, INTERVAL %s MINUTE)
              AND end_at > DATE_SUB(%s, INTERVAL %s MINUTE)
        """, (resource_id, end_at, GAP_MINUTES, start_at, GAP_MINUTES))
        if cursor.fetchone()['cnt'] > 0:
            return jsonify({'success': False,
                            'error': '前後の予約との間に%d分以上の間隔が必要です。' % GAP_MINUTES}), 400

        needs_approval = bool(resource['approval_required']) and not is_privileged(ctx, resource)
        status = 'pending' if needs_approval else 'active'
        decided_by = None if needs_approval else user_id
        decided_at = None if needs_approval else now

        cursor.execute("""
            INSERT INTO strm_reservations
                (resource_id, user_id, start_at, end_at, summary, status,
                 created_at, decided_by, decided_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (resource_id, user_id, start_at, end_at, summary, status,
              now, decided_by, decided_at))
        conn.commit()

        message = '予約を申請しました（承認待ち）。' if needs_approval else '予約が完了しました。'
        return jsonify({'success': True, 'id': cursor.lastrowid,
                        'status': status, 'message': message})

    except Exception as e:
        logging.error("strm.api_create_reservation error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': 'データベースエラーが発生しました。'}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@strm_bp.route('/api/reservations/<int:reservation_id>/cancel', methods=['POST'])
@login_required
def api_cancel_reservation(reservation_id):
    """予約取り下げ。本人のみ・終了前のみ・pending/active のみ（サーバ側で検証）。"""
    user_id = session.get('user_id')
    now = get_jst_now()
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT id, user_id, end_at, status
            FROM strm_reservations
            WHERE id = %s
        """, (reservation_id,))
        row = cursor.fetchone()
        if not row:
            return jsonify({'success': False, 'error': '予約が見つかりません。'}), 404
        if row['user_id'] != user_id:
            return jsonify({'success': False, 'error': '自分の予約のみ取り下げできます。'}), 403
        if row['status'] not in BLOCKING_STATUSES:
            return jsonify({'success': False, 'error': 'この予約はすでに取り下げ・却下されています。'}), 400
        if row['end_at'] < now:
            return jsonify({'success': False, 'error': '終了時刻を過ぎた予約は取り下げできません。'}), 400

        cursor.execute("""
            UPDATE strm_reservations
            SET status = 'canceled', decided_by = %s, decided_at = %s
            WHERE id = %s AND user_id = %s AND status IN ('pending', 'active')
        """, (user_id, now, reservation_id, user_id))
        conn.commit()

        return jsonify({'success': True, 'message': '予約を取り下げました。'})

    except Exception as e:
        logging.error("strm.api_cancel_reservation error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': 'データベースエラーが発生しました。'}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


def _decide_reservation(reservation_id, new_status, done_message):
    """承認（active）／却下（rejected）の共通処理。承認者の誰か1人の操作で決定する。"""
    user_id = session.get('user_id')
    ctx = get_user_context(user_id)
    now = get_jst_now()
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT id, resource_id, status
            FROM strm_reservations
            WHERE id = %s
        """, (reservation_id,))
        row = cursor.fetchone()
        if not row:
            return jsonify({'success': False, 'error': '予約が見つかりません。'}), 404
        if row['status'] != 'pending':
            return jsonify({'success': False, 'error': 'この予約は承認待ちではありません。'}), 400

        resource = _fetch_resource(cursor, row['resource_id'], only_active=False)
        if not resource or not can_approve(ctx, resource):
            return jsonify({'success': False, 'error': '承認権限がありません。'}), 403

        cursor.execute("""
            UPDATE strm_reservations
            SET status = %s, decided_by = %s, decided_at = %s
            WHERE id = %s AND status = 'pending'
        """, (new_status, user_id, now, reservation_id))
        conn.commit()

        return jsonify({'success': True, 'message': done_message})

    except Exception as e:
        logging.error("strm._decide_reservation error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': 'データベースエラーが発生しました。'}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@strm_bp.route('/api/reservations/<int:reservation_id>/approve', methods=['POST'])
@login_required
def api_approve_reservation(reservation_id):
    """予約承認（承認グループの誰か1人、または時空資源総管理者）"""
    return _decide_reservation(reservation_id, 'active', '予約を承認しました。')


@strm_bp.route('/api/reservations/<int:reservation_id>/reject', methods=['POST'])
@login_required
def api_reject_reservation(reservation_id):
    """予約却下（承認者専用）。却下されると枠が解放される。"""
    return _decide_reservation(reservation_id, 'rejected', '予約を却下しました。')


@strm_bp.route('/api/pending', methods=['GET'])
@login_required
def api_pending():
    """承認ダッシュボード用データ。自分が承認者である承認待ち一覧と、最近の決定履歴を返す。"""
    user_id = session.get('user_id')
    ctx = get_user_context(user_id)
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)

        resources_by_id = {r['id']: r for r in _fetch_resources(cursor, only_active=False)}
        approvable_ids = [rid for rid, r in resources_by_id.items() if can_approve(ctx, r)]

        items = []
        recent = []
        if approvable_ids:
            placeholders = ','.join(['%s'] * len(approvable_ids))

            cursor.execute("""
                SELECT r.id, r.resource_id, r.start_at, r.end_at, r.summary, r.created_at,
                       u.full_name AS reserver_name
                FROM strm_reservations r
                JOIN users u ON r.user_id = u.id
                WHERE r.status = 'pending' AND r.resource_id IN (%s)
                ORDER BY r.created_at
            """ % placeholders, approvable_ids)
            for row in cursor.fetchall():
                items.append({
                    'id': row['id'],
                    'resource_name': resources_by_id[row['resource_id']]['name'],
                    'reserver_name': row['reserver_name'],
                    'start': fmt_datetime(row['start_at']),
                    'end_time': fmt_time(row['end_at']),
                    'summary': row['summary'] or '',
                    'created': fmt_datetime(row['created_at']),
                })

            # 最近の決定履歴（担当資源の直近20件）
            cursor.execute("""
                SELECT r.id, r.resource_id, r.start_at, r.end_at, r.summary, r.status,
                       r.decided_at,
                       u.full_name AS reserver_name,
                       d.full_name AS decided_by_name
                FROM strm_reservations r
                JOIN users u ON r.user_id = u.id
                LEFT JOIN users d ON r.decided_by = d.id
                WHERE r.resource_id IN (%s)
                  AND r.status IN ('active', 'rejected', 'canceled')
                  AND r.decided_at IS NOT NULL
                ORDER BY r.decided_at DESC
                LIMIT 20
            """ % placeholders, approvable_ids)
            for row in cursor.fetchall():
                recent.append({
                    'id': row['id'],
                    'resource_name': resources_by_id[row['resource_id']]['name'],
                    'reserver_name': row['reserver_name'],
                    'start': fmt_datetime(row['start_at']),
                    'end_time': fmt_time(row['end_at']),
                    'summary': row['summary'] or '',
                    'status': row['status'],
                    'status_label': STATUS_LABELS.get(row['status'], row['status']),
                    'decided_by_name': row['decided_by_name'] or '',
                    'decided_at': fmt_datetime(row['decided_at']),
                })

        return jsonify({'success': True, 'items': items, 'recent': recent,
                        'pending_count': len(items)})
    except Exception as e:
        logging.error("strm.api_pending error: %s", e)
        return jsonify({'success': False, 'error': 'データの取得中にエラーが発生しました。'}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


# ---------------------------------------------------------------
# API: 年間予約状況
# ---------------------------------------------------------------
@strm_bp.route('/api/annual', methods=['GET'])
@login_required
def api_annual():
    """年度（4月〜翌3月）のカレンダー構造と、日別の午前|午後予約件数（確定分）を返す。"""
    now = get_jst_now()
    default_year = now.year if now.month >= 4 else now.year - 1
    year = request.args.get('year', type=int) or default_year

    resource_ids = []
    for v in request.args.getlist('resources[]'):
        try:
            resource_ids.append(int(v))
        except (TypeError, ValueError):
            continue

    months = []
    for i in range(12):
        mm = 4 + i
        yy = year if mm <= 12 else year + 1
        mm = mm if mm <= 12 else mm - 12
        months.append({
            'label': '%d年 %d月' % (yy, mm),
            'year': yy,
            'month': mm,
            'first_weekday': (datetime.date(yy, mm, 1).weekday() + 1) % 7,  # 日曜=0
            'days': calendar.monthrange(yy, mm)[1],
        })

    counts = {}
    if resource_ids:
        try:
            conn = _connect()
            cursor = conn.cursor(dictionary=True)
            placeholders = ','.join(['%s'] * len(resource_ids))
            cursor.execute("""
                SELECT start_at
                FROM strm_reservations
                WHERE status = 'active'
                  AND resource_id IN (%s)
                  AND start_at >= %%s AND start_at < %%s
            """ % placeholders,
                resource_ids + ['%d-04-01 00:00:00' % year, '%d-04-01 00:00:00' % (year + 1)])
            for row in cursor.fetchall():
                d = fmt_date(row['start_at'])
                part = 'am' if row['start_at'].hour <= 12 else 'pm'
                counts.setdefault(d, {'am': 0, 'pm': 0})
                counts[d][part] += 1
        except Exception as e:
            logging.error("strm.api_annual error: %s", e)
            return jsonify({'success': False, 'error': 'データの取得中にエラーが発生しました。'}), 500
        finally:
            if 'conn' in locals() and conn.is_connected():
                cursor.close()
                conn.close()

    return jsonify({
        'success': True,
        'year': year,
        'year_label': '%d年度' % year,
        'months': months,
        'counts': counts,
        'today': fmt_date(now.date()),
    })


# ---------------------------------------------------------------
# API: 資源管理
# ---------------------------------------------------------------
def _group_names(cursor):
    """user_groups のグループ名一覧（まいぐる未導入環境では空dict）。"""
    try:
        cursor.execute("SELECT id, name FROM user_groups ORDER BY name")
        return {row['id']: row['name'] for row in cursor.fetchall()}
    except Exception as e:
        logging.error("strm._group_names error: %s", e)
        return {}


def _named(group_ids, group_names):
    return [{'id': gid, 'name': group_names.get(gid, '(不明: %s)' % gid)} for gid in group_ids]


@strm_bp.route('/api/resources', methods=['GET'])
@login_required
def api_resources():
    """資源一覧。all=1（管理権限時）で停止中も含める。"""
    user_id = session.get('user_id')
    ctx = get_user_context(user_id)
    show_all = request.args.get('all') == '1' and can_manage(ctx)
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        group_names = _group_names(cursor)

        items = []
        for r in _fetch_resources(cursor, only_active=not show_all):
            items.append({
                'id': r['id'],
                'name': r['name'],
                'category': r['category'],
                'description': r['description'] or '',
                'approval_required': bool(r['approval_required']),
                'requires_approval': bool(r['approval_required']),
                'allow_regular': bool(r['allow_regular']),
                'allow_guest': bool(r['allow_guest']),
                'applicant_groups': _named(r['groups']['applicant'], group_names),
                'approver_groups': _named(r['groups']['approver'], group_names),
                'privileged_groups': _named(r['groups']['privileged'], group_names),
                'is_active': bool(r['is_active']),
            })
        return jsonify({'success': True, 'resources': items})
    except Exception as e:
        logging.error("strm.api_resources error: %s", e)
        return jsonify({'success': False, 'error': 'データの取得中にエラーが発生しました。'}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@strm_bp.route('/api/groups', methods=['GET'])
@login_required
def api_groups():
    """まいぐるのグループ一覧（資源管理画面のグループ選択用）。"""
    ctx = get_user_context(session.get('user_id'))
    if not can_manage(ctx):
        return jsonify({'success': False, 'error': '権限がありません。'}), 403
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        groups = [{'id': gid, 'name': name} for gid, name in _group_names(cursor).items()]
        groups.sort(key=lambda g: g['name'])
        return jsonify({'success': True, 'groups': groups,
                        'super_admin_group': GROUP_SUPER_ADMIN})
    except Exception as e:
        logging.error("strm.api_groups error: %s", e)
        return jsonify({'success': False, 'error': 'データの取得中にエラーが発生しました。'}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


def _validate_resource_payload(data):
    """資源作成・更新の入力検証。(payload, error) を返す。"""
    name = (data.get('name') or '').strip()[:100]
    category = (data.get('category') or '部屋').strip()[:50] or '部屋'
    description = (data.get('description') or '').strip()
    approval_required = bool(data.get('approval_required'))
    allow_regular = bool(data.get('allow_regular', True))
    allow_guest = bool(data.get('allow_guest', True))

    def _ids(key):
        out = set()
        for v in (data.get(key) or []):
            try:
                out.add(int(v))
            except (TypeError, ValueError):
                continue
        return sorted(out)

    approver = _ids('approver_group_ids')
    privileged = _ids('privileged_group_ids')
    applicant = _ids('applicant_group_ids')

    if not name:
        return None, '資源名は必須です。'
    if approval_required and not approver:
        return None, '要承認の場合は承認グループを1つ以上指定してください。'
    if not approval_required:
        # 承認不要資源では承認関連設定は意味を持たないためクリアする
        approver, privileged, applicant = [], [], []
        allow_regular, allow_guest = True, True

    return {
        'name': name,
        'category': category,
        'description': description,
        'approval_required': 1 if approval_required else 0,
        'allow_regular': 1 if allow_regular else 0,
        'allow_guest': 1 if allow_guest else 0,
        'group_rows': ([('applicant', g) for g in applicant] +
                       [('approver', g) for g in approver] +
                       [('privileged', g) for g in privileged]),
    }, None


def _save_resource_groups(cursor, resource_id, group_rows):
    """資源のロール別グループ設定を全置換する。"""
    cursor.execute("DELETE FROM strm_resource_groups WHERE resource_id = %s", (resource_id,))
    for role, group_id in group_rows:
        cursor.execute("""
            INSERT INTO strm_resource_groups (resource_id, group_id, role)
            VALUES (%s, %s, %s)
        """, (resource_id, group_id, role))


@strm_bp.route('/api/resources', methods=['POST'])
@login_required
def api_resource_create():
    """資源作成（時空資源総管理者・admin専用）"""
    ctx = get_user_context(session.get('user_id'))
    if not can_manage(ctx):
        return jsonify({'success': False, 'error': '権限がありません。'}), 403
    payload, err = _validate_resource_payload(request.json or {})
    if err:
        return jsonify({'success': False, 'error': err}), 400
    now = get_jst_now()
    try:
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO strm_resources
                (name, category, description, approval_required,
                 allow_regular, allow_guest, is_active, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s)
        """, (payload['name'], payload['category'], payload['description'],
              payload['approval_required'], payload['allow_regular'],
              payload['allow_guest'], now, now))
        resource_id = cursor.lastrowid
        _save_resource_groups(cursor, resource_id, payload['group_rows'])
        conn.commit()
        return jsonify({'success': True, 'id': resource_id, 'message': '資源を登録しました。'})
    except Exception as e:
        logging.error("strm.api_resource_create error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': 'データベースエラーが発生しました。'}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@strm_bp.route('/api/resources/<int:resource_id>/update', methods=['POST'])
@login_required
def api_resource_update(resource_id):
    """資源更新（時空資源総管理者・admin専用）"""
    ctx = get_user_context(session.get('user_id'))
    if not can_manage(ctx):
        return jsonify({'success': False, 'error': '権限がありません。'}), 403
    payload, err = _validate_resource_payload(request.json or {})
    if err:
        return jsonify({'success': False, 'error': err}), 400
    now = get_jst_now()
    try:
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE strm_resources
            SET name = %s, category = %s, description = %s,
                approval_required = %s, allow_regular = %s, allow_guest = %s,
                updated_at = %s
            WHERE id = %s
        """, (payload['name'], payload['category'], payload['description'],
              payload['approval_required'], payload['allow_regular'],
              payload['allow_guest'], now, resource_id))
        if cursor.rowcount == 0:
            # 変更なし更新の可能性もあるため存在確認
            cursor.execute("SELECT COUNT(*) FROM strm_resources WHERE id = %s", (resource_id,))
            if cursor.fetchone()[0] == 0:
                return jsonify({'success': False, 'error': '資源が見つかりません。'}), 404
        _save_resource_groups(cursor, resource_id, payload['group_rows'])
        conn.commit()
        return jsonify({'success': True, 'message': '資源を更新しました。'})
    except Exception as e:
        logging.error("strm.api_resource_update error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': 'データベースエラーが発生しました。'}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@strm_bp.route('/api/resources/<int:resource_id>/toggle', methods=['POST'])
@login_required
def api_resource_toggle(resource_id):
    """資源の停止／再開（時空資源総管理者・admin専用・論理削除）"""
    ctx = get_user_context(session.get('user_id'))
    if not can_manage(ctx):
        return jsonify({'success': False, 'error': '権限がありません。'}), 403
    now = get_jst_now()
    try:
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE strm_resources
            SET is_active = 1 - is_active, updated_at = %s
            WHERE id = %s
        """, (now, resource_id))
        if cursor.rowcount == 0:
            return jsonify({'success': False, 'error': '資源が見つかりません。'}), 404
        conn.commit()
        return jsonify({'success': True, 'message': '資源の状態を切り替えました。'})
    except Exception as e:
        logging.error("strm.api_resource_toggle error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': 'データベースエラーが発生しました。'}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()
