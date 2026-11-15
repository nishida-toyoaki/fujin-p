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
app_share.gitsync — 関所（ローカル git リポジトリ）への写しと GitHub 連携（段階6b）

稼働ツリー → 関所（既定 ~/fujin-p-repo）→ GitHub の二段．
  写し：アプリの許可リスト内ファイルを稼働側で整形（tidy）してから関所へコピー，消えたものは git rm
  commit：アプリ単位・パス限定（他アプリの未整理な変更は巻き込まない）
  push：GITHUB_TOKEN（config.py）があればアプシャが実行．ボタンは必ず人が押す

config.py（任意）
  FUJINP_REPO_DIR   関所のパス（既定 ~/fujin-p-repo）
  GITHUB_TOKEN      push 用トークン（fine-grained PAT・Contents: Read and write）
  GIT_AUTHOR_NAME / GIT_AUTHOR_EMAIL  commit の作者（既定はリポジトリ設定，無ければ FUJIN-P app_share）

API（admin）
  GET  /app_share/api/git/status                 関所の状態（ブランチ・未 push・対象外の変更）
  GET  /app_share/api/git/app/<app_name>/diff    稼働ツリーと関所の差（要 commit 判定）
  POST /app_share/api/git/app/<app_name>/commit  写し＋commit
  POST /app_share/api/git/push                   push（確認済みの前提）
