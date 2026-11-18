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
FUJIN-P 立ち上げスクリプト（2026-10-31）

元のサイトのアプシャで書き出した全アプリパッケージ（fujinp_apps_overview_<日時>.json）を読み，
その bootstrap 欄から，新品の PythonAnywhere にカーネルとアプシャまでを立ち上げる．
一般アプリ（apps[]）は，立ち上がったサイトのアプシャで同じ JSON から取り込む．

使い方（Bash コンソールで．JSON はホーム直下に置いておく）:

    python3 fujinp_bootstrap.py <JSON> --list      中身を見る（何も書かない）
    python3 fujinp_bootstrap.py <JSON> --files     ファイルを書き出す
    （ここで依存ライブラリを入れ，データベースを作り，config.py を書く）
    python3 fujinp_bootstrap.py <JSON> --schema    テーブルを作り，正本を登録して発行する
    python3 fujinp_bootstrap.py <JSON> --admin     最初の admin を登録する

--files が書くもの
    bootstrap.kernel    ホーム直下のコード，templates/，static_for_distribution/，fujinp/registry.py
                        （config.py と fujinp/app_registry.json は書かない．後者は --schema の発行で作る）
    bootstrap.admin     fujinp/admin/ 以下
    bootstrap.app_share fujinp/app_share/ 以下
    static_for_distribution/ の各ファイルを static/ の同じ位置にも写す

--schema が作るもの（既にあるテーブルや行には触らない．何度実行しても同じ結果になる）
    bootstrap.platform・admin・app_share の tables[] の DDL で，共通テーブルとアプシャの表
    アプシャの正本の3行（_platform・admin・app_share），その台帳・文書，区画と背景色
    最後に fujinp/app_registry.json を発行する

