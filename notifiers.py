# SPDX-FileCopyrightText: 2024-2026 Toyoaki Nishida
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# This file is part of FUJIN-P.
# Copyright (C) 2024-2026 Toyoaki Nishida
#
# FUJIN-P is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# FUJIN-P is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with FUJIN-P.  If not, see <https://www.gnu.org/licenses/>.
#
# Source: https://github.com/nishida-toyoaki/fujin-p

"""
notifiers.py
FUJIN-P 共通 Slack 通知モジュール（基盤サブシステム・このファイル1つで完結）。

配置：
    FUJIN-P トップレベル（config.py / db.py と同じディレクトリ）。
    各アプリ（ethics_review / ext_engagement / 今後の新アプリ）の
    routes.py・スケジュールスクリプトのどこからでも
        from notifiers import notify_dm, notify_channel
    の一行で使える。既存アプリ内の slack_notifier.py は置き換えない
    （それらはアプリ独立性原則のもとで従来どおり動き続ける）。

提供する関数（送信は単機能・2本のみ）：
    notify_dm(email, text, send_at=None, log_label='DM',
              sender=None, app=None)
        … 個人宛 DM。宛先はメールアドレスで指定
          （FUJIN-P と Slack で同じメールアドレスを使っている前提。
           既存アプリと同じメールアドレス照合方式）。
    notify_channel(channel_id, text, send_at=None, log_label='チャンネル通知',
                   sender=None, app=None)
        … チャンネル宛て投稿。宛先はチャンネル ID（C... で始まる文字列）で指定。

通知台帳（このファイル後半・notify_ledger テーブル）：
    2関数とも、送信のたびに成否を問わず共通台帳 notify_ledger へ
    自動で1行記録する（呼び出し側の作業は不要）。
    テーブルは初回の記録時に CREATE TABLE IF NOT EXISTS で自動作成する。
    sender（発送した人のメール等。自動送信なら None）と
    app（発信元アプリ名。例 'ethics_review'）を渡しておくと、
    将来の個人ダッシュボードで「発送した通知」「届いた通知」を
    そのまま引ける。参照用に ledger_fetch_for_user / ledger_fetch_recent を
    用意してある。台帳記録の失敗は送信結果に影響しない。
    古い記録の削除（保存期間ポリシー）は将来の課題。当面は全件残す。

発信時刻（send_at）：
    None                     … 即時送信（chat.postMessage）
    datetime.datetime        … その時刻に 1 回だけ送信（chat.scheduleMessage）
                               naive な datetime は JST とみなす。
                               tzinfo 付きならそのタイムゾーンに従う。
    int / float              … UNIX タイムスタンプ（秒）として扱う。
    ※ Slack の仕様上、指定時刻は「現在より後〜120日以内」であること。
      過去の時刻は time_in_past エラーになる。
    ※ 予約送信の成功時は戻り値に scheduled_message_id を含める。
      取り下げに伴う予約キャンセル（chat.deleteScheduledMessage）は
      上位機能として次段階で扱う。呼び出し側は必要なら
      scheduled_message_id を保存しておくこと（台帳にも記録される）。

設計原則（既存の slack_notifier.py / notify_log.py と同じ）：
    「失敗しても呼び出し元を巻き込まない」ことを最優先とする。
    送信2関数も台帳記録も例外を投げない。失敗は必ず
    {'ok': False, 'error': '...'}（台帳は False）で表現する。
    本処理（申請・審査・カレンダー更新）は通知の成否に関わらず成立させる。
    手動起点・自動起点のどちらから呼んでもよい。

事前準備（Slack 側）：
    1. Bot Token (xoxb-...) を用意（既存アプリと同じ Bot を流用してよい）
    2. スコープ：chat:write（投稿・予約投稿）、
                 users:read.email（メール→ユーザー検索）、
                 im:write（DM チャンネルを開く）
    3. 投稿先チャンネルに Bot を /invite しておく（プライベートは必須）

設定（config / db 経由。コードに直書きしない）：
    SLACK_BOT_TOKEN : xoxb-...（既存アプリと共通）
    DB 接続         : db.py の DatabaseConfig.default()（check_deadlines.py と同じ）
    ※ チャンネル ID は config に持たせず引数で渡す。どのチャンネルを
      使うかは各アプリの判断であり、この基盤モジュールは関知しない。
"""
import datetime
import logging

try:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
    _SLACK_SDK_AVAILABLE = True
except ImportError:
    _SLACK_SDK_AVAILABLE = False
    logging.warning("notifiers: slack_sdk が見つかりません。"
                    "Slack 通知は無効化されます（pip install slack_sdk）。")

# FUJIN-P の標準タイムゾーン（naive datetime は JST とみなす）
JST = datetime.timezone(datetime.timedelta(hours=9))

# Slack API のエラーコード → 日本語の説明
_ERROR_JA = {
    'channel_not_found': 'チャンネルが見つかりません（チャンネルIDを確認してください）',
    'not_in_channel':    'Bot がそのチャンネルに参加していません（/invite で招待してください）',
    'is_archived':       'そのチャンネルはアーカイブ済みです',
    'invalid_auth':      'トークンが無効です（SLACK_BOT_TOKEN を確認してください）',
    'token_revoked':     'トークンが失効しています',
    'missing_scope':     'トークンに必要なスコープがありません'
                         '（chat:write / users:read.email / im:write を確認）',
    'account_inactive':  'Bot アカウントが無効化されています',
    'ratelimited':       'レート制限中です（しばらく待って再試行してください）',
    'users_not_found':   'このメールアドレスのSlackユーザーが見つかりません',
    'time_in_past':      '指定した発信時刻が過去です（未来の時刻を指定してください）',
    'time_too_far':      '指定した発信時刻が遠すぎます（120日以内にしてください）',
}


# ════════════════════════════════════════════════════════════════
# 内部ヘルパ（外から呼ばない）
# ════════════════════════════════════════════════════════════════

def _load_token():
    """config から SLACK_BOT_TOKEN を読む。読めなければ None。"""
    try:
        from config import Config
        return getattr(Config, 'SLACK_BOT_TOKEN', None)
    except Exception as e:
        logging.error("notifiers: config 読み込み失敗: %s", e)
        return None


def _to_post_at(send_at):
    """
    send_at を Slack の post_at（UNIX 秒・int）に変換する。

    Returns:
        (post_at, None)  … 変換成功。即時送信なら (None, None)
        (None, 'エラー') … 変換失敗
    """
    if send_at is None:
        return None, None
    if isinstance(send_at, bool):          # bool は int の亜種なので先に弾く
        return None, '発信時刻の指定が不正です'
    if isinstance(send_at, (int, float)):
        return int(send_at), None
    if isinstance(send_at, datetime.datetime):
        if send_at.tzinfo is None:
            send_at = send_at.replace(tzinfo=JST)   # naive は JST とみなす
        return int(send_at.timestamp()), None
    return None, ('発信時刻は None（即時）・datetime・UNIX秒 の'
                  'いずれかで指定してください')


def _slack_error(e, where):
    """SlackApiError を {'ok': False, ...} に変換し、ログも残す。"""
    err = e.response.get('error', str(e)) if e.response else str(e)
    logging.error("notifiers.%s error: %s", where, err)
    return {'ok': False, 'error': _ERROR_JA.get(err, f'Slack APIエラー: {err}')}


def _post_or_schedule(client, channel, text, post_at):
    """
    channel（C.../D...）へ即時投稿または予約投稿する共通処理。

    Returns:
        即時成功: {'ok': True, 'ts': '...', 'channel': '...'}
        予約成功: {'ok': True, 'scheduled_message_id': '...',
                   'post_at': 1234567890, 'channel': '...'}
    """
    if post_at is None:
        resp = client.chat_postMessage(channel=channel, text=text,
                                       link_names=True)
        return {'ok': True,
                'ts': resp.get('ts', ''),
                'channel': resp.get('channel', '')}
    resp = client.chat_scheduleMessage(channel=channel, text=text,
                                       post_at=post_at, link_names=True)
    return {'ok': True,
            'scheduled_message_id': resp.get('scheduled_message_id', ''),
            'post_at': resp.get('post_at', post_at),
            'channel': resp.get('channel', '')}


