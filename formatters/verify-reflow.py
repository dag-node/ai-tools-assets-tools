#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Prove that a reflow changed line breaks and left the text alone.

```bash
python3 formatters/verify-reflow.py --base <revision> [--repo P] [--prose | --source] [--] <path>...
python3 formatters/verify-reflow.py --against <dir> [--prose | --source] [--] <path>...
```

Reads each path at the base (a git revision, or the same relative path under `--against`) and in
the tree, and reports every difference in four comparisons. Passing all four says the only
change is where the lines break, which is what lets a reflow commit be reviewed in bulk instead
of hunk by hunk:

1. The PROTECTED LINES, line for line: YAML frontmatter, a fenced block with its fences, an HTML
   comment from a `<!--` at a line start to its `-->`, a table row, an alert line, an indented
   code block, and a line carrying `prose-check: ignore`. A line break inside one is intentional,
   so a difference is a defect in the filler. The fence rule is CommonMark's (a fence closes on
   its own character at its own length or longer) rather than a toggle, since a toggle reads
   the ```` ```bash ```` inside a `~~~markdown` block as a close and agrees with a filler that
   made the same mistake. A protected line whose words survive and whose spacing changed is
   reported as `respaced`, apart from one whose words changed: a filler that realigned a row it
   should have left is a different defect from one that damaged it, and both fail.
2. The TOKEN STREAM: every line split on whitespace past its line prefix, so a changed, dropped,
   added, or reordered word is reported with its position.
3. The BLOCK SIGNATURES: the leading whitespace, line prefix and list marker of the first line
   of every blank-separated block, how many of its lines carry another prefix, and how many open
   with a block marker. Their count catches a merged or split paragraph, which the token stream
   cannot see because every token survives; their values catch a dropped indent, a dropped
   prefix, a respaced marker, or a list item or heading a wrap invented by moving its marker to a
   line start, none of which it can see because it splits on whitespace and reads past a prefix.
4. The COLUMN TABLES of a source file's comments, read as the comment filler reads them
   (`formatters/emacs/ai-tools-fill.el`): rows of a first column, a gap of two spaces or more and
   a second column, with continuation lines indented under the second. The filler re-pads such a
   table and wraps its cells, so its lines are not protected; what holds instead is its first
   column, row by row, and its alignment: a table whose rows start their second column at one
   column in the base does so in the tree, at that column or another, and each continuation line
   sits where a continuation of the base table sat or at the second column itself. A table whose
   rows the base did not align is held to its first column alone. The token stream ignores
   spacing, so without this a column a fill broke passes.

A difference is reported with the line it starts on in each copy, and every one is reported,
since the second difference in a file is as much the filler's as the first.

A LINE PREFIX is the structure a filler carries onto every new line it makes: a blockquote's `>`
on a page, and a comment marker in a source file, where the prose a filler wraps is the text the
marker opens. A prefix is read past for the token stream and recorded in the block signature, so
the gate reads a marker that landed on another line as the fill it is, and reports a marker a
filler dropped — which turns a comment line into a code line. How a path is read follows the
checker's extension rule: a page and a man page are read whole as prose, every other path as
source. `--prose` and `--source` override it, since that rule fails silently in one direction —
a document copy whose name lost its extension reads as source, where its `#` headings would be
read as comment markers.

Three of the block rules then read a comment as the comment filler does, since a comment reaches
its reader as written and a page's rules would report the fill itself:

- what stays verbatim is a line holding a column -- a gap of two spaces or more that does not
  follow a sentence end, as in a signature or an example -- outside a column table, rather than
  a four-space indented block, since a comment's leading indentation is the language's own and
  a code line is left to the token stream;
- the markers counted are a bullet and a table row, where a page also counts `+`, a
  parenthesized number, a heading and an HTML comment, each of which a wrap lands on a line
  start by filling ordinary prose;
- `prose-check: ignore` marks the line where it opens the comment's text, where a page carries
  it at the end of the line it marks.

