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

# ============================================================
# user_migration/routes.py 統合版 v2.1
# ユーザ＋グループを一括マイグレーション
#
# 設計方針：
#   - api_export_all で ユーザ＋グループを一括取得
#   - 分析・コンフリクト解決・実行を一本のジョブで管理
#   - 実行順序はバックエンドが保証（ユーザ先・グループ後）
#
# v2.1（2026-07-25）での修正：
#   #1  api_receive_export_key に nonce 照合を導入（無認証での上書きを防止）
#   #2  ensure_tables() に許可申請テーブル等のDDLを追加
#   #3  管理者不在グループは実行者を暫定管理者にして適用（NOT NULL違反の解消）
#   #4  users.updated_at を設定
#   #5  user_features.granted_at / granted_by を設定、feature_code を50文字に丸め
#   #6  論理削除済み・無効ユーザをエクスポート対象外にし、移行先の削除済みは照会対象に
#   #7  例外時にジョブ状態を error へ戻す
#   #8  ユーザ／グループを可能なら単一トランザクションで実行
#   #9  実行時にコンフリクトを再判定し、未審査のものは適用せず警告に残す
#   #10 コンフリクトグループも実行時の最新データで適用
#   #13 実行済みジョブのコンフリクトは変更不可
#   #14 ユーザ用DB／管理用DBの参照を helper に一本化
# ============================================================

import datetime
import json
import logging
import secrets
import requests
import mysql.connector
from flask import request, jsonify, session, render_template
from decorators import login_required
from auth import redirect_to_dashboard
from config import Config
from db import DatabaseConfig
from . import user_migration_bp
from datetime import timezone, timedelta

logging.basicConfig(level=logging.DEBUG)

# ─── テーブル名 ──────────────────────────────────────────────
ALLIANCE_SITES     = "table_share_alliance_sites"
MIGRATION_JOBS     = "user_migration_jobs"                  # 統合ジョブ
CONFLICTS          = "user_migration_conflicts"             # ユーザコンフリクト
GROUP_CONFLICTS    = "user_migration_group_conflicts"       # グループコンフリクト
EXPORT_PERMISSIONS = "user_migration_export_permissions"    # 受け取った申請（送る側）
OUTGOING_REQUESTS  = "user_migration_outgoing_requests"     # 送った申請（受け取る側）

USER_DB = Config.DB_ACCOUNT + "$default"
JST     = timezone(timedelta(hours=9))

# 発行した nonce の有効期間（承認までに時間がかかることを想定して長め）
NONCE_TTL_HOURS = 168   # 7日


def _now_jst():
    return datetime.datetime.now(JST).replace(tzinfo=None)


# ============================================================
# DB接続ヘルパー（#14）
#   users / features / user_features        → ユーザ用DB（USER_DB）
#   user_groups / user_group_memberships    → 管理用DB（既定DB）
#   ゆーまいの管理テーブル・アライアンスサイト → 管理用DB（既定DB）
#   多くの構成では両者は同一DB。同一の場合は単一コネクションで実行する（#8）
# ============================================================

def _db_users():
    return DatabaseConfig.get_config(USER_DB)


def _db_mgmt():
    return DatabaseConfig.default()


def _is_same_database() -> bool:
    """ユーザ用DBと管理用DBが同一かどうか"""
    try:
        a, b = _db_users(), _db_mgmt()
        return (a.get('host')     == b.get('host') and
                a.get('port')     == b.get('port') and
                a.get('database') == b.get('database'))
    except Exception:
        return False


_COLUMN_CACHE = {}


def _table_columns(cursor, table: str) -> set:
    """テーブルの実カラム名を取得（サイトごとのスキーマ差異を吸収する）"""
    if table in _COLUMN_CACHE:
        return _COLUMN_CACHE[table]
    cols = set()
    try:
        cursor.execute(f"SHOW COLUMNS FROM `{table}`")
        for r in cursor.fetchall():
            cols.add(r.get('Field') if isinstance(r, dict) else r[0])
        _COLUMN_CACHE[table] = cols
    except Exception as e:
        logging.warning("_table_columns(%s): %s", table, e)
    return cols


def _active_user_where(cols: set, alias: str = '') -> str:
    """論理削除・無効ユーザを除外する WHERE 句を組み立てる（#6）"""
    p = f"{alias}." if alias else ""
    conds = []
    if 'deleted_at' in cols:
        conds.append(f"{p}deleted_at IS NULL")
    if 'is_active' in cols:
        conds.append(f"{p}is_active = 1")
    return " AND ".join(conds)


# ============================================================
# DDL（#2）
# ============================================================

