"""appo - あぽる ルート定義"""
import datetime
import functools
import json
import logging
from contextlib import contextmanager

from flask import render_template, request, jsonify, session
import mysql.connector
from pytz import timezone

from config import Config  # noqa: F401  （実装ガイド 5.1 の必須インポート．本アプリは定数を参照しない）
from db import DatabaseConfig
from decorators import login_required
from auth import redirect_to_dashboard

from . import appo_bp

# ---------------------------------------------------------------
# 定数・日時ヘルパー（実装ガイド 5.2）
# ---------------------------------------------------------------
JST = timezone('Asia/Tokyo')

LATE_HOURS = 24  # 開始前この時間以内の成立・キャンセルを「直前」と表示する
LATE = datetime.timedelta(hours=LATE_HOURS)

WEEKDAYS_JA = ['月', '火', '水', '木', '金', '土', '日']  # date.weekday() 順

MODES = {'office': 'オフィス', 'online': 'オンライン', 'hybrid': 'オフィス＋オンライン'}
KINDS = {'exclusive': '排他的（1対1）', 'open': '非排他的（懇談）'}
P_STATUS = {'pending': '承諾待ち', 'accepted': '成立', 'declined': '不成立', 'canceled': 'キャンセル'}
P_ORIGIN = {'invite': '招待', 'request': '請求'}
LIVE = ('pending', 'accepted')

MAX_TITLE = 200
MAX_TEXT = 1000


def get_jst_now():
    return datetime.datetime.now(JST).replace(tzinfo=None)


def fmt_datetime(d):
    if d is None:
        return ''
    if isinstance(d, (datetime.datetime, datetime.date)):
        return d.strftime('%Y-%m-%d %H:%M')
    return str(d)


def fmt_date(d):
    if d is None:
        return ''
    if isinstance(d, (datetime.datetime, datetime.date)):
        return d.strftime('%Y-%m-%d')
    return str(d)


def fmt_time(d):
    if d is None:
        return ''
    if isinstance(d, datetime.datetime):
        return d.strftime('%H:%M')
    return str(d)


def parse_date(s):
    if not s:
        return None
    try:
        return datetime.datetime.strptime(s[:10], '%Y-%m-%d').date()
    except Exception:
        return None


# ---------------------------------------------------------------
# 共通ヘルパー
# ---------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.message = message
        self.code = code


def api_route(fn):
    """API の例外を JSON に変換する．"""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ApiError as e:
            return jsonify({'success': False, 'error': e.message}), e.code
        except Exception as e:
            logging.exception("appo.%s error: %s", fn.__name__, e)
            return jsonify({'success': False, 'error': 'サーバでエラーが発生しました．'}), 500
    return wrapper


@contextmanager
def _db():
    conn = mysql.connector.connect(**DatabaseConfig.default())
    cursor = conn.cursor(dictionary=True)
    try:
        yield cursor, conn
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def _uid():
    return session.get('user_id')


def _int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _text(data, key, limit):
    return (data.get(key) or '').strip()[:limit]


def _parse_span(data):
    """date / start_time / end_time から同一日内の (start_at, end_at) を作る．"""
    date_s = (data.get('date') or '').strip()
    start_s = (data.get('start_time') or '').strip()
    end_s = (data.get('end_time') or '').strip()
    if not (date_s and start_s and end_s):
        raise ApiError('日付と時刻を入力してください．')
    try:
        start_at = datetime.datetime.strptime('%s %s' % (date_s, start_s[:5]), '%Y-%m-%d %H:%M')
        end_at = datetime.datetime.strptime('%s %s' % (date_s, end_s[:5]), '%Y-%m-%d %H:%M')
    except ValueError:
        raise ApiError('日時の形式が正しくありません．')
    if end_at <= start_at:
        raise ApiError('終了時刻は開始時刻より後にしてください．')
    if start_at < get_jst_now():
        raise ApiError('過去の日時は指定できません．')
    return start_at, end_at


def _parse_place(data):
    mode = data.get('mode') or 'office'
    if mode not in MODES:
        raise ApiError('場所の種別が正しくありません．')
    location = _text(data, 'location', MAX_TITLE)
    online_url = _text(data, 'online_url', 500)
    if online_url and not online_url.lower().startswith(('https://', 'http://')):
        raise ApiError('オンラインのURLは http:// または https:// で始めてください．')
    return mode, location, online_url


def _user_name(cursor, user_id):
    cursor.execute("SELECT full_name FROM users WHERE id = %s", (user_id,))
    row = cursor.fetchone()
    return row['full_name'] if row else ''


def _active_user_ids(cursor, ids):
    ids = [i for i in ids if i is not None]
    if not ids:
        return set()
    ph = ','.join(['%s'] * len(ids))
    cursor.execute("SELECT id FROM users WHERE id IN (%s) AND is_active = 1 AND deleted_at IS NULL" % ph, ids)
    return {r['id'] for r in cursor.fetchall()}


def _busy(cursor, user_id, start_at, end_at, exclude_id=None):
    """user_id がその時間帯にホストまたは成立済みゲストとして予定を持つか．"""
    sql = """
        SELECT m.id FROM appo_meetings m
        WHERE m.status = 'scheduled'
          AND m.start_at < %s AND m.end_at > %s
          AND ((m.host_id = %s AND (m.listed = 1 OR m.origin <> 'request'))
               OR EXISTS (SELECT 1 FROM appo_participants p
                          WHERE p.meeting_id = m.id AND p.guest_id = %s AND p.status = 'accepted'))
    """
    params = [end_at, start_at, user_id, user_id]
    if exclude_id:
        sql += " AND m.id <> %s"
        params.append(exclude_id)
    cursor.execute(sql + " LIMIT 1", params)
    return cursor.fetchone() is not None


def _get_meeting(cursor, meeting_id, for_update=False):
    sql = """SELECT m.*, u.full_name AS host_name FROM appo_meetings m
             JOIN users u ON m.host_id = u.id WHERE m.id = %s"""
    if for_update:
        sql += " FOR UPDATE"
    cursor.execute(sql, (meeting_id,))
    return cursor.fetchone()


def _participants_for(cursor, meeting_ids):
    out = {mid: [] for mid in meeting_ids}
    if not meeting_ids:
        return out
    ph = ','.join(['%s'] * len(meeting_ids))
    cursor.execute("""
        SELECT p.*, u.full_name AS guest_name
        FROM appo_participants p JOIN users u ON p.guest_id = u.id
        WHERE p.meeting_id IN (%s) ORDER BY p.id
    """ % ph, list(meeting_ids))
    for row in cursor.fetchall():
        out.setdefault(row['meeting_id'], []).append(row)
    return out


