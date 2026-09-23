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
app_share.kernel — カーネルの取り込み（2026-09-23）

カーネルパッケージ（⚙カーネルエクスポートの出力，export_type=fujinp_kernel_package）を
読み，ホーム直下のカーネルファイル一式を入れ替える．admin 専用
（routes.py の before_request が既定で admin 必須にする）．

  GET  /app_share/kernel/import     取り込み画面
  POST /app_share/kernel/check      検証（ファイルごとの新規／変更／同じ，注意点）
  POST /app_share/kernel/apply      適用（バックアップ → 書き込み．任意で Reload）

入れ替えは無条件で，パッケージにあるファイルを全部書く．例外は次の2つだけ．
  ・fujinp/app_registry.json は書かない（サイト固有の発行物．発行で作り直す）
  ・config.py は書かない（パッケージにも入っていない）
書き込みを止めるのは，.py の構文エラーとパッケージの形の不正だけ
（壊れたパッケージでサイトが起動しなくなるのを防ぐため）．

書き込む前に ~/kernel_backups/<日時>/ へ上書き対象の元ファイルを写し，
restore.py を置く．サイトが起動しなくなっても Bash コンソールから
  python3 ~/kernel_backups/<日時>/restore.py
で元に戻せる（新規に作られたファイルは消し，WSGI を touch して Reload する）．
"""

import os
import re
import json
import base64
import shutil
import datetime

from flask import render_template, request, session

from . import app_share_bp
from . import routes as _r
from . import manage as _m
from config import Config
from decorators import login_required
from fujinp import registry as _reg

KERNEL_TYPE = 'fujinp_kernel_package'
ROOT = _r.SITE_CODE_ROOT
APPS_DIR = _r.BASE_DIR
BACKUP_ROOT = os.path.join(ROOT, 'kernel_backups')

# 取り込まないファイル（理由つき）
NEVER_WRITE = {
    'fujinp/app_registry.json': 'サイト固有の発行物（取り込み後にアプシャの発行で作り直す）',
    'config.py': 'サイトの秘密情報（取り込まない）',
}
# カーネルとして書いてよい場所（ホーム直下のファイル，templates/，static_for_distribution/，
# fujinp/ 直下の __init__.py と registry.py）．パッケージの形の検査で，運用上の制約ではない
_FUJINP_FILES = ('fujinp/__init__.py', 'fujinp/registry.py')
_DIR_PREFIXES = ('templates/', 'static_for_distribution/')
_HOME_RE = re.compile(r'/home/([A-Za-z0-9_.\-]+)/')


def _now():
    return datetime.datetime.now(_r.JST)


def _safe_rel(rel):
    if not rel or rel.startswith('/') or '\\' in rel or '\x00' in rel:
        return False
    parts = rel.split('/')
    if any(p in ('', '.', '..') for p in parts):
        return False
    if len(parts) == 1:
        return not rel.startswith('.')
    if rel in _FUJINP_FILES:
        return True
    return rel.startswith(_DIR_PREFIXES) and '__pycache__' not in parts


def _file_bytes(entry):
    if entry.get('skipped') or entry.get('content') is None:
        return None
    if entry.get('encoding') == 'base64':
        try:
            return base64.b64decode(entry['content'])
        except Exception:
            return None
    return (entry.get('content') or '').encode('utf-8')


def _local_user():
    return os.path.basename(os.path.expanduser('~').rstrip('/'))


def _analyze(pkg):
    """パッケージを手元と比べる．書き込みの可否もここで決める．"""
    files = pkg.get('files') or []
    counts = {'new': 0, 'changed': 0, 'same': 0, 'excluded': 0, 'skipped': 0, 'invalid': 0}
    items, syntax_errors, site_notes = [], [], []
    me = _local_user()
    for f in files:
        rel = (f.get('path') or '').strip()
        item = {'path': rel, 'size': f.get('size')}
        if rel in NEVER_WRITE:
            item.update(status='excluded', reason=NEVER_WRITE[rel])
        elif not _safe_rel(rel):
            item.update(status='invalid', reason='カーネルの場所ではないパス')
        else:
            raw = _file_bytes(f)
            if raw is None:
                item.update(status='skipped', reason=f.get('reason') or '内容なし')
            else:
                lp = os.path.join(ROOT, rel)
                if not os.path.exists(lp):
                    item['status'] = 'new'
                else:
                    try:
                        with open(lp, 'rb') as fh:
                            item['status'] = 'same' if fh.read() == raw else 'changed'
                    except Exception:
                        item['status'] = 'changed'
                if rel.endswith('.py') and item['status'] != 'same':
                    try:
                        compile(raw.decode('utf-8'), rel, 'exec')
                    except SyntaxError as e:
                        syntax_errors.append({'path': rel, 'line': e.lineno, 'message': e.msg})
                    except Exception as e:
                        syntax_errors.append({'path': rel, 'line': None, 'message': str(e)})
                if f.get('encoding') != 'base64' and rel.endswith(('.py', '.html', '.json', '.txt', '.cfg', '.ini')):
                    for i, line in enumerate((f.get('content') or '').splitlines(), 1):
                        for m in _HOME_RE.finditer(line):
                            if m.group(1) != me and not line.lstrip().startswith('#'):
                                site_notes.append({'path': rel, 'line': i, 'user': m.group(1)})
        counts[item['status']] += 1
        items.append(item)
    # 設定：カーネルのコードが参照するのに，このサイトの config.py に無い名前
    missing_keys = [k for k in (pkg.get('config_keys_used') or []) if not hasattr(Config, k)]
    # app.py が登録する Blueprint の由来が手元にあるか（パッケージ内のファイルも数える）
    pkg_paths = {i['path'] for i in items}
    missing_apps = []
    for ra in pkg.get('required_apps') or []:
        mod = (ra or {}).get('module') or ''
        if not mod:
            continue
        parts = mod.split('.')
        if parts[0] == 'fujinp' and len(parts) > 1:
            ok = os.path.isdir(os.path.join(APPS_DIR, parts[1]))
        else:
            rel = parts[0] + '.py'
            ok = os.path.exists(os.path.join(ROOT, rel)) or rel in pkg_paths
        if not ok:
            missing_apps.append(ra)
    local_hash = None
    try:
        with open(os.path.join(ROOT, 'kernel_version.json'), encoding='utf-8') as fh:
            local_hash = (json.load(fh) or {}).get('version_id')
    except Exception:
        pass
    return {
        'counts': counts,
        'files': items,
        'syntax_errors': syntax_errors,
        'site_notes': site_notes[:50],
        'missing_config_keys': missing_keys,
        'missing_apps': missing_apps,
        'can_apply': not syntax_errors and (counts['new'] + counts['changed']) > 0,
        'package': {'site_name': pkg.get('site_name'), 'site_url': pkg.get('site_url'),
                    'generated_at': pkg.get('generated_at'), 'generated_by': pkg.get('generated_by'),
                    'version_id': pkg.get('version_id'), 'content_hash': pkg.get('content_hash'),
                    'public_download': bool(pkg.get('public_download'))},
        'local': {'site_name': _local_user(), 'version_id': local_hash},
    }


_RESTORE_PY = '''#!/usr/bin/env python3
# FUJIN-P カーネル取り込みの取り消し（アプシャが自動生成）
#   python3 {script}
# 上書きしたファイルを元に戻し，取り込みで新しく作られたファイルを消して，
# WSGI を touch して Web アプリを再起動する．
import os, json, shutil
HERE = os.path.dirname(os.path.abspath(__file__))
m = json.load(open(os.path.join(HERE, 'manifest.json'), encoding='utf-8'))
root = m['root']
for rel in m['overwritten']:
    dst = os.path.join(root, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(os.path.join(HERE, 'files', rel), dst)
    print('戻した:', rel)
for rel in m['created']:
    p = os.path.join(root, rel)
    if os.path.exists(p):
        os.remove(p)
        print('消した:', rel)
try:
    os.utime(m['wsgi'], None)
    print('Reload しました:', m['wsgi'])
except Exception as e:
    print('Reload できませんでした（Web タブから Reload してください）:', e)
'''


def _backup(targets):
    """上書き・新規作成するファイルを記録し，元ファイルを写す．戻り値はバックアップのディレクトリ"""
    stamp = _now().strftime('%Y%m%d_%H%M%S_%f')
    bdir = os.path.join(BACKUP_ROOT, stamp)
    os.makedirs(os.path.join(bdir, 'files'), exist_ok=True)
    overwritten, created = [], []
    for rel in targets:
        lp = os.path.join(ROOT, rel)
        if os.path.exists(lp):
            dst = os.path.join(bdir, 'files', rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(lp, dst)
            overwritten.append(rel)
        else:
            created.append(rel)
    manifest = {'root': ROOT, 'wsgi': _reg.wsgi_path(), 'taken_at': _now().strftime('%Y-%m-%d %H:%M:%S'),
                'overwritten': overwritten, 'created': created}
    with open(os.path.join(bdir, 'manifest.json'), 'w', encoding='utf-8') as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    script = os.path.join(bdir, 'restore.py')
    with open(script, 'w', encoding='utf-8') as fh:
        fh.write(_RESTORE_PY.format(script=script))
    return bdir, script


def _load_pkg():
    body = request.get_json(silent=True) or {}
    pkg = body.get('package') if isinstance(body.get('package'), dict) else body
    if not isinstance(pkg, dict) or pkg.get('export_type') != KERNEL_TYPE:
        return None, body
    return pkg, body


@app_share_bp.route('/kernel/import')
@login_required
def kernel_import_page():
    return render_template('app_share_kernel_import.html', local_site=_local_user())


@app_share_bp.route('/kernel/check', methods=['POST'])
@login_required
def kernel_check():
    pkg, _ = _load_pkg()
    if pkg is None:
        return _m._err('FUJIN-P のカーネルパッケージではありません（export_type）')
    return _m._ok(result=_analyze(pkg))


@app_share_bp.route('/kernel/apply', methods=['POST'])
@login_required
def kernel_apply():
    pkg, body = _load_pkg()
    if pkg is None:
        return _m._err('FUJIN-P のカーネルパッケージではありません（export_type）')
    res = _analyze(pkg)
    if res['syntax_errors']:
        return _m._err('構文エラーのある .py が含まれているため，何も書き込みませんでした', 400)
    by_path = {f.get('path'): f for f in pkg.get('files') or []}
    targets = [i['path'] for i in res['files'] if i['status'] in ('new', 'changed')]
    if not targets:
        return _m._ok(result={'written': 0, 'message': '手元と同じでした．書き込みはありません'})
    bdir, script = _backup(targets)
    written, errors = [], []
    for rel in targets:
        raw = _file_bytes(by_path[rel])
        lp = os.path.join(ROOT, rel)
        try:
            os.makedirs(os.path.dirname(lp), exist_ok=True)
            tmp = lp + '.kernel_tmp'
            with open(tmp, 'wb') as fh:
                fh.write(raw)
            os.replace(tmp, lp)
            written.append(rel)
        except Exception as e:
            errors.append(f'{rel}: {e}')
    result = {'written': len(written), 'written_files': written, 'errors': errors,
              'backup_dir': bdir, 'restore_command': f'python3 {script}',
              'missing_config_keys': res['missing_config_keys'], 'missing_apps': res['missing_apps']}
    if (body.get('options') or {}).get('reload'):
        result['reloaded'] = _reg.reload_site()
    return _m._ok(result=result)
