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
slack_minutes.mrkdwn - Slack mrkdwn → Markdown 変換（v2.0 新設）

Slack が API で返す本文（mrkdwn）を，一般的な Markdown に復元する．
DB には Slack の生テキストをそのまま保存し，表示・出力のたびに
この関数で変換する（変換規則を後から改善しても，保存済みデータを
作り直す必要がない）．

変換内容
  <@U123> / <@U123|name>      → @表示名
  <#C123|name> / <#C123>      → #チャンネル名
  <!here> <!channel> <!everyone> → @here 等
  <!subteam^S123|@grp>        → @grp
  <!date^…|fallback>          → fallback
  <url|label> / <url>         → [label](url) / [url](url)
  <mailto:a|a>                → [a](mailto:a)
  *bold* _italic_ ~strike~    → **bold** *italic* ~~strike~~
  `code` ```block```          → そのまま（内部は変換しない）
  &amp; &lt; &gt;             → & < >
  • 箇条書き                  → - 箇条書き
  単独の改行                  → 行末2スペース（Markdown の強制改行）

resolve_user(user_id) / resolve_channel(channel_id) は呼び出し側が
渡す（DB キャッシュ付きの解決関数）．省略時は ID をそのまま出す．
"""
import html
import re

# ── 絵文字の最小対応表（リアクション表示用）────────────────
# 未収録のものは :name: のまま表示する
EMOJI = {
    '+1': '👍', 'thumbsup': '👍', '-1': '👎', 'thumbsdown': '👎',
    'heart': '❤️', 'heart_eyes': '😍', 'blue_heart': '💙',
    'tada': '🎉', 'eyes': '👀', 'pray': '🙏', 'clap': '👏',
    'smile': '😄', 'smiley': '😃', 'grin': '😁', 'grinning': '😀',
    'laughing': '😆', 'joy': '😂', 'rolling_on_the_floor_laughing': '🤣',
    'blush': '😊', 'wink': '😉', 'sweat_smile': '😅', 'relaxed': '☺️',
    'thinking_face': '🤔', 'sob': '😭', 'cry': '😢', 'astonished': '😲',
    'open_mouth': '😮', 'scream': '😱', 'fearful': '😨',
    'white_check_mark': '✅', 'heavy_check_mark': '✔️', 'x': '❌',
    'ok_hand': '👌', 'raised_hands': '🙌', 'wave': '👋', 'bow': '🙇',
    'muscle': '💪', 'fire': '🔥', 'star': '⭐', 'sparkles': '✨',
    '100': '💯', 'rocket': '🚀', 'bulb': '💡', 'memo': '📝',
    'book': '📖', 'books': '📚', 'coffee': '☕', 'beers': '🍻',
    'sun_with_face': '🌞', 'sunny': '☀️', 'cherry_blossom': '🌸',
    'exclamation': '❗', 'question': '❓', 'point_up': '☝️',
    'point_right': '👉', 'raised_hand': '✋', 'handshake': '🤝',
    'partying_face': '🥳', 'star-struck': '🤩', 'hugging_face': '🤗',
    'slightly_smiling_face': '🙂', 'upside_down_face': '🙃',
    'face_with_monocle': '🧐', 'nerd_face': '🤓', 'yum': '😋',
    'innocent': '😇', 'sunglasses': '😎', 'flushed': '😳',
    'confused': '😕', 'neutral_face': '😐', 'persevere': '😣',
    'sweat': '😓', 'weary': '😩', 'disappointed': '😞',
    'sparkling_heart': '💖', 'two_hearts': '💕', 'gift': '🎁',
    'birthday': '🎂', 'balloon': '🎈', 'confetti_ball': '🎊',
    'computer': '💻', 'robot_face': '🤖', 'brain': '🧠',
    'ballot_box_with_check': '☑️', 'seedling': '🌱', 'four_leaf_clover': '🍀',
    'ok': '🆗', 'new': '🆕', 'ng': '🆖', 'up': '🆙', 'cool': '🆒',
}


def emoji(name: str) -> str:
    """リアクション名 → 絵文字（未収録は :name:）．skin-tone 接尾辞は除去"""
    base = re.sub(r'::skin-tone-\d$', '', name or '')
    return EMOJI.get(base, f':{base}:')


# ── <…> トークン ───────────────────────────────────────────

_TOKEN_RE = re.compile(r'<([^<>]+)>')


def _convert_token(body: str, resolve_user, resolve_channel) -> str:
    """<…> の中身 body を Markdown 片に変換する"""
    if body.startswith('@'):
        uid, _, label = body[1:].partition('|')
        name = label or (resolve_user(uid) if resolve_user else uid)
        return f'@{name}'
    if body.startswith('#'):
        cid, _, label = body[1:].partition('|')
        name = label or (resolve_channel(cid) if resolve_channel else cid)
        return f'#{name}'
    if body.startswith('!'):
        inner = body[1:]
        if inner in ('here', 'channel', 'everyone'):
            return f'@{inner}'
        if inner.startswith('subteam^'):
            _, _, label = inner.partition('|')
            return label or '@group'
        if inner.startswith('date^'):
            _, _, fallback = inner.partition('|')
            return fallback or inner
        _, _, label = inner.partition('|')
        return label or f'@{inner}'
    # URL / mailto / tel
    url, _, label = body.partition('|')
    url = html.unescape(url)
    label = html.unescape(label) if label else ''
    if url.startswith('mailto:'):
        return f'[{label or url[7:]}]({url})'
    if url.startswith('tel:'):
        return label or url[4:]
    if label and label != url:
        return f'[{label}]({url})'
    return f'[{url}]({url})'


# ── 書式（コード外のみ）────────────────────────────────────

_BOLD_RE   = re.compile(r'(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])')
_ITALIC_RE = re.compile(r'(?<![\w_])_(?=\S)(.+?)(?<=\S)_(?![\w_])')
_STRIKE_RE = re.compile(r'(?<![\w~])~(?=\S)(.+?)(?<=\S)~(?![\w~])')
_BULLET_RE = re.compile(r'^(\s*)[•◦▪]\s+', re.M)
_CODE_SPLIT_RE = re.compile(r'(```.*?```|`[^`\n]+`)', re.S)


def _convert_plain(seg: str, resolve_user, resolve_channel) -> str:
    """コードではない区間の変換"""
    seg = _TOKEN_RE.sub(
        lambda m: _convert_token(m.group(1), resolve_user, resolve_channel),
        seg)
    seg = html.unescape(seg)
    seg = _BOLD_RE.sub(r'**\1**', seg)
    seg = _STRIKE_RE.sub(r'~~\1~~', seg)
    seg = _ITALIC_RE.sub(r'*\1*', seg)
    seg = _BULLET_RE.sub(r'\1- ', seg)
    return seg


def _convert_code(seg: str) -> str:
    """コード区間：Slack の実体参照だけ戻し，```直後に改行を補う"""
    seg = html.unescape(seg)
    if seg.startswith('```'):
        inner = seg[3:-3]
        if not inner.startswith('\n'):
            inner = '\n' + inner
        if not inner.endswith('\n'):
            inner = inner + '\n'
        return '```' + inner + '```'
    return seg


