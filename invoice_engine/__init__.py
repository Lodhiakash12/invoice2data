"""Template-driven PDF document parsing engine (on top of invoice2data).

Nothing in this package knows about a specific issuer, document type or field: each document type
is a folder under document_types/ (its fields, checks and templates), shared fields live in
config/fields.yml.
"""

from . import compat, safe_regex

safe_regex.install()          # every template pattern runs with a time limit (see safe_regex.py)
compat.install()              # an empty number stays empty instead of becoming 0 (see compat.py)

from .catalog import Catalog, DocumentType, load_catalog  # noqa: E402
from .document import process_pdf  # noqa: E402
from .registry import Registry  # noqa: E402
from .templates import TemplateSet, load_templates  # noqa: E402

__all__ = ["Catalog", "DocumentType", "Registry", "TemplateSet", "load_catalog", "load_templates", "process_pdf"]
