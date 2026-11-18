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

# 2026-10-25 セキュリティ改修（C-1）
#   Markdown 本文中の ```sql および ```sql_md_texts ブロックを DB で実行する機能を撤去した．
#   旧版は「開いた人が admin なら実行」「sql_md_texts は区分を問わず実行」だったため，
#   他人が書いた本文を admin が開く，あるいは公開ページを誰かが開くだけで任意の SQL が走った．
#   以後，```sql は他の言語名と同じ単なるコードブロックとして表示する．
#   引数 user_category は呼び出し側との互換のために残すが，描画結果には影響しない．
#   DB 接続（mysql.connector / DatabaseConfig）はこのモジュールから無くなった．
#
# 2026-10-26 セキュリティ改修（R-1）
#   変換結果の HTML を許可リスト方式で無害化する（sanitize_html）．本文中の生の HTML
#   （<script>，<img onerror=…>，javascript: リンク，SVG 内のスクリプト等）は取り除かれる．
#   mermaid ブロックの中身は文字として埋め込み，mermaid の securityLevel を strict にした．
#
# 2026-10-26 セキュリティ改修（R-2）
#   アップロードされた SVG ファイルの無害化 sanitize_svg_bytes を加えた（コレポ・マイノート・えふえふが使う）．
#
# 2026-11-09 無害化の不具合修正
#   <meta charset="utf-8"> のように閉じタグを持たない meta・link・base を「中身ごと捨てる」扱いにしていたため，
#   捨てる状態が解けず，それ以降の本文がすべて消えていた（HTML 文書を丸ごと貼ったノートやコレポの統合レポートが白紙になる）．
#   これらは単独のタグとして捨てるだけにした．あわせて <style> は，url( ・@import・expression 等の危険な記述と
#   「<」を含まないものに限って残すことにした（文書を丸ごと貼ったときの見た目を保つため）．

from flask import Blueprint, request, render_template, jsonify, session, redirect, url_for, flash
from auth import login_required
import markdown
import re
import os
import logging


# ────────────────────────────────────────────────────────────
# HTML サニタイザ（2026-10-26 セキュリティ改修 R-1）
#   Markdown から作った HTML を，許可リストにあるタグ・属性だけに絞る．
#   本文に書かれた <script>，<img onerror=…>，javascript: リンク，SVG の中のスクリプト等を
#   取り除き，他人の書いた本文を開いた人のブラウザでスクリプトが動かないようにする．
#   外部ライブラリ（bleach 等）に頼らず Python 標準の html.parser で組んである
#   （カーネルの部品が import に失敗すると全アプリが止まるため）．
#   他のアプリからも sanitize_html(html_text) として使える．
# ────────────────────────────────────────────────────────────
from html.parser import HTMLParser as _HTMLParser
import html as _html

# 中身ごと捨てるタグ
_SAN_DROP_CONTENT = {
    'script', 'style', 'iframe', 'frame', 'frameset', 'object', 'embed', 'applet',
    'noscript', 'noembed', 'noframes', 'template', 'textarea', 'xmp', 'plaintext',
    'foreignobject', 'form', 'select', 'button', 'audio', 'video', 'math', 'base',
    'link', 'meta', 'head',
}
# 閉じタグを持たないので，捨てても「中身ごと」にはならない要素（2026-11-09）
_SAN_DROP_VOID = {'meta', 'link', 'base'}
# 中身のない（閉じタグを持たない）HTML 要素
_SAN_VOID = {'br', 'hr', 'img', 'col', 'wbr'}

