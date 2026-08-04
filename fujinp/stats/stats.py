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

import os
import time
import datetime
from pytz import timezone
from flask import Blueprint, request, jsonify, session, render_template
from decorators import login_required
from db import get_db_cursor
from config import Config
from auth import redirect_to_dashboard


# Blueprint設定
stats_bp = Blueprint('stats', __name__, template_folder='stats_templates')

# ==========================================================================
# アプリ規模スキャン関連
# ==========================================================================

# スキャン対象の拡張子
CODE_EXTENSIONS = ('.py', '.html')

# スキャン時に除外するディレクトリ名
EXCLUDED_DIR_NAMES = {
    '__pycache__', '.git', '.idea', '.vscode', 'node_modules',
    '.mypy_cache', '.pytest_cache', '.venv', 'venv', 'env',
}

# FUJINP プラットフォームのルートディレクトリ
FUJINP_ROOT = os.path.join(Config.UPLOAD_BASE_DIR, 'fujinp')
ADMIN_HOME = Config.UPLOAD_BASE_DIR
ADMIN_TEMPLATES = os.path.join(Config.UPLOAD_BASE_DIR, 'templates')

# ==========================================================================
# 重い集計の簡易キャッシュ
# ==========================================================================
# アプリ規模スキャン（全ファイルの行数カウント）と DBテーブル統計（全テーブルの
# COUNT(*)）は、ページを開くたびに実行するとコストが大きい。プロセス内で
# 一定時間だけ結果を使い回す。0 を設定するとキャッシュ無効（毎回再集計）。
STATS_CACHE_TTL_SECONDS = 300

_STATS_CACHE = {}


def _cached(key, producer, ttl=None):
    """
    プロセス内の簡易TTLキャッシュ。
    ttl <= 0 のときはキャッシュせず producer() をそのまま返す。
    ワーカープロセスごとに独立したキャッシュになる（共有はしない）。
    """
    if ttl is None:
        ttl = STATS_CACHE_TTL_SECONDS
    if ttl <= 0:
        return producer()

    now = time.monotonic()
    entry = _STATS_CACHE.get(key)
    if entry is not None and (now - entry[0]) < ttl:
        return entry[1]

    value = producer()
    _STATS_CACHE[key] = (now, value)
    return value


def _count_lines_safe(file_path):
    """ファイルの行数を安全にカウント（読めなければ0）"""
    try:
        with open(file_path, 'rb') as f:
            # バイト単位で改行を数える（エンコーディング不問）
            count = 0
            for chunk in iter(lambda: f.read(65536), b''):
                count += chunk.count(b'\n')
            # 最終行に改行がない場合のために、ファイルサイズ>0なら最低1行
            if count == 0:
                f.seek(0, os.SEEK_END)
                if f.tell() > 0:
                    count = 1
            else:
                # 末尾改行がない場合の補正
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b'\n':
                    count += 1
            return count
    except (OSError, IOError):
        return 0


def _scan_directory_recursive(root_path):
    """
    ディレクトリを再帰的に走査し、.pyと.htmlファイルの行数を集計。
    戻り値: {'py_lines': int, 'html_lines': int, 'py_files': int, 'html_files': int}
    """
    result = {'py_lines': 0, 'html_lines': 0, 'py_files': 0, 'html_files': 0}

    if not os.path.isdir(root_path):
        return result

    try:
        for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
            # 除外ディレクトリと隠しディレクトリをスキップ
            dirnames[:] = [
                d for d in dirnames
                if d not in EXCLUDED_DIR_NAMES and not d.startswith('.')
            ]

            for fname in filenames:
                if fname.startswith('.'):
                    continue
                fpath = os.path.join(dirpath, fname)
                if not os.path.isfile(fpath):
                    continue

                if fname.endswith('.py'):
                    lines = _count_lines_safe(fpath)
                    result['py_lines'] += lines
                    result['py_files'] += 1
                elif fname.endswith('.html'):
                    lines = _count_lines_safe(fpath)
                    result['html_lines'] += lines
                    result['html_files'] += 1
    except OSError:
        pass

    return result


