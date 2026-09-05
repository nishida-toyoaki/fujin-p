-- テーコン（table_converter） スキーマ
-- ここにあるのは対応式・標本・残余・実行の記録だけ。
-- 分解した中身が入るのは，対応式が名指しした「ふつうのSQLテーブル」で，
-- そちらはテーコンの持ち物ではない（対応式の画面から CREATE 文を出す）。

CREATE TABLE IF NOT EXISTS tcv_specs (
  id         INT AUTO_INCREMENT PRIMARY KEY,
  name       VARCHAR(64)  NOT NULL,               -- 英小文字と下線。対応式の呼び名
  title      VARCHAR(200) NULL,
  sheet      VARCHAR(120) NULL,                   -- 対象のシート名
  spec_json  MEDIUMTEXT   NULL,                   -- 対応式そのもの
  note       TEXT         NULL,
  status     VARCHAR(16)  NOT NULL DEFAULT 'draft',  -- draft / ready
  owner_id   INT          NULL,
  created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_tcv_spec_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS tcv_samples (
  id         INT AUTO_INCREMENT PRIMARY KEY,
  spec_id    INT          NULL,                   -- どの対応式のための標本か
  title      VARCHAR(200) NULL,
  filename   VARCHAR(255) NULL,
  sheet      VARCHAR(120) NULL,
  note       TEXT         NULL,
  owner_id   INT          NULL,
  created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY ix_tcv_sample_spec (spec_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS tcv_residue (
  id       BIGINT AUTO_INCREMENT PRIMARY KEY,
  spec_id  INT          NOT NULL,
  dataset  VARCHAR(64)  NOT NULL DEFAULT '',      -- 版（同じ様式の何年度ぶんか）
  mode     VARCHAR(8)   NOT NULL,                 -- in（帯の中） / out（帯の外）
  band     VARCHAR(64)  NULL,                     -- 宛先の帯
  ord_no   INT          NOT NULL DEFAULT 0,       -- その帯の何回目か
  dr       INT          NOT NULL DEFAULT 0,       -- 宛先からの行のずれ
  r_no     INT          NOT NULL DEFAULT 0,       -- 元の行（控え。組み立てには使わない）
  c_no     INT          NOT NULL DEFAULT 0,
  rs       INT          NOT NULL DEFAULT 1,
  cs       INT          NOT NULL DEFAULT 1,
  v        MEDIUMTEXT   NULL,
  KEY ix_tcv_res (spec_id, dataset, band, ord_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS tcv_runs (
  id         BIGINT AUTO_INCREMENT PRIMARY KEY,
  spec_id    INT          NULL,
  dataset    VARCHAR(64)  NULL,
  direction  VARCHAR(16)  NOT NULL,               -- decompose（絵→表） / compose（表→絵）
  summary    TEXT         NULL,
  actor_id   INT          NULL,
  created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY ix_tcv_run_spec (spec_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS tcv_paint (
  id         INT AUTO_INCREMENT PRIMARY KEY,
  sample_id  INT          NOT NULL,             -- どの絵テーブルか
  sheet      VARCHAR(120) NOT NULL DEFAULT '',  -- どのシートか
  marks_json MEDIUMTEXT   NULL,                 -- セル区分（番地 → blank / label / data）
  note       TEXT         NULL,
  owner_id   INT          NULL,
  created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_tcv_paint (sample_id, sheet)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