"""

import os
import re
import json
import hashlib
import datetime
import subprocess
from urllib.parse import urlsplit, urlunsplit

from flask import request, jsonify, session

from . import app_share_bp
from . import manage as _m
from . import tidy as _t
from config import Config
from db import DatabaseConfig
from decorators import login_required

BASE_DIR = _m.BASE_DIR
SITE_CODE_ROOT = _m.SITE_CODE_ROOT
JST = _m.JST

TEXT_EXTS = ('.py', '.html', '.htm', '.js', '.css', '.md', '.txt', '.json', '.sql', '.yml', '.yaml',
             '.csv', '.cfg', '.ini', '.svg')
ALWAYS_DIRS = ('templates', 'data_for_distribution')
ROOT_EXTS = ('.sql', '.json', '.md', '.txt')
# 正本へ移った情報のファイル．関所には載せない（6d で稼働側からも消す）
ROOT_EXCLUDE_FILES = {'app_info.json', 'version.json', 'manifest.json'}
EXCLUDE_DIRS = {'static', '__pycache__', 'import_staging', 'import_backups', '.git'}
MAX_FILE = 5 * 1024 * 1024

# fujinp/ の下でカーネルが受け持つファイル（アプリの commit でも一緒に写す）．
# app_registry.json はサイトごとの発行物で，発行のたびに書き直される（generated_at も変わる）ので，
# 関所の対象にしない（2026-10-31．発行のたびに「要commit」が出る擬陽性をなくす）
KERNEL_FUJINP_FILES = ('__init__.py', 'registry.py')
# 関所から外す（追跡をやめる）ファイル．次のカーネルの commit で git rm する
KERNEL_UNTRACK = ('fujinp/app_registry.json',)
# カーネルの対象にするホーム直下のディレクトリ（配下を全部）
KERNEL_DIRS = ('templates',)
# ホーム直下で対象にしないファイル（秘密情報・関所の設定）
KERNEL_ROOT_SKIP = {'config.py', '.gitignore'}
KERNEL_ROOT_SKIP_PREFIXES = ('client_secret', 'credentials', 'service_account')
PLATFORM_ROW = _m.PLATFORM_ROW


# ============================================================
# 設定・git 実行
# ============================================================

def repo_dir():
    d = getattr(Config, 'FUJINP_REPO_DIR', None) or os.path.join(os.path.expanduser('~'), 'fujin-p-repo')
    return d


def _git(args, timeout=120, env_extra=None):
    env = dict(os.environ)
    env['GIT_TERMINAL_PROMPT'] = '0'
    env['LANG'] = 'C.UTF-8'
    if env_extra:
        env.update(env_extra)
    p = subprocess.run(['git'] + list(args), cwd=repo_dir(), capture_output=True, text=True,
                       timeout=timeout, env=env)
    return p.returncode, p.stdout, p.stderr


def _mask_token(s, token):
    return s.replace(token, '<token>') if token and s else s


def _repo_ok():
    d = repo_dir()
    return os.path.isdir(os.path.join(d, '.git'))


def _author_env():
    name = getattr(Config, 'GIT_AUTHOR_NAME', None)
    email = getattr(Config, 'GIT_AUTHOR_EMAIL', None)
    rc, out, _ = _git(['config', 'user.name'])
    has_name = rc == 0 and out.strip()
    rc, out, _ = _git(['config', 'user.email'])
    has_email = rc == 0 and out.strip()
    env = {}
    if not has_name or name:
        n = name or 'FUJIN-P app_share'
        env['GIT_AUTHOR_NAME'] = n
        env['GIT_COMMITTER_NAME'] = n
    if not has_email or email:
        e = email or 'app_share@fujin-p.invalid'
        env['GIT_AUTHOR_EMAIL'] = e
        env['GIT_COMMITTER_EMAIL'] = e
    return env


# ============================================================
# 写しの対象（許可リスト）
# ============================================================

def _is_text(path):
    return path.lower().endswith(TEXT_EXTS)


def _norm_bytes(path, raw):
    """関所へ写すときの正規化．整形の対象は整形と同じ規則（LF・BOM除去・末尾改行），
    それ以外のテキスト（CSV など）は CRLF→LF だけ．バイナリはそのまま"""
    if _t.is_tidy_target(path):
        return _t.normalize_bytes(path, raw)
    if _is_text(path):
        return raw.replace(b'\r\n', b'\n')
    return raw


def _sha(raw):
    return hashlib.sha1(raw).hexdigest()


def _allowed_app_file(rel):
    """アプリ配下の相対パスが許可リスト内か（パッケージと同じ規則）"""
    parts = rel.split('/')
    top = parts[0] if len(parts) > 1 else ''
    fn = parts[-1]
    if fn.endswith('.pyc'):
        return False
    if not top and fn in ROOT_EXCLUDE_FILES:
        return False
    if fn.endswith('.py'):
        return True
    if top in ALWAYS_DIRS:
        return True
    if not top and fn.endswith(ROOT_EXTS):
        return True
    return False


def _site_app_files(app_name):
    """稼働ツリーのアプリ配下で写す対象 {rel: abs_path}"""
    app_dir = os.path.join(BASE_DIR, app_name)
    out = {}
    if not os.path.isdir(app_dir):
        return out
    for root, dirs, files in os.walk(app_dir):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith('.')]
        for fn in files:
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, app_dir).replace(os.sep, '/')
            if _allowed_app_file(rel):
                try:
                    if os.path.getsize(p) <= MAX_FILE:
                        out[rel] = p
                except OSError:
                    pass
    return out


def _repo_tracked(prefix):
    """関所で追跡中の，prefix（例 'fujinp/my_md_notes/'）配下の相対パス集合（関所ルート基準）"""
    rc, out, err = _git(['ls-files', '--', prefix.rstrip('/')])
    if rc != 0:
        return set()
    return set(l.strip() for l in out.splitlines() if l.strip())


def _kernel_site_files():
    """カーネルの写しの対象 {関所ルート基準の相対パス: 稼働側の絶対パス}（2026-10-31）．
      ・ホーム直下の .py 全部（config.py，ドットで始まる名前，資格情報ファイルの名前を除く）．
        カーネルの版のハッシュ・カーネルの書き出しと同じ範囲で，新しく足した .py も漏れない
      ・ホーム直下の .py 以外は，関所で追跡中のものだけ（README.md・requirements.txt など）．
        一度きりの .sql や作業メモの .txt が勝手に GitHub に載らないようにする
      ・templates/ の配下全部（__pycache__・ドットで始まる名前・.pyc を除く）
      ・fujinp/ の __init__.py と registry.py（app_registry.json は対象外）"""
    tracked_root = {t for t in _repo_tracked('.') if '/' not in t}
    site = {}
    for name in os.listdir(SITE_CODE_ROOT):
        p = os.path.join(SITE_CODE_ROOT, name)
        if not os.path.isfile(p):
            continue
        if name.startswith('.') or name in KERNEL_ROOT_SKIP or name.startswith(KERNEL_ROOT_SKIP_PREFIXES):
            continue
        if not (name.endswith('.py') or name in tracked_root):
            continue
        try:
            if os.path.getsize(p) <= MAX_FILE:
                site[name] = p
        except OSError:
            pass
    for d in KERNEL_DIRS:
        droot = os.path.join(SITE_CODE_ROOT, d)
        if not os.path.isdir(droot):
            continue
        for root, dirs, files in os.walk(droot):
            dirs[:] = [x for x in dirs if x != '__pycache__' and not x.startswith('.')]
            for fn in files:
                if fn.startswith('.') or fn.endswith('.pyc'):
                    continue
                p = os.path.join(root, fn)
                try:
                    if os.path.getsize(p) <= MAX_FILE:
                        site[os.path.relpath(p, SITE_CODE_ROOT).replace(os.sep, '/')] = p
                except OSError:
                    pass
    for kf in KERNEL_FUJINP_FILES:
        p = os.path.join(BASE_DIR, kf)
        if os.path.isfile(p):
            site['fujinp/' + kf] = p
    return site


def _kernel_removed(site):
    """関所で追跡中だが，カーネルとしてはもう写さないもの（commit で git rm する）．
      ・KERNEL_UNTRACK（app_registry.json）
      ・templates/ と fujinp/ のカーネル2件のうち，稼働側から消えたもの
    ホーム直下の追跡ファイルは，稼働側に無くても消さない（LICENSE など関所にだけ置くものがあるため）"""
    out = []
    for t in sorted(_repo_tracked('.')):
        if t in site:
            continue
        if t in KERNEL_UNTRACK:
            out.append(t)
            continue
        top = t.split('/')[0] if '/' in t else ''
        in_scope = top in KERNEL_DIRS or t in tuple('fujinp/' + kf for kf in KERNEL_FUJINP_FILES)
        if in_scope and not os.path.exists(os.path.join(SITE_CODE_ROOT, t)):
            out.append(t)
    return out


def _scope_for(app_name):
    """(稼働側 {rel: abs}, 関所側 prefix, 関所側の相対パスに変換する関数)"""
    if app_name == PLATFORM_ROW:
        return _kernel_site_files(), None, (lambda rel: rel)
    site = _site_app_files(app_name)
    prefix = f'fujinp/{app_name}/'
    return site, prefix, (lambda rel: prefix + rel)


def _diff_for(app_name):
    """稼働ツリーと関所の差．戻り値 dict(changed, added, deleted, same, last_change, needs_commit)"""
    site, prefix, to_repo = _scope_for(app_name)
    rd = repo_dir()
    changed, added, same, deleted = [], [], [], []
    last = None
    for rel, p in sorted(site.items()):
        rp = os.path.join(rd, to_repo(rel))
        try:
            raw = _norm_bytes(rel, open(p, 'rb').read())
            mt = datetime.datetime.fromtimestamp(os.path.getmtime(p), JST).replace(tzinfo=None)
        except Exception:
            continue
        if not os.path.isfile(rp):
            added.append(rel)
            last = max(last, mt) if last else mt
            continue
        try:
            rraw = _norm_bytes(rel, open(rp, 'rb').read())
        except Exception:
            rraw = b''
        if _sha(raw) != _sha(rraw):
            changed.append(rel)
            last = max(last, mt) if last else mt
        else:
            same.append(rel)
    outside = []
    if app_name == PLATFORM_ROW:
        deleted = _kernel_removed(site)
    if prefix:
        tracked = _repo_tracked(prefix)
        site_repo_paths = set(to_repo(r) for r in site)
        app_dir = os.path.join(BASE_DIR, app_name)
        for t in sorted(tracked):
            if t in site_repo_paths:
                continue
            rel = t[len(prefix):]
            fn = rel.split('/')[-1]
            if (not os.path.isfile(os.path.join(app_dir, rel))) or ('/' not in rel and fn in ROOT_EXCLUDE_FILES):
                deleted.append(rel)          # 稼働側に無い（または正本へ移ったファイル）→ git rm
            else:
                outside.append(rel)          # 稼働側にはあるが写しの対象外（関所には残す）
    # 常に一緒に写すカーネル2件（アプリの commit でも同梱）
    kernel_extra = []
    if app_name != PLATFORM_ROW:
        for kf in KERNEL_FUJINP_FILES:
            p = os.path.join(BASE_DIR, kf)
            rp = os.path.join(rd, 'fujinp', kf)
            if os.path.isfile(p):
                a = _norm_bytes(kf, open(p, 'rb').read())
                b = _norm_bytes(kf, open(rp, 'rb').read()) if os.path.isfile(rp) else b''
                if _sha(a) != _sha(b):
                    kernel_extra.append('fujinp/' + kf)
    return {
        'app_name': app_name,
        'changed': changed, 'added': added, 'deleted': deleted, 'outside': outside, 'same_count': len(same),
        'kernel_extra': kernel_extra,
        'last_change': last.strftime('%Y-%m-%d %H:%M') if last else None,
        'needs_commit': bool(changed or added or deleted),
    }


def _copy_scope(app_name):
    """稼働ツリー → 関所 へ写す（LF 正規化）．消えたものは git rm．
    戻り値 (written, removed, paths_for_add)"""
    site, prefix, to_repo = _scope_for(app_name)
    rd = repo_dir()
    written = []
    for rel, p in site.items():
        raw = _norm_bytes(rel, open(p, 'rb').read())
        rp = os.path.join(rd, to_repo(rel))
        os.makedirs(os.path.dirname(rp), exist_ok=True)
        cur = open(rp, 'rb').read() if os.path.isfile(rp) else None
        if cur != raw:
            with open(rp, 'wb') as f:
                f.write(raw)
            written.append(to_repo(rel))
    removed = []
    if app_name == PLATFORM_ROW:
        for t in _kernel_removed(site):
            _git(['rm', '-q', '--cached', '--', t])
            try:
                os.remove(os.path.join(rd, t))
            except OSError:
                pass
            removed.append(t)
    if prefix:
        for rel in _diff_for(app_name)['deleted']:
            t = prefix + rel
            _git(['rm', '-q', '--cached', '--', t])
            try:
                os.remove(os.path.join(rd, t))
            except OSError:
                pass
            removed.append(t)
    add_paths = [prefix.rstrip('/')] if prefix else sorted(set(to_repo(r) for r in site))
    if app_name != PLATFORM_ROW:
        for kf in KERNEL_FUJINP_FILES:
            p = os.path.join(BASE_DIR, kf)
            if os.path.isfile(p):
                raw = _norm_bytes(kf, open(p, 'rb').read())
                rp = os.path.join(rd, 'fujinp', kf)
                os.makedirs(os.path.dirname(rp), exist_ok=True)
                cur = open(rp, 'rb').read() if os.path.isfile(rp) else None
                if cur != raw:
                    with open(rp, 'wb') as f:
                        f.write(raw)
                    written.append('fujinp/' + kf)
                add_paths.append('fujinp/' + kf)
    return written, removed, add_paths


# ============================================================
# API
# ============================================================

@app_share_bp.route('/api/git/status')
@login_required
def api_git_status():
    if not _repo_ok():
        return _m._ok(ok=False, repo=repo_dir(), error='関所（git リポジトリ）が見つかりません')
    rc, branch, _ = _git(['rev-parse', '--abbrev-ref', 'HEAD'])
    rc2, remote, _ = _git(['remote', 'get-url', 'origin'])
    remote = remote.strip()
    token = getattr(Config, 'GITHUB_TOKEN', None) or ''
    remote_shown = _mask_token(remote, token)
    if re.search(r'://[^/@]+@', remote_shown):
        remote_shown = re.sub(r'://[^/@]+@', '://<credential>@', remote_shown)
    rc3, ahead, _ = _git(['rev-list', '--count', 'origin/' + branch.strip() + '..HEAD'])
    rc4, unpushed, _ = _git(['log', '--oneline', '--no-decorate', 'origin/' + branch.strip() + '..HEAD'])
    rc5, st, _ = _git(['status', '--porcelain'])
    dirty = [l for l in st.splitlines() if l.strip()]
    rc6, head, _ = _git(['log', '-1', '--format=%h %ci %s'])
    return _m._ok(ok=True, repo=repo_dir(), branch=branch.strip(), remote=remote_shown,
                  ahead=int(ahead.strip()) if rc3 == 0 and ahead.strip().isdigit() else None,
                  unpushed=unpushed.splitlines() if rc4 == 0 else [],
                  dirty_count=len(dirty), dirty=dirty[:40],
                  head=head.strip() if rc6 == 0 else None,
                  token_configured=bool(token))


@app_share_bp.route('/api/git/app/<app_name>/diff')
@login_required
def api_git_diff(app_name):
    if not _m._valid_app(app_name):
        return _m._err('アプリ名が不正です')
    if not _repo_ok():
        return _m._err('関所（git リポジトリ）が見つかりません: ' + repo_dir(), 500)
    return _m._ok(diff=_diff_for(app_name))


@app_share_bp.route('/api/git/app/<app_name>/commit', methods=['POST'])
@login_required
def api_git_commit(app_name):
    if not _m._valid_app(app_name):
        return _m._err('アプリ名が不正です')
    if not _repo_ok():
        return _m._err('関所（git リポジトリ）が見つかりません: ' + repo_dir(), 500)
    d = request.get_json(silent=True) or {}
    with _m._db() as (cur, conn):
        row = _m._load_registry_row(cur, app_name)
        if not row:
            return _m._err('レジストリにありません', 404)
        cur.execute("SELECT id, title FROM app_share_issues WHERE app_name=%s AND status='open' ORDER BY id",
                    (app_name,))
        open_issues = cur.fetchall()
    version_id = row.get('version_id') or '未確定'
    tidied = _t.tidy_app(app_name)          # 写す前に稼働側を整形する（2026-09-17）
    written, removed, add_paths = _copy_scope(app_name)
    steps = []
    if tidied['changed']:
        steps.append({'cmd': '（アプシャの整形）', 'rc': 0,
                      'out': '整形したファイル: ' + ', '.join(tidied['changed'])})
    rc, out, err = _git(['add', '-A', '--'] + add_paths)
    steps.append({'cmd': 'git add -A -- ' + ' '.join(add_paths), 'rc': rc, 'out': (out + err).strip()})
    if rc != 0:
        return _m._ok(committed=False, steps=steps, written=written, removed=removed,
                      error='git add に失敗')
    # カーネルで git rm したものは add の対象外（もう無い）なので，差の確認と commit のパスに足す
    commit_paths = list(add_paths)
    if app_name == PLATFORM_ROW:
        commit_paths += [r for r in removed if r not in commit_paths]
    rc, staged, _ = _git(['diff', '--cached', '--name-status', '--'] + commit_paths)
    staged_lines = [l for l in staged.splitlines() if l.strip()]
    if not staged_lines:
        return _m._ok(committed=False, steps=steps, written=written, removed=removed,
                      message='関所と一致しています（commit するものがありません）')
    title = (d.get('message') or '').strip() or f'{app_name} {version_id}'
    body = ''
    if open_issues:
        body = 'Known issues (open):\n' + '\n'.join(f'- #{i["id"]} {i["title"]}' for i in open_issues)
    args = ['commit', '-q', '-m', title]
    if body:
        args += ['-m', body]
    args += ['--'] + commit_paths
    rc, out, err = _git(args, env_extra=_author_env())
    steps.append({'cmd': 'git commit -m "%s"%s -- %s' % (title, ' -m "<known issues>"' if body else '', ' '.join(commit_paths)),
                  'rc': rc, 'out': (out + err).strip()})
    if rc != 0:
        return _m._ok(committed=False, steps=steps, written=written, removed=removed, error='git commit に失敗')
    rc, h, _ = _git(['rev-parse', 'HEAD'])
    commit = h.strip()
    now = _m._now()
    with _m._db() as (cur, conn):
        cur.execute("UPDATE app_share_registry SET git_commit=%s, git_committed_at=%s WHERE app_name=%s",
                    (commit, now, app_name))
        conn.commit()
    return _m._ok(committed=True, commit=commit, short=commit[:7], committed_at=_m._fmt(now),
                  title=title, body=body, staged=staged_lines, written=written, removed=removed, steps=steps)


@app_share_bp.route('/api/git/push', methods=['POST'])
@login_required
def api_git_push():
    """push．画面側で確認を取ってから呼ぶ．トークンは config.py の GITHUB_TOKEN．
    push 先 URL は都度組み立て，関所の設定には書き残さない．"""
    if not _repo_ok():
        return _m._err('関所（git リポジトリ）が見つかりません: ' + repo_dir(), 500)
    token = getattr(Config, 'GITHUB_TOKEN', None) or ''
    rc, remote, _ = _git(['remote', 'get-url', 'origin'])
    remote = remote.strip()
    rc, branch, _ = _git(['rev-parse', '--abbrev-ref', 'HEAD'])
    branch = branch.strip()
    if not token:
        return _m._ok(pushed=False, error='config.py に GITHUB_TOKEN がありません',
                      manual=f'cd {repo_dir()} && git push origin {branch}')
    u = urlsplit(remote)
    if u.scheme not in ('https',) or not u.netloc:
        return _m._ok(pushed=False, error=f'origin が https ではありません（{_mask_token(remote, token)}）．手で push してください',
                      manual=f'cd {repo_dir()} && git push origin {branch}')
    host = u.netloc.rsplit('@', 1)[-1]
    push_url = urlunsplit(('https', f'x-access-token:{token}@{host}', u.path, '', ''))
    rc, out, err = _git(['push', push_url, f'HEAD:{branch}'], timeout=300)
    text = _mask_token(out + err, token).strip()
    if rc != 0:
        return _m._ok(pushed=False, error='git push に失敗', output=text)
    # origin/<branch> を進めて未 push 件数を 0 にする
    _git(['fetch', '-q', push_url, f'+refs/heads/{branch}:refs/remotes/origin/{branch}'], timeout=120)
    rc2, ahead, _ = _git(['rev-list', '--count', f'origin/{branch}..HEAD'])
    return _m._ok(pushed=True, output=text, branch=branch,
                  ahead=int(ahead.strip()) if rc2 == 0 and ahead.strip().isdigit() else None)


# ============================================================
# 一覧用：全アプリの要 commit 状態（api/summary に合成）
# ============================================================

def unpushed_by_app(app_names):
    """未 push の commit（origin/<branch>..HEAD）を，触れたファイルからアプリごとに数える（2026-10-31）．
    fujinp/<app>/ の下はそのアプリ，それ以外（ホーム直下・templates/・fujinp/registry.py）はカーネル．
    戻り値 {app_name: 件数}．関所が無い・origin が無いときは {}"""
    out = {}
    if not _repo_ok():
        return out
    rc, branch, _ = _git(['rev-parse', '--abbrev-ref', 'HEAD'])
    if rc != 0:
        return out
    rc, log, _ = _git(['log', '--format=@@%H', '--name-only', 'origin/' + branch.strip() + '..HEAD'])
    if rc != 0:
        return out
    names = set(app_names)
    cur, seen = None, set()
    for line in log.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith('@@'):
            cur = line[2:]
            continue
        parts = line.split('/')
        app = parts[1] if (len(parts) >= 3 and parts[0] == 'fujinp' and parts[1] in names) else PLATFORM_ROW
        if (app, cur) in seen:
            continue
        seen.add((app, cur))
        out[app] = out.get(app, 0) + 1
    return out


def git_summary_all(app_names):
    """{app_name: {'needs_commit', 'last_change', 'changed', 'added', 'deleted'}}"""
    out = {}
    if not _repo_ok():
        return out
    for a in app_names:
        try:
            d = _diff_for(a)
            out[a] = {'needs_commit': d['needs_commit'], 'last_change': d['last_change'],
                      'changed': len(d['changed']), 'added': len(d['added']), 'deleted': len(d['deleted'])}
        except Exception as e:
            out[a] = {'error': str(e)}
    return out
