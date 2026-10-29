"""Template checking on load. A template with errors is rejected before it can run;
warnings are reported but the template is used."""

from __future__ import annotations

import difflib
import re

import regex

from .layout import lint_layout
from .registry import Registry, rule_from
from .rules import DOCUMENT_FUNCTIONS, LINE_FUNCTIONS, RuleError, parse_expression, parse_rule

KNOWN_PARSERS = {"regex", "static", "lines", "table", "region"}
REGION_KEYS = {"parser", "heading", "stop", "max_lines", "skip", "same_line", "first_line", "join", "replace"}
LINE_KEYS = ("start", "end", "first_line", "line", "last_line", "skip_line")
# ^ or $ used as line anchors (not inside [...] and not escaped)
_ANCHOR = re.compile(r"(?<![\\\[])\^|(?<!\\)\$")
_NUMERIC_DATE = re.compile(r"\\d\{?\d?,?\d?\}?[-/.]")


def _suggest(name: str, known) -> str:
    match = difflib.get_close_matches(name, list(known), n=1, cutoff=0.8)
    return f" (did you mean {match[0]!r}?)" if match else ""


def _regexes(spec) -> list[str]:
    pattern = spec.get("regex") if isinstance(spec, dict) else spec
    if pattern is None:
        return []
    return pattern if isinstance(pattern, list) else [pattern]


def _compile(rx, where: str, errors: list[str]):
    """Compile with both engines: `re` for the checks below, `regex` because templates run on it."""
    if not isinstance(rx, str):
        errors.append(f"{where}: pattern is not text: {rx!r}")
        return None
    try:
        compiled = re.compile(rx)
        regex.compile(rx)
        return compiled
    except (re.error, regex.error) as exc:
        errors.append(f"{where}: invalid regex ({exc})")
        return None


def _check_replace(pairs, where: str, errors: list[str]) -> None:
    """[pattern, replacement] pairs: the replacement may only name groups the pattern has.
    An undefined group (\\2 with one group) would otherwise stop the run on the first match."""
    if isinstance(pairs, list) and len(pairs) == 2 and all(isinstance(x, str) for x in pairs):
        pairs = [pairs]                            # one pair written flat: [pattern, replacement]
    for pair in pairs or []:
        if not (isinstance(pair, list) and len(pair) == 2 and all(isinstance(x, str) for x in pair)):
            errors.append(f"{where}: entry must be [pattern, replacement] as text: {pair!r}")
            continue
        compiled = _compile(pair[0], where, errors)
        if compiled is None:
            continue
        try:
            compiled.sub(pair[1], "")             # parses the replacement without needing a match
            _expand_check(compiled, pair[1])
        except (re.error, IndexError) as exc:
            errors.append(f"{where}: replacement {pair[1]!r} is invalid for pattern {pair[0]!r} ({exc})")


def _expand_check(compiled, replacement: str) -> None:
    """Raise when the replacement names a group the pattern does not define."""
    for match in re.finditer(r"\\(\d+)|\\g<(\w+)>", replacement):
        number, name = match.group(1), match.group(2)
        if number is not None and int(number) > compiled.groups:
            raise re.error(f"group {number} is not defined (the pattern has {compiled.groups})")
        if name is not None and not name.isdigit() and name not in compiled.groupindex:
            raise re.error(f"group {name!r} is not defined")
        if name is not None and name.isdigit() and int(name) > compiled.groups:
            raise re.error(f"group {name} is not defined (the pattern has {compiled.groups})")


_MAPPINGS = ("fields", "options", "checks", "lines")
_LISTS = ("keywords", "exclude_keywords", "continuation_keywords", "required_fields", "optional_fields")


def _shape_errors(template) -> list[str]:
    """A template written by hand or by the AI can have a section in the wrong shape (a list
    where named entries are needed). Report it instead of failing on it."""
    errors = []
    for key in _MAPPINGS:
        value = template.get(key)
        if value is not None and not isinstance(value, dict):
            errors.append(f"{key} must be a mapping of names to values (name: value), not a "
                          f"{type(value).__name__}")
    for key in _LISTS:
        value = template.get(key)
        if value is not None and not isinstance(value, list):
            errors.append(f"{key} must be a list ([a, b, ...]), not a {type(value).__name__}")
    for name, spec in (template.get("fields") or {}).items() if isinstance(template.get("fields"), dict) else ():
        if not isinstance(spec, (str, dict, list)):
            errors.append(f"field {name}: must be a pattern (text) or a mapping with parser: ..., "
                          f"not a {type(spec).__name__}")
    return errors


