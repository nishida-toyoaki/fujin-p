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

"""テーコンの画面。

流れは三つ。

  ① 見る     絵エクセルを預けて，方眼と行の署名をながめる
  ② 決める   生成AIに依頼文を渡して対応式をこしらえ，プレビューで確かめる
  ③ 通す     分解して SQL テーブルへ入れる／テーブルから絵を組み立てる
"""

import json

from flask import (Response, jsonify, redirect, render_template, request,
                   session, url_for)

import auth
from decorators import login_required

from . import engine as en
from . import paint as pt
from . import skeleton as sk
from . import sheet as sh
from . import spec as sp
from . import store
from . import table_converter_bp

_OPEN = ('table_converter.return_to_fujin',)


@table_converter_bp.before_request
def _gate():
    """deny by default。テーコンは任意のSQLテーブルへ書くので admin だけに開ける。"""
    if request.endpoint in (None, 'table_converter.static'):
        return None
    if not session.get('user_id'):
        if request.path.startswith('/table_converter/api/'):
            return jsonify({'success': False, 'error': 'ログインしてください'}), 401
        return redirect(url_for('auth.login', next=request.path))
    if request.endpoint in _OPEN:
        return None
    if session.get('user_category') != 'admin':
        if request.path.startswith('/table_converter/api/'):
            return jsonify({'success': False, 'error': '管理者だけが使えます'}), 403
        return render_template('table_converter/denied.html'), 403
    return None


@table_converter_bp.route('/return_to_fujin')
@login_required
def return_to_fujin():
    return auth.redirect_to_dashboard()


def _uid():
    return session.get('user_id')


# ================================================================ 一覧

@table_converter_bp.route('/')
def index():
    return render_template('table_converter/index.html',
                           specs=store.list_specs(),
                           samples=store.list_samples(),
                           runs=store.list_runs(limit=12))


# ================================================================ 標本（絵エクセル）

@table_converter_bp.route('/sample/upload', methods=['POST'])
def sample_upload():
    f = request.files.get('file')
    if not f or not f.filename.lower().endswith(('.xlsx', '.xlsm')):
        return redirect(url_for('table_converter.index'))
    try:
        sid = store.save_sample(f.filename, f.read(),
                                (request.form.get('title') or '').strip(), _uid())
    except ValueError:
        return redirect(url_for('table_converter.index'))
    return redirect(url_for('table_converter.sample', sample_id=sid))


@table_converter_bp.route('/sample/<int:sample_id>')
def sample(sample_id):
    """標本の見通し。全シートを一覧にし，塗りと復元の様子を並べる。"""
    s = store.get_sample(sample_id)
    data = store.sample_bytes(sample_id)
    if not s or data is None:
        return redirect(url_for('table_converter.index'))
    try:
        grids = sh.read_xlsx(data)
    except Exception as e:                                   # noqa: BLE001
        return render_template('table_converter/sample.html', s=s, rows=[], names=[],
                               err='読めませんでした: %s' % e, specs=store.list_specs())
    paints = store.paints_of(sample_id)
    rows = []
    for name, g in grids.items():
        marks = dict(pt.guess(g))
        marks.update(paints.get(name) or {})
        st = pt.stats(marks, g)
        chk = sk.check(g, marks)
        rows.append({'name': name, 'rows': g.rows, 'cols': g.cols, 'framed': st['framed'],
                     'sigs': len(set(g.sig(r) for r in range(1, g.rows + 1))),
                     'label': st['label'], 'data': st['data'],
                     'painted': name in paints, 'ok': chk['ok'], 'ndiff': len(chk['diff'])})
    return render_template('table_converter/sample.html', s=s, rows=rows,
                           names=list(grids), err='', specs=store.list_specs())


@table_converter_bp.route('/sample/<int:sample_id>/delete', methods=['POST'])
def sample_delete(sample_id):
    store.delete_sample(sample_id)
    return redirect(url_for('table_converter.index'))


# ================================================================ セル区分（塗り分け）

