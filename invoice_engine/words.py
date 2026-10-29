"""Word positions of a digital PDF: every word with its box on the page, from pdftotext -bbox-layout.

The page text the templates read (pdftotext -layout) places words in character columns. When a
value is wider than its column, pdftotext pushes the rest of that line to the right, so on that
line a column can stand several characters away from its heading. The word boxes do not move: a
word is where the PDF draws it. Two uses:

- `layout_text`: the page text rebuilt from the boxes (kept for OCR later). Not used for tables:
  pdftotext -layout widens its columns to fit crowded rows, which a rebuild at one fixed scale
  does not (tested on a PDF with a long narrow-lettered description: the rebuild misplaced the
  row, pdftotext did not).
- `region`: the words of one block on the page, found by a heading and its column: "Billing
  Address" and the words below it, inside its column, down to a stop line or a gap. A name or an
  address cannot run into the column next to it or the label below it (parser: region).

The document being read is known through `current`: document.attempt sets it for the duration of
one template's parse (the PDF, its pages, and the rewrite of the stress test, if any).
"""

from __future__ import annotations

import contextvars
import re
import statistics
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import poppler

current: contextvars.ContextVar[dict | None] = contextvars.ContextVar("invoice_engine_document", default=None)


@dataclass(frozen=True)
class Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2


@lru_cache(maxsize=256)
def _read(path: str, mtime: float) -> tuple[tuple[Word, ...], ...]:
    xml = poppler.run(["-bbox-layout", "-enc", "UTF-8"], Path(path))
    xml = re.sub(r"<!DOCTYPE[^>]*>", "", xml)
    xml = re.sub(r'\sxmlns="[^"]+"', "", xml, count=1)
    root = ET.fromstring(xml)
    pages = []
    for page in root.iter("page"):
        words = []
        for w in page.iter("word"):
            text = (w.text or "").strip()
            if text:
                words.append(Word(float(w.get("xMin")), float(w.get("yMin")), float(w.get("xMax")),
                                  float(w.get("yMax")), text))
        pages.append(tuple(words))
    return tuple(pages)


def page_words(pdf: Path) -> list[list[Word]]:
    """The words of every page (empty lists for a scanned page). Read once per file."""
    pdf = Path(pdf)
    return [list(p) for p in _read(str(pdf.resolve()), pdf.stat().st_mtime)]


def rows(words: list[Word]) -> list[list[Word]]:
    """Words grouped into visual lines (the same height on the page), each left to right. Words
    of one line can come from different text blocks (columns), so blocks are not used."""
    if not words:
        return []
    height = statistics.median(w.y1 - w.y0 for w in words) or 1.0
    out: list[list[Word]] = []
    for word in sorted(words, key=lambda w: (w.yc, w.x0)):
        if out and abs(word.yc - statistics.fmean(x.yc for x in out[-1])) <= height * 0.45:
            out[-1].append(word)
        else:
            out.append([word])
    return [sorted(row, key=lambda w: w.x0) for row in out]


def char_width(words: list[Word]) -> float:
    widths = [(w.x1 - w.x0) / len(w.text) for w in words if len(w.text) >= 3]
    return statistics.median(widths) if widths else 4.0


def _column_starts(lines: list[list[Word]], unit: float) -> dict[float, float]:
    """Left edges of words that start a column (first on their line, or after a wide gap), with
    edges less than a character apart snapped together: a heading and the values under it that
    the PDF draws a fraction of a character apart land in the same text column."""
    starts = sorted({w.x0 for row in lines for i, w in enumerate(row)
                     if i == 0 or w.x0 - row[i - 1].x1 >= unit * 1.5})
    snapped: dict[float, float] = {}
    group_start = None
    previous = None
    for x in starts:
        if previous is None or x - previous > unit * 0.8:
            group_start = x
        snapped[x] = group_start
        previous = x
    return snapped


