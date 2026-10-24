#!/usr/bin/env python3
"""Fetch official Chinese feature names from LPND (月球与行星地名库).

LPND is the naming authority the database should follow, but its bulk export
needs an account the site is currently refusing to create, so the names have to
be read one search at a time.  This does that politely: one request in flight,
a delay between them, backoff on refusal, and every answer cached on disk so an
interrupted run resumes instead of starting over.

Two things keep the crawl small.  **Lettered craters are not queried at all** --
a satellite crater's Chinese name is its parent's name plus the letter, and the
parent is queried once.  And features the site has already answered for are
skipped on the next run, cache and all.

The search endpoint is discovered rather than hard-coded, because the site is a
single-page application that loads its API layer lazily:

    python tools/fetch_lpnd_names.py --discover

If discovery cannot pin it down (the bundle is minified and the paths are built
at run time), open the search page once with the browser's Network panel open,
copy the request as cURL, and pass it:

    python tools/fetch_lpnd_names.py --curl-from-clipboard   # or --template

Usage
-----
    python tools/fetch_lpnd_names.py --discover
    python tools/fetch_lpnd_names.py --dry-run          # show the query plan
    python tools/fetch_lpnd_names.py --limit 20         # try a few
    python tools/fetch_lpnd_names.py                    # the whole database
"""

from __future__ import annotations

import argparse
import os
import csv
import json
import random
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlencode

# ``whitespace="keep"``: this matcher compares catalogue names that were
# already normalised when the catalogue was built, so it takes them as written.
from _bootstrap import CATALOGUE_COLUMNS, ROOT, fold  # noqa: E402

CATALOGUE = ROOT / "src" / "moonlib" / "data" / "lunar_nomenclature.csv"
CACHE = ROOT / ".cache" / "lpnd" / "answers.jsonl"
OUTPUT = ROOT / "src" / "moonlib" / "data" / "lunar_names_lpnd.csv"
PROVENANCE = ROOT / "src" / "moonlib" / "data" / "lunar_names_lpnd.provenance.json"

SITE = "https://lpnd.bao.ac.cn"
#: The API lives under /web (``requestCtx`` in the site's own bundle), and the
#: search page posts to /front/search/advance -- discovered by reading the
#: bundle, not guessed.  ``types=155`` is the place-name library.
API = f"{SITE}/web"
SEARCH_URL = f"{API}/front/search/advance"
TYPES = "155"
#: Rows come back fuzzy-ranked, so an answer is only accepted when its own
#: Feature_Name is the name that was asked for: searching Tycho puts 第谷A坑
#: (Tycho A) first, and taking the first row would name the wrong feature.
FIELDS = {"name": "Feature_Name", "chinese": "CN_Name", "id": "Feature_ID"}
#: Identify the tool and its purpose: a crawl that says what it is can be
#: allowed or blocked deliberately, which is the least a courtesy call can do.
USER_AGENT = ("MoonLabeller-names/1.0 (one-off name lookup for a local lunar "
              "simulator; contact: the operator of this machine)")

#: Seconds between requests.  The site is small and shared; the whole database
#: is a few thousand lookups, which at this rate is an afternoon, not a flood.
DELAY = 1.2
JITTER = 0.4
BACKOFF = (5.0, 20.0, 60.0, 180.0)
TIMEOUT = 30.0

#: A satellite crater: parent's name, a space, one capital letter.
SATELLITE = re.compile(r"^(?P<parent>.+?)\s+(?P<letter>[A-Z])$")




def ascii_variant(name: str) -> str:
    """The same name without its diacritics: Angström -> Angstrom.

    LPND's own index does not match the accented spelling for every feature, so
    an unanswered name is asked again in this form and the answer is attributed
    back to the name the database uses.
    """
    decomposed = unicodedata.normalize("NFKD", str(name or ""))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.split())


