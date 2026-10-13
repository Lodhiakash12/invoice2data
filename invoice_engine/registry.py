"""Field registry and default checks, loaded from config/fields.yml and config/checks.yml."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

TYPES = {"text", "date", "money", "rate", "count", "number", "list"}
NUMERIC_TYPES = {"money", "rate", "count", "number"}


@dataclass(frozen=True)
class FieldSpec:
    name: str
    level: str                 # "document" or "line"
    type: str
    role: str | None = None
    single: bool = False
    sums_to: str | None = None

    @property
    def numeric(self) -> bool:
        return self.type in NUMERIC_TYPES

    @property
    def must_be_single(self) -> bool:
        return self.single or self.type in ("money", "date")


@dataclass(frozen=True)
class Rule:
    name: str
    rule: str
    tolerance: str = "0.01"
    unless: tuple[str, ...] = ()


class Registry:
    def __init__(self, config_dir: Path):
        fields = _read_yaml(config_dir / "fields.yml")
        checks = _read_yaml(config_dir / "checks.yml")
        self.document = self._specs(fields.get("document") or {}, "document")
        self.line = self._specs(fields.get("line") or {}, "line")
        self.line_rules = [rule_from(r) for r in checks.get("line") or []]
        self.document_rules = [rule_from(r) for r in checks.get("document") or []]
        for spec in self.line.values():
            if spec.sums_to and spec.sums_to not in self.document:
                raise ValueError(f"fields.yml: line field {spec.name} sums_to unknown field {spec.sums_to}")

    @staticmethod
    def _specs(entries: dict, level: str) -> dict[str, FieldSpec]:
        specs = {}
        for name, attrs in entries.items():
            attrs = attrs or {}
            kind = attrs.get("type", "text")
            if kind not in TYPES:
                raise ValueError(f"fields.yml: {level}.{name} has unknown type {kind!r}")
            specs[name] = FieldSpec(name=name, level=level, type=kind, role=attrs.get("role"),
                                    single=bool(attrs.get("single", False)), sums_to=attrs.get("sums_to"))
        return specs

    # --- questions the engine asks -------------------------------------------------
    def document_fields_with_role(self, role: str) -> list[str]:
        return [s.name for s in self.document.values() if s.role == role]

    def line_fields_with_role(self, role: str) -> list[str]:
        return [s.name for s in self.line.values() if s.role == role]

    def line_sums(self) -> list[tuple[str, str]]:
        """(line field, document field) pairs whose column total must match."""
        return [(s.name, s.sums_to) for s in self.line.values() if s.sums_to]

    def is_money(self, name: str, level: str = "document") -> bool:
        spec = (self.document if level == "document" else self.line).get(name)
        return bool(spec and spec.type == "money")

    def is_numeric_line_field(self, name: str) -> bool:
        spec = self.line.get(name)
        return bool(spec and spec.numeric)

    def must_be_single(self, name: str) -> bool:
        spec = self.document.get(name)
        return spec.must_be_single if spec else name.startswith(("amount", "date"))

    def line_column_order(self) -> list[str]:
        return list(self.line)


def rule_from(entry: dict) -> Rule:
    if not isinstance(entry, dict) or "rule" not in entry:
        raise ValueError(f"a check needs at least 'rule': {entry!r}")
    return Rule(name=str(entry.get("name") or entry["rule"]), rule=str(entry["rule"]),
                tolerance=str(entry.get("tolerance", "0.01")),
                unless=tuple(str(u) for u in entry.get("unless") or ()))


def _read_yaml(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"configuration file not found: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