Exits 0 when every path passes and 1 otherwise, printing a line per difference: the path, the
check that failed, and the line in each copy. The base is verified before any path is read --
`--base` names a tree of the repository, `--against` names a directory -- and a run whose base
does not exist exits 1 on that alone, so a misspelt revision is an error rather than a run of
skipped paths. A path the tree holds and the base does not is reported as skipped rather than as
a pass; a path the tree does not hold is a failure.
Both copies are read through `formatters/text_file.py`, so a copy that is not plain text is reported
as a failure with its reason and no token of it reaches the terminal; a path that resolves outside
the tree or the base directory is refused the same way. This is the mechanical half of the reflow gate;
`prose-check.py --kept` is the other half and judges a REWRITE, which a reflow that passes here
has not made.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
from collections import Counter
from difflib import SequenceMatcher
from typing import NamedTuple

import text_file

GIT_TIMEOUT = 60
FENCE_MARK = re.compile(r"^\s*(`{3,}|~{3,})")
TABLE = re.compile(r"^\s*\|")
COMMENT_OPEN, COMMENT_CLOSE = re.compile(r"^\s*<!--"), "-->"
FRONTMATTER = re.compile(r"^---\s*$")
IGNORE_MARKER = "prose-check: ignore"
# A blockquote's prefix is structure, not a token: a filled quote carries it on every new line, so the tokens are read
# past it and the block signature records it instead.
QUOTE = re.compile(r"^(\s*(?:>\s?)+)(.*)$")
# A line comment's marker is that same structure in a source file. The set is the checker's own LINE_COMMENT -- a `#`
# that does not open a shebang, and `//` -- so both tools read one comment the same way.
LINE_COMMENT = re.compile(r"^(\s*(?:#(?!!)|//+)\s?)(.*)$")
# The extensions the checker reads whole as prose. Every other path is read as source.
PROSE_WHOLE_FILE = (".md", ".1", ".5", ".7", ".8")
# A comment line holding a column: a gap of two spaces or more that does not follow a sentence end, as in a signature
# or an example. This is the comment filler's own reading of a line to leave as written (`ai-tools-fill--skip-line`),
# and it stands where a page has its four-space indented block -- a comment's leading indentation is the language's
# own. A line of a column table is read by the table rules instead.
COLUMNED = re.compile(r"[^ \t.!?] {2,}[^ \t]")
# A column table as the comment filler reads it (`ai-tools-fill--table-line` and the constants beside it): a comment
# line's marker, the spaces after it and its text; the gap that ends a row's first column; the text that is not a row
# although it may hold a gap; and a fence marker, which ends a table.
COLUMN_LINE = re.compile(r"^([ \t]*(?:#|//))( *)(\S.*)$")
COLUMN_GAP = re.compile(r"[^ \t.!?]( {2,})[^ \t]")
NOT_A_ROW = re.compile(r"[|│┃║]|[-=_*─━]{3}|^(?:!|shellcheck\b|noqa\b|pylint:|type:|pragma\b|SPDX-|ref-index:"
                       r"|prose-check:|ai-tools-admin-[a-z-]*:|[A-Za-z_][A-Za-z0-9_]*=|args:|stdout:|stderr:"
                       r"|returns?:|\$[0-9]+[ \t]|Values:|Defaults?:)")
COMMENT_FENCE = re.compile(r"^[ \t]*(?:#|//)[ \t]*(?:```|~~~)")
ALERT = re.compile(r"^\[![A-Z]+\]\s*$")
# A list marker with the spaces after it: its width is the item's content indent, so a marker respaced is a structure
# change and is part of the block signature.
ITEM = re.compile(r"^\s*([-*+] +|\d+[.)] +)\S")
# A token that opens a block at a line start. A wrap that moves one there invents the block, and the token stream cannot
# see it, so the count of such lines is part of each block's signature.
MARKER_LINE = re.compile(r"^\s*(?:[-*+] |\d+[.)] |#{1,6} |\||`{3,}|~{3,}|<!--)")
# The same for a comment, which reaches its reader as written: a bullet and a table row are the shapes a filler must
# keep, and the rest are read as the ordinary prose they are. A tree's comments put `+` between two names and a mode
# in parentheses (`0700)`) at a line start by wrapping alone, so counting those reports a fill that did its job.
SOURCE_MARKER_LINE = re.compile(r"^\s*(?:[-*] |\||`{3,}|~{3,})")

