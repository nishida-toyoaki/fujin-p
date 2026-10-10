# あわみ（awami）組み込み手順 — 3点セット

配置先：`/home/<owner>/fujinp/awami/`（このフォルダをそのままコピー）

## ① app.py への登録2行

`/home/<owner>/fujinp/app.py` に追記：

```python
# インポート部に追加
from fujinp.awami import our_meeting_bp

# Blueprint登録部に追加
app.register_blueprint(our_meeting_bp)
```

## ② ダッシュボードのランチャ行

`admin_dashboard.html` / `guest_dashboard.html` 等に追加：

```html
<a href="{{ url_for('our_meeting.index') }}" class="app-card feature-app">
    <div class="app-title"><span class="app-icon">🕸</span><h3>あわみ</h3></div>
    <p>ナラティブ素を結合子でつなぎ，語りを網として共有する</p>
</a>
```

## ③ 残りの組み込み作業

1. **schema.sql の適用**（新規導入時）．v0.1からの更新は migration_v0.2.sql → migration_v0.3.sql の順に適用 — `<owner>$default` データベースで実行．
   末尾の結合子初期語彙の INSERT は**一度だけ**実行すること
   （再実行すると語彙が重複する）．
2. **config.py への追記** — 不要（アプリ固有定数なし）．
3. **依存アプリの確認** — 「まいぐる（user_groups）」導入済みであること．
   `routes.py` の `get_effective_group_ids()` は，まいぐる公開の
   `get_user_effective_group_ids()` を
   `from fujinp.user_groups import ...` で import する．
   実環境での公開場所が異なる場合は，この関数内の import 行を
   実際のパスに合わせて1行修正する（失敗時は
   `user_group_memberships` 直接照会にフォールバックするため動作は継続する）．
4. **Webアプリのリロード** — PythonAnywhere の Web タブでリロード．

## 動作確認の手順（推奨）

1. 一覧画面 `/awami/` でキャンバスを作成（ACL未指定＝自分専用）
2. ダブルクリックでノードを2〜3個作成し，ドラッグで配置
3. 右クリック→「結合子を張る」で主を決め，従ノードをクリック選択→確定
4. ノードに実体URL（例：`/document_archive/plain/77`）を設定し，
   右クリック→「開く」で既存プレビュアーに飛ぶことを確認
5. 別ユーザ（キャンバスACLに合致）でログインし，ノードACLの
   有無によって表示が変わること，端点を失ったエッジが消えることを確認


## あわみ固有の注意

- schema.sql は最終形の1ファイルです（migrationは不要）。意見募集用の awami_polls / awami_poll_options / awami_votes / awami_writeins を含みます。
- app.py への追記：
  from fujinp.awami import our_meeting_bp
  app.register_blueprint(our_meeting_bp)
- 動作確認：キャンバスを開き「📢 意見募集」→問いと選択肢を入れて開始→司会コンソールから「URLを大きく表示」を投影し、別ユーザで参加者URLを開いて投票→stats表示が2秒ごとに更新されることを確認。
