"""こんか v0.1 の画面。

編集者が依頼を出す → 執筆者が依頼文を見て書き，倉庫へ送り返す →
編集者が「いまの出来」を尋ねると，〈状態，中身，未達〉が返る。
"""

import json
import re
import time

from flask import (Response, jsonify, redirect, render_template, request, session,
                   url_for)

import auth
from decorators import login_required

from . import cqm_bp, store


_PUBLIC_OK = ('cqm.arena_view', 'cqm.arena_gather', 'cqm.arena_layout', 'cqm.return_to_fujin')


@cqm_bp.before_request
def _gate():
    """deny by default。公開範囲が public の作品の閲覧経路だけは未ログインでも通す
    （権限の判定は各経路で行う）。それ以外はログイン済みだけ。"""
    if request.endpoint in (None, 'cqm.static'):
        return None
    if request.endpoint in _PUBLIC_OK:
        return None
    if not session.get('user_id'):
        return redirect(url_for('auth.login', next=request.path))
    return None


def editor_required(f):
    """編集者（admin／まいぐるの admin・こんか編集者）だけの経路。"""
    from functools import wraps
    @wraps(f)
    def inner(*a, **k):
        if not store.is_editor(me()):
            if request.is_json or request.path.startswith('/cqm/api/'):
                return _json_ng('編集者だけが使えます', 403)
            return redirect(url_for('cqm.index'))
        return f(*a, **k)
    return inner


@cqm_bp.route('/return_to_fujin')
@login_required
def return_to_fujin():
    return auth.redirect_to_dashboard()


def me():
    if not session.get('user_id'):
        return {'id': 0, 'names': set(), 'admin': False, 'category': ''}
    c = session.get('cqm_me')
    if c and c.get('id') == session.get('user_id') and (time.time() - c.get('at', 0)) < 600:
        return {'id': c['id'], 'names': set(c['names']), 'admin': c['admin'], 'category': c['category']}
    m = store.me_info(session.get('user_id'), session.get('user_category'))
    session['cqm_me'] = {'id': m['id'], 'names': sorted(m['names']), 'admin': m['admin'],
                         'category': m['category'], 'at': time.time()}
    return m


def can_edit(arena=None):
    """編集（組み立て・割り当て）してよいか。作品の acl.edit，所有者，admin。
    アリーナが無い文脈（倉庫部品の直接編集など）は admin か所有者，
    それ以外は倉庫部品側の acl で判定する。"""
    if arena is None:
        return True
    return bool(store.work_perm(arena, me()).get('edit'))


@cqm_bp.route('/')
@login_required
def index():
    boxes = store.list_boxes()
    by_box = {}
    for r in store.list_requests():
        by_box.setdefault(r['box_id'], []).append(r)
    m = me(); editor = store.is_editor(m)
    works = []
    for r in store.list_arenas(store.WORK):
        wp = store.work_perm(store.get_quantum(r['id']), m)
        if wp.get('view'):
            r['can_edit'] = wp.get('edit'); works.append(r)
    return render_template(
        'cqm/index.html',
        works=works,
        parts=store.list_arenas(store.PART) if editor else [],
        boxes=boxes if editor else [], by_box=by_box, can_edit=editor)


@cqm_bp.route('/box/<int:box_id>')
@login_required
@editor_required
def box(box_id):
    b = store.get_quantum(box_id)
    if not b:
        return redirect(url_for('cqm.index'))
    answer = store.demand(b, 'gather', {'actor_id': session.get('user_id')})
    store.log(box_id, 'gather', {}, answer, session.get('user_id'))
    return render_template('cqm/box.html', box=b, answer=answer,
                           kids=store.children(box_id),
                           requests=store.list_requests(box_id))


