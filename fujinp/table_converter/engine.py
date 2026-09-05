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

"""分解と生成 —— 対応式にしたがって，絵と表のあいだを往復する。

分解は方眼を上から読み，帯に合った行は表の行を生み，合わなかったところは
残余に落とす。生成は帯を上から展開し，最後に残余を重ねる。

ここは SQL を知らない。入り口も出口も素の辞書とリストで，
台帳への出し入れは store.py が受け持つ。
"""

from . import sheet as sh
from . import spec as sp


# ================================================================ 分解（絵 → 表）

class _Cursor(object):
    def __init__(self, grid, spec):
        self.g = grid
        self.spec = spec
        self.data = dict((a, []) for a in (spec.get('tables') or {}))
        self.seq = {}              # 別名ごとの通し番号（読んだ順）
        self.bandn = {}            # 帯ごとの回数（残余の宛先に使う）
        self.residue = []
        self.used = set()          # 帯に取られたセル
        self.marks = {}            # (r,c) -> 画面の色分け
        self.report = {'rows_matched': 0, 'rows_residue': 0, 'instances': {},
                       'list_unparsed': 0, 'cells_total': 0, 'cells_used': 0}


def decompose(grid, spec):
    """方眼を分解して {表, 残余, 報告} を返す。"""
    cur = _Cursor(grid, spec)
    r = 1
    last = ('', 0, 0)             # 直前に閉じた上位の帯（id, 連番, 終わった行）
    while r <= grid.rows:
        moved = None
        for band in spec['bands']:
            n = _run(cur, band, r, None, top=True)
            if n and n > r:
                moved = n
                last = (band.get('id') or '', cur.bandn.get(band.get('id'), 0), n)
                break
        if moved:
            cur.report['rows_matched'] += moved - r
            r = moved
            continue
        _residue_row(cur, r, last)
        cur.report['rows_residue'] += 1
        r += 1
    for rr in range(1, grid.rows + 1):
        for cc in range(1, grid.cols + 1):
            if grid.anchor(rr, cc):
                cur.report['cells_total'] += 1
                if (rr, cc) in cur.used:
                    cur.report['cells_used'] += 1
    return {'data': cur.data, 'residue': cur.residue,
            'report': cur.report, 'marks': cur.marks}


def _run(cur, band, r, parent, top=False):
    """帯を r 行目から動かす。進んだ先の行を返す（合わなければ None）。"""
    rows = band.get('rows') or []
    kind = band.get('kind') or 'once'
    alias = band.get('use')
    made = 0
    while r <= cur.g.rows:
        got = _match_rows(cur, rows, r, dry=True)
        if got is None:
            break
        got = _match_rows(cur, rows, r, dry=False)
        ordn = cur.bandn.get(band.get('id'), 0) + 1
        cur.bandn[band.get('id')] = ordn
        cur.report['instances'][band.get('id')] = ordn
        row = None
        if alias:
            row = _make_row(cur, band, alias, got, parent, ordn)
        start = r
        rr = r + len(rows)
        # 残った枠（帯の中で誰も取らなかったセル）
        _residue_inside(cur, band, ordn, start, rr)
        # 子の帯
        while True:
            adv = False
            for kid in (band.get('bands') or []):
                n = _run(cur, kid, rr, row or parent)
                if n and n > rr:
                    rr = n
                    adv = True
                    break
            if not adv:
                break
        # しめくくり
        for t in (band.get('tail') or []):
            if _match_rows(cur, [t], rr, dry=True) is not None:
                _match_rows(cur, [t], rr, dry=False)
                rr += 1
            elif not t.get('opt'):
                break
        gap = band.get('gap')
        if gap:
            n = 0
            while n < int(gap.get('max') or 1) and rr <= cur.g.rows and cur.g.is_blank(rr):
                n += 1
                rr += 1
            if row is not None and gap.get('col'):
                row[gap['col']] = n
        _lists(cur, band, row, got)
        r = rr
        made += 1
        if kind == 'once':
            break
    return r if made else None