config.py はどこにも入っていない．config_template.py を写して書く．
"""

import base64
import datetime
import getpass
import json
import os
import re
import sys
import time

JST = datetime.timezone(datetime.timedelta(hours=9), 'JST')

KERNEL_NEVER_WRITE = {'config.py', 'fujinp/app_registry.json'}
STATIC_SRC = 'static_for_distribution/'
STATIC_DST = 'static/'
BOOT_ROWS = (('platform', '_platform'), ('admin', 'admin'), ('app_share', 'app_share'))
DB_TARGETS = ('default', 'fujinp', 'public')

SELF_NAME = os.path.basename(__file__)


# ------------------------------------------------------------------
# JSON
# ------------------------------------------------------------------

def load_overview(path):
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(path):
        sys.exit(f'{path} がありません．')
    with open(path, encoding='utf-8') as f:
        d = json.load(f)
    if d.get('export_type') != 'fujinp_apps_overview':
        sys.exit('全アプリパッケージ（fujinp_apps_overview）ではありません．')
    b = d.get('bootstrap')
    if not isinstance(b, dict) or not isinstance(b.get('kernel'), dict):
        sys.exit('この JSON には bootstrap 欄がありません．\n'
                 '2026-10-31 以降のアプシャで書き出した全アプリパッケージを使ってください．')
    for key, _ in BOOT_ROWS:
        if not isinstance(b.get(key), dict):
            sys.exit(f'bootstrap.{key} がありません（JSON が壊れているか，古い書き出しです）．')
    return d


def _bytes(entry):
    if entry.get('skipped') or entry.get('content') is None:
        return None
    if entry.get('encoding') == 'base64':
        return base64.b64decode(entry['content'])
    return entry['content'].encode('utf-8')


def _safe(rel):
    if not rel or rel.startswith('/') or '\\' in rel or '\x00' in rel:
        return False
    return all(p not in ('', '.', '..') for p in rel.split('/'))


def planned_files(d):
    """(書き出し先の相対パス, 中身のバイト列, 由来) の一覧"""
    b = d['bootstrap']
    out, skipped = [], []
    for f in b['kernel'].get('files') or []:
        rel = (f.get('path') or '').strip()
        if rel in KERNEL_NEVER_WRITE or not _safe(rel):
            continue
        raw = _bytes(f)
        if raw is None:
            skipped.append((rel, f.get('reason') or '内容なし'))
            continue
        out.append((rel, raw, 'kernel'))
        if rel.startswith(STATIC_SRC):
            out.append((STATIC_DST + rel[len(STATIC_SRC):], raw, 'static'))
    for key, app in (('admin', 'admin'), ('app_share', 'app_share')):
        for f in b[key].get('files') or []:
            rel = (f.get('path') or '').strip()
            if not _safe(rel):
                continue
            raw = _bytes(f)
            if raw is None:
                skipped.append((f'fujinp/{app}/{rel}', f.get('reason') or '内容なし'))
                continue
            out.append((f'fujinp/{app}/{rel}', raw, app))
    return out, skipped


# ------------------------------------------------------------------
# --list
# ------------------------------------------------------------------

def cmd_list(d):
    b = d['bootstrap']
    k = b['kernel']
    print(f"全アプリパッケージ：{d.get('site_url')}（{d.get('generated_at')} 書き出し，{d.get('generated_by')}）")
    print(f"  カーネル   版 {k.get('version_id') or '未確定'}・{k.get('file_count')} ファイル"
          f"{'（公開用の絞り込み版）' if k.get('public_download') else ''}")
    for key, app in BOOT_ROWS:
        p = b[key]
        print(f"  {app:<10} 版 {p.get('version_id') or '未確定'}・{len(p.get('files') or [])} ファイル・"
              f"{len(p.get('tables') or [])} 表")
    print(f"  一般アプリ {len(d.get('apps') or [])} 本（立ち上げのあと，アプシャで取り込む）")
    files, skipped = planned_files(d)
    total = sum(len(r) for _, r, _ in files)
    print(f'\n--files で書くファイル：{len(files)} 本・合計 {total:,} バイト')
    for rel, raw, src in files:
        print(f'  {len(raw):>9,}  {rel}' + ('   ← static_for_distribution/ の写し' if src == 'static' else ''))
    if skipped:
        print(f'\n中身が無いので書かないもの：{len(skipped)} 本')
        for rel, why in skipped:
            print(f'    {rel}（{why}）')
    print('\n--schema で作る表')
    for key, app in BOOT_ROWS:
        for t in b[key].get('tables') or []:
            print(f"    {t.get('db_target') or 'default':<8} {t['table_name']}（{app}）")
    print('\nconfig.py は入っていない．config_template.py を写して書く．')


# ------------------------------------------------------------------
# --files
# ------------------------------------------------------------------

def cmd_files(d, dest, force):
    files, skipped = planned_files(d)
    stamp = time.strftime('%Y%m%d_%H%M%S')
    backup = os.path.join(dest, f'fujinp_bootstrap_backup_{stamp}')
    written, same, kept, backed = [], [], [], []
    for rel, raw, _src in files:
        path = os.path.join(dest, rel)
        if os.path.exists(path):
            with open(path, 'rb') as fh:
                if fh.read() == raw:
                    same.append(rel)
                    continue
            if not force:
                kept.append(rel)
                continue
            bpath = os.path.join(backup, rel)
            os.makedirs(os.path.dirname(bpath), exist_ok=True)
            with open(path, 'rb') as f_old, open(bpath, 'wb') as f_bak:
                f_bak.write(f_old.read())
            backed.append(rel)
        os.makedirs(os.path.dirname(path) or dest, exist_ok=True)
        tmp = path + '.bootstrap_tmp'
        with open(tmp, 'wb') as fh:
            fh.write(raw)
        os.replace(tmp, path)
        written.append(rel)

    print(f'書き出し先: {dest}')
    print(f'書いたファイル: {len(written)}　同じだったファイル: {len(same)}')
    if backed:
        print(f'上書き前に退避: {len(backed)} → {backup}')
    if kept:
        print(f'中身が違うが触らなかったファイル: {len(kept)}')
        for n in kept[:20]:
            print(f'    {n}')
        if len(kept) > 20:
            print(f'    …ほか {len(kept) - 20} 件')
        print('  上書きするなら --files --force を付けて実行する（上書き前に退避する）．')
    if skipped:
        print(f'中身が無いので書かなかったもの: {len(skipped)}')
        for rel, why in skipped:
            print(f'    {rel}（{why}）')

    print()
    print('次にやること')
    print('  1. 依存ライブラリを入れる')
    print('       pip3.13 install --user -r requirements.txt   （数字は Web タブの Python の版）')
    print('  2. データベースを作る（PythonAnywhere の Databases タブ）')
    print('       <アカウント>$default ← 必須')
    print('       <アカウント>$fujinp・<アカウント>$public ← アプリを入れるとき')
    print('  3. config.py を書く')
    print('       cp config_template.py config.py  として中の <...> を埋める')
    print('       Google 認証と Slack は手引き「外部サービス設定」を読んでから')
    print('  4. テーブルを作り，正本を登録する')
    print(f'       python3 {SELF_NAME} <JSON> --schema')


# ------------------------------------------------------------------
# --schema
# ------------------------------------------------------------------

_CREATE_RE = re.compile(r'^\s*CREATE\s+TABLE\s+(?!IF\s+NOT\s+EXISTS)', re.I)
_AUTOINC_RE = re.compile(r'\s+AUTO_INCREMENT=\d+', re.I)


def ddl_if_not_exists(ddl):
    ddl = _CREATE_RE.sub('CREATE TABLE IF NOT EXISTS ', ddl.strip().rstrip(';'), count=1)
    return _AUTOINC_RE.sub('', ddl)


def _j(v):
    return None if v is None else json.dumps(v, ensure_ascii=False)


def _ts(v):
    if not v:
        return None
    try:
        return datetime.datetime.strptime(str(v)[:19], '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None


def _now():
    return datetime.datetime.now(JST).replace(tzinfo=None)


def _registry_meta(d):
    """元のサイトの正本（カーネルの fujinp/app_registry.json）から並び順・有効・公開を引く"""
    meta = {}
    for f in d['bootstrap']['kernel'].get('files') or []:
        if f.get('path') == 'fujinp/app_registry.json' and f.get('content'):
            try:
                for a in json.loads(f['content']).get('apps') or []:
                    meta[a.get('app_name')] = a
            except Exception:
                pass
    return meta


def plan_schema(d):
    """[(db_target, 説明, SQL, 引数)] の一覧を作る"""
    b = d['bootstrap']
    meta = _registry_meta(d)
    steps = []
    # 1) テーブル
    for key, app in BOOT_ROWS:
        for t in b[key].get('tables') or []:
            if not t.get('ddl'):
                continue
            steps.append((t.get('db_target') or 'default', f"表 {t['table_name']}",
                          ddl_if_not_exists(t['ddl']), None))
    # 2) 正本の3行
    defaults = {'_platform': 10.0, 'admin': 20.0, 'app_share': 30.0}
    for key, app in BOOT_ROWS:
        p = b[key]
        reg = p.get('registry') or {}
        m = meta.get(app) or {}
        steps.append(('default', f'正本 {app}',
                      """INSERT INTO app_share_registry
                         (app_name, display_name, icon, description, sort_order, kind, enabled, disclosed,
                          blueprints, launchers, libraries, config_keys,
                          version_id, version_confirmed_at, version_confirmed_by)
                         VALUES (%s,%s,%s,%s,%s,'kernel',%s,%s,%s,%s,%s,%s,%s,%s,%s)
                         ON DUPLICATE KEY UPDATE app_name=app_name""",
                      (app, p.get('display_name') or app, p.get('icon') or '📦', p.get('description') or '',
                       float(m.get('sort_order') if m.get('sort_order') is not None else defaults[app]),
                       1 if m.get('enabled', True) else 0, 1 if m.get('disclosed', True) else 0,
                       _j(reg.get('blueprints') or []), _j(reg.get('launchers') or []),
                       _j(reg.get('libraries') or []), _j(reg.get('config_keys') or []),
                       p.get('version_id'), _ts(p.get('version_confirmed_at')),
                       p.get('version_confirmed_by'))))
        # 台帳
        for i, t in enumerate(p.get('tables') or []):
            steps.append(('default', f"台帳 {app}.{t['table_name']}",
                          """INSERT INTO app_share_tables
                             (app_name, table_name, db_target, ddl, captured_at, status, note, sort_order)
                             VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                             ON DUPLICATE KEY UPDATE app_name=app_name""",
                          (app, t['table_name'], t.get('db_target') or 'default', t.get('ddl'), _now(),
                           t.get('status') or 'confirmed', t.get('note'), int(t.get('sort_order') or i))))
        # 文書
        for doc_type, doc in (p.get('documents') or {}).items():
            if doc_type not in ('manual', 'spec') or not isinstance(doc, dict):
                continue
            steps.append(('default', f'文書 {app}.{doc_type}',
                          """INSERT INTO app_share_documents (app_name, doc_type, title, content, code_fingerprint)
                             VALUES (%s,%s,%s,%s,%s)
                             ON DUPLICATE KEY UPDATE app_name=app_name""",
                          (app, doc_type, doc.get('title') or '', doc.get('content') or '',
                           _j(doc.get('code_fingerprint')))))
    # 3) 区画と背景色（3つのパッケージの site_style を合わせる）
    sections, dashboards = {}, {}
    for key, _app in BOOT_ROWS:
        ss = b[key].get('site_style') or {}
        for s in ss.get('sections') or []:
            if s.get('key'):
                sections.setdefault(s['key'], s)
        for name, bg in (ss.get('dashboards') or {}).items():
            dashboards.setdefault(name, bg)
    for k, s in sections.items():
        steps.append(('default', f'区画 {k}',
                      """INSERT INTO app_share_sections
                         (section_key, title, css_class, color_from, color_to, text_color, sort_order,
                          show_admin, show_guest, require_groups, require_categories)
                         VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                         ON DUPLICATE KEY UPDATE section_key=section_key""",
                      (k, s.get('title') or k, s.get('css_class') or '', s.get('color_from') or '',
                       s.get('color_to') or '', s.get('text_color') or '', float(s.get('sort_order') or 0),
                       1 if s.get('show_admin', True) else 0, 1 if s.get('show_guest', True) else 0,
                       _j(s.get('require_groups') or []), _j(s.get('require_categories') or []))))
    for name, bg in dashboards.items():
        steps.append(('default', f'背景色 {name}',
                      """INSERT INTO app_share_dashboards (dashboard, bg_from, bg_to)
                         VALUES (%s,%s,%s) ON DUPLICATE KEY UPDATE dashboard=dashboard""",
                      (name, (bg or {}).get('bg_from') or '', (bg or {}).get('bg_to') or '')))
    return steps


def _import_site(dest):
    if dest not in sys.path:
        sys.path.insert(0, dest)
    if not os.path.exists(os.path.join(dest, 'config.py')):
        sys.exit(f'{dest}/config.py がありません．config_template.py を写して書いてから実行してください．')
    try:
        import mysql.connector  # noqa: F401
    except ImportError:
        sys.exit('mysql-connector-python が入っていません．\n'
                 '  pip3.13 install --user -r requirements.txt  を先に実行してください．')
    try:
        from config import Config
        from db import DatabaseConfig
    except Exception as e:
        sys.exit(f'config.py / db.py を読めませんでした: {e}\n先に --files を実行しましたか．')
    return Config, DatabaseConfig


def _connect(DatabaseConfig, target):
    import mysql.connector
    getter = getattr(DatabaseConfig, target, None)
    if getter is None:
        raise RuntimeError(f'db.py に DatabaseConfig.{target}() がありません')
    return mysql.connector.connect(**getter())


def cmd_schema(d, dest, dry_run):
    steps = plan_schema(d)
    if dry_run:
        for target, label, sql, args in steps:
            print(f'-- [{target}] {label}')
            print(sql if args is None else ' '.join(sql.split()) + f'   -- 引数 {len(args)} 個')
            print()
        print(f'{len(steps)} 文（実行していません）')
        return
    Config, DatabaseConfig = _import_site(dest)
    conns, before = {}, {}
    ok, fail = 0, []
    try:
        for target in sorted({s[0] for s in steps}):
            try:
                conns[target] = _connect(DatabaseConfig, target)
            except Exception as e:
                sys.exit(f'データベース {target} に接続できません: {e}\n'
                         'config.py の DB_HOST / DB_USER / DB_PASSWORD / DB_ACCOUNT と，\n'
                         'Databases タブでそのデータベースが作ってあるかを確かめてください．')
            cur = conns[target].cursor()
            cur.execute('SHOW TABLES')
            before[target] = {r[0] for r in cur.fetchall()}
            cur.execute('SET FOREIGN_KEY_CHECKS=0')
            cur.close()
        for target, label, sql, args in steps:
            cur = conns[target].cursor()
            try:
                cur.execute(sql, args) if args is not None else cur.execute(sql)
                ok += 1
            except Exception as e:
                # MySQL 8 の照合順序を知らないサーバ（MariaDB など）では，互換の照合順序で作り直す
                if args is None and 'utf8mb4_0900_ai_ci' in sql and 'collation' in str(e).lower():
                    try:
                        cur.execute(sql.replace('utf8mb4_0900_ai_ci', 'utf8mb4_unicode_ci'))
                        ok += 1
                        continue
                    except Exception as e2:
                        e = e2
                fail.append((label, str(e)))
            finally:
                cur.close()
        for target, conn in conns.items():
            cur = conn.cursor()
            cur.execute('SET FOREIGN_KEY_CHECKS=1')
            cur.close()
            conn.commit()

        print(f'実行した文: {ok} / {len(steps)}')
        for target, conn in conns.items():
            cur = conn.cursor()
            cur.execute('SHOW TABLES')
            after = {r[0] for r in cur.fetchall()}
            cur.close()
            created = sorted(after - before[target])
            print(f'[{target}] 新しく作られたテーブル: {len(created)}'
                  + (f'（既にあった {len(before[target] & after)} 表には触っていません）' if before[target] else ''))
            for t in created:
                print(f'    {t}')
        if fail:
            print(f'\n失敗した文: {len(fail)}')
            for label, err in fail:
                print(f'    {label}: {err}')

        # 正本（app_registry.json）を発行する
        try:
            from fujinp import registry as _reg
            dcur = conns['default'].cursor(dictionary=True)
            data = _reg.publish(dcur)
            conns['default'].commit()
            dcur.close()
            print(f"\n正本を発行しました: {_reg.REGISTRY_FILE}")
            print(f"    アプリ {len(data.get('apps') or [])} 件・区画 {len(data.get('sections') or [])} 件")
        except Exception as e:
            print(f'\n正本の発行に失敗しました: {e}')
            print('    fujinp/registry.py が置かれているか，app_share_registry に3行が入ったかを確かめてください．')
    finally:
        for conn in conns.values():
            try:
                conn.close()
            except Exception:
                pass

    print()
    print('次にやること')
    print('  5. WSGI ファイルを設定して Reload する（Web タブ）')
    print('  6. 最初の admin を登録する')
    print(f'       python3 {SELF_NAME} <JSON> --admin')
    print('  7. ブラウザでサイトを開き，そのメールアドレスとパスワードでログインする')
    print('       管理者ダッシュボードにアプシャのカードが出れば，カーネルの立ち上げは終わりです．')
    print('       一般アプリは，アプシャの「新規アプリ取り込み」で同じ JSON から取り込みます．')


# ------------------------------------------------------------------
# --admin
# ------------------------------------------------------------------

def _ask(label):
    while True:
        v = input(f'{label}: ').strip()
        if v:
            return v
        print('  入力してください．')


def cmd_admin(dest):
    Config, DatabaseConfig = _import_site(dest)
    try:
        from werkzeug.security import generate_password_hash
    except ImportError:
        sys.exit('werkzeug が入っていません．先に  pip3.13 install --user -r requirements.txt  を実行してください．')
    print('最初の admin を登録します（users テーブルに1行入れます）．\n')
    email = _ask('メールアドレス')
    full_name = _ask('氏名')
    while True:
        pw1 = getpass.getpass('パスワード（8文字以上・画面には出ません）: ')
        if len(pw1) < 8:
            print('  8文字以上にしてください．')
            continue
        if pw1 != getpass.getpass('もう一度: '):
            print('  一致しません．もう一度入力してください．')
            continue
        break
    conn = _connect(DatabaseConfig, 'default')
    try:
        cur = conn.cursor()
        cur.execute('SELECT id, category FROM users WHERE email=%s', (email,))
        row = cur.fetchone()
        if row:
            sys.exit(f'{email} は既に登録されています（id={row[0]}，区分={row[1]}）．何も変えていません．')
        now = _now()
        cur.execute("""INSERT INTO users (email, password_hash, full_name, category, is_active, created_at, updated_at)
                       VALUES (%s,%s,%s,'admin',1,%s,%s)""",
                    (email, generate_password_hash(pw1), full_name, now, now))
        conn.commit()
        print(f'\n登録しました（id={cur.lastrowid}）．このメールアドレスとパスワードでログインできます．')
    finally:
        conn.close()


# ------------------------------------------------------------------

USAGE = f"""FUJIN-P 立ち上げスクリプト

  python3 {SELF_NAME} <JSON> --list               中身を見る（何も書かない）
  python3 {SELF_NAME} <JSON> --files [--force]    ファイルを書き出す
  python3 {SELF_NAME} <JSON> --schema [--dry-run] テーブルを作り，正本を登録して発行する
  python3 {SELF_NAME} <JSON> --admin              最初の admin を登録する

  <JSON>      元のサイトのアプシャで書き出した fujinp_apps_overview_<日時>.json
  --dest DIR  書き出し先（既定はホームディレクトリ）
  --force     中身が違う既存のファイルも上書きする（上書き前に退避する）
  --dry-run   実行する SQL を表示するだけで，データベースには触らない
"""


def main(argv):
    args = list(argv[1:])
    if not args or '-h' in args or '--help' in args:
        print(USAGE)
        return 0
    dest = os.path.expanduser('~')
    if '--dest' in args:
        i = args.index('--dest')
        if i + 1 >= len(args):
            sys.exit('--dest の後に書き出し先を書いてください．')
        dest = args[i + 1]
        del args[i:i + 2]
    dest = os.path.abspath(os.path.expanduser(dest))
    force = '--force' in args
    dry = '--dry-run' in args
    cmds = [a for a in args if a in ('--list', '--files', '--schema', '--admin')]
    paths = [a for a in args if not a.startswith('--')]
    if len(cmds) != 1 or len(paths) != 1:
        print(USAGE)
        return 2
    if cmds[0] == '--admin':
        cmd_admin(dest)
        return 0
    d = load_overview(paths[0])
    if cmds[0] == '--list':
        cmd_list(d)
    elif cmds[0] == '--files':
        cmd_files(d, dest, force)
    elif cmds[0] == '--schema':
        cmd_schema(d, dest, dry)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
