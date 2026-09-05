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

"""セル区分 —— 絵テーブルの一つ一つのセルを，空白・ラベル・データに分ける。

対応式をこしらえる前に，まずここを済ませる。どのセルが様式の側の決まり文句で，
どのセルが中身なのかが決まれば，帯の輪郭はおのずと見えてくる。

  空白（blank）  何も無いところ。様式の余白
  ラベル（label） 様式の側の決まり文句。年度が変わっても動かない
  データ（data）  中身。年度や案件ごとに変わる

分けかたは三つの道で決まる。**見当**（機械の当てずっぽう）を土台に，
**生成AIの区分**を重ね，最後に**人の手**で塗り直す。あとの手が前の手に勝つ。
"""

import json
import re

from . import sheet as sh

KINDS = ('blank', 'label', 'data')
BRUSH_JA = {'blank': '空白', 'label': 'ラベル', 'data': 'データ'}
_ALIAS = {'空白': 'blank', 'ラベル': 'label', 'データ': 'data',
          'b': 'blank', 'l': 'label', 'd': 'data',
          '空': 'blank', '見出し': 'label', '値': 'data'}
RE_ADDR = re.compile(r'^([A-Za-z]+)(\d+)$')
RE_RANGE = re.compile(r'^([A-Za-z]+\d+)\s*:\s*([A-Za-z]+\d+)$')


def addr(r, c):
    return '%s%d' % (sh.col_letter(c), r)


def parse_addr(a):
    m = RE_ADDR.match(str(a or '').strip())
    if not m:
        return None
    return int(m.group(2)), sh.col_index(m.group(1))


def kind_of(v):
    v = str(v or '').strip().lower()
    if v in KINDS:
        return v
    return _ALIAS.get(str(v).strip())


# ================================================================ 見当

def guess(grid):
    """機械の当てずっぽう。中身があればデータ，無ければ空白。"""
    m = {}
    for r in range(1, grid.rows + 1):
        for c in range(1, grid.cols + 1):
            if (r, c) in grid.covered:
                continue
            m[addr(r, c)] = 'data' if grid.val(r, c).strip() else 'blank'
    return m


def guess_labels(grid, least=3):
    """同じ文字が何度も出てくるセルは，様式の決まり文句である見込みが高い。"""
    from collections import Counter
    cnt = Counter()
    for r in range(1, grid.rows + 1):
        for c in range(1, grid.cols + 1):
            t = sh.norm(grid.val(r, c))
            if t and len(t) <= 40:
                cnt[t] += 1
    out = {}
    for r in range(1, grid.rows + 1):
        for c in range(1, grid.cols + 1):
            t = sh.norm(grid.val(r, c))
            if t and cnt[t] >= least:
                out[addr(r, c)] = 'label'
    return out


# ================================================================ 絵テーブル → JSON

def to_json(grid, sheet_name, maxlen=60, sig_rows=12, brief=0):
    """絵テーブルを JSON にする。生成AIに渡すのはこれ。

    brief に本数を入れると，署名ごとに先頭の何行かだけを載せる。
    大きな帳票をそのまま渡すと依頼文が長くなりすぎるときに使う。
    区分は署名ごとに決まるので，見本が数行あれば足りることが多い。
    """
    from collections import OrderedDict
    sigs = OrderedDict()
    for r in range(1, grid.rows + 1):
        sigs.setdefault(grid.sig(r), []).append(r)
    keep = None
    if brief:
        keep = set()
        for rs_ in sigs.values():
            keep.update(rs_[:int(brief)])
    cells = []
    for r in range(1, grid.rows + 1):
        if keep is not None and r not in keep:
            continue
        for c in range(1, grid.cols + 1):
            if not grid.anchor(r, c):
                continue
            rs, cs = grid.span(r, c)
            v = sh.norm(grid.val(r, c))
            e = {'a': addr(r, c), 'r': r, 'c': c, 'v': v[:maxlen] + ('…' if len(v) > maxlen else '')}
            if (rs, cs) != (1, 1):
                e['rs'], e['cs'] = rs, cs
            cells.append(e)
    return {
        'kind': 'tcv_sheet', 'version': 1, 'sheet': sheet_name,
        'rows': grid.rows, 'cols': grid.cols,
        'note': ('中身は %d 字で切ってある。枠の無いところ（空のマス目）は cells に載せていない。' % maxlen)
                + ('署名ごとに先頭 %d 行ぶんの cells だけを載せてある（同じ署名の行は同じ'
                   '区分になるとみて，by_sig で答えてほしい）。' % brief if brief else ''),
        'col_widths': dict((sh.col_letter(c), w) for c, w in sorted(grid.col_widths.items())),
        'signatures': [{'sig': s, 'count': len(rs_), 'rows': rs_[:sig_rows]}
                       for s, rs_ in sigs.items()],
        'cells': cells,
    }