def ensure_tables():
    ddls = [
        f"""
        CREATE TABLE IF NOT EXISTS `{MIGRATION_JOBS}` (
            id               INT AUTO_INCREMENT PRIMARY KEY,
            alliance_site_id INT NOT NULL,
            status           ENUM('fetched','conflict_review','executing','done','error')
                             NOT NULL DEFAULT 'fetched',
            total_remote_users    INT DEFAULT 0,
            total_new_users       INT DEFAULT 0,
            total_conflicts_users INT DEFAULT 0,
            total_remote_groups   INT DEFAULT 0,
            total_new_groups      INT DEFAULT 0,
            total_conflicts_groups INT DEFAULT 0,
            total_applied_users   INT DEFAULT 0,
            total_applied_groups  INT DEFAULT 0,
            summary          TEXT,
            created_at       DATETIME,
            created_by       INT,
            finished_at      DATETIME
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        f"""
        CREATE TABLE IF NOT EXISTS `{CONFLICTS}` (
            id               INT AUTO_INCREMENT PRIMARY KEY,
            job_id           INT NOT NULL,
            email            VARCHAR(255) NOT NULL,
            conflict_type    ENUM('regular_guest','admin_source') NOT NULL,
            local_user_json  TEXT,
            remote_user_json TEXT,
            resolution       ENUM('pending','use_remote','use_local','skip',
                                  'set_admin','set_regular','set_guest')
                             DEFAULT 'pending',
            resolved_at      DATETIME,
            resolved_by      INT,
            KEY idx_job_id (job_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        f"""
        CREATE TABLE IF NOT EXISTS `{GROUP_CONFLICTS}` (
            id                  INT AUTO_INCREMENT PRIMARY KEY,
            job_id              INT NOT NULL,
            remote_group_name   VARCHAR(255) NOT NULL,
            conflict_type       ENUM('name_conflict','manager_missing','members_missing') NOT NULL,
            all_issue_types     VARCHAR(255),
            remote_group_json   TEXT,
            missing_user_emails TEXT,
            resolution          ENUM('pending','use_remote','use_local','skip')
                                DEFAULT 'pending',
            resolved_at         DATETIME,
            resolved_by         INT,
            KEY idx_job_id (job_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        # ★ v2.1 で追加：これが無いと新規サイトで許可申請が 500 になっていた
        f"""
        CREATE TABLE IF NOT EXISTS `{EXPORT_PERMISSIONS}` (
            id             INT AUTO_INCREMENT PRIMARY KEY,
            requester_url  VARCHAR(500) NOT NULL,
            requester_name VARCHAR(255) DEFAULT NULL,
            requester_nonce VARCHAR(128) DEFAULT NULL,
            status         ENUM('pending','approved','rejected') DEFAULT 'pending',
            requested_at   DATETIME DEFAULT NULL,
            processed_at   DATETIME DEFAULT NULL,
            processed_by   INT DEFAULT NULL,
            UNIQUE KEY uk_requester_url (requester_url(191))
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        # ★ v2.1 で追加：受け取る側が「自分が出した申請」を覚えておくための台帳
        f"""
        CREATE TABLE IF NOT EXISTS `{OUTGOING_REQUESTS}` (
            id            INT AUTO_INCREMENT PRIMARY KEY,
            exporter_url  VARCHAR(500) NOT NULL,
            exporter_name VARCHAR(255) DEFAULT NULL,
            nonce         VARCHAR(128) NOT NULL,
            status        ENUM('pending','received','expired') DEFAULT 'pending',
            requested_at  DATETIME DEFAULT NULL,
            received_at   DATETIME DEFAULT NULL,
            requested_by  INT DEFAULT NULL,
            UNIQUE KEY uk_exporter_url (exporter_url(191))
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
    ]

    # 既存インストール向けの追随ALTER（存在しない場合のみ実行）
    alters = [
        (EXPORT_PERMISSIONS, 'requester_nonce',
         f"ALTER TABLE `{EXPORT_PERMISSIONS}` ADD COLUMN requester_nonce VARCHAR(128) DEFAULT NULL"),
        (GROUP_CONFLICTS, 'all_issue_types',
         f"ALTER TABLE `{GROUP_CONFLICTS}` ADD COLUMN all_issue_types VARCHAR(255) DEFAULT NULL"),
    ]

    conn = None
    try:
        conn = mysql.connector.connect(**_db_mgmt())
        cursor = conn.cursor(dictionary=True)
        for ddl in ddls:
            try:
                cursor.execute(ddl)
            except Exception as e:
                logging.warning("ensure_tables ddl: %s", e)
        for table, column, ddl in alters:
            try:
                if column not in _table_columns(cursor, table):
                    cursor.execute(ddl)
                    _COLUMN_CACHE.pop(table, None)
            except Exception as e:
                logging.warning("ensure_tables alter %s.%s: %s", table, column, e)
        conn.commit()
    except Exception as e:
        logging.warning("ensure_tables: %s", e)
    finally:
        if conn is not None and conn.is_connected():
            try:
                cursor.close()
            except Exception:
                pass
            conn.close()


# ============================================================
# 権限チェック
# ============================================================

def check_migration_permission(user_id) -> bool:
    return _is_admin(user_id)


def _is_admin(user_id) -> bool:
    if not user_id:
        return False
    conn = None
    try:
        conn = mysql.connector.connect(**_db_users())
        cursor = conn.cursor(dictionary=True)
        cols  = _table_columns(cursor, 'users')
        where = _active_user_where(cols)
        sql   = "SELECT category FROM users WHERE id = %s"
        if where:
            sql += f" AND {where}"
        cursor.execute(sql, (user_id,))
        row = cursor.fetchone()
        return bool(row and row['category'] == 'admin')
    except Exception as e:
        logging.error("_is_admin error: %s", e)
        return False
    finally:
        if conn is not None and conn.is_connected():
            try:
                cursor.close()
            except Exception:
                pass
            conn.close()


# ============================================================
# ユーティリティ
# ============================================================

def _get_site_by_id(site_id):
    conn = mysql.connector.connect(**_db_mgmt())
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            f"SELECT * FROM {ALLIANCE_SITES} WHERE id = %s AND is_active = 1",
            (site_id,)
        )
        return cursor.fetchone()
    finally:
        cursor.close(); conn.close()


def _verify_api_key(api_key: str) -> bool:
    if not api_key:
        return False
    for attr in ('USER_MIGRATION_API_KEY', 'TABLE_SHARE_API_KEY'):
        expected = getattr(Config, attr, None)
        if expected and secrets.compare_digest(str(api_key), str(expected)):
            return True
    return False


def _fetch_local_users_all(conn=None, include_inactive: bool = False):
    """
    ローカルユーザをメールキーで取得する。
    include_inactive=True のとき、論理削除・無効ユーザも 'inactive': True 付きで含める（#6）。
    """
    own = conn is None
    if own:
        conn = mysql.connector.connect(**_db_users())
    cursor = conn.cursor(dictionary=True)
    try:
        cols      = _table_columns(cursor, 'users')
        has_del   = 'deleted_at' in cols
        has_act   = 'is_active' in cols
        extra     = []
        if has_del:
            extra.append("deleted_at")
        if has_act:
            extra.append("is_active")
        select_extra = (", " + ", ".join(extra)) if extra else ""
        where        = "" if include_inactive else (
            f" WHERE {_active_user_where(cols)}" if _active_user_where(cols) else "")

        cursor.execute(
            f"SELECT id, email, full_name AS name, category{select_extra} FROM users{where}"
        )
        users = cursor.fetchall()

        result = {}
        for u in users:
            inactive = bool(
                (has_del and u.get('deleted_at') is not None) or
                (has_act and u.get('is_active') in (0, False))
            )
            u.pop('deleted_at', None)
            u.pop('is_active', None)
            u['inactive'] = inactive
            cursor.execute("""
                SELECT f.feature_name
                FROM user_features uf
                JOIN features f ON uf.feature_id = f.id
                WHERE uf.user_id = %s
            """, (u['id'],))
            u['features'] = [r['feature_name'] for r in cursor.fetchall()]
            result[u['email']] = u
        return result
    finally:
        cursor.close()
        if own:
            conn.close()


def _fetch_local_groups_all(conn=None):
    """ローカルのグループ一覧をメール照合用に取得（メンバーは有効ユーザのみ）"""
    own = conn is None
    if own:
        conn = mysql.connector.connect(**_db_mgmt())
    cursor = conn.cursor(dictionary=True)
    try:
        ucols      = _table_columns(cursor, 'users')
        active_u   = _active_user_where(ucols, 'u')
        member_and = f" AND {active_u}" if active_u else ""

        cursor.execute("""
            SELECT g.id, g.name, g.description, g.manager_user_id,
                   u.email as manager_email, u.full_name as manager_name
            FROM user_groups g
            LEFT JOIN users u ON g.manager_user_id = u.id
        """)
        groups = cursor.fetchall()
        for g in groups:
            cursor.execute(f"""
                SELECT u.email, u.full_name, m.valid_from, m.valid_until
                FROM user_group_memberships m
                JOIN users u ON m.user_id = u.id
                WHERE m.group_id = %s{member_and}
            """, (g['id'],))
            g['members'] = cursor.fetchall()
        return groups
    finally:
        cursor.close()
        if own:
            conn.close()


def _feature_code_for(cursor, feature_name: str) -> str:
    """features.feature_code は varchar(50) UNIQUE。50文字に丸めつつ衝突を回避する（#5）"""
    base = (feature_name or '').strip()[:50] or 'feature'
    code = base
    for n in range(2, 60):
        cursor.execute("SELECT id FROM features WHERE feature_code = %s", (code,))
        if not cursor.fetchone():
            return code
        suffix = f"_{n}"
        code = base[:50 - len(suffix)] + suffix
    return (base[:42] + '_' + secrets.token_hex(3))[:50]


def _get_or_create_feature_id(cursor, feature_name: str) -> int:
    cursor.execute("SELECT id FROM features WHERE feature_name = %s", (feature_name,))
    row = cursor.fetchone()
    if row:
        return row['id']

    cols   = _table_columns(cursor, 'features')
    names  = ['feature_code', 'feature_name']
    values = [_feature_code_for(cursor, feature_name), feature_name[:100]]
    if 'created_at' in cols:
        names.append('created_at'); values.append(_now_jst())
    ph = ', '.join(['%s'] * len(names))
    cursor.execute(
        f"INSERT INTO features ({', '.join(names)}) VALUES ({ph})", values
    )
    return cursor.lastrowid


def _apply_user_to_local(cursor, email, name, category, features, actor_id=None):
    """users を UPSERT し、user_features を移行元の内容で置換する"""
    now   = _now_jst()
    ucols = _table_columns(cursor, 'users')

    cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
    row = cursor.fetchone()

    if row:
        uid  = row['id']
        sets = ["full_name = %s", "category = %s"]
        vals = [name, category]
        if 'updated_at' in ucols:                       # #4
            sets.append("updated_at = %s"); vals.append(now)
        if 'deleted_at' in ucols:                       # 復活させる場合は削除フラグを解除
            sets.append("deleted_at = NULL")
        if 'is_active' in ucols:
            sets.append("is_active = 1")
        vals.append(uid)
        cursor.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = %s", vals)
    else:
        names  = ['email', 'full_name', 'category']
        values = [email, name, category]
        if 'created_at' in ucols:
            names.append('created_at'); values.append(now)
        if 'updated_at' in ucols:                       # #4
            names.append('updated_at'); values.append(now)
        if 'is_active' in ucols:
            names.append('is_active');  values.append(1)
        ph = ', '.join(['%s'] * len(names))
        cursor.execute(
            f"INSERT INTO users ({', '.join(names)}) VALUES ({ph})", values
        )
        uid = cursor.lastrowid

    # 機能割り当ては置換（差分マージではない）
    fcols = _table_columns(cursor, 'user_features')
    cursor.execute("DELETE FROM user_features WHERE user_id = %s", (uid,))
    for fname in features or []:
        fid    = _get_or_create_feature_id(cursor, fname)
        names  = ['user_id', 'feature_id']
        values = [uid, fid]
        if 'granted_at' in fcols:                       # #5
            names.append('granted_at'); values.append(_now_jst())
        if 'granted_by' in fcols and actor_id:
            names.append('granted_by'); values.append(actor_id)
        ph = ', '.join(['%s'] * len(names))
        cursor.execute(
            f"INSERT IGNORE INTO user_features ({', '.join(names)}) VALUES ({ph})",
            values
        )
    return uid


# ============================================================
# コンフリクト判定（分析時・実行時で共通に使う）（#9 #10）
# ============================================================

def _classify_user(email, remote_user, local_users):
    """
    戻り値: 'new' | 'conflict' | 'admin_source'
      new          … 照会不要でそのまま追加
      conflict     … 同メールの非adminが移行先に存在（削除済みを含む）
      admin_source … 移行元または移行先が admin
    """
    lu = local_users.get(email)
    if remote_user.get('category') == 'admin':
        return 'admin_source'
    if lu is None:
        return 'new'
    if lu.get('category') == 'admin':
        return 'admin_source'
    return 'conflict'


def _classify_group(g, local_group_names, expected_emails):
    """
    グループの問題点を列挙する。
    expected_emails は「移行完了後に移行先に存在すると見込まれるメール」の集合。
    （同一ジョブで追加される新規ユーザを含めるため、不要な不在判定を出さない）
    """
    issues = []
    if g.get('name') in local_group_names:
        issues.append({'type': 'name_conflict'})
    if g.get('manager_email') and g['manager_email'] not in expected_emails:
        issues.append({'type': 'manager_missing'})
    missing = [m['email'] for m in (g.get('members') or [])
               if m.get('email') and m['email'] not in expected_emails]
    if missing:
        issues.append({'type': 'members_missing', 'missing': missing})
    return issues


# ============================================================
# ダッシュボード
# ============================================================

@user_migration_bp.route('/')
@login_required
def dashboard():
    ensure_tables()
    return render_template('user_migration_dashboard.html')


# ============================================================
# 公開API（移行元サイト側）
# ============================================================

@user_migration_bp.route('/api/site_info', methods=['GET'])
def api_site_info():
    return jsonify({
        'success': True,
        'fujin_p_user_migration': True,
        'site_id':   Config.DB_ACCOUNT,
        'site_name': getattr(Config, 'SITE_DISPLAY_NAME', Config.DB_ACCOUNT),
        'version':   '2.1',
        'features':  ['export_all', 'permission_nonce'],
        'endpoints': {
            'export_all':         '/user_migration/api/export_all',
            'request_permission': '/user_migration/api/request_permission',
            'receive_export_key': '/user_migration/api/receive_export_key',
        }
    })


@user_migration_bp.route('/api/export_all', methods=['POST'])
def api_export_all():
    """ユーザ＋グループを一括エクスポート（論理削除・無効ユーザは除外）"""
    import sys, traceback

    data = request.json or {}
    if not _verify_api_key(data.get('api_key')):
        return jsonify({'success': False, 'error': '認証失敗'}), 401

    try:
        # ユーザ取得（有効ユーザのみ）
        users_by_email = _fetch_local_users_all(include_inactive=False)
        safe_users = []
        for u in users_by_email.values():
            safe_u = {
                k: (v.isoformat() if isinstance(v, (datetime.datetime, datetime.date)) else v)
                for k, v in u.items() if k != 'inactive'
            }
            safe_users.append(safe_u)

        # グループ取得
        groups = _fetch_local_groups_all()
        safe_groups = []
        for g in groups:
            safe_g = {}
            for k, v in g.items():
                if k == 'members':
                    safe_members = []
                    for m in v:
                        safe_m = {
                            mk: (mv.isoformat() if isinstance(mv, (datetime.datetime, datetime.date)) else mv)
                            for mk, mv in m.items()
                        }
                        safe_members.append(safe_m)
                    safe_g['members'] = safe_members
                elif isinstance(v, (datetime.datetime, datetime.date)):
                    safe_g[k] = v.isoformat()
                else:
                    safe_g[k] = v
            safe_groups.append(safe_g)

        return jsonify({
            'success': True,
            'users':   safe_users,
            'groups':  safe_groups
        })

    except Exception as e:
        print(f"[EXPORT_ALL] ERROR: {e}", file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================
# アライアンスサイト一覧
# ============================================================

@user_migration_bp.route('/get_alliance_sites', methods=['GET'])
@login_required
def get_alliance_sites():
    conn = None
    try:
        conn = mysql.connector.connect(**_db_mgmt())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(f"""
            SELECT id, site_name, site_url, description, is_active, created_at
            FROM {ALLIANCE_SITES} WHERE is_active = 1 ORDER BY site_name
        """)
        sites = cursor.fetchall()
        for s in sites:
            if s.get('created_at'):
                s['created_at'] = s['created_at'].strftime('%Y-%m-%d %H:%M:%S')
        return jsonify({'success': True, 'sites': sites})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn is not None and conn.is_connected():
            try:
                cursor.close()
            except Exception:
                pass
            conn.close()


# ============================================================
# Step1：一括取得・分析
# ============================================================

@user_migration_bp.route('/fetch_and_analyze', methods=['POST'])
@login_required
def fetch_and_analyze():
    """
    リモートからユーザ＋グループを一括取得し、コンフリクト分析してジョブを作成する。
    承認は一度。実行時にユーザ→グループの順で適用する。
    """
    if not check_migration_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '権限がありません'}), 403

    ensure_tables()

    data = request.json or {}
    site_id = data.get('alliance_site_id')
    if not site_id:
        return jsonify({'success': False, 'error': 'alliance_site_id が必要です'}), 400

    site = _get_site_by_id(site_id)
    if not site:
        return jsonify({'success': False, 'error': 'サイトが見つかりません'}), 404

    conn = None
    try:
        # ── リモートから一括取得 ──────────────────────────
        resp = requests.post(
            f"{site['site_url']}/user_migration/api/export_all",
            json={'api_key': site['api_key']},
            timeout=30
        )
        if not resp.ok:
            return jsonify({'success': False, 'error': f'リモート接続エラー: {resp.status_code}'}), 500
        rd = resp.json()
        if not rd.get('success'):
            return jsonify({'success': False, 'error': rd.get('error', '不明なエラー')}), 500

        remote_users  = {u['email']: u for u in rd.get('users', []) if u.get('email')}
        remote_groups = rd.get('groups', [])

        # ── ローカルの現状取得（削除済みも照会対象にするため include_inactive）──
        local_users       = _fetch_local_users_all(include_inactive=True)
        local_group_names = {g['name'] for g in _fetch_local_groups_all()}
        active_emails     = {e for e, u in local_users.items() if not u.get('inactive')}

        # ── ユーザ分析 ────────────────────────────────────
        new_users     = []
        conflicts     = []
        admin_sources = []

        for email, ru in remote_users.items():
            kind = _classify_user(email, ru, local_users)
            if kind == 'admin_source':
                admin_sources.append({
                    'email': email, 'remote_user': ru,
                    'local_user': local_users.get(email)
                })
            elif kind == 'conflict':
                conflicts.append({
                    'email': email, 'remote_user': ru,
                    'local_user': local_users[email]
                })
            else:
                new_users.append(ru)

        # ── グループ分析 ──────────────────────────────────
        # 同一ジョブで確実に追加される新規ユーザは「移行後に存在する」とみなす
        expected_emails = active_emails | {u['email'] for u in new_users}

        group_conflicts = []
        group_clean     = []

        for g in remote_groups:
            issues = _classify_group(g, local_group_names, expected_emails)
            if issues:
                group_conflicts.append({'group': g, 'issues': issues})
            else:
                group_clean.append(g)

        # ── ジョブ作成 ────────────────────────────────────
        now     = _now_jst()
        user_id = session.get('user_id')
        needs_review = len(conflicts) + len(admin_sources) + len(group_conflicts)

        conn = mysql.connector.connect(**_db_mgmt())
        cursor = conn.cursor(dictionary=True)

        cursor.execute(f"""
            INSERT INTO {MIGRATION_JOBS}
            (alliance_site_id, status,
             total_remote_users, total_new_users, total_conflicts_users,
             total_remote_groups, total_new_groups, total_conflicts_groups,
             created_at, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            site_id,
            'conflict_review' if needs_review else 'fetched',
            len(remote_users), len(new_users), len(conflicts) + len(admin_sources),
            len(remote_groups), len(group_clean), len(group_conflicts),
            now, user_id
        ))
        job_id = cursor.lastrowid

        # ユーザコンフリクト保存
        for c in conflicts:
            cursor.execute(f"""
                INSERT INTO {CONFLICTS}
                (job_id, email, conflict_type, local_user_json, remote_user_json, resolution)
                VALUES (%s, %s, 'regular_guest', %s, %s, 'pending')
            """, (job_id, c['email'],
                  json.dumps(c['local_user'],  ensure_ascii=False, default=str),
                  json.dumps(c['remote_user'], ensure_ascii=False, default=str)))

        for a in admin_sources:
            cursor.execute(f"""
                INSERT INTO {CONFLICTS}
                (job_id, email, conflict_type, local_user_json, remote_user_json, resolution)
                VALUES (%s, %s, 'admin_source', %s, %s, 'pending')
            """, (job_id, a['email'],
                  json.dumps(a.get('local_user'), ensure_ascii=False, default=str),
                  json.dumps(a['remote_user'],    ensure_ascii=False, default=str)))

        # グループコンフリクト保存（複数の問題も all_issue_types に残す）
        for gc in group_conflicts:
            g = gc['group']
            missing_emails = []
            for issue in gc['issues']:
                if issue['type'] == 'members_missing':
                    missing_emails = issue.get('missing', [])
            all_types = ','.join(i['type'] for i in gc['issues'])
            cursor.execute(f"""
                INSERT INTO {GROUP_CONFLICTS}
                (job_id, remote_group_name, conflict_type, all_issue_types,
                 remote_group_json, missing_user_emails, resolution)
                VALUES (%s, %s, %s, %s, %s, %s, 'pending')
            """, (job_id, g['name'], gc['issues'][0]['type'], all_types,
                  json.dumps(g, ensure_ascii=False, default=str),
                  json.dumps(missing_emails, ensure_ascii=False)))

        conn.commit()

        return jsonify({
            'success':       True,
            'job_id':        job_id,
            # ユーザ
            'total_remote_users':  len(remote_users),
            'new_users':           len(new_users),
            'user_conflicts':      len(conflicts),
            'admin_sources':       len(admin_sources),
            # グループ
            'total_remote_groups': len(remote_groups),
            'new_groups':          len(group_clean),
            'group_conflicts':     len(group_conflicts),
            # 全体
            'needs_review':        needs_review > 0,
        })

    except requests.RequestException as e:
        return jsonify({'success': False, 'error': f'ネットワークエラー: {e}'}), 500
    except Exception as e:
        logging.error("fetch_and_analyze: %s", e)
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn is not None and conn.is_connected():
            try:
                cursor.close()
            except Exception:
                pass
            conn.close()


# ============================================================
# コンフリクト取得・解決
# ============================================================

@user_migration_bp.route('/get_conflicts', methods=['GET'])
@login_required
def get_conflicts():
    job_id = request.args.get('job_id')
    if not job_id:
        return jsonify({'success': False, 'error': 'job_id が必要です'}), 400
    conn = None
    try:
        conn = mysql.connector.connect(**_db_mgmt())
        cursor = conn.cursor(dictionary=True)

        cursor.execute(f"SELECT status FROM {MIGRATION_JOBS} WHERE id = %s", (job_id,))
        job = cursor.fetchone()
        job_status = job['status'] if job else None
        editable   = job_status in ('fetched', 'conflict_review')

        # ユーザコンフリクト
        cursor.execute(f"""
            SELECT id, email, conflict_type, local_user_json, remote_user_json,
                   resolution, resolved_at
            FROM {CONFLICTS} WHERE job_id = %s ORDER BY conflict_type, email
        """, (job_id,))
        user_rows = cursor.fetchall()
        for r in user_rows:
            if r.get('resolved_at'):
                r['resolved_at'] = r['resolved_at'].strftime('%Y-%m-%d %H:%M:%S')
            if r['local_user_json']:
                r['local_user']  = json.loads(r['local_user_json'])
            if r['remote_user_json']:
                r['remote_user'] = json.loads(r['remote_user_json'])

        # グループコンフリクト
        gcols     = _table_columns(cursor, GROUP_CONFLICTS)
        all_types = ", all_issue_types" if 'all_issue_types' in gcols else ""
        cursor.execute(f"""
            SELECT id, remote_group_name, conflict_type{all_types},
                   remote_group_json, missing_user_emails,
                   resolution, resolved_at
            FROM {GROUP_CONFLICTS} WHERE job_id = %s ORDER BY conflict_type, remote_group_name
        """, (job_id,))
        group_rows = cursor.fetchall()
        for r in group_rows:
            if r.get('resolved_at'):
                r['resolved_at'] = r['resolved_at'].strftime('%Y-%m-%d %H:%M:%S')
            if r.get('remote_group_json'):
                r['remote_group'] = json.loads(r['remote_group_json'])
            if r.get('missing_user_emails'):
                r['missing_emails'] = json.loads(r['missing_user_emails'])
            r['issue_types'] = [t for t in (r.get('all_issue_types') or
                                            r.get('conflict_type') or '').split(',') if t]

        pending = (sum(1 for r in user_rows  if r['resolution'] == 'pending') +
                   sum(1 for r in group_rows if r['resolution'] == 'pending'))

        return jsonify({
            'success':         True,
            'job_status':      job_status,
            'editable':        editable,
            'user_conflicts':  user_rows,
            'group_conflicts': group_rows,
            'pending_count':   pending
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn is not None and conn.is_connected():
            try:
                cursor.close()
            except Exception:
                pass
            conn.close()


@user_migration_bp.route('/resolve_conflict', methods=['POST'])
@login_required
def resolve_conflict():
    """ユーザ・グループ両方のコンフリクト解決を受け付ける（実行済みジョブは拒否 #13）"""
    if not check_migration_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '権限がありません'}), 403

    data       = request.json or {}
    cid        = data.get('conflict_id')
    ctype      = data.get('conflict_kind')   # 'user' or 'group'
    resolution = data.get('resolution')

    if ctype not in ('user', 'group'):
        return jsonify({'success': False, 'error': 'conflict_kind不正'}), 400
    if not cid:
        return jsonify({'success': False, 'error': 'conflict_id が必要です'}), 400

    valid_user  = {'use_remote', 'use_local', 'skip', 'set_admin', 'set_regular', 'set_guest'}
    valid_group = {'use_remote', 'use_local', 'skip'}

    if ctype == 'user'  and resolution not in valid_user:
        return jsonify({'success': False, 'error': 'resolution不正'}), 400
    if ctype == 'group' and resolution not in valid_group:
        return jsonify({'success': False, 'error': 'resolution不正'}), 400

    table = CONFLICTS if ctype == 'user' else GROUP_CONFLICTS

    conn = None
    try:
        conn = mysql.connector.connect(**_db_mgmt())
        cursor = conn.cursor(dictionary=True)

        # ジョブ状態を確認してから更新する
        cursor.execute(f"""
            SELECT c.id, j.status
            FROM {table} c
            LEFT JOIN {MIGRATION_JOBS} j ON c.job_id = j.id
            WHERE c.id = %s
        """, (cid,))
        row = cursor.fetchone()
        if not row:
            return jsonify({'success': False, 'error': 'コンフリクトが見つかりません'}), 404
        if row['status'] not in ('fetched', 'conflict_review'):
            return jsonify({
                'success': False,
                'error': 'このジョブは既に実行中または実行済みのため変更できません'
            }), 409

        cursor.execute(f"""
            UPDATE {table}
            SET resolution = %s, resolved_at = %s, resolved_by = %s
            WHERE id = %s
        """, (resolution, _now_jst(), session.get('user_id'), cid))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn is not None and conn.is_connected():
            try:
                cursor.close()
            except Exception:
                pass
            conn.close()


# ============================================================
# Step3：統合実行（ユーザ→グループの順）
# ============================================================

def _mark_job_error(job_id, message):
    """例外終了時にジョブを error に落とす（#7）"""
    conn = None
    try:
        conn = mysql.connector.connect(**_db_mgmt())
        cur  = conn.cursor()
        cur.execute(f"""
            UPDATE {MIGRATION_JOBS}
            SET status='error', summary=%s, finished_at=%s
            WHERE id=%s AND status <> 'done'
        """, (json.dumps({'error': str(message)}, ensure_ascii=False), _now_jst(), job_id))
        conn.commit()
        cur.close()
    except Exception as e:
        logging.error("_mark_job_error: %s", e)
    finally:
        if conn is not None and conn.is_connected():
            conn.close()


@user_migration_bp.route('/execute_migration', methods=['POST'])
@login_required
def execute_migration():
    """
    ユーザマイグレーションを先に完了させてから、グループマイグレーションを実行する。
    pendingコンフリクトが1件でもあれば拒否。
    実行時にコンフリクトを再判定し、未審査のものは適用しない（#9 #10）。
    """
    if not check_migration_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '権限がありません'}), 403

    data    = request.json or {}
    job_id  = data.get('job_id')
    actor   = session.get('user_id')
    if not job_id:
        return jsonify({'success': False, 'error': 'job_id が必要です'}), 400

    mgmt_conn = user_conn = None
    mgmt_cur  = user_cur  = None
    single_tx = _is_same_database()
    started   = False

    try:
        mgmt_conn = mysql.connector.connect(**_db_mgmt())
        mgmt_cur  = mgmt_conn.cursor(dictionary=True)

        # ── ジョブ確認 ────────────────────────────────────
        mgmt_cur.execute(f"SELECT * FROM {MIGRATION_JOBS} WHERE id = %s", (job_id,))
        job = mgmt_cur.fetchone()
        if not job:
            return jsonify({'success': False, 'error': 'ジョブが見つかりません'}), 404
        if job['status'] == 'done':
            return jsonify({'success': False, 'error': '既に完了しています'}), 400
        if job['status'] == 'executing':
            return jsonify({'success': False, 'error': '実行中です'}), 409

        # ── pending確認（ユーザ＋グループ両方） ──────────
        mgmt_cur.execute(
            f"SELECT COUNT(*) AS cnt FROM {CONFLICTS} WHERE job_id=%s AND resolution='pending'",
            (job_id,)
        )
        u_pending = mgmt_cur.fetchone()['cnt']
        mgmt_cur.execute(
            f"SELECT COUNT(*) AS cnt FROM {GROUP_CONFLICTS} WHERE job_id=%s AND resolution='pending'",
            (job_id,)
        )
        g_pending = mgmt_cur.fetchone()['cnt']

        if u_pending + g_pending > 0:
            return jsonify({
                'success': False,
                'error':   f'未解決のコンフリクトが {u_pending + g_pending} 件あります'
            }), 400

        # ── リモートから再取得 ────────────────────────────
        site = _get_site_by_id(job['alliance_site_id'])
        if not site:
            return jsonify({'success': False, 'error': '移行元サイトが見つかりません'}), 404

        resp = requests.post(
            f"{site['site_url']}/user_migration/api/export_all",
            json={'api_key': site['api_key']},
            timeout=30
        )
        if not resp.ok:
            return jsonify({'success': False, 'error': f'リモート接続エラー: {resp.status_code}'}), 500
        rd = resp.json()
        if not rd.get('success'):
            return jsonify({'success': False, 'error': rd.get('error')}), 500

        remote_users  = {u['email']: u for u in rd.get('users', []) if u.get('email')}
        remote_groups = rd.get('groups', [])

        # ── コンフリクト解決内容読み込み ──────────────────
        mgmt_cur.execute(f"SELECT * FROM {CONFLICTS} WHERE job_id=%s", (job_id,))
        user_resolved = {r['email']: r for r in mgmt_cur.fetchall()}

        mgmt_cur.execute(f"SELECT * FROM {GROUP_CONFLICTS} WHERE job_id=%s", (job_id,))
        group_resolved = {r['remote_group_name']: r for r in mgmt_cur.fetchall()}

        local_users = _fetch_local_users_all(include_inactive=True)

        # ── 実行開始 ──────────────────────────────────────
        mgmt_cur.execute(
            f"UPDATE {MIGRATION_JOBS} SET status='executing' WHERE id=%s", (job_id,)
        )
        mgmt_conn.commit()
        started = True

        if single_tx:
            user_conn, user_cur = mgmt_conn, mgmt_cur
        else:
            user_conn = mysql.connector.connect(**_db_users())
            user_cur  = user_conn.cursor(dictionary=True)

        u_applied = 0; u_skipped = 0; u_errors = []; u_warnings = []
        cat_map = {'set_admin': 'admin', 'set_regular': 'regular', 'set_guest': 'guest'}

        # ════ PHASE 1: ユーザ ════════════════════════════
        for email, ru in remote_users.items():
            try:
                kind    = _classify_user(email, ru, local_users)
                res_row = user_resolved.get(email)

                if kind == 'new':
                    # 分析後に移行元へ増えたユーザも新規として扱う（照会不要の区分）
                    _apply_user_to_local(user_cur, email, ru.get('name', ''),
                                         ru.get('category', 'regular'),
                                         ru.get('features', []), actor)
                    u_applied += 1
                    continue

                # ここから先は要照会。解決行が無ければ「分析後に発生した未審査の衝突」（#9）
                if not res_row:
                    u_skipped += 1
                    u_warnings.append(
                        f"{email}: 分析後に状況が変わり未審査のため適用しませんでした"
                    )
                    continue

                resolution = res_row['resolution']
                if resolution in ('skip', 'use_local'):
                    u_skipped += 1
                    continue

                if kind == 'admin_source':
                    new_cat = cat_map.get(resolution)
                    if not new_cat:
                        u_skipped += 1
                        u_warnings.append(f"{email}: admin処理の選択が不正のため適用しませんでした")
                        continue
                else:  # conflict
                    if resolution != 'use_remote':
                        u_skipped += 1
                        continue
                    new_cat = ru.get('category', 'regular')

                lu = local_users.get(email)
                if lu and lu.get('inactive'):
                    u_warnings.append(f"{email}: 移行先の削除済みユーザを再有効化しました")

                _apply_user_to_local(user_cur, email, ru.get('name', ''), new_cat,
                                     ru.get('features', []), actor)
                u_applied += 1

            except Exception as e:
                u_errors.append({'email': email, 'error': str(e)})

        if not single_tx:
            user_conn.commit()

        # ── ユーザ適用後、メール→IDマップを再取得 ─────────
        ucols     = _table_columns(user_cur, 'users')
        act_where = _active_user_where(ucols)
        user_cur.execute(
            "SELECT id, email FROM users" + (f" WHERE {act_where}" if act_where else "")
        )
        email_to_id = {r['email']: r['id'] for r in user_cur.fetchall()}

        # ── ローカルグループ名→IDマップ ───────────────────
        mgmt_cur.execute("SELECT id, name FROM user_groups")
        name_to_id = {r['name']: r['id'] for r in mgmt_cur.fetchall()}

        g_applied = 0; g_skipped = 0; g_warnings = []

        # ════ PHASE 2: グループ ══════════════════════════
        def apply_group(g):
            """1グループを適用する。例外は呼び出し側で捕捉する"""
            nonlocal g_applied

            # 管理者不在時は実行者を暫定管理者にする（manager_user_id は NOT NULL）（#3）
            manager_id = email_to_id.get(g.get('manager_email'))
            if not manager_id:
                manager_id = actor if actor in email_to_id.values() else None
                if not manager_id:
                    # 実行者IDが users に見当たらない場合は最初の実在メンバーを使う
                    for m in (g.get('members') or []):
                        cand = email_to_id.get(m.get('email'))
                        if cand:
                            manager_id = cand
                            break
                if not manager_id:
                    g_warnings.append(
                        f"[{g.get('name')}] 管理者を決定できないためスキップしました"
                    )
                    return False
                g_warnings.append(
                    f"[{g.get('name')}] 管理者 {g.get('manager_email') or '(未設定)'} が不在のため、"
                    f"暫定管理者を設定しました"
                )

            gcols = _table_columns(mgmt_cur, 'user_groups')
            if g['name'] in name_to_id:
                gid  = name_to_id[g['name']]
                sets = ["description=%s", "manager_user_id=%s"]
                vals = [g.get('description', ''), manager_id]
                if 'updated_at' in gcols:
                    sets.append("updated_at=%s"); vals.append(_now_jst())
                vals.append(gid)
                mgmt_cur.execute(
                    f"UPDATE user_groups SET {', '.join(sets)} WHERE id=%s", vals
                )
            else:
                names  = ['name', 'description', 'manager_user_id']
                values = [g['name'], g.get('description', ''), manager_id]
                if 'created_at' in gcols:
                    names.append('created_at'); values.append(_now_jst())
                ph = ', '.join(['%s'] * len(names))
                mgmt_cur.execute(
                    f"INSERT INTO user_groups ({', '.join(names)}) VALUES ({ph})", values
                )
                gid = mgmt_cur.lastrowid
                name_to_id[g['name']] = gid

            mcols = _table_columns(mgmt_cur, 'user_group_memberships')
            mgmt_cur.execute("DELETE FROM user_group_memberships WHERE group_id=%s", (gid,))
            for m in (g.get('members') or []):
                uid = email_to_id.get(m.get('email'))
                if not uid:
                    g_warnings.append(f"[{g['name']}] {m.get('email')} が不在のためスキップ")
                    continue
                names  = ['group_id', 'user_id', 'valid_from', 'valid_until']
                values = [gid, uid, m.get('valid_from'), m.get('valid_until')]
                if 'created_at' in mcols:
                    names.append('created_at'); values.append(_now_jst())
                ph = ', '.join(['%s'] * len(names))
                mgmt_cur.execute(
                    f"INSERT IGNORE INTO user_group_memberships ({', '.join(names)}) "
                    f"VALUES ({ph})", values
                )
            g_applied += 1
            return True

        # 実行時点での再判定に使う情報
        local_group_names_now = set(name_to_id.keys())
        emails_now            = set(email_to_id.keys())

        for g in remote_groups:
            name = g.get('name')
            if not name:
                continue
            try:
                res_row = group_resolved.get(name)
                if res_row:
                    if res_row['resolution'] in ('skip', 'use_local'):
                        g_skipped += 1
                        continue
                    # use_remote：実行時点の最新データで適用する（#10）
                    apply_group(g)
                    continue

                # 解決行が無い＝分析時は問題なしと判定されたグループ。
                # 実行時点で新たに問題が出ていないか再判定する（#9）
                issues = _classify_group(g, local_group_names_now, emails_now)
                blocking = [i for i in issues if i['type'] == 'name_conflict']
                if blocking:
                    g_skipped += 1
                    g_warnings.append(
                        f"[{name}] 分析後に同名グループが出現したため未審査として適用しませんでした"
                    )
                    continue
                apply_group(g)

            except Exception as e:
                g_warnings.append(f"[{name}] 適用失敗: {e}")   # 1件の失敗で全体を落とさない（#8）

        mgmt_conn.commit()

        # ── ジョブ完了 ────────────────────────────────────
        summary = json.dumps({
            'users':  {'applied': u_applied, 'skipped': u_skipped,
                       'errors': u_errors, 'warnings': u_warnings},
            'groups': {'applied': g_applied, 'skipped': g_skipped, 'warnings': g_warnings},
            'atomic': single_tx
        }, ensure_ascii=False)

        mgmt_cur.execute(f"""
            UPDATE {MIGRATION_JOBS}
            SET status=%s, total_applied_users=%s, total_applied_groups=%s,
                summary=%s, finished_at=%s
            WHERE id=%s
        """, (
            'done' if not u_errors else 'error',
            u_applied, g_applied, summary, _now_jst(), job_id
        ))
        mgmt_conn.commit()

        all_warnings = u_warnings + g_warnings
        return jsonify({
            'success':    True,
            'u_applied':  u_applied,
            'u_skipped':  u_skipped,
            'g_applied':  g_applied,
            'g_skipped':  g_skipped,
            'warnings':   all_warnings,
            'errors':     u_errors,
            'message':    (f'ユーザ {u_applied} 件、グループ {g_applied} 件 適用完了'
                           + (f'（警告 {len(all_warnings)} 件）' if all_warnings else ''))
        })

    except Exception as e:
        logging.error("execute_migration: %s", e)
        import traceback; logging.error(traceback.format_exc())
        for obj in (mgmt_conn, user_conn):
            try:
                if obj is not None and obj.is_connected():
                    obj.rollback()
            except Exception:
                pass
        if started:
            _mark_job_error(job_id, e)
        return jsonify({'success': False, 'error': str(e)}), 500

    finally:
        for obj in (mgmt_cur, user_cur, mgmt_conn, user_conn):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass


