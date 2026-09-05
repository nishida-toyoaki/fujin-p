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

"""対応式 —— 絵エクセルと，ふつうのSQLテーブル群との対応。

## 考え方

絵エクセルの一枚は，たいてい「帯（band）の縦の連なり」でできている。

  ・固定帯（once）   決まった文字が決まった位置に，一度だけ出る
  ・反復帯（repeat） あるテーブルの各行につき一度ずつ出る。入れ子にできる

帯の中は「枠（slot）」の集まりで，枠は置き場所（行の中の位置・縦横の合併数）と
値の出どころを持つ。値の出どころは次の五つ。

  {"lit": "文字"}                     様式の側の決まり文句。データではない
  {"col": "列名"}                     その帯のテーブルの列
  {"fmt": "…{列名}…"}                 列から組み立てた文字（読むときは逆に解く）
  {"list": {...}}                     一つのセルに何件も入っているもの（子テーブルへ）
  {"skip": true}                      対応式は関知しない（残余へ回す）

これで絵の一枚は

    絵 ＝ Σ（帯のパターン ⊗ テーブルの行）  ⊕  残余

と分解される。左が積で，これがふつうのSQLテーブル群になる。
右の残余は，帯で説明のつかなかったセルで，そのまま台帳に控える。
残余があっても往復はできる。残余が減るほど，その様式をよく捉えたことになる。

## 往復について

分解（絵→表）は帯を上から順に照合し，合った帯はテーブルの行を生み，
合わなかった行は残余に落とす。生成（表→絵）は帯を上から展開し，
最後に残余を重ねる。だから同じ対応式で往復すると元に戻る。
"""

import json
import re

RE_NAME = re.compile(r'^[a-z][a-z0-9_]{1,40}$')
RE_IDENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,62}$')
RE_FIELD = re.compile(r'\{([A-Za-z_][A-Za-z0-9_]*)\}')

SPEC_VERSION = 1
DEFAULT_DATASET_COL = 'dataset'


class SpecError(Exception):
    pass


# ================================================================ 読み込みと検証

def load(text):
    """文字列（JSON）を対応式にする。構文と筋の通りかたを検める。"""
    if isinstance(text, dict):
        d = text
    else:
        try:
            d = json.loads(text)
        except ValueError as e:
            raise SpecError('JSON として読めません: %s' % e)
    if not isinstance(d, dict):
        raise SpecError('対応式は JSON のオブジェクトです')
    validate(d)
    return d


def validate(d):
    errs = check(d)
    if errs:
        raise SpecError('／'.join(errs))
    return True


def check(d):
    """筋の通らないところを列挙する（空なら通っている）。"""
    e = []
    if int(d.get('spec_version') or 0) != SPEC_VERSION:
        e.append('spec_version は %d です' % SPEC_VERSION)
    if not RE_NAME.match(str(d.get('name') or '')):
        e.append('name は小文字英数と下線で 2〜41 文字')
    if not d.get('sheet'):
        e.append('sheet（対象のシート名）が要ります')
    tables = d.get('tables')
    if not isinstance(tables, dict) or not tables:
        e.append('tables が空です')
        return e
    for alias, t in tables.items():
        if not RE_NAME.match(alias):
            e.append('テーブルの別名「%s」が使えません' % alias)
        if not RE_IDENT.match(str(t.get('table') or '')):
            e.append('%s: SQL のテーブル名が使えません' % alias)
        cols = t.get('columns')
        if not isinstance(cols, dict) or not cols:
            e.append('%s: columns が空です' % alias)
            continue
        for c in cols:
            if not RE_IDENT.match(c):
                e.append('%s: 列名「%s」が使えません' % (alias, c))
        seq = t.get('seq')
        if seq and seq not in cols:
            e.append('%s: seq「%s」が columns にありません' % (alias, seq))
        for k, v in (t.get('inherit') or {}).items():
            if k not in cols:
                e.append('%s: inherit の「%s」が columns にありません' % (alias, k))
            pa, _, pc = str(v).partition('.')
            if pa not in tables or pc not in (tables.get(pa, {}).get('columns') or {}):
                e.append('%s: inherit の参照先「%s」が見つかりません' % (alias, v))
    bands = d.get('bands')
    if not isinstance(bands, list) or not bands:
        e.append('bands が空です')
        return e
    for b in bands:
        e += _check_band(b, tables, '')
    return e


