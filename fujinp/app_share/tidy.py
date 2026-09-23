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
app_share.tidy — 整形とコードの指紋（2026-09-17）

整形
  アプリ配下の写しの対象（gitsync の許可リスト）のうちテキストファイルを，
  次の3点でそろえる．中身が変わるファイルだけ書き直す（変わらなければ mtime も変わらない）．
    1. 改行を LF にする（CRLF → LF）
    2. 先頭の BOM を除く
    3. 末尾を改行1つで終える（改行が無ければ足す）
  エクスポートのゲート表示・版確定・GitHub への commit の直前に必ず通す．
  関所へ写すとき（gitsync._norm_bytes）も同じ規則で正規化する．

指紋
  .py とテンプレート（templates/ 配下）の「相対パス → サイズ（整形後）」の一覧．
  文書（マニュアル／仕様書）を保存したときに app_share_documents.code_fingerprint へ記録し，
  エクスポートのゲートで「いまのコード」と比べる．本数かサイズが違えばコードが変わったとみなす．

API（admin．routes.py の before_request が既定で admin 必須にする）
  POST /app_share/api/app/<app_name>/tidy               整形
  GET  /app_share/api/app/<app_name>/fingerprint        いまの指紋
  POST /app_share/api/app/<app_name>/docs/mark_checked  文書は現行コードと照合済みとして指紋だけ記録