def _my_participation(parts, uid):
    """自分の参加行（生きている行を優先，なければ最新）．"""
    mine = [p for p in parts if p['guest_id'] == uid]
    if not mine:
        return None
    live = [p for p in mine if p['status'] in LIVE]
    return (live or mine)[-1]


def _can_view(m, parts, uid):
    if m['host_id'] == uid or any(p['guest_id'] == uid for p in parts):
        return True
    return bool(m['listed']) and m['status'] in ('scheduled', 'canceled')


def _is_member(m, parts, uid):
    """サブチャンネルを読み書きできるか（ホストと，不成立以外の参加行を持つ人）．"""
    if m['host_id'] == uid:
        return True
    return any(p['guest_id'] == uid and p['status'] in ('pending', 'accepted', 'canceled') for p in parts)


def _show_guest(p, uid, m):
    return m['host_id'] == uid or p['guest_id'] == uid or bool(p['show_name'])


def _summarize(m, parts, uid, now):
    """カレンダーと一覧に出す要約．日時はすべて文字列．"""
    is_host = m['host_id'] == uid
    accepted = [p for p in parts if p['status'] == 'accepted']
    pending = [p for p in parts if p['status'] == 'pending' and p['origin'] == 'request']
    mine = _my_participation(parts, uid)

    names, hidden = [], 0
    for p in accepted:
        if _show_guest(p, uid, m):
            names.append(p['guest_name'])
        else:
            hidden += 1

    if m['end_at'] < now:
        time_state = 'past'
    elif m['start_at'] <= now:
        time_state = 'current'
    else:
        time_state = 'future'

    if m['status'] == 'canceled':
        state, label = 'canceled', 'キャンセル'
    elif m['status'] == 'void':
        state, label = 'void', '不成立'
    elif m['origin'] == 'request' and not m['listed'] and not accepted:
        state, label = 'proposed', '請求中'
    elif m['kind'] == 'exclusive':
        if accepted:
            state, label = 'booked', '成立'
        elif m['listed']:
            state, label = 'free', '空き'
        else:
            state, label = 'inviting', '招待中'
    else:
        cap = m['capacity'] or 0
        if cap and len(accepted) >= cap:
            state, label = 'full', '懇談 満席'
        else:
            state = 'open'
            label = '懇談 %d名' % len(accepted) + ('/%d' % cap if cap else '')

    late_booked = any(p['decided_at'] and (m['start_at'] - p['decided_at']) < LATE for p in accepted)
    late_cancel = bool(m['canceled_at']) and (m['start_at'] - m['canceled_at']) < LATE
    late_guest_cancels = sum(1 for p in parts
                             if p['status'] == 'canceled' and p['decided_at'] and p['canceled_at']
                             and (m['start_at'] - p['canceled_at']) < LATE)

    live_mine = mine is not None and mine['status'] in LIVE
    cap = m['capacity'] or 0
    can_request = (m['status'] == 'scheduled' and bool(m['listed']) and not is_host
                   and not live_mine and now < m['end_at']
                   and ((m['kind'] == 'exclusive' and not accepted)
                        or (m['kind'] == 'open' and (not cap or len(accepted) < cap))))

    return {
        'id': m['id'],
        'host_id': m['host_id'],
        'host_name': m['host_name'],
        'title': m['title'] or '',
        'date': fmt_date(m['start_at']),
        'weekday': WEEKDAYS_JA[m['start_at'].weekday()],
        'start_time': fmt_time(m['start_at']),
        'end_time': fmt_time(m['end_at']),
        'mode': m['mode'],
        'mode_label': MODES.get(m['mode'], m['mode']),
        'location': m['location'] or '',
        'kind': m['kind'],
        'kind_label': KINDS.get(m['kind'], m['kind']),
        'capacity': cap,
        'auto_accept': bool(m['auto_accept']),
        'listed': bool(m['listed']),
        'origin': m['origin'],
        'status': m['status'],
        'state': state,
        'state_label': label,
        'time_state': time_state,
        'accepted_count': len(accepted),
        'pending_count': len(pending) if is_host else 0,
        'visible_guests': names,
        'hidden_count': hidden,
        'my_role': 'host' if is_host else ('guest' if mine else None),
        'my_status': mine['status'] if mine else '',
        'my_status_label': P_STATUS.get(mine['status'], '') if mine else '',
        'my_participant_id': mine['id'] if mine else None,
        'late_booked': late_booked,
        'late_cancel': late_cancel,
        'late_guest_cancels': late_guest_cancels,
        'canceled_at': fmt_datetime(m['canceled_at']),
        'cancel_reason': m['cancel_reason'] or '',
        'can_request': can_request,
    }


def _todo_count(cursor, uid, now):
    cursor.execute("""
        SELECT COUNT(*) AS cnt
        FROM appo_participants p JOIN appo_meetings m ON p.meeting_id = m.id
        WHERE p.status = 'pending' AND m.status = 'scheduled' AND m.end_at > %s
          AND ((p.origin = 'request' AND m.host_id = %s) OR (p.origin = 'invite' AND p.guest_id = %s))
    """, (now, uid, uid))
    return cursor.fetchone()['cnt']


def _insert_invites(cursor, meeting, guest_ids, uid, now):
    """招待行を作る．すでに生きている参加行がある人は飛ばす．作成数を返す．"""
    guest_ids = [g for g in guest_ids if g and g != meeting['host_id']]
    valid = _active_user_ids(cursor, guest_ids)
    cursor.execute("SELECT guest_id FROM appo_participants WHERE meeting_id = %s AND status IN ('pending','accepted')",
                   (meeting['id'],))
    existing = {r['guest_id'] for r in cursor.fetchall()}
    n = 0
    for g in guest_ids:
        if g in valid and g not in existing:
            cursor.execute("""
                INSERT INTO appo_participants
                    (meeting_id, guest_id, origin, status, show_name, created_at, updated_at)
                VALUES (%s, %s, 'invite', 'pending', 0, %s, %s)
            """, (meeting['id'], g, now, now))
            existing.add(g)
            n += 1
    return n


# ---------------------------------------------------------------
# 画面
# ---------------------------------------------------------------
@appo_bp.route('/return_to_fujin')
@login_required
def return_to_fujin():
    return redirect_to_dashboard()


