"""URL enrichment: metadata extraction, YouTube transcripts, OG tags."""

import json
import re
from typing import Optional
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup


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
    """Fetch Open Graph metadata from a URL."""
    result = {"title": "", "description": "", "thumbnail": "", "site_name": ""}
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
            resp = await client.get(url, headers={"User-Agent": "Dugg/0.1 (metadata fetcher)"})
            resp.raise_for_status()
    except (httpx.HTTPError, httpx.TimeoutException):
        return result

    soup = BeautifulSoup(resp.text, "html.parser")

    og_map = {
        "og:title": "title",
        "og:description": "description",
        "og:image": "thumbnail",
        "og:site_name": "site_name",
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

    return result


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
    """Full enrichment pipeline for a URL. Returns structured metadata."""
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

    return result
