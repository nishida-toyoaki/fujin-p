-- あわみ（awami / our_meeting）テーブル定義（最終形）
-- 実行先: <owner>$default データベース
-- 共通テーブル users / user_groups 群はプラットフォーム既存のため作成しない

CREATE TABLE IF NOT EXISTS awami_canvases (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(200) NOT NULL COMMENT 'キャンバス名',
    description TEXT COMMENT '説明',
    access_policy VARCHAR(20) NOT NULL DEFAULT 'private'
        COMMENT 'public/domestic/private/group/domestic_group',
    owner_user_id INT NOT NULL COMMENT '作成者（users.id）＝講師／司会者',
    created_at DATETIME COMMENT '作成日時（JST）',
    updated_at DATETIME COMMENT '更新日時（JST）',
    INDEX idx_owner (owner_user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS awami_canvas_access_groups (
    id INT AUTO_INCREMENT PRIMARY KEY,
    canvas_id INT NOT NULL COMMENT 'awami_canvases.id',
    group_id INT NOT NULL COMMENT 'user_groups.id',
    INDEX idx_canvas (canvas_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS awami_nodes (
    id INT AUTO_INCREMENT PRIMARY KEY,
    canvas_id INT NOT NULL COMMENT 'awami_canvases.id',
    label VARCHAR(200) NOT NULL COMMENT 'ナラティブ素名（表示ラベル）',
    url VARCHAR(500) COMMENT '実体URL（MD文書等）',
    note TEXT COMMENT '注記',
    access_policy VARCHAR(20) NULL
        COMMENT 'NULL=キャンバスに従う/public/domestic/private/group/domestic_group',
    x DOUBLE NOT NULL DEFAULT 0 COMMENT 'キャンバス座標X（手動配置）',
    y DOUBLE NOT NULL DEFAULT 0 COMMENT 'キャンバス座標Y（手動配置）',
    created_by INT COMMENT '作成者（users.id）',
    created_at DATETIME COMMENT '作成日時（JST）',
    updated_at DATETIME COMMENT '更新日時（JST）',
    INDEX idx_canvas (canvas_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS awami_node_access_groups (
    id INT AUTO_INCREMENT PRIMARY KEY,
    node_id INT NOT NULL COMMENT 'awami_nodes.id',
    group_id INT NOT NULL COMMENT 'user_groups.id',
    INDEX idx_node (node_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS awami_connector_types (
    id INT AUTO_INCREMENT PRIMARY KEY,
    category VARCHAR(50) NOT NULL COMMENT '分類（時間・因果・意図…）',
    name VARCHAR(100) NOT NULL COMMENT '結合子名（その前に 等）',
    directed TINYINT(1) NOT NULL DEFAULT 1 COMMENT '向きあり（主→従）か',
    sort_order INT NOT NULL DEFAULT 0 COMMENT '表示順',
    is_active TINYINT(1) NOT NULL DEFAULT 1 COMMENT '有効フラグ',
    INDEX idx_active (is_active, sort_order)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS awami_edges (
    id INT AUTO_INCREMENT PRIMARY KEY,
    canvas_id INT NOT NULL COMMENT 'awami_canvases.id',
    connector_type_id INT NOT NULL COMMENT 'awami_connector_types.id',
    note TEXT COMMENT '注記',
    created_at DATETIME COMMENT '作成日時（JST）',
    updated_at DATETIME COMMENT '更新日時（JST）',
    INDEX idx_canvas (canvas_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS awami_edge_members (
    id INT AUTO_INCREMENT PRIMARY KEY,
    edge_id INT NOT NULL COMMENT 'awami_edges.id',
    node_id INT NOT NULL COMMENT 'awami_nodes.id',
    position INT NOT NULL COMMENT '順位（1=主）',
    INDEX idx_edge (edge_id),
    INDEX idx_node (node_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 結合子の初期語彙（重複投入を避けるため一度だけ実行）
INSERT INTO awami_connector_types (category, name, directed, sort_order) VALUES
('時間', 'その前に', 1, 10), ('時間', 'その後に', 1, 11), ('時間', '同時に', 0, 12),
('因果', '原因となる', 1, 20), ('因果', '結果として生じる', 1, 21),
('意図', '目的とする', 1, 30), ('意図', '動機となる', 1, 31),
('対立', '妨げる', 1, 40), ('対立', '反する', 0, 41), ('対立', '覆す', 1, 42),
('展開', '可能にする', 1, 50), ('展開', '促進する', 1, 51), ('展開', '転機となる', 1, 52),
('解釈', '意味する', 1, 60), ('解釈', '象徴する', 1, 61), ('解釈', '～として受け止められる', 1, 62),
('感情', '喜びをもたらす', 1, 70), ('感情', '不安を生む', 1, 71),
('仮想', 'もし～なら', 1, 80), ('仮想', '～であり得た', 0, 81),
('メタ', '形而上的な説明', 1, 90), ('具体', '具体例', 1, 100);

-- ── あわみ拡張：意見募集（online voting / write-in）──────────────
CREATE TABLE IF NOT EXISTS awami_polls (
    id INT AUTO_INCREMENT PRIMARY KEY,
    canvas_id INT NOT NULL COMMENT 'awami_canvases.id',
    token VARCHAR(16) NOT NULL COMMENT '参加者URL用ランダム12桁トークン',
    choice_question VARCHAR(500) COMMENT 'n択の問い（NULL可）',
    writein_question VARCHAR(500) COMMENT 'write-inの問い（NULL可）',
    status ENUM('open','closed') NOT NULL DEFAULT 'open',
    created_by INT COMMENT '作成者（講師／司会者）users.id',
    created_at DATETIME COMMENT '作成日時（JST）',
    closed_at DATETIME COMMENT '締切日時（JST）',
    UNIQUE KEY uq_awami_token (token),
    INDEX idx_canvas_status (canvas_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS awami_poll_options (
    id INT AUTO_INCREMENT PRIMARY KEY,
    poll_id INT NOT NULL COMMENT 'awami_polls.id',
    opt_index INT NOT NULL COMMENT '選択肢番号（0始まり・登録順）',
    label VARCHAR(255) NOT NULL COMMENT '選択肢の表示',
    INDEX idx_poll (poll_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS awami_votes (
    id INT AUTO_INCREMENT PRIMARY KEY,
    poll_id INT NOT NULL COMMENT 'awami_polls.id',
    user_id INT NOT NULL COMMENT '投票者 users.id',
    opt_index INT NOT NULL COMMENT '選んだ選択肢番号',
    voted_at DATETIME COMMENT '投票日時（JST）',
    UNIQUE KEY uq_awami_vote (poll_id, user_id),
    INDEX idx_poll (poll_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='n択投票：一人一票（再投票は上書き）';

CREATE TABLE IF NOT EXISTS awami_writeins (
    id INT AUTO_INCREMENT PRIMARY KEY,
    poll_id INT NOT NULL COMMENT 'awami_polls.id',
    seq_no INT NOT NULL COMMENT '通し番号（poll内で1始まり）',
    user_id INT NOT NULL COMMENT '投稿者 users.id',
    content MEDIUMTEXT COMMENT 'write-in本文（MD）',
    created_at DATETIME COMMENT '投稿日時（JST）',
    INDEX idx_poll (poll_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='write-in：何度でも追記可・番号と時刻を全員に表示';
