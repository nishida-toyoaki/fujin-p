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

"""かたりべ (kataribe) - ルート定義（v1はClaude連携を依頼文コピー方式で行うため，AI呼び出しAPIは持たない）"""
import datetime
from functools import wraps
import json
import logging
import os
import re
import uuid

from pytz import timezone

from flask import (
    render_template, request, jsonify, session,
    redirect, url_for, send_from_directory
)
import mysql.connector

# FUJIN-P共通モジュール（常に存在する前提）
from config import Config
from db import DatabaseConfig, Tables
from decorators import login_required
from auth import redirect_to_dashboard

from . import kataribe_bp

# ── 認可（deny by default） ──
# 作る・直すのは admin だけ．見るのはログイン済みのうち，各プレゼンの
# 公開範囲（share_key，マイノートと同区分）で許された利用者だけ．
# 将来グループへ広げるときは ALLOWED_GROUPS にグループコードを並べ，
# _may_edit() にその判定を1つ足せばよい（各ルートは触らない）．
ALLOWED_GROUPS = ()

# 閲覧だけの利用者にも開くエンドポイント
VIEW_ENDPOINTS = (
    'kataribe.gallery', 'kataribe.play', 'kataribe.api_get',
    'kataribe.image', 'kataribe.return_to_fujin', 'kataribe.index',
)


def _may_edit():
    """作品を作る・直すことができる利用者か．"""
    if session.get('user_category') == 'admin':
        return True
    # 将来: if set(session.get('user_groups') or ()) & set(ALLOWED_GROUPS): return True
    return False


def edit_required(view):
    """編集API用の認可。HTMLへ転送せず、常にJSONで失敗理由を返す。"""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get('user_id'):
            return jsonify({'success': False, 'error': 'ログインが切れています。再ログインしてください'}), 401
        if not _may_edit():
            return jsonify({'success': False, 'error': 'この操作を行う編集権限がありません'}), 403
        return view(*args, **kwargs)
    return wrapped


@kataribe_bp.before_request
def _gate():
    """入口で一括して閉じる．未ログインは各ルートのデコレータに任せる．"""
    if not session.get('user_id'):
        return None
    if request.endpoint in VIEW_ENDPOINTS:
        return None                      # 閲覧はログイン済みなら通す
    if not _may_edit():
        if request.endpoint and request.endpoint.startswith('kataribe.api_'):
            return jsonify({'success': False, 'error': 'この操作を行う編集権限がありません'}), 403
        return redirect_to_dashboard()   # 編集系は admin だけ
    return None


# ── 日時ヘルパー（FUJIN-P標準） ──
JST = timezone('Asia/Tokyo')


def get_jst_now():
    """現在の日時をJSTで取得（naive datetime）．INSERT/UPDATEに使う．"""
    return datetime.datetime.now(JST).replace(tzinfo=None)


# ── 公開範囲（マイノート・コレポと同じ区分） ──
#   private        - 所有者とadminのみ
#   public         - ログイン済みの全ユーザ（ゲスト含む）
#   domestic       - 構成員（regular）だけ
#   group          - 指定グループの有効所属者だけ
#   domestic_group - 構成員または指定グループの有効所属者（和集合）
# 許可グループは kataribe_access_groups（全削除→再挿入方式）に持つ．
SHARE_KEYS = ('private', 'public', 'domestic', 'group', 'domestic_group')

SHARE_LABELS = {
    'private': '非公開',
    'public': 'ゲストにも',
    'domestic': '構成員だけ',
    'group': 'グループ',
    'domestic_group': '構成員＋グループ',
}

SHARE_ICONS = {
    'private': '🔒',
    'public': '🌐',
    'domestic': '🏢',
    'group': '👥',
    'domestic_group': '🏢＋👥',
}


def normalize_share_key(value, current='private'):
    """公開範囲キーを SHARE_KEYS のいずれかに正規化する"""
    if value in SHARE_KEYS:
        return value
    return current if current in SHARE_KEYS else 'private'