@cqm_bp.route('/box/<int:box_id>/edit', methods=['GET', 'POST'])
@login_required
@editor_required
def box_edit(box_id):
    """倉庫部品の編集。型に応じて中身の書き方が変わる（いまは型テキストだけ）。"""
    if not can_edit():
        return redirect(url_for('cqm.box', box_id=box_id))
    b = store.get_quantum(box_id)
    if not b or b.get('kind') != 'box' or b.get('recipe') == 'arena':
        return redirect(url_for('cqm.index'))
    answer = None
    if request.method == 'POST':
        m = me()
        wp = {'view': True, 'edit': m['admin'] or b.get('owner_id') == m['id']}
        if not store.box_perm(b, m, wp, {}).get('write'):
            return redirect(url_for('cqm.box', box_id=box_id))
        store.set_box_meta(box_id,
                           (request.form.get('title') or '').strip() or '（無題）',
                           (request.form.get('addressee') or '').strip())
        b = store.get_quantum(box_id)
        answer = store.demand(b, 'distribute',
                              {'value': request.form.get('body') or '',
                               'actor_id': session.get('user_id'),
                               'origin': 'box_edit'})
        store.log(box_id, 'distribute', {'via': 'box_edit'}, answer,
                  session.get('user_id'))
        b = store.get_quantum(box_id)
    if b.get('recipe') == 'text':
        body = b.get('body') or ''
    else:
        body = '\n'.join('・' + (k.get('body') or '')
                          for k in store.children(box_id) if k['kind'] == 'item')
    return render_template('cqm/box_edit.html', box=b, body=body, answer=answer)


@cqm_bp.route('/box/<int:box_id>/copy', methods=['POST'])
@login_required
@editor_required
def box_copy(box_id):
    if not can_edit():
        return redirect(url_for('cqm.index'))
    new_id = store.copy_box(box_id, session.get('user_id'))
    if not new_id:
        return redirect(url_for('cqm.index'))
    return redirect(url_for('cqm.box_edit', box_id=new_id))


@cqm_bp.route('/box/new', methods=['POST'])
@login_required
@editor_required
def box_new():
    if not can_edit():
        return redirect(url_for('cqm.index'))
    title = (request.form.get('title') or '').strip() or '無題の倉庫部品'
    box, err = store.create_box('', title, 'text', '', session.get('user_id'))
    if err or not box:
        return redirect(url_for('cqm.index'))
    return redirect(url_for('cqm.box_edit', box_id=box['id']))


@cqm_bp.route('/box/<int:box_id>/request', methods=['POST'])
@login_required
@editor_required
def make_request(box_id):
    store.create_request(
        box_id,
        (request.form.get('addressee') or '').strip(),
        (request.form.get('message') or '').strip(),
        (request.form.get('due_on') or '').strip(),
        session.get('user_id'))
    return redirect(url_for('cqm.box', box_id=box_id))


@cqm_bp.route('/write/<int:req_id>', methods=['GET', 'POST'])
@login_required
@editor_required
def write(req_id):
    req = store.get_request(req_id)
    if not req:
        return redirect(url_for('cqm.index'))
    b = store.get_quantum(req['box_id'])
    answer = None
    if request.method == 'POST':
        params = {'value': request.form.get('body', ''),
                  'actor_id': session.get('user_id'),
                  'author': req.get('addressee') or '',
                  'origin': 'request:%d' % req_id}
        answer = store.demand(b, 'distribute', params)
        store.log(b['id'], 'distribute', {'request_id': req_id}, answer,
                  session.get('user_id'))
        if answer.ok:
            store.answer_request(req_id, 'answered')
    current = store.demand(b, 'gather', {'actor_id': session.get('user_id')})
    lines = current.value if isinstance(current.value, list) else []
    text = '\n'.join('・' + ln.get('body', '') for ln in lines
                     if isinstance(ln, dict) and ln.get('body'))
    return render_template('cqm/write.html', req=req, box=b, answer=answer,
                           text=text)


# ------------------------------------------------------------ アリーナ（複合部品）

def _json_ok(**kw):
    kw.setdefault('success', True)
    return jsonify(kw)


def _json_ng(msg, code=400):
    return jsonify({'success': False, 'error': msg}), code


@cqm_bp.route('/arena/')
@login_required
def arena_index():
    return redirect(url_for('cqm.index'))


@cqm_bp.route('/arena/new', methods=['POST'])
@login_required
@editor_required
def arena_new():
    if not can_edit():
        return redirect(url_for('cqm.index'))
    level = store.WORK if request.form.get('level') == store.WORK else store.PART
    title = (request.form.get('title') or '').strip() or \
        ('無題の作品' if level == store.WORK else '無題の複合部品')
    aid = store.create_arena(title, session.get('user_id'), level)
    return redirect(url_for('cqm.arena_edit', arena_id=aid))


@cqm_bp.route('/arena/<int:arena_id>/copy', methods=['POST'])
@login_required
@editor_required
def arena_copy(arena_id):
    if not can_edit():
        return redirect(url_for('cqm.index'))
    new_id = store.copy_arena(arena_id, session.get('user_id'))
    if not new_id:
        return redirect(url_for('cqm.index'))
    return redirect(url_for('cqm.arena_edit', arena_id=new_id))


