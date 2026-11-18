#!/usr/bin/env python3
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

"""check_package.py — FUJIN-P アプリパッケージ（JSON）のポリシー適合検査

FUJIN-P 作業手順書②（新規アプリの開発と公開）§2.1 の関所．
アプシャが書き出したパッケージJSONを受け取り，配布してよい形になっているかを検べる．

    python3 ~/check_package.py ~/Downloads/app_package_<app>_*.json

終了コード 0 = 適合（搬入してよい） / 1 = 不適合 / 2 = 読めない

検査項目
  A. 構造        export_type / format_version / files[] の形式，size と content の一致
  B. 許可リスト  *.py（階層を問わず）／templates/ 以下／data_for_distribution/ 以下／
                 アプリ直下の .sql .json .md .txt．static/ と version.json は入ってはならない
  C. 機関固有性  組織名としての「福知山公立大学」等（地名の「福知山」は可）
  D. 固定パス    /home/<user>/ の直書き
  E. 秘密        PASSWORD / SECRET / API_KEY / TOKEN への literal 代入
  F. 日時        datetime.now() の裸使い（JST変換なし）
  G. 文書        app_info.json の14フィールド，user_manual / spec_memo の有無
"""

import json
import os
import re
import sys
import glob

# ── 許可リスト（手順書 §2.1） ──
ALLOWED_ROOT_EXT = ('.sql', '.json', '.md', '.txt')
ALLOWED_DIRS = ('templates/', 'data_for_distribution/')
FORBIDDEN_NAMES = ('version.json',)

APP_INFO_FIELDS = (
    'blueprint_name', 'config_notes', 'description', 'directory_structure',
    'display_name', 'endpoints', 'libraries', 'migration_guide',
    'mysql_schema', 'overview', 'python_files', 'sql_tables_description',
    'template_files', 'url',
)

# ── 検査パターン ──
RE_INSTITUTION = re.compile(r'福知山公立大学|公立大|fukuchiyama\.ac\.jp|学内|大学構成員|@fukuchiyama')
RE_FIXED_PATH = re.compile(r'/home/[A-Za-z0-9_]+/')
RE_SECRET = re.compile(r'(PASSWORD|SECRET|API_KEY|TOKEN)\s*=\s*[\'"][^<\'"]')
RE_NAIVE_NOW = re.compile(r'datetime\.now\(\s*\)')

problems = []   # 適合しない（終了コード1）
notes = []      # 目で見て判断するもの


def ng(msg):
    problems.append(msg)


def note(msg):
    notes.append(msg)


def allowed_path(path):
    """許可リストに合致するか"""
    if os.path.basename(path) in FORBIDDEN_NAMES:
        return False
    if '/static/' in path or path.startswith('static/'):
        return False
    if path.endswith('.py'):
        return True
    for d in ALLOWED_DIRS:
        if path.startswith(d):
            return True
    if '/' not in path and path.endswith(ALLOWED_ROOT_EXT):
        return True
    return False


def check_structure(pkg):
    if pkg.get('export_type') != 'fujinp_app_package':
        ng('export_type が fujinp_app_package ではない: %r' % pkg.get('export_type'))
    if pkg.get('format_version') not in (2, '2'):
        note('format_version が 2 以外: %r' % pkg.get('format_version'))
    if not pkg.get('app_name'):
        ng('app_name が空')
    files = pkg.get('files')
    if not isinstance(files, list) or not files:
        ng('files[] が無い，または空')
        return []
    declared = pkg.get('file_count')
    if declared is not None and declared != len(files):
        ng('file_count(%s) と files の件数(%d) が違う' % (declared, len(files)))
    return files


def check_files(files):
    seen = set()
    for f in files:
        path = f.get('path', '')
        if not path:
            ng('path の無いエントリがある')
            continue
        if path in seen:
            ng('%s : 同じパスが2回入っている' % path)
        seen.add(path)

        if not allowed_path(path):
            ng('%s : 許可リスト外（static / version.json / 対象外の拡張子）' % path)

        content = f.get('content')
        if content is None:
            ng('%s : content が無い' % path)
            continue
        size = f.get('size')
        real = len(content.encode('utf-8'))
        if size is not None and size != real:
            ng('%s : size(%s) と実バイト数(%d) が食い違う' % (path, size, real))

        scan_text(path, content)
    return seen


