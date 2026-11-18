#!/usr/bin/env python3
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

"""
アプシャ移送の適用（取り込み側サイトで実行する）

  cd ~ && python3.13 as_apply.py as_transfer_XXXXXXXX_XXXXXX.zip         … 点検だけ（何も変えない）
  cd ~ && python3.13 as_apply.py as_transfer_XXXXXXXX_XXXXXX.zip --yes   … 実行

実行すると次の順で進む．
  1. 置き換わるファイルの現物を ~/as_backup_<日時>.zip に控える
  2. zip の中身を書き込む（ダッシュボードのテンプレートは，同じ場所に既にあるものだけ）
  3. テーブルの列の不足を調べ，MySQLコンソールに貼る SQL を表示する（SQL は実行しない）
  4. config.py に足りない定数を表示する（config.py は変更しない）
データベースと config.py には触れない．
"""
import datetime
import importlib.util
import json
import os
import re
import sys
import zipfile

HOME = os.path.expanduser('~')
sys.path.insert(0, HOME)
os.chdir(HOME)

APP_DIR = 'fujinp/app_share'


def _s(v):
    if isinstance(v, (bytes, bytearray)):
        return v.decode('utf-8')
    return v


def _rows(cur, sql, args=()):
    cur.execute(sql, args)
    rs = cur.fetchall()
    cols = [d[0] for d in cur.description]
    out = []
    for r in rs:
        d = r if isinstance(r, dict) else dict(zip(cols, r))
        out.append({k: _s(v) for k, v in d.items()})
    return out


def local_code_files(rel_dir):
    base = os.path.join(HOME, rel_dir)
    out = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d != '__pycache__']
        rel_root = os.path.relpath(root, base)
        in_tpl = 'templates' in rel_root.split(os.sep)
        for f in files:
            if f.endswith('.py') or in_tpl:
                out.append(os.path.relpath(os.path.join(root, f), HOME))
    return set(out)


def target_columns():
    import db
    out = {}
    with db.get_db_cursor() as pair:
        cur = pair[0] if isinstance(pair, (tuple, list)) else pair
        rows = _rows(cur, "SELECT TABLE_NAME AS t, COLUMN_NAME AS c, COLUMN_TYPE AS ty "
                          "FROM information_schema.COLUMNS "
                          "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME LIKE %s", ('app\\_share\\_%',))
    for r in rows:
        out.setdefault(r['t'], {})[r['c']] = r['ty']
    return out


def coldef(c):
    parts = [f"`{c['name']}`", c['type'], 'NULL' if c['nullable'] == 'YES' else 'NOT NULL']
    d = c.get('dflt')
    extra = c.get('extra') or ''
    generated = 'DEFAULT_GENERATED' in extra.upper()
    extra = re.sub(r'(?i)DEFAULT_GENERATED', '', extra).strip()
    if d is not None and str(d).upper() != 'NULL':
        d = str(d)
        if generated or '(' in d or d.upper().startswith('CURRENT_TIMESTAMP'):
            parts.append(f'DEFAULT {d}')              # 式（CURRENT_TIMESTAMP など）
        elif len(d) >= 2 and d[0] == "'" and d[-1] == "'":
            parts.append(f'DEFAULT {d}')              # MariaDB は引用符つきで返す
        else:
            parts.append("DEFAULT '" + d.replace("'", "''") + "'")
    if extra:
        parts.append(extra)
    if c.get('comment'):
        parts.append("COMMENT '" + c['comment'].replace("'", "''") + "'")
    return ' '.join(parts)


def schema_report(src):
    print('── 3. テーブルの点検 ──')
    if not src:
        print('写し元のテーブル定義が zip に入っていません．この点検は飛ばします．')
        return
    try:
        tgt = target_columns()
    except Exception as e:
        print(f'!! このサイトのテーブル定義を読めませんでした: {e}')
        return
    sqls, notes = [], []
    for t, info in sorted(src.items()):
        if t not in tgt:
            sqls.append(info['create'].rstrip(';') + ';')
            continue
        prev = None
        for c in info['columns']:
            if c['name'] not in tgt[t]:
                pos = f' AFTER `{prev}`' if prev else ' FIRST'
                sqls.append(f'ALTER TABLE `{t}` ADD COLUMN {coldef(c)}{pos};')
            elif str(tgt[t][c['name']]).lower() != str(c['type']).lower():
                notes.append(f'{t}.{c["name"]}: 型が違います（このサイト {tgt[t][c["name"]]} ／ 写し元 {c["type"]}）')
            prev = c['name']
    for t in sorted(set(tgt) - set(src)):
        notes.append(f'{t}: このサイトにだけあるテーブルです（そのまま残します）')
    if sqls:
        print('次の SQL を MySQL コンソールに貼って実行してください．')
        print('-----ここから-----')
        for s in sqls:
            print(s)
        print('-----ここまで-----')
    else:
        print('列の不足はありません．SQL の作業は要りません．')
    for n in notes:
        print(f'（参考）{n}')