def _with_layout(a, layout):
    """画面が持っている組み立てを重ねる（保存前でも今の姿で答えるため）。"""
    if isinstance(layout, dict) and isinstance(layout.get('root'), dict):
        a = dict(a)
        a['body'] = json.dumps(layout, ensure_ascii=False)
    return a


def _arena_page(arena_id, editing):
    a = store.get_quantum(arena_id)
    if not a or a.get('recipe') != 'arena':
        return redirect(url_for('cqm.index'))
    m = me(); wp = store.work_perm(a, m)
    if not wp.get('view'):
        if not session.get('user_id'):
            return redirect(url_for('auth.login', next=request.path))
        return redirect(url_for('cqm.index'))
    if editing and not wp.get('edit'):
        return redirect(url_for('cqm.arena_view', arena_id=arena_id))
    return render_template('cqm/arena.html', arena=a, layout=store._layout(a),
                           level=(a.get('attrs') or {}).get('level') or store.PART,
                           editing=bool(editing), wperm=wp,
                           me={'names': sorted(m['names']), 'admin': m['admin']})


@cqm_bp.route('/arena/<int:arena_id>')
@login_required
def arena_view(arena_id):
    """閲覧。どの部品も誰でも開ける。"""
    return _arena_page(arena_id, False)


@cqm_bp.route('/arena/<int:arena_id>/edit')
@login_required
@editor_required
def arena_edit(arena_id):
    return _arena_page(arena_id, True)


@cqm_bp.route('/api/arena/<int:arena_id>/meta', methods=['POST'])
@login_required
@editor_required
def arena_meta(arena_id):
    a = store.get_quantum(arena_id)
    if not a or not can_edit(a):
        return _json_ng('編集できません', 403)
    b = request.get_json(silent=True) or {}
    title = (b.get('title') or '').strip() or '無題'
    store.set_arena_meta(arena_id, title, b.get('level'))
    return _json_ok()


@cqm_bp.route('/api/arena/<int:arena_id>/layout')
@login_required
def arena_layout(arena_id):
    """埋め込んだ複合部品を親の画面から組み立てるために，そのレイアウトを渡す。"""
    a = store.get_quantum(arena_id)
    if not a or a.get('recipe') != 'arena':
        return _json_ng('アリーナがありません', 404)
    # 埋め込みの複合部品のレイアウトは，親の作品（via）を見られる人なら描くのに要る．
    # それ以外で複合部品を直に引けるのは編集者だけ
    m = me()
    if not store.is_editor(m):
        try:
            parent = store.get_quantum(int(request.args.get('via') or 0))
        except ValueError:
            parent = None
        if not parent or not store.work_perm(parent, m).get('view'):
            return _json_ng('閲覧の権限がありません', 403)
    return _json_ok(id=arena_id, title=a.get('title') or '',
                    level=(a.get('attrs') or {}).get('level') or store.PART,
                    layout=store._layout(a))


@cqm_bp.route('/api/arena/<int:arena_id>/save', methods=['POST'])
@login_required
@editor_required
def arena_save(arena_id):
    a = store.get_quantum(arena_id)
    if not a or a.get('recipe') != 'arena':
        return _json_ng('アリーナがありません', 404)
    if not can_edit(a):
        return _json_ng('編集できません', 403)
    lay = (request.get_json(silent=True) or {}).get('layout')
    if not isinstance(lay, dict) or not isinstance(lay.get('root'), dict):
        return _json_ng('レイアウトが不正です')
    store.save_layout(arena_id, lay)
    return _json_ok()


