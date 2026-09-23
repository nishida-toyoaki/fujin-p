## まいあし 旧文書（2026年7月版・保存用）

この文書は，まいあしのユーザマニュアル（2026-07-25）と技術仕様書（2026-07-29）を1つにまとめたものです．いずれもアプシャの正本化（2026-08-25）と使用コントローラー（2026-08-27）より前に書かれたもので，現行のコードとは食い違う箇所があります．書き直すまでの控えとして，ここに置いてあります．

新しい「別サイトへのインストール」機能については，全体マニュアル・第1段階マニュアル・第2段階マニュアルを見てください．

---

# 旧ユーザマニュアル（2026-07-25 時点）

# 👣 まいあし（MaiAshi）ユーザーズマニュアル

- 対象アプリ：`migration_assistant`（FUJIN-P アプリパッケージ）
- 対象コード：`migration_assistant_routes.py`、`migration_assistant_index.html`（いずれも 2026-07-12 版に 2026-07-25 の改修を適用したもの）
- 改訂：2026-07-25（前版 2026-02-11 を全面改訂。同日の改修内容も反映）

まいあしは、**FUJIN-P のスタブ（機能モジュール）を段階的に構築するプロセス**を教材（コース）として提供し、学習者がステップを一つずつ進めながら移行・構築作業を進められるよう支援するアプリです。教材を作る人（師匠）と教材で学ぶ人（弟子）という**師弟制度**を軸に構成されています。

---

## 0. 前版からの主な変更点（お読みください）

前版マニュアル（2026-02-11）と現在の実装には次の食い違いがありました。本版で修正済みです。

| 前版の記述 | 現在の実装 |
| --- | --- |
| 受講開始時に「進捗を著者と共有する」をON/OFFで選べる（オプトイン） | **選択肢はありません。受講すると進捗は必ず著者（師匠）に共有されます** |
| 共有をOFFにすると進捗データが削除される／OFFでは進捗が保存されない | 該当機能なし。進捗は常に保存されます |
| 進捗共有設定を後から変更できる（`toggle_share` API） | **該当APIは存在しません** |
| 師匠ダッシュボードに「進捗を共有中の弟子」が表示される | 見出しは「👥 受講中の弟子」。受講者は全員表示されます |
| （記載なし） | 教材ごとの固有URL `/migration_assistant/course/<course_id>` が追加されました |
| （記載なし） | 進捗は5段階（未着手／取り組み中／苦戦／完了／放棄）から選びます |
| （記載なし） | 受講の取り消し、教材の削除、教材の一括結合表示（📖 教材一括）が使えます |

---

## 1. 役割（ロール）

| 役割 | できること |
| --- | --- |
| **弟子（学習者）** | 公開教材を探して受講開始、ステップごとに進捗を記録、ステップ本文を読む、受講の取り消し |
| **師匠（著者/講師）** | 教材の作成・公開設定・内容編集（フェーズ/ステージ/ステップ）、受講者の進捗確認、教材の一括結合表示、教材の削除 |
| **管理者** | 「師匠候補」の承認・取消（運用記録） |

### 師匠になるための手続き

**教材を作るために管理者の承認は必要ありません。** ログインしていれば誰でも師匠ダッシュボードから教材を作成でき、自分が作成した教材はいつでも編集できます。教材の編集画面を最初に開いた時点で、システムが「師匠候補」の記録を自動的に作成します（備考欄に「Course ID ○○ の作成により自動昇格」と記録されます）。

管理者パネルの「師匠候補の承認」は、**誰を師匠として認定したかを運用上記録・管理するための機能**です。承認の有無が教材作成・編集の可否を左右することはありません。

---

## 2. 入口（メインページ）

- URL：`/migration_assistant/`（`/migration_assistant/index` も同じ）
- ログインしていない場合はエラーページ（「ログインが必要です」）が表示されます。

画面は左右2つのパネルに分かれています。

- **左パネル「コース名」**：自分が受講中の教材カードが並びます。上部の **「📚 新規教材」** ボタンで公開教材一覧（受講申し込み画面）が開きます。
- **右パネル**：左でカードを選ぶと、その教材のフェーズ／ステージ／ステップと進捗が表示されます。未選択のときは「左のリストからコースを選択してください」と表示されます。

ヘッダーのリンク

| リンク | 遷移先 | 表示条件 |
| --- | --- | --- |
| 👨‍🏫 師匠ダッシュボード | `/migration_assistant/mentor_dashboard` | 常に表示 |
| ⚙️ 管理者パネル | `/migration_assistant/admin_panel` | 管理者のみ表示 |
| 🏠 FUJIN-Pダッシュボードに戻る | `/migration_assistant/return_to_fujin` | 常に表示 |

---

## 3. 弟子（学習者）向け

### 3.1 公開教材を探して受講する

1. 左パネル上部の **「📚 新規教材」** ボタンを押します（公開教材一覧モーダルが開きます）。
2. 一覧には、公開されている教材のタイトル、**👨‍🏫 著者**、説明、**📚 フェーズ数**が表示されます。公開教材が無いときは「📭 公開されている教材がありません」と表示されます。
3. 各カードの下部に次の案内があります。

   > ※受講を開始すると、著者にあなたの進捗が自動的に共有されます。

4. **「この教材で学習を開始する →」** を押すと確認ダイアログ（「『教材名』の学習を開始しますか？」）が出ます。OKすると「✅ 受講申し込みが完了しました！」と表示され、左パネルに教材カードが追加されます。
5. すでに受講している教材をもう一度開始しようとすると「❌ 現在受講中科目の重複履修はできません」と表示され、受講は増えません。

> **重要：進捗共有について**
> 進捗共有のON/OFFを選ぶ設定はありません。受講した時点で、その教材の著者が自動的にあなたの師匠となり、あなたの進捗は師匠ダッシュボードから見える状態になります。共有したくない場合は、その教材を受講しない（または後述の手順で受講を取り消す）という選択になります。

### 3.2 教材の構造

教材は師匠が次の3階層で設計します。

- **フェーズ（Phase）**
  - **ステージ（Stage）**
    - **ステップ（Step）** … 進捗を記録する最小単位

画面上の見出しは「Phase 1: ○○」「Stage 1-2: ○○」のように番号付きで表示され、各ステップには「1-2-3」形式の通し番号が付きます。

### 3.3 ステップを進める（進捗の記録）

各ステップの行には、進捗を選ぶラジオボタンが5つ並んでいます。

| ステータス | 意味 | 画面上の色 |
| --- | --- | --- |
| 未着手 | まだ手をつけていない（初期値） | グレー |
| 取り組み中 | 作業中 | 青 |
| 苦戦 | 詰まっている（師匠に気づいてもらいたいとき） | オレンジ |
| 完了 | 終わった | 緑 |
| 放棄 | 進めないと判断した | 赤 |

ラジオボタンを選ぶとその場でサーバーに保存され、左パネルの進捗バー（「完了」の数 ÷ 教材の全ステップ数）も更新されます。保存ボタンを押す必要はありません。

「**苦戦**」は師匠側の進捗一覧にも赤字で表示されます。行き詰まったときは遠慮なく「苦戦」を選んでおくと、師匠が状況に気づきやすくなります。