def _check_band(b, tables, path):
    e = []
    if not isinstance(b, dict):
        return ['%s: 帯が JSON のオブジェクトではありません' % path]
    bid = b.get('id') or '(無名)'
    here = (path + '/' if path else '') + str(bid)
    kind = b.get('kind') or 'once'
    if kind not in ('once', 'repeat'):
        e.append('%s: kind は once か repeat です' % here)
    alias = b.get('use')
    if kind == 'repeat':
        if alias not in tables:
            e.append('%s: use「%s」がテーブルにありません' % (here, alias))
    cols = (tables.get(alias, {}).get('columns') or {}) if alias else {}
    rows = b.get('rows')
    if not isinstance(rows, list) or not rows:
        e.append('%s: rows が空です' % here)
        rows = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            e.append('%s: rows[%d] が不正です' % (here, i))
            continue
        used = {}
        for s in (row.get('slots') or []):
            c = s.get('c')
            if not isinstance(c, int) or c < 1:
                e.append('%s: rows[%d] の枠に列 c がありません' % (here, i))
                continue
            for cc in range(c, c + int(s.get('cs') or 1)):
                if cc in used:
                    e.append('%s: rows[%d] で列 %d が重なっています' % (here, i, cc))
                used[cc] = True
            e += _check_value(s.get('v'), cols, tables, '%s rows[%d]' % (here, i))
    for k in (b.get('when') or {}):
        if k not in cols:
            e.append('%s: when の「%s」が columns にありません' % (here, k))
    gap = b.get('gap')
    if gap and gap.get('col') and gap['col'] not in cols:
        e.append('%s: gap の「%s」が columns にありません' % (here, gap['col']))
    sibs = [x for x in (b.get('bands') or []) if x.get('use')]
    shared = [x for x in sibs if len([y for y in sibs if y.get('use') == x.get('use')]) > 1]
    for x in shared:
        if not x.get('when'):
            e.append('%s/%s: 同じテーブルを使う帯が並んでいます。'
                     'どの形だったか残せるよう when を付けてください' % (here, x.get('id')))
    for k in (b.get('bands') or []):
        e += _check_band(k, tables, here)
    for i, row in enumerate(b.get('tail') or []):
        if not isinstance(row, dict):
            e.append('%s: tail[%d] が不正です' % (here, i))
    return e


def _check_value(v, cols, tables, where):
    if not isinstance(v, dict):
        return ['%s: 値の出どころがありません' % where]
    if 'lit' in v or 'skip' in v:
        return []
    if 'col' in v:
        return [] if v['col'] in cols else ['%s: 列「%s」がありません' % (where, v['col'])]
    if 'fmt' in v:
        miss = [f for f in RE_FIELD.findall(str(v['fmt'])) if f not in cols]
        return ['%s: fmt の「%s」がありません' % (where, ','.join(miss))] if miss else []
    if 'list' in v:
        L = v['list']
        if not isinstance(L, dict):
            return ['%s: list が不正です' % where]
        a = L.get('table')
        if a not in tables:
            return ['%s: list の table「%s」がありません' % (where, a)]
        kcols = tables[a].get('columns') or {}
        miss = [f for f in RE_FIELD.findall(str(L.get('fmt') or '')) if f not in kcols]
        if miss:
            return ['%s: list の fmt の「%s」がありません' % (where, ','.join(miss))]
        for k in (L.get('match') or {}):
            if k not in kcols:
                return ['%s: list の match の「%s」がありません' % (where, k)]
        return []
    return ['%s: 値の出どころが分かりません' % where]


# ================================================================ 値式

def fmt_build(fmt, row):
    """{列名} を値で埋める。"""
    def rep(m):
        v = row.get(m.group(1))
        return '' if v is None else str(v)
    return RE_FIELD.sub(rep, str(fmt))


def fmt_parse(fmt, text):
    """組み立てた文字を逆に解いて {列名: 値} にする。解けなければ None。"""
    fields = RE_FIELD.findall(str(fmt))
    if not fields:
        return {} if str(fmt).strip() == (text or '').strip() else None
    pat, last = [], len(fields) - 1
    i = 0
    for n, m in enumerate(RE_FIELD.finditer(str(fmt))):
        pat.append(re.escape(str(fmt)[i:m.start()]))
        pat.append('(?P<f%d>.%s)' % (n, '+' if n == last else '+?'))
        i = m.end()
    pat.append(re.escape(str(fmt)[i:]))
    m = re.match(r'^\s*' + ''.join(pat) + r'\s*$', text or '', re.S)
    if not m:
        return None
    return dict((f, m.group('f%d' % n).strip()) for n, f in enumerate(fields))


# ================================================================ SQL の宣言

TYPE_DEFAULT = 'TEXT'


def ddl(d):
    """対応式が要求するテーブルの CREATE 文。MySQL コンソールに貼れる形で返す。"""
    ds = d.get('dataset_col', DEFAULT_DATASET_COL)
    out = []
    for alias, t in sorted((d.get('tables') or {}).items()):
        name = t['table']
        lines = ['CREATE TABLE IF NOT EXISTS `%s` (' % name,
                 '  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,']
        if ds:
            lines.append('  `%s` VARCHAR(64) NOT NULL DEFAULT %s,' % (ds, "''"))
        lines.append('  `_ord` INT NOT NULL DEFAULT 0,')
        for c, ty in t['columns'].items():
            lines.append('  `%s` %s NULL,' % (c, ty or TYPE_DEFAULT))
        lines.append('  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP '
                     'ON UPDATE CURRENT_TIMESTAMP,')
        keys = []
        if ds:
            keys.append('`%s`' % ds)
        keys.append('`_ord`')
        lines.append('  KEY `ix_%s_ord` (%s)' % (name[:48], ', '.join(keys)))
        lines.append(') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;')
        head = '-- %s%s\n' % (alias, ('（%s）' % t['note']) if t.get('note') else '')
        out.append(head + '\n'.join(lines))
    return '\n\n'.join(out)


