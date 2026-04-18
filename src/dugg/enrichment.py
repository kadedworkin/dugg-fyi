"""URL enrichment: metadata extraction, YouTube transcripts, OG tags."""

import json
import logging
import re
import shutil
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger("dugg.enrichment")


def _yt_dlp_path() -> str:
    """Resolve yt-dlp binary path, preferring the current venv's bin/."""
    venv_bin = Path(sys.executable).parent / "yt-dlp"
    if venv_bin.is_file():
        return str(venv_bin)
    system = shutil.which("yt-dlp")
    if system:
        return system
    raise FileNotFoundError("yt-dlp not found in venv or on PATH")


def _yt_dlp_env() -> dict:
    """Build env for yt-dlp subprocesses with deno on PATH."""
    import os

    env = os.environ.copy()
    deno_bin = Path.home() / ".deno" / "bin"
    if deno_bin.is_dir():
        env["PATH"] = str(deno_bin) + os.pathsep + env.get("PATH", "")
    return env

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


# ---------------------------------------------------------------------------
# Canonical URL extractors for pasted content (email forwards, etc.)
#
# Each extractor is a (compiled_regex, group_index) tuple.  The regex runs
# against the transcript body; the first match wins.  Add new platforms by
# appending to the list — same extensibility model as the blocklist.
# ---------------------------------------------------------------------------
_CANONICAL_URL_EXTRACTORS: list[tuple[re.Pattern, int]] = [
    # Substack — open.substack.com/pub/{slug}/p/{post-slug}
    (re.compile(r"https?://open\.substack\.com/pub/[a-z0-9_-]+/p/[a-z0-9_-]+"), 0),
    # Substack — www.{publication}.com/p/{post-slug} (custom domain posts)
    (re.compile(r"https?://(?:www\.)?[a-z0-9-]+\.(?:substack\.com|com)/p/[a-z0-9_-]+"), 0),
    # Beehiiv — links in email bodies
    (re.compile(r"https?://(?:www\.)?[a-z0-9-]+\.beehiiv\.com/p/[a-z0-9_-]+"), 0),
    # Ghost — /{slug}/ pattern on known Ghost hosts
    (re.compile(r"https?://(?:www\.)?[a-z0-9-]+\.ghost\.io/[a-z0-9_-]+/?"), 0),
]


def extract_canonical_url(body: str) -> Optional[str]:
    """Scan pasted body for a canonical article URL.

    Returns the first match from _CANONICAL_URL_EXTRACTORS, cleaned of
    tracking parameters, or None if nothing matches.
    """
    for pattern, group in _CANONICAL_URL_EXTRACTORS:
        m = pattern.search(body)
        if m:
            url = m.group(group)
            return sanitize_url(url)
    return None


def fetch_published_at(url: str, body: str = "") -> str:
    """Quick sync fetch of a canonical URL's publication date.

    Checks article:published_time OG tag, JSON-LD datePublished, and
    other common date locations.  If the canonical URL is blocked (403,
    common with open.substack.com on hosting providers), resolves to the
    publication's custom domain via the redirect chain or body scan.

    Returns ISO date string or empty string on failure.
    """
    _headers = {"User-Agent": "Mozilla/5.0 (compatible; Dugg/0.1)"}

    def _try_fetch(target: str) -> str:
        try:
            resp = httpx.get(target, headers=_headers, follow_redirects=True, timeout=10.0)
            if resp.status_code != 200:
                return ""
        except (httpx.HTTPError, httpx.TimeoutException):
            return ""
        soup = BeautifulSoup(resp.text, "html.parser")
        meta = soup.find("meta", attrs={"property": "article:published_time"})
        if meta and meta.get("content"):
            return meta["content"]
        return _extract_published_at(soup)

    result = _try_fetch(url)
    if result:
        return result

    # open.substack.com often 403s from servers due to Cloudflare bot
    # detection. Try to resolve the publication's custom domain from the
    # email body or by following the redirect chain with subprocess curl.
    parsed = urlparse(url)
    if parsed.hostname and "substack.com" in parsed.hostname:
        parts = parsed.path.strip("/").split("/")
        post_slug = parts[3] if len(parts) >= 4 and parts[0] == "pub" else parts[-1]

        # Try to find the custom domain URL in the body
        if body:
            # Look for {domain}/p/{same-post-slug} in the body
            import re as _re
            pattern = _re.compile(
                r"https?://(?:www\.)?([a-z0-9-]+\.[a-z]{2,})/p/" + _re.escape(post_slug)
            )
            m = pattern.search(body)
            if m:
                alt = f"https://www.{m.group(1)}/p/{post_slug}"
                result = _try_fetch(alt)
                if result:
                    return result

        # Last resort: use curl to follow the redirect (bypasses Cloudflare)
        import subprocess
        try:
            proc = subprocess.run(
                ["curl", "-sI", "-L", "-o", "/dev/null", "-w", "%{url_effective}", url],
                capture_output=True, text=True, timeout=10,
            )
            effective = proc.stdout.strip()
            if effective and effective != url and "substack.com" not in effective:
                return _try_fetch(effective)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
    return ""


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
        "author": "",
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
        "article:author": "author",
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

    # Author fallbacks: meta[name=author], dc.creator, LD+JSON
    if not result["author"]:
        for attr_name in ("author", "dc.creator", "article.author"):
            author_meta = soup.find("meta", attrs={"name": attr_name})
            if author_meta and author_meta.get("content"):
                result["author"] = author_meta["content"]
                break
    if not result["author"]:
        # Try LD+JSON for author
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                data = json.loads(script.string or "")
            except (json.JSONDecodeError, TypeError):
                continue
            candidates = data if isinstance(data, list) else [data]
            for item in candidates:
                if not isinstance(item, dict):
                    continue
                author = item.get("author")
                if isinstance(author, dict):
                    author = author.get("name", "")
                elif isinstance(author, list) and author:
                    author = author[0].get("name", "") if isinstance(author[0], dict) else str(author[0])
                if author:
                    result["author"] = str(author)
                    break
            if result["author"]:
                break

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

    try:
        ytdlp = _yt_dlp_path()
    except FileNotFoundError:
        logger.warning("yt-dlp not found — skipping transcript for %s", video_id)
        return ""

    with tempfile.TemporaryDirectory() as tmpdir:
        output_path = Path(tmpdir) / "subs"
        cmd = [
            ytdlp,
            "--skip-download",
            "--write-auto-sub",
            "--sub-lang", "en",
            "--sub-format", "vtt",
            "--remote-components", "ejs:github",
            "-o", str(output_path),
            f"https://www.youtube.com/watch?v={video_id}",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_yt_dlp_env(),
            )
            await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            logger.warning("yt-dlp transcript timed out for %s", video_id)
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