def _match_rows(cur, rows, r, dry=True):
    """行の並びが方眼に合うか。合えば {列: 値} を返す。"""
    g = cur.g
    out = {}
    for i, spec_row in enumerate(rows):
        rr = r + i
        if rr > g.rows:
            if spec_row.get('opt'):
                continue
            return None
        if spec_row.get('blank') or spec_row.get('covered'):
            if not g.is_blank(rr):
                return None
            continue
        if spec_row.get('sig') and g.sig(rr) != spec_row['sig']:
            return None
        if (spec_row.get('slots') or []) and g.is_blank(rr):
            return None                     # 空の行を帯と見なさない（際限なく合ってしまう）
        for s in (spec_row.get('slots') or []):
            c = int(s['c'])
            rs, cs = int(s.get('rs') or 1), int(s.get('cs') or 1)
            v = s.get('v') or {}
            has = g.anchor(rr, c)
            if not has:
                # 決まり文句と req の枠は必ず要る。データの枠は，無ければ空とみなす
                if 'lit' in v or s.get('req'):
                    if s.get('opt'):
                        continue
                    return None
                if s.get('opt'):
                    continue
                if 'col' in v:
                    out[v['col']] = ''
                if not dry:
                    cur.marks[(rr, c)] = 'hit'
                continue
            if not s.get('loose') and g.span(rr, c) != (rs, cs):
                if s.get('opt'):
                    continue
                return None
            raw = g.val(rr, c)
            if 'lit' in v:
                if sh.norm(raw) != sh.norm(v['lit']):
                    return None
            elif 'col' in v:
                out[v['col']] = raw.strip()
            elif 'fmt' in v:
                got = sp.fmt_parse(v['fmt'], raw)
                if got is None:
                    if s.get('opt'):
                        continue
                    return None
                out.update(got)
            elif 'list' in v:
                out.setdefault('__lists', {})[id(s)] = (v['list'], raw)
            if not dry:
                cur.used.add((rr, c))
                cur.marks[(rr, c)] = 'hit' if 'lit' not in v else 'lit'
    return out


def _make_row(cur, band, alias, got, parent, ordn):
    t = cur.spec['tables'][alias]
    cols = t['columns']
    n = cur.seq.get(alias, 0) + 1
    cur.seq[alias] = n
    row = dict((c, None) for c in cols)
    for k, v in got.items():
        if k in cols:
            row[k] = v
    for k, ref in (t.get('inherit') or {}).items():
        pa, _, pc = str(ref).partition('.')
        if parent and pc in parent:
            row[k] = parent[pc]
    for k, v in (band.get('when') or {}).items():   # 帯そのものが書き込む定数（様式の形）
        if k in cols:
            row[k] = v
    if t.get('seq') and row.get(t['seq']) in (None, ''):
        row[t['seq']] = n
    row['_ord'] = n
    cur.data.setdefault(alias, []).append(row)
    return row


def _lists(cur, band, row, got):
    """一つのセルに詰まっていたものを，子の表へほぐす。"""
    for key, (L, raw) in (got.get('__lists') or {}).items():
        alias = L['table']
        t = cur.spec['tables'][alias]
        cols = t['columns']
        sep = L.get('sep') or '\n'
        n = 0
        for line in [x for x in str(raw or '').split(sep) if x.strip()]:
            n += 1
            item = dict((c, None) for c in cols)
            parsed = sp.fmt_parse(L.get('fmt') or '{__all__}', line) if L.get('fmt') else None
            if parsed is None:
                cur.report['list_unparsed'] += 1
                target = L.get('raw') or (sp.RE_FIELD.findall(L.get('fmt') or '') or [None])[0]
                if target and target in cols:
                    item[target] = line.strip()
            else:
                for k, v in parsed.items():
                    if k in cols:
                        item[k] = v
            for k, v in (L.get('match') or {}).items():
                if k in cols:
                    item[k] = v
            for k, ref in (t.get('inherit') or {}).items():
                _, _, pc = str(ref).partition('.')
                if row and pc in row:
                    item[k] = row[pc]
            item['_ord'] = n
            cur.data.setdefault(alias, []).append(item)