def _book_of(sample_id):
    """標本を本（ブック）まるごと開く。(標本, {シート名: 方眼}) を返す。"""
    s = store.get_sample(sample_id)
    data = store.sample_bytes(sample_id)
    if not s or data is None:
        return None, {}
    return s, sh.read_xlsx(data)


def _sheet_of(sample_id, want=''):
    """標本の一枚を開く。(標本, 方眼, シート名, すべてのシート名) を返す。"""
    s, grids = _book_of(sample_id)
    if not s:
        return None, None, '', []
    name = want or s.get('sheet') or (list(grids) or [''])[0]
    if name not in grids:
        name = (list(grids) or [''])[0]
    return s, grids.get(name), name, list(grids)


def _bases(grids, paints):
    """いま覚えている塗り。無いシートは機械の見当。"""
    out = {}
    for n, g in grids.items():
        m = pt.guess(g)
        m.update(paints.get(n) or {})
        out[n] = m
    return out


def _paint_page(s, g, name, err='', rep=None, marks=None, r0=1, brief=0, names=None):
    win = 400
    r1 = min(g.rows, r0 + win - 1)
    marks = marks if marks is not None else _marks_now(s['id'], name, g)
    done = set(store.paints_of(s['id']))
    return render_template(
        'table_converter/paint.html', s=s, g=g, sheet=name, err=err, rep=rep,
        names=names or [name], done=done,
        kata=sk.check(g, marks),
        st=pt.stats(marks, g), nsigs=len(set(g.sig(r) for r in range(1, g.rows + 1))),
        ask_text=pt.ask(pt.to_json(g, name, brief=brief)), brief=brief,
        grid_html=pt.to_html(g, marks, r0, r1),
        marks_json=json.dumps(marks, ensure_ascii=False),
        repeat_json=json.dumps(pt.guess_labels(g), ensure_ascii=False),
        win=win, r0=r0, r1=r1,
        pages=list(range(1, g.rows + 1, win)))


def _marks_now(sample_id, name, g):
    """いま覚えている塗り。無ければ機械の見当。"""
    row = store.get_paint(sample_id, name)
    base = pt.guess(g)
    if row and row.get('marks'):
        base.update(row['marks'])
    return base


@table_converter_bp.route('/sample/<int:sample_id>/paint')
def paint(sample_id):
    s, g, name, names = _sheet_of(sample_id, request.args.get('sheet') or '')
    if g is None:
        return redirect(url_for('table_converter.index'))
    return _paint_page(s, g, name, r0=max(1, int(request.args.get('from') or 1)),
                       brief=int(request.args.get('brief') or 0), names=names)


@table_converter_bp.route('/sample/<int:sample_id>/book.json')
def book_json(sample_id):
    """本まるごとの JSON。全シートを一度に生成AIへ渡すときに使う。"""
    s, grids = _book_of(sample_id)
    if not s:
        return redirect(url_for('table_converter.index'))
    book = pt.book_json(grids, brief=int(request.args.get('brief') or 3))
    if request.args.get('ask'):
        return Response(pt.ask_book(book), mimetype='text/plain; charset=utf-8')
    return Response(json.dumps(book, ensure_ascii=False, indent=1),
                    mimetype='application/json; charset=utf-8')


@table_converter_bp.route('/sample/<int:sample_id>/verify')
def verify(sample_id):
    """型（骨＋穴）を取り出して戻し，元どおりになるかを全シートで確かめる。"""
    s, grids = _book_of(sample_id)
    if not s:
        return redirect(url_for('table_converter.index'))
    paints = store.paints_of(sample_id)
    rows = sk.check_book(grids, paints)
    return render_template('table_converter/verify.html', s=s, rows=rows,
                           ok=all(r['ok'] for r in rows), names=list(grids))


@table_converter_bp.route('/sample/<int:sample_id>/form.json')
def form_json(sample_id):
    """一枚ぶんの型。骨とデータ穴を持ち出す。"""
    s, g, name, _ = _sheet_of(sample_id, request.args.get('sheet') or '')
    if g is None:
        return redirect(url_for('table_converter.index'))
    marks = _marks_now(sample_id, name, g)
    body = {'form': sk.extract(g, marks), 'values': sk.values(g, marks)}
    return Response(json.dumps(body, ensure_ascii=False, indent=1),
                    mimetype='application/json; charset=utf-8')


