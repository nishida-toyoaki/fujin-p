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
app_share.package — アプリパッケージの輸出入（段階6c，format_version 3）

パッケージ＝正本レコード（基本・起動・ランチャ・台帳・目録・版）＋文書＋不具合（open）＋ files[]．
app_info.json／version.json／manifest.json は含めない（情報は正本にある）．

  GET  /app_share/package/export/<app_name>     輸出（?docs= 無しなら確認ゲートを表示．admin 以外はゲートなしでそのまま書き出す）
  GET  /app_share/package/export/<app_name>?docs=ok|later|draft|final
       ok=文書は最新（更新不要）／later=更新せず書き出す／draft=文書作成用の暫定版／final=文書を添付した最終版
  POST /app_share/api/app/<app_name>/docs/attach  Claude が書いたマニュアル・仕様書を保存（ゲートの「完了」）
  GET  /app_share/package/import                取り込み画面（?app=<name> で対象を指定可）
  POST /app_share/package/check                 検証（JSON を受けて差分等を返す）
  POST /app_share/package/apply                 適用（ファイル・正本・文書・台帳・テーブル・不具合・発行）
  POST /app_share/api/app/<app_name>/delete     レジストリ行と付随データ（文書・台帳・不具合）の削除
  GET  /app_share/package/export_all            全アプリの文書の点検ページ（1本ずつ確認してから書き出す）
  POST /app_share/package/export_all            確認済みの選択を受けて全アプリパッケージ（fujinp_apps_overview）を返す
  GET  /app_share/api/docs/status_all           全アプリの文書の新旧（点検ページの再確認用）
  GET  /app_share/package/export_all/public     全アプリパッケージをそのまま書き出す（ログインした全員）
  POST /app_share/package/site/check            全アプリパッケージの一覧検証（新規／更新／同じ／古い）
書き出し（GET の3本）はログインした全員に開いている．それ以外は admin．