def get_app_sizes():
    """
    /home/nishida/fujinp 配下の各サブディレクトリを「アプリ」として、
    .pyと.htmlの行数を集計してリストで返す（行数降順）。
    """
    apps = []

    if not os.path.isdir(FUJINP_ROOT):
        return apps

    try:
        entries = sorted(os.listdir(FUJINP_ROOT))
    except OSError:
        return apps

    for name in entries:
        # 隠しディレクトリと除外ディレクトリをスキップ
        if name.startswith('.') or name in EXCLUDED_DIR_NAMES:
            continue

        app_path = os.path.join(FUJINP_ROOT, name)
        if not os.path.isdir(app_path):
            continue

        stats = _scan_directory_recursive(app_path)
        total_lines = stats['py_lines'] + stats['html_lines']

        # 1行も無いアプリ（誤って混入したディレクトリ等）はスキップ
        if total_lines == 0:
            continue

        apps.append({
            'name': name,
            'py_lines': stats['py_lines'],
            'html_lines': stats['html_lines'],
            'total_lines': total_lines,
            'py_files': stats['py_files'],
            'html_files': stats['html_files'],
            'total_files': stats['py_files'] + stats['html_files'],
        })

    # 総行数で降順ソート
    apps.sort(key=lambda a: a['total_lines'], reverse=True)
    return apps


def get_admin_files_size():
    """
    Admin用：/home/nishida/ 直下と /home/nishida/templates/ 配下の
    .py/.htmlファイル一覧（行数降順）。
    戻り値: {'home_files': [...], 'templates_files': [...], 'totals': {...}}
    """
    home_files = []
    templates_files = []

    # /home/nishida/ 直下のファイル（再帰しない）
    if os.path.isdir(ADMIN_HOME):
        try:
            for fname in sorted(os.listdir(ADMIN_HOME)):
                if fname.startswith('.'):
                    continue
                fpath = os.path.join(ADMIN_HOME, fname)
                if not os.path.isfile(fpath):
                    continue
                if not fname.endswith(CODE_EXTENSIONS):
                    continue
                lines = _count_lines_safe(fpath)
                home_files.append({
                    'name': fname,
                    'lines': lines,
                    'ext': '.py' if fname.endswith('.py') else '.html',
                })
        except OSError:
            pass

    # /home/nishida/templates/ 配下（再帰）
    if os.path.isdir(ADMIN_TEMPLATES):
        try:
            for dirpath, dirnames, filenames in os.walk(ADMIN_TEMPLATES, followlinks=False):
                dirnames[:] = [
                    d for d in dirnames
                    if d not in EXCLUDED_DIR_NAMES and not d.startswith('.')
                ]
                for fname in filenames:
                    if fname.startswith('.'):
                        continue
                    if not fname.endswith(CODE_EXTENSIONS):
                        continue
                    fpath = os.path.join(dirpath, fname)
                    if not os.path.isfile(fpath):
                        continue
                    # ADMIN_TEMPLATES からの相対パスで表示
                    rel = os.path.relpath(fpath, ADMIN_TEMPLATES)
                    lines = _count_lines_safe(fpath)
                    templates_files.append({
                        'name': rel,
                        'lines': lines,
                        'ext': '.py' if fname.endswith('.py') else '.html',
                    })
        except OSError:
            pass

    home_files.sort(key=lambda f: f['lines'], reverse=True)
    templates_files.sort(key=lambda f: f['lines'], reverse=True)

    totals = {
        'home_total_lines': sum(f['lines'] for f in home_files),
        'home_total_files': len(home_files),
        'templates_total_lines': sum(f['lines'] for f in templates_files),
        'templates_total_files': len(templates_files),
    }

    return {
        'home_files': home_files,
        'templates_files': templates_files,
        'totals': totals,
    }


# ==========================================================================
# MySQL テーブル統計関連
# ==========================================================================

# 統計対象のデータベース定義
# (表示用ラベル, db.py の database 引数, 実スキーマ名)
DB_STAT_TARGETS = [
    ('default', 'default', Config.DB_DEFAULT),
    ('fujinp',  'fujinp',  Config.DB_FUJINP),
]


