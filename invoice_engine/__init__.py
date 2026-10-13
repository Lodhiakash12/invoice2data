"""Template-driven PDF invoice parsing engine (on top of invoice2data).

Nothing in this package knows about a specific issuer or field: layouts live in
templates/, field meanings in config/fields.yml, arithmetic rules in config/checks.yml.
"""

from .document import process_pdf
from .registry import Registry
from .templates import TemplateSet, load_templates

__all__ = ["Registry", "TemplateSet", "load_templates", "process_pdf"]