# ============================================================
# ジョブ履歴
# ============================================================

@user_migration_bp.route('/get_jobs', methods=['GET'])
@login_required
def get_jobs():
    conn = None
    try:
        conn = mysql.connector.connect(**_db_mgmt())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(f"""
            SELECT j.*, a.site_name
            FROM {MIGRATION_JOBS} j
            LEFT JOIN {ALLIANCE_SITES} a ON j.alliance_site_id = a.id
            ORDER BY j.created_at DESC LIMIT 50
        """)
        jobs = cursor.fetchall()
        for j in jobs:
            for f in ('created_at', 'finished_at'):
                if j.get(f):
                    j[f] = j[f].strftime('%Y-%m-%d %H:%M:%S')
        return jsonify({'success': True, 'jobs': jobs})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn is not None and conn.is_connected():
            try:
                cursor.close()
            except Exception:
                pass
            conn.close()


# ────────────────────────────────────────────────────────────
# 公開API：許可リクエスト受付（送る側）
# 受け取りたいサイトがこのエンドポイントを呼ぶ
# ────────────────────────────────────────────────────────────

@user_migration_bp.route('/api/request_permission', methods=['POST'])
def api_request_permission():
    """
    受け取り側サイトが、このサイト（送る側）に許可申請を送る。
    認証不要（申請自体は誰でも送れるが、adminが承認しない限り有効にならない）。
    v2.1：申請元が発行した nonce を保存し、承認時に返送する。
    """
    ensure_tables()

    data = request.json or {}
    requester_url  = (data.get('requester_url') or '').rstrip('/')
    requester_name = data.get('requester_name') or requester_url
    nonce          = (data.get('nonce') or '')[:128]

    if not requester_url:
        return jsonify({'success': False, 'error': 'requester_url が必要です'}), 400
    if not requester_url.startswith(('http://', 'https://')):
        return jsonify({'success': False, 'error': 'requester_url が不正です'}), 400

    conn = None
    try:
        conn = mysql.connector.connect(**_db_mgmt())
        cursor = conn.cursor(dictionary=True)

        cursor.execute(f"""
            SELECT id, status FROM {EXPORT_PERMISSIONS}
            WHERE requester_url = %s
        """, (requester_url,))
        existing = cursor.fetchone()

        if existing:
            if existing['status'] == 'approved':
                return jsonify({'success': False,
                                'error': '既に承認済みです',
                                'status': 'approved'}), 409
            if existing['status'] == 'pending':
                # nonce だけ最新に差し替える（再申請で新しい nonce になっている場合に対応）
                cursor.execute(f"""
                    UPDATE {EXPORT_PERMISSIONS}
                    SET requester_name = %s, requester_nonce = %s, requested_at = %s
                    WHERE id = %s
                """, (requester_name, nonce, _now_jst(), existing['id']))
                conn.commit()
                return jsonify({'success': False,
                                'error': '申請済みです。承認をお待ちください',
                                'status': 'pending'}), 409
            # rejected → 再申請を受け付ける
            cursor.execute(f"""
                UPDATE {EXPORT_PERMISSIONS}
                SET requester_name = %s, requester_nonce = %s, status = 'pending',
                    requested_at = %s, processed_at = NULL, processed_by = NULL
                WHERE id = %s
            """, (requester_name, nonce, _now_jst(), existing['id']))
            conn.commit()
            return jsonify({
                'success': True,
                'message': '許可申請を再送信しました。管理者の承認をお待ちください。'
            })

        cursor.execute(f"""
            INSERT INTO {EXPORT_PERMISSIONS}
            (requester_url, requester_name, requester_nonce, status, requested_at)
            VALUES (%s, %s, %s, 'pending', %s)
        """, (requester_url, requester_name, nonce, _now_jst()))
        conn.commit()

        return jsonify({
            'success': True,
            'message': '許可申請を送信しました。管理者の承認をお待ちください。'
        })

    except Exception as e:
        logging.error("api_request_permission: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn is not None and conn.is_connected():
            try:
                cursor.close()
            except Exception:
                pass
            conn.close()


# ────────────────────────────────────────────────────────────
# 公開API：APIキー受け取り（受け取る側）
# 承認時に送る側から自動的に呼ばれる
# ────────────────────────────────────────────────────────────

@user_migration_bp.route('/api/receive_export_key', methods=['POST'])
def api_receive_export_key():
    """
    送る側が承認した際に、このサイト（受け取る側）に自動でAPIキーを送ってくる。

    v2.1（#1）：自サイトが発行した nonce と照合してから登録する。
      - nonce 一致          → api_key を登録し is_active=1 で有効化
      - nonce 不一致        → 403 で拒否
      - nonce 無し（旧版）  → 互換モード。既存サイトの上書きは拒否し、
                              新規のみ is_active=0 の保留状態で登録する
    """
    ensure_tables()

    data = request.json or {}
    exporter_url  = (data.get('exporter_url') or '').rstrip('/')
    exporter_name = data.get('exporter_name') or exporter_url
    api_key       = data.get('api_key') or ''
    nonce         = (data.get('nonce') or '')[:128]

    if not exporter_url or not api_key:
        return jsonify({'success': False, 'error': 'パラメータ不足'}), 400

    conn = None
    try:
        conn = mysql.connector.connect(**_db_mgmt())
        cursor = conn.cursor(dictionary=True)

        # ── 自サイトが出した申請と照合する ────────────────
        matched = None
        if nonce:
            cursor.execute(f"""
                SELECT id, nonce, requested_at, status
                FROM {OUTGOING_REQUESTS}
                WHERE exporter_url = %s
            """, (exporter_url,))
            row = cursor.fetchone()
            if not row or not row.get('nonce'):
                return jsonify({'success': False,
                                'error': 'このサイトへの申請記録がありません'}), 403
            if not secrets.compare_digest(str(row['nonce']), str(nonce)):
                logging.warning("receive_export_key: nonce mismatch from %s", exporter_url)
                return jsonify({'success': False, 'error': '照合に失敗しました'}), 403
            if row.get('requested_at'):
                age = _now_jst() - row['requested_at']
                if age > timedelta(hours=NONCE_TTL_HOURS):
                    cursor.execute(
                        f"UPDATE {OUTGOING_REQUESTS} SET status='expired' WHERE id=%s",
                        (row['id'],)
                    )
                    conn.commit()
                    return jsonify({'success': False,
                                    'error': '申請の有効期限が切れています。再度申請してください'}), 403
            matched = row

        cursor.execute(f"SELECT id, is_active FROM {ALLIANCE_SITES} WHERE site_url = %s",
                       (exporter_url,))
        existing = cursor.fetchone()

        if matched:
            # 照合済み → 正式登録
            if existing:
                cursor.execute(f"""
                    UPDATE {ALLIANCE_SITES}
                    SET site_name = %s, api_key = %s, is_active = 1
                    WHERE site_url = %s
                """, (exporter_name, api_key, exporter_url))
            else:
                cursor.execute(f"""
                    INSERT INTO {ALLIANCE_SITES}
                    (site_name, site_url, api_key, description, is_active, created_at)
                    VALUES (%s, %s, %s, 'FUJIN-P Alliance Site', 1, %s)
                """, (exporter_name, exporter_url, api_key, _now_jst()))

            cursor.execute(f"""
                UPDATE {OUTGOING_REQUESTS}
                SET status='received', received_at=%s, exporter_name=%s
                WHERE id=%s
            """, (_now_jst(), exporter_name, matched['id']))
            conn.commit()
            return jsonify({
                'success': True,
                'activated': True,
                'message': f'エクスポート許可を受け取りました（{exporter_name}）'
            })

        # ── 互換モード：nonce 非対応の旧バージョンからの送信 ──
        if existing:
            logging.warning(
                "receive_export_key: nonce無しでの既存サイト上書きを拒否 (%s)", exporter_url
            )
            return jsonify({
                'success': False,
                'error': ('照合情報が無いため、既に登録済みのサイト情報は更新できません。'
                          '移行元サイトを v2.1 に更新するか、テーシャの管理画面で'
                          'APIキーを手動更新してください。')
            }), 409

        cursor.execute(f"""
            INSERT INTO {ALLIANCE_SITES}
            (site_name, site_url, api_key, description, is_active, created_at)
            VALUES (%s, %s, %s, 'FUJIN-P Alliance Site（未承認・要有効化）', 0, %s)
        """, (exporter_name, exporter_url, api_key, _now_jst()))
        conn.commit()
        return jsonify({
            'success': True,
            'activated': False,
            'message': ('APIキーを受け取りましたが、照合情報が無いため保留状態で登録しました。'
                        'テーシャの管理画面で内容を確認して有効化してください。')
        })

    except Exception as e:
        logging.error("api_receive_export_key: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn is not None and conn.is_connected():
            try:
                cursor.close()
            except Exception:
                pass
            conn.close()


# ────────────────────────────────────────────────────────────
# 管理API：エクスポート許可一覧（送る側のダッシュボード用）
# ────────────────────────────────────────────────────────────

@user_migration_bp.route('/get_export_permissions', methods=['GET'])
@login_required
def get_export_permissions():
    """このサイトへのエクスポート許可申請一覧"""
    if not check_migration_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '権限がありません'}), 403

    ensure_tables()

    conn = None
    try:
        conn = mysql.connector.connect(**_db_mgmt())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(f"""
            SELECT id, requester_url, requester_name, status,
                   requested_at, processed_at
            FROM {EXPORT_PERMISSIONS}
            ORDER BY
                CASE status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END,
                requested_at DESC
        """)
        perms = cursor.fetchall()
        for p in perms:
            if p.get('requested_at'):
                p['requested_at'] = p['requested_at'].strftime('%Y-%m-%d %H:%M:%S')
            if p.get('processed_at'):
                p['processed_at'] = p['processed_at'].strftime('%Y-%m-%d %H:%M:%S')

        # 自サイトが送った申請の状況も返す
        cursor.execute(f"""
            SELECT id, exporter_url, exporter_name, status, requested_at, received_at
            FROM {OUTGOING_REQUESTS}
            ORDER BY requested_at DESC
        """)
        outgoing = cursor.fetchall()
        for o in outgoing:
            for f in ('requested_at', 'received_at'):
                if o.get(f):
                    o[f] = o[f].strftime('%Y-%m-%d %H:%M:%S')

        return jsonify({'success': True, 'permissions': perms, 'outgoing': outgoing})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn is not None and conn.is_connected():
            try:
                cursor.close()
            except Exception:
                pass
            conn.close()


