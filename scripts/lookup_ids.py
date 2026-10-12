#!/usr/bin/env python3
"""Resolve an anime show name to AniBridge target IDs.

Given a show name, search AniList, Kitsu, and TMDB and print a YAML block
with the discovered values for ``tmdb_show:<id>:s1``, ``anidb:<id>``,
``anilist:<id>``, ``mal:<id>``, ``kitsu:<id>``, and ``tvdb:<id>`` — the ID
slots used by the mapping files in this repo.

Provider notes
--------------
* **AniList** is the canonical source of truth (GraphQL, no auth). It returns
  the AniList id and the MAL id (``idMal``), and is the seed for Kitsu lookup.
  Its ``externalLinks`` field is scanned for an AniDB id as the first hop of
  the AniDB fallback chain (in practice this almost never returns a result —
  AniList's ``site`` enum is only streaming/social).
* **Kitsu** (JSON:API, no auth) is searched by name; its
  ``GET /anime/{id}/mappings`` endpoint exposes the cross-provider bridges
  (``externalSite`` of ``"anidb"``, ``"thetvdb"``, ``"anilist/anime"``,
  ``"myanimelist/anime"``, …) and is the most reliable source for both
  AniDB and TVDB.
* **TMDB v3** (requires API key) is searched by name for the TV series id.
  Because the project schema is ``tmdb_show:<id>:s1`` for the first season,
  we always pin the season suffix to ``s1``. ``/tv/{tmdb_id}/external_ids``
  is the TVDB fallback when Kitsu's mappings don't have one.

Fallback chains
---------------
* **AniDB**: AniList ``externalLinks`` → Kitsu ``/anime/{kitsu_id}/mappings``.
  Kitsu is the most reliable source but it is last so the chain is explicit
  and easy to read in the source. (A previous Jikan-based step was removed
  because the Jikan v4 API is too unreliable from many networks.)
* **TVDB**: Kitsu ``/anime/{kitsu_id}/mappings`` → TMDB
  ``/tv/{tmdb_id}/external_ids``. Kitsu wins on coverage without needing
  auth; TMDB fills the gap for shows that Kitsu hasn't catalogued yet.

TMDB API key
------------
TMDB requires a v3 API key. It is resolved in this order (first hit wins):

1. ``--tmdb-key`` CLI flag
2. ``[tmdb] api_key = ...`` in the config file (INI format; default location
   printed by ``--help``; override with ``--config <path>``)
3. ``TMDB_API_KEY`` environment variable

If none are set, the script prints a clear error and exits non-zero.

Output
------
A YAML comment header is printed first (search summary, source of each id,
suggested mapping.yml snippet), then a flat id-only YAML mapping that can be
read, piped, or pasted directly. The non-numeric AniBridge suffixes are
appended so the lines drop straight into a ``mapping.yml``: ``tmdb_show``
and ``tvdb_show`` get ``:s1`` (matching the existing
``tvdb_show:<id>:s1`` convention used in this repo), ``anidb`` gets ``:R``
(its AniDB resource marker), and the rest are bare numeric ids.
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# === Provider endpoints ====================================================

ANILIST_URL = "https://graphql.anilist.co"
KITSU_URL = "https://kitsu.app/api/edge"
TMDB_URL = "https://api.themoviedb.org/3"

USER_AGENT = "anibridge-mappings-lookup/1.0 (+https://github.com/JourneyOver)"

# === Configuration paths ===================================================


def default_config_path() -> Path:
    """Return the default config file location, next to this script.

    The config file is co-located with ``lookup_ids.py`` so it sits next to
    the script that reads it and travels with the repo. Override with
    ``--config <path>`` to point at a different file.
    """
    return Path(__file__).resolve().parent / "config.ini"


def load_tmdb_key_from_config(path: Path) -> str | None:
    """Read ``tmdb_api_key`` from an INI config file, or ``None``."""
    if not path.is_file():
        return None
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except (configparser.Error, OSError) as exc:
        print(f"Warning: cannot read config file {path}: {exc}", file=sys.stderr)
        return None
    if parser.has_option("tmdb", "api_key"):
        return parser.get("tmdb", "api_key").strip() or None
    return None


def resolve_tmdb_key(args: argparse.Namespace) -> str | None:
    """Resolve the TMDB API key from CLI flag, config, or env (in that order)."""
    if args.tmdb_key:
        return args.tmdb_key
    config_path = Path(args.config) if args.config else default_config_path()
    key = load_tmdb_key_from_config(config_path)
    if key:
        return key
    env = os.environ.get("TMDB_API_KEY", "").strip()
    return env or None


# === HTTP helpers ==========================================================


class ProviderError(RuntimeError):
    """Raised for any provider-side failure (network, HTTP, schema)."""


def _http_get_json(url: str, *, headers: dict[str, str] | None = None) -> Any:
    """GET ``url`` with sane defaults; return decoded JSON or raise."""
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            **(headers or {}),
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise ProviderError(f"GET {url} -> HTTP {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"GET {url} -> {exc.reason}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"GET {url} returned non-JSON body: {exc}") from exc


def _http_post_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
) -> Any:
    """POST ``payload`` (JSON-encoded) to ``url`` and return decoded JSON."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            **(headers or {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise ProviderError(f"POST {url} -> HTTP {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"POST {url} -> {exc.reason}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"POST {url} returned non-JSON body: {exc}") from exc


# === Provider queries ======================================================

ANILIST_QUERY = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
    id
    idMal
    title { romaji english native }
    format
    startDate { year month day }
    externalLinks { site url }
  }
}
"""


def search_anilist(name: str) -> dict[str, Any] | None:
    """Search AniList for ``name``; return the top match dict or ``None``."""
    data = _http_post_json(
        ANILIST_URL,
        {"query": ANILIST_QUERY, "variables": {"search": name}},
    )
    if "errors" in data:
        raise ProviderError(f"AniList GraphQL errors: {data['errors']}")
    media = (data.get("data") or {}).get("Media")
    return media


def search_kitsu(name: str) -> dict[str, Any] | None:
    """Search Kitsu for ``name``; return the top anime resource or ``None``.

    Kitsu is a JSON:API host; the ``Accept: application/vnd.api+json``
    header is required (sending ``application/json`` returns HTTP 406).
    """
    params = {"filter[text]": name, "page[limit]": "1"}
    url = f"{KITSU_URL}/anime?{urllib.parse.urlencode(params)}"
    data = _http_get_json(url, headers={"Accept": "application/vnd.api+json"})
    items = data.get("data") or []
    return items[0] if items else None


def get_kitsu_bridges(kitsu_id: str) -> dict[str, str]:
    """Fetch all external-id bridges for ``kitsu_id`` in one call.

    Kitsu's ``/anime/{id}/mappings`` returns every external site it knows
    about (e.g. ``anidb``, ``thetvdb``, ``anilist/anime``,
    ``myanimelist/anime``, ``trakt``, ``hulu``). This is the most reliable
    source for both AniDB and TVDB ids — they are extracted from this one
    response rather than via separate provider calls.

    Returns a dict ``{externalSite: externalId}`` of string ids; empty if the
    show has no mappings or the lookup fails.
    """
    url = f"{KITSU_URL}/anime/{kitsu_id}/mappings"
    data = _http_get_json(url, headers={"Accept": "application/vnd.api+json"})
    bridges: dict[str, str] = {}
    for mapping in data.get("data") or []:
        attrs = mapping.get("attributes") or {}
        site = attrs.get("externalSite")
        ext_id = attrs.get("externalId")
        if site and ext_id is not None:
            bridges[str(site)] = str(ext_id)
    return bridges


_ANIDB_URL_RE = re.compile(r"anidb\.net/anime/(\d+)")


def search_tmdb(name: str, api_key: str) -> dict[str, Any] | None:
    """Search TMDB v3 for ``name``; return the top TV series result or ``None``."""
    params = {"api_key": api_key, "query": name, "include_adult": "false"}
    url = f"{TMDB_URL}/search/tv?{urllib.parse.urlencode(params)}"
    data = _http_get_json(url)
    results = data.get("results") or []
    return results[0] if results else None


def get_tmdb_tvdb(tmdb_id: int, api_key: str) -> int | None:
    """Fetch the TVDB series id for a TMDB TV show via ``/external_ids``.

    Returns the integer TVDB id, or ``None`` if TMDB doesn't have one.
    """
    url = f"{TMDB_URL}/tv/{tmdb_id}/external_ids?api_key={urllib.parse.quote(api_key, safe='')}"
    try:
        data = _http_get_json(url)
    except ProviderError as exc:
        # 404 means "no external ids for this show" — treat as None.
        if "404" in str(exc):
            return None
        raise
    raw = data.get("tvdb_id")
    if isinstance(raw, int) and raw > 0:
        return raw
    return None


def get_anilist_anidb(anilist: dict[str, Any] | None) -> int | None:
    """Attempt to extract an AniDB id from ``anilist['externalLinks']``.

    In practice this almost always returns ``None``: AniList's
    ``externalLinks`` ``site`` enum only contains streaming/social sites and
    never lists AniDB or Kitsu. The function exists so the AniList step in
    the AniDB fallback chain is explicit; if a future AniList schema change
    starts surfacing AniDB, this is the place that picks it up.
    """
    if not anilist:
        return None
    for link in anilist.get("externalLinks") or []:
        site = (link.get("site") or "").lower()
        url_field = link.get("url") or ""
        if "anidb" in site or "anidb.net" in url_field:
            match = _ANIDB_URL_RE.search(url_field)
            if match:
                return int(match.group(1))
            digits = "".join(ch for ch in url_field if ch.isdigit())
            if digits:
                return int(digits)
    return None


# === Aggregation ==========================================================


@dataclass
class ResolvedShow:
    """All ids discovered for a show, plus per-id provenance notes."""

    query: str
    title: str | None = None
    anilist_id: int | None = None
    anilist_source: str | None = None
    mal_id: int | None = None
    mal_source: str | None = None
    kitsu_id: str | None = None
    kitsu_source: str | None = None
    anidb_id: int | None = None
    anidb_source: str | None = None
    tvdb_id: int | None = None
    tvdb_source: str | None = None
    tmdb_id: int | None = None
    tmdb_source: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_any(self) -> bool:
        return any(
            getattr(self, name) is not None
            for name in (
                "anilist_id",
                "mal_id",
                "kitsu_id",
                "anidb_id",
                "tvdb_id",
                "tmdb_id",
            )
        )


def _pick_title(anilist: dict[str, Any] | None) -> str | None:
    if not anilist:
        return None
    titles = anilist.get("title") or {}
    return titles.get("english") or titles.get("romaji") or titles.get("native")


def resolve(name: str, *, tmdb_key: str | None) -> ResolvedShow:
    """Run the lookup pipeline and aggregate the results.

    Resolution order:

    1. AniList search — canonical anchor. Supplies anilist_id and mal_id
       (``idMal``); its ``externalLinks`` are scanned for anidb (per the
       AniDB chain). If AniList didn't return ``idMal`` the script has no
       MAL fallback — Jikan was removed because its v4 API is unreliable
       from many networks.
    2. Kitsu search — supplies kitsu_id. One follow-up call to
       ``/anime/{kitsu_id}/mappings`` returns the bridge dict used for both
       anidb and thetvdb.
    3. TMDB search — supplies tmdb_id. ``/tv/{tmdb_id}/external_ids`` is the
       TVDB fallback if Kitsu's mappings didn't have one.

    The AniDB fallback chain runs **AniList → Kitsu**. The TVDB chain runs
    **Kitsu → TMDB**.
    """
    result = ResolvedShow(query=name)

    # 1. AniList is the anchor.
    anilist = None
    try:
        anilist = search_anilist(name)
    except ProviderError as exc:
        result.errors.append(f"AniList: {exc}")
    if anilist is not None:
        result.title = _pick_title(anilist)
        aid = anilist.get("id")
        if isinstance(aid, int):
            result.anilist_id = aid
            result.anilist_source = "AniList search"
        mal = anilist.get("idMal")
        if isinstance(mal, int):
            result.mal_id = mal
            result.mal_source = "AniList idMal"

    # 2. Kitsu for kitsu_id. The /mappings call supplies anidb + thetvdb.
    kitsu = None
    try:
        kitsu = search_kitsu(result.title or name)
    except ProviderError as exc:
        result.errors.append(f"Kitsu: {exc}")
    bridges: dict[str, str] = {}
    if kitsu is not None:
        kid = kitsu.get("id")
        if isinstance(kid, str) and kid.isdigit():
            result.kitsu_id = kid
            result.kitsu_source = "Kitsu search"
        if result.kitsu_id is not None:
            try:
                bridges = get_kitsu_bridges(result.kitsu_id)
            except ProviderError as exc:
                result.errors.append(f"Kitsu mappings: {exc}")

    # 3. AniDB fallback chain: AniList externalLinks -> Kitsu mappings.
    if result.anidb_id is None:
        try:
            anidb = get_anilist_anidb(anilist)
        except ProviderError as exc:
            result.errors.append(f"AniList externalLinks scan: {exc}")
            anidb = None
        if anidb is not None:
            result.anidb_id = anidb
            result.anidb_source = "AniList externalLinks"
    if result.anidb_id is None:
        raw = bridges.get("anidb")
        if raw and raw.isdigit():
            result.anidb_id = int(raw)
            result.anidb_source = "Kitsu mappings (externalSite=anidb)"

    # 4. TMDB for the tmdb_show id (skip cleanly if no key).
    if tmdb_key:
        tmdb = None
        try:
            tmdb = search_tmdb(result.title or name, tmdb_key)
        except ProviderError as exc:
            result.errors.append(f"TMDB: {exc}")
        if tmdb is not None:
            tid = tmdb.get("id")
            if isinstance(tid, int):
                result.tmdb_id = tid
                result.tmdb_source = "TMDB /search/tv"

    # 5. TVDB fallback chain: Kitsu -> TMDB external_ids.
    if result.tvdb_id is None:
        raw = bridges.get("thetvdb")
        if raw and raw.isdigit():
            result.tvdb_id = int(raw)
            result.tvdb_source = "Kitsu mappings (externalSite=thetvdb)"
    if result.tvdb_id is None and result.tmdb_id is not None and tmdb_key:
        try:
            tvdb = get_tmdb_tvdb(result.tmdb_id, tmdb_key)
        except ProviderError as exc:
            result.errors.append(f"TMDB external_ids: {exc}")
            tvdb = None
        if tvdb is not None:
            result.tvdb_id = tvdb
            result.tvdb_source = "TMDB /tv/{tmdb_id}/external_ids"

    return result


# === YAML output ===========================================================


def _fmt_optional(value: Any) -> str:
    return "null" if value is None else str(value)


def emit_yaml(result: ResolvedShow) -> str:
    """Render the resolved show as a pasteable YAML block + comment header."""
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = []

    lines.append(f"# Resolved {when} for query: {result.query!r}")
    if result.title:
        lines.append(f"# Title: {result.title}")
    lines.append("#")
    lines.append("# Sources:")
    for label, value, source in (
        ("anilist",   result.anilist_id, result.anilist_source),
        ("mal",       result.mal_id,     result.mal_source),
        ("kitsu",     result.kitsu_id,   result.kitsu_source),
        ("anidb",     result.anidb_id,   result.anidb_source),
        ("tvdb_show", result.tvdb_id,    result.tvdb_source),
        ("tmdb_show", result.tmdb_id,    result.tmdb_source),
    ):
        if value is not None:
            lines.append(f"#   {label:<10} {value}  ({source})")
        else:
            lines.append(f"#   {label:<10} (not found)")
    lines.append("#")
    lines.append("# Suggested mapping.yml snippet (replace EPISODES with the real range):")
    if result.tmdb_id is not None:
        lines.append("#")
        lines.append(f"#   tmdb_show:{result.tmdb_id}:s1:")
        if result.anidb_id is not None:
            lines.append(f"#     anidb:{result.anidb_id}:R:")
            lines.append("#       EPISODES: EPISODES")
        for label, value in (
            ("anilist", result.anilist_id),
            ("mal",     result.mal_id),
            ("kitsu",   result.kitsu_id),
        ):
            if value is not None:
                lines.append(f"#     {label}:{value}:")
                lines.append("#       EPISODES: EPISODES")
        if result.tvdb_id is not None:
            lines.append(f"#     tvdb_show:{result.tvdb_id}:s1:")
            lines.append("#       EPISODES: EPISODES")
    lines.append("")

    # Flat ID-only YAML for piping / pasting.
    lines.append("title: " + (result.title or result.query))
    lines.append(f"tmdb_show: {_fmt_optional((str(result.tmdb_id) + ':s1') if result.tmdb_id else None)}")
    lines.append(f"anidb: {_fmt_optional((str(result.anidb_id) + ':R') if result.anidb_id else None)}")
    lines.append(f"anilist: {_fmt_optional(result.anilist_id)}")
    lines.append(f"mal: {_fmt_optional(result.mal_id)}")
    lines.append(f"kitsu: {_fmt_optional(result.kitsu_id)}")
    lines.append(f"tvdb_show: {_fmt_optional((str(result.tvdb_id) + ':s1') if result.tvdb_id else None)}")

    if result.errors:
        lines.append("")
        lines.append("# Errors during lookup (printed to stderr too):")
        for err in result.errors:
            lines.append(f"#   - {err}")

    return "\n".join(lines) + "\n"


# === CLI ===================================================================


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Look up AniBridge target IDs (tmdb_show, tvdb_show, anidb, "
            "anilist, mal, kitsu) for an anime show name and print them as "
            "YAML."
        ),
    )
    parser.add_argument(
        "name",
        help="Anime show name to search for (e.g. 'Overgeared').",
    )
    parser.add_argument(
        "--tmdb-key",
        help=(
            "TMDB v3 API key. Overrides the config file and the "
            "TMDB_API_KEY env var."
        ),
    )
    parser.add_argument(
        "--config",
        help=(
            "Path to the INI config file (default: "
            f"{default_config_path()}). The file may contain a [tmdb] "
            "section with an 'api_key' option."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; return the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    tmdb_key = resolve_tmdb_key(args)
    if tmdb_key is None:
        print(
            "Warning: TMDB API key not configured; tmdb_show will not be "
            "resolved. Provide --tmdb-key, set [tmdb] api_key in the config "
            "file, or set TMDB_API_KEY.",
            file=sys.stderr,
        )

    result = resolve(args.name, tmdb_key=tmdb_key)

    if not result.has_any:
        print(
            f"Error: no ids found for {args.name!r} across any provider.",
            file=sys.stderr,
        )
        return 1

    sys.stdout.write(emit_yaml(result))
    for err in result.errors:
        print(f"error: {err}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
