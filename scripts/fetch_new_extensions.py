#!/usr/bin/env python3
"""Fetch Visual Studio Code extensions published between the last list and now.

The Visual Studio Marketplace does not expose a documented API for listing
newly published extensions. Its undocumented ``extensionquery`` REST endpoint
supports stable sorting by first-publication date, but requires a full text
search term for which extensions are returned. To cover extensions regardless
of their name, the window is probed once for every letter and digit; results
are merged and deduplicated.

The end of the last list is stored in the manifest so the next run resumes
where the previous one stopped. Extensions published, removed and re-published
by their publisher reappear with a new first publication date.
"""

import argparse
import csv
import datetime as dt
import http.client
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

QUERY_URL = "https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery"
DEFAULT_USER_AGENT = (
    "new-vscode-extensions/1.0 (https://github.com/GHLists/new-vscode-extensions)"
)

PAGE_SIZE = 250
MAX_PAGES_PER_PROBE = 3
PROBE_DELAY_SECONDS = 0.3
DESCRIPTION_LIMIT = 300
FLAGS = 914  # IncludeVersions, files, version properties, asset URIs, latest version only
CSV_HEADER = (
    "created_at",
    "extension",
    "publisher",
    "title",
    "version",
    "description",
)

TRANSIENT_ERRORS = (
    urllib.error.URLError,
    TimeoutError,
    json.JSONDecodeError,
    http.client.HTTPException,
    OSError,
)


def iso(moment):
    moment = moment.astimezone(dt.timezone.utc)
    if moment.microsecond:
        fraction = f"{moment.microsecond:06d}".rstrip("0")
        return moment.strftime("%Y-%m-%dT%H:%M:%S") + f".{fraction}Z"
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(value):
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    moment = dt.datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc)


def timestamp_filename(moment):
    moment = moment.astimezone(dt.timezone.utc)
    stamp = moment.strftime("%Y-%m-%dT%H-%M-%S")
    if moment.microsecond:
        stamp += "-" + f"{moment.microsecond:06d}".rstrip("0")
    return stamp + "Z"


