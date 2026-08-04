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

"""FUJIN-P ライセンスヘッダ一括挿入スクリプト（拡張子別対応版）

使い方（対象ツリーの直下で実行する。既定は dry-run で、書き込みには --yes が必要）:
    python add_license_headers.py            # dry-run: 何が対象かを表示するだけ
    python add_license_headers.py --yes      # 実際にヘッダを挿入
    python add_license_headers.py --remove --yes   # 挿入したヘッダを除去（原状復帰）
    python add_license_headers.py --root PATH      # 対象ツリーを指定（既定: カレント）

対象: .py（フル告知・#行コメント）, .html（{# #} 短縮告知）, .js / .css（/* */ 短縮告知）
除外: config.py, config_template.py, GPL依存6スクリプト, *.min.js, *.min.css,
      .git / dist / static / __pycache__ 配下,
      マーカー既存ファイル（冪等）, 他者の著作権表示を含むファイル（要目視）
"""

import argparse
import sys
from pathlib import Path

MARKER = "This file is part of FUJIN-P."
YEARS = "2024-2026"
HOLDER = "Toyoaki Nishida"
SOURCE = "https://github.com/nishida-toyoaki/fujin-p"

FULL_NOTICE_PY = f"""\
# SPDX-FileCopyrightText: {YEARS} {HOLDER}
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# {MARKER}
# Copyright (C) {YEARS} {HOLDER}
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
# Source: {SOURCE}
"""

SHORT_NOTICE_HTML = f"""\
{{#
  {MARKER}
  SPDX-FileCopyrightText: {YEARS} {HOLDER}
  SPDX-License-Identifier: AGPL-3.0-or-later
  Source: {SOURCE}
#}}
"""

SHORT_NOTICE_CSTYLE = f"""\
/*
 * {MARKER}
 * SPDX-FileCopyrightText: {YEARS} {HOLDER}
 * SPDX-License-Identifier: AGPL-3.0-or-later
 * Source: {SOURCE}
 */
"""

HEADERS = {
    ".py": FULL_NOTICE_PY,
    ".html": SHORT_NOTICE_HTML,
    ".js": SHORT_NOTICE_CSTYLE,
    ".css": SHORT_NOTICE_CSTYLE,
}

EXCLUDE_NAMES = {
    "config.py",
    "config_template.py",
    # GPL依存（mysqlclient密結合）の6スクリプト（配布ツリーには無いはずだが念のため）
    "savemysqltables.py", "savemysqltables_fujinp.py",
    "get_schema.py", "get_schema_fujinp.py",
    "drop_all_tables.py", "drop_all_tables_fujinp.py",
}
EXCLUDE_DIRS = {".git", "dist", "static", "__pycache__", "node_modules"}
HEAD_SCAN = 2000  # 冒頭この文字数だけを既存表示の検査に使う


def is_excluded(path: Path, root: Path) -> bool:
    if path.name in EXCLUDE_NAMES:
        return True
    if path.name.endswith(".min.js") or path.name.endswith(".min.css"):
        return True
    rel = path.relative_to(root)
    return any(part in EXCLUDE_DIRS for part in rel.parts[:-1])


def detect_newline(text: str) -> str:
    return "\r\n" if "\r\n" in text.split("\n", 2)[0] + "\n" or "\r\n" in text[:200] else "\n"


def has_other_copyright(head: str) -> bool:
    low = head.lower()
    if ("copyright" in low or "(c)" in low or "©" in head) and HOLDER not in head:
        return True
    return False


def build_header(ext: str, newline: str) -> str:
    header = HEADERS[ext]
    if newline != "\n":
        header = header.replace("\n", newline)
    return header + newline  # ヘッダの後に空行を1つ


def insert_pos_py(text: str) -> int:
    """shebang／encoding行があればその直後、なければ先頭。返り値は文字位置。"""
    pos = 0
    for _ in range(2):
        nl = text.find("\n", pos)
        if nl < 0:
            break
        line = text[pos:nl + 1]
        s = line.strip()
        if s.startswith("#!") or ("coding" in s and s.startswith("#")):
            pos = nl + 1
        else:
            break
    return pos


def process(path: Path, root: Path, remove: bool):
    """返り値: ('insert'|'remove'|'skip_marker'|'skip_other'|'skip_none', 変更後テキスト|None)"""
    with open(path, encoding="utf-8", newline="") as f:  # 改行コードを無変換で保持
        raw = f.read()
    head = raw[:HEAD_SCAN]
    ext = path.suffix.lower()
    newline = "\r\n" if "\r\n" in raw[:400] else "\n"
    header = build_header(ext, newline)

    if remove:
        if MARKER not in head:
            return ("skip_none", None)
        if header in raw:
            return ("remove", raw.replace(header, "", 1))
        # 改行コード違いの保険
        alt = build_header(ext, "\n" if newline == "\r\n" else "\r\n")
        if alt in raw:
            return ("remove", raw.replace(alt, "", 1))
        return ("skip_other", None)  # マーカーはあるが定型と不一致：手で確認

    if MARKER in head:
        return ("skip_marker", None)
    if has_other_copyright(head):
        return ("skip_other", None)

    pos = insert_pos_py(raw) if ext == ".py" else 0
    return ("insert", raw[:pos] + header + raw[pos:])


def main():
    ap = argparse.ArgumentParser(description="FUJIN-P AGPLヘッダ一括挿入（既定はdry-run）")
    ap.add_argument("--root", default=".", help="対象ツリーのルート（既定: カレントディレクトリ）")
    ap.add_argument("--yes", action="store_true", help="実際にファイルへ書き込む")
    ap.add_argument("--remove", action="store_true", help="挿入済みヘッダを除去する")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"エラー: ルートが見つかりません: {root}")
        sys.exit(1)

    counts = {"insert": 0, "remove": 0, "skip_marker": 0, "skip_other": 0, "skip_none": 0}
    by_ext = {}
    others = []

    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in HEADERS:
            continue
        if is_excluded(path, root):
            continue
        try:
            action, new_text = process(path, root, args.remove)
        except UnicodeDecodeError:
            print(f"  [encoding?] {path.relative_to(root)}  ← UTF-8で読めずスキップ")
            continue
        counts[action] += 1
        if action in ("insert", "remove"):
            by_ext[path.suffix.lower()] = by_ext.get(path.suffix.lower(), 0) + 1
            if args.yes:
                with open(path, "w", encoding="utf-8", newline="") as f:
                    f.write(new_text)
            verb = "挿入" if action == "insert" else "除去"
            mode = "" if args.yes else "（dry-run）"
            print(f"  [{verb}{mode}] {path.relative_to(root)}")
        elif action == "skip_other":
            others.append(path.relative_to(root))

    print()
    mode = "実行" if args.yes else "dry-run（--yes を付けると書き込みます）"
    op = "除去" if args.remove else "挿入"
    print(f"=== {op} {mode} ===")
    print(f"対象: {counts['remove' if args.remove else 'insert']} 件 "
          f"（内訳: {', '.join(f'{k} {v}' for k, v in sorted(by_ext.items())) or 'なし'}）")
    print(f"マーカー既存のためスキップ: {counts['skip_marker']} 件")
    if others:
        print(f"他者の著作権表示等のためスキップ（要目視確認）: {len(others)} 件")
        for p in others:
            print(f"    - {p}")


if __name__ == "__main__":
    main()