Signature = tuple[str, int, int]
# Each element of a comparison carries the 1-based line it was read from, which the report names.
Numbered = list[tuple[int, object]]


class Table(NamedTuple):
    """A column table: its rows as (line, first-column words, second-column column), its continuation lines as
    (line, column), and the column its rows align at, or None where they do not align."""
    rows: list[tuple[int, tuple[str, ...], int]]
    continuations: list[tuple[int, int]]
    column: int | None


class Partition(NamedTuple):
    protected: Numbered
    tokens: Numbered
    blocks: Numbered
    tables: list[Table]


def table_line(line: str) -> tuple[str, int, tuple[tuple[str, ...], int] | None] | None:
    """(marker, spaces after it, row) of a comment line, or None where `line` is not one. The row is (the first
    column's words, the column its second column starts at) on a row and None on any other line."""
    match = COLUMN_LINE.match(line)
    if not match:
        return None
    marker, spaces, text = match.groups()
    row = None
    if not NOT_A_ROW.search(text):
        gap = COLUMN_GAP.search(text)
        if gap:
            row = (tuple(text[:gap.start(1)].split()), len(marker) + len(spaces) + gap.end(1))
    return marker, len(spaces), row


def starts_word_at(line: str, column: int) -> bool:
    """Whether a word of `line` starts at `column`."""
    return 0 < column < len(line) and line[column - 1] == " " and line[column] != " "


def column_tables(lines: list[str]) -> list[Table]:
    """The column tables of a source file's comments, as the comment filler reads them.

    A table opens on a row outside a fenced block and takes the rows after it under the same marker and indent, and
    each continuation line: deeper than the rows and not a fence marker. It takes two rows, or a row and
    a continuation. The rows align where two of them, or the one, agree on a column and a word of every row starts
    there. The filler also requires a continuation to sit no further left than the leftmost second column; this reads
    a line left of it as a continuation too, so a continuation a fill moved left of its column is still held to it.
    """
    fenced, inside = set(), False
    for index, line in enumerate(lines):
        if COMMENT_FENCE.match(line):
            inside = not inside
        elif inside:
            fenced.add(index)
    tables: list[Table] = []
    index = 0
    while index < len(lines):
        first = table_line(lines[index])
        if first is None or first[2] is None or index in fenced:
            index += 1
            continue
        marker, indent = first[0], first[1]
        rows: list[tuple[int, tuple[str, ...], int]] = []
        continuations: list[tuple[int, int]] = []
        end = index
        while end < len(lines):
            read = table_line(lines[end])
            if read is None or read[0] != marker:
                break
            if read[1] == indent and read[2] is not None:
                rows.append((end + 1, read[2][0], read[2][1]))
            elif read[1] > indent and not COMMENT_FENCE.match(lines[end]):
                continuations.append((end + 1, len(marker) + read[1]))
            else:
                break
            end += 1
        if len(rows) < 2 and not continuations:
            index += 1
            continue
        counts = Counter(column for _, _, column in rows)
        column, agreeing = max(sorted(counts.items()), key=lambda item: item[1])
        aligned = agreeing >= min(2, len(rows)) and all(
            starts_word_at(lines[number - 1], column) for number, _, _ in rows)
        tables.append(Table(rows, continuations, column if aligned else None))
        index = end
    return tables


def line_parts(line: str, source: bool) -> tuple[str, str, bool]:
    """(line prefix, the text after it, whether a comment marker opened it) of `line`.

    The prefix is a source file's comment marker followed by any blockquote prefix inside it, and
    is empty on a code line and outside a blockquote.
    """
    prefix, marked = "", False
    if source:
        comment = LINE_COMMENT.match(line)
        if comment:
            prefix, line, marked = comment.group(1), comment.group(2), True
    quote = QUOTE.match(line)
    if quote:
        prefix, line = prefix + quote.group(1), quote.group(2)
    return prefix, line, marked