def _residue_row(cur, r, last):
    g = cur.g
    for c in range(1, g.cols + 1):
        if not g.anchor(r, c) or (r, c) in cur.used:
            continue
        rs, cs = g.span(r, c)
        cur.residue.append({'mode': 'out', 'band': last[0], 'ord': last[1],
                            'dr': r - last[2], 'r': r, 'c': c, 'rs': rs, 'cs': cs,
                            'v': g.val(r, c)})
        cur.marks[(r, c)] = 'res'


def _residue_inside(cur, band, ordn, start, end):
    g = cur.g
    for rr in range(start, min(end, g.rows + 1)):
        for c in range(1, g.cols + 1):
            if not g.anchor(rr, c) or (rr, c) in cur.used:
                continue
            rs, cs = g.span(rr, c)
            cur.residue.append({'mode': 'in', 'band': band.get('id') or '', 'ord': ordn,
                                'dr': rr - start, 'r': rr, 'c': c, 'rs': rs, 'cs': cs,
                                'v': g.val(rr, c)})
            cur.marks[(rr, c)] = 'res'
            cur.used.add((rr, c))


# ================================================================ 生成（表 → 絵）

def compose(spec, data, residue=None):
    """表から絵を組み立てる。上位の帯は，対応式に書いた順に並ぶ。"""
    g = sh.Grid(spec.get('sheet') or 'Sheet1')
    grid = spec.get('grid') or {}
    for key, w in (grid.get('col_widths') or {}).items():
        g.col_widths[int(key) if str(key).isdigit() else sh.col_index(key)] = float(w)
    res_in, res_out, res_top = _sort_residue(residue or [])
    ctx = {'in': res_in, 'out': res_out, 'n': {}}
    r = 1
    for e in res_top:
        _put_res(g, int(e['dr']), e)
        r = max(r, int(e['dr']) + 1)
    for band in spec['bands']:
        for row in _rows_of(data, spec, band, None):
            r = _emit_one(g, spec, data, band, row, r, ctx)
            if (band.get('kind') or 'once') == 'once':
                break
    if grid.get('row_height'):
        for rr in range(1, g.rows + 1):
            g.row_heights.setdefault(rr, float(grid['row_height']))
    return g


def _sort_residue(residue):
    inside, outside, top = {}, {}, []
    for e in residue:
        key = (e.get('band') or '', int(e.get('ord') or 0))
        if e.get('mode') == 'in':
            inside.setdefault(key, []).append(e)
        elif e.get('band'):
            outside.setdefault(key, []).append(e)
        else:
            top.append(e)
    return inside, outside, sorted(top, key=lambda x: (int(x['dr']), int(x['c'])))


def _rows_of(data, spec, band, parent):
    """この帯が生む行。once の帯は [None] を返す。"""
    alias = band.get('use')
    if not alias:
        return [None]
    t = spec['tables'][alias]
    rows = list(data.get(alias) or [])
    for k, v in (band.get('when') or {}).items():
        rows = [x for x in rows if str(x.get(k) or '') == str(v)]
    inh = t.get('inherit') or {}
    if parent is not None and inh:
        def fits(x):
            for k, ref in inh.items():
                _, _, pc = str(ref).partition('.')
                if pc in parent and str(x.get(k)) != str(parent.get(pc)):
                    return False
            return True
        rows = [x for x in rows if fits(x)]
    return sorted(rows, key=lambda x: (int(x.get('_ord') or 0), int(x.get('id') or 0)))


