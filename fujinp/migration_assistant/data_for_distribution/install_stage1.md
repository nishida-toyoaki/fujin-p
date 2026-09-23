## 第1段階 — 種（カーネル＋アプシャ）を立ち上げる

この文書は `fujinp_kernel_install.py`（FUJIN-P のカーネルとアプシャを収めた導入スクリプト）を使って，新品の PythonAnywhere サイトに FUJIN-P の種を立ち上げる手順です．スクリプトは FUJIN-P の公開リポジトリ（GitHub）の https://github.com/u-fukuchiyama/fujin-p/blob/main/fujinp/migration_assistant/data_for_distribution/fujinp_kernel_install.py から誰でもダウンロードできます．FUJIN-P のサイトにアカウントがあれば，まいあしの「🚚 別サイトへのインストール」の画面からも入手できます．

種が立ち上がった状態とは，admin でログインすると管理者ダッシュボードが出て，そこにアプシャのカードがある状態を指します．アプリはまだ1本も入っていません．アプリはこのあと，アプシャの取り込み機能で1本ずつ入れます．

所要はおよそ 30 分です．手で行うのは，データベースの作成，`config.py` の記入，WSGI の設定，最初の admin の登録の4つだけです．ファイルの配置とテーブルの作成は自動です．

## 用意するもの

PythonAnywhere の有料アカウント（Developer プラン以上）．FUJIN-P はデータベース（MySQL）を全面的に使うので，無料プランでは動きません．Web タブで Flask の Web アプリを1つ作っておいてください．Python は 3.10 以上を選びます．

`fujinp_kernel_install.py` を1本，手元にダウンロードしておきます．

## 1. スクリプトを置く

PythonAnywhere の Files 画面を開き，ホームディレクトリ（`/home/<アカウント名>/`）に `fujinp_kernel_install.py` をアップロードします．

Bash コンソールを開き，中身を確かめます．

```bash
cd ~
python3 fujinp_kernel_install.py --list
```

ファイルの一覧が出ます．この操作は何も書き込みません．

## 2. ファイルを展開する

```bash
python3 fujinp_kernel_install.py --files
```

ホームディレクトリに `app.py`・`auth.py`・`db.py` などが置かれ，`templates/` と `fujinp/`（`fujinp/admin/`・`fujinp/app_share/`）ができます．既にあるファイルは触りません．上書きしたいときは `--files --force` を付けます．その場合，上書きされる前のファイルは `fujinp_seed_backup_<日時>/` に退避されます．

`config.py` はこの中に入っていません．次の手順で自分で書きます．

## 3. データベースを作る

PythonAnywhere の Databases タブで，データベースを作ります．

種に必要なのは `<アカウント名>$default` の1つです．アプリを入れる段になったら `<アカウント名>$fujinp` と `<アカウント名>$public` も作ります．先に3つとも作っておいても構いません．

同じ画面に出ている接続情報（ホスト名・ユーザ名・パスワード）を次で使います．

## 4. 依存ライブラリを入れる

```bash
cd ~
pip3.13 install --user -r requirements.txt
```

`pip3.13` の数字は，Web タブに出ている Python の版に合わせます．版が違うと，サイトを開いたときに `ModuleNotFoundError` になります．

## 5. config.py を書く

```bash
cd ~
cp config_template.py config.py
```

Files 画面で `config.py` を開き，`<...>` の箇所を埋めます．埋めるのは次の6つです．

`SECRET_KEY` はセッションの署名に使う長いランダム文字列です．次の1行で作った値を貼ります．

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

`DB_ACCOUNT` は PythonAnywhere のアカウント名です．`BASE_URL` は `https://<アカウント名>.pythonanywhere.com` です．`DB_HOST`・`DB_USER`・`DB_PASSWORD` は Databases タブに出ている値をそのまま入れます．

Google 認証とメールは空のままで構いません．種はメールアドレスとパスワードのログインで立ち上げ，Google 認証はあとから足します．

## 6. テーブルを作る

```bash
cd ~
python3 fujinp_kernel_install.py --schema
```