def partition(text: str, source: bool = False) -> Partition:
    """`text` as (protected lines, tokens, block signatures, column tables), read as source where `source`.

    A block signature is the leading whitespace, line prefix and list marker of a blank-separated
    block's first line, the count of its lines carrying a different prefix, and the count of its
    lines opening with a block marker, so a prefix a filler dropped on a continuation line, a
    marker it respaced, or a list item, heading, row or fence a wrap invented mid-block is a
    difference here. Every element but a table carries the line it was read from.
    """
    protected: Numbered = []
    tokens: Numbered = []
    blocks: list[list] = []
    fence, in_comment, block = None, False, None
    # An indented code block is protected whole: four spaces after a blank line, outside a list, where the same indent
    # is a continuation paragraph the filler may fill (the reading `prose-check.py` and `formatters/fill-markdown.py`
    # share).
    listed, in_code = False, False
    lines = text.split("\n")
    tables = column_tables(lines) if source else []
    in_table = {number for table in tables
                for number in [row[0] for row in table.rows] + [line[0] for line in table.continuations]}
    front = 0
    if lines and FRONTMATTER.match(lines[0]):
        front = 1
        while front < len(lines) and not FRONTMATTER.match(lines[front]):
            front += 1
        front = min(front + 1, len(lines))
        protected.extend(enumerate(lines[:front], 1))
    for number, line in enumerate(lines[front:], front + 1):
        prefix, rest, marked = line_parts(line, source)
        # A comment's prose begins after its marker, so a source file's indents, blank lines and blocks are read
        # from the text the marker opens; a page and a code line carry no marker and are read as they stand.
        body = rest if marked else line
        mark = FENCE_MARK.match(rest)
        if fence is None and not mark:  # a fence and its content leave the state alone
            if ITEM.match(rest):
                listed = True
            elif body.strip() and len(body) - len(body.lstrip(" ")) < 2:
                listed = False
        if fence is not None:
            protected.append((number, line))
            if mark and mark.group(1)[0] == fence[0] and len(mark.group(1)) >= len(fence):
                fence = None
        elif mark:
            protected.append((number, line))
            fence = mark.group(1)
        elif in_comment:
            protected.append((number, line))
            in_comment = COMMENT_CLOSE not in line
        elif COMMENT_OPEN.match(line):
            protected.append((number, line))
            in_comment = COMMENT_CLOSE not in line
        # The ignore marker is a directive where it opens a comment's text, and prose where a sentence names it. A page
        # carries it at the end of a line it marks, so only a source file reads the position.
        elif TABLE.match(rest) or ALERT.match(rest.strip()) or (
                rest.startswith(IGNORE_MARKER) if source else IGNORE_MARKER in line):
            protected.append((number, line))
        elif source and marked and number not in in_table and COLUMNED.search(rest):
            protected.append((number, line))
        elif not source and (in_code or (block is None and not listed and line.startswith("    "))):
            protected.append((number, line))
            in_code = True
        if not body.strip():
            block, in_code = None, False
            continue
        # In a source file a prefix change opens a block too: a comment paragraph and the code under it are separate
        # text, often with no blank line between them. On a page a line that drops the quote prefix is a lazy
        # continuation of the same block, so the count of those is what the signature records instead.
        if block is None or (source and prefix != block[1]):
            item = ITEM.match(rest)
            head = body[:len(body) - len(body.lstrip(" "))] + prefix + (item.group(1) if item else "")
            block = [head, prefix, 0, 0, number]
            blocks.append(block)
        elif prefix != block[1]:
            block[2] += 1
        block[3] += bool((SOURCE_MARKER_LINE if source else MARKER_LINE).match(rest))
        tokens.extend((number, token) for token in rest.split())
    return Partition(protected, tokens,
                     [(number, (head, lazy, markers)) for head, _, lazy, markers, number in blocks], tables)