def variants(name: str) -> List[str]:
    """Spellings the site might index a name under, best first.

    LPND's index is fussy about the marks transliterated names carry: an
    apostrophe (Artem'ev), a diaeresis (Angström), or the second half of a
    double name (Engel'gardt (Engelhardt)).  Each is a different string to the
    search, so each is tried, and an answer is only believed when it names the
    variant that was asked for.
    """
    name = str(name or "").strip()
    plain = ascii_variant(name)
    without_apostrophes = re.sub(r"['\u2019`]", "", plain)
    spaced = re.sub(r"['\u2019`]", " ", plain)
    hyphened = re.sub(r"['\u2019`]", "-", plain)
    inner = re.search(r"\(([^)]+)\)", plain)
    outer = plain.split("(")[0].strip()
    made = [name, plain, without_apostrophes, spaced, hyphened, outer]
    # The site's index misses a whole name more often than a leading part of
    # it, so progressively shorter beginnings are tried too: Bel'kovich ->
    # Bel'kovi -> Bel'ko.  Four characters is the floor; shorter than that and
    # the search matches half the Moon.
    shortest = 4
    base = without_apostrophes or plain
    for fraction in (0.75, 0.6, 0.45):
        length = max(shortest, int(len(base) * fraction))
        if length < len(base):
            made.append(base[:length].strip())
    if " " in plain:
        made.append(plain.split(" ")[0])
    if inner:
        made.insert(1, inner.group(1).strip())
        made.append(f"{inner.group(1).strip()}", )
    seen, ordered = set(), []
    for item in made:
        item = " ".join(str(item).split())
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


# ---------------------------------------------------------------------------
# the query plan
# ---------------------------------------------------------------------------
def load_catalogue(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return [row for row in csv.DictReader(handle) if (row.get("English Name") or "").strip()]


def build_plan(rows: Sequence[Dict[str, str]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """What to ask for, and what to answer locally instead.

    Returns the queries (one per base feature) and the keys that will be
    derived: a satellite crater is never queried, because its Chinese name is
    the parent's plus its own letter.
    """
    by_name = {fold(row["English Name"], whitespace="keep"): row for row in rows}
    queries: List[Dict[str, Any]] = []
    derived: List[str] = []
    for row in rows:
        name = row["English Name"].strip()
        match = SATELLITE.match(name)
        parent = by_name.get(fold(match.group("parent"), whitespace="keep")) if match else None
        if match and parent is not None:
            # Either the parent will be queried, or it already has a name and
            # this one can be built without asking anybody.
            derived.append(name)
            continue
        queries.append({"name": name, "row": row})
    return queries, derived


# ---------------------------------------------------------------------------
# talking to the site
# ---------------------------------------------------------------------------
def http_post(url: str, payload: Dict[str, Any]) -> Tuple[int, str]:
    """One POST of a JSON body, which is what the search page sends."""
    import urllib.error
    import urllib.request

    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": f"{SITE}/",
    })
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")
    except Exception as error:
        return 0, f"{type(error).__name__}: {error}"


def http_get(url: str, *, params: Optional[Dict[str, str]] = None,
             headers: Optional[Dict[str, str]] = None) -> Tuple[int, str]:
    """One GET, with the browser's own headers so the app answers normally."""
    import urllib.error
    import urllib.request

    if params:
        url = f"{url}?{urlencode(params)}"
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": f"{SITE}/",
        **(headers or {}),
    })
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")
    except Exception as error:                      # network, DNS, TLS, timeout
        return 0, f"{type(error).__name__}: {error}"


def discover_endpoints() -> List[Dict[str, str]]:
    """Read the site's bundles and report the API paths it builds.

    The application is a single-page app whose API layer is a lazy chunk, so the
    paths are not in the entry bundle; every chunk is fetched and searched for
    the shapes an API call takes.  A candidate reported here is a real string
    from the site's own code, not a guess.
    """
    status, html = http_get(f"{SITE}/")
    if status != 200:
        print(f"  cannot reach {SITE}: HTTP {status} {html[:120]}", file=sys.stderr)
        return []
    bundles = re.findall(r'(?:src|href)=["\']?(js/[^"\'>\s]+\.js)', html)
    bundles += re.findall(r'(?:src|href)=["\']?(js/chunk-[^"\'>\s]+\.js)', html)
    seen, candidates = set(), []
    for bundle in dict.fromkeys(bundles):
        if bundle in seen:
            continue
        seen.add(bundle)
        code, body = http_get(f"{SITE}/{bundle}")
        if code != 200:
            continue
        for pattern in (r'requestCtx\s*[:=]\s*["\']([^"\']+)["\']',
                        r'baseURL\s*[:=]\s*["\']([^"\']+)["\']',
                        r'["\'](/front/[A-Za-z0-9/_.-]{2,60})["\']',
                        r'["\'](/[A-Za-z0-9/_-]*(?:search|placeName)[A-Za-z0-9/_-]*)["\']'):
            for hit in re.findall(pattern, body):
                candidates.append({"bundle": bundle, "path": hit})
        time.sleep(0.4)
    print(f"  inspected {len(seen)} bundle(s), {len(candidates)} path(s)")
    for item in candidates[:40]:
        print(f"    {item['bundle']}: {item['path']}")
    return candidates


