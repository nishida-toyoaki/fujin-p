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

# resanitize_svgs.py — 既存のアップロード SVG を一括で無害化する一回限りの道具（2026-10-26 R-2）
#
# 使い方（PythonAnywhere の Bash コンソールで，ホームディレクトリから）：
#   cd ~ && python3 resanitize_svgs.py          … 調べるだけ（何も書き換えない）
#   cd ~ && python3 resanitize_svgs.py --yes    … 書き換える（元のファイルは ~/svg_backup_日時/ に退避）
#
# 対象：ホーム以下で，利用者のアップロード置き場にある .svg
#   …/static/mdimgs/（コレポ・マイノートの公開領域），…/static/ffimgs/（えふえふの公開領域），
#   …/data/files/（マイノート・えふえふの保護領域）
# 無害化には markdown_converter.sanitize_svg_bytes（R-2 版）を使う．
import os
import sys
import shutil
import datetime

HOME = os.path.expanduser('~')
sys.path.insert(0, HOME)
# markdown_converter は auth を import し，auth はアプリ全体（app.py）を起動しようとする．
# ここでは無害化の関数だけが要るので，auth を空の代用品に差し替えてから読み込む．
import types  # noqa: E402
_auth_stub = types.ModuleType('auth')
_auth_stub.login_required = lambda f: f
sys.modules['auth'] = _auth_stub
from markdown_converter import sanitize_svg_bytes   # noqa: E402

MARKERS = (os.sep + os.path.join('static', 'mdimgs') + os.sep,
           os.sep + os.path.join('static', 'ffimgs') + os.sep,
           os.sep + os.path.join('data', 'files') + os.sep)
SKIP_DIRS = {'.git', 'fujin-p-repo', '.cache', '.local', '.virtualenvs', 'node_modules'}


def targets():
    for root, dirs, files in os.walk(HOME):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith('svg_backup_')]
        r = root + os.sep
        if not any(m in r for m in MARKERS):
            continue
        for f in files:
            if f.lower().endswith('.svg'):
                yield os.path.join(root, f)


def main():
    write = '--yes' in sys.argv
    backup = os.path.join(HOME, 'svg_backup_' + datetime.datetime.now().strftime('%Y%m%d_%H%M%S'))
    n_all = n_changed = 0
    for p in targets():
        n_all += 1
        with open(p, 'rb') as fh:
            data = fh.read()
        clean = sanitize_svg_bytes(data)
        if clean == data:
            continue
        n_changed += 1
        rel = os.path.relpath(p, HOME)
        print(('書き換え ' if write else '要書き換え ') + rel)
        if write:
            dst = os.path.join(backup, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(p, dst)
            with open(p, 'wb') as fh:
                fh.write(clean)
    print('SVG %d 本のうち %d 本が%s' % (n_all, n_changed,
          ('書き換え済み（元のファイルは %s）' % backup) if write and n_changed else
          ('書き換え対象です．--yes を付けて実行すると書き換えます．' if n_changed else '対象外（変更なし）でした．')))


if __name__ == '__main__':
    main()