def span(elements: Numbered, low: int, high: int) -> str:
    """The values of `elements` from `low` to `high`, with three on each side, joined for a report line."""
    values = [str(value) for _, value in elements[max(0, low - 3):max(high, low) + 3]]
    return " ".join(values) if values else "(none)"


def line_at(elements: Numbered, index: int) -> str:
    """The line the element at `index` was read from, or the end of the copy past its last element."""
    return f"line {elements[index][0]}" if index < len(elements) else "the end"


def respaced(old: list, new: list) -> bool:
    """Whether each line of `new` holds the words of the line of `old` beside it, at the same indent."""
    return len(old) == len(new) and all(
        a.split() == b.split() and a[:len(a) - len(a.lstrip())] == b[:len(b) - len(b.lstrip())]
        for a, b in zip(old, new))


def differences(name: str, before: Numbered, after: Numbered) -> list[tuple[str, str]]:
    """(check, detail) for every run of elements one copy holds and the other does not.

    A run of protected lines whose words and indents survive is reported as `respaced line`.
    """
    old = [value for _, value in before]
    new = [value for _, value in after]
    if old == new:
        return []
    found = []
    for tag, low, high, start, stop in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        check = name
        if name == "protected line" and tag == "replace" and respaced(old[low:high], new[start:stop]):
            check = "respaced line"
        found.append((check, f"base {line_at(before, low)}, tree {line_at(after, start)}\n"
                             f"      base: {span(before, low, high)}\n"
                             f"      tree: {span(after, start, stop)}"))
    return found


def first_columns(tables: list[tuple[tuple[str, ...], ...]]) -> str:
    """The first columns of `tables`, a row's words joined by spaces, its rows by commas, and tables by slashes."""
    return " / ".join(", ".join(" ".join(words) for words in table) for table in tables) or "(none)"


def table_differences(before: list[Table], after: list[Table]) -> list[tuple[str, str]]:
    """(check, detail) for each column table whose first column, or whose alignment, the tree does not keep.

    The tables are paired by their first columns. A table the base aligned is aligned in the tree, at any column,
    and each continuation line of it sits where one of the base table's sat against the column, or at the column.
    """
    found = []
    old = [tuple(words for _, words, _ in table.rows) for table in before]
    new = [tuple(words for _, words, _ in table.rows) for table in after]
    for tag, low, high, start, stop in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag != "equal":
            base_line = f"line {before[low].rows[0][0]}" if high > low else "the end"
            tree_line = f"line {after[start].rows[0][0]}" if stop > start else "the end"
            found.append(("column table", f"base {base_line}, tree {tree_line}\n"
                                          f"      base: {first_columns(old[low:high])}\n"
                                          f"      tree: {first_columns(new[start:stop])}"))
            continue
        for base, tree in zip(before[low:high], after[start:stop]):
            if base.column is None:
                continue
            where = f"base line {base.rows[0][0]}, tree line {tree.rows[0][0]}"
            if tree.column is None:
                found.append(("column alignment", f"{where}: the rows the base aligned at column {base.column} "
                                                  f"do not align"))
                continue
            offsets = {column - base.column for _, column in base.continuations} | {0}
            stray = [number for number, column in tree.continuations if column - tree.column not in offsets]
            if stray:
                found.append(("column alignment", f"{where}: the continuation at tree line {stray[0]} left "
                                                  f"its column"))
    return found


def inside(root: pathlib.Path, path: str) -> pathlib.Path:
    """`path` under `root`, or `text_file.Refused` where it resolves outside `root`."""
    resolved = (root / path).resolve()
    if root != resolved and root not in resolved.parents:
        raise text_file.Refused(path, f"resolves outside {root}")
    return resolved


