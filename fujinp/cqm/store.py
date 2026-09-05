"""倉庫（台帳）と，量子のあいだのやり取り —— ファイル＋メモリ版。

原理は三つだけ。

1. 倉庫にあるのは量子の行だけで，様式の語彙を持たない。
2. 親は子にパラメータ付きのデマンドを送り，子は〈状態，中身〉の対で答える。
3. 親は子の NG を握り潰さない。畳んで上へ運ぶ。

置き場：実行時は SQL を使わない。倉庫の全部（量子・並び・依頼・記録）を
fujinp/cqm/data/store.json 1本に持ち，起動時に読んでメモリに索引を作り，
編集のたびに一時ファイルへ書いて差し替える。プロセスが複数あっても
ファイルの更新時刻を見て読み直す。SQL は「格納」「読み込み」の明示の操作だけ。
利用者と まいぐる のグループは FUJIN-P の共通 DB（default）から引く。
"""

import json
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import db as _db_module

JST = timezone(timedelta(hours=9))
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
STORE_FILE = os.path.join(DATA_DIR, 'store.json')
BACKUP_DIR = os.path.join(DATA_DIR, 'backup')
LOG_CAP = 2000


def _now():
    """日時3層ルール：JST の DATETIME。"""
    return datetime.now(JST).replace(tzinfo=None)


def _ts():
    return _now().strftime('%Y-%m-%d %H:%M:%S')


# ================================================================ DB（共通DB と，明示の格納・読み込みだけ）

_HELPER_NAMES = ('get_db_cursor', 'get_db_connection', 'get_connection', 'get_conn', 'connect')


def _call_helper(database=None):
    last = None
    for name in _HELPER_NAMES:
        f = getattr(_db_module, name, None)
        if not callable(f):
            continue
        try:
            return f(database=database) if database else f()
        except TypeError as e:
            last = e
            for arg in ((database,) if database else ('default', None)):
                try:
                    return f(arg)
                except TypeError as e2:
                    last = e2
    raise RuntimeError('db.py に使える接続ヘルパが見つかりません: %s' % last)


@contextmanager
def _db(database=None):
    """(cursor, connection)。カーソルは辞書型。database='default' で共通 DB。"""
    obj = _call_helper(database)
    ctx = None
    if hasattr(obj, '__enter__') and not hasattr(obj, 'cursor'):
        ctx = obj
        obj = ctx.__enter__()
    if isinstance(obj, (tuple, list)):
        conn = obj[1]; owns = False
    else:
        conn = obj; owns = ctx is None
    cur = conn.cursor(dictionary=True)
    try:
        yield cur, conn
    finally:
        try:
            cur.close()
        except Exception:
            pass
        if ctx is not None:
            ctx.__exit__(None, None, None)
        elif owns:
            try:
                conn.close()
            except Exception:
                pass


# ================================================================ 倉庫の器（ファイル＋メモリ）

class _State:
    def __init__(self):
        self.d = None          # {'next_id', 'quanta': {id: q}, 'links': [...], 'requests': {id: r}, 'log': [...]}
        self.mtime = None
        self.lock = threading.RLock()
        self.dirty = False
        # 索引
        self.bykey = {}
        self.kids = {}         # parent_id -> [ {'child': id, 'ord': n}, ... ]（ord 順）


_S = _State()


def _empty():
    return {'next_id': 1, 'quanta': {}, 'links': [], 'requests': {}, 'log': [], 'saved_at': ''}


def _index():
    S = _S
    S.bykey = {}
    S.kids = {}
    for q in S.d['quanta'].values():
        if q.get('key_path'):
            S.bykey[q['key_path']] = q
    for ln in S.d['links']:
        S.kids.setdefault(ln['parent'], []).append(ln)
    for v in S.kids.values():
        v.sort(key=lambda x: (x.get('ord', 0), x.get('child', 0)))


