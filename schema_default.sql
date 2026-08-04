-- ============================================================
-- FUJIN-P Migration Schema : default
-- Generated : 2026-07-30 12:46 JST
-- Source    : nishida4fujinp$default (nishida4fujinp / PythonAnywhere)
-- ============================================================
--
-- このファイルは「default」データベース専用です。
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
USE `target$default`;

SET FOREIGN_KEY_CHECKS = 0;

CREATE TABLE IF NOT EXISTS `app_feature_permissions` (
  `id` int NOT NULL AUTO_INCREMENT,
  `app_id` int NOT NULL,
  `user_category` varchar(20) NOT NULL DEFAULT 'guest',
  `feature_id` int NOT NULL,
  `permission_id` int NOT NULL,
  `granted_at` datetime DEFAULT NULL COMMENT '付与日時（JST）',
  `granted_by` int DEFAULT NULL COMMENT '付与者のuser_id',
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_app_feature_perm` (`app_id`,`feature_id`,`permission_id`),
  UNIQUE KEY `unique_app_cat_feature_perm` (`app_id`,`user_category`,`feature_id`,`permission_id`),
  KEY `feature_id` (`feature_id`),
  KEY `permission_id` (`permission_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='アプリ × フィーチャー × 権限ラベル';

CREATE TABLE IF NOT EXISTS `app_permission_labels` (
  `id` int NOT NULL AUTO_INCREMENT,
  `app_id` int NOT NULL,
  `permission_code` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '権限コード（英小文字+アンダースコア）',
  `permission_name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '権限の表示名',
  `description` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '権限の説明',
  `sort_order` int DEFAULT '0' COMMENT '表示順',
  `created_at` datetime DEFAULT NULL COMMENT '作成日時',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_app_perm` (`app_id`,`permission_code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `app_share_backups` (
  `id` int NOT NULL AUTO_INCREMENT,
  `backup_name` varchar(255) NOT NULL,
  `backup_path` varchar(500) DEFAULT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `created_by` int DEFAULT NULL,
  `description` text,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `app_share_documents` (
  `id` int NOT NULL AUTO_INCREMENT,
  `app_name` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'アプリのディレクトリ名',
  `doc_type` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'manual=ユーザマニュアル, spec=技\n術仕様書',
  `title` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT '' COMMENT 'ドキュメントタイトル',
  `content` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT 'Markdownテキスト',
  `updated_by` int DEFAULT NULL COMMENT '最終更新者のuser_id',
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_app_doc` (`app_name`,`doc_type`),
  KEY `idx_app_name` (`app_name`),
  KEY `idx_doc_type` (`doc_type`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `app_share_install_history` (
  `id` int NOT NULL AUTO_INCREMENT,
  `app_name` varchar(100) NOT NULL,
  `version` int DEFAULT '1',
  `source_site` varchar(255) DEFAULT NULL,
  `installed_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `installed_by` int DEFAULT NULL,
  `backup_name` varchar(255) DEFAULT NULL,
  `status` varchar(50) DEFAULT 'pending',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `app_share_published` (
  `id` int NOT NULL AUTO_INCREMENT,
  `site_name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'サイト識別子',
  `app_name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'アプリ名（ディレクトリ名）',
  `display_name` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '表示名',
  `description` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '説明',
  `icon` varchar(10) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT 0xF09F93A6 COMMENT 'アイコン（絵文字）',
  `dashboard_section` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT 'feature' COMMENT 'ダッシュボード\n配置(admin/standard/feature/dev)',
  `version` int DEFAULT '1' COMMENT 'バージョン',
  `content_hash` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'パッケージハッシュ(SHA25\n6)',
  `package_data` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT 'パッケージデータ(Base64)',
  `manifest` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT 'マニフェスト(JSON)',
  `published_at` datetime DEFAULT NULL COMMENT '初回公開日時',
  `published_by` int DEFAULT NULL COMMENT '公開者',
  `updated_at` datetime DEFAULT NULL COMMENT '最終更新日時',
  `updated_by` int DEFAULT NULL COMMENT '更新者',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_app` (`app_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `app_share_registry` (
  `id` int NOT NULL AUTO_INCREMENT,
  `app_name` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'アプリのディレクトリ名（識別子',
  `display_name` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '' COMMENT '表示名',
  `icon` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 0xF09F93A6 COMMENT 'UTF-8絵文字アイコ\nン',
  `description` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '概要（2行程度）',
  `sort_order` double NOT NULL DEFAULT '0' COMMENT '表示順（小さい順・実数可）',
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_app_name` (`app_name`),
  KEY `idx_sort_order` (`sort_order`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `approved_users` (
  `id` int NOT NULL AUTO_INCREMENT,
  `email` varchar(255) NOT NULL COMMENT 'メールアドレス',
  `full_name` varchar(255) NOT NULL COMMENT '氏名',
  `category` varchar(50) NOT NULL DEFAULT 'guest' COMMENT 'カテゴリ',
  `affiliation` varchar(255) DEFAULT NULL COMMENT '所属',
  `approved_by` int DEFAULT NULL COMMENT '承認者のユーザーID',
  `approved_at` datetime NOT NULL COMMENT '承認日時',
  `notes` text COMMENT 'メモ',
  PRIMARY KEY (`id`),
  UNIQUE KEY `email` (`email`),
  KEY `approved_by` (`approved_by`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='承認済み（パスワード未設定）ユーザー';

CREATE TABLE IF NOT EXISTS `apps` (
  `id` int NOT NULL AUTO_INCREMENT,
  `app_code` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '英小文字+アンダースコアのコード',
  `app_name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '表示名',
  `description` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT 'アプリの説明',
  `app_url` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'アプリのURL（参考用）',
  `is_active` tinyint(1) DEFAULT '1' COMMENT '有効/無効',
  `created_at` datetime DEFAULT NULL COMMENT '作成日時',
  `updated_at` datetime DEFAULT NULL COMMENT '更新日時',
  PRIMARY KEY (`id`),
  UNIQUE KEY `app_code` (`app_code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='FUJIN-P管理対象アプリケーション';

CREATE TABLE IF NOT EXISTS `awami_canvas_access_groups` (
  `id` int NOT NULL AUTO_INCREMENT,
  `canvas_id` int NOT NULL COMMENT 'awami_canvases.id',
  `group_id` int NOT NULL COMMENT 'user_groups.id',
  PRIMARY KEY (`id`),
  KEY `idx_canvas` (`canvas_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `awami_canvases` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'キャンバス名',
  `description` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '説明',
  `access_policy` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'private' COMMENT 'public/domestic/private/group/domestic_group',
  `owner_user_id` int NOT NULL COMMENT '作成者（users.id）＝講師／司会者',
  `created_at` datetime DEFAULT NULL COMMENT '作成日時（JST）',
  `updated_at` datetime DEFAULT NULL COMMENT '更新日時（JST）',
  PRIMARY KEY (`id`),
  KEY `idx_owner` (`owner_user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `awami_connector_types` (
  `id` int NOT NULL AUTO_INCREMENT,
  `category` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '分類（時間・因果・意図…）',
  `name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '結合子名（その前に 等）',
  `directed` tinyint(1) NOT NULL DEFAULT '1' COMMENT '向きあり（主→従）か',
  `sort_order` int NOT NULL DEFAULT '0' COMMENT '表示順',
  `is_active` tinyint(1) NOT NULL DEFAULT '1' COMMENT '有効フラグ',
  PRIMARY KEY (`id`),
  KEY `idx_active` (`is_active`,`sort_order`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `awami_edge_members` (
  `id` int NOT NULL AUTO_INCREMENT,
  `edge_id` int NOT NULL COMMENT 'awami_edges.id',
  `node_id` int NOT NULL COMMENT 'awami_nodes.id',
  `position` int NOT NULL COMMENT '順位（1=主）',
  `role` varchar(3) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'out' COMMENT 'in=入力側 / out=出力側',
  PRIMARY KEY (`id`),
  KEY `idx_edge` (`edge_id`),
  KEY `idx_node` (`node_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `awami_edges` (
  `id` int NOT NULL AUTO_INCREMENT,
  `canvas_id` int NOT NULL COMMENT 'awami_canvases.id',
  `connector_type_id` int NOT NULL COMMENT 'awami_connector_types.id',
  `note` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '注記',
  `created_at` datetime DEFAULT NULL COMMENT '作成日時（JST）',
  `updated_at` datetime DEFAULT NULL COMMENT '更新日時（JST）',
  `label_x` double DEFAULT NULL COMMENT 'ラベルX座標（NULL=自動配置）',
  `label_y` double DEFAULT NULL COMMENT 'ラベルY座標（NULL=自動配置）',
  PRIMARY KEY (`id`),
  KEY `idx_canvas` (`canvas_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `awami_node_access_groups` (
  `id` int NOT NULL AUTO_INCREMENT,
  `node_id` int NOT NULL COMMENT 'awami_nodes.id',
  `group_id` int NOT NULL COMMENT 'user_groups.id',
  PRIMARY KEY (`id`),
  KEY `idx_node` (`node_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `awami_node_opens` (
  `id` int NOT NULL AUTO_INCREMENT,
  `canvas_id` int NOT NULL COMMENT 'awami_canvases.id',
  `record_id` int DEFAULT NULL COMMENT 'awami_records.id（討論の記録の器）',
  `node_id` int DEFAULT NULL COMMENT 'awami_nodes.id（削除後も記録は残す）',
  `label` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '開いた時点のノードラベル（スナップショット※コメント末尾は貼付欠落）',
  `url` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '開いた実体URL（スナップショット）',
  `user_id` int DEFAULT NULL COMMENT '開いた人 users.id',
  `opened_at` datetime DEFAULT NULL COMMENT '日時（JST）',
  PRIMARY KEY (`id`),
  KEY `idx_canvas` (`canvas_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='ノードのURLを開いたイベント（討論の展開に表示）';

CREATE TABLE IF NOT EXISTS `awami_nodes` (
  `id` int NOT NULL AUTO_INCREMENT,
  `canvas_id` int NOT NULL COMMENT 'awami_canvases.id',
  `label` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'ナラティブ素名（表示ラベル）',
  `url` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '実体URL（MD文書等）',
  `note` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '注記',
  `access_policy` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'NULL=キャンバスに従う/public/domestic/private/group/domestic_group',
  `x` double NOT NULL DEFAULT '0' COMMENT 'キャンバス座標X（手動配置）',
  `y` double NOT NULL DEFAULT '0' COMMENT 'キャンバス座標Y（手動配置）',
  `created_by` int DEFAULT NULL COMMENT '作成者（users.id）',
  `created_at` datetime DEFAULT NULL COMMENT '作成日時（JST）',
  `updated_at` datetime DEFAULT NULL COMMENT '更新日時（JST）',
  PRIMARY KEY (`id`),
  KEY `idx_canvas` (`canvas_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `awami_plan_items` (
  `id` int NOT NULL AUTO_INCREMENT,
  `canvas_id` int NOT NULL COMMENT 'awami_canvases.id',
  `plan_id` int DEFAULT NULL COMMENT 'awami_plans.id',
  `node_id` int NOT NULL COMMENT 'awami_nodes.id',
  `position` int NOT NULL COMMENT '提示順（1始まり）',
  `indent` tinyint(1) NOT NULL DEFAULT '0' COMMENT '1=直上ノードのsub（オプショナル提示）',
  `created_at` datetime DEFAULT NULL COMMENT '追加日時（JST）',
  PRIMARY KEY (`id`),
  KEY `idx_canvas` (`canvas_id`,`position`),
  KEY `idx_plan` (`plan_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='プレゼン計画：時間順に開くノードの線形リスト';

CREATE TABLE IF NOT EXISTS `awami_plans` (
  `id` int NOT NULL AUTO_INCREMENT,
  `canvas_id` int NOT NULL COMMENT 'awami_canvases.id',
  `name` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '計画名',
  `created_at` datetime DEFAULT NULL COMMENT '作成日時（JST）',
  PRIMARY KEY (`id`),
  KEY `idx_canvas` (`canvas_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='プレゼン計画の器（1キャンバスに複数）';

CREATE TABLE IF NOT EXISTS `awami_poll_options` (
  `id` int NOT NULL AUTO_INCREMENT,
  `poll_id` int NOT NULL COMMENT 'awami_polls.id',
  `opt_index` int NOT NULL COMMENT '選択肢番号（0始まり・登録順）',
  `label` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '選択肢の表示',
  PRIMARY KEY (`id`),
  KEY `idx_poll` (`poll_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `awami_polls` (
  `id` int NOT NULL AUTO_INCREMENT,
  `canvas_id` int NOT NULL COMMENT 'awami_canvases.id',
  `record_id` int DEFAULT NULL COMMENT 'awami_records.id',
  `token` varchar(16) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '参加者URL用ランダム12桁トークン',
  `choice_question` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'n択の問い（NULL可）',
  `writein_question` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'write-inの問い（NULL可）',
  `status` enum('open','closed') CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'open',
  `created_by` int DEFAULT NULL COMMENT '作成者（講師／司会者）users.id',
  `created_at` datetime DEFAULT NULL COMMENT '作成日時（JST）',
  `closed_at` datetime DEFAULT NULL COMMENT '締切日時（JST）',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_awami_token` (`token`),
  KEY `idx_canvas_status` (`canvas_id`,`status`),
  KEY `idx_record` (`record_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `awami_records` (
  `id` int NOT NULL AUTO_INCREMENT,
  `canvas_id` int NOT NULL COMMENT 'awami_canvases.id',
  `name` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '記録名',
  `snapshot_json` mediumtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT 'スナップショット本体（events＋choice_pollsのJSON）',
  `flow_count` int NOT NULL DEFAULT '0' COMMENT '展開の件数',
  `poll_count` int NOT NULL DEFAULT '0' COMMENT '投票の件数',
  `created_at` datetime DEFAULT NULL COMMENT '作成日時（JST）',
  PRIMARY KEY (`id`),
  KEY `idx_canvas` (`canvas_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='討論の記録の器（1キャンバスに複数）';

CREATE TABLE IF NOT EXISTS `awami_transport_log` (
  `id` int NOT NULL AUTO_INCREMENT,
  `awanara_canvas_id` int NOT NULL COMMENT '取込元 awanara_canvases.id',
  `awami_canvas_id` int NOT NULL COMMENT '取込先 awami_canvases.id',
  `imported_at` datetime DEFAULT NULL COMMENT '取込日時（JST）',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_awami_transport` (`awanara_canvas_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='あわなら→（※テーブルCOMMENT末尾は貼付欠落。「あわなら→あわみ」の取込記録）';

CREATE TABLE IF NOT EXISTS `awami_votes` (
  `id` int NOT NULL AUTO_INCREMENT,
  `poll_id` int NOT NULL COMMENT 'awami_polls.id',
  `user_id` int DEFAULT NULL COMMENT '投票者 users.id（未ログインはNULL）',
  `anon_key` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '未ログイン投票者の匿名キー（※コメント末尾は貼付欠落）',
  `opt_index` int NOT NULL COMMENT '選んだ選択肢番号',
  `voted_at` datetime DEFAULT NULL COMMENT '投票日時（JST）',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_awami_vote` (`poll_id`,`user_id`),
  UNIQUE KEY `uq_awami_vote_anon` (`poll_id`,`anon_key`),
  KEY `idx_poll` (`poll_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='n択投票：一人一票（再投票は上書き）';

CREATE TABLE IF NOT EXISTS `awami_writeins` (
  `id` int NOT NULL AUTO_INCREMENT,
  `poll_id` int NOT NULL COMMENT 'awami_polls.id',
  `seq_no` int NOT NULL COMMENT '通し番号（poll内で1始まり）',
  `user_id` int DEFAULT NULL COMMENT '投稿者 users.id（未ログインはNULL）',
  `anon_key` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '未ログイン投稿者の匿名キー',
  `content` mediumtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT 'write-in本文（MD）',
  `created_at` datetime DEFAULT NULL COMMENT '投稿日時（JST）',
  PRIMARY KEY (`id`),
  KEY `idx_poll` (`poll_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='write-in：何度でも追記可・番号と時刻を全員に表示';

CREATE TABLE IF NOT EXISTS `awanara_canvas_access_groups` (
  `id` int NOT NULL AUTO_INCREMENT,
  `canvas_id` int NOT NULL COMMENT 'awanara_canvases.id',
  `group_id` int NOT NULL COMMENT 'user_groups.id',
  PRIMARY KEY (`id`),
  KEY `idx_canvas` (`canvas_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `awanara_canvases` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'キャンバス名',
  `description` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '説明',
  `access_policy` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'private' COMMENT 'public/domestic/private/group/domestic_group',
  `owner_user_id` int NOT NULL COMMENT '作成者（users.id）第1版は単独owner',
  `created_at` datetime DEFAULT NULL COMMENT '作成日時（JST）',
  `updated_at` datetime DEFAULT NULL COMMENT '更新日時（JST）',
  PRIMARY KEY (`id`),
  KEY `idx_owner` (`owner_user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `awanara_connector_types` (
  `id` int NOT NULL AUTO_INCREMENT,
  `category` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '分類（時間・因果・意図…）',
  `name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '結合子名（その前に 等）',
  `directed` tinyint(1) NOT NULL DEFAULT '1' COMMENT '向きあり（主→従）か',
  `sort_order` int NOT NULL DEFAULT '0' COMMENT '表示順',
  `is_active` tinyint(1) NOT NULL DEFAULT '1' COMMENT '有効フラグ',
  PRIMARY KEY (`id`),
  KEY `idx_active` (`is_active`,`sort_order`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `awanara_edge_members` (
  `id` int NOT NULL AUTO_INCREMENT,
  `edge_id` int NOT NULL COMMENT 'awanara_edges.id',
  `node_id` int NOT NULL COMMENT 'awanara_nodes.id',
  `position` int NOT NULL COMMENT '順位（1=主）',
  PRIMARY KEY (`id`),
  KEY `idx_edge` (`edge_id`),
  KEY `idx_node` (`node_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `awanara_edges` (
  `id` int NOT NULL AUTO_INCREMENT,
  `canvas_id` int NOT NULL COMMENT 'awanara_canvases.id',
  `connector_type_id` int NOT NULL COMMENT 'awanara_connector_types.id',
  `note` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '注記',
  `created_at` datetime DEFAULT NULL COMMENT '作成日時（JST）',
  `updated_at` datetime DEFAULT NULL COMMENT '更新日時（JST）',
  PRIMARY KEY (`id`),
  KEY `idx_canvas` (`canvas_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `awanara_node_access_groups` (
  `id` int NOT NULL AUTO_INCREMENT,
  `node_id` int NOT NULL COMMENT 'awanara_nodes.id',
  `group_id` int NOT NULL COMMENT 'user_groups.id',
  PRIMARY KEY (`id`),
  KEY `idx_node` (`node_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `awanara_nodes` (
  `id` int NOT NULL AUTO_INCREMENT,
  `canvas_id` int NOT NULL COMMENT 'awanara_canvases.id',
  `label` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'ナラティブ素名（表示ラベル）',
  `url` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '実体URL（MD文書等）',
  `note` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '注記',
  `access_policy` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'NULL=キャンバスに従う/public/domestic/private/group/domestic_group',
  `x` double NOT NULL DEFAULT '0' COMMENT 'キャンバス座標X（手動配置）',
  `y` double NOT NULL DEFAULT '0' COMMENT 'キャンバス座標Y（手動配置）',
  `created_by` int DEFAULT NULL COMMENT '作成者（users.id）',
  `created_at` datetime DEFAULT NULL COMMENT '作成日時（JST）',
  `updated_at` datetime DEFAULT NULL COMMENT '更新日時（JST）',
  PRIMARY KEY (`id`),
  KEY `idx_canvas` (`canvas_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `block_breaker_scores` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL COMMENT 'ユーザーID（users.id）',
  `score` int NOT NULL COMMENT '獲得スコア',
  `cleared` tinyint(1) DEFAULT '0' COMMENT '全ブロック消去フラグ（1=クリア）',
  `played_at` datetime DEFAULT NULL COMMENT 'プレイ日時（JST）',
  PRIMARY KEY (`id`),
  KEY `idx_user` (`user_id`),
  KEY `idx_score` (`score`),
  KEY `idx_played` (`played_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `course_enrollments` (
  `id` int NOT NULL AUTO_INCREMENT,
  `student_user_id` int NOT NULL,
  `course_id` int NOT NULL,
  `mentor_user_id` int NOT NULL,
  `enrolled_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `student_user_id` (`student_user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3;

CREATE TABLE IF NOT EXISTS `course_phase_contents` (
  `id` int NOT NULL AUTO_INCREMENT,
  `course_id` int NOT NULL,
  `phase_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `phase_number` int NOT NULL,
  `phase_title` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `phase_description` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_course_phase` (`course_id`,`phase_id`),
  KEY `course_id` (`course_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `course_progress` (
  `id` int NOT NULL AUTO_INCREMENT,
  `student_user_id` int NOT NULL,
  `course_id` int NOT NULL,
  `phase_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `stage_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `step_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `status` enum('未着手','取り組み中','苦戦','完了','放棄') CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT '未着手',
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_progress` (`student_user_id`,`course_id`,`phase_id`,`stage_id`,`step_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `course_stage_contents` (
  `id` int NOT NULL AUTO_INCREMENT,
  `course_id` int NOT NULL,
  `phase_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `stage_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `stage_number` int NOT NULL,
  `stage_title` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_course_stage` (`course_id`,`phase_id`,`stage_id`),
  KEY `course_id` (`course_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `course_step_contents` (
  `id` int NOT NULL AUTO_INCREMENT,
  `course_id` int NOT NULL,
  `phase_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `stage_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `step_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `step_number` int NOT NULL,
  `step_title` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `step_detail` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_course_step` (`course_id`,`phase_id`,`stage_id`,`step_id`),
  KEY `course_id` (`course_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `courses` (
  `id` int NOT NULL AUTO_INCREMENT,
  `creator_user_id` int NOT NULL,
  `course_title` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `course_description` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci,
  `is_public` tinyint(1) DEFAULT '1',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `dc_nodes` (
  `id` int NOT NULL AUTO_INCREMENT,
  `story_id` int NOT NULL,
  `parent_id` int DEFAULT NULL,
  `sort_order` int DEFAULT '0',
  `body_md` mediumtext,
  `part_html` mediumtext,
  `meta_note` text,
  `created_at` datetime NOT NULL,
  `updated_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_story` (`story_id`),
  KEY `idx_parent` (`parent_id`),
  KEY `idx_sort` (`sort_order`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `dc_stories` (
  `id` int NOT NULL AUTO_INCREMENT,
  `title` varchar(200) NOT NULL,
  `description` text,
  `owner_user_id` int NOT NULL,
  `access_policy` enum('public','domestic','group','private') DEFAULT 'private',
  `group_id` int DEFAULT NULL,
  `sort_order` int DEFAULT '0',
  `created_at` datetime NOT NULL,
  `updated_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_owner` (`owner_user_id`),
  KEY `idx_policy` (`access_policy`),
  KEY `idx_sort` (`sort_order`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `features` (
  `id` int NOT NULL AUTO_INCREMENT,
  `feature_code` varchar(50) NOT NULL COMMENT 'フィーチャーコード（例: test_user）',
  `feature_name` varchar(100) NOT NULL COMMENT 'フィーチャー名（例: テストユーザー）',
  `description` text COMMENT 'フィーチャーの説明',
  `priority` float NOT NULL DEFAULT '2.5' COMMENT '優先度（0-5, 小さいほど高優先）',
  `is_active` tinyint(1) DEFAULT '1' COMMENT '有効/無効',
  `created_at` datetime NOT NULL COMMENT '作成日時',
  PRIMARY KEY (`id`),
  UNIQUE KEY `feature_code` (`feature_code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='フィーチャー（権限）マスタ';

CREATE TABLE IF NOT EXISTS `fujinp_helper_entries` (
  `id` int NOT NULL AUTO_INCREMENT,
  `author_user_id` int NOT NULL COMMENT '作成者ユーザーID',
  `subject` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'サブジェクト（検索対象）',
  `memo` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT 'メモ本文（Markdown）',
  `view_type` enum('author_only','group','all') CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'author_only' COMMENT ': author_only=著者のみ / group=指定グループ / all=全員',
  `view_group_id` int DEFAULT NULL COMMENT '閲覧グループID（view_type=group 時、user_groups.id）',
  `edit_type` enum('author_only','group') CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'author_only' COMMENT '編集権: author_only=著者のみ / group=指定グループ',
  `edit_group_id` int DEFAULT NULL COMMENT '編集グループID（edit_type=group 時、user_groups.id）',
  `status` enum('active','deprecated') CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'active' COMMENT 'ステータス',
  `deprecation_reason` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '廃止理由（Markdown）',
  `deprecation_link` varchar(1000) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '参照先URL（廃止時）',
  `deprecated_at` datetime DEFAULT NULL COMMENT '廃止日時（JST）',
  `created_at` datetime NOT NULL COMMENT '作成日時（JST）',
  `updated_at` datetime NOT NULL COMMENT '最終更新日時（JST）',
  PRIMARY KEY (`id`),
  KEY `idx_author` (`author_user_id`),
  KEY `idx_status` (`status`),
  KEY `idx_updated` (`updated_at`),
  KEY `idx_view_group` (`view_group_id`),
  KEY `idx_edit_group` (`edit_group_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `fukko_logs` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL COMMENT 'ユーザーID（users.id）',
  `conversation_id` varchar(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '会話ID（UUID）',
  `role` enum('user','assistant') CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '発話者',
  `content` mediumtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '発話内容',
  `model` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '使用モデル（assistant行のみ）',
  `input_tokens` int DEFAULT NULL COMMENT '入力トークン数（assistant行のみ）',
  `output_tokens` int DEFAULT NULL COMMENT '出力トークン数（assistant行のみ）',
  `created_at` datetime NOT NULL COMMENT '記録日時（JST）',
  PRIMARY KEY (`id`),
  KEY `idx_user` (`user_id`),
  KEY `idx_conversation` (`conversation_id`),
  KEY `idx_created` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `image_archive` (
  `id` int NOT NULL AUTO_INCREMENT,
  `label` varchar(100) NOT NULL,
  `drive_file_id` varchar(200) DEFAULT NULL,
  `drive_url` text,
  `mimetype` varchar(100) DEFAULT NULL,
  `original_filename` varchar(300) DEFAULT NULL,
  `filesize` bigint DEFAULT NULL,
  `title` varchar(300) DEFAULT NULL,
  `memo` text,
  `source_app` varchar(100) DEFAULT NULL,
  `created_by` int DEFAULT NULL,
  `created_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `label` (`label`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `ir_project_access_groups` (
  `project_id` int NOT NULL,
  `group_id` int NOT NULL,
  PRIMARY KEY (`project_id`,`group_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `ir_project_annotations` (
  `id` int NOT NULL AUTO_INCREMENT,
  `project_id` int NOT NULL,
  `indicator_id` int NOT NULL,
  `label` varchar(200) NOT NULL,
  `value` decimal(15,4) NOT NULL,
  `color` varchar(20) DEFAULT '#ef4444',
  `sort_order` int DEFAULT '0',
  `created_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `project_id` (`project_id`),
  KEY `indicator_id` (`indicator_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `ir_project_indicators` (
  `id` int NOT NULL AUTO_INCREMENT,
  `project_id` int NOT NULL,
  `name` varchar(300) NOT NULL,
  `sql_query` text NOT NULL,
  `value_column` varchar(100) NOT NULL,
  `year_column` varchar(100) NOT NULL DEFAULT '年度',
  `description` text,
  `sort_order` int DEFAULT '0',
  `created_at` datetime DEFAULT NULL,
  `updated_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `project_id` (`project_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `ir_projects` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(200) NOT NULL,
  `description` text,
  `owner_user_id` int DEFAULT NULL,
  `group_id` int DEFAULT NULL,
  `access_policy` enum('public','domestic','group','private') NOT NULL DEFAULT 'private',
  `created_at` datetime DEFAULT NULL,
  `updated_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `ir_table_set_items` (
  `id` int NOT NULL AUTO_INCREMENT,
  `table_set_id` int NOT NULL,
  `database_name` varchar(100) NOT NULL,
  `table_name` varchar(100) NOT NULL,
  `manager_group_id` int DEFAULT NULL,
  `added_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `table_set_id` (`table_set_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `ir_table_sets` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(200) NOT NULL,
  `description` text,
  `owner_user_id` int DEFAULT NULL,
  `created_at` datetime DEFAULT NULL,
  `updated_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `migrate_fujinp_scions_packages` (
  `id` int NOT NULL AUTO_INCREMENT,
  `filename` varchar(255) NOT NULL,
  `description` text,
  `source_owner` varchar(128) DEFAULT NULL,
  `file_count` int DEFAULT '0',
  `included_dbs` text,
  `size_bytes` bigint DEFAULT '0',
  `created_by` varchar(255) DEFAULT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_filename` (`filename`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `migration_assistant_mentor_assignments` (
  `id` int NOT NULL AUTO_INCREMENT,
  `student_user_id` int NOT NULL COMMENT '弟子のユーザーID (users.id)',
  `mentor_user_id` int NOT NULL COMMENT '師匠のユーザーID (users.id)',
  `system_id` int NOT NULL COMMENT '対象システムID (migration_assistant_systems.id)',
  `assigned_at` datetime DEFAULT CURRENT_TIMESTAMP COMMENT '指名日時',
  `notes` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT 'メモ（任意）',
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_student_system` (`student_user_id`,`system_id`) COMMENT '1システムにつき1人の師匠',
  KEY `idx_student` (`student_user_id`),
  KEY `idx_mentor` (`mentor_user_id`),
  KEY `idx_system` (`system_id`),
  KEY `idx_mentor_student` (`mentor_user_id`,`student_user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `migration_assistant_mentors` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL COMMENT '師匠になれるユーザーID (users.id)',
  `approved_by` int NOT NULL COMMENT '承認したadminのユーザーID (users.id)',
  `valid_from` datetime DEFAULT NULL COMMENT '有効開始日時（任意）',
  `valid_until` datetime DEFAULT NULL COMMENT '有効終了日時（任意）',
  `notes` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '備考（任意）',
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_user_mentor` (`user_id`),
  KEY `idx_valid_period` (`valid_from`,`valid_until`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='まいあし：師匠候補の管理（adminが承認）';

CREATE TABLE IF NOT EXISTS `migration_assistant_phase_contents` (
  `id` int NOT NULL AUTO_INCREMENT,
  `mentor_user_id` int NOT NULL COMMENT '編集した師匠のユーザーID',
  `phase_id` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'Phase ID (例: phase1)',
  `phase_number` int NOT NULL COMMENT 'Phase番号',
  `phase_title` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'Phaseタイトル（師匠',
  `phase_description` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT 'Phase説明',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_mentor_phase` (`mentor_user_id`,`phase_id`),
  KEY `idx_mentor` (`mentor_user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Phaseタイトル';

CREATE TABLE IF NOT EXISTS `migration_assistant_progress` (
  `id` int NOT NULL AUTO_INCREMENT,
  `student_user_id` int NOT NULL,
  `mentor_user_id` int NOT NULL,
  `phase_id` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `stage_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `step_id` varchar(150) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `status` enum('未着手','取り組み中','苦戦','完了','放棄') CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '未着手',
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_progress` (`student_user_id`,`mentor_user_id`,`phase_id`,`stage_id`,`step_id`),
  KEY `idx_student` (`student_user_id`),
  KEY `idx_mentor` (`mentor_user_id`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `migration_assistant_stage_contents` (
  `id` int NOT NULL AUTO_INCREMENT,
  `mentor_user_id` int NOT NULL COMMENT '編集した師匠のユーザーID',
  `phase_id` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'Phase ID',
  `stage_id` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'Stage ID (例: stage1_1)',
  `stage_number` int NOT NULL COMMENT 'Stage番号',
  `stage_title` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'Stageタイトル（師匠が編集可',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_mentor_stage` (`mentor_user_id`,`phase_id`,`stage_id`),
  KEY `idx_mentor` (`mentor_user_id`),
  KEY `idx_phase` (`phase_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Stageタイトル';

CREATE TABLE IF NOT EXISTS `migration_assistant_step_contents` (
  `id` int NOT NULL AUTO_INCREMENT,
  `mentor_user_id` int NOT NULL COMMENT '編集した師匠のユーザーID',
  `phase_id` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'Phase ID',
  `stage_id` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'Stage ID',
  `step_id` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'Step ID (例: step1_1_1)',
  `step_number` int NOT NULL COMMENT 'Step番号',
  `step_title` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'Stepタイトル（師匠が編集可能',
  `step_detail` mediumtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT 'Step詳細（Markdown形式、師匠が',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_mentor_step` (`mentor_user_id`,`phase_id`,`stage_id`,`step_id`),
  KEY `idx_mentor` (`mentor_user_id`),
  KEY `idx_stage` (`phase_id`,`stage_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='師匠が編集するStepタイトルと詳細';

CREATE TABLE IF NOT EXISTS `my_md_notes_contents` (
  `id` int NOT NULL AUTO_INCREMENT,
  `ノートID` int NOT NULL COMMENT 'my_md_notes_notes.id（1対1）',
  `内容` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT 'Markdown本文',
  `作成日時` datetime NOT NULL COMMENT 'JST',
  `更新日時` datetime NOT NULL COMMENT 'JST',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_my_md_notes_contents_note` (`ノートID`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='マイノート：Markdown本文';

CREATE TABLE IF NOT EXISTS `my_md_notes_notes` (
  `id` int NOT NULL AUTO_INCREMENT,
  `オーナーID` int NOT NULL COMMENT '作成者（users.id）',
  `名前` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'ノート名',
  `共有キー` enum('private','public','shared') CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'private' COMMENT '共有設定',
  `序列` int NOT NULL DEFAULT '0' COMMENT '表示順（小さいほど先）',
  `作成日時` datetime NOT NULL COMMENT 'JST',
  `更新日時` datetime NOT NULL COMMENT 'JST',
  PRIMARY KEY (`id`),
  KEY `idx_my_md_notes_notes_owner` (`オーナーID`),
  KEY `idx_my_md_notes_notes_updated` (`更新日時`),
  KEY `idx_my_md_notes_notes_share` (`共有キー`),
  KEY `idx_my_md_notes_notes_name` (`名前`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='マイノート：ノート本体';

CREATE TABLE IF NOT EXISTS `my_md_notes_shares` (
  `id` int NOT NULL AUTO_INCREMENT,
  `ノートID` int NOT NULL COMMENT 'my_md_notes_notes.id',
  `共有先ユーザID` int NOT NULL COMMENT 'users.id',
  `権限` enum('閲覧','編集') CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '閲覧',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_my_md_notes_shares_note_user` (`ノートID`,`共有先ユーザID`),
  KEY `idx_my_md_notes_shares_user` (`共有先ユーザID`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='マイノート：特定ユーザー共有';

CREATE TABLE IF NOT EXISTS `notify_ledger` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `created_at` datetime NOT NULL,
  `app` varchar(64) DEFAULT NULL,
  `kind` varchar(128) DEFAULT NULL,
  `target_kind` varchar(16) NOT NULL,
  `target` varchar(255) NOT NULL,
  `sender` varchar(255) DEFAULT NULL,
  `text` text,
  `send_at` datetime DEFAULT NULL,
  `status` varchar(16) NOT NULL,
  `error` varchar(255) DEFAULT NULL,
  `slack_ts` varchar(32) DEFAULT NULL,
  `scheduled_message_id` varchar(64) DEFAULT NULL,
  `slack_channel` varchar(32) DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_target` (`target_kind`,`target`),
  KEY `idx_sender` (`sender`),
  KEY `idx_created` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `official_data_archive_composite_components` (
  `id` int NOT NULL AUTO_INCREMENT,
  `composite_id` int NOT NULL,
  `indicator_view_id` int NOT NULL,
  `color` varchar(16) NOT NULL DEFAULT '#2e6da4',
  `seq` int NOT NULL DEFAULT '1',
  PRIMARY KEY (`id`),
  KEY `idx_oda_cc_composite` (`composite_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `official_data_archive_composites` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(255) NOT NULL,
  `sort_order` int NOT NULL DEFAULT '100',
  `manager_group_id` int DEFAULT NULL,
  `created_by` int DEFAULT NULL,
  `created_at` datetime NOT NULL,
  `updated_by` int DEFAULT NULL,
  `updated_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `official_data_archive_indicator_views` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(255) NOT NULL,
  `query` text NOT NULL,
  `sort_order` decimal(11,3) NOT NULL DEFAULT '100.000',
  `chart_type` varchar(8) NOT NULL DEFAULT 'bar',
  `manager_group_id` int DEFAULT NULL,
  `created_by` int DEFAULT NULL,
  `created_at` datetime NOT NULL,
  `updated_by` int DEFAULT NULL,
  `updated_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `official_data_archive_tables` (
  `id` int NOT NULL AUTO_INCREMENT,
  `table_name` varchar(64) NOT NULL,
  `database_name` varchar(16) NOT NULL DEFAULT 'fujinp',
  `display_name` varchar(255) DEFAULT NULL,
  `note` varchar(255) DEFAULT NULL,
  `manager_group_id` int DEFAULT NULL,
  `created_by` int DEFAULT NULL,
  `created_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_oda_db_table` (`database_name`,`table_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `official_data_archive_updates` (
  `id` int NOT NULL AUTO_INCREMENT,
  `table_item_id` int NOT NULL,
  `table_name` varchar(64) NOT NULL,
  `database_name` varchar(16) NOT NULL,
  `original_filename` varchar(255) DEFAULT NULL,
  `stored_filename` varchar(255) NOT NULL,
  `note` varchar(255) DEFAULT NULL,
  `uploaded_by` int DEFAULT NULL,
  `uploaded_by_name` varchar(255) DEFAULT NULL,
  `uploaded_at` datetime NOT NULL,
  `status` varchar(16) NOT NULL DEFAULT 'pending',
  `applied_by` int DEFAULT NULL,
  `applied_at` datetime DEFAULT NULL,
  `backup_table` varchar(80) DEFAULT NULL,
  `reject_reason` varchar(255) DEFAULT NULL,
  `source_ref` varchar(64) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_oda_upd_src` (`source_ref`),
  KEY `idx_oda_upd_item` (`table_item_id`),
  KEY `idx_oda_upd_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `password_reset_tokens` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `token` varchar(255) NOT NULL,
  `expires_at` datetime NOT NULL,
  `created_at` datetime NOT NULL,
  `used` tinyint(1) NOT NULL DEFAULT '0',
  PRIMARY KEY (`id`),
  UNIQUE KEY `token` (`token`),
  KEY `idx_user_id` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `registration_requests` (
  `id` int NOT NULL AUTO_INCREMENT,
  `email` varchar(255) NOT NULL COMMENT 'メールアドレス',
  `full_name` varchar(255) NOT NULL COMMENT '氏名',
  `category` varchar(50) DEFAULT '承認待ち_登録希望者' COMMENT 'カテゴリ',
  `affiliation` varchar(255) DEFAULT NULL COMMENT '所属',
  `status` varchar(20) NOT NULL DEFAULT 'pending' COMMENT 'ステータス: pending, approved, rejected, bla\ncklisted',
  `requested_at` datetime NOT NULL COMMENT '申請日時',
  `processed_at` datetime DEFAULT NULL COMMENT '処理日時',
  `processed_by` int DEFAULT NULL COMMENT '処理者のユーザーID',
  `rejection_reason` text COMMENT '不承認理由',
  `ip_address` varchar(45) DEFAULT NULL COMMENT '申請元IPアドレス',
  PRIMARY KEY (`id`),
  KEY `processed_by` (`processed_by`),
  KEY `idx_email` (`email`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='外部ユー';

CREATE TABLE IF NOT EXISTS `slack_minutes_messages` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `session_id` int NOT NULL COMMENT 'slack_minutes_sessions.id',
  `channel_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'Slack チャンネル ID',
  `channel_name` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'チャンネル名',
  `slack_ts` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'Slack メッセージ ts（一意キー）',
  `sender_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'Slack ユーザー ID',
  `sender_name` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '表示名（取得時点）',
  `text` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT 'メッセージ本文',
  `posted_at` datetime DEFAULT NULL COMMENT '投稿日時（JST）',
  `thread_ts` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'スレッド親 ts（スレッド投稿の場合）',
  `created_at` datetime NOT NULL COMMENT 'レコード作成日時（JST）',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_channel_ts` (`channel_id`,`slack_ts`),
  KEY `idx_session` (`session_id`),
  KEY `idx_posted` (`posted_at`),
  KEY `idx_channel` (`channel_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `slack_minutes_sessions` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL COMMENT '操作した users.id',
  `channel_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'Slack チャンネル ID（C...）',
  `channel_name` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'チャンネル名',
  `fetched_at` datetime NOT NULL COMMENT '取得実行日時（JST）',
  `status` varchar(16) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'running' COMMENT 'running / done / error',
  `fetched_count` int DEFAULT NULL COMMENT '取得メッセージ総数',
  `saved_count` int DEFAULT NULL COMMENT '新規保存数（重複除く）',
  PRIMARY KEY (`id`),
  KEY `idx_channel` (`channel_id`),
  KEY `idx_fetched` (`fetched_at`),
  KEY `idx_user` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `sorakara_regions` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL COMMENT 'ユーザーID（users.id）',
  `name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '地域名',
  `description` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '説明',
  `lat` decimal(9,6) NOT NULL COMMENT '中心緯度（世界測地系）',
  `lon` decimal(9,6) NOT NULL COMMENT '中心経度（世界測地系）',
  `zoom` tinyint NOT NULL DEFAULT '12' COMMENT '地理院タイルズーム（dem_pngは最大14）',
  `radius` tinyint NOT NULL DEFAULT '2' COMMENT '読み込み半径タイル数（2→5x5）',
  `created_at` datetime DEFAULT NULL COMMENT '作成日時（JST）',
  PRIMARY KEY (`id`),
  KEY `idx_user` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `sql_saver_audit` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int DEFAULT NULL COMMENT '実行者のユーザーID',
  `action` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'backup / restore / clear_all',
  `succeeded` tinyint(1) NOT NULL DEFAULT '1',
  `detail` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '操作内容の要約JSON（60,000文字で切り詰め）',
  `created_at` datetime NOT NULL COMMENT '実行日時（JST）',
  PRIMARY KEY (`id`),
  KEY `idx_created_at` (`created_at`),
  KEY `idx_action` (`action`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `strm_reservations` (
  `id` int NOT NULL AUTO_INCREMENT,
  `resource_id` int NOT NULL COMMENT '資源ID（strm_resources.id）',
  `user_id` int NOT NULL COMMENT '予約者ID（共通 users.id）',
  `start_at` datetime NOT NULL COMMENT '利用開始日時（JST）',
  `end_at` datetime NOT NULL COMMENT '利用終了日時（JST）',
  `summary` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '概要（例: 会議）',
  `status` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'active' COMMENT 'pending=承認待ち \n/ active=確定 / canceled=取り下げ / rejected=却下',
  `created_at` datetime DEFAULT NULL COMMENT '申請日時（JST）',
  `decided_by` int DEFAULT NULL COMMENT '確定・取り下げ・却下の操作者（users.id）',
  `decided_at` datetime DEFAULT NULL COMMENT '決定日時（JST）',
  PRIMARY KEY (`id`),
  KEY `idx_resource_time` (`resource_id`,`start_at`),
  KEY `idx_user` (`user_id`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `strm_resources` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '資源名（例: 1204会議室、2208アクト',
  `category` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '部屋' COMMENT 'カテゴリ（部屋、 \nなど）',
  `description` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '説明',
  `approval_required` tinyint(1) NOT NULL DEFAULT '0' COMMENT '0=承認申請不要（即確定） / 1=要承認',
  `allow_regular` tinyint(1) NOT NULL DEFAULT '1' COMMENT '申請資格: users.category=regular に申請を許\n可（要承認時のみ有効）',
  `allow_guest` tinyint(1) NOT NULL DEFAULT '1' COMMENT '申請資格: users.category=guest に申請を許可（\n要承認時のみ有効）',
  `is_active` tinyint(1) DEFAULT '1' COMMENT '1=利用可 0=停止（論理削除）',
  `created_at` datetime DEFAULT NULL COMMENT '作成日時（JST）',
  `updated_at` datetime DEFAULT NULL COMMENT '更新日時（JST）',
  PRIMARY KEY (`id`),
  KEY `idx_active` (`is_active`),
  KEY `idx_category` (`category`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `table_master_archives` (
  `id` int NOT NULL AUTO_INCREMENT,
  `archive_number` int NOT NULL COMMENT 'テーブル第N号（MAX+1で採番）',
  `title` varchar(255) NOT NULL COMMENT '公開ページの見出し',
  `description` text COMMENT '説明文（HTML可）',
  `source_view_id` int DEFAULT NULL COMMENT '生成元ビュー table_master_views.id / NULL=アドホック',
  `source_database` varchar(100) DEFAULT NULL COMMENT '生成元データベース',
  `source_query` text COMMENT '生成元SQL（/archive/rerun で再実行）',
  `html_content` longtext COMMENT '公開するHTMLテーブル',
  `is_public` tinyint(1) NOT NULL DEFAULT '1' COMMENT '1=公開 / 0=非公開（下書き）',
  `created_by` int DEFAULT NULL COMMENT '作成者 users.id（FK制約なし）',
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_archive_number` (`archive_number`),
  KEY `idx_source_view` (`source_view_id`),
  KEY `idx_is_public` (`is_public`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='アーカイブ（公開テーブル＝テーブル第N号）';

CREATE TABLE IF NOT EXISTS `table_master_deletion_history` (
  `id` int NOT NULL AUTO_INCREMENT,
  `database_name` varchar(100) NOT NULL,
  `table_name` varchar(100) NOT NULL,
  `row_count` int DEFAULT '0',
  `reason` text,
  `deleted_by` int DEFAULT NULL,
  `deleted_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_database` (`database_name`),
  KEY `idx_table` (`table_name`),
  KEY `idx_deleted_at` (`deleted_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `table_master_edit_history` (
  `id` int NOT NULL AUTO_INCREMENT,
  `database_name` varchar(255) NOT NULL,
  `table_name` varchar(255) NOT NULL,
  `operation` enum('INSERT','UPDATE','DELETE') NOT NULL,
  `row_identifier` text,
  `changes_json` text,
  `edited_by` int DEFAULT NULL,
  `edited_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_table` (`database_name`,`table_name`),
  KEY `idx_edited_at` (`edited_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='データ編集履歴';

CREATE TABLE IF NOT EXISTS `table_master_project_access_groups` (
  `id` int NOT NULL AUTO_INCREMENT,
  `project_id` int NOT NULL,
  `group_id` int NOT NULL COMMENT 'user_groups.id（FK制約なし・別DB参照）',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_proj_group` (`project_id`,`group_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='プロジェクトへのグループアクセス制御（access_policy=group 時参照）';

CREATE TABLE IF NOT EXISTS `table_master_project_tables` (
  `id` int NOT NULL AUTO_INCREMENT,
  `project_id` int NOT NULL,
  `database_name` varchar(100) NOT NULL,
  `table_name` varchar(200) NOT NULL,
  `added_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_project_table` (`project_id`,`database_name`,`table_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='プロジェクトに登録されたDBテーブル一覧';

CREATE TABLE IF NOT EXISTS `table_master_project_views` (
  `id` int NOT NULL AUTO_INCREMENT,
  `project_id` int NOT NULL,
  `view_id` int NOT NULL,
  `added_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `html_content` longtext COMMENT '編集済みHTMLテーブル（最終保存状態）',
  `sort_order` decimal(7,6) DEFAULT NULL COMMENT '表示順（0.000000〜0.999999、小さいほど上位、NULL=0.5扱い）',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_project_view` (`project_id`,`view_id`),
  KEY `view_id` (`view_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='プロジェクト×ビュー紐づけ（HTML保存・表示順管理）';

CREATE TABLE IF NOT EXISTS `table_master_projects` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(200) NOT NULL COMMENT 'プロジェクト名',
  `description` text COMMENT '説明',
  `created_by` int DEFAULT NULL COMMENT '作成ユーザーID（FK制約なし）',
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `access_policy` enum('public','domestic','private','group') NOT NULL DEFAULT 'public',
  PRIMARY KEY (`id`),
  UNIQUE KEY `name` (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='プロジェクト（ビュー・テーブルをまとめる管理単位）';

CREATE TABLE IF NOT EXISTS `table_master_query_history` (
  `id` int NOT NULL AUTO_INCREMENT,
  `database_name` varchar(100) NOT NULL,
  `query_text` text NOT NULL,
  `row_count` int DEFAULT '0',
  `execution_time` float DEFAULT '0',
  `executed_by` int DEFAULT NULL,
  `executed_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_database` (`database_name`),
  KEY `idx_executed_at` (`executed_at`),
  KEY `idx_user` (`executed_by`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='クエリ実行履歴';

CREATE TABLE IF NOT EXISTS `table_master_rename_history` (
  `id` int NOT NULL AUTO_INCREMENT,
  `database_name` varchar(255) NOT NULL,
  `old_table_name` varchar(255) NOT NULL,
  `new_table_name` varchar(255) NOT NULL,
  `reason` text,
  `renamed_by` int DEFAULT NULL,
  `renamed_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_database` (`database_name`),
  KEY `idx_renamed_at` (`renamed_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='テーブル名変更履歴';

CREATE TABLE IF NOT EXISTS `table_master_views` (
  `id` int NOT NULL AUTO_INCREMENT,
  `view_name` varchar(255) NOT NULL,
  `database_name` varchar(255) NOT NULL,
  `sql_query` text NOT NULL,
  `description` text,
  `created_by` int DEFAULT NULL COMMENT '作成者 users.id（FK制約なし）',
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_view` (`database_name`,`view_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='保存されたSQLビュー定義';

CREATE TABLE IF NOT EXISTS `table_post_history` (
  `id` int NOT NULL AUTO_INCREMENT,
  `request_id` int DEFAULT NULL,
  `database_name` varchar(255) NOT NULL,
  `table_name` varchar(255) NOT NULL,
  `recorded_at` datetime NOT NULL,
  `snapshot` longtext,
  `row_count` int DEFAULT NULL,
  `note` varchar(255) DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_table` (`database_name`,`table_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `table_post_projects` (
  `id` int NOT NULL AUTO_INCREMENT,
  `project_name` varchar(255) NOT NULL,
  `description` text,
  `created_by` varchar(255) DEFAULT NULL,
  `created_at` datetime NOT NULL,
  `is_active` tinyint(1) NOT NULL DEFAULT '1',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `table_post_requests` (
  `id` int NOT NULL AUTO_INCREMENT,
  `project_id` int NOT NULL,
  `database_name` varchar(255) NOT NULL,
  `table_name` varchar(255) NOT NULL,
  `submitted_by` varchar(255) DEFAULT NULL,
  `submitted_at` datetime NOT NULL,
  `payload` longtext,
  `row_count` int DEFAULT NULL,
  `status` varchar(32) NOT NULL DEFAULT 'pending',
  PRIMARY KEY (`id`),
  KEY `idx_project` (`project_id`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `table_post_status` (
  `id` int NOT NULL AUTO_INCREMENT,
  `project_id` int NOT NULL,
  `database_name` varchar(255) NOT NULL,
  `table_name` varchar(255) NOT NULL,
  `assignee` varchar(255) DEFAULT NULL,
  `due_date` date DEFAULT NULL,
  `last_updater` varchar(255) DEFAULT NULL,
  `last_updated_at` datetime DEFAULT NULL,
  `final_status` text,
  PRIMARY KEY (`id`),
  KEY `idx_project` (`project_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `table_share_alliance_sites` (
  `id` int NOT NULL AUTO_INCREMENT,
  `site_name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'サイト名（表示用）',
  `site_url` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'サイトURL（例: https://xxx.pythonanywhere.com）',
  `api_key` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '認証用APIキー',
  `description` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '説明',
  `is_active` tinyint(1) DEFAULT '1' COMMENT '有効フラグ',
  `created_at` datetime NOT NULL COMMENT '登録日時',
  `created_by` int DEFAULT NULL COMMENT '登録者のuser_id',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_site_url` (`site_url`),
  KEY `idx_is_active` (`is_active`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `table_share_api_keys` (
  `id` int NOT NULL AUTO_INCREMENT,
  `api_key` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'APIキー',
  `description` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '説明（発行先など）',
  `is_active` tinyint(1) DEFAULT '1' COMMENT '有効フラグ',
  `created_at` datetime NOT NULL COMMENT '発行日時',
  `created_by` int DEFAULT NULL COMMENT '発行者のuser_id',
  `expires_at` datetime DEFAULT NULL COMMENT '有効期限（NULLは無期限）',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_api_key` (`api_key`),
  KEY `idx_is_active` (`is_active`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `table_share_published` (
  `id` int NOT NULL AUTO_INCREMENT,
  `site_name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'サイト識別子（PythonAnywhere アカウント名）',
  `database_name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'データベース名',
  `table_name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'テーブル名',
  `version` int NOT NULL DEFAULT '1' COMMENT 'バージョン番号',
  `content_hash` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'コンテンツのSHA256ハッシュ',
  `encrypted_content` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '暗号化されたテーブルデータ（JSON）',
  `row_count` int DEFAULT '0' COMMENT '行数',
  `description` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '説明',
  `published_at` datetime NOT NULL COMMENT '初回公開日時',
  `published_by` int DEFAULT NULL COMMENT '初回公開者のuser_id',
  `updated_at` datetime NOT NULL COMMENT '更新日時',
  `updated_by` int DEFAULT NULL COMMENT '更新者のuser_id',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_db_table` (`database_name`,`table_name`),
  KEY `idx_updated_at` (`updated_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `table_share_subscriptions` (
  `id` int NOT NULL AUTO_INCREMENT,
  `alliance_site_id` int NOT NULL COMMENT 'アライアンスサイトID',
  `remote_database` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'リモートのデータベース名',
  `remote_table` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'リモートのテーブル名',
  `local_database` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'ローカルのデータベース名',
  `local_table` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'ローカルのテーブル名',
  `auto_sync` tinyint(1) DEFAULT '0' COMMENT '自動同期フラグ',
  `last_synced_at` datetime DEFAULT NULL COMMENT '最終同期日時',
  `last_synced_version` int DEFAULT NULL COMMENT '最終同期バージョン',
  `created_at` datetime NOT NULL COMMENT '設定作成日時',
  `created_by` int DEFAULT NULL COMMENT '設定作成者のuser_id',
  PRIMARY KEY (`id`),
  KEY `idx_alliance_site` (`alliance_site_id`),
  KEY `idx_local_table` (`local_database`,`local_table`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `table_share_sync_history` (
  `id` int NOT NULL AUTO_INCREMENT,
  `subscription_id` int NOT NULL COMMENT '購読設定ID',
  `remote_version` int NOT NULL COMMENT '取得したリモートバージョン',
  `content_hash` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'コンテンツのSHA256ハッシュ',
  `row_count` int DEFAULT '0' COMMENT '同期した行数',
  `synced_at` datetime NOT NULL COMMENT '同期日時',
  `synced_by` int DEFAULT NULL COMMENT '同期実行者のuser_id',
  `status` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'success' COMMENT '状態（success/failed）',
  `backup_table` varchar(150) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'バックアップテーブル名',
  `error_message` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT 'エラーメッセージ（失敗時）',
  PRIMARY KEY (`id`),
  KEY `idx_subscription` (`subscription_id`),
  KEY `idx_synced_at` (`synced_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `table_snapshots` (
  `id` int NOT NULL AUTO_INCREMENT,
  `database_name` varchar(255) NOT NULL,
  `table_name` varchar(255) NOT NULL,
  `download_timestamp` datetime DEFAULT NULL,
  `download_snapshot` longtext,
  `upload_timestamp` datetime DEFAULT NULL,
  `upload_snapshot` longtext,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `ura_boxes` (
  `id` int NOT NULL AUTO_INCREMENT,
  `garden_id` int NOT NULL,
  `title` varchar(255) NOT NULL,
  `description` text,
  `url` text NOT NULL,
  `x` int NOT NULL DEFAULT '100',
  `y` int NOT NULL DEFAULT '100',
  `width` int NOT NULL DEFAULT '120',
  `height` int NOT NULL DEFAULT '59',
  `color` varchar(16) NOT NULL DEFAULT '#5cb87a',
  `sort_order` int NOT NULL DEFAULT '0',
  `created_at` datetime NOT NULL,
  `updated_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_garden_sort` (`garden_id`,`sort_order`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `ura_gardens` (
  `id` int NOT NULL AUTO_INCREMENT,
  `gardenset_id` int NOT NULL,
  `title` varchar(255) NOT NULL,
  `description` text,
  `color` varchar(16) NOT NULL DEFAULT '#5cb87a',
  `indent_level` int NOT NULL DEFAULT '0',
  `sort_order` int NOT NULL DEFAULT '0',
  `created_at` datetime NOT NULL,
  `updated_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_gs_sort` (`gardenset_id`,`sort_order`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `ura_gardenset_acl` (
  `id` int NOT NULL AUTO_INCREMENT,
  `gardenset_id` int NOT NULL,
  `kind` enum('view','edit') NOT NULL,
  `principal_type` enum('category','group') NOT NULL,
  `principal_value` varchar(64) NOT NULL,
  `created_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_gs_kind_pt_pv` (`gardenset_id`,`kind`,`principal_type`,`principal_value`),
  KEY `idx_lookup` (`gardenset_id`,`kind`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `ura_gardensets` (
  `id` int NOT NULL AUTO_INCREMENT,
  `title` varchar(255) NOT NULL,
  `description` text,
  `owner_user_id` int NOT NULL,
  `view_policy` enum('public','domestic','group','private') DEFAULT NULL,
  `view_group_id` int DEFAULT NULL,
  `edit_group_id` int DEFAULT NULL,
  `canvas_width` int NOT NULL DEFAULT '2400',
  `canvas_height` int NOT NULL DEFAULT '1600',
  `sort_order` int NOT NULL DEFAULT '0',
  `created_at` datetime NOT NULL,
  `updated_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_owner` (`owner_user_id`),
  KEY `idx_sort` (`sort_order`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `user_events` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `event_type` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `event_data` json DEFAULT NULL,
  `occurred_at` datetime NOT NULL,
  `ip_address` varchar(45) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_user_id` (`user_id`),
  KEY `idx_event_type` (`event_type`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `user_features` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL COMMENT 'ユーザーID',
  `feature_id` int NOT NULL COMMENT 'フィーチャーID',
  `granted_at` datetime NOT NULL COMMENT '付与日時',
  `granted_by` int DEFAULT NULL COMMENT '付与者のユーザーID',
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_user_feature` (`user_id`,`feature_id`),
  KEY `feature_id` (`feature_id`),
  KEY `granted_by` (`granted_by`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='ユーザーフィーチャー紐付け';

CREATE TABLE IF NOT EXISTS `user_group_global_managers` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `valid_from` datetime DEFAULT NULL,
  `valid_until` datetime DEFAULT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_user_id` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3;

CREATE TABLE IF NOT EXISTS `user_group_memberships` (
  `id` int NOT NULL AUTO_INCREMENT,
  `group_id` int NOT NULL,
  `user_id` int NOT NULL,
  `valid_from` datetime DEFAULT NULL,
  `valid_until` datetime DEFAULT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_group_id` (`group_id`),
  KEY `idx_user_id` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3;

CREATE TABLE IF NOT EXISTS `user_group_subgroups` (
  `id` int NOT NULL AUTO_INCREMENT,
  `parent_group_id` int NOT NULL,
  `child_group_id` int NOT NULL,
  `valid_from` datetime DEFAULT NULL,
  `valid_until` datetime DEFAULT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_parent_group_id` (`parent_group_id`),
  KEY `idx_child_group_id` (`child_group_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3;

CREATE TABLE IF NOT EXISTS `user_groups` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(255) NOT NULL,
  `description` text,
  `manager_user_id` int NOT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `manager_user_id` (`manager_user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3;

CREATE TABLE IF NOT EXISTS `user_migration_conflicts` (
  `id` int NOT NULL AUTO_INCREMENT,
  `job_id` int NOT NULL,
  `email` varchar(255) NOT NULL,
  `conflict_type` enum('regular_guest','admin_source') NOT NULL,
  `local_user_json` text,
  `remote_user_json` text,
  `resolution` enum('pending','use_remote','use_local','skip','set_admin','set_regular','set_guest') DEFAULT 'pending',
  `resolved_at` datetime DEFAULT NULL,
  `resolved_by` int DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `user_migration_export_permissions` (
  `id` int NOT NULL AUTO_INCREMENT,
  `requester_url` varchar(500) NOT NULL,
  `requester_name` varchar(255) DEFAULT NULL,
  `status` enum('pending','approved','rejected') DEFAULT 'pending',
  `requested_at` datetime DEFAULT NULL,
  `processed_at` datetime DEFAULT NULL,
  `processed_by` int DEFAULT NULL,
  `requester_nonce` varchar(128) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `user_migration_group_conflicts` (
  `id` int NOT NULL AUTO_INCREMENT,
  `job_id` int NOT NULL,
  `remote_group_name` varchar(255) NOT NULL,
  `conflict_type` enum('name_conflict','manager_missing','members_missing') NOT NULL,
  `remote_group_json` text,
  `missing_user_emails` text,
  `resolution` enum('pending','use_remote','use_local','skip') DEFAULT 'pending',
  `resolved_at` datetime DEFAULT NULL,
  `resolved_by` int DEFAULT NULL,
  `all_issue_types` varchar(255) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `user_migration_jobs` (
  `id` int NOT NULL AUTO_INCREMENT,
  `alliance_site_id` int NOT NULL,
  `status` enum('fetched','conflict_review','executing','done','error') NOT NULL DEFAULT 'fetched',
  `total_remote_users` int DEFAULT '0',
  `total_new_users` int DEFAULT '0',
  `total_conflicts_users` int DEFAULT '0',
  `total_remote_groups` int DEFAULT '0',
  `total_new_groups` int DEFAULT '0',
  `total_conflicts_groups` int DEFAULT '0',
  `total_applied_users` int DEFAULT '0',
  `total_applied_groups` int DEFAULT '0',
  `summary` text,
  `created_at` datetime DEFAULT NULL,
  `created_by` int DEFAULT NULL,
  `finished_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `user_migration_outgoing_requests` (
  `id` int NOT NULL AUTO_INCREMENT,
  `exporter_url` varchar(500) NOT NULL,
  `exporter_name` varchar(255) DEFAULT NULL,
  `nonce` varchar(128) NOT NULL,
  `status` enum('pending','received','expired') DEFAULT 'pending',
  `requested_at` datetime DEFAULT NULL,
  `received_at` datetime DEFAULT NULL,
  `requested_by` int DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_exporter_url` (`exporter_url`(191))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `users` (
  `id` int NOT NULL AUTO_INCREMENT,
  `email` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `password_hash` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `full_name` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `category` enum('admin','regular','guest') CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'regular',
  `affiliation` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '所属',
  `is_active` tinyint(1) DEFAULT '1',
  `created_at` datetime NOT NULL,
  `updated_at` datetime NOT NULL,
  `deleted_at` datetime DEFAULT NULL COMMENT '削除日時（論理削除用）',
  `deleted_by` int DEFAULT NULL COMMENT '削除者のユーザーID',
  PRIMARY KEY (`id`),
  UNIQUE KEY `email` (`email`),
  KEY `idx_email` (`email`),
  KEY `deleted_by` (`deleted_by`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


SET FOREIGN_KEY_CHECKS = 1;