def _hard_breaks(md: str) -> str:
    """コードブロック外の単独改行を Markdown の強制改行にする"""
    out, in_code = [], False
    lines = md.split('\n')
    for i, line in enumerate(lines):
        if line.strip().startswith('```'):
            in_code = not in_code
            out.append(line)
            continue
        if in_code or i == len(lines) - 1 or not line.strip():
            out.append(line)
            continue
        nxt = lines[i + 1]
        if not nxt.strip():
            out.append(line)             # 次が空行なら段落区切り
        else:
            out.append(line.rstrip() + '  ')
    return '\n'.join(out)


def mrkdwn_to_md(text: str, resolve_user=None, resolve_channel=None) -> str:
    """Slack mrkdwn → Markdown"""
    if not text:
        return ''
    parts = _CODE_SPLIT_RE.split(text)
    conv = []
    for p in parts:
        if not p:
            continue
        if p.startswith('`'):
            conv.append(_convert_code(p))
        else:
            conv.append(_convert_plain(p, resolve_user, resolve_channel))
    return _hard_breaks(''.join(conv)).strip()


def mrkdwn_to_plain(text: str, resolve_user=None, resolve_channel=None) -> str:
    """Slack mrkdwn → 書式なしテキスト（目録の要約・検索用）"""
    if not text:
        return ''
    seg = _TOKEN_RE.sub(
        lambda m: _plain_token(m.group(1), resolve_user, resolve_channel),
        text)
    seg = html.unescape(seg)
    seg = seg.replace('```', '')
    seg = _BULLET_RE.sub(r'\1- ', seg)
    return seg.strip()


def _plain_token(body, resolve_user, resolve_channel):
    md = _convert_token(body, resolve_user, resolve_channel)
    m = re.match(r'\[(.*)\]\((.*)\)$', md)
    if m:
        return m.group(1)
    return md


def format_size(n) -> str:
    try:
        n = int(n)
    except Exception:
        return ''
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return f'{n:.0f}{unit}' if unit == 'B' else f'{n:.1f}{unit}'
        n /= 1024
    return f'{n:.1f}TB'
