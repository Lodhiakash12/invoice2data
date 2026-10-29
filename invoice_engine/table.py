"""Item tables read by their column headings (field parser `table`).

The `lines` parser reads each row with one fixed pattern: "qty, gross, discount, taxable, ONE tax,
total". A document of the same layout with one more column (CGST + SGST instead of IGST, a CESS
or Discount column) or one less (no Qty) then matches nothing. This parser reads the header of the
table instead: every value on a row goes to the column whose heading is above it, so columns that
are added or missing are handled without a new pattern.

    lines:
      parser: table
      start: 'Product\\s+Title\\s+Qty'      # a regex on the first header line
      end: '(?m)^\\s+Total\\s+\\d'           # a regex on the line after the table
      columns:                              # line field: heading (regex), searched in the header
        category: 'Product'
        name: 'Title'
        qty: 'Qty'
        gross_amount: 'Gross'
        discount: 'Discount'
        price_subtotal: 'Taxable'
        igst_amount: 'IGST'
        cgst_amount: 'CGST'
        sgst_amount: 'SGST'
        price_total: 'Total'
      row: [price_total]        # columns a line must have a value in to start a row (default: the
                                # rightmost numeric column)
      text: after               # text lines belong to the row above (after) or below (before)
      row_end: '^IMEI'          # optional: a line that closes the row (later text goes to the next)
      extra:                    # optional: patterns tried first on lines that start no row; their
        - '^\\s+IGST:\\s*(?P<igst_rate>[\\d.]+)\\s*%'   # named groups are added to the row
      skip: '^\\s*Page \\d'      # optional
      prefix: '^SAC:\\s*(?P<hsn_sac>\\d+)'   # optional: a value in front of the row with no heading
      header_lines: 4           # optional: most lines the header can have (it ends at a blank line)
      max_gap: 12               # optional: furthest a value may be from its heading (characters)
      types: {qty: int, price_total: float, ...}

How a value finds its column: the line is cut into cells (each number is a cell; words separated
by single spaces form one text cell, numbers included: "Pack of 2"). The cells are matched to the columns in left-to-right order,
each to the column whose heading is nearest; a number can only go to a numeric column (one with a
type in `types`). A text column without a heading of its own (or text left of every heading) is
collected in `name`. Values a row already has are never overwritten: a second value for the same
column is joined to the first, which the engine reports as an error instead of keeping one of them.

A column the template does not list is never read into another column: a value printed under a
heading that no listed column matched (TCS, say, when only CESS is listed) stops the document with
an error that names the heading, so the column can be added.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from invoice2data.exceptions import TemplateSyntaxError

_CURRENCY_WORD = r"(?:\u20b9|Rs\.?|INR|\$|\u20ac|\u00a3|USD|EUR|GBP)"
_CURRENCY = re.compile(_CURRENCY_WORD)
_DIGITS = r"\d(?:[\d.,']*\d)?"                    # 1,23,456.78 / 1.234,56 / 1'234.50
# a number: an optional sign (leading, or trailing as in "135.00-"), currency, brackets and %
_NUMBER = re.compile(rf"^\(?[-\u2013\u2212]?{_CURRENCY_WORD}?\s?[-\u2013\u2212]?{_DIGITS}\s?%?\)?-?$")
# an empty value printed as a dash or "NA"
_PLACEHOLDER = re.compile(r"^(?:-|\u2013|\u2014|NA|N/A|n/a|Nil|NIL|nil)$")
# a quantity printed with its unit: "2 Nos", "1 PCS", "3 kg"
_QTY_UNIT = re.compile(rf"^({_DIGITS})\s+(?:nos?|pcs?|pc|kgs?|g|gms?|ltrs?|l|ml|mtrs?|m|box(?:es)?|sets?|"
                       r"pairs?|units?|each|ea|doz(?:en)?|sqft|qty)\.?$", re.I)
_SERIAL = re.compile(r"^(\d{1,3})\s+(\S.*)$")
_NUMERIC_TYPES = ("int", "float")


@dataclass
class Cell:
    start: int
    end: int
    text: str
    numeric: bool
    raw: str = ""                                      # as printed (a quantity keeps its unit here)


@dataclass
class Column:
    field: str
    start: int
    end: int
    numeric: bool


class TableLayoutError(ValueError):
    """The table settings do not fit how the document prints its rows."""


def clean_number(text: str, rate: bool = False) -> str:
    """'-\u20b91,270.34' -> '-1,270.34', '(135.00)' -> '-135.00' (brackets mean minus, except for a rate:
    '(18.0)' -> '18.0'), '135.00-' -> '-135.00', '9%' -> '9', '-' or 'NA' -> ''."""
    value = _CURRENCY.sub("", text).replace("\u2013", "-").replace("\u2212", "-").replace(" ", "")
    if _PLACEHOLDER.match(text.strip()):
        return ""
    negative = False
    if value.startswith("(") and value.endswith(")"):
        value, negative = value[1:-1], not rate
    if value.endswith("-"):
        value, negative = value[:-1], True
    if value.startswith("-"):
        value, negative = value[1:], True
    value = value.strip("%")
    return ("-" if negative else "") + value


def cells(line: str) -> list[Cell]:
    """Numbers are cells of their own; neighbouring words (one space apart) form a text cell,
    numbers included ("Pack of 2", "41 g"). A currency sign printed apart from its number
    ("\u20b9 845.00", "845.00 \u20b9") is joined to it, a dash or NA is an empty value, and a quantity
    with its unit ("2 Nos") is a number."""
    words = [(m.start(), m.end(), m.group()) for m in re.finditer(r"\S+", line)]
    merged = []
    i = 0
    while i < len(words):
        start, end, word = words[i]
        nxt = words[i + 1] if i + 1 < len(words) else None
        if nxt and nxt[0] - end <= 1 and (
                (_CURRENCY.fullmatch(word) and _NUMBER.match(nxt[2]))
                or (_NUMBER.match(word) and _CURRENCY.fullmatch(nxt[2]))):
            merged.append((start, nxt[1], word + nxt[2]))
            i += 2
            continue
        merged.append((start, end, word))
        i += 1
    out: list[Cell] = []
    for start, end, word in merged:
        numeric = bool(_NUMBER.match(word) or _PLACEHOLDER.match(word))
        previous = out[-1] if out else None
        # one space apart, a number and a word belong together: values of different columns are
        # printed further apart
        joins = previous is not None and start - previous.end <= 1 and not (numeric and previous.numeric)
        if joins:
            previous.end, previous.numeric = end, False
            previous.text = previous.raw = previous.text + " " + word
        else:
            out.append(Cell(start, end, word, numeric, word))
    for cell in out:
        unit = _QTY_UNIT.match(cell.text) if not cell.numeric else None
        if unit:
            cell.text, cell.numeric = unit.group(1), True
    return out


class UnknownColumnError(ValueError):
    """A value is printed under a heading the template does not list."""


def unlisted_headings(header: list[str], columns: list[Column]) -> list[Column]:
    """Heading words in the header that belong to no listed column: words that do not overlap a
    listed heading's span on any header line ("Amount", "Value" under a heading are part of it).
    A value nearer to one of these than to its own column is under an unknown column."""
    unknown = []
    for line in header:
        for match in re.finditer(r"\S+", line):
            word = match.group()
            if not re.search(r"[A-Za-z]{2,}", word):
                continue                               # symbols and units: currency, %, /
            start, end = match.start(), match.end()
            if any(start < c.end + 1 and c.start - 1 < end for c in columns):
                continue
            unknown.append(Column(word, start, end, True))
    return unknown


def find_columns(header: list[str], columns: dict, types: dict) -> list[Column]:
    """Columns whose heading is printed in the header lines, left to right. Two columns whose
    headings are the same printed word are a template error: the values would be shared out by
    position instead of by heading."""
    found, where = [], {}
    for field, heading in columns.items():
        for number, line in enumerate(header):
            match = re.search(heading, line)
            if match:
                column = Column(field, match.start(), match.end(), types.get(field) in _NUMERIC_TYPES)
                for other in found:
                    if where[other.field] == number and column.start < other.end and other.start < column.end:
                        raise TemplateSyntaxError(
                            f"`table` columns {other.field} and {field} match the same heading "
                            f"{line[min(column.start, other.start):max(column.end, other.end)]!r}: make "
                            "each heading pattern match only its own column")
                found.append(column)
                where[field] = number
                break
    return sorted(found, key=lambda c: c.start)


def _gap(cell: Cell, column: Column) -> float:
    """Horizontal distance between a cell and a heading (0 when they overlap), plus a small part
    of the distance between their centres to break ties."""
    gap = max(0, column.start - cell.end, cell.start - column.end)
    centre = abs((cell.start + cell.end) / 2 - (column.start + column.end) / 2)
    return gap + centre / 1000


def assign(row_cells: list[Cell], columns: list[Column], max_gap: int = 12) -> list[tuple[Cell, Column]] | None:
    """Match the cells to the columns in left-to-right order with the least total distance. A
    number may go to any column, text only to a text column, and no cell to a heading more than
    max_gap characters away (a number inside a description is not a value of a far column).
    None when no matching exists."""
    n, m = len(row_cells), len(columns)
    if n == 0 or n > m:
        return None
    inf = float("inf")
    # best[i][j]: cost of placing the first i cells in the first j columns
    best = [[inf] * (m + 1) for _ in range(n + 1)]
    choice = [[False] * (m + 1) for _ in range(n + 1)]
    for j in range(m + 1):
        best[0][j] = 0.0
    for i in range(1, n + 1):
        for j in range(i, m + 1):
            skip = best[i][j - 1]
            cell, column = row_cells[i - 1], columns[j - 1]
            take = inf
            if (cell.numeric or not column.numeric) and _gap(cell, column) <= max_gap:
                take = best[i - 1][j - 1] + _gap(cell, column)
            if take <= skip:
                best[i][j], choice[i][j] = take, True
            else:
                best[i][j] = skip
    if best[n][m] == inf:
        return None
    pairs, i, j = [], n, m
    while i > 0:
        if choice[i][j]:
            pairs.append((row_cells[i - 1], columns[j - 1]))
            i -= 1
        j -= 1
    return list(reversed(pairs))


def _add(row: dict, field: str, value: str) -> None:
    value = value.strip()
    if not value:
        return
    row[field] = f"{row[field]}\n{value}" if row.get(field) else value


_OUTSIDE = object()                                    # a line of text that is not part of the table


def _row_values(line: str, columns: list[Column], name_field: str, max_gap: int = 12,
                unknown: list[Column] = ()):
    """The values of one line by column; None when its cells cannot be placed; _OUTSIDE for text
    that runs across the amount columns (a footer or policy sentence between two pages of the
    table: it belongs to no row). A number nearer to a heading the template does not list than to
    the column it would be put in is an error: the value would otherwise be stored under the wrong
    name (a TCS amount as CESS, say)."""
    row_cells = cells(line)
    numeric_columns = [c for c in columns if c.numeric]
    first_numeric = min(c.start for c in numeric_columns) if numeric_columns else None
    if first_numeric is not None and all(not c.numeric for c in row_cells) \
            and any(c.end > first_numeric + 2 for c in row_cells):
        return _OUTSIDE
    # a serial number printed one space before the description ("1 Pigeon ...")
    if row_cells and columns and columns[0].numeric and not row_cells[0].numeric:
        serial = _SERIAL.match(row_cells[0].text)
        if serial and _gap(Cell(row_cells[0].start, row_cells[0].start + len(serial.group(1)), "", True),
                           columns[0]) <= max_gap:
            first = row_cells[0]
            number_end = first.start + len(serial.group(1))
            row_cells[0:1] = [Cell(first.start, number_end, serial.group(1), True, serial.group(1)),
                              Cell(number_end + 1, first.end, serial.group(2), False, serial.group(2))]
    zone = min(c.start for c in columns) - 2 if columns else 0
    left = [c for c in row_cells if c.end <= zone]
    placed = [c for c in row_cells if c.end > zone]
    values: dict[str, str] = {}
    for cell in left:                                  # text left of every heading: description
        _add(values, name_field, cell.raw or cell.text)
    pairs = assign(placed, columns, max_gap) if placed else []
    if pairs is None:
        return None
    for cell, column in pairs:
        if column.numeric and cell.numeric:
            closer = [u for u in unknown if _gap(cell, u) < _gap(cell, column)]
            if closer:
                raise UnknownColumnError(
                    f"the value {cell.text} is printed under the heading {closer[0].field!r}, which the "
                    f"table's columns do not list (it would have been read as {column.field}): add that "
                    "column to columns, with its own field")
        if column.numeric:
            rate = "rate" in column.field or "percent" in column.field
            _add(values, column.field, clean_number(cell.text, rate) if cell.numeric else cell.text)
        else:
            _add(values, column.field, cell.raw or cell.text)
    return values


def _header_at(lines: list[str], index: int, limit: int) -> list[str]:
    header = []
    for line in lines[index:]:
        if not line.strip() or len(header) >= limit:
            break
        header.append(line)
    return header


def parse_block(settings: dict, header: list[str], body: list[str]) -> list[dict]:
    types = settings.get("types") or {}
    limit = int(settings.get("header_lines", 4))
    max_gap = int(settings.get("max_gap", 12))
    name_field = "name"
    mode = settings.get("text", "after")
    extra = settings.get("extra") or []
    extra = extra if isinstance(extra, list) else [extra]
    skip = settings.get("skip")
    skip = skip if isinstance(skip, list) else [skip] if skip else []
    row_end = settings.get("row_end")
    prefix = settings.get("prefix")
    text_fields = {f for f in settings["columns"] if types.get(f) not in _NUMERIC_TYPES} | {name_field}

    def read_header(lines: list[str]):
        cols = find_columns(lines, settings["columns"], types)
        numeric = [c.field for c in cols if c.numeric]
        wanted = settings.get("row") or numeric[-1:]
        wanted = wanted if isinstance(wanted, list) else [wanted]
        return cols, unlisted_headings(lines, cols), wanted

    columns, unknown, row_fields = read_header(header)
    rows: list[dict] = []
    current: dict | None = None
    is_open = False
    pending: dict = {}

    def merge(target: dict, values: dict) -> None:
        for field, value in values.items():
            _add(target, field, value)

    index = 0
    while index < len(body):
        line = body[index]
        index += 1
        if not line.strip() or any(re.search(p, line) for p in skip):
            continue
        if re.search(settings["start"], line):
            # the header printed again (the table continues on the next page): its columns can
            # stand a little elsewhere on this page
            again = _header_at(body, index - 1, limit)
            columns, unknown, row_fields = read_header(again)
            index += len(again) - 1
            continue
        if not columns or not all(f in {c.field for c in columns} for f in row_fields):
            continue                                   # this print has no such column
        prefixed: dict = {}
        if prefix:
            # a value printed in front of the row with no heading of its own ("SAC: 998599"): read
            # by the pattern, then blanked so the rest of the line keeps its columns
            found = re.match(prefix, line)
            if found:
                prefixed = {k: v for k, v in found.groupdict().items() if v}
                line = " " * found.end() + line[found.end():]
                if not line.strip():
                    if current is not None and is_open and mode == "after":
                        merge(current, prefixed)
                    else:
                        merge(pending, prefixed)
                    continue
        values = _row_values(line, columns, name_field, max_gap, unknown)
        if isinstance(values, dict):
            values = {**prefixed, **values}
        if values is _OUTSIDE:
            continue
        if values is None and unknown and sum(c.numeric for c in cells(line)) > len(row_fields):
            # an amounts line whose values do not fit the listed columns: one more column is printed
            raise UnknownColumnError(
                f"a row's values do not fit the listed columns ({line.strip()[:90]!r}); the header also "
                f"prints {', '.join(repr(u.field) for u in unknown[:4])}: add the missing column to columns")
        starts_row = values is not None and all(values.get(f) for f in row_fields)
        if starts_row:
            # (with row_end, text above the first row is expected: later rows get theirs the same way)
            if (mode == "after" and not row_end and not rows and any(pending.get(f) for f in text_fields)
                    and not any(values.get(f) for f in text_fields)):
                raise TableLayoutError(
                    "the description is printed above the amounts of its row (the first amounts line has "
                    "no description of its own, the text above it has): set text: before")
            row: dict = {}
            merge(row, pending)
            merge(row, values)
            pending = {}
            rows.append(row)
            current, is_open = row, True
        else:
            extra_values = None
            for pattern in extra:
                match = re.search(pattern, line)
                if match:
                    extra_values = {k: v for k, v in match.groupdict().items() if v}
                    break
            if extra_values is None:
                extra_values = values if values is not None else {name_field: line.strip()}
            target = current if (current is not None and is_open and mode == "after") else pending
            merge(target, extra_values)
        if row_end and re.search(row_end, line):
            is_open = False
    if pending and rows:
        merge(rows[-1], pending)
    return rows


def parse(template, field: str, settings: dict, content: str) -> list[dict]:
    """invoice2data parser entry point (registered as `table` by compat.install)."""
    for key in ("start", "end", "columns"):
        if key not in settings:
            raise TemplateSyntaxError(f"`table` parser: missing required `{key}`",
                                      template.get("template_name"))
    header_lines = int(settings.get("header_lines", 4))
    return _typed(template, settings, _parse_text(template, settings, content, header_lines))


def _typed(template, settings: dict, rows: list[dict]) -> list[dict]:
    types = settings.get("types") or {}
    for row in rows:
        for name in list(row):
            if name in types:
                row[name] = template.coerce_type(row[name], types[name])
    return rows


def _parse_text(template, settings: dict, content: str, header_lines: int) -> list[dict]:
    rows: list[dict] = []
    while True:
        start = re.search(settings["start"], content)
        if not start:
            break
        line_start = content.rfind("\n", 0, start.start()) + 1
        rest = content[line_start:]
        end = re.search(settings["end"], rest[start.end() - line_start:])
        if not end:
            break
        table = rest[: start.end() - line_start + end.start()]
        lines = table.split("\n")
        header = []
        for line in lines:
            if not line.strip() or len(header) >= header_lines:
                break
            header.append(line)
        body = lines[len(header):]
        rows += parse_block(settings, header, body)
        content = rest[start.end() - line_start + end.end():]
    return rows