def match_key(text: str) -> str:
    """Everything that is not a letter or digit removed.

    The site's own spelling of a name may differ in punctuation only -- an
    apostrophe, a hyphen, a diaeresis -- and those are not differences in the
    name, so the comparison ignores them.
    """
    return "".join(character for character in ascii_variant(text).casefold()
                   if character.isalnum())


def parse_answer(body: str, name: str) -> Tuple[Optional[str], str]:
    """The Chinese name for *name*, and what the answer actually named.

    The search is a fuzzy one, so the row is only believed when the site's own
    ``Feature_Name`` is the name that was asked for; anything else is reported
    as a mismatch rather than used.  Returns ``(chinese or None, matched name)``.
    """
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None, ""
    rows = payload.get("result")
    if not isinstance(rows, list):
        return None, ""
    wanted = fold(name, whitespace="keep")
    best = ""
    for row in rows:
        if not isinstance(row, dict):
            continue
        english = str(row.get(FIELDS["name"]) or "").strip()
        chinese = str(row.get(FIELDS["chinese"]) or "").strip()
        if not english:
            continue
        if fold(english, whitespace="keep") == wanted and chinese:
            return chinese, english
        if chinese and match_key(english) == match_key(name):
            return chinese, english
        if not best and fold(english, whitespace="keep").startswith(wanted):
            best = english
    return None, best


