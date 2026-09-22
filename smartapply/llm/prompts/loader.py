"""Utilities for loading versioned LLM prompt templates."""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files
from typing import Any

from jinja2 import Environment, StrictUndefined

_TEMPLATE_ROOT = "templates"


@lru_cache(maxsize=1)
def _environment() -> Environment:
    return Environment(
        autoescape=False,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )


def load_prompt(template_name: str) -> str:
    """Read the current template; edits must affect the next request."""
    template_path = files("smartapply.llm.prompts").joinpath(
        _TEMPLATE_ROOT,
        template_name,
    )
    return template_path.read_text(encoding="utf-8")


def render_prompt(template_name: str, **context: Any) -> str:
    """Render a prompt template with strict variable checking."""
    template = _environment().from_string(load_prompt(template_name))
    return template.render(**context)
