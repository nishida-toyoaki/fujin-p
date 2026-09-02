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
fujinp.registry — アプリ正本（app_registry.json）の読み書き

正本は DB（app_share_registry / app_share_sections）にあり，アプシャの
「発行」で fujinp/app_registry.json に写される．kernel（app.py・admin・guest）
はこの JSON だけを読む．DB が読めない状態でも起動できるようにするため．

  読む側（起動時・DB不要）
    load_registry()                     JSON を dict で返す（壊れていれば空）
    register_blueprints(app)            enabled かつ kind='app' の Blueprint を登録
    launcher_sections(dashboard, ...)   ダッシュボードの区画とカードを返す
    public_cards()                      ログイン不要で使えるカード（ログイン画面用）

  使用コントローラー（2026-08-27，format_version 2）
    ランチャ（カード）1枚ごとに「使用区分」visibility を持つ．表示だけを制御する
    減算方式で，アプリ側の認可には関与しない．
      private        - admin だけ（admin ダッシュボードにのみ出る）
      public         - ログイン済みの全ユーザ（ゲストにも）
      domestic       - 構成員（regular）だけ
      group          - 指定グループ（groups）の有効所属者だけ
      domestic_group - 構成員または指定グループの有効所属者
      open           - ログインなしでも使える（ログイン画面にも並ぶ）
    dashboards（admin / guest）は「どのダッシュボードに置くか」という配置だけを
    表し，admin ダッシュボードでは区分を評価しない（管理者は全部見える）．
    区画（sections）は置き場所の見出しだけになり，区画側の表示条件は廃止した．
    旧形式（require_groups / require_categories / 区画条件）のカードは
    derive_visibility() で読み替えるので，移行前の JSON でも同じ表示になる．

  書く側（アプシャ・種まきスクリプト）
    build_registry_from_db(cursor)      DB から発行用 dict を組み立てる
    write_registry(data)                JSON を原子的に書く
    publish(cursor)                     build → write → published_at 更新
    reload_site()                       PythonAnywhere の WSGI を touch して再起動
