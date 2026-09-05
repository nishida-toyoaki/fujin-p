-- こんか（CQM） v0.1 スキーマ
-- 様式の語彙はここに現れない。欄の名前や部局名はすべて attrs のデータ。

CREATE TABLE IF NOT EXISTS cqm_quanta (
  id         BIGINT AUTO_INCREMENT PRIMARY KEY,
  kind       VARCHAR(16)  NOT NULL,            -- 'item'（箇条） / 'box'（複合）
  key_path   VARCHAR(255) NULL,                -- 箱の安定した名前。様式が変わっても生き延びる
  title      VARCHAR(255) NULL,
  body       MEDIUMTEXT   NULL,                -- 箇条の中身
  recipe     VARCHAR(32)  NULL,                -- 箱の作法（seq / record / count）
  attrs      JSON         NULL,                -- 担当・欄の名前・出自など，様式に属すること
  owner_id   INT          NULL,
  created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_cqm_key (key_path),
  KEY ix_cqm_kind (kind)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS cqm_links (
  id        BIGINT AUTO_INCREMENT PRIMARY KEY,
  parent_id BIGINT NOT NULL,
  child_id  BIGINT NOT NULL,
  ord       INT    NOT NULL DEFAULT 0,         -- 並びは箱の持ち物
  KEY ix_cqm_links_parent (parent_id, ord),
  KEY ix_cqm_links_child (child_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS cqm_requests (
  id          BIGINT AUTO_INCREMENT PRIMARY KEY,
  box_id      BIGINT       NOT NULL,
  addressee   VARCHAR(120) NULL,               -- 執筆を頼む相手（v0.1 は名前の文字列）
  message     TEXT         NULL,               -- 依頼文
  due_on      DATE         NULL,
  status      VARCHAR(16)  NOT NULL DEFAULT 'open',   -- open / answered
  created_by  INT          NULL,
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  answered_at DATETIME     NULL,
  persons     TEXT         NULL,              -- 相手：個人（読点区切り）
  grps        TEXT         NULL,              -- 相手：グループ（読点区切り）
  KEY ix_cqm_req_box (box_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS cqm_log (
  id          BIGINT AUTO_INCREMENT PRIMARY KEY,
  quantum_id  BIGINT       NOT NULL,
  verb        VARCHAR(32)  NOT NULL,           -- gather / distribute
  params      JSON         NULL,
  status      VARCHAR(16)  NOT NULL,           -- ok / partial / ng
  note        VARCHAR(500) NULL,
  actor_id    INT          NULL,
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY ix_cqm_log_q (quantum_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
