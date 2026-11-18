# 新サイトの外部サービス設定 — Google認証とSlack

（別サイトへのインストール手引きへの追記．config.py を config_template.py から作る段階で読む）

FUJIN-P のコードとデータはカーネルパッケージと全アプリパッケージで新サイトに運べますが，Google と Slack は FUJIN-P の外にあるサービスです．両者は「どのサイトからの要求を受け付けるか」を自分の側で覚えているので，config.py に旧サイトと同じ値を写しただけでは動きません．この節では，新サイトのために Google と Slack の側で何を登録し直す必要があるのか，その理由と手順を説明します．

## 1. Google 認証（ログイン）

### 1.1 なぜ設定し直す必要があるのか

FUJIN-P の「Google でログイン」は OAuth 2.0 / OpenID Connect という仕組みで動いています．利用者がログインボタンを押すと，FUJIN-P は利用者を Google のログイン画面に送り，Google は認証が済むと利用者を FUJIN-P の決まったURL（コールバックURL）に送り返します．このとき Google は，送り返す先のURLが「あらかじめ登録されたもの」と一致するかを厳密に確かめます．登録に無いURLへは決して送り返しません．これは，第三者が勝手なサイトを立てて他人の認証結果を横取りするのを防ぐための仕組みです．

FUJIN-P のコールバックURLはサイトのドメインを含みます．

```
https://<新サイト名>.pythonanywhere.com/google_callback
```

旧サイトの GOOGLE_CLIENT_ID と GOOGLE_CLIENT_SECRET を新サイトの config.py に写しても，Google 側の登録には旧サイトのURLしか載っていないので，新サイトからのログインは「redirect_uri_mismatch」というエラーで止まります．新サイトのURLを Google 側に追加登録することが，この節の作業です．

### 1.2 手順

1. Google Cloud Console（https://console.cloud.google.com/）に，旧サイトの OAuth クライアントを作ったときの Google アカウントでログインします．
2. 左上のプロジェクト選択から，FUJIN-P 用のプロジェクトを選びます．
3. 「APIとサービス」→「認証情報」を開きます．
4. 「OAuth 2.0 クライアント ID」の一覧から，旧サイトで使っているクライアント（GOOGLE_CLIENT_ID の値と同じIDのもの）を開きます．
5. 「承認済みのリダイレクト URI」に次のURLを追加し，保存します．
   ```
   https://<新サイト名>.pythonanywhere.com/google_callback
   ```
   旧サイトのURLは消さずに残します（旧サイトを閉じるまでは両方必要です）．
6. 同じ画面の「承認済みの JavaScript 生成元」に旧サイトのURLが登録されている場合は，新サイトのURL（https://<新サイト名>.pythonanywhere.com）も追加します．
7. 新サイトの config.py の GOOGLE_CLIENT_ID・GOOGLE_CLIENT_SECRET に，旧サイトと同じ値を入れます（同じクライアントを両サイトで共用します）．
8. 新サイトを再起動（PythonAnywhere の Web タブで Reload）し，ログイン画面から「Google でログイン」を試します．

保存から反映まで数分かかることがあります．すぐにエラーが出ても，少し待ってから再試行してください．

### 1.3 クライアントを共用するか，新しく作るか

旧サイトと同じ OAuth クライアントにURLを追加する方法（上の手順）が最も簡単で，通常はこれで十分です．新サイトを別の組織に引き渡す場合や，旧サイトの管理者と新サイトの管理者が異なる場合は，新サイトの管理者が自分の Google Cloud プロジェクトで新しい OAuth クライアントを作り，その CLIENT_ID と CLIENT_SECRET を config.py に入れます．その場合，OAuth 同意画面の設定（アプリ名・サポートメール・スコープ openid, email, profile）も新しく作る必要があります．

### 1.4 Google Drive 連携（いまくら・ウラルス・レガシーHP など）

Google Drive をバックエンドに使うアプリは，ログイン用とは別の OAuth クライアント（DRIVE_CLIENT_ID・DRIVE_CLIENT_SECRET）と，長期の アクセス許可を表す DRIVE_REFRESH_TOKEN を使います．UNIV_DRIVE_* は大学のアカウント側の同じ組です．

DRIVE_REFRESH_TOKEN はサイトのURLには縛られておらず，Google アカウントと OAuth クライアントの組に対して発行されたものなので，旧サイトの値をそのまま写して動きます．DRIVE_REDIRECT_URI は，トークンを最初に取得するときに使ったURLで，取得済みのトークンを使い続けるだけなら変更は不要です．新サイトで Drive の認可を取り直す（新しい Google アカウントの Drive に切り替える）場合だけ，Drive 用クライアントの「承認済みのリダイレクト URI」に新サイトのURLを追加し，DRIVE_REDIRECT_URI も合わせて変更します．

