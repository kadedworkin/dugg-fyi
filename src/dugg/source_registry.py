"""Per-source presentation hints for typed clients.

Ingestion stamps a `source_type` on every resource (youtube, article,
tweet, github, reddit, podcast, skill, note, rss, email, document, paste).
Clients like the iOS app and the web feed need consistent badge labels,
colors, and primary-action behavior for each of those types. This module
is the single source of truth; the /api/* serializer reads it and emits
a `source_hints` blob per resource.

To add a new source: add an entry to REGISTRY. Ingestion/detection lives
in enrichment.detect_source_type — that decides which key matches here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .enrichment import extract_youtube_id


@dataclass(frozen=True)
class Badge:
    label: str
    color: str  # hex like "#DC2626"


@dataclass(frozen=True)
class DeepLink:
    scheme: str  # e.g. "youtube" — must be whitelisted in iOS LSApplicationQueriesSchemes
    template: str  # {native_id} placeholder
    extract: Callable[[str], Optional[str]]  # url -> native id or None


@dataclass(frozen=True)
class SourceSpec:
    badge: Badge
    deep_link: Optional[DeepLink] = None
    primary_label: Optional[str] = None  # override default "Open in Safari"


REGISTRY: dict[str, SourceSpec] = {
    "youtube": SourceSpec(
        badge=Badge("YouTube", "#DC2626"),
        deep_link=DeepLink(
            scheme="youtube",
            template="youtube://www.youtube.com/watch?v={native_id}",
            extract=extract_youtube_id,
        ),
        primary_label="Open in YouTube",
    ),
    "article":  SourceSpec(badge=Badge("Article",  "#2563EB")),
    "tweet":    SourceSpec(badge=Badge("Tweet",    "#1DA1F2")),
    "github":   SourceSpec(badge=Badge("GitHub",   "#24292E")),
    "reddit":   SourceSpec(badge=Badge("Reddit",   "#FF4500")),
    "podcast":  SourceSpec(badge=Badge("Podcast",  "#9333EA")),
    "skill":    SourceSpec(badge=Badge("Skill",    "#14B8A6")),
    "note":     SourceSpec(badge=Badge("Note",     "#6B7280")),
    "rss":      SourceSpec(badge=Badge("RSS",      "#F59E0B")),
    "email":    SourceSpec(badge=Badge("Email",    "#059669")),
    "document": SourceSpec(badge=Badge("Document", "#0891B2")),
    "paste":    SourceSpec(badge=Badge("Paste",    "#6B7280")),
}


def hints_for(source_type: str, url: str) -> Optional[dict]:
    """Return the source_hints blob for a resource, or None if unregistered.

    The blob shape is stable API contract for iOS and web clients:
        {
          "badge":   {"label": str, "color": "#RRGGBB"},
          "primary": {
            "label":     str,
            "scheme":    str | None,     # only set if deep-linkable
            "deep_link": str | None,     # fully resolved, no placeholders
          }
        }
    """
    spec = REGISTRY.get(source_type)
    if spec is None:
        return None

    primary: dict = {"label": spec.primary_label or "Open in Safari"}
    if spec.deep_link is not None:
        native_id = spec.deep_link.extract(url)
        if native_id:
            primary["scheme"] = spec.deep_link.scheme
            primary["deep_link"] = spec.deep_link.template.format(native_id=native_id)

    return {
        "badge": {"label": spec.badge.label, "color": spec.badge.color},
        "primary": primary,
    }
