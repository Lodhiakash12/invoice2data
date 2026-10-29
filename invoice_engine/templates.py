"""Loads templates, checks them, and orders them for matching.

Versions: templates with the same `family` are versions of one layout (e.g. when an issuer
changes its invoice design, the new template gets `version: 2` and the old one stays).
Matching tries the highest priority first, then the newest version of each family.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from invoice2data.extract.loader import read_templates

from .lint import lint_template
from .registry import Registry

TEMPLATE_SUFFIXES = (".yml", ".yaml", ".json")


@dataclass
class TemplateSet:
    templates: list = field(default_factory=list)          # usable, in matching order
    rejected: dict[str, list[str]] = field(default_factory=dict)
    warnings: dict[str, list[str]] = field(default_factory=dict)


def family_of(template) -> str:
    return str(template.get("family") or Path(template["template_name"]).stem)


def version_of(template) -> int:
    version = template.get("version", 1)
    return version if isinstance(version, int) else 1


def _read_isolated(folder: Path, problems: dict[str, list[str]]) -> list:
    """invoice2data reads a folder in one go, and one file in an unexpected shape stops it for
    every file. Then each file is read on its own: the bad ones are rejected with the reason."""
    import shutil
    import tempfile
    loaded = []
    for path in sorted(folder.rglob("*")):
        if path.suffix.lower() not in TEMPLATE_SUFFIXES:
            continue
        with tempfile.TemporaryDirectory() as single:
            shutil.copyfile(path, Path(single) / path.name)
            try:
                loaded += read_templates(single)
            except Exception as exc:
                problems[path.name] = [f"could not be read: {type(exc).__name__}: {exc}"]
    return loaded


def load_templates(folder: Path, registry: Registry) -> TemplateSet:
    result = TemplateSet()
    try:
        loaded = read_templates(str(folder))
    except Exception:
        loaded = _read_isolated(folder, result.rejected)
    names_on_disk = [p.name for p in sorted(folder.rglob("*")) if p.suffix.lower() in TEMPLATE_SUFFIXES]
    loaded_names = {t["template_name"] for t in loaded}
    for name in names_on_disk:
        if name not in loaded_names and name not in result.rejected:
            result.rejected[name] = ["could not be read (YAML syntax error or missing keywords)"]

    seen: dict[tuple[str, int], str] = {}
    for template in sorted(loaded, key=lambda t: t["template_name"]):
        name = template["template_name"]
        errors, warnings = lint_template(template, registry)
        key = (family_of(template), version_of(template))
        if key in seen:
            errors.append(f"family {key[0]!r} version {key[1]} is already defined by {seen[key]}")
        if warnings:
            result.warnings[name] = warnings
        if errors:
            result.rejected[name] = errors
            continue
        seen[key] = name
        result.templates.append(template)

    result.templates.sort(key=lambda t: (-t.get("priority", 5), family_of(t), -version_of(t),
                                         t["template_name"]))
    return result