@appo_bp.route('/')
@login_required
def index():
    uid = _uid()
    todo = 0
    try:
        with _db() as (cursor, conn):
            todo = _todo_count(cursor, uid, get_jst_now())
    except Exception as e:
        logging.error("appo.index error: %s", e)
    return render_template('appo/index.html', todo_count=todo, late_hours=LATE_HOURS)


# ---------------------------------------------------------------
# API: 参照
# ---------------------------------------------------------------
def _visible_summaries(cursor, uid, d_from, d_to, now, extra=False):
    """d_from〜d_to（両端含む）に始まる，uid が見てよいミーティングの要約（開始順）．
    extra=True のときは，時間割の行に出す補足（メモ，URL，参加者，サブチャンネルの最新）も付ける．"""
    cursor.execute("""
        SELECT m.*, u.full_name AS host_name
        FROM appo_meetings m JOIN users u ON m.host_id = u.id
        WHERE m.status IN ('scheduled', 'canceled')
          AND m.start_at >= %s AND m.start_at < %s
        ORDER BY m.start_at, m.id
    """, (datetime.datetime.combine(d_from, datetime.time.min),
          datetime.datetime.combine(d_to + datetime.timedelta(days=1), datetime.time.min)))
    meetings = cursor.fetchall()
    parts = _participants_for(cursor, [m['id'] for m in meetings])
    out = []
    for m in meetings:
        ps = parts.get(m['id'], [])
        if _can_view(m, ps, uid):
            s = _summarize(m, ps, uid, now)
            if extra:
                s.update(_day_extras(cursor, m, ps, uid))
            out.append(s)
    return out


def _day_extras(cursor, m, ps, uid):
    """時間割の行に出す補足．表示してよい範囲は詳細画面と同じ規則に従う．"""
    is_host = m['host_id'] == uid
    mine = _my_participation(ps, uid)
    show_url = is_host or (mine is not None and mine['status'] == 'accepted')
    people = []
    if is_host:
        people = [{'name': p['guest_name'], 'status': p['status'],
                   'label': '%s（%s・%s）' % (p['guest_name'], P_ORIGIN.get(p['origin'], ''), P_STATUS.get(p['status'], ''))}
                  for p in ps if p['status'] in ('pending', 'accepted', 'canceled')]
    last = None
    msg_count = 0
    if _is_member(m, ps, uid):
        cursor.execute("""
            SELECT g.user_id, g.body, g.created_at, u.full_name
            FROM appo_messages g JOIN users u ON g.user_id = u.id
            WHERE g.meeting_id = %s ORDER BY g.created_at DESC, g.id DESC
        """, (m['id'],))
        rows = cursor.fetchall()
        msg_count = len(rows)
        if rows:
            g = rows[0]
            if g['user_id'] == m['host_id'] or g['user_id'] == uid or is_host:
                name = g['full_name']
            else:
                p = next((x for x in ps if x['guest_id'] == g['user_id']), None)
                name = g['full_name'] if (p and p['show_name']) else '参加者'
            last = {'name': name, 'body': g['body'][:80], 'at': fmt_datetime(g['created_at'])}
    return {
        'note': m['note'] or '',
        'online_url': (m['online_url'] or '') if show_url else '',
        'online_url_hidden': bool(m['online_url']) and not show_url,
        'people': people,
        'msg_count': msg_count,
        'last_message': last,
        'my_message': (mine['message'] or '') if mine else '',
    }