_SAN_HTML_TAGS = {
    'p', 'br', 'hr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'strong', 'b', 'em', 'i', 'u',
    's', 'strike', 'del', 'ins', 'sub', 'sup', 'small', 'mark', 'code', 'pre', 'kbd',
    'samp', 'var', 'q', 'cite', 'blockquote', 'ul', 'ol', 'li', 'dl', 'dt', 'dd',
    'table', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td', 'caption', 'colgroup', 'col',
    'a', 'img', 'span', 'div', 'abbr', 'details', 'summary', 'figure', 'figcaption',
    'center', 'wbr', 'ruby', 'rt', 'rp',
}
_SAN_SVG_TAGS = {
    'svg', 'g', 'path', 'rect', 'circle', 'ellipse', 'line', 'polyline', 'polygon',
    'text', 'tspan', 'textpath', 'defs', 'lineargradient', 'radialgradient', 'stop',
    'marker', 'title', 'desc', 'use', 'clippath', 'mask', 'pattern', 'symbol', 'a',
    'image',
}

_SAN_GLOBAL_ATTRS = {'class', 'id', 'title', 'style', 'lang', 'dir'}
_SAN_HTML_ATTRS = {
    'a': {'href', 'name', 'target', 'rel'},
    'img': {'src', 'alt', 'width', 'height', 'loading'},
    'td': {'colspan', 'rowspan', 'align', 'valign', 'width'},
    'th': {'colspan', 'rowspan', 'align', 'valign', 'width', 'scope'},
    'col': {'span', 'width'}, 'colgroup': {'span', 'width'},
    'ol': {'start', 'type', 'reversed'}, 'li': {'value'},
    'table': {'border', 'width', 'align', 'cellpadding', 'cellspacing'},
    'details': {'open'},
    'p': {'align'}, 'div': {'align'}, 'h1': {'align'}, 'h2': {'align'},
    'h3': {'align'}, 'h4': {'align'}, 'h5': {'align'}, 'h6': {'align'},
}
_SAN_SVG_ATTRS = {
    'viewbox', 'width', 'height', 'x', 'y', 'x1', 'y1', 'x2', 'y2', 'cx', 'cy', 'r',
    'rx', 'ry', 'd', 'points', 'fill', 'fill-rule', 'clip-rule', 'stroke',
    'stroke-width', 'stroke-linecap', 'stroke-linejoin', 'stroke-dasharray',
    'stroke-dashoffset', 'stroke-miterlimit', 'opacity', 'fill-opacity',
    'stroke-opacity', 'transform', 'font-size', 'font-family', 'font-weight',
    'font-style', 'text-anchor', 'dominant-baseline', 'alignment-baseline',
    'xmlns', 'xmlns:xlink', 'version', 'preserveaspectratio', 'offset', 'stop-color',
    'stop-opacity', 'gradientunits', 'gradienttransform', 'spreadmethod', 'fx', 'fy',
    'marker-end', 'marker-start', 'marker-mid', 'markerwidth', 'markerheight',
    'refx', 'refy', 'orient', 'markerunits', 'clip-path', 'mask', 'clippathunits',
    'maskunits', 'patternunits', 'patterntransform', 'dx', 'dy', 'rotate',
    'letter-spacing', 'word-spacing', 'text-decoration', 'visibility', 'display',
    'href', 'xlink:href', 'textlength', 'lengthadjust', 'startoffset', 'role',
    'aria-label', 'vector-effect', 'shape-rendering', 'paint-order',
}
_SAN_URL_ATTRS = {'href', 'src', 'xlink:href'}
# data-* は既定で残す（マイノートの data-att 等が使う）．Bootstrap 等が解釈する名前は落とす
_SAN_DATA_RE = re.compile(r'^data-[a-z0-9_-]{1,40}$')
_SAN_DATA_DENY = ('data-bs-', 'data-toggle', 'data-target', 'data-content',
                  'data-template', 'data-html', 'data-url', 'data-src', 'data-href')
_SAN_SCHEME_RE = re.compile(r'^[a-z][a-z0-9+.\-]*:')
_SAN_IMG_DATA_RE = re.compile(r'^data:image/(png|jpe?g|gif|webp);base64,')
_SAN_BAD_CSS_RE = re.compile(r'expression|javascript|vbscript|url\s*\(|@import|behavior|-moz-binding', re.I)


