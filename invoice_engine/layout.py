"""Layout fingerprints: where a template's label words sit on its sample pages.

A fingerprint is the list of label words in reading order (top to bottom, then left to right),
each with the column it starts in when the page width is cut into 12 equal strips:

    layout:
      columns: 12
      sample_pages: 2
      order: [[tax, 1], [invoice, 2], [gstin, 8], [bill, 1], ...]

Only words that also occur in the template's own patterns (keywords and field patterns) are
kept, so names, addresses and amounts are never stored. Rows are not stored: a longer item
table or a taller page moves labels down but keeps their column and their order.

The fingerprint is used only to break a tie, when two layouts read a document equally well
(document.py). It never overrides the evidence of the checks.
"""

from __future__ import annotations

import math
import re
from difflib import SequenceMatcher
from html import unescape
from pathlib import Path

from . import poppler

COLUMNS = 12
# Provisional, from 7 same-layout and 6 different-layout page pairs: same layouts scored
# 0.78-1.00, different layouts 0.09-0.40 (Meesho siblings 0.74-0.86 share one layout).
# A tie is broken only when the closest template is at least MIN_SIMILARITY and ahead of the
# next by at least MIN_MARGIN; otherwise the document stays in review.
MIN_SIMILARITY = 0.6
MIN_MARGIN = 0.3

_WORD = re.compile(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="[\d.]+" yMax="[\d.]+">([^<]*)</word>')
_PAGE = re.compile(r'<page width="([\d.]+)" height="([\d.]+)">(.*?)</page>', re.S)
_LETTERS = re.compile(r"[A-Za-z]{3,}")


def page_words(pdf: Path, pages: list[int] | None = None) -> list[list[tuple[str, float, float]]]:
    """Per page: (word, x as share of the page width, y in points) for every word."""
    result = []
    for number, (width, _height, body) in enumerate(_PAGE.findall(poppler.run(["-bbox", "-enc", "UTF-8"], pdf)), start=1):
        if pages is not None and number not in pages:
            continue
        width = float(width)
        result.append([(unescape(w), float(x) / width, float(y)) for x, y, w in _WORD.findall(body)])
    return result


def label_vocabulary(template) -> set[str]:
    """Lower-case words of 3+ letters that occur in the template's keywords and patterns."""
    texts: list[str] = []
    for key in ("keywords", "exclude_keywords", "continuation_keywords"):
        texts += [k for k in template.get(key) or [] if isinstance(k, str)]

    def collect(spec):
        if isinstance(spec, str):
            texts.append(spec)
        elif isinstance(spec, list):
            for item in spec:
                collect(item)
        elif isinstance(spec, dict):
            for key, value in spec.items():
                if key in ("regex", "start", "end", "first_line", "line", "last_line", "skip_line", "rules"):
                    collect(value)

    for spec in (template.get("fields") or {}).values():
        collect(spec if isinstance(spec, (str, list)) else spec)
    # \s, \d and the like leave single letters behind; three letters or more are real words
    return {w.lower() for text in texts for w in _LETTERS.findall(text)}


def fingerprint(pages: list[list[tuple[str, float, float]]], vocabulary: set[str],
                columns: int = COLUMNS) -> list[list]:
    """[word, column] for every label word, pages in order, top to bottom, left to right."""
    order = []
    for words in pages:
        labels = [(w.lower().strip(":#.,()"), x, y) for w, x, y in words]
        labels = [(w, x, y) for w, x, y in labels if w in vocabulary]
        labels.sort(key=lambda t: (round(t[2] / 6), t[1]))          # same line = within ~6 pt
        order += [[w, min(int(x * columns), columns - 1)] for w, x, _ in labels]
    return order


def similarity(a: list, b: list) -> float:
    """Share of the two ordered lists that line up (same word, same column, same order)."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, [tuple(x) for x in a], [tuple(x) for x in b], autojunk=False).ratio()


def merge(samples: list[list[list]]) -> list[list]:
    """Keep the first sample's labels that line up with at least half of the samples."""
    if len(samples) == 1:
        return samples[0]
    base = [tuple(x) for x in samples[0]]
    votes = [1] * len(base)
    for other in samples[1:]:
        matcher = SequenceMatcher(None, base, [tuple(x) for x in other], autojunk=False)
        for block in matcher.get_matching_blocks():
            for i in range(block.a, block.a + block.size):
                votes[i] += 1
    need = math.ceil(len(samples) / 2)
    return [list(item) for item, n in zip(base, votes) if n >= need]


def layout_block(order: list[list], sample_pages: int, columns: int = COLUMNS) -> str:
    """The YAML text of a layout block, ASCII only, one entry per line."""
    lines = ["layout:", f"  columns: {columns}", f"  sample_pages: {sample_pages}", "  order:"]
    lines += [f"    - ['{word}', {column}]" for word, column in order]      # quoted: 'yes'/'off' stay text
    return "\n".join(lines) + "\n"


def write_layout(template_file: Path, block: str) -> None:
    """Replace (or add) the layout block at the end of a template file; the rest stays as it is."""
    text = template_file.read_text(encoding="utf-8")
    text = re.split(r"(?m)^layout:\s*$", text)[0].rstrip() + "\n\n"
    with open(template_file, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text + block)


def lint_layout(block) -> list[str]:
    """Errors in a template's layout block (empty list = fine)."""
    if not isinstance(block, dict):
        return ["layout must be a mapping with columns, sample_pages and order"]
    errors = []
    columns = block.get("columns")
    if not isinstance(columns, int) or not 2 <= columns <= 48:
        errors.append(f"layout.columns must be a whole number from 2 to 48, got {columns!r}")
        columns = COLUMNS
    if not isinstance(block.get("sample_pages"), int) or block["sample_pages"] < 1:
        errors.append("layout.sample_pages must be a whole number >= 1")
    order = block.get("order")
    if not isinstance(order, list) or not order:
        errors.append("layout.order must be a non-empty list of [word, column]")
    else:
        for item in order:
            if not (isinstance(item, list) and len(item) == 2 and isinstance(item[0], str)
                    and isinstance(item[1], int) and 0 <= item[1] < columns):
                errors.append(f"layout.order entry must be [word, column 0-{columns - 1}]: {item!r}")
                break
    return errors