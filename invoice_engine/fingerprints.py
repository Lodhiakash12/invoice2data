"""Building layout fingerprints from sample PDFs, for every template at once or for some.

Every template should carry a layout block, so that a tie between two layouts can be decided
the day it first happens, without anyone having to build the fingerprint then. This module is
the one place that builds them; it is used by

    python process_invoice.py --fingerprint samples            every template
    python process_invoice.py --fingerprint x.yml a.pdf b.pdf  one template

and is meant to be called by an automatic template-writing step right after a new template has
been written and has read its sample correctly:

    build_layouts(catalog, [sample_pdf], only={"new_template.yml"})

Samples: a document is a sample of a template only when the engine, with every template
competing, chose that template for it and the result is not FAIL (and it is not a duplicate
copy). So a fingerprint is never learned from another layout's pages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import layout as layout_fp
from .document import process_pdf


@dataclass
class LayoutResult:
    template: str
    samples: list[str] = field(default_factory=list)     # "file.pdf pages 1-2 (OK)"
    labels: int = 0
    written: bool = False


def collect_samples(catalog, pdfs: list[Path], only: set[str] | None = None) -> dict[str, list[tuple[Path, list[int], str]]]:
    """{template name: [(pdf, pages, status), ...]} for the documents each template won."""
    found: dict[str, list[tuple[Path, list[int], str]]] = {}
    for pdf in pdfs:
        result = process_pdf(Path(pdf), catalog)
        for doc in result["documents"]:
            name = doc["template"]
            if doc["status"] == "FAIL" or doc.get("copy_of") or (only and name not in only):
                continue
            found.setdefault(name, []).append((Path(pdf), list(doc["pages"]), doc["status"]))
    return found


def build_layouts(catalog, pdfs: list[Path], only: set[str] | None = None,
                  write: bool = True) -> list[LayoutResult]:
    """Build (and by default write) the layout block of every template in `only` (default: all)
    from the sample documents it wins in `pdfs`. A template without samples is left as it is."""
    names = sorted(only) if only else sorted(t["template_name"] for t in catalog.templates)
    by_name = {t["template_name"]: t for t in catalog.templates}
    samples = collect_samples(catalog, pdfs, set(names))
    results = []
    for name in names:
        template = by_name.get(name)
        outcome = LayoutResult(template=name)
        results.append(outcome)
        if template is None:
            continue
        vocabulary = layout_fp.label_vocabulary(template)
        prints = []
        for pdf, pages, status in samples.get(name, []):
            prints.append(layout_fp.fingerprint(layout_fp.page_words(pdf, pages), vocabulary))
            shown = f"{pages[0]}-{pages[-1]}" if len(pages) > 1 else str(pages[0])
            outcome.samples.append(f"{pdf.name} pages {shown} ({status})")
        if not prints:
            continue
        order = layout_fp.merge(prints)
        outcome.labels = len(order)
        if write and order:
            folder = catalog.type_of(template).folder / "templates"
            layout_fp.write_layout(folder / name, layout_fp.layout_block(order, len(prints)))
            template["layout"] = {"columns": layout_fp.COLUMNS, "sample_pages": len(prints),
                                  "order": order}           # the loaded template uses it at once
            outcome.written = True
    return results