def get_user_active_group_ids(user_id):
    """ユーザーが現在有効に所属しているグループIDのリスト
    （user_groups / user_group_memberships を参照，有効期間チェック付き）"""
    if not user_id:
        return []
    try:
        now = get_jst_now()
        with mysql.connector.connect(**DatabaseConfig.default()) as conn:
            with conn.cursor(dictionary=True) as cursor:
                cursor.execute("""
                    SELECT group_id FROM user_group_memberships
                    WHERE user_id = %s
                      AND (valid_from IS NULL OR valid_from <= %s)
                      AND (valid_until IS NULL OR valid_until >= %s)
                """, (user_id, now, now))
                return [r['group_id'] for r in cursor.fetchall()]
    except mysql.connector.Error as e:
        logging.error("kataribe get_user_active_group_ids error: %s", e)
        return []


def get_all_user_groups():
    """全ユーザーグループの一覧（公開範囲の選択肢用）"""
    try:
        with mysql.connector.connect(**DatabaseConfig.default()) as conn:
            with conn.cursor(dictionary=True) as cursor:
                cursor.execute("SELECT id, name FROM user_groups ORDER BY id DESC")
                return cursor.fetchall()
    except mysql.connector.Error as e:
        logging.error("kataribe get_all_user_groups error: %s", e)
        return []


def get_pres_access_group_ids(pres_id):
    """プレゼンのグループ公開で許可されているグループIDのリスト"""
    try:
        with mysql.connector.connect(**DatabaseConfig.default()) as conn:
            with conn.cursor(dictionary=True) as cursor:
                cursor.execute("""
                    SELECT group_id FROM kataribe_access_groups WHERE pres_id = %s
                """, (pres_id,))
                return [r['group_id'] for r in cursor.fetchall()]
    except mysql.connector.Error as e:
        logging.error("kataribe get_pres_access_group_ids error: %s", e)
        return []


def can_view_pres(pres, user_id, user_category, my_group_ids=None):
    """プレゼンの閲覧可否を公開範囲（share_key）で判定する（マイノートと同じ規則）．

    呼び出し元はすべて @login_required 配下にある．
      admin／所有者     - 常に可
      public            - ログイン済みの全ユーザ（ゲスト含む）
      domestic          - 構成員（regular）のみ
      group             - 指定グループの有効所属者のみ
      domestic_group    - 構成員または指定グループの有効所属者
      private           - 所有者とadminのみ
    my_group_ids を渡すと所属グループの問い合わせを省く（一覧で使う）．
    """
    if user_category == 'admin' or pres.get('user_id') == user_id:
        return True

    policy = pres.get('share_key') or 'private'

    if policy == 'public':
        return True

    if policy == 'domestic':
        return user_category == 'regular'

    if policy in ('group', 'domestic_group'):
        if policy == 'domestic_group' and user_category == 'regular':
            return True
        allowed = set(get_pres_access_group_ids(pres['id']))
        mine = set(my_group_ids if my_group_ids is not None
                   else get_user_active_group_ids(user_id))
        return bool(allowed & mine)

    return False


def _load_pres_head(pres_id):
    """プレゼンの見出し情報（id, user_id, title, share_key）を1件返す．無ければ None"""
    try:
        with mysql.connector.connect(**DatabaseConfig.default()) as conn:
            with conn.cursor(dictionary=True) as cursor:
                cursor.execute("""
                    SELECT id, user_id, title, share_key
                    FROM kataribe_presentations WHERE id = %s
                """, (pres_id,))
                return cursor.fetchone()
    except mysql.connector.Error as e:
        logging.error("kataribe _load_pres_head error: %s", e)
        return None


def _save_access_groups(cursor, pres_id, share_key, group_ids):
    """許可グループを全削除→再挿入で書き換える（group系以外は空にする）"""
    cursor.execute("DELETE FROM kataribe_access_groups WHERE pres_id = %s", (pres_id,))
    if share_key in ('group', 'domestic_group'):
        for gid in sorted(set(group_ids)):
            cursor.execute("""
                INSERT INTO kataribe_access_groups (pres_id, group_id) VALUES (%s, %s)
            """, (pres_id, gid))


