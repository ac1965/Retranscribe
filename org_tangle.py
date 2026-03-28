#!/usr/bin/env python3
"""org_tangle.py — Org-babel tangle を Python で再実装する.

org-babel-tangle と同等のロジックで README.org から全ソースファイルを生成する。

対応する Org-babel 機能:
- #+BEGIN_SRC lang :tangle <path>  — ブロックレベルの tangle 指定
- :PROPERTIES: / :header-args:lang: :tangle <path>  — セクションレベルのデフォルト
- :tangle no  — スキップ
- #+PROPERTY: header-args [:mkdirp yes  — ファイルレベルのデフォルト
- 同一ファイルへの複数ブロック追記（org-babel と同じ動作）
- noweb 参照は未対応（retranscribe には不要）

使い方:
    python org_tangle.py                     # カレントの README.org を処理
    python org_tangle.py README.org          # ファイル指定
    python org_tangle.py README.org --dry-run  # ファイル一覧のみ表示
    python org_tangle.py README.org --out /tmp/out  # 出力先を指定
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import textwrap  # ← 追加
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# データ構造
# ---------------------------------------------------------------------------

@dataclass
class Block:
    """1つの #+BEGIN_SRC ブロック."""
    tangle: str           # 出力ファイルパス
    lang: str             # 言語識別子
    content: str          # ブロック本文
    src_line: int         # 元ファイルの行番号（デバッグ用）


# ---------------------------------------------------------------------------
# パーサー
# ---------------------------------------------------------------------------

# #+BEGIN_SRC lang ... :tangle <path> ...
_BEGIN_SRC_RE = re.compile(
    r"^\s*#\+BEGIN_SRC\s+(\S+)(.*)?$", re.IGNORECASE
)
_END_SRC_RE = re.compile(r"^\s*#\+END_SRC\s*$", re.IGNORECASE)

# セクション :PROPERTIES: 内の :header-args:lang: :tangle <path>
# 例: :header-args:python: :tangle retranscribe/foo.py
# 例: :header-args: :tangle retranscribe/foo.py  (言語非限定)
_HEADER_ARGS_RE = re.compile(
    r"^\s*:header-args(?::(\S+))?:\s+(.+)$", re.IGNORECASE
)

# ファイルレベル #+PROPERTY: header-args[:lang] :tangle <path>
_PROPERTY_RE = re.compile(
    r"^\s*#\+PROPERTY:\s+header-args(?::(\S+))?\s+(.+)$", re.IGNORECASE
)

def _extract_tangle(header_rest: str) -> Optional[str]:
    """ヘッダー文字列から :tangle <value> を抽出する."""
    m = re.search(r":tangle\s+(\S+)", header_rest)
    if m:
        v = m.group(1)
        return None if v.lower() == "no" else v
    return None  # :tangle 指定なし → None（継承）


def parse_org(text: str) -> list[Block]:
    """Org テキストを走査して tangle 対象ブロックのリストを返す."""

    lines = text.splitlines()
    blocks: list[Block] = []

    # ファイルレベルのデフォルト tangle: lang -> Optional[str]
    file_defaults: dict[str, Optional[str]] = {}

    # セクションスタック: 見出しレベルごとの (lang -> Optional[str])
    # level 0 = ルート
    section_stack: list[dict[str, Optional[str]]] = [{}]
    current_level = 0

    in_properties = False
    in_src = False
    src_lang = ""
    src_tangle: Optional[str] = None
    src_lines: list[str] = []
    src_start_line = 0

    for lineno, line in enumerate(lines, 1):

        # ── ファイルレベル PROPERTY ──────────────────────────────────────
        if not in_src:
            m = _PROPERTY_RE.match(line)
            if m:
                lang_key = (m.group(1) or "").lower()  # "" = 全言語
                tangle_val = _extract_tangle(m.group(2))
                file_defaults[lang_key] = tangle_val
                continue

        # ── 見出し ───────────────────────────────────────────────────────
        if not in_src:
            h = re.match(r"^(\*+)\s", line)
            if h:
                lvl = len(h.group(1))
                # スタックをこのレベルまで巻き戻す
                while len(section_stack) > lvl:
                    section_stack.pop()
                while len(section_stack) <= lvl:
                    # 親の設定を引き継ぐ
                    section_stack.append(dict(section_stack[-1]))
                current_level = lvl
                in_properties = False
                continue

        # ── PROPERTIES ブロック ──────────────────────────────────────────
        if not in_src:
            if line.strip() == ":PROPERTIES:":
                in_properties = True
                continue
            if line.strip() == ":END:":
                in_properties = False
                continue
            if in_properties:
                m = _HEADER_ARGS_RE.match(line)
                if m:
                    lang_key = (m.group(1) or "").lower()
                    tangle_val = _extract_tangle(m.group(2))
                    if tangle_val is not None:
                        section_stack[-1][lang_key] = tangle_val
                    elif ":tangle" in m.group(2).lower():
                        # :tangle no が明示されている
                        section_stack[-1][lang_key] = None
                continue

        # ── #+BEGIN_SRC ──────────────────────────────────────────────────
        if not in_src:
            m = _BEGIN_SRC_RE.match(line)
            if m:
                lang = m.group(1).lower()
                header_rest = m.group(2) or ""

                # tangle 解決の優先順位:
                # 1. ブロック自身の :tangle
                # 2. セクション :header-args:lang:
                # 3. セクション :header-args: (言語非限定)
                # 4. ファイル #+PROPERTY: header-args:lang:
                # 5. ファイル #+PROPERTY: header-args: (言語非限定)
                block_tangle = _extract_tangle(header_rest)

                if block_tangle is None and ":tangle" not in header_rest.lower():
                    # ブロックに :tangle 指定がない → セクション設定を探す
                    sec = section_stack[-1]
                    if lang in sec:
                        block_tangle = sec[lang]
                    elif "" in sec:
                        block_tangle = sec[""]
                    elif lang in file_defaults:
                        block_tangle = file_defaults[lang]
                    elif "" in file_defaults:
                        block_tangle = file_defaults[""]

                in_src = True
                src_lang = lang
                src_tangle = block_tangle
                src_lines = []
                src_start_line = lineno
                continue

        # ── #+END_SRC ────────────────────────────────────────────────────
        else:
            if _END_SRC_RE.match(line):
                if src_tangle:
                    blocks.append(Block(
                        tangle=src_tangle,
                        lang=src_lang,
                        content="\n".join(src_lines),
                        src_line=src_start_line,
                    ))
                in_src = False
                src_lang = ""
                src_tangle = None
                src_lines = []
                continue
            src_lines.append(line)

    return blocks