def _collect_db_stats_one(database_key, schema_name):
    """
    1データベース分のテーブル一覧＋正確な行数を、**1接続を使い回して**取得する。
    以前はテーブル1本ごとに get_db_cursor() を開いていたため、
    テーブル数に比例して接続確立が発生していた（2026-07-26 改修）。
    戻り値: (tables, total_rows)
    """
    tables_with_count = []
    total_rows = 0

    try:
        with get_db_cursor(database=database_key) as (cursor, conn):
            cursor.execute("""
                SELECT TABLE_NAME, ENGINE, DATA_LENGTH, INDEX_LENGTH
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = %s
                  AND TABLE_TYPE = 'BASE TABLE'
                ORDER BY TABLE_NAME
            """, (schema_name,))
            tables_meta = cursor.fetchall()

            for row in tables_meta:
                table_name = row['TABLE_NAME']
                data_bytes = row['DATA_LENGTH'] or 0
                index_bytes = row['INDEX_LENGTH'] or 0

                # テーブル名はバックティックでクオート
                #（バックティック文字自体は念のため除去）
                safe_name = table_name.replace('`', '')
                try:
                    cursor.execute(f"SELECT COUNT(*) AS cnt FROM `{safe_name}`")
                    result = cursor.fetchone()
                    row_count = int(result['cnt']) if result else 0
                    count_failed = False
                except Exception as e:
                    print(f"[stats] COUNT(*) failed for {database_key}.{table_name}: {e}")
                    row_count = 0
                    count_failed = True

                total_rows += row_count
                tables_with_count.append({
                    'name': table_name,
                    'engine': row['ENGINE'] or '',
                    'row_count': row_count,
                    'count_failed': count_failed,
                    'data_bytes': data_bytes,
                    'index_bytes': index_bytes,
                    'total_bytes': data_bytes + index_bytes,
                })
    except Exception as e:
        # 接続失敗・権限不足等は空で返す（ページ全体を落とさない）
        print(f"[stats] Failed to collect stats for {schema_name}: {e}")

    return tables_with_count, total_rows


def get_database_stats():
    """
    各データベースのテーブル一覧と正確な行数を取得。
    サイズの表示用文字列（*_display）もここで付与して返す。
    戻り値: [{'label': str, 'schema': str, 'tables': [...], 'totals': {...}}, ...]
    """
    results = []

    for label, db_key, schema_name in DB_STAT_TARGETS:
        tables_with_count, total_rows = _collect_db_stats_one(db_key, schema_name)

        # 行数降順でソート
        tables_with_count.sort(key=lambda t: t['row_count'], reverse=True)

        total_bytes = 0
        for t in tables_with_count:
            total_bytes += t['total_bytes']
            t['total_bytes_display'] = _format_bytes(t['total_bytes'])

        results.append({
            'label': label,
            'schema': schema_name,
            'tables': tables_with_count,
            'totals': {
                'table_count': len(tables_with_count),
                'total_rows': total_rows,
                'total_bytes': total_bytes,
                'total_bytes_display': _format_bytes(total_bytes),
            },
        })

    return results


def _format_bytes(num_bytes):
    """バイト数を人間可読な形式に（テンプレート用ヘルパーだが、Pythonで整形して渡す）"""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    elif num_bytes < 1024 ** 2:
        return f"{num_bytes / 1024:.1f} KB"
    elif num_bytes < 1024 ** 3:
        return f"{num_bytes / (1024 ** 2):.1f} MB"
    else:
        return f"{num_bytes / (1024 ** 3):.2f} GB"

# タイムゾーン設定
JST = timezone('Asia/Tokyo')

def get_jst_now():
    """現在の日時をJSTで取得（naive datetime）"""
    return datetime.datetime.now(JST).replace(tzinfo=None)

def record_view_event(user_id):
    """statsページ閲覧イベントを記録"""
    jst_now = get_jst_now()
    ip_address = request.remote_addr

    with get_db_cursor() as (cursor, conn):
        cursor.execute("""
            INSERT INTO user_events (user_id, event_type, event_data, occurred_at, ip_address)
            VALUES (%s, %s, %s, %s, %s)
        """, (user_id, 'view_stats', None, jst_now, ip_address))
        conn.commit()

def get_active_users():
    """現在アクティブなユーザー一覧（最終アクティビティから30分以内）"""
    cutoff_time = get_jst_now() - datetime.timedelta(minutes=30)

    with get_db_cursor() as (cursor, conn):
        cursor.execute("""
            SELECT u.id, u.full_name, u.category, MAX(ue.occurred_at) as last_activity
            FROM users u
            INNER JOIN user_events ue ON u.id = ue.user_id
            WHERE u.deleted_at IS NULL
            AND ue.occurred_at > %s
            GROUP BY u.id, u.full_name, u.category
            ORDER BY last_activity DESC
        """, (cutoff_time,))
        return cursor.fetchall()