"""

import os
import json
import shutil
import datetime

from flask import request

from . import app_share_bp
from . import manage as _m
from decorators import login_required

JST = _m.JST

# 整形の対象（テキスト）．CSV は Excel 向けに BOM を持つことがあるので対象外
TIDY_EXTS = ('.py', '.html', '.htm', '.js', '.css', '.md', '.txt', '.json', '.sql',
             '.yml', '.yaml', '.cfg', '.ini', '.svg')
BOM = b'\xef\xbb\xbf'
FP_MAX_FILES = 2000


# ============================================================
# 整形
# ============================================================

def is_tidy_target(rel):
    return rel.lower().endswith(TIDY_EXTS)


def normalize_bytes(rel, raw):
    """整形後のバイト列．対象外の拡張子はそのまま返す"""
    if not is_tidy_target(rel):
        return raw
    if raw.startswith(BOM):
        raw = raw[len(BOM):]
    raw = raw.replace(b'\r\n', b'\n')
    if raw and not raw.endswith(b'\n'):
        raw += b'\n'
    return raw


def _scope_files(app_name):
    """写しの対象 {rel: abs}．カーネル（_platform）は関所の追跡状況から決まる"""
    from . import gitsync as _g
    try:
        if app_name == _m.PLATFORM_ROW:
            if not _g._repo_ok():
                return {}
            return _g._scope_for(app_name)[0]
        return _g._site_app_files(app_name)
    except Exception:
        return {}


def _write_atomic(path, data):
    tmp = path + '.tidy_tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
    try:
        shutil.copymode(path, tmp)
    except OSError:
        pass
    os.replace(tmp, path)


def tidy_app(app_name):
    """アプリ1本を整形する．戻り値 dict(checked, changed[], errors[])"""
    checked, changed, errors = 0, [], []
    for rel, p in sorted(_scope_files(app_name).items()):
        if not is_tidy_target(rel) or os.path.basename(p) == 'config.py':
            continue
        try:
            with open(p, 'rb') as f:
                raw = f.read()
            new = normalize_bytes(rel, raw)
            checked += 1
            if new != raw:
                _write_atomic(p, new)
                changed.append(rel)
        except Exception as e:
            errors.append({'path': rel, 'error': str(e)})
    return {'app_name': app_name, 'checked': checked, 'changed': changed, 'errors': errors}


# ============================================================
# 指紋
# ============================================================

def _is_code(app_name, rel):
    if rel.endswith('.py'):
        return True
    if app_name == _m.PLATFORM_ROW:
        return rel.endswith('.html')
    return rel.startswith('templates/')


def scan(app_name):
    """(指紋 {rel: size}, コードの最終更新 datetime|None)．サイズは整形後の大きさ"""
    files, latest = {}, None
    for rel, p in sorted(_scope_files(app_name).items()):
        if not _is_code(app_name, rel):
            continue
        try:
            with open(p, 'rb') as f:
                files[rel] = len(normalize_bytes(rel, f.read()))
            m = datetime.datetime.fromtimestamp(os.path.getmtime(p), JST).replace(tzinfo=None)
            latest = m if latest is None or m > latest else latest
        except OSError:
            continue
        if len(files) >= FP_MAX_FILES:
            break
    return files, latest


def fingerprint(app_name):
    files, _ = scan(app_name)
    return make_fp(files)


def make_fp(files):
    return {'taken_at': _m._fmt(_m._now()), 'count': len(files), 'files': files}


def parse_fp(v):
    """DB の JSON 列（str／bytes／dict）や受け取った値を {files: {rel: int}} に揃える．不正なら None"""
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray)):
        v = v.decode('utf-8', 'replace')
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return None
    if not isinstance(v, dict):
        return None
    files = v.get('files') if 'files' in v else v
    if not isinstance(files, dict) or len(files) > FP_MAX_FILES:
        return None
    out = {}
    for k, s in files.items():
        if not isinstance(k, str) or len(k) > 500:
            return None
        try:
            out[k] = int(s)
        except (TypeError, ValueError):
            return None
    return {'taken_at': v.get('taken_at') if isinstance(v.get('taken_at'), str) else None,
            'count': len(out), 'files': out}


def compare(old_files, new_files):
    """指紋どうしの差．戻り値 dict(added[], removed[], changed[{path, before, after}], same)"""
    old_files = old_files or {}
    new_files = new_files or {}
    added = sorted(k for k in new_files if k not in old_files)
    removed = sorted(k for k in old_files if k not in new_files)
    changed = [{'path': k, 'before': old_files[k], 'after': new_files[k]}
               for k in sorted(new_files) if k in old_files and old_files[k] != new_files[k]]
    return {'added': added, 'removed': removed, 'changed': changed,
            'same': not (added or removed or changed)}


_FP_COL = {'ok': False}


def fp_column_ready(cur):
    """app_share_documents.code_fingerprint 列があるか（あると分かったらプロセス内で覚える）"""
    if _FP_COL['ok']:
        return True
    try:
        cur.execute("SHOW COLUMNS FROM app_share_documents LIKE 'code_fingerprint'")
        row = cur.fetchone()
    except Exception:
        return False
    if row:
        _FP_COL['ok'] = True
    return bool(row)


def record_fp(cur, app_name, doc_types, fp):
    """文書行に指紋を記録する．updated_at は動かさない（ON UPDATE を打ち消す）"""
    if not fp_column_ready(cur):
        return 0
    n = 0
    for dt_ in doc_types:
        cur.execute("""UPDATE app_share_documents
                       SET code_fingerprint=%s, updated_at=updated_at
                       WHERE app_name=%s AND doc_type=%s""",
                    (json.dumps(fp, ensure_ascii=False), app_name, dt_))
        n += cur.rowcount or 0
    return n


# ============================================================
# API
# ============================================================

@app_share_bp.route('/api/app/<app_name>/tidy', methods=['POST'])
@login_required
def api_tidy(app_name):
    if not _m._valid_app(app_name):
        return _m._err('アプリ名が不正です')
    return _m._ok(**tidy_app(app_name))


@app_share_bp.route('/api/app/<app_name>/fingerprint')
@login_required
def api_fingerprint(app_name):
    if not _m._valid_app(app_name):
        return _m._err('アプリ名が不正です')
    tidy_app(app_name)
    return _m._ok(fingerprint=fingerprint(app_name))


@app_share_bp.route('/api/app/<app_name>/docs/mark_checked', methods=['POST'])
@login_required
def api_docs_mark_checked(app_name):
    """文書を作り直さず，いまのコードの指紋だけを記録する（中身が変わっていないと人が確かめた場合）"""
    if not _m._valid_app(app_name):
        return _m._err('アプリ名が不正です')
    tidy_app(app_name)
    fp = fingerprint(app_name)
    with _m._db() as (cur, conn):
        if not fp_column_ready(cur):
            return _m._err('app_share_documents に code_fingerprint 列がありません（ALTER TABLE を先に実行してください）', 500)
        n = record_fp(cur, app_name, ('manual', 'spec'), fp)
        conn.commit()
    if not n:
        return _m._err('記録できる文書がありません（マニュアルと仕様書を先に登録してください）')
    return _m._ok(recorded=n, count=fp['count'])