def book_json(grids, maxlen=60, brief=3):
    """本（ブック）まるごとを JSON にする。全シートを一度に渡すときはこれ。"""
    return {'kind': 'tcv_book', 'version': 1, 'sheet_count': len(grids),
            'sheets': [to_json(g, name, maxlen=maxlen, brief=brief)
                       for name, g in grids.items()]}


# ================================================================ 生成AIへの依頼文

ASK = '''\
添付（または下）の絵テーブルについて，一つ一つのセルを次の三つに分けてください。

  blank … 何も無いところ。様式の余白
  label … 様式の側の決まり文句。年度や案件が変わっても動かない文字
          （欄の見出し，「中期計画番号」「合計」のような決まった語，注記の定型文）
  data  … 中身。年度や案件ごとに変わる文字（本文，数値，氏名，日付，番号）

見分けかたの目安。

  ・同じ文字がシートの中で何度も出てくるなら label の見込みが高い
  ・行の署名（合併のかたち）が同じ行どうしは，たいてい同じ区分の並びになる
  ・番号や連番は，欄の名前ではなく中身なので data
  ・「※…とする」のような注記は，毎年そのまま残るなら label，書き換わるなら data
  ・迷ったら data にしてください（人があとで塗り直します）

## 答えかた

次の形の JSON だけを出してください。前後に説明は要りません。

{
  "kind": "tcv_marks",
  "version": 1,
  "sheet": "シート名",
  "by_sig": [
    {"sig": "B,C,D,E,F,G", "cols": {"B": "data", "C": "data", "D": "data",
                                     "E": "data", "F": "data", "G": "data"}}
  ],
  "ranges": [ {"range": "B9:G10", "kind": "label"} ],
  "marks": { "B1": "label", "F4": "label" },
  "comment": "迷ったところがあれば，ここに短く"
}

- `by_sig` は，その署名を持つ行すべてに，列ごとの区分をあてはめます。**まずこれで大づかみに**。
- `ranges` は，番地の範囲にまとめてあてはめます。
- `marks` は，一つずつ指すときに使います。
- あとに書いたものが前に勝ちます（by_sig → ranges → marks の順にあてはめます）。
- 触れなかったセルは，機械の見当（中身があれば data，無ければ blank）のままになります。

## 絵テーブル

'''


BOOK_TAIL = """

## 全シートまとめて答えるとき

この本には何枚かのシートが入っています。シートごとに分けて，次の形で答えてください。

{
  "kind": "tcv_marks",
  "version": 1,
  "sheets": {
    "シート名A": {"by_sig": [...], "ranges": [...], "marks": {...}},
    "シート名B": {"by_sig": [...], "ranges": [...], "marks": {...}}
  },
  "comment": "迷ったところ"
}

大事なのは data の見極めです。label と blank はまとめて「型」として取り置かれ，
data だけが表に組み替えられます。**data を取りこぼさないこと**を第一に，
迷ったら data にしてください。逆に，年度が変わっても動かない文字を data にすると，
型が痩せて，あとで様式を組み立て直せなくなります。
"""


def ask(sheet_json):
    return ASK + '```json\n' + json.dumps(sheet_json, ensure_ascii=False, indent=1) + '\n```\n'


def ask_book(book):
    return (ASK + BOOK_TAIL + '\n## 絵テーブル（全シート）\n\n```json\n'
            + json.dumps(book, ensure_ascii=False, indent=1) + '\n```\n')


# ================================================================ 区分を重ねる

def parse_reply(text):
    """貼り込まれたものを JSON にする。コードブロックの囲みが付いていても読む。"""
    if isinstance(text, dict):
        return text
    t = str(text or '').strip()
    t = re.sub(r'^```(?:json)?\s*|\s*```$', '', t)
    try:
        d = json.loads(t)
    except ValueError as e:
        raise ValueError('JSON として読めません: %s' % e)
    if not isinstance(d, dict):
        raise ValueError('JSON のオブジェクトを貼ってください')
    return d


def apply_book(text, grids, bases):
    """全シートぶんの区分を，いっぺんに重ねる。

    "sheets" を持つ形なら全シートへ，持たない形なら "sheet" の指す一枚へ重ねる。
    """
    d = parse_reply(text)
    out = dict((n, dict(bases.get(n) or {})) for n in grids)
    rep = {'by_sig': 0, 'ranges': 0, 'marks': 0, 'painted': 0, 'skipped': [],
           'comment': str(d.get('comment') or '')[:600], 'sheets': []}
    todo = d.get('sheets')
    if not isinstance(todo, dict):
        name = d.get('sheet') or (list(grids) or [''])[0]
        todo = {name: d}
    for name, part in todo.items():
        g = grids.get(name)
        if g is None:
            rep['skipped'].append('シート「%s」がありません' % name)
            continue
        marks, r1 = apply_marks(part, g, out.get(name))
        out[name] = marks
        for k in ('by_sig', 'ranges', 'marks', 'painted'):
            rep[k] += r1[k]
        rep['skipped'] += ['%s: %s' % (name, x) for x in r1['skipped']]
        if r1['comment'] and not rep['comment']:
            rep['comment'] = r1['comment']
        rep['sheets'].append({'sheet': name, 'painted': r1['painted']})
    return out, rep


