# SPDX-FileCopyrightText: 2024-2026 Toyoaki Nishida
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# This file is part of FUJIN-P.
# Source: https://github.com/nishida-toyoaki/fujin-p

"""
migrate_launcher_visibility.py — ランチャの使用区分（使用コントローラー）への移行

app_share_registry.launchers の各カードに visibility / groups / params を付け，
require_groups / require_categories を取り除く．区画（app_share_sections）の
表示条件はカード側に畳み込み，狭い方に倒す（移行で見える人は増えない）．
ログイン画面に固定で並べていた「ゲスト向け公開アプリ」は open にする．

使い方（ホームディレクトリで）
    python3 migrate_launcher_visibility.py            … 確認表を出すだけ（DB に書かない）
    python3 migrate_launcher_visibility.py --yes      … DB を書き換えて app_registry.json を発行

何度実行しても同じ結果になる（既に visibility を持つカードはそのまま）．
"""
import os
import re
import sys
import json
import argparse

HOME = os.path.dirname(os.path.abspath(__file__))
if HOME not in sys.path:
    sys.path.insert(0, HOME)

import mysql.connector
from db import DatabaseConfig
from fujinp import registry as R

try:
    from config import Config
except Exception:
    Config = None

# ログイン画面に固定で並べていたカード（guest.py の GUEST_APPS と login.html）
LOGIN_PAGE_OPEN = {
    'document_archive': None,
    'free_hand_curve': None,
    'ts_solvers': None,
    'tag_chase': 'TAG_CHASE_PUBLIC',      # config のフラグが真のときだけ open
    'sorakara': 'SORAKARA_PUBLIC',
}


def _jload(v, default):
    if v in (None, ''):
        return default
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return default


def _who_old(card, sec):
    """旧ロジックで guest ダッシュボードに見えていた人（文章）"""
    if 'guest' not in (card.get('dashboards') or []) or not sec.get('show_guest', True):
        return 'admin のみ'
    conds = []
    cats = set()
    for src in (card.get('require_categories') or [], sec.get('require_categories') or []):
        if src:
            cats = set(src) if not cats else cats & set(src)
    cats.discard('admin')
    if card.get('require_categories') or sec.get('require_categories'):
        conds.append('カテゴリ ' + '/'.join(sorted(cats)) if cats else 'カテゴリ条件が空')
    for g in (card.get('require_groups') or []):
        conds.append(f'グループ「{g}」')
    for g in (sec.get('require_groups') or []):
        conds.append(f'区画のグループ「{g}」')
    return ' かつ '.join(conds) if conds else 'ログイン済みの全員'


def _decorators(app_name, bp_modules, endpoint):
    """endpoint の関数に付いているデコレータを .py の字面から探す"""
    func = endpoint.split('.', 1)[-1]
    dirs = set()
    for m in bp_modules:
        parts = (m or '').split('.')
        if len(parts) >= 2 and parts[0] == 'fujinp':
            dirs.add(os.path.join(HOME, 'fujinp', parts[1]))
    dirs.add(os.path.join(HOME, 'fujinp', app_name))
    pat = re.compile(r'^\s*def\s+' + re.escape(func) + r'\s*\(')
    found = []
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for root, _, files in os.walk(d):
            if '__pycache__' in root:
                continue
            for fn in files:
                if not fn.endswith('.py'):
                    continue
                try:
                    lines = open(os.path.join(root, fn), encoding='utf-8', errors='replace').read().splitlines()
                except Exception:
                    continue
                for i, line in enumerate(lines):
                    if pat.match(line):
                        decos = []
                        j = i - 1
                        while j >= 0 and lines[j].strip().startswith('@'):
                            decos.insert(0, lines[j].strip())
                            j -= 1
                        found.append((os.path.relpath(os.path.join(root, fn), HOME), decos))
    if not found:
        return '（関数が見つからない）'
    out = []
    for path, decos in found:
        names = [d for d in decos if not d.startswith('@' + endpoint.split('.')[0] + '_bp')
                 and 'route' not in d]
        out.append(', '.join(names) if names else 'デコレータなし（route のみ）')
    return ' / '.join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--yes', action='store_true', help='DB を書き換えて発行する')
    args = ap.parse_args()

    conn = mysql.connector.connect(**DatabaseConfig.default())
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT section_key, show_admin, show_guest, require_groups, require_categories, title "
                "FROM app_share_sections")
    secs = {}
    for r in cur.fetchall():
        secs[r['section_key']] = {'key': r['section_key'], 'title': r['title'],
                                  'show_admin': bool(r['show_admin']), 'show_guest': bool(r['show_guest']),
                                  'require_groups': _jload(r['require_groups'], []),
                                  'require_categories': _jload(r['require_categories'], [])}
    cur.execute("SELECT app_name, enabled, blueprints, launchers FROM app_share_registry ORDER BY sort_order, id")
    rows = cur.fetchall()

    updates = []
    print('| アプリ | 区画 | カード | 配置 | 現在見える人（guest側） | 移行後 | グループ | 認可デコレータ | 注意 |')
    print('|---|---|---|---|---|---|---|---|---|')
    for r in rows:
        cards = _jload(r['launchers'], [])
        mods = [b.get('module') for b in _jload(r['blueprints'], [])]
        new_cards = []
        changed = False
        for c in cards:
            sec = secs.get(c.get('section'), {})
            already = c.get('visibility') in R.VISIBILITY_KEYS
            vis, groups, warns = R.derive_visibility(c, sec)
            # ログイン画面の固定カード → open
            flag = LOGIN_PAGE_OPEN.get(r['app_name'], 'no')
            if not already and flag != 'no':
                on = True if flag is None else bool(Config and getattr(Config, flag, False))
                if on:
                    vis = 'open'
                    warns.append('ログイン画面の固定カードだったので open')
                else:
                    warns.append(f'config の {flag} が偽なので open にしない')
            nc = R.normalize_launcher(dict(c, visibility=vis, groups=groups), sec)
            if nc != c:
                changed = True
            new_cards.append(nc)
            deco = _decorators(r['app_name'], mods, c.get('endpoint') or '')
            print(f"| {r['app_name']}{'' if r['enabled'] else '（無効）'} | {sec.get('title', c.get('section'))} | "
                  f"{c.get('label')} | {'+'.join(c.get('dashboards') or [])} | {_who_old(c, sec)} | "
                  f"{R.VISIBILITY_LABELS[vis]}{'（変更なし）' if already else ''} | "
                  f"{'・'.join(groups)} | {deco} | {'；'.join(warns)} |")
        if changed:
            updates.append((r['app_name'], new_cards))

    print()
    print(f'書き換え対象：{len(updates)} アプリ')
    if not args.yes:
        print('（確認だけ．書き換えるには --yes を付けて実行）')
        cur.close(); conn.close()
        return
    for app_name, cards in updates:
        cur.execute("UPDATE app_share_registry SET launchers=%s WHERE app_name=%s",
                    (json.dumps(cards, ensure_ascii=False), app_name))
    # 区画の表示条件は廃止（見出し・色・順だけ残す）
    cur.execute("UPDATE app_share_sections SET show_admin=1, show_guest=1, "
                "require_groups=JSON_ARRAY(), require_categories=JSON_ARRAY()")
    conn.commit()
    data = R.publish(cur)
    conn.commit()
    cur.close(); conn.close()
    print(f"書き換えて発行しました（apps={len(data['apps'])}）．Web タブで Reload してください．")


if __name__ == '__main__':
    main()
