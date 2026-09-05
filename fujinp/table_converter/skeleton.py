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

"""型（かた） —— データ以外のすべてを骨として取り置き，データを穴として空ける。

セル区分が決まると，シートは二つに割れる。

  骨（ほね）　 データでないセルのすべて。ラベルの文字，合併のかたち，
  　　　　　　 列の幅と行の高さ，空のマス目の配置。**年度が変わっても動かない部分**
  穴（あな）　 データセルの置き場所だけ。中身は入っていない

  シート ＝ 骨 ＋ 穴に入れたデータ

元のエクセルがどれほど変な作りでも，この割りかたなら必ず元に戻せる。
骨は丸ごと取り置き，穴は番地で指すだけだからである。対応式が様式を
どこまで説明できたかとは関わりなく，**復元だけは保証される**。

対応式がやるのは，このあと「穴に入るデータ」をふつうのSQLテーブルの形に
組み替えることであって，復元の土台はここで先に据えておく。
"""

from . import paint as pt
from . import sheet as sh


# ================================================================ 取り出し

def extract(grid, marks):
    """方眼と塗りから，骨を取り出す。"""
    bones, holes = [], []
    for r in range(1, grid.rows + 1):
        for c in range(1, grid.cols + 1):
            if not grid.anchor(r, c):
                continue
            a = pt.addr(r, c)
            rs, cs = grid.span(r, c)
            e = {'a': a, 'r': r, 'c': c}
            if (rs, cs) != (1, 1):
                e['rs'], e['cs'] = rs, cs
            if marks.get(a) == 'data':
                holes.append(e)
            else:
                e['v'] = grid.val(r, c)
                bones.append(e)
    return {'kind': 'tcv_form', 'version': 1, 'sheet': grid.name,
            'rows': grid.rows, 'cols': grid.cols,
            'col_widths': dict((sh.col_letter(c), w)
                               for c, w in sorted(grid.col_widths.items())),
            'row_heights': dict((str(r), h) for r, h in sorted(grid.row_heights.items())),
            'bones': bones, 'holes': holes}


def values(grid, marks):
    """穴に入っていた中身。番地 → 文字。切り詰めない。"""
    out = {}
    for r in range(1, grid.rows + 1):
        for c in range(1, grid.cols + 1):
            if not grid.anchor(r, c):
                continue
            a = pt.addr(r, c)
            if marks.get(a) == 'data':
                out[a] = grid.val(r, c)
    return out


# ================================================================ 復元

def restore(form, vals):
    """骨に中身を入れて，方眼に戻す。"""
    g = sh.Grid(form.get('sheet') or 'Sheet1')
    for e in (form.get('bones') or []):
        g.put(int(e['r']), int(e['c']), e.get('v') or '',
              int(e.get('rs') or 1), int(e.get('cs') or 1))
    for e in (form.get('holes') or []):
        g.put(int(e['r']), int(e['c']), (vals or {}).get(e['a'], ''),
              int(e.get('rs') or 1), int(e.get('cs') or 1))
    for key, w in (form.get('col_widths') or {}).items():
        g.col_widths[sh.col_index(key) if not str(key).isdigit() else int(key)] = float(w)
    for key, h in (form.get('row_heights') or {}).items():
        try:
            g.row_heights[int(key)] = float(h)
        except (TypeError, ValueError):
            pass
    g.rows = max(g.rows, int(form.get('rows') or 0))
    g.cols = max(g.cols, int(form.get('cols') or 0))
    return g


def check(grid, marks):
    """取り出して戻して，元どおりになるかを確かめる。"""
    form = extract(grid, marks)
    vals = values(grid, marks)
    back = restore(form, vals)
    from . import engine as en
    d = en.diff(grid, back)
    return {'sheet': grid.name, 'bones': len(form['bones']), 'holes': len(form['holes']),
            'values': len(vals), 'diff': d, 'ok': not d,
            'form': form, 'vals': vals}


def check_book(grids, paints):
    """本（ブック）まるごと確かめる。"""
    out = []
    for name, g in grids.items():
        marks = paints.get(name) or pt.guess(g)
        r = check(g, marks)
        r.pop('form', None)
        r.pop('vals', None)
        r['painted'] = name in paints
        out.append(r)
    return out


def restore_book(grids, paints, overrides=None):
    """本まるごと復元する。overrides に {シート名: {番地: 文字}} を入れると差し替わる。"""
    overrides = overrides or {}
    out = {}
    for name, g in grids.items():
        marks = paints.get(name) or pt.guess(g)
        form = extract(g, marks)
        vals = dict(values(g, marks))
        vals.update(overrides.get(name) or {})
        out[name] = restore(form, vals)
    return out
