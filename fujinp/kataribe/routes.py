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
import json
import logging
import os
import re

from pytz import timezone

from flask import (
    render_template, request, jsonify, session,
    redirect, url_for, send_from_directory
)
import mysql.connector

# FUJIN-P共通モジュール（常に存在する前提）
from config import Config
from db import DatabaseConfig, Tables
from decorators import login_required, admin_required
from auth import redirect_to_dashboard

from . import kataribe_bp

# ── 認可（deny by default） ──
# 作る・直すのは admin だけ．見るのはログイン済みなら誰でもできる．
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


@kataribe_bp.before_request
def _gate():
    """入口で一括して閉じる．未ログインは各ルートのデコレータに任せる．"""
    if not session.get('user_id'):
        return None
    if request.endpoint in VIEW_ENDPOINTS:
        return None                      # 閲覧はログイン済みなら通す
    if not _may_edit():
        return redirect_to_dashboard()   # 編集系は admin だけ
    return None


# ── 日時ヘルパー（FUJIN-P標準） ──
JST = timezone('Asia/Tokyo')


def get_jst_now():
    """現在の日時をJSTで取得（naive datetime）．INSERT/UPDATEに使う．"""
    return datetime.datetime.now(JST).replace(tzinfo=None)


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


def normalize_spec(spec):
    """スペックJSONを検証し，欠けた項目を補って返す．不正なら ValueError．

    タイル方式（v2）と旧ブロック方式（v1）の両方を受け付ける．
    シーンに tiles があればタイル方式，なければ blocks を見る．
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

        tiles = sc.get('tiles')
        if isinstance(tiles, list) and tiles:
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
                scene['tiles'] = []
        out['scenes'].append(scene)
    return out


# ── 画面ルート ──

@kataribe_bp.route('/')
@login_required
def index():
    """エディタ画面（一覧・編集・プレビュー）．編集できない利用者は閲覧一覧へ"""
    if not _may_edit():
        return redirect(url_for('kataribe.gallery'))
    return render_template('kataribe/index.html')


@kataribe_bp.route('/gallery')
@login_required
def gallery():
    """閲覧用の一覧（ログイン済みなら誰でも見られる）"""
    rows = []
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT p.id, p.title, p.updated_at, u.full_name AS author
            FROM kataribe_presentations p
            LEFT JOIN users u ON u.id = p.user_id
            ORDER BY p.updated_at DESC
        """)
        for r in cursor.fetchall():
            rows.append({
                'id': r['id'],
                'title': r['title'],
                'author': r.get('author') or '',
                'updated_at': fmt_datetime(r.get('updated_at'))
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
    """再生専用画面（ログイン済みなら誰でも）"""
    return render_template('kataribe/play.html',
                           pres_id=pres_id, can_edit=_may_edit())


@kataribe_bp.route('/return_to_fujin')
@login_required
def return_to_fujin():
    """FUJINダッシュボードに戻る"""
    return redirect_to_dashboard()


# ── データAPI ──

@kataribe_bp.route('/api/list', methods=['GET'])
@admin_required
def api_list():
    """自分のプレゼン一覧を取得"""
    try:
        user_id = session.get('user_id')
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT id, title, updated_at
            FROM kataribe_presentations
            WHERE user_id = %s
            ORDER BY updated_at DESC
        """, (user_id,))
        rows = cursor.fetchall()
        for r in rows:
            r['updated_at'] = fmt_datetime(r.get('updated_at'))
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
    """プレゼン1件を取得．編集できる利用者は自分のもの，閲覧だけの利用者は誰のものでも読める"""
    try:
        user_id = session.get('user_id')
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        if _may_edit():
            cursor.execute("""
                SELECT id, title, spec_json, created_at, updated_at
                FROM kataribe_presentations
                WHERE id = %s AND user_id = %s
            """, (pres_id, user_id))
        else:
            cursor.execute("""
                SELECT id, title, spec_json, created_at, updated_at
                FROM kataribe_presentations
                WHERE id = %s
            """, (pres_id,))
        row = cursor.fetchone()
        if not row:
            return jsonify({'success': False, 'error': '見つかりません'}), 404
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
@admin_required
def api_save():
    """プレゼンの新規作成・更新"""
    try:
        data = request.json or {}
        user_id = session.get('user_id')
        pres_id = data.get('id')
        try:
            spec = normalize_spec(data.get('spec') or {})
        except ValueError as ve:
            return jsonify({'success': False, 'error': str(ve)}), 400
        title = (data.get('title') or spec.get('title') or '').strip()
        if not title:
            return jsonify({'success': False, 'error': 'タイトルは必須です'}), 400
        spec['title'] = title

        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        now = get_jst_now()
        spec_text = json.dumps(spec, ensure_ascii=False)

        if pres_id:
            cursor.execute("""
                UPDATE kataribe_presentations
                SET title = %s, spec_json = %s, updated_at = %s
                WHERE id = %s AND user_id = %s
            """, (title, spec_text, now, pres_id, user_id))
            if cursor.rowcount == 0:
                conn.rollback()
                return jsonify({'success': False, 'error': '対象がありません'}), 404
        else:
            cursor.execute("""
                INSERT INTO kataribe_presentations
                    (user_id, title, spec_json, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s)
            """, (user_id, title, spec_text, now, now))
            pres_id = cursor.lastrowid

        conn.commit()
        return jsonify({'success': True, 'id': pres_id})
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
@admin_required
def api_delete(pres_id):
    """プレゼンの削除（本人のもののみ）"""
    try:
        user_id = session.get('user_id')
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
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
@admin_required
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
ALLOWED_UPLOAD_EXT = ALLOWED_IMG_EXT + ALLOWED_EMBED_EXT
MAX_IMG_BYTES = 8 * 1024 * 1024      # 1件あたり8MBまで


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
@admin_required
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


@kataribe_bp.route('/api/images', methods=['GET'])
@admin_required
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
