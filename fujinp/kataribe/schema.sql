-- This file is part of FUJIN-P.
-- SPDX-FileCopyrightText: 2024-2026 Toyoaki Nishida
-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Source: https://github.com/nishida-toyoaki/fujin-p

-- かたりべ (kataribe) テーブル定義
-- 実行先: <owner>$default データベース

CREATE TABLE IF NOT EXISTS kataribe_presentations (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL COMMENT 'ユーザーID（users.id）',
    title VARCHAR(200) NOT NULL COMMENT 'プレゼン題名',
    spec_json MEDIUMTEXT COMMENT 'スペック（シーン・ブロック・語り）のJSON',
    created_at DATETIME COMMENT '作成日時（JST）',
    updated_at DATETIME COMMENT '更新日時（JST）',
    INDEX idx_user (user_id),
    INDEX idx_updated (updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
