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

"""awami routes - あわみ（our_meeting）"""
import datetime
import json
import logging
import re
from pytz import timezone
from flask import (
    render_template, request, jsonify, session,
    redirect, url_for, flash
)
import mysql.connector

# FUJIN-P共通モジュール（常に存在する前提）
from config import Config
from db import DatabaseConfig
from decorators import login_required
from auth import redirect_to_dashboard

from . import our_meeting_bp

# =========================================================
# 日時ヘルパー（FUJIN-P実装ガイド v2.0 セクション5.2）
# =========================================================
JST = timezone('Asia/Tokyo')


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


# =========================================================
# 共通ユーティリティ
# =========================================================
def _connect():
    return mysql.connector.connect(**DatabaseConfig.default())


# 文書アーカイブと同一のアクセスポリシー語彙（＋ノードでは NULL=キャンバスに従う）
VALID_ACCESS_POLICIES = {'public', 'domestic', 'private', 'group', 'domestic_group'}


def get_user_category(user_id):
    """users.category を返す（文書アーカイブ同様，まず session を見る）。"""
    cat = session.get('user_category')
    if cat:
        return cat
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT category FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()
        return row['category'] if row else None
    except Exception as e:
        logging.error("awami get_user_category error: %s", e)
        return None
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


def is_admin(category):
    return category == 'admin'


def get_effective_group_ids(user_id):
    """有効なグループID集合を返す。

    まいぐる（user_groups）が公開する get_user_effective_group_ids() を
    最優先で使う。import パスが環境と異なる場合は、下の import 行を
    実際の公開場所に合わせて修正すること（組み込み手順参照）。
    """
    try:
        from fujinp.user_groups import get_user_effective_group_ids
        return set(get_user_effective_group_ids(user_id))
    except Exception:
        pass
    try:
        from user_groups import get_user_effective_group_ids
        return set(get_user_effective_group_ids(user_id))
    except Exception:
        pass
    # フォールバック（まいぐるの関数が import できない場合の直接照会）
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        now = get_jst_now()
        cursor.execute("""
            SELECT group_id FROM user_group_memberships
            WHERE user_id = %s
              AND (valid_from IS NULL OR valid_from <= %s)
              AND (valid_until IS NULL OR valid_until >= %s)
        """, (user_id, now, now))
        return set(r['group_id'] for r in cursor.fetchall())
    except Exception as e:
        logging.error("awami get_effective_group_ids fallback error: %s", e)
        return set()
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


def list_all_groups():
    """ACL設定UI用のグループ一覧（id, name）。"""
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id, name FROM user_groups ORDER BY name")
        return cursor.fetchall()
    except Exception as e:
        logging.error("awami list_all_groups error: %s", e)
        return []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


# =========================================================
# アクセスポリシー共通判定（文書アーカイブと同一の語彙）
# =========================================================
def entity_visible(policy, allowed_group_ids, category, user_group_ids):
    """owner／admin／作成者の特例は呼び出し側で処理済みであること。"""
    if policy == 'public':
        return True
    if policy == 'domestic':
        return category == 'regular'
    if policy == 'group':
        return bool(set(user_group_ids) & set(allowed_group_ids))
    if policy == 'domestic_group':
        if category == 'regular':
            return True
        return bool(set(user_group_ids) & set(allowed_group_ids))
    return False          # private・不明値は非公開


def _load_group_map(cursor, table, key_col, ids):
    """{id: [group_id, ...]} を返す汎用ローダ。"""
    result = {i: [] for i in ids}
    if not ids:
        return result
    fmt = ','.join(['%s'] * len(ids))
    cursor.execute(
        "SELECT " + key_col + " AS k, group_id FROM " + table +
        " WHERE " + key_col + " IN (" + fmt + ")", list(ids))
    for r in cursor.fetchall():
        result[r['k']].append(r['group_id'])
    return result


def load_canvas_group_ids(cursor, canvas_ids):
    return _load_group_map(cursor, 'awami_canvas_access_groups',
                           'canvas_id', canvas_ids)


def _normalize_policy(policy, allow_inherit):
    """無効値は安全側 private に。allow_inherit=True なら None を許す。"""
    if allow_inherit and policy in (None, '', 'inherit'):
        return None
    if policy not in VALID_ACCESS_POLICIES:
        return 'private'
    return policy


def save_canvas_access(cursor, canvas_id, access_policy, group_ids):
    access_policy = _normalize_policy(access_policy, allow_inherit=False)
    cursor.execute(
        "UPDATE awami_canvases SET access_policy = %s WHERE id = %s",
        (access_policy, canvas_id))
    cursor.execute(
        "DELETE FROM awami_canvas_access_groups WHERE canvas_id = %s",
        (canvas_id,))
    if access_policy in ('group', 'domestic_group'):
        for g in group_ids or []:
            cursor.execute(
                "INSERT INTO awami_canvas_access_groups (canvas_id, group_id) "
                "VALUES (%s, %s)", (canvas_id, int(g)))
    return access_policy


def get_canvas(cursor, canvas_id):
    cursor.execute(
        "SELECT id, name, description, owner_user_id, access_policy, "
        "created_at, updated_at FROM awami_canvases WHERE id = %s",
        (canvas_id,))
    return cursor.fetchone()


def can_view_canvas(cursor, canvas, user_id, category, group_ids):
    if canvas is None:
        return False
    if canvas['owner_user_id'] == user_id or is_admin(category):
        return True
    allowed = load_canvas_group_ids(cursor, [canvas['id']])[canvas['id']]
    return entity_visible(canvas.get('access_policy'), allowed,
                          category, group_ids)


# =========================================================
# ノードのアクセスポリシー（文書アーカイブと同一の判定）
# =========================================================
def load_node_group_ids(cursor, node_ids):
    return _load_group_map(cursor, 'awami_node_access_groups',
                           'node_id', node_ids)


def node_visible(node, allowed_group_ids, user_id, category, user_group_ids):
    """文書アーカイブの can_view と同じ規則．

    access_policy が NULL のノードは「キャンバスに従う」＝キャンバスを
    開ける人には見える．admin と作成者は常に可．
    """
    if is_admin(category) or node.get('created_by') == user_id:
        return True
    policy = node.get('access_policy')
    if policy is None or policy == '':
        return True                      # キャンバスに従う（ゲートは通過済み）
    return entity_visible(policy, allowed_group_ids, category, user_group_ids)


def save_node_access(cursor, node_id, access_policy, group_ids):
    """None/'inherit' は NULL（キャンバスに従う）．無効値は private に。"""
    access_policy = _normalize_policy(access_policy, allow_inherit=True)
    cursor.execute(
        "UPDATE awami_nodes SET access_policy = %s WHERE id = %s",
        (access_policy, node_id))
    cursor.execute(
        "DELETE FROM awami_node_access_groups WHERE node_id = %s", (node_id,))
    if access_policy in ('group', 'domestic_group'):
        for g in group_ids or []:
            cursor.execute(
                "INSERT INTO awami_node_access_groups (node_id, group_id) "
                "VALUES (%s, %s)", (node_id, int(g)))
    return access_policy


# =========================================================
# FUJIN-Pダッシュボードへ戻る（プラットフォーム標準の中継ルート）
# 仕様: 「FUJIN-Pダッシュボードへの戻り方」参照。@login_required は付けない。
# =========================================================
@our_meeting_bp.route('/return_to_fujin')
def return_to_fujin():
    """FUJIN-Pダッシュボードに戻る（ユーザカテゴリに応じた戻り先へ）"""
    return redirect_to_dashboard()


# =========================================================
# 画面
# =========================================================
@our_meeting_bp.route('/')
@login_required
def index():
    """キャンバス一覧（単純なリスト）"""
    user_id = session.get('user_id')
    category = get_user_category(user_id)
    group_ids = get_effective_group_ids(user_id)
    canvases = []
    all_groups = list_all_groups()
    group_names = {g['id']: g['name'] for g in all_groups}
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT c.id, c.name, c.description, c.owner_user_id, "
            "       c.access_policy, c.updated_at, u.full_name AS owner_name "
            "FROM awami_canvases c "
            "LEFT JOIN users u ON u.id = c.owner_user_id "
            "ORDER BY c.updated_at DESC, c.id DESC")
        rows = cursor.fetchall()
        cgroups = load_canvas_group_ids(cursor, [r['id'] for r in rows])
        for r in rows:
            is_owner = (r['owner_user_id'] == user_id)
            allowed = cgroups[r['id']]
            if is_owner or is_admin(category) \
                    or entity_visible(r['access_policy'], allowed,
                                      category, group_ids):
                canvases.append({
                    'id': r['id'],
                    'name': r['name'],
                    'description': r['description'] or '',
                    'owner_name': r['owner_name'] or '',
                    'updated_at': fmt_datetime(r['updated_at']),
                    'is_owner': is_owner,
                    'access_policy': r['access_policy'] or 'private',
                    'group_ids': allowed,
                    'group_names': [group_names.get(g, '#%d' % g)
                                    for g in allowed],
                })
    except Exception as e:
        logging.error("awami index error: %s", e)
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()
    return render_template('awami/index.html',
                           canvases=canvases,
                           groups=all_groups,
                           is_admin=(category == 'admin'))


@our_meeting_bp.route('/canvas/<int:canvas_id>')
@login_required
def canvas_view(canvas_id):
    """キャンバス画面（ナラティブ・ネットワーク）"""
    user_id = session.get('user_id')
    category = get_user_category(user_id)
    group_ids = get_effective_group_ids(user_id)
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        canvas = get_canvas(cursor, canvas_id)
        if not can_view_canvas(cursor, canvas, user_id, category, group_ids):
            flash('このキャンバスへのアクセス権がありません')
            return redirect(url_for('our_meeting.index'))
        is_owner = (canvas['owner_user_id'] == user_id)
        # CoRePo連携（新規文書ノード）用：キャンバスの許可グループもテンプレートへ渡す。
        # ノードの公開範囲が「↰ キャンバスに従う」のとき、CoRePo側の共有設定へ
        # 同期する値（access_policy＋グループ）としてフロントが使う。
        canvas_group_ids = load_canvas_group_ids(cursor, [canvas_id])[canvas_id]
        return render_template('awami/canvas.html',
                               canvas=canvas, is_owner=is_owner,
                               canvas_group_ids=canvas_group_ids,
                               is_admin=is_admin(category))
    except Exception as e:
        logging.error("awami canvas_view error: %s", e)
        return redirect(url_for('our_meeting.index'))
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