# ---------------------------------------------------------------------------
# 書き出し
# ---------------------------------------------------------------------------

def write_files(
    blocks: list[Block],
    base_dir: Path,
    dry_run: bool = False,
) -> dict[str, int]:
    """ブロック群をファイルに書き出す.

    Returns:
        {出力パス: 書き出したブロック数} の辞書
    """
    # tangle パスごとにブロックを集約（順序保持）
    file_blocks: dict[str, list[Block]] = {}
    for block in blocks:
        file_blocks.setdefault(block.tangle, []).append(block)

    result: dict[str, int] = {}

    for rel_path, blks in file_blocks.items():
        out_path = base_dir / rel_path
        result[rel_path] = len(blks)

        if dry_run:
            continue

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as f:
            for i, blk in enumerate(blks):
                if i > 0:
                    f.write("\n")
                content = textwrap.dedent(blk.content)  # ← 変更: インデント除去
                f.write(content)
                if content and not content.endswith("\n"):  # ← 変更: content を参照
                    f.write("\n")

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Org-babel tangle を Python で実行する",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "org_file",
        nargs="?",
        default="README.org",
        help="Org ファイルのパス（デフォルト: README.org）",
    )
    parser.add_argument(
        "--out", "-o",
        default=None,
        help="出力ベースディレクトリ（デフォルト: org ファイルと同じディレクトリ）",
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="ファイルを書き出さずにtangleターゲットの一覧のみ表示",
    )
    parser.add_argument(
        "--lang",
        default=None,
        help="指定した言語のブロックのみ処理（例: python, toml）",
    )
    parser.add_argument(
        "--file", "-f",
        default=None,
        help="指定したtangleパスのブロックのみ処理（例: retranscribe/cli.py）",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="処理中の詳細を表示",
    )

    args = parser.parse_args()

    org_path = Path(args.org_file)
    if not org_path.exists():
        print(f"Error: {org_path} が見つかりません", file=sys.stderr)
        sys.exit(1)

    base_dir = Path(args.out) if args.out else org_path.parent

    # 読み込みとパース
    text = org_path.read_text(encoding="utf-8")
    blocks = parse_org(text)

    # フィルタリング
    if args.lang:
        blocks = [b for b in blocks if b.lang == args.lang.lower()]
    if args.file:
        blocks = [b for b in blocks if b.tangle == args.file]

    if not blocks:
        print("tangle対象ブロックが見つかりませんでした。")
        sys.exit(0)

    # 書き出し
    result = write_files(blocks, base_dir, dry_run=args.dry_run)

    # 結果表示
    if args.dry_run:
        print(f"[dry-run] tangle対象: {len(result)} ファイル")
        for rel, count in sorted(result.items()):
            print(f"  {rel}  ({count} ブロック)")
    else:
        total_blocks = sum(result.values())
        print(f"tangle完了: {len(result)} ファイル / {total_blocks} ブロック")
        if args.verbose:
            for rel, count in sorted(result.items()):
                out = base_dir / rel
                size = out.stat().st_size
                print(f"  {rel}  ({count} ブロック, {size:,} bytes)")
        else:
            for rel in sorted(result.keys()):
                print(f"  {rel}")


if __name__ == "__main__":
    main()