def _san_url_ok(value, tag, attr, in_svg):
    s = re.sub(r'[\x00-\x20\x7f]+', '', value or '').lower()
    if in_svg and attr in ('href', 'xlink:href') and tag in ('use', 'textpath'):
        return s.startswith('#')                 # SVG 内参照は文書内の id だけ
    if in_svg and tag == 'image':
        return bool(_SAN_IMG_DATA_RE.match(s)) or s.startswith(('http://', 'https://', '/'))
    if tag == 'img' and _SAN_IMG_DATA_RE.match(s):
        return True
    if not _SAN_SCHEME_RE.match(s):
        return True                              # 相対URL・#・/ で始まるもの
    return s.startswith(('http:', 'https:', 'mailto:'))


class _Sanitizer(_HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.stack = []          # 出力した開きタグ
        self.skip = 0            # 中身ごと捨てている深さ
        self.svg = 0             # svg の中にいる深さ
        self.css = None          # 残す候補の <style> の中身（2026-11-09）

    def _attrs(self, tag, attrs, in_svg):
        allowed = set(_SAN_GLOBAL_ATTRS)
        allowed |= (_SAN_SVG_ATTRS if in_svg else _SAN_HTML_ATTRS.get(tag, set()))
        if in_svg and tag == 'a':
            allowed |= {'href', 'xlink:href', 'target'}
        kept, blank = [], False
        for name, value in attrs:
            name = (name or '').lower()
            value = '' if value is None else value
            if name.startswith('on'):
                continue
            if name not in allowed:
                if not (_SAN_DATA_RE.match(name) and not name.startswith(_SAN_DATA_DENY)):
                    continue
            if name in _SAN_URL_ATTRS and not _san_url_ok(value, tag, name, in_svg):
                continue
            if name == 'style' and _SAN_BAD_CSS_RE.search(value):
                continue
            if name == 'target':
                if value != '_blank':
                    continue
                blank = True
            if name == 'rel':
                continue                          # 下で付け直す
            kept.append((name, value))
        if blank:
            kept.append(('rel', 'noopener noreferrer'))
        return ''.join(' %s="%s"' % (n, _html.escape(v, quote=True)) for n, v in kept)

    def _open(self, tag, attrs, selfclose):
        if tag in _SAN_DROP_VOID:
            selfclose = True                      # 閉じタグが来ない要素は単独で捨てる
        if self.skip:
            if tag in _SAN_DROP_CONTENT and not selfclose:
                self.skip += 1
            return
        if tag == 'style' and not selfclose and not self.svg:
            self.css = []                         # 中身を見てから残すか決める
            self.skip = 1
            return
        if tag in _SAN_DROP_CONTENT or (tag == 'title' and not self.svg):
            if not selfclose:
                self.skip = 1
            return
        in_svg = self.svg > 0 or tag == 'svg'
        if not (tag in _SAN_SVG_TAGS if in_svg else tag in _SAN_HTML_TAGS):
            return                                # タグだけ捨てて中身の文字は残す
        a = self._attrs(tag, attrs, in_svg)
        if selfclose or (tag in _SAN_VOID and not in_svg):
            self.out.append('<%s%s%s>' % (tag, a, ' /' if in_svg else ''))
            return
        self.out.append('<%s%s>' % (tag, a))
        self.stack.append(tag)
        if tag == 'svg':
            self.svg += 1

    def handle_starttag(self, tag, attrs):
        self._open(tag, attrs, False)

    def handle_startendtag(self, tag, attrs):
        self._open(tag, attrs, True)

    def handle_endtag(self, tag):
        if self.skip:
            if tag in _SAN_DROP_CONTENT or (tag == 'title' and not self.svg):
                self.skip -= 1
                if self.skip == 0 and tag == 'style' and self.css is not None:
                    css = ''.join(self.css)
                    self.css = None
                    if '<' not in css and not _SAN_BAD_CSS_RE.search(css):
                        self.out.append('<style>%s</style>' % css)
            return
        if tag not in self.stack:
            return
        while self.stack:
            t = self.stack.pop()
            self.out.append('</%s>' % t)
            if t == 'svg':
                self.svg -= 1
            if t == tag:
                break

    def handle_data(self, data):
        if self.css is not None and self.skip == 1:
            self.css.append(data)
            return
        if not self.skip:
            self.out.append(_html.escape(data, quote=False))

    def handle_comment(self, data):
        pass

    def handle_decl(self, decl):
        pass

    def handle_pi(self, data):
        pass

    def unknown_decl(self, data):
        pass

    def result(self):
        self.close()
        while self.stack:
            t = self.stack.pop()
            self.out.append('</%s>' % t)
        return ''.join(self.out)


def sanitize_html(html_text):
    """許可リスト方式で HTML を無害化して返す。"""
    if not html_text:
        return html_text or ''
    p = _Sanitizer()
    p.feed(html_text)
    return p.result()



# ────────────────────────────────────────────────────────────
# SVG ファイルのサニタイザ（2026-10-26 セキュリティ改修 R-2）
#   アップロードされた SVG ファイルを XML として読み，許可リストにある要素・属性だけを残す．
#   <script>，<foreignObject>，アニメーション要素（set・animate），on* 属性，
#   javascript: や外部への参照は取り除く．SVG ファイルは XML として表示されるので，
#   大文字小文字（viewBox 等）を保つため html.parser ではなく xml.etree を使う．
#   DOCTYPE・ENTITY を含むもの，XML として読めないもの，svg で始まらないものは，
#   中身のない SVG（読み込めなかった旨の表示）に置き換える．
# ────────────────────────────────────────────────────────────
import xml.etree.ElementTree as _ET

_SVG_NS = 'http://www.w3.org/2000/svg'
_XLINK_NS = 'http://www.w3.org/1999/xlink'
_XML_NS = 'http://www.w3.org/XML/1998/namespace'
_ET.register_namespace('', _SVG_NS)
_ET.register_namespace('xlink', _XLINK_NS)

_SVGF_ELEMENTS = {
    'svg', 'g', 'path', 'rect', 'circle', 'ellipse', 'line', 'polyline', 'polygon',
    'text', 'tspan', 'textPath', 'defs', 'linearGradient', 'radialGradient', 'stop',
    'marker', 'title', 'desc', 'use', 'clipPath', 'mask', 'pattern', 'symbol', 'image',
    'a', 'switch', 'filter', 'feGaussianBlur', 'feOffset', 'feBlend', 'feColorMatrix',
    'feMerge', 'feMergeNode', 'feFlood', 'feComposite', 'feDropShadow', 'feMorphology',
}
_SVGF_PLACEHOLDER = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="40" viewBox="0 0 320 40">'
    '<rect width="320" height="40" fill="#fdecea"/>'
    '<text x="10" y="25" font-size="14" fill="#7b1d13">SVG を安全に読み込めませんでした</text>'
    '</svg>').encode('utf-8')