@cqm_bp.route('/api/arena/<int:arena_id>/gather', methods=['POST'])
@login_required
def arena_gather(arena_id):
    """情報収集 — 末端のテキストボックスに集めるデマンドを落として回る。"""
    a = store.get_quantum(arena_id)
    if not a or a.get('recipe') != 'arena':
        return _json_ng('アリーナがありません', 404)
    body = request.get_json(silent=True) or {}
    params = body.get('params') or {}
    params['actor_id'] = session.get('user_id')
    if isinstance(body.get('layouts'), dict):
        params['layouts'] = body['layouts']          # 埋め込みの分も，保存前の組み立てで見る
    a = _with_layout(a, body.get('layout'))      # 保存前の組み立ても見る
    m = me(); params['me'] = m
    params['wperm'] = store.work_perm(a, m); params['wacl'] = store.work_acl(a)
    if not params['wperm'].get('view'):
        return _json_ng('閲覧の権限がありません', 403)
    ans = store.demand(a, 'gather', params)      # 見えない部品は倉庫側で伏せて返る
    layouts = {}
    def _collect_layouts(node, depth=0):
        if not isinstance(node, dict) or depth > 6:
            return
        if node.get('t') == 'text' and node.get('embed') and node.get('qid'):
            q = store.get_quantum(node['qid'])
            if q and q.get('recipe') == 'arena':
                layouts[str(q['id'])] = {'title': q.get('title') or '', 'layout': store._layout(q)}
            _collect_layouts(node['embed'], depth + 1)
        for c in node.get('items') or []:
            _collect_layouts(c, depth)
    if ans.value:
        _collect_layouts(ans.value.get('root'))
    store.log(arena_id, 'gather', {'via': 'arena'}, ans, session.get('user_id'))
    ids = [n.get('qid') for n in store.leaves(ans.value.get('root'))
           if isinstance(ans.value, dict) and n.get('qid')] if ans.value else []
    reqs = store.open_requests_for(ids)
    pn = store.user_names(','.join((v.get('persons') or '') for v in reqs.values()))
    gn = store.group_names(','.join((v.get('grps') or '') for v in reqs.values()))
    mine = {str(k): {'id': v['id'], 'addressee': v.get('addressee') or '',
                     'persons': v.get('persons') or '', 'grps': v.get('grps') or '',
                     'persons_names': [{'id': i, 'name': pn.get(i, '#%s' % i)} for i in store._ids(v.get('persons') or '')],
                     'grps_names': [{'id': i, 'name': gn.get(i, '#%s' % i)} for i in store._ids(v.get('grps') or '')],
                     'message': v.get('message') or '', 'status': v.get('status') or '',
                     'due_on': str(v['due_on']) if v.get('due_on') else ''}
            for k, v in reqs.items()}
    return _json_ok(answer=ans.as_dict(), requests=mine, layouts=layouts)


@cqm_bp.route('/api/arena/<int:arena_id>/request', methods=['POST'])
@login_required
@editor_required
def arena_request(arena_id):
    """情報要求 — 末端のテキストボックスごとに執筆の依頼を発する。"""
    a = store.get_quantum(arena_id)
    if not a or a.get('recipe') != 'arena':
        return _json_ng('アリーナがありません', 404)
    if not can_edit(a):
        return _json_ng('編集できません', 403)
    body = request.get_json(silent=True) or {}
    message = (body.get('message') or '').strip()
    due_on = (body.get('due_on') or '').strip()
    only = set(str(x) for x in (body.get('refs') or []))
    a = _with_layout(a, body.get('layout'))
    made, skipped = [], []
    for nd in store.leaves(store._layout(a).get('root')):
        ref = str(nd.get('ref') or '')
        if not ref or (only and ref not in only):
            continue
        q = store.resolve(ref)
        if not q or q['kind'] != 'box':
            skipped.append({'ref': ref, 'why': '依頼できる箱ではありません'})
            continue
        if store.open_requests_for([q['id']]):
            skipped.append({'ref': ref, 'why': 'すでに依頼中です'})
            continue
        addressee = (q.get('attrs') or {}).get('addressee') or nd.get('addressee') or ''
        rid = store.create_request(q['id'], addressee, message, due_on,
                                   session.get('user_id'))
        made.append({'ref': ref, 'request_id': rid, 'addressee': addressee,
                     'title': q.get('title') or ''})
    return _json_ok(made=made, skipped=skipped)


@cqm_bp.route('/api/arena/<int:arena_id>/write', methods=['POST'])
@login_required
def arena_write(arena_id):
    """執筆者が入れた値を，指し先の箱へ配る。"""
    a = store.get_quantum(arena_id)
    if not a or a.get('recipe') != 'arena':
        return _json_ng('アリーナがありません', 404)
    values = (request.get_json(silent=True) or {}).get('values')
    if not isinstance(values, dict) or not values:
        return _json_ng('送る中身がありません')
    m = me(); wp = store.work_perm(a, m); wacl = store.work_acl(a)
    for ref in list(values):
        q = store.resolve(ref)
        if q and q.get('kind') == 'box' and q.get('recipe') != 'arena':
            if not store.box_perm(q, m, wp, wacl).get('write'):
                return _json_ng('「%s」に書く権限がありません（凍結中か，担当でない）' % (q.get('title') or ref), 403)
    ans = store.demand(a, 'distribute', {'value': values,
                                         'actor_id': session.get('user_id'),
                                         'origin': 'arena:%d' % arena_id})
    store.log(arena_id, 'distribute', {'via': 'arena', 'n': len(values)}, ans,
              session.get('user_id'))
    if ans.status != 'ng':
        for nd in store.leaves(store._layout(a).get('root')):
            ref = str(nd.get('ref') or '')
            if ref not in values:
                continue
            q = store.resolve(ref)
            if q:
                for rid in [r['id'] for r in store.list_requests(q['id'], True)]:
                    store.answer_request(rid, 'answered')
    return _json_ok(answer=ans.as_dict())