def _precheck(text, send_at):
    """
    SDK・config・引数の共通チェック。

    Returns:
        (token, post_at, None)      … 問題なし
        (None, None, {'ok': False}) … 問題あり（そのまま返せる辞書）
    """
    if not _SLACK_SDK_AVAILABLE:
        return None, None, {'ok': False, 'error': 'slack_sdk が未インストールです'}
    if not text:
        return None, None, {'ok': False, 'error': '投稿本文が空です'}
    token = _load_token()
    if not token:
        return None, None, {'ok': False,
                            'error': 'SLACK_BOT_TOKEN が設定されていません'}
    post_at, err = _to_post_at(send_at)
    if err:
        return None, None, {'ok': False, 'error': err}
    return token, post_at, None


# ════════════════════════════════════════════════════════════════
# 送信本体（内部関数。公開関数から呼ばれる）
# ════════════════════════════════════════════════════════════════

def _send_dm(email, text, send_at, log_label):
    """notify_dm の送信本体。戻り値は notify_dm と同じ辞書。"""
    token, post_at, bad = _precheck(text, send_at)
    if bad:
        logging.warning("notifiers: %s — %s", log_label, bad['error'])
        return bad
    if not email:
        return {'ok': False, 'error': 'メールアドレスが空です'}

    try:
        client = WebClient(token=token)
        # メールアドレス → Slack ユーザー ID
        user = (client.users_lookupByEmail(email=email).get('user') or {})
        uid = user.get('id')
        if not uid:
            return {'ok': False, 'error': 'ユーザーIDを取得できませんでした'}
        # DM チャンネルを開いて投稿（即時 or 予約）
        dm = (client.conversations_open(users=uid).get('channel') or {}).get('id')
        if not dm:
            return {'ok': False, 'error': 'DMチャンネルを開けませんでした'}
        return _post_or_schedule(client, dm, text, post_at)
    except SlackApiError as e:
        result = _slack_error(e, 'notify_dm')
        logging.warning("notifiers: %s の送信に失敗 — %s",
                        log_label, result['error'])
        return result
    except Exception as e:
        logging.error("notifiers.notify_dm unexpected error: %s", e)
        return {'ok': False, 'error': f'DM送信に失敗しました: {e}'}


def _send_channel(channel_id, text, send_at, log_label):
    """notify_channel の送信本体。戻り値は notify_channel と同じ辞書。"""
    token, post_at, bad = _precheck(text, send_at)
    if bad:
        logging.warning("notifiers: %s — %s", log_label, bad['error'])
        return bad
    if not channel_id:
        return {'ok': False, 'error': '投稿先チャンネルIDが設定されていません'}

    try:
        client = WebClient(token=token)
        return _post_or_schedule(client, channel_id, text, post_at)
    except SlackApiError as e:
        result = _slack_error(e, 'notify_channel')
        logging.warning("notifiers: %s の送信に失敗 — %s",
                        log_label, result['error'])
        return result
    except Exception as e:
        logging.error("notifiers.notify_channel unexpected error: %s", e)
        return {'ok': False, 'error': f'通知送信に失敗しました: {e}'}