def _svgf_href_ok(local_tag, value):
    s = re.sub(r'[\x00-\x20\x7f]+', '', value or '').lower()
    if s.startswith('#'):
        return True                               # 文書内の参照
    if local_tag == 'image':
        return bool(_SAN_IMG_DATA_RE.match(s))     # 埋め込みのラスタ画像だけ
    if local_tag == 'a':
        return s.startswith(('http://', 'https://'))
    return False


def _svgf_clean(el):
    local = el.tag.split('}')[-1] if isinstance(el.tag, str) else ''
    for name in list(el.attrib.keys()):
        value = el.attrib[name]
        ns, _, lname = name[1:].partition('}') if name.startswith('{') else ('', '', name)
        lname_l = lname.lower()
        keep = True
        if ns not in ('', _XLINK_NS, _XML_NS):
            keep = False
        elif lname_l.startswith('on'):
            keep = False
        elif lname_l == 'href':
            keep = _svgf_href_ok(local, value)
        elif ns == _XLINK_NS:
            keep = False
        elif lname_l == 'style' and _SAN_BAD_CSS_RE.search(value):
            keep = False
        elif re.search(r'javascript\s*:|vbscript\s*:|data\s*:\s*text', value, re.I):
            keep = False
        elif lname_l in ('attributename', 'begin', 'values', 'to', 'from', 'by'):
            keep = False
        if not keep:
            del el.attrib[name]
    for child in list(el):
        if not isinstance(child.tag, str):        # コメント・処理命令
            el.remove(child)
            continue
        cns, _, clocal = child.tag[1:].partition('}') if child.tag.startswith('{') else ('', '', child.tag)
        if cns not in (_SVG_NS, '') or clocal not in _SVGF_ELEMENTS:
            # 許可外の要素は中身ごと除く（直後の文字は残す）
            tail = child.tail
            idx = list(el).index(child)
            el.remove(child)
            if tail:
                if idx == 0:
                    el.text = (el.text or '') + tail
                else:
                    prev = list(el)[idx - 1]
                    prev.tail = (prev.tail or '') + tail
            continue
        _svgf_clean(child)


