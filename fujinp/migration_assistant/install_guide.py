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
まいあし：別サイトへのインストール

新品の PythonAnywhere に FUJIN-P を立ち上げるための配布物と手引きを，
1つの画面にまとめて置く．中身は data_for_distribution/ の4ファイル．

  install_overview.md        全体マニュアル（2段階の見取り図）
  install_stage1.md          第1段階マニュアル（種の立ち上げ）
  fujinp_kernel_install.py   第1段階で使う導入スクリプト（配布物）
  install_stage2.md          第2段階マニュアル（アプシャでの取り込み）
  legacy_docs_202607.md      まいあしの旧文書（保存用）

導入スクリプトは build_installer.py で，いま動いているサイトのカーネルから作り直す．
画面はこのファイルの中で組み立てる（テンプレートファイルを持たない）．
まいあし本体の教材・進捗の仕組みとは独立していて，相互に依存しない．
"""

import datetime
import os

from flask import (abort, render_template_string, send_file, session, url_for)

from . import migration_assistant

DIST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data_for_distribution')

INSTALLER_NAME = 'fujinp_kernel_install.py'

# key: (ファイル名, 表題, 一行の説明)
DOCS = {
    'overview': ('install_overview.md', '全体マニュアル',
                 '2つの段階と，手で行う4つの設定の見取り図．最初にこれを読む．'),
    'stage1': ('install_stage1.md', '第1段階マニュアル',
               'カーネルとアプシャだけの「種」を立ち上げる．ファイル展開からログインまで．'),
    'stage2': ('install_stage2.md', '第2段階マニュアル',
               '立ち上がったアプシャで，アプリのパッケージを1本ずつ取り込む．'),
    'legacy': ('legacy_docs_202607.md', 'まいあし 旧文書（2026年7月版）',
               '正本化より前のユーザマニュアルと技術仕様書．控えとして残してある．'),
}


def _logged_in():
    return bool(session.get('user_id'))


def _read(name):
    path = os.path.join(DIST_DIR, name)
    if not os.path.isfile(path):
        return None
    with open(path, encoding='utf-8') as f:
        return f.read()


def _installer_info():
    path = os.path.join(DIST_DIR, INSTALLER_NAME)
    if not os.path.isfile(path):
        return None
    st = os.stat(path)
    jst = datetime.timezone(datetime.timedelta(hours=9), 'JST')
    return {
        'size': st.st_size,
        'mtime': datetime.datetime.fromtimestamp(st.st_mtime, jst).strftime('%Y-%m-%d %H:%M'),
    }


def _to_html(md_text):
    """Markdown を HTML に．markdown_converter が使えないときは素の markdown に落ちる．"""
    try:
        from markdown_converter import process_markdown
        return process_markdown(md_text, session.get('user_category') or 'guest')
    except Exception:
        try:
            import markdown as _md
            return _md.markdown(md_text, extensions=['fenced_code', 'tables'])
        except Exception:
            import html as _h
            return '<pre>' + _h.escape(md_text) + '</pre>'


# ============================================================
# 画面
# ============================================================

_BASE_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Hiragino Sans","Noto Sans JP",sans-serif;
     background:linear-gradient(135deg,#6366f1 0%,#8b5cf6 100%);min-height:100vh;padding:18px;color:#1f2937}
.container{max-width:960px;margin:0 auto}
.header{background:#fff;padding:18px 24px;border-radius:10px;margin-bottom:16px;box-shadow:0 2px 4px rgba(0,0,0,.1)}
.header h1{font-size:1.35rem;margin-bottom:4px}
.header .sub{color:#666;font-size:.85rem;line-height:1.6}
.hdr-btns{float:right;display:flex;gap:8px;flex-wrap:wrap}
.hdr-btns a{padding:8px 14px;border-radius:5px;text-decoration:none;font-size:.82rem;font-weight:600;color:#fff;background:#64748b}
.hdr-btns a.home{background:#ef4444}
.panel{background:#fff;padding:22px 26px;border-radius:10px;box-shadow:0 2px 4px rgba(0,0,0,.1);margin-bottom:16px}
.lead{font-size:.9rem;color:#444;line-height:1.9}
.steps{display:grid;grid-template-columns:1fr;gap:12px;margin-top:16px}
.step{border:1px solid #e5e7eb;border-radius:8px;padding:16px 18px}
.step .no{display:inline-block;background:#4f46e5;color:#fff;border-radius:999px;
          width:26px;height:26px;line-height:26px;text-align:center;font-size:.8rem;font-weight:700;margin-right:8px}
.step h2{display:inline;font-size:1.02rem}
.step p{margin:10px 0 12px;color:#555;font-size:.87rem;line-height:1.8}
.btn{display:inline-block;padding:9px 16px;border-radius:6px;text-decoration:none;
     font-size:.85rem;font-weight:600;border:1px solid transparent}
.btn.read{background:#eef2ff;color:#3730a3;border-color:#c7d2fe}
.btn.dl{background:#4f46e5;color:#fff}
.btn + .btn{margin-left:8px}
.meta{color:#888;font-size:.78rem;margin-left:10px}
.note{margin-top:18px;background:#f1f3f5;border-left:4px solid #868e96;border-radius:4px;
      padding:12px 14px;color:#495057;font-size:.85rem;line-height:1.8}
.doc{background:#fff;padding:26px 30px;border-radius:10px;box-shadow:0 2px 4px rgba(0,0,0,.1);line-height:1.9}
.doc h2{margin:24px 0 12px;font-size:1.2rem;border-bottom:2px solid #eef2ff;padding-bottom:6px}
.doc h3{margin:20px 0 10px;font-size:1.03rem}
.doc p{margin:10px 0;font-size:.92rem}
.doc ul,.doc ol{margin:10px 0 10px 22px;font-size:.92rem}
.doc li{margin:4px 0}
.doc code{background:#f3f4f6;padding:2px 5px;border-radius:4px;font-size:.86em}
.doc pre{background:#111827;color:#e5e7eb;padding:14px 16px;border-radius:8px;overflow-x:auto;margin:12px 0}
.doc pre code{background:none;color:inherit;padding:0}
.doc table{border-collapse:collapse;margin:12px 0;font-size:.88rem}
.doc th,.doc td{border:1px solid #e5e7eb;padding:6px 10px;text-align:left}
.doc th{background:#f9fafb}
.missing{background:#fff5f5;border:1px solid #fecaca;color:#991b1b;border-radius:8px;padding:14px 16px;font-size:.88rem}
"""

