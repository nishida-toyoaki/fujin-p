## 第1段階 — カーネルとアプシャまでを立ち上げる

この文書は，立ち上げスクリプト `fujinp_bootstrap.py` を使って，新品の PythonAnywhere サイトに FUJIN-P のカーネルとアプシャまでを立ち上げる手順です．
立ち上がった状態とは，admin でログインすると管理者ダッシュボードが出て，そこにアプシャのカードがある状態を指します．
一般のアプリはまだ1本も入っていません．
入るカーネル・admin・アプシャは，全アプリパッケージを書き出した日の版です．

所要はおよそ30分です．
手で行うのは，データベースの作成，`config.py` の用意，WSGI の設定の3つです．
ファイルの配置，テーブルの作成，最初の admin の登録はスクリプトが行います．
MySQL コンソールを開く必要はありません．

## 用意するもの

元のサイトで書き出した全アプリパッケージ（`fujinp_apps_overview_<日時>.json`）を手元に置きます．
元のサイトの `config.py` を控えてあれば，それも置きます．

PythonAnywhere の Web タブで，Flask の Web アプリを1つ作っておきます．
Python の版は元のサイトに合わせます．

## 1. データベースを作る

PythonAnywhere の Databases タブで，`<アカウント名>$default`・`<アカウント名>$fujinp`・`<アカウント名>$public` の3つを作ります．
カーネルとアプシャが使うのは `$default` だけですが，第2段階でアプリを入れると残りの2つも使います．

同じ画面に出ている接続情報（ホスト名・ユーザ名・パスワード）を手順4で使います．

## 2. 全アプリパッケージを置き，立ち上げスクリプトを取り出す

Files 画面でホームディレクトリ（`/home/<アカウント名>/`）を開き，全アプリパッケージをアップロードします．
大きいファイル（15MB ほど）なので，アップロードにはしばらくかかります．

Bash コンソールを開き，次を実行します（`fujinp_apps_overview_<日時>.json` は実際のファイル名に置き換えます）．

```text
cd ~
python3 -c "import json,sys; d=json.load(open(sys.argv[1])); f=[x for x in d['bootstrap']['kernel']['files'] if x['path']=='fujinp_bootstrap.py'][0]; open('fujinp_bootstrap.py','w',encoding='utf-8').write(f['content'])" fujinp_apps_overview_<日時>.json
python3 fujinp_bootstrap.py fujinp_apps_overview_<日時>.json --list
```

ホームディレクトリに `fujinp_bootstrap.py` ができ，入っているものの一覧が出ます．
カーネル・admin・アプシャの版，書き出すファイル，作る表が並びます．
`--list` は何も書き込みません．

`bootstrap 欄がありません` と出たときは，その JSON が古いアプシャで書き出されたものです．
元のサイトで書き出し直してもらってください．

## 3. ファイルを書き出し，ライブラリを入れる

```text
python3 fujinp_bootstrap.py fujinp_apps_overview_<日時>.json --files
pip3.13 install --user -r requirements.txt
```

ホームディレクトリに `app.py`・`auth.py`・`db.py` などが置かれ，`templates/`・`static/`・`static_for_distribution/`・`fujinp/admin/`・`fujinp/app_share/` ができます．
`static/` には，配布用静的ファイル（規約類・アイコン・`manifest.json` など）が `static_for_distribution/` から写されます．
既にあって中身が違うファイルは触りません．上書きしたいときは `--files --force` を付けます．その場合，上書きされる前のファイルは `fujinp_bootstrap_backup_<日時>/` に退避されます．

`pip3.13` の数字は，Web タブに出ている Python の版に合わせます．
版が違うと，サイトを開いたときに `ModuleNotFoundError` になります．

## 4. config.py を置く

元のサイトの `config.py` を控えてあるときは，それをホームディレクトリにアップロードします．
同じアカウント名で作り直すなら，そのまま使えます．
アカウント名が違うときは，`DB_ACCOUNT`・`BASE_URL`・`DB_HOST`・`DB_USER`・`DB_PASSWORD`・`DB_DEFAULT`・`DB_FUJINP`・`DB_PUBLIC` を新しいサイトの値に直します．

控えが無いときは，雛形から書きます．

```text
cd ~
cp config_template.py config.py
```

Files 画面で `config.py` を開き，次の項目の `<...>` を埋めます．

