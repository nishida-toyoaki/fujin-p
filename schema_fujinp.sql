-- ============================================================
-- FUJIN-P Migration Schema : fujinp
-- Generated : 2026-07-30 12:46 JST
-- Source    : nishida4fujinp$fujinp (nishida4fujinp / PythonAnywhere)
-- ============================================================
--
-- このファイルは「fujinp」データベース専用です。
--
-- 使い方（マイグレーション先 target）:
--   1. MySQL コンソールを開く
--   2. 下の USE 文の target を自分のアカウント名に書き換えて実行
--   3. source でこのファイルを読み込む
--
-- 注: 外部キー制約（FOREIGN KEY）は除去してあります。
--     テーブル・カラム・データ構造はそのままで、参照整合性の
--     自動チェックのみ無効化した緩やかなスキーマです。
--
-- ============================================================

-- ↓ target を自分のアカウント名に書き換えてください
USE `target$fujinp`;

SET FOREIGN_KEY_CHECKS = 0;

CREATE TABLE IF NOT EXISTS `T_03_01_学部入試実施状況` (
  `入学年度` int DEFAULT NULL,
  `学部` varchar(255) DEFAULT NULL,
  `学科` varchar(255) DEFAULT NULL,
  `項目` varchar(255) DEFAULT NULL,
  `実績` int DEFAULT NULL,
  `序列` int DEFAULT NULL,
  `備考` text,
  `出典URL` text
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3;

CREATE TABLE IF NOT EXISTS `T_06_04_受託共同研究事業費受入実績` (
  `整理番号` varchar(255) DEFAULT NULL,
  `委託者` varchar(255) DEFAULT NULL,
  `委託年度` int DEFAULT NULL,
  `内容` text,
  `所属` varchar(255) DEFAULT NULL,
  `担当者` varchar(255) DEFAULT NULL,
  `経費` int DEFAULT NULL,
  `直接経費` int DEFAULT NULL,
  `間接経費` int DEFAULT NULL,
  `備考` text
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3;

CREATE TABLE IF NOT EXISTS `colrep_access_groups` (
  `project_id` int NOT NULL,
  `group_id` int NOT NULL,
  PRIMARY KEY (`project_id`,`group_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `colrep_projects` (
  `id` int NOT NULL AUTO_INCREMENT,
  `プロジェクト名` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `責任者` int NOT NULL,
  `テーブル名` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `Composer` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci,
  `is_public` tinyint(1) DEFAULT '0',
  `access_policy` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'private',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_project_name` (`プロジェクト名`),
  UNIQUE KEY `uk_table_name` (`テーブル名`),
  KEY `idx_updated` (`更新日時`),
  KEY `idx_responsible` (`責任者`),
  KEY `idx_public` (`is_public`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `document_access_groups` (
  `doc_id` int NOT NULL,
  `group_id` int NOT NULL,
  PRIMARY KEY (`doc_id`,`group_id`),
  KEY `idx_doc_id` (`doc_id`),
  KEY `idx_group_id` (`group_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `public_documents` (
  `id` int NOT NULL AUTO_INCREMENT,
  `title` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `public_description` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci,
  `owner_memo` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci,
  `content` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci,
  `created_by` int DEFAULT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `access_policy` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT 'public',
  `file_type` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'MIMEタイプ（バイナリアップロード時）',
  `file_path` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'ストレージ上の相対パス',
  `corepo_source_json` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT 'CoRePoプロジェクトのソース(JSON)。アーカイブ保存時に格納。再インポート用。',
  PRIMARY KEY (`id`),
  KEY `idx_title` (`title`),
  KEY `idx_created_at` (`created_at`),
  KEY `idx_created_by` (`created_by`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


SET FOREIGN_KEY_CHECKS = 1;