def fmt_datetime(d):
    """datetime → 'YYYY-MM-DD HH:MM' 文字列．None は空文字．"""
    if d is None:
        return ''
    if isinstance(d, (datetime.datetime, datetime.date)):
        return d.strftime('%Y-%m-%d %H:%M')
    return str(d)


# ── スペック（ブロック記述形式）ユーティリティ ──

BLOCK_TYPES = ('title', 'lead', 'card', 'band', 'note')   # 旧形式（v1）のブロック種別
THEMES = ('light', 'dark', 'cover')
KINDS = ('body', 'cover', 'end')          # 本文／表紙／エンドノート


def _step_pair(item):
    """in / out を正規化して返す．"""
    try:
        b_in = max(1, int(item.get('in', 1)))
    except Exception:
        b_in = 1
    b_out = item.get('out', None)
    if b_out in ('', None):
        b_out = None
    else:
        try:
            b_out = int(b_out)
            if b_out <= b_in:
                b_out = b_in + 1
        except Exception:
            b_out = None
    return b_in, b_out


STEP_OPS = ('row', 'col', 'box', 'end', 'part', 'br')


def normalize_steps(steps):
    """手続き方式（v4）の命令列を検証して返す．

    命令は row（行）／col（段）／box（箱を開く）／end（箱を閉じる）／
    part（部品）／br（改行）の6種類．part の at は「第何歩で出るか」で，
    0 は最初から出ている部品．書かれていなければ再生側が順に振る．
    """
    out = []
    for c in steps:
        if not isinstance(c, dict):
            continue
        op = c.get('op', 'part')
        if op not in STEP_OPS:
            continue
        if op in ('end', 'br'):
            out.append({'op': op})
        elif op == 'row':
            cmd = {'op': 'row'}
            if c.get('align') in ('left', 'center', 'right'):
                cmd['align'] = c['align']
            out.append(cmd)
        elif op == 'col':
            try:
                w = max(1, min(12, int(c.get('w', 1))))
            except Exception:
                w = 1
            cmd = {'op': 'col', 'w': w}
            if c.get('align') in ('middle', 'bottom'):
                cmd['align'] = c['align']
            out.append(cmd)
        elif op == 'box':
            out.append({'op': 'box', 'cls': str(c.get('cls', ''))[:120]})
        else:
            cmd = {
                'op': 'part',
                'name': str(c.get('name', ''))[:100],
                'html': str(c.get('html', '')),
            }
            at = c.get('at')
            if at is not None:
                try:
                    cmd['at'] = max(0, int(at))
                except Exception:
                    pass
            if c.get('flow') == 'inline':
                cmd['flow'] = 'inline'
            out.append(cmd)
    return out


def normalize_spec(spec):
    """スペックJSONを検証し，欠けた項目を補って返す．不正なら ValueError．

    手続き方式（v4）と，タイル方式（v2）・旧ブロック方式（v1）を受け付ける．
    シーンに steps があれば手続き方式，なければ tiles，それも無ければ blocks を見る．
    """
    if not isinstance(spec, dict):
        raise ValueError('スペックはオブジェクトである必要があります')
    out = {
        'title': str(spec.get('title', '無題のプレゼン'))[:200],
        'scenes': []
    }
    scenes = spec.get('scenes', [])
    if not isinstance(scenes, list):
        raise ValueError('scenes は配列である必要があります')

    for sc in scenes:
        if not isinstance(sc, dict):
            continue
        theme = sc.get('theme', 'light')
        if theme not in THEMES:
            theme = 'light'
        kind = sc.get('kind', 'body')
        if kind not in KINDS:
            kind = 'body'
        scene = {
            'title': str(sc.get('title', ''))[:200],
            'subtitle': str(sc.get('subtitle', ''))[:200],
            'kind': kind,
            'theme': theme,
        }
        if sc.get('align') == 'top':
            scene['align'] = 'top'          # 詰め方：上そろえ（既定は中央そろえ）

        steps = sc.get('steps')
        tiles = sc.get('tiles')
        if isinstance(steps, list) and steps:
            scene['steps'] = normalize_steps(steps)
        elif isinstance(tiles, list) and tiles:
            scene['tiles'] = []
            for t in tiles:
                if not isinstance(t, dict):
                    continue
                t_in, t_out = _step_pair(t)
                try:
                    row = max(1, int(t.get('row', 1)))
                except Exception:
                    row = 1
                try:
                    span = max(1, min(6, int(t.get('span', 1))))
                except Exception:
                    span = 1
                scene['tiles'].append({
                    'name': str(t.get('name', ''))[:100],
                    'html': str(t.get('html', '')),
                    'row': row,
                    'span': span,
                    'in': t_in,
                    'out': t_out,
                    'narration': str(t.get('narration', ''))
                })
        else:
            blocks = sc.get('blocks', [])
            if not isinstance(blocks, list):
                blocks = []
            norm_blocks = []
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                btype = b.get('type', 'card')
                if btype not in BLOCK_TYPES:
                    btype = 'card'
                b_in, b_out = _step_pair(b)
                norm_blocks.append({
                    'type': btype,
                    'heading': str(b.get('heading', '')),
                    'body': str(b.get('body', '')),
                    'in': b_in,
                    'out': b_out,
                    'narration': str(b.get('narration', ''))
                })
            if norm_blocks:
                scene['blocks'] = norm_blocks
            else:
                scene['steps'] = []          # 空のシーンは手続き方式の空箱として持つ
        out['scenes'].append(scene)
    return out