_INDEX_HTML = """<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>別サイトへのインストール — まいあし</title><style>{{ css }}</style></head>
<body><div class="container">

  <div class="header">
    <div class="hdr-btns">
      <a href="{{ url_for('migration_assistant.index') }}">👣 まいあしへ</a>
      <a class="home" href="{{ url_for('migration_assistant.return_to_fujin') }}">🏠 ダッシュボード</a>
    </div>
    <h1>🚚 別サイトへのインストール</h1>
    <div class="sub">新品の PythonAnywhere に FUJIN-P を立ち上げ，アプリを載せるまでの配布物と手引き</div>
  </div>

  <div class="panel">
    <div class="lead">
      作業は2つの段階に分かれます．第1段階でカーネルとアプシャだけの「種」を立ち上げ，
      第2段階でそのアプシャを使ってアプリを1本ずつ取り込みます．
      間に挟まる手作業は，データベースの作成・<code>config.py</code> の記入・WSGI の設定・
      最初の admin の登録の4つだけです．
    </div>

    <div class="steps">

      <div class="step">
        <span class="no">0</span><h2>{{ docs['overview'][1] }}</h2>
        <p>{{ docs['overview'][2] }}</p>
        <a class="btn read" href="{{ url_for('migration_assistant.install_guide_doc', key='overview') }}">📖 読む</a>
        <a class="btn read" href="{{ url_for('migration_assistant.install_guide_download', key='overview') }}">⬇ .md</a>
      </div>

      <div class="step">
        <span class="no">1</span><h2>{{ docs['stage1'][1] }}</h2>
        <p>{{ docs['stage1'][2] }}</p>
        <a class="btn read" href="{{ url_for('migration_assistant.install_guide_doc', key='stage1') }}">📖 読む</a>
        <a class="btn read" href="{{ url_for('migration_assistant.install_guide_download', key='stage1') }}">⬇ .md</a>
      </div>

      <div class="step">
        <span class="no">＊</span><h2>導入スクリプト（{{ installer_name }}）</h2>
        <p>
          カーネルとアプシャの全ファイルを抱えた1本のスクリプトです．
          立ち上げ先のホームディレクトリに置き，<code>--files</code> で展開し，
          <code>config.py</code> を書いてから <code>--schema</code> でテーブルを作ります．
          <code>config.py</code> は入っていません．
        </p>
        {% if installer %}
        <a class="btn dl" href="{{ url_for('migration_assistant.install_guide_download', key='installer') }}">⬇ ダウンロード</a>
        <a class="btn read" href="{{ url_for('migration_assistant.install_guide_code') }}">👁 コードを見る</a>
        <span class="meta">{{ '{:,}'.format(installer.size) }} バイト・{{ installer.mtime }}</span>
        {% else %}
        <div class="missing">{{ installer_name }} がアプリの data_for_distribution/ にありません．</div>
        {% endif %}
      </div>

      <div class="step">
        <span class="no">2</span><h2>{{ docs['stage2'][1] }}</h2>
        <p>{{ docs['stage2'][2] }}</p>
        <a class="btn read" href="{{ url_for('migration_assistant.install_guide_doc', key='stage2') }}">📖 読む</a>
        <a class="btn read" href="{{ url_for('migration_assistant.install_guide_download', key='stage2') }}">⬇ .md</a>
      </div>

    </div>

    <div class="note">
      まいあしの旧文書（2026年7月版のユーザマニュアルと技術仕様書）は1つにまとめて
      <a href="{{ url_for('migration_assistant.install_guide_doc', key='legacy') }}">こちら</a> に置いてあります．
      正本化より前の記述なので，現行のコードとは食い違う箇所があります．
    </div>
  </div>

</div></body></html>
"""

