-- ============================================================
-- fujin_forum（えふえふ）v1.1  テーブル定義（MySQL コンソールに貼る）
-- ============================================================

CREATE TABLE IF NOT EXISTS fujin_forum_channels (
  id               INT NOT NULL AUTO_INCREMENT,
  name             VARCHAR(100) NOT NULL COMMENT 'チャンネル名（# なし）',
  description      TEXT NULL COMMENT '説明',
  share_key        ENUM('private','public','domestic','group','domestic_group') NOT NULL DEFAULT 'private'
                   COMMENT '公開範囲：private=作成者と admin / public=ゲストにも / domestic=構成員だけ / group / domestic_group',
  created_by       INT NULL COMMENT 'users.id',
  created_at       DATETIME NOT NULL COMMENT 'JST',
  updated_at       DATETIME NOT NULL COMMENT 'JST',
  is_archived      TINYINT(1) NOT NULL DEFAULT 0 COMMENT '1=読み取り専用',
  sort_order       DOUBLE NOT NULL DEFAULT 0,
  slack_channel_id VARCHAR(32) NULL COMMENT '取込元の Slack チャンネル ID（すらくみ）',
  PRIMARY KEY (id),
  UNIQUE KEY uq_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='えふえふ：チャンネル';

CREATE TABLE IF NOT EXISTS fujin_forum_access_groups (
  channel_id INT NOT NULL,
  group_id   INT NOT NULL COMMENT 'user_groups.id',
  PRIMARY KEY (channel_id, group_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='えふえふ：許可グループ';

CREATE TABLE IF NOT EXISTS fujin_forum_posts (
  id            INT NOT NULL AUTO_INCREMENT,
  channel_id    INT NOT NULL,
  parent_id     INT NULL COMMENT '返信なら親記事の id（1段）',
  user_id       INT NULL COMMENT 'users.id（Slack 由来で対応づかない場合は NULL）',
  author_name   VARCHAR(200) NOT NULL DEFAULT '' COMMENT '表示名（投稿時点）',
  body_md       MEDIUMTEXT NULL COMMENT 'Markdown 本文',
  created_at    DATETIME NOT NULL COMMENT 'JST（Slack 由来は元の投稿日時）',
  updated_at    DATETIME NOT NULL COMMENT 'JST',
  edited_at     DATETIME NULL COMMENT '本文を編集した日時',
  deleted_at    DATETIME NULL COMMENT '論理削除',
  source        ENUM('user','slack') NOT NULL DEFAULT 'user',
  slack_ts      VARCHAR(32) NULL COMMENT '取込元の Slack ts（冪等取込の照合キー）',
  reply_count   INT NOT NULL DEFAULT 0,
  last_reply_at DATETIME NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_slack (channel_id, slack_ts),
  KEY idx_channel_parent (channel_id, parent_id, created_at),
  KEY idx_parent (parent_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='えふえふ：記事と返信';

CREATE TABLE IF NOT EXISTS fujin_forum_reactions (
  id           INT NOT NULL AUTO_INCREMENT,
  post_id      INT NOT NULL,
  user_id      INT NULL COMMENT 'users.id（Slack 由来は NULL）',
  reactor_name VARCHAR(200) NOT NULL DEFAULT '',
  emoji        VARCHAR(32) NOT NULL,
  created_at   DATETIME NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_user_emoji (post_id, user_id, emoji),
  KEY idx_post (post_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='えふえふ：リアクション';

CREATE TABLE IF NOT EXISTS fujin_forum_attachments (
  id          INT NOT NULL AUTO_INCREMENT,
  post_id     INT NULL COMMENT '本文が参照する記事（投稿前は NULL）',
  channel_id  INT NOT NULL COMMENT 'アクセス権の判定に使うチャンネル',
  name        VARCHAR(500) NOT NULL,
  mimetype    VARCHAR(100) NULL,
  size        BIGINT NULL,
  local_path  VARCHAR(600) NULL COMMENT '保護領域 data/files/ からの相対パス（原本）',
  public_path VARCHAR(600) NULL COMMENT '公開複製の URL パス（/static/ffimgs/…）．未公開は NULL',
  uploaded_by INT NULL COMMENT 'users.id（Slack 由来は NULL）',
  source      ENUM('user','slack') NOT NULL DEFAULT 'user',
  created_at  DATETIME NOT NULL,
  PRIMARY KEY (id),
  KEY idx_post (post_id),
  KEY idx_channel (channel_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='えふえふ：添付（原本は保護領域，公開複製は任意）';

CREATE TABLE IF NOT EXISTS fujin_forum_reads (
  user_id      INT NOT NULL,
  channel_id   INT NOT NULL,
  last_read_at DATETIME NOT NULL,
  PRIMARY KEY (user_id, channel_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='えふえふ：既読';
