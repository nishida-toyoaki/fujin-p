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
# Source: https://github.com/u-fukuchiyama/fujin-p

"""
まいあし：導入スクリプト（fujinp_kernel_install.py）の組み立て

いま動いているサイトのカーネルから，配布用の導入スクリプトを作り直す．
Bash コンソールで実行する（Flask は使わない）．

    python3 ~/fujinp/migration_assistant/build_installer.py          中身の一覧を出すだけ
    python3 ~/fujinp/migration_assistant/build_installer.py --yes    導入スクリプトを書き換える

集める規則（アプシャのカーネル書き出しと同じ考え方）
  ホーム直下      … .py はすべて．ただし EXCLUDE_ROOT に挙げたものを除く
  templates/     … すべて
  fujinp/        … registry.py と，カーネルのアプリ（KERNEL_APPS）のディレクトリ
  種専用のファイル … 今の導入スクリプトの中身をそのまま引き継ぐ（SEED_OWNED）

アプリだけが使う共通部品（notifiers.py など）もホーム直下にあれば必ず入る．
書き換える前の導入スクリプトは ~/fujinp_installer_backups/ に日時付きで残す．
（data_for_distribution/ に置くと，アプシャのパッケージや GitHub に紛れ込むため）
"""

import base64
import datetime
import gzip
import io
import os
import re
import sys
import tarfile

HOME = os.path.expanduser('~')
HERE = os.path.dirname(os.path.abspath(__file__))
INSTALLER = os.path.join(HERE, 'data_for_distribution', 'fujinp_kernel_install.py')
PREFIX = 'home/'

# ホーム直下で入れないもの
EXCLUDE_ROOT = {
    'config.py',                      # 秘密情報．種は config_template.py を配る
    'add_license_headers.py',         # 公開作業の道具
    'check_package.py',               # パッケージ点検の道具
    'migrate_launcher_visibility.py', # 一度きりの移行道具
    'check_site_config.py',           # サイト点検の道具
    'fujinp_kernel_install.py',       # 導入スクリプト自身（ホームに置かれていても入れない）
}
EXCLUDE_ROOT_PREFIXES = ('client_secret', 'credentials', 'service_account')

# カーネルのアプリ（fujinp/ の下）
KERNEL_APPS = ('admin', 'app_share')
KERNEL_FUJINP_FILES = ('registry.py',)

# 種専用のファイル．サイトのものではなく，今の導入スクリプトの中身を引き継ぐ．
#   config_template.py … サイトの雛形は <YOUR_…> だらけなので，種用に整えた版を使う
#   requirements.txt   … 種で足した行（mysql-connector-python など）を含む版を使う
SEED_OWNED = ('config_template.py', 'requirements.txt')
SEED_OWNED_DIRS = ('fujinp_seed/',)

# ディレクトリを集めるときに飛ばすもの
SKIP_DIRS = {'__pycache__', 'import_backups', 'sql_saver_work', '.git'}
SKIP_SUFFIXES = ('.pyc', '.swp', '.swo', '~', '.bak')

# 秘密らしき記述（見つけたら止める）
SECRET_PATTERNS = [
    (re.compile(r'GOCSPX-[A-Za-z0-9_-]{10,}'), 'Google クライアントシークレット'),
    (re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----'), '秘密鍵'),
    (re.compile(r'xox[bpa]-[0-9A-Za-z-]{10,}'), 'Slack トークン'),
    (re.compile(r'sk-ant-[A-Za-z0-9_-]{10,}'), 'Anthropic API キー'),
    (re.compile(r'github_pat_[A-Za-z0-9_]{20,}'), 'GitHub トークン'),
]


# ------------------------------------------------------------------
# 今の導入スクリプトを読む
# ------------------------------------------------------------------

PAYLOAD_RE = re.compile(r"PAYLOAD = \(\n(.*?)\n\)\n", re.S)


def read_installer():
    with open(INSTALLER, encoding='utf-8') as f:
        src = f.read().replace('\r\n', '\n')
    m = PAYLOAD_RE.search(src)
    if not m:
        sys.exit(f'{INSTALLER} の中に PAYLOAD が見つかりません．')
    raw = gzip.decompress(base64.b64decode(''.join(re.findall(r"'([^']*)'", m.group(1)))))
    tf = tarfile.open(fileobj=io.BytesIO(raw))
    old = {}
    for mem in tf.getmembers():
        if mem.isfile() and mem.name.startswith(PREFIX):
            old[mem.name[len(PREFIX):]] = tf.extractfile(mem).read()
    return src, m, old


# ------------------------------------------------------------------
# サイトから集める
# ------------------------------------------------------------------

def _walk(base_rel):
    base = os.path.join(HOME, base_rel)
    out = []
    for root, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith('.'))
        for fn in sorted(files):
            if fn.startswith('.') or fn.endswith(SKIP_SUFFIXES):
                continue
            out.append(os.path.relpath(os.path.join(root, fn), HOME))
    return out