def config_report(zf, names):
    print('── 4. config.py の点検 ──')
    required, optional = set(), set()
    for n in names:
        if not n.endswith('.py'):
            continue
        txt = zf.read(n).decode('utf-8', 'replace')
        required |= set(re.findall(r'\bconfig\.([A-Z][A-Z0-9_]+)', txt))
        optional |= set(re.findall(r"getattr\(\s*config\s*,\s*['\"]([A-Z][A-Z0-9_]+)['\"]", txt))
    try:
        import config
    except Exception as e:
        print(f'!! config.py を読めませんでした: {e}')
        return
    miss_req = sorted(k for k in required if not hasattr(config, k))
    miss_opt = sorted(k for k in optional - required if not hasattr(config, k))
    if not miss_req and not miss_opt:
        print('足りない定数はありません．config.py の作業は要りません．')
    if miss_req:
        print('次の定数が config.py にありません（無いとエラーになります）．')
        for k in miss_req:
            print(f'   {k}')
    if miss_opt:
        print('次の定数は config.py にありません（無くても動きますが，その機能は使えません）．')
        for k in miss_opt:
            print(f'   {k}')


def _check_module(mod, zipped, missing):
    if mod.startswith('fujinp.') or mod == 'fujinp':
        p = mod.replace('.', '/')
        cand = [p + '.py', p + '/__init__.py']
        if not any(c in zipped or os.path.exists(os.path.join(HOME, c)) for c in cand):
            missing.add(mod)
        return
    top = mod.split('.')[0]
    if f'{top}.py' in zipped:
        return
    try:
        if importlib.util.find_spec(top) is None:
            missing.add(top)
    except Exception:
        missing.add(top)


def import_report(zf, names):
    zipped = set(names)
    missing = set()
    for n in names:
        if not n.endswith('.py'):
            continue
        for line in zf.read(n).decode('utf-8', 'replace').splitlines():
            m = re.match(r'\s*from\s+([A-Za-z_][\w\.]*)\s+import\b', line)
            if m:
                mods = [m.group(1)]
            else:
                m = re.match(r'\s*import\s+([A-Za-z_][\w\.]*(?:\s*,\s*[A-Za-z_][\w\.]*)*)', line)
                if not m:
                    continue
                mods = [x.strip() for x in m.group(1).split(',')]
            for mod in mods:
                _check_module(mod, zipped, missing)
    if missing:
        print('（注意）次のモジュールがこのサイトに見当たりません．表示をそのまま Claude に見せてください．')
        for m in sorted(missing):
            print(f'   {m}')


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    doit = '--yes' in sys.argv[1:]
    if not args:
        print(__doc__)
        return
    zpath = os.path.abspath(args[0])
    zf = zipfile.ZipFile(zpath)
    meta = json.loads(zf.read('_as_meta/manifest.json'))
    src_schema = json.loads(zf.read('_as_meta/schema.json'))
    names = [n for n in zf.namelist() if not n.startswith('_as_meta/') and not n.endswith('/')]
    dashes = set(meta.get('dashboards', []))

    print(f'写し元: {meta.get("source_home")}（荷造り {meta.get("packed_at")}）')
    print(f'このサイト: {HOME}')
    print('点検だけを行います（何も変えません）' if not doit else '実行します')
    print()

    print('── 1・2. ファイル ──')
    plan, skipped = [], []
    for n in names:
        dst = os.path.join(HOME, n)
        if n in dashes and not os.path.exists(dst):
            skipped.append(n)
            continue
        new = zf.read(n)
        old = open(dst, 'rb').read() if os.path.exists(dst) else None
        state = '新規' if old is None else ('同一' if old == new else '置換')
        plan.append((n, state))
    for label in ('置換', '新規'):
        items = [n for n, s in plan if s == label]
        print(f'{label} {len(items)} 件')
        for n in items:
            print(f'   {n}')
    print(f'同一 {sum(1 for _, s in plan if s == "同一")} 件（書き換え不要）')
    if skipped:
        print(f'写し元にだけある場所のテンプレート {len(skipped)} 件は使いません')
        for n in skipped:
            print(f'   {n}')
    extras = sorted(local_code_files(APP_DIR) - set(names))
    if extras:
        print(f'このサイトにだけあるファイル {len(extras)} 件（消さずに残します）')
        for n in extras:
            print(f'   {n}')
    import_report(zf, names)
    print()

    if doit:
        stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        bpath = os.path.join(HOME, f'as_backup_{stamp}.zip')
        replaced = [n for n, s in plan if s == '置換']
        created = [n for n, s in plan if s == '新規']
        with zipfile.ZipFile(bpath, 'w', zipfile.ZIP_DEFLATED) as bz:
            for n in replaced:
                bz.write(os.path.join(HOME, n), n)
            bz.writestr('_as_meta/created_files.txt', '\n'.join(created) + '\n')
        for n in replaced + created:
            dst = os.path.join(HOME, n)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, 'wb') as f:
                f.write(zf.read(n))
        print(f'控え: {bpath}')
        print(f'書き込み: 置換 {len(replaced)} 件，新規 {len(created)} 件')
        print()

    schema_report(src_schema)
    print()
    config_report(zf, names)
    print()
    if doit:
        print('ファイルの入れ替えは終わりました．手順書の次の段に進んでください．')
    else:
        print('点検は以上です．問題がなければ，同じ行の末尾に --yes を付けて実行してください．')


if __name__ == '__main__':
    main()