def base_problem(repo: pathlib.Path, revision: str | None, against: pathlib.Path | None) -> str | None:
    """What keeps the base from being read at all, or None: `--against` does not name a directory, or `--base`
    does not name a tree of `repo` (a misspelt revision, a repository that is none, a git that cannot run).

    Checked once before any path, since `base_text` reads a nonzero `git show` as a path the base
    does not hold, and that reading is sound only once the revision itself is known to resolve.
    """
    if against is not None:
        return None if against.is_dir() else f"--against {against} is not a directory"
    try:
        verified = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", "--end-of-options", f"{revision}^{{tree}}"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT, check=False)
    except OSError as exc:
        return f"git cannot run: {exc.strerror}"
    if verified.returncode:
        return f"--base {revision}: " + (verified.stderr.strip() or f"not a tree of {repo}")
    return None


def base_text(repo: pathlib.Path, revision: str | None, against: pathlib.Path | None,
              path: str) -> str | None:
    """The base copy of `path`, or None where the base does not hold it.

    The base itself is one `base_problem` has passed, so a nonzero `git show` is the path's absence.
    """
    if against is not None:
        try:
            return text_file.read(str(inside(against, path)))[0]
        except FileNotFoundError:
            return None
    shown = subprocess.run(
        ["git", "-C", str(repo), "show", "--end-of-options", f"{revision}:./{path}"],
        capture_output=True, stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT, check=False)
    return None if shown.returncode else text_file.decode(f"{revision}:{path}", shown.stdout)


def verify(repo: pathlib.Path, revision: str | None, against: pathlib.Path | None,
           path: str, force: bool | None = None) -> list[tuple[str, str]]:
    """(check, detail) for every difference on `path`, empty when the reflow is pure. A path the base does not hold,
    or one either copy of which `text_file` refuses or the system does not read, yields one entry: `skipped`,
    `refused` or `unreadable`.

    `force` reads the path as source (True) or as prose (False), where None leaves the extension
    to decide.
    """
    source = not path.endswith(PROSE_WHOLE_FILE) if force is None else force
    try:
        # The tree copy first: a path the tree does not hold is unreadable, not a skip.
        after = partition(text_file.read(str(inside(repo, path)))[0], source)
        base = base_text(repo, revision, against, path)
        if base is None:
            return [("skipped", f"the base does not hold {path}")]
        before = partition(base, source)
    except text_file.Refused as exc:
        return [("refused", exc.reason)]
    except OSError as exc:
        return [("unreadable", exc.strerror)]
    return (differences("protected line", before.protected, after.protected)
            + differences("token", before.tokens, after.tokens)
            + differences("block signature", before.blocks, after.blocks)
            + table_differences(before.tables, after.tables))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="prove a reflow changed only line breaks")
    base = parser.add_mutually_exclusive_group(required=True)
    base.add_argument("--base", dest="revision", metavar="REVISION",
                      help="the revision the reflow started from")
    base.add_argument("--against", metavar="DIR", help="read the base copies under DIR instead")
    parser.add_argument("--repo", default=".", help="the tree holding the reflowed paths")
    reading = parser.add_mutually_exclusive_group()
    reading.add_argument("--prose", dest="force", action="store_const", const=False,
                         help="read every line as prose, whatever the extension")
    reading.add_argument("--source", dest="force", action="store_const", const=True,
                         help="read a comment marker as a line prefix, whatever the extension")
    parser.add_argument("paths", nargs="+", metavar="PATH")
    args = parser.parse_args(argv)
    repo = pathlib.Path(args.repo).resolve()
    against = pathlib.Path(args.against).resolve() if args.against else None
    problem = base_problem(repo, args.revision, against)
    if problem is not None:
        print(f"verify-reflow: {problem}", file=sys.stderr)
        return 1

    failed = skipped = passed = 0
    for path in args.paths:
        result = verify(repo, args.revision, against, path, args.force)
        if not result:
            passed += 1
        elif result[0][0] == "skipped":
            skipped += 1
            print(f"SKIP {path}: {result[0][1]}")
        elif result[0][0] in ("refused", "unreadable"):
            failed += 1
            print(f"FAIL {path}: {result[0][0]}, {result[0][1]}")
        else:
            failed += 1
            for check, detail in result:
                print(f"FAIL {path}: {check} differs at {detail}")
    print(f"{passed} pure reflow(s), {failed} failure(s), {skipped} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