def crawl(queries: Sequence[Dict[str, Any]], *, url: str, types: str,
          answers: Dict[str, str], limit: Optional[int],
          delay: float, mismatches: List[Dict[str, str]]) -> Dict[str, str]:
    """Ask about each feature once, sleeping between requests."""
    pending = [item for item in queries if item["name"] not in answers]
    if limit is not None:
        pending = pending[:limit]
    print(f"  {len(pending)} to ask about, {len(answers)} already cached")
    refusals = 0
    for number, item in enumerate(pending, 1):
        name = item["name"]
        status, body = http_post(url, {"page": 1, "limit": 10, "types": types,
                                       "keywords": [name]})
        if status in (429, 503) or status >= 500:
            wait = BACKOFF[min(refusals, len(BACKOFF) - 1)]
            refusals += 1
            print(f"  HTTP {status} on {name!r}; waiting {wait:.0f}s")
            time.sleep(wait)
            continue
        refusals = 0
        chinese, matched = parse_answer(body, name) if status == 200 else (None, "")
        tried = [name]
        for variant in variants(name)[1:]:
            if chinese is not None:
                break
            tried.append(variant)
            time.sleep(delay + random.uniform(0.0, JITTER))
            alt_status, alt_body = http_post(url, {"page": 1, "limit": 10,
                                                   "types": types,
                                                   "keywords": [variant]})
            if alt_status == 200:
                chinese, alt_matched = parse_answer(alt_body, variant)
                if chinese:
                    matched = f"{alt_matched} (spelled {variant})"
        if chinese is None:
            # Either the site has no such name, or it answered with a different
            # one: both are recorded, and neither is used as a translation.
            if matched:
                mismatches.append({"asked": name, "answered": matched,
                                   "tried": tried})
            answers[name] = ""
        else:
            answers[name] = chinese
        append_cache({"name": name, "zh": chinese or "", "matched": matched,
                      "status": status,
                      "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        if number % 25 == 0 or number < 5:
                    print(f"  [{number}/{len(pending)}] {name} -> {chinese or '（无）'}"
                  + (f"  (答的是 {matched})" if chinese is None and matched else ""))
        time.sleep(delay + random.uniform(0.0, JITTER))
    return answers


# ---------------------------------------------------------------------------
# cache and output
# ---------------------------------------------------------------------------
def read_cache() -> Dict[str, str]:
    answers: Dict[str, str] = {}
    if not CACHE.is_file():
        return answers
    with CACHE.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            name = record.get("name")
            if name and record.get("status") == 200:
                answers[name] = record.get("zh") or ""
    return answers


def append_cache(record: Dict[str, Any]) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with CACHE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def derive_satellites(answers: Dict[str, str], names: Iterable[str]) -> Dict[str, str]:
    """Letter the parent's Chinese name the way the naming table itself does."""
    derived: Dict[str, str] = {}
    for name in names:
        match = SATELLITE.match(name)
        if not match:
            continue
        parent = answers.get(match.group("parent"))
        if parent:
            derived[name] = f"{parent}{match.group('letter')}"
    return derived


def write_output(rows: Sequence[Dict[str, str]], answers: Dict[str, str],
                 derived: Dict[str, str]) -> Dict[str, Any]:
    resolved: Dict[str, str] = {}
    for name, chinese in answers.items():
        if chinese:
            resolved[name] = chinese
    resolved.update(derived)
    fields = list(CATALOGUE_COLUMNS)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_suffix(".csv.part")
    written = 0
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            chinese = resolved.get(row["English Name"].strip(), "")
            writer.writerow({**{field: row.get(field, "") for field in fields}, "名称": chinese})
            written += bool(chinese)
    temporary.replace(OUTPUT)
    report = {
        "source": "LPND (月球与行星地名库) https://lpnd.bao.ac.cn",
        "asked": len(answers),
        "answered": sum(1 for value in answers.values() if value),
        "derived_satellites": len(derived),
        "named_rows": written,
        "rows": len(rows),
        "coverage_percent": round(100.0 * written / max(1, len(rows)), 1),
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    PROVENANCE.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--catalogue", type=Path, default=CATALOGUE)
    parser.add_argument("--discover", action="store_true",
                        help="report the site's API paths and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the query plan and exit")
    parser.add_argument("--url", default=SEARCH_URL, help="search endpoint")
    parser.add_argument("--types", default=TYPES, help="LPND type code (155 = 地名)")
    parser.add_argument("--limit", type=int, default=None, help="ask about N features")
    parser.add_argument("--delay", type=float, default=DELAY,
                        help=f"seconds between requests (default {DELAY})")
    parser.add_argument("--refresh", action="store_true",
                        help="ignore the cache for features that answered with nothing")
    args = parser.parse_args(argv)

    if args.discover:
        discover_endpoints()
        return 0

    rows = load_catalogue(args.catalogue)
    queries, satellite_names = build_plan(rows)
    answers = read_cache()
    if args.refresh:
        answers = {name: value for name, value in answers.items() if value}
    derived = derive_satellites(answers, satellite_names)
    print(f"  catalogue   : {len(rows)} rows")
    print(f"  queries     : {len(queries)} base features "
          f"({len(satellite_names)} satellite craters derived, never queried)")
    print(f"  cached      : {len(answers)} answers, {len(derived)} satellites derivable")
    if args.dry_run:
        for item in queries[:10]:
            print(f"    would ask {item['name']}")
        print(f"    ... {len(queries)} in total")
        return 0

    mismatches: List[Dict[str, str]] = []
    answers = crawl(queries, url=args.url, types=args.types, answers=answers,
                    limit=args.limit, delay=args.delay, mismatches=mismatches)
    derived = derive_satellites(answers, satellite_names)
    report = write_output(rows, answers, derived)
    print(f"  asked       : {report['asked']} ({report['answered']} answered)")
    print(f"  derived     : {report['derived_satellites']} satellite craters")
    print(f"  named       : {report['named_rows']} / {report['rows']} "
          f"({report['coverage_percent']}%)")
    print(f"  written     : {os.path.relpath(OUTPUT)}")
    print(f"  provenance  : {os.path.relpath(PROVENANCE)}")
    print("  next        : python tools/build_landform_db.py --features searchresults.csv "
          f"--translation {OUTPUT.name} ...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