format_version 2（旧形式）のパッケージも読める（正本部分は推定して candidate 扱い）．
"""

import os
import re
import json
import base64
import shutil
import datetime
import importlib.util

from flask import render_template, request, jsonify, session, Response

from . import app_share_bp
from . import manage as _m
from . import gitsync as _g
from . import tidy as _t
from config import Config
from db import DatabaseConfig
from decorators import login_required
from fujinp import registry as _reg
import mysql.connector

BASE_DIR = _m.BASE_DIR
JST = _m.JST
FORMAT_VERSION = 3
EXPORT_TYPE = 'fujinp_app_package'
MAX_FILE = 5 * 1024 * 1024
BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'import_backups')

# 書き出しはログインした全員に開く（routes.py の before_request の admin 既定から外す）．
# FUJIN-P はオープンソースなので，ほかのサイトのオーナーがアプリを持ち帰れるようにする．
_m._routes._NON_ADMIN_ENDPOINTS = frozenset(
    set(_m._routes._NON_ADMIN_ENDPOINTS) | {'package_export', 'package_export_all_public'})


def _is_admin():
    return _m._routes.check_admin_permission(session.get('user_id'))


def _public_choice(cur, app_name, files=None):
    """点検を経ない書き出しの文書の扱い（サーバの判定で ok／later を決める）"""
    st = _doc_status(cur, app_name, files=files)
    return 'later' if st['any_stale'] else 'ok'


# ============================================================
# 輸出
# ============================================================

def _collect_files(app_name):
    """許可リストどおりに files[] を集める（gitsync と同じ規則）"""
    files = []
    for rel, p in sorted(_g._site_app_files(app_name).items()):
        try:
            size = os.path.getsize(p)
            mtime = datetime.datetime.fromtimestamp(os.path.getmtime(p), JST).strftime('%Y-%m-%d %H:%M:%S')
            raw = open(p, 'rb').read()
            try:
                files.append({'path': rel, 'size': size, 'mtime': mtime, 'encoding': 'text',
                              'content': raw.decode('utf-8')})
            except UnicodeDecodeError:
                files.append({'path': rel, 'size': size, 'mtime': mtime, 'encoding': 'base64',
                              'content': base64.b64encode(raw).decode('ascii')})
        except Exception as e:
            files.append({'path': rel, 'skipped': True, 'reason': str(e)})
    return files


def build_package(cur, app_name, generated_by=None, site_url=None, docs_choice=None):
    row = _m._load_registry_row(cur, app_name)
    if not row:
        return None
    cur.execute("""SELECT table_name, db_target, ddl, status, note, sort_order
                   FROM app_share_tables WHERE app_name=%s ORDER BY sort_order, table_name""", (app_name,))
    tables = [dict(r) for r in cur.fetchall()]
    fp_col = ', code_fingerprint' if _t.fp_column_ready(cur) else ''
    cur.execute(f"""SELECT doc_type, title, content, updated_at{fp_col} FROM app_share_documents
                   WHERE app_name=%s AND doc_type IN ('manual','spec')""", (app_name,))
    docs = {}
    for r in cur.fetchall():
        docs[r['doc_type']] = {'title': r['title'] or '', 'content': r['content'] or '',
                               'updated_at': _m._fmt(r['updated_at'])}
        fp = _t.parse_fp(r.get('code_fingerprint'))
        if fp:
            docs[r['doc_type']]['code_fingerprint'] = fp
    cur.execute("""SELECT title, detail, reported_at, note FROM app_share_issues
                   WHERE app_name=%s AND status='open' ORDER BY id""", (app_name,))
    issues = [{'title': r['title'], 'detail': r['detail'] or '', 'reported_at': _m._fmt(r['reported_at']),
               'note': r['note'] or ''} for r in cur.fetchall()]
    files = _collect_files(app_name)
    st = _doc_status(cur, app_name, files=files)
    doc_update = {
        'choice': docs_choice,
        'current': docs_choice in ('ok', 'final'),
        'asked_at': _m._fmt(_m._now()),
        'manual_updated_at': st['docs']['manual']['updated_at'],
        'spec_updated_at': st['docs']['spec']['updated_at'],
        'latest_file_mtime': st['latest_file_mtime'],
        'stale': st['any_stale'],
        # 2026-09-17：指紋による判定．code_diff は文書を書いた時点のコードからの変化
        'judged_by': st['judged_by'],
        'manual_state': st['docs']['manual'].get('state'),
        'spec_state': st['docs']['spec'].get('state'),
        'code_diff': st['diff'],
        'code_fingerprint': st['fingerprint'],
        # 旧 v3 ゲート互換（final=True は「文書が最新」）
        'final': True if docs_choice in ('ok', 'final') else (False if docs_choice in ('later', 'draft') else None),
    }
    _NOTE = {
        'ok':    ('【文書は最新】マニュアルと仕様書がコードと揃っている版．',
                  '文書はコードと揃っています．更新は不要です．'),
        'final': ('【文書更新あり】マニュアルと仕様書を添付した最終版．',
                  'この版でマニュアルと仕様書を更新しました．documents が現状のコードに対応します．'),
        'later': ('【改修の途中】マニュアルと仕様書の更新は行わない版．',
                  '改修の途中です．マニュアルと仕様書の更新は行っていません．'),
        'draft': ('【暫定】マニュアルと仕様書を作成するための材料．',
                  'この暫定パッケージを材料に，マニュアルと仕様書を作成してください．'
                  '完成した文書はアプシャのエクスポート確認ページに貼り込むと，保存と最終版の書き出しが行われます．'),
    }
    head_note, req = _NOTE.get(docs_choice, ('', ''))
    if req:
        doc_update['request'] = req
    note = (head_note +
            'FUJIN-P アプリパッケージ v3．registry=正本（起動・ランチャ・ライブラリ目録・定数目録），'
            'tables=所有テーブルとDDL，documents=マニュアル／仕様書（叙述），issues=既知の不具合(open)，'
            'files=アプリ本体（*.py／templates/／data_for_distribution/／直下の .sql .md .txt .json．'
            'app_info.json・version.json は含まない）．')
    return {
        'export_type': EXPORT_TYPE,
        'format_version': FORMAT_VERSION,
        'app_name': app_name,
        'display_name': row.get('display_name') or app_name,
        'icon': row.get('icon') or '📦',
        'description': row.get('description') or '',
        'version_id': row.get('version_id'),
        'version_confirmed_at': _m._fmt(row.get('version_confirmed_at')),
        'version_confirmed_by': row.get('version_confirmed_by'),
        'updated_at': _m._fmt(row.get('updated_at')),
        'registry': {
            'kind': row.get('kind') or 'app',
            'blueprints': _m._jload(row.get('blueprints'), []),
            'launchers': _m._jload(row.get('launchers'), []),
            'libraries': _m._jload(row.get('libraries'), []),
            'config_keys': _m._jload(row.get('config_keys'), []),
        },
        'tables': tables,
        'documents': docs,
        'issues': issues,
        'file_count': len(files),
        'files': files,
        'doc_update': doc_update,
        'site_name': getattr(Config, 'DB_ACCOUNT', ''),
        'site_url': site_url or (request.host_url.rstrip('/') if request else ''),
        'generated_at': _m._fmt(_m._now()),
        'generated_by': generated_by,
        'package_note': note,
    }


# ============================================================
# 輸出前の確認（文書更新の節目かどうか）
# ============================================================

def _latest_file_mtime(app_name):
    """アプリ配下で写す対象のうち最も新しい更新時刻（JST の naive datetime）"""
    latest = None
    for _rel, p in _g._site_app_files(app_name).items():
        try:
            m = datetime.datetime.fromtimestamp(os.path.getmtime(p), JST).replace(tzinfo=None)
        except OSError:
            continue
        if latest is None or m > latest:
            latest = m
    return latest


def _db_clock_skew(cur):
    """DB の NOW() と JST の now の差（秒）．DB のセッションTZが UTC なら約 -32400．

    app_share_documents.updated_at は CURRENT_TIMESTAMP で入るので，ファイルの
    mtime（JST）と比べる前にこの差を戻す．"""
    try:
        cur.execute("SELECT NOW() AS n")
        n = (cur.fetchone() or {}).get('n')
        if n:
            return (n - _m._now()).total_seconds()
    except Exception:
        pass
    return 0.0


def _doc_status_by_time(rows, latest, skew, out):
    """指紋の列が無いときの判定（旧方式：更新日時の比較）"""
    for dt_, label in (('manual', 'マニュアル'), ('spec', '仕様書')):
        r = rows.get(dt_)
        if not r:
            out['docs'][dt_] = {'label': label, 'exists': False, 'title': '', 'chars': 0, 'state': 'missing',
                                'updated_at': None, 'stale': True, 'verdict': '未登録', 'diff': None}
            continue
        u = r.get('updated_at')
        stale, verdict = False, 'コードより新しい'
        if latest and isinstance(u, datetime.datetime):
            if (u - skew) < latest - datetime.timedelta(seconds=60):
                stale, verdict = True, 'コードより古い'
        elif not latest:
            verdict = ''
        out['docs'][dt_] = {'label': label, 'exists': True, 'title': r.get('title') or '',
                            'chars': r.get('clen') or 0, 'updated_at': _m._fmt(u),
                            'state': 'changed' if stale else 'match',
                            'stale': stale, 'verdict': verdict + '（時刻で判定）', 'diff': None}


def _merge_diffs(diffs):
    added, removed, changed = set(), set(), {}
    for d in diffs:
        if not d:
            continue
        added.update(d['added'])
        removed.update(d['removed'])
        for c in d['changed']:
            changed.setdefault(c['path'], c)
    return {'added': sorted(added), 'removed': sorted(removed),
            'changed': [changed[k] for k in sorted(changed)],
            'same': not (added or removed or changed)}


def _doc_status(cur, app_name, files=None, skew=None):
    """マニュアル／仕様書の登録状況と，コードとの整合（2026-09-17 改訂）．

    文書を保存したときに記録したコードの指紋（.py とテンプレートの本数とサイズ）と，
    いまのコードの指紋を比べる．state は次のいずれか．
      match     … 一致（更新不要）
      changed   … 文書を書いた後にコードが変わった（更新を促す）
      unchecked … 指紋の記録が無い（照合されていない．更新を促す）
      missing   … 文書が未登録
    code_fingerprint 列が無いサイトでは旧方式（更新日時の比較）で判定する．
    files と skew は旧呼び出しとの互換のために受け取るだけで使わない（旧方式の判定を除く）．"""
    fp_ready = _t.fp_column_ready(cur)
    try:
        cur.execute(f"""SELECT doc_type, title, updated_at, CHAR_LENGTH(content) AS clen
                               {', code_fingerprint' if fp_ready else ''}
                        FROM app_share_documents
                        WHERE app_name=%s AND doc_type IN ('manual','spec')""", (app_name,))
        rows = {r['doc_type']: r for r in cur.fetchall()}
    except Exception:
        rows = {}
    now_files, latest = _t.scan(app_name)
    out = {'latest_file_mtime': _m._fmt(latest), 'code_files': len(now_files),
           'code_bytes': sum(now_files.values()), 'fingerprint': _t.make_fp(now_files),
           'judged_by': 'fingerprint' if fp_ready else 'time', 'docs': {}}
    if not fp_ready:
        sk = datetime.timedelta(seconds=round(_db_clock_skew(cur) if skew is None else skew))
        _doc_status_by_time(rows, latest, sk, out)
    else:
        for dt_, label in (('manual', 'マニュアル'), ('spec', '仕様書')):
            r = rows.get(dt_)
            if not r:
                out['docs'][dt_] = {'label': label, 'exists': False, 'title': '', 'chars': 0,
                                    'updated_at': None, 'stale': True, 'state': 'missing',
                                    'verdict': '未登録', 'diff': None, 'checked_at': None}
                continue
            fp = _t.parse_fp(r.get('code_fingerprint'))
            if fp is None:
                state, verdict, diff = 'unchecked', '未照合', None
            else:
                diff = _t.compare(fp['files'], now_files)
                state, verdict = ('match', 'コードと一致') if diff['same'] else ('changed', 'コードが変わっています')
            out['docs'][dt_] = {'label': label, 'exists': True, 'title': r.get('title') or '',
                                'chars': r.get('clen') or 0, 'updated_at': _m._fmt(r.get('updated_at')),
                                'stale': state != 'match', 'state': state, 'verdict': verdict,
                                'diff': diff, 'checked_at': (fp or {}).get('taken_at')}
    out['any_stale'] = any(d['stale'] for d in out['docs'].values())
    out['diff'] = _merge_diffs([d.get('diff') for d in out['docs'].values()])
    states = {d['state'] for d in out['docs'].values()}
    out['any_changed'] = 'changed' in states
    out['any_unchecked'] = 'unchecked' in states
    out['both_exist'] = all(d['exists'] for d in out['docs'].values())
    miss = [d['label'] for d in out['docs'].values() if not d['exists']]
    out['missing'] = 'と'.join(miss) if miss else ''
    return out


@app_share_bp.route('/package/export/<app_name>')
@login_required
def package_export(app_name):
    """?docs= が無ければ確認ゲートを表示し，ok／later／draft／final が付いていればダウンロードする．"""
    if not _m._valid_app(app_name):
        return "アプリ名が不正です", 400
    choice = (request.args.get('docs') or '').strip().lower()

    if not _is_admin():
        return _public_export(app_name)

    # 点検と書き出しの前に必ず整形する（2026-09-17）
    tidied = _t.tidy_app(app_name)

    if choice not in ('ok', 'later', 'draft', 'final'):
        with _m._db() as (cur, conn):
            row = _m._load_registry_row(cur, app_name)
            if not row:
                return "レジストリにありません", 404
            st = _doc_status(cur, app_name)
        return render_template('app_share_export_gate.html',
                               app_name=app_name,
                               display_name=row.get('display_name') or app_name,
                               icon=row.get('icon') or '📦',
                               version_id=row.get('version_id'),
                               version_confirmed_at=_m._fmt(row.get('version_confirmed_at')),
                               st=st, tidied=tidied,
                               prompt_text=_docgen_prompt(),
                               prompt_source=_docgen_prompt_source())

    with _m._db() as (cur, conn):
        cur.execute("SELECT full_name FROM users WHERE id=%s", (session.get('user_id'),))
        u = cur.fetchone()
        pkg = build_package(cur, app_name, generated_by=(u or {}).get('full_name'), docs_choice=choice)
    if not pkg:
        return "レジストリにありません", 404
    suffix = {'final': '_docs', 'draft': '_draft'}.get(choice, '')
    fn = f'app_package_{app_name}_{_m._now().strftime("%Y%m%d_%H%M%S")}{suffix}.json'
    resp = Response(json.dumps(pkg, ensure_ascii=False, indent=2), mimetype='application/json',
                    headers={'Content-Disposition': f'attachment; filename="{fn}"'})
    # 確認ページの控えの経路（fetch が使えない環境）向けの完了合図
    token = re.sub(r'[^A-Za-z0-9]', '', request.args.get('t') or '')[:32]
    if token:
        resp.set_cookie('fujinp_export', f'{token}|{fn}', max_age=180, path='/', samesite='Lax')
    return resp


def _public_export(app_name):
    """admin 以外の書き出し．非公開（disclosed=0）のアプリは出さない．
    文書の点検ゲートは通さず，文書の新旧はサーバの判定を doc_update に記録する．"""
    with _m._db() as (cur, conn):
        row = _m._load_registry_row(cur, app_name)
        if not row or (row.get('disclosed') is not None and not int(row.get('disclosed') or 0)):
            return "レジストリにありません", 404
        if app_name == _m.PLATFORM_ROW:
            return "カーネルは ⚙ カーネルの書き出しを使ってください", 400
        cur.execute("SELECT full_name FROM users WHERE id=%s", (session.get('user_id'),))
        u = cur.fetchone()
        choice = _public_choice(cur, app_name)
        pkg = build_package(cur, app_name, generated_by=(u or {}).get('full_name'), docs_choice=choice)
    if not pkg:
        return "レジストリにありません", 404
    pkg['doc_update']['public_download'] = True
    fn = f'app_package_{app_name}_{_m._now().strftime("%Y%m%d_%H%M%S")}.json'
    return Response(json.dumps(pkg, ensure_ascii=False, indent=2), mimetype='application/json',
                    headers={'Content-Disposition': f'attachment; filename="{fn}"'})


# ============================================================
# 文書作成の段取り（プロンプトと貼り込み）
# ============================================================

_DOCGEN_PROMPT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'docgen_prompt.md')

_DOCGEN_PROMPT_DEFAULT = """# FUJIN-P アプリの文書作成依頼