# =========================================================
# API：キャンバス
# =========================================================
@our_meeting_bp.route('/api/canvas/create', methods=['POST'])
@login_required
def api_canvas_create():
    try:
        data = request.json or {}
        name = (data.get('name') or '').strip()
        if not name:
            return jsonify({'success': False, 'error': '名前は必須です'}), 400
        user_id = session.get('user_id')
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        now = get_jst_now()
        cursor.execute(
            "INSERT INTO awami_canvases "
            "(name, description, owner_user_id, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (name, data.get('description') or '', user_id, now, now))
        canvas_id = cursor.lastrowid
        save_canvas_access(cursor, canvas_id,
                           data.get('access_policy'),
                           data.get('access_group_ids'))
        conn.commit()
        return jsonify({'success': True, 'id': canvas_id})
    except Exception as e:
        logging.error("awami api_canvas_create error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


def _require_owner(cursor, canvas_id, user_id):
    canvas = get_canvas(cursor, canvas_id)
    if canvas is None:
        return None, (jsonify({'success': False, 'error': 'キャンバスがありません'}), 404)
    if canvas['owner_user_id'] != user_id:
        return None, (jsonify({'success': False, 'error': '編集権限がありません'}), 403)
    return canvas, None


@our_meeting_bp.route('/api/canvas/<int:canvas_id>/update', methods=['POST'])
@login_required
def api_canvas_update(canvas_id):
    try:
        data = request.json or {}
        user_id = session.get('user_id')
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        canvas, err = _require_owner(cursor, canvas_id, user_id)
        if err:
            return err
        cursor.execute(
            "UPDATE awami_canvases SET name = %s, description = %s, "
            "updated_at = %s WHERE id = %s",
            ((data.get('name') or canvas['name']).strip(),
             data.get('description') or '', get_jst_now(), canvas_id))
        save_canvas_access(cursor, canvas_id,
                           data.get('access_policy'),
                           data.get('access_group_ids'))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("awami api_canvas_update error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@our_meeting_bp.route('/api/canvas/<int:canvas_id>/delete', methods=['POST'])
@login_required
def api_canvas_delete(canvas_id):
    try:
        user_id = session.get('user_id')
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        canvas, err = _require_owner(cursor, canvas_id, user_id)
        if err:
            return err
        cursor.execute(
            "SELECT id FROM awami_nodes WHERE canvas_id = %s", (canvas_id,))
        node_ids = [r['id'] for r in cursor.fetchall()]
        cursor.execute(
            "DELETE m FROM awami_edge_members m "
            "JOIN awami_edges e ON e.id = m.edge_id "
            "WHERE e.canvas_id = %s", (canvas_id,))
        cursor.execute("DELETE FROM awami_edges WHERE canvas_id = %s", (canvas_id,))
        if node_ids:
            fmt = ','.join(['%s'] * len(node_ids))
            cursor.execute(
                "DELETE FROM awami_node_access_groups "
                "WHERE node_id IN (" + fmt + ")", node_ids)
        cursor.execute("DELETE FROM awami_nodes WHERE canvas_id = %s", (canvas_id,))
        cursor.execute(
            "DELETE FROM awami_canvas_access_groups WHERE canvas_id = %s",
            (canvas_id,))
        cursor.execute("DELETE FROM awami_canvases WHERE id = %s", (canvas_id,))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("awami api_canvas_delete error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


# =========================================================
# API：キャンバスデータ（表示フィルタの中核）
# =========================================================
@our_meeting_bp.route('/api/canvas/<int:canvas_id>/data')
@login_required
def api_canvas_data(canvas_id):
    """可視ノード・可視エッジ・結合子タイプ（＋ownerならグループ一覧）を返す。

    表示規則（第1版）：
      - owner には mother ネット全体が見える。
      - それ以外のユーザには、ノードACLに合致しないノードは見えない。
        ACL未設定のノードはキャンバスに従う（＝キャンバスが開ければ見える）。
      - エッジは不可視ノードを端点リストから落とし、主ノード（position=1）が
        不可視、または可視端点が2未満なら、エッジごと非表示。
    """
    user_id = session.get('user_id')
    category = get_user_category(user_id)
    group_ids = get_effective_group_ids(user_id)
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        canvas = get_canvas(cursor, canvas_id)
        if not can_view_canvas(cursor, canvas, user_id, category, group_ids):
            return jsonify({'success': False, 'error': 'アクセス権がありません'}), 403
        is_owner = (canvas['owner_user_id'] == user_id)
        can_edit = is_owner            # 第1版：編集はownerのみ

        # --- ノード（文書アーカイブ同一のポリシー判定）---
        cursor.execute(
            "SELECT id, label, url, note, x, y, created_by, access_policy "
            "FROM awami_nodes WHERE canvas_id = %s", (canvas_id,))
        rows = cursor.fetchall()
        node_groups = load_node_group_ids(cursor, [r['id'] for r in rows])
        nodes = []
        visible_ids = set()
        for r in rows:
            if node_visible(r, node_groups[r['id']], user_id, category, group_ids):
                visible_ids.add(r['id'])
                node = {'id': r['id'], 'label': r['label'],
                        'url': r['url'] or '', 'note': r['note'] or '',
                        'x': float(r['x']), 'y': float(r['y'])}
                if can_edit or is_admin(category):
                    node['access_policy'] = r['access_policy']
                    node['access_group_ids'] = node_groups[r['id']]
                nodes.append(node)

        # --- エッジ（ハイパーエッジ：m入力→[ラベル]→n出力）---
        cursor.execute(
            "SELECT e.id, e.connector_type_id, e.note, e.label_x, e.label_y, "
            "       t.category AS type_category, t.name AS type_name, t.directed "
            "FROM awami_edges e "
            "JOIN awami_connector_types t ON t.id = e.connector_type_id "
            "WHERE e.canvas_id = %s", (canvas_id,))
        edge_rows = cursor.fetchall()
        edges = []
        if edge_rows:
            fmt = ','.join(['%s'] * len(edge_rows))
            cursor.execute(
                "SELECT edge_id, node_id, role, position FROM awami_edge_members "
                "WHERE edge_id IN (" + fmt + ") ORDER BY edge_id, role, position",
                [e['id'] for e in edge_rows])
            members = {}
            for m in cursor.fetchall():
                members.setdefault(m['edge_id'], []).append(m)
            for e in edge_rows:
                mem = members.get(e['id'], [])
                # 閲覧者に見えないノードは端点から落とす。入力・出力の
                # どちらかが空になったらエッジごと非表示。
                inputs = [m['node_id'] for m in mem
                          if m['role'] == 'in' and m['node_id'] in visible_ids]
                outputs = [m['node_id'] for m in mem
                           if m['role'] != 'in' and m['node_id'] in visible_ids]
                if not inputs or not outputs:
                    continue
                edges.append({
                    'id': e['id'],
                    'type_id': e['connector_type_id'],
                    'type_category': e['type_category'],
                    'type_name': e['type_name'],
                    'directed': bool(e['directed']),
                    'note': e['note'] or '',
                    'inputs': inputs,
                    'outputs': outputs,
                    'label_x': float(e['label_x']) if e['label_x'] is not None else None,
                    'label_y': float(e['label_y']) if e['label_y'] is not None else None,
                })

        # --- 結合子タイプ ---
        cursor.execute(
            "SELECT id, category, name, directed FROM awami_connector_types "
            "WHERE is_active = 1 ORDER BY sort_order, id")
        types = [{'id': t['id'], 'category': t['category'],
                  'name': t['name'], 'directed': bool(t['directed'])}
                 for t in cursor.fetchall()]

        payload = {'success': True,
                   'canvas': {'id': canvas['id'], 'name': canvas['name'],
                              'description': canvas['description'] or '',
                              'is_owner': is_owner},
                   'nodes': nodes, 'edges': edges, 'connector_types': types}
        if is_owner or is_admin(category):
            payload['groups'] = list_all_groups()
        return jsonify(payload)
    except Exception as e:
        logging.error("awami api_canvas_data error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


# =========================================================
# API：文脈探索（URL逆引き）
# 指定URLを実体URLに持つノードを含み、かつ閲覧者に見えるキャンバスを返す。
# 他アプリ（CoRePo等）から「このURLを参照しているキャンバス」を辿るために使う。
# =========================================================
def _normalize_url(u):
    """フロント（locateNode）と同じ正規化：scheme+host除去・末尾スラッシュ除去・小文字化。"""
    s = (u or '').strip()
    if not s:
        return ''
    s = s.rstrip('/')
    s = re.sub(r'^https?://[^/]+', '', s, flags=re.IGNORECASE)
    return s.lower()


@our_meeting_bp.route('/api/find_by_url')
@login_required
def api_find_by_url():
    """?url=<実体URL> を参照するノードを含む、閲覧可能なキャンバス一覧を返す。

    返却は閲覧者本人の権限で絞り込む：
      - キャンバスを開ける（can_view_canvas）こと
      - かつ、その中の該当ノードが本人に見える（node_visible）こと
    複数キャンバスに跨る場合は candidates に列挙。呼び出し側で選択させる。
    """
    target = _normalize_url(request.args.get('url', ''))
    if not target:
        return jsonify({'success': False, 'error': 'urlパラメータが必要です'}), 400
    user_id = session.get('user_id')
    category = get_user_category(user_id)
    group_ids = get_effective_group_ids(user_id)
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        # URLの前方一致で候補ノードを粗く絞り、厳密一致は正規化で判定
        cursor.execute(
            "SELECT n.id, n.canvas_id, n.label, n.url, n.created_by, "
            "       n.access_policy, c.name AS canvas_name, c.owner_user_id "
            "FROM awami_nodes n "
            "JOIN awami_canvases c ON c.id = n.canvas_id "
            "WHERE n.url IS NOT NULL AND n.url <> ''")
        rows = cursor.fetchall()
        node_groups = load_node_group_ids(cursor, [r['id'] for r in rows])

        # キャンバスの可視性はキャンバス単位で1回だけ判定してキャッシュ
        canvas_ok = {}
        canvases = {}
        for r in rows:
            if _normalize_url(r['url']) != target:
                continue
            cid = r['canvas_id']
            if cid not in canvas_ok:
                cv = get_canvas(cursor, cid)
                canvas_ok[cid] = can_view_canvas(
                    cursor, cv, user_id, category, group_ids)
            if not canvas_ok[cid]:
                continue
            if not node_visible(r, node_groups[r['id']],
                                user_id, category, group_ids):
                continue
            # 同一キャンバスに複数該当ノードがあっても、キャンバスは1件に集約
            if cid not in canvases:
                canvases[cid] = {
                    'canvas_id': cid,
                    'canvas_name': r['canvas_name'],
                    'node_label': r['label'],
                    'url': url_for('our_meeting.canvas_view', canvas_id=cid,
                                   locate=r['url']),
                }
        candidates = sorted(canvases.values(), key=lambda x: x['canvas_id'])
        return jsonify({'success': True, 'count': len(candidates),
                        'candidates': candidates})
    except Exception as e:
        logging.error("awami api_find_by_url error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


# =========================================================
# API：ノード
# =========================================================
@our_meeting_bp.route('/api/canvas/<int:canvas_id>/node/create', methods=['POST'])
@login_required
def api_node_create(canvas_id):
    try:
        data = request.json or {}
        label = (data.get('label') or '').strip()
        if not label:
            return jsonify({'success': False, 'error': 'ラベルは必須です'}), 400
        user_id = session.get('user_id')
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        canvas, err = _require_owner(cursor, canvas_id, user_id)
        if err:
            return err
        now = get_jst_now()
        cursor.execute(
            "INSERT INTO awami_nodes "
            "(canvas_id, label, url, note, x, y, created_by, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (canvas_id, label, data.get('url') or '', data.get('note') or '',
             float(data.get('x') or 0), float(data.get('y') or 0),
             user_id, now, now))
        node_id = cursor.lastrowid
        save_node_access(cursor, node_id,
                         data.get('access_policy'), data.get('access_group_ids'))
        cursor.execute(
            "UPDATE awami_canvases SET updated_at = %s WHERE id = %s",
            (now, canvas_id))
        conn.commit()
        return jsonify({'success': True, 'id': node_id})
    except Exception as e:
        logging.error("awami api_node_create error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


def _node_owner_check(cursor, node_id, user_id):
    cursor.execute(
        "SELECT n.id, n.canvas_id, c.owner_user_id FROM awami_nodes n "
        "JOIN awami_canvases c ON c.id = n.canvas_id WHERE n.id = %s",
        (node_id,))
    row = cursor.fetchone()
    if row is None:
        return None, (jsonify({'success': False, 'error': 'ノードがありません'}), 404)
    if row['owner_user_id'] != user_id:
        return None, (jsonify({'success': False, 'error': '編集権限がありません'}), 403)
    return row, None


@our_meeting_bp.route('/api/node/<int:node_id>/update', methods=['POST'])
@login_required
def api_node_update(node_id):
    try:
        data = request.json or {}
        user_id = session.get('user_id')
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        row, err = _node_owner_check(cursor, node_id, user_id)
        if err:
            return err
        now = get_jst_now()
        cursor.execute(
            "UPDATE awami_nodes SET label = %s, url = %s, note = %s, "
            "updated_at = %s WHERE id = %s",
            ((data.get('label') or '').strip() or '（無題）',
             data.get('url') or '', data.get('note') or '', now, node_id))
        save_node_access(cursor, node_id,
                         data.get('access_policy'), data.get('access_group_ids'))
        cursor.execute(
            "UPDATE awami_canvases SET updated_at = %s WHERE id = %s",
            (now, row['canvas_id']))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("awami api_node_update error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@our_meeting_bp.route('/api/node/<int:node_id>/move', methods=['POST'])
@login_required
def api_node_move(node_id):
    try:
        data = request.json or {}
        user_id = session.get('user_id')
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        row, err = _node_owner_check(cursor, node_id, user_id)
        if err:
            return err
        cursor.execute(
            "UPDATE awami_nodes SET x = %s, y = %s, updated_at = %s "
            "WHERE id = %s",
            (float(data.get('x') or 0), float(data.get('y') or 0),
             get_jst_now(), node_id))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("awami api_node_move error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@our_meeting_bp.route('/api/node/<int:node_id>/delete', methods=['POST'])
@login_required
def api_node_delete(node_id):
    """ノード削除。所属エッジから外し、端点が2未満になったエッジは削除。"""
    try:
        user_id = session.get('user_id')
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        row, err = _node_owner_check(cursor, node_id, user_id)
        if err:
            return err
        cursor.execute(
            "SELECT DISTINCT edge_id FROM awami_edge_members WHERE node_id = %s",
            (node_id,))
        edge_ids = [r['edge_id'] for r in cursor.fetchall()]
        cursor.execute(
            "DELETE FROM awami_edge_members WHERE node_id = %s", (node_id,))
        for eid in edge_ids:
            # 入力・出力のどちらかが空になったエッジはエッジごと削除
            cursor.execute(
                "SELECT SUM(role = 'in') AS n_in, SUM(role <> 'in') AS n_out "
                "FROM awami_edge_members WHERE edge_id = %s", (eid,))
            r = cursor.fetchone()
            if not r or not r['n_in'] or not r['n_out']:
                cursor.execute(
                    "DELETE FROM awami_edge_members WHERE edge_id = %s", (eid,))
                cursor.execute("DELETE FROM awami_edges WHERE id = %s", (eid,))
        cursor.execute(
            "DELETE FROM awami_node_access_groups WHERE node_id = %s", (node_id,))
        cursor.execute("DELETE FROM awami_nodes WHERE id = %s", (node_id,))
        cursor.execute(
            "UPDATE awami_canvases SET updated_at = %s WHERE id = %s",
            (get_jst_now(), row['canvas_id']))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("awami api_node_delete error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


# =========================================================
# API：エッジ（ハイパーエッジ）
# =========================================================
@our_meeting_bp.route('/api/canvas/<int:canvas_id>/edge/create', methods=['POST'])
@login_required
def api_edge_create(canvas_id):
    """結合子を張る（m入力→n出力）。input_ids / output_ids は各1以上・全体で重複なし。"""
    try:
        data = request.json or {}
        input_ids = [int(m) for m in (data.get('input_ids') or [])]
        output_ids = [int(m) for m in (data.get('output_ids') or [])]
        all_ids = input_ids + output_ids
        if len(input_ids) < 1 or len(output_ids) < 1:
            return jsonify({'success': False,
                            'error': '入力・出力それぞれ1つ以上のノードが必要です'}), 400
        if len(set(all_ids)) != len(all_ids):
            return jsonify({'success': False,
                            'error': '同じノードを複数回指定しています'}), 400
        user_id = session.get('user_id')
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        canvas, err = _require_owner(cursor, canvas_id, user_id)
        if err:
            return err
        fmt = ','.join(['%s'] * len(all_ids))
        cursor.execute(
            "SELECT COUNT(*) AS n FROM awami_nodes "
            "WHERE canvas_id = %s AND id IN (" + fmt + ")",
            [canvas_id] + all_ids)
        if cursor.fetchone()['n'] != len(all_ids):
            return jsonify({'success': False,
                            'error': 'このキャンバスにないノードが含まれています'}), 400
        now = get_jst_now()
        cursor.execute(
            "INSERT INTO awami_edges "
            "(canvas_id, connector_type_id, note, label_x, label_y, "
            " created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (canvas_id, int(data.get('connector_type_id')),
             data.get('note') or '',
             data.get('label_x'), data.get('label_y'), now, now))
        edge_id = cursor.lastrowid
        for pos, nid in enumerate(input_ids, start=1):
            cursor.execute(
                "INSERT INTO awami_edge_members (edge_id, node_id, role, position) "
                "VALUES (%s, %s, 'in', %s)", (edge_id, nid, pos))
        for pos, nid in enumerate(output_ids, start=1):
            cursor.execute(
                "INSERT INTO awami_edge_members (edge_id, node_id, role, position) "
                "VALUES (%s, %s, 'out', %s)", (edge_id, nid, pos))
        cursor.execute(
            "UPDATE awami_canvases SET updated_at = %s WHERE id = %s",
            (now, canvas_id))
        conn.commit()
        return jsonify({'success': True, 'id': edge_id})
    except Exception as e:
        logging.error("awami api_edge_create error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


def _edge_owner_check(cursor, edge_id, user_id):
    cursor.execute(
        "SELECT e.id, e.canvas_id, c.owner_user_id FROM awami_edges e "
        "JOIN awami_canvases c ON c.id = e.canvas_id WHERE e.id = %s",
        (edge_id,))
    row = cursor.fetchone()
    if row is None:
        return None, (jsonify({'success': False, 'error': 'エッジがありません'}), 404)
    if row['owner_user_id'] != user_id:
        return None, (jsonify({'success': False, 'error': '編集権限がありません'}), 403)
    return row, None


@our_meeting_bp.route('/api/edge/<int:edge_id>/update', methods=['POST'])
@login_required
def api_edge_update(edge_id):
    """結合子タイプ・注記の変更（端点の付け替えは削除→再作成で行う）。"""
    try:
        data = request.json or {}
        user_id = session.get('user_id')
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        row, err = _edge_owner_check(cursor, edge_id, user_id)
        if err:
            return err
        now = get_jst_now()
        cursor.execute(
            "UPDATE awami_edges SET connector_type_id = %s, note = %s, "
            "updated_at = %s WHERE id = %s",
            (int(data.get('connector_type_id')), data.get('note') or '',
             now, edge_id))
        cursor.execute(
            "UPDATE awami_canvases SET updated_at = %s WHERE id = %s",
            (now, row['canvas_id']))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("awami api_edge_update error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@our_meeting_bp.route('/api/edge/<int:edge_id>/delete', methods=['POST'])
@login_required
def api_edge_delete(edge_id):
    try:
        user_id = session.get('user_id')
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        row, err = _edge_owner_check(cursor, edge_id, user_id)
        if err:
            return err
        cursor.execute(
            "DELETE FROM awami_edge_members WHERE edge_id = %s", (edge_id,))
        cursor.execute("DELETE FROM awami_edges WHERE id = %s", (edge_id,))
        cursor.execute(
            "UPDATE awami_canvases SET updated_at = %s WHERE id = %s",
            (get_jst_now(), row['canvas_id']))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("awami api_edge_delete error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@our_meeting_bp.route('/api/edge/<int:edge_id>/label_move', methods=['POST'])
@login_required
def api_edge_label_move(edge_id):
    """結合子ラベル（[ラベル]箱）の位置を保存。x/y に null を渡すと自動配置に戻す。"""
    try:
        data = request.json or {}
        user_id = session.get('user_id')
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        row, err = _edge_owner_check(cursor, edge_id, user_id)
        if err:
            return err
        x = data.get('x')
        y = data.get('y')
        cursor.execute(
            "UPDATE awami_edges SET label_x = %s, label_y = %s, "
            "updated_at = %s WHERE id = %s",
            (float(x) if x is not None else None,
             float(y) if y is not None else None,
             get_jst_now(), edge_id))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("awami api_edge_label_move error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@our_meeting_bp.route('/api/edge/<int:edge_id>/toggle_member', methods=['POST'])
@login_required
def api_edge_toggle_member(edge_id):
    """入力／出力リンクの追加・削除（トグル）。role='in'|'out'。

    指定ノードが
      ・その側に既にある   → 外す（各側1つ以上は必須。最後の1本なら409）
      ・反対側にある       → 反対側から外してこの側へ移す（反対側が最後の1本なら409）
      ・どこにも無い       → この側に加える
    """
    try:
        data = request.json or {}
        node_id = int(data.get('node_id') or 0)
        role = data.get('role')
        if role not in ('in', 'out'):
            return jsonify({'success': False, 'error': 'roleはin/outです'}), 400
        user_id = session.get('user_id')
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        row, err = _edge_owner_check(cursor, edge_id, user_id)
        if err:
            return err
        cursor.execute(
            "SELECT 1 FROM awami_nodes WHERE id = %s AND canvas_id = %s",
            (node_id, row['canvas_id']))
        if cursor.fetchone() is None:
            return jsonify({'success': False,
                            'error': 'このキャンバスにないノードです'}), 400
        cursor.execute(
            "SELECT id, role FROM awami_edge_members "
            "WHERE edge_id = %s AND node_id = %s", (edge_id, node_id))
        cur = cursor.fetchone()
        other = 'out' if role == 'in' else 'in'
        role_ja = {'in': '入力', 'out': '出力'}

        def count_role(r):
            cursor.execute(
                "SELECT COUNT(*) AS n FROM awami_edge_members "
                "WHERE edge_id = %s AND role = %s", (edge_id, r))
            return cursor.fetchone()['n']

        def next_pos(r):
            cursor.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 AS p "
                "FROM awami_edge_members WHERE edge_id = %s AND role = %s",
                (edge_id, r))
            return cursor.fetchone()['p']

        if cur and cur['role'] == role:
            if count_role(role) <= 1:
                return jsonify({'success': False,
                                'error': role_ja[role] + 'は1つ以上必要です．'
                                'すべて外すには結合子ごと削除してください'}), 409
            cursor.execute(
                "DELETE FROM awami_edge_members WHERE id = %s", (cur['id'],))
            action = 'removed'
        elif cur:
            if count_role(other) <= 1:
                return jsonify({'success': False,
                                'error': role_ja[other] + 'は1つ以上必要です'
                                '（このノードを移すと空になります）'}), 409
            cursor.execute(
                "UPDATE awami_edge_members SET role = %s, position = %s "
                "WHERE id = %s", (role, next_pos(role), cur['id']))
            action = 'moved'
        else:
            cursor.execute(
                "INSERT INTO awami_edge_members (edge_id, node_id, role, position) "
                "VALUES (%s, %s, %s, %s)", (edge_id, node_id, role, next_pos(role)))
            action = 'added'
        now = get_jst_now()
        cursor.execute(
            "UPDATE awami_edges SET updated_at = %s WHERE id = %s", (now, edge_id))
        cursor.execute(
            "UPDATE awami_canvases SET updated_at = %s WHERE id = %s",
            (now, row['canvas_id']))
        conn.commit()
        return jsonify({'success': True, 'action': action})
    except Exception as e:
        logging.error("awami api_edge_toggle_member error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


# =========================================================
# JSONエクスポート／インポート（owner専用）
# エクスポート: キャンバスの全ノード・全エッジを自己完結JSONで書き出す。
#   結合子タイプは（分類・名前・向き）を埋め込み，ID非依存＝環境間で可搬。
#   ノードの key はエクスポート時のノードID（エッジの端点参照用の局所名）。
# インポート: JSONの内容を「現在のキャンバスに追加」する。IDはすべて新規発番，
#   既存ノード・エッジには一切触れない（上書きなし・削除なし）。
#   不足する結合子タイプは自動追加（transport_awanara と同じ思想）。
# =========================================================
@our_meeting_bp.route('/api/canvas/<int:canvas_id>/export')
@login_required
def api_canvas_export(canvas_id):
    user_id = session.get('user_id')
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        canvas, err = _require_owner(cursor, canvas_id, user_id)
        if err:
            return err
        cursor.execute(
            "SELECT id, label, url, note, x, y, access_policy "
            "FROM awami_nodes WHERE canvas_id = %s ORDER BY id", (canvas_id,))
        node_rows = cursor.fetchall()
        node_groups = load_node_group_ids(cursor, [r['id'] for r in node_rows])
        nodes = [{'key': r['id'], 'label': r['label'], 'url': r['url'] or '',
                  'note': r['note'] or '', 'x': float(r['x']), 'y': float(r['y']),
                  'access_policy': r['access_policy'],
                  'access_group_ids': node_groups[r['id']]} for r in node_rows]
        cursor.execute(
            "SELECT e.id, e.note, e.label_x, e.label_y, "
            "       t.category, t.name, t.directed "
            "FROM awami_edges e "
            "JOIN awami_connector_types t ON t.id = e.connector_type_id "
            "WHERE e.canvas_id = %s ORDER BY e.id", (canvas_id,))
        edge_rows = cursor.fetchall()
        edges = []
        if edge_rows:
            fmt = ','.join(['%s'] * len(edge_rows))
            cursor.execute(
                "SELECT edge_id, node_id, role, position FROM awami_edge_members "
                "WHERE edge_id IN (" + fmt + ") ORDER BY edge_id, role, position",
                [e['id'] for e in edge_rows])
            mem = {}
            for m in cursor.fetchall():
                mem.setdefault(m['edge_id'], []).append(m)
            for e in edge_rows:
                ms = mem.get(e['id'], [])
                edges.append({
                    'type_category': e['category'],
                    'type_name': e['name'],
                    'directed': bool(e['directed']),
                    'note': e['note'] or '',
                    'label_x': float(e['label_x']) if e['label_x'] is not None else None,
                    'label_y': float(e['label_y']) if e['label_y'] is not None else None,
                    'inputs': [m['node_id'] for m in ms if m['role'] == 'in'],
                    'outputs': [m['node_id'] for m in ms if m['role'] != 'in'],
                })
        payload = {
            'format': 'awami-canvas-json',
            'version': 1,
            'exported_at': fmt_datetime(get_jst_now()),
            'canvas': {'name': canvas['name'],
                       'description': canvas['description'] or ''},
            'nodes': nodes,
            'edges': edges,
        }
        return jsonify({'success': True, 'data': payload})
    except Exception as e:
        logging.error("awami api_canvas_export error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


def _normalize_import_payload(data):
    """取込JSONを共通形式 (nodes, edges) に正規化する。非対応形式は None。

    受け付ける形式：
    - 'awami-canvas-export'（キャンバス画面の💾 JSONエクスポートが生成する形式）:
        nodes[].id を key に読み替え。access_policy 'inherit'→None。
        access_groups [{id,name}] → group id 列。edges[].label {name,category,
        directed} と label_position {x,y}|null。inputs/outputs は
        [{node_id, label}] 形式
    - 'awami-canvas-json'（サーバ書出 /export 形式）: そのまま
    共通形式: nodes[]={key,label,url,note,x,y,access_policy,access_group_ids},
              edges[]={type_category,type_name,directed,note,label_x,label_y,
                       inputs[keys],outputs[keys]}
    """
    fmt = (data or {}).get('format')
    if fmt == 'awami-canvas-json':
        return (data.get('nodes') or []), (data.get('edges') or [])
    if fmt != 'awami-canvas-export':
        return None
    nodes = []
    for nd in (data.get('nodes') or []):
        ap = nd.get('access_policy')
        if ap in ('inherit', '', None):
            ap = None
        groups = [g.get('id') for g in (nd.get('access_groups') or [])
                  if isinstance(g, dict) and g.get('id') is not None]
        nodes.append({'key': nd.get('id'), 'label': nd.get('label'),
                      'url': nd.get('url') or '', 'note': nd.get('note') or '',
                      'x': nd.get('x') or 0, 'y': nd.get('y') or 0,
                      'access_policy': ap, 'access_group_ids': groups})
    edges = []
    for eg in (data.get('edges') or []):
        lab = eg.get('label') or {}
        pos = eg.get('label_position')
        if not isinstance(pos, dict):
            pos = {}
        edges.append({
            'type_category': lab.get('category'),
            'type_name': lab.get('name'),
            'directed': lab.get('directed'),
            'note': eg.get('note') or '',
            'label_x': pos.get('x'),
            'label_y': pos.get('y'),
            'inputs': [m.get('node_id') for m in (eg.get('inputs') or [])
                       if isinstance(m, dict)],
            'outputs': [m.get('node_id') for m in (eg.get('outputs') or [])
                        if isinstance(m, dict)],
        })
    return nodes, edges


@our_meeting_bp.route('/api/canvas/<int:canvas_id>/import', methods=['POST'])
@login_required
def api_canvas_import(canvas_id):
    """エクスポートJSONの内容を現在のキャンバスに新規追加する。

    - 受付形式は2種（_normalize_import_payload 参照）
    - ノード・エッジのIDはすべて新規発番（既存の上書き・削除は一切しない）
    - エッジの端点はファイル内の key で解決（対応の取れない端点は落とし，
      入力か出力が空になったエッジはスキップして件数を報告）
    - 結合子タイプは（分類・名前・向き）で照合し，無ければ自動追加
    """
    user_id = session.get('user_id')
    data = request.json or {}
    normalized = _normalize_import_payload(data)
    if normalized is None:
        return jsonify({'success': False,
                        'error': 'あわみのJSONエクスポート形式ではありません'
                                 '（format: awami-canvas-export / '
                                 'awami-canvas-json のみ受付）'}), 400
    in_nodes, in_edges = normalized
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        canvas, err = _require_owner(cursor, canvas_id, user_id)
        if err:
            return err
        now = get_jst_now()

        # 結合子タイプ対応表（(分類,名前,向き) → id）。無いものは後で追加
        cursor.execute(
            "SELECT id, category, name, directed FROM awami_connector_types")
        typemap = {(r['category'], r['name'], int(r['directed'] or 0)): r['id']
                   for r in cursor.fetchall()}
        cursor.execute(
            "SELECT COALESCE(MAX(sort_order), 0) AS m FROM awami_connector_types")
        next_sort = cursor.fetchone()['m']
        added_types = 0

        # --- ノード（key → 新ID の対応表を作りながら挿入）---
        keymap = {}
        n_nodes = 0
        for nd in in_nodes:
            label = (str(nd.get('label') or '')).strip() or '（無題）'
            cursor.execute(
                "INSERT INTO awami_nodes "
                "(canvas_id, label, url, note, x, y, "
                " created_by, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (canvas_id, label[:200], (str(nd.get('url') or ''))[:500],
                 str(nd.get('note') or ''),
                 float(nd.get('x') or 0), float(nd.get('y') or 0),
                 user_id, now, now))
            nid = cursor.lastrowid
            save_node_access(cursor, nid, nd.get('access_policy'),
                             nd.get('access_group_ids'))
            if nd.get('key') is not None:
                keymap[nd.get('key')] = nid
            n_nodes += 1

        # --- エッジ ---
        n_edges = 0
        skipped = 0
        for eg in in_edges:
            # key解決＋順序保持の重複除去。入力と両属しているものは入力を優先
            ins, seen = [], set()
            for k in (eg.get('inputs') or []):
                nid = keymap.get(k)
                if nid and nid not in seen:
                    ins.append(nid); seen.add(nid)
            outs = []
            for k in (eg.get('outputs') or []):
                nid = keymap.get(k)
                if nid and nid not in seen:
                    outs.append(nid); seen.add(nid)
            if not ins or not outs:
                skipped += 1
                continue
            cat = (str(eg.get('type_category') or '')).strip() or 'その他'
            name = (str(eg.get('type_name') or '')).strip() or '関連'
            directed = 1 if eg.get('directed') else 0
            tkey = (cat, name, directed)
            if tkey not in typemap:
                next_sort += 1
                cursor.execute(
                    "INSERT INTO awami_connector_types "
                    "(category, name, directed, sort_order, is_active) "
                    "VALUES (%s, %s, %s, %s, 1)",
                    (cat[:50], name[:100], directed, next_sort))
                typemap[tkey] = cursor.lastrowid
                added_types += 1
            lx = eg.get('label_x')
            ly = eg.get('label_y')
            cursor.execute(
                "INSERT INTO awami_edges "
                "(canvas_id, connector_type_id, note, label_x, label_y, "
                " created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (canvas_id, typemap[tkey], str(eg.get('note') or ''),
                 float(lx) if lx is not None else None,
                 float(ly) if ly is not None else None, now, now))
            eid = cursor.lastrowid
            for pos, nid in enumerate(ins, start=1):
                cursor.execute(
                    "INSERT INTO awami_edge_members (edge_id, node_id, role, position) "
                    "VALUES (%s, %s, 'in', %s)", (eid, nid, pos))
            for pos, nid in enumerate(outs, start=1):
                cursor.execute(
                    "INSERT INTO awami_edge_members (edge_id, node_id, role, position) "
                    "VALUES (%s, %s, 'out', %s)", (eid, nid, pos))
            n_edges += 1

        cursor.execute(
            "UPDATE awami_canvases SET updated_at = %s WHERE id = %s",
            (now, canvas_id))
        conn.commit()
        return jsonify({'success': True,
                        'imported_nodes': n_nodes,
                        'imported_edges': n_edges,
                        'skipped_edges': skipped,
                        'added_connector_types': added_types})
    except Exception as e:
        logging.error("awami api_canvas_import error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


# =========================================================
# 結合子タイプ（ラベルセット）管理：admin専用
# 結合子タイプは全キャンバス共通（グローバル）のため、編集はシステム管理者のみ。
# 変更は全キャンバスの表示・選択肢に即時反映される。
# 「重複を統合」は（分類・名前・向き）が同一のタイプ群を最小IDの1件に束ね、
# 既存エッジの connector_type_id を付け替えてから余分な行を削除する。
# =========================================================
def _admin_guard():
    """admin以外には (None, 403応答) を返す。adminなら (user_id, None)。"""
    user_id = session.get('user_id')
    if get_user_category(user_id) != 'admin':
        return None, (jsonify({'success': False,
                               'error': 'ラベルセットの管理はシステム管理者のみ可能です'}), 403)
    return user_id, None


@our_meeting_bp.route('/connector_types')
@login_required
def connector_types_page():
    """結合子タイプ（ラベルセット）管理ページ（admin専用）"""
    if get_user_category(session.get('user_id')) != 'admin':
        return "結合子タイプ（ラベルセット）の管理はシステム管理者のみ開けます", 403
    return render_template('awami/connector_types.html')


@our_meeting_bp.route('/api/connector_types')
@login_required
def api_connector_types_list():
    """全結合子タイプ（無効含む）＋使用エッジ数を返す（admin専用）"""
    _, err = _admin_guard()
    if err:
        return err
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT t.id, t.category, t.name, t.directed, t.sort_order, "
            "       t.is_active, "
            "       (SELECT COUNT(*) FROM awami_edges e "
            "        WHERE e.connector_type_id = t.id) AS n_edges "
            "FROM awami_connector_types t "
            "ORDER BY t.sort_order, t.id")
        types = [{'id': r['id'], 'category': r['category'], 'name': r['name'],
                  'directed': bool(r['directed']),
                  'sort_order': int(r['sort_order'] or 0),
                  'is_active': bool(r['is_active']),
                  'n_edges': int(r['n_edges'] or 0)} for r in cursor.fetchall()]
        return jsonify({'success': True, 'types': types})
    except Exception as e:
        logging.error("awami api_connector_types_list error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


def _type_duplicate_exists(cursor, category, name, directed, exclude_id=None):
    """（分類・名前・向き）が同じタイプが既にあるか。"""
    q = ("SELECT id FROM awami_connector_types "
         "WHERE category = %s AND name = %s AND directed = %s")
    params = [category, name, 1 if directed else 0]
    if exclude_id:
        q += " AND id <> %s"
        params.append(exclude_id)
    cursor.execute(q, params)
    return cursor.fetchone() is not None


@our_meeting_bp.route('/api/connector_types/create', methods=['POST'])
@login_required
def api_connector_type_create():
    """新しいラベルを追加（admin専用）"""
    _, err = _admin_guard()
    if err:
        return err
    data = request.json or {}
    category = (data.get('category') or '').strip()
    name = (data.get('name') or '').strip()
    if not category or not name:
        return jsonify({'success': False, 'error': '分類と名前は必須です'}), 400
    directed = 1 if data.get('directed') else 0
    try:
        sort_order = int(data.get('sort_order') or 0)
    except (TypeError, ValueError):
        sort_order = 0
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        if _type_duplicate_exists(cursor, category, name, directed):
            return jsonify({'success': False,
                            'error': '同じ分類・名前・向きのラベルが既にあります'}), 409
        cursor.execute(
            "INSERT INTO awami_connector_types "
            "(category, name, directed, sort_order, is_active) "
            "VALUES (%s, %s, %s, %s, 1)",
            (category, name, directed, sort_order))
        conn.commit()
        return jsonify({'success': True, 'id': cursor.lastrowid})
    except Exception as e:
        logging.error("awami api_connector_type_create error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@our_meeting_bp.route('/api/connector_type/<int:type_id>/update', methods=['POST'])
@login_required
def api_connector_type_update(type_id):
    """ラベルの書き換え（分類・名前・向き・表示順・有効/無効）（admin専用）"""
    _, err = _admin_guard()
    if err:
        return err
    data = request.json or {}
    category = (data.get('category') or '').strip()
    name = (data.get('name') or '').strip()
    if not category or not name:
        return jsonify({'success': False, 'error': '分類と名前は必須です'}), 400
    directed = 1 if data.get('directed') else 0
    is_active = 1 if data.get('is_active', True) else 0
    try:
        sort_order = int(data.get('sort_order') or 0)
    except (TypeError, ValueError):
        sort_order = 0
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT id FROM awami_connector_types WHERE id = %s", (type_id,))
        if cursor.fetchone() is None:
            return jsonify({'success': False, 'error': 'ラベルがありません'}), 404
        if _type_duplicate_exists(cursor, category, name, directed,
                                  exclude_id=type_id):
            return jsonify({'success': False,
                            'error': '同じ分類・名前・向きの別ラベルが既にあります'
                                     '（先に「重複を統合」を実行してください）'}), 409
        cursor.execute(
            "UPDATE awami_connector_types "
            "SET category = %s, name = %s, directed = %s, "
            "    sort_order = %s, is_active = %s WHERE id = %s",
            (category, name, directed, sort_order, is_active, type_id))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("awami api_connector_type_update error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@our_meeting_bp.route('/api/connector_type/<int:type_id>/delete', methods=['POST'])
@login_required
def api_connector_type_delete(type_id):
    """ラベルの削除（admin専用）。使用中（エッジが参照）の場合は拒否する。"""
    _, err = _admin_guard()
    if err:
        return err
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT id FROM awami_connector_types WHERE id = %s", (type_id,))
        if cursor.fetchone() is None:
            return jsonify({'success': False, 'error': 'ラベルがありません'}), 404
        cursor.execute(
            "SELECT COUNT(*) AS n FROM awami_edges "
            "WHERE connector_type_id = %s", (type_id,))
        n = cursor.fetchone()['n']
        if n > 0:
            return jsonify({'success': False,
                            'error': 'このラベルは%d本の結合子で使用中のため削除できません．'
                                     '「無効」にして新規選択肢から外すか，'
                                     '先に該当エッジのタイプを変更してください' % n}), 409
        cursor.execute(
            "DELETE FROM awami_connector_types WHERE id = %s", (type_id,))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("awami api_connector_type_delete error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@our_meeting_bp.route('/api/connector_types/dedupe', methods=['POST'])
@login_required
def api_connector_types_dedupe():
    """重複を統合（admin専用）。

    （分類・名前・向き）が同一のタイプ群ごとに最小IDの1件を残し、
    既存エッジの connector_type_id を残す1件へ付け替えてから、
    余分な行を削除する。1つでも有効な行があれば残す1件は有効にする。
    冪等（重複が無ければ何もしない）。
    """
    _, err = _admin_guard()
    if err:
        return err
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT id, category, name, directed, is_active "
            "FROM awami_connector_types ORDER BY id")
        groups = {}
        for r in cursor.fetchall():
            key = (r['category'], r['name'], int(r['directed'] or 0))
            groups.setdefault(key, []).append(r)
        merged_groups = 0
        removed = 0
        reassigned = 0
        for rows in groups.values():
            if len(rows) < 2:
                continue
            keeper = rows[0]                      # 最小ID（ORDER BY id のため先頭）
            dup_ids = [r['id'] for r in rows[1:]]
            fmt = ','.join(['%s'] * len(dup_ids))
            cursor.execute(
                "UPDATE awami_edges SET connector_type_id = %s "
                "WHERE connector_type_id IN (" + fmt + ")",
                [keeper['id']] + dup_ids)
            reassigned += cursor.rowcount
            if any(r['is_active'] for r in rows) and not keeper['is_active']:
                cursor.execute(
                    "UPDATE awami_connector_types SET is_active = 1 "
                    "WHERE id = %s", (keeper['id'],))
            cursor.execute(
                "DELETE FROM awami_connector_types WHERE id IN (" + fmt + ")",
                dup_ids)
            removed += len(dup_ids)
            merged_groups += 1
        conn.commit()
        return jsonify({'success': True, 'merged_groups': merged_groups,
                        'removed': removed, 'reassigned_edges': reassigned})
    except Exception as e:
        logging.error("awami api_connector_types_dedupe error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


# =========================================================
# あわみ拡張：意見募集（online voting / write-in）
# ポップアップはすべてURL付きの独立ページ。参加者URLはランダム12桁
# トークンの合言葉方式（キャンバスの閲覧権とは独立）。
# =========================================================
import secrets

TOKEN_ALPHABET = 'abcdefghjkmnpqrstuvwxyz23456789'  # 紛らわしい文字を除外


def _new_token():
    return ''.join(secrets.choice(TOKEN_ALPHABET) for _ in range(12))


def get_poll(cursor, token):
    cursor.execute(
        "SELECT p.*, c.owner_user_id, c.name AS canvas_name "
        "FROM awami_polls p JOIN awami_canvases c ON c.id = p.canvas_id "
        "WHERE p.token = %s", (token,))
    return cursor.fetchone()


def get_poll_options(cursor, poll_id):
    cursor.execute(
        "SELECT opt_index, label FROM awami_poll_options "
        "WHERE poll_id = %s ORDER BY opt_index", (poll_id,))
    return cursor.fetchall()


def _voter_identity():
    """投票者の識別子を返す (user_id, anon_key)。

    ログイン済みなら user_id を使う。未ログインならセッションに匿名キー
    （ランダム16桁）を発行して使う。これにより投票URLはログイン不要のまま、
    n択の「一人一票（同一ブラウザからの再投票は上書き）」を保つ。
    """
    user_id = session.get('user_id')
    if user_id:
        return user_id, None
    anon = session.get('awami_anon')
    if not anon:
        anon = ''.join(secrets.choice(TOKEN_ALPHABET) for _ in range(16))
        session['awami_anon'] = anon
        session.permanent = True
    return None, anon


def _poll_owner_check(poll, user_id):
    """司会者（＝キャンバスowner）か。当初は講師／司会者のみに開く。"""
    return poll is not None and poll['owner_user_id'] == user_id


# ---------- 画面（すべてURL付きポップアップ） ----------
@our_meeting_bp.route('/canvas/<int:canvas_id>/poll/new')
@login_required
def poll_new(canvas_id):
    """意見募集の作成フォーム（講師／司会者のみ）"""
    user_id = session.get('user_id')
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        canvas = get_canvas(cursor, canvas_id)
        if canvas is None or canvas['owner_user_id'] != user_id:
            return "意見募集の作成は講師／司会者（キャンバス作成者）のみ可能です", 403
        return render_template('awami/poll_new.html', canvas=canvas)
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@our_meeting_bp.route('/poll/<token>/console')
@login_required
def poll_console(token):
    """司会コンソール（講師／司会者のみ）"""
    user_id = session.get('user_id')
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        poll = get_poll(cursor, token)
        if not _poll_owner_check(poll, user_id):
            return "このページは講師／司会者のみ開けます", 403
        options = get_poll_options(cursor, poll['id'])
        vote_url = request.url_root.rstrip('/') + url_for('our_meeting.poll_vote', token=token)
        return render_template('awami/poll_console.html',
                               poll=poll, options=options, vote_url=vote_url)
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@our_meeting_bp.route('/poll/<token>/display')
@login_required
def poll_display(token):
    """参加者URLの大表示ウィンドウ（プロジェクタ投影用・講師／司会者のみ）"""
    user_id = session.get('user_id')
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        poll = get_poll(cursor, token)
        if not _poll_owner_check(poll, user_id):
            return "このページは講師／司会者のみ開けます", 403
        vote_url = request.url_root.rstrip('/') + url_for('our_meeting.poll_vote', token=token)
        return render_template('awami/poll_display.html', poll=poll, vote_url=vote_url)
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@our_meeting_bp.route('/poll/<token>/stats')
@login_required
def poll_stats(token):
    """stats表示ウィンドウ（URLを知っている人＝会場向け）"""
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        poll = get_poll(cursor, token)
        if poll is None:
            return "意見募集が見つかりません", 404
        return render_template('awami/poll_stats.html', poll=poll)
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@our_meeting_bp.route('/vote/<token>')
def poll_vote(token):
    """受講生の投票ページ（URLを知っていればアクセス可）"""
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        poll = get_poll(cursor, token)
        if poll is None:
            return "意見募集が見つかりません", 404
        options = get_poll_options(cursor, poll['id'])
        return render_template('awami/poll_vote.html', poll=poll, options=options)
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


# ---------- API ----------
@our_meeting_bp.route('/api/canvas/<int:canvas_id>/poll/create', methods=['POST'])
@login_required
def api_poll_create(canvas_id):
    """意見募集を作成（講師／司会者のみ）。n択・write-inのどちらか一方でも可。"""
    user_id = session.get('user_id')
    data = request.json or {}
    choice_question = (data.get('choice_question') or '').strip()
    options = [o.strip() for o in (data.get('options') or []) if o and o.strip()]
    writein_question = (data.get('writein_question') or '').strip()

    if choice_question and len(options) < 2:
        return jsonify({'success': False,
                        'error': '選択式には選択肢を2つ以上入れてください'}), 400
    if options and not choice_question:
        return jsonify({'success': False,
                        'error': '選択肢がある場合は選択式の問いを入れてください'}), 400
    if not choice_question and not writein_question:
        return jsonify({'success': False,
                        'error': '選択式の問いか、write-inの問いのどちらかが必要です'}), 400
    if choice_question and writein_question:
        return jsonify({'success': False,
                        'error': '意見募集は単機能です。投票と自由意見は'
                                 'それぞれ別に発行してください'}), 400
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        canvas = get_canvas(cursor, canvas_id)
        if canvas is None or canvas['owner_user_id'] != user_id:
            return jsonify({'success': False,
                            'error': '意見募集の作成は講師／司会者のみ可能です'}), 403
        token = _new_token()
        now = get_jst_now()
        cursor.execute(
            "INSERT INTO awami_polls "
            "(canvas_id, token, choice_question, writein_question, "
            " status, created_by, created_at) "
            "VALUES (%s, %s, %s, %s, 'open', %s, %s)",
            (canvas_id, token, choice_question or None,
             writein_question or None, user_id, now))
        poll_id = cursor.lastrowid
        for i, label in enumerate(options):
            cursor.execute(
                "INSERT INTO awami_poll_options (poll_id, opt_index, label) "
                "VALUES (%s, %s, %s)", (poll_id, i, label))
        conn.commit()
        return jsonify({'success': True, 'token': token,
                        'console_url': url_for('our_meeting.poll_console', token=token)})
    except Exception as e:
        logging.error("awami api_poll_create error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@our_meeting_bp.route('/api/poll/<token>/toggle', methods=['POST'])
@login_required
def api_poll_toggle(token):
    """締め切り／再開（講師／司会者のみ）"""
    user_id = session.get('user_id')
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        poll = get_poll(cursor, token)
        if not _poll_owner_check(poll, user_id):
            return jsonify({'success': False, 'error': '権限がありません'}), 403
        new_status = 'closed' if poll['status'] == 'open' else 'open'
        cursor.execute(
            "UPDATE awami_polls SET status = %s, closed_at = %s WHERE id = %s",
            (new_status, get_jst_now() if new_status == 'closed' else None,
             poll['id']))
        conn.commit()
        return jsonify({'success': True, 'status': new_status})
    except Exception as e:
        logging.error("awami api_poll_toggle error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@our_meeting_bp.route('/api/vote/<token>', methods=['POST'])
def api_vote(token):
    """投票を受け付ける。n択は一人一票（再投票は上書き）、write-inは追記。
    どちらか片方だけでも可（欠けていても構わない）。"""
    user_id, anon_key = _voter_identity()
    data = request.json or {}
    opt_index = data.get('opt_index')          # int or None
    writein = (data.get('writein') or '').strip()

    if opt_index is None and not writein:
        return jsonify({'success': False,
                        'error': '選択かwrite-inのどちらかを入力してください'}), 400
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        poll = get_poll(cursor, token)
        if poll is None:
            return jsonify({'success': False, 'error': '意見募集が見つかりません'}), 404
        if poll['status'] != 'open':
            return jsonify({'success': False,
                            'error': 'この意見募集は締め切られています'}), 409

        result = {}
        if opt_index is not None:
            opt_index = int(opt_index)
            cursor.execute(
                "SELECT 1 FROM awami_poll_options "
                "WHERE poll_id = %s AND opt_index = %s", (poll['id'], opt_index))
            if not cursor.fetchone():
                return jsonify({'success': False, 'error': '不正な選択肢です'}), 400
            now = get_jst_now()
            cursor.execute(
                "INSERT INTO awami_votes "
                "(poll_id, user_id, anon_key, opt_index, voted_at) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE opt_index = VALUES(opt_index), "
                "voted_at = VALUES(voted_at)",
                (poll['id'], user_id, anon_key, opt_index, now))
            result['choice'] = 'accepted'

        if writein:
            now = get_jst_now()
            cursor.execute(
                "SELECT COALESCE(MAX(seq_no), 0) + 1 AS n FROM awami_writeins "
                "WHERE poll_id = %s FOR UPDATE", (poll['id'],))
            seq_no = cursor.fetchone()['n']
            cursor.execute(
                "INSERT INTO awami_writeins "
                "(poll_id, seq_no, user_id, anon_key, content, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (poll['id'], seq_no, user_id, anon_key, writein, now))
            result['writein_no'] = seq_no

        conn.commit()
        result['success'] = True
        return jsonify(result)
    except Exception as e:
        logging.error("awami api_vote error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@our_meeting_bp.route('/api/poll/<token>/results')
def api_poll_results(token):
    """集計結果とwrite-in一覧（即時表示用ポーリング先）。
    表示順は当面、問い・選択肢の登録順。"""
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        poll = get_poll(cursor, token)
        if poll is None:
            return jsonify({'success': False, 'error': '意見募集が見つかりません'}), 404

        options = get_poll_options(cursor, poll['id'])
        cursor.execute(
            "SELECT opt_index, COUNT(*) AS n FROM awami_votes "
            "WHERE poll_id = %s GROUP BY opt_index", (poll['id'],))
        counts = {r['opt_index']: r['n'] for r in cursor.fetchall()}
        total = sum(counts.values())

        cursor.execute(
            "SELECT seq_no, content, created_at FROM awami_writeins "
            "WHERE poll_id = %s ORDER BY seq_no", (poll['id'],))
        writeins = [{'no': r['seq_no'],
                     'time': r['created_at'].strftime('%H:%M:%S')
                             if r['created_at'] else '',
                     'content': r['content']} for r in cursor.fetchall()]

        return jsonify({
            'success': True,
            'status': poll['status'],
            'choice_question': poll['choice_question'] or '',
            'options': [{'index': o['opt_index'], 'label': o['label'],
                         'count': counts.get(o['opt_index'], 0)}
                        for o in options],
            'total_votes': total,
            'writein_question': poll['writein_question'] or '',
            'writeins': writeins,
        })
    except Exception as e:
        logging.error("awami api_poll_results error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@our_meeting_bp.route('/api/canvas/<int:canvas_id>/active_polls')
@login_required
def api_active_polls(canvas_id):
    """開催中の意見募集一覧（受講生画面の隅の投票ボタン用）"""
    user_id = session.get('user_id')
    category = get_user_category(user_id)
    group_ids = get_effective_group_ids(user_id)
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        canvas = get_canvas(cursor, canvas_id)
        if not can_view_canvas(cursor, canvas, user_id, category, group_ids):
            return jsonify({'success': False, 'error': 'アクセス権がありません'}), 403
        cursor.execute(
            "SELECT token, choice_question, writein_question FROM awami_polls "
            "WHERE canvas_id = %s AND status = 'open' ORDER BY id", (canvas_id,))
        polls = [{'token': r['token'],
                  'question': r['choice_question'] or r['writein_question'] or '意見募集'}
                 for r in cursor.fetchall()]
        return jsonify({'success': True, 'polls': polls,
                        'is_owner': canvas['owner_user_id'] == user_id})
    except Exception as e:
        logging.error("awami api_active_polls error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


# =========================================================
# 討論の展開・投票結果：キャンバス単位の記録ウィンドウ（2本立て）
# 討論の展開＝自由意見・ノードopen・意見募集開始を統合した時系列。
# 投票結果＝n択の問いと結果の並び。
# =========================================================
def _require_canvas_view(canvas_id):
    """キャンバス閲覧権チェック。可なら (canvas, None)、不可なら (None, 応答)。"""
    user_id = session.get('user_id')
    category = get_user_category(user_id)
    group_ids = get_effective_group_ids(user_id)
    conn = _connect()
    try:
        cursor = conn.cursor(dictionary=True)
        canvas = get_canvas(cursor, canvas_id)
        ok = can_view_canvas(cursor, canvas, user_id, category, group_ids)
        return (canvas if ok else None)
    finally:
        cursor.close()
        conn.close()


@our_meeting_bp.route('/canvas/<int:canvas_id>/flow')
@login_required
def canvas_flow(canvas_id):
    """討論の展開ウィンドウ（キャンバスを閲覧できる人）"""
    canvas = _require_canvas_view(canvas_id)
    if canvas is None:
        return "このキャンバスの記録を閲覧する権限がありません", 403
    return render_template('awami/canvas_flow.html', canvas=canvas,
                           sel_record=request.args.get('record', type=int))


@our_meeting_bp.route('/canvas/<int:canvas_id>/results')
@login_required
def canvas_results(canvas_id):
    """投票結果ウィンドウ（キャンバスを閲覧できる人）"""
    canvas = _require_canvas_view(canvas_id)
    if canvas is None:
        return "このキャンバスの記録を閲覧する権限がありません", 403
    return render_template('awami/canvas_results.html', canvas=canvas,
                           sel_record=request.args.get('record', type=int))


@our_meeting_bp.route('/api/canvas/<int:canvas_id>/node_open', methods=['POST'])
@login_required
def api_node_open(canvas_id):
    """ノードの実体URLを開いたイベントを記録する（討論の展開に流れる）"""
    user_id = session.get('user_id')
    data = request.json or {}
    node_id = data.get('node_id')
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        canvas = get_canvas(cursor, canvas_id)
        category = get_user_category(user_id)
        group_ids = get_effective_group_ids(user_id)
        if not can_view_canvas(cursor, canvas, user_id, category, group_ids):
            return jsonify({'success': False, 'error': 'アクセス権がありません'}), 403
        cursor.execute(
            "SELECT label, url FROM awami_nodes WHERE id = %s AND canvas_id = %s",
            (node_id, canvas_id))
        node = cursor.fetchone()
        if node is None or not node['url']:
            return jsonify({'success': False, 'error': 'ノードが見つかりません'}), 404
        cursor.execute(
            "INSERT INTO awami_node_opens "
            "(canvas_id, node_id, label, url, user_id, opened_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (canvas_id, node_id, node['label'], node['url'],
             user_id, get_jst_now()))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("awami api_node_open error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


def _build_live_record(cursor, canvas_id, is_owner):
    """いまこの瞬間の討論展開（events）＋投票結果（choice_polls）を組み立てて返す。
    記録スナップショットの中身もこれと同じ形。"""
    events = []
    cursor.execute(
        "SELECT w.seq_no, w.content, w.created_at, "
        "       p.writein_question AS question "
        "FROM awami_writeins w JOIN awami_polls p ON p.id = w.poll_id "
        "WHERE p.canvas_id = %s", (canvas_id,))
    for r in cursor.fetchall():
        events.append((r['created_at'], 2, {
            'type': 'writein', 'no': r['seq_no'],
            'question': r['question'] or '', 'content': r['content']}))
    cursor.execute(
        "SELECT label, url, opened_at FROM awami_node_opens "
        "WHERE canvas_id = %s", (canvas_id,))
    for r in cursor.fetchall():
        events.append((r['opened_at'], 1, {
            'type': 'node_open', 'label': r['label'] or '', 'url': r['url'] or ''}))
    cursor.execute(
        "SELECT token, choice_question, writein_question, created_at "
        "FROM awami_polls WHERE canvas_id = %s", (canvas_id,))
    for r in cursor.fetchall():
        events.append((r['created_at'], 0, {
            'type': 'poll_start',
            'kind': '投票' if r['choice_question'] else '自由意見',
            'question': r['choice_question'] or r['writein_question'] or '',
            'vote_url': url_for('our_meeting.poll_vote', token=r['token']),
            'console_url': url_for('our_meeting.poll_console',
                                   token=r['token']) if is_owner else ''}))
    events.sort(key=lambda x: (x[0] or datetime.datetime.min, x[1]))
    out_events = []
    for dt, _, p in events:
        p['time'] = dt.strftime('%H:%M:%S') if dt else ''
        out_events.append(p)

    cursor.execute(
        "SELECT id, token, choice_question, status, created_at "
        "FROM awami_polls "
        "WHERE canvas_id = %s AND choice_question IS NOT NULL "
        "ORDER BY created_at, id", (canvas_id,))
    choice_polls = []
    for p in cursor.fetchall():
        opts = get_poll_options(cursor, p['id'])
        cursor.execute(
            "SELECT opt_index, COUNT(*) AS n FROM awami_votes "
            "WHERE poll_id = %s GROUP BY opt_index", (p['id'],))
        counts = {r['opt_index']: r['n'] for r in cursor.fetchall()}
        choice_polls.append({
            'question': p['choice_question'], 'status': p['status'],
            'time': p['created_at'].strftime('%H:%M:%S') if p['created_at'] else '',
            'options': [{'label': o['label'], 'count': counts.get(o['opt_index'], 0)}
                        for o in opts],
            'total': sum(counts.values()),
            'console_url': url_for('our_meeting.poll_console',
                                   token=p['token']) if is_owner else ''})
    return out_events, choice_polls


@our_meeting_bp.route('/api/canvas/<int:canvas_id>/record')
@login_required
def api_canvas_record(canvas_id):
    """討論の展開・投票結果のデータ。
    record_id 指定時は保存済みスナップショットを、無指定なら現在のライブを返す。
    """
    user_id = session.get('user_id')
    category = get_user_category(user_id)
    group_ids = get_effective_group_ids(user_id)
    record_id = request.args.get('record_id', type=int)
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        canvas = get_canvas(cursor, canvas_id)
        if not can_view_canvas(cursor, canvas, user_id, category, group_ids):
            return jsonify({'success': False, 'error': 'アクセス権がありません'}), 403
        is_owner = canvas['owner_user_id'] == user_id

        if record_id:
            cursor.execute(
                "SELECT name, snapshot_json FROM awami_records "
                "WHERE id = %s AND canvas_id = %s", (record_id, canvas_id))
            rec = cursor.fetchone()
            if rec is None or not rec['snapshot_json']:
                return jsonify({'success': False, 'error': '記録が見つかりません'}), 404
            snap = json.loads(rec['snapshot_json'])
            return jsonify({'success': True, 'is_owner': is_owner, 'frozen': True,
                            'record_name': rec['name'],
                            'events': snap.get('events', []),
                            'choice_polls': snap.get('choice_polls', [])})

        out_events, choice_polls = _build_live_record(cursor, canvas_id, is_owner)
        return jsonify({'success': True, 'is_owner': is_owner, 'frozen': False,
                        'events': out_events, 'choice_polls': choice_polls})
    except Exception as e:
        logging.error("awami api_canvas_record error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


# =========================================================
# プレゼン計画：時間順に開くノードの線形リスト（owner用）。
# 並べ替え・インデント（直上のsub化）・リハーサル／実行の2モード。
# 実行モードのopenは既存の node_open で討論の展開に記録される
# （即興で開いたものと同じイベント）。
# =========================================================
def _canvas_owner_or_none(canvas_id, user_id):
    conn = _connect()
    try:
        cursor = conn.cursor(dictionary=True)
        canvas = get_canvas(cursor, canvas_id)
        if canvas is None or canvas['owner_user_id'] != user_id:
            return None
        return canvas
    finally:
        cursor.close()
        conn.close()


@our_meeting_bp.route('/canvas/<int:canvas_id>/plan')
@login_required
def canvas_plan(canvas_id):
    """プレゼン計画ウィンドウ（講師／司会者のみ）"""
    canvas = _canvas_owner_or_none(canvas_id, session.get('user_id'))
    if canvas is None:
        return "プレゼン計画は講師／司会者（キャンバス作成者）のみ開けます", 403
    return render_template('awami/canvas_plan.html', canvas=canvas,
                           sel_plan=request.args.get('plan', type=int))


@our_meeting_bp.route('/api/canvas/<int:canvas_id>/plan')
@login_required
def api_plan_list(canvas_id):
    """計画の一覧（position順）。ノードは参照で持つ（削除済みは明示）。"""
    if _canvas_owner_or_none(canvas_id, session.get('user_id')) is None:
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    plan_id = request.args.get('plan_id', type=int)
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT pi.id, pi.node_id, pi.position, pi.indent, "
            "       n.label, n.url "
            "FROM awami_plan_items pi "
            "LEFT JOIN awami_nodes n ON n.id = pi.node_id "
            "WHERE pi.canvas_id = %s AND pi.plan_id = %s "
            "ORDER BY pi.position", (canvas_id, plan_id))
        items = [{'id': r['id'], 'node_id': r['node_id'],
                  'indent': int(r['indent'] or 0),
                  'label': r['label'] if r['label'] is not None else '（見つかりません）',
                  'url': r['url'] or '',
                  'deleted': r['label'] is None} for r in cursor.fetchall()]
        return jsonify({'success': True, 'items': items})
    except Exception as e:
        logging.error("awami api_plan_list error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@our_meeting_bp.route('/api/canvas/<int:canvas_id>/plan/add', methods=['POST'])
@login_required
def api_plan_add(canvas_id):
    """ノードを計画の最後に追加（キャンバスのノードメニューから呼ばれる）"""
    if _canvas_owner_or_none(canvas_id, session.get('user_id')) is None:
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    data = request.json or {}
    node_id = data.get('node_id')
    plan_id = data.get('plan_id')
    if not plan_id:
        return jsonify({'success': False, 'error': '計画が選ばれていません'}), 400
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT id, label FROM awami_nodes WHERE id = %s AND canvas_id = %s",
            (node_id, canvas_id))
        node = cursor.fetchone()
        if node is None:
            return jsonify({'success': False, 'error': 'ノードが見つかりません'}), 404
        cursor.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 AS p "
            "FROM awami_plan_items WHERE plan_id = %s FOR UPDATE", (plan_id,))
        pos = cursor.fetchone()['p']
        cursor.execute(
            "INSERT INTO awami_plan_items "
            "(canvas_id, plan_id, node_id, position, indent, created_at) "
            "VALUES (%s, %s, %s, %s, 0, %s)",
            (canvas_id, plan_id, node_id, pos, get_jst_now()))
        conn.commit()
        return jsonify({'success': True, 'position': pos,
                        'label': node['label']})
    except Exception as e:
        logging.error("awami api_plan_add error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


def _get_plan_item(cursor, item_id):
    cursor.execute(
        "SELECT pi.*, c.owner_user_id FROM awami_plan_items pi "
        "JOIN awami_canvases c ON c.id = pi.canvas_id "
        "WHERE pi.id = %s", (item_id,))
    return cursor.fetchone()


@our_meeting_bp.route('/api/plan_item/<int:item_id>/move', methods=['POST'])
@login_required
def api_plan_move(item_id):
    """上下入替（direction: 'up' / 'down'）"""
    direction = (request.json or {}).get('direction')
    if direction not in ('up', 'down'):
        return jsonify({'success': False, 'error': 'directionはup/downです'}), 400
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        item = _get_plan_item(cursor, item_id)
        if item is None or item['owner_user_id'] != session.get('user_id'):
            return jsonify({'success': False, 'error': '権限がありません'}), 403
        op = '<' if direction == 'up' else '>'
        order = 'DESC' if direction == 'up' else 'ASC'
        cursor.execute(
            f"SELECT id, position FROM awami_plan_items "
            f"WHERE canvas_id = %s AND position {op} %s "
            f"ORDER BY position {order} LIMIT 1",
            (item['canvas_id'], item['position']))
        neighbor = cursor.fetchone()
        if neighbor is None:
            return jsonify({'success': True, 'moved': False})  # 端なので動かない
        cursor.execute("UPDATE awami_plan_items SET position = %s WHERE id = %s",
                       (neighbor['position'], item['id']))
        cursor.execute("UPDATE awami_plan_items SET position = %s WHERE id = %s",
                       (item['position'], neighbor['id']))
        conn.commit()
        return jsonify({'success': True, 'moved': True})
    except Exception as e:
        logging.error("awami api_plan_move error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@our_meeting_bp.route('/api/plan_item/<int:item_id>/indent', methods=['POST'])
@login_required
def api_plan_indent(item_id):
    """インデントの切替（1=直上ノードのsub＝オプショナル提示）"""
    indent = 1 if (request.json or {}).get('indent') else 0
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        item = _get_plan_item(cursor, item_id)
        if item is None or item['owner_user_id'] != session.get('user_id'):
            return jsonify({'success': False, 'error': '権限がありません'}), 403
        cursor.execute("UPDATE awami_plan_items SET indent = %s WHERE id = %s",
                       (indent, item_id))
        conn.commit()
        return jsonify({'success': True, 'indent': indent})
    except Exception as e:
        logging.error("awami api_plan_indent error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@our_meeting_bp.route('/api/plan_item/<int:item_id>/delete', methods=['POST'])
@login_required
def api_plan_delete(item_id):
    """計画からはずす（ノード自体は消えない）"""
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        item = _get_plan_item(cursor, item_id)
        if item is None or item['owner_user_id'] != session.get('user_id'):
            return jsonify({'success': False, 'error': '権限がありません'}), 403
        cursor.execute("DELETE FROM awami_plan_items WHERE id = %s", (item_id,))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("awami api_plan_delete error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


# =========================================================
# あわなら → あわみ トランスポート：2026-07-21 撤去
# 旧あわなら（awanara_*）からの一括取込機能（POST /api/transport_awanara）は
# 役目を終えたため，一覧画面のボタンとともに削除した（ユーザ指示）。
# 取込履歴テーブル awami_transport_log は記録として残置。
# 復活が必要な場合はプロジェクト保管の旧 routes.py（2026-07-21以前）を参照。
# =========================================================


# =========================================================
# 計画・記録の器（v0.7）：キャンバス×複数の計画×複数の記録。
# 両者に脈絡はない。素朴なCRUDのみ。owner専用。
# =========================================================
def _container_list(canvas_id, table):
    conn = _connect()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            f"SELECT id, name, created_at FROM {table} "
            f"WHERE canvas_id = %s ORDER BY id", (canvas_id,))
        return [{'id': r['id'], 'name': r['name']} for r in cursor.fetchall()]
    finally:
        cursor.close()
        conn.close()


def _container_create(canvas_id, table, name):
    conn = _connect()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            f"INSERT INTO {table} (canvas_id, name, created_at) "
            f"VALUES (%s, %s, %s)", (canvas_id, name, get_jst_now()))
        conn.commit()
        return cursor.lastrowid
    finally:
        cursor.close()
        conn.close()


@our_meeting_bp.route('/api/canvas/<int:canvas_id>/plans')
@login_required
def api_plans(canvas_id):
    if _canvas_owner_or_none(canvas_id, session.get('user_id')) is None:
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    return jsonify({'success': True, 'plans': _container_list(canvas_id, 'awami_plans')})


@our_meeting_bp.route('/api/canvas/<int:canvas_id>/plans/create', methods=['POST'])
@login_required
def api_plan_create(canvas_id):
    if _canvas_owner_or_none(canvas_id, session.get('user_id')) is None:
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    name = ((request.json or {}).get('name') or '').strip() or '無題の計画'
    pid = _container_create(canvas_id, 'awami_plans', name)
    return jsonify({'success': True, 'id': pid, 'name': name})


@our_meeting_bp.route('/api/canvas/<int:canvas_id>/records')
@login_required
def api_records(canvas_id):
    if _canvas_owner_or_none(canvas_id, session.get('user_id')) is None:
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    conn = _connect()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT id, name, flow_count, poll_count, created_at "
            "FROM awami_records WHERE canvas_id = %s ORDER BY id DESC", (canvas_id,))
        recs = [{'id': r['id'], 'name': r['name'],
                 'flow_count': r['flow_count'], 'poll_count': r['poll_count'],
                 'created_at': r['created_at'].strftime('%Y-%m-%d %H:%M')
                               if r['created_at'] else ''} for r in cursor.fetchall()]
        return jsonify({'success': True, 'records': recs})
    finally:
        cursor.close()
        conn.close()


@our_meeting_bp.route('/api/canvas/<int:canvas_id>/records/snapshot', methods=['POST'])
@login_required
def api_record_snapshot(canvas_id):
    """いまこの瞬間の討論展開＋投票結果を固定保存する（owner専用）。"""
    user_id = session.get('user_id')
    if _canvas_owner_or_none(canvas_id, user_id) is None:
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    name = ((request.json or {}).get('name') or '').strip() or '無題の記録'
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        # スナップショットは owner 視点で凍結（console_url も残す）
        events, choice_polls = _build_live_record(cursor, canvas_id, True)
        snap = json.dumps({'events': events, 'choice_polls': choice_polls},
                          ensure_ascii=False)
        cursor.execute(
            "INSERT INTO awami_records "
            "(canvas_id, name, snapshot_json, flow_count, poll_count, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (canvas_id, name, snap, len(events), len(choice_polls), get_jst_now()))
        conn.commit()
        return jsonify({'success': True, 'id': cursor.lastrowid, 'name': name,
                        'flow_count': len(events), 'poll_count': len(choice_polls)})
    except Exception as e:
        logging.error("awami api_record_snapshot error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


def _container_rename(table, cid, user_id, name):
    conn = _connect()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            f"SELECT x.id FROM {table} x JOIN awami_canvases c "
            f"ON c.id = x.canvas_id WHERE x.id = %s AND c.owner_user_id = %s",
            (cid, user_id))
        if cursor.fetchone() is None:
            return False
        cursor.execute(f"UPDATE {table} SET name = %s WHERE id = %s", (name, cid))
        conn.commit()
        return True
    finally:
        cursor.close()
        conn.close()


def _container_delete(table, cid, user_id):
    conn = _connect()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            f"SELECT x.id FROM {table} x JOIN awami_canvases c "
            f"ON c.id = x.canvas_id WHERE x.id = %s AND c.owner_user_id = %s",
            (cid, user_id))
        if cursor.fetchone() is None:
            return False
        cursor.execute(f"DELETE FROM {table} WHERE id = %s", (cid,))
        conn.commit()
        return True
    finally:
        cursor.close()
        conn.close()


@our_meeting_bp.route('/api/plan/<int:plan_id>/rename', methods=['POST'])
@login_required
def api_plan_rename(plan_id):
    name = ((request.json or {}).get('name') or '').strip() or '無題の計画'
    ok = _container_rename('awami_plans', plan_id, session.get('user_id'), name)
    return jsonify({'success': ok, 'name': name}) if ok else \
        (jsonify({'success': False, 'error': '権限がありません'}), 403)


@our_meeting_bp.route('/api/plan/<int:plan_id>/delete', methods=['POST'])
@login_required
def api_plan_delete_container(plan_id):
    # 計画の器を消す。項目も一緒に消す（素朴に）。
    user_id = session.get('user_id')
    if not _container_delete('awami_plans', plan_id, user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM awami_plan_items WHERE plan_id = %s", (plan_id,))
        conn.commit()
    finally:
        cursor.close()
        conn.close()
    return jsonify({'success': True})


@our_meeting_bp.route('/api/record/<int:record_id>/rename', methods=['POST'])
@login_required
def api_record_rename(record_id):
    name = ((request.json or {}).get('name') or '').strip() or '無題の記録'
    ok = _container_rename('awami_records', record_id, session.get('user_id'), name)
    return jsonify({'success': ok, 'name': name}) if ok else \
        (jsonify({'success': False, 'error': '権限がありません'}), 403)


@our_meeting_bp.route('/api/record/<int:record_id>/delete', methods=['POST'])
@login_required
def api_record_delete(record_id):
    # スナップショットを削除する（撮り直し・未熟なものを捨てる用）。
    user_id = session.get('user_id')
    if not _container_delete('awami_records', record_id, user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    return jsonify({'success': True})


# =========================================================
# 計画・記録の一覧ページ（独立ページ・owner専用）
# 量が増えてもプルダウンで辛くならないよう、本格的な一覧表で管理する。
# =========================================================
@our_meeting_bp.route('/canvas/<int:canvas_id>/plans')
@login_required
def canvas_plans(canvas_id):
    canvas = _canvas_owner_or_none(canvas_id, session.get('user_id'))
    if canvas is None:
        return "プレゼン計画の一覧は講師／司会者のみ開けます", 403
    return render_template('awami/canvas_plans.html', canvas=canvas)


@our_meeting_bp.route('/canvas/<int:canvas_id>/records')
@login_required
def canvas_records(canvas_id):
    canvas = _canvas_owner_or_none(canvas_id, session.get('user_id'))
    if canvas is None:
        return "記録の一覧は講師／司会者のみ開けます", 403
    return render_template('awami/canvas_records.html', canvas=canvas)


@our_meeting_bp.route('/api/canvas/<int:canvas_id>/plans_detail')
@login_required
def api_plans_detail(canvas_id):
    """計画一覧（項目数・作成日時つき）"""
    if _canvas_owner_or_none(canvas_id, session.get('user_id')) is None:
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    conn = _connect()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT p.id, p.name, p.created_at, "
            "  (SELECT COUNT(*) FROM awami_plan_items pi WHERE pi.plan_id = p.id) AS n_items "
            "FROM awami_plans p WHERE p.canvas_id = %s ORDER BY p.id DESC", (canvas_id,))
        plans = [{'id': r['id'], 'name': r['name'], 'n_items': r['n_items'],
                  'created_at': r['created_at'].strftime('%Y-%m-%d %H:%M')
                                if r['created_at'] else ''} for r in cursor.fetchall()]
        return jsonify({'success': True, 'plans': plans})
    finally:
        cursor.close()
        conn.close()