"""Checks one parsed document: required fields, single values, line items and the
arithmetic rules from config/checks.yml plus the template's own checks.

Besides the issues, check_document() returns the evidence the parse produced: how many
arithmetic checks had their values and agreed (passed) and how many could not run (skipped).
A skipped check proves nothing, and neither does a check whose values are all zero (0 == 0), so
neither counts in a template's favour."""

from __future__ import annotations

from decimal import Decimal

from .registry import Registry, Rule, rule_from
from .textchecks import text_issues
from .rules import DOCUMENT_FUNCTIONS, FAIL, LINE_FUNCTIONS, PASS, Evaluator, Missing, evaluate_rule, round2, to_decimal

DEFAULT_REQUIRED = ["date", "amount", "invoice_number", "issuer"]


def template_rules(template) -> tuple[set[str], list[Rule], list[Rule]]:
    """(disabled default rule names, extra line rules, extra document rules) of a template."""
    checks = template.get("checks") or {}
    disabled = set(checks.get("disable") or [])
    line = [rule_from(r) for r in checks.get("line") or []]
    document = [rule_from(r) for r in checks.get("document") or []]
    return disabled, line, document


def numeric_values(row: dict) -> dict[str, Decimal]:
    out = {}
    for key, value in row.items():
        number = to_decimal(value)
        if number is not None:
            out[key] = number
    return out


def validate(fields: dict, lines: list[dict], tax_lines: list[dict], template,
             registry: Registry) -> list[tuple[str, str]]:
    """Return (level, message) pairs. ERROR = wrong or missing data, WARN = worth a look."""
    return check_document(fields, lines, tax_lines, template, registry)[0]


