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
migrate_fujinp_scions （さいまる）- ルート定義

FUJIN-P サイトのマイグレーション支援ツール。

■ エクスポーター
    実行母体の FUJIN-P サイトを「まるっとコピー」したエクスポート
    パッケージ（ZIP）を作成・レビュー・登録する。
      - 対象ファイル: ~/fujinp ~/apps ~/templates の3ディレクトリ配下の
        .html / .py ファイルと、~ 直下の .py ファイル（秘密ファイルを除く）。
        画像・CSS・JS・static 等はパッケージに含めない（手動で移行する）。
      - config.py は匿名化して config_template.py として同梱する。
      - MySQL スキーマは DB 単位の opt-in で、DB ごとに別ファイル
        （schema_default.sql, schema_fujinp.sql 等）として同梱する。
      - 登録前に config_template.py をエディタでレビュー（手動修正）できる。
      - 登録した ZIP は <module>/migration_packages/ に保存し、
        メタ情報を MySQL テーブルで管理する。

■ インポーター
    登録済みパッケージの一覧を表示し、選択した ZIP をダウンロードする。

■ 認証方針（2026-07-25 改訂）
    - エクスポーター系（/exporter, /api/export/*）と削除
      （/api/import/delete）は @admin_required。
    - パッケージの一覧・ダウンロード（/importer, /api/import/list,
      /api/import/download）は ALLOW_GUEST_DOWNLOAD の値で決まる。
        False（既定）… admin のみ。パッケージにはサイト全体のソース
                        コードが含まれるため、既定では admin 限定。
        True          … guest を含むログイン済みユーザー全員。
      guest への配布を許可する運用方針の場合のみ True にすること。
    - ハブ画面（/）と /return_to_fujin は @login_required。
    - @admin_required はログインチェックを内包するため @login_required は
      重ねない。

■ セキュリティ修正メモ（2026-07-25）
    1. Blueprint の static_folder を無効化し、パッケージ保存先を static/
       配下から移動した。以前は
       /migrate_fujinp_scions/static/migration_packages/<name>
       で未認証ダウンロードが可能だった。
    2. パッケージの一覧・取得を既定で admin 限定にした
       （ALLOW_GUEST_DOWNLOAD）。
    3. スキーマ生成に渡される DB 名を、サーバ側で列挙した owner 所有 DB
       との照合で検証するようにした（SQL への文字列連結・ZIP エントリ名
       へのパストラバーサルを防ぐ）。
    4. 状態変更 API（POST）にカスタムヘッダ必須の CSRF ガードを追加した。
    5. ZIP 収集時にシンボリックリンクを辿らないようにした
       （~/fujinp/x.py -> ~/config.py のようなリンクでの秘密漏洩を防ぐ）。
    6. 秘密情報を含みうるファイル名（config*.py, secret*.py,
       credentials*.py, *.env* 等）をパッケージ対象から除外した。
    7. API のエラー応答から内部詳細（例外文字列）を除いた。詳細はログのみ。
    8. config.py の匿名化を強化し、自動処理できなかった行に
       「⚠ 要確認」マーカーを付けるようにした。さらに登録直前に
       伏せ字漏れを再チェックする。
    9. DB から取り出したファイル名も basename 検証してから I/O する。
"""
import os
import io
import re
import ast
import sys
import time
import shutil
import zipfile
import logging
import datetime
import subprocess
import threading
from functools import wraps
from urllib.parse import urlparse

from pytz import timezone

from flask import (
    render_template, request, jsonify, session, send_from_directory
)
import mysql.connector

from db import DatabaseConfig
from decorators import login_required, admin_required
from auth import redirect_to_dashboard

from . import migrate_fujinp_scions_bp

# ---------------------------------------------------------------------------
# 定数・設定
# ---------------------------------------------------------------------------

JST = timezone('Asia/Tokyo')


def get_jst_now():
    """現在の日時を JST で取得（naive datetime）"""
    return datetime.datetime.now(JST).replace(tzinfo=None)


# ---- 配布ポリシー ----------------------------------------------------------
# パッケージの一覧・ダウンロードを、guest を含むログイン済みユーザー全員に
# 開放するかどうか。
#
#   True （現在の設定）
#       … 本サイトを AGPL v3 で公開する方針のため、利用者がソースコードを
#         受け取れる状態にしておく。GNU AGPL v3 第13条は、ネットワーク越しに
#         プログラムと対話する利用者に対して「対応するソース」を提供する
#         機会を与えることを求めており、この画面がその窓口になる。
#   False
#       … admin のみ。パッケージを社内限りの移行手段としてのみ使う場合。
#
# ※ True にする場合の注意（詳細は技術仕様書の general_notes を参照）:
#     - パッケージにはスキーマ SQL を同梱しないこと（AGPL は DB 構造の
#       開示を求めていない。ステップ2のチェックをすべて外す）。
#     - 同梱ファイルへの秘密情報の直書きが「公開」になる。ステップ4の
#       警告を force で握りつぶさないこと。
ALLOW_GUEST_DOWNLOAD = True

# まるっとコピーの対象とするトップレベルディレクトリ（ホワイトリスト）
WHITELIST_DIRS = ['fujinp', 'apps', 'templates']

# パッケージに含めるファイルの拡張子（これ以外は一切含めない）。
# 画像・フォント・データファイル等はパッケージ対象外（手動で移行する）。
#
# .js / .css を含めるのは AGPL v3 での頒布のため。ブラウザで動く
# JavaScript も「対応するソース」に含まれるので、これが欠けていると
# パッケージだけでは頒布の要件を満たせない。
PACKAGE_EXTENSIONS = ('.html', '.py', '.js', '.css')

# ソースとして中身を検査する拡張子（秘密情報スキャンの対象）
PYTHON_EXTENSIONS = ('.py',)
TEXT_SCAN_EXTENSIONS = ('.js', '.css', '.html')

# 走査時に丸ごとスキップするディレクトリ名。
#
# ※ 以前は 'static' もここにあったが、AGPL 頒布のために .js / .css を
#   同梱する必要が出たので外した。ただし static を歩くようになった結果、
#   「利用者がアップロードしたファイル」や「ビルド成果物」まで拾ってしまう
#   ため、代わりに下の2グループを名前で落とす。
#   何が落ちたかは api_export_info の skipped_dirs で画面に出す
#   （黙って落とすと、移行先で理由の分からない不具合になるため）。

# 生成物・第三者ライブラリ。入れても頒布の要件は満たさず、肥大するだけ。
SKIP_DIRS_BUILD = (
    '__pycache__', 'node_modules', 'bower_components', 'vendor',
    'site-packages', 'dist-packages', 'venv', 'virtualenv',
    'htmlcov', 'coverage', '__snapshots__', 'egg-info',
)

# 利用者のデータ置き場。ソースではないうえ、パッケージは
# ログイン済みユーザーがダウンロードできるため、混入は情報漏洩になる。
# （static/uploads/ に置かれた .js / .html / .css が典型）
SKIP_DIRS_DATA = (
    'migration_packages',
    'uploads', 'upload', 'uploaded', 'user_uploads', 'userfiles',
    'media', 'attachments', 'avatars', 'thumbnails', 'thumbs',
    'userdata', 'user_data', 'instance',
    'cache', 'caches', 'tmp', 'temp', 'logs',
)

SKIP_DIRS = SKIP_DIRS_BUILD + SKIP_DIRS_DATA

# config.py は匿名化して config_template.py に置換するため、
# .py ファイルでも特別扱いする
SECRET_PY = 'config.py'

# 秘密情報を含む可能性が高く、そのままでは同梱してはならないファイル名の
# パターン（basename を小文字化して照合する）。
# config.py だけを除外していると config_local.py / secrets.py などが
# 素通りしてしまうため広めに落とすが、広げすぎると configure_routes.py や
# config_forms.py のような通常のアプリモジュールまで消えてしまうため、
# 「設定・秘密そのもの」を指す名前だけに限定する。
# 何が除外されたかは api_export_info の excluded_secret_files で画面に出す。
SECRET_FILE_PATTERNS = (
    re.compile(r'^config\.py$'),
    re.compile(r'^config_(local|prod|production|dev|development|'
               r'staging|secret|secrets|private)\.py$'),
    re.compile(r'^local_config\.py$'),
    re.compile(r'^secret\.py$'),
    re.compile(r'^secrets\.py$'),
    re.compile(r'^[a-z0-9_]+_secrets?\.py$'),
    re.compile(r'^credential\.py$'),
    re.compile(r'^credentials\.py$'),
    re.compile(r'^[a-z0-9_]+_credentials?\.py$'),
    re.compile(r'^local_settings\.py$'),
    re.compile(r'^settings_local\.py$'),
    re.compile(r'^\.env.*$'),
    re.compile(r'^.+\.env$'),
    re.compile(r'^secrets?\.js$'),
    re.compile(r'^credentials?\.js$'),
    re.compile(r'^config_secret\.js$'),
    # さいまる自身が生成して同梱するファイル。ホームに実物が置かれて
    # いると、生の（伏せ字前の）内容が同名で ZIP に二重登録される。
    re.compile(r'^config_template\.py$'),
)

# パッケージ内で「さいまるが生成して writestr する」名前。
# 収集した実ファイルがこれらと衝突すると、ZIP に同名エントリが2つでき、
# 展開時にどちらが残るかが処理系依存になる（＝伏せ字前の実物が残りうる）。
GENERATED_NAMES = ('config_template.py', 'requirements.txt')
GENERATED_NAME_RE = re.compile(r'^schema_[A-Za-z0-9_-]+\.sql$')


def _is_generated_name(rel_path: str) -> bool:
    """ZIP 内パスが、さいまるの生成物と衝突する名前か"""
    norm = rel_path.replace(os.sep, '/')
    if '/' in norm:
        return False              # トップレベルの名前だけが衝突しうる
    return norm in GENERATED_NAMES or bool(GENERATED_NAME_RE.match(norm))

# 拡張子は .py / .html ではないが、パッケージに含めたいトップレベルの
# ファイル（大文字小文字は問わない）。
# AGPL v3 などで頒布する場合、ライセンス本文が同梱されていないと
# 受け取った側が条件を確認できないため。
EXTRA_TOPLEVEL_FILES = (
    'license', 'license.txt', 'license.md',
    'copying', 'copying.txt',
    'notice', 'notice.txt',
    'authors', 'authors.txt',
)

# 「変更するのに適した形式」ではない可能性が高いファイル（ビルド成果物）。
# AGPL v3 が求める「対応するソース」は、圧縮・結合された成果物ではなく
# 元のソースと生成手順のほう。同梱はするが、画面で注意を促す。
MINIFIED_RE = re.compile(
    r'('
    r'\.min\.(js|css)'                       # jquery.min.js
    r'|[-._~](bundle|chunk|pack|runtime|polyfills)\.(js|css)'
    r'|^(bundle|chunk|runtime|polyfills|vendors?)\.(js|css)'
    r'|-min\.(js|css)'
    r'|[-.][0-9a-f]{8,}\.(js|css)'            # main.a3f9c2b1.js（内容ハッシュ）
    r'|~[a-z0-9]+\.[0-9a-f]{4,}\.(js|css)'    # runtime~main.4d2f.js
    r')$',
    re.IGNORECASE
)

# ファイル名で判別できないビルド成果物（ckeditor.js など）を拾うための
# 内容ヒューリスティック。1行がこの長さを超えていたら圧縮済みとみなす。
MINIFIED_LINE_LEN = 500
MINIFIED_SNIFF_BYTES = 16384

# レビュー対象ファイル（パッケージ登録前に手動で編集・確認できるファイル）
# admin.py は無害なためレビュー対象外（トップレベル .py としてそのまま同梱）。
REVIEW_TARGETS = ['config_template.py']

# パッケージ ZIP の保存ディレクトリ
#   ※ static/ 配下に置くと Blueprint の静的配信で未認証ダウンロードが
#     できてしまうため、static の外に置くこと。
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
PACKAGE_DIR = os.path.join(_MODULE_DIR, 'migration_packages')

# 旧バージョンの保存先（static 配下）。既存パッケージの読み出し・削除の
# ためだけに参照する。新規保存には使わない。
LEGACY_PACKAGE_DIR = os.path.join(_MODULE_DIR, 'static', 'migration_packages')

# メタ情報テーブル
PACKAGE_TABLE = 'migrate_fujinp_scions_packages'

# DB 名として許可する文字（PythonAnywhere は 'owner$name' 形式）
_DB_NAME_RE = re.compile(r'^[A-Za-z0-9_$-]+$')

# ZIP ファイル名として許可する形（メタ情報テーブル由来の値も検証する）
_ZIP_NAME_RE = re.compile(r'^[A-Za-z0-9_.-]+\.zip$')

# pip freeze 1 回あたりのタイムアウト（秒）。
# 混雑した PythonAnywhere ワーカーでは 15 秒では足りないことがあるため
# 少し長めに取り、代わりに全体の締め切り（PIP_FREEZE_DEADLINE）を設けて
# 全滅時にリクエストが数分ブロックするのを防ぐ。
PIP_FREEZE_TIMEOUT = 30
PIP_FREEZE_DEADLINE = 90        # 候補の総当たり全体に使ってよい秒数

# 同梱ファイルの秘密情報スキャンで、1ファイルあたり読み込む上限バイト数
SECRET_SCAN_MAX_BYTES = 1024 * 1024

# 画面に返す秘密情報スキャン結果の最大件数（多すぎても読めないため）
SECRET_SCAN_MAX_HITS = 50

# 1ファイルあたりの最大件数。1つのファイルが上限を食い尽くして、
# 他のファイルの本物の検出が押し出されるのを防ぐ。
SECRET_SCAN_MAX_HITS_PER_FILE = 5

# .js / .css の検査で、これ未満の長さの値は秘密とみなさない。
# キーが keyCode / apiVersion のような場合の誤検知を減らすため。
TEXT_SCAN_MIN_VALUE_LEN = 8


# ---------------------------------------------------------------------------
# 共通ユーティリティ（エラー応答・CSRF・権限）
# ---------------------------------------------------------------------------

def _fail(user_msg: str, status: int = 500, exc: Exception = None,
          context: str = ''):
    """
    API のエラー応答を作る。

    例外の文字列には DB 名・テーブル名・サーバのパス・接続情報などが
    含まれうるため、クライアントには返さない。詳細はログにのみ残す。
    """
    if exc is not None:
        logging.error("%s: %s", context or 'migrate_fujinp_scions', exc,
                      exc_info=True)
    return jsonify({'success': False, 'error': user_msg}), status


def csrf_guard(f):
    """
    状態変更を伴う POST への簡易 CSRF ガード。

    2つの条件で守る:
      1. カスタムヘッダ X-Requested-With: XMLHttpRequest を必須にする。
         このヘッダはクロスオリジンの <form> 送信では付けられず、
         fetch/XHR で付けると CORS プリフライトが発生するため、
         同一オリジン以外からは通らない。
      2. Origin / Referer が付いている場合、ホストが一致することを確認する。

    サイト全体の CSRF トークン基盤に手を入れずに、フォーム送信型の
    CSRF（特に /api/import/delete）を塞ぐことを目的とする。
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
            logging.warning("CSRF guard: X-Requested-With 欠落 path=%s",
                            request.path)
            return jsonify({
                'success': False,
                'error': 'リクエストが不正です。画面を再読み込みしてから'
                         'もう一度お試しください。'
            }), 400

        origin = request.headers.get('Origin') or request.headers.get('Referer')
        if origin:
            try:
                if urlparse(origin).netloc != request.host:
                    logging.warning("CSRF guard: オリジン不一致 origin=%s "
                                    "host=%s", origin, request.host)
                    return jsonify({'success': False,
                                    'error': 'リクエスト元が不正です。'}), 400
            except Exception:
                return jsonify({'success': False,
                                'error': 'リクエスト元が不正です。'}), 400
        return f(*args, **kwargs)
    return wrapper


def package_access_required(f):
    """
    パッケージの一覧・ダウンロードに対する権限デコレータ。

    ALLOW_GUEST_DOWNLOAD の値に応じて login_required / admin_required を
    切り替える。判定はリクエストごとに行う。
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        if ALLOW_GUEST_DOWNLOAD:
            return login_required(f)(*args, **kwargs)
        return admin_required(f)(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# 環境ヘルパー
# ---------------------------------------------------------------------------

def get_home_dir() -> str:
    """owner のホームディレクトリを返す（PythonAnywhere: /home/username）"""
    return os.path.expanduser('~')


def get_owner_name() -> str:
    """PythonAnywhere アカウント名を返す"""
    return os.path.basename(get_home_dir())


_legacy_migrated = False
_legacy_lock = threading.Lock()


def migrate_legacy_packages() -> list:
    """
    旧保存先（static/migration_packages/）に残っている ZIP を
    新しい保存先へ移動する。

    旧保存先は Blueprint の静的配信で未認証ダウンロードができていた場所。
    static_folder を無効化しただけでは、ファイル自体はその名前の
    ディレクトリに残り続ける（別の静的マッピングや将来の設定変更で
    再び露出しうる）。プロセス起動後の初回アクセス時に1度だけ移す。

    Returns:
        移動したファイル名のリスト
    """
    global _legacy_migrated
    moved = []
    # ロックを取り、移行が完全に終わってからフラグを立てる。
    # 先にフラグを立てると、移行中の別スレッドが「移行済み」と誤認して
    # 旧保存先のパスを掴み、その直後に消える（TOCTOU）。
    # 失敗時はフラグを立てず、次回のアクセスで再試行する。
    with _legacy_lock:
        if _legacy_migrated:
            return moved
        moved = _migrate_legacy_packages_locked()
        _legacy_migrated = True
    return moved


def _migrate_legacy_packages_locked() -> list:
    """migrate_legacy_packages の実処理（ロック保持中に呼ぶ）"""
    moved = []
    if not os.path.isdir(LEGACY_PACKAGE_DIR):
        return moved

    try:
        os.makedirs(PACKAGE_DIR, exist_ok=True)
        for name in sorted(os.listdir(LEGACY_PACKAGE_DIR)):
            src = os.path.join(LEGACY_PACKAGE_DIR, name)
            if not os.path.isfile(src):
                continue
            dst = os.path.join(PACKAGE_DIR, name)
            if os.path.exists(dst):
                # 新旧に同名がある場合は旧側を消さずに残す（要手動確認）
                logging.warning("旧保存先に同名ファイルが残っています: %s", src)
                continue
            try:
                shutil.move(src, dst)
                moved.append(name)
            except Exception as e:
                logging.error("旧パッケージの移動に失敗 %s: %s", src, e)
        if moved:
            logging.warning(
                "旧保存先（static 配下）から %d 件のパッケージを移動しました: %s"
                " ／ このディレクトリに置かれていた ZIP は過去に未認証で"
                "取得できた可能性があります。公開期間中に登録された"
                "パッケージに含まれる認証情報は再発行を検討してください。",
                len(moved), ', '.join(moved))
    except Exception as e:
        logging.error("旧パッケージの移動処理でエラー: %s", e)
    return moved


def ensure_package_dir() -> str:
    """パッケージ保存ディレクトリの存在を保証して返す"""
    os.makedirs(PACKAGE_DIR, exist_ok=True)
    migrate_legacy_packages()
    return PACKAGE_DIR


def _safe_zip_name(filename: str) -> str:
    """
    メタ情報テーブル由来のファイル名を検証して返す。

    DB の値であってもファイルシステム操作の入力になるため、
    パス区切り・親ディレクトリ参照が混じっていないことを必ず確認する。
    """
    if not filename:
        raise ValueError('ファイル名が空です')
    base = os.path.basename(filename)
    if base != filename or not _ZIP_NAME_RE.match(base):
        raise ValueError('不正なファイル名です: %r' % (filename,))
    return base


def _resolve_package_path(filename: str):
    """
    パッケージ ZIP の実体があるディレクトリとファイル名を返す。

    新しい保存先（PACKAGE_DIR）を優先し、無ければ旧保存先
    （LEGACY_PACKAGE_DIR）を見る。どちらにも無ければ (None, name)。
    """
    name = _safe_zip_name(filename)
    for directory in (ensure_package_dir(), LEGACY_PACKAGE_DIR):
        if os.path.isfile(os.path.join(directory, name)):
            return directory, name
    return None, name


def _is_secret_filename(basename: str) -> bool:
    """秘密情報を含みうるためパッケージに入れてはならないファイル名か"""
    low = basename.lower()
    return any(p.match(low) for p in SECRET_FILE_PATTERNS)


def _is_package_file(basename: str) -> bool:
    """
    このファイル名をパッケージに含めるべきか判定する。
      - 拡張子が PACKAGE_EXTENSIONS（.html / .py）のもののみ採用
      - '.' で始まる隠しファイルは除外
      - 秘密情報を含みうる名前（config*.py 等）は除外
    """
    if basename.startswith('.'):
        return False
    if not basename.lower().endswith(PACKAGE_EXTENSIONS):
        return False
    if _is_secret_filename(basename):
        return False
    return True


# ---------------------------------------------------------------------------
# 採用ファイルの収集（ホワイトリスト方式）
# ---------------------------------------------------------------------------

def collect_toplevel_py() -> list:
    """
    ~ 直下の .py ファイル（秘密ファイルを除く）と、ライセンス等の
    EXTRA_TOPLEVEL_FILES を [(絶対パス, ZIP内相対パス), ...] で返す。

    セキュリティ:
      - config.py だけでなく config_local.py / secrets.py 等も除外する。
      - シンボリックリンクは辿らない（リンク先が ~ の外や秘密ファイルを
        指している可能性があるため）。
    """
    home = get_home_dir()
    result = []
    for name in sorted(os.listdir(home)):
        is_py = name.endswith('.py')
        is_extra = name.lower() in EXTRA_TOPLEVEL_FILES
        if not is_py and not is_extra:
            continue
        if is_py and (name == SECRET_PY or _is_secret_filename(name)):
            continue
        full = os.path.join(home, name)
        if os.path.islink(full):
            logging.info("シンボリックリンクのためスキップ: %s", full)
            continue
        if os.path.isfile(full):
            result.append((full, name))
    return result


def collect_whitelist_dirs() -> list:
    """
    WHITELIST_DIRS の各ディレクトリ配下を再帰的に走査し、
    .html / .py ファイルだけを [(絶対パス, ZIP内相対パス), ...] で返す。

    - 採用するのは .html / .py のみ（PACKAGE_EXTENSIONS）。
      画像・CSS・JS・データファイル等は含めない。
    - __pycache__ / static / migration_packages（SKIP_DIRS）は丸ごと
      スキップ。※ さいまる自身のパッケージ置き場が巻き込まれる肥大も防ぐ。
    - '.' で始まる隠しファイル・隠しディレクトリは除外。
    - 秘密情報を含みうる名前のファイルは除外。
    - 走査中に見つかったシンボリックリンク（ファイル・ディレクトリとも）は
      辿らない。~/fujinp/settings.py -> ~/config.py のようなリンクがあると、
      除外したはずの秘密ファイルが同梱されてしまうため。
    - ただし WHITELIST_DIRS のルート自体がシンボリックリンクである場合は
      辿る。~/apps -> /home/x/real_apps のように、プロジェクト本体を
      ホーム外に置いてリンクする構成は珍しくない。ここを拒否すると
      「ディレクトリはあるのに 0 ファイル」という取りこぼしになる。
      ZIP 内のパスは top（fujinp/ apps/ templates/）起点で組み立てる。
    """
    home = get_home_dir()
    result = []
    for top in WHITELIST_DIRS:
        base = os.path.join(home, top)
        if not os.path.isdir(base):
            continue
        # ルートがリンクでも中身を採る。以降の相対パスはこの実体から測る。
        real_base = os.path.realpath(base)
        for root, dirs, files in os.walk(real_base, topdown=True,
                                         followlinks=False):
            # スキップ対象・隠し・仮想環境・シンボリックリンクを枝刈り
            dirs[:] = [d for d in sorted(dirs)
                       if _skip_reason(d, os.path.join(root, d)) is None]
            for filename in sorted(files):
                if not _is_package_file(filename):
                    continue
                full = os.path.join(root, filename)
                if os.path.islink(full):
                    logging.info("シンボリックリンクのためスキップ: %s", full)
                    continue
                # ZIP 内では top を起点にした相対パス（fujinp/... 等）にする
                rel = os.path.join(top, os.path.relpath(full, real_base))
                result.append((full, rel))
    return result


def _is_venv_dir(path: str) -> bool:
    """Python 仮想環境のルートか（pyvenv.cfg があるか）"""
    try:
        return os.path.isfile(os.path.join(path, 'pyvenv.cfg'))
    except Exception:
        return False


def _skip_reason(dirname: str, fullpath: str):
    """このディレクトリを丸ごと落とすなら理由を返す。落とさないなら None。"""
    if dirname.startswith('.'):
        return '隠しディレクトリ'
    if dirname in SKIP_DIRS_BUILD:
        return '生成物・第三者ライブラリ'
    if dirname in SKIP_DIRS_DATA:
        return '利用者データ・キャッシュ置き場'
    if _is_venv_dir(fullpath):
        return 'Python 仮想環境（pyvenv.cfg あり）'
    if os.path.islink(fullpath):
        return 'シンボリックリンク'
    return None


def collect_skipped_dirs() -> list:
    """
    走査時に丸ごと落としたディレクトリを [{'path':…, 'reason':…}, …] で返す。

    黙って落とすと「なぜか移行先に無い」という事故になるため、
    画面に出すための情報を集める。隠しディレクトリは数が多く自明なので
    含めない。
    """
    home = get_home_dir()
    skipped = []
    for top in WHITELIST_DIRS:
        base = os.path.join(home, top)
        if not os.path.isdir(base):
            continue
        real_base = os.path.realpath(base)
        for root, dirs, _files in os.walk(real_base, topdown=True,
                                          followlinks=False):
            keep = []
            for d in sorted(dirs):
                full = os.path.join(root, d)
                reason = _skip_reason(d, full)
                if reason is None:
                    keep.append(d)
                elif reason != '隠しディレクトリ':
                    rel = os.path.join(top, os.path.relpath(full, real_base))
                    skipped.append({'path': rel.replace(os.sep, '/'),
                                    'reason': reason})
            dirs[:] = keep
    return skipped


def _looks_minified(full: str) -> bool:
    """
    ファイルの冒頭を読み、極端に長い行があれば圧縮済みとみなす。
    ckeditor.js のように名前からは判別できない成果物を拾うため。
    """
    try:
        with open(full, 'r', encoding='utf-8', errors='replace') as f:
            head = f.read(MINIFIED_SNIFF_BYTES)
    except Exception:
        return False
    return any(len(line) > MINIFIED_LINE_LEN for line in head.split('\n'))


def is_minified_file(full: str, rel: str) -> bool:
    """ビルド成果物（変更するのに適した形式ではないもの）か"""
    if MINIFIED_RE.search(os.path.basename(rel)):
        return True
    if rel.lower().endswith(('.js', '.css')):
        return _looks_minified(full)
    return False


def collect_minified_files(files) -> list:
    """
    同梱ファイルのうち、圧縮・結合された成果物らしきものを返す。

    AGPL v3 が求めるのは「変更するのに適した形式」であって、
    .min.js のような成果物ではない。同梱自体は妨げないが、
    元ソースを別途用意すべきかどうかの判断材料として画面に出す。
    """
    return sorted(rel.replace(os.sep, '/') for full, rel in files
                  if is_minified_file(full, rel))


def collect_excluded_secret_files() -> list:
    """
    「秘密ファイルの名前」に該当してパッケージから除外されたファイルの
    一覧を返す（ホームからの相対パス）。

    黙って落とすと、移行先で理由の分からない ImportError になる。
    画面で見えるようにするための情報収集。
    """
    home = get_home_dir()
    excluded = []

    for name in sorted(os.listdir(home)):
        if name.endswith('.py') and _is_secret_filename(name):
            excluded.append(name)

    for top in WHITELIST_DIRS:
        base = os.path.join(home, top)
        if not os.path.isdir(base):
            continue
        real_base = os.path.realpath(base)
        for root, dirs, files in os.walk(real_base, topdown=True,
                                         followlinks=False):
            dirs[:] = [d for d in sorted(dirs)
                       if _skip_reason(d, os.path.join(root, d)) is None]
            for filename in sorted(files):
                if filename.startswith('.'):
                    continue
                if not filename.lower().endswith(PACKAGE_EXTENSIONS):
                    continue
                if _is_secret_filename(filename):
                    excluded.append(os.path.join(
                        top, os.path.relpath(os.path.join(root, filename),
                                             real_base)))
    return excluded


def collect_package_files() -> list:
    """
    パッケージに含める実ファイルの一覧を返す。
    （config_template.py / requirements.txt / schema_*.sql は
      別途 writestr で同梱するため含まない）

    Returns:
        [(絶対パス, ZIP内相対パス), ...]
    """
    return collect_toplevel_py() + collect_whitelist_dirs()


# ---------------------------------------------------------------------------
# config.py の匿名化
# ---------------------------------------------------------------------------
# requirements.txt の生成
# ---------------------------------------------------------------------------

def _find_python_candidates() -> list:
    """
    pip freeze を実行できそうな Python 実行ファイルの候補を
    優先順で返す（重複・存在しないものは除く）。

    WSGI プロセスでは sys.executable が uwsgi ラッパー等を指して
    pip を実行できないことがあるため、実在するフルパスの Python を
    自前で探索する。
    """
    candidates = []

    # 1. 現在の sys.executable（正しいこともある）
    candidates.append(sys.executable)

    # 2. PythonAnywhere で安定して存在するフルパス
    for base in ('/usr/local/bin', '/usr/bin', '/bin'):
        for ver in ('python3.13', 'python3.12', 'python3.11',
                    'python3.10', 'python3.9', 'python3', 'python'):
            candidates.append(os.path.join(base, ver))

    # 3. PATH 上の名前（フルパス解決は subprocess に任せる）
    for ver in ('python3.13', 'python3.12', 'python3.11',
                'python3.10', 'python3'):
        candidates.append(ver)

    # 重複排除（順序維持）。フルパスのものは存在チェック。
    seen = set()
    result = []
    for c in candidates:
        if not c or c in seen:
            continue
        seen.add(c)
        if os.path.sep in c:          # フルパス指定なら存在確認
            if os.path.isfile(c):
                result.append(c)
        else:                          # PATH 上の名前はそのまま候補に
            result.append(c)
    return result


def generate_requirements_txt() -> str:
    """
    pip freeze を実行し、移行先で必要なライブラリ一覧を
    requirements.txt の内容として返す。

    手順:
      1. 複数の Python 実行ファイル候補それぞれで `-m pip freeze --user`
         を試し、最初に成功（returncode 0 かつ出力あり）したものを採用。
      2. --user で全て失敗したら、`-m pip freeze`（--user なし）で再試行。
      3. それでも取得できない場合は、試したコマンドと各 stderr を
         requirements.txt のコメントとして全て書き出す（原因究明用）。

    タイムアウトは1回あたり PIP_FREEZE_TIMEOUT 秒。候補を総当たりする
    ため、1回を短めにしないと全滅時の待ち時間が現実的でなくなる。
    """
    now_str = get_jst_now().strftime('%Y-%m-%d %H:%M JST')
    header = (
        '# FUJIN-P requirements\n'
        '# Generated by migrate_fujinp_scions (さいまる): %s\n'
        '# 移行先での復元: pip install --user -r requirements.txt\n'
        '#\n' % now_str
    )

    pythons = _find_python_candidates()
    attempts = []   # 診断用: [(コマンド文字列, returncode, stderr先頭), ...]
    deadline = time.monotonic() + PIP_FREEZE_DEADLINE

    # --user あり → --user なし の順で試す
    for pip_args in (['freeze', '--user'], ['freeze']):
        for py in pythons:
            if time.monotonic() + PIP_FREEZE_TIMEOUT > deadline:
                attempts.append(('(打ち切り)', 'TIMEOUT',
                                 '全体の締め切り %d 秒を超えたため、'
                                 '残りの候補は試していません'
                                 % PIP_FREEZE_DEADLINE))
                break
            cmd = [py, '-m', 'pip'] + pip_args
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=PIP_FREEZE_TIMEOUT
                )
            except Exception as e:
                attempts.append((' '.join(cmd), 'EXC', str(e)[:300]))
                continue

            body = (result.stdout or '').strip()
            if result.returncode == 0 and body:
                note = ('' if '--user' in pip_args else
                        '# 注: --user が使えなかったため --user なしの '
                        'pip freeze を使用しています。\n'
                        '#     システム標準パッケージが含まれることがあります。\n')
                used = '# 取得コマンド: %s\n' % ' '.join(cmd)
                return header + note + used + '#\n' + body + '\n'

            # 失敗を記録（stderr が無ければ stdout を診断に回す）
            err = (result.stderr or result.stdout or '').strip()
            attempts.append((' '.join(cmd), result.returncode,
                             err[:300] if err else '(出力なし)'))
        else:
            continue
        break       # 締め切り超過で内側を break したら外側も抜ける

    # すべて失敗 → 診断情報を書き出す
    logging.error("generate_requirements_txt: 全候補で失敗 attempts=%s",
                  attempts)
    diag = ['# [WARNING] pip freeze の取得にすべて失敗しました。',
            '# 移行先で必要なライブラリは手動で確認してください。',
            '#',
            '# --- 診断情報（試したコマンドと結果）---']
    for cmd_str, rc, err in attempts:
        diag.append('#   $ %s' % cmd_str)
        diag.append('#     exit=%s' % rc)
        for ln in str(err).split('\n'):
            diag.append('#     %s' % ln)
    return header + '\n'.join(diag) + '\n'


# ---------------------------------------------------------------------------
# レビュー対象ファイルの読み込み
# ---------------------------------------------------------------------------

def load_review_file(name: str) -> str:
    """
    レビュー対象ファイルの内容を返す。
      - config_template.py: config.py から動的生成した内容
    """
    if name == 'config_template.py':
        return generate_config_template()

    raise ValueError('レビュー対象外のファイルです: %r' % (name,))


# ---------------------------------------------------------------------------
# MySQL スキーマ
# ---------------------------------------------------------------------------

def _quote_ident(name) -> str:
    """
    MySQL の識別子をバッククォートで安全に囲む。

    識別子はプレースホルダー（%s）にできないため文字列連結が避けられない。
    バッククォートを2重化してエスケープしたうえで囲む。
    呼び出し側では、これに加えて必ずホワイトリスト照合も行うこと。
    """
    return '`' + str(name).replace('`', '``') + '`'


def get_owner_databases() -> list:
    """
    MySQL から owner に属するデータベース名の一覧を返す。
    PythonAnywhere では 'username$dbname' の形式。
    """
    owner = get_owner_name()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute("SHOW DATABASES")
        all_dbs = [row[0] for row in cursor.fetchall()]
        cursor.close()
        conn.close()
        prefix = owner + '$'
        return [db for db in all_dbs if db.startswith(prefix)]
    except Exception as e:
        logging.error("get_owner_databases error: %s", e)
        return []


def validate_databases(requested) -> list:
    """
    クライアントから渡された DB 名リストを検証して返す。

    セキュリティ:
      クライアント（admin であっても）の入力がそのまま
        - SQL 文（USE `...`）への連結
        - ZIP エントリ名（schema_<name>.sql）
      に流れるため、次の2段階で必ず検証する。
        1. 文字種チェック（英数字・アンダースコア・$・ハイフンのみ）
        2. サーバ側で列挙した owner 所有 DB との完全一致照合

    Raises:
        ValueError: 不正な DB 名が含まれていた場合
    """
    if not requested:
        return []
    if not isinstance(requested, (list, tuple)):
        raise ValueError('databases はリストで指定してください')

    allowed = set(get_owner_databases())
    validated = []
    for raw in requested:
        if not isinstance(raw, str):
            raise ValueError('データベース名が不正です')
        name = raw.strip()
        if not _DB_NAME_RE.match(name):
            raise ValueError('データベース名に使用できない文字が'
                             '含まれています: %r' % (name,))
        if name not in allowed:
            raise ValueError('このアカウントのデータベースでは'
                             'ありません: %r' % (name,))
        if name not in validated:
            validated.append(name)
    return validated


def _strip_foreign_keys(create_sql: str) -> str:
    """
    SHOW CREATE TABLE の出力から、外部キー制約の定義行を取り除く。

    FUJIN-P の実データは人の出入り等で参照整合性を厳密に保てないため、
    マイグレーション先のスキーマは外部キー制約を持たない緩やかな形に
    する。これにより、エクスポート元のDB名がREFERENCES句に残ることで
    起きるクロスDB参照エラーも回避できる。

    取り除くのは次の形の行:
        CONSTRAINT `name` FOREIGN KEY (...) REFERENCES `...`.`...` (...) ...
        FOREIGN KEY (...) REFERENCES ...        （CONSTRAINT 名なしの形）
    末尾カンマの整合（外部キー行が最後の要素だった場合、直前の行の
    末尾カンマを取り除く）も行う。
    """
    lines = create_sql.split('\n')
    kept = []
    for line in lines:
        stripped = line.strip()
        # 外部キー制約の行を判定（CONSTRAINT付き / なし の両方）
        is_fk = (
            (stripped.startswith('CONSTRAINT') and 'FOREIGN KEY' in stripped)
            or stripped.startswith('FOREIGN KEY')
        )
        if is_fk:
            continue
        kept.append(line)

    # 末尾カンマの整合:
    # CREATE TABLE 本体の最後の定義行は末尾にカンマを持たない。
    # 外部キー行を消した結果、その手前の行に余分なカンマが残ることが
    # あるので、「閉じ括弧 ) で始まる行」の直前の定義行の末尾カンマを除く。
    for i in range(len(kept)):
        s = kept[i].strip()
        if s.startswith(')'):
            # 直前の非空行を探して末尾カンマを除去
            j = i - 1
            while j >= 0 and not kept[j].strip():
                j -= 1
            if j >= 0:
                kept[j] = kept[j].rstrip()
                if kept[j].endswith(','):
                    kept[j] = kept[j][:-1]
            break
    return '\n'.join(kept)


def generate_schema_sql_for_db(full_dbname: str) -> str:
    """
    指定した1つのデータベースの CREATE TABLE 文をまとめた SQL を返す。

    マイグレーション先では、このファイルは対応する1つのデータベース
    （target$<bare>）に対してのみ実行する。ファイル冒頭に、その
    データベース用の USE 文を明記する。

    前提: full_dbname は validate_databases() を通った値であること。
    """
    if not _DB_NAME_RE.match(full_dbname):
        raise ValueError('不正なデータベース名です: %r' % (full_dbname,))

    owner = get_owner_name()
    bare_name = (full_dbname.split('$', 1)[1]
                 if '$' in full_dbname else full_dbname)
    now_str = get_jst_now().strftime('%Y-%m-%d %H:%M JST')

    lines = [
        '-- ============================================================',
        '-- FUJIN-P Migration Schema : %s' % bare_name,
        '-- Generated : %s' % now_str,
        '-- Source    : %s (%s / PythonAnywhere)' % (full_dbname, owner),
        '-- ============================================================',
        '--',
        '-- このファイルは「' + bare_name + '」データベース専用です。',
        '--',
        '-- 使い方（マイグレーション先 target）:',
        '--   1. MySQL コンソールを開く',
        '--   2. 下の USE 文の target を自分のアカウント名に書き換えて実行',
        '--   3. source でこのファイルを読み込む',
        '--',
        '-- 注: 外部キー制約（FOREIGN KEY）は除去してあります。',
        '--     テーブル・カラム・データ構造はそのままで、参照整合性の',
        '--     自動チェックのみ無効化した緩やかなスキーマです。',
        '--',
        '-- ============================================================',
        '',
        '-- ↓ target を自分のアカウント名に書き換えてください',
        'USE `target$%s`;' % bare_name,
        '',
        'SET FOREIGN_KEY_CHECKS = 0;',
        '',
    ]

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        try:
            cur = conn.cursor()
            cur.execute("USE " + _quote_ident(full_dbname))
            cur.execute("SHOW TABLES")
            tables = [row[0] for row in cur.fetchall()]
            cur.close()

            if not tables:
                lines.append('-- (このデータベースにはテーブルがありません)')
                lines.append('')
            else:
                for table in tables:
                    try:
                        cur2 = conn.cursor()
                        cur2.execute("SHOW CREATE TABLE "
                                     + _quote_ident(table))
                        row = cur2.fetchone()
                        cur2.close()
                        if row:
                            create_sql = row[1]
                            # 外部キー制約を除去（緩やかなスキーマにする）
                            create_sql = _strip_foreign_keys(create_sql)
                            if 'IF NOT EXISTS' not in create_sql:
                                create_sql = create_sql.replace(
                                    'CREATE TABLE',
                                    'CREATE TABLE IF NOT EXISTS', 1
                                )
                            create_sql = re.sub(
                                r'\s*AUTO_INCREMENT=\d+', '', create_sql
                            )
                            lines.append(create_sql + ';')
                            lines.append('')
                    except Exception as te:
                        logging.error("SHOW CREATE TABLE 失敗 %s.%s: %s",
                                      full_dbname, table, te)
                        lines.append('-- [ERROR] %s: 取得に失敗しました'
                                     '（詳細はサーバログを参照）' % table)
                        lines.append('')
        except Exception as de:
            logging.error("スキーマ取得失敗 %s: %s", full_dbname, de)
            lines.append('-- [ERROR] %s へのアクセスに失敗しました'
                         '（詳細はサーバログを参照）' % full_dbname)
            lines.append('')
        conn.close()
    except Exception as e:
        logging.error("MySQL 接続エラー: %s", e)
        lines.append('-- [FATAL] MySQL 接続エラー（詳細はサーバログを参照）')

    lines.append('')
    lines.append('SET FOREIGN_KEY_CHECKS = 1;')
    lines.append('')
    return '\n'.join(lines)


def schema_filename_for_db(full_dbname: str) -> str:
    """
    データベース名から、同梱するスキーマファイル名を返す。
    例: nishida$fujinp -> schema_fujinp.sql

    ZIP エントリ名になるため、想定外の文字が混じっていないことを
    ここでも確認する（パストラバーサル防止の多重化）。
    """
    if not _DB_NAME_RE.match(full_dbname):
        raise ValueError('不正なデータベース名です: %r' % (full_dbname,))
    bare_name = (full_dbname.split('$', 1)[1]
                 if '$' in full_dbname else full_dbname)
    safe = re.sub(r'[^A-Za-z0-9_-]', '_', bare_name)
    return 'schema_%s.sql' % safe


# ---------------------------------------------------------------------------
# ZIP 生成
# ---------------------------------------------------------------------------

def build_package_zip(databases: list = None,
                      config_template_text: str = None,
                      files: list = None):
    """
    エクスポートパッケージの ZIP を生成して BytesIO で返す。

    パッケージのトップに以下を同梱する:
      - ホワイトリスト対象ファイル（fujinp/ apps/ templates/ とトップレベル .py）
      - config_template.py : config.py を匿名化したもの
      - requirements.txt   : pip freeze の結果
      - schema_<bare>.sql  : 選択された各 DB のスキーマ（DB ごとに別ファイル）
                             例: schema_default.sql, schema_fujinp.sql

    Args:
        databases           : スキーマを同梱する DB 名のリスト
                              （validate_databases() を通った値であること）
        config_template_text: レビュー済みの config_template.py 内容
                              （None の場合は自動生成）
        files               : 同梱する [(絶対パス, ZIP内相対パス), ...]
                              （None なら collect_package_files() で収集）

    Returns:
        (BytesIO, file_count: int)
    """
    if files is None:
        files = collect_package_files()

    zip_buffer = io.BytesIO()
    file_count = 0

    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for full, rel in files:
            # さいまるが後で writestr する名前と衝突するものは入れない。
            # 入れてしまうと同名エントリが2つでき、展開結果が不定になる。
            if _is_generated_name(rel):
                logging.warning("生成物と同名のため同梱しません: %s", rel)
                continue
            try:
                zf.write(full, rel)
                file_count += 1
            except Exception as e:
                logging.error("ZIP 追加失敗 %s: %s", rel, e)

        # config_template.py を同梱（config.py の代替）
        if config_template_text is None:
            config_template_text = generate_config_template()
        zf.writestr('config_template.py', config_template_text)
        file_count += 1

        # requirements.txt を同梱（pip freeze の結果）
        zf.writestr('requirements.txt', generate_requirements_txt())
        file_count += 1

        # スキーマを DB ごとに別ファイルで同梱
        # （schema_default.sql, schema_fujinp.sql のように1DB1ファイル）
        if databases:
            for full_dbname in databases:
                fname = schema_filename_for_db(full_dbname)
                schema_sql = generate_schema_sql_for_db(full_dbname)
                zf.writestr(fname, schema_sql)
                file_count += 1

    zip_buffer.seek(0)
    return zip_buffer, file_count


# ---------------------------------------------------------------------------

# 名前にこれらが含まれる変数は「秘密情報の可能性が高い」とみなす。
# 該当する場合は、短い値・安全そうな値であっても伏せ字にし、
# 自動処理できない形（f 文字列・連結・関数呼び出し等）なら警告を付ける。
_SENSITIVE_NAME_RE = re.compile(
    r'(SECRET|PASSWORD|PASSWD|PWD|PW|TOKEN|API|KEY|CLIENT_ID|CLIENT_SECRET|'
    r'CREDENTIAL|PRIVATE|SALT|CERT|SIGNATURE|AUTH|SESSION|COOKIE|'
    r'MYSQL|DATABASE|DB_|DSN|HOST|PORT|USER|MAIL|SMTP|DOMAIN|URI|URL|'
    r'REDIRECT|WEBHOOK|ACCOUNT|LICENSE|ADMIN|EMAIL|ALLOWED|WHITELIST|'
    r'ALLOWLIST|OWNER|TENANT)',
    re.IGNORECASE
)

# 単純な文字列代入: NAME = 'value'  /  NAME: 'value'
_ASSIGN_STR_RE = re.compile(
    r"^(?P<indent>\s*)"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?P<op>\s*[:=]\s*)"
    r"(?P<quote>['\"])(?P<val>.*?)(?P=quote)"
    r"(?P<rest>\s*(#.*)?)$"
)

# 何らかの代入だが右辺が単純文字列でないもの
_ASSIGN_ANY_RE = re.compile(
    r"^(?P<indent>\s*)"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?P<op>\s*[:=]\s*)"
    r"(?P<value>\S.*)$"
)

# 辞書の中の「'キー': '値'」形式（DATABASES = {'default': {'PASSWORD': '...'}}
# のようなネストした設定を拾うために必要）
_DICT_ITEM_RE = re.compile(
    r"^(?P<indent>\s*)"
    r"(?P<kq>['\"])(?P<key>[^'\"]+)(?P=kq)"
    r"(?P<op>\s*:\s*)"
    r"(?P<vq>['\"])(?P<val>.*?)(?P=vq)"
    r"(?P<rest>\s*,?\s*(#.*)?)$"
)

# 三重引用符での代入開始
_TRIPLE_START_RE = re.compile(
    r"^(?P<indent>\s*)"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?P<op>\s*[:=]\s*)"
    r"(?P<quote>\"\"\"|''')"
)

# base64 / hex のような、見るからにトークンらしい文字列
_TOKEN_LIKE_RE = re.compile(r'^[A-Za-z0-9+/=_\-]{24,}$')

# 発行元がはっきりしている鍵・トークンの書式。長さが短くても拾う。
_KNOWN_SECRET_RE = re.compile(
    r'^('
    r'AKIA[0-9A-Z]{12,}'            # AWS アクセスキー ID
    r'|ASIA[0-9A-Z]{12,}'           # AWS 一時キー
    r'|(sk|pk|rk)_(live|test)_[A-Za-z0-9]{10,}'   # Stripe
    r'|xox[baprs]-[A-Za-z0-9-]{10,}'              # Slack
    r'|gh[pousr]_[A-Za-z0-9]{20,}'                # GitHub
    r'|github_pat_[A-Za-z0-9_]{20,}'
    r'|AIza[0-9A-Za-z_\-]{20,}'                   # Google API
    r'|SG\.[A-Za-z0-9_\-]{10,}'                   # SendGrid
    r'|eyJ[A-Za-z0-9_\-]{10,}'                    # JWT
    r'|-----BEGIN [A-Z ]*PRIVATE KEY-----'
    r')')

# 匿名化しない無害な値（真偽値・None・空、既知の設定切替キー等）
SAFE_VALUES = {'True', 'False', 'None', '',
               'development', 'production', 'default'}

WARN_TEXT = '⚠ 要確認: 自動匿名化できません。値を目視で確認してください'


# 行のどこにあっても拾いたい「キー: 値」「名前 = 値」の組。
#   {'password': 'x'} のような1行辞書、connect(password='x') のような
#   キーワード引数、obj.secret_key = 'x' のような属性代入を拾う。
_INLINE_KV_RE = re.compile(
    r"(?P<q1>['\"])(?P<key>[A-Za-z_][A-Za-z0-9_.\-]*)(?P=q1)"
    r"(?P<sep>\s*:\s*)"
    r"(?P<q2>['\"])(?P<val>(?:\\.|(?!(?P=q2)).)*)(?P=q2)"
)
_INLINE_KW_RE = re.compile(
    r"(?P<name>[A-Za-z_][A-Za-z0-9_.]*)"
    r"(?P<sep>\s*=\s*)"
    r"(?P<q>['\"])(?P<val>(?:\\.|(?!(?P=q)).)*)(?P=q)"
)


def _placeholder_for(name: str) -> str:
    return '<YOUR_' + re.sub(r'[^A-Za-z0-9_]', '_', name).upper() + '>'


def _mask_inline_sensitive(line: str):
    """
    1行の中に現れる「秘密情報らしいキー／名前に対応する文字列値」を
    すべて伏せ字化する。

    行頭の単純な代入だけを見る規則では拾えない次の形を潰すのが目的:
        SETTINGS = {'password': 'x'}      1行に収まった辞書
        conn = connect(password='x')      キーワード引数
        obj.secret_key = 'x'              属性への代入
        {'AWS_KEY': 'AKIA....'}           トークン然とした値

    Returns:
        (処理後の行, 伏せ字化したキー名のリスト)
    """
    touched = []

    def kv(m):
        key, val = m.group('key'), m.group('val')
        if _is_masked(val):
            return m.group(0)
        if _SENSITIVE_NAME_RE.search(key) or _TOKEN_LIKE_RE.match(val):
            if val in SAFE_VALUES:
                return m.group(0)
            touched.append(key)
            return (m.group('q1') + key + m.group('q1') + m.group('sep')
                    + m.group('q2') + _placeholder_for(key) + m.group('q2'))
        return m.group(0)

    def kw(m):
        name, val = m.group('name'), m.group('val')
        if _is_masked(val):
            return m.group(0)
        if _SENSITIVE_NAME_RE.search(name) or _TOKEN_LIKE_RE.match(val):
            touched.append(name)
            return (name + m.group('sep') + m.group('q')
                    + _placeholder_for(name) + m.group('q'))
        return m.group(0)

    line = _INLINE_KV_RE.sub(kv, line)
    line = _INLINE_KW_RE.sub(kw, line)
    return line, touched


def _bracket_delta(line: str) -> int:
    """行に含まれる開き括弧と閉じ括弧の差（文字列中の括弧は数えない）"""
    depth = 0
    quote = None
    escaped = False
    for ch in line:
        if quote:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in ('\'', '"'):
            quote = ch
        elif ch == '#':
            break
        elif ch in '([{':
            depth += 1
        elif ch in ')]}':
            depth -= 1
    return depth


_BLOCK_VALUE_RE = re.compile(
    r"(?P<q>['\"])(?P<val>(?:\\.|(?!(?P=q)).)*)(?P=q)(?P<after>\s*:?)"
)


def _mask_block_values(line: str, placeholder: str) -> str:
    """
    「秘密らしい名前」の複数行定義の内側にある文字列値を伏せ字化する。

    辞書のキー（直後に ':' が続くもの）は構造を保つために残し、
    値だけを置き換える。
    """
    def rep(m):
        if m.group('after').strip().endswith(':'):
            return m.group(0)          # 辞書のキーは残す
        val = m.group('val')
        if _is_masked(val) or val in SAFE_VALUES or len(val) < 3:
            return m.group(0)
        return m.group('q') + placeholder + m.group('q') + m.group('after')
    return _BLOCK_VALUE_RE.sub(rep, line)


def _warn_line(indent: str, name: str) -> str:
    """
    警告コメントを「その行の上」に置くための1行を返す。

    行末に追記すると、バックスラッシュによる行継続
    （SECRET_KEY = 'a' \\ + 'b'）のときに継続が壊れて SyntaxError に
    なるため、必ず独立した行として出す。
    """
    return '%s# %s (%s)' % (indent, WARN_TEXT, name)


def _anonymize_config_source(source: str):
    """
    config.py のソースを受け取り、秘密情報になりうる値を
    プレースホルダー <YOUR_属性名> に置換して返す。

    Returns:
        (匿名化後のソース, 警告メッセージのリスト)

    方針:
      - 「属性 = '値'」「属性: '値'」の右辺の文字列値を置換する。
      - 属性名が秘密情報らしい（_SENSITIVE_NAME_RE に一致する）場合は、
        短い値・SAFE_VALUES に含まれる値であっても置換する。
        値の長さから中身を推測されるのを防ぐため、置換後は必ず
        <YOUR_属性名> の形にする。
      - 右辺が単純な文字列でない場合（f 文字列・文字列連結・関数呼び出し・
        リスト・辞書・三重引用符）は自動置換できない。
        属性名が秘密情報らしいときは行末に警告マーカーを付け、
        ファイル冒頭にも警告一覧を出す。レビュー時の見落としを減らす。
      - 左辺の属性名・辞書のキー・import 文などは対象外。

    例:
      SECRET_KEY = 'abc123'          -> SECRET_KEY = '<YOUR_SECRET_KEY>'
      DEBUG = True                   -> 変更しない
      URI = f"https://{D}/callback"  -> 変更せず ⚠ マーカーを付与
    """
    warnings = []
    out_lines = []

    # 「秘密らしい名前 = { …複数行… }」の途中にいるかどうか。
    # 中に入っている間は、キー名が無害でも値を伏せ字化する。
    block_name = None
    block_depth = 0

    lines = source.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i]

        # --- 0a. 秘密ブロックの内側 ---------------------------------------
        if block_name is not None:
            masked = _mask_block_values(line, _placeholder_for(block_name))
            out_lines.append(masked)
            block_depth += _bracket_delta(line)
            if block_depth <= 0:
                block_name = None
                block_depth = 0
            i += 1
            continue

        # --- 0. 行内の「秘密らしいキー／名前 = 値」を先に潰す --------------
        # 行頭の単純な代入しか見ない規則では、1行辞書・キーワード引数・
        # 属性代入がすり抜けるため、まずここで拾う。
        if not _TRIPLE_START_RE.match(line):
            line, touched = _mask_inline_sensitive(line)
            for key in touched:
                warnings.append('%s の値を伏せ字化しました' % key)

        # --- 1. 三重引用符での代入（複数行文字列） -------------------------
        m3 = _TRIPLE_START_RE.match(line)
        if m3:
            name = m3.group('name')
            quote = m3.group('quote')
            after = line[m3.end():]
            closed = quote in after     # 1行で閉じているか
            block = [line]
            j = i
            while not closed and j + 1 < len(lines):
                j += 1
                block.append(lines[j])
                if quote in lines[j]:
                    closed = True
            if _SENSITIVE_NAME_RE.search(name):
                # 秘密情報らしい複数行文字列はブロックごと置換する
                out_lines.append(
                    '%s%s%s%s<YOUR_%s>%s' % (
                        m3.group('indent'), name, m3.group('op'),
                        quote, name.upper(), quote)
                )
                warnings.append(
                    '%s: 複数行文字列だったため、内容をまるごと '
                    '<YOUR_%s> に置換しました' % (name, name.upper())
                )
            else:
                out_lines.extend(block)
            i = j + 1
            continue

        # --- 1.5 辞書の 'キー': '値' ---------------------------------------
        # DATABASES = {'default': {'PASSWORD': 'xxx'}} のように、
        # 左辺が識別子でない形の秘密情報を拾う。
        md = _DICT_ITEM_RE.match(line)
        if md:
            key = md.group('key')
            val = md.group('val')
            sensitive = bool(_SENSITIVE_NAME_RE.search(key))
            token_like = bool(_TOKEN_LIKE_RE.match(val))
            if (sensitive or token_like) and val not in SAFE_VALUES:
                ph = '<YOUR_' + re.sub(r'[^A-Za-z0-9_]', '_',
                                       key).upper() + '>'
                out_lines.append(
                    md.group('indent') + md.group('kq') + key + md.group('kq')
                    + md.group('op') + md.group('vq') + ph + md.group('vq')
                    + md.group('rest')
                )
                warnings.append(
                    "辞書のキー '%s' の値を伏せ字化しました" % key)
            else:
                out_lines.append(line)
            i += 1
            continue

        # --- 2. 単純な文字列代入 -------------------------------------------
        m = _ASSIGN_STR_RE.match(line)
        if m:
            name = m.group('name')
            val = m.group('val')
            sensitive = bool(_SENSITIVE_NAME_RE.search(name))
            # 秘密情報らしい名前は、短くても SAFE_VALUES でも必ず伏せる
            if not sensitive and (val in SAFE_VALUES or len(val) < 3):
                out_lines.append(line)
            else:
                placeholder = '<YOUR_' + name.upper() + '>'
                out_lines.append(
                    m.group('indent') + name + m.group('op')
                    + m.group('quote') + placeholder + m.group('quote')
                    + m.group('rest')
                )
            i += 1
            continue

        # --- 3. その他の代入（自動置換できない形） -------------------------
        ma = _ASSIGN_ANY_RE.match(line)
        if ma:
            name = ma.group('name')
            value = ma.group('value')
            bare = value.split('#', 1)[0].strip()
            # 真偽値・数値・None は無害なので触らない
            harmless = (
                bare in ('True', 'False', 'None')
                or re.fullmatch(r'-?\d+(\.\d+)?', bare) is not None
            )
            has_literal = ("'" in bare) or ('"' in bare)
            if _SENSITIVE_NAME_RE.search(name) and not harmless:
                # 警告は必ず「上の行」に置く。行末に足すとバックスラッシュ
                # による行継続が壊れて SyntaxError になる。
                out_lines.append(_warn_line(ma.group('indent'), name))
                # 複数行の { } / [ ] が開いたままなら、閉じるまでの値を
                # まとめて伏せ字化する（キー名が無害でも中身は秘密のため）
                delta = _bracket_delta(line)
                if delta > 0:
                    out_lines.append(line)
                    block_name = name
                    block_depth = delta
                    warnings.append(
                        '%s: 複数行の定義のため、閉じ括弧までの文字列値を'
                        'まとめて伏せ字化しました' % name)
                    i += 1
                    continue
                continues = bare.endswith('\\')
                if has_literal and not continues:
                    # リスト・辞書・連結の中の文字列リテラルを個別に伏せる
                    placeholder = '<YOUR_' + name.upper() + '>'
                    replaced = re.sub(
                        r"(['\"])(?:(?!\1).){3,}?\1",
                        lambda mm: mm.group(1) + placeholder + mm.group(1),
                        bare
                    )
                    out_lines.append(
                        ma.group('indent') + name + ma.group('op') + replaced
                    )
                    warnings.append(
                        '%s: 単純な代入ではないため機械的に伏せ字化しました。'
                        '構文が壊れていないか確認してください' % name
                    )
                else:
                    out_lines.append(line)
                    warnings.append(
                        '%s: 値が f 文字列・変数・関数呼び出し・行継続の'
                        'ため自動匿名化できません。手で確認してください'
                        % name
                    )
                i += 1
                continue

        out_lines.append(line)
        i += 1

    return '\n'.join(out_lines), warnings


def generate_config_template() -> str:
    """
    ~/config.py を読み込み、秘密情報になりうる値を
    プレースホルダー <YOUR_...> に置換した config_template.py の内容を返す。
    config.py が存在しない場合は汎用テンプレートを返す。
    """
    config_path = os.path.join(get_home_dir(), SECRET_PY)

    header = [
        '"""',
        'FUJIN-P Configuration Template',
        '',
        'このファイルをコピーして config.py に名前を変え、',
        '各プレースホルダー <YOUR_...> を実際の値に書き換えてください。',
        '',
        'Generated by migrate_fujinp_scions (さいまる): %s'
        % get_jst_now().strftime('%Y-%m-%d %H:%M JST'),
        '"""',
        '',
    ]

    if os.path.exists(config_path) and not os.path.islink(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            source = f.read()

        processed, warnings = _anonymize_config_source(source)

        notice = [
            '# !! このファイルは config.py から自動生成されたテンプレートです !!',
            '# !! <YOUR_...> を実際の値に置き換えて config.py として保存 !!',
            '',
        ]
        if warnings:
            notice += [
                '# ---------------------------------------------------------',
                '# ⚠ 自動匿名化できなかった／機械的に処理した箇所があります。',
                '#   下記を必ず目視で確認してください。',
                '# ---------------------------------------------------------',
            ]
            notice += ['#   - ' + w for w in warnings]
            notice.append('')

        lines = notice + processed.split('\n')
    else:
        owner = get_owner_name()
        lines = [
            'class Config:',
            '    # Flask',
            '    SECRET_KEY = "<YOUR_SECRET_KEY>"',
            '',
            '    # MySQL (PythonAnywhere)',
            '    MYSQL_HOST = "%s.mysql.pythonanywhere-services.com"' % owner,
            '    MYSQL_USER = "<YOUR_PYTHONANYWHERE_USERNAME>"',
            '    MYSQL_PASSWORD = "<YOUR_MYSQL_PASSWORD>"',
            '    MYSQL_DB = "<YOUR_USERNAME>$fujinp"',
            '',
            '    # Google OAuth',
            '    GOOGLE_CLIENT_ID = "<YOUR_GOOGLE_CLIENT_ID>"',
            '    GOOGLE_CLIENT_SECRET = "<YOUR_GOOGLE_CLIENT_SECRET>"',
            '    REDIRECT_URI = "https://<YOUR_DOMAIN>/auth/callback"',
            '',
            '    # App',
            '    ALLOWED_DOMAIN = "<YOUR_ALLOWED_EMAIL_DOMAIN>"',
            '    SITE_URL = "https://<YOUR_PYTHONANYWHERE_USERNAME>.pythonanywhere.com"',
        ]

    return '\n'.join(header + lines)


def _is_masked(value: str) -> bool:
    """すでにプレースホルダー化されている値か"""
    v = value.strip()
    return v.startswith('<') and v.endswith('>')


# モジュールパス・URL・ファイルパスらしい文字列（秘密ではないことが多い）
_PATHY_RE = re.compile(
    r'^(?:[a-z0-9_]+(?:\.[a-z0-9_]+)+'      # django.db.backends.mysql
    r'|/[\w./\-]*'                          # /var/www/...
    r'|[a-z]+://[\w./\-]*'                   # https://example.com/...
    r'|%\([a-z_]+\)s'                        # %(name)s
    r')$'
)


def _looks_like_path(value: str) -> bool:
    """モジュールパス・URL・ファイルパスのような、秘密でない可能性が
    高い値か（辞書の中の誤検知を減らすための判定）"""
    return bool(_PATHY_RE.match(value.strip()))


def _iter_str_constants(node):
    """式ノード配下の文字列リテラルを (値, 行番号) で列挙する"""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            yield sub.value, getattr(sub, 'lineno', 0)


def _iter_value_constants(node):
    """
    式ノード配下の文字列リテラルを (値, 行番号) で列挙する。
    ただし辞書の「キー」は列挙しない。

    DATABASES = {'default': {'ENGINE': '...'}} のようなとき、
    キー名（'default' / 'ENGINE'）まで秘密候補として拾うと
    誤検知だらけになるため。
    """
    if isinstance(node, ast.Dict):
        for v in node.values:
            for item in _iter_value_constants(v):
                yield item
    elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        for e in node.elts:
            for item in _iter_value_constants(e):
                yield item
    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value, getattr(node, 'lineno', 0)
    else:
        for item in _iter_str_constants(node):
            yield item


def _target_names(node):
    """代入先ノードから名前らしきものを取り出す"""
    names = []
    for t in (node if isinstance(node, (list, tuple)) else [node]):
        if isinstance(t, ast.Name):
            names.append(t.id)
        elif isinstance(t, ast.Attribute):
            names.append(t.attr)
        elif isinstance(t, (ast.Tuple, ast.List)):
            names.extend(_target_names(list(t.elts)))
    return names


def scan_source_for_secrets(source: str) -> list:
    """
    Python ソースを AST で走査し、秘密情報らしき文字列リテラルを
    [(行番号, 手がかりの名前, 値の断片), ...] で返す。

    行ベースの正規表現では拾えない次の形を検出するのが目的:
        DATABASES = {'default': {'PASSWORD': 'hunter2'}}
        conn = connect(password='hunter2')
        obj.secret_key = 'hunter2'
    さらに、名前に手がかりが無くても base64/hex 風の長い文字列は
    トークンとみなして拾う。

    構文エラーで parse できない場合は行ベースの検査にフォールバックする。
    """
    hits = []
    seen = set()

    def add(lineno, name, value):
        if not isinstance(value, str):
            return
        if _is_masked(value) or value in SAFE_VALUES or len(value) < 3:
            return
        key = (lineno, name)
        if key in seen:
            return
        seen.add(key)
        # 値そのものは返さない（画面・スクリーンショット・ログに
        # 秘密の断片が残らないようにする）。長さだけを手がかりにする。
        hits.append((lineno, name, '%d文字の文字列' % len(value)))

    try:
        tree = ast.parse(source)
    except SyntaxError:
        # parse できないときは従来どおり行単位で最低限の検査をする
        for no, line in enumerate(source.split('\n'), 1):
            m = _ASSIGN_STR_RE.match(line)
            if m and _SENSITIVE_NAME_RE.search(m.group('name')):
                add(no, m.group('name'), m.group('val'))
        return hits

    for node in ast.walk(tree):
        # 1. 代入（NAME = ... / obj.attr = ... / NAME: t = ...）
        value = None
        names = []
        if isinstance(node, ast.Assign):
            names = _target_names(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            names = _target_names(node.target)
            value = node.value
        if value is not None:
            for nm in names:
                if not _SENSITIVE_NAME_RE.search(nm):
                    continue
                if isinstance(value, ast.Dict):
                    # API_KEYS = {'sendgrid': 'SG.xxx'} のように
                    # 「名前は秘密だがキーは無害」という形を拾う。
                    # ただし辞書のキー自体と、モジュールパス・URL・
                    # ファイルパス風の値（'django.db.backends.mysql' など）
                    # は除く。そうしないと DATABASES 定義のたびに警告が出て、
                    # 「とりあえず OK」を押す癖がついてしまう。
                    for s, ln in _iter_value_constants(value):
                        if len(s) >= 6 and not _looks_like_path(s):
                            add(ln, nm, s)
                else:
                    for s, ln in _iter_str_constants(value):
                        add(ln, nm, s)

        # 2. 辞書の 'キー': '値'（ネストも ast.walk で全部見える）
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant)
                        and isinstance(k.value, str)
                        and _SENSITIVE_NAME_RE.search(k.value)):
                    for s, ln in _iter_str_constants(v):
                        add(ln, k.value, s)

        # 3. キーワード引数（connect(password='...') など）
        if isinstance(node, ast.keyword) and node.arg:
            if _SENSITIVE_NAME_RE.search(node.arg):
                for s, ln in _iter_str_constants(node.value):
                    add(ln, node.arg, s)

        # 4. 名前に手がかりが無くてもトークン然とした文字列は拾う
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _TOKEN_LIKE_RE.match(node.value):
                add(getattr(node, 'lineno', 0), '(トークン風の文字列)',
                    node.value)

    hits.sort(key=lambda h: h[0])
    return hits


def find_unmasked_secrets(text: str):
    """
    確定前の config_template.py に、伏せ字化されていない秘密情報らしき
    値が残っていないか調べ、[(行番号, 手がかりの名前), ...] を返す。

    登録直前の最終チェックに使う。admin がレビュー中に警告ごと値を
    戻してしまった場合の取りこぼしを拾う。
    """
    return [(ln, name) for ln, name, _v in scan_source_for_secrets(text)]


# JavaScript でよくある「キー: '値'」（キーが引用符で囲まれていない形）
# JS の代入（バッククォートも引用符として扱う）
_INLINE_JSASSIGN_RE = re.compile(
    r"(?P<name>[A-Za-z_$][A-Za-z0-9_$.]*)"
    r"\s*=\s*"
    r"(?P<q>['\"`])(?P<val>(?:\\.|(?!(?P=q)).)*)(?P=q)"
)

# 「名前 =」「名前 :」までだけを見る（右辺が式でも名前を取れるように）
_JS_ASSIGN_NAME_RE = re.compile(
    r"(?P<name>[A-Za-z_$][A-Za-z0-9_$.]*)\s*[=:]\s*"
)

# 行内の任意の文字列リテラル
_ANY_STRING_RE = re.compile(
    r"(?P<q>['\"`])(?P<val>(?:\\.|(?!(?P=q)).)*)(?P=q)"
)

_INLINE_JSKEY_RE = re.compile(
    r"(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)"
    r"\s*:\s*"
    r"(?P<q>['\"`])(?P<val>(?:\\.|(?!(?P=q)).)*)(?P=q)"
)


# 識別子をトークンに分割して照合するための語彙。
# JavaScript は camelCase なので、_SENSITIVE_NAME_RE の「部分一致」だと
# viewport / transport / author / keyboard / hostname のような
# 普通の識別子まで拾ってしまう。警告が狼少年になるのを避けるため、
# .js / .css ではトークン単位の完全一致で判定する。
_SENSITIVE_WORDS = {
    'SECRET', 'SECRETS', 'PASSWORD', 'PASSWD', 'PWD', 'PW', 'PASS',
    'TOKEN', 'APIKEY', 'KEY', 'CLIENTID', 'CLIENTSECRET',
    'CREDENTIAL', 'CREDENTIALS', 'PRIVATE', 'SALT', 'CERT', 'SIGNATURE',
    'AUTH', 'AUTHORIZATION', 'SESSION', 'COOKIE', 'BEARER',
    'DSN', 'ACCESSKEY', 'SECRETKEY', 'REFRESH', 'WEBHOOK', 'LICENSE',
}

_TOKEN_SPLIT_RE = re.compile(r'[^A-Za-z0-9]+')
_CAMEL_SPLIT_RE = re.compile(r'(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])')


def _split_identifier(name: str) -> list:
    """識別子を単語に分割する（apiKey -> ['API','KEY']、API_KEY も同じ）"""
    words = []
    for part in _TOKEN_SPLIT_RE.split(name):
        if not part:
            continue
        for w in _CAMEL_SPLIT_RE.split(part):
            if w:
                words.append(w.upper())
    return words


def _is_sensitive_identifier(name: str) -> bool:
    """
    識別子が秘密情報らしいか（トークン単位の完全一致）。

    語尾の複数形 S は落として照合する（KEYS -> KEY）。
    'viewport' や 'author' のような、たまたま部分文字列が一致するだけの
    識別子は拾わない。
    """
    for w in _split_identifier(name):
        if w in _SENSITIVE_WORDS:
            return True
        if w.endswith('S') and w[:-1] in _SENSITIVE_WORDS:
            return True
    return False


def _token_like_parts(value: str) -> bool:
    """
    値の中に、トークン然とした断片が含まれるか。
    'Bearer eyJhbGciOi...' のように接頭辞つきで書かれる形も拾う。
    """
    for part in value.split():
        if _TOKEN_LIKE_RE.match(part) or _KNOWN_SECRET_RE.match(part):
            return True
    return False


def scan_text_for_secrets(text: str) -> list:
    """
    Python 以外のソース（.js / .css / .html）を行単位で走査し、
    秘密情報らしき文字列を [(行番号, 手がかりの名前, 説明), ...] で返す。

    JavaScript は AST で解析しないため、Python 用の
    scan_source_for_secrets ほど正確ではない。次の形を拾う。

        const API_KEY = 'xxx';                  秘密らしい名前への代入
        const API_KEY = `xxx`;                  テンプレートリテラルも
        const T = process.env.X || 'xxx';       環境変数のフォールバック
        apiKey: 'xxx',                          オブジェクトのキー
        {'password': 'xxx'}                     引用符つきキー
        fetch(h, 'Bearer eyJhbGci...')          トークン然とした文字列
                                                （名前に手がかりが無くても）

    誤検知を減らすための制約:
      - 名前の判定はトークン単位の完全一致（_is_sensitive_identifier）。
        viewport / author / keyboard などは拾わない。
      - 値は TEXT_SCAN_MIN_VALUE_LEN 文字以上。keyCode: 'Enter' のような
        短い設定値は対象外。
      - モジュールパス・URL・ファイルパス風の値は対象外。
      - 行コメント（// や * で始まる行）は対象外。
    """
    hits = []
    seen = set()

    def add(lineno, name, value):
        if len(value) < TEXT_SCAN_MIN_VALUE_LEN:
            return
        if _is_masked(value) or value in SAFE_VALUES:
            return
        if _looks_like_path(value):
            return
        key = (lineno, name)
        if key in seen:
            return
        seen.add(key)
        hits.append((lineno, name, '%d文字の文字列' % len(value)))

    for no, line in enumerate(text.split('\n'), 1):
        stripped = line.strip()
        if stripped.startswith('//') or stripped.startswith('*'):
            continue

        # 1. キー／名前が秘密らしいもの
        for rx, grp in ((_INLINE_KV_RE, 'key'),
                        (_INLINE_JSKEY_RE, 'name'),
                        (_INLINE_JSASSIGN_RE, 'name')):
            for m in rx.finditer(line):
                if _is_sensitive_identifier(m.group(grp)):
                    add(no, m.group(grp), m.group('val'))

        # 2. 秘密らしい名前への代入で、右辺が単純な文字列でない形。
        #    const T = process.env.X || 'sk_live_...' のような、
        #    実際によくあるフォールバック記法を拾う。
        for ma in _JS_ASSIGN_NAME_RE.finditer(line):
            if not _is_sensitive_identifier(ma.group('name')):
                continue
            for lm in _ANY_STRING_RE.finditer(line[ma.end():]):
                add(no, ma.group('name'), lm.group('val'))

        # 3. 名前に手がかりが無くても、トークン然とした文字列は拾う
        for lm in _ANY_STRING_RE.finditer(line):
            val = lm.group('val')
            if _token_like_parts(val):
                add(no, '(トークン風の文字列)', val)

    return hits


def scan_package_files_for_secrets(files) -> list:
    """
    パッケージに同梱する .py ファイルの中身を走査し、
    ハードコードされた秘密情報らしき箇所を返す。

    config.py を除外しても、db.py や各アプリの .py に接続情報や
    トークンが直書きされていれば ZIP に入ってしまう。パッケージは
    「別の運用者に渡す」ものなので、ここが最大の残存漏洩面になる。

    Args:
        files: [(絶対パス, ZIP内相対パス), ...]

    Returns:
        [{'file': ZIP内パス, 'line': 行番号, 'name': 手がかり,
          'preview': 値の断片}, ...]（最大 SECRET_SCAN_MAX_HITS 件）
    """
    results = []
    truncated = False
    for full, rel in files:
        low = rel.lower()
        is_py = low.endswith(PYTHON_EXTENSIONS)
        is_text = low.endswith(TEXT_SCAN_EXTENSIONS)
        if not is_py and not is_text:
            continue

        # 圧縮済みの成果物は走査しない。
        # 1行が数万文字あるため誤検知が大量に出て、上限を食い尽くし、
        # 他のファイルの本物の検出が押し出されてしまう。
        # （成果物であること自体は minified_files で別途知らせる）
        if not is_py and is_minified_file(full, rel):
            logging.info("秘密情報スキャン: ビルド成果物のためスキップ %s",
                         rel)
            results.append({'file': rel.replace(os.sep, '/'), 'line': 0,
                            'name': '(未スキャン)',
                            'preview': '圧縮済みのため走査していません'})
            continue

        try:
            if os.path.getsize(full) > SECRET_SCAN_MAX_BYTES:
                logging.info("秘密情報スキャン: サイズ超過のためスキップ %s",
                             rel)
                results.append({'file': rel.replace(os.sep, '/'), 'line': 0,
                                'name': '(未スキャン)',
                                'preview': 'サイズ超過のため走査していません'})
                continue
            with open(full, 'r', encoding='utf-8', errors='replace') as f:
                source = f.read()
        except Exception as e:
            logging.info("秘密情報スキャンでファイルを読めません %s: %s",
                         rel, e)
            continue

        # .py は AST で、それ以外（.js / .css / .html）は行単位で走査する
        found = (scan_source_for_secrets(source) if is_py
                 else scan_text_for_secrets(source))

        # 1ファイルが上限を独占しないように、ファイル単位でも絞る
        shown = found[:SECRET_SCAN_MAX_HITS_PER_FILE]
        for lineno, name, preview in shown:
            results.append({'file': rel.replace(os.sep, '/'),
                            'line': lineno, 'name': name,
                            'preview': preview})
        if len(found) > len(shown):
            results.append({
                'file': rel.replace(os.sep, '/'), 'line': 0,
                'name': '(他 %d 件)' % (len(found) - len(shown)),
                'preview': 'このファイル内の残りは省略しました'})

        if len(results) >= SECRET_SCAN_MAX_HITS:
            truncated = True

    if truncated:
        logging.warning("秘密情報スキャン: 検出が %d 件を超えました",
                        SECRET_SCAN_MAX_HITS)
        results = results[:SECRET_SCAN_MAX_HITS]
        results.append({'file': '(以降省略)', 'line': 0,
                        'name': '(打ち切り)',
                        'preview': '検出が多いため表示を打ち切りました。'
                                   'サーバログを確認してください'})
    return results


def _connect_default():
    """default DB への接続を返す"""
    return mysql.connector.connect(**DatabaseConfig.default())


def insert_package_record(filename, description, source_owner,
                          file_count, included_dbs, size_bytes, created_by):
    """パッケージのメタ情報を MySQL に登録する"""
    conn = _connect_default()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO {table}
                (filename, description, source_owner, file_count,
                 included_dbs, size_bytes, created_by, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""".format(
                table=PACKAGE_TABLE),
            (filename, description, source_owner, file_count,
             ','.join(included_dbs) if included_dbs else '',
             size_bytes, created_by, get_jst_now())
        )
        conn.commit()
        new_id = cur.lastrowid
        cur.close()
        return new_id
    finally:
        conn.close()


def list_package_records() -> list:
    """登録済みパッケージのメタ情報を新しい順で返す"""
    conn = _connect_default()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """SELECT id, filename, description, source_owner, file_count,
                      included_dbs, size_bytes, created_by, created_at
               FROM {table}
               ORDER BY created_at DESC, id DESC""".format(table=PACKAGE_TABLE)
        )
        rows = cur.fetchall()
        cur.close()
        # datetime を文字列化
        for r in rows:
            if isinstance(r.get('created_at'), datetime.datetime):
                r['created_at'] = r['created_at'].strftime('%Y-%m-%d %H:%M')
        return rows
    finally:
        conn.close()


def get_package_record(pkg_id: int):
    """ID 指定で 1 件のパッケージメタ情報を返す（無ければ None）"""
    conn = _connect_default()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT * FROM {table} WHERE id = %s".format(table=PACKAGE_TABLE),
            (pkg_id,)
        )
        row = cur.fetchone()
        cur.close()
        return row
    finally:
        conn.close()


def delete_package_record(pkg_id: int) -> str:
    """
    パッケージのメタ情報と実 ZIP ファイルを削除する。
    削除したファイル名を返す（レコードが無ければ None）。

    実ファイルは新旧どちらの保存先にあっても削除する。
    ファイル名は必ず basename 検証を通してから I/O する。
    """
    rec = get_package_record(pkg_id)
    if not rec:
        return None

    # 実ファイルを削除（新保存先・旧保存先の両方を見る）
    try:
        name = _safe_zip_name(rec['filename'])
        for directory in (ensure_package_dir(), LEGACY_PACKAGE_DIR):
            fpath = os.path.join(directory, name)
            if os.path.isfile(fpath):
                try:
                    os.remove(fpath)
                except Exception as e:
                    logging.error("ZIP ファイル削除失敗 %s: %s", fpath, e)
    except ValueError as e:
        # 不正なファイル名がテーブルに入っていた場合はファイル削除を諦め、
        # メタ情報だけ消す（参照できない幽霊レコードを残さない）
        logging.error("削除対象のファイル名が不正: %s", e)

    # レコードを削除
    conn = _connect_default()
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM {table} WHERE id = %s".format(table=PACKAGE_TABLE),
            (pkg_id,)
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()

    return rec['filename']


# ---------------------------------------------------------------------------
# 共通ルート
# ---------------------------------------------------------------------------

def _is_admin() -> bool:
    """
    現在のログインユーザーが admin かどうかを session から判定する。
    画面側のボタン表示制御に使う簡易判定。
    判定基準は decorators.admin_required と同一（user_category == 'admin'）。
    （API レベルの保護は @admin_required デコレータが担う）
    """
    return session.get('user_category') == 'admin'


def _can_download() -> bool:
    """現在のユーザーがパッケージを一覧・取得できるか（画面表示制御用）"""
    return ALLOW_GUEST_DOWNLOAD or _is_admin()


@migrate_fujinp_scions_bp.route('/')
@login_required
def index():
    """ハブ画面（エクスポーター / インポーターへの入口）"""
    return render_template('migrate_fujinp_scions/index.html',
                           is_admin=_is_admin(),
                           can_download=_can_download(),
                           allow_guest_download=ALLOW_GUEST_DOWNLOAD)


# ===========================================================================
# エクスポーター
# ===========================================================================

@migrate_fujinp_scions_bp.route('/exporter')
@admin_required
def exporter():
    """エクスポーター画面（admin 専用）"""
    return render_template('migrate_fujinp_scions/exporter.html',
                           allow_guest_download=ALLOW_GUEST_DOWNLOAD)


@migrate_fujinp_scions_bp.route('/api/export/info')
@admin_required
def api_export_info():
    """
    エクスポート環境情報の取得（admin 専用）。
    ホワイトリストディレクトリの採用状況、トップレベル .py、
    保有 DB 一覧などを返す。
    """
    try:
        home = get_home_dir()
        owner = get_owner_name()

        # ホワイトリストディレクトリの状態
        # ※ 走査は1回だけ行い、その結果をトップレベル名で集計する
        #   （以前はディレクトリごとに全走査していたため3倍の時間がかかっていた）
        whitelist_files = collect_whitelist_dirs()
        counts = {}
        for _abs, rel in whitelist_files:
            top = rel.replace(os.sep, '/').split('/')[0]
            counts[top] = counts.get(top, 0) + 1

        # 拡張子ごとの内訳（.js / .css を含めた影響を見えるようにする）
        ext_counts = {}
        for _abs, rel in whitelist_files:
            ext = os.path.splitext(rel)[1].lower() or '(なし)'
            ext_counts[ext] = ext_counts.get(ext, 0) + 1

        dir_info = []
        for top in WHITELIST_DIRS:
            base = os.path.join(home, top)
            if os.path.isdir(base):
                dir_info.append({'name': top, 'exists': True,
                                 'files': counts.get(top, 0)})
            else:
                dir_info.append({'name': top, 'exists': False, 'files': 0})

        # トップレベル .py（秘密ファイル除く）＋ライセンス等
        toplevel_pairs = collect_toplevel_py()
        toplevel = [rel for _abs, rel in toplevel_pairs]
        for _abs, rel in toplevel_pairs:
            ext = os.path.splitext(rel)[1].lower() or '(なし)'
            ext_counts[ext] = ext_counts.get(ext, 0) + 1

        # 圧縮・結合された成果物らしきもの（AGPL 的に注意が要る）
        try:
            minified = collect_minified_files(whitelist_files + toplevel_pairs)
        except Exception as e:
            logging.error("ビルド成果物の判定に失敗: %s", e)
            minified = []

        # 丸ごと落としたディレクトリ（利用者データ置き場・生成物など）
        try:
            skipped_dirs = collect_skipped_dirs()
        except Exception as e:
            logging.error("スキップ済みディレクトリの取得に失敗: %s", e)
            skipped_dirs = []

        # 「秘密ファイル名」に該当して除外されたもの（黙って落とさない）
        try:
            excluded = collect_excluded_secret_files()
        except Exception as e:
            logging.error("除外ファイル一覧の取得に失敗: %s", e)
            excluded = []

        # config.py の有無
        config_exists = os.path.exists(os.path.join(home, SECRET_PY))

        # 保有 DB とテーブル数
        databases = get_owner_databases()
        db_info = []
        try:
            conn = _connect_default()
            for full_db in databases:
                bare = full_db.split('$', 1)[1] if '$' in full_db else full_db
                try:
                    cur = conn.cursor()
                    cur.execute("USE " + _quote_ident(full_db))
                    cur.execute("SHOW TABLES")
                    t_count = len(cur.fetchall())
                    cur.close()
                    db_info.append({'full': full_db, 'bare': bare,
                                    'tables': t_count})
                except Exception:
                    db_info.append({'full': full_db, 'bare': bare,
                                    'tables': -1})
            conn.close()
        except Exception:
            db_info = [{'full': d, 'bare': d.split('$', 1)[-1], 'tables': -1}
                       for d in databases]

        total_files = sum(d['files'] for d in dir_info) + len(toplevel)

        return jsonify({
            'success': True,
            'owner': owner,
            'home': home,
            'dirs': dir_info,
            'toplevel_py': toplevel,
            'excluded_secret_files': excluded,
            'ext_counts': ext_counts,
            'minified_files': minified,
            'skipped_dirs': skipped_dirs,
            'config_exists': config_exists,
            'approx_file_count': total_files,
            'databases': db_info,
            'review_targets': REVIEW_TARGETS,
            'allow_guest_download': ALLOW_GUEST_DOWNLOAD,
        })
    except Exception as e:
        return _fail('環境情報の取得に失敗しました。', 500, e,
                     'api_export_info')


@migrate_fujinp_scions_bp.route('/api/export/review')
@admin_required
def api_export_review():
    """
    レビュー対象ファイルの内容を取得する（admin 専用）。
    クエリパラメータ file=config_template.py
    """
    try:
        name = request.args.get('file', '')
        if name not in REVIEW_TARGETS:
            # 入力値をそのまま返すと反射型の攻撃に使われうるため返さない
            logging.warning("api_export_review: レビュー対象外の要求 %r", name)
            return jsonify({'success': False,
                            'error': 'レビュー対象外のファイルです'}), 400
        content = load_review_file(name)
        return jsonify({'success': True, 'file': name, 'content': content})
    except Exception as e:
        return _fail('レビュー内容の取得に失敗しました。', 500, e,
                     'api_export_review')


@migrate_fujinp_scions_bp.route('/api/export/register', methods=['POST'])
@admin_required
@csrf_guard
def api_export_register():
    """
    エクスポートパッケージを生成し、ZIP ファイルとして保存して
    メタ情報を MySQL に登録する（admin 専用）。

    リクエスト body:
        databases            : スキーマを同梱する DB 名のリスト
        description          : パッケージ説明（自由テキスト・必須）
        config_template_text : レビュー済み config_template.py（任意）
        force                : 伏せ字漏れ警告を承知で続行する場合 true
    """
    try:
        data = request.get_json(silent=True) or {}
        description = (data.get('description') or '').strip()
        config_template_text = data.get('config_template_text')
        force = bool(data.get('force'))

        if not description:
            return jsonify({'success': False,
                            'error': 'パッケージの説明は必須です。'}), 400

        # --- DB 名の検証（SQL 連結・ZIP エントリ名への混入を防ぐ） -------
        try:
            databases = validate_databases(data.get('databases'))
        except ValueError as ve:
            logging.warning("api_export_register: 不正な databases 指定 %s", ve)
            return jsonify({
                'success': False,
                'error': '選択されたデータベースが不正です。画面を'
                         '再読み込みしてやり直してください。'
            }), 400

        # --- 秘密情報の最終チェック --------------------------------------
        # (a) config_template.py に伏せ字漏れがないか
        # (b) 同梱する .py に秘密情報が直書きされていないか
        #     （config.py を除外しても db.py 等に直書きがあれば漏れる）
        package_files = collect_package_files()

        # config_template.py は移行先で config.py になる。構文が壊れて
        # いると移行先が起動しないうえ、秘密情報の検査（AST 解析）も
        # 効かなくなるため、ここで必ず弾く。force では素通しさせない。
        if config_template_text:
            try:
                ast.parse(config_template_text)
            except SyntaxError as se:
                return jsonify({
                    'success': False,
                    'error': 'config_template.py が Python として不正です'
                             '（%d行目付近: %s）。移行先で config.py として'
                             '使えないうえ、秘密情報の自動チェックも'
                             '効きません。ステップ3で修正してください。'
                             % (se.lineno or 0, se.msg),
                    'syntax_error': {'line': se.lineno or 0,
                                     'message': se.msg},
                }), 400

        if not force:
            suspects = (find_unmasked_secrets(config_template_text)
                        if config_template_text else [])
            file_hits = scan_package_files_for_secrets(package_files)

            if suspects or file_hits:
                parts = []
                if suspects:
                    detail = '、'.join('%d行目 %s' % (no, nm)
                                       for no, nm in suspects[:10])
                    if len(suspects) > 10:
                        detail += ' 他'
                    parts.append('config_template.py に伏せ字化されていない'
                                 '可能性のある値があります（%s）' % detail)
                if file_hits:
                    detail = '、'.join(
                        '%s:%d (%s)' % (h['file'], h['line'], h['name'])
                        for h in file_hits[:10])
                    if len(file_hits) > 10:
                        detail += ' 他%d件' % (len(file_hits) - 10)
                    parts.append('同梱ファイルに秘密情報らしき直書きが'
                                 'あります（%s）' % detail)
                msg = '。'.join(parts) + '。内容を確認してください。'
                if ALLOW_GUEST_DOWNLOAD:
                    # 公開配布の設定では、漏れた秘密は「公開」になる
                    msg = ('【このパッケージはログイン済みユーザー全員が'
                           'ダウンロードできる設定です】' + msg)
                return jsonify({
                    'success': False,
                    'needs_confirm': True,
                    'error': msg,
                    'public_download': ALLOW_GUEST_DOWNLOAD,
                    'suspects': [{'line': no, 'name': nm}
                                 for no, nm in suspects],
                    'file_hits': file_hits,
                }), 400

        # ZIP 生成
        zip_buffer, file_count = build_package_zip(
            databases=databases,
            config_template_text=config_template_text,
            files=package_files,
        )
        zip_bytes = zip_buffer.getvalue()

        # ファイル名（日時入り）。同一秒での二重登録に備えて衝突を回避する
        owner = get_owner_name()
        safe_owner = re.sub(r'[^A-Za-z0-9_-]', '_', owner)
        ts = get_jst_now().strftime('%Y%m%d_%H%M%S')
        pkg_dir = ensure_package_dir()
        filename = 'fujinp_export_%s_%s.zip' % (safe_owner, ts)
        seq = 1
        while os.path.exists(os.path.join(pkg_dir, filename)):
            seq += 1
            filename = 'fujinp_export_%s_%s_%d.zip' % (safe_owner, ts, seq)

        # ディスクに保存
        fpath = os.path.join(pkg_dir, _safe_zip_name(filename))
        with open(fpath, 'wb') as f:
            f.write(zip_bytes)

        # メタ情報を MySQL に登録（登録者は user_id を記録）
        created_by = str(session.get('user_id') or '')
        try:
            new_id = insert_package_record(
                filename=filename,
                description=description,
                source_owner=owner,
                file_count=file_count,
                included_dbs=databases,
                size_bytes=len(zip_bytes),
                created_by=created_by,
            )
        except Exception:
            # メタ情報を登録できなければ、参照されない ZIP を残さない
            try:
                os.remove(fpath)
            except Exception:
                pass
            raise

        logging.info("パッケージ登録: id=%s file=%s (%d ファイル, %d bytes)",
                     new_id, filename, file_count, len(zip_bytes))

        return jsonify({
            'success': True,
            'id': new_id,
            'filename': filename,
            'file_count': file_count,
            'size_bytes': len(zip_bytes),
            'included_dbs': databases,
        })
    except Exception as e:
        return _fail('パッケージの登録に失敗しました。詳細はサーバログを'
                     '確認してください。', 500, e, 'api_export_register')


# ===========================================================================
# インポーター
# ===========================================================================

@migrate_fujinp_scions_bp.route('/importer')
@package_access_required
def importer():
    """
    インポーター画面。

    権限は ALLOW_GUEST_DOWNLOAD に従う（既定は admin のみ）。
    """
    return render_template('migrate_fujinp_scions/importer.html',
                           is_admin=_is_admin(),
                           allow_guest_download=ALLOW_GUEST_DOWNLOAD)


@migrate_fujinp_scions_bp.route('/api/import/list')
@package_access_required
def api_import_list():
    """登録済みパッケージの一覧を返す"""
    try:
        packages = list_package_records()
        return jsonify({'success': True, 'packages': packages})
    except Exception as e:
        return _fail('一覧の取得に失敗しました。', 500, e, 'api_import_list')


@migrate_fujinp_scions_bp.route('/api/import/download/<int:pkg_id>')
@package_access_required
def api_import_download(pkg_id):
    """
    指定パッケージの ZIP をダウンロードする。

    ZIP は静的配信されない場所に置かれており、必ずこのルートを
    通す（＝権限チェックを必ず経由する）。
    """
    try:
        rec = get_package_record(pkg_id)
        if not rec:
            return jsonify({'success': False,
                            'error': 'パッケージが見つかりません'}), 404

        try:
            directory, name = _resolve_package_path(rec['filename'])
        except ValueError as ve:
            logging.error("api_import_download: 不正なファイル名 %s", ve)
            return jsonify({'success': False,
                            'error': 'パッケージが見つかりません'}), 404

        if directory is None:
            return jsonify({'success': False,
                            'error': 'ZIP ファイルが存在しません'}), 404

        logging.info("パッケージ取得: id=%s file=%s user=%s",
                     pkg_id, name, session.get('user_id'))

        return send_from_directory(
            directory, name, as_attachment=True, download_name=name
        )
    except Exception as e:
        return _fail('ダウンロードに失敗しました。', 500, e,
                     'api_import_download')


@migrate_fujinp_scions_bp.route('/api/import/delete/<int:pkg_id>',
                                methods=['POST'])
@admin_required
@csrf_guard
def api_import_delete(pkg_id):
    """指定パッケージ（メタ情報＋ZIP）を削除する（admin 専用）"""
    try:
        filename = delete_package_record(pkg_id)
        if filename is None:
            return jsonify({'success': False,
                            'error': 'パッケージが見つかりません'}), 404
        logging.info("パッケージ削除: id=%s file=%s user=%s",
                     pkg_id, filename, session.get('user_id'))
        return jsonify({'success': True, 'filename': filename})
    except Exception as e:
        return _fail('削除に失敗しました。', 500, e, 'api_import_delete')


@migrate_fujinp_scions_bp.route('/return_to_fujin')
@login_required
def return_to_fujin():
    """FUJIN-Pダッシュボードに戻る"""
    return redirect_to_dashboard()