_DOC_HTML = """<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ title }} — まいあし</title><style>{{ css }}</style></head>
<body><div class="container">

  <div class="header">
    <div class="hdr-btns">
      <a href="{{ url_for('migration_assistant.install_guide') }}">🚚 インストールへ戻る</a>
      <a class="home" href="{{ url_for('migration_assistant.return_to_fujin') }}">🏠 ダッシュボード</a>
    </div>
    <h1>{{ title }}</h1>
    <div class="sub">
      <a href="{{ url_for('migration_assistant.install_guide_download', key=key) }}">⬇ この文書を .md で受け取る</a>
    </div>
  </div>

  <div class="doc">{{ body|safe }}</div>

</div></body></html>
"""

_CODE_HTML = """<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ name }} — まいあし</title><style>{{ css }}
.code{background:#111827;color:#e5e7eb;padding:16px;border-radius:10px;overflow-x:auto;
      font-size:.78rem;line-height:1.5;max-height:70vh}
</style></head>
<body><div class="container">

  <div class="header">
    <div class="hdr-btns">
      <a href="{{ url_for('migration_assistant.install_guide') }}">🚚 インストールへ戻る</a>
      <a class="home" href="{{ url_for('migration_assistant.return_to_fujin') }}">🏠 ダッシュボード</a>
    </div>
    <h1>{{ name }}</h1>
    <div class="sub">
      先頭だけを表示しています（全体は {{ '{:,}'.format(total) }} バイト）．
      大半はカーネルとアプシャのファイルを収めた埋め込みデータです．
      <a href="{{ url_for('migration_assistant.install_guide_download', key='installer') }}">⬇ ダウンロード</a>
    </div>
  </div>

  <div class="panel"><pre class="code">{{ head }}</pre></div>

</div></body></html>
"""


@migration_assistant.route('/install')
@migration_assistant.route('/install/')
def install_guide():
    """別サイトへのインストール（配布物と手引きの入口）"""
    if not _logged_in():
        abort(403)
    return render_template_string(_INDEX_HTML, css=_BASE_CSS, docs=DOCS,
                                  installer=_installer_info(),
                                  installer_name=INSTALLER_NAME)


@migration_assistant.route('/install/doc/<key>')
def install_guide_doc(key):
    """手引きを画面で読む"""
    if not _logged_in():
        abort(403)
    if key not in DOCS:
        abort(404)
    name, title, _desc = DOCS[key]
    text = _read(name)
    if text is None:
        body = f'<div class="missing">{name} がアプリの data_for_distribution/ にありません．</div>'
    else:
        body = _to_html(text)
    return render_template_string(_DOC_HTML, css=_BASE_CSS, title=title, key=key, body=body)


@migration_assistant.route('/install/code')
def install_guide_code():
    """導入スクリプトの先頭を画面で見る（埋め込みデータの手前まで）"""
    if not _logged_in():
        abort(403)
    text = _read(INSTALLER_NAME)
    if text is None:
        abort(404)
    cut = text.find('PAYLOAD = (')
    head = text if cut < 0 else text[:cut] + 'PAYLOAD = (\n    …（埋め込みデータ）…\n)\n'
    return render_template_string(_CODE_HTML, css=_BASE_CSS, name=INSTALLER_NAME,
                                  head=head, total=len(text.encode('utf-8')))


@migration_assistant.route('/install/download/<key>')
def install_guide_download(key):
    """手引き（.md）と導入スクリプト（.py）を受け取る"""
    if not _logged_in():
        abort(403)
    if key == 'installer':
        name = INSTALLER_NAME
    elif key in DOCS:
        name = DOCS[key][0]
    else:
        abort(404)
    path = os.path.join(DIST_DIR, name)
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=name)
