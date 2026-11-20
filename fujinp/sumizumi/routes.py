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

"""すみずみ（sumizumi）— 定理をすみずみまで理解するための学習アプリ（v2）

一つの定理を，結論の式から出発する「骨格」（節点の列）としてたどる．節点を一つ進むごとに
頭の中の文字（記号）が少しずつ増えていく．その様子を「文字の棚」に並べる．各節点には，
説明・プロット・数値・SymPy で導く，の「脇道」がぶら下がり，必要なときだけその場で開く．
骨格の節点をすべて確かめたら終わり．最後に自分用のノートを書く．

  GET  /sumizumi/                                   トップ（定理の一覧と進み具合）
  GET  /sumizumi/t/<theorem_id>?node=<node_id>      骨格の画面（node=note でノート）
  GET  /sumizumi/api/derive/<name>                  SymPy で導く（登録済みの導出だけ）
  POST /sumizumi/api/progress/<theorem_id>/<node_id>   節点を確かめた印をつける／外す（{"done": bool}）
  POST /sumizumi/api/note/<theorem_id>              自分のノートを保存する
  POST /sumizumi/api/preview                        ノートのプレビュー（数式は MathJax が描く）
  POST /sumizumi/api/note/<theorem_id>/export       ノートをマイノート（my_md_notes）へ書き出す
  GET  /sumizumi/return_to_fujin                    FUJIN-P のダッシュボードへ戻る

テーブル：sumizumi_notes（ノート．unit_id 列に定理IDを入れる），sumizumi_progress（確かめた印）．
いずれも default DB．日時はアプリが JST の naive datetime を書く．
教材は content/theorems.json（アプリ配下．配布対象）．学習者が書いた式をサーバで評価する機能は持たない．
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from flask import abort, jsonify, redirect, render_template, request, session, url_for

from auth import redirect_to_dashboard
from db import get_db_cursor
from decorators import login_required

from . import sumizumi_bp as bp

# SymPy の読み込みには時間がかかるので，checks は最初の「導く」のときに読み込む
# （サイト全体の起動を遅くしないため）．

DB = 'default'
JST = timezone(timedelta(hours=9), 'JST')
APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONTENT_PATH = os.path.join(APP_DIR, 'content', 'theorems.json')
MAX_NOTE_CHARS = 200_000
_ID_RE = re.compile(r'^[a-z][a-z0-9_]{0,63}$')
KIND_LABELS = {'explain': '説明', 'plot': 'プロット', 'numeric': '数値', 'sympy': 'SymPyで導く'}


def now_jst():
    return datetime.now(JST).replace(tzinfo=None)


def fmt_dt(v):
    if not v:
        return ''
    if isinstance(v, str):
        return v[:16]
    return v.strftime('%Y-%m-%d %H:%M')


# ─────────────────────────────────────────────────────────────────
# 教材
# ─────────────────────────────────────────────────────────────────

_cache = {'mtime': None, 'data': None}


def load_content():
    mtime = os.path.getmtime(CONTENT_PATH)
    if _cache['mtime'] != mtime:
        with open(CONTENT_PATH, encoding='utf-8') as fp:
            data = json.load(fp)
        data['by_id'] = {t['id']: t for t in data['theorems']}
        _cache.update(mtime=mtime, data=data)
    return _cache['data']


def get_theorem(tid):
    if not _ID_RE.match(tid or ''):
        return None
    return load_content()['by_id'].get(tid)


# ─────────────────────────────────────────────────────────────────
# 記録（ノート・確かめた印）
# ─────────────────────────────────────────────────────────────────

def load_progress(user_id, tid=None):
    """{theorem_id: {node_id: done_at}}．テーブルが無いときは None"""
    try:
        with get_db_cursor(DB) as (cur, conn):
            if tid:
                cur.execute("SELECT theorem_id, node_id, done_at FROM sumizumi_progress WHERE user_id=%s AND theorem_id=%s",
                            (user_id, tid))
            else:
                cur.execute("SELECT theorem_id, node_id, done_at FROM sumizumi_progress WHERE user_id=%s", (user_id,))
            out = {}
            for r in cur.fetchall():
                out.setdefault(r['theorem_id'], {})[r['node_id']] = r['done_at']
            return out
    except Exception as e:
        print(f'[sumizumi] 進み具合を読めませんでした: {e}')
        return None


def load_note(user_id, tid):
    """(row or None, ready)"""
    try:
        with get_db_cursor(DB) as (cur, conn):
            cur.execute("""SELECT content, my_md_note_id, updated_at FROM sumizumi_notes
                           WHERE user_id=%s AND unit_id=%s""", (user_id, tid))
            return cur.fetchone(), True
    except Exception as e:
        print(f'[sumizumi] ノートを読めませんでした: {e}')
        return None, False


def same_origin():
    src = request.headers.get('Origin') or request.headers.get('Referer') or ''
    return bool(src) and urlparse(src).netloc == request.host


def _post_guard():
    if not same_origin():
        abort(403)
    if not request.is_json:
        abort(400)


# ─────────────────────────────────────────────────────────────────
# 画面
# ─────────────────────────────────────────────────────────────────

@bp.route('/')
@login_required
def index():
    data = load_content()
    prog = load_progress(session['user_id'])
    return render_template('sumizumi/index.html', theorems=data['theorems'],
                           progress=prog or {}, progress_ready=prog is not None)


@bp.route('/t/<theorem_id>')
@login_required
def theorem(theorem_id):
    th = get_theorem(theorem_id)
    if not th:
        abort(404)
    nodes = th['nodes']
    prog_all = load_progress(session['user_id'], theorem_id)
    done = (prog_all or {}).get(theorem_id, {})
    node_ids = [n['id'] for n in nodes]

    want = request.args.get('node')
    if want is None:
        # 初めて開いたとき：まだ確かめていない最初の節点（全部済みならノート）
        want = next((i for i in node_ids if i not in done), 'note')
        return redirect(url_for('sumizumi.theorem', theorem_id=theorem_id, node=want))
    if want != 'note' and want not in node_ids:
        abort(404)

    idx = node_ids.index(want) if want != 'note' else len(nodes)
    # 文字の棚：今の節点までに出てきた文字（今の節点の分には「新」をつける）
    shelf = []
    for i, nd in enumerate(nodes[:idx + 1]):
        for s in nd.get('symbols', []):
            shelf.append(dict(s, node_no=i + 1, is_new=(i == idx)))
    note, note_ready = (load_note(session['user_id'], theorem_id) if want == 'note' else (None, True))
    return render_template('sumizumi/theorem.html', th=th, nodes=nodes, idx=idx, node=(nodes[idx] if want != 'note' else None),
                           done=done, progress_ready=prog_all is not None, shelf=shelf,
                           kind_labels=KIND_LABELS, note=note, note_ready=note_ready, fmt_dt=fmt_dt)


@bp.route('/return_to_fujin')
@login_required
def return_to_fujin():
    """FUJIN-P のダッシュボードへ戻る"""
    return redirect_to_dashboard()


# ─────────────────────────────────────────────────────────────────
# API
# ─────────────────────────────────────────────────────────────────

@bp.route('/api/derive/<name>')
@login_required
def api_derive(name):
    from . import checks
    if name not in checks.DERIVATIONS:
        return jsonify({'success': False, 'error': '登録されていない導出です．'}), 404
    return jsonify({'success': True, 'result': checks.run(name)})


@bp.route('/api/progress/<theorem_id>/<node_id>', methods=['POST'])
@login_required
def api_progress(theorem_id, node_id):
    _post_guard()
    th = get_theorem(theorem_id)
    if not th or node_id not in [n['id'] for n in th['nodes']]:
        abort(404)
    done = bool((request.get_json(silent=True) or {}).get('done'))
    try:
        with get_db_cursor(DB) as (cur, conn):
            if done:
                cur.execute("""INSERT INTO sumizumi_progress (user_id, theorem_id, node_id, done_at)
                               VALUES (%s, %s, %s, %s)
                               ON DUPLICATE KEY UPDATE done_at=VALUES(done_at)""",
                            (session['user_id'], theorem_id, node_id, now_jst()))
            else:
                cur.execute("DELETE FROM sumizumi_progress WHERE user_id=%s AND theorem_id=%s AND node_id=%s",
                            (session['user_id'], theorem_id, node_id))
            conn.commit()
    except Exception as e:
        print(f'[sumizumi] 進み具合を記録できませんでした: {e}')
        return jsonify({'success': False, 'error': '記録できませんでした（テーブル sumizumi_progress を確かめてください）．'}), 500
    return jsonify({'success': True, 'done': done})


@bp.route('/api/note/<theorem_id>', methods=['POST'])
@login_required
def api_note_save(theorem_id):
    _post_guard()
    if not get_theorem(theorem_id):
        abort(404)
    content = (request.get_json(silent=True) or {}).get('content', '')
    if not isinstance(content, str):
        return jsonify({'success': False, 'error': '内容が文字列ではありません．'}), 400
    if len(content) > MAX_NOTE_CHARS:
        return jsonify({'success': False, 'error': f'ノートは {MAX_NOTE_CHARS:,} 字までです．'}), 400
    now = now_jst()
    try:
        with get_db_cursor(DB) as (cur, conn):
            cur.execute("""INSERT INTO sumizumi_notes (user_id, unit_id, content, created_at, updated_at)
                           VALUES (%s, %s, %s, %s, %s)
                           ON DUPLICATE KEY UPDATE content=VALUES(content), updated_at=VALUES(updated_at)""",
                        (session['user_id'], theorem_id, content, now, now))
            conn.commit()
    except Exception as e:
        print(f'[sumizumi] ノートを保存できませんでした: {e}')
        return jsonify({'success': False, 'error': 'ノートを保存できませんでした（テーブル sumizumi_notes を確かめてください）．'}), 500
    return jsonify({'success': True, 'updated_at': fmt_dt(now)})


# 数式を Markdown 変換から守る（_ や * が強調に化けないように）
_MATH_RE = re.compile(r'(\$\$.+?\$\$|\\\[.+?\\\]|\\\(.+?\\\)|(?<![\\$])\$(?!\$)[^$\n]+?\$)', re.S)


def render_note(md):
    from markdown_converter import process_markdown
    keep = []

    def stash(m):
        keep.append(m.group(0))
        return f'SUMIZUMIMATH{len(keep) - 1}X'

    html = process_markdown(_MATH_RE.sub(stash, md or ''))

    def restore(m):
        s = keep[int(m.group(1))]
        return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    return re.sub(r'SUMIZUMIMATH(\d+)X', restore, html)


@bp.route('/api/preview', methods=['POST'])
@login_required
def api_preview():
    _post_guard()
    content = (request.get_json(silent=True) or {}).get('content', '')
    if not isinstance(content, str) or len(content) > MAX_NOTE_CHARS:
        return jsonify({'success': False, 'error': '内容を読めませんでした．'}), 400
    return jsonify({'success': True, 'html': render_note(content)})


@bp.route('/api/note/<theorem_id>/export', methods=['POST'])
@login_required
def api_note_export(theorem_id):
    """保存済みのノートをマイノートへ書き出す．2回目からは同じマイノートを上書きする"""
    _post_guard()
    th = get_theorem(theorem_id)
    if not th:
        abort(404)
    uid = session['user_id']
    now = now_jst()
    title = f'すみずみ：{th["title"]}'
    try:
        with get_db_cursor(DB) as (cur, conn):
            cur.execute("SELECT content, my_md_note_id FROM sumizumi_notes WHERE user_id=%s AND unit_id=%s",
                        (uid, theorem_id))
            row = cur.fetchone()
            if not row or not (row['content'] or '').strip():
                return jsonify({'success': False, 'error': 'まずノートを書いて保存してください．'}), 400
            target = None
            if row['my_md_note_id']:
                cur.execute("SELECT id FROM my_md_notes_notes WHERE id=%s AND オーナーID=%s",
                            (row['my_md_note_id'], uid))
                hit = cur.fetchone()
                target = hit['id'] if hit else None
            if target:
                cur.execute("UPDATE my_md_notes_notes SET 名前=%s, 更新日時=%s WHERE id=%s", (title, now, target))
                cur.execute("UPDATE my_md_notes_contents SET 内容=%s, 更新日時=%s WHERE ノートID=%s",
                            (row['content'], now, target))
                if cur.rowcount == 0:
                    cur.execute("""INSERT INTO my_md_notes_contents (ノートID, 内容, 作成日時, 更新日時)
                                   VALUES (%s, %s, %s, %s)""", (target, row['content'], now, now))
                action = 'updated'
            else:
                cur.execute("""INSERT INTO my_md_notes_notes (オーナーID, 名前, 共有キー, 序列, 作成日時, 更新日時)
                               VALUES (%s, %s, 'private', 0, %s, %s)""", (uid, title, now, now))
                target = cur.lastrowid
                cur.execute("""INSERT INTO my_md_notes_contents (ノートID, 内容, 作成日時, 更新日時)
                               VALUES (%s, %s, %s, %s)""", (target, row['content'], now, now))
                cur.execute("UPDATE sumizumi_notes SET my_md_note_id=%s WHERE user_id=%s AND unit_id=%s",
                            (target, uid, theorem_id))
                action = 'created'
            conn.commit()
    except Exception as e:
        print(f'[sumizumi] マイノートへ書き出せませんでした: {e}')
        return jsonify({'success': False,
                        'error': 'マイノートへ書き出せませんでした．このサイトにマイノート（my_md_notes）が入っているか確かめてください．'}), 500
    try:
        link = url_for('my_md_notes.view_note', note_id=target)
    except Exception:
        link = None
    return jsonify({'success': True, 'action': action, 'note_id': target, 'url': link})