def _load(force=False):
    """ファイルを読む（更新時刻が変わっていれば読み直す）。"""
    S = _S
    with S.lock:
        try:
            mt = os.path.getmtime(STORE_FILE)
        except OSError:
            mt = None
        if S.d is not None and not force and mt == S.mtime:
            return S.d
        if mt is None:
            S.d = _empty()
        else:
            with open(STORE_FILE, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            raw.setdefault('next_id', 1); raw.setdefault('quanta', {}); raw.setdefault('links', [])
            raw.setdefault('requests', {}); raw.setdefault('log', [])
            raw['quanta'] = {int(k): v for k, v in raw['quanta'].items()}
            raw['requests'] = {int(k): v for k, v in raw['requests'].items()}
            S.d = raw
        S.mtime = mt
        _index()
        return S.d


def _save():
    """一時ファイルへ書いて差し替える（途中で切れても壊れない）。1日1回は控えを取る。"""
    S = _S
    with S.lock:
        os.makedirs(DATA_DIR, exist_ok=True)
        S.d['saved_at'] = _ts()
        tmp = STORE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(S.d, f, ensure_ascii=False)
        os.replace(tmp, STORE_FILE)
        S.mtime = os.path.getmtime(STORE_FILE)
        try:
            os.makedirs(BACKUP_DIR, exist_ok=True)
            day = os.path.join(BACKUP_DIR, 'store_%s.json' % _now().strftime('%Y%m%d'))
            if not os.path.exists(day):
                with open(day, 'w', encoding='utf-8') as f:
                    json.dump(S.d, f, ensure_ascii=False)
        except OSError:
            pass


def D():
    return _load()


def _nid():
    d = D()
    n = d['next_id']
    d['next_id'] = n + 1
    return n


def store_info():
    d = D()
    try:
        size = os.path.getsize(STORE_FILE)
    except OSError:
        size = 0
    return {'file': STORE_FILE, 'bytes': size, 'quanta': len(d['quanta']), 'links': len(d['links']),
            'requests': len(d['requests']), 'saved_at': d.get('saved_at') or ''}


# ---------------------------------------------------------------- 返り値の対

class Answer:
    """〈状態，中身〉の対。status は 'ok' / 'partial' / 'ng'。

    pending には，畳み込まれた未達（誰の・どの箱が，なぜ）を積む。
    """

    def __init__(self, status, value=None, note='', pending=None):
        self.status = status
        self.value = value
        self.note = note
        self.pending = pending or []

    @property
    def ok(self):
        return self.status == 'ok'

    def as_dict(self):
        return {
            'status': self.status,
            'value': self.value,
            'note': self.note,
            'pending': self.pending,
        }


def ok(value=None, note='', pending=None):
    return Answer('ok', value, note, pending)


def partial(value=None, note='', pending=None):
    return Answer('partial', value, note, pending)


def ng(note='', value=None):
    return Answer('ng', value, note)


# ------------------------------------------------------------------ 倉庫の読み

def _q(qid):
    try:
        return D()['quanta'].get(int(qid))
    except (TypeError, ValueError):
        return None


def get_quantum(qid):
    q = _q(qid)
    return dict(q) if q else None


def get_by_key(key_path):
    D()
    q = _S.bykey.get(key_path)
    return dict(q) if q else None


def children(parent_id):
    D()
    out = []
    for ln in _S.kids.get(int(parent_id), []):
        q = _q(ln['child'])
        if q:
            r = dict(q); r['link_ord'] = ln.get('ord', 0); out.append(r)
    return out


def list_boxes(exclude_arena=True):
    d = D()
    rows = []
    for q in d['quanta'].values():
        if q.get('kind') != 'box':
            continue
        if exclude_arena and q.get('recipe') == 'arena':
            continue
        r = dict(q); r['n_children'] = len(_S.kids.get(q['id'], []))
        rows.append(r)
    rows.sort(key=lambda r: (r.get('key_path') or '\uffff', r['id']))
    return rows


def _loads(raw):
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return v if isinstance(v, dict) else {}


# ------------------------------------------------------------------ 倉庫の書き

def _new_quantum(kind, key_path=None, title=None, body=None, recipe=None, attrs=None, owner_id=None):
    d = D()
    q = {'id': _nid(), 'kind': kind, 'key_path': key_path or None, 'title': title,
         'body': body, 'recipe': recipe, 'attrs': dict(attrs or {}), 'owner_id': owner_id,
         'created_at': _ts(), 'updated_at': _ts()}
    d['quanta'][q['id']] = q
    if q['key_path']:
        _S.bykey[q['key_path']] = q
    return q


def _touch(q):
    q['updated_at'] = _ts()


def _link(parent_id, child_id, ord_=None):
    d = D()
    lst = _S.kids.setdefault(int(parent_id), [])
    if ord_ is None:
        ord_ = (max([x.get('ord', 0) for x in lst]) + 1) if lst else 1
    ln = {'parent': int(parent_id), 'child': int(child_id), 'ord': ord_}
    d['links'].append(ln); lst.append(ln)
    lst.sort(key=lambda x: (x.get('ord', 0), x.get('child', 0)))
    return ln


def add_item(parent_id, body, attrs=None, actor_id=None):
    """箇条をひとつ生やして，親の並びの末尾につなぐ。"""
    q = _new_quantum('item', body=body, attrs=attrs, owner_id=actor_id)
    _link(parent_id, q['id'])
    _save()
    return q['id']


def update_item(qid, body):
    q = _q(qid)
    if q:
        q['body'] = body; _touch(q); _save()


def unlink(parent_id, child_id):
    d = D()
    d['links'] = [x for x in d['links'] if not (x['parent'] == int(parent_id) and x['child'] == int(child_id))]
    _S.kids[int(parent_id)] = [x for x in _S.kids.get(int(parent_id), []) if x['child'] != int(child_id)]
    _save()


def unlink_all(parent_id):
    d = D()
    d['links'] = [x for x in d['links'] if x['parent'] != int(parent_id)]
    _S.kids[int(parent_id)] = []


def log(quantum_id, verb, params, answer, actor_id=None):
    d = D()
    d['log'].append({'quantum_id': quantum_id, 'verb': verb, 'params': params or {},
                     'status': answer.status, 'note': (answer.note or '')[:500],
                     'actor_id': actor_id, 'created_at': _ts()})
    if len(d['log']) > LOG_CAP:
        del d['log'][:len(d['log']) - LOG_CAP]
    # 記録だけでは保存しない（次の書き込みと一緒に落ちる）


# -------------------------------------------------------------------- 作法たち

RECIPES = {}


def recipe(name):
    def deco(cls):
        RECIPES[name] = cls
        return cls
    return deco


class Recipe:
    """作法。集める向きと配る向きを持つ。持たない向きは NG を返す。"""

    @staticmethod
    def gather(box, params):
        return ng('この作法は集める向きを持ちません')

    @staticmethod
    def distribute(box, value, params):
        return ng('この作法は配る向きを持ちません')


@recipe('seq')
class Seq(Recipe):
    """箇条を順に連ねる。配る向きでは，受け取った塊を行で割って子へ配る。"""

    @staticmethod
    def gather(box, params):
        kids = children(box['id'])
        pending = []
        lines = []
        for k in kids:
            if k['kind'] == 'item':
                body = (k['body'] or '').strip()
                if body:
                    lines.append({'id': k['id'], 'body': body,
                                  'attrs': k.get('attrs') or {}})
                else:
                    pending.append({'key': name_of(box), 'child': k['id'],
                                    'why': '空の箇条があります'})
            else:
                sub = demand(k, 'gather', params)
                pending.extend(sub.pending)
                if sub.value:
                    lines.append({'id': k['id'], 'body': _flatten(sub.value),
                                  'attrs': k.get('attrs') or {}})
        if not lines:
            pending.append({'key': name_of(box), 'child': None,
                            'why': 'まだ一つも書かれていません'})
            return Answer('ng', [], 'まだ一つも書かれていません', pending)
        if pending:
            return partial(lines, '一部が未達です', pending)
        return ok(lines)

    @staticmethod
    def distribute(box, value, params):
        """編集で獲得した値を受け取り，どうするかは自分で決める。

        いまの決め方: 行で割って，先頭から既存の子に当て，余りは生やし，
        あふれた子は並びから外す。中身を消しはしない。
        """
        incoming = [ln.strip() for ln in (value or '').replace('\r', '').split('\n')]
        incoming = [ln.lstrip('・').strip() for ln in incoming if ln.strip()]
        kids = [k for k in children(box['id']) if k['kind'] == 'item']
        actor = params.get('actor_id')
        touched = 0
        for i, line in enumerate(incoming):
            if i < len(kids):
                if (kids[i]['body'] or '').strip() != line:
                    update_item(kids[i]['id'], line)
                    touched += 1
            else:
                add_item(box['id'], line,
                         {'author': params.get('author', ''),
                          'origin': params.get('origin', '')}, actor)
                touched += 1
        dropped = 0
        for k in kids[len(incoming):]:
            unlink(box['id'], k['id'])
            dropped += 1
        note = '%d 件を受け取りました' % len(incoming)
        if dropped:
            note += '（%d 件を並びから外しました。中身は倉庫に残っています）' % dropped
        return ok({'accepted': len(incoming), 'touched': touched, 'dropped': dropped},
                  note)


@recipe('record')
class Record(Recipe):
    """名前の付いた欄を束ねる。欄の名前は attrs のデータで，ここには書かない。"""

    @staticmethod
    def gather(box, params):
        kids = children(box['id'])
        fields = []
        pending = []
        worst = 'ok'
        for k in kids:
            label = (k.get('attrs') or {}).get('label', '')
            if k['kind'] == 'item':
                body = (k['body'] or '').strip()
                sub = ok([{'id': k['id'], 'body': body}]) if body else \
                    Answer('ng', [], '未記入です',
                           [{'key': name_of(k) + '（' + (
                               (k.get('attrs') or {}).get('label', '') or '欄') + '）',
                             'child': k['id'], 'why': '未記入です'}])
            else:
                sub = demand(k, 'gather', params)
            pending.extend(sub.pending)
            if sub.status == 'ng':
                worst = 'partial' if worst == 'ok' else worst
            elif sub.status == 'partial' and worst == 'ok':
                worst = 'partial'
            fields.append({'id': k['id'], 'label': label, 'kind': k['kind'],
                           'status': sub.status, 'lines': sub.value or []})
        if not kids:
            return Answer('ng', [], '欄がまだありません',
                          [{'key': name_of(box), 'child': None,
                            'why': '欄がまだありません'}])
        if pending and worst == 'ok':
            worst = 'partial'
        return Answer(worst, fields, '' if worst == 'ok' else '未達があります', pending)


_TAG_SCRIPT = re.compile(r'<\s*(script|iframe|object|embed)\b.*?<\s*/\s*\1\s*>',
                         re.I | re.S)
_TAG_OPEN = re.compile(r'<\s*(script|iframe|object|embed|link|meta)\b[^>]*>', re.I)
_ON_ATTR = re.compile(r'\son[a-z]+\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)', re.I)
_JS_URL = re.compile(r'(href|src)\s*=\s*("|\')\s*javascript:[^"\']*\2', re.I)


def clean_html(raw):
    """書かれた HTML から，動くものを落とす。見た目の指定はそのまま通す。"""
    h = raw or ''
    h = _TAG_SCRIPT.sub('', h)
    h = _TAG_OPEN.sub('', h)
    h = _ON_ATTR.sub('', h)
    h = _JS_URL.sub('', h)
    return h


@recipe('text')
class Text(Recipe):
    """型「テキスト」— 中身は HTML をそのまま書ける一枚の文書。"""

    @staticmethod
    def gather(box, params):
        raw = (box.get('body') or '').strip()
        if not raw:
            return Answer('ng', [], 'まだ書かれていません',
                          [{'key': name_of(box), 'child': box['id'],
                            'why': 'まだ書かれていません'}])
        h = clean_html(raw)
        return ok([{'id': box['id'], 'body': re.sub(r'<[^>]+>', '', h).strip()[:200],
                    'html': h}])

    @staticmethod
    def distribute(box, value, params):
        html = value if isinstance(value, str) else ''
        q = _q(box['id'])
        if q is None:
            return ng('倉庫にありません')
        q['body'] = html; _touch(q); _save()
        return ok({'length': len(html)}, '%d 文字を受け取りました' % len(html))


@recipe('count')
class Count(Recipe):
    """数え上げ。集めるだけで，配る向きは持たない（割り戻せないため）。"""

    @staticmethod
    def gather(box, params):
        kids = children(box['id'])
        return ok({'count': len(kids)})


# ------------------------------------------------------------------ デマンド発送

def demand(box, verb, params=None):
    """親から子へ。パラメータ付きで送り，〈状態，中身〉の対を受け取る。"""
    params = params or {}
    r = RECIPES.get(box.get('recipe') or '')
    if r is None:
        return ng('作法「%s」を知りません' % (box.get('recipe') or '（無し）'))
    if verb == 'gather':
        return r.gather(box, params)
    if verb == 'distribute':
        return r.distribute(box, params.get('value'), params)
    return ng('デマンド「%s」を知りません' % verb)


def _flatten(value):
    if isinstance(value, list):
        return '\n'.join(v.get('body', '') if isinstance(v, dict) else str(v)
                         for v in value)
    return str(value)


# ---------------------------------------------------------------------- 依頼（執筆担当表）

def _display_names(persons, grps):
    pn = user_names(persons); gn = group_names(grps)
    return '、'.join([gn[i] for i in _ids(grps) if i in gn] + [pn[i] for i in _ids(persons) if i in pn])


def create_request(box_id, addressee, message, due_on, actor_id, persons='', grps=''):
    """依頼を立てる。persons／grps は ID の並び，addressee は表示用。"""
    d = D()
    persons = ','.join(str(i) for i in _ids(persons)); grps = ','.join(str(i) for i in _ids(grps))
    if not addressee:
        addressee = _display_names(persons, grps)
    r = {'id': _nid(), 'box_id': int(box_id), 'addressee': (addressee or '')[:120], 'message': message or '',
         'due_on': (due_on or '')[:10] or None, 'status': 'open', 'created_by': actor_id,
         'created_at': _ts(), 'answered_at': None, 'persons': persons, 'grps': grps}
    d['requests'][r['id']] = r
    _save()
    return r['id']


def update_request(rid, persons=None, grps=None, message=None, due_on=None, status=None):
    r = D()['requests'].get(int(rid))
    if not r:
        return
    if persons is not None or grps is not None:
        p = ','.join(str(i) for i in _ids(persons)) if persons is not None else (r.get('persons') or '')
        g = ','.join(str(i) for i in _ids(grps)) if grps is not None else (r.get('grps') or '')
        r['persons'], r['grps'] = p, g
        r['addressee'] = _display_names(p, g)[:120]
    if message is not None:
        r['message'] = message
    if due_on is not None:
        r['due_on'] = (due_on or '')[:10] or None
    if status is not None:
        r['status'] = status; r['answered_at'] = _ts()
    _save()


def request_names(r):
    names = set('user:%s' % i for i in _ids(r.get('persons') or ''))
    names |= set('group:%s' % i for i in _ids(r.get('grps') or ''))
    if not names and r.get('addressee'):
        names |= set(_norm_names(r.get('addressee')))
    return names


def _req_row(r):
    out = dict(r)
    q = _q(r['box_id']) or {}
    out['title'] = q.get('title') or ''; out['key_path'] = q.get('key_path') or ''; out['recipe'] = q.get('recipe') or ''
    return out


def get_request(rid):
    r = D()['requests'].get(int(rid))
    return _req_row(r) if r else None


def list_requests(box_id=None, open_only=False):
    rows = [r for r in D()['requests'].values()
            if (box_id is None or r['box_id'] == int(box_id)) and (not open_only or r['status'] == 'open')]
    rows.sort(key=lambda r: -r['id'])
    return [_req_row(r) for r in rows]


def answer_request(rid, status):
    update_request(rid, status=status)


def set_request_status(rid, status):
    update_request(rid, status=status)


def active_request(box_id):
    rows = [r for r in D()['requests'].values()
            if r['box_id'] == int(box_id) and r['status'] in ('open', 'writing', 'done')]
    return dict(max(rows, key=lambda r: r['id'])) if rows else None


def open_requests_for(box_ids):
    out = {}
    ids = set(int(x) for x in box_ids or [])
    for r in sorted(D()['requests'].values(), key=lambda r: r['id']):
        if r['box_id'] in ids and r['status'] in ('open', 'writing', 'done'):
            out[r['box_id']] = dict(r)
    return out


def requests_for_boxes(box_ids):
    """並びは部品の出現順（box_ids の順），同じ部品の中は依頼の順。"""
    ids = [int(x) for x in box_ids or []]
    order = {b: i for i, b in enumerate(ids)}
    rows = [_req_row(r) for r in D()['requests'].values() if r['box_id'] in order]
    rows.sort(key=lambda r: (order.get(r['box_id'], 10 ** 9), r['id']))
    return rows


def ensure_request(box_id, addressee, message, due_on, actor_id):
    r = active_request(box_id)
    if r:
        return r['id']
    return create_request(box_id, addressee, message, due_on, actor_id, persons='', grps=addressee)


def name_of(q):
    """未達などで人に見せる名前。key_path が無ければ題名で言う。"""
    if not q:
        return '(未指定)'
    return (q.get('key_path') or q.get('title') or ('#%s' % q.get('id')))


def resolve(ref):
    """テキストボックスが指す倉庫の部品を引く。ID でも key_path でもよい。"""
    if ref is None or ref == '':
        return None
    s = str(ref).strip()
    if s.isdigit():
        return get_quantum(int(s))
    return get_by_key(s)


def _layout(box):
    raw = box.get('body') or ''
    try:
        lay = json.loads(raw)
    except (ValueError, TypeError):
        lay = {}
    if not isinstance(lay, dict):
        lay = {}
    lay.setdefault('w', 1000)
    lay.setdefault('h', 620)
    lay.setdefault('root', {'t': 'col', 'align': 'top', 'items': []})
    return lay


def save_layout(box_id, lay):
    q = _q(box_id)
    if q:
        q['body'] = json.dumps(lay, ensure_ascii=False); _touch(q); _save()


def leaves(node, out=None):
    """レイアウトの木から，倉庫を指すテキストボックスだけを拾う。"""
    out = [] if out is None else out
    if not isinstance(node, dict):
        return out
    if node.get('t') == 'text':
        out.append(node)
    else:
        for c in node.get('items') or []:
            leaves(c, out)
    return out


@recipe('arena')
class Arena(Recipe):
    """長方形の中に，横並び／縦並びのカルーセルを入れ子にした複合部品。

    集める向きは，末端のテキストボックスが指す倉庫の部品に集めるデマンドを
    落として回り，返ってきた対をそのまま木の形で持ち上げる（情報収集）。
    配る向きは，執筆者が入れた値を，指し先の箱へそれぞれ配る。
    """

    @staticmethod
    def gather(box, params):
        lay = _layout(box)
        over = (params or {}).get('layouts') or {}     # 画面が持つ保存前のレイアウト（アリーナID→layout）
        if str(box.get('id')) in over and isinstance(over[str(box['id'])], dict):
            lay = dict(_layout({'body': json.dumps(over[str(box['id'])], ensure_ascii=False)}))
        pending = []

        def walk(node):
            if not isinstance(node, dict):
                return {}
            if node.get('t') != 'text':
                return dict(node, items=[walk(c) for c in node.get('items') or []])
            out = dict(node)
            q = resolve(node.get('ref'))
            if not q:
                out.update(status='ng', lines=[], note='倉庫に見つかりません')
                pending.append({'key': node.get('ref') or '(未指定)', 'child': None,
                                'why': '倉庫に見つかりません'})
                return out
            out['qid'] = q['id']
            out['key'] = q.get('key_path') or ''
            out['qtitle'] = q.get('title') or ''
            out['qrecipe'] = q.get('recipe') or ''
            me = params.get('me'); wperm = params.get('wperm') or {'view': True, 'edit': True}
            wacl = params.get('wacl') or {}
            if me is not None and q['kind'] == 'box' and q.get('recipe') != 'arena':
                pm = box_perm(q, me, wperm, wacl)
                out['perm'] = {k: pm[k] for k in ('view', 'edit', 'write', 'writer', 'frozen', 'assigned')}
                out['acl'] = pm['acl']
                if not pm['view']:
                    out.update(status='hidden', lines=[], note='閲覧の権限がありません')
                    return out
            if q['kind'] == 'box' and q.get('recipe') == 'arena':
                sub = demand(q, 'gather', params)      # 複合部品をそのまま埋め込む
                pending.extend(sub.pending)
                out.update(status=sub.status, lines=[], note=sub.note,
                           embed=(sub.value or {}).get('root'),
                           embed_title=q.get('title') or '')
                return out
            if q['kind'] == 'box':
                sub = demand(q, 'gather', params)
            else:
                body = (q.get('body') or '').strip()
                sub = ok([{'id': q['id'], 'body': body}]) if body else \
                    Answer('ng', [], '未記入です',
                           [{'key': name_of(q), 'child': q['id'], 'why': '未記入です'}])
            pending.extend(sub.pending)
            lines = sub.value if isinstance(sub.value, list) else []
            out.update(status=sub.status, lines=lines, note=sub.note)
            return out

        tree = walk(lay.get('root'))
        n = len(leaves(lay.get('root')))
        value = {'w': lay.get('w'), 'h': lay.get('h'), 'root': tree, 'leaf_count': n}
        if not n:
            return Answer('ng', value, 'テキストボックスがまだありません', [])
        if pending:
            return partial(value, '一部が未達です', pending)
        return ok(value)

    @staticmethod
    def distribute(box, value, params):
        """value は {ref: 書かれた塊} の対応。指し先の箱へそれぞれ配る。

        参照は倉庫の名前なので，入れ子にした複合部品の中の箱にもそのまま届く。
        """
        values = value if isinstance(value, dict) else {}
        results, bad = [], 0
        for ref in values:
            ref = str(ref)
            q = resolve(ref)
            if not q or q['kind'] != 'box':
                results.append({'ref': ref, 'status': 'ng', 'note': '配れる箱ではありません'})
                bad += 1
                continue
            sub = demand(q, 'distribute', dict(params, value=values[ref]))
            results.append({'ref': ref, 'status': sub.status, 'note': sub.note})
            if sub.status == 'ng':
                bad += 1
        if not results:
            return ng('配る先がありません')
        if bad:
            return partial({'results': results}, '%d 件が配れませんでした' % bad,
                           [{'key': r['ref'], 'child': None, 'why': r['note']}
                            for r in results if r['status'] == 'ng'])
        return ok({'results': results}, '%d 箇所に配りました' % len(results))


WORK = 'work'      # 最終部品（作品）
PART = 'part'      # 複合部品


def list_arenas(level=None):
    rows = []
    for q in D()['quanta'].values():
        if q.get('kind') == 'box' and q.get('recipe') == 'arena':
            r = dict(q); r['level'] = (q.get('attrs') or {}).get('level') or PART
            if level is None or r['level'] == level:
                rows.append(r)
    rows.sort(key=lambda r: r.get('updated_at') or '', reverse=True)
    return rows


def create_arena(title, actor_id, level=PART, lay=None):
    lay = lay or {'w': 1000, 'h': 620, 'root': {'t': 'col', 'align': 'top', 'items': []}}
    q = _new_quantum('box', title=title, body=json.dumps(lay, ensure_ascii=False), recipe='arena',
                     attrs={'level': WORK if level == WORK else PART}, owner_id=actor_id)
    _save()
    return q['id']


def copy_arena(arena_id, actor_id, title=None):
    a = get_quantum(arena_id)
    if not a or a.get('recipe') != 'arena':
        return None
    return create_arena(title or ((a.get('title') or '無題') + ' の複製'), actor_id,
                        (a.get('attrs') or {}).get('level') or PART, _layout(a))


def set_arena_meta(arena_id, title, level):
    q = _q(arena_id)
    if not q:
        return
    q['title'] = title
    q['attrs'] = dict(q.get('attrs') or {}); q['attrs']['level'] = WORK if level == WORK else PART
    _touch(q); _save()


def create_box(key_path, title, recipe, addressee, actor_id):
    """倉庫部品（箱）をその場で生やす。key_path は空でもよい。"""
    D()
    if key_path and _S.bykey.get(key_path):
        return None, 'その key_path はすでに使われています'
    attrs = {}
    if addressee:
        attrs['addressee'] = addressee
    if title:
        attrs['label'] = title
    q = _new_quantum('box', key_path=key_path or None, title=title or '（無題）',
                     recipe=recipe if recipe in ('text', 'seq', 'record', 'count') else 'text',
                     body='' if recipe == 'text' else None, attrs=attrs, owner_id=actor_id)
    _save()
    return get_quantum(q['id']), None


def copy_box(box_id, actor_id):
    b = _q(box_id)
    if not b or b.get('kind') != 'box' or b.get('recipe') == 'arena':
        return None
    q = _new_quantum('box', title=(b.get('title') or '無題') + ' の複製', body=b.get('body'),
                     recipe=b.get('recipe'), attrs=b.get('attrs') or {}, owner_id=actor_id)
    for i, k in enumerate(children(box_id), 1):
        if k['kind'] == 'item':
            it = _new_quantum('item', body=k.get('body') or '', attrs=k.get('attrs') or {}, owner_id=actor_id)
            _link(q['id'], it['id'], i)
        else:
            _link(q['id'], k['id'], i)
    _save()
    return q['id']


def set_box_meta(box_id, title, addressee):
    q = _q(box_id)
    if not q:
        return
    q['title'] = title
    attrs = dict(q.get('attrs') or {})
    if addressee:
        attrs['addressee'] = addressee
    else:
        attrs.pop('addressee', None)
    q['attrs'] = attrs; _touch(q); _save()


def search_boxes(q, limit=20, scope='store', exclude_id=None):
    """部品を探す。scope は 'store'（倉庫部品）／'part'（複合部品）／'all'。"""
    q = (q or '').strip().lower()
    if scope != 'part' and not q:
        return []
    out = []
    for x in D()['quanta'].values():
        is_arena = x.get('recipe') == 'arena'
        if scope == 'part' and not (x.get('kind') == 'box' and is_arena):
            continue
        if scope == 'store' and is_arena:
            continue
        if exclude_id and x['id'] == int(exclude_id):
            continue
        hay = ' '.join([x.get('key_path') or '', x.get('title') or '', x.get('body') or '']).lower()
        if q and q not in hay:
            continue
        r = {'id': x['id'], 'kind': x.get('kind'), 'key_path': x.get('key_path') or '', 'title': x.get('title') or '',
             'recipe': x.get('recipe') or '', 'is_arena': is_arena,
             'level': ((x.get('attrs') or {}).get('level') or PART) if is_arena else ''}
        b = x.get('body') or ''
        r['excerpt'] = '' if is_arena else ((b[:60] + '…') if len(b) > 60 else b)
        out.append(r)
    out.sort(key=lambda r: (r['kind'] != 'box', r['key_path'] or '\uffff', r['id']))
    return out[:limit]


def _norm_names(v):
    if isinstance(v, str):
        v = [x.strip() for x in re.split(r'[,\n、，]', v)]
    return [str(x).strip() for x in (v or []) if str(x).strip()]


def _ids(v):
    """'12,15' や [12, '15'] を整数の並びに。"""
    if isinstance(v, str):
        v = re.split(r'[,\s、，]+', v)
    out = []
    for x in v or []:
        try:
            n = int(str(x).strip().split(':')[-1])
        except (TypeError, ValueError):
            continue
        if n and n not in out:
            out.append(n)
    return out


def users_list(q='', limit=40):
    """ユーザを選ぶための一覧（default DB の users。削除済み・無効を除く）。"""
    like = '%%%s%%' % (q or '')
    try:
        with _db('default') as (cur, conn):
            cur.execute(
                "SELECT id, email, full_name, affiliation, category FROM users "
                "WHERE deleted_at IS NULL AND COALESCE(is_active, 1) = 1 "
                "AND (full_name LIKE %s OR email LIKE %s OR COALESCE(affiliation,'') LIKE %s) "
                "ORDER BY full_name, email LIMIT %s", (like, like, like, limit))
            return cur.fetchall()
    except Exception:
        return []


def groups_list():
    """まいぐるのグループ一覧（default DB の user_groups）。"""
    try:
        with _db('default') as (cur, conn):
            cur.execute("SELECT id, name, description FROM user_groups ORDER BY name")
            return cur.fetchall()
    except Exception:
        return []


def user_names(ids):
    """{id: 氏名}（氏名が無ければメール）"""
    ids = _ids(ids)
    if not ids:
        return {}
    marks = ','.join(['%s'] * len(ids))
    try:
        with _db('default') as (cur, conn):
            cur.execute("SELECT id, full_name, email FROM users WHERE id IN (%s)" % marks, tuple(ids))
            return {r['id']: (r.get('full_name') or r.get('email') or ('#%s' % r['id'])) for r in cur.fetchall()}
    except Exception:
        return {i: '#%s' % i for i in ids}


def group_names(ids):
    ids = _ids(ids)
    if not ids:
        return {}
    marks = ','.join(['%s'] * len(ids))
    try:
        with _db('default') as (cur, conn):
            cur.execute("SELECT id, name FROM user_groups WHERE id IN (%s)" % marks, tuple(ids))
            return {r['id']: r['name'] for r in cur.fetchall()}
    except Exception:
        return {i: '#%s' % i for i in ids}


def principals(user_id):
    """本人が名乗れる札：user:<id> と，いま有効な所属の group:<id>。
    旧データ（名前で書かれた割り当て）のために，氏名・メール・グループ名も添える。"""
    names = set()
    if not user_id:
        return names
    names.add('user:%s' % user_id)
    try:
        with _db('default') as (cur, conn):
            cur.execute("SELECT full_name, email FROM users WHERE id = %s", (user_id,))
            r = cur.fetchone()
            if r:
                for k in ('full_name', 'email'):
                    if r.get(k):
                        names.add(str(r[k]))
            now = _now()
            cur.execute(
                "SELECT m.group_id, g.name FROM user_group_memberships m "
                "JOIN user_groups g ON g.id = m.group_id WHERE m.user_id = %s "
                "AND (m.valid_from IS NULL OR m.valid_from <= %s) "
                "AND (m.valid_until IS NULL OR m.valid_until >= %s)", (user_id, now, now))
            for r in cur.fetchall():
                names.add('group:%s' % r['group_id'])
                if r.get('name'):
                    names.add(str(r['name']))
    except Exception:
        pass
    return names


EDITOR_GROUPS = ('admin', 'こんか編集者')       # まいぐるのこのグループが「こんかの編集者」
POLICIES = ('private', 'domestic', 'public', 'group', 'domestic_group')  # 文書アーカイブと同じ公開範囲


def is_editor(me):
    """編集ビューと設定を触れる人：カテゴリ admin か，まいぐるの admin／こんか編集者の一員。"""
    if not me:
        return False
    if me.get('admin'):
        return True
    return bool(set(me.get('names') or []) & set(EDITOR_GROUPS))


def work_acl(arena):
    lay = _layout(arena)
    a = lay.get('acl') if isinstance(lay.get('acl'), dict) else {}
    pol = a.get('policy') if a.get('policy') in POLICIES else 'private'
    return {'policy': pol, 'groups': _ids(a.get('groups') or []),
            'frozen': bool(a.get('frozen')), 'due': a.get('due') or '',
            'view': _norm_names(a.get('view')), 'edit': _norm_names(a.get('edit'))}


def box_acl(box):
    a = (box.get('attrs') or {}).get('acl') if box else None
    a = a if isinstance(a, dict) else {}
    return {'edit': _norm_names(a.get('edit')), 'write': _norm_names(a.get('write')),
            'view': _norm_names(a.get('view')), 'open': bool(a.get('open')),
            'view_mode': 'or' if a.get('view_mode') == 'or' else 'and',
            'write_p': _norm_names(a.get('write_p')), 'write_g': _norm_names(a.get('write_g'))}


def me_info(user_id, category):
    return {'id': user_id, 'names': principals(user_id), 'admin': (category == 'admin'),
            'category': category or ''}


def has_assignment(arena, me, depth=0):
    """本人がこの作品のどこかの部品の執筆担当か（埋め込みの中も）。"""
    if not me or depth > 6:
        return False
    names = set(me.get('names') or [])
    for nd in leaves(_layout(arena).get('root')):
        q = resolve(nd.get('ref'))
        if not q:
            continue
        if q.get('recipe') == 'arena':
            if has_assignment(q, me, depth + 1):
                return True
        elif q.get('kind') == 'box':
            if names & set(box_acl(q)['write']):
                return True
            r = active_request(q['id'])
            if r and (names & request_names(r)):
                return True
    return False


def work_perm(arena, me):
    """作品に対する本人の権限。

    編集：編集者（admin／こんか編集者）だけ。
    閲覧：最終部品（作品）は公開範囲（文書アーカイブと同じ5種）で決め，執筆の担当者はいつでも開ける。
          複合部品は編集者だけ。
    """
    if me is None:
        return {'view': True, 'edit': True}
    edit = is_editor(me)
    if edit:
        return {'view': True, 'edit': True}
    level = (arena.get('attrs') or {}).get('level') or PART
    if level != WORK:
        return {'view': False, 'edit': False}
    acl = work_acl(arena)
    names = set(me.get('names') or [])
    cat = me.get('category') or ''
    pol = acl['policy']
    in_group = bool(names & set('group:%s' % g for g in acl['groups']))
    if pol == 'public':
        view = True
    elif pol == 'domestic':
        view = cat == 'regular'
    elif pol == 'group':
        view = in_group
    elif pol == 'domestic_group':
        view = cat == 'regular' or in_group
    else:                                   # private
        view = False
    if not view and me.get('id'):
        view = bool(arena.get('owner_id') and arena.get('owner_id') == me.get('id')) or has_assignment(arena, me)
    return {'view': view, 'edit': False}


def box_perm(box, me, wperm, wacl):
    """部品に対する本人の権限（閲覧は AND）。need は「空で担当が無い」= 割り当てが要る印。"""
    acl = box_acl(box)
    names = set((me or {}).get('names') or [])
    admin = bool((me or {}).get('admin')) or (me is None)
    edit = admin or bool(wperm.get('edit')) or bool(names & set(acl['edit']))
    writer = bool(names & set(acl['write']))
    if not writer:
        r = active_request(box['id']) if box.get('id') else None
        writer = bool(r) and bool(names & request_names(r))
    frozen = bool(wacl.get('frozen')) and not acl['open']
    write = (edit or writer) and (not frozen or admin or wperm.get('edit'))
    listed = bool(names & set(acl['view']))
    if acl['view_mode'] == 'or':
        # 例外で許す：作品で見えなくても，ここに名前があれば見える
        view = bool(wperm.get('view')) or listed or edit or writer
    else:
        # 例外で限定：作品で見えていて，かつ（限定が無い か 名前がある か 担当）
        view = bool(wperm.get('view')) and (edit or writer or not acl['view'] or listed)
    return {'view': view, 'edit': edit, 'write': write, 'writer': writer, 'frozen': frozen,
            'assigned': bool(acl['write'] or acl['edit']), 'acl': acl}


def set_box_acl(box_id, edit, write, view, open_, view_mode='and', write_p=None, write_g=None):
    q = _q(box_id)
    if not q:
        return
    old = box_acl(q)
    attrs = dict(q.get('attrs') or {})
    attrs['acl'] = {'edit': _norm_names(edit), 'write': _norm_names(write),
                    'view': _norm_names(view), 'open': bool(open_),
                    'view_mode': 'or' if view_mode == 'or' else 'and',
                    'write_p': _norm_names(write_p) if write_p is not None else old.get('write_p', []),
                    'write_g': _norm_names(write_g) if write_g is not None else old.get('write_g', [])}
    q['attrs'] = attrs; _touch(q); _save()


def set_work_acl(arena_id, policy, groups, frozen, due):
    a = get_quantum(arena_id)
    if not a:
        return
    lay = _layout(a)
    lay['acl'] = {'policy': policy if policy in POLICIES else 'private', 'groups': _ids(groups),
                  'frozen': bool(frozen), 'due': (due or '')[:10]}
    save_layout(arena_id, lay)


def assign_writers(box_id, persons, grps, message, due_on, actor_id):
    """執筆の担当を決める＝その部品の依頼を1本立てる（あれば直す）。acl.write も同期する。"""
    b = get_quantum(box_id)
    if not b:
        return None
    pids = _ids(persons); gids = _ids(grps)
    names = ['user:%s' % i for i in pids] + ['group:%s' % i for i in gids]
    acl = box_acl(b)
    set_box_acl(box_id, acl['edit'], names, acl['view'], acl['open'], acl['view_mode'],
                write_p=[str(i) for i in pids], write_g=[str(i) for i in gids])
    r = active_request(box_id)
    if not names:
        if r:
            update_request(r['id'], status='closed')
        return None
    if r:
        update_request(r['id'], persons=persons, grps=grps,
                       message=(message if message else None), due_on=(due_on if due_on else None))
        return r['id']
    return create_request(box_id, '', message or 'この部品の執筆をお願いします。', due_on, actor_id,
                          persons=persons, grps=grps)


# ================================================================ 書き出しと読み込み

def _collect(arena_id, seen=None, depth=0):
    """作品から辿れるもの（複合部品・倉庫部品・箇条）を集める。"""
    seen = seen if seen is not None else {'arenas': [], 'boxes': [], 'items': []}
    if depth > 6:
        return seen
    a = get_quantum(arena_id)
    if not a or a.get('recipe') != 'arena':
        return seen
    if any(x['id'] == a['id'] for x in seen['arenas']):
        return seen
    seen['arenas'].append(a)
    for nd in leaves(_layout(a).get('root')):
        q = resolve(nd.get('ref'))
        if not q:
            continue
        if q.get('recipe') == 'arena':
            _collect(q['id'], seen, depth + 1)
        elif q.get('kind') == 'box':
            if not any(x['id'] == q['id'] for x in seen['boxes']):
                seen['boxes'].append(q)
                for k in children(q['id']):
                    if k['kind'] == 'item':
                        seen['items'].append({'parent': q['id'], 'id': k['id'],
                                              'body': k.get('body') or '',
                                              'attrs': k.get('attrs') or {},
                                              'ord': k.get('link_ord') or 0})
    return seen


def export_bundle(arena_id, with_acl=False):
    """作品ひとまとまりを JSON にする。相手や割り当てはサイト固有なので既定では入れない。"""
    s = _collect(arena_id)

    def strip(attrs):
        d = dict(attrs or {})
        if not with_acl:
            d.pop('acl', None)
        return d

    def lay(a):
        L = _layout(a)
        if not with_acl:
            L = dict(L); L.pop('acl', None)
        return L

    return {
        'export_type': 'cqm_bundle', 'format_version': 1,
        'exported_at': _now().strftime('%Y-%m-%d %H:%M:%S'),
        'root': arena_id,
        'arenas': [{'id': a['id'], 'title': a.get('title') or '', 'key_path': a.get('key_path') or '',
                    'level': (a.get('attrs') or {}).get('level') or PART,
                    'layout': lay(a), 'attrs': strip(a.get('attrs'))} for a in s['arenas']],
        'boxes': [{'id': b['id'], 'key_path': b.get('key_path') or '', 'title': b.get('title') or '',
                   'recipe': b.get('recipe') or '', 'body': b.get('body') or '',
                   'attrs': strip(b.get('attrs'))} for b in s['boxes']],
        'items': s['items'],
    }


def _ref_map(bundle, id_map):
    """書き出し時の指し先を，取り込み先の指し先（key_path か新しい ID）に読み替える表。
    取り込んだ実物を見て決めるので，'new' で作り直したときも正しく繋がる。"""
    m = {}
    for a in bundle.get('arenas', []):
        nid = id_map['arena'].get(a['id'])
        if nid:
            m[str(a['id'])] = str(nid)
            if a.get('key_path'):
                m[a['key_path']] = str(nid)
    for b in bundle.get('boxes', []):
        nid = id_map['box'].get(b['id'])
        if not nid:
            continue
        nb = get_quantum(nid) or {}
        ref = nb.get('key_path') or str(nid)
        m[str(b['id'])] = ref
        if b.get('key_path'):
            m[b['key_path']] = ref
    return m


def _remap(node, m):
    if not isinstance(node, dict):
        return node
    out = dict(node)
    if out.get('t') == 'text':
        r = str(out.get('ref') or '')
        if r in m and m[r] is not None:
            out['ref'] = str(m[r])
    if out.get('items'):
        out['items'] = [_remap(c, m) for c in out['items']]
    return out


def import_bundle(bundle, actor_id, mode='merge'):
    """読み込む。mode='merge' は key_path が同じ倉庫部品に中身を上書き，'new' はすべて新しく作る。
    メモリ上で組み立ててから1回だけ保存する。"""
    if not isinstance(bundle, dict) or bundle.get('export_type') != 'cqm_bundle':
        return {'ok': False, 'error': 'こんかの書き出しファイルではありません'}
    rep = {'ok': True, 'boxes_new': 0, 'boxes_updated': 0, 'arenas_new': 0, 'items': 0}
    id_map = {'box': {}, 'arena': {}}
    D()
    for b in bundle.get('boxes', []):
        key = b.get('key_path') or ''
        cur = _S.bykey.get(key) if (key and mode == 'merge') else None
        if cur:
            cur['kind'] = 'box'                         # 旧データで箇条として作られていても箱に直す
            cur['recipe'] = b.get('recipe') or cur.get('recipe') or 'text'
            cur['title'] = b.get('title') or cur.get('title')
            cur['body'] = b.get('body')
            if b.get('attrs'):
                at = dict(cur.get('attrs') or {}); at.update(b['attrs']); cur['attrs'] = at
            _touch(cur)
            unlink_all(cur['id'])
            id_map['box'][b['id']] = cur['id']
            rep['boxes_updated'] += 1
        else:
            kp = key if (mode == 'merge' and key and key not in _S.bykey) else None
            q = _new_quantum('box', key_path=kp, title=b.get('title') or '（無題）', body=b.get('body'),
                             recipe=b.get('recipe') or 'text', attrs=b.get('attrs') or {}, owner_id=actor_id)
            id_map['box'][b['id']] = q['id']
            rep['boxes_new'] += 1
    for it in sorted(bundle.get('items', []), key=lambda x: (x.get('parent'), x.get('ord') or 0)):
        pid = id_map['box'].get(it.get('parent'))
        if pid:
            q = _new_quantum('item', body=it.get('body') or '', attrs=it.get('attrs') or {}, owner_id=actor_id)
            _link(pid, q['id'])
            rep['items'] += 1
    for a in bundle.get('arenas', []):
        q = _new_quantum('box', title=a.get('title') or '無題', recipe='arena',
                         body=json.dumps(a.get('layout') or {}, ensure_ascii=False),
                         attrs={'level': WORK if (a.get('level') == WORK) else PART}, owner_id=actor_id)
        id_map['arena'][a['id']] = q['id']
        rep['arenas_new'] += 1
    m = _ref_map(bundle, id_map)
    for a in bundle.get('arenas', []):
        nid = id_map['arena'][a['id']]
        L = dict(a.get('layout') or {})
        L['root'] = _remap(L.get('root') or {}, m)
        _q(nid)['body'] = json.dumps(L, ensure_ascii=False)
    rep['root'] = id_map['arena'].get(bundle.get('root'))
    rep['gc'] = gc_orphans()
    _save()
    return rep


def gc_orphans():
    """どの箱にも繋がっていない箇条を捨てる（上書き取り込みで外れた古い箇条など）。"""
    d = D()
    linked = set(x['child'] for x in d['links'])
    gone = [i for i, q in d['quanta'].items() if q.get('kind') == 'item' and i not in linked]
    for i in gone:
        d['quanta'].pop(i, None)
    if gone:
        _index()
    return len(gone)


# ================================================================ SQL への格納と，SQL からの読み込み（明示の操作）

_TABLES_DDL = {
    'cqm_quanta': "CREATE TABLE IF NOT EXISTS cqm_quanta (id BIGINT PRIMARY KEY, kind VARCHAR(16) NOT NULL, key_path VARCHAR(255) NULL, title VARCHAR(255) NULL, body MEDIUMTEXT NULL, recipe VARCHAR(32) NULL, attrs JSON NULL, owner_id INT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, UNIQUE KEY uk_cqm_key (key_path), KEY ix_cqm_kind (kind)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
    'cqm_links': "CREATE TABLE IF NOT EXISTS cqm_links (id BIGINT AUTO_INCREMENT PRIMARY KEY, parent_id BIGINT NOT NULL, child_id BIGINT NOT NULL, ord INT NOT NULL DEFAULT 0, KEY ix_cqm_links_parent (parent_id, ord)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
    'cqm_requests': "CREATE TABLE IF NOT EXISTS cqm_requests (id BIGINT PRIMARY KEY, box_id BIGINT NOT NULL, addressee VARCHAR(120) NULL, message TEXT NULL, due_on DATE NULL, status VARCHAR(16) NOT NULL DEFAULT 'open', created_by INT NULL, created_at DATETIME NOT NULL, answered_at DATETIME NULL, persons TEXT NULL, grps TEXT NULL, KEY ix_cqm_req_box (box_id, status)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
}


def sql_store():
    """倉庫の全部を SQL の写しに置く（表を空にしてから入れ直す）。"""
    d = D()
    with _db() as (cur, conn):
        for ddl in _TABLES_DDL.values():        # 表はアプシャが作るのが本筋。無ければここで作る
            try:
                cur.execute(ddl)
            except Exception:
                pass
        for t in ('cqm_links', 'cqm_requests', 'cqm_quanta'):
            cur.execute('DELETE FROM %s' % t)
        rows = [(q['id'], q.get('kind'), q.get('key_path'), q.get('title'), q.get('body'), q.get('recipe'),
                 json.dumps(q.get('attrs') or {}, ensure_ascii=False), q.get('owner_id'),
                 q.get('created_at') or _ts(), q.get('updated_at') or _ts()) for q in d['quanta'].values()]
        cur.executemany('INSERT INTO cqm_quanta (id, kind, key_path, title, body, recipe, attrs, owner_id, created_at, updated_at) '
                        'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)', rows)
        cur.executemany('INSERT INTO cqm_links (parent_id, child_id, ord) VALUES (%s,%s,%s)',
                        [(x['parent'], x['child'], x.get('ord', 0)) for x in d['links']])
        cur.executemany('INSERT INTO cqm_requests (id, box_id, addressee, message, due_on, status, created_by, created_at, answered_at, persons, grps) '
                        'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                        [(r['id'], r['box_id'], r.get('addressee'), r.get('message'), r.get('due_on'), r.get('status'),
                          r.get('created_by'), r.get('created_at') or _ts(), r.get('answered_at'), r.get('persons'), r.get('grps'))
                         for r in d['requests'].values()])
        conn.commit()
    return {'quanta': len(rows), 'links': len(d['links']), 'requests': len(d['requests'])}


