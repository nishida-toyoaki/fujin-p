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

"""すみずみ（sumizumi）— SymPy／NumPy による導出

教材データ（content/theorems.json）の脇道 kind='sympy' は，ここにある導出を名前で呼ぶ．
画面にはあらかじめ「命題 P を示すために，SymPy を用いて X から Y を導く」を出しておき，
ボタンが押されたらこの関数が実際に計算して，途中の式を順に返す．

学習者が書いた式をサーバで評価する機能は持たない（SymPy の式の読み込みは内部で eval を
使うため，注入の入口になる）．導くのは，あらかじめ用意した式だけである．

各関数は次の形の dict を返す．
    steps    : [{'op': 何をしたか, 'code': SymPy で行った操作, 'latex': その結果}]
    reached  : bool  目標 Y に届いたか
    result   : str   届いたときに言えること（「よって P」）
結果は引数を持たないので lru_cache で一度だけ計算する．
"""

import time
from functools import lru_cache

import numpy as np
import sympy as sp

f = sp.symbols('f', real=True)
F = sp.symbols('F', positive=True)
u = sp.symbols('u', real=True)
t_nz = sp.symbols('t', real=True, nonzero=True)
w_nz = sp.symbols('w', real=True, nonzero=True)
n = sp.symbols('n', integer=True)
m = sp.symbols('m', integer=True)
k_nz = sp.symbols('k', integer=True, nonzero=True)
I, pi = sp.I, sp.pi


def _lx(e):
    return sp.latex(e)


def _step(op, code, latex):
    return {'op': op, 'code': code, 'latex': latex}


def _sinc(z):
    return sp.sin(pi * z) / (pi * z)


# ─────────────────────────────────────────────────────────────────
# 節点1：主張（第6行）
# ─────────────────────────────────────────────────────────────────

@lru_cache(maxsize=None)
def d_sinc_interp():
    steps = []
    tt = sp.symbols('t', real=True)
    e = sp.Function('sinc')(2 * F * tt - n).subs(tt, m / (2 * F))
    steps.append(_step('サンプル点 t=m/(2F) を sinc(2Ft−n) に入れる', 'sinc(2*F*t - n).subs(t, m/(2*F))',
                       r'\mathrm{sinc}\!\left(2F\cdot\frac{m}{2F}-n\right)'))
    steps.append(_step('2F が約分されて，sinc の中身は整数 k=m−n になる', '（subs の結果）',
                       _lx(e).replace(r'\operatorname{sinc}', r'\mathrm{sinc}') + r'=\mathrm{sinc}(k)'))
    val = sp.simplify(sp.sin(pi * k_nz) / (pi * k_nz))
    steps.append(_step('k≠0 のとき：sinc(k)=sin(πk)/(πk)．k は整数なので sin(πk)=0',
                       "k = sp.symbols('k', integer=True, nonzero=True)\nsp.simplify(sp.sin(sp.pi*k)/(sp.pi*k))",
                       r'\frac{\sin \pi k}{\pi k}=%s' % _lx(val)))
    lim = sp.limit(sp.sin(pi * u) / (pi * u), u, 0)
    steps.append(_step('k=0 のとき：0/0 になるので極限をとる', 'sp.limit(sp.sin(sp.pi*u)/(sp.pi*u), u, 0)',
                       r'\lim_{u\to0}\frac{\sin\pi u}{\pi u}=%s' % _lx(lim)))
    steps.append(_step('和に戻すと，n=m の項だけが残る', '',
                       r'\sum_{n} x\!\left(\tfrac{n}{2F}\right)\mathrm{sinc}(m-n)=x\!\left(\tfrac{m}{2F}\right)'))
    ok = val == 0 and lim == 1
    return {'steps': steps, 'reached': ok,
            'result': 'sinc(2Ft−n) はサンプル点 m/(2F) で n=m のとき 1，それ以外で 0．よって sinc の和はサンプル点でちょうどサンプル値を通る．'}


# ─────────────────────────────────────────────────────────────────
# 節点2：フーリエ変換と帯域制限
# ─────────────────────────────────────────────────────────────────

