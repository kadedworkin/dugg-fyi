"""Tests for dugg.source_registry."""

from dugg.enrichment import detect_source_type
from dugg.source_registry import REGISTRY, hints_for


def test_unknown_source_returns_none():
    assert hints_for("not-a-real-type", "https://example.com") is None


def test_article_has_badge_but_no_deep_link():
    hints = hints_for("article", "https://blog.example.com/post")
    assert hints == {
        "badge": {"label": "Article", "color": "#2563EB"},
        "primary": {"label": "Open in Safari"},
    }


def test_youtube_watch_url_produces_deep_link():
    hints = hints_for("youtube", "https://www.youtube.com/watch?v=v3Fr2JR47KA")
    assert hints["badge"] == {"label": "YouTube", "color": "#DC2626"}
    assert hints["primary"] == {
        "label": "Open in YouTube",
        "scheme": "youtube",
        "deep_link": "youtube://www.youtube.com/watch?v=v3Fr2JR47KA",
    }


def test_youtube_short_url_produces_deep_link():
    hints = hints_for("youtube", "https://youtu.be/abc123")
    assert hints["primary"]["deep_link"] == "youtube://www.youtube.com/watch?v=abc123"


def test_youtube_without_id_falls_back_to_safari_label():
    # URL matches type but extractor returns nothing — no deep_link, but the
    # "Open in YouTube" label stays since the user still expects it there.
    hints = hints_for("youtube", "https://www.youtube.com/feed/subscriptions")
    assert "deep_link" not in hints["primary"]
    assert "scheme" not in hints["primary"]
    assert hints["primary"]["label"] == "Open in YouTube"


def test_website_has_badge_but_no_deep_link():
    hints = hints_for("website", "https://dugg.fyi")
    assert hints == {
        "badge": {"label": "Website", "color": "#475569"},
        "primary": {"label": "Open in Safari"},
    }


def test_bare_domain_detected_as_website():
    assert detect_source_type("https://dugg.fyi") == "website"
    assert detect_source_type("https://dugg.fyi/") == "website"
    assert detect_source_type("http://example.com") == "website"


def test_domain_with_path_detected_as_article():
    assert detect_source_type("https://blog.example.com/some-post") == "article"
    assert detect_source_type("https://example.com/a/b") == "article"


def test_known_hosts_beat_website_detection():
    # Bare URLs on known hosts should still use their host-specific type,
    # not fall through to "website".
    assert detect_source_type("https://youtube.com") == "youtube"
    assert detect_source_type("https://github.com") == "github"


def test_all_registered_types_have_non_empty_badge():
    for source_type, spec in REGISTRY.items():
        assert spec.badge.label, f"{source_type} missing badge label"
        assert spec.badge.color.startswith("#"), f"{source_type} badge color should be hex"