def sql_load():
    """SQL の写しを読んで，倉庫のファイルにする（いまの倉庫は置き換わる）。"""
    with _db() as (cur, conn):
        cur.execute('SELECT * FROM cqm_quanta'); qs = cur.fetchall()
        cur.execute('SELECT parent_id, child_id, ord FROM cqm_links ORDER BY parent_id, ord, id'); ls = cur.fetchall()
        cur.execute('SELECT * FROM cqm_requests'); rs = cur.fetchall()
    d = _empty()
    for q in qs:
        q = dict(q); q['attrs'] = _loads(q.get('attrs'))
        for k in ('created_at', 'updated_at'):
            q[k] = str(q.get(k) or _ts())[:19]
        d['quanta'][int(q['id'])] = q
    d['links'] = [{'parent': int(x['parent_id']), 'child': int(x['child_id']), 'ord': int(x.get('ord') or 0)} for x in ls]
    for r in rs:
        r = dict(r)
        for k in ('created_at', 'answered_at'):
            r[k] = (str(r[k])[:19] if r.get(k) else None)
        r['due_on'] = str(r['due_on']) if r.get('due_on') else None
        d['requests'][int(r['id'])] = r
    d['next_id'] = max([0] + list(d['quanta'].keys()) + list(d['requests'].keys())) + 1
    with _S.lock:
        _S.d = d; _index(); _save()
    return {'quanta': len(d['quanta']), 'links': len(d['links']), 'requests': len(d['requests'])}


