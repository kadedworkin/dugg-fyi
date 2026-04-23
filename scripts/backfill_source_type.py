"""Reclassify resources whose source_type was hardcoded to 'article' (or left
'unknown') but whose URL clearly belongs to a typed source like YouTube or
GitHub. Introduced after rss.py was fixed to call detect_source_type on ingest.

Usage: uv run python scripts/backfill_source_type.py [--db PATH] [--apply]
Defaults to dry-run unless --apply is passed.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from dugg.enrichment import detect_source_type, extract_youtube_id


DEFAULT_DB = Path.home() / ".dugg" / "dugg.db"


def _youtube_thumbnail(url: str) -> str:
    vid = extract_youtube_id(url)
    return f"https://img.youtube.com/vi/{vid}/maxresdefault.jpg" if vid else ""


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--apply", action="store_true", help="Commit changes (default: dry-run)")
    args = p.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, url, source_type, thumbnail FROM resources "
        "WHERE source_type IN ('article', 'unknown', '', 'youtube') "
        "   OR thumbnail IS NULL OR thumbnail = ''"
    ).fetchall()

    # (id, url, old_type, new_type, old_thumb, new_thumb)
    updates: list[tuple[str, str, str, str, str, str]] = []
    for row in rows:
        detected = detect_source_type(row["url"])
        new_type = row["source_type"]
        if row["source_type"] in ("article", "unknown", "") and detected != "article":
            new_type = detected
        new_thumb = row["thumbnail"] or ""
        if new_type == "youtube" and not new_thumb:
            new_thumb = _youtube_thumbnail(row["url"])
        if new_type != row["source_type"] or new_thumb != (row["thumbnail"] or ""):
            updates.append((row["id"], row["url"], row["source_type"] or "", new_type,
                            row["thumbnail"] or "", new_thumb))

    print(f"Scanned {len(rows)} candidate rows; would update {len(updates)}.")
    for rid, url, old_t, new_t, old_th, new_th in updates[:50]:
        type_change = f"{old_t or '<empty>'} → {new_t}" if old_t != new_t else f"{new_t}"
        thumb_note = " (+ thumb)" if old_th != new_th else ""
        print(f"  {rid[:8]}  {type_change}{thumb_note}  {url}")
    if len(updates) > 50:
        print(f"  ... and {len(updates) - 50} more")

    if args.apply and updates:
        conn.executemany(
            "UPDATE resources SET source_type = ?, thumbnail = ? WHERE id = ?",
            [(new_t, new_th, rid) for (rid, _, _, new_t, _, new_th) in updates],
        )
        conn.commit()
        print(f"Applied {len(updates)} updates.")
    elif updates:
        print("Dry run only — pass --apply to commit.")

    conn.close()


if __name__ == "__main__":
    main()
