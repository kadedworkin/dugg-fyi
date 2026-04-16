"""URL enrichment: metadata extraction, YouTube transcripts, OG tags."""

import json
import logging
import re
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger("dugg.enrichment")

# Tracking parameters to strip during URL sanitization
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "gclsrc", "dclid", "msclkid", "twclid",
    "mc_cid", "mc_eid", "oly_enc_id", "oly_anon_id",
    "_ga", "_gl", "ref", "ref_src",
}

# Blocked URL schemes
_ALLOWED_SCHEMES = {"http", "https"}

# Default blocklist path (ships with the package)
_BLOCKLIST_PATH = Path(__file__).parent.parent.parent / "blocklist.txt"
_blocklist_cache: Optional[set] = None


def _load_blocklist() -> set:
    """Load domain blocklist from file. Cached after first load."""
    global _blocklist_cache
    if _blocklist_cache is not None:
        return _blocklist_cache
    _blocklist_cache = set()
    if _BLOCKLIST_PATH.exists():
        for line in _BLOCKLIST_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                _blocklist_cache.add(line.lower())
    return _blocklist_cache


def validate_url(url: str) -> tuple[bool, str]:
    """Validate a URL for safety. Returns (is_valid, reason)."""
    if not url:
        return False, "Empty URL"
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "Malformed URL"

    if not parsed.scheme:
        return False, "Missing URL scheme"
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return False, f"Blocked scheme: {parsed.scheme}"
    if not parsed.hostname:
        return False, "Missing hostname"

    # Check domain blocklist
    hostname = parsed.hostname.lower()
    blocklist = _load_blocklist()
    if hostname in blocklist:
        return False, f"Blocked domain: {hostname}"
    # Also check without www. prefix
    bare_host = hostname.removeprefix("www.")
    if bare_host in blocklist:
        return False, f"Blocked domain: {bare_host}"

    return True, "ok"


def sanitize_url(url: str) -> str:
    """Strip tracking parameters from a URL to normalize for dedup."""
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        cleaned = {k: v for k, v in params.items() if k.lower() not in _TRACKING_PARAMS}
        new_query = urlencode(cleaned, doseq=True) if cleaned else ""
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, ""))
    except Exception:
        return url


def detect_source_type(url: str) -> str:
    """Detect the type of resource from its URL."""
    parsed = urlparse(url)
    host = parsed.hostname or ""

    if any(h in host for h in ("youtube.com", "youtu.be")):
        return "youtube"
    if "twitter.com" in host or "x.com" in host:
        return "tweet"
    if "github.com" in host:
        return "github"
    if "reddit.com" in host:
        return "reddit"
    if any(h in host for h in ("podcasts.apple.com", "spotify.com", "overcast.fm")):
        return "podcast"
    return "article"


def extract_youtube_id(url: str) -> Optional[str]:
    """Extract YouTube video ID from various URL formats."""
    parsed = urlparse(url)
    if "youtu.be" in (parsed.hostname or ""):
        return parsed.path.lstrip("/")
    if "youtube.com" in (parsed.hostname or ""):
        qs = parse_qs(parsed.query)
        return qs.get("v", [None])[0]
    return None