### 3.4 ステップの本文を読む

各ステップの **「詳細」** ボタンを押すと、師匠が書いたステップ本文（Markdown）がモーダルで表示されます。数式（`$...$` および `$$...$$`）は自動的に整形されて表示されます。

> このモーダルは、画面上部に案内が出るとおり **一番下にある「✕ 閉じる」ボタン**で閉じてください（背景クリックでは閉じません）。

### 3.5 教材ごとの固有URL（リンクの共有・ブックマーク）

受講中の教材カードは、それぞれ固有のURLを持ちます。

```
/migration_assistant/course/<course_id>
```

- カードを普通にクリックすると、ページを再読み込みせずにその教材が開き、アドレスバーのURLだけが固有URLに切り替わります。
- **Ctrl（⌘）+クリック・中クリック**で別タブに開けます。カードを右クリックして「リンクをコピー」すれば、その教材へのURLをブックマークや共有に使えます。
- ブラウザの**戻る／進む**でも教材の選択状態が復元されます。
- 固有URLを開いたとき、そのコースを自分が受講していない（または存在しない）場合は、教材が選択されていない通常のメインページが表示されます。他人の受講状態は見られません。

### 3.6 受講を取り消す

左パネルの教材カードにマウスを乗せると、右上に **「×」** ボタンが出ます。押すと確認ダイアログが表示されます。

> 「教材名」を削除しますか？
> ※ 進捗情報も全て削除されます

OKすると受講登録が削除され、**その教材の進捗データもすべて削除されます**（元に戻せません）。同じ教材をもう一度受講することは可能ですが、進捗はゼロから再スタートになります。

---

## 4. 師匠（著者/講師）向け

### 4.1 師匠ダッシュボード

- URL：`/migration_assistant/mentor_dashboard`

左パネル「**マイ教材**」に自分が作成した教材が並びます。各カードにはフェーズ数（📚）、更新日時（🕐）、受講者数（👥 ○人受講中）が表示されます。教材が無いときは「まだ教材がありません」と表示されます。

教材を選ぶと右パネルに次のボタンが並びます。

| ボタン | 機能 |
| --- | --- |
| ⚙️ タイトル・概要を変更 | タイトル・説明・公開設定を変更 |
| 📝 教材を編集 | コンテンツ編集画面へ移動 |
| 📖 教材一括 | 教材全体を1本のMarkdown／HTMLに結合して別タブ表示 |
| 🗑️ 削除 | 教材を削除（受講者が1人以上いる教材では**ボタン自体が表示されません**） |

その下に「**👥 受講中の弟子**」セクションがあります。受講者が居ない場合は「まだ弟子がいません」と表示されます。

### 4.2 教材（コース）を新規作成する

1. 左パネルの **「📚 新規教材」** ボタンを押します。
2. 入力項目

   | 項目 | 必須 | 備考 |
   | --- | --- | --- |
   | 教材タイトル | **必須** | 空欄だと「教材タイトルは必須です」と警告 |
   | 教材の説明 | 任意 | この教材で学べる内容の概要 |
   | 公開する（弟子が検索・受講できるようにする） | — | **既定でON** |

3. **「作成」** を押すと「✅ 教材を作成しました！」と表示され、**そのままコンテンツ編集画面に自動で移動**します。

> 「公開する」が既定でONになっている点に注意してください。準備中の教材を人目に触れさせたくない場合は、作成時にチェックを外すか、後から「⚙️ タイトル・概要を変更」でOFFにしてください。

### 4.3 教材の設定を変更する

「⚙️ タイトル・概要を変更」から、タイトル（必須）、説明、**公開する**のチェックをまとめて変更し、「変更を保存」で確定します。

### 4.4 教材の内容を編集する（コンテンツ編集画面）

- URL：`/migration_assistant/mentor_content_editor/<course_id>`
- 編集できるのは**自分が作成した教材のみ**です（管理者は他人の教材も編集可）。権限がない場合は「この教材の編集権限がありません（作成者本人のみ編集可能です）」と表示されます。

#### 階層の編集

| 対象 | 操作 |
| --- | --- |
| フェーズ | 「➕ Phase追加（最初）」「➕ Phase追加」（各フェーズの末尾）／「✏️ 編集」（タイトル）／「🗑️ 削除」 |
| ステージ | 「➕ Stage追加（最初）」「➕ Stage追加」／「✏️ 編集」／「🗑️ 削除」 |
| ステップ | 「➕ Step追加（最初）」「➕ Step追加」／「✏️ 編集」（タイトル）／「📝 詳細編集」（本文）／「🗑️ 削除」 |

- 追加ボタンは「どこに挿入するか」を兼ねています。**先頭に入れたいときは「（最初）」付きのボタン**、途中に入れたいときは**その直前の項目の下にある追加ボタン**を押します。番号は挿入後に自動で振り直されます。
- **ドラッグによる並べ替えはできません。** 順番を変えたい場合は、目的の位置に新しく作って中身を移すか、削除して作り直してください。
- 削除の確認文言
  - フェーズ：「このPhase内の全データが削除されます。よろしいですか？」（配下のステージ・ステップも消えます）
  - ステージ：「このStage内の全Stepが削除されます。よろしいですか？」
  - ステップ：「このStepを削除しますか？」

#### ステップ本文（Markdown）の編集

「📝 詳細編集」で本文エディタが開きます。

- **👁️ プレビュー更新**：入力内容をサーバーでHTMLに変換して右側に表示します。数式（`$...$` / `$$...$$`）も整形されます。
- **🖼️ 画像アップロード**：ファイル選択ダイアログから画像を選ぶと、アップロード後に `![ファイル名](URL)` がカーソル位置に挿入されます。
  - 対応形式：png / jpg / jpeg / gif / webp
  - サイズ上限：**10MB**（超えると「ファイルサイズは10MB以下にしてください」）。ブラウザ側とサーバー側の両方で確認します
  - **ドラッグ&ドロップおよびクリップボードからの貼り付けには対応していません。**
- **🔗 リンク挿入**：URL → リンクテキストの順に入力すると `[テキスト](URL)` が挿入されます。
- 保存：**「💾 保存」** または **「💾 保存して閉じる」**。`Ctrl+S`（Mac は ⌘+S）でも保存できます。
- 未保存のまま閉じようとすると「変更箇所がありますがこのまま終了しますか？」と確認されます。

> **注意：自動保存はされません。**
> 編集画面の上部にも同じ注意が表示されます。**必ず「💾 保存」を押してください。** 保存せずにダイアログを閉じると変更は破棄されます（確認ダイアログは出ます）。

- フェーズ／ステージ／ステップのタイトルは、それぞれの編集モーダルで「💾 保存」または入力欄で **Enter** を押すと保存されます。

### 4.5 受講者（弟子）の進捗を確認する

師匠ダッシュボードの「👥 受講中の弟子」に、その教材の受講者が一覧表示されます。

各行の表示内容

- 氏名
- 受講開始: 日時
- 進捗: 完了ステップ数 / 全ステップ数 ステップ完了
- 進捗バーと百分率（完了ステップ数 ÷ 全ステップ数、整数％）