async def _fetch_youtube_page_data(video_id: str) -> dict:
    """Fetch YouTube description and publish date by parsing ytInitialData from the watch page."""
    result = {"description": "", "published_at": ""}
    url = f"https://www.youtube.com/watch?v={video_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            r = await client.get(url, headers=headers)
        m = re.search(r"var ytInitialData = ({.*?});</script>", r.text)
        if not m:
            return result
        data = json.loads(m.group(1))
        contents = (
            data.get("contents", {})
            .get("twoColumnWatchNextResults", {})
            .get("results", {})
            .get("results", {})
            .get("contents", [])
        )
        for item in contents:
            # Description from videoSecondaryInfoRenderer
            desc = (
                item.get("videoSecondaryInfoRenderer", {})
                .get("attributedDescription", {})
                .get("content", "")
            )
            if desc:
                result["description"] = desc.strip()
            # Publish date from videoPrimaryInfoRenderer
            date_text = (
                item.get("videoPrimaryInfoRenderer", {})
                .get("dateText", {})
                .get("simpleText", "")
            )
            if date_text:
                result["published_at"] = _parse_youtube_date(date_text)

        # Also try microformat for a more reliable ISO date
        microformat = (
            data.get("microformat", {})
            .get("playerMicroformatRenderer", {})
        )
        if microformat.get("publishDate") and not result["published_at"]:
            result["published_at"] = microformat["publishDate"]
        elif microformat.get("uploadDate") and not result["published_at"]:
            result["published_at"] = microformat["uploadDate"]

    except Exception as exc:
        logger.debug("HTTP page data fetch failed for %s: %s", video_id, exc)
    return result


def _parse_youtube_date(text: str) -> str:
    """Parse YouTube date strings like 'Apr 3, 2026' or 'Premiered Apr 3, 2026' into ISO format."""
    import re as _re
    # Strip common prefixes
    text = _re.sub(r"^(?:Premiered|Streamed live|Streamed)\s+", "", text.strip())
    from datetime import datetime as _dt
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return _dt.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


async def fetch_youtube_description(video_id: str) -> str:
    """Fetch YouTube video description. Tries HTTP scrape first, yt-dlp as fallback."""
    page_data = await _fetch_youtube_page_data(video_id)
    if page_data["description"]:
        return page_data["description"]

    # Fallback to yt-dlp
    import asyncio

    try:
        ytdlp = _yt_dlp_path()
    except FileNotFoundError:
        logger.warning("yt-dlp not found — skipping description for %s", video_id)
        return ""

    cmd = [
        ytdlp,
        "--skip-download",
        "--print", "description",
        "--remote-components", "ejs:github",
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_yt_dlp_env(),
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        return stdout.decode("utf-8", errors="replace").strip()
    except asyncio.TimeoutError:
        logger.warning("yt-dlp description timed out for %s", video_id)
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

            # Fetch description + publish date from page scrape
            page_data = await _fetch_youtube_page_data(video_id)
            result["description"] = page_data["description"]
            if page_data["published_at"]:
                result["raw_metadata"]["published_at"] = page_data["published_at"]

            # If page scrape missed description or date, try yt-dlp fallback
            need_desc = not result["description"]
            need_date = "published_at" not in result["raw_metadata"]
            if need_desc or need_date:
                import asyncio as _aio
                try:
                    ytdlp = _yt_dlp_path()
                    fields = []
                    if need_desc:
                        fields.append("%(description)s")
                    if need_date:
                        fields.append("%(upload_date)s")
                    separator = "|||DUGG_SEP|||"
                    print_fmt = separator.join(fields)
                    cmd = [ytdlp, "--skip-download", "--print", print_fmt,
                           "--remote-components", "ejs:github",
                           f"https://www.youtube.com/watch?v={video_id}"]
                    proc = await _aio.create_subprocess_exec(
                        *cmd, stdout=_aio.subprocess.PIPE, stderr=_aio.subprocess.PIPE, env=_yt_dlp_env())
                    stdout, _ = await _aio.wait_for(proc.communicate(), timeout=30)
                    parts = stdout.decode("utf-8", errors="replace").strip().split(separator)
                    idx = 0
                    if need_desc and idx < len(parts) and parts[idx].strip():
                        result["description"] = parts[idx].strip()
                        idx += 1
                    elif need_desc:
                        idx += 1
                    if need_date and idx < len(parts) and parts[idx].strip():
                        raw_date = parts[idx].strip()
                        # yt-dlp returns YYYYMMDD format
                        if len(raw_date) == 8 and raw_date.isdigit():
                            result["raw_metadata"]["published_at"] = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
                except (FileNotFoundError, _aio.TimeoutError):
                    pass

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