def fetch_json(url, body, user_agent, retries=3, backoff=5.0):
    last_error = None
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={
                "User-Agent": user_agent,
                "Content-Type": "application/json",
                "Accept": "application/json;api-version=3.0-preview.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except TRANSIENT_ERRORS as error:
            last_error = error
        if attempt < retries:
            print(f"attempt {attempt} failed ({last_error}), retrying", file=sys.stderr)
            time.sleep(backoff * attempt)
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def query_page(probe, page, page_size, user_agent, retries):
    body = {
        "filters": [
            {
                "pageNumber": page,
                "pageSize": page_size,
                "sortBy": 5,  # PublishedDate
                "sortOrder": 0,  # descending
                "criteria": [
                    {"filterType": 10, "value": probe},
                    {"filterType": 8, "value": "Microsoft.VisualStudio.Code"},
                ],
            }
        ],
        "assetTypes": [],
        "flags": FLAGS,
    }
    doc = fetch_json(QUERY_URL, body, user_agent, retries)
    results = (doc or {}).get("results")
    if not results or not isinstance(results, list):
        raise RuntimeError("marketplace response has no results")
    result = results[0]
    total = None
    for metadata in result.get("resultMetadata") or []:
        items = metadata.get("metadataItems") or []
        for item in items:
            if item.get("name") == "TotalCount":
                total = item.get("count")
    return result.get("extensions") or [], total


def fetch_probe_new_extensions(probe, since, until, args):
    """Return new extensions for one probe; (list, exhausted)."""
    extensions = []
    for page in range(1, args.max_pages + 1):
        published_rows, _total = query_page(
            probe, page, args.page_size, args.user_agent, args.retries
        )
        if not published_rows:
            return extensions, True
        oldest = None
        stop = False
        for extension in published_rows:
            if not isinstance(extension, dict):
                continue
            published = parse_timestamp(extension.get("publishedDate"))
            if published <= since:
                stop = True
                break
            if published > until:
                continue
            extensions.append(extension)
            oldest = published if oldest is None else min(oldest, published)
        if stop or oldest is None or oldest <= since:
            return extensions, True
        time.sleep(PROBE_DELAY_SECONDS)
    return extensions, False


def clean_text(value, limit=DESCRIPTION_LIMIT):
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "\u2026"
    return text


def build_row(extension, created):
    publisher = extension.get("publisher") or {}
    publisher_name = (
        publisher.get("publisherName")
        if isinstance(publisher, dict) and publisher.get("publisherName")
        else (extension.get("extensionName") or "").split(".", 1)[0]
    )
    name = extension.get("extensionName") or ""
    versions = extension.get("versions") or []
    version = versions[0].get("version") if versions else ""
    return {
        "created_at": iso(created),
        "extension": f"{publisher_name}.{name}",
        "publisher": publisher_name,
        "title": extension.get("displayName") or "",
        "version": version,
        "description": clean_text(extension.get("shortDescription")),
    }


def probe_letters():
    return [chr(letter) for letter in range(ord("a"), ord("z") + 1)] + [
        str(digit) for digit in range(10)
    ]


def write_csv(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADER)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def read_manifest_text(path):
    """Read the manifest from disk, or fall back to the committed copy.

    The workflow checks out only ``scripts`` from the repository, so the
    manifest can be missing from the working tree even though it is committed.
    """
    manifest_path = Path(path)
    try:
        return manifest_path.read_text(encoding="utf-8")
    except OSError:
        pass
    try:
        result = subprocess.run(
            ["git", "show", f"HEAD:{manifest_path.as_posix()}"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout


def load_manifest(path):
    text = read_manifest_text(path)
    if text is None:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"manifest {path} is not valid JSON") from error
    if not isinstance(data, dict):
        raise RuntimeError(f"manifest {path} must contain a JSON object")
    version = data.get("state_version", 1)
    if version != 1:
        raise RuntimeError(f"manifest {path} has an unsupported state version")
    return data


def save_manifest(path, manifest):
    manifest_path = Path(path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest_path.with_name(f".{manifest_path.name}.tmp")
    text = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, manifest_path)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        help="UTC start timestamp as ISO 8601 (default: end of the last list)",
    )
    parser.add_argument(
        "--until",
        help="UTC end timestamp as ISO 8601 (default: now)",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=PAGE_SIZE,
        help=f"marketplace page size per probe (default: {PAGE_SIZE})",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=MAX_PAGES_PER_PROBE,
        help=f"maximum marketplace pages walked per probe (default: {MAX_PAGES_PER_PROBE})",
    )
    parser.add_argument("--output-dir", default="data")
    parser.add_argument("--manifest", default="latest.json")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument(
        "--lookback-hours",
        type=float,
        default=1.0,
        help="window length when no previous list exists (default: 1)",
    )
    parser.add_argument(
        "--overlap-hours",
        type=float,
        default=2.0,
        help="hours rescinded by the marketplace indexer; each run rescans this window (default: 2)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    now = dt.datetime.now(dt.timezone.utc)
    until = parse_timestamp(args.until) if args.until else now
    manifest = load_manifest(args.manifest)

    if args.since:
        since = parse_timestamp(args.since)
        if "window" in manifest:
            stored_window = parse_timestamp(manifest["window"])
            if since < stored_window:
                raise RuntimeError(
                    "backfill would move the window backwards; "
                    f"the manifest window is {iso(stored_window)}"
                )
    elif "window" in manifest:
        since = parse_timestamp(manifest["window"])
    else:
        since = until - dt.timedelta(hours=args.lookback_hours)

    # The marketplace indexer can take an hour or two to surface freshly
    # published extensions, so every run also rescans the overlap and skips
    # extensions that were already recorded in an earlier run.
    if until - dt.timedelta(hours=args.overlap_hours) < since:
        since = until - dt.timedelta(hours=args.overlap_hours)

    if since >= until:
        print(f"nothing to do ({iso(since)} >= {iso(until)})", file=sys.stderr)
        return 0

    # The full text search is the only filter knob of the undocumented
    # marketplace API, so every letter and digit is probed and merged.
    recorded = dict(manifest.get("recorded") or {})
    seen = set()
    extensions = []
    exhausted = True
    for probe in probe_letters():
        found, probe_exhausted = fetch_probe_new_extensions(probe, since, until, args)
        if not probe_exhausted:
            exhausted = False
        for extension in found:
            publisher = extension.get("publisher") or {}
            key = (
                (publisher.get("publisherName") or "?", extension.get("extensionName") or "?")
                if isinstance(publisher, dict)
                else ("?", extension.get("extensionName") or "?")
            )
            key = extension.get("extensionId") or key
            if key in seen:
                continue
            seen.add(key)
            extensions.append(extension)
        time.sleep(PROBE_DELAY_SECONDS)

    rows = []
    skipped = 0
    for extension in extensions:
        if not isinstance(extension, dict) or not extension.get("publishedDate"):
            skipped += 1
            continue
        try:
            created = parse_timestamp(extension["publishedDate"])
        except (TypeError, ValueError):
            skipped += 1
            continue
        if created <= since or created > until:
            continue
        publisher = extension.get("publisher") or {}
        identity = extension.get("extensionId") or (
            f"{publisher.get('publisherName')}.{extension.get('extensionName')}"
            if isinstance(publisher, dict)
            else extension.get("extensionName") or ""
        )
        if identity in recorded:
            continue
        rows.append(build_row(extension, created))
        recorded[identity] = iso(until)
    rows.sort(key=lambda row: row["created_at"])
    if skipped:
        print(f"skipped {skipped} malformed extensions", file=sys.stderr)

    recorded_names = len(recorded) - len(manifest.get("recorded") or {})
    manifest["recorded"] = recorded
    manifest["window"] = iso(until)
    manifest["source_truncated"] = not exhausted
    if rows:
        output = (
            Path(args.output_dir)
            / f"new-extensions-{timestamp_filename(until)}.csv"
        )
        write_csv(output, rows)
        manifest["list"] = {
            "path": output.as_posix(),
            "from": iso(since),
            "to": iso(until),
            "count": len(rows),
        }
        print(
            f"wrote {len(rows)} extensions published between {iso(since)} "
            f"and {iso(until)} to {output}"
        )
    else:
        print(f"no new extensions between {iso(since)} and {iso(until)}")
    if not exhausted:
        print(
            "marketplace page limit reached; the window may be incomplete",
            file=sys.stderr,
        )
    save_manifest(args.manifest, manifest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