添付の JSON はアプシャでエクスポートしたアプリ1つ分のパッケージです．この中身だけを根拠に，
ユーザマニュアルと技術仕様書を日本語（句読点は全角「．」「，」）の Markdown で書いてください．
documents に現行の文書があれば改訂，無ければ新規に書いてください．
doc_update.code_diff に，文書を書いた後に変わったファイルが載っていれば，そこを重点的に見直してください．

出力は次の区切り記号のまま，この順に，前後に説明を付けずに返してください．

<<<MANUAL>>>
（ユーザマニュアル）
<<<SPEC>>>
（技術仕様書）
<<<END>>>
"""


def _docgen_prompt():
    """アプリ直下の docgen_prompt.md（編集可）．無ければ内蔵の既定文．"""
    try:
        with open(_DOCGEN_PROMPT_FILE, encoding='utf-8') as f:
            t = f.read().strip()
            if t:
                return t
    except OSError:
        pass
    return _DOCGEN_PROMPT_DEFAULT.strip()


def _docgen_prompt_source():
    return 'docgen_prompt.md' if os.path.exists(_DOCGEN_PROMPT_FILE) else '内蔵の既定文'


@app_share_bp.route('/api/app/<app_name>/docs/attach', methods=['POST'])
@login_required
def api_docs_attach(app_name):
    """ゲートの「完了」．Claude が書いたマニュアルと仕様書を app_share_documents に保存する．
    save_document と同じく updated_at は CURRENT_TIMESTAMP（ゲートの新旧判定はこれを前提にしている）．"""
    if not _m._valid_app(app_name):
        return _m._err('アプリ名が不正です')
    d = request.get_json(silent=True) or {}
    manual = (d.get('manual') or '').strip()
    spec = (d.get('spec') or '').strip()
    if not manual and not spec:
        return _m._err('マニュアルも仕様書も空です')
    mt = (d.get('manual_title') or '').strip() or f'{app_name} ユーザマニュアル'
    st_ = (d.get('spec_title') or '').strip() or f'{app_name} 技術仕様書'
    user_id = session.get('user_id')
    saved = []
    # 文書が対応するコードの指紋：暫定パッケージを書き出した時点のもの（無ければいま）
    _t.tidy_app(app_name)
    now_fp = _t.fingerprint(app_name)
    draft_fp = _t.parse_fp(d.get('fingerprint'))
    fp = draft_fp or now_fp
    since_draft = _t.compare(draft_fp['files'], now_fp['files']) if draft_fp else None
    with _m._db() as (cur, conn):
        row = _m._load_registry_row(cur, app_name)
        if not row:
            return _m._err('レジストリにありません', 404)
        for doc_type, title, content in (('manual', mt, manual), ('spec', st_, spec)):
            if not content:
                continue
            cur.execute("""INSERT INTO app_share_documents (app_name, doc_type, title, content, updated_by)
                           VALUES (%s,%s,%s,%s,%s)
                           ON DUPLICATE KEY UPDATE title=VALUES(title), content=VALUES(content),
                               updated_by=VALUES(updated_by), updated_at=CURRENT_TIMESTAMP""",
                        (app_name, doc_type, title[:500], content, user_id))
            saved.append({'doc_type': doc_type, 'title': title, 'chars': len(content)})
        _t.record_fp(cur, app_name, [x['doc_type'] for x in saved], fp)
        cur.execute("UPDATE app_share_registry SET updated_at=%s WHERE app_name=%s", (_m._now(), app_name))
        conn.commit()
        st = _doc_status(cur, app_name)
    return _m._ok(saved=saved, status=st,
                  changed_since_draft=(since_draft if since_draft and not since_draft['same'] else None))


# ============================================================
# 旧形式（v2）の読み替え
# ============================================================

def _normalize_package(pkg):
    """v2 パッケージを v3 相当の dict に揃える．v3 はそのまま．"""
    if pkg.get('format_version', 0) >= 3 and 'registry' in pkg:
        pkg.setdefault('tables', [])
        pkg.setdefault('documents', {})
        pkg.setdefault('issues', [])
        return pkg, False
    app_name = pkg.get('app_name') or ''
    files = pkg.get('files') or []
    # 起動：files から Blueprint 定義を推定
    bps = []
    for f in files:
        rel = f.get('path') or ''
        if not rel.endswith('.py') or f.get('encoding') != 'text':
            continue
        parts = rel[:-3].split('/')
        if parts[-1] == '__init__':
            parts = parts[:-1]
        module = '.'.join(['fujinp', app_name] + parts)
        for m in _m._BP_DEF_RE.finditer(f.get('content') or ''):
            bps.append({'module': module, 'attr': m.group(1), 'name': m.group(2), 'url_prefix': None})
    libs = []
    for name in (pkg.get('libraries') or {}).get('third_party', []):
        libs.append({'name': _m.PIP_NAME_MAP.get(name, name), 'import': name, 'kind': 'pip',
                     'status': 'candidate', 'note': ''})
    for name in (pkg.get('libraries') or {}).get('local', []):
        libs.append({'name': name, 'import': name, 'kind': 'local', 'status': 'candidate', 'note': ''})
    tables = []
    dbs = pkg.get('sql_databases') or {}
    for t, ddl in (pkg.get('sql_schemas') or {}).items():
        tables.append({'table_name': t, 'db_target': dbs.get(t, 'default'), 'ddl': ddl,
                       'status': 'confirmed' if t.startswith(app_name + '_') else 'candidate', 'note': '',
                       'sort_order': 0})
    docs = {}
    for src, dst in (('user_manual', 'manual'), ('spec_memo', 'spec')):
        d = pkg.get(src)
        if isinstance(d, dict):
            docs[dst] = {'title': d.get('title') or '', 'content': d.get('content') or '',
                         'updated_at': d.get('updated_at')}
    out = dict(pkg)
    out['registry'] = {'kind': 'app', 'blueprints': bps, 'launchers': [], 'libraries': libs,
                       'config_keys': []}
    out['tables'] = tables
    out['documents'] = docs
    out['issues'] = []
    out['files'] = [f for f in files if (f.get('path') or '').split('/')[-1] not in _g.ROOT_EXCLUDE_FILES]
    return out, True


# ============================================================
# 検証
# ============================================================

def _safe_rel(rel):
    if not rel or rel.startswith(('/', '\\')):
        return False
    parts = rel.replace('\\', '/').split('/')
    return '..' not in parts and '' not in parts


def _file_bytes(entry):
    if entry.get('skipped') or entry.get('content') is None:
        return None
    if entry.get('encoding') == 'base64':
        try:
            return base64.b64decode(entry['content'])
        except Exception:
            return None
    return str(entry['content']).encode('utf-8')


def _check(cur, pkg):
    app_name = pkg.get('app_name') or ''
    ok_name = bool(_m._valid_app(app_name))
    row = _m._load_registry_row(cur, app_name) if ok_name else None
    app_dir = _m._app_path(app_name) if ok_name else None
    # 版
    local_ver = (row or {}).get('version_id')
    pkg_ver = pkg.get('version_id')
    local_ts = (row or {}).get('updated_at')
    pkg_ts = _m._routes._parse_ts(pkg.get('updated_at'))
    if local_ts and pkg_ts:
        vstatus = 'newer' if pkg_ts > local_ts else ('older' if pkg_ts < local_ts else 'same')
    else:
        vstatus = 'unknown'
    # ファイル
    counts = {'new': 0, 'changed': 0, 'same': 0, 'skipped': 0}
    details = []
    pkg_paths = set()
    for f in pkg.get('files') or []:
        rel = f.get('path') or ''
        if not _safe_rel(rel):
            counts['skipped'] += 1
            details.append({'path': rel, 'status': 'skipped', 'reason': '不正なパス'})
            continue
        pkg_paths.add(rel)
        raw = _file_bytes(f)
        if raw is None:
            counts['skipped'] += 1
            details.append({'path': rel, 'status': 'skipped', 'reason': f.get('reason') or '内容なし'})
            continue
        lp = os.path.join(app_dir, rel) if app_dir else None
        if not lp or not os.path.exists(lp):
            st = 'new'
        else:
            try:
                st = 'same' if open(lp, 'rb').read() == raw else 'changed'
            except Exception:
                st = 'changed'
        counts[st] += 1
        details.append({'path': rel, 'status': st, 'size': f.get('size')})
    local_only = []
    if app_dir and os.path.isdir(app_dir):
        for rel in _g._site_app_files(app_name):
            if rel not in pkg_paths:
                local_only.append(rel)
    # テーブル
    dbs = _m._db_names()
    tables = []
    for t in pkg.get('tables') or []:
        name = t.get('table_name')
        suf = t.get('db_target') or 'default'
        dbname = dbs.get(suf)
        item = {'table_name': name, 'db_target': suf, 'database': dbname,
                'ledger_status': t.get('status'), 'in_ledger': False}
        if not re.match(r'^[\w$]+$', name or ''):
            item['status'] = 'invalid'
        elif not dbname:
            item['status'] = 'nodb'
        else:
            live = None
            try:
                live = _m._show_create(dbname, name)
            except Exception as e:
                item['error'] = str(e)
            if live is None:
                item['status'] = 'missing'
                item['create_sql'] = _m._create_sql(t.get('ddl'))
            else:
                cmp = _m._compare(t.get('ddl'), live)
                item.update(cmp)
                item['alter_sqls'] = _m._alter_sqls(name, cmp)
        tables.append(item)
    if ok_name:
        cur.execute("SELECT table_name FROM app_share_tables WHERE app_name=%s", (app_name,))
        have = {r['table_name'] for r in cur.fetchall()}
        for item in tables:
            item['in_ledger'] = item['table_name'] in have
    reg = pkg.get('registry') or {}
    libs = _m._check_libraries(reg.get('libraries') or [])
    cfg = _m._check_config_keys(reg.get('config_keys') or [])
    # 不具合（同じ件名が open で既にあるものは重複扱い）
    issues = []
    if ok_name:
        cur.execute("SELECT title FROM app_share_issues WHERE app_name=%s AND status='open'", (app_name,))
        have_t = {r['title'] for r in cur.fetchall()}
    else:
        have_t = set()
    for i in pkg.get('issues') or []:
        issues.append({'title': i.get('title'), 'duplicate': i.get('title') in have_t})
    # 起動中との関係
    rt = _m._runtime_blueprints()
    bps = []
    for b in reg.get('blueprints') or []:
        bps.append(dict(b, registered=bool(b.get('name') and b['name'] in rt)))
    return {
        'identity': {'app_name': app_name, 'valid': ok_name, 'exists_registry': row is not None,
                     'dir_exists': bool(app_dir and os.path.isdir(app_dir)),
                     'export_type_ok': pkg.get('export_type') == EXPORT_TYPE,
                     'format_version': pkg.get('format_version')},
        'version': {'package': pkg_ver, 'local': local_ver, 'package_updated_at': pkg.get('updated_at'),
                    'local_updated_at': _m._fmt(local_ts), 'status': vstatus},
        'files': {'counts': counts, 'details': details, 'local_only': sorted(local_only)},
        'registry': {'kind': reg.get('kind'), 'blueprints': bps, 'launchers': reg.get('launchers') or [],
                     'libraries': libs, 'config_keys': cfg},
        'tables': tables,
        'documents': {k: {'title': v.get('title'), 'length': len(v.get('content') or ''),
                          'updated_at': v.get('updated_at')} for k, v in (pkg.get('documents') or {}).items()},
        'issues': issues,
        'site': {'site_name': pkg.get('site_name'), 'site_url': pkg.get('site_url'),
                 'generated_at': pkg.get('generated_at'), 'generated_by': pkg.get('generated_by')},
    }


@app_share_bp.route('/package/check', methods=['POST'])
@login_required
def package_check():
    pkg = request.get_json(silent=True)
    if not isinstance(pkg, dict):
        return _m._err('JSON を読めません')
    if pkg.get('export_type') != EXPORT_TYPE:
        return _m._err('FUJIN-P のアプリパッケージではありません（export_type）')
    pkg, converted = _normalize_package(pkg)
    with _m._db() as (cur, conn):
        res = _check(cur, pkg)
    res['converted_from_v2'] = converted
    return _m._ok(result=res)


# ============================================================
# 適用
# ============================================================

def _apply_files(app_name, pkg):
    app_dir = _m._app_path(app_name)
    backup = None
    if os.path.isdir(app_dir):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        backup = f"{app_name}_{_m._now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copytree(app_dir, os.path.join(BACKUP_DIR, backup),
                        ignore=shutil.ignore_patterns('import_backups', 'import_staging', '__pycache__'))
    else:
        os.makedirs(app_dir, exist_ok=True)
    written, skipped, errors = 0, 0, []
    for f in pkg.get('files') or []:
        rel = f.get('path') or ''
        if not _safe_rel(rel):
            skipped += 1
            continue
        raw = _file_bytes(f)
        if raw is None:
            skipped += 1
            continue
        try:
            lp = os.path.join(app_dir, rel)
            os.makedirs(os.path.dirname(lp), exist_ok=True)
            with open(lp, 'wb') as fh:
                fh.write(raw)
            written += 1
        except Exception as e:
            errors.append(f'{rel}: {e}')
    return written, skipped, backup, errors


def _apply_registry(cur, app_name, pkg, user_id):
    reg = pkg.get('registry') or {}
    row = _m._load_registry_row(cur, app_name)
    vals = dict(display_name=pkg.get('display_name') or app_name, icon=pkg.get('icon') or '📦',
                description=pkg.get('description') or '',
                kind=reg.get('kind') if reg.get('kind') in ('app', 'kernel') else 'app',
                blueprints=_m._jdump(reg.get('blueprints') or []),
                # ★2026-08-27 旧形式（require_*）のカードは使用区分に読み替えて取り込む
                launchers=_m._jdump([_reg.normalize_launcher(c) for c in (reg.get('launchers') or [])
                                     if isinstance(c, dict)]),
                libraries=_m._jdump(reg.get('libraries') or []),
                config_keys=_m._jdump(reg.get('config_keys') or []),
                version_id=pkg.get('version_id'),
                version_confirmed_at=_m._routes._parse_ts(pkg.get('version_confirmed_at')),
                version_confirmed_by=pkg.get('version_confirmed_by'),
                app_name=app_name)
    if row:
        cur.execute("""UPDATE app_share_registry SET display_name=%(display_name)s, icon=%(icon)s,
            description=%(description)s, kind=%(kind)s, enabled=1, blueprints=%(blueprints)s,
            launchers=%(launchers)s, libraries=%(libraries)s, config_keys=%(config_keys)s,
            version_id=%(version_id)s, version_confirmed_at=%(version_confirmed_at)s,
            version_confirmed_by=%(version_confirmed_by)s, updated_at=UTC_TIMESTAMP()
            WHERE app_name=%(app_name)s""", vals)
    else:
        cur.execute("SELECT COALESCE(MAX(sort_order),0)+10 AS n FROM app_share_registry")
        nxt = cur.fetchone()['n']
        cur.execute("""INSERT INTO app_share_registry
            (app_name, display_name, icon, description, sort_order, kind, enabled, blueprints, launchers,
             libraries, config_keys, version_id, version_confirmed_at, version_confirmed_by)
            VALUES (%(app_name)s, %(display_name)s, %(icon)s, %(description)s, %(sort_order)s, %(kind)s, 1,
             %(blueprints)s, %(launchers)s, %(libraries)s, %(config_keys)s, %(version_id)s,
             %(version_confirmed_at)s, %(version_confirmed_by)s)""", dict(vals, sort_order=nxt))
    for doc_type, d in (pkg.get('documents') or {}).items():
        if doc_type not in ('manual', 'spec') or not isinstance(d, dict):
            continue
        dts = _m._routes._parse_ts(d.get('updated_at')) or datetime.datetime.utcnow()
        cur.execute("""INSERT INTO app_share_documents (app_name, doc_type, title, content, updated_by, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE title=VALUES(title), content=VALUES(content),
                updated_by=VALUES(updated_by), updated_at=VALUES(updated_at)""",
            (app_name, doc_type, d.get('title') or '', d.get('content') or '', user_id, dts))
        # 文書が対応するコードの指紋も運ぶ（無ければ未照合として NULL に戻す）
        if _t.fp_column_ready(cur):
            fp = _t.parse_fp(d.get('code_fingerprint'))
            cur.execute("""UPDATE app_share_documents SET code_fingerprint=%s, updated_at=updated_at
                           WHERE app_name=%s AND doc_type=%s""",
                        (json.dumps(fp, ensure_ascii=False) if fp else None, app_name, doc_type))
    for i, t in enumerate(pkg.get('tables') or []):
        if not re.match(r'^[\w$]+$', t.get('table_name') or ''):
            continue
        cur.execute("""INSERT INTO app_share_tables (app_name, table_name, db_target, ddl, captured_at, status, note, sort_order)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE ddl=VALUES(ddl), captured_at=VALUES(captured_at), db_target=VALUES(db_target),
                note=VALUES(note)""",
            (app_name, t['table_name'], t.get('db_target') or 'default', t.get('ddl') or '', _m._now(),
             'confirmed' if t.get('status') == 'confirmed' else 'candidate', t.get('note') or '',
             t.get('sort_order') or i * 10))
    added_issues = 0
    cur.execute("SELECT title FROM app_share_issues WHERE app_name=%s AND status='open'", (app_name,))
    have_t = {r['title'] for r in cur.fetchall()}
    for i in pkg.get('issues') or []:
        title = (i.get('title') or '').strip()
        if not title or title in have_t:
            continue
        cur.execute("""INSERT INTO app_share_issues (app_name, title, detail, status, reported_at, reported_by, note)
            VALUES (%s,%s,%s,'open',%s,%s,%s)""",
            (app_name, title[:500], i.get('detail') or '', _m._now(), user_id,
             ((i.get('note') or '') + f' [{pkg.get("site_name") or "import"} から]')[:500]))
        added_issues += 1
    return added_issues


@app_share_bp.route('/package/apply', methods=['POST'])
@login_required
def package_apply():
    body = request.get_json(silent=True) or {}
    pkg = body.get('package')
    opts = body.get('options') or {}
    if not isinstance(pkg, dict) or pkg.get('export_type') != EXPORT_TYPE:
        return _m._err('パッケージが不正です')
    pkg, converted = _normalize_package(pkg)
    app_name = pkg.get('app_name') or ''
    if not _m._valid_app(app_name):
        return _m._err('アプリ名が不正です')
    user_id = session.get('user_id')
    result = {'app_name': app_name}
    rt_before = _m._runtime_blueprints()
    # 1) ファイル
    written, skipped, backup, errors = _apply_files(app_name, pkg)
    result['files'] = {'written': written, 'skipped': skipped, 'backup': backup, 'errors': errors}
    # 2) 正本・文書・台帳・不具合
    with _m._db() as (cur, conn):
        result['issues_added'] = _apply_registry(cur, app_name, pkg, user_id)
        conn.commit()
    # 3) テーブルの宣言・改訂（足すだけ）
    executed = []
    if opts.get('apply_tables', True):
        with _m._db() as (cur, conn):
            plan = _m._compare_app_tables(cur, app_name)
        for item in plan:
            sqls = []
            if item.get('status') == 'missing':
                sqls = [item['create_sql']]
            elif item.get('status') == 'diff':
                sqls = item.get('alter_sqls') or []
            if not sqls:
                continue
            conn2 = mysql.connector.connect(**DatabaseConfig.get_config(item['database']))
            try:
                cu = conn2.cursor()
                for s in sqls:
                    try:
                        cu.execute(s)
                        executed.append({'table_name': item['table_name'], 'sql': s, 'ok': True})
                    except Exception as e:
                        executed.append({'table_name': item['table_name'], 'sql': s, 'ok': False, 'error': str(e)})
                conn2.commit()
                cu.close()
            finally:
                conn2.close()
    result['tables_executed'] = executed
    # 4) 発行
    with _m._db() as (cur, conn):
        data = _reg.publish(cur)
        conn.commit()
    want = set()
    for a in data['apps']:
        if a.get('kind') == 'kernel' or not a.get('enabled'):
            continue
        for b in a.get('blueprints') or []:
            if b.get('name'):
                want.add(b['name'])
    kernel = {'auth', 'profile', 'admin', 'guest', 'app_share'}
    running = rt_before - kernel
    result['published'] = True
    result['need_reload'] = (want != running) or (written > 0)
    result['not_running'] = sorted(want - running)
    result['converted_from_v2'] = converted
    if opts.get('reload'):
        result['reloaded'] = _reg.reload_site()
    return _m._ok(result=result)


@app_share_bp.route('/package/import')
@login_required
def package_import_page():
    app_name = request.args.get('app') or ''
    if app_name and not _m._valid_app(app_name):
        app_name = ''
    return render_template('app_share_package.html', app_name=app_name)


# ============================================================
# レジストリ行の削除（付随データごと）
# ============================================================

@app_share_bp.route('/api/app/<app_name>/delete', methods=['POST'])
@login_required
def api_app_delete(app_name):
    if not _m._valid_app(app_name):
        return _m._err('アプリ名が不正です')
    if app_name in ('app_share', 'admin', _m.PLATFORM_ROW):
        return _m._err('kernel の行は削除できません')
    with _m._db() as (cur, conn):
        row = _m._load_registry_row(cur, app_name)
        if not row:
            return _m._err('レジストリにありません', 404)
        n = {}
        for tbl in ('app_share_documents', 'app_share_tables', 'app_share_issues'):
            cur.execute(f"DELETE FROM {tbl} WHERE app_name=%s", (app_name,))
            n[tbl] = cur.rowcount
        cur.execute("DELETE FROM app_share_registry WHERE app_name=%s", (app_name,))
        conn.commit()
    return _m._ok(message=f'「{row.get("display_name") or app_name}」をレジストリから削除しました（ファイルは残ります）',
                  deleted=n)


# ============================================================
# 全アプリパッケージ（overview v3）
# ============================================================

OVERVIEW_TYPE = 'fujinp_apps_overview'


def _all_app_doc_status(cur, tidy=False):
    """kind='app' の全アプリについて，文書とコードの整合を並べる（点検ページ用）．
    tidy=True なら判定の前に各アプリを整形する（admin の点検ページから呼ぶとき）"""
    cur.execute("""SELECT app_name, display_name, icon, enabled, version_id
                   FROM app_share_registry WHERE kind='app' ORDER BY sort_order, id""")
    rows = cur.fetchall()
    skew = _db_clock_skew(cur)
    out = []
    for r in rows:
        if tidy:
            _t.tidy_app(r['app_name'])
        st = _doc_status(cur, r['app_name'], skew=skew)
        out.append({'app_name': r['app_name'], 'display_name': r.get('display_name') or r['app_name'],
                    'icon': r.get('icon') or '📦', 'enabled': int(r.get('enabled') or 0),
                    'version_id': r.get('version_id'),
                    'latest_file_mtime': st['latest_file_mtime'],
                    'code_files': st['code_files'], 'judged_by': st['judged_by'],
                    'diff_count': len(st['diff']['added']) + len(st['diff']['removed']) + len(st['diff']['changed']),
                    'manual': st['docs']['manual'], 'spec': st['docs']['spec'],
                    'missing': st['missing'], 'stale': st['any_stale']})
    return out


EXCLUDED_NOTE = ('アプシャ（app_share）自身とカーネルは，一括の書き出しに含めない．'
                 '書き出しと取り込みを担うアプリを一括で入れ替えると，作業の足場が'
                 '途中で崩れるため．配布は個別のアプリパッケージで行う．')


def _excluded_apps(cur):
    """一括の書き出しに含めない行（kind='kernel'）．アプシャ自身を含む．"""
    cur.execute("""SELECT app_name, display_name, icon FROM app_share_registry
                   WHERE kind='kernel' ORDER BY sort_order, id""")
    return [{'app_name': r['app_name'],
             'display_name': r.get('display_name') or r['app_name'],
             'icon': r.get('icon') or '⚙'} for r in cur.fetchall()]


@app_share_bp.route('/api/docs/status_all')
@login_required
def api_docs_status_all():
    with _m._db() as (cur, conn):
        apps = _all_app_doc_status(cur, tidy=_is_admin())
    return _m._ok(apps=apps, checked_at=_m._fmt(_m._now()))


@app_share_bp.route('/package/export_all', methods=['GET'])
@login_required
def package_export_all():
    """全アプリの文書を1本ずつ点検するページ．書き出しは POST（点検済みの選択を添える）."""
    with _m._db() as (cur, conn):
        apps = _all_app_doc_status(cur, tidy=True)
        excluded = _excluded_apps(cur)
    return render_template('app_share_export_all_gate.html', apps=apps,
                           excluded=excluded, excluded_note=EXCLUDED_NOTE,
                           checked_at=_m._fmt(_m._now()))


@app_share_bp.route('/package/export_all', methods=['POST'])
@login_required
def package_export_all_download():
    """decisions={app_name: 'ok'|'later'} を受け，全アプリについて確認済みであることを
    サーバ側でも検めてから fujinp_apps_overview を返す．
      ok    … 文書がコードと一致（サーバの判定と一致していること）
      later … 古い／未登録のまま出すことを人が選んだ
    未確認のアプリ，または ok と申告されたのに古いアプリがあれば書き出さない．"""
    body = request.get_json(silent=True) or {}
    decisions = body.get('decisions') or {}
    if not isinstance(decisions, dict):
        return _m._err('選択の形式が不正です')
    with _m._db() as (cur, conn):
        cur.execute("SELECT full_name FROM users WHERE id=%s", (session.get('user_id'),))
        u = cur.fetchone()
        by = (u or {}).get('full_name')
        status = _all_app_doc_status(cur, tidy=True)
        excluded = _excluded_apps(cur)
        problems = []
        for a in status:
            d = (decisions.get(a['app_name']) or '').lower()
            if d not in ('ok', 'later'):
                problems.append({'app_name': a['app_name'], 'reason': '未確認'})
            elif d == 'ok' and a['stale']:
                problems.append({'app_name': a['app_name'], 'reason': '「一致」と申告されたが文書がコードと合っていない／未登録'})
        if problems:
            return jsonify({'success': False, 'error': '点検が済んでいないアプリがあります',
                            'problems': problems}), 409
        apps = []
        for a in status:
            pkg = build_package(cur, a['app_name'], generated_by=by,
                                docs_choice=(decisions.get(a['app_name']) or '').lower())
            if pkg:
                apps.append(pkg)
    n_later = sum(1 for a in status if (decisions.get(a['app_name']) or '').lower() == 'later')
    out = {'export_type': OVERVIEW_TYPE, 'format_version': FORMAT_VERSION,
           'site_name': getattr(Config, 'DB_ACCOUNT', ''),
           'site_url': request.host_url.rstrip('/'),
           'generated_at': _m._fmt(_m._now()), 'generated_by': by,
           'app_count': len(apps), 'apps': apps,
           'excluded': excluded, 'excluded_note': EXCLUDED_NOTE,
           'doc_review': {'reviewed': True, 'reviewed_at': _m._fmt(_m._now()),
                          'current': len(apps) - n_later, 'deferred': n_later,
                          'deferred_apps': [a['app_name'] for a in status
                                            if (decisions.get(a['app_name']) or '').lower() == 'later']}}
    fn = f'fujinp_apps_overview_{_m._now().strftime("%Y%m%d_%H%M%S")}.json'
    return Response(json.dumps(out, ensure_ascii=False, indent=2), mimetype='application/json',
                    headers={'Content-Disposition': f'attachment; filename="{fn}"'})


@app_share_bp.route('/package/export_all/public', methods=['GET'])
@login_required
def package_export_all_public():
    """全アプリパッケージを点検なしでそのまま書き出す（ログインした全員）．
    非公開（disclosed=0）のアプリは含めない．文書の新旧はサーバの判定で ok／later を付ける．"""
    with _m._db() as (cur, conn):
        cur.execute("SELECT full_name FROM users WHERE id=%s", (session.get('user_id'),))
        u = cur.fetchone()
        by = (u or {}).get('full_name')
        try:
            cur.execute("SELECT app_name FROM app_share_registry WHERE kind='app' AND COALESCE(disclosed,1)=1")
        except Exception:
            cur.execute("SELECT app_name FROM app_share_registry WHERE kind='app'")
        disclosed = {r['app_name'] for r in cur.fetchall()}
        status = [a for a in _all_app_doc_status(cur) if a['app_name'] in disclosed]
        excluded = _excluded_apps(cur)
        apps, deferred = [], []
        for a in status:
            choice = 'later' if a['stale'] else 'ok'
            pkg = build_package(cur, a['app_name'], generated_by=by, docs_choice=choice)
            if pkg:
                pkg['doc_update']['public_download'] = True
                apps.append(pkg)
                if choice == 'later':
                    deferred.append(a['app_name'])
    out = {'export_type': OVERVIEW_TYPE, 'format_version': FORMAT_VERSION,
           'site_name': getattr(Config, 'DB_ACCOUNT', ''),
           'site_url': request.host_url.rstrip('/'),
           'generated_at': _m._fmt(_m._now()), 'generated_by': by,
           'app_count': len(apps), 'apps': apps,
           'excluded': excluded, 'excluded_note': EXCLUDED_NOTE,
           'public_download': True,
           'doc_review': {'reviewed': False, 'reviewed_at': None,
                          'note': '点検を経ずに書き出した版．文書の新旧はサーバの判定による．',
                          'current': len(apps) - len(deferred), 'deferred': len(deferred),
                          'deferred_apps': deferred}}
    fn = f'fujinp_apps_overview_{_m._now().strftime("%Y%m%d_%H%M%S")}.json'
    return Response(json.dumps(out, ensure_ascii=False, indent=2), mimetype='application/json',
                    headers={'Content-Disposition': f'attachment; filename="{fn}"'})


@app_share_bp.route('/package/site/check', methods=['POST'])
@login_required
def package_site_check():
    """全アプリパッケージの一覧検証．各アプリの v3 パッケージを個別検証にかける前の目安．"""
    ov = request.get_json(silent=True)
    if not isinstance(ov, dict) or ov.get('export_type') != OVERVIEW_TYPE:
        return _m._err('全アプリパッケージ（fujinp_apps_overview）ではありません')
    rows = []
    with _m._db() as (cur, conn):
        for pkg in ov.get('apps') or []:
            if not isinstance(pkg, dict) or pkg.get('export_type') != EXPORT_TYPE:
                continue
            name = pkg.get('app_name') or ''
            if not _m._valid_app(name):
                continue
            row = _m._load_registry_row(cur, name)
            local_ts = (row or {}).get('updated_at')
            pkg_ts = _m._routes._parse_ts(pkg.get('updated_at'))
            if not row:
                st = 'new'
            elif local_ts and pkg_ts:
                st = 'newer' if pkg_ts > local_ts else ('older' if pkg_ts < local_ts else 'same')
            else:
                st = 'unknown'
            rows.append({'app_name': name, 'display_name': pkg.get('display_name') or name,
                         'icon': pkg.get('icon') or '📦',
                         'package_version': pkg.get('version_id'), 'local_version': (row or {}).get('version_id'),
                         'package_updated_at': pkg.get('updated_at'), 'local_updated_at': _m._fmt(local_ts),
                         'dir_exists': os.path.isdir(_m._app_path(name)),
                         'file_count': len(pkg.get('files') or []), 'status': st})
    return _m._ok(apps=rows, site={'site_name': ov.get('site_name'), 'generated_at': ov.get('generated_at'),
                                   'generated_by': ov.get('generated_by')})