**「詳細」** ボタンを押すと「○○ さんの進捗状況」モーダルが開き、ステップ単位の一覧表（ステップ／ステータス）が表示されます。ステータスは「完了」が緑、「苦戦」が赤、「取り組み中」が青、記録のないステップは「未着手」（グレー）です。

一覧は受講者全員が対象です（共有のON/OFFという概念はありません）。

### 4.6 教材を一括で読む・印刷する（📖 教材一括）

「📖 教材一括」ボタンを押すと、教材の全フェーズ／ステージ／ステップの本文を1本に結合したページが**別タブ**で開きます。

- 上部のボタン
  - **Markdownをコピー**：結合した生Markdownをクリップボードにコピー
  - **生Markdown表示** / **レンダリング表示**：表示の切り替え
  - **印刷 / PDF**：ブラウザの印刷ダイアログを開く（PDF保存に利用できます）
- 本文が空のステップは「*（本文未記入）*」と表示されるため、書き忘れの点検に使えます。
- ポップアップがブロックされると「ポップアップがブロックされました。ポップアップを許可してください。」と表示されます。ブラウザ側でポップアップを許可してください。
- 閲覧できるのは**自分が作成した教材**のみです（管理者は全教材）。

### 4.7 教材を削除する

「🗑️ 削除」ボタンで削除できます。確認文言は「『教材名』を削除しますか？ ※ コンテンツも全て削除されます」です。フェーズ／ステージ／ステップのすべてが削除されます。

**受講者が1人以上いる教材は削除できません。** その場合は削除ボタンが表示されません（サーバー側でも「弟子が受講中のため削除できません」と拒否されます）。削除したい場合は、受講者に受講の取り消しを依頼するか、公開設定をOFFにして運用から外してください。

---

## 5. 管理者向け：師匠候補の承認

- URL：`/migration_assistant/admin_panel`（管理者以外は「管理者権限が必要です」）

「**師匠候補の管理**」画面で、ユーザーが「師匠」として指名できる候補者を承認・管理します。

### 5.1 一覧の表示項目

「承認済み師匠候補一覧」に、ユーザー（氏名＋ID）／承認者／有効期間／備考／状態／操作が表示されます。有効期間は開始が未設定なら「即時」、終了が未設定なら「無期限」と表示され、状態は現在時刻が有効期間内なら緑の「有効」、外なら赤の「無効」になります。

### 5.2 新規承認

**「＋ 新規承認」** を押して次を入力し、「承認する」で確定します。

| 項目 | 必須 | 備考 |
| --- | --- | --- |
| ユーザーを選択 | **必須** | 未選択だと「ユーザーを選択してください」 |
| 有効開始日時 | 任意 | 未入力なら即時有効 |
| 有効終了日時 | 任意 | 未入力なら無期限 |
| 備考 | 任意 | 承認理由や注意事項 |

同じユーザーを重複して承認しようとすると「このユーザーは既に師匠候補として登録されています」となります。

### 5.3 取消

一覧の **「取消」** ボタンを押すと、確認ダイアログが表示されます。

> この師匠承認を取り消しますか？
> （既に指名されている弟子との関係も解除されます）

OKすると承認記録が削除され、「師匠承認を取り消しました」と表示されます。

> **注意：** 現在の実装では、この承認記録は教材作成・編集の可否を制御していません（第1章「師匠になるための手続き」参照）。また、取消しても既存の受講関係（`course_enrollments`）はそのまま残ります。ダイアログの文言と実際の挙動は一致していないため、運用記録の管理として使ってください。

---

## 6. うまく動かないとき

| 症状 | 確認すること |
| --- | --- |
| 「ログインが必要です」と表示される | FUJIN-P にログインし直してください |
| 「⚙️ 管理者パネル」が出てこない | 管理者（category=admin）のみ表示されます |
| 「読み込みエラーが発生しました」（左パネル） | ログイン状態とDB接続を確認してください |
| 教材の編集画面が開かない | 自分が作成した教材か確認してください（他人の教材は管理者のみ編集可） |
| 「📖 教材一括」で何も開かない | ブラウザのポップアップブロックを解除してください |
| 教材を削除できない（ボタンが無い） | 受講者がいる教材は削除できません |
| 編集した本文が消えた | 自動保存はされません。「💾 保存」を押したか確認してください |
| 固有URLを開いたのに教材が選択されない | そのコースを自分が受講しているか確認してください |
| 同じ教材のカードが2枚ある | 2026-07-25 の改修前に二重受講したデータです。片方を「×」で取り消せますが**進捗も消えます**。惜しい場合は片方だけを使ってください（進捗は教材単位で記録されるため、どちらのカードでも同じ進捗が見えます） |

一般的なエラーは共通のエラーページ（「🚧 開発中」の画面）にメッセージ付きで表示されます。この画面の「ページを閉じる」ボタンはタブを閉じる（閉じられない場合は1つ前に戻る）動作です。

---

## 付録A. URL一覧

### 画面

| 画面 | URL |
| --- | --- |
| メインページ | `/migration_assistant/` , `/migration_assistant/index` |
| 教材の固有URL（学習者） | `/migration_assistant/course/<course_id>` |
| 師匠ダッシュボード | `/migration_assistant/mentor_dashboard` |
| 教材コンテンツ編集 | `/migration_assistant/mentor_content_editor/<course_id>` |
| 管理者パネル | `/migration_assistant/admin_panel` |
| FUJIN-Pダッシュボードへ戻る | `/migration_assistant/return_to_fujin` |

### 弟子API（画面が内部で使用）

| 用途 | メソッドとパス |
| --- | --- |
| 受講中教材の一覧 | `GET /migration_assistant/api/systems` |
| 公開教材の一覧 | `GET /migration_assistant/api/v2/courses/public` |
| 受講申し込み | `POST /migration_assistant/api/v2/enrollments/enroll` |
| 受講の取り消し | `DELETE /migration_assistant/api/v2/enrollments/<enrollment_id>` |
| 教材の構造取得 | `GET /migration_assistant/api/student/content?system_id=<enrollment_id>` |
| ステップ本文取得 | `GET /migration_assistant/api/student/step_detail/<phase_id>/<stage_id>/<step_id>?system_id=<enrollment_id>` |
| 進捗の取得 | `GET /migration_assistant/api/student/progress?system_id=<enrollment_id>` |
| 進捗の保存 | `POST /migration_assistant/api/student/progress` |

### 師匠API（画面が内部で使用）

