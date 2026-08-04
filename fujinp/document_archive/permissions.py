#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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
文書アーカイブ（document_archive）権限判定モジュール

このモジュールはアクセス制御に関する判定を一箇所に集約したものです。
以前は routes.py 内に
  - build_permission_clause() : 一覧・検索用（SQL の WHERE 句）
  - check_view_permission()   : 単票表示用（Python）
という同じルールの二重実装があり、ポリシー追加時に片方だけ直す危険がありました。
本モジュールでは ACCESS_POLICIES という単一の定義表を両者が参照します。

■ アクセスポリシー
  public         一般公開      : 全ユーザ（未ログインを含む）
  domestic       学内公開      : user_category = 'regular'
  private        非公開        : 作成者本人と admin のみ
  group          グループ限定  : 指定グループ所属者（カテゴリ不問）
  domestic_group 学内＋グループ: regular または 指定グループ所属者（和集合）

■ 共通ルール（全ポリシー横断）
  - admin は全件閲覧可
  - 作成者本人はポリシーに関わらず常に閲覧可
  - 定義表に無いポリシー値は閲覧不可（fail-safe）
"""

import logging
from datetime import datetime, timedelta, timezone

from flask import session

from db import get_db_cursor

logger = logging.getLogger(__name__)

# JST（このアプリのDBは naive な JST で日時を保持している）
JST = timezone(timedelta(hours=9), 'JST')

# 新規文書を作成できるフィーチャー名（features.feature_name）
CREATE_FEATURES = ('CoRePo管理者', 'FUJINPソムリエ')

# CoRePo へのインポートを許可するグループ名（colrep 側 COLREP_MANAGER_GROUP と一致させること）
COLREP_MANAGER_GROUP = 'コレポ管理者'


# ─────────────────────────────────────────────────────────────────
# アクセスポリシー定義（唯一の正）
# ─────────────────────────────────────────────────────────────────
#   label       : 画面表示用の名称
#   uses_groups : document_access_groups を使うか
#   by_category : このポリシーを無条件で閲覧できる user_category の集合
#   everyone    : カテゴリを問わず誰でも閲覧可か（未ログインを含む）
#
# 「作成者本人」「admin」は全ポリシー共通のため定義表には含めない。
#
# 注意: public は「ログイン済みなら誰でも」が従来仕様のため everyone=True とする。
#       by_category に guest/regular/admin を列挙する形にすると、
#       user_category が未設定・想定外の値のユーザが閲覧できなくなる（デグレード）。
ACCESS_POLICIES = {
    'public': {
        'label': '一般公開',
        'uses_groups': False,
        'by_category': set(),
        'everyone': True,
    },
    'domestic': {
        'label': '学内公開',
        'uses_groups': False,
        'by_category': {'regular'},
        'everyone': False,
    },
    'private': {
        'label': '非公開',
        'uses_groups': False,
        'by_category': set(),
        'everyone': False,
    },
    'group': {
        'label': 'グループ',
        'uses_groups': True,
        'by_category': set(),
        'everyone': False,
    },
    'domestic_group': {
        'label': '学内＋グループ',
        'uses_groups': True,
        'by_category': {'regular'},
        'everyone': False,
    },
}

VALID_ACCESS_POLICIES = frozenset(ACCESS_POLICIES.keys())

# 誰でも（未ログインを含め）閲覧できるポリシー
PUBLIC_POLICIES = frozenset(
    name for name, spec in ACCESS_POLICIES.items() if spec['everyone']
)

# 公開範囲がグループ選択を必要とするポリシー
GROUP_POLICIES = frozenset(
    name for name, spec in ACCESS_POLICIES.items() if spec['uses_groups']
)


# ─────────────────────────────────────────────────────────────────
# セッション情報の取り出し（型を必ず正規化する）
# ─────────────────────────────────────────────────────────────────

def current_user_id():
    """
    セッションの user_id を int で返す（未ログイン・不正値は 0）。

    以前は int() を通す箇所と通さない箇所が混在しており、セッションに
    文字列で入った場合に「一覧には出るのに詳細が開けない」という
    取りこぼしが発生しうる状態だった。取得は必ず本関数を通すこと。
    """
    raw = session.get('user_id')
    try:
        return int(raw) if raw not in (None, '') else 0
    except (TypeError, ValueError):
        logger.warning(f"[current_user_id] 不正な user_id: {raw!r}")
        return 0


def current_user_category():
    """セッションの user_category を返す（未設定は空文字）"""
    return session.get('user_category') or ''


def is_logged_in():
    return current_user_id() != 0


def is_admin_user():
    """管理者かどうかを判定"""
    return current_user_category() == 'admin'


# ─────────────────────────────────────────────────────────────────
# グループ・フィーチャーの取得（default DB）
# ─────────────────────────────────────────────────────────────────

def get_user_active_group_ids(user_id=None):
    """
    指定ユーザが現在所属している有効なグループIDのリストを返す（JST基準）。
    valid_from / valid_until による有効期間つき。
    """
    user_id = current_user_id() if user_id is None else int(user_id or 0)
    if not user_id:
        return []
    try:
        now_jst = datetime.now(JST).replace(tzinfo=None)
        with get_db_cursor(database='default') as (cursor, conn):
            cursor.execute(
                """
                SELECT group_id FROM user_group_memberships
                WHERE user_id = %s
                  AND (valid_from  IS NULL OR valid_from  <= %s)
                  AND (valid_until IS NULL OR valid_until >= %s)
                """,
                (user_id, now_jst, now_jst),
            )
            return [r['group_id'] for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"グループ取得エラー: {e}")
        return []


def get_group_name_map(group_ids):
    """グループIDと名前の対応 {id: name} を取得（default DB）"""
    ids = []
    for g in group_ids or ():
        try:
            value = int(g)
        except (TypeError, ValueError):
            logger.warning(f"[group] 不正なグループIDを無視: {g!r}")
            continue
        ids.append(value)
    ids = sorted(set(ids))
    if not ids:
        return {}
    try:
        with get_db_cursor(database='default') as (cursor, conn):
            placeholders = ','.join(['%s'] * len(ids))
            cursor.execute(
                f"SELECT id, name FROM user_groups WHERE id IN ({placeholders})",
                tuple(ids),
            )
            return {row['id']: row['name'] for row in cursor.fetchall()}
    except Exception as e:
        logger.error(f"グループ名取得エラー: {e}")
        return {}


def get_group_names_by_ids(group_ids):
    """グループIDリストからグループ名のリストを取得（指定順を保つ）"""
    name_map = get_group_name_map(group_ids)
    names = []
    for g in group_ids or ():
        try:
            key = int(g)
        except (TypeError, ValueError):
            continue
        if key in name_map:
            names.append(name_map[key])
    return names


def get_user_feature_names(user_id=None):
    """ユーザが保持するフィーチャー名の集合を返す（default DB）"""
    user_id = current_user_id() if user_id is None else int(user_id or 0)
    if not user_id:
        return set()
    try:
        with get_db_cursor(database='default') as (cursor, conn):
            cursor.execute(
                """
                SELECT f.feature_name
                FROM features f
                INNER JOIN user_features uf ON f.id = uf.feature_id
                WHERE uf.user_id = %s
                """,
                (user_id,),
            )
            return {row['feature_name'] for row in cursor.fetchall()}
    except Exception as e:
        logger.error(f"フィーチャー取得エラー: {e}")
        return set()


# ─────────────────────────────────────────────────────────────────
# 操作権限
# ─────────────────────────────────────────────────────────────────

def can_create_document():
    """新規文書を作成できるか（admin、または作成フィーチャー保持者）"""
    if is_admin_user():
        return True
    if not is_logged_in():
        return False
    return bool(get_user_feature_names() & set(CREATE_FEATURES))


def can_modify_document(document):
    """
    文書を編集・削除できるか。

    admin / 作成者本人 / created_by = 0（作成者不明）の3条件。
    document は dict（created_by を含む）または None。
    """
    if is_admin_user():
        return True
    if not document or not is_logged_in():
        return False
    raw = document.get('created_by')
    if raw is None:
        # NULL は「作成者不明(0)」とは区別し、安全側に倒して不可とする
        return False
    try:
        created_by = int(raw)
    except (TypeError, ValueError):
        return False
    # created_by = 0 は「作成者不明」で、従来どおりログイン済みユーザなら編集可
    if created_by == 0:
        return True
    return created_by == current_user_id()


# 旧名（互換のため残す。新規コードでは can_modify_document を使うこと）
can_delete_document = can_modify_document


def can_import_corepo():
    """アーカイブ済みCoRePoプロジェクトをインポートできるか"""
    if is_admin_user():
        return True
    if not is_logged_in():
        return False
    names = get_group_names_by_ids(get_user_active_group_ids())
    return COLREP_MANAGER_GROUP in names


# ─────────────────────────────────────────────────────────────────
# 閲覧権限（単票用・Python判定）
# ─────────────────────────────────────────────────────────────────

def check_view_permission(document, user_id=None):
    """
    文書を閲覧できるかを判定する。ACCESS_POLICIES を唯一の根拠とする。

    未ログイン（user_id=0）の場合は everyone が True のポリシーのみ許可。
    """
    if not document:
        return False

    user_id = current_user_id() if user_id is None else int(user_id or 0)
    policy = document.get('access_policy')
    spec = ACCESS_POLICIES.get(policy)

    # admin は全件可（build_permission_clause の "1=1" と揃える）
    if is_admin_user():
        return True

    # 定義表に無いポリシーは admin 以外一律不可（fail-safe）
    if spec is None:
        logger.warning(f"[check_view_permission] 未知のポリシー: {policy!r}")
        return False

    # 誰でも見えるポリシー（未ログインを含む）
    if spec['everyone']:
        return True

    # 未ログインはここで打ち切り
    if not user_id:
        return False

    # 作成者本人は常に可
    try:
        created_by = int(document.get('created_by') or 0)
    except (TypeError, ValueError):
        created_by = 0
    if created_by and created_by == user_id:
        return True

    # カテゴリによる許可
    if current_user_category() in spec['by_category']:
        return True

    # グループによる許可
    if spec['uses_groups']:
        allowed = set(document.get('allowed_group_ids') or [])
        if allowed and (set(get_user_active_group_ids(user_id)) & allowed):
            return True

    return False


# ─────────────────────────────────────────────────────────────────
# 閲覧権限（一覧・検索用・SQL WHERE 句）
# ─────────────────────────────────────────────────────────────────

def build_permission_clause(user_id=None):
    """
    一覧・検索の WHERE 句に差し込む権限条件を (句, パラメータtuple) で返す。

    値は全てプレースホルダ（%s）で渡す。呼び出し側は必ず
        cursor.execute(sql, clause_params + その他のパラメータ)
    の順でパラメータを連結すること（句が SQL 中で先に現れるため）。

    対象テーブルには別名 d（public_documents）を付けておくこと。
    """
    user_id = current_user_id() if user_id is None else int(user_id or 0)

    # admin は全件
    if is_admin_user():
        return "1=1", ()

    category = current_user_category()

    # 未ログイン：誰でも見えるポリシーのみ
    if not user_id:
        anon = sorted(PUBLIC_POLICIES)
        if not anon:
            return "1=0", ()
        placeholders = ','.join(['%s'] * len(anon))
        return f"(d.access_policy IN ({placeholders}))", tuple(anon)

    group_ids = get_user_active_group_ids(user_id)

    conditions = ["d.created_by = %s"]          # 作成者本人は常に可
    params = [user_id]

    # 誰でも見える／カテゴリで見えるポリシー群
    by_category = sorted(
        p for p, s in ACCESS_POLICIES.items()
        if s['everyone'] or category in s['by_category']
    )
    if by_category:
        placeholders = ','.join(['%s'] * len(by_category))
        conditions.append(f"d.access_policy IN ({placeholders})")
        params.extend(by_category)

    # グループ所属で見えるポリシー群（カテゴリで既に見えるものは除外して重複を避ける）
    if group_ids:
        group_policies = sorted(
            p for p, s in ACCESS_POLICIES.items()
            if s['uses_groups'] and p not in by_category
        )
        if group_policies:
            p_ph = ','.join(['%s'] * len(group_policies))
            g_ph = ','.join(['%s'] * len(group_ids))
            conditions.append(
                f"""(d.access_policy IN ({p_ph})
                     AND EXISTS (
                         SELECT 1 FROM document_access_groups dag
                         WHERE dag.doc_id = d.id AND dag.group_id IN ({g_ph})
                     ))"""
            )
            params.extend(group_policies)
            params.extend(group_ids)

    clause = "(\n    " + "\n    OR ".join(conditions) + "\n)"
    return clause, tuple(params)


def policy_label(policy):
    """公開範囲の表示名（未知の値は「不明」）"""
    spec = ACCESS_POLICIES.get(policy)
    return spec['label'] if spec else '不明'
