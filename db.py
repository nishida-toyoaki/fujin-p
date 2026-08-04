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

import mysql.connector
from contextlib import contextmanager
from config import Config

# ==================== データベース接続設定（集中管理） ====================

class DatabaseConfig:
    """データベース接続設定の集中管理クラス"""

    # 基本接続情報（全DBで共通）
    BASE_CONFIG = {
        'host': Config.DB_HOST,
        'user': Config.DB_USER,
        'password': Config.DB_PASSWORD,
        'charset': 'utf8mb4',
        'use_pure': True
    }

    @classmethod
    def get_config(cls, database):
        """データベース接続設定を取得"""
        config = cls.BASE_CONFIG.copy()
        config['database'] = database
        return config

    @classmethod
    def default(cls):
        """defaultデータベース用設定"""
        return cls.get_config(Config.DB_DEFAULT)

    @classmethod
    def fujinp(cls):
        """fujinpデータベース用設定"""
        return cls.get_config(Config.DB_FUJINP)

    @classmethod
    def public(cls):
        """publicデータベース用設定"""
        return cls.get_config(Config.DB_PUBLIC)

    @classmethod
    def base(cls):
        """データベース名を指定しない基本設定"""
        return cls.BASE_CONFIG.copy()


# ==================== 旧形式との互換性（非推奨） ====================

# 既存コードとの互換性のため残す（将来的に削除予定）
base_db_config = DatabaseConfig.base()
default_db_config = DatabaseConfig.default()
fujinp_db_config = DatabaseConfig.fujinp()


# ==================== コンテキストマネージャー ====================

@contextmanager
def get_db_connection(database='default', use_public=False):
    """
    データベース接続（コンテキストマネージャー）

    Args:
        database (str): 'default', 'fujinp', 'public' のいずれか
        use_public (bool): 後方互換性のため（非推奨）
    """
    if use_public:
        config = DatabaseConfig.public()
    elif database == 'default':
        config = DatabaseConfig.default()
    elif database == 'fujinp':
        config = DatabaseConfig.fujinp()
    elif database == 'public':
        config = DatabaseConfig.public()
    else:
        # カスタムデータベース名
        config = DatabaseConfig.get_config(database)

    conn = mysql.connector.connect(**config)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def get_db_cursor(database='default', use_public=False, dictionary=True):
    """
    カーソル取得（コンテキストマネージャー）

    Args:
        database (str): 'default', 'fujinp', 'public' のいずれか
        use_public (bool): 後方互換性のため（非推奨）
        dictionary (bool): 辞書形式でデータを取得するか
    """
    with get_db_connection(database=database, use_public=use_public) as conn:
        cursor = conn.cursor(dictionary=dictionary)
        try:
            yield cursor, conn
        finally:
            cursor.close()


# ==================== テーブル名管理（マイグレーション対応） ====================

class Tables:
    """テーブル名の集中管理クラス"""

    # ===== データベース名 =====
    DB_DEFAULT = Config.DB_DEFAULT
    DB_FUJINP = Config.DB_FUJINP
    DB_PUBLIC = Config.DB_PUBLIC

    # ===== default データベースのテーブル =====
    USERS = f"{DB_DEFAULT}.users"
    FEATURES = f"{DB_DEFAULT}.features"
    USER_FEATURES = f"{DB_DEFAULT}.user_features"
    USER_GROUPS = f"{DB_DEFAULT}.user_groups"
    USER_GROUP_MEMBERSHIPS = f"{DB_DEFAULT}.user_group_memberships"
    USER_GROUP_SUBGROUPS = f"{DB_DEFAULT}.user_group_subgroups"
    USER_GROUP_GLOBAL_MANAGERS = f"{DB_DEFAULT}.user_group_global_managers"

    # ===== fujinp データベースのテーブル =====
    COLREP_PROJECTS = f"{DB_FUJINP}.{Config.COLREP_PROJECTS_TABLE}"

    # Table Cycle設定
    TABLE_SNAPSHOTS = f"{DB_DEFAULT}.table_snapshots"

    @classmethod
    def get(cls, table_name, database='default'):
        """
        動的にテーブル名を取得

        Args:
            table_name (str): テーブル名
            database (str): 'default', 'fujinp', 'public' のいずれか

        Returns:
            str: 完全修飾テーブル名（例: "nishida$default.users"）
        """
        db_map = {
            'default': cls.DB_DEFAULT,
            'fujinp': cls.DB_FUJINP,
            'public': cls.DB_PUBLIC
        }
        db = db_map.get(database, cls.DB_DEFAULT)
        return f"{db}.{table_name}"


# ==================== ユーティリティ関数 ====================

def test_connection(database='default'):
    """
    データベース接続テスト

    Args:
        database (str): テストするデータベース

    Returns:
        bool: 接続成功時 True
    """
    try:
        with get_db_connection(database=database) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()
            cursor.close()
            return True
    except Exception as e:
        print(f"Database connection failed: {e}")
        return False