@table_converter_bp.route('/sample/<int:sample_id>/restore.xlsx')
def restore_xlsx(sample_id):
    """型に中身を入れて組み立て直した本を，xlsx で受け取る。"""
    s, grids = _book_of(sample_id)
    if not s:
        return redirect(url_for('table_converter.index'))
    back = sk.restore_book(grids, store.paints_of(sample_id))
    blob = sh.write_xlsx(back, {})
    return Response(blob, mimetype='application/vnd.openxmlformats-officedocument'
                                   '.spreadsheetml.sheet',
                    headers={'Content-Disposition':
                             'attachment; filename="restored_%d.xlsx"' % sample_id})


@table_converter_bp.route('/sample/<int:sample_id>/sheet.json')
def sheet_json(sample_id):
    s, g, name, _ = _sheet_of(sample_id, request.args.get('sheet') or '')
    if g is None:
        return redirect(url_for('table_converter.index'))
    return Response(json.dumps(pt.to_json(g, name, brief=int(request.args.get('brief') or 0)),
                               ensure_ascii=False, indent=1),
                    mimetype='application/json; charset=utf-8')


@table_converter_bp.route('/sample/<int:sample_id>/paint/apply', methods=['POST'])
def paint_apply(sample_id):
    """生成AIが返してきた区分を重ねる。全シートぶんでも，一枚ぶんでも受ける。"""
    s, grids = _book_of(sample_id)
    if not s or not grids:
        return redirect(url_for('table_converter.index'))
    name = request.form.get('sheet') or (list(grids) or [''])[0]
    if name not in grids:
        name = (list(grids) or [''])[0]
    paints = store.paints_of(sample_id)
    bases = _bases(grids, paints)
    try:
        allmarks, rep = pt.apply_book(request.form.get('reply') or '', grids, bases)
    except ValueError as e:
        return _paint_page(s, grids[name], name, err=str(e), names=list(grids))
    touched = set(x['sheet'] for x in rep['sheets'])
    for n in touched:
        store.save_paint(sample_id, n, pt.dump_marks(allmarks[n], grids[n], n), '', _uid())
    store.link_sample(sample_id, s.get('spec_id'), name)
    return _paint_page(s, grids[name], name, rep=rep, marks=allmarks[name],
                       names=list(grids))


@table_converter_bp.route('/sample/<int:sample_id>/paint/save', methods=['POST'])
def paint_save(sample_id):
    name = request.form.get('sheet') or ''
    s, g, name, _ = _sheet_of(sample_id, name)
    if g is None:
        return redirect(url_for('table_converter.index'))
    try:
        marks = json.loads(request.form.get('marks') or '{}')
    except ValueError:
        marks = {}
    marks = dict((a, k) for a, k in marks.items() if k in pt.KINDS)
    store.save_paint(sample_id, name, pt.dump_marks(marks, g, name), '', _uid())
    store.link_sample(sample_id, s.get('spec_id'), name)
    return redirect(url_for('table_converter.paint', sample_id=sample_id, sheet=name,
                            **{'from': request.form.get('from') or 1}))


@table_converter_bp.route('/sample/<int:sample_id>/paint/all', methods=['POST'])
def paint_all(sample_id):
    """まだ塗っていないシートに，機械の見当をそのまま置く（全シートを一気に土台へ）。"""
    s, grids = _book_of(sample_id)
    if not s:
        return redirect(url_for('table_converter.index'))
    done = set(store.paints_of(sample_id))
    for n, g in grids.items():
        if n not in done:
            store.save_paint(sample_id, n, pt.dump_marks(pt.guess(g), g, n), '', _uid())
    return redirect(url_for('table_converter.sample', sample_id=sample_id))


# ================================================================ 対応式