| 用途 | メソッドとパス |
| --- | --- |
| マイ教材一覧 | `GET /migration_assistant/api/v2/mentor/my_courses` |
| 教材の作成 | `POST /migration_assistant/api/v2/courses/create` |
| 教材設定の更新 | `PUT /migration_assistant/api/v2/courses/<course_id>` |
| 教材の削除 | `DELETE /migration_assistant/api/v2/courses/<course_id>` |
| 受講者一覧 | `GET /migration_assistant/api/v2/courses/<course_id>/students` |
| 受講者の進捗詳細 | `GET /migration_assistant/api/mentor/student/<student_id>/system/<enrollment_id>` |
| 教材構造の取得 | `GET /migration_assistant/api/v2/courses/<course_id>/content` |
| 教材の一括結合 | `GET /migration_assistant/api/v2/courses/<course_id>/aggregate` |
| フェーズ | `POST .../api/v2/mentor/content/phase` , `/phase/add` , `/phase/delete` |
| ステージ | `POST .../api/v2/mentor/content/stage` , `/stage/add` , `/stage/delete` |
| ステップ | `POST .../api/v2/mentor/content/step` , `/step/add` , `/step/delete` |
| ステップ本文（生Markdown）取得 | `GET /migration_assistant/api/v2/mentor/content/step/detail` |
| Markdownプレビュー | `POST /migration_assistant/api/preview` |
| 画像アップロード | `POST /migration_assistant/api/upload_image` |

### 管理者API

| 用途 | メソッドとパス |
| --- | --- |
| 師匠候補一覧 | `GET /migration_assistant/api/admin/mentors` |
| 師匠候補の承認 | `POST /migration_assistant/api/admin/mentors` |
| 師匠承認の取消 | `DELETE /migration_assistant/api/admin/mentors/<mentor_id>` |
| 管理者判定 | `GET /migration_assistant/api/check_admin` |

> 前版に記載のあった `POST /api/v2/enrollments/<enrollment_id>/toggle_share`（進捗共有の切替）は**現在の実装には存在しません**。

---

## 付録B. 運用のおすすめ

教材を作る側では、ステップを小さく具体的に区切ると弟子が詰まりにくくなります。DB操作や不可逆な移行手順といった注意事項は、ステップ本文の冒頭に明記してください。公開設定は既定でONなので、書きかけの教材は非公開にしてから作り込むのが安全です。書き上げたら「📖 教材一括」で通し読みし、「*（本文未記入）*」が残っていないかを点検するとよいでしょう。

学ぶ側では、受講した時点で進捗が著者に見えることを理解しておいてください。行き詰まったときは「苦戦」を選んでおくと、師匠が状況に気づける手がかりになります。受講の取り消しは進捗データも消えるため、いったん止めたいだけなら「放棄」を選んで残しておく方が安全です。

管理者は、師匠候補の承認が現状では権限を制御していないことを踏まえ、承認記録を「誰を師匠として認定したか」の台帳として運用してください。

---

# 旧技術仕様書（2026-07-29 時点）

## blueprint_name

migration_assistant

## config_notes

# config.py — UPLOAD_FOLDER が必須（既存の FUJIN-P 設定を流用）
#   画像アップロードの保存先ディレクトリ。既存アプリと共用でよい
UPLOAD_FOLDER = '/home/<user>/static/mdimgs'
#   ※ 公開URLは /static/mdimgs/<filename> 固定（routes.py 内でハードコード）。
#      UPLOAD_FOLDER の実体を /static/mdimgs に対応させておくこと

# db.py — 既存のものをそのまま使用（追記不要）
#   DatabaseConfig.default() … mysql.connector.connect(**config) に展開できる dict を返す
#   Tables.USERS            … users テーブル名の定数

# auth.py — 既存のものをそのまま使用（追記不要）
#   redirect_to_dashboard() … FUJIN-P ダッシュボードへのリダイレクト

# markdown_converter.py — 既存のものをそのまま使用（追記不要）
#   process_markdown(text) … 学習者向けステップ本文の Markdown→HTML 変換

# アプリ内の定数（routes.py 冒頭。config.py への追記は不要）
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
MAX_IMAGE_BYTES    = 10 * 1024 * 1024                        # 画像アップロードの上限 10MB
PROGRESS_STATUSES  = ('未着手','取り組み中','苦戦','完了','放棄')  # course_progress.status の許可値
JST                = timezone(timedelta(hours=9), 'JST')      # DBがUTCのため明示的に使用

# セッション
#   session['user_id'] を FUJIN-P のログイン処理が設定していること（本アプリでは設定しない）
#   app.secret_key が設定済みであること

# 追加の環境変数・APIキーは不要

## description

FUJIN-P 機能モジュール（スタブ）の移行・構築プロセスを教材（コース）として提供し、師弟制度と進捗管理により学習者の作業を段階的に支援する Web アプリケーション。

## directory_structure

migration_assistant/
├── __init__.py                                   Blueprint 定義
├── app_info.json                                 本技術情報ファイル
├── migration_assistant_routes.py                 全ルート・全ロジック（2,187行）
└── migration_assistant_templates/
    ├── migration_assistant_index.html            メイン画面（学習者）
    ├── OLDmigration_assistant_index.html         旧版バックアップ（動作には不要）
    ├── migration_assistant_mentor_dashboard.html 師匠ダッシュボード
    ├── migration_assistant_mentor_content_editor.html  教材コンテンツ編集
    ├── migration_assistant_admin_panel.html      管理者パネル（師匠承認）
    ├── migration_assistant_admin_migration.html  旧→新テーブル転写（無効化済み）
    └── error.html                                共通エラー画面

※ テンプレートのディレクトリ名は templates ではなく migration_assistant_templates
   （__init__.py の template_folder で指定）。
※ schema.sql はパッケージに含まれない。DB構築時は本書 mysql_schema の内容を
   schema.sql として保存して実行する。
※ __init__.py は static_folder='static' を宣言しているため、画像等をアプリ内に置く場合は
   migration_assistant/static/ を作成する。現状は使用しておらず、
   アップロード画像は Config.UPLOAD_FOLDER（公開URL /static/mdimgs/）に保存する。

## display_name

まいあし（MaiAshi）

## endpoints

■ 画面（HTML を返す）
GET    /                                          - メインページ（学習者）※ /index も同一
GET    /index                                     - 同上
GET    /course/<course_id>                        - 教材の固有URL。本人の受講を引き当て、選択状態で開く
GET    /mentor_dashboard                          - 師匠ダッシュボード
GET    /mentor_content_editor/<course_id>         - 教材コンテンツ編集（作成者本人または管理者）
GET    /admin_panel                               - 管理者パネル（管理者のみ）
GET    /return_to_fujin                           - FUJIN-P ダッシュボードへ戻る（リダイレクト）
GET    /admin_migrationNG                         - 旧データ転写画面（管理者のみ。実質封印）

■ 共通・ユーティリティ API
GET    /api/check_admin                           - 管理者判定 → {is_admin}
POST   /api/preview                               - Markdown プレビュー {markdown} → {html}
POST   /api/upload_image                          - 画像アップロード（multipart file）→ {filename, url}

■ 学習者 API
GET    /api/systems                               - 受講中教材の一覧（進捗集計つき）
GET    /api/v2/courses/public                     - 公開教材の一覧
POST   /api/v2/enrollments/enroll                 - 受講申し込み {course_id}
DELETE /api/v2/enrollments/<enrollment_id>        - 受講の取り消し（進捗データも削除）
GET    /api/student/content?system_id=            - 教材の構造（Phase/Stage/Step）を取得
GET    /api/student/step_detail/<phase_id>/<stage_id>/<step_id>?system_id=  - ステップ本文（HTML変換済み）
GET    /api/student/progress?system_id=           - 進捗の取得
POST   /api/student/progress                      - 進捗の保存（UPSERT）