@lru_cache(maxsize=None)
def d_flat_spectrum():
    steps = []
    integrand = sp.exp(I * 2 * pi * f * t_nz)
    raw = sp.integrate(integrand, (f, -F, F))
    steps.append(_step('X(f)=1（|f|≦F）として，逆変換の積分を −F から F まで計算する（t≠0）',
                       'sp.integrate(sp.exp(sp.I*2*sp.pi*f*t), (f, -F, F))', _lx(raw)))
    s = sp.simplify(raw.rewrite(sp.sin))
    steps.append(_step('指数関数を sin・cos で書き直して整理する（オイラーの公式）', 'sp.simplify(expr.rewrite(sp.sin))', _lx(s)))
    target = 2 * F * _sinc(2 * F * t_nz)
    steps.append(_step('目標の形 2F sinc(2Ft) を書く', 'target = 2*F*sp.sin(sp.pi*2*F*t)/(sp.pi*2*F*t)', r'2F\,\frac{\sin(\pi\cdot 2Ft)}{\pi\cdot 2Ft}'))
    diff = sp.simplify(s - target)
    steps.append(_step('差をとって簡約する', 'sp.simplify(expr - target)', _lx(diff)))
    return {'steps': steps, 'reached': diff == 0,
            'result': '差が 0 になった．よって，帯域 F の平らなスペクトルを持つ信号は 2F sinc(2Ft) である．t=0 では積分は 2F で，sinc(0)=1 と合う．'}


@lru_cache(maxsize=None)
def d_boundary():
    steps = []
    nr = sp.symbols('n', real=True)
    e_real = sp.sin(2 * pi * F * (nr / (2 * F)))
    steps.append(_step('x(t)=sin(2πFt) にサンプル点 t=n/(2F) を入れる', 'sp.sin(2*sp.pi*F*t).subs(t, n/(2*F))',
                       r'\sin\!\left(2\pi F\cdot\frac{n}{2F}\right)'))
    steps.append(_step('F が約分される', '（subs の結果．n を実数として扱った場合）', _lx(e_real)))
    e = sp.sin(2 * pi * F * (n / (2 * F)))
    steps.append(_step('n は整数なので sin(πn)=0', "n = sp.symbols('n', integer=True)", _lx(e)))
    vals = [sp.sin(pi * j) for j in range(-3, 4)]
    steps.append(_step('n=−3〜3 で値を並べる', '[sp.sin(sp.pi*j) for j in range(-3, 4)]', r',\ '.join(_lx(v) for v in vals)))
    ok = sp.simplify(e) == 0 and all(v == 0 for v in vals)
    return {'steps': steps, 'reached': ok,
            'result': 'サンプルはすべて 0 になった．よって sin(2πFt) は，間隔 1/(2F) の標本化では 0 という信号と区別できない．'
                      'このとき X(f) は f=±F に点（デルタ関数）を持ち，「|f|>F で 0」の境目で破綻している．'}


@lru_cache(maxsize=None)
def d_alias():
    steps = []
    a = sp.cos(2 * pi * sp.Rational(3, 2) * n / 2)
    b = sp.cos(2 * pi * sp.Rational(1, 2) * n / 2)
    steps.append(_step('1.5 Hz と 0.5 Hz の余弦波に，サンプル点 t=n/2（fs=2）を入れる',
                       'a = sp.cos(2*sp.pi*sp.Rational(3,2)*n/2)\nb = sp.cos(2*sp.pi*sp.Rational(1,2)*n/2)',
                       r'%s\quad\text{と}\quad %s' % (_lx(a), _lx(b))))
    steps.append(_step('3π/2·n = 2πn − π/2·n と書き直す（cos は周期 2π）', '', r'\cos\!\left(2\pi n-\tfrac{\pi n}{2}\right)=\cos\tfrac{\pi n}{2}'))
    diffs = [sp.simplify((a - b).subs(n, j)) for j in range(-6, 7)]
    steps.append(_step('n=−6〜6 で差をとる', '[sp.simplify((a-b).subs(n, j)) for j in range(-6, 7)]', r',\ '.join(_lx(v) for v in diffs)))
    ok = all(v == 0 for v in diffs)
    return {'steps': steps, 'reached': ok,
            'result': '差はすべて 0 になった．よって fs=2 では 1.5 Hz の波が 0.5 Hz の波に化けて見える（エイリアシング）．'}


