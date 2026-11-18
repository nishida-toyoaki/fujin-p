# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Toyoaki Nishida
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# This file is part of FUJIN-P.
# Copyright (C) 2026 Toyoaki Nishida
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

"""safe_fetch — 外部の URL を安全に取りに行く（カーネル部品，2026-10-26）

利用者が指定した URL をサーバに取りに行かせると，サーバの内側（127.0.0.1・169.254.169.254・
社内アドレスなど）を読まれるおそれがある（SSRF）．名前解決の結果を先に確かめるだけでは，
2 回目の名前解決で内部アドレスを返される（DNS リバインディング）とすり抜ける．

この部品は，実際に接続したソケットの相手の IP を接続の瞬間に確かめ，内部アドレスなら切る．
転送（リダイレクト）は自前で 1 段ずつ追い，段ごとに同じ確かめをする．

PythonAnywhere の無料アカウントのように外向きの通信がプロキシ経由になる環境では，
接続相手はプロキシなので確かめは省き（プロキシ側の許可リストが外部サイトに限っている），
名前解決による事前の確かめだけを行う．

使い方
    from safe_fetch import fetch, FetchBlocked
    r = fetch(url, max_bytes=10 * 1024 * 1024, timeout=20, headers={...})
    r.status, r.url, r.content_type, r.content, r.too_large, r.encoding

    # requests のセッションとして使う（同じ確かめが効く）
    from safe_fetch import session
    s = session()
"""
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

MAX_REDIRECTS = 3


class FetchBlocked(Exception):
    """内部アドレスへの接続・http(s) 以外・転送の多すぎなど，取りに行かなかったとき"""


def is_public_ip(ip):
    try:
        a = ipaddress.ip_address(ip.split('%', 1)[0])
    except ValueError:
        return False
    if getattr(a, 'ipv4_mapped', None):
        a = a.ipv4_mapped
    return not (a.is_private or a.is_loopback or a.is_link_local or a.is_reserved
                or a.is_multicast or a.is_unspecified)


def host_is_public(host):
    """名前解決した全アドレスが公開アドレスか（事前の確かめ）"""
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return False
    return bool(infos) and all(is_public_ip(i[4][0]) for i in infos)


class _PeerCheck:
    def _new_conn(self):
        sock = super()._new_conn()
        try:
            peer = sock.getpeername()[0]
        except OSError:
            sock.close()
            raise FetchBlocked('接続先を確かめられませんでした')
        if not is_public_ip(peer):
            sock.close()
            raise FetchBlocked('内部ネットワークのアドレスには接続しません')
        return sock


class _HTTPConn(_PeerCheck, HTTPConnection):
    pass


class _HTTPSConn(_PeerCheck, HTTPSConnection):
    pass


class _HTTPPool(HTTPConnectionPool):
    ConnectionCls = _HTTPConn


class _HTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _HTTPSConn


class _SafeAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {'http': _HTTPPool, 'https': _HTTPSPool}


def session():
    """接続相手を確かめる requests.Session（転送は自動で追わせないこと）"""
    s = requests.Session()
    s.mount('http://', _SafeAdapter())
    s.mount('https://', _SafeAdapter())
    return s


class Result:
    def __init__(self, status, url, content_type, content, too_large, encoding):
        self.status = status
        self.url = url
        self.content_type = content_type
        self.content = content
        self.too_large = too_large
        self.encoding = encoding


def check_url(url):
    """取りに行ってよい URL か（http(s)・ホストあり・公開アドレス）．だめなら FetchBlocked"""
    p = urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.hostname:
        raise FetchBlocked('http か https の URL だけを取りに行けます')
    if not host_is_public(p.hostname):
        raise FetchBlocked('内部ネットワークのアドレスは取りに行けません')


def fetch(url, max_bytes=10 * 1024 * 1024, timeout=20, headers=None, max_redirects=MAX_REDIRECTS):
    """GET して Result を返す．200 以外もそのまま status に入れて返す（例外にしない）．
    内部アドレス・http(s) 以外・転送の多すぎは FetchBlocked．通信の失敗は requests.RequestException"""
    s = session()
    try:
        for _ in range(max_redirects + 1):
            check_url(url)
            r = s.get(url, timeout=timeout, stream=True, allow_redirects=False, headers=headers or {})
            if r.is_redirect:
                loc = r.headers.get('Location') or ''
                r.close()
                url = urljoin(url, loc)
                continue
            ctype = r.headers.get('Content-Type', '')
            chunks, total, too_large = [], 0, False
            for ch in r.iter_content(65536):
                total += len(ch)
                if total > max_bytes:
                    too_large = True
                    break
                chunks.append(ch)
            enc = requests.utils.get_encoding_from_headers(r.headers) if 'charset' in ctype.lower() else None
            status = r.status_code
            r.close()
            return Result(status, url, ctype, b''.join(chunks), too_large, enc)
        raise FetchBlocked('転送が多すぎます')
    finally:
        s.close()
