-- ============================================================
-- slack_minutes v2.1  スキーマ変更（v2.0 適用後に MySQL コンソールで実行）
-- チャンネルごとの公開範囲を追加する
-- ============================================================

ALTER TABLE slack_minutes_channels
  ADD COLUMN visibility VARCHAR(16) NOT NULL DEFAULT 'admin'
    COMMENT '公開範囲：admin（adminのみ）/ members（ログイン済み全員．guest 含む）'
    AFTER is_private;

-- 既定値の初期化：パブリックチャンネルは members，プライベートは admin
UPDATE slack_minutes_channels
   SET visibility = IF(is_private = 1, 'admin', 'members');