@lru_cache(maxsize=None)
def d_tri_spectrum():
    steps = []
    X = 1 - sp.Abs(f) / F
    raw = sp.integrate(X * sp.exp(I * 2 * pi * f * t_nz), (f, -F, F))
    steps.append(_step('X(f)=1−|f|/F（|f|≦F）の逆変換を計算する（t≠0）',
                       'sp.integrate((1-sp.Abs(f)/F)*sp.exp(sp.I*2*sp.pi*f*t), (f, -F, F))', _lx(sp.simplify(raw))))
    s = sp.simplify(raw.rewrite(sp.sin))
    steps.append(_step('sin で書き直して整理する', 'sp.simplify(expr.rewrite(sp.sin))', _lx(s)))
    target = F * _sinc(F * t_nz) ** 2
    diff = sp.simplify(s - target)
    steps.append(_step('目標 F sinc²(Ft) との差を簡約する', 'sp.simplify(expr - F*(sp.sin(sp.pi*F*t)/(sp.pi*F*t))**2)', _lx(diff)))
    return {'steps': steps, 'reached': diff == 0,
            'result': '差が 0 になった．よって三角形のスペクトルの信号は F sinc²(Ft) で，これは帯域 F に制限された信号の例になる．'}


# ─────────────────────────────────────────────────────────────────
# 節点3：区間 [−F,F] のフーリエ級数
# ─────────────────────────────────────────────────────────────────

@lru_cache(maxsize=None)
def d_orth():
    steps = []
    kr = sp.symbols('k', real=True, nonzero=True)
    raw = sp.integrate(sp.exp(I * pi * kr * f / F), (f, -F, F))
    steps.append(_step('番号の差 k=m−n≠0 の指数関数 e^{iπkf/F} を区間 [−F,F] で積分する（まず k を実数として）',
                       "k = sp.symbols('k', real=True, nonzero=True)\nsp.integrate(sp.exp(sp.I*sp.pi*k*f/F), (f, -F, F))", _lx(raw)))
    s = sp.simplify(raw.rewrite(sp.sin))
    steps.append(_step('sin で書き直す', 'sp.simplify(expr.rewrite(sp.sin))', _lx(s)))
    z = sp.simplify(s.subs(kr, k_nz))
    steps.append(_step('k は 0 でない整数なので sin(πk)=0', "k = sp.symbols('k', integer=True, nonzero=True)\nsp.simplify(expr.subs(...))", _lx(z)))
    same = sp.integrate(sp.Integer(1), (f, -F, F))
    steps.append(_step('番号が同じ（k=0）なら被積分関数は 1', 'sp.integrate(1, (f, -F, F))', _lx(same)))
    return {'steps': steps, 'reached': z == 0 and same == 2 * F,
            'result': '番号の違う指数関数どうしは区間 [−F,F] の上で積分すると 0，同じなら 2F．よって両辺に e^{…} をかけて積分すれば，係数を一つずつ取り出せる．'}


@lru_cache(maxsize=None)
def d_tri_coef():
    steps = []
    g = 1 - sp.Abs(u)
    nn = sp.symbols('n', integer=True, positive=True)
    cn = sp.simplify(sp.integrate(g * sp.exp(-I * pi * nn * u), (u, -1, 1)) / 2)
    steps.append(_step('例：区間 [−1,1]（長さ 2）の上の 1−|u| の係数 c_n を一般の n≧1 で計算する',
                       "sp.integrate((1-sp.Abs(u))*sp.exp(-sp.I*sp.pi*n*u), (u, -1, 1))/2", _lx(cn)))
    c0 = sp.integrate(g, (u, -1, 1)) / 2
    steps.append(_step('n=0 の係数', 'sp.integrate(1-sp.Abs(u), (u, -1, 1))/2', _lx(c0)))
    closed = (1 - (-1) ** nn) / (pi ** 2 * nn ** 2)
    diff = sp.simplify(cn - closed)
    steps.append(_step('目標 (1−(−1)ⁿ)/(π²n²) との差を簡約する', 'sp.simplify(cn - (1-(-1)**n)/(sp.pi**2*n**2))', _lx(diff)))
    return {'steps': steps, 'reached': diff == 0 and c0 == sp.Rational(1, 2),
            'result': '係数は 1/n² の速さで小さくなる．関数が連続で角があるだけなので，部分和は一様に元の関数へ近づく．'}