@cqm_bp.route('/api/box/new', methods=['POST'])
@login_required
@editor_required
def api_box_new():
    """倉庫部品をその場で生やす（アリーナの編集中に使う）。"""
    if not can_edit():
        return _json_ng('編集できません', 403)
    b = request.get_json(silent=True) or {}
    title = (b.get('title') or '').strip()
    if not title:
        return _json_ng('題名を入れてください')
    box, err = store.create_box((b.get('key_path') or '').strip(), title,
                               (b.get('recipe') or 'seq').strip(),
                               (b.get('addressee') or '').strip(),
                               session.get('user_id'))
    if err:
        return _json_ng(err)
    return _json_ok(box={'id': box['id'], 'key_path': box.get('key_path') or '',
                         'title': box.get('title') or '',
                         'recipe': box.get('recipe') or ''})


@cqm_bp.route('/api/box/<int:box_id>/text', methods=['POST'])
@login_required
def api_box_text(box_id):
    """型テキストの倉庫部品に，その場で中身を書き込む。"""
    b = store.get_quantum(box_id)
    if not b or b.get('recipe') != 'text':
        return _json_ng('型テキストの倉庫部品ではありません', 404)
    body0 = request.get_json(silent=True) or {}
    m = me()
    a = store.get_quantum(int(body0.get('arena') or 0)) if body0.get('arena') else None
    wp = store.work_perm(a, m) if a else {'view': True, 'edit': m['admin'] or b.get('owner_id') == m['id']}
    wacl = store.work_acl(a) if a else {}
    if not store.box_perm(b, m, wp, wacl).get('write'):
        return _json_ng('書く権限がありません（凍結中か，担当でない）', 403)
    html = body0.get('html') or ''
    ans = store.demand(b, 'distribute', {'value': html,
                                         'actor_id': session.get('user_id'),
                                         'origin': 'arena_inline'})
    store.log(box_id, 'distribute', {'via': 'arena_inline'}, ans,
              session.get('user_id'))
    title = b.get('title') or ''
    if title in ('', '（無題）', '無題'):
        plain = re.sub(r'<[^>]+>', '', html).strip().replace('\n', ' ')
        if plain:
            title = plain[:24]
            store.set_box_meta(box_id, title,
                               (b.get('attrs') or {}).get('addressee') or '')
    return _json_ok(answer=ans.as_dict(), title=title)


@cqm_bp.route('/api/arena/<int:arena_id>/acl', methods=['POST'])
@login_required
@editor_required
def arena_acl(arena_id):
    """作品の閲覧・編集の割り当てと凍結。"""
    a = store.get_quantum(arena_id)
    if not a or a.get('recipe') != 'arena':
        return _json_ng('アリーナがありません', 404)
    if not can_edit(a):
        return _json_ng('編集できません', 403)
    b = request.get_json(silent=True) or {}
    store.set_work_acl(arena_id, (b.get('policy') or 'private'), b.get('groups') or [],
                       bool(b.get('frozen')), (b.get('due') or '').strip())
    return _json_ok(acl=store.work_acl(store.get_quantum(arena_id)))