def scan_text(path, text):
    is_py = path.endswith('.py')
    for i, line in enumerate(text.splitlines(), 1):
        where = '%s:%d' % (path, i)
        if RE_INSTITUTION.search(line):
            ng('%s : 機関固有の表記 → %s' % (where, line.strip()[:80]))
        if is_py and RE_FIXED_PATH.search(line):
            # コメント中の例示は注意にとどめる
            if line.lstrip().startswith('#'):
                note('%s : コメント内に /home/... の記述 → %s' % (where, line.strip()[:80]))
            else:
                ng('%s : 固定パスの直書き → %s' % (where, line.strip()[:80]))
        if RE_SECRET.search(line):
            ng('%s : 秘密の直書きの疑い → %s' % (where, line.strip()[:60]))
        if is_py and RE_NAIVE_NOW.search(line):
            ng('%s : datetime.now() の裸使い（JST変換が要る） → %s' % (where, line.strip()[:80]))


def check_docs(pkg, paths):
    if 'app_info.json' not in paths:
        ng('app_info.json が入っていない（ダッシュボードの「仕様書」が空になる）')
    else:
        for f in pkg['files']:
            if f.get('path') == 'app_info.json':
                try:
                    info = json.loads(f['content'])
                except Exception as e:
                    ng('app_info.json が JSON として読めない: %s' % e)
                    return
                fields = info.get('fields', {})
                missing = [k for k in APP_INFO_FIELDS if not (fields.get(k, {}) or {}).get('value')]
                if missing:
                    ng('app_info.json の未記入フィールド: %s' % ', '.join(missing))
                extra = [k for k in fields if k not in APP_INFO_FIELDS]
                if extra:
                    note('app_info.json に規定外のフィールド: %s' % ', '.join(extra))

    if not (pkg.get('user_manual') or {}).get('content'):
        ng('ユーザーズマニュアルが空（アプシャ4点セットの1つ）')
    if not (pkg.get('spec_memo') or {}).get('content'):
        ng('仕様書メモが空（アプシャ4点セットの1つ）')

    if not any(p.endswith('.sql') for p in paths):
        note('スキーマ(.sql)が入っていない（DBを使わないアプリなら正常）')
    if not any(p.endswith('routes.py') for p in paths):
        note('routes.py が入っていない（構成を確認すること）')


def main():
    args = sys.argv[1:]
    if not args or args[0] in ('-h', '--help'):
        print(__doc__)
        return 0 if args else 2

    targets = []
    for a in args:
        targets.extend(sorted(glob.glob(os.path.expanduser(a))) or [os.path.expanduser(a)])

    if len(targets) > 1:
        print('※ %d 件が該当します．最新のものだけを検べるなら，ファイル名を明示してください．' % len(targets))
        for t in targets:
            print('   -', t)
        print()

    worst = 0
    for path in targets:
        problems.clear()
        notes.clear()
        print('── %s' % path)
        try:
            with open(path, encoding='utf-8') as fp:
                pkg = json.load(fp)
        except Exception as e:
            print('   読めません: %s' % e)
            worst = max(worst, 2)
            continue

        files = check_structure(pkg)
        paths = check_files(files) if files else set()
        check_docs(pkg, paths)

        print('   アプリ: %s（%s） / ファイル %d 件 / %.1f KB'
              % (pkg.get('app_name', '?'), pkg.get('display_name', '?'),
                 len(files), os.path.getsize(path) / 1024))
        for p in sorted(paths):
            print('     - %s' % p)

        if notes:
            print('   [注意]')
            for m in notes:
                print('     ・%s' % m)

        if problems:
            print('   ✗ ポリシー不適合 — %d 件' % len(problems))
            for m in problems:
                print('     ・%s' % m)
            worst = max(worst, 1)
        else:
            print('   ✓ ポリシー適合')
        print()

    return worst


if __name__ == '__main__':
    sys.exit(main())