def sanitize_svg_bytes(data):
    """アップロードされた SVG（bytes）を無害化して bytes で返す。"""
    try:
        if isinstance(data, str):
            data = data.encode('utf-8')
        head = data[:4096].decode('utf-8', errors='replace')
        if re.search(r'<!DOCTYPE|<!ENTITY', data.decode('utf-8', errors='replace'), re.I):
            return _SVGF_PLACEHOLDER
        root = _ET.fromstring(data)
        if root.tag not in ('{%s}svg' % _SVG_NS, 'svg'):
            return _SVGF_PLACEHOLDER
        if root.tag == 'svg':                     # 名前空間なしの SVG は付け直す
            for el in root.iter():
                if isinstance(el.tag, str) and not el.tag.startswith('{'):
                    el.tag = '{%s}%s' % (_SVG_NS, el.tag)
        _svgf_clean(root)
        out = _ET.tostring(root, encoding='unicode')
        return ('<?xml version="1.0" encoding="UTF-8"?>\n' + out).encode('utf-8')
    except Exception as e:
        logging.warning("sanitize_svg_bytes: %s", e)
        return _SVGF_PLACEHOLDER

markdown_converter_bp = Blueprint('markdown_converter', __name__)


def escape_dollar_signs(text):
    # 既にエスケープされているドル記号はそのままにする
    text = re.sub(r'(?<!\\)\$\$', r'\\$\\$', text)
    # 単一のドル記号をエスケープする（ただし、既にエスケープされているものは除く）
    text = re.sub(r'(?<!\\)\$', r'\\$', text)
    return text

#  内部リンク拡張は使用しない
#
#def process_internal_links(content):
#    def replacement(match):
#        text = match.group(1)
#        note_id = match.group(2)
#        return f'<a href="{url_for("trial_user.view_note", note_id=note_id)}">{text}</a>'
#
#    # 新しい形式 [テキスト]{ID} に対応する正規表現
#    pattern = r'\[([^\]]+)\]\{(\d+)\}'
#    return re.sub(pattern, replacement, content)

