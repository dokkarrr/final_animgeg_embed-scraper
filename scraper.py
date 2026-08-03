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

Strategy for episode URLs
--------------------------
AnimeGG episode URLs are NOT predictable from the slug + episode number.
The real URLs are only reliable when read from the series listing page
(e.g. https://www.animegg.org/series/detectiveconan).

So for every series we:
  1. Fetch the /series/<slug> page.
  2. Parse all <a href="..."> inside <ul class="newmanga"> to get the
     actual episode page URLs.
  3. Sort them by the episode number we extract from the link text /
     <i class="anititle"> so that ep 1 comes first.
  4. Fall back to the old guessed URL only when the series page cannot
     be fetched (network error) or has no episode links at all.

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
    suffix = "" if index == 1 else str(index)
    return OUTPUT_DIR / f"animegg_series{suffix}.json"


def find_last_chunk_index() -> int:
    idx = 1
    while chunk_path(idx + 1).exists():
        idx += 1
    return idx


def load_chunk(index: int) -> list:
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
    chunk_path(index).write_text(
        json.dumps(records, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def chunk_size_bytes(index: int) -> int:
    p = chunk_path(index)
    return p.stat().st_size if p.exists() else 0


def append_record_to_chunks(record: dict) -> tuple[int, int]:
    record_bytes = len(
        json.dumps(record, ensure_ascii=False).encode("utf-8")
    )
    idx = find_last_chunk_index()
    records = load_chunk(idx)
    if records and chunk_size_bytes(idx) + record_bytes + 4 > MAX_CHUNK_BYTES:
        idx += 1
        records = []
        print(f"  ↳ Starting new chunk: {chunk_path(idx).name}")
    records.append(record)
    save_chunk(idx, records)
    return idx, len(records)


# ── HTTP helper ────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


def fetch_html(url: str) -> str:
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
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
    return series_url.rstrip("/").split("/")[-1]


# ── Episode URL discovery ──────────────────────────────────────────────────────

def _ep_number_from_text(text: str) -> float:
    """
    Extract a numeric episode key from the link text / title text so we can
    sort correctly.  Handles things like "Episode 1206", "Detective Conan 352-353",
    "851", etc.  Returns a float so that e.g. 352.5 sorts between 352 and 353.
    """
    # Grab all digit-groups
    nums = re.findall(r"\d+", text)
    if not nums:
        return 0.0
    if len(nums) == 1:
        return float(nums[0])
    # Two numbers → treat as a range; sort by the first one with a tiny offset
    return float(nums[0]) + 0.5


def get_episode_urls_from_series_page(slug: str, total_eps: int) -> list[tuple[int, str]]:
    """
    Fetch the series listing page and return a list of (ep_number, full_url)
    pairs sorted ascending by episode number.

    Falls back to an empty list on any error so the caller can decide what to do.
    """
    series_url = f"{BASE_URL}/series/{slug}"
    try:
        html = fetch_html(series_url)
    except requests.exceptions.RequestException as exc:
        print(f"  ⚠ Could not fetch series page ({exc}); will fall back to guessed URLs.")
        return []

    soup = BeautifulSoup(html, "html.parser")

    # The episode list lives in <ul class="newmanga">
    ul = soup.find("ul", class_="newmanga")
    if ul is None:
        print("  ⚠ No <ul class='newmanga'> found on series page; will fall back.")
        return []

    results: list[tuple[float, str]] = []
    for li in ul.find_all("li"):
        a = li.find("a", class_="anm_det_pop")
        if a is None:
            continue
        href = a.get("href", "").strip()
        if not href:
            continue
        full_url = urljoin(BASE_URL, href)

        # Best label: the <strong> text inside the link (e.g. "Detective Conan 1206")
        strong = a.find("strong")
        label  = strong.get_text(strip=True) if strong else a.get_text(strip=True)

        ep_key = _ep_number_from_text(label)
        results.append((ep_key, full_url))

    if not results:
        print("  ⚠ Series page had no episode links; will fall back.")
        return []

    # Sort ascending (page usually lists newest-first)
    results.sort(key=lambda x: x[0])

    # Convert float keys to int episode numbers 1..N
    # We preserve the original URL; the ep number in the JSON is its 1-based
    # position in the sorted list (or the rounded key if you prefer).
    ep_pairs: list[tuple[int, str]] = []
    for rank, (key, url) in enumerate(results, start=1):
        ep_num = int(round(key)) if key == int(key) else int(key)  # e.g. 352.5 → 352
        ep_pairs.append((ep_num, url))

    print(f"  ✓ Series page: found {len(ep_pairs)} episode links "
          f"(expected {total_eps})")
    return ep_pairs


def build_guessed_episode_url(slug: str, episode: int) -> str:
    """Old-style fallback URL — may 404 for many series."""
    return f"{BASE_URL}/{slug}-episode-{episode}"


# ── Git helpers ────────────────────────────────────────────────────────────────

def git_commit_and_push(message: str) -> None:
    try:
        subprocess.run(["git", "add", "output/"], check=True)
        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            capture_output=True,
        )
        if diff.returncode != 0:
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

    Strategy
    --------
    1. Fetch the /series/<slug> page and collect the real episode URLs from the
       <ul class="newmanga"> listing — these are the only reliable URLs.
    2. If the series page fails or is empty, fall back to the old guessed URLs
       (/{slug}-episode-{N}) which at least work for simple slugs like
       naruto-shippuden.
    3. Visit each episode URL and extract the first two iframe srcs as sub/dub.

    Output shape
    ------------
    {
        "serial_no":    1,
        "title":        "Detective Conan",
        "animegg_url":  "https://www.animegg.org/series/detectiveconan",
        "mal_url":      "https://myanimelist.net/anime/235",
        "mal_id":       235,
        "total_ep":     1182,
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
    slug        = slug_from_series_url(series_url)

    print(f"\n{'#' * 70}")
    print(f"  [{serial_no}] {title}  ({total_eps} episodes)  slug={slug}")
    print(f"{'#' * 70}\n")

    # ── Step 1: discover real episode URLs from the series listing page ────────
    ep_pairs = get_episode_urls_from_series_page(slug, total_eps)

    use_discovered = bool(ep_pairs)

    if not use_discovered:
        # Build guessed pairs as fallback
        ep_pairs = [
            (ep, build_guessed_episode_url(slug, ep))
            for ep in range(1, total_eps + 1)
        ]
        print(f"  ↳ Using {len(ep_pairs)} guessed URLs as fallback.")

    # ── Step 2: scrape each episode page ──────────────────────────────────────
    episodes = []
    total = len(ep_pairs)

    for ep_num, ep_url in ep_pairs:
        print(f"  [{ep_num:>5}/{total_eps}] {ep_url}")

        sub = dub = None
        error = None

        try:
            html  = fetch_html(ep_url)
            soup  = BeautifulSoup(html, "html.parser")
            iframes = extract_iframes(soup, ep_url)
            sub   = iframes[0] if len(iframes) > 0 else None
            dub   = iframes[1] if len(iframes) > 1 else None
        except requests.exceptions.RequestException as exc:
            error = str(exc)
            print(f"           ERROR: {exc}")

        ep_record = {"ep": ep_num, "sub": sub, "dub": dub}
        if error:
            ep_record["error"] = error
        # Store the actual URL we used so it is easy to re-fetch later
        ep_record["url"] = ep_url

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

    print(f"Fetching master list from:\n  {ALL_JSON_URL}\n")
    try:
        resp = requests.get(ALL_JSON_URL, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        all_entries: list[dict] = resp.json()
    except Exception as exc:
        print(f"FATAL: Could not load master list — {exc}")
        sys.exit(1)

    print(f"Found {len(all_entries)} anime entries.\n")

    serial_from = os.environ.get("SERIAL_FROM", "").strip()
    serial_to   = os.environ.get("SERIAL_TO",   "").strip()

    if serial_from or serial_to:
        lo = int(serial_from) if serial_from else 1
        hi = int(serial_to)   if serial_to   else all_entries[-1].get("serial_no", len(all_entries))
        all_entries = [e for e in all_entries if lo <= e.get("serial_no", 0) <= hi]
        print(f"Range filter applied: serial_no {lo} → {hi}  ({len(all_entries)} entries)\n")

    completed = load_progress()
    print(f"Already completed: {len(completed)} series.\n")

    current_chunk = find_last_chunk_index()
    current_size  = chunk_size_bytes(current_chunk)
    print(f"Current output chunk: {chunk_path(current_chunk).name}  "
          f"({current_size / 1024:.1f} KB used of {MAX_CHUNK_BYTES // 1024} KB max)\n")

    batch_count = 0

    for entry in all_entries:
        serial_no = entry.get("serial_no")

        if serial_no in completed:
            print(f"[{serial_no}] {entry.get('title')} — already done, skipping.")
            continue

        record = scrape_series(entry)

        chunk_idx, count_in_chunk = append_record_to_chunks(record)
        size_kb = chunk_size_bytes(chunk_idx) / 1024
        print(
            f"\n  ✓ Appended to {chunk_path(chunk_idx).name}  "
            f"(entry #{count_in_chunk} in this file, {size_kb:.1f} KB)"
        )

        completed.add(serial_no)
        save_progress(completed)
        batch_count += 1

        if batch_count % COMMIT_EVERY == 0:
            timestamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
            git_commit_and_push(
                f"chore: scraped {COMMIT_EVERY} more anime "
                f"(total done: {len(completed)}) [{timestamp}]"
            )

    if batch_count % COMMIT_EVERY != 0:
        timestamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
        git_commit_and_push(
            f"chore: final batch — scraped {batch_count} series [{timestamp}]"
        )

    print(f"\n{SEPARATOR}")
    print(f"All done. {len(completed)} series completed total.")


if __name__ == "__main__":
    main()