"""

import os
import json
import datetime
import importlib
import logging
import tempfile

REGISTRY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app_registry.json')
FORMAT_VERSION = 2

JST = datetime.timezone(datetime.timedelta(hours=9))

_log = logging.getLogger('fujinp.registry')
_cache = {'mtime': None, 'data': None}

EMPTY = {'format_version': FORMAT_VERSION, 'generated_at': None, 'apps': [], 'sections': []}


# ============================================================
# 読む側
# ============================================================

def load_registry(force=False):
    """app_registry.json を読む．ファイルが無い・壊れている場合は空の正本を返す
    （起動を止めない）．mtime が変わっていなければキャッシュを返す．"""
    try:
        mtime = os.path.getmtime(REGISTRY_FILE)
    except OSError:
        _log.warning('registry: %s が見つかりません（アプリ登録なしで起動）', REGISTRY_FILE)
        return dict(EMPTY)
    if not force and _cache['data'] is not None and _cache['mtime'] == mtime:
        return _cache['data']
    try:
        with open(REGISTRY_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError('top-level is not an object')
        data.setdefault('apps', [])
        data.setdefault('sections', [])
    except Exception as e:
        _log.error('registry: %s の読み込みに失敗（%s）．空の正本で続行', REGISTRY_FILE, e)
        return dict(EMPTY)
    _cache['mtime'] = mtime
    _cache['data'] = data
    return data


def register_blueprints(app):
    """正本の Blueprint を app に登録する．
    ・kind='kernel' と enabled=0 は対象外（kernel は app.py が固定登録する）
    ・同名の Blueprint が登録済みならスキップ（並走期間の二重登録防止）
    ・1アプリの失敗は記録して次へ進む（1アプリの不具合でサイトを止めない）
    戻り値: (added, skipped, failed) それぞれ [(app_name, blueprint名), ...]"""
    reg = load_registry()
    added, skipped, failed = [], [], []
    for a in reg.get('apps', []):
        if a.get('kind') == 'kernel' or not a.get('enabled', True):
            continue
        for bp in a.get('blueprints') or []:
            label = f"{a.get('app_name')}:{bp.get('attr')}"
            try:
                mod = importlib.import_module(bp['module'])
                obj = getattr(mod, bp['attr'])
                if obj.name in app.blueprints:
                    skipped.append((a.get('app_name'), obj.name))
                    continue
                kwargs = {}
                if bp.get('url_prefix'):
                    kwargs['url_prefix'] = bp['url_prefix']
                app.register_blueprint(obj, **kwargs)
                added.append((a.get('app_name'), obj.name))
            except Exception as e:
                failed.append((a.get('app_name'), bp.get('attr'), str(e)))
                app.logger.error('registry: %s の登録に失敗: %s', label, e)
    app.logger.info('registry: 正本から %d 件追加，%d 件は登録済みのため省略，%d 件失敗',
                    len(added), len(skipped), len(failed))
    if failed:
        app.logger.error('registry: 失敗の内訳 %s', failed)
    return added, skipped, failed


# ============================================================
# 使用区分（使用コントローラー）
# ============================================================

VISIBILITY_KEYS = ('private', 'public', 'domestic', 'group', 'domestic_group', 'open')
VISIBILITY_LABELS = {
    'private':        '非公開（admin のみ）',
    'public':         'ゲストにも',
    'domestic':       '構成員だけ',
    'group':          'グループ',
    'domestic_group': '構成員＋グループ',
    'open':           '公開（ログイン不要）',
}
VISIBILITY_ICONS = {
    'private': '🔒', 'public': '🌐', 'domestic': '🏫',
    'group': '👥', 'domestic_group': '🏫👥', 'open': '🔓',
}


def derive_visibility(card, section=None):
    """旧形式（dashboards / require_* と区画条件）から使用区分を導く．
    カード条件と区画条件の積を取り，狭い方に倒す（移行で見える人が増えない）．
    戻り値: (visibility, groups, warnings)"""
    warnings = []
    if card.get('visibility') in VISIBILITY_KEYS:
        return card['visibility'], list(card.get('groups') or []), warnings
    section = section or {}
    dashboards = card.get('dashboards') or []
    if 'guest' not in dashboards or not section.get('show_guest', True):
        return 'private', [], warnings
    cats = set()
    for src in (card.get('require_categories') or [], section.get('require_categories') or []):
        if src:
            cats = set(src) if not cats else cats & set(src)
    cats.discard('admin')
    c_groups = [g for g in (card.get('require_groups') or []) if g]
    s_groups = [g for g in (section.get('require_groups') or []) if g]
    if c_groups and s_groups:
        both = [g for g in c_groups if g in s_groups]
        if both:
            groups = both
        else:
            groups = c_groups
            warnings.append('カードと区画のグループ条件が交わらないためカード側を採用')
    else:
        groups = c_groups or s_groups
    has_cat = bool(card.get('require_categories') or section.get('require_categories'))
    if has_cat and not cats:
        warnings.append('カテゴリ条件の積が空（admin 以外に見えていなかった）')
        return 'private', [], warnings
    regular_only = has_cat and cats == {'regular'}
    if has_cat and not regular_only:
        warnings.append(f'カテゴリ条件 {sorted(cats)} は表現できないため「ゲストにも」扱い')
    if groups and regular_only:
        warnings.append('「構成員 かつ グループ」は表現できないため「グループ」に')
        return 'group', groups, warnings
    if groups:
        return 'group', groups, warnings
    if regular_only:
        return 'domestic', [], warnings
    return 'public', [], warnings


def normalize_launcher(card, section=None):
    """カードを新形式に揃える（visibility / groups / params を持ち，require_* を持たない）"""
    vis, groups, _ = derive_visibility(card, section)
    out = {
        'dashboards': [d for d in (card.get('dashboards') or []) if d in ('admin', 'guest')],
        'section': card.get('section') or 'guest',
        'endpoint': card.get('endpoint') or '',
        'params': (card.get('params') or '').strip(),
        'label': card.get('label') or '',
        'icon': card.get('icon') or '',
        'description': card.get('description') or '',
        'sort_order': card.get('sort_order', 0),
        'visibility': vis,
        'groups': [g for g in groups if g],
        'extra_class': card.get('extra_class') or '',
    }
    return out


def card_visible(card, user_category, group_names=(), logged_in=True):
    """使用区分による可否．admin は常に可．未ログインは open だけ"""
    vis = card.get('visibility') or 'private'
    if not logged_in:
        return vis == 'open'
    if user_category == 'admin':
        return True
    if vis in ('open', 'public'):
        return True
    if vis == 'domestic':
        return user_category == 'regular'
    groups = card.get('groups') or []
    in_group = any(g in group_names for g in groups)
    if vis == 'group':
        return in_group
    if vis == 'domestic_group':
        return user_category == 'regular' or in_group
    return False


def _parse_params(text):
    """'doc_id=8&x=y' → {'doc_id': '8', 'x': 'y'}"""
    out = {}
    for part in (text or '').split('&'):
        if '=' in part:
            k, v = part.split('=', 1)
            if k.strip():
                out[k.strip()] = v.strip()
    return out


def _card_dict(a, c, sec_map):
    """発行 JSON のカード → 描画用 dict（href 解決込み）．解決できなければ None"""
    from flask import url_for, current_app
    vis, groups, _ = derive_visibility(c, sec_map.get(c.get('section')))
    try:
        href = url_for(c['endpoint'], **_parse_params(c.get('params')))
    except Exception as e:
        current_app.logger.warning('registry: ランチャ %s（%s）を解決できません: %s',
                                   c.get('label'), c.get('endpoint'), e)
        return None
    return {
        'href': href,
        'label': c.get('label') or a.get('display_name') or a.get('app_name'),
        'icon': c.get('icon') or a.get('icon') or '📦',
        'description': c.get('description') or a.get('description') or '',
        'extra_class': c.get('extra_class') or '',
        'app_name': a.get('app_name'),
        'endpoint': c.get('endpoint'),
        'section': c.get('section'),
        'sort_order': c.get('sort_order', 0),
        'visibility': vis,
        'visibility_label': VISIBILITY_LABELS.get(vis, vis),
        'visibility_icon': VISIBILITY_ICONS.get(vis, ''),
        'groups': groups,
    }


def launcher_sections(dashboard, user_category=None, group_names=()):
    """ダッシュボードに描く区画とカードを返す．
    dashboard: 'admin' | 'guest' | 'public'
      admin  - dashboards に admin を含むカード全部（区分は評価しない）
      guest  - dashboards に guest を含み，使用区分を満たすカード
      public - 使用区分が open のカード（ログイン不要．ログイン画面用．配置は無視）
    区画は見出しと色だけで，カードが1枚も無い区画は出さない．
    戻り値: [{'key','title','css_class','cards':[...]}]"""
    reg = load_registry()
    group_names = list(group_names or [])
    sec_map = {s.get('key'): s for s in reg.get('sections', [])}
    sections = sorted(reg.get('sections', []), key=lambda s: s.get('sort_order', 0))
    if dashboard == 'public':
        sections = sections + [{'key': None, 'title': '', 'css_class': '', 'sort_order': 1e9}]
    out = []
    for sec in sections:
        cards = []
        for a in reg.get('apps', []):
            if not a.get('enabled', True):
                continue
            for c in a.get('launchers') or []:
                if dashboard == 'public':
                    if sec.get('key') is not None and c.get('section') != sec.get('key'):
                        continue
                    if sec.get('key') is None and c.get('section') in sec_map:
                        continue
                else:
                    if c.get('section') != sec.get('key'):
                        continue
                    if dashboard not in (c.get('dashboards') or []):
                        continue
                d = _card_dict(a, c, sec_map)
                if d is None:
                    continue
                if dashboard == 'public' and d['visibility'] != 'open':
                    continue
                if dashboard == 'guest' and not card_visible(d, user_category, group_names, True):
                    continue
                cards.append(d)
        if cards:
            cards.sort(key=lambda c: c['sort_order'])
            out.append({'key': sec.get('key'), 'title': sec.get('title'),
                        'css_class': sec.get('css_class', ''), 'cards': cards})
    return out


def public_cards():
    """ログイン不要（open）のカードを平らな一覧で返す（ログイン画面用）．
    同じ endpoint が複数の区画にあれば1枚にまとめる"""
    seen, out = set(), []
    for sec in launcher_sections('public'):
        for c in sec['cards']:
            key = (c['endpoint'], c['href'])
            if key in seen:
                continue
            seen.add(key)
            out.append(c)
    return out


def find_public_card(app_name):
    """ログイン不要で使えるカードを app_name で引く（guest.go 用）．無ければ None"""
    for c in public_cards():
        if c['app_name'] == app_name:
            return c
    return None


# ============================================================
# 書く側
# ============================================================

def _jload(v, default):
    if v is None or v == '':
        return default
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return default


def build_registry_from_db(cursor):
    """DB（app_share_registry / app_share_sections）から発行用 dict を組み立てる．
    cursor は dictionary=True のカーソル．"""
    cursor.execute("""
        SELECT app_name, display_name, icon, description, sort_order, kind, enabled,
               blueprints, launchers, version_id, version_confirmed_at
        FROM app_share_registry
        ORDER BY sort_order, id
    """)
    apps = []
    for r in cursor.fetchall():
        apps.append({
            'app_name': r['app_name'],
            'display_name': r['display_name'] or r['app_name'],
            'icon': r['icon'] or '📦',
            'description': r['description'] or '',
            'sort_order': float(r['sort_order'] or 0),
            'kind': r['kind'] or 'app',
            'enabled': bool(r['enabled']),
            'blueprints': _jload(r['blueprints'], []),
            'launchers': _jload(r['launchers'], []),
            'version_id': r['version_id'],
            'version_confirmed_at': (r['version_confirmed_at'].strftime('%Y-%m-%d %H:%M:%S')
                                     if r['version_confirmed_at'] else None),
        })
    cursor.execute("""
        SELECT section_key, title, css_class, sort_order, show_admin, show_guest,
               require_groups, require_categories
        FROM app_share_sections ORDER BY sort_order
    """)
    sections = []
    for r in cursor.fetchall():
        sections.append({
            'key': r['section_key'],
            'title': r['title'],
            'css_class': r['css_class'] or '',
            'sort_order': float(r['sort_order'] or 0),
            'show_admin': bool(r['show_admin']),
            'show_guest': bool(r['show_guest']),
            'require_groups': _jload(r['require_groups'], []),
            'require_categories': _jload(r['require_categories'], []),
        })
    return {
        'format_version': FORMAT_VERSION,
        'generated_at': datetime.datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S'),
        'note': 'FUJIN-P アプリ正本の写し．正本は app_share_registry / app_share_sections（アプシャ）．'
                '手で編集せず，アプシャの「発行」で再生成する．',
        'apps': apps,
        'sections': sections,
    }


def write_registry(data, path=REGISTRY_FILE):
    """JSON を原子的に書く（tmp に書いて os.replace）．書きかけを読まれないため．"""
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix='.app_registry.', suffix='.json', dir=d)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write('\n')
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    _cache['mtime'] = None  # 次の load で読み直す
    return path


def publish(cursor):
    """DB → app_registry.json．published_at を更新する（commit は呼び出し側）．"""
    data = build_registry_from_db(cursor)
    write_registry(data)
    cursor.execute("UPDATE app_share_registry SET published_at=%s",
                   (datetime.datetime.now(JST).replace(tzinfo=None),))
    return data


def wsgi_path():
    """PythonAnywhere の WSGI ファイル（/var/www/<user>_pythonanywhere_com_wsgi.py）"""
    home = os.path.expanduser('~')
    user = os.path.basename(home.rstrip('/'))
    return f'/var/www/{user}_pythonanywhere_com_wsgi.py'


def reload_site():
    """WSGI ファイルを touch して Web アプリを再起動する．成功なら True．"""
    p = wsgi_path()
    try:
        os.utime(p, None)
        return True
    except Exception as e:
        _log.warning('registry: WSGI の touch に失敗（%s）: %s', p, e)
        return False