@table_converter_bp.route('/spec/new', methods=['GET', 'POST'])
def spec_new():
    if request.method == 'POST':
        text = request.form.get('spec_json') or ''
        try:
            d = sp.load(text)
        except sp.SpecError as e:
            return render_template('table_converter/spec.html', row=None, text=text,
                                   errs=[str(e)], ddl='', datasets=[], runs=[],
                                   samples=store.list_samples())
        sid = store.save_spec(None, d, (request.form.get('note') or '').strip(),
                              'draft', _uid())
        return redirect(url_for('table_converter.spec_page', spec_id=sid))
    return render_template('table_converter/spec.html', row=None, text=_TEMPLATE_SPEC,
                           errs=[], ddl='', datasets=[], runs=[],
                           samples=store.list_samples())


@table_converter_bp.route('/spec/<int:spec_id>')
def spec_page(spec_id):
    row = store.get_spec(spec_id)
    if not row:
        return redirect(url_for('table_converter.index'))
    d = row['spec']
    errs = sp.check(d)
    return render_template(
        'table_converter/spec.html', row=row, text=row['spec_json'], errs=errs,
        ddl=sp.ddl(d) if not errs else '',
        missing=store.missing_tables(d) if not errs else [],
        datasets=store.datasets(d) if not errs else [],
        runs=store.list_runs(spec_id), samples=store.list_samples())


@table_converter_bp.route('/spec/<int:spec_id>/save', methods=['POST'])
def spec_save(spec_id):
    text = request.form.get('spec_json') or ''
    try:
        d = sp.load(text)
    except sp.SpecError as e:
        row = store.get_spec(spec_id)
        return render_template('table_converter/spec.html', row=row, text=text,
                               errs=[str(e)], ddl='', datasets=[], missing=[],
                               runs=store.list_runs(spec_id), samples=store.list_samples())
    store.save_spec(spec_id, d, (request.form.get('note') or '').strip(),
                    request.form.get('status') or 'draft', _uid())
    return redirect(url_for('table_converter.spec_page', spec_id=spec_id))


@table_converter_bp.route('/spec/<int:spec_id>/delete', methods=['POST'])
def spec_delete(spec_id):
    store.delete_spec(spec_id)
    return redirect(url_for('table_converter.index'))


@table_converter_bp.route('/spec/<int:spec_id>/create_tables', methods=['POST'])
def spec_create_tables(spec_id):
    row = store.get_spec(spec_id)
    if row:
        try:
            store.create_tables(row['spec'])
        except Exception:                                    # noqa: BLE001
            pass
    return redirect(url_for('table_converter.spec_page', spec_id=spec_id))


# ================================================================ プレビュー（読むだけ）

@table_converter_bp.route('/tryout')
def tryout():
    """標本を対応式にかけてみる。SQL には何も書かない。"""
    spec_id = int(request.args.get('spec') or 0)
    sample_id = int(request.args.get('sample') or 0)
    row = store.get_spec(spec_id)
    data = store.sample_bytes(sample_id) if sample_id else None
    if not row or data is None:
        return redirect(url_for('table_converter.index'))
    d = row['spec']
    name = request.args.get('sheet') or d.get('sheet') or ''
    grids = sh.read_xlsx(data)
    g = grids.get(name) or (list(grids.values()) or [None])[0]
    if g is None:
        return redirect(url_for('table_converter.index'))
    out = en.decompose(g, d)
    back = en.compose(d, out['data'], out['residue'])
    df = en.diff(g, back)
    r0 = max(1, int(request.args.get('from') or 1))
    return render_template(
        'table_converter/tryout.html', row=row, sample_id=sample_id, sheet=name,
        rep=out['report'], data=out['data'], residue=out['residue'], diff=df,
        tables=dict(sp.tables_of(d)), r0=r0,
        grid_html=sh.to_html(g, r0, r0 + 59, out['marks']),
        datasets=store.datasets(d), missing=store.missing_tables(d))


# ================================================================ 分解（絵 → 表）