# ─────────────────────────────────────────────────────────────────
# 節点4：係数はサンプル値
# ─────────────────────────────────────────────────────────────────

@lru_cache(maxsize=None)
def d_coef_sample():
    steps = []
    t = sp.symbols('t', real=True)
    Xf = sp.Function('X')
    x_t = sp.Integral(Xf(f) * sp.exp(I * 2 * pi * f * t), (f, -F, F))
    steps.append(_step('節点2の式：x(t) を有限区間の積分として書く', 'x_t = sp.Integral(X(f)*sp.exp(sp.I*2*sp.pi*f*t), (f, -F, F))', r'x(t)=' + _lx(x_t)))
    cn = sp.Integral(Xf(f) * sp.exp(I * 2 * pi * n * f / (2 * F)), (f, -F, F)) / (2 * F)
    steps.append(_step('節点3の係数 c_n の定義', 'c_n = sp.Integral(X(f)*sp.exp(sp.I*2*sp.pi*n*f/(2*F)), (f, -F, F))/(2*F)', r'c_n=' + _lx(cn)))
    xs = x_t.subs(t, n / (2 * F))
    steps.append(_step('x(t) の t に n/(2F) を入れる', 'x_t.subs(t, n/(2*F))', r'x\!\left(\tfrac{n}{2F}\right)=' + _lx(xs)))
    diff = sp.simplify(cn - xs / (2 * F))
    steps.append(_step('c_n と x(n/2F)/(2F) の差を簡約する（X は未知の関数のまま）', 'sp.simplify(c_n - x_t.subs(t, n/(2*F))/(2*F))', _lx(diff)))
    # 具体例
    Fv = sp.Integer(1)
    X = 1 - sp.Abs(f)
    rows = []
    ok_ex = True
    for j in range(0, 5):
        c = sp.simplify(sp.integrate(X * sp.exp(I * 2 * pi * j * f / 2), (f, -1, 1)) / 2)
        s = sp.simplify(sp.integrate(X * sp.exp(I * 2 * pi * f * sp.Rational(j, 2)), (f, -1, 1)) / 2)
        ok_ex = ok_ex and sp.simplify(c - s) == 0
        rows.append(r'n=%d:\ c_{%d}=%s,\ \tfrac{1}{2}x(\tfrac{%d}{2})=%s' % (j, j, _lx(c), j, _lx(s)))
    steps.append(_step('念のため具体例（F=1，X(f)=1−|f|）で値を並べる', 'for n in range(5): ...', r'\begin{array}{l}' + r'\\ '.join(rows) + r'\end{array}'))
    return {'steps': steps, 'reached': diff == 0 and ok_ex,
            'result': '差は 0．X(f) が何であっても，級数の係数 c_n はサンプル値 x(n/2F) を 2F で割ったものになる．'
                      'よってサンプル値がわかれば係数がわかり，係数がわかれば X(f) が決まり，X(f) が決まれば x(t) が決まる．'}


# ─────────────────────────────────────────────────────────────────
# 節点6：積分して sinc，第6行へ戻る
# ─────────────────────────────────────────────────────────────────