def process_mermaid(md_content):
    """
    Mermaidブロックを処理してHTMLに変換する
    """
    import re

    def create_mermaid_html(mermaid_content, diagram_id):
        return f"""
        <div class="mermaid-diagram" id="mermaid-{diagram_id}">
            <div class="mermaid">
                {mermaid_content}
            </div>
        </div>
        """

    # Mermaidブロックを検出して処理
    mermaid_pattern = re.compile(r'```mermaid\n(.*?)\n```', re.DOTALL)
    diagram_counter = 0

    def replace_mermaid(match):
        nonlocal diagram_counter
        diagram_counter += 1
        mermaid_content = match.group(1).strip()
        return create_mermaid_html(mermaid_content, diagram_counter)

    # Mermaidブロックを処理
    processed_content = mermaid_pattern.sub(replace_mermaid, md_content)

    # Mermaidスクリプトを追加（ダイアグラムが存在する場合のみ）
    if diagram_counter > 0:
        mermaid_script = """
        <script src="https://cdnjs.cloudflare.com/ajax/libs/mermaid/10.8.0/mermaid.min.js"></script>
        <script>
            mermaid.initialize({
                startOnLoad: true,
                theme: 'default',
                securityLevel: 'strict',
                htmlLabels: true,
                flowchart: { useMaxWidth: true, htmlLabels: true },
                clickable: true
            });
        </script>
        """
        # スクリプトタグを</body>の直前に挿入
        processed_content = re.sub(
            r'</body>',
            f'{mermaid_script}</body>',
            processed_content
        )

    return processed_content


def process_markdown_for_preview(md_content, user_category='guest'):
    """
    プレビュー用に、HTMLの本体部分のみを生成する
    user_category は互換のために受け取るだけで，描画には使わない（SQL 実行機能は撤去済み）．
    """
    # 既存のprocess_markdownのロジックを再利用し、最後のHTMLテンプレート部分だけを省略する
    try:
        # 1. 基本的な前処理
        md_content = (md_content or '').replace('\r', '')
        md_content = escape_dollar_signs(md_content)
        # md_content = process_internal_links(md_content)  #  内部リンク拡張は使用しない

        # 2. Mermaid, SVGブロックをプレースホルダーに置き換え
        mermaid_blocks = {}
        mermaid_counter = 0
        mermaid_pattern = re.compile(r'```mermaid\s*\n(.*?)\n\s*```', re.DOTALL)
        def mermaid_replace(match):
            nonlocal mermaid_counter
            mermaid_counter += 1
            content = match.group(1).strip()
            placeholder = f"MERMAID_PLACEHOLDER_{mermaid_counter}"
            mermaid_blocks[placeholder] = f'<div class="mermaid">{_html.escape(content, quote=False)}</div>'
            return placeholder
        md_content = mermaid_pattern.sub(mermaid_replace, md_content)

        svg_blocks = {}
        svg_counter = 0
        svg_pattern = re.compile(r'```svg\s*\n(.*?)\n\s*```', re.DOTALL)
        def svg_replace(match):
            nonlocal svg_counter
            svg_counter += 1
            content = match.group(1).strip()
            placeholder = f"SVG_PLACEHOLDER_{svg_counter}"
            svg_blocks[placeholder] = f'<div class="svg-container">{content}</div>'
            return placeholder
        md_content = svg_pattern.sub(svg_replace, md_content)

        # 3. 標準Markdownの変換
        html_content = markdown.markdown(md_content, extensions=[
            'extra', 'nl2br', 'sane_lists', 'fenced_code' # , 'pymdownx.tasklist'
        ])

        # 4. プレースホルダーをHTMLに置き換え
        for placeholder, block_html in mermaid_blocks.items():
            html_content = html_content.replace(f'<p>{placeholder}</p>', block_html)
        for placeholder, block_html in svg_blocks.items():
            html_content = html_content.replace(f'<p>{placeholder}</p>', block_html)

        # 5. 無害化（許可リスト）
        return sanitize_html(html_content)

    except Exception as e:
        return f"<p class='text-danger'>Markdown処理中にエラーが発生しました: {str(e)}</p>"

