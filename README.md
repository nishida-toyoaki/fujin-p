# FUJIN-P

FUJIN-P（Fukuchiyama Universal Joint Information Nexus – Prototype）は，Flask／Blueprint構成のウェブアプリケーションプラットフォームです．小さなアプリを次々に立ち上げて組織の情報流通を支えることを目的とし，2024年9月以来，生成AIとの協働によって開発されてきました．認証・ユーザ管理・グループ管理・通知（Slack・メール）などの共通基盤（カーネル）の上に，文書アーカイブ，会議・審議支援，各種台帳管理，コミュニティ運営など多数のアプリが載っています．

本リポジトリは日本語文化圏に向けた公開です．画面・文書はすべて日本語で，日時はJST（DBはDATETIME）を前提に動作します．国際化・多言語化は，有用だと思う方が自由に拡張してください．

## リポジトリ構成

- ルート直下 — カーネル（app.py，auth.py，db.py，decorators.py ほか共通モジュールと templates/）
- `fujinp/` — アプリケーション群（Blueprintパッケージ）
- `static/` — 配布用静的ファイル（利用規約・プライバシーポリシー・画像類）
- `dist/` — さいまる（migrate_fujinp_scions）互換の配布パッケージJSON2点
  - カーネルパッケージ（ルート直下コードの機械可読版）
  - アプリ概要パッケージ（全アプリのソース・マニュアル・技術仕様書を同梱）
- `config_template.py` — 設定ファイルのテンプレート
- `add_license_headers.py` — ライセンスヘッダ一括挿入スクリプト（開発用）

## セットアップの概要

FUJIN-PはPythonAnywhere上での運用を前提に開発されています（他のWSGI環境でも動作するはずですが未検証です）．

1. リポジトリの内容をサーバに配置します
2. `pip install --user -r requirements.txt` で依存パッケージを導入します
3. `config_template.py` をコピーして `config.py` を作り，`<YOUR_...>` プレースホルダを実際の値（MySQL接続情報，Slackトークン等）に書き換えます．ファイル冒頭の⚠注記にある項目は特に目視で確認してください
4. MySQLデータベースを用意し，各アプリのスキーマを宣言します．スキーマ情報は `dist/` のアプリ概要パッケージに含まれる技術仕様書を参照してください
5. WSGI設定でアプリを起動します

各アプリの使い方は，`dist/` のアプリ概要パッケージに同梱されたマニュアルを参照してください．

## ライセンス

GNU Affero General Public License v3.0 or later（AGPL-3.0-or-later）で公開します．全文は [LICENSE](LICENSE) を参照してください．

Copyright © 2024–2026 Toyoaki Nishida（西田豊明）

本ソフトウェアは現状のまま提供され，いかなる保証もありません．改良版をネットワークサービスとして提供する場合も，AGPLの定めに従いソースコードを公開してください．

連絡先：toyoaki.nishida@gmail.com