@cqm_bp.route('/api/box/<int:box_id>/acl', methods=['POST'])
@login_required
@editor_required
def box_acl(box_id):
    """部品の編集・執筆・閲覧の割り当て。執筆の担当には依頼も立てる。"""
    b = store.get_quantum(box_id)
    if not b or b.get('kind') != 'box' or b.get('recipe') == 'arena':
        return _json_ng('倉庫部品がありません', 404)
    body0 = request.get_json(silent=True) or {}
    a = store.get_quantum(int(body0.get('arena') or 0)) if body0.get('arena') else None
    if a and not can_edit(a):
        return _json_ng('編集できません', 403)
    if not a:
        m = me()
        if not (m['admin'] or b.get('owner_id') == m['id']):
            return _json_ng('編集できません', 403)
    store.set_box_acl(box_id, body0.get('edit') or [], store.box_acl(b)['write'],
                      body0.get('view') or [], bool(body0.get('open')),
                      body0.get('view_mode') or 'and')
    due = (body0.get('due') or '').strip() or (store.work_acl(a)['due'] if a else '')
    persons = body0.get('write_p') if 'write_p' in body0 else body0.get('write') or ''
    grps = body0.get('write_g') if 'write_g' in body0 else ''
    rid = store.assign_writers(box_id, persons, grps, (body0.get('message') or '').strip(),
                               due, session.get('user_id'))
    return _json_ok(acl=store.box_acl(store.get_quantum(box_id)),
                    requests=([{'request_id': rid}] if rid else []))


@cqm_bp.route('/api/box/<int:box_id>/state', methods=['POST'])
@login_required
def box_state(box_id):
    """執筆者が自分の状態を知らせる：writing（執筆中）／done（執筆完了）。
    編集者は open（許可・再開）／frozen（凍結）も指示できる。"""
    b = store.get_quantum(box_id)
    if not b:
        return _json_ng('倉庫部品がありません', 404)
    body0 = request.get_json(silent=True) or {}
    state = (body0.get('state') or '').strip()
    a = store.get_quantum(int(body0.get('arena') or 0)) if body0.get('arena') else None
    m = me(); wp = store.work_perm(a, m) if a else {'view': True, 'edit': m['admin']}
    wacl = store.work_acl(a) if a else {}
    pm = store.box_perm(b, m, wp, wacl)
    if state in ('writing', 'done'):
        if not (pm.get('writer') or pm.get('edit')):
            return _json_ng('担当ではありません', 403)
        r = store.active_request(box_id)
        rid = r['id'] if r and (pm.get('edit') or (m['names'] & store.request_names(r))) else None
        if not rid:
            rid = store.create_request(box_id, '', '（本人が着手）', wacl.get('due') or '', m['id'],
                                       persons=next(iter(sorted(m['names']))), grps='')
        store.update_request(rid, status=state)
        return _json_ok(request_id=rid, state=state)
    if state in ('open', 'frozen'):
        if not pm.get('edit'):
            return _json_ng('編集者だけが指示できます', 403)
        acl = store.box_acl(b)
        store.set_box_acl(box_id, acl['edit'], acl['write'], acl['view'], state == 'open', acl['view_mode'])
        return _json_ok(state=state)
    return _json_ng('状態が不正です')


@cqm_bp.route('/api/arena/<int:arena_id>/requests')
@login_required
@editor_required
def arena_requests(arena_id):
    """依頼一覧：この作品（埋め込みの中も）の部品に出ている依頼。"""
    a = store.get_quantum(arena_id)
    if not a or a.get('recipe') != 'arena':
        return _json_ng('アリーナがありません', 404)
    m = me()
    ids = []
    def walk(arena, depth):
        if depth > 6:
            return
        for nd in store.leaves(store._layout(arena).get('root')):
            q = store.resolve(nd.get('ref'))
            if not q:
                continue
            if q.get('recipe') == 'arena':
                walk(q, depth + 1)
            elif q.get('kind') == 'box':
                ids.append(q['id'])
    walk(a, 0)
    rows = store.requests_for_boxes(list(dict.fromkeys(ids)))
    out = []
    pn = store.user_names(','.join((r.get('persons') or '') for r in rows))
    gn = store.group_names(','.join((r.get('grps') or '') for r in rows))
    for r in rows:
        pids = store._ids(r.get('persons') or ''); gids = store._ids(r.get('grps') or '')
        out.append({'id': r['id'], 'box_id': r['box_id'], 'title': r.get('title') or '',
                    'key_path': r.get('key_path') or '', 'addressee': r.get('addressee') or '',
                    'persons': ','.join(str(i) for i in pids), 'grps': ','.join(str(i) for i in gids),
                    'persons_names': [{'id': i, 'name': pn.get(i, '#%s' % i)} for i in pids],
                    'grps_names': [{'id': i, 'name': gn.get(i, '#%s' % i)} for i in gids],
                    'message': r.get('message') or '', 'due_on': str(r['due_on']) if r.get('due_on') else '',
                    'status': r.get('status') or '', 'created_at': str(r.get('created_at') or '')[:16],
                    'state_at': str(r.get('answered_at') or '')[:16]})
    return _json_ok(requests=out, work_acl=store.work_acl(a),
                    parts=[{'id': i, 'title': (store.get_quantum(i) or {}).get('title') or ''}
                           for i in dict.fromkeys(ids)])