# ================================================================ 削除

def referrers(target):
    """この部品を指している複合部品・作品（と，箇条なら親の箱）。"""
    D()
    t = target
    refs = set([str(t['id'])] + ([t['key_path']] if t.get('key_path') else []))
    out = []
    for q in D()['quanta'].values():
        if q.get('recipe') != 'arena' or q['id'] == t['id']:
            continue
        for nd in leaves(_layout(q).get('root')):
            if str(nd.get('ref') or '') in refs:
                out.append({'id': q['id'], 'title': q.get('title') or '', 'kind': 'arena'})
                break
    if t.get('kind') == 'item':
        for ln in D()['links']:
            if ln['child'] == t['id']:
                p = _q(ln['parent'])
                if p:
                    out.append({'id': p['id'], 'title': p.get('title') or '', 'kind': 'box'})
    return out


def delete_quantum(qid, force=False):
    """作品・複合部品・倉庫部品を消す。どこかから指されていれば断る（force で強行）。
    箱を消すと中の箇条も消える（他の箱と共有していなければ）。"""
    q = _q(qid)
    if not q:
        return {'ok': False, 'error': '見つかりません'}
    who = referrers(q)
    if who and not force:
        return {'ok': False, 'error': '指されています：' + '、'.join(x['title'] or ('#%s' % x['id']) for x in who),
                'referrers': who}
    d = D()
    kids = [ln['child'] for ln in _S.kids.get(q['id'], [])]
    unlink_all(q['id'])
    d['links'] = [x for x in d['links'] if x['child'] != q['id']]
    d['quanta'].pop(q['id'], None)
    if q.get('key_path'):
        _S.bykey.pop(q['key_path'], None)
    for rid in [r['id'] for r in d['requests'].values() if r['box_id'] == q['id']]:
        d['requests'].pop(rid, None)
    _index()
    n = gc_orphans()
    _save()
    return {'ok': True, 'title': q.get('title') or '', 'items_removed': n}