# ── 画面ルート ──

@kataribe_bp.route('/')
@login_required
def index():
    """エディタ画面（一覧・編集・プレビュー）．編集できない利用者は閲覧一覧へ"""
    if not _may_edit():
        return redirect(url_for('kataribe.gallery'))
    return render_template('kataribe/index.html',
                           all_groups=get_all_user_groups(),
                           share_labels=SHARE_LABELS)


@kataribe_bp.route('/gallery')
@login_required
def gallery():
    """閲覧用の一覧．公開範囲（share_key）で自分が見られるものだけを並べる"""
    rows = []
    user_id = session.get('user_id')
    category = session.get('user_category')
    my_groups = get_user_active_group_ids(user_id)
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT p.id, p.user_id, p.title, p.share_key, p.updated_at,
                   u.full_name AS author
            FROM kataribe_presentations p
            LEFT JOIN users u ON u.id = p.user_id
            ORDER BY p.updated_at DESC
        """)
        for r in cursor.fetchall():
            if not can_view_pres(r, user_id, category, my_groups):
                continue
            key = normalize_share_key(r.get('share_key'))
            rows.append({
                'id': r['id'],
                'title': r['title'],
                'author': r.get('author') or '',
                'updated_at': fmt_datetime(r.get('updated_at')),
                'share_key': key,
                'share_label': SHARE_ICONS[key] + ' ' + SHARE_LABELS[key],
            })
    except Exception as e:
        logging.error("kataribe gallery error: %s", e)
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()
    return render_template('kataribe/gallery.html',
                           presentations=rows, can_edit=_may_edit())


@kataribe_bp.route('/play/<int:pres_id>')
@login_required
def play(pres_id):
    """再生専用画面．公開範囲で閲覧できない利用者は一覧へ戻す"""
    head = _load_pres_head(pres_id)
    if not head or not can_view_pres(head, session.get('user_id'),
                                     session.get('user_category')):
        return redirect(url_for('kataribe.gallery'))
    return render_template('kataribe/play.html',
                           pres_id=pres_id, can_edit=_may_edit())


@kataribe_bp.route('/handout/<int:pres_id>')
@login_required
def handout(pres_id):
    """シーン一覧の配布資料（M×N・A4縦・印刷でPDF化）"""
    return render_template('kataribe/handout.html', pres_id=pres_id)


@kataribe_bp.route('/return_to_fujin')
@login_required
def return_to_fujin():
    """FUJINダッシュボードに戻る"""
    return redirect_to_dashboard()


# ── データAPI ──

@kataribe_bp.route('/api/list', methods=['GET'])
@edit_required
def api_list():
    """自分のプレゼン一覧を取得"""
    try:
        user_id = session.get('user_id')
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT id, title, share_key, updated_at
            FROM kataribe_presentations
            WHERE user_id = %s
            ORDER BY updated_at DESC
        """, (user_id,))
        rows = cursor.fetchall()
        for r in rows:
            r['updated_at'] = fmt_datetime(r.get('updated_at'))
            r['share_key'] = normalize_share_key(r.get('share_key'))
            r['share_icon'] = SHARE_ICONS[r['share_key']]
            r['share_label'] = SHARE_LABELS[r['share_key']]
        return jsonify({'success': True, 'presentations': rows})
    except Exception as e:
        logging.error("kataribe api_list error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@kataribe_bp.route('/api/get/<int:pres_id>', methods=['GET'])
@login_required
def api_get(pres_id):
    """プレゼン1件を取得．編集できる利用者は自分のもの，閲覧だけの利用者は公開範囲で見られるもの"""
    try:
        user_id = session.get('user_id')
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        if _may_edit():
            cursor.execute("""
                SELECT id, user_id, title, share_key, spec_json, created_at, updated_at
                FROM kataribe_presentations
                WHERE id = %s AND user_id = %s
            """, (pres_id, user_id))
        else:
            cursor.execute("""
                SELECT id, user_id, title, share_key, spec_json, created_at, updated_at
                FROM kataribe_presentations
                WHERE id = %s
            """, (pres_id,))
        row = cursor.fetchone()
        if not row:
            return jsonify({'success': False, 'error': '見つかりません'}), 404
        if not can_view_pres(row, user_id, session.get('user_category')):
            return jsonify({'success': False, 'error': 'このプレゼンを閲覧する権限がありません'}), 403
        share_key = normalize_share_key(row.get('share_key'))
        try:
            spec = json.loads(row['spec_json']) if row['spec_json'] else {}
        except Exception:
            spec = {}
        return jsonify({
            'success': True,
            'presentation': {
                'id': row['id'],
                'title': row['title'],
                'spec': normalize_spec(spec) if spec else {'title': row['title'], 'scenes': []},
                'share_key': share_key,
                'access_groups': (get_pres_access_group_ids(row['id'])
                                  if share_key in ('group', 'domestic_group') else []),
                'created_at': fmt_datetime(row.get('created_at')),
                'updated_at': fmt_datetime(row.get('updated_at'))
            }
        })
    except Exception as e:
        logging.error("kataribe api_get error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@kataribe_bp.route('/api/save', methods=['POST'])
@edit_required
def api_save():
    """プレゼンの新規作成・更新"""
    try:
        data = request.json or {}
        user_id = session.get('user_id')
        pres_id = data.get('id')
        is_new = not pres_id
        try:
            spec = normalize_spec(data.get('spec') or {})
        except ValueError as ve:
            return jsonify({'success': False, 'error': str(ve)}), 400
        title = (data.get('title') or spec.get('title') or '').strip()
        if not title:
            return jsonify({'success': False, 'error': 'タイトルは必須です'}), 400
        spec['title'] = title

        # 公開範囲（省略時は既存値，新規なら private）
        group_ids = []
        for g in (data.get('access_groups') or []):
            try:
                group_ids.append(int(g))
            except (TypeError, ValueError):
                pass
        share_given = 'share_key' in data
        share_key = normalize_share_key(data.get('share_key'), 'private')
        if share_given and share_key in ('group', 'domestic_group') and not group_ids:
            return jsonify({'success': False,
                            'error': 'グループ公開では許可するグループを1つ以上選んでください'}), 400

        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        now = get_jst_now()
        spec_text = json.dumps(spec, ensure_ascii=False)

        if pres_id:
            cursor.execute("""
                SELECT share_key FROM kataribe_presentations
                WHERE id = %s AND user_id = %s
            """, (pres_id, user_id))
            cur = cursor.fetchone()
            if not cur:
                conn.rollback()
                return jsonify({'success': False, 'error': '対象がありません'}), 404
            if not share_given:
                share_key = normalize_share_key(cur.get('share_key'))
            cursor.execute("""
                UPDATE kataribe_presentations
                SET title = %s, spec_json = %s, share_key = %s, updated_at = %s
                WHERE id = %s AND user_id = %s
            """, (title, spec_text, share_key, now, pres_id, user_id))
        else:
            cursor.execute("""
                INSERT INTO kataribe_presentations
                    (user_id, title, spec_json, share_key, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (user_id, title, spec_text, share_key, now, now))
            pres_id = cursor.lastrowid

        if share_given or is_new:
            _save_access_groups(cursor, pres_id, share_key, group_ids)

        conn.commit()
        return jsonify({'success': True, 'id': pres_id,
                        'share_key': share_key,
                        'share_label': SHARE_LABELS[share_key]})
    except Exception as e:
        logging.error("kataribe api_save error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@kataribe_bp.route('/api/delete/<int:pres_id>', methods=['POST'])
@edit_required
def api_delete(pres_id):
    """プレゼンの削除（本人のもののみ）"""
    try:
        user_id = session.get('user_id')
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute("DELETE FROM kataribe_access_groups WHERE pres_id = %s", (pres_id,))
        cursor.execute("""
            DELETE FROM kataribe_presentations
            WHERE id = %s AND user_id = %s
        """, (pres_id, user_id))
        conn.commit()
        return jsonify({'success': True, 'deleted': cursor.rowcount})
    except Exception as e:
        logging.error("kataribe api_delete error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@kataribe_bp.route('/api/sample', methods=['GET'])
@edit_required
def api_sample():
    """同梱サンプル（data_for_distribution/sample_presentation.json）を返す"""
    try:
        import os
        base = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(base, 'data_for_distribution', 'sample_presentation.json')
        with open(path, encoding='utf-8') as f:
            spec = json.load(f)
        return jsonify({'success': True, 'spec': normalize_spec(spec)})
    except Exception as e:
        logging.error("kataribe api_sample error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


# ── 画像アップロード ──
# 保存先: ~/fujinp/kataribe/img （このファイルと同じ階層の img/）
IMG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'img')
ALLOWED_IMG_EXT = ('.png', '.jpg', '.jpeg', '.svg')
ALLOWED_EMBED_EXT = ('.html', '.htm')          # 埋め込み用の自己完結HTML
ALLOWED_VIDEO_EXT = ('.mp4',)                  # 公開UPで扱う動画
ALLOWED_UPLOAD_EXT = ALLOWED_IMG_EXT + ALLOWED_EMBED_EXT
MAX_IMG_BYTES = 8 * 1024 * 1024      # 画像は1件8MBまで
MAX_VIDEO_BYTES = 64 * 1024 * 1024   # 動画は1件64MBまで
# 公開画像の置き場（PythonAnywhereの /static/ マッピング先．フルURLで参照できる）
PUBLIC_IMG_DIR = os.path.join(os.path.expanduser('~'), 'static', 'mdimgs')


def _safe_stem(name):
    """ファイル名から安全な語幹を作る（日本語も残す）．"""
    stem = os.path.splitext(os.path.basename(name or ''))[0]
    stem = re.sub(r'[^\w\u3040-\u30ff\u4e00-\u9fff-]', '_', stem)
    return (stem or 'image')[:40]


@kataribe_bp.route('/img/<path:filename>')
@login_required
def image(filename):
    """アップロードされた画像を返す"""
    return send_from_directory(IMG_DIR, filename)


@kataribe_bp.route('/api/upload_image', methods=['POST'])
@edit_required
def api_upload_image():
    """画像を ~/fujinp/kataribe/img に保存し，参照用URLを返す"""
    try:
        f = request.files.get('file')
        if not f or not f.filename:
            return jsonify({'success': False, 'error': 'ファイルがありません'}), 400
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ALLOWED_UPLOAD_EXT:
            return jsonify({'success': False,
                            'error': 'PNG / JPG / SVG / HTML のみ扱えます'}), 400

        data = f.read()
        if len(data) > MAX_IMG_BYTES:
            return jsonify({'success': False,
                            'error': '画像が大きすぎます（8MBまで）'}), 400

        os.makedirs(IMG_DIR, exist_ok=True)
        stamp = get_jst_now().strftime('%Y%m%d%H%M%S')
        fname = '{}_{}_{}{}'.format(stamp, session.get('user_id', 0), _safe_stem(f.filename), ext)
        with open(os.path.join(IMG_DIR, fname), 'wb') as out:
            out.write(data)

        return jsonify({
            'success': True,
            'filename': fname,
            'url': url_for('kataribe.image', filename=fname),
            'size': len(data),
            'kind': 'embed' if ext in ALLOWED_EMBED_EXT else 'image'
        })
    except Exception as e:
        logging.error("kataribe api_upload_image error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


def _light_sanitize_svg(data):
    """SVGから script 要素とイベント属性を取り除く（公開置き場用の軽い消毒）"""
    try:
        text = data.decode('utf-8', errors='ignore')
    except Exception:
        return data
    text = re.sub(r'<script\b[^>]*>.*?</script>', '', text, flags=re.I | re.S)
    text = re.sub(r'\son[a-z]+\s*=\s*"[^"]*"', '', text, flags=re.I)
    text = re.sub(r"\son[a-z]+\s*=\s*'[^']*'", '', text, flags=re.I)
    return text.encode('utf-8')


@kataribe_bp.route('/api/upload_image_public', methods=['POST'])
@edit_required
def api_upload_image_public():
    """画像・MP4動画を ~/static/mdimgs に保存し，フルURLを返す（公開置き場）"""
    try:
        f = request.files.get('file')
        if not f or not f.filename:
            return jsonify({'success': False, 'error': 'ファイルがありません'}), 400
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ALLOWED_IMG_EXT + ALLOWED_VIDEO_EXT:
            return jsonify({'success': False,
                            'error': '公開UPは PNG / JPG / SVG / MP4 のみ扱えます'}), 400

        data = f.read()
        limit = MAX_VIDEO_BYTES if ext in ALLOWED_VIDEO_EXT else MAX_IMG_BYTES
        if len(data) > limit:
            return jsonify({'success': False,
                            'error': 'ファイルが大きすぎます（画像8MB／動画64MBまで）'}), 400
        if ext == '.svg':
            if b'<svg' not in data[:2048].lower():
                return jsonify({'success': False, 'error': 'SVGファイルではないようです'}), 400
            data = _light_sanitize_svg(data)

        os.makedirs(PUBLIC_IMG_DIR, exist_ok=True)
        stamp = get_jst_now().strftime('%Y%m%d_%H%M%S')
        fname = '{}_{}_{}_{}{}'.format(uuid.uuid4().hex[:8], _safe_stem(f.filename),
                                       session.get('user_id', 0), stamp, ext)
        with open(os.path.join(PUBLIC_IMG_DIR, fname), 'wb') as out:
            out.write(data)

        return jsonify({
            'success': True,
            'filename': fname,
            'url': request.host_url.rstrip('/') + '/static/mdimgs/' + fname,
            'size': len(data),
            'kind': 'video' if ext in ALLOWED_VIDEO_EXT else 'image'
        })
    except Exception as e:
        logging.error("kataribe api_upload_image_public error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@kataribe_bp.route('/api/images', methods=['GET'])
@edit_required
def api_images():
    """アップロード済み画像の一覧（新しい順）"""
    try:
        if not os.path.isdir(IMG_DIR):
            return jsonify({'success': True, 'images': []})
        want = request.args.get('kind', 'image')
        exts = ALLOWED_EMBED_EXT if want == 'embed' else ALLOWED_IMG_EXT
        items = []
        for fn in os.listdir(IMG_DIR):
            if os.path.splitext(fn)[1].lower() not in exts:
                continue
            full = os.path.join(IMG_DIR, fn)
            items.append({
                'filename': fn,
                'url': url_for('kataribe.image', filename=fn),
                'size': os.path.getsize(full),
                'mtime': os.path.getmtime(full)
            })
        items.sort(key=lambda x: x['mtime'], reverse=True)
        for it in items:
            it.pop('mtime', None)
        return jsonify({'success': True, 'images': items})
    except Exception as e:
        logging.error("kataribe api_images error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500