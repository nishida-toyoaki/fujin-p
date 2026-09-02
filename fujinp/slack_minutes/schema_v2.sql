-- ============================================================
-- slack_minutes v2.0  スキーマ変更（PythonAnywhere の MySQL コンソールに貼る）
-- 既存データは保持される．v1.x → v2.0 のアップグレード用．
-- ============================================================

-- 1) messages：完全アーカイブ用の列を追加
ALTER TABLE slack_minutes_messages
  ADD COLUMN subtype        VARCHAR(32)  NULL     COMMENT 'Slack subtype（thread_broadcast 等）' AFTER thread_ts,
  ADD COLUMN reply_count    INT NOT NULL DEFAULT 0 COMMENT 'Slack が返す返信数（親のみ）' AFTER subtype,
  ADD COLUMN edited_at      DATETIME     NULL     COMMENT '最終編集日時（JST）' AFTER reply_count,
  ADD COLUMN reactions_json TEXT         NULL     COMMENT 'リアクション [{name,count,users:[表示名]}]' AFTER edited_at,
  ADD COLUMN raw_json       MEDIUMTEXT   NULL     COMMENT 'Slack API が返したメッセージの生データ' AFTER reactions_json,
  ADD COLUMN updated_at     DATETIME     NULL     COMMENT '最終同期日時（JST）' AFTER created_at;

ALTER TABLE slack_minutes_messages
  ADD KEY idx_thread (channel_id, thread_ts);

-- 2) sessions：取得モードと区切り実行の状態
ALTER TABLE slack_minutes_sessions
  ADD COLUMN mode          VARCHAR(16) NOT NULL DEFAULT 'diff' COMMENT 'diff（差分）/ archive（完全アーカイブ）' AFTER status,
  ADD COLUMN phase         VARCHAR(16) NULL COMMENT 'archive の進行段階 history/threads/files/done' AFTER mode,
  ADD COLUMN state_json    TEXT        NULL COMMENT 'archive の再開用状態' AFTER phase,
  ADD COLUMN updated_count INT NULL COMMENT '再同期（上書き）した件数' AFTER saved_count,
  ADD COLUMN reply_count   INT NULL COMMENT '取り込んだスレッド返信数' AFTER updated_count,
  ADD COLUMN file_count    INT NULL COMMENT '処理した添付ファイル数' AFTER reply_count;

-- 3) users：ユーザー名の永続キャッシュ（メンション復元用）
CREATE TABLE IF NOT EXISTS slack_minutes_users (
  user_id      VARCHAR(32)  NOT NULL COMMENT 'Slack ユーザー ID',
  name         VARCHAR(200) NULL     COMMENT 'ハンドル名（user.name）',
  display_name VARCHAR(200) NULL     COMMENT '表示名（profile.display_name）',
  real_name    VARCHAR(200) NULL     COMMENT '本名（profile.real_name）',
  is_bot       TINYINT(1)   NOT NULL DEFAULT 0,
  deleted      TINYINT(1)   NOT NULL DEFAULT 0,
  fetched_at   DATETIME     NOT NULL COMMENT '取得日時（JST）',
  PRIMARY KEY (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 4) channels：チャンネル情報（名前・目的・最終アーカイブ日時）
CREATE TABLE IF NOT EXISTS slack_minutes_channels (
  channel_id       VARCHAR(32)  NOT NULL COMMENT 'Slack チャンネル ID',
  name             VARCHAR(200) NOT NULL COMMENT 'チャンネル名',
  is_private       TINYINT(1)   NOT NULL DEFAULT 0,
  topic            TEXT         NULL,
  purpose          TEXT         NULL,
  slack_created_at DATETIME     NULL     COMMENT 'Slack 上の作成日時（JST）',
  last_archived_at DATETIME     NULL     COMMENT '完全アーカイブ取得の最終完了日時（JST）',
  updated_at       DATETIME     NOT NULL COMMENT 'レコード更新日時（JST）',
  PRIMARY KEY (channel_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 5) files：添付ファイルの目録と保存状態（実体は slack_minutes/data/files/ 配下）
CREATE TABLE IF NOT EXISTS slack_minutes_files (
  id            INT          NOT NULL AUTO_INCREMENT,
  file_id       VARCHAR(32)  NOT NULL COMMENT 'Slack ファイル ID',
  channel_id    VARCHAR(32)  NOT NULL,
  slack_ts      VARCHAR(32)  NOT NULL COMMENT '添付元メッセージの ts',
  name          VARCHAR(500) NULL,
  title         VARCHAR(500) NULL,
  mimetype      VARCHAR(100) NULL,
  filetype      VARCHAR(32)  NULL,
  size          BIGINT       NULL     COMMENT 'バイト数（Slack の申告値）',
  url_private   TEXT         NULL     COMMENT 'url_private_download（要 Bot Token）',
  local_path    VARCHAR(600) NULL     COMMENT 'data/files/ からの相対パス',
  status        VARCHAR(16)  NOT NULL DEFAULT 'pending' COMMENT 'pending/done/error/expired',
  error         TEXT         NULL,
  created_at    DATETIME     NOT NULL COMMENT '登録日時（JST）',
  downloaded_at DATETIME     NULL     COMMENT '保存完了日時（JST）',
  PRIMARY KEY (id),
  UNIQUE KEY uq_file_msg (file_id, channel_id, slack_ts),
  KEY idx_channel_ts (channel_id, slack_ts),
  KEY idx_status (channel_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