def apply_marks(text, grid, base=None):
    """生成AIが返してきた区分を，いまの塗りに重ねる。(塗り, 報告) を返す。"""
    m = dict(base or {})
    rep = {'by_sig': 0, 'ranges': 0, 'marks': 0, 'painted': 0, 'skipped': [],
           'comment': ''}
    d = parse_reply(text)
    rep['comment'] = str(d.get('comment') or '')[:600]

    def paint(r, c, k):
        if r < 1 or c < 1 or r > grid.rows or c > grid.cols:
            return 0
        if (r, c) in grid.covered:
            return 0
        m[addr(r, c)] = k
        return 1

    for e in (d.get('by_sig') or []):
        s = str(e.get('sig') or '')
        cols = e.get('cols') or {}
        n = 0
        for r in range(1, grid.rows + 1):
            if grid.sig(r) != s:
                continue
            for letter, k in cols.items():
                kk = kind_of(k)
                if kk:
                    n += paint(r, sh.col_index(letter), kk)
        rep['by_sig'] += 1
        rep['painted'] += n
        if not n:
            rep['skipped'].append('署名「%s」の行が見つかりません' % s)

    for e in (d.get('ranges') or []):
        kk = kind_of(e.get('kind'))
        mm = RE_RANGE.match(str(e.get('range') or '').strip())
        if not kk or not mm:
            rep['skipped'].append('範囲「%s」が読めません' % e.get('range'))
            continue
        a, b = parse_addr(mm.group(1)), parse_addr(mm.group(2))
        if not a or not b:
            continue
        for r in range(min(a[0], b[0]), max(a[0], b[0]) + 1):
            for c in range(min(a[1], b[1]), max(a[1], b[1]) + 1):
                rep['painted'] += paint(r, c, kk)
        rep['ranges'] += 1

    for a, k in (d.get('marks') or {}).items():
        kk = kind_of(k)
        rc = parse_addr(a)
        if not kk or not rc:
            rep['skipped'].append('番地「%s」が読めません' % a)
            continue
        rep['painted'] += paint(rc[0], rc[1], kk)
        rep['marks'] += 1
    return m, rep


def dump_marks(marks, grid, sheet_name):
    """いまの塗りを JSON にする（控えや持ち出しに使う）。"""
    return {'kind': 'tcv_marks', 'version': 1, 'sheet': sheet_name,
            'marks': dict((a, k) for a, k in sorted(marks.items()) if k in KINDS)}


def stats(marks, grid):
    """区分の数え上げ。枠のあるところだけを勘定する。"""
    out = {'blank': 0, 'label': 0, 'data': 0, 'framed': 0, 'framed_blank': 0}
    for r in range(1, grid.rows + 1):
        for c in range(1, grid.cols + 1):
            if (r, c) in grid.covered:
                continue
            k = marks.get(addr(r, c), 'blank')
            out[k] = out.get(k, 0) + 1
            if grid.anchor(r, c):
                out['framed'] += 1
                if k == 'blank':
                    out['framed_blank'] += 1
    return out


# ================================================================ 塗り分けの画面

def to_html(grid, marks, r0=1, r1=None, max_chars=40):
    """塗り分けできる方眼。td に番地を持たせ，画面側で塗る。"""
    r1 = min(grid.rows, r1 or grid.rows)
    out = ['<table class="paint" id="pg"><tr><th class="rn"></th>']
    for c in range(1, grid.cols + 1):
        out.append('<th class="cn" data-c="%d" title="この列をまとめて塗る">%s</th>'
                   % (c, sh.col_letter(c)))
    out.append('</tr>')
    for r in range(r0, r1 + 1):
        sig = grid.sig(r)
        out.append('<tr data-sig="%s"><th class="rn" data-r="%d" title="%s">%d</th>'
                   % (_esc(sig), r, _esc(sig), r))
        for c in range(1, grid.cols + 1):
            if (r, c) in grid.covered:
                continue
            rs, cs = grid.span(r, c)
            a = addr(r, c)
            v = grid.val(r, c)
            short = v if len(v) <= max_chars else v[:max_chars] + '…'
            attr = (' rowspan="%d"' % rs if rs > 1 else '') + \
                   (' colspan="%d"' % cs if cs > 1 else '')
            out.append('<td%s data-a="%s" data-r="%d" data-c="%d" class="k-%s%s" title="%s">%s</td>'
                       % (attr, a, r, c, marks.get(a, 'blank'),
                          ' fr' if grid.anchor(r, c) else '',
                          _esc(v)[:300], _esc(short).replace('\n', '<br>')))
        out.append('</tr>')
    out.append('</table>')
    return ''.join(out)


def _esc(t):
    return (t or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;') \
                    .replace('"', '&quot;')