@cqm_bp.route('/api/users')
@login_required
@editor_required
def api_users():
    """担当を選ぶためのユーザ一覧（氏名・メール・所属で絞る）。"""
    rows = store.users_list(request.args.get('q') or '')
    return _json_ok(users=[{'id': r['id'], 'name': r.get('full_name') or r.get('email') or '',
                            'email': r.get('email') or '', 'affiliation': r.get('affiliation') or '',
                            'category': r.get('category') or ''} for r in rows])


@cqm_bp.route('/api/groups')
@login_required
@editor_required
def api_groups():
    """担当を選ぶためのまいぐるのグループ一覧。"""
    rows = store.groups_list()
    return _json_ok(groups=[{'id': r['id'], 'name': r['name'], 'description': r.get('description') or ''}
                            for r in rows])


@cqm_bp.route('/api/request/<int:rid>', methods=['POST'])
@login_required
@editor_required
def request_update(rid):
    """執筆担当表からの直し：相手・依頼文・期限・状態。編集者のみ。"""
    r = store.get_request(rid)
    if not r:
        return _json_ng('依頼がありません', 404)
    body0 = request.get_json(silent=True) or {}
    a = store.get_quantum(int(body0.get('arena') or 0)) if body0.get('arena') else None
    if not (a and can_edit(a)):
        m = me()
        if not m['admin']:
            return _json_ng('編集できません', 403)
    if 'persons' in body0 or 'grps' in body0:
        store.assign_writers(r['box_id'], body0.get('persons', r.get('persons') or ''),
                             body0.get('grps', r.get('grps') or ''),
                             body0.get('message'), body0.get('due_on'), session.get('user_id'))
    else:
        store.update_request(rid, message=body0.get('message'), due_on=body0.get('due_on'),
                             status=body0.get('status'))
    if body0.get('status') in ('open', 'writing', 'done', 'closed', 'frozen'):
        store.update_request(rid, status=body0['status'])
    return _json_ok()


@cqm_bp.route('/api/request/new', methods=['POST'])
@login_required
@editor_required
def request_new():
    """執筆担当表から，部品を選んで依頼を立てる。"""
    body0 = request.get_json(silent=True) or {}
    a = store.get_quantum(int(body0.get('arena') or 0)) if body0.get('arena') else None
    if not (a and can_edit(a)):
        return _json_ng('編集できません', 403)
    box_id = int(body0.get('box_id') or 0)
    rid = store.assign_writers(box_id, body0.get('persons') or '', body0.get('grps') or '',
                               (body0.get('message') or '').strip(), (body0.get('due_on') or '').strip(),
                               session.get('user_id'))
    return _json_ok(request_id=rid)


@cqm_bp.route('/arena/<int:arena_id>/assignments')
@login_required
@editor_required
def arena_assignments(arena_id):
    """執筆担当表（別窓で開く本格的な一覧）。"""
    a = store.get_quantum(arena_id)
    if not a or a.get('recipe') != 'arena':
        return redirect(url_for('cqm.index'))
    return render_template('cqm/assignments.html', arena=a, can_edit=can_edit(a))


@cqm_bp.route('/arena/<int:arena_id>/delete', methods=['POST'])
@cqm_bp.route('/box/<int:arena_id>/delete', methods=['POST'])
@login_required
@editor_required
def delete_quantum(arena_id):
    """作品・複合部品・倉庫部品を消す（編集者だけ。指されていれば断る）。"""
    rep = store.delete_quantum(arena_id, force=(request.form.get('force') == '1'))
    if not rep.get('ok'):
        return render_template('cqm/deleted.html', rep=rep, qid=arena_id)
    return redirect(url_for('cqm.index'))


# ------------------------------------------------------------ 書き出しと読み込み

@cqm_bp.route('/arena/<int:arena_id>/export.json')
@login_required
@editor_required
def export_json(arena_id):
    a = store.get_quantum(arena_id)
    if not a or a.get('recipe') != 'arena':
        return _json_ng('アリーナがありません', 404)
    data = store.export_bundle(arena_id, with_acl=(request.args.get('acl') == '1'))
    body = json.dumps(data, ensure_ascii=False, indent=2)
    name = 'cqm_%s_%s.json' % (arena_id, store._now().strftime('%Y%m%d_%H%M%S'))
    return Response(body, mimetype='application/json; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename="%s"' % name})