def process_markdown(md_content, user_category='guest'):
    """
    Markdownコンテンツを処理してHTMLに変換する
    user_category は互換のために受け取るだけで，描画には使わない（SQL 実行機能は撤去済み）．
    """
    # 基本的な前処理
    md_content = (md_content or '').replace('\r', '')
    # md_content = escape_dollar_signs(md_content) # $記号はエスケープせずそのままにする
    # md_content = process_internal_links(md_content)  #  内部リンク拡張は使用しない

    # 基本的なCSSスタイルを定義（既存のスタイルを維持）
    css_style = """
    <style>
        .sql-result-table {
            border-collapse: collapse;
            margin: 0 auto;
            table-layout: fixed;
            width: 100%;
            font-size: 12px;
        }
        .sql-result-table th, .sql-result-table td {
            border: 1px solid #ddd;
            padding: 4px;
            text-align: left;
            vertical-align: top;
            word-wrap: break-word;
            overflow-wrap: break-word;
            hyphens: auto;
        }
        .sql-result-table th {
            background-color: #f2f2f2;
            font-size: 13px;
        }
        .table-container {
            width: 100%;
            overflow-x: auto;
        }
        ul, ol {
            padding-left: 20px;
            margin: 10px 0;
        }
        li {
            margin-bottom: 5px;
        }
        ol {
            list-style-type: decimal;
        }
        .task-list-item {
            list-style-type: none;
        }
        .task-list-item-checkbox {
            margin-right: 0.5em;
        }
        /* Mermaid図用のスタイル */
        .mermaid-diagram {
            margin: 20px 0;
            padding: 10px;
            background: #f8f9fa;
            border-radius: 4px;
            overflow-x: auto;
        }
        .mermaid {
            display: flex;
            justify-content: center;
        }
        .mermaid svg {
            max-width: 100%;
        }
        /* クリッカブル要素のスタイル */
        .mermaid [id*="node"] a {
            cursor: pointer !important;
        }
        .mermaid [id*="node"]:hover {
            opacity: 0.7;
            transition: opacity 0.2s;
        }
        /* クリッカブルノードのスタイル */
        .mermaid g[class*="clickable"] {
            cursor: pointer !important;
            transition: opacity 0.2s;
        }
        .mermaid g[class*="clickable"]:hover {
            opacity: 0.7;
        }
        /* SVG表示用のスタイル追加 */
        .svg-container {
            display: flex;
            justify-content: center;
            margin: 20px 0;
            max-width: 100%;
            overflow-x: auto;
        }
        .svg-container svg {
            max-width: 100%;
            height: auto;
        }
    </style>
    """

    try:
        # 1. Mermaid図の処理
        mermaid_blocks = {}  # Mermaidブロックを一時的に保存
        diagram_counter = 0

        # Mermaidブロックを一時的なプレースホルダーに置き換え
        mermaid_pattern = re.compile(
            r'```mermaid\s*\n'  # 開始行
            r'(.*?)'            # 図の定義内容（最小一致）
            r'\n\s*```',        # 終了行
            re.DOTALL
        )

        def mermaid_replace(match):
            nonlocal diagram_counter
            diagram_counter += 1
            content = match.group(1).strip()
            placeholder = f"MERMAID_PLACEHOLDER_{diagram_counter}"
            mermaid_blocks[placeholder] = f"""
            <div class="mermaid" id="mermaid-{diagram_counter}">
        {_html.escape(content, quote=False)}
            </div>
            """

            return placeholder

        md_content = mermaid_pattern.sub(mermaid_replace, md_content)

        katex_script = """
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.9/katex.min.css">
        <script defer src="https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.9/katex.min.js"></script>
        <script defer src="https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.9/contrib/auto-render.min.js"
            onload="renderMathInElement(document.body, {
                delimiters: [
                    {left: '$$', right: '$$', display: true},
                    {left: '$', right: '$', display: false}
                ]
            });">
        </script>
        <style>
            .katex { font-size: 1em !important; }
            .katex-display { overflow: auto hidden; }
        </style>
        """

        # 2. SVGブロックの処理
        svg_blocks = {}
        svg_counter = 0
        svg_pattern = re.compile(r'```svg\s*\n(.*?)\n\s*```', re.DOTALL)

        def svg_replace(match):
            nonlocal svg_counter
            svg_counter += 1
            content = match.group(1).strip()
            placeholder = f"SVG_PLACEHOLDER_{svg_counter}"
            svg_blocks[placeholder] = f"""
            <div class="svg-container">
                {content}
            </div>
            """
            return placeholder

        md_content = svg_pattern.sub(svg_replace, md_content)

        # 3. Markdownの変換（fenced_code拡張を含める）
        html_content = markdown.markdown(md_content, extensions=[
            'extra', 'nl2br', 'sane_lists',
            ##'pymdownx.tasklist',
            'fenced_code'  # 明示的にfenced_code拡張を追加
        ])

        # 4. プレースホルダーをMermaid HTMLに置き換え
        for placeholder, mermaid_html in mermaid_blocks.items():
            html_content = html_content.replace(placeholder, mermaid_html)

        # SVGの置き換え
        for placeholder, svg_html in svg_blocks.items():
            html_content = html_content.replace(placeholder, svg_html)

        # 5. 数式の処理（既存の処理）
        html_content = re.sub(r'\\∖\((.*?)\\∖\)', r'$\1$', html_content)
        html_content = re.sub(r'\\∖\[(.*?)\\∖\]', r'$$\1$$', html_content)

        # 6. 無害化（許可リスト）
        html_content = sanitize_html(html_content)

        mermaid_script = """
        <script src="https://cdn.jsdelivr.net/npm/mermaid@10.8.0/dist/mermaid.min.js"></script>
        <script>
            document.addEventListener('DOMContentLoaded', function() {
                mermaid.initialize({
                    startOnLoad: true,
                    theme: 'default',
                    securityLevel: 'strict',
                    htmlLabels: true,
                    flowchart: { useMaxWidth: true },
                    mindmap: { padding: 10 }
                });

                // クリックイベントの設定
                mermaid.parseError = function(err, hash) {
                    console.error('Mermaid error:', err);
                };
            });
        </script>
        """

        # 6. 最終的なHTML生成
        full_html_content = f"""
        <!DOCTYPE html>
        <html lang="ja">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Markdown Converted Content</title>
            {css_style}
            {katex_script}
            {mermaid_script if diagram_counter > 0 else ''}
        </head>
        <body>
            <div class="content">
                {html_content}
            </div>
        </body>
        </html>
        """
        return full_html_content

    except Exception as e:
        return f"<p>An error occurred: {str(e)}</p>"