def orphan_parts():
    """どこからも指されていない複合部品（作品は含めない）。途中で切れた取り込みの残骸などの掃除に。"""
    D()
    referenced = set()
    for q in D()['quanta'].values():
        if q.get('recipe') == 'arena':
            for nd in leaves(_layout(q).get('root')):
                referenced.add(str(nd.get('ref') or ''))
    out = []
    for q in D()['quanta'].values():
        if q.get('recipe') != 'arena' or (q.get('attrs') or {}).get('level') == WORK:
            continue
        if str(q['id']) in referenced or (q.get('key_path') and q['key_path'] in referenced):
            continue
        out.append({'id': q['id'], 'title': q.get('title') or '', 'updated_at': q.get('updated_at') or ''})
    out.sort(key=lambda x: x['id'])
    return out


def delete_orphan_parts():
    """指されていない複合部品をまとめて消す（中身の倉庫部品は残す）。
    消すと，それに指されていた複合部品も孤児になるので，無くなるまで繰り返す。"""
    total = 0
    d = D()
    for _ in range(20):
        ids = [x['id'] for x in orphan_parts()]
        if not ids:
            break
        for i in ids:
            d['quanta'].pop(i, None)
        s = set(ids)
        d['links'] = [x for x in d['links'] if x['parent'] not in s]
        _index()
        total += len(ids)
    if total:
        _save()
    return total