def lint_template(template, registry: Registry) -> tuple[list[str], list[str]]:
    shape = _shape_errors(template)
    if shape:                                   # the rest of the checks need the right shape
        return shape, []
    errors: list[str] = []
    warnings: list[str] = []
    options = template.get("options") or {}

    # keywords
    keywords = template.get("keywords")
    if not isinstance(keywords, list) or not keywords:
        errors.append("keywords must be a non-empty list")
    else:
        for k in keywords + list(template.get("exclude_keywords") or []):
            if not isinstance(k, str):
                errors.append(f"keyword {k!r} is not text (quote keywords that contain a colon)")
            elif not k.isascii():
                warnings.append(f"keyword {k!r} is not ASCII (fragile if the file is re-encoded)")

    # fields
    fields = template.get("fields") or {}
    has_numeric_dates = False
    for name, spec in fields.items():
        where = f"field {name}"
        parser = spec.get("parser") if isinstance(spec, dict) else "regex"
        if parser not in KNOWN_PARSERS:
            errors.append(f"{where}: unknown parser {parser!r}")
            continue
        if parser == "lines":
            _lint_lines(spec, f"{where} (lines)", registry, errors, warnings)
        elif parser == "table":
            _lint_table(spec, f"{where} (table)", registry, errors, warnings)
        elif parser == "region":
            if not spec.get("heading"):
                errors.append(f"{where} (region): missing 'heading' (a regex on the heading's line)")
            for key in ("heading", "stop"):
                if spec.get(key) is not None:
                    _compile(spec[key], f"{where}.{key}", errors)
            for key in ("skip", "max_lines"):
                if key in spec and (not isinstance(spec[key], int) or spec[key] < 0):
                    errors.append(f"{where}.{key} must be a whole number >= 0")
            unknown = sorted(set(spec) - REGION_KEYS)
            if unknown:
                errors.append(f"{where} (region): unknown setting(s) {', '.join(unknown)} (known: "
                              f"{', '.join(sorted(REGION_KEYS - {'parser'}))})")
            if "join" in spec and not isinstance(spec["join"], str):
                errors.append(f"{where}.join must be a text (what goes between the lines, e.g. ', ')")
            for pair in spec.get("replace") or []:
                if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
                    errors.append(f"{where}.replace: each entry must be [pattern, replacement]")
                else:
                    _compile(pair[0], f"{where}.replace", errors)
        elif parser == "regex":
            for rx in _regexes(spec):
                compiled = _compile(rx, where, errors)
                if compiled is None:
                    continue
                if compiled.groups != 1:
                    errors.append(f"{where}: regex must have exactly 1 capture group, has {compiled.groups}")
                if _ANCHOR.search(rx) and "(?m)" not in rx:
                    errors.append(f"{where}: uses ^ or $ without (?m) - they would only match at the "
                                  "start/end of the whole text (use \\A for start of text)")
                if registry.document.get(name, None) and registry.document[name].type == "date":
                    has_numeric_dates |= bool(_NUMERIC_DATE.search(rx))
        if parser in ("lines", "table"):
            continue  # a table (e.g. tax_lines): its columns were checked above
        if name not in registry.document:
            if name.startswith("amount"):
                errors.append(f"{where}: amount field is not in config/fields.yml{_suggest(name, registry.document)}"
                              " - register it with its role so the checks know what it means")
            else:
                warnings.append(f"{where}: not in config/fields.yml{_suggest(name, registry.document)}")
    if has_numeric_dates and not options.get("date_formats"):
        errors.append("numeric dates are read but options.date_formats is empty "
                      "(02-09-2026 could be read month-first)")

    # line items
    if "lines" in template:
        _lint_lines(template["lines"], "lines", registry, errors, warnings)

    # required fields must be produced by the template
    produced = set(fields) | {"issuer", "currency"}
    for name in template.get("required_fields") or []:
        if name not in produced:
            errors.append(f"required field {name!r} is not defined in fields")

    # options.replace and field-level replace lists
    _check_replace(options.get("replace"), "options.replace", errors)
    for name, spec in fields.items():
        if isinstance(spec, dict) and spec.get("replace") is not None:
            _check_replace(spec.get("replace"), f"field {name} replace", errors)

    min_checks = template.get("min_checks", 1)
    if not isinstance(min_checks, int) or isinstance(min_checks, bool) or min_checks < 0:
        errors.append(f"min_checks must be a whole number >= 0, got {min_checks!r}")

    rounding = template.get("total_rounding")
    if rounding is not None and (isinstance(rounding, bool) or not isinstance(rounding, (int, float))
                                 or not 0 < rounding <= 10):
        errors.append(f"total_rounding must be a number above 0 and at most 10 (1 = whole rupees), got {rounding!r}")
    for key in ("total_rounding", "total_rounding_mode"):
        if key in options and key not in template:
            warnings.append(f"options.{key} belongs at the top level of the template (it is read from "
                            "options too, but move it)")
    mode = template.get("total_rounding_mode")
    if mode is not None:
        values = mode if isinstance(mode, list) else [mode]
        if not values or any(v not in ("up", "nearest", "down") for v in values) or len(set(values)) != len(values):
            errors.append(f"total_rounding_mode must be up, nearest or down, or a list of them while a first "
                          f"sample fits more than one ([up, nearest]), got {mode!r}")

    optional = template.get("optional_fields")
    if optional is not None:
        if not isinstance(optional, list) or not all(isinstance(n, str) for n in optional):
            errors.append("optional_fields must be a list of field names")
        else:
            for name in optional:
                if name not in fields:
                    errors.append(f"optional_fields: {name!r} is not one of this template's fields")
                elif name in (template.get("required_fields") or []):
                    errors.append(f"optional_fields: {name!r} is also in required_fields")

    # engine directives
    follow = template.get("continuation_keywords")
    if follow is not None and (not isinstance(follow, list) or not all(isinstance(k, str) for k in follow)):
        errors.append("continuation_keywords must be a list of text")
    version = template.get("version", 1)
    if not isinstance(version, int) or version < 1:
        errors.append(f"version must be a whole number >= 1, got {version!r}")

    # layout fingerprint (optional, written by: process_invoice.py --fingerprint)
    if "layout" in template:
        errors.extend(lint_layout(template["layout"]))
    else:
        warnings.append("no layout block (used to decide a tie with a look-alike layout); build it "
                        "with: python process_invoice.py --fingerprint <this template> <sample PDFs>")

    # template checks
    checks = template.get("checks") or {}
    if not isinstance(checks, dict):
        errors.append("checks must be a mapping with disable / line / document")
    else:
        known_names = {r.name for r in registry.line_rules + registry.document_rules}
        for name in checks.get("disable") or []:
            if name not in known_names and not name.startswith("sum of line "):
                errors.append(f"checks.disable: no default check named {name!r}{_suggest(name, known_names)}")
        for level, allowed in (("line", LINE_FUNCTIONS), ("document", DOCUMENT_FUNCTIONS)):
            for entry in checks.get(level) or []:
                try:
                    rule = rule_from(entry)
                    parse_rule(rule.rule, allowed)
                    parse_expression(rule.tolerance, allowed)
                    for condition in rule.unless:
                        parse_expression(condition, allowed)
                except (RuleError, ValueError) as exc:
                    errors.append(f"checks.{level}: {exc}")
    return errors, warnings