## 2. Slack

### 2.1 なぜ注意が必要なのか

FUJIN-P の Slack 通知（研究倫理審査・外部連携審査・キターレオンライン審議・テーブルポストなど）は，config.py の SLACK_BOT_TOKEN（xoxb- で始まるボットトークン）を使って，各アプリの *_SLACK_CHANNEL_ID が指すチャンネルに投稿します．トークンはサイトのURLに縛られていないので，旧サイトの値を新サイトに写せば，新サイトからもすぐに投稿できます．

これは便利な反面，移行作業中の落とし穴になります．旧サイトと新サイトの両方に同じトークンと同じチャンネルIDが入っていると，両方が同じチャンネルに投稿します．たとえば移行作業の確認のために新サイトで審査を動かすと，本番のチャンネルに試験投稿が出てしまい，旧サイトで同じ案件が進んでいればその通知と二重になります．

もう一つ，予約投稿の問題があります．FUJIN-P の通知は即時投稿のほかに，締切前のリマインドなどを Slack の予約投稿（chat.scheduleMessage）として登録します．予約投稿は Slack の側で保持され，登録したサイトが止まっても予定時刻に投稿されます．旧サイトが登録した予約は，旧サイトを閉じた後も Slack から出続けます．

### 2.2 手順

1. 新サイトの config.py を最初に作る時点では，SLACK_BOT_TOKEN と各 *_SLACK_CHANNEL_ID を空のままにしておきます．空であれば，通知は送られずに通知台帳（notify_ledger）に「トークン未設定」として記録されるだけで，アプリ本体の動作には支障がありません．
2. 新サイトでの動作確認が済み，旧サイトの該当アプリを閉じた（書き込みを止めた）後に，SLACK_BOT_TOKEN と *_SLACK_CHANNEL_ID を旧サイトと同じ値で設定し，Reload します．
3. 旧サイトの config.py からは，同じタイミングで SLACK_BOT_TOKEN を外す（または空にする）か，旧サイトの該当アプリを停止します．これで同じチャンネルへ投稿するのは新サイトだけになります．
4. 旧サイトが登録した予約投稿が残っていないかを確認します．旧サイトの通知台帳（notify_ledger）で scheduled_message_id が入っている行のうち，予定時刻（post_at）が未来のものが該当します．不要なものは，その scheduled_message_id を使って Slack 側で取り消します（アプリに取り下げ機能があればそれを使い，無ければ Slack API の chat.deleteScheduledMessage を呼びます）．
5. キターレオンライン審議の締切バッチ（KITARE_DEADLINE_BATCH_TOKEN で保護された URL を PythonAnywhere の Scheduled task から定時に呼ぶ仕組み）を使っている場合は，新サイトの Scheduled task に新サイトのURLで登録し直し，旧サイトの Scheduled task を止めます．これを忘れると，旧サイトのバッチが旧サイトのデータで締切通知を出し続けます．

### 2.3 Slack ワークスペースが別になる場合

新サイトを別の組織に引き渡し，Slack ワークスペースも別になる場合は，新しいワークスペースで Slack アプリを作り，ボットトークン（chat:write，予約投稿を使うなら chat:write に加えて必要なスコープ）を発行して SLACK_BOT_TOKEN に入れ，通知先チャンネルのIDを各 *_SLACK_CHANNEL_ID に入れます．ボットは投稿先の各チャンネルに招待しておく必要があります（招待されていないチャンネルには投稿できず，台帳に not_in_channel が記録されます）．すらくみね（SURAKUMINE_*）や Slack_port の複数ボット設定（SLACK_BOTS）も，同じ要領でワークスペースごとに作り直します．

## 3. 切替の順序（まとめ）

Google とSlack の設定を含めた，サービス提供元を旧サイトから新サイトへ移すときの順序です．

1. 新サイトにカーネルと全アプリパッケージを入れ，config.py を作る（Google の値は旧サイトと同じ，Slack の値は空）．
2. Google Cloud Console で新サイトのコールバックURLを追加登録する．
3. 新サイトで Google ログインと各アプリの動作を確認する（この時点では Slack には何も出ない）．
4. 移すアプリについて，旧サイトで書き込みを止める（アプリごとに権限を外す）．
5. データ（テーブルとアップロード領域）をアプリごとに移す．
6. 新サイトに Slack の値を入れて Reload し，旧サイトからは外す．旧サイトの予約投稿と Scheduled task を片づける．
7. 新サイトでサービスを再開し，利用者に新URLを案内する．パスワードでログインしていた利用者には，新サイトで「パスワードを忘れた」から再設定するよう伝える．
