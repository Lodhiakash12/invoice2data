"""Safe evaluation of check rules such as "untaxed() + tax_total() == amount".

Rules are parsed with Python's ast module and only a small whitelist is executed:
numbers, + - * /, unary minus, field names and the helper functions below.
Nothing else (attributes, imports, calls to other functions) is allowed, so a rule
written in a template can never run code.
"""

from __future__ import annotations

import ast
import operator
from functools import lru_cache
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")

_BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}

#: Functions available in every rule; context-specific ones are added by the caller.
COMMON_FUNCTIONS = frozenset({"default", "first", "any", "abs", "round2"})
LINE_FUNCTIONS = COMMON_FUNCTIONS | {"taxes"}
DOCUMENT_FUNCTIONS = COMMON_FUNCTIONS | {"lines", "taxlines", "role", "tax_total", "untaxed",
                                         "line_taxes", "line_rate_tax"}


class Missing(Exception):
    """A value the rule needs is not on the document: the rule is skipped."""


class RuleError(ValueError):
    """The rule itself is malformed."""


class AnyOf(list):
    """Several acceptable right-hand values (from any(...))."""


def to_decimal(value) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    return Decimal(str(value))


def round2(value: Decimal) -> Decimal:
    return value.quantize(CENT, ROUND_HALF_UP)


@lru_cache(maxsize=1024)
def parse_rule(text: str, allowed_functions: frozenset) -> tuple[ast.AST, ast.AST]:
    """Parse 'left == right'; raise RuleError when the syntax or a function is not allowed."""
    try:
        tree = ast.parse(text, mode="eval").body
    except SyntaxError as exc:
        raise RuleError(f"cannot parse rule {text!r}: {exc.msg}") from None
    if not (isinstance(tree, ast.Compare) and len(tree.ops) == 1 and isinstance(tree.ops[0], ast.Eq)):
        raise RuleError(f"rule must have the form 'left == right': {text!r}")
    _check_nodes(tree.left, allowed_functions, text)
    _check_nodes(tree.comparators[0], allowed_functions, text)
    return tree.left, tree.comparators[0]


@lru_cache(maxsize=1024)
def parse_expression(text: str, allowed_functions: frozenset) -> ast.AST:
    try:
        tree = ast.parse(text, mode="eval").body
    except SyntaxError as exc:
        raise RuleError(f"cannot parse expression {text!r}: {exc.msg}") from None
    _check_nodes(tree, allowed_functions, text)
    return tree


def _check_nodes(node: ast.AST, allowed: frozenset, text: str) -> None:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            if not isinstance(sub.func, ast.Name) or sub.func.id not in allowed:
                name = getattr(sub.func, "id", "?")
                raise RuleError(f"function {name!r} is not allowed in {text!r}")
            if sub.keywords:
                raise RuleError(f"keyword arguments are not allowed in {text!r}")
        elif not isinstance(sub, (ast.BinOp, ast.UnaryOp, ast.Name, ast.Constant, ast.Load,
                                  ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub, ast.UAdd)):
            raise RuleError(f"{type(sub).__name__} is not allowed in {text!r}")
        if isinstance(sub, ast.Constant) and not isinstance(sub.value, (int, float, str)):
            raise RuleError(f"constant {sub.value!r} is not allowed in {text!r}")


class Evaluator:
    """Evaluates parsed rules against named values and helper functions."""

    def __init__(self, names: dict, functions: dict):
        self.names = names            # field name -> Decimal (only present numeric values)
        self.functions = functions    # function name -> python callable(*evaluated args)

    def value(self, node: ast.AST):
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                return node.value
            return Decimal(str(node.value))
        if isinstance(node, ast.Name):
            if node.id in self.names:
                return self.names[node.id]
            raise Missing(node.id)
        if isinstance(node, ast.UnaryOp):
            operand = self.value(node.operand)
            return -operand if isinstance(node.op, ast.USub) else operand
        if isinstance(node, ast.BinOp):
            left, right = self.value(node.left), self.value(node.right)
            try:
                return _BINOPS[type(node.op)](left, right)
            except (ZeroDivisionError, InvalidOperation):
                raise Missing("division by zero") from None
        if isinstance(node, ast.Call):
            name = node.func.id
            if name == "default":
                try:
                    return self.value(node.args[0])
                except Missing:
                    return self.value(node.args[1])
            if name == "first":
                for arg in node.args:
                    try:
                        return self.value(arg)
                    except Missing:
                        continue
                raise Missing("first()")
            if name == "any":
                values = AnyOf()
                for arg in node.args:
                    try:
                        values.append(self.value(arg))
                    except Missing:
                        continue
                if not values:
                    raise Missing("any()")
                return values
            if name == "abs":
                return abs(self.value(node.args[0]))
            if name == "round2":
                return round2(self.value(node.args[0]))
            args = [self.value(a) for a in node.args]
            return self.functions[name](*args)
        raise RuleError(f"unsupported expression element {type(node).__name__}")


PASS, SKIP, FAIL = "pass", "skip", "fail"


def evaluate_rule(rule, evaluator: Evaluator, allowed: frozenset, prefix: str = "") -> tuple[str, str | None]:
    """Return (outcome, message). outcome is PASS (the values were there and agree), SKIP (a value
    the rule needs is not on the document, so the rule proves nothing) or FAIL (message says why)."""
    for condition in rule.unless:
        try:
            evaluator.value(parse_expression(condition, allowed))
            return SKIP, None              # an "unless" value is present
        except Missing:
            pass
    left_node, right_node = parse_rule(rule.rule, allowed)
    try:
        left = evaluator.value(left_node)
        right = evaluator.value(right_node)
        tolerance = evaluator.value(parse_expression(rule.tolerance, allowed))
    except Missing:
        return SKIP, None                  # values not on this document: rule not applicable
    targets = right if isinstance(right, AnyOf) else [right]
    if any(abs(left - t) <= tolerance for t in targets):
        return PASS, None
    if isinstance(right, AnyOf):
        shown = ", ".join(str(round2(t)) for t in targets)
        return FAIL, (f"{prefix}{rule.name}: calculated {round2(left)}, printed values {shown} "
                      f"(none within {round2(tolerance)})")
    difference = abs(left - right)
    return FAIL, (f"{prefix}{rule.name}: calculated {round2(left)}, printed {round2(right)} "
                  f"(off by {round2(difference)}, allowed {round2(tolerance)})")


def run_rule(rule, evaluator: Evaluator, allowed: frozenset, prefix: str = "") -> str | None:
    """Return an error message when the rule fails, None when it passes or is skipped."""
    return evaluate_rule(rule, evaluator, allowed, prefix)[1]