def get_user_rankings(limit=10):
    """ユーザー別アクセスランキング"""
    with get_db_cursor() as (cursor, conn):
        cursor.execute("""
            SELECT u.full_name, u.category, COUNT(*) as event_count,
                   MAX(ue.occurred_at) as last_seen
            FROM users u
            INNER JOIN user_events ue ON u.id = ue.user_id
            WHERE u.deleted_at IS NULL
            GROUP BY u.id, u.full_name, u.category
            ORDER BY event_count DESC
            LIMIT %s
        """, (limit,))
        return cursor.fetchall()

def get_hourly_distribution():
    """時間帯別アクセス分布（0-23時）"""
    with get_db_cursor() as (cursor, conn):
        cursor.execute("""
            SELECT HOUR(occurred_at) as hour, COUNT(*) as count
            FROM user_events
            GROUP BY HOUR(occurred_at)
            ORDER BY hour
        """)
        results = cursor.fetchall()

        # 0-23時の配列を作成（データがない時間は0）
        hourly_data = [0] * 24
        for row in results:
            hourly_data[row['hour']] = row['count']

        return hourly_data

def get_daily_stats(days=7):
    """
    過去N日間（今日を含む）の日別統計。
    以前は「現在時刻からN日前」を閾値にしていたため境界日が部分的に混じり、
    N+1日分返ることがあった。JSTの日付境界に合わせて厳密化（2026-07-26 改修）。
    """
    start_date = get_jst_now().date() - datetime.timedelta(days=days - 1)
    start_dt = datetime.datetime.combine(start_date, datetime.time.min)

    with get_db_cursor() as (cursor, conn):
        cursor.execute("""
            SELECT DATE(occurred_at) as date, COUNT(*) as count,
                   COUNT(DISTINCT user_id) as unique_users
            FROM user_events
            WHERE occurred_at >= %s
            GROUP BY DATE(occurred_at)
            ORDER BY date
        """, (start_dt,))
        return cursor.fetchall()

def get_weekday_distribution():
    """曜日別アクセス分布（0=月曜、6=日曜）"""
    with get_db_cursor() as (cursor, conn):
        cursor.execute("""
            SELECT WEEKDAY(occurred_at) as weekday, COUNT(*) as count
            FROM user_events
            GROUP BY WEEKDAY(occurred_at)
            ORDER BY weekday
        """)
        results = cursor.fetchall()

        # 曜日データ（0=月曜）
        weekday_names = ['月', '火', '水', '木', '金', '土', '日']
        weekday_data = [0] * 7
        for row in results:
            weekday_data[row['weekday']] = row['count']

        return list(zip(weekday_names, weekday_data))

def get_event_type_distribution():
    """イベントタイプ別分布"""
    with get_db_cursor() as (cursor, conn):
        cursor.execute("""
            SELECT event_type, COUNT(*) as count
            FROM user_events
            GROUP BY event_type
            ORDER BY count DESC
        """)
        return cursor.fetchall()