| 項目 | 入れる値 |
|---|---|
| `SECRET_KEY` | セッションの署名に使う長いランダム文字列．`python3 -c "import secrets; print(secrets.token_urlsafe(48))"` で作った値を貼る．`os.environ.get(...)` の行ごと `SECRET_KEY = '<作った値>'` に置き換えてよい |
| `DB_ACCOUNT` | PythonAnywhere のアカウント名 |
| `BASE_URL` | `https://<アカウント名>.pythonanywhere.com` |
| `DB_HOST`・`DB_USER`・`DB_PASSWORD` | Databases タブに出ている値 |
| `DB_DEFAULT`・`DB_FUJINP`・`DB_PUBLIC` | `'<アカウント名>$default'`・`'<アカウント名>$fujinp'`・`'<アカウント名>$public'` |
| `UPLOAD_BASE_DIR` | 投稿ファイルを置く場所．`'/home/<アカウント名>/fujinp_file_uploads'` など |
| `ORGANIZATION`・`ORGANIZATION_EN` | 組織名（規約類に入る）．例：`'福知山公立大学'`・`'The University of Fukuchiyama'` |

`SESSION_COOKIE_SAMESITE` は `'Lax'` のままにします．
Google 認証・メール・Slack などの残りの `<...>` は，そのままで構いません．立ち上げはメールアドレスとパスワードのログインで行い，Google 認証などはあとから足します．
その手順と順序は，手引き「[外部サービス設定（Google認証・Slack）](/migration_assistant/guide/external)」にあります．

## 5. テーブルを作る

```text
python3 fujinp_bootstrap.py fujinp_apps_overview_<日時>.json --schema
```

`<アカウント名>$default` に，共通テーブル（`users` など9表）とアプシャの表（6表）が作られます．
続けて，アプシャの正本にカーネル・admin・アプシャの3行と，その台帳・文書・区画・背景色が入り，最後に正本 `fujinp/app_registry.json` が発行されます．
作られたテーブルの一覧が表示されます．
既にあるテーブルと行には触りません．何度実行しても同じ結果になります．

接続できないときは，`config.py` の接続の項目と，データベースが作ってあるかを順に確かめてください．

## 6. WSGI を設定して Reload する

Web タブの WSGI configuration file を開き，中身を次のようにします（`<アカウント名>` は自分のものに置き換えます）．

```text
import sys

path = '/home/<アカウント名>'
if path not in sys.path:
    sys.path.insert(0, path)

from app import app as application
```

Virtualenv を使っている場合は，その欄にパスを入れます．
使っていない場合は空のままで構いません（手順3で `--user` を付けて入れたので，そのまま読まれます）．

保存して，緑の Reload ボタンを押します．

## 7. 最初の admin を登録する

```text
python3 fujinp_bootstrap.py fujinp_apps_overview_<日時>.json --admin
```

メールアドレス・氏名・パスワードを訊かれます．パスワードは8文字以上にしてください．
入力が済むと，その相手が admin として `users` テーブルに登録されます．
同じメールアドレスが既にあるときは，何も変えずに知らせます．

あとで利用者のデータを戻すと，この行は戻したデータに入れ替わります．
戻すデータに admin の利用者が入っていることを，戻す前に確かめておいてください．

## 8. ログインする

ブラウザで `https://<アカウント名>.pythonanywhere.com/` を開きます．
ログイン画面が出るので，7で登録したメールアドレスとパスワードを入れます．

管理者ダッシュボードが出て，アプシャのカード（📦 アプシャ）があれば，カーネルとアプシャまでが立ち上がっています．
続けて第2段階に進みます．

## うまくいかないときの確認順

サイトを開いて Something went wrong が出るときは，Web タブのエラーログを見ます．
`ModuleNotFoundError` なら手順3のライブラリ，`config` に関するものなら手順4，データベースに関するものなら手順1と5を見直します．
`SameSite must be 'Strict', 'Lax', or 'None'` と出るときは，`config.py` の `SESSION_COOKIE_SAMESITE` が `'Lax'` になっているかを確かめます．

ログイン画面は出るがログインできないときは，手順7をもう一度実行します．既に登録されていれば，そう表示されます．

ログインはできるがダッシュボードにアプシャのカードが出ないときは，`fujinp/app_registry.json` が書けているかを見ます．
無ければ手順5をもう一度実行し，Reload します．

## 入っていないもの

`config.py` は入っていません．手順4で置きます．

アプシャの文書編集画面にある画像のアップロードは，マイノート（`my_md_notes`）が入っているサイトでだけ動きます．
第1段階の時点では，押しても「画像アップロード失敗」と出ます．第2段階で全アプリを入れると動くようになります．
