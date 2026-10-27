"""Document types and the templates they hold, with fast routing of a page to its candidates.

A document type is one folder under document_types/:

    document_types/invoice/
        type.yml       name, description, routing signals, Excel summary columns
        fields.yml     the fields this type adds to config/fields.yml
        checks.yml     the arithmetic checks every document of this type gets
        templates/     one YAML per layout

Adding a type (bank statements, payslips ...) = adding a folder; no engine code changes.

Finding the candidate templates of a page happens in two cheap steps before any template runs:
1. routing: the types whose signal words are on the page (all types when none are);
2. a keyword index: a template is considered only when its most specific keyword is on the page
   (templates that share text options share one prepared copy of the page).
Only the templates that pass both steps get invoice2data's full keyword test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .registry import Registry
from .templates import TemplateSet, family_of, load_templates

TYPES_FOLDER = "document_types"


@dataclass
class DocumentType:
    name: str
    folder: Path
    description: str = ""
    signals: list[str] = field(default_factory=list)
    min_signals: int = 1
    summary_columns: list[str] = field(default_factory=list)
    registry: Registry | None = None
    template_set: TemplateSet = field(default_factory=TemplateSet)

    def routes(self, lower_text: str) -> bool:
        if not self.signals:
            return False
        return sum(1 for s in self.signals if s in lower_text) >= self.min_signals


def _text_options(template) -> str:
    """The options that change the text before keywords are matched; templates with equal
    options see the same prepared text, so it is computed once per page for all of them."""
    options = template.options if hasattr(template, "options") else template.get("options", {})
    return repr([options.get(k) for k in ("remove_whitespace", "remove_accents", "lowercase", "replace")])


class KeywordIndex:
    """A page can only match a template whose longest (most specific) keyword is on it. Templates
    are grouped by their text options: each group's prepared text is computed once per page and
    its anchors are looked up in it, so the cost grows with the number of anchors, not with the
    work of preparing the text for every template."""

    def __init__(self, templates: list):
        self.groups: dict[str, dict] = {}
        for template in templates:
            group = self.groups.setdefault(_text_options(template), {"first": template, "anchors": {}, "none": []})
            keywords = [k for k in template.get("keywords") or [] if isinstance(k, str) and k]
            if keywords:
                group["anchors"].setdefault(max(keywords, key=len), []).append(template)
            else:
                group["none"].append(template)

    def possible(self, text: str) -> dict[int, str]:
        """{id(template): its prepared page text} for every template whose anchor is on the page."""
        found: dict[int, str] = {}
        for group in self.groups.values():
            prepared = group["first"].prepare_input(text)
            for template in group["none"]:
                found[id(template)] = prepared
            for anchor, templates in group["anchors"].items():
                if anchor in prepared:
                    for template in templates:
                        found[id(template)] = prepared
        return found


class Catalog:
    """All document types; the engine asks it for a page's candidate templates and for the
    registry (fields and checks) that applies to a template."""

    def __init__(self, types: list[DocumentType]):
        self.types = types
        self.templates = [t for dt in types for t in dt.template_set.templates]
        self._type_by_template = {t["template_name"]: dt for dt in types for t in dt.template_set.templates}
        self.index = KeywordIndex(self.templates)

    @classmethod
    def single(cls, templates: list, registry: Registry, name: str = "invoice") -> "Catalog":
        """One type holding the given templates (tests and callers that load templates directly)."""
        return cls([DocumentType(name=name, folder=Path("."), registry=registry,
                                 template_set=TemplateSet(templates=list(templates)))])

    # --- questions the engine asks ----------------------------------------------------------
    def type_of(self, template) -> DocumentType:
        return self._type_by_template[template["template_name"]]

    def registry_for(self, template) -> Registry:
        return self.type_of(template).registry

    def type_named(self, name: str) -> DocumentType | None:
        return next((dt for dt in self.types if dt.name == name), None)

    def route(self, text: str) -> list[DocumentType]:
        """Types whose signals are on the page; every type when none are (no page is lost)."""
        lower = text.lower()
        routed = [dt for dt in self.types if dt.routes(lower)]
        return routed or list(self.types)

    def candidates(self, text: str, errors: list[str] | None = None) -> list:
        """Templates whose keywords match this page, in matching order."""
        allowed = {dt.name for dt in self.route(text)}
        try:
            possible = self.index.possible(text)
        except Exception:
            possible = None                               # a broken text option: fall back to one by one
        found = []
        for template in self.templates:
            if self.type_of(template).name not in allowed or (possible is not None and id(template) not in possible):
                continue
            try:
                prepared = possible[id(template)] if possible is not None else template.prepare_input(text)
                if template.matches_input(prepared):
                    found.append(template)
            except Exception as exc:  # one broken template must not stop the others
                if errors is not None:
                    errors.append(f"template {template['template_name']} skipped: {type(exc).__name__}: {exc}")
        return found

    def without(self, names: set[str]) -> "Catalog":
        """The same document types without the named templates (held by the automatic checks)."""
        if not names:
            return self
        from dataclasses import replace
        types = []
        for dt in self.types:
            kept = [t for t in dt.template_set.templates if t["template_name"] not in names]
            template_set = TemplateSet(templates=kept, rejected=dt.template_set.rejected,
                                       warnings=dt.template_set.warnings)
            types.append(replace(dt, template_set=template_set))
        return Catalog(types)

    # --- load results -------------------------------------------------------------------------
    @property
    def rejected(self) -> dict[str, list[str]]:
        return {f"{dt.name}/{n}": p for dt in self.types for n, p in dt.template_set.rejected.items()}

    @property
    def warnings(self) -> dict[str, list[str]]:
        return {f"{dt.name}/{n}": p for dt in self.types for n, p in dt.template_set.warnings.items()}


def load_catalog(root: Path, config_dir: Path | None = None) -> Catalog:
    """Read every document type under root/document_types (or root itself when it is that folder)."""
    root = Path(root)
    types_dir = root if root.name == TYPES_FOLDER else root / TYPES_FOLDER
    config_dir = Path(config_dir) if config_dir else types_dir.parent / "config"
    if not types_dir.is_dir():
        raise FileNotFoundError(f"document types folder not found: {types_dir}")
    types: list[DocumentType] = []
    for folder in sorted(p for p in types_dir.iterdir() if p.is_dir() and (p / "type.yml").is_file()):
        spec = yaml.safe_load((folder / "type.yml").read_text(encoding="utf-8-sig")) or {}
        name = str(spec.get("name") or folder.name)
        dt = DocumentType(name=name, folder=folder, description=str(spec.get("description") or ""),
                          signals=[str(s).lower() for s in spec.get("signals") or []],
                          min_signals=int(spec.get("min_signals", 1)),
                          summary_columns=[str(c) for c in spec.get("summary_columns") or []],
                          registry=Registry(config_dir, folder))
        templates_dir = folder / "templates"
        dt.template_set = load_templates(templates_dir, dt.registry) if templates_dir.is_dir() else TemplateSet()
        types.append(dt)
    _check_unique(types)
    return Catalog(types)


def _check_unique(types: list[DocumentType]) -> None:
    """Template file names and layout families must be unique across types: outputs and
    versions refer to them by name."""
    seen: dict[str, str] = {}
    for dt in types:
        for template in list(dt.template_set.templates):
            for key in (f"name:{template['template_name']}", f"family:{family_of(template)}"):
                if key in seen and seen[key] != dt.name:
                    dt.template_set.templates.remove(template)
                    dt.template_set.rejected[template["template_name"]] = [
                        f"{key.split(':', 1)[1]!r} is already used in document type {seen[key]!r}"]
                    break
                seen.setdefault(key, dt.name)
