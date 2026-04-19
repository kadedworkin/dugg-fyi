"""Skill (SKILL.md) parsing and validation.

A skill is an explicitly authored procedure following the Anthropic SKILL.md
convention: a YAML frontmatter block (delimited by `---`) with at least
``name`` and ``description`` fields, followed by markdown body content.

The DB layer stores the parsed frontmatter dict and the raw body separately
so the canonical SKILL.md can be reconstructed on read and so the frontmatter
is queryable without reparsing the body.
"""

import re
from typing import Tuple

import yaml


_SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")

_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)",
    re.DOTALL,
)


def parse_skill_markdown(text: str) -> Tuple[dict, str]:
    """Parse a SKILL.md string into (frontmatter_dict, body_markdown).

    The frontmatter block must be delimited by ``---`` on its own line at the
    very start of the file and end with ``---`` on its own line. Required
    frontmatter fields are ``name`` and ``description``; both must be
    non-empty strings. Optional fields (``when-to-use``, ``model``,
    ``tools``, ``tags``, plus any others) pass through as-is.

    Raises:
        ValueError: if the frontmatter block is missing, malformed YAML, or
            missing a required non-empty ``name`` / ``description``.
    """
    if not isinstance(text, str):
        raise ValueError("skill input must be a string")

    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise ValueError(
            "skill is missing a YAML frontmatter block delimited by '---' lines"
        )

    raw_frontmatter = match.group(1)
    try:
        frontmatter = yaml.safe_load(raw_frontmatter) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"skill frontmatter is not valid YAML: {exc}") from exc

    if not isinstance(frontmatter, dict):
        raise ValueError("skill frontmatter must be a YAML mapping")

    for required in ("name", "description"):
        value = frontmatter.get(required)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"skill frontmatter is missing required non-empty '{required}' field"
            )

    body = text[match.end():]
    return frontmatter, body


def validate_skill_name(name: str) -> None:
    """Validate a skill name is a lowercase dash-separated slug.

    Pattern: starts with a lowercase letter, followed by 1-63 characters of
    lowercase letters, digits, or dashes. Rejects uppercase, underscores,
    spaces, leading digits, and names shorter than 2 or longer than 64.

    Raises:
        ValueError: if ``name`` does not match the expected pattern.
    """
    if not isinstance(name, str):
        raise ValueError("skill name must be a string")
    if not _SKILL_NAME_RE.match(name):
        raise ValueError(
            f"skill name {name!r} must match ^[a-z][a-z0-9-]{{1,63}}$ "
            "(lowercase letters, digits, dashes; 2-64 chars; must start with a letter)"
        )


def render_skill_markdown(frontmatter: dict, body: str) -> str:
    """Reconstruct a SKILL.md string from a frontmatter dict and body.

    Inverse of ``parse_skill_markdown``. Used by ``dugg skill get`` to print
    the canonical form back out.
    """
    yaml_block = yaml.safe_dump(frontmatter, sort_keys=False).rstrip("\n")
    body_text = body if body.startswith("\n") else "\n" + body
    return f"---\n{yaml_block}\n---{body_text}"