@user_migration_bp.route('/approve_export_permission/<int:perm_id>', methods=['POST'])
@login_required
def approve_export_permission(perm_id):
    """
    申請を承認し、APIキーを申請元サイトに自動送信する。
    APIキーは Config.USER_MIGRATION_API_KEY（無ければ TABLE_SHARE_API_KEY）を使用。
    申請時に受け取った nonce を同送し、相手サイトで照合させる。
    """
    if not check_migration_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '権限がありません'}), 403

    conn = None
    try:
        conn = mysql.connector.connect(**_db_mgmt())
        cursor = conn.cursor(dictionary=True)

        cursor.execute(f"SELECT * FROM {EXPORT_PERMISSIONS} WHERE id = %s", (perm_id,))
        perm = cursor.fetchone()
        if not perm:
            return jsonify({'success': False, 'error': '申請が見つかりません'}), 404
        if perm['status'] == 'approved':
            return jsonify({'success': False, 'error': '既に承認済みです'}), 409

        cursor.execute(f"""
            UPDATE {EXPORT_PERMISSIONS}
            SET status = 'approved', processed_at = %s, processed_by = %s
            WHERE id = %s
        """, (_now_jst(), session.get('user_id'), perm_id))
        conn.commit()
        cursor.close(); conn.close(); conn = None

        api_key = (getattr(Config, 'USER_MIGRATION_API_KEY', None)
                   or getattr(Config, 'TABLE_SHARE_API_KEY', None)
                   or '')
        if not api_key:
            return jsonify({
                'success': True, 'push_ok': False,
                'message': '承認しました。⚠️ APIキーが未設定のため送信できませんでした。'
                           'config.py の TABLE_SHARE_API_KEY を確認してください。'
            })

        my_url  = getattr(Config, 'SITE_URL',
                          f"https://{Config.DB_ACCOUNT}.pythonanywhere.com")
        my_name = getattr(Config, 'SITE_DISPLAY_NAME', Config.DB_ACCOUNT)

        push_ok  = False
        push_msg = ''
        try:
            resp = requests.post(
                f"{perm['requester_url']}/user_migration/api/receive_export_key",
                json={
                    'exporter_url':  my_url,
                    'exporter_name': my_name,
                    'api_key':       api_key,
                    'nonce':         perm.get('requester_nonce') or ''
                },
                timeout=30
            )
            body     = resp.json() if resp.content else {}
            push_ok  = bool(resp.ok and body.get('success'))
            push_msg = body.get('message') or body.get('error') or ''
        except Exception as push_err:
            logging.warning("approve_export_permission push failed: %s", push_err)
            push_msg = str(push_err)

        return jsonify({
            'success':  True,
            'push_ok':  push_ok,
            'message':  '承認しました。' + (
                f'APIキーを自動送信しました。{push_msg}' if push_ok
                else f'⚠️ APIキーの自動送信に失敗しました（{push_msg}）。'
                     '手動でキーを共有してください。'
            )
        })

    except Exception as e:
        logging.error("approve_export_permission: %s", e)
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn is not None and conn.is_connected():
            try:
                cursor.close()
            except Exception:
                pass
            conn.close()