@table_converter_bp.route('/spec/<int:spec_id>/decompose', methods=['POST'])
def decompose(spec_id):
    row = store.get_spec(spec_id)
    sample_id = int(request.form.get('sample') or 0)
    dataset = (request.form.get('dataset') or '').strip()
    data = store.sample_bytes(sample_id) if sample_id else None
    if not row or data is None:
        return redirect(url_for('table_converter.spec_page', spec_id=spec_id))
    d = row['spec']
    miss = store.missing_tables(d)
    if miss:
        return render_template('table_converter/done.html', row=row, ok=False,
                               msg='テーブルがまだありません: %s' % '，'.join(miss),
                               summary={})
    name = request.form.get('sheet') or d.get('sheet') or ''
    grids = sh.read_xlsx(data)
    g = grids.get(name) or (list(grids.values()) or [None])[0]
    out = en.decompose(g, d)
    counts = store.write_data(d, dataset, out['data'])
    nres = store.write_residue(spec_id, dataset, out['residue'])
    summary = dict(out['report'])
    summary['tables'] = counts
    summary['residue'] = nres
    store.log_run(spec_id, dataset, 'decompose', summary, _uid())
    return render_template('table_converter/done.html', row=row, ok=True,
                           msg='分解して SQL テーブルへ入れました（版「%s」）' % dataset,
                           summary=summary, dataset=dataset)


# ================================================================ 生成（表 → 絵）

@table_converter_bp.route('/spec/<int:spec_id>/compose')
def compose(spec_id):
    row = store.get_spec(spec_id)
    if not row:
        return redirect(url_for('table_converter.index'))
    d = row['spec']
    dataset = (request.args.get('dataset') or '').strip()
    data = store.read_data(d, dataset)
    residue = store.read_residue(spec_id, dataset)
    g = en.compose(d, data, residue)
    r0 = max(1, int(request.args.get('from') or 1))
    return render_template('table_converter/compose.html', row=row, dataset=dataset,
                           g=g, grid_html=sh.to_html(g, r0, r0 + 59), r0=r0,
                           counts=dict((a, len(data.get(a) or []))
                                       for a, _ in sp.tables_of(d)),
                           residue=len(residue))


@table_converter_bp.route('/spec/<int:spec_id>/download')
def download(spec_id):
    row = store.get_spec(spec_id)
    if not row:
        return redirect(url_for('table_converter.index'))
    d = row['spec']
    dataset = (request.args.get('dataset') or '').strip()
    g = en.compose(d, store.read_data(d, dataset), store.read_residue(spec_id, dataset))
    blob = sh.write_xlsx({(d.get('sheet') or 'Sheet1'): g}, d.get('styles') or {})
    store.log_run(spec_id, dataset, 'compose', {'rows': g.rows, 'cols': g.cols}, _uid())
    fn = '%s_%s.xlsx' % (d.get('name') or 'sheet', dataset or 'all')
    return Response(blob, mimetype='application/vnd.openxmlformats-officedocument'
                                   '.spreadsheetml.sheet',
                    headers={'Content-Disposition': 'attachment; filename="%s"' % fn})


# ================================================================ 対応式のひな型

_TEMPLATE_SPEC = json.dumps({
    "spec_version": 1,
    "name": "atarashii_youshiki",
    "title": "新しい様式",
    "sheet": "Sheet1",
    "grid": {"cols": 8, "col_widths": {"A": 8.0}, "row_height": 15},
    "dataset_col": "dataset",
    "tables": {
        "gyo": {"table": "xx_gyo", "seq": "row_no",
                "columns": {"row_no": "INT", "namae": "VARCHAR(200)", "atai": "TEXT"}}
    },
    "styles": {"lbl": {"bold": True, "fill": "F2F2F2"}},
    "bands": [
        {"id": "midashi", "kind": "once",
         "rows": [{"slots": [{"c": 1, "cs": 2, "v": {"lit": "見出し"}, "style": "lbl"}]}]},
        {"id": "gyo", "kind": "repeat", "use": "gyo",
         "rows": [{"slots": [{"c": 1, "v": {"col": "namae"}},
                             {"c": 2, "v": {"col": "atai"}}]}]}
    ]
}, ensure_ascii=False, indent=2)