@appo_bp.route('/api/month', methods=['GET'])
@login_required
@api_route
def api_month():
    """月カレンダー（日曜始まり，前後の月の日を含めて週単位で埋める）．"""
    uid = _uid()
    now = get_jst_now()
    today = now.date()
    ym = (request.args.get('month') or '').strip()
    try:
        first = datetime.datetime.strptime(ym[:7], '%Y-%m').date()
    except ValueError:
        first = today.replace(day=1)
    nxt = (first.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
    last = nxt - datetime.timedelta(days=1)
    grid_start = first - datetime.timedelta(days=(first.weekday() + 1) % 7)
    grid_end = last + datetime.timedelta(days=(5 - last.weekday()) % 7)
    prev = (first - datetime.timedelta(days=1)).replace(day=1)

    with _db() as (cursor, conn):
        by_date = {}
        for s in _visible_summaries(cursor, uid, grid_start, grid_end, now):
            by_date.setdefault(s['date'], []).append(s)
        todo = _todo_count(cursor, uid, now)

    weeks, d = [], grid_start
    while d <= grid_end:
        week = []
        for _ in range(7):
            key = fmt_date(d)
            week.append({'date': key, 'day': d.day, 'in_month': d.month == first.month,
                         'is_today': d == today, 'is_past': d < today, 'items': by_date.get(key, [])})
            d += datetime.timedelta(days=1)
        weeks.append(week)
    return jsonify({
        'success': True,
        'weeks': weeks,
        'month_label': '%d年%d月' % (first.year, first.month),
        'month': first.strftime('%Y-%m'),
        'prev_month': prev.strftime('%Y-%m'),
        'next_month': nxt.strftime('%Y-%m'),
        'this_month': today.strftime('%Y-%m'),
        'today': fmt_date(today),
        'todo_count': todo,
    })


@appo_bp.route('/api/table', methods=['GET'])
@login_required
@api_route
def api_table():
    """一覧表（期間内の見えるミーティングを1行1件で返す．絞り込み・並べ替えは画面側）．"""
    uid = _uid()
    now = get_jst_now()
    today = now.date()
    d_from = parse_date(request.args.get('from', '')) or today
    d_to = parse_date(request.args.get('to', '')) or (d_from + datetime.timedelta(days=30))
    if d_to < d_from:
        d_from, d_to = d_to, d_from
    if (d_to - d_from).days > 366:
        raise ApiError('期間は1年以内にしてください．')
    with _db() as (cursor, conn):
        items = _visible_summaries(cursor, uid, d_from, d_to, now)
        todo = _todo_count(cursor, uid, now)
    return jsonify({'success': True, 'items': items, 'from': fmt_date(d_from), 'to': fmt_date(d_to),
                    'today': fmt_date(today), 'todo_count': todo})


@appo_bp.route('/api/week', methods=['GET'])
@login_required
@api_route
def api_week():
    """指定週のカレンダー（行＝ホスト，列＝日曜始まりの7日）．"""
    uid = _uid()
    now = get_jst_now()
    today = now.date()
    base = parse_date(request.args.get('start', '')) or today
    week_start = base - datetime.timedelta(days=(base.weekday() + 1) % 7)
    days = [week_start + datetime.timedelta(days=i) for i in range(7)]
    week_dates = [{
        'date': fmt_date(d),
        'label': '%d/%d' % (d.month, d.day),
        'weekday': WEEKDAYS_JA[d.weekday()],
        'is_today': d == today,
        'is_past': d < today,
    } for d in days]

    with _db() as (cursor, conn):
        rows = {}
        my_name = _user_name(cursor, uid)
        rows[uid] = {'host_id': uid, 'host_name': my_name, 'is_me': True, 'cells': {}}
        for s in _visible_summaries(cursor, uid, days[0], days[6], now):
            row = rows.setdefault(s['host_id'], {'host_id': s['host_id'], 'host_name': s['host_name'],
                                                 'is_me': False, 'cells': {}})
            row['cells'].setdefault(s['date'], []).append(s)
        todo = _todo_count(cursor, uid, now)

    others = sorted([r for k, r in rows.items() if k != uid], key=lambda r: r['host_name'] or '')
    return jsonify({
        'success': True,
        'rows': [rows[uid]] + others,
        'week_dates': week_dates,
        'week_label': '%s 〜 %s' % (days[0].strftime('%Y年%m月%d日'), days[6].strftime('%Y年%m月%d日')),
        'prev_start': fmt_date(week_start - datetime.timedelta(days=7)),
        'next_start': fmt_date(week_start + datetime.timedelta(days=7)),
        'today': fmt_date(today),
        'todo_count': todo,
    })


@appo_bp.route('/api/members', methods=['GET'])
@login_required
@api_route
def api_members():
    """招待・請求の相手に選べる会員（自分を除く）．"""
    uid = _uid()
    with _db() as (cursor, conn):
        cursor.execute("""
            SELECT id, full_name, affiliation FROM users
            WHERE is_active = 1 AND deleted_at IS NULL AND id <> %s
            ORDER BY full_name
        """, (uid,))
        members = [{'id': r['id'], 'name': r['full_name'] or '', 'affiliation': r['affiliation'] or ''}
                   for r in cursor.fetchall()]
    return jsonify({'success': True, 'members': members})


@appo_bp.route('/api/meetings/<int:meeting_id>/detail', methods=['GET'])
@login_required
@api_route
def api_meeting_detail(meeting_id):
    uid = _uid()
    now = get_jst_now()
    with _db() as (cursor, conn):
        m = _get_meeting(cursor, meeting_id)
        if not m:
            raise ApiError('ミーティングが見つかりません．', 404)
        ps = _participants_for(cursor, [meeting_id])[meeting_id]
        if not _can_view(m, ps, uid):
            raise ApiError('このミーティングは表示できません．', 403)

        s = _summarize(m, ps, uid, now)
        is_host = m['host_id'] == uid
        mine = _my_participation(ps, uid)
        alive = m['status'] == 'scheduled' and now < m['end_at']
        member = _is_member(m, ps, uid)
        show_url = is_host or (mine is not None and mine['status'] == 'accepted')

        s.update({
            'note': m['note'] or '',
            'online_url': (m['online_url'] or '') if show_url else '',
            'online_url_hidden': bool(m['online_url']) and not show_url,
            'created_at': fmt_datetime(m['created_at']),
            'is_host': is_host,
            'can_invite': is_host and alive,
            'can_edit': is_host and alive,
            'can_cancel_meeting': is_host and alive,
            'is_member': member,
        })

        participants = []
        if is_host:
            for p in ps:
                participants.append({
                    'id': p['id'],
                    'name': p['guest_name'],
                    'origin_label': P_ORIGIN.get(p['origin'], p['origin']),
                    'status': p['status'],
                    'status_label': P_STATUS.get(p['status'], p['status']),
                    'show_name': bool(p['show_name']),
                    'message': p['message'] or '',
                    'created_at': fmt_datetime(p['created_at']),
                    'decided_at': fmt_datetime(p['decided_at']),
                    'canceled_at': fmt_datetime(p['canceled_at']),
                    'can_accept': alive and p['status'] == 'pending' and p['origin'] == 'request',
                    'can_decline': alive and p['status'] == 'pending' and p['origin'] == 'request',
                })
        s['participants'] = participants

        my_row = None
        if mine:
            my_row = {
                'id': mine['id'],
                'origin': mine['origin'],
                'origin_label': P_ORIGIN.get(mine['origin'], mine['origin']),
                'status': mine['status'],
                'status_label': P_STATUS.get(mine['status'], mine['status']),
                'show_name': bool(mine['show_name']),
                'message': mine['message'] or '',
                'can_accept': alive and mine['status'] == 'pending' and mine['origin'] == 'invite',
                'can_decline': alive and mine['status'] == 'pending' and mine['origin'] == 'invite',
                'can_cancel': alive and mine['status'] in LIVE,
                'can_toggle_visibility': mine['status'] in LIVE,
            }
        s['mine'] = my_row

        messages = []
        if member:
            cursor.execute("""
                SELECT g.id, g.user_id, g.body, g.created_at, u.full_name
                FROM appo_messages g JOIN users u ON g.user_id = u.id
                WHERE g.meeting_id = %s ORDER BY g.created_at, g.id LIMIT 300
            """, (meeting_id,))
            guest_rows = {p['guest_id']: p for p in ps}
            for g in cursor.fetchall():
                if g['user_id'] == m['host_id']:
                    name = g['full_name'] + '（ホスト）'
                elif g['user_id'] == uid or is_host:
                    name = g['full_name']
                else:
                    p = guest_rows.get(g['user_id'])
                    name = g['full_name'] if (p and p['show_name']) else '参加者（名前非表示）'
                messages.append({'id': g['id'], 'name': name, 'body': g['body'],
                                 'created_at': fmt_datetime(g['created_at']),
                                 'is_mine': g['user_id'] == uid})
        s['messages'] = messages
        s['can_post'] = member and m['status'] != 'void'
    return jsonify({'success': True, 'meeting': s})


@appo_bp.route('/api/mine', methods=['GET'])
@login_required
@api_route
def api_mine():
    """マイあぽ：対応が必要／ホスト：招待中／ホスト：記録／ゲストとして（いずれも日付の新しい順）．"""
    uid = _uid()
    now = get_jst_now()
    with _db() as (cursor, conn):
        cursor.execute("""
            SELECT m.*, u.full_name AS host_name
            FROM appo_meetings m JOIN users u ON m.host_id = u.id
            WHERE m.host_id = %s
               OR m.id IN (SELECT meeting_id FROM appo_participants WHERE guest_id = %s)
            ORDER BY m.start_at DESC, m.id DESC
        """, (uid, uid))
        meetings = cursor.fetchall()
        parts = _participants_for(cursor, [m['id'] for m in meetings])

        todo, inviting, records, as_guest = [], [], [], []
        for m in meetings:
            ps = parts.get(m['id'], [])
            s = _summarize(m, ps, uid, now)
            alive = m['status'] == 'scheduled' and now < m['end_at']
            if m['host_id'] == uid:
                s['people'] = [{'name': p['guest_name'], 'origin_label': P_ORIGIN.get(p['origin'], p['origin']),
                                'status': p['status'], 'status_label': P_STATUS.get(p['status'], p['status'])}
                               for p in ps]
                s['origin_label'] = '請求を受けた' if m['origin'] == 'request' else '自分で出した'
                for p in ps:
                    if alive and p['status'] == 'pending' and p['origin'] == 'request':
                        todo.append({'meeting': s, 'kind': '請求が届いています',
                                     'from_name': p['guest_name'], 'message': p['message'] or ''})
                waiting = [p for p in ps if p['status'] == 'pending' and p['origin'] == 'invite']
                if alive and waiting:
                    s['waiting_names'] = [p['guest_name'] for p in waiting]
                    inviting.append(s)
                records.append(s)
            else:
                mine = _my_participation(ps, uid)
                if alive and mine and mine['status'] == 'pending' and mine['origin'] == 'invite':
                    todo.append({'meeting': s, 'kind': '招待が届いています',
                                 'from_name': m['host_name'], 'message': ''})
                as_guest.append(s)
    todo.sort(key=lambda x: (x['meeting']['date'], x['meeting']['start_time']))
    return jsonify({'success': True, 'todo': todo, 'inviting': inviting,
                    'records': records[:200], 'as_guest': as_guest[:100],
                    'todo_count': len(todo)})


@appo_bp.route('/api/day', methods=['GET'])
@login_required
@api_route
def api_day():
    """当日の時間割：週・月カレンダーと同じ範囲（見てよいミーティング）を，補足つきで返す．"""
    uid = _uid()
    now = get_jst_now()
    today = now.date()
    day = parse_date(request.args.get('date', '')) or today
    with _db() as (cursor, conn):
        items = _visible_summaries(cursor, uid, day, day, now, extra=True)
        todo = _todo_count(cursor, uid, now)
    return jsonify({
        'success': True,
        'date': fmt_date(day),
        'label': '%d年%d月%d日（%s）' % (day.year, day.month, day.day, WEEKDAYS_JA[day.weekday()]),
        'is_today': day == today,
        'is_past': day < today,
        'now_time': fmt_time(now) if day == today else '',
        'prev_date': fmt_date(day - datetime.timedelta(days=1)),
        'next_date': fmt_date(day + datetime.timedelta(days=1)),
        'today': fmt_date(today),
        'items': items,
        'todo_count': todo,
    })


# ---------------------------------------------------------------
# マイ設定（ユーザごとの表示設定）
# ---------------------------------------------------------------
ORDER_MODES = {'name': '名前順', 'affiliation': '所属順', 'slots_first': 'その日に枠のある人を先に',
               'manual': '自分で決めた順'}
UNSET_POLICIES = {'auto': 'その日に枠があれば表示', 'hide': '表示しない'}
DEFAULT_PREFS = {'order_mode': 'slots_first', 'manual_order': [], 'show': {}, 'unset_policy': 'auto'}


def _clean_prefs(raw, base=None):
    """保存・読み出しの両方で通す．知らないキーや型の違う値は捨て，base（既定は初期設定）の値を残す．"""
    p = dict(base or DEFAULT_PREFS)
    p['manual_order'] = list(p['manual_order'])
    p['show'] = dict(p['show'])
    if not isinstance(raw, dict):
        return p
    if raw.get('order_mode') in ORDER_MODES:
        p['order_mode'] = raw['order_mode']
    if raw.get('unset_policy') in UNSET_POLICIES:
        p['unset_policy'] = raw['unset_policy']
    if isinstance(raw.get('manual_order'), list):
        order, seen = [], set()
        for v in raw['manual_order'][:5000]:
            i = _int(v)
            if i and i not in seen:
                seen.add(i)
                order.append(i)
        p['manual_order'] = order
    if isinstance(raw.get('show'), dict):
        show = {}
        for k, v in list(raw['show'].items())[:5000]:
            i = _int(k)
            if i:
                show[str(i)] = bool(v)
        p['show'] = show
    return p


def _load_prefs(cursor, uid):
    cursor.execute("SELECT prefs_json FROM appo_user_prefs WHERE user_id = %s", (uid,))
    row = cursor.fetchone()
    if not row or not row['prefs_json']:
        return dict(DEFAULT_PREFS)
    try:
        return _clean_prefs(json.loads(row['prefs_json']))
    except ValueError:
        return dict(DEFAULT_PREFS)


@appo_bp.route('/api/prefs', methods=['GET'])
@login_required
@api_route
def api_prefs_get():
    """マイ設定の読み出し（設定と，並べ替え用の会員一覧）．"""
    uid = _uid()
    with _db() as (cursor, conn):
        prefs = _load_prefs(cursor, uid)
        cursor.execute("""
            SELECT id, full_name, affiliation FROM users
            WHERE is_active = 1 AND deleted_at IS NULL AND id <> %s
            ORDER BY full_name
        """, (uid,))
        accounts = [{'id': r['id'], 'name': r['full_name'] or '', 'affiliation': r['affiliation'] or ''}
                    for r in cursor.fetchall()]
    modes = [[k, ORDER_MODES[k]] for k in ('manual', 'name', 'affiliation', 'slots_first')]
    policies = [[k, UNSET_POLICIES[k]] for k in ('auto', 'hide')]
    return jsonify({'success': True, 'prefs': prefs, 'accounts': accounts,
                    'order_modes': modes, 'unset_policies': policies})


@appo_bp.route('/api/prefs', methods=['POST'])
@login_required
@api_route
def api_prefs_save():
    """マイ設定の保存．送られたキーだけを今の設定に重ねる．"""
    uid = _uid()
    data = request.json or {}
    now = get_jst_now()
    with _db() as (cursor, conn):
        cur = _load_prefs(cursor, uid)
        merged = dict(cur)
        for k in DEFAULT_PREFS:
            if k in data:
                merged[k] = data[k]
        if isinstance(data.get('show_patch'), dict):
            show = dict(cur['show'])
            show.update(data['show_patch'])
            merged['show'] = show
        prefs = _clean_prefs(merged, base=cur)
        body = json.dumps(prefs, ensure_ascii=False)
        cursor.execute("""
            INSERT INTO appo_user_prefs (user_id, prefs_json, updated_at) VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE prefs_json = VALUES(prefs_json), updated_at = VALUES(updated_at)
        """, (uid, body, now))
        conn.commit()
    return jsonify({'success': True, 'prefs': prefs, 'message': '設定を保存しました．'})


# ---------------------------------------------------------------
# API: ホストの操作（枠を出す・招待・編集・キャンセル）
# ---------------------------------------------------------------
@appo_bp.route('/api/meetings', methods=['POST'])
@login_required
@api_route
def api_meeting_create():
    """ホストが枠（オフィスアワー）を出す／ゲストを招待する．"""
    uid = _uid()
    data = request.json or {}
    now = get_jst_now()
    start_at, end_at = _parse_span(data)
    mode, location, online_url = _parse_place(data)
    kind = data.get('kind') or 'exclusive'
    if kind not in KINDS:
        raise ApiError('ミーティングの種別が正しくありません．')
    capacity = max(0, _int(data.get('capacity'), 0) or 0) if kind == 'open' else 1
    auto_accept = 1 if (kind == 'open' and data.get('auto_accept')) else 0
    listed = 1 if data.get('listed', True) else 0
    title = _text(data, 'title', MAX_TITLE) or ('オフィスアワー' if listed else 'ミーティング')
    note = _text(data, 'note', MAX_TEXT * 2)
    invitees = sorted({g for g in (_int(v) for v in (data.get('invitee_ids') or [])) if g and g != uid})
    if not listed and not invitees:
        raise ApiError('会員に公開しない場合は，招待する人を1人以上選んでください．')

    with _db() as (cursor, conn):
        if _busy(cursor, uid, start_at, end_at):
            raise ApiError('その時間帯にはあなたの予定（ホストまたは成立済みの参加）がすでにあります．')
        cursor.execute("""
            INSERT INTO appo_meetings
                (host_id, created_by, origin, title, note, start_at, end_at, mode, location, online_url,
                 kind, capacity, auto_accept, listed, status, created_at, updated_at)
            VALUES (%s, %s, 'host', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'scheduled', %s, %s)
        """, (uid, uid, title, note, start_at, end_at, mode, location, online_url,
              kind, capacity, auto_accept, listed, now, now))
        mid = cursor.lastrowid
        n = _insert_invites(cursor, {'id': mid, 'host_id': uid}, invitees, uid, now)
        conn.commit()
    msg = '枠を出しました．' if listed else 'ミーティングを作成しました．'
    if n:
        msg += '%d名を招待しました（相手の承諾で成立します）．' % n
    return jsonify({'success': True, 'id': mid, 'message': msg})


@appo_bp.route('/api/meetings/<int:meeting_id>/invite', methods=['POST'])
@login_required
@api_route
def api_meeting_invite(meeting_id):
    uid = _uid()
    data = request.json or {}
    now = get_jst_now()
    guest_ids = sorted({g for g in (_int(v) for v in (data.get('guest_ids') or [])) if g})
    if not guest_ids:
        raise ApiError('招待する人を選んでください．')
    with _db() as (cursor, conn):
        m = _get_meeting(cursor, meeting_id, for_update=True)
        if not m:
            raise ApiError('ミーティングが見つかりません．', 404)
        if m['host_id'] != uid:
            raise ApiError('招待できるのはホストだけです．', 403)
        if m['status'] != 'scheduled' or now >= m['end_at']:
            raise ApiError('このミーティングには招待できません．')
        if m['kind'] == 'exclusive':
            cursor.execute("SELECT COUNT(*) AS cnt FROM appo_participants WHERE meeting_id = %s AND status = 'accepted'",
                           (meeting_id,))
            if cursor.fetchone()['cnt']:
                raise ApiError('排他的ミーティングはすでに成立しています．')
        n = _insert_invites(cursor, m, guest_ids, uid, now)
        cursor.execute("UPDATE appo_meetings SET updated_at = %s WHERE id = %s", (now, meeting_id))
        conn.commit()
    if not n:
        raise ApiError('新たに招待した人はいません（すでに招待・参加中の人は除きます）．')
    return jsonify({'success': True, 'message': '%d名を招待しました．' % n})


@appo_bp.route('/api/meetings/<int:meeting_id>/update', methods=['POST'])
@login_required
@api_route
def api_meeting_update(meeting_id):
    """ホストが題目・場所・URL・メモを直す（日時と種別は変えない）．"""
    uid = _uid()
    data = request.json or {}
    now = get_jst_now()
    mode, location, online_url = _parse_place(data)
    title = _text(data, 'title', MAX_TITLE)
    note = _text(data, 'note', MAX_TEXT * 2)
    with _db() as (cursor, conn):
        m = _get_meeting(cursor, meeting_id, for_update=True)
        if not m:
            raise ApiError('ミーティングが見つかりません．', 404)
        if m['host_id'] != uid:
            raise ApiError('編集できるのはホストだけです．', 403)
        if m['status'] != 'scheduled' or now >= m['end_at']:
            raise ApiError('このミーティングは編集できません．')
        cursor.execute("""
            UPDATE appo_meetings SET title = %s, mode = %s, location = %s, online_url = %s, note = %s, updated_at = %s
            WHERE id = %s
        """, (title or m['title'], mode, location, online_url, note, now, meeting_id))
        conn.commit()
    return jsonify({'success': True, 'message': '内容を更新しました．'})


@appo_bp.route('/api/meetings/<int:meeting_id>/cancel', methods=['POST'])
@login_required
@api_route
def api_meeting_cancel(meeting_id):
    uid = _uid()
    data = request.json or {}
    now = get_jst_now()
    reason = _text(data, 'reason', 500)
    with _db() as (cursor, conn):
        m = _get_meeting(cursor, meeting_id, for_update=True)
        if not m:
            raise ApiError('ミーティングが見つかりません．', 404)
        if m['host_id'] != uid:
            raise ApiError('ミーティングをキャンセルできるのはホストだけです．', 403)
        if m['status'] != 'scheduled' or now >= m['end_at']:
            raise ApiError('このミーティングはキャンセルできません．')
        cursor.execute("""
            UPDATE appo_meetings SET status = 'canceled', canceled_at = %s, cancel_reason = %s, updated_at = %s
            WHERE id = %s
        """, (now, reason, now, meeting_id))
        conn.commit()
    late = (m['start_at'] - now) < LATE
    return jsonify({'success': True,
                    'message': 'ミーティングをキャンセルしました．' + ('（直前キャンセルとして表示されます）' if late else '')})


# ---------------------------------------------------------------
# API: ゲストの操作（請求・枠への申し込み）
# ---------------------------------------------------------------
@appo_bp.route('/api/proposals', methods=['POST'])
@login_required
@api_route
def api_proposal_create():
    """ゲストが日時を指定してホストにミーティングを請求する（ホストの承諾で成立）．"""
    uid = _uid()
    data = request.json or {}
    now = get_jst_now()
    host_id = _int(data.get('host_id'))
    if not host_id or host_id == uid:
        raise ApiError('請求する相手を選んでください．')
    start_at, end_at = _parse_span(data)
    mode, location, online_url = _parse_place(data)
    kind = data.get('kind') or 'exclusive'
    if kind not in KINDS:
        raise ApiError('ミーティングの種別が正しくありません．')
    title = _text(data, 'title', MAX_TITLE) or '面会のお願い'
    message = _text(data, 'message', MAX_TEXT)
    show_name = 1 if data.get('show_name') else 0

    with _db() as (cursor, conn):
        if host_id not in _active_user_ids(cursor, [host_id]):
            raise ApiError('相手が見つかりません．', 404)
        if _busy(cursor, host_id, start_at, end_at):
            raise ApiError('その時間帯には相手に予定または公開中の枠があります．カレンダーで空き枠を確かめてください．')
        if _busy(cursor, uid, start_at, end_at):
            raise ApiError('その時間帯にはあなたの予定がすでにあります．')
        cursor.execute("""
            INSERT INTO appo_meetings
                (host_id, created_by, origin, title, note, start_at, end_at, mode, location, online_url,
                 kind, capacity, auto_accept, listed, status, created_at, updated_at)
            VALUES (%s, %s, 'request', %s, '', %s, %s, %s, %s, %s, %s, %s, 0, 0, 'scheduled', %s, %s)
        """, (host_id, uid, title, start_at, end_at, mode, location, online_url,
              kind, 1 if kind == 'exclusive' else 0, now, now))
        mid = cursor.lastrowid
        cursor.execute("""
            INSERT INTO appo_participants
                (meeting_id, guest_id, origin, status, show_name, message, created_at, updated_at)
            VALUES (%s, %s, 'request', 'pending', %s, %s, %s, %s)
        """, (mid, uid, show_name, message, now, now))
        conn.commit()
    return jsonify({'success': True, 'id': mid, 'message': '請求しました（相手の承諾で成立します）．'})


@appo_bp.route('/api/meetings/<int:meeting_id>/request', methods=['POST'])
@login_required
@api_route
def api_meeting_request(meeting_id):
    """公開中の枠に請求する（排他的＝面会の請求，非排他的＝懇談への参加申し込み）．"""
    uid = _uid()
    data = request.json or {}
    now = get_jst_now()
    message = _text(data, 'message', MAX_TEXT)
    show_name = 1 if data.get('show_name') else 0
    with _db() as (cursor, conn):
        m = _get_meeting(cursor, meeting_id, for_update=True)
        if not m:
            raise ApiError('ミーティングが見つかりません．', 404)
        ps = _participants_for(cursor, [meeting_id])[meeting_id]
        s = _summarize(m, ps, uid, now)
        if not s['can_request']:
            raise ApiError('この枠には請求できません（成立済み・満席・終了・すでに請求中のいずれかです）．')
        if _busy(cursor, uid, m['start_at'], m['end_at'], exclude_id=meeting_id):
            raise ApiError('その時間帯にはあなたの予定がすでにあります．')
        status = 'accepted' if (m['kind'] == 'open' and m['auto_accept']) else 'pending'
        cursor.execute("""
            INSERT INTO appo_participants
                (meeting_id, guest_id, origin, status, show_name, message, created_at, decided_at, updated_at)
            VALUES (%s, %s, 'request', %s, %s, %s, %s, %s, %s)
        """, (meeting_id, uid, status, show_name, message, now, now if status == 'accepted' else None, now))
        conn.commit()
    msg = '参加が決まりました．' if status == 'accepted' else '請求しました（ホストの承諾で成立します）．'
    return jsonify({'success': True, 'message': msg})


# ---------------------------------------------------------------
# API: 参加行の操作（承諾・辞退・取り下げ・表示切替）
# ---------------------------------------------------------------
def _load_participant(cursor, participant_id):
    cursor.execute("SELECT * FROM appo_participants WHERE id = %s", (participant_id,))
    p = cursor.fetchone()
    if not p:
        raise ApiError('参加の記録が見つかりません．', 404)
    m = _get_meeting(cursor, p['meeting_id'], for_update=True)
    cursor.execute("SELECT * FROM appo_participants WHERE id = %s", (participant_id,))
    p = cursor.fetchone()
    return p, m


def _decider(p, m):
    """承諾・辞退を決める人：請求ならホスト，招待ならゲスト．"""
    return m['host_id'] if p['origin'] == 'request' else p['guest_id']


@appo_bp.route('/api/participants/<int:participant_id>/accept', methods=['POST'])
@login_required
@api_route
def api_participant_accept(participant_id):
    uid = _uid()
    data = request.json or {}
    now = get_jst_now()
    with _db() as (cursor, conn):
        p, m = _load_participant(cursor, participant_id)
        if _decider(p, m) != uid:
            raise ApiError('この承諾はあなたの操作ではありません．', 403)
        if p['status'] != 'pending':
            raise ApiError('承諾待ちではありません．')
        if m['status'] != 'scheduled' or now >= m['end_at']:
            raise ApiError('このミーティングはすでに終了またはキャンセルされています．')
        cursor.execute("SELECT COUNT(*) AS cnt FROM appo_participants WHERE meeting_id = %s AND status = 'accepted'",
                       (m['id'],))
        accepted = cursor.fetchone()['cnt']
        if m['kind'] == 'exclusive' and accepted:
            raise ApiError('排他的ミーティングはすでに別の人と成立しています．')
        if m['kind'] == 'open' and m['capacity'] and accepted >= m['capacity']:
            raise ApiError('定員に達しています．')
        if _busy(cursor, p['guest_id'], m['start_at'], m['end_at'], exclude_id=m['id']):
            raise ApiError('ゲストはその時間帯にすでに別の予定が成立しています．')
        if m['origin'] == 'request' and not m['listed'] and _busy(cursor, m['host_id'], m['start_at'], m['end_at'],
                                                                 exclude_id=m['id']):
            raise ApiError('ホストはその時間帯にすでに予定があります．')

        show_name = p['show_name']
        if uid == p['guest_id'] and 'show_name' in data:
            show_name = 1 if data.get('show_name') else 0
        cursor.execute("""
            UPDATE appo_participants SET status = 'accepted', show_name = %s, decided_at = %s, decided_by = %s, updated_at = %s
            WHERE id = %s
        """, (show_name, now, uid, now, participant_id))
        if m['origin'] == 'request' and not m['listed']:
            cursor.execute("UPDATE appo_meetings SET listed = 1, updated_at = %s WHERE id = %s", (now, m['id']))
        auto_declined = 0
        if m['kind'] == 'exclusive':
            cursor.execute("""
                UPDATE appo_participants SET status = 'declined', decided_at = %s, decided_by = NULL, updated_at = %s
                WHERE meeting_id = %s AND status = 'pending' AND id <> %s
            """, (now, now, m['id'], participant_id))
            auto_declined = cursor.rowcount
        conn.commit()
    msg = 'ミーティングが成立しました．'
    if auto_declined:
        msg += 'ほかの承諾待ち%d件は不成立になりました．' % auto_declined
    return jsonify({'success': True, 'message': msg})


@appo_bp.route('/api/participants/<int:participant_id>/decline', methods=['POST'])
@login_required
@api_route
def api_participant_decline(participant_id):
    uid = _uid()
    now = get_jst_now()
    with _db() as (cursor, conn):
        p, m = _load_participant(cursor, participant_id)
        if _decider(p, m) != uid:
            raise ApiError('この操作はあなたの権限ではありません．', 403)
        if p['status'] != 'pending':
            raise ApiError('承諾待ちではありません．')
        cursor.execute("""
            UPDATE appo_participants SET status = 'declined', decided_at = %s, decided_by = %s, updated_at = %s
            WHERE id = %s
        """, (now, uid, now, participant_id))
        if m['origin'] == 'request' and not m['listed']:
            cursor.execute("UPDATE appo_meetings SET status = 'void', updated_at = %s WHERE id = %s", (now, m['id']))
        conn.commit()
    return jsonify({'success': True, 'message': 'お断りしました．'})


@appo_bp.route('/api/participants/<int:participant_id>/cancel', methods=['POST'])
@login_required
@api_route
def api_participant_cancel(participant_id):
    """ゲスト本人が請求の取り下げ・参加のキャンセルをする．"""
    uid = _uid()
    now = get_jst_now()
    with _db() as (cursor, conn):
        p, m = _load_participant(cursor, participant_id)
        if p['guest_id'] != uid:
            raise ApiError('取り下げ・キャンセルできるのは本人だけです．', 403)
        if p['status'] not in LIVE:
            raise ApiError('すでに不成立またはキャンセル済みです．')
        if now >= m['end_at']:
            raise ApiError('終了したミーティングはキャンセルできません．')
        cursor.execute("""
            UPDATE appo_participants SET status = 'canceled', canceled_at = %s, updated_at = %s WHERE id = %s
        """, (now, now, participant_id))
        if m['origin'] == 'request' and not m['listed'] and m['status'] == 'scheduled':
            cursor.execute("UPDATE appo_meetings SET status = 'void', updated_at = %s WHERE id = %s", (now, m['id']))
        conn.commit()
    late = p['status'] == 'accepted' and (m['start_at'] - now) < LATE
    word = '取り下げました．' if p['status'] == 'pending' else 'キャンセルしました．'
    return jsonify({'success': True, 'message': word + ('（直前キャンセルとして表示されます）' if late else '')})


@appo_bp.route('/api/participants/<int:participant_id>/visibility', methods=['POST'])
@login_required
@api_route
def api_participant_visibility(participant_id):
    """ゲストが，自分の名前をほかの会員に表示するかを決める．"""
    uid = _uid()
    data = request.json or {}
    now = get_jst_now()
    show = 1 if data.get('show_name') else 0
    with _db() as (cursor, conn):
        cursor.execute("SELECT guest_id FROM appo_participants WHERE id = %s", (participant_id,))
        p = cursor.fetchone()
        if not p:
            raise ApiError('参加の記録が見つかりません．', 404)
        if p['guest_id'] != uid:
            raise ApiError('表示を決められるのは本人だけです．', 403)
        cursor.execute("UPDATE appo_participants SET show_name = %s, updated_at = %s WHERE id = %s",
                       (show, now, participant_id))
        conn.commit()
    return jsonify({'success': True,
                    'message': '名前をほかの会員にも表示します．' if show else '名前はホストだけに表示します．'})


# ---------------------------------------------------------------
# API: サブチャンネル
# ---------------------------------------------------------------
@appo_bp.route('/api/meetings/<int:meeting_id>/messages', methods=['POST'])
@login_required
@api_route
def api_message_post(meeting_id):
    uid = _uid()
    data = request.json or {}
    now = get_jst_now()
    body = _text(data, 'body', MAX_TEXT)
    if not body:
        raise ApiError('メッセージを入力してください．')
    with _db() as (cursor, conn):
        m = _get_meeting(cursor, meeting_id)
        if not m:
            raise ApiError('ミーティングが見つかりません．', 404)
        ps = _participants_for(cursor, [meeting_id])[meeting_id]
        if not _is_member(m, ps, uid) or m['status'] == 'void':
            raise ApiError('このサブチャンネルには書き込めません．', 403)
        cursor.execute("INSERT INTO appo_messages (meeting_id, user_id, body, created_at) VALUES (%s, %s, %s, %s)",
                       (meeting_id, uid, body, now))
        conn.commit()
    return jsonify({'success': True, 'message': '書き込みました．'})
