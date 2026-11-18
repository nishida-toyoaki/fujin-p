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

"""こんか — 部品の身元（ID と URL）と，部品どうしの構成関係。

すべての量子（作品・複合部品・倉庫部品・箇条）に，同じ形の URL を与える。

  /cqm/q/<id>              身元ページ（人が読む）
  /cqm/k/<key_path>        key_path から身元ページへ
  /cqm/api/q/<id>          身元（JSON）：何を含み，何に含まれ，どの作品に効くか
  /cqm/api/work/<id>/graph 作品から辿れる構成のグラフ（JSON）

構成関係は「一般的な型」ではなく，個々の部品どうしの具体的な結びつきとして返す。
たとえば「年度計画 30 の業務の実績は，箇条 #a，#b，#c がこの順に並んだもの」が
そのまま辺として出てくる。倉庫の形には手を加えない（読むだけ）。
"""

import re

from flask import abort, jsonify, redirect, render_template, request, url_for

from . import cqm_bp, store
from .routes import editor_required, me
from decorators import login_required


# ------------------------------------------------------------------ 分類

LEVEL_LABEL = {'work': '作品', 'part': '複合部品', 'box': '倉庫部品', 'item': '箇条'}


def level_of(q):
    """作品／複合部品／倉庫部品／箇条 のどれか。"""
    if not q:
        return None
    if q.get('kind') == 'item':
        return 'item'
    if q.get('recipe') == 'arena':
        lv = store._loads(q.get('attrs')).get('level')
        return 'work' if lv == store.WORK else 'part'
    return 'box'


def q_url(qid, external=False):
    return url_for('cqm.q_page', qid=int(qid), _external=external)


def _brief(q, **extra):
    lv = level_of(q)
    r = {'id': q['id'], 'level': lv, 'level_label': LEVEL_LABEL.get(lv, ''),
         'title': q.get('title') or '', 'key_path': q.get('key_path') or '',
         'recipe': q.get('recipe') or '', 'url': q_url(q['id'])}
    if lv == 'item':
        body = re.sub(r'\s+', ' ', q.get('body') or '')
        r['snippet'] = body[:60]
    r.update(extra)
    return r


# ------------------------------------------------------------------ 構成の辺

def _walk_layout(node, path, out):
    """レイアウトの木を歩き，指し先のある枠を (道すじ, 枠) で集める。
    道すじは根からの番号の列（'0.2.1'）で，横並び・縦並びの別も添える。"""
    if not isinstance(node, dict):
        return out
    if node.get('t') == 'text':
        out.append((path, node))
        return out
    for i, c in enumerate(node.get('items') or []):
        _walk_layout(c, path + [(node.get('t') or '', i)], out)
    return out


def _path_str(path):
    return '.'.join(str(i) for _, i in path) or '-'


def _shape_str(path):
    # 例：col0>row2>col1 （どの並びの何番目に置かれているか）
    return '>'.join('%s%d' % (t, i) for t, i in path) or '-'


def parts_of(q):
    """q を直接構成するもの（子）を，置かれ方つきで返す。"""
    lv = level_of(q)
    out = []
    if lv in ('work', 'part'):
        lay = store._layout(q)
        for path, nd in _walk_layout(lay.get('root'), [], []):
            ref = nd.get('ref')
            t = store.resolve(ref)
            size = nd.get('size') or {}
            edge = {'relation': 'places', 'slot': _path_str(path), 'shape': _shape_str(path),
                    'ref': ref, 'cells': {k: size.get(k) for k in ('w', 'h') if size.get(k)}}
            if t:
                out.append(_brief(t, **edge))
            else:
                out.append(dict(edge, id=None, level=None, title='（指し先なし）', url=None))
    elif lv == 'box':
        for k in store.children(q['id']):
            if k.get('kind') == 'item':
                out.append(_brief(k, relation='lists', ord=k.get('link_ord') or 0))
    return out


def _reverse_index():
    """指し先 → それを置いている複合部品・作品（1回の走査でまとめて作る）。"""
    idx = {}
    for a in store.D()['quanta'].values():
        if a.get('recipe') != 'arena':
            continue
        for path, nd in _walk_layout(store._layout(a).get('root'), [], []):
            t = store.resolve(nd.get('ref'))
            if t:
                idx.setdefault(t['id'], []).append((a['id'], _path_str(path)))
    for ln in store.D()['links']:
        idx.setdefault(ln['child'], []).append((ln['parent'], 'ord%s' % ln.get('ord', 0)))
    return idx