def _lint_lines(spec, where: str, registry: Registry, errors: list[str], warnings: list[str]) -> None:
    rules = spec.get("rules") if isinstance(spec, dict) and "rules" in spec else [spec]
    for rule in rules:
        if not isinstance(rule, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        for key in ("start", "end", "line"):
            if key not in rule:
                errors.append(f"{where}: missing '{key}'")
        for key in LINE_KEYS:
            for rx in (rule.get(key) if isinstance(rule.get(key), list) else [rule.get(key)]):
                if rx is None:
                    continue
                compiled = _compile(rx, f"{where}.{key}", errors)
                if compiled is None:
                    continue
                # start/end are searched in the whole text; the others line by line
                if key in ("start", "end") and _ANCHOR.search(rx) and "(?m)" not in rx:
                    errors.append(f"{where}.{key}: uses ^ or $ without (?m)")
                for group in compiled.groupindex:
                    if group not in registry.line:
                        warnings.append(f"{where}.{key}: column {group!r} not in config/fields.yml"
                                        f"{_suggest(group, registry.line)}")


def _lint_table(spec, where: str, registry: Registry, errors: list[str], warnings: list[str]) -> None:
    """A table read by its column headings (parser: table, see table.py)."""
    for key in ("start", "end", "columns"):
        if key not in spec:
            errors.append(f"{where}: missing '{key}'")
    for key in ("start", "end", "row_end", "prefix"):
        if spec.get(key) is not None:
            _compile(spec[key], f"{where}.{key}", errors)
    for key in ("extra", "skip"):
        value = spec.get(key)
        for rx in (value if isinstance(value, list) else [value] if value is not None else []):
            compiled = _compile(rx, f"{where}.{key}", errors)
            for group in (compiled.groupindex if compiled else {}):
                if group not in registry.line:
                    warnings.append(f"{where}.{key}: column {group!r} not in config/fields.yml"
                                    f"{_suggest(group, registry.line)}")
    columns = spec.get("columns")
    types = spec.get("types") or {}
    if columns is not None and not isinstance(columns, dict):
        errors.append(f"{where}.columns must be a mapping of line field: heading pattern")
        return
    for field, heading in (columns or {}).items():
        _compile(heading, f"{where}.columns.{field}", errors)
        if field not in registry.line:
            warnings.append(f"{where}.columns: {field!r} not in config/fields.yml{_suggest(field, registry.line)}")
    numeric = [f for f in (columns or {}) if types.get(f) in ("int", "float")]
    if columns and not numeric:
        errors.append(f"{where}: no numeric column (give the amount columns a type: float)")
    rows = spec.get("row")
    for field in (rows if isinstance(rows, list) else [rows] if rows else []):
        if field not in (columns or {}):
            errors.append(f"{where}.row: {field!r} is not one of the columns")
    if spec.get("text", "after") not in ("after", "before"):
        errors.append(f"{where}.text must be after or before")