def get_user_badges(user_id):
    """ユーザーバッジを計算（面白統計）"""
    badges = []

    with get_db_cursor() as (cursor, conn):
        # アーリーバード：朝5-7時にアクセス
        cursor.execute("""
            SELECT COUNT(*) as count
            FROM user_events
            WHERE user_id = %s AND HOUR(occurred_at) BETWEEN 5 AND 7
        """, (user_id,))
        early_bird = cursor.fetchone()['count']
        if early_bird >= 5:
            badges.append({'name': '🌅 アーリーバード', 'desc': f'朝型人間 ({early_bird}回)'})

        # ナイトオウル：夜22時-翌2時にアクセス
        cursor.execute("""
            SELECT COUNT(*) as count
            FROM user_events
            WHERE user_id = %s AND (HOUR(occurred_at) >= 22 OR HOUR(occurred_at) <= 2)
        """, (user_id,))
        night_owl = cursor.fetchone()['count']
        if night_owl >= 5:
            badges.append({'name': '🦉 ナイトオウル', 'desc': f'夜型人間 ({night_owl}回)'})

        # ウィークエンドウォリアー：土日にアクセス
        cursor.execute("""
            SELECT COUNT(*) as count
            FROM user_events
            WHERE user_id = %s AND WEEKDAY(occurred_at) IN (5, 6)
        """, (user_id,))
        weekend = cursor.fetchone()['count']
        if weekend >= 10:
            badges.append({'name': '🏖️ ウィークエンドウォリアー', 'desc': f'週末も活動 ({weekend}回)'})

        # ストリーク計算（連続アクセス日数）
        cursor.execute("""
            SELECT DISTINCT DATE(occurred_at) as access_date
            FROM user_events
            WHERE user_id = %s
            ORDER BY access_date DESC
        """, (user_id,))
        dates = [row['access_date'] for row in cursor.fetchall()]

        streak = 0
        if dates:
            # dates は DATE(occurred_at) すなわち JST の日付。
            # 以前は datetime.date.today()（サーバーローカル時刻＝UTC）と
            # 比較していたため、JST 00:00-09:00 の間はストリークが必ず0に
            # なっていた（2026-07-26 改修）。
            current_date = get_jst_now().date()
            for i, date in enumerate(dates):
                expected_date = current_date - datetime.timedelta(days=i)
                if date == expected_date:
                    streak += 1
                else:
                    break

        if streak >= 3:
            badges.append({'name': '🔥 ストリーク', 'desc': f'{streak}日連続アクセス'})

    return badges

def get_total_stats():
    """全体統計"""
    with get_db_cursor() as (cursor, conn):
        # 総アクセス数
        cursor.execute("SELECT COUNT(*) as total FROM user_events")
        total_events = cursor.fetchone()['total']

        # 総ユーザー数（削除されていない）
        cursor.execute("SELECT COUNT(*) as total FROM users WHERE deleted_at IS NULL")
        total_users = cursor.fetchone()['total']

        # 今日のアクセス数
        today = get_jst_now().date()
        cursor.execute("""
            SELECT COUNT(*) as total
            FROM user_events
            WHERE DATE(occurred_at) = %s
        """, (today,))
        today_events = cursor.fetchone()['total']

        # 今週のアクセス数
        week_start = today - datetime.timedelta(days=today.weekday())
        cursor.execute("""
            SELECT COUNT(*) as total
            FROM user_events
            WHERE DATE(occurred_at) >= %s
        """, (week_start,))
        week_events = cursor.fetchone()['total']

        return {
            'total_events': total_events,
            'total_users': total_users,
            'today_events': today_events,
            'week_events': week_events
        }

def get_peak_hour(hourly=None):
    """
    最もアクセスが多い時間帯。データが1件も無い場合は None を返す
    （以前は全ゼロでも 0 を返し「ピーク: 0時」と表示されていた）。
    hourly を渡せば時間帯別分布の再取得を省略できる（2026-07-26 改修）。
    """
    if hourly is None:
        hourly = get_hourly_distribution()
    if not hourly or max(hourly) == 0:
        return None
    return hourly.index(max(hourly))

def get_all_users_for_freq():
    """
    アクセス頻度グラフ用：イベント記録が1件以上ある全ユーザー一覧。
    戻り値: [{'id': int, 'full_name': str, 'category': str, 'total': int}, ...]
    （アクセス総数の降順）
    """
    with get_db_cursor() as (cursor, conn):
        cursor.execute("""
            SELECT u.id, u.full_name, u.category, COUNT(*) AS total
            FROM users u
            INNER JOIN user_events ue ON u.id = ue.user_id
            WHERE u.deleted_at IS NULL
            GROUP BY u.id, u.full_name, u.category
            ORDER BY total DESC, u.full_name ASC
        """)
        return cursor.fetchall()