■ 師匠 API
GET    /api/v2/mentor/my_courses                  - 自分が作成した教材の一覧
POST   /api/v2/courses/create                     - 教材の新規作成 {course_title, course_description, is_public}
PUT    /api/v2/courses/<course_id>                - 教材設定の更新（タイトル・説明・公開）
DELETE /api/v2/courses/<course_id>                - 教材の削除（受講者0名のときのみ）
GET    /api/v2/courses/<course_id>/content        - 教材の構造を取得（エディタ・ダッシュボード用）
GET    /api/v2/courses/<course_id>/students       - 受講者一覧と進捗
GET    /api/v2/courses/<course_id>/progress_by_step - ステップ別の進捗分布
GET    /api/v2/courses/<course_id>/aggregate      - 全ステップ本文を結合 → {title, markdown, html}
GET    /api/mentor/students                       - 担当する弟子の一覧（※フロントからは未使用）
GET    /api/mentor/student/<student_id>/system/<enrollment_id> - 特定の弟子の進捗マップ
GET    /api/v2/courses/<course_id>/students/<student_id>/progress - 弟子の進捗詳細（※フロントからは未使用）

■ 師匠 API（コンテンツ編集）
GET    /api/v2/mentor/content/step/detail         - ステップ本文（生Markdown）を取得
POST   /api/v2/mentor/content/phase               - Phase のタイトル・説明を更新
POST   /api/v2/mentor/content/phase/add           - Phase を追加（position: first|after, after_number）
POST   /api/v2/mentor/content/phase/delete        - Phase を削除（配下の Stage / Step も削除）
POST   /api/v2/mentor/content/stage               - Stage のタイトルを更新
POST   /api/v2/mentor/content/stage/add           - Stage を追加
POST   /api/v2/mentor/content/stage/delete        - Stage を削除（配下の Step も削除）
POST   /api/v2/mentor/content/step                - Step のタイトル・本文を更新
POST   /api/v2/mentor/content/step/add            - Step を追加
POST   /api/v2/mentor/content/step/delete         - Step を削除

■ 管理者 API
GET    /api/admin/mentors                         - 師匠候補の一覧
POST   /api/admin/mentors                         - 師匠候補の承認 {user_id, valid_from, valid_until, notes}
DELETE /api/admin/mentors/<mentor_id>             - 師匠承認の取消
POST   /api/admin/migrate_to_coursesNG            - 旧データ転写（実質封印）

■ 認可の実現方式
require_course_editor(course_id)     … content/ 配下10件 ＋ courses/<id>/content ＋ progress_by_step
require_enrollment_owner(system_id)  … student/step_detail、student/progress（GET・POST）
関数先頭の session['user_id'] 確認   … 画面、管理者API、preview、upload_image、courses/public、create_course
SQL の WHERE 句で本人／師匠／作成者に限定 … systems、my_courses、courses/<id>/students、
                                             delete_course、update_course_settings_api ほか

■ 主なレスポンス
成功 : {"success": true, ...}
失敗 : {"success": false, "error": "..."} ＋ HTTP 400 / 401 / 403 / 404 / 500
       401 Unauthorized / 403 権限なし / 404 対象なし / 400 入力不備
enroll のエラー文字列は index.html の日本語化テーブルに対応させている
       Unauthorized / Course not found / Already enrolled

■ アプリ外への依存（注意）
GET /user_groups/api/users/candidates  - 管理者パネルのユーザー選択が使用。
                                         本アプリではなく user_groups Blueprint 側のエンドポイント

## libraries

■ サードパーティ
Flask                    - Webフレームワーク（render_template, jsonify, request, session）
mysql-connector-python    - MySQL接続（mysql.connector）
Markdown                 - Markdown→HTML変換（プレビューと教材一括結合で使用。
                           拡張: extra, nl2br, sane_lists, fenced_code, tables）
Werkzeug                 - secure_filename（画像アップロードのファイル名サニタイズ）

■ FUJIN-P 共通モジュール（同梱不要。既存のものを使用）
config.Config                        - UPLOAD_FOLDER
db.DatabaseConfig                    - DatabaseConfig.default() で接続情報を取得
db.Tables                            - Tables.USERS（users テーブル名の定数）
auth.redirect_to_dashboard           - FUJIN-P ダッシュボードへの復帰
markdown_converter.process_markdown  - 学習者向けステップ本文の Markdown→HTML 変換

■ 標準ライブラリ
datetime  - JST（timezone(timedelta(hours=9))）での日時生成
os        - パス操作、ディレクトリ作成、ファイルサイズ判定（SEEK_END）
logging   - エラー・認可拒否の記録
html      - Markdown 変換失敗時のエスケープ
traceback - 例外のスタックトレース出力

■ フロントエンド（CDN）
marked.js       - index.html が読み込むが実質未使用
KaTeX 0.16.9    - 数式表示（index / mentor_content_editor）

■ 注意
mysql.connector は DatabaseConfig.default() を展開してリクエストごとに接続する
（コネクションプールは使用していない）。

## migration_guide

1. DB構築
   - schema.sql をMySQLで実行（7テーブル。CREATE TABLE IF NOT EXISTS）
   - 対象DB: user_account$default
   - 前提: FUJIN-P 共通の users テーブルが存在すること
   - 既存環境に course_enrollments がある場合は、schema.sql 末尾のコメント手順で
     重複行を整理してから UNIQUE KEY unique_student_course を追加する

2. モジュール配置
   - migration_assistant/ ディレクトリを配置
   - migration_assistant_templates/OLDmigration_assistant_index.html は動作に不要
     （旧版バックアップ。削除して差し支えない）

3. app.py への追記
   from migration_assistant import migration_assistant
   app.register_blueprint(migration_assistant)

   ※ Blueprint 変数名は migration_assistant（末尾に _bp は付かない）
   ※ url_prefix='/migration_assistant' は __init__.py 側で宣言済みのため、
      register_blueprint() で指定しない

4. config.py の確認
   - Config.UPLOAD_FOLDER が設定済みであること
   - その実体が公開URL /static/mdimgs/ に対応していること
     （画像URLは routes.py 内で /static/mdimgs/<filename> 固定）

5. ダッシュボードへの追記
   - admin_dashboard.html と guest_dashboard.html に起動リンクを追加
     <a href="{{ url_for('migration_assistant.index') }}" class="app-card">
       <div>👣 まいあし</div>
     </a>

6. Webアプリをリロード

7. 動作確認
   - /migration_assistant/ にアクセスして画面が出ること
   - 師匠ダッシュボードで「📚 新規教材」→ 作成 → 教材編集画面へ自動遷移すること
   - 教材編集画面で Step を追加し、本文を書いて「💾 保存」できること
   - 別ユーザーでログインし、公開教材を受講してステップの進捗を記録できること
   - 同じ教材をもう一度受講しようとして
     「現在受講中科目の重複履修はできません」が出ること
   - 管理者パネル（/migration_assistant/admin_panel）で師匠候補一覧が表示されること
     ※ ユーザー選択には user_groups Blueprint の
        /user_groups/api/users/candidates が必要