def layout_text(words: list[Word]) -> str:
    """The page as text, each word in the character column of its x position. Words closer than a
    character and a half are one space apart (one phrase); further apart, a word stands at its
    own column (at least two spaces after the previous one). Vertical gaps become blank lines."""
    if not words:
        return ""
    unit = char_width(words)
    lines = rows(words)
    height = statistics.median(w.y1 - w.y0 for w in words) or 1.0
    snapped = _column_starts(lines, unit)
    origin = min(snapped.values()) if snapped else 0.0
    out: list[str] = []
    previous_y = None
    for row in lines:
        y = statistics.fmean(w.yc for w in row)
        if previous_y is not None:
            blank = int(round((y - previous_y) / (height * 1.25))) - 1
            out.extend([""] * max(0, min(blank, 2)))
        previous_y = y
        text = ""
        last = None
        for word in row:
            column = int(round((snapped.get(word.x0, word.x0) - origin) / unit))
            if last is None:
                text = " " * column + word.text
            elif word.x0 - last.x1 < unit * 1.5:
                text += " " + word.text
            else:
                text += " " * max(2, column - len(text)) + word.text
            last = word
        out.append(text)
    return "\n".join(out)


def document_text(pages: list[int], rewrite=None) -> str | None:
    """The rebuilt text of the current document's pages (None when no PDF is being read or it
    has no words), in the engine's standard form, with the stress test's rewrite applied."""
    context = current.get()
    if not context or not context.get("pdf"):
        return None
    from .segment import canonical
    try:
        all_pages = page_words(context["pdf"])
    except Exception:
        return None
    texts = []
    for number in pages:
        if 1 <= number <= len(all_pages) and all_pages[number - 1]:
            text = layout_text(all_pages[number - 1])
            change = context.get("rewrite")
            texts.append(canonical(change(text) if change else text))
    return "\n".join(texts) if texts else None


# --- regions ---------------------------------------------------------------------------------------

def _row_text(row: list[Word]) -> str:
    return " ".join(w.text for w in row)


def _column_edges(rows_near: list[list[Word]], beyond: float, unit: float) -> list[float]:
    """Left edges of words that start a column (after a gap of two characters or more) on the
    given rows, right of `beyond`."""
    edges = []
    for row in rows_near:
        for previous, word in zip(row, row[1:]):
            if word.x0 - previous.x1 >= unit * 2 and word.x0 > beyond:
                edges.append(word.x0)
    return edges


def _staggered_right(lines: list[list[Word]], index: int, start: float, after: float, unit: float,
                     height: float, label_edge: float | None = None) -> float:
    """The left edge of the next column when its lines are not level with the block's (two
    columns whose lines are offset by half a line: Invoice To: | State Code: 24 printed between
    the buyer's lines). Rows near the heading that have no word at the block's left edge belong
    to the other column; their leftmost word right of the heading is that column's edge."""
    edge = float("inf")
    previous_y = statistics.fmean(w.yc for w in lines[index])
    for row in lines[index + 1:index + 12]:
        y = statistics.fmean(w.yc for w in row)
        if y - previous_y > height * 2.6:
            break
        if label_edge is not None and any(w.x1 <= label_edge for w in row):
            break                                       # the next label: the block ends here
        previous_y = y
        if any(abs(w.x0 - start) < unit * 2 for w in row):
            continue
        beyond = [w.x0 for w in row if w.x0 > after + unit * 2]
        if beyond and len(beyond) == len(row):         # the whole row lies right of the heading
            edge = min(edge, min(beyond) - unit * 0.5)
    return edge