# ════════════════════════════════════════════════════════════════
# 通知台帳（notify_ledger テーブル）
#
#   送った通知を、成否を問わずすべて1行ずつ記録する。
#   アプリ別テーブル（ext_engagement_notify_log 等）は置き換えない。
#   あちらはアプリの案件画面用、こちらは FUJIN-P 全体の台帳。
#
#   台帳には「誰が発送したか（sender）」「誰に／どこに届くか（target）」を
#   残すので、将来の個人ダッシュボードで
#     - 自分が発送した通知 … sender = 本人メール
#     - 自分に届いた通知   … target_kind='dm' かつ target = 本人メール
#   をそのまま引ける。チャンネル宛ては個人に展開できないため、
#   所属チャンネルの通知として別枠で表示する想定。
#
#   「記録に失敗しても呼び出し元を巻き込まない」：例外は投げない。
# ════════════════════════════════════════════════════════════════

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS notify_ledger (
    id                   BIGINT       NOT NULL AUTO_INCREMENT,
    created_at           DATETIME     NOT NULL,        -- 記録時刻（JST）
    app                  VARCHAR(64)  NULL,            -- 発信元アプリ（例 'ethics_review'）
    kind                 VARCHAR(128) NULL,            -- 通知の種類（log_label）
    target_kind          VARCHAR(16)  NOT NULL,        -- 'dm' / 'channel'
    target               VARCHAR(255) NOT NULL,        -- メールアドレス or チャンネルID
    sender               VARCHAR(255) NULL,            -- 発送者（メール等）。自動送信は NULL
    text                 TEXT         NULL,            -- 本文（全文を残す）
    send_at              DATETIME     NULL,            -- 予約発信時刻（JST）。即時は NULL
    status               VARCHAR(16)  NOT NULL,        -- 'success' / 'failed'
    error                VARCHAR(255) NULL,            -- 失敗時のエラー内容
    slack_ts             VARCHAR(32)  NULL,            -- 即時送信の Slack ts
    scheduled_message_id VARCHAR(64)  NULL,            -- 予約送信の ID（キャンセルに使える）
    slack_channel        VARCHAR(32)  NULL,            -- 実際の投稿先（C.../D...）
    PRIMARY KEY (id),
    KEY idx_target  (target_kind, target),
    KEY idx_sender  (sender),
    KEY idx_created (created_at)
) CHARACTER SET utf8mb4
"""

# CREATE TABLE IF NOT EXISTS を毎回投げないためのプロセス内フラグ
_table_ready = False


def _ledger_connect():
    """
    FUJIN-P 共通の作法で DB 接続を開く（check_deadlines.py と同じ）。
    Returns: conn または None（例外は投げない）
    """
    try:
        import mysql.connector
        from db import DatabaseConfig
        return mysql.connector.connect(**DatabaseConfig.default())
    except Exception as e:
        logging.warning("notifiers(台帳): DB接続に失敗: %s", e)
        return None


def _ensure_table(conn):
    """台帳テーブルが無ければ作る。プロセス内で1回だけ実行。"""
    global _table_ready
    if _table_ready:
        return
    cur = conn.cursor()
    cur.execute(_TABLE_SQL)
    conn.commit()
    cur.close()
    _table_ready = True


def _now_jst():
    """JST の現在時刻（naive datetime。DATETIME カラム用）。"""
    return datetime.datetime.now(JST).replace(tzinfo=None)


def _post_at_to_jst(post_at):
    """UNIX 秒（またはNone）→ JST naive datetime（またはNone）。"""
    if post_at is None:
        return None
    return datetime.datetime.fromtimestamp(int(post_at), JST).replace(tzinfo=None)


def _record_ledger(target_kind, target, text, result, send_at,
                   sender, app, kind):
    """
    送信結果を台帳へ1行記録する内部処理。公開2関数が送信のたびに呼ぶ。
    どんな失敗もここで握り、送信結果（result）には影響させない。

    Returns: 記録成功なら True、失敗なら False。例外は投げない。
    """
    try:
        conn = _ledger_connect()
        if conn is None:
            return False
        try:
            _ensure_table(conn)
            post_at, _err = _to_post_at(send_at)   # 変換不能なら None（即時扱い）
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO notify_ledger
                    (created_at, app, kind, target_kind, target, sender,
                     text, send_at, status, error,
                     slack_ts, scheduled_message_id, slack_channel)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                _now_jst(), app, kind, target_kind, target or '', sender,
                text, _post_at_to_jst(post_at),
                'success' if result.get('ok') else 'failed',
                (str(result.get('error'))[:255] if result.get('error') else None),
                result.get('ts'),
                result.get('scheduled_message_id'),
                result.get('channel'),
            ))
            conn.commit()
            cur.close()
            return True
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:
        logging.warning("notifiers: 台帳記録に失敗（target=%s, kind=%s）: %s",
                        target, kind, e)
        return False


# ── 参照系（将来の個人ダッシュボード用の下ごしらえ。送信側は使わない） ──

def ledger_fetch_for_user(email, limit=100):
    """
    ある人に関係する通知記録を新しい順に返す。
      - 発送した通知 … sender = email
      - 届いた通知   … target_kind='dm' かつ target = email

    Returns:
        list[dict]（失敗時は空リスト。例外は投げない）
        各 dict に direction キーを付ける（'sent' / 'received'）。
    """
    conn = _ledger_connect()
    if conn is None:
        return []
    try:
        _ensure_table(conn)
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT *,
                   CASE WHEN sender = %s THEN 'sent' ELSE 'received' END
                       AS direction
            FROM notify_ledger
            WHERE sender = %s
               OR (target_kind = 'dm' AND target = %s)
            ORDER BY created_at DESC, id DESC
            LIMIT %s
        """, (email, email, email, int(limit)))
        rows = cur.fetchall()
        cur.close()
        return rows
    except Exception as e:
        logging.warning("notifiers.ledger_fetch_for_user 失敗（%s）: %s",
                        email, e)
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def ledger_fetch_recent(limit=100):
    """
    直近の通知記録を新しい順に返す（管理・デバッグ用）。
    Returns: list[dict]（失敗時は空リスト。例外は投げない）
    """
    conn = _ledger_connect()
    if conn is None:
        return []
    try:
        _ensure_table(conn)
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT * FROM notify_ledger
            ORDER BY created_at DESC, id DESC
            LIMIT %s
        """, (int(limit),))
        rows = cur.fetchall()
        cur.close()
        return rows
    except Exception as e:
        logging.warning("notifiers.ledger_fetch_recent 失敗: %s", e)
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════
# 公開関数（この2本だけを使う）＝送信＋台帳記録
# ════════════════════════════════════════════════════════════════

def notify_dm(email, text, send_at=None, log_label='DM',
              sender=None, app=None):
    """
    個人宛 DM を 1 件送り（即時または指定時刻に1回）、共通台帳に記録する。

    メールアドレスから Slack ユーザーを引き当て（users.lookupByEmail）、
    Bot との DM チャンネルを開き（conversations.open）、そこへ投稿する。

    Args:
        email     : 送り先の人の（FUJIN-P 上の）メールアドレス
        text      : 本文
        send_at   : None＝即時／datetime または UNIX 秒＝その時刻に1回
                    （naive datetime は JST とみなす）
        log_label : 通知の種類名。失敗ログと台帳の kind に使う
                    （例 '締切当日リマインドDM'）
        sender    : 発送した人（メール等）。自動送信なら None のままでよい
        app       : 発信元アプリ名（例 'ethics_review'）。台帳の絞り込みに使う

    Returns:
        即時成功: {'ok': True, 'ts': '...', 'channel': 'D...'}
        予約成功: {'ok': True, 'scheduled_message_id': '...',
                   'post_at': ..., 'channel': 'D...'}
        失敗    : {'ok': False, 'error': '日本語のエラーメッセージ'}

    例外は投げない。メール照合失敗（FUJIN-P と Slack でメールが食い違う）
    も {'ok': False} を返すだけで、呼び出し元の本処理は止めない。
    台帳記録は成否を問わず自動で行われ、失敗しても戻り値に影響しない。
    """
    result = _send_dm(email, text, send_at, log_label)
    _record_ledger('dm', email, text, result, send_at,
                   sender, app, log_label)
    return result


def notify_channel(channel_id, text, send_at=None, log_label='チャンネル通知',
                   sender=None, app=None):
    """
    チャンネル宛て通知を 1 件送り（即時または指定時刻に1回）、
    共通台帳に記録する。

    Args:
        channel_id : 投稿先チャンネル ID（C... で始まる文字列。
                     各アプリが config に持つ自分用チャンネル ID を渡す）
        text       : 本文。@channel メンションを含めたい場合は
                     呼び出し側で本文に '<!channel>' を埋め込む
        send_at    : None＝即時／datetime または UNIX 秒＝その時刻に1回
                     （naive datetime は JST とみなす）
        log_label  : 通知の種類名。失敗ログと台帳の kind に使う
                     （例 '依頼受付通知'）
        sender     : 発送した人（メール等）。自動送信なら None のままでよい
        app        : 発信元アプリ名（例 'ext_engagement'）

    Returns:
        即時成功: {'ok': True, 'ts': '...', 'channel': 'C...'}
        予約成功: {'ok': True, 'scheduled_message_id': '...',
                   'post_at': ..., 'channel': 'C...'}
        失敗    : {'ok': False, 'error': '日本語のエラーメッセージ'}

    例外は投げない。台帳記録は成否を問わず自動で行われ、
    失敗しても戻り値に影響しない。
    """
    result = _send_channel(channel_id, text, send_at, log_label)
    _record_ledger('channel', channel_id, text, result, send_at,
                   sender, app, log_label)
    return result