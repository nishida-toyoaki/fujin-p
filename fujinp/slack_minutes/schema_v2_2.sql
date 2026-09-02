-- ============================================================
-- slack_minutes v2.2  スキーマ変更（v2.1 適用後に MySQL コンソールで実行）
-- 公開範囲をマイノート／コレポと同じ5区分にし，許可グループ表を追加する
-- ============================================================

-- 1) 既存行はすべて非公開（admin のみ）に揃える．公開は admin が画面で設定する
UPDATE slack_minutes_channels SET visibility = 'private';

-- 2) 列を 5 値の ENUM に（マイノートの共有キーと同じ）
ALTER TABLE slack_minutes_channels
  MODIFY COLUMN visibility ENUM('private','public','domestic','group','domestic_group')
    NOT NULL DEFAULT 'private'
    COMMENT '公開範囲：private=adminのみ / public=ゲストにも / domestic=構成員だけ / group=グループ / domestic_group=構成員＋グループ';

-- 3) 許可グループ（group / domestic_group のとき参照．まいぐる user_groups.id）
CREATE TABLE IF NOT EXISTS slack_minutes_access_groups (
  channel_id VARCHAR(32) NOT NULL COMMENT 'slack_minutes_channels.channel_id',
  group_id   INT         NOT NULL COMMENT 'user_groups.id',
  PRIMARY KEY (channel_id, group_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