def region(pages: list[list[Word]], heading: str, stop: str | None = None, max_lines: int = 8,
           skip: int = 0, same_line: bool = False) -> list[str] | None:
    """The lines of text below a heading, inside the heading's column, top to bottom.

    The column starts at the heading's first word (a character to the left is allowed) and ends
    where the next word on the heading's line begins, if that word stands apart (two characters
    or more: the next column's heading); else at the page edge. On each line below, the words in
    the column are taken from the left until a gap of three characters (a gap that wide is the
    next column, such as a note printed at the right edge). Reading stops at a line that matches
    `stop` (not included), at a vertical gap of more than about a blank line, or after max_lines.
    same_line: the value starts on the heading's line ("Address: X"): the column runs from the
    value's first word to the next word on that line that stands apart (the next column's label),
    and the lines below hang under the value; a line with a word left of the value (the next
    label, "Pincode:") ends the block.
    skip: lines left out at the top (the name line, when the address is wanted)."""
    pattern = re.compile(heading)
    stop_pattern = re.compile(stop) if stop else None
    for words in pages:
        if not words:
            continue
        lines = rows(words)
        unit = char_width(words)
        height = statistics.median(w.y1 - w.y0 for w in words)
        for index, row in enumerate(lines):
            # the row as text; a colon or other mark drawn as a word of its own ("Address", ":")
            # is joined to the word before it, as it reads ("Address:")
            text, spans = "", []
            for word in row:
                if text and not re.fullmatch(r"[:;,.#-]+", word.text):
                    text += " "
                spans.append((len(text), len(text) + len(word.text), word))
                text += word.text
            found = pattern.search(text)
            if not found:
                continue
            covered = [w for start, end, w in spans if start < found.end() and end > found.start()]
            if not covered:
                continue
            after = [w for w in row if w.x0 >= covered[-1].x1]
            right = float("inf")
            collected: list[str] = []
            if same_line:
                # "Address: value ...": the column is the value's, from its first word to the next
                # word that stands apart (the next column's label); the lines below hang under it
                if not after:
                    return None
                # columns line up: where a word starts after a wide gap on the lines just above
                # and below, the next column starts, even where this line runs up to it
                edges = _column_edges(lines[max(0, index - 2):index] + lines[index + 1:index + 3],
                                      after[0].x0 + unit * 2, unit)
                value, previous = [after[0]], after[0]
                for word in after[1:]:
                    if word.x0 - previous.x1 >= unit * 2 or any(abs(word.x0 - e) < unit * 0.8 for e in edges):
                        right = word.x0 - unit * 0.5
                        break
                    value.append(word)
                    previous = word
                left = value[0].x0 - unit
                collected.append(" ".join(w.text for w in value))
            else:
                left = covered[0].x0 - unit
                # the next column's heading: the first word after the heading that stands apart from
                # the word before it (two characters or more)
                previous = covered[-1]
                for word in after:
                    if word.x0 - previous.x1 >= unit * 2:
                        right = word.x0 - unit * 0.5
                        break
                    previous = word
            if same_line:
                right = min(right, _staggered_right(lines, index, value[0].x0, value[0].x0, unit, height, left))
            else:
                right = min(right, _staggered_right(lines, index, covered[0].x0, covered[-1].x1, unit, height))
            last_y = statistics.fmean(w.yc for w in row)
            for below in (lines[index + 1:] if len(collected) < max_lines + skip else []):
                y = statistics.fmean(w.yc for w in below)
                # a heading is often set apart from its first line: a little more room there
                if y - last_y > height * (2.6 if collected else 3.4):
                    break
                if same_line and any(w.x1 <= left for w in below):
                    break               # a new label in the label column: the next field
                if any(w.x0 < left < w.x1 - unit for w in below):
                    # a word runs across the column's left edge: the block is not left-aligned
                    # under its heading (centred or right-aligned), so its lines cannot be told
                    # from the next column's: no value rather than a part of one
                    return None
                inside = []
                for word in below:
                    if word.x0 < left or word.x0 >= right:
                        continue
                    if inside and word.x0 - inside[-1].x1 > unit * 3:
                        break
                    inside.append(word)
                if not inside:
                    continue
                line = " ".join(w.text for w in inside)
                if stop_pattern and stop_pattern.search(line):
                    break
                collected.append(line)
                last_y = y
                if len(collected) >= max_lines + skip:
                    break
            collected = collected[skip:]
            return collected or None
    return None


def parse(template, field: str, settings: dict, content: str):
    """invoice2data parser entry point (registered as `region` by compat.install): the field's
    value is the text of a region of the page, found by its heading (see region()). Without the
    PDF (a template run on text alone) the field is empty."""
    context = current.get()
    if not context or not context.get("pdf"):
        return None
    try:
        all_pages = page_words(context["pdf"])
    except Exception:
        return None
    pages = [all_pages[p - 1] for p in context.get("pages") or [] if 1 <= p <= len(all_pages)]
    lines = region(pages, settings["heading"], settings.get("stop"), int(settings.get("max_lines", 8)),
                   int(settings.get("skip", 0)), bool(settings.get("same_line", False)))
    if not lines:
        return None
    if settings.get("first_line"):
        lines = lines[:1]
    value = str(settings.get("join", " ")).join(lines)
    for pair in settings.get("replace") or []:
        value = re.sub(pair[0], pair[1], value)
    return value.strip() or None