8. 任意の後片付け
   - 旧システムのテーブル（migration_assistant_phase_contents ほか5テーブル）は
     現行コードから参照されないため、移行完了後に削除可
   - /admin_migrationNG と migration_assistant_admin_migration.html も同様
     （ルート名の NG サフィックスにより到達不能。転写用の一時機能）

## mysql_schema

-- =============================================================================
-- まいあし（migration_assistant）schema.sql
-- 対象DB: user_account$default
-- 前提: FUJIN-P 共通の users テーブルが存在すること（本ファイルでは作成しない）
-- 文字セット: 全テーブル utf8mb4 / utf8mb4_unicode_ci
--   ※ 本番の course_enrollments は utf8mb3 で作られているが、
--      新規構築では utf8mb4 に揃える（進捗ステータスの日本語ENUMとの整合のため）
-- 外部キー制約は設けていない（既存実装に合わせる。参照整合はアプリ側で担保）
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 教材（コース）本体
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS courses (
    id                 INT AUTO_INCREMENT PRIMARY KEY,
    creator_user_id    INT NOT NULL                COMMENT '作成者（師匠）のユーザーID (users.id)',
    course_title       VARCHAR(255) NOT NULL       COMMENT '教材タイトル',
    course_description TEXT                        COMMENT '教材の説明（概要）',
    is_public          TINYINT(1) DEFAULT 1        COMMENT '公開フラグ。1=公開（既定）',
    created_at         TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at         TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_creator (creator_user_id),
    KEY idx_public  (is_public)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='まいあし：教材（コース）';

-- -----------------------------------------------------------------------------
-- 受講登録（学習者と教材と師匠の関係）
--   UNIQUE(student_user_id, course_id) が重複受講を防ぐ。
--   既存DBに後から追加する場合は add_unique_enrollment.sql の手順で
--   重複行を整理してから ALTER TABLE すること。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS course_enrollments (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    student_user_id INT NOT NULL                COMMENT '弟子（学習者）のユーザーID (users.id)',
    course_id       INT NOT NULL                COMMENT '受講する教材ID (courses.id)',
    mentor_user_id  INT NOT NULL                COMMENT '師匠のユーザーID。受講時に courses.creator_user_id を複製',
    enrolled_at     TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP COMMENT '受講開始日時（アプリがJSTで明示的に書き込む）',
    UNIQUE KEY unique_student_course (student_user_id, course_id),
    KEY idx_student     (student_user_id),
    KEY idx_mentor_user (mentor_user_id),
    KEY idx_course      (course_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='まいあし：受講登録';

-- -----------------------------------------------------------------------------
-- 教材コンテンツ：Phase（第1階層）
--   phase_id は int ではなく 'p_<ミリ秒エポック>' 形式の文字列。
--   表示順は phase_number（挿入・削除のたびに1起点で振り直す）。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS course_phase_contents (
    id                INT AUTO_INCREMENT PRIMARY KEY,
    course_id         INT NOT NULL              COMMENT '所属教材ID (courses.id)',
    phase_id          VARCHAR(100) NOT NULL     COMMENT 'Phase識別子 p_<ミリ秒エポック>',
    phase_number      INT NOT NULL              COMMENT '表示順（1起点の連番）',
    phase_title       VARCHAR(255) NOT NULL     COMMENT 'Phaseタイトル',
    phase_description TEXT                      COMMENT 'Phaseの説明',
    created_at        TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY unique_course_phase (course_id, phase_id),
    KEY course_id (course_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='まいあし：教材コンテンツ Phase';

-- -----------------------------------------------------------------------------
-- 教材コンテンツ：Stage（第2階層）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS course_stage_contents (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    course_id    INT NOT NULL                   COMMENT '所属教材ID (courses.id)',
    phase_id     VARCHAR(100) NOT NULL          COMMENT '親PhaseのID',
    stage_id     VARCHAR(100) NOT NULL          COMMENT 'Stage識別子 s_<ミリ秒エポック>',
    stage_number INT NOT NULL                   COMMENT 'Phase内の表示順（1起点の連番）',
    stage_title  VARCHAR(255) NOT NULL          COMMENT 'Stageタイトル',
    created_at   TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY unique_course_stage (course_id, phase_id, stage_id),
    KEY course_id (course_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='まいあし：教材コンテンツ Stage';

-- -----------------------------------------------------------------------------
-- 教材コンテンツ：Step（第3階層。進捗記録の最小単位）
--   step_detail に Markdown の本文を保持する。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS course_step_contents (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    course_id   INT NOT NULL                    COMMENT '所属教材ID (courses.id)',
    phase_id    VARCHAR(100) NOT NULL           COMMENT '親PhaseのID',
    stage_id    VARCHAR(100) NOT NULL           COMMENT '親StageのID',
    step_id     VARCHAR(100) NOT NULL           COMMENT 'Step識別子 st_<ミリ秒エポック>',
    step_number INT NOT NULL                    COMMENT 'Stage内の表示順（1起点の連番）',
    step_title  VARCHAR(255) NOT NULL           COMMENT 'Stepタイトル',
    step_detail TEXT                            COMMENT 'ステップ本文（Markdown。生のまま保存）',
    created_at  TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY unique_course_step (course_id, phase_id, stage_id, step_id),
    KEY course_id (course_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='まいあし：教材コンテンツ Step';

-- -----------------------------------------------------------------------------
-- 学習者のステップ別進捗
--   UNIQUE(unique_progress) により ON DUPLICATE KEY UPDATE で UPSERT する。
--   student_user_id × course_id で管理するため、同一教材の受講が複数あっても
--   進捗は共有され二重化しない。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS course_progress (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    student_user_id INT NOT NULL                COMMENT '弟子のユーザーID (users.id)',
    course_id       INT NOT NULL                COMMENT '対象教材ID (courses.id)',
    phase_id        VARCHAR(100) DEFAULT NULL   COMMENT '対象PhaseのID',
    stage_id        VARCHAR(100) DEFAULT NULL   COMMENT '対象StageのID',
    step_id         VARCHAR(100) DEFAULT NULL   COMMENT '対象StepのID',
    status          ENUM('未着手','取り組み中','苦戦','完了','放棄')
                        DEFAULT '未着手'         COMMENT '進捗ステータス',
    updated_at      TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                        COMMENT '最終更新日時（アプリがJSTで明示的に書き込む）',
    UNIQUE KEY unique_progress (student_user_id, course_id, phase_id, stage_id, step_id),
    KEY idx_course_step (course_id, step_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='まいあし：学習者のステップ別進捗';

-- -----------------------------------------------------------------------------
-- 師匠候補の承認記録
--   認可判定には使われない（記録台帳）。
--   教材編集画面を初めて開いた時点で、作成者を師匠として自動登録する
--   （INSERT ... ON DUPLICATE KEY UPDATE のため unique_user_mentor が必須）。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS migration_assistant_mentors (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    user_id     INT NOT NULL                    COMMENT '師匠になれるユーザーID (users.id)',
    approved_by INT NOT NULL                    COMMENT '承認したadminのユーザーID (users.id)',
    valid_from  DATETIME DEFAULT NULL           COMMENT '有効開始日時（任意。NULLは即時有効）',
    valid_until DATETIME DEFAULT NULL           COMMENT '有効終了日時（任意。NULLは無期限）',
    notes       TEXT                            COMMENT '備考（任意）',
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY unique_user_mentor (user_id),
    KEY idx_valid_period (valid_from, valid_until)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='まいあし：師匠候補の管理（adminが承認）';

-- =============================================================================
-- 既存DBに一意制約を後から追加する場合（重複行を整理してから実行）
-- =============================================================================
-- 1) 重複の確認
-- SELECT student_user_id, course_id, COUNT(*) AS dup_count, MIN(id) AS keep_id
-- FROM course_enrollments GROUP BY student_user_id, course_id HAVING COUNT(*) > 1;
--
-- 2) 重複行の削除（同一組につき最小の id を残す）
-- DELETE e FROM course_enrollments e
-- JOIN (SELECT student_user_id, course_id, MIN(id) AS keep_id
--       FROM course_enrollments GROUP BY student_user_id, course_id) k
--   ON e.student_user_id = k.student_user_id AND e.course_id = k.course_id
-- WHERE e.id <> k.keep_id;
--
-- 3) 一意制約とインデックスの追加
-- ALTER TABLE course_enrollments
--     ADD UNIQUE KEY unique_student_course (student_user_id, course_id),
--     ADD KEY idx_mentor_user (mentor_user_id),
--     ADD KEY idx_course (course_id);

-- =============================================================================
-- 旧システムのテーブル（本アプリの現行コードからは参照されない）
--   migration_assistant_phase_contents / _stage_contents / _step_contents
--   migration_assistant_progress / migration_assistant_mentor_assignments
--   新規構築では作成不要。既存環境では移行完了後に削除して差し支えない。
-- =============================================================================

## overview

## 概要
FUJIN-P の機能モジュール（スタブ）を段階的に構築するプロセスを「教材（コース）」として提供し、
学習者がステップを一つずつ進めながら移行・構築作業を進められるよう支援する Web アプリケーション。
教材を作る人（師匠）と教材で学ぶ人（弟子）という師弟制度を軸に構成されている。

## 主な機能
- 教材の3階層構造（Phase / Stage / Step）による学習コンテンツの作成・編集
- ステップ単位の進捗記録（未着手 / 取り組み中 / 苦戦 / 完了 / 放棄 の5段階）
- 公開教材の一覧と受講申し込み（受講すると教材の作成者が自動的に師匠になる）
- 教材ごとの固有URL（/migration_assistant/course/<course_id>）によるリンク共有・ブックマーク
  - 通常クリックは history.pushState でURLのみ更新（SPA挙動）、Ctrl/⌘+クリックで別タブ
  - ブラウザの戻る／進むに追従（popstate で選択状態を復元）
- Markdown による教材本文の記述、サーバーサイドプレビュー、画像アップロード（10MB上限）
- KaTeX による数式表示（$...$ / $$...$$）
- 師匠ダッシュボードでの受講者一覧・進捗率・ステップ別進捗の確認
- 教材一括結合（全 Phase/Stage/Step の本文を1本の Markdown / HTML にまとめて別タブ表示、印刷・PDF化）
- 管理者による「師匠候補」の承認・取消（運用記録）

## 進捗共有の設計
進捗共有のオプトイン（ON/OFF選択）は存在しない。受講した時点で教材の作成者が
mentor_user_id として記録され、進捗は常に師匠から見える状態になる。
共有を止める手段は受講の取り消し（進捗データも削除される）のみ。

## 権限モデル
- 画面・APIとも session['user_id'] を必須とする（/return_to_fujin を除く）
- 管理者判定は users.category == 'admin'
- 教材の作成はログインのみで可能（管理者による師匠承認は不要）
- 教材の編集は「作成者本人または管理者」。共通ヘルパー require_course_editor() で判定
- 学習者APIは「受講が本人のものか」を共通ヘルパー require_enrollment_owner() で判定
- migration_assistant_mentors（師匠承認）は認可判定には使われず、記録台帳として機能する
  （教材編集画面を初めて開いた時点で作成者を師匠として自動登録する）

## タイムゾーン方針
DBサーバがUTCのため、NOW() や DEFAULT CURRENT_TIMESTAMP に依存せず、
アプリ側で JST（UTC+9）を生成して naive datetime として明示的に書き込む。

    JST = timezone(timedelta(hours=9), 'JST')
    now_jst = datetime.now(JST).replace(tzinfo=None)

## 改修履歴
- 2026-07-12 教材ごとの固有URL（/course/<course_id>）を追加
- 2026-07-25 認可チェックを計19エンドポイントに追加
             ・require_course_editor      12件（content/ 配下10件 ＋ courses/<id>/content ＋ progress_by_step）
             ・require_enrollment_owner    3件（student/step_detail、student/progress の GET・POST）
             ・関数先頭のログイン確認       4件（enroll、courses/public、preview、upload_image）
             あわせて次を実施（すべて稼働環境に反映済み）
             ・重複受講の防止：アプリ側の事前チェック ＋ DB側 UNIQUE(student_user_id, course_id)
             ・画像アップロードのサーバー側サイズ上限（MAX_IMAGE_BYTES = 10MB）
             ・進捗ステータスの入力値検証（PROGRESS_STATUSES との照合）
             ・DB接続のクローズ漏れ5件の修正
             ・コンテンツ編集画面の案内文「変更は自動的に保存されます。」を
               「自動保存は行われません。」に訂正
               （自動保存は未接続の死んだコードで動作せず、案内文を信じて保存せずに
                 閉じると編集内容が失われるため）

## 稼働環境との軽微な差分（実害なし・対応は任意）
- 文字セット：本書 mysql_schema は course_enrollments を utf8mb4 としているが、
  稼働DBは utf8mb3 のまま。同テーブルは全カラムが int / timestamp で
  文字列カラムを持たないため実害はない。新規構築時のみ utf8mb4 で作られる。
- インデックス：本書 mysql_schema の idx_mentor_user / idx_course は稼働DBに未追加。
  師匠ダッシュボードの mentor_user_id 検索と、教材固有URL（/course/<course_id>）の
  course_id 検索がフルスキャンになる。件数が小さいうちは問題にならない。
- 死んだコード：migration_assistant_mentor_content_editor.html の自動保存関数
  （startAutoSave / stopAutoSave / markAsUnsaved / debouncedSave）は未削除。
  どこからも呼ばれないため動作に影響はないが、呼び出すと未定義変数で失敗する。

## python_files

__init__.py                        - Blueprint 定義（url_prefix, template_folder, static_folder）
migration_assistant_routes.py      - ルート定義（全ロジック。44ルート / 43エンドポイント関数 / 2,187行）

※ モデル層・サービス層の分離はなく、各ビュー関数が直接 SQL を実行する。
   認可判定のみ共通ヘルパー2つに切り出してある。
     require_course_editor(course_id)        … 作成者本人または管理者か（12エンドポイントで使用）
     require_enrollment_owner(enrollment_id) … 受講が本人のものか（3エンドポイントで使用。course_id を返す）

※ OLDmigration_assistant_routes.py（固有URL機能追加前のバックアップ）は削除済み。

## sql_tables_description

## courses（教材＝コース）
- id: 主キー（自動採番）
- creator_user_id: 作成者（師匠）のユーザーID（users.id）。編集権限の判定に使う
- course_title: 教材タイトル（必須）
- course_description: 教材の説明（概要）。任意
- is_public: 公開フラグ。1=公開（既定）。公開教材一覧は is_public=1 のみを返す
- created_at: 作成日時（アプリがJSTで書き込む）
- updated_at: 更新日時。教材設定の更新時にアプリがJSTで書き込む。公開一覧の並び順に使う

## course_enrollments（受講登録）
- id: 主キー（自動採番）。API上の system_id / enrollment_id はこの値
- student_user_id: 弟子（学習者）のユーザーID（users.id）
- course_id: 受講する教材ID（courses.id）
- mentor_user_id: 師匠のユーザーID。受講時に courses.creator_user_id を複製する。
                  学習者が師匠を選ぶ手段はなく、NULL にはならない
- enrolled_at: 受講開始日時（アプリがJSTで書き込む）
- UNIQUE(student_user_id, course_id): 同一ユーザーの同一教材への重複受講を防ぐ

## course_phase_contents（教材コンテンツ Phase）
- id: 主キー（自動採番）
- course_id: 所属教材ID（courses.id）
- phase_id: Phase識別子。'p_<ミリ秒エポック>' 形式の文字列（int ではない）
- phase_number: 表示順。1起点の連番。追加・削除のたびに全件を振り直す
- phase_title: Phaseタイトル。新規追加時の既定値は「新しいPhase」
- phase_description: Phaseの説明
- created_at / updated_at: 作成・更新日時
- UNIQUE(course_id, phase_id): 教材内での Phase 識別子の一意性

## course_stage_contents（教材コンテンツ Stage）
- id: 主キー（自動採番）
- course_id: 所属教材ID（courses.id）
- phase_id: 親Phaseの phase_id
- stage_id: Stage識別子。's_<ミリ秒エポック>'
- stage_number: 同一Phase内の表示順。1起点の連番
- stage_title: Stageタイトル。新規追加時の既定値は「新しいStage」
- created_at / updated_at: 作成・更新日時
- UNIQUE(course_id, phase_id, stage_id)

## course_step_contents（教材コンテンツ Step）
- id: 主キー（自動採番）
- course_id: 所属教材ID（courses.id）
- phase_id: 親Phaseの phase_id
- stage_id: 親Stageの stage_id
- step_id: Step識別子。'st_<ミリ秒エポック>'
- step_number: 同一Stage内の表示順。1起点の連番
- step_title: Stepタイトル。新規追加時の既定値は「新しいStep」
- step_detail: ステップ本文。Markdown を生のまま保存する。
               学習者向けAPIのみ process_markdown() でHTMLに変換して返し、
               師匠向けAPIは生Markdownを返す
- created_at / updated_at: 作成・更新日時
- UNIQUE(course_id, phase_id, stage_id, step_id)
- このテーブルの件数が「教材の全ステップ数」＝進捗率の分母になる

## course_progress（学習者のステップ別進捗）
- id: 主キー（自動採番）
- student_user_id: 弟子のユーザーID（users.id）
- course_id: 対象教材ID（courses.id）。受講ID（enrollment）ではなく教材単位で持つ
- phase_id / stage_id / step_id: 対象ステップを指す3つの識別子
- status: 進捗ステータス。ENUM('未着手','取り組み中','苦戦','完了','放棄')。既定 '未着手'
          アプリ側の定数 PROGRESS_STATUSES と一致させる
- updated_at: 最終更新日時（アプリがJSTで書き込む）。師匠画面の「最終活動日時」に使う
- UNIQUE(student_user_id, course_id, phase_id, stage_id, step_id):
  ON DUPLICATE KEY UPDATE による UPSERT の前提。
  レコードが無いステップは「未着手」として扱う（行を作らない）
- 進捗率は status='完了' の件数 ÷ 当該教材の course_step_contents 件数

## migration_assistant_mentors（師匠候補の承認記録）
- id: 主キー（自動採番）
- user_id: 師匠になれるユーザーID（users.id）
- approved_by: 承認したadminのユーザーID（users.id）。
               自動登録時は本人のIDが入る
- valid_from: 有効開始日時。NULL は「即時」として表示される
- valid_until: 有効終了日時。NULL は「無期限」として表示される
- notes: 備考。自動登録時は「Course ID N の作成により自動昇格」が入る
- created_at / updated_at: 作成・更新日時
- UNIQUE(user_id): 1ユーザー1レコード。教材編集画面を開いたときの
  INSERT ... ON DUPLICATE KEY UPDATE がこのキーに依存する
- 【重要】このテーブルは認可判定には使われない。教材の作成・編集は
  courses.creator_user_id と users.category='admin' のみで判定される

## 参照する FUJIN-P 共通テーブル（本アプリでは作成しない）
- users: id, full_name（画面表示名）, category（'admin' で管理者判定）。
         テーブル名はコード内で db.Tables.USERS 定数を参照する
- user_groups: 本アプリは直接参照しない。ただし管理者パネルのユーザー選択が
               /user_groups/api/users/candidates を呼ぶため、user_groups Blueprint に依存する

## 旧システムのテーブル（現行コードからは参照されない）
- migration_assistant_phase_contents / _stage_contents / _step_contents:
  旧・師匠ごとのコンテンツ（mentor_user_id 基準）。course_ 系に置き換わった
- migration_assistant_progress: 旧進捗。course_progress に置き換わった
- migration_assistant_mentor_assignments: 旧師弟関係（system_id 基準）。
  course_enrollments に置き換わった

## template_files

migration_assistant_index.html                - メイン画面（学習者）。受講中教材の一覧、Phase/Stage/Step、進捗ラジオボタン、公開教材一覧モーダル、ステップ詳細モーダル
migration_assistant_mentor_dashboard.html     - 師匠ダッシュボード。マイ教材一覧、新規作成、設定変更、教材一括、受講者一覧・進捗詳細
migration_assistant_mentor_content_editor.html - 教材コンテンツ編集。Phase/Stage/Step の追加・削除・タイトル編集、Markdown本文編集、プレビュー、画像アップロード
migration_assistant_admin_panel.html          - 管理者パネル。師匠候補の一覧・新規承認・取消
migration_assistant_admin_migration.html      - 旧→新テーブル転写画面。ルート名の NG サフィックスにより到達不能（封印済み）
error.html                                    - 共通エラー画面。変数 error を表示
OLDmigration_assistant_index.html             - 旧版バックアップ。動作には不要（削除可）

■ 外部CDN
marked.js（index。読み込んでいるが実質未使用。本文はサーバー側で変換済み）
KaTeX 0.16.9 + auto-render（index / mentor_content_editor。数式表示）
※ mentor_dashboard / admin_panel は外部CDNを使わず、CSS・JS ともインライン

## url

/migration_assistant/

## general_notes

# migration_assistant 技術情報
# このファイルはアプリの技術ドキュメントです。
# 各項目を編集し、保存ボタンで app_info.json に保存されます。
