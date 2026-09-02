# えふえふ（fujin_forum）v1.0 — 配置とアプシャ登録メモ  2026-08-27

## 配置
- `~/fujinp/fujin_forum/`（__init__.py・routes.py・templates/fujin_forum/*.html）
- `schema.sql` を MySQL コンソールで実行（6テーブル．既存テーブルには触らない）
- 添付の置き場 `~/static/ffimgs/` は初回アップロード時に自動生成

## アプシャの登録内容
- app_name: fujin_forum　表示名: えふえふ　アイコン: 💬　kind: app
- blueprints: module=fujinp.fujin_forum, attr=fujin_forum_bp, name=fujin_forum, url_prefix=（空．Blueprint 側で /fujin_forum）
- launchers: endpoint=fujin_forum.index，区画は「ゲスト向け」など，使用区分は運用に合わせて
  （ゲストにも／構成員だけ など．チャンネルごとの公開範囲は別にアプリ内で制御される）
- libraries: Flask / mysql-connector-python / pytz / Werkzeug（pip），
  config / db / decorators / auth / markdown_converter（local），
  fujinp.slack_minutes.mrkdwn（local．取込時のみ．無ければ生テキスト取込）
- config_keys: UPLOAD_BASE_DIR（任意．無ければホーム）
- tables: fujin_forum_channels / _access_groups / _posts / _reactions / _attachments / _reads

## 依存する他アプリ
- まいぐる：user_groups / user_group_memberships（公開範囲のグループ判定．無くても動く＝グループ区分が空になるだけ）
- すらくみ v2.0 以降：取込元（slack_minutes_messages / _files / _users / _channels と data/files/）