def get_daily_total_access():
    """
    積み上げ面グラフ用：全ユーザー合算の日別アクセス数。
    「その他」（＝全体 − 選択ユーザー合計）を算出するための母数。
    戻り値: {'total': int, 'series': [{'date': 'YYYY-MM-DD', 'count': int}, ...]}
    series はアクセスがあった日のみ（昇順）。
    """
    with get_db_cursor() as (cursor, conn):
        cursor.execute("""
            SELECT DATE(ue.occurred_at) AS d, COUNT(*) AS count
            FROM user_events ue
            INNER JOIN users u ON u.id = ue.user_id
            WHERE u.deleted_at IS NULL
            GROUP BY DATE(ue.occurred_at)
            ORDER BY d ASC
        """)
        rows = cursor.fetchall()

    series = []
    total = 0
    for row in rows:
        d = row['d']
        date_str = d.isoformat() if hasattr(d, 'isoformat') else str(d)
        cnt = int(row['count'])
        total += cnt
        series.append({'date': date_str, 'count': cnt})

    return {'total': total, 'series': series}


def get_user_daily_access(user_id):
    """
    指定ユーザーの全期間にわたる日別アクセス数。
    戻り値: {'total': int, 'series': [{'date': 'YYYY-MM-DD', 'count': int}, ...]}
    series はアクセスがあった日のみ（昇順）。グラフ側でゼロ日を補間する。
    """
    with get_db_cursor() as (cursor, conn):
        cursor.execute("""
            SELECT DATE(occurred_at) AS d, COUNT(*) AS count
            FROM user_events
            WHERE user_id = %s
            GROUP BY DATE(occurred_at)
            ORDER BY d ASC
        """, (user_id,))
        rows = cursor.fetchall()

    series = []
    total = 0
    for row in rows:
        d = row['d']
        # MySQLコネクタは DATE 型を datetime.date で返す
        date_str = d.isoformat() if hasattr(d, 'isoformat') else str(d)
        cnt = int(row['count'])
        total += cnt
        series.append({'date': date_str, 'count': cnt})

    return {'total': total, 'series': series}


# 「直近アクセス記録」で選べる期間（ラベル, 時間数）
RECENT_ACCESS_PERIODS = [
    ('24時間', 24),
    ('48時間', 48),
    ('7日', 24 * 7),
]


def get_recent_access_log(hours=24):
    """
    直近 hours 時間の、ユーザー別アクセス回数（打ち切りなし・全員）。
    ゼロ回のユーザーは含めない。アクセス回数の降順。
    戻り値: {'hours': int, 'cutoff': 'YYYY-MM-DD HH:MM', 'users': [
                {'full_name': str, 'category': str, 'count': int,
                 'last_access': 'YYYY-MM-DD HH:MM'}, ...]}
    """
    cutoff = get_jst_now() - datetime.timedelta(hours=hours)

    with get_db_cursor() as (cursor, conn):
        cursor.execute("""
            SELECT u.full_name, u.category,
                   COUNT(*) AS cnt,
                   MAX(ue.occurred_at) AS last_access
            FROM users u
            INNER JOIN user_events ue ON u.id = ue.user_id
            WHERE u.deleted_at IS NULL
              AND ue.occurred_at > %s
            GROUP BY u.id, u.full_name, u.category
            ORDER BY cnt DESC, last_access DESC
        """, (cutoff,))
        rows = cursor.fetchall()

    users = []
    for row in rows:
        la = row['last_access']
        last_str = la.strftime('%Y-%m-%d %H:%M') if la else ''
        users.append({
            'full_name': row['full_name'],
            'category': row['category'],
            'count': int(row['cnt']),
            'last_access': last_str,
        })

    return {
        'hours': hours,
        'cutoff': cutoff.strftime('%Y-%m-%d %H:%M'),
        'users': users,
    }


def get_ip_stats():
    """IPアドレス別統計（Admin用）"""
    with get_db_cursor() as (cursor, conn):
        cursor.execute("""
            SELECT ip_address, COUNT(*) as count
            FROM user_events
            WHERE ip_address IS NOT NULL
            GROUP BY ip_address
            ORDER BY count DESC
            LIMIT 20
        """)
        return cursor.fetchall()