async def fetch_og_metadata(url: str) -> dict:
    """Fetch Open Graph metadata and article body text from a URL."""
    result = {
        "title": "",
        "description": "",
        "thumbnail": "",
        "site_name": "",
        "article_text": "",
        "published_at": "",
        "updated_at": "",
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
            resp = await client.get(url, headers={"User-Agent": "Dugg/0.1 (metadata fetcher)"})
            resp.raise_for_status()
    except (httpx.HTTPError, httpx.TimeoutException):
        return result

    html = resp.text
    soup = BeautifulSoup(html, "html.parser")

    og_map = {
        "og:title": "title",
        "og:description": "description",
        "og:image": "thumbnail",
        "og:site_name": "site_name",
        "article:published_time": "published_at",
        "article:modified_time": "updated_at",
    }
    for meta in soup.find_all("meta"):
        prop = meta.get("property", "") or meta.get("name", "")
        if prop in og_map and meta.get("content"):
            result[og_map[prop]] = meta["content"]

    if not result["title"]:
        title_tag = soup.find("title")
        if title_tag:
            result["title"] = title_tag.get_text(strip=True)

    if not result["description"]:
        desc_meta = soup.find("meta", attrs={"name": "description"})
        if desc_meta and desc_meta.get("content"):
            result["description"] = desc_meta["content"]

    if not result["published_at"]:
        result["published_at"] = _extract_published_at(soup)
    if not result["updated_at"]:
        mod_meta = soup.find("meta", attrs={"name": "last-modified"})
        if mod_meta and mod_meta.get("content"):
            result["updated_at"] = mod_meta["content"]

    # Extract article body text using readability-lxml
    result["article_text"] = extract_article_text(html)

    return result


def _extract_published_at(soup: BeautifulSoup) -> str:
    """Probe common publication-date locations in an HTML document."""
    for name in ("pubdate", "publishdate", "date", "dc.date", "dc.date.issued", "article.published"):
        m = soup.find("meta", attrs={"name": name})
        if m and m.get("content"):
            return m["content"]
    for itemprop in ("datePublished", "dateCreated"):
        el = soup.find(attrs={"itemprop": itemprop})
        if el:
            val = el.get("content") or el.get("datetime") or el.get_text(strip=True)
            if val:
                return val
    time_el = soup.find("time", attrs={"datetime": True})
    if time_el and time_el.get("datetime"):
        return time_el["datetime"]
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if isinstance(item, dict):
                val = item.get("datePublished") or item.get("dateCreated")
                if val:
                    return val
    return ""


def extract_article_text(html: str) -> str:
    """Extract clean article body text using readability-lxml.

    Returns plain text content suitable for indexing. Falls back gracefully
    if the library is not installed or extraction fails.
    """
    if not html or len(html) < 100:
        return ""
    try:
        from readability import Document
        doc = Document(html)
        summary_html = doc.summary()
        # Strip HTML tags from the readable content
        clean_soup = BeautifulSoup(summary_html, "html.parser")
        text = clean_soup.get_text(separator=" ", strip=True)
        # Normalize whitespace
        text = re.sub(r"\s+", " ", text).strip()
        return text
    except Exception:
        return ""


async def fetch_youtube_metadata(video_id: str) -> dict:
    """Fetch YouTube video metadata via oEmbed."""
    result = {"title": "", "description": "", "thumbnail": "", "author": "", "author_url": ""}
    try:
        oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(oembed_url)
            resp.raise_for_status()
            data = resp.json()
            result["title"] = data.get("title", "")
            result["author"] = data.get("author_name", "")
            result["author_url"] = data.get("author_url", "")
            result["thumbnail"] = f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
    except (httpx.HTTPError, httpx.TimeoutException, json.JSONDecodeError):
        pass
    return result


async def fetch_youtube_transcript(video_id: str) -> str:
    """Fetch YouTube transcript using yt-dlp subtitle extraction."""
    import asyncio
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        output_path = Path(tmpdir) / "subs"
        cmd = [
            "yt-dlp",
            "--skip-download",
            "--write-auto-sub",
            "--sub-lang", "en",
            "--sub-format", "vtt",
            "-o", str(output_path),
            f"https://www.youtube.com/watch?v={video_id}",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=60)
        except (asyncio.TimeoutError, FileNotFoundError):
            return ""

        # Find the generated subtitle file
        vtt_files = list(Path(tmpdir).glob("*.vtt"))
        if not vtt_files:
            return ""

        raw = vtt_files[0].read_text(encoding="utf-8", errors="replace")
        return _clean_vtt(raw)


def _clean_vtt(vtt_text: str) -> str:
    """Strip VTT formatting to plain text."""
    lines = []
    seen = set()
    for line in vtt_text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("WEBVTT") or line.startswith("Kind:") or line.startswith("Language:"):
            continue
        if re.match(r"^\d{2}:\d{2}", line):
            continue
        if "-->" in line:
            continue
        # Strip HTML tags
        clean = re.sub(r"<[^>]+>", "", line)
        clean = clean.strip()
        if clean and clean not in seen:
            seen.add(clean)
            lines.append(clean)
    return " ".join(lines)


async def fetch_youtube_description(video_id: str) -> str:
    """Fetch YouTube video description using yt-dlp."""
    import asyncio

    cmd = [
        "yt-dlp",
        "--skip-download",
        "--print", "description",
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        return stdout.decode("utf-8", errors="replace").strip()
    except (asyncio.TimeoutError, FileNotFoundError):
        return ""


async def enrich_url(url: str) -> dict:
    """Full enrichment pipeline for a URL. Returns structured metadata.

    Validates URL scheme and checks against domain blocklist before enriching.
    Strips tracking parameters for normalization.
    """
    # Validate URL
    is_valid, reason = validate_url(url)
    if not is_valid:
        logger.warning(f"URL validation failed for {url}: {reason}")
        return {
            "source_type": "unknown",
            "title": "",
            "description": "",
            "thumbnail": "",
            "transcript": "",
            "raw_metadata": {"_validation_error": reason},
        }

    # Sanitize tracking params
    url = sanitize_url(url)

    source_type = detect_source_type(url)
    result = {
        "source_type": source_type,
        "title": "",
        "description": "",
        "thumbnail": "",
        "transcript": "",
        "raw_metadata": {},
    }

    if source_type == "youtube":
        video_id = extract_youtube_id(url)
        if video_id:
            meta = await fetch_youtube_metadata(video_id)
            result["title"] = meta.get("title", "")
            result["thumbnail"] = meta.get("thumbnail", "")
            result["raw_metadata"] = meta

            desc = await fetch_youtube_description(video_id)
            result["description"] = desc

            transcript = await fetch_youtube_transcript(video_id)
            result["transcript"] = transcript
    else:
        og = await fetch_og_metadata(url)
        result["title"] = og.get("title", "")
        result["description"] = og.get("description", "")
        result["thumbnail"] = og.get("thumbnail", "")
        result["raw_metadata"] = og
        # Use extracted article text as transcript for articles
        article_text = og.get("article_text", "")
        if article_text:
            result["transcript"] = article_text

    return result
