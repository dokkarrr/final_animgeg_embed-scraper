#!/usr/bin/env python3
"""
scraper.py

Reads the master anime list from a remote JSON URL, scrapes each series
episode-by-episode for sub/dub embed iframe URLs, and writes all results
into chunked output files under output/:

    output/animegg_series.json
    output/animegg_series2.json
    output/animegg_series3.json
    ...

Each file is capped at MAX_CHUNK_BYTES (5 MB). A new file is started
automatically when the current one would exceed the limit.

Auto-commits every COMMIT_EVERY series so long runs don't lose progress.

Usage:
    python scraper.py

For GitHub Actions, see .github/workflows/scrape.yml
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ── Remote source ──────────────────────────────────────────────────────────────
ALL_JSON_URL = (
    "https://raw.githubusercontent.com/"
    "ytbro8326-sudo/animeg_last_mal_id_mapper/refs/heads/main/all.json"
)

# ── General settings ───────────────────────────────────────────────────────────
BASE_URL        = "https://www.animegg.org"
OUTPUT_DIR      = Path("output")
REQUEST_TIMEOUT = 15          # seconds per HTTP request
RETRY_ATTEMPTS  = 3           # retries on transient errors
RETRY_DELAY     = 5           # seconds between retries
POLITE_DELAY    = 1.0         # seconds between episode requests
COMMIT_EVERY    = 1           # commit & push after every N completed anime
MAX_CHUNK_BYTES = 5 * 1024 * 1024   # 5 MB per output file
SEPARATOR       = "=" * 70


# ── Chunk file helpers ─────────────────────────────────────────────────────────

def chunk_path(index: int) -> Path:
    """
    index 1  →  output/animegg_series.json
    index 2  →  output/animegg_series2.json
    index 3  →  output/animegg_series3.json
    """
    suffix = "" if index == 1 else str(index)
    return OUTPUT_DIR / f"animegg_series{suffix}.json"


def find_last_chunk_index() -> int:
    """Return the index of the highest-numbered chunk file that exists (min 1)."""
    idx = 1
    while chunk_path(idx + 1).exists():
        idx += 1
    return idx


def load_chunk(index: int) -> list:
    """Load the JSON array from a chunk file, or return [] if missing/corrupt."""
    p = chunk_path(index)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


def save_chunk(index: int, records: list) -> None:
    """Write the records list as a pretty-printed JSON array."""
    chunk_path(index).write_text(
        json.dumps(records, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def chunk_size_bytes(index: int) -> int:
    """Return on-disk byte size of a chunk file (0 if missing)."""
    p = chunk_path(index)
    return p.stat().st_size if p.exists() else 0


def append_record_to_chunks(record: dict) -> tuple[int, int]:
    """
    Append *record* to the current chunk. If the file would exceed
    MAX_CHUNK_BYTES after the append, start a new chunk first.

    Returns (chunk_index_written, new_record_count_in_that_chunk).
    """
    record_bytes = len(
        json.dumps(record, ensure_ascii=False).encode("utf-8")
    )

    # Find current active chunk
    idx = find_last_chunk_index()
    records = load_chunk(idx)

    # If adding this record would push the file over the limit, start fresh
    # (estimate: current file size + record bytes + ~4 bytes JSON overhead)
    if records and chunk_size_bytes(idx) + record_bytes + 4 > MAX_CHUNK_BYTES:
        idx += 1
        records = []
        print(f"  ↳ Starting new chunk: {chunk_path(idx).name}")

    records.append(record)
    save_chunk(idx, records)
    return idx, len(records)


# ── Helpers ────────────────────────────────────────────────────────────────────

def fetch_html(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.text
        except requests.exceptions.RequestException as exc:
            if attempt == RETRY_ATTEMPTS:
                raise
            print(f"    Attempt {attempt} failed ({exc}). Retrying in {RETRY_DELAY}s…")
            time.sleep(RETRY_DELAY)


def extract_iframes(soup: BeautifulSoup, base_url: str) -> list[str]:
    srcs = []
    for tag in soup.find_all("iframe"):
        src = tag.get("src", "").strip()
        if src:
            srcs.append(urljoin(base_url, src))
    return srcs


def slug_from_series_url(series_url: str) -> str:
    """
    https://www.animegg.org/series/one-piece  →  one-piece
    https://www.animegg.org/series/detectiveconan  →  detectiveconan
    """
    return series_url.rstrip("/").split("/")[-1]


def build_episode_url(slug: str, episode: int) -> str:
    return f"{BASE_URL}/{slug}-episode-{episode}"


# ── Git helpers ────────────────────────────────────────────────────────────────

def git_commit_and_push(message: str) -> None:
    """Stage output/, commit (if changed), push."""
    try:
        subprocess.run(["git", "add", "output/"], check=True)
        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            capture_output=True,
        )
        if diff.returncode != 0:          # there are staged changes
            subprocess.run(["git", "commit", "-m", message], check=True)
            subprocess.run(["git", "push"], check=True)
            print(f"  ✓ Committed & pushed: {message}")
        else:
            print("  ℹ No changes to commit.")
    except subprocess.CalledProcessError as exc:
        print(f"  ⚠ Git error (non-fatal): {exc}")


# ── Per-series scraper ─────────────────────────────────────────────────────────

def scrape_series(entry: dict) -> dict:
    """
    Scrape all episodes for one anime entry and return the structured record.

    Output shape:
    {
        "serial_no":    1,
        "title":        "One Piece",
        "animegg_url":  "https://www.animegg.org/series/one-piece",
        "mal_url":      "https://myanimelist.net/anime/21",
        "mal_id":       21,
        "total_ep":     1160,
        "episodes": [
            { "ep": 1, "sub": "...", "dub": "..." },
            ...
        ]
    }
    """
    serial_no   = entry.get("serial_no")
    title       = entry.get("title", "Unknown")
    series_url  = entry.get("url", "")
    mal_id      = entry.get("mal_id")
    mal_url     = entry.get("mal_url", "")
    total_eps   = entry.get("episode_count", 0)

    slug = slug_from_series_url(series_url)

    print(f"\n{'#' * 70}")
    print(f"  [{serial_no}] {title}  ({total_eps} episodes)  slug={slug}")
    print(f"{'#' * 70}\n")

    episodes = []

    for ep in range(1, total_eps + 1):
        url = build_episode_url(slug, ep)
        print(f"  [{ep:>5}/{total_eps}] {url}")

        sub = None
        dub = None
        error = None

        try:
            html = fetch_html(url)
            soup = BeautifulSoup(html, "html.parser")
            iframes = extract_iframes(soup, url)
            sub = iframes[0] if len(iframes) > 0 else None
            dub = iframes[1] if len(iframes) > 1 else None
        except requests.exceptions.RequestException as exc:
            error = str(exc)
            print(f"           ERROR: {exc}")

        ep_record = {"ep": ep, "sub": sub, "dub": dub}
        if error:
            ep_record["error"] = error

        episodes.append(ep_record)
        time.sleep(POLITE_DELAY)

    return {
        "serial_no":   serial_no,
        "title":       title,
        "animegg_url": series_url,
        "mal_url":     mal_url,
        "mal_id":      mal_id,
        "total_ep":    total_eps,
        "episodes":    episodes,
    }


# ── Progress tracking ──────────────────────────────────────────────────────────

PROGRESS_FILE = OUTPUT_DIR / "_progress.json"


def load_progress() -> set[int]:
    """Return set of serial_no values already completed."""
    if PROGRESS_FILE.exists():
        try:
            data = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
            return set(data.get("completed", []))
        except Exception:
            pass
    return set()


def save_progress(completed: set[int]) -> None:
    PROGRESS_FILE.write_text(
        json.dumps({"completed": sorted(completed)}, indent=2),
        encoding="utf-8",
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── Fetch master list ──────────────────────────────────────────────────
    print(f"Fetching master list from:\n  {ALL_JSON_URL}\n")
    try:
        resp = requests.get(ALL_JSON_URL, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        all_entries: list[dict] = resp.json()
    except Exception as exc:
        print(f"FATAL: Could not load master list — {exc}")
        sys.exit(1)

    print(f"Found {len(all_entries)} anime entries.\n")

    # ── Optional serial_no range filter (set via workflow_dispatch inputs) ─
    serial_from = os.environ.get("SERIAL_FROM", "").strip()
    serial_to   = os.environ.get("SERIAL_TO",   "").strip()

    if serial_from or serial_to:
        lo = int(serial_from) if serial_from else 1
        hi = int(serial_to)   if serial_to   else all_entries[-1].get("serial_no", len(all_entries))
        all_entries = [e for e in all_entries if lo <= e.get("serial_no", 0) <= hi]
        print(f"Range filter applied: serial_no {lo} → {hi}  ({len(all_entries)} entries)\n")

    completed = load_progress()
    print(f"Already completed: {len(completed)} series.\n")

    # Show current chunk status on resume
    current_chunk = find_last_chunk_index()
    current_size  = chunk_size_bytes(current_chunk)
    print(f"Current output chunk: {chunk_path(current_chunk).name}  "
          f"({current_size / 1024:.1f} KB used of {MAX_CHUNK_BYTES // 1024} KB max)\n")

    batch_count = 0   # how many series finished in this run

    for entry in all_entries:
        serial_no = entry.get("serial_no")

        if serial_no in completed:
            print(f"[{serial_no}] {entry.get('title')} — already done, skipping.")
            continue

        # ── Scrape ────────────────────────────────────────────────────────
        record = scrape_series(entry)

        # ── Append to chunked output files ────────────────────────────────
        chunk_idx, count_in_chunk = append_record_to_chunks(record)
        size_kb = chunk_size_bytes(chunk_idx) / 1024
        print(
            f"\n  ✓ Appended to {chunk_path(chunk_idx).name}  "
            f"(entry #{count_in_chunk} in this file, {size_kb:.1f} KB)"
        )

        completed.add(serial_no)
        save_progress(completed)
        batch_count += 1

        # ── Auto-commit every COMMIT_EVERY series ────────────────────────
        if batch_count % COMMIT_EVERY == 0:
            timestamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
            git_commit_and_push(
                f"chore: scraped {COMMIT_EVERY} more anime "
                f"(total done: {len(completed)}) [{timestamp}]"
            )

    # ── Final commit for any leftover series ──────────────────────────────
    if batch_count % COMMIT_EVERY != 0:
        timestamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
        git_commit_and_push(
            f"chore: final batch — scraped {batch_count} series [{timestamp}]"
        )

    print(f"\n{SEPARATOR}")
    print(f"All done. {len(completed)} series completed total.")


if __name__ == "__main__":
    main()