@markdown_converter_bp.route('/')
@login_required
def index():
    return_to = request.args.get('return_to')
    if not return_to:
        return_to = url_for('auth.dashboard')
    return render_template('markdown_converter.html', return_to = return_to)

@markdown_converter_bp.route('/convert', methods=['POST'])
@login_required
def convert_markdown():
    md_content = request.form.get('markdown_content')

    if md_content:
        # ブラウザからの入力を処理
        output_html = process_markdown(md_content)
        return jsonify({
            'success': True,
            'html_content': output_html
        })
    else:
        # 2026-10-25: 旧版はここで固定パス（/home/nishida/markdowns/）のファイルを変換して
        # サーバに書き出していた．ログイン済みの誰でもサーバのファイルを書かせられる経路なので撤去．
        return jsonify({
            'success': False,
            'error': 'markdown_content が空です'
        }), 400


def process_markdown_file(md_file_path, output_file):
    try:
        # Markdownファイルの読み込み
        with open(md_file_path, 'r', encoding='utf-8') as md_file:
            md_content = md_file.read()

        # Markdownの処理
        html_content = process_markdown(md_content)

        # HTMLファイルを書き出し
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(html_content)

        return html_content

    except Exception as e:
        return f"<p>An error occurred: {str(e)}</p>"