`<アカウント名>$default` に 14 のテーブルが作られ，アプシャの区画6行とカーネル3行（`_platform`・`admin`・`app_share`）が入り，最後に正本 `fujinp/app_registry.json` が書き出されます．

作られたテーブルの一覧が表示されます．既にあるテーブルには触りません．何度実行しても同じ結果になります．

接続できないときは，`config.py` の4項目とデータベースが作ってあるかを順に確かめてください．

## 7. WSGI を設定して Reload する

Web タブの WSGI configuration file を開き，中身を次のようにします（`<アカウント名>` は自分のものに置き換えます）．

```python
import sys

path = '/home/<アカウント名>'
if path not in sys.path:
    sys.path.insert(0, path)

from app import app as application
```

Virtualenv を使っている場合は，その欄にパスを入れます．使っていない場合は空のままで構いません（手順4で `--user` を付けて入れたので，そのまま読まれます）。

保存して，緑の Reload ボタンを押します．

## 8. 最初の admin を登録する

```bash
cd ~
python3 fujinp_seed/make_admin.py
```

メールアドレス・氏名・パスワードを訊かれます．パスワードは8文字以上にしてください．入力が済むと `users` テーブルへの `INSERT` 文が1つ表示されます．

Databases タブから MySQL コンソールを開き（`<アカウント名>$default` を選びます），表示された文をそのまま貼って実行します．

このスクリプトはデータベースに接続せず，何も書き込みません．パスワードのハッシュを作って文を組み立てるだけです．

## 9. ログインする

ブラウザで `https://<アカウント名>.pythonanywhere.com/` を開きます．ログイン画面が出るので，8 で登録したメールアドレスとパスワードを入れます．

管理者ダッシュボードが出て，「機関内向け」の区画にアプシャのカード（📦 アプシャ）があれば，種は立ち上がっています．

## ここから先

アプシャを開くと，レジストリに3行（`_platform`・`admin`・`app_share`）が並んでいます．ここにアプリのパッケージを取り込んでいきます．

アプリを入れる流れは，配布元のサイト（アプリが動いている FUJIN-P サイト）のアプシャでパッケージの JSON を持ち帰り，こちらのアプシャでその JSON を検証して適用し，Reload する，という3手です．配布元にはログインできれば足り，admin である必要はありません．全アプリをまとめた JSON も持ち帰れます．適用のときに，そのアプリのファイル・正本・テーブル・文書がまとめて入ります．アプリが必要とする設定（Slack のトークンなど）は `config.py` に自分で足します．何が要るかはアプシャの管理画面の「定数」タブに出ます。

ユーザの移行（ゆーまい）も，アプリとして取り込みます．

## うまくいかないときの確認順

サイトを開いて Something went wrong が出るときは，Web タブのエラーログを見ます．`ModuleNotFoundError` なら手順4のライブラリ，`config` に関するものなら手順5，データベースに関するものなら手順3と6を見直します．

ログイン画面は出るがログインできないときは，手順8の `INSERT` が実際に実行されたかを MySQL コンソールで確かめます（`SELECT id, email, category FROM users;`）．

ログインはできるがダッシュボードにアプシャのカードが出ないときは，`fujinp/app_registry.json` が書けているかを見ます．無ければ手順6をもう一度実行します．

管理者ダッシュボードで「ユーザ管理」を押して画面が出ないときは，手順6でテーブルが全部できているかを確かめます．

## 入っていないもの

`config.py` は配布物に含まれません．秘密情報を持つファイルなので，各サイトで書きます．

`static/` は含まれません．カーネルのテンプレートは静的ファイルを参照していないためです．アプリを入れると必要になることがあります．

アプシャの文書編集画面にある画像のアップロードは，マイノート（`my_md_notes`）が入っているサイトでだけ動きます．種の段階では，押しても「画像アップロード失敗」と出ます．文書そのものの編集と保存は動きます．

`fujinp/admin/templates/admin/dashboard.html` は配布物から外してあります．どこからも描画されない古いテンプレートで，中に個別アプリの参照が直書きされているためです．
