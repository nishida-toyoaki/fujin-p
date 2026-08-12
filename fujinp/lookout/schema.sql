-- lookout（みはらし） テーブル定義
-- 実行先: <owner>$default データベース

CREATE TABLE IF NOT EXISTS lookout_history (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL COMMENT 'ユーザーID（users.id）',
    lat DOUBLE NOT NULL COMMENT '緯度（十進度）',
    lon DOUBLE NOT NULL COMMENT '経度（十進度）',
    ground_elev DOUBLE COMMENT '地表標高（m，10m DEM由来）',
    created_at DATETIME COMMENT '閲覧日時（JST）',
    INDEX idx_user (user_id),
    INDEX idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