@lru_cache(maxsize=None)
def d_kernel():
    steps = []
    t = (w_nz + n) / (2 * F)
    steps.append(_step('w=2Ft−n と置く．すると t−n/(2F)=w/(2F)', "w = sp.symbols('w', real=True, nonzero=True)\nt = (w + n)/(2*F)",
                       r't-\frac{n}{2F}=\frac{w}{2F}'))
    raw = sp.integrate(sp.exp(I * 2 * pi * f * (t - n / (2 * F))), (f, -F, F))
    steps.append(_step('ずらした指数関数を −F から F まで積分する', 'sp.integrate(sp.exp(sp.I*2*sp.pi*f*(t - n/(2*F))), (f, -F, F))', _lx(raw)))
    s = sp.simplify(raw)
    steps.append(_step('整理する', 'sp.simplify(expr)', _lx(s)))
    target = 2 * F * _sinc(w_nz)
    diff = sp.simplify((s - target).rewrite(sp.sin))
    steps.append(_step('目標 2F sinc(w)=2F sin(πw)/(πw) との差を簡約する', 'sp.simplify((expr - 2*F*sp.sin(sp.pi*w)/(sp.pi*w)).rewrite(sp.sin))', _lx(diff)))
    return {'steps': steps, 'reached': diff == 0,
            'result': '差は 0．よって ∫_{−F}^{F} e^{i2πf(t−n/2F)} df = 2F sinc(2Ft−n)．w=0 では積分は 2F で sinc(0)=1 と合う．'}


@lru_cache(maxsize=None)
def d_assemble():
    steps = []
    a = sp.symbols('a_n')
    w = sp.symbols('w')
    term = (a / (2 * F)) * 2 * F * sp.Function('sinc')(w)
    steps.append(_step('第4行の和の1項：係数 c_n=a_n/(2F) と積分 2F sinc(2Ft−n) をかける（a_n=x(n/2F)，w=2Ft−n）',
                       "a = sp.symbols('a_n')\nterm = (a/(2*F)) * 2*F * sinc(w)", r'\frac{a_n}{2F}\cdot 2F\,\mathrm{sinc}(w)'))
    steps.append(_step('2F が打ち消し合う', 'sp.simplify(term)', _lx(sp.simplify(term)).replace(r'\operatorname{sinc}', r'\mathrm{sinc}')))
    steps.append(_step('和に戻すと第6行になる', '', r'x(t)=\sum_{n} x\!\left(\tfrac{n}{2F}\right)\mathrm{sinc}(2Ft-n)'))
    # 数値で確かめる
    tg = np.linspace(-3, 3, 601)
    exact = np.sinc(tg) ** 2
    errs = []
    for N in (10, 100, 1000):
        ns = np.arange(-N, N + 1)
        rec = ((np.sinc(ns / 2.0) ** 2)[None, :] * np.sinc(2 * tg[:, None] - ns[None, :])).sum(axis=1)
        errs.append(float(np.max(np.abs(rec - exact))))
    steps.append(_step('数値で確かめる：x(t)=sinc²(t)（F=1）を |n|≦N の項で組み立て，−3≦t≦3 での最大誤差を見る',
                       'np.sinc(2*t - n) を使って和をとる（NumPy）',
                       r'N=10:\ %.1e,\quad N=100:\ %.1e,\quad N=1000:\ %.1e' % tuple(errs)))
    ok = errs[0] > errs[1] > errs[2] and errs[2] < 1e-4
    return {'steps': steps, 'reached': ok,
            'result': '式の上では 2F が消えて第6行に戻り，数値でも項を増やすほど元の信号に近づいた（この例では N を10倍にすると誤差はおよそ1/1000）．'
                      'これで骨格がひと回りして，最初の主張が示された．'}


DERIVATIONS = {
    'd_sinc_interp': d_sinc_interp,
    'd_flat_spectrum': d_flat_spectrum,
    'd_boundary': d_boundary,
    'd_alias': d_alias,
    'd_tri_spectrum': d_tri_spectrum,
    'd_orth': d_orth,
    'd_tri_coef': d_tri_coef,
    'd_coef_sample': d_coef_sample,
    'd_kernel': d_kernel,
    'd_assemble': d_assemble,
}


def run(name):
    """名前で導出を実行する．登録簿に無い名前は None（任意の式は受け付けない）"""
    fn = DERIVATIONS.get(name)
    if fn is None:
        return None
    t0 = time.time()
    try:
        res = dict(fn())
    except Exception as e:  # SymPy の内部で失敗しても画面は落とさない
        res = {'steps': [], 'reached': False, 'result': f'導く途中でエラーが起きました（{type(e).__name__}: {e}）．'}
    res['elapsed'] = round(time.time() - t0, 3)
    res['name'] = name
    return res
