-- ============================================================
-- fujin_forum（えふえふ）v1.0 → v1.1  添付テーブルの変更（v1.0 のテーブルを作った場合のみ）
-- 新規なら schema.sql だけでよい
-- ============================================================
ALTER TABLE fujin_forum_attachments
  MODIFY COLUMN post_id INT NULL COMMENT '本文が参照する記事（投稿前は NULL）',
  ADD COLUMN channel_id  INT NOT NULL DEFAULT 0 COMMENT 'アクセス権の判定に使うチャンネル' AFTER post_id,
  ADD COLUMN local_path  VARCHAR(600) NULL COMMENT '保護領域 data/files/ からの相対パス（原本）' AFTER size,
  ADD COLUMN public_path VARCHAR(600) NULL COMMENT '公開複製の URL パス．未公開は NULL' AFTER local_path,
  ADD COLUMN uploaded_by INT NULL COMMENT 'users.id（Slack 由来は NULL）' AFTER public_path,
  ADD COLUMN source ENUM('user','slack') NOT NULL DEFAULT 'user' AFTER uploaded_by,
  DROP COLUMN url_path,
  ADD KEY idx_channel (channel_id);