def check_document(fields: dict, lines: list[dict], tax_lines: list[dict], template,
                   registry: Registry) -> tuple[list[tuple[str, str]], dict[str, int]]:
    """(issues, evidence). evidence = {"checks_passed": n, "checks_skipped": n}."""
    issues: list[tuple[str, str]] = []
    outcomes = {"pass": 0, "skip": 0, "fail": 0, "trivial": 0}

    def run(rule, evaluator, allowed, prefix=""):
        outcome, message = evaluate_rule(rule, evaluator, allowed, prefix)
        outcomes[outcome] += 1
        return message

    # 1. Required fields
    required = list(template.get("required_fields") or DEFAULT_REQUIRED)
    for name in required:
        if fields.get(name) in (None, "", []):
            issues.append(("ERROR", f"required field not found: {name}"))

    # 2. Fields that must hold one value
    for name, value in fields.items():
        if isinstance(value, list) and registry.must_be_single(name):
            issues.append(("ERROR", f"{name} has several different values {value}"))

    # 2b. The document number is a number: a value without any digit is a label read by mistake
    #     ("Train" from "Train No./Name") or a placeholder such as N.A.
    number = fields.get("invoice_number")
    for value in (number if isinstance(number, list) else [number]):
        if value not in (None, "") and not any(c.isdigit() for c in str(value)):
            issues.append(("WARN", f"invoice_number {value!r} has no digit: a label or placeholder, not the "
                                   "document's number"))

    # 2c. Text fields no arithmetic can prove: registration number formats, the PAN inside the
    #     GSTIN, the GSTIN's state, labels that ran into a name or an address (textchecks.py)
    issues.extend(text_issues(fields))

    # 3. Template fields that found nothing (optional_fields: printed on some documents of
    #    this layout only, so their absence is normal)
    optional = set(template.get("optional_fields") or [])
    for name, spec in template["fields"].items():
        is_structural = isinstance(spec, dict) and spec.get("parser") in ("static", "lines", "table")
        if not is_structural and name not in fields and name not in required and name not in optional:
            issues.append(("WARN", f"template field found nothing: {name}"))

    # 4. Line items (a 'lines' field with parser: lines, or the older top-level 'lines:' block)
    lines_spec = template["fields"].get("lines")
    has_line_rules = "lines" in template or (isinstance(lines_spec, dict) and lines_spec.get("parser") in ("lines", "table"))
    if has_line_rules and not lines:
        issues.append(("ERROR", "no line items found although the template defines line rules"))

    disabled, extra_line_rules, extra_document_rules = template_rules(template)
    line_rules = [r for r in registry.line_rules if r.name not in disabled] + extra_line_rules
    tax_fields = registry.line_fields_with_role("tax")

    # 5. Line rules
    for i, line in enumerate(lines, start=1):
        values = numeric_values(line)
        present_taxes = [values[t] for t in tax_fields if t in values]

        def taxes(present=present_taxes):
            if not present:
                raise Missing("taxes")
            return sum(present, Decimal(0))

        names = dict(values, n_taxes=Decimal(max(len(present_taxes), 1)))
        evaluator = Evaluator(names, {"taxes": taxes})
        for rule in line_rules:
            message = run(rule, evaluator, LINE_FUNCTIONS, prefix=f"line {i}: ")
            if message:
                issues.append(("ERROR", message))

    # 6. Document rules (generated column sums + defaults + template rules)
    doc_values = numeric_values(fields)
    line_values = [numeric_values(line) for line in lines]
    tax_line_values = [numeric_values(t) for t in tax_lines]

    def column_sum(rows, name):
        if not rows or any(name not in r for r in rows):
            raise Missing(name)
        return sum((r[name] for r in rows), Decimal(0))

    def role(name):
        present = [doc_values[f] for f in registry.document_fields_with_role(name) if f in doc_values]
        if not present:
            raise Missing(f"role {name}")
        return sum(present, Decimal(0))

    def tax_total():
        if "amount_tax" in doc_values:
            return doc_values["amount_tax"]
        return role("tax")

    def untaxed():
        if "amount_untaxed" in doc_values:
            return doc_values["amount_untaxed"]
        return column_sum(line_values, "price_subtotal")

    def line_taxes():
        if not line_values:
            raise Missing("line taxes")
        total = Decimal(0)
        for row in line_values:
            present = [row[t] for t in tax_fields if t in row]
            if not present:
                raise Missing("line taxes")
            total += sum(present, Decimal(0))
        return total

    def line_rate_tax():
        if not line_values or any("line_tax_percent" not in r or "price_subtotal" not in r
                                  for r in line_values):
            raise Missing("line rates")
        return sum((round2(r["price_subtotal"] * r["line_tax_percent"] / 100) for r in line_values),
                   Decimal(0))

    functions = {
        "lines": lambda name: column_sum(line_values, name),
        "taxlines": lambda name: column_sum(tax_line_values, name),
        "role": role, "tax_total": tax_total, "untaxed": untaxed,
        "line_taxes": line_taxes, "line_rate_tax": line_rate_tax,
    }
    names = dict(doc_values, n_lines=Decimal(max(len(lines), 1)),
                 n_tax_lines=Decimal(max(len(tax_lines), 1)))
    evaluator = Evaluator(names, functions)

    sum_rules = []
    for line_field, doc_field in registry.line_sums():
        # the grand total also includes adjustments such as round-off
        extra = " + default(role('adjustment'), 0)" if registry.document[doc_field].role == "total" else ""
        sum_rules.append(Rule(name=f"sum of line {line_field} vs {doc_field}",
                              rule=f"lines('{line_field}'){extra} == {doc_field}", tolerance="0.01 * n_lines"))
    document_rules = [r for r in sum_rules + registry.document_rules if r.name not in disabled]
    # total_rounding: the document rounds its grand total (to whole rupees: 1) without printing a
    # round-off line; total_rounding_mode says how: up (IRCTC: 518.15 -> 519), nearest, down. A
    # check that ends in the total then compares the rounded sum, exactly. It applies only when the
    # printed total is a multiple of the unit (an unrounded total is checked as it is).
    # A first sample can fit more than one rule (1378.60 -> 1379 is both up and nearest): the
    # template then lists them (total_rounding_mode: [up, nearest]) and a document passes with any
    # of them; evidence["rounding_fits"] says which fit, so a later document that fits only one can
    # settle the rule (pipeline.py).
    options = template.get("options") or {}
    setting = template.get("total_rounding") or options.get("total_rounding")   # options: older AI drafts
    modes = rounding_modes(template.get("total_rounding_mode") or options.get("total_rounding_mode"))
    unit = to_decimal(setting) if setting else None
    total = to_decimal(fields.get("amount")) if unit else None
    rounding_fits = None
    if unit and unit > 0 and total is not None and total % unit == 0:
        candidates = {mode: _rounded_rules(document_rules, _ROUNDING_FUNCTIONS[mode], unit) for mode in modes}
        rounding_fits = [mode for mode in modes
                         if all(evaluate_rule(r, evaluator, DOCUMENT_FUNCTIONS)[0] != FAIL
                                for r in candidates[mode] if r.name.endswith("(rounded)"))]
        document_rules = candidates[rounding_fits[0] if rounding_fits else modes[0]]
    for rule in document_rules + extra_document_rules:
        message = run(rule, evaluator, DOCUMENT_FUNCTIONS)
        if message:
            issues.append(("ERROR", message))

    # 7. Amounts that no check verified (e.g. a template that reads only number, date and total).
    #    A template for a document that prints no arithmetic at all can declare min_checks: 0.
    needed = template.get("min_checks", 1)
    if outcomes[FAIL] == 0 and outcomes[PASS] < needed:
        totals = [n for n in registry.document_fields_with_role("total") if fields.get(n) not in (None, "", [])]
        money = [n for n, v in fields.items() if registry.is_money(n) and to_decimal(v) is not None]
        if totals:
            issues.append(("WARN", f"{totals[0]} is not verified: no arithmetic check had the values it needs"))
        elif money:
            issues.append(("WARN", f"amounts are not verified ({', '.join(money[:3])}"
                                   f"{', ...' if len(money) > 3 else ''}): no arithmetic check passed"
                                   + (" (checks on all-zero values prove nothing)" if outcomes["trivial"] else "")))

    evidence = {"checks_passed": outcomes[PASS], "checks_skipped": outcomes["skip"],
                "checks_trivial": outcomes["trivial"]}
    if rounding_fits is not None and len(modes) > 1:
        evidence["rounding_fits"] = rounding_fits
    return issues, evidence


ROUNDING_MODES = ("up", "nearest", "down")
_ROUNDING_FUNCTIONS = {"up": "ceil_to", "nearest": "round_to", "down": "floor_to"}


def rounding_modes(setting) -> list[str]:
    """total_rounding_mode as a list: 'up' -> ['up'], [up, nearest] -> ['up', 'nearest'],
    nothing -> ['nearest']. Unknown words are left out (lint reports them)."""
    values = setting if isinstance(setting, list) else [setting] if setting else ["nearest"]
    modes = [str(v) for v in values if str(v) in ROUNDING_MODES]
    return list(dict.fromkeys(modes)) or ["nearest"]


def _rounded_rules(rules: list[Rule], function: str, unit) -> list[Rule]:
    out = []
    for r in rules:
        left, _, right = r.rule.rpartition("==")
        if right.strip() == "amount" and left.strip():
            r = Rule(r.name + " (rounded)", f"{function}({left.strip()}, {unit}) == amount", r.tolerance, r.unless)
        out.append(r)
    return out


def status_of(issues) -> str:
    levels = {level for level, _ in issues}
    return "FAIL" if "ERROR" in levels else "CHECK" if "WARN" in levels else "OK"