def collect(old):
    """戻り値: (files{rel: bytes}, skipped_root[名前])"""
    files, skipped = {}, []
    for name in sorted(os.listdir(HOME)):
        path = os.path.join(HOME, name)
        if not os.path.isfile(path) or name.startswith('.'):
            continue
        if not name.endswith('.py'):
            continue
        if name in EXCLUDE_ROOT or name.startswith(EXCLUDE_ROOT_PREFIXES) or name in SEED_OWNED:
            skipped.append(name)
            continue
        files[name] = open(path, 'rb').read()
    for rel in _walk('templates'):
        files[rel] = open(os.path.join(HOME, rel), 'rb').read()
    for fn in KERNEL_FUJINP_FILES:
        rel = os.path.join('fujinp', fn)
        files[rel] = open(os.path.join(HOME, rel), 'rb').read()
    for app in KERNEL_APPS:
        for rel in _walk(os.path.join('fujinp', app)):
            files[rel] = open(os.path.join(HOME, rel), 'rb').read()
    # 種専用のファイルは今の導入スクリプトから引き継ぐ
    for rel, data in old.items():
        if rel in SEED_OWNED or rel.startswith(SEED_OWNED_DIRS):
            files[rel] = data
    # 改行を LF にそろえる（テキストだけ）
    for rel, data in list(files.items()):
        if b'\0' not in data:
            files[rel] = data.replace(b'\r\n', b'\n')
    return files, skipped


def scan_secrets(files):
    hits = []
    for rel, data in files.items():
        if b'\0' in data:
            continue
        text = data.decode('utf-8', errors='replace')
        for i, ln in enumerate(text.splitlines(), 1):
            for pat, label in SECRET_PATTERNS:
                if pat.search(ln):
                    hits.append((rel, i, label))
    return hits


# ------------------------------------------------------------------
# 組み立て
# ------------------------------------------------------------------

def build(src, m, files):
    now = int(datetime.datetime.now().timestamp())
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w') as tf:
        for rel in sorted(files):
            data = files[rel]
            info = tarfile.TarInfo(PREFIX + rel)
            info.size = len(data)
            info.mtime = now
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
    enc = base64.b64encode(gzip.compress(buf.getvalue(), mtime=0)).decode()
    lines = '\n'.join("    '%s'" % enc[i:i + 100] for i in range(0, len(enc), 100))
    out = src[:m.start()] + 'PAYLOAD = (\n' + lines + '\n)\n' + src[m.end():]
    today = datetime.date.today().isoformat()
    out, n = re.subn(r"BUILD_INFO = '[^']*'",
                     f"BUILD_INFO = 'FUJIN-P kernel seed / built {today} / {len(files)} files'", out)
    if n != 1:
        sys.exit('BUILD_INFO の行が見つかりません．')
    return out if out.endswith('\n') else out + '\n'


def main(argv):
    write = '--yes' in argv
    src, m, old = read_installer()
    files, skipped = collect(old)

    added = sorted(set(files) - set(old))
    removed = sorted(set(old) - set(files))
    changed = sorted(r for r in files if r in old and files[r] != old[r])

    print(f'組み立て元: {HOME}')
    print(f'今の導入スクリプト: {len(old)} ファイル → 作り直すと: {len(files)} ファイル\n')
    print('ホーム直下の .py（サイトから入れるもの）')
    for rel in sorted(r for r in files if '/' not in r and r.endswith('.py') and r not in SEED_OWNED):
        print(f'    {rel}')
    print('ホーム直下の .py（入れないもの）')
    for name in skipped:
        tag = '種専用（今の導入スクリプトから引き継ぐ）' if name in SEED_OWNED else '除外リスト'
        print(f'    {name}　… {tag}')
    print(f'\n増えるファイル: {len(added)}')
    for r in added:
        print(f'    + {r}')
    print(f'減るファイル: {len(removed)}')
    for r in removed:
        print(f'    - {r}')
    print(f'中身が変わるファイル: {len(changed)}')
    for r in changed:
        print(f'    ~ {r}')

    hits = scan_secrets(files)
    if hits:
        print('\n秘密らしき記述が見つかりました．書き換えを止めます．')
        for rel, i, label in hits:
            print(f'    {rel}:{i}　{label}')
        return 1

    if not write:
        print('\n一覧を出しただけで，何も書き換えていません．')
        print('よければ，末尾に --yes を付けてもう一度実行してください．')
        return 0

    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    bdir = os.path.join(HOME, 'fujinp_installer_backups')
    os.makedirs(bdir, exist_ok=True)
    backup = os.path.join(bdir, f'fujinp_kernel_install_{stamp}.py')
    with open(INSTALLER, 'rb') as f_old, open(backup, 'wb') as f_bak:
        f_bak.write(f_old.read())
    out = build(src, m, files)
    with open(INSTALLER, 'w', encoding='utf-8', newline='\n') as f:
        f.write(out)
    print(f'\n書き換えました: {INSTALLER}')
    print(f'前の版の控え: {backup}')
    print('確かめ方: python3 ' + INSTALLER + ' --list | head -2')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