@stats_bp.route('/')
@login_required
def stat_main():
    """統計ページメイン"""
    user_id = session.get('user_id')
    user_name = session.get('user_name', 'ゲスト')
    user_category = session.get('user_category', 'guest')

    # 閲覧イベントを記録
    record_view_event(user_id)

    # 基本統計（Guest/Admin共通）
    active_users = get_active_users()
    total_stats = get_total_stats()
    hourly_dist = get_hourly_distribution()
    peak_hour = get_peak_hour(hourly_dist)
    weekday_dist = get_weekday_distribution()
    daily_stats = get_daily_stats(days=7)
    event_types = get_event_type_distribution()

    # アプリ規模（Guest/Admin共通）※ファイル走査が重いのでキャッシュ経由
    app_sizes = _cached('app_sizes', get_app_sizes)
    app_sizes_total = sum(a['total_lines'] for a in app_sizes)
    app_sizes_max = max((a['total_lines'] for a in app_sizes), default=1)

    # データベース統計（Guest/Admin共通）※全テーブルのCOUNT(*)が重いのでキャッシュ経由
    db_stats = _cached('db_stats', get_database_stats)
    # テンプレート用：バー表示の最大行数（全DB通して、ゼロ除算回避）
    db_stats_max_rows = 1
    for db in db_stats:
        for t in db['tables']:
            if t['row_count'] > db_stats_max_rows:
                db_stats_max_rows = t['row_count']

    # Admin専用統計
    admin_data = None
    if user_category == 'admin':
        admin_data = {
            'user_rankings': get_user_rankings(limit=20),
            'ip_stats': get_ip_stats(),
            'admin_files': _cached('admin_files', get_admin_files_size),
            # アクセス頻度グラフ用：イベントのある全ユーザー一覧
            'all_users_freq': get_all_users_for_freq(),
            # 積み上げ面グラフ用：日別の全体アクセス数（「その他」の母数）
            'daily_total_access': get_daily_total_access(),
            # 直近アクセス記録（既定24時間。期間切替はAJAXで再取得）
            'recent_access': get_recent_access_log(hours=24),
            'recent_access_periods': RECENT_ACCESS_PERIODS,
        }

    # 現在のユーザーのバッジ
    user_badges = get_user_badges(user_id)

    return render_template('stats_index.html',
                         user_name=user_name,
                         user_category=user_category,
                         active_users=active_users,
                         total_stats=total_stats,
                         hourly_dist=hourly_dist,
                         peak_hour=peak_hour,
                         weekday_dist=weekday_dist,
                         daily_stats=daily_stats,
                         event_types=event_types,
                         user_badges=user_badges,
                         app_sizes=app_sizes,
                         app_sizes_total=app_sizes_total,
                         app_sizes_max=app_sizes_max,
                         db_stats=db_stats,
                         db_stats_max_rows=db_stats_max_rows,
                         admin_data=admin_data,
                         # 表示用パス（テンプレートに /home/nishida を直書きしない）
                         admin_home=ADMIN_HOME,
                         fujinp_root=FUJINP_ROOT)


@stats_bp.route('/user_access_freq')
@login_required
def user_access_freq():
    """
    アクセス頻度グラフ用：指定ユーザーの全期間日別アクセス数を JSON で返す。
    クエリパラメータ: ?user_id=<int>
    Admin専用（他ユーザーの利用状況を含むため）。
    """
    # Admin以外は拒否（直接URLアクセス対策）
    if session.get('user_category') != 'admin':
        return jsonify({'error': 'forbidden'}), 403

    user_id_raw = request.args.get('user_id', '')
    try:
        user_id = int(user_id_raw)
    except (TypeError, ValueError):
        return jsonify({'error': 'invalid user_id'}), 400

    data = get_user_daily_access(user_id)
    return jsonify({
        'user_id': user_id,
        'total': data['total'],
        'series': data['series'],
    })


@stats_bp.route('/recent_access')
@login_required
def recent_access():
    """
    直近アクセス記録：指定期間のユーザー別アクセス回数を JSON で返す。
    クエリパラメータ: ?hours=<int>
    Admin専用。
    """
    if session.get('user_category') != 'admin':
        return jsonify({'error': 'forbidden'}), 403

    hours_raw = request.args.get('hours', '24')
    try:
        hours = int(hours_raw)
    except (TypeError, ValueError):
        return jsonify({'error': 'invalid hours'}), 400

    # 想定外の極端な値を弾く（1時間〜31日）
    if hours < 1 or hours > 24 * 31:
        return jsonify({'error': 'hours out of range'}), 400

    data = get_recent_access_log(hours=hours)
    return jsonify(data)


@stats_bp.route('/return_to_fujin')
@login_required
def return_to_fujin():
    """FUJIN-Pダッシュボードに戻る"""
    return redirect_to_dashboard()