"""Prompt templates: sandboxed Jinja2 rendering, typed variables, cache-friendliness linting."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import jsonschema
from jinja2 import StrictUndefined
from jinja2.exceptions import TemplateError
from jinja2.sandbox import ImmutableSandboxedEnvironment

from lumen.common.errors import InvalidInput

# Sandboxed + immutable: a template can't call arbitrary methods or mutate inputs (templates are
# authored by many people and rendered in production services). StrictUndefined: a missing
# variable is an error, never a silently empty string in a production prompt.
_ENV = ImmutableSandboxedEnvironment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True)
_VAR = re.compile(r"\{\{|\{%")
CACHE_LINT_STATIC_CHARS = 200


def content_hash(messages: list[dict[str, str]], variables_schema: dict[str, Any], model_config: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps({"m": messages, "s": variables_schema, "c": model_config},
                                     sort_keys=True).encode()).hexdigest()


def validate_template(messages: list[dict[str, str]]) -> list[str]:
    """Compile every message (syntax errors are rejected) and return lint warnings."""
    if not messages:
        raise InvalidInput("a prompt needs at least one message")
    for i, m in enumerate(messages):
        if m.get("role") not in ("system", "user", "assistant", "developer"):
            raise InvalidInput(f"message {i}: invalid role {m.get('role')!r}")
        try:
            _ENV.parse(m.get("content", ""))
        except TemplateError as exc:
            raise InvalidInput(f"message {i}: template error: {exc}") from exc
    return lint_cache_friendliness(messages)


def lint_cache_friendliness(messages: list[dict[str, str]]) -> list[str]:
    """Provider prompt caches match on an exact *prefix*. A variable early in the prompt
    followed by lots of static text means that static text can never be cached; warn so the
    author moves dynamic content to the end."""
    joined = "".join(m.get("content", "") for m in messages)
    match = _VAR.search(joined)
    if not match:
        return []
    tail = _VAR.sub("", joined[match.start():])
    static_after = len(re.sub(r"\{\{.*?\}\}|\{%.*?%\}", "", joined[match.start():], flags=re.S))
    if static_after > CACHE_LINT_STATIC_CHARS and len(tail) > CACHE_LINT_STATIC_CHARS:
        return [f"cache-unfriendly: {static_after} chars of static text follow the first variable; move "
                "variables to the end of the prompt so the static prefix can be served from the provider cache"]
    return []


def render(messages: list[dict[str, str]], variables_schema: dict[str, Any], variables: dict[str, Any]
           ) -> list[dict[str, str]]:
    if variables_schema:
        try:
            jsonschema.validate(variables, variables_schema)
        except jsonschema.ValidationError as exc:
            raise InvalidInput(f"variables do not match schema: {exc.message}") from exc
    try:
        return [{"role": m["role"], "content": _ENV.from_string(m["content"]).render(**variables)} for m in messages]
    except TemplateError as exc:
        raise InvalidInput(f"render failed: {exc}") from exc