@user_migration_bp.route('/reject_export_permission/<int:perm_id>', methods=['POST'])
@login_required
def reject_export_permission(perm_id):
    """申請を却下する"""
    if not check_migration_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '権限がありません'}), 403

    conn = None
    try:
        conn = mysql.connector.connect(**_db_mgmt())
        cursor = conn.cursor()
        cursor.execute(f"""
            UPDATE {EXPORT_PERMISSIONS}
            SET status = 'rejected', processed_at = %s, processed_by = %s
            WHERE id = %s
        """, (_now_jst(), session.get('user_id'), perm_id))
        conn.commit()
        return jsonify({'success': True, 'message': '却下しました'})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn is not None and conn.is_connected():
            try:
                cursor.close()
            except Exception:
                pass
            conn.close()


# ────────────────────────────────────────────────────────────
# 管理API：許可リクエスト送信（受け取る側のダッシュボード用）
# ────────────────────────────────────────────────────────────

@user_migration_bp.route('/request_export_permission', methods=['POST'])
@login_required
def request_export_permission():
    """
    このサイト（受け取る側）が、指定した送る側サイトに許可申請を送る。
    送信前に nonce を発行して自サイトに記録し、承認返送時の照合に使う（#1）。
    """
    if not check_migration_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': '権限がありません'}), 403

    ensure_tables()

    data         = request.json or {}
    exporter_url = (data.get('exporter_url') or '').strip().rstrip('/')
    if not exporter_url:
        return jsonify({'success': False, 'error': 'exporter_url が必要です'}), 400
    if not exporter_url.startswith(('http://', 'https://')):
        return jsonify({'success': False, 'error': 'URLは http(s):// で始めてください'}), 400

    my_url  = getattr(Config, 'SITE_URL',
                      f"https://{Config.DB_ACCOUNT}.pythonanywhere.com")
    my_name = getattr(Config, 'SITE_DISPLAY_NAME', Config.DB_ACCOUNT)

    nonce = secrets.token_urlsafe(48)
    now   = _now_jst()

    conn = None
    try:
        # 先に自サイトへ nonce を記録する（記録できない申請は送らない）
        conn = mysql.connector.connect(**_db_mgmt())
        cursor = conn.cursor()
        cursor.execute(f"""
            INSERT INTO {OUTGOING_REQUESTS}
            (exporter_url, exporter_name, nonce, status, requested_at, requested_by)
            VALUES (%s, %s, %s, 'pending', %s, %s)
            ON DUPLICATE KEY UPDATE
                nonce = VALUES(nonce), status = 'pending',
                requested_at = VALUES(requested_at), requested_by = VALUES(requested_by),
                received_at = NULL
        """, (exporter_url, exporter_url, nonce, now, session.get('user_id')))
        conn.commit()
        cursor.close(); conn.close(); conn = None

        resp = requests.post(
            f"{exporter_url}/user_migration/api/request_permission",
            json={
                'requester_url':  my_url,
                'requester_name': my_name,
                'nonce':          nonce
            },
            timeout=30
        )
        if resp.ok:
            return jsonify(resp.json())
        # 相手が 409 を返す場合も内容をそのまま伝える
        try:
            body = resp.json()
            body.setdefault('success', False)
            return jsonify(body)
        except Exception:
            return jsonify({
                'success': False,
                'error': f'相手サイトへの接続に失敗しました（{resp.status_code}）'
            })

    except requests.RequestException as e:
        return jsonify({'success': False, 'error': f'ネットワークエラー: {e}'}), 500
    except Exception as e:
        logging.error("request_export_permission: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn is not None and conn.is_connected():
            conn.close()


@user_migration_bp.route('/return_to_fujin')
@login_required
def return_to_fujin():
    """FUJIN-Pダッシュボードに戻る"""
    return redirect_to_dashboard()