def _emit_one(g, spec, data, band, row, r, ctx):
    """帯を一回ぶん置く。置いた先の行を返す。"""
    bid = band.get('id') or ''
    ordn = ctx['n'].get(bid, 0) + 1
    ctx['n'][bid] = ordn
    start = r
    for i, spec_row in enumerate(band.get('rows') or []):
        _emit_row(g, spec, data, spec_row, row, r + i)
    r += len(band.get('rows') or [])
    for e in ctx['in'].get((bid, ordn), []):
        _put_res(g, start + int(e['dr']), e)
        r = max(r, start + int(e['dr']) + 1)
    # 子の帯は，読んだ順（_ord）に混ぜて並べる
    items = []
    for kid in (band.get('bands') or []):
        for krow in _rows_of(data, spec, kid, row):
            items.append((int(krow.get('_ord') or 0) if krow else 0, kid, krow))
    items.sort(key=lambda x: x[0])
    for _, kid, krow in items:
        r = _emit_one(g, spec, data, kid, krow, r, ctx)
    for t in (band.get('tail') or []):
        _emit_row(g, spec, data, t, row, r)
        r += 1
    gap = band.get('gap')
    if gap and row is not None and gap.get('col'):
        try:
            r += max(0, min(int(gap.get('max') or 1), int(row.get(gap['col']) or 0)))
        except (TypeError, ValueError):
            pass
    end = r
    for e in ctx['out'].get((bid, ordn), []):
        _put_res(g, end + int(e['dr']), e)
        r = max(r, end + int(e['dr']) + 1)
    return r


def _emit_row(g, spec, data, spec_row, row, r):
    if spec_row.get('blank') or spec_row.get('covered'):
        return
    for s in (spec_row.get('slots') or []):
        v = s.get('v') or {}
        text = ''
        if 'lit' in v:
            text = v['lit']
        elif 'col' in v:
            text = '' if row is None else (row.get(v['col']) or '')
        elif 'fmt' in v:
            text = sp.fmt_build(v['fmt'], row or {})
        elif 'list' in v:
            text = _list_text(spec, data, v['list'], row)
        elif 'skip' in v:
            continue
        if text == '' and s.get('opt'):
            continue
        g.put(r, int(s['c']), text, int(s.get('rs') or 1), int(s.get('cs') or 1),
              s.get('style'))


def _list_text(spec, data, L, row):
    alias = L['table']
    t = spec['tables'][alias]
    rows = list(data.get(alias) or [])
    for k, val in (L.get('match') or {}).items():
        rows = [x for x in rows if str(x.get(k) or '') == str(val)]
    for k, ref in (t.get('inherit') or {}).items():
        _, _, pc = str(ref).partition('.')
        if row and pc in row:
            rows = [x for x in rows if str(x.get(k)) == str(row.get(pc))]
    order = L.get('order') or ['_ord']
    rows.sort(key=lambda x: tuple(str(x.get(k) or '') for k in order))
    fmt = L.get('fmt') or ''
    return (L.get('sep') or '\n').join(sp.fmt_build(fmt, x) for x in rows)


def _put_res(g, r, e):
    g.put(r, int(e['c']), e.get('v') or '', int(e.get('rs') or 1), int(e.get('cs') or 1))


# ================================================================ 突き合わせ

def diff(a, b, limit=200):
    """二つの方眼を比べる。往復が元に戻ったかを見るのに使う。"""
    out = []
    cells = set(a.values.keys()) | set(b.values.keys())
    for (r, c) in sorted(cells):
        va, vb = sh.norm(a.val(r, c)), sh.norm(b.val(r, c))
        sa, sb = a.span(r, c), b.span(r, c)
        if va == vb and sa == sb:
            continue
        out.append({'r': r, 'c': c, 'a': va[:120], 'b': vb[:120],
                    'span_a': '%dx%d' % sa, 'span_b': '%dx%d' % sb})
        if len(out) >= limit:
            break
    return out