def tables_of(d):
    return [(a, t['table']) for a, t in sorted((d.get('tables') or {}).items())]


# ================================================================ 生成AIへの依頼文

GRAMMAR = '''\
対応式は次の形の JSON です。

{
  "spec_version": 1,
  "name": "英小文字と下線の名前",
  "title": "日本語の題名",
  "sheet": "対象のシート名",
  "grid": {"cols": 列数, "col_widths": {"A": 1.75, "B": 7.0}, "row_height": 15},
  "dataset_col": "dataset",
  "tables": {
    "別名": {"table": "SQLのテーブル名", "seq": "連番を入れる列名",
             "inherit": {"自分の列": "親の別名.列"},
             "columns": {"列名": "SQLの型", ...}}
  },
  "bands": [ 帯, ... ],
  "styles": {"名前": {"bold": true, "fill": "F2F2F2", "align": "center"}}
}

帯は次の形です。

{
  "id": "帯の名前",
  "kind": "once" か "repeat",
  "use": "repeat のときに一行ずつ生む，テーブルの別名",
  "rows": [ 行, ... ],          物理行の並び。帯の高さはこの本数
  "bands": [ 子の帯, ... ],     この帯の下にぶら下がって繰り返される帯
  "tail":  [ 行, ... ]          帯の後ろに置くしめくくり（合わなくてよい行は "opt": true）
}

行は次のいずれかです。

  {"blank": true}                    空の行
  {"covered": true}                  上の行の合併に呑まれている行
  {"sig": "B2x1,C1x5"}               行の署名（照合を確かにするための任意の目印）
  {"slots": [ 枠, ... ]}             枠の並び

枠は {"c": 列番号(1始まり), "rs": 縦の合併数, "cs": 横の合併数,
      "v": 値の出どころ, "style": "見かけの名前",
      "opt": 枠ごと無くてよいなら true,
      "req": 空でも枠は必ず要るなら true,
      "loose": 合併のかたちを問わないなら true}。

読むときの決まり。lit の枠と req の枠は，そこに枠が無ければ帯が合わない。
それ以外のデータの枠は，空セルなら「空の値」とみなして合ったことにする。
帯の変種（同じ場所に違うかたちが来る様式）は，見分けの効く枠に req を付け，
帯に "when": {"列名": "値"} を書いて，どの形だったかを列に残す。

値の出どころは五つ。
  {"lit": "決まり文句"}                様式の側の文字。データではない
  {"col": "列名"}                      その帯のテーブルの列
  {"fmt": "…{列名}…"}                  列から組み立てた文字。読むときは逆に解く
  {"list": {"table": "子の別名", "fmt": "{列}　{列}", "sep": "\\n",
            "match": {"列名": "定数"}, "order": ["列名"]}}
                                       一つのセルに何件も入っているもの
  {"skip": true}                       対応式は関知しない（残余へ回す）

守ってほしいこと。
  ・様式の決まり文句は lit に畳む。データの列にしない。
  ・繰り返し現れるものは repeat の帯にする。入れ子は bands に置く。
  ・一つのセルに複数件が詰まっているときだけ list を使う。
  ・列は，あとで人が SQL で扱える粒度に割る（番号・本文・評価などを混ぜない）。
  ・説明のつかない行は無理に帯にしない。残余に落ちてよい。
  ・帯のうしろの空行は tail ではなく "gap": {"col": "列名", "max": 3} で数える。
'''


def prompt(grid, sheet_name, census, sample_rows=''):
    """人が生成AIに渡す依頼文をこしらえる。"""
    head = ['この絵エクセルを分解して，対応式（JSON）を提案してください。',
            '',
            '## 対応式の書き方', '', GRAMMAR, '',
            '## シート「%s」の様子' % sheet_name, '',
            '大きさ: %d 行 × %d 列' % (grid.rows, grid.cols), '',
            '列の幅（エクセルの文字数）:']
    from . import sheet as sh
    head.append('  ' + ', '.join('%s=%.2f' % (sh.col_letter(c), w)
                                 for c, w in sorted(grid.col_widths.items())))
    head += ['', '行の署名（合併のかたち）の出現回数:', '']
    for s, n in census[:24]:
        head.append('  %5d 回   %s' % (n, s))
    head += ['', '行ごとの署名と，その行の最初の文字:', '', '```', sample_rows, '```', '',
             '## お願い', '',
             '上の様子を見て，この様式をいちばんよく説明する対応式を一つ提案してください。',
             'JSON だけを出し，前後に説明を付けないでください。',
             '迷ったところがあれば，JSON の後に「迷ったところ」として短く添えてください。']
    return '\n'.join(head)