@cqm_bp.route('/arena/<int:arena_id>/export.xlsx')
@login_required
def export_xlsx(arena_id):
    a = store.get_quantum(arena_id)
    if not a or a.get('recipe') != 'arena':
        return redirect(url_for('cqm.index'))
    if not store.work_perm(a, me()).get('view'):
        return redirect(url_for('cqm.index'))
    from . import xlsx_io
    data = xlsx_io.build_xlsx(arena_id)
    if data is None:
        return redirect(url_for('cqm.index'))
    name = 'cqm_%s_%s.xlsx' % (arena_id, store._now().strftime('%Y%m%d_%H%M%S'))
    return Response(data, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': 'attachment; filename="%s"' % name})


@cqm_bp.route('/transfer', methods=['GET', 'POST'])
@cqm_bp.route('/arena/<int:arena_id>/transfer', methods=['GET', 'POST'])
@login_required
@editor_required
def transfer(arena_id=None):
    """書き出しと読み込みの画面。"""
    a = store.get_quantum(arena_id) if arena_id else None
    report = None
    if request.method == 'POST' and request.form.get('gc') == 'parts':
        n = store.delete_orphan_parts()
        report = {'ok': True, 'kind': 'sql', 'text': 'どこからも指されていない複合部品を %d 件消しました' % n}
        return render_template('cqm/transfer.html', arena=a, report=report, info=store.store_info(),
                               orphans=store.orphan_parts(),
                               works=store.list_arenas(store.WORK), parts=store.list_arenas(store.PART))
    if request.method == 'POST' and request.form.get('sql') in ('store', 'load'):
        try:
            if request.form.get('sql') == 'store':
                n = store.sql_store(); report = {'ok': True, 'kind': 'sql', 'text': 'SQL に格納しました：量子 %d，並び %d，依頼 %d' % (n['quanta'], n['links'], n['requests'])}
            else:
                n = store.sql_load(); report = {'ok': True, 'kind': 'sql', 'text': 'SQL から読み込みました：量子 %d，並び %d，依頼 %d（倉庫のファイルを置き換えました）' % (n['quanta'], n['links'], n['requests'])}
        except Exception as e:
            report = {'ok': False, 'error': 'SQL との受け渡しに失敗：%s' % e}
        return render_template('cqm/transfer.html', arena=a, report=report, info=store.store_info(),
                               orphans=store.orphan_parts(),
                               works=store.list_arenas(store.WORK), parts=store.list_arenas(store.PART))
    if request.method == 'POST':
        f = request.files.get('file')
        mode = request.form.get('mode') or 'merge'
        if not f or not f.filename:
            report = {'ok': False, 'error': 'ファイルを選んでください'}
        elif f.filename.lower().endswith('.json'):
            try:
                bundle = json.loads(f.read().decode('utf-8'))
            except Exception as e:
                bundle = None
                report = {'ok': False, 'error': '読めません：%s' % e}
            if bundle is not None:
                report = store.import_bundle(bundle, session.get('user_id'), mode)
                report['kind'] = 'json'
        elif f.filename.lower().endswith('.xlsx'):
            from . import xlsx_io
            values, err = xlsx_io.read_xlsx(f.read())
            if err:
                report = {'ok': False, 'error': err}
            else:
                m = me()
                wp = store.work_perm(a, m) if a else {'view': True, 'edit': True}
                wacl = store.work_acl(a) if a else {}
                rep = xlsx_io.apply_values(values, m, wp, wacl, session.get('user_id'))
                rep.update(ok=True, kind='xlsx'); report = rep
        else:
            report = {'ok': False, 'error': '.json か .xlsx を選んでください'}
    return render_template('cqm/transfer.html', arena=a, report=report, info=store.store_info(),
                           orphans=store.orphan_parts(),
                           works=store.list_arenas(store.WORK), parts=store.list_arenas(store.PART))


@cqm_bp.route('/api/search')
@login_required
@editor_required
def api_search():
    scope = request.args.get('scope') or 'store'
    try:
        exclude = int(request.args.get('exclude') or 0) or None
    except ValueError:
        exclude = None
    return _json_ok(items=store.search_boxes(request.args.get('q') or '',
                                             scope=scope, exclude_id=exclude))