def used_by(q, idx=None):
    """q を直接使っているもの（親）。"""
    idx = idx if idx is not None else _reverse_index()
    out, seen = [], set()
    for pid, slot in idx.get(q['id'], []):
        p = store.get_quantum(pid)
        if p and (pid, slot) not in seen:
            seen.add((pid, slot))
            out.append(_brief(p, slot=slot))
    return out


def works_of(q, idx=None, limit_depth=12):
    """q が最終的に効いている作品（親を上へ辿る）。"""
    idx = idx if idx is not None else _reverse_index()
    works, seen, frontier = {}, {q['id']}, [q['id']]
    for _ in range(limit_depth):
        nxt = []
        for cid in frontier:
            for pid, _slot in idx.get(cid, []):
                if pid in seen:
                    continue
                seen.add(pid)
                p = store.get_quantum(pid)
                if not p:
                    continue
                if level_of(p) == 'work':
                    works[pid] = _brief(p)
                nxt.append(pid)
        frontier = nxt
        if not frontier:
            break
    return list(works.values())


def identity(q, idx=None):
    idx = idx if idx is not None else _reverse_index()
    r = _brief(q)
    r.update(updated_at=q.get('updated_at') or '', created_at=q.get('created_at') or '',
             parts=parts_of(q), used_by=used_by(q, idx), works=works_of(q, idx))
    if r['level'] in ('box', 'item'):
        r['body'] = q.get('body') or ''
    return r


def work_graph(arena_id):
    """作品から辿れる部品すべてを節点に，構成関係を辺にしたグラフ。"""
    root = store.get_quantum(arena_id)
    if not root or level_of(root) not in ('work', 'part'):
        return None
    nodes, edges, seen, stack = {}, [], set(), [(root, 0)]
    while stack:
        q, depth = stack.pop()
        if q['id'] in seen or depth > 12:
            continue
        seen.add(q['id'])
        nodes[q['id']] = _brief(q)
        for p in parts_of(q):
            if not p.get('id'):
                edges.append({'from': q['id'], 'to': None, 'relation': p['relation'],
                              'slot': p.get('slot'), 'ref': p.get('ref')})
                continue
            e = {'from': q['id'], 'to': p['id'], 'relation': p['relation']}
            for k in ('slot', 'shape', 'ord', 'cells'):
                if p.get(k) not in (None, {}, ''):
                    e[k] = p[k]
            edges.append(e)
            child = store.get_quantum(p['id'])
            if child:
                stack.append((child, depth + 1))
    counts = {}
    for n in nodes.values():
        counts[n['level']] = counts.get(n['level'], 0) + 1
    return {'root': _brief(root), 'counts': counts,
            'nodes': sorted(nodes.values(), key=lambda n: n['id']), 'edges': edges}


# ------------------------------------------------------------------ 経路

@cqm_bp.route('/q/<int:qid>')
@login_required
@editor_required
def q_page(qid):
    q = store.get_quantum(qid)
    if not q:
        abort(404)
    return render_template('cqm/identity.html', q=identity(q))


@cqm_bp.route('/k/<path:key_path>')
@login_required
@editor_required
def k_page(key_path):
    q = store.get_by_key(key_path)
    if not q:
        abort(404)
    return redirect(q_url(q['id']))


@cqm_bp.route('/api/q/<int:qid>')
@login_required
@editor_required
def q_api(qid):
    q = store.get_quantum(qid)
    if not q:
        return jsonify({'status': 'ng', 'note': '部品がありません'}), 404
    return jsonify({'status': 'ok', 'value': identity(q)})


@cqm_bp.route('/api/work/<int:arena_id>/graph')
@login_required
@editor_required
def work_graph_api(arena_id):
    g = work_graph(arena_id)
    if g is None:
        return jsonify({'status': 'ng', 'note': '作品または複合部品がありません'}), 404
    return jsonify({'status': 'ok', 'value': g})
