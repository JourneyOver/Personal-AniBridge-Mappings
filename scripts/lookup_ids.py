#!/usr/bin/env python3
"""Resolve an anime show name to AniBridge target IDs.

Given a show name, search AniList, animap.id, Kitsu, and TMDB and print a
YAML block with the discovered values for ``tmdb_show:<id>:s1``,
``anidb:<id>``, ``anilist:<id>``, ``mal:<id>``, ``kitsu:<id>``, and
``tvdb:<id>`` — the ID slots used by the mapping files in this repo.

Provider notes
--------------
* **AniList** (GraphQL, no auth) is the default canonical anchor: it returns
  the AniList id, the MAL id (``idMal``), and the ``# Title:`` display line,
  and is the seed for Kitsu lookup. Its fuzzy search can return a poor top
  hit (e.g. a mini-anime), so an animap exact-title match wins the **whole
  record** — ids *and* the display title — when their MAL ids disagree (see
  Fallback chains). Its ``externalLinks`` field is scanned for an AniDB id
  as a later hop of the AniDB fallback chain (in practice this almost never
  returns a result — AniList's ``site`` enum is only streaming/social).
* **animap.id** (JSON, no auth) is searched by name, and its
  ``GET /api/map/mal/{id}`` route returns one curated cross-ID record keyed
  by MAL id with AniDB, TVDB, and TMDB ids (plus AniList/ANN/Kitsu/IMDb).
  ``/api/map`` is the legacy-but-documented route (the newer ``/api/v1/map``
  endpoints keep cross-ids buried inside ``sources`` URLs). Because it is
  unauthenticated, animap is the **preferred source for the bridge slots**
  (anidb/tvdb/tmdb) and lets ``tmdb_show``/``tvdb_show`` resolve with no
  TMDB API key. It is a fallback for anilist/mal/kitsu — except when its
  picked result is an **exact title match** whose MAL id disagrees with
  AniList's, in which case animap overrides anilist/mal/kitsu and the display
  title too so the emitted record stays self-consistent.
* **Kitsu** (JSON:API, no auth) is searched by name; its
  ``GET /anime/{id}/mappings`` endpoint exposes the cross-provider bridges
  (``externalSite`` of ``"anidb"``, ``"thetvdb"``, ``"anilist/anime"``,
  ``"myanimelist/anime"``, …) and remains a reliable source for both AniDB
  and TVDB when animap has no entry.
* **TMDB v3** (optional API key) is searched by name for the TV series id.
  Because the project schema is ``tmdb_show:<id>:s1`` for the first season,
  we always pin the season suffix to ``s1``. ``/tv/{tmdb_id}/external_ids``
  is the final TVDB fallback when animap and Kitsu don't have one.

Fallback chains
---------------
* **Anchor ids** (anilist/mal/kitsu): AniList (and Kitsu search) by default.
  When animap's picked search result is an exact title match and its MAL id
  differs from AniList's, animap becomes authoritative for the whole record
  (anilist/mal/kitsu), so a fuzzy AniList top hit cannot be mixed with
  animap's bridge ids.
* **AniDB**: animap ``/api/map/mal/{id}`` → AniList ``externalLinks`` →
  Kitsu ``/anime/{kitsu_id}/mappings``.
* **TVDB**: animap ``/api/map/mal/{id}`` → Kitsu
  ``/anime/{kitsu_id}/mappings`` → TMDB ``/tv/{tmdb_id}/external_ids``.
* **TMDB**: animap ``/api/map/mal/{id}`` (``type == "tv"`` only) → TMDB
  ``/search/tv``.

TMDB API key
------------
TMDB requires a v3 API key, but it is **optional**: when no key is set,
animap can still supply ``tmdb_show`` and ``tvdb_show``. The key is resolved
in this order (first hit wins):

1. ``--tmdb-key`` CLI flag
2. ``[tmdb] api_key = ...`` in the config file (INI format; default location
   printed by ``--help``; override with ``--config <path>``)
3. ``TMDB_API_KEY`` environment variable

If none are set, the script prints a non-fatal warning and continues with
animap/Kitsu/AniList as the only sources.

Output
------
A YAML comment header is printed first (search summary, source of each id,
suggested mapping.yml snippet), then a flat id-only YAML mapping that can be
read, piped, or pasted directly. The non-numeric AniBridge suffixes are
appended so the lines drop straight into a ``mapping.yml``: ``tmdb_show``
and ``tvdb_show`` get ``:s1`` (matching the existing
``tvdb_show:<id>:s1`` convention used in this repo), ``anidb`` gets ``:R``
(its AniDB resource marker), and the rest are bare numeric ids.

Layout of this file
-------------------
1. Constants and configuration
2. HTTP helpers
3. Provider clients (AniList, animap, Kitsu, TMDB)
4. Data model (``Slot``, ``AnimapMatch``, ``ResolvedShow``)
5. Resolution pipeline (``resolve`` and one small function per step)
6. YAML rendering
7. CLI
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

# === 1. Constants and configuration =======================================

ANILIST_URL = "https://graphql.anilist.co"
KITSU_URL = "https://kitsu.app/api/edge"
TMDB_URL = "https://api.themoviedb.org/3"
ANIMAP_URL = "https://animap.id"
ANIMAP_SEARCH_URL = f"{ANIMAP_URL}/api/search"
ANIMAP_MAP_URL = f"{ANIMAP_URL}/api/map"

USER_AGENT = "anibridge-mappings-lookup/1.0 (+https://github.com/JourneyOver)"
HTTP_TIMEOUT = 20

# Kitsu is a JSON:API host; plain ``application/json`` returns HTTP 406.
KITSU_HEADERS = {"Accept": "application/vnd.api+json"}

# Where each slot's id lives in an animap record.
ANIMAP_KEYS = {
    "anilist": "anilist_id",
    "mal": "mal_id",
    "kitsu": "kitsu_id",
    "anidb": "anidb_id",
    "tvdb": "tvdb_id",
    "tmdb": "tmdb_id",
}


def default_config_path() -> Path:
    """Return the default config file location, next to this script.

    The config file is co-located with ``lookup_ids.py`` so it sits next to
    the script that reads it and travels with the repo. Override with
    ``--config <path>`` to point at a different file.
    """
    return Path(__file__).resolve().parent / "config.ini"


def load_tmdb_key_from_config(path: Path) -> str | None:
    """Read ``[tmdb] api_key`` from an INI config file, or ``None``."""
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
    return (
        load_tmdb_key_from_config(config_path)
        or os.environ.get("TMDB_API_KEY", "").strip()
        or None
    )


# === 2. HTTP helpers ======================================================


class ProviderError(RuntimeError):
    """Raised for any provider-side failure (network, HTTP, schema)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _request_json(
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    """GET ``url`` (or POST ``payload`` as JSON) and return the decoded body."""
    method = "GET" if payload is None else "POST"
    request_headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request_headers.update(headers or {})

    req = urllib.request.Request(
        url, data=body, headers=request_headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise ProviderError(
            f"{method} {url} -> HTTP {exc.code} {exc.reason}", status=exc.code
        ) from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"{method} {url} -> {exc.reason}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"{method} {url} returned non-JSON body: {exc}") from exc


def _get_json(url: str, *, headers: dict[str, str] | None = None) -> Any:
    return _request_json(url, headers=headers)


def _post_json(url: str, payload: dict[str, Any]) -> Any:
    return _request_json(url, payload=payload)


# === 3. Provider clients ==================================================

# --- AniList ---------------------------------------------------------------

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

_ANIDB_URL_RE = re.compile(r"anidb\.net/anime/(\d+)")


def search_anilist(name: str) -> dict[str, Any] | None:
    """Search AniList for ``name``; return the top match dict or ``None``."""
    data = _post_json(
        ANILIST_URL, {"query": ANILIST_QUERY, "variables": {"search": name}}
    )
    if "errors" in data:
        raise ProviderError(f"AniList GraphQL errors: {data['errors']}")
    return (data.get("data") or {}).get("Media")


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
        url = link.get("url") or ""
        if "anidb" not in site and "anidb.net" not in url:
            continue
        match = _ANIDB_URL_RE.search(url)
        if match:
            return int(match.group(1))
        digits = "".join(ch for ch in url if ch.isdigit())
        if digits:
            return int(digits)
    return None


def anilist_title(anilist: dict[str, Any] | None) -> str | None:
    """Pick the display title: English, then romaji, then native."""
    titles = (anilist or {}).get("title") or {}
    return titles.get("english") or titles.get("romaji") or titles.get("native")


# --- animap.id -------------------------------------------------------------


def _normalize_title(value: str) -> str:
    """Casefold a title and reduce punctuation/whitespace runs to single spaces."""
    return re.sub(r"[\W_]+", " ", value.casefold()).strip()


def search_animap(name: str, *, limit: int = 5) -> list[dict[str, Any]]:
    """Search animap.id for ``name``; return the raw result list.

    animap responds with a JSON array ordered by relevance (exact title
    matches first, then mapped entries, then unmapped AniDB entries).
    """
    params = {"q": name, "limit": str(max(1, min(100, limit)))}
    data = _get_json(f"{ANIMAP_SEARCH_URL}?{urllib.parse.urlencode(params)}")
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def animap_exact_match(item: dict[str, Any] | None, query: str) -> bool:
    """Return True if ``item['titles']`` has an exact normalized match for ``query``."""
    if not item:
        return False
    wanted = _normalize_title(query)
    return any(
        isinstance(title, str) and _normalize_title(title) == wanted
        for title in item.get("titles") or []
    )


def pick_animap_result(
    results: list[dict[str, Any]], query: str
) -> dict[str, Any] | None:
    """Pick the best animap search result for ``query``.

    Prefer the first result whose ``titles`` contains an exact normalized
    title match; otherwise fall back to the first result. This keeps queries
    like ``Solo Leveling Season 2`` from selecting the season 1 entry.
    """
    for item in results:
        if animap_exact_match(item, query):
            return item
    return results[0] if results else None


def get_animap_map(mal_id: int) -> dict[str, Any] | None:
    """Fetch the animap cross-ID record for ``mal_id``.

    ``/api/map/mal/{id}`` is the legacy-but-documented route and returns a
    one-element JSON array. A 404 means animap has no map entry for that MAL
    id; every other provider failure is re-raised.
    """
    try:
        data = _get_json(f"{ANIMAP_MAP_URL}/mal/{mal_id}")
    except ProviderError as exc:
        if exc.status == 404:
            return None
        raise
    if isinstance(data, list):
        return data[0] if data and isinstance(data[0], dict) else None
    return data if isinstance(data, dict) else None


def _positive_int(value: Any) -> int | None:
    """Coerce an int or numeric string to a positive int, else ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, int) and value > 0:
        return value
    return None


def _as_list(raw: Any) -> list[Any]:
    if raw is None:
        return []
    return raw if isinstance(raw, list) else [raw]


def animap_ids(raw: Any) -> list[int]:
    """Extract positive integer ids from an animap field.

    animap fields may be a scalar or a list, and may arrive as ints or
    numeric strings depending on the endpoint.
    """
    ids = (_positive_int(value) for value in _as_list(raw))
    return [i for i in ids if i is not None]


def animap_tmdb_tv_ids(raw: Any) -> list[int]:
    """Extract positive TMDB ids of type ``tv`` from animap's tmdb field."""
    ids = (
        _positive_int(entry.get("id"))
        for entry in _as_list(raw)
        if isinstance(entry, dict) and entry.get("type") == "tv"
    )
    return [i for i in ids if i is not None]


def animap_display_title(record: dict[str, Any]) -> str | None:
    """Best human-readable title in an animap record.

    ``/api/search`` fallback records carry no ``title``, so fall back to the
    first of ``titles`` so the header still names the match.
    """
    for candidate in [record.get("title"), *(record.get("titles") or [])]:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


# --- Kitsu -----------------------------------------------------------------


def search_kitsu(name: str) -> dict[str, Any] | None:
    """Search Kitsu for ``name``; return the top anime resource or ``None``."""
    params = {"filter[text]": name, "page[limit]": "1"}
    data = _get_json(
        f"{KITSU_URL}/anime?{urllib.parse.urlencode(params)}", headers=KITSU_HEADERS
    )
    items = data.get("data") or []
    return items[0] if items else None


def get_kitsu_bridges(kitsu_id: str) -> dict[str, str]:
    """Fetch all external-id bridges for ``kitsu_id`` in one call.

    Kitsu's ``/anime/{id}/mappings`` returns every external site it knows
    about (e.g. ``anidb``, ``thetvdb``, ``anilist/anime``,
    ``myanimelist/anime``, ``trakt``, ``hulu``). This is the most reliable
    source for both AniDB and TVDB ids — they are extracted from this one
    response rather than via separate provider calls.

    Returns ``{externalSite: externalId}`` as strings; empty if the show has
    no mappings.
    """
    data = _get_json(f"{KITSU_URL}/anime/{kitsu_id}/mappings", headers=KITSU_HEADERS)
    bridges: dict[str, str] = {}
    for mapping in data.get("data") or []:
        attrs = mapping.get("attributes") or {}
        site, ext_id = attrs.get("externalSite"), attrs.get("externalId")
        if site and ext_id is not None:
            bridges[str(site)] = str(ext_id)
    return bridges


# --- TMDB ------------------------------------------------------------------


def search_tmdb(name: str, api_key: str) -> dict[str, Any] | None:
    """Search TMDB v3 for ``name``; return the top TV series result or ``None``."""
    params = {"api_key": api_key, "query": name, "include_adult": "false"}
    data = _get_json(f"{TMDB_URL}/search/tv?{urllib.parse.urlencode(params)}")
    results = data.get("results") or []
    return results[0] if results else None


def get_tmdb_tvdb(tmdb_id: int, api_key: str) -> int | None:
    """Fetch the TVDB series id for a TMDB TV show via ``/external_ids``."""
    key = urllib.parse.quote(api_key, safe="")
    try:
        data = _get_json(f"{TMDB_URL}/tv/{tmdb_id}/external_ids?api_key={key}")
    except ProviderError as exc:
        if exc.status == 404:  # "no external ids for this show"
            return None
        raise
    return _positive_int(data.get("tvdb_id"))


# === 4. Data model ========================================================


@dataclass
class Slot:
    """One resolved id plus a note on which provider supplied it."""

    value: int | str | None = None
    source: str | None = None

    def set(self, value: int | str, source: str | None) -> None:
        self.value = value
        self.source = source


@dataclass
class AnimapMatch:
    """The animap record chosen for a query, with how it was obtained."""

    record: dict[str, Any]
    source: str
    mal_id: int | None
    exact_query: str | None  # the query that exact-matched, if any
    title: str | None
    year: int | None

    def candidates(self, slot: str) -> list[int]:
        """All ids animap offers for ``slot``, best first."""
        raw = self.record.get(ANIMAP_KEYS[slot])
        return animap_tmdb_tv_ids(raw) if slot == "tmdb" else animap_ids(raw)


@dataclass
class ResolvedShow:
    """All ids discovered for a show, plus provenance and diagnostics."""

    query: str
    title: str | None = None
    anilist: Slot = field(default_factory=Slot)
    mal: Slot = field(default_factory=Slot)
    kitsu: Slot = field(default_factory=Slot)
    anidb: Slot = field(default_factory=Slot)
    tvdb: Slot = field(default_factory=Slot)
    tmdb: Slot = field(default_factory=Slot)
    animap: AnimapMatch | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_any(self) -> bool:
        return any(getattr(self, name).value is not None for name in ANIMAP_KEYS)

    def pick_animap_id(
        self, slot: str, candidates: list[int], *, current: int | None = None
    ) -> int | None:
        """Choose the first candidate, warning when animap supplied alternatives.

        ``current`` is the value already held for the slot (if any); the
        alternatives warning is suppressed when the first candidate does not
        change it.
        """
        if not candidates:
            return None
        if len(candidates) > 1 and candidates[0] != current:
            rendered = ", ".join(str(c) for c in candidates)
            self.warnings.append(
                f"animap returned multiple {slot} ids ({rendered}); "
                f"using {candidates[0]}"
            )
        return candidates[0]

    def fill_from_animap(self, slot: str, *, force: bool = False) -> None:
        """Fill ``slot`` from animap, unless already set (or ``force`` it)."""
        target: Slot = getattr(self, slot)
        if self.animap is None or (target.value is not None and not force):
            return
        chosen = self.pick_animap_id(slot, self.animap.candidates(slot))
        if chosen is not None:
            target.set(str(chosen) if slot == "kitsu" else chosen, self.animap.source)


# === 5. Resolution pipeline ===============================================


def resolve(
    name: str, *, tmdb_key: str | None, use_animap: bool = True
) -> ResolvedShow:
    """Run the lookup pipeline and aggregate the results.

    Resolution order:

    1. AniList search — default canonical anchor. Supplies anilist_id and
       mal_id (``idMal``), plus the ``# Title:`` display line unless animap
       wins the exact-title conflict (then animap's title is used); its
       ``externalLinks`` are scanned for anidb per the AniDB chain.
    2. animap.id search — supplies curated cross-IDs keyed by MAL id. Its
       ``/api/map/mal/{id}`` record is the preferred source for anidb, tvdb,
       and tmdb, and a fallback for anilist, mal, and kitsu. When the picked
       result is an exact title match whose MAL id disagrees with AniList's,
       animap becomes authoritative for anilist/mal/kitsu as well.
    3. Kitsu search — supplies kitsu_id. One follow-up call to
       ``/anime/{kitsu_id}/mappings`` returns the bridge dict used for both
       anidb and thetvdb.
    4. TMDB search — supplies tmdb_id only when animap didn't. Its
       ``/tv/{tmdb_id}/external_ids`` is the last-resort TVDB fallback.

    The AniDB chain runs **animap → AniList → Kitsu**. The TVDB chain runs
    **animap → Kitsu → TMDB**.
    """
    result = ResolvedShow(query=name)

    anilist = _seed_from_anilist(result, name)
    if use_animap:
        _attach_animap(result, name)
    animap_authoritative = _reconcile_anchors(result)
    bridges = _resolve_kitsu(result, name, skip_search=animap_authoritative)
    _resolve_anidb(result, anilist, bridges)
    _resolve_tmdb(result, name, tmdb_key)
    _resolve_tvdb(result, bridges, tmdb_key)
    return result


def _seed_from_anilist(result: ResolvedShow, name: str) -> dict[str, Any] | None:
    """Step 1: AniList supplies the title, anilist id, and MAL id."""
    try:
        anilist = search_anilist(name)
    except ProviderError as exc:
        result.errors.append(f"AniList: {exc}")
        return None
    if anilist is None:
        return None

    result.title = anilist_title(anilist)
    if isinstance(anilist.get("id"), int):
        result.anilist.set(anilist["id"], "AniList search")
    if isinstance(anilist.get("idMal"), int):
        result.mal.set(anilist["idMal"], "AniList idMal")
    return anilist


def _attach_animap(result: ResolvedShow, name: str) -> None:
    """Step 2: search animap, then fetch its cross-ID record for the MAL id."""
    picked: dict[str, Any] | None = None
    exact_query: str | None = None
    try:
        picked = pick_animap_result(search_animap(name), name)
        if animap_exact_match(picked, name):
            exact_query = name
        if picked is None and result.title and result.title != name:
            picked = pick_animap_result(search_animap(result.title), result.title)
            if animap_exact_match(picked, result.title):
                exact_query = result.title
    except ProviderError as exc:
        result.errors.append(f"animap search: {exc}")
    if picked is None:
        return

    mal_id = _positive_int(picked.get("mal_id"))
    record: dict[str, Any] | None = None
    if mal_id is not None:
        try:
            record = get_animap_map(mal_id)
        except ProviderError as exc:
            result.errors.append(f"animap map: {exc}")
    if record is not None:
        source = f"animap /api/map/mal/{mal_id}"
    else:
        record, source = picked, "animap /api/search"

    year = record.get("year")
    result.animap = AnimapMatch(
        record=record,
        source=source,
        mal_id=mal_id,
        exact_query=exact_query,
        title=animap_display_title(record),
        year=year if isinstance(year, int) else None,
    )


def _reconcile_anchors(result: ResolvedShow) -> bool:
    """Settle anilist/mal ids between AniList and animap.

    AniList's fuzzy top hit can be a different work (e.g. a mini-anime)
    while animap found the real series by exact title. In that case animap
    wins the whole record so the emitted ids stay self-consistent. Otherwise
    animap only fills anchor ids AniList did not supply.

    Returns True when animap was made authoritative.
    """
    match = result.animap
    if match is None:
        return False

    authoritative = (
        match.exact_query is not None
        and match.mal_id is not None
        and result.mal.value is not None
        and match.mal_id != result.mal.value
    )
    if authoritative:
        result.warnings.append(
            f"animap exact match {match.exact_query!r} (mal {match.mal_id}) "
            f"disagrees with AniList top match (mal {result.mal.value}); "
            "using animap ids"
        )
        # Follow animap's title too, so the emitted ``# Title:``/``title:``
        # lines name the record the ids actually belong to.
        if match.title:
            result.title = match.title
        result.fill_from_animap("anilist", force=True)
        result.mal.set(match.mal_id, match.source)
        result.fill_from_animap("kitsu", force=True)

    result.fill_from_animap("anilist")
    result.fill_from_animap("mal")
    return authoritative


def _fetch_bridges(result: ResolvedShow) -> dict[str, str]:
    """Kitsu bridge dict for the current kitsu id; ``{}`` on failure."""
    if result.kitsu.value is None:
        return {}
    try:
        return get_kitsu_bridges(str(result.kitsu.value))
    except ProviderError as exc:
        result.errors.append(f"Kitsu mappings: {exc}")
        return {}


def _resolve_kitsu(
    result: ResolvedShow, name: str, *, skip_search: bool
) -> dict[str, str]:
    """Step 3: settle the kitsu id and return its bridge dict.

    When animap is authoritative its kitsu id is trusted, so Kitsu's name
    search is skipped and bridges are read for the animap id directly.
    """
    if not (skip_search and result.kitsu.value is not None):
        try:
            kitsu = search_kitsu(result.title or name)
        except ProviderError as exc:
            result.errors.append(f"Kitsu: {exc}")
            kitsu = None
        kid = kitsu.get("id") if kitsu else None
        if isinstance(kid, str) and kid.isdigit() and result.kitsu.value is None:
            result.kitsu.set(kid, "Kitsu search")

    bridges = _fetch_bridges(result)

    # Kitsu's name search occasionally tops with a duplicate recap/special
    # whose own MAL bridge points at a different work than the canonical
    # (AniList/animap) MAL id. This is the secondary safety net; the animap
    # exact-match conflict is handled first, so the two never both swap
    # kitsu for the same record.
    match = result.animap
    kitsu_mal = bridges.get("myanimelist/anime")
    if (
        match is not None
        and result.kitsu.source == "Kitsu search"
        and result.mal.value is not None
        and kitsu_mal
        and kitsu_mal != str(result.mal.value)
    ):
        current = _positive_int(result.kitsu.value)
        chosen = result.pick_animap_id(
            "kitsu", match.candidates("kitsu"), current=current
        )
        if chosen is not None and str(chosen) != result.kitsu.value:
            result.kitsu.set(str(chosen), match.source)
            bridges = _fetch_bridges(result)

    result.fill_from_animap("kitsu")
    return bridges


def _resolve_anidb(
    result: ResolvedShow, anilist: dict[str, Any] | None, bridges: dict[str, str]
) -> None:
    """AniDB chain: animap → AniList externalLinks → Kitsu mappings."""
    result.fill_from_animap("anidb")
    if result.anidb.value is None:
        found = get_anilist_anidb(anilist)
        if found is not None:
            result.anidb.set(found, "AniList externalLinks")
    if result.anidb.value is None:
        raw = bridges.get("anidb")
        if raw and raw.isdigit():
            result.anidb.set(int(raw), "Kitsu mappings (externalSite=anidb)")


def _resolve_tmdb(result: ResolvedShow, name: str, tmdb_key: str | None) -> None:
    """TMDB chain: animap (``type == "tv"``) → TMDB ``/search/tv``."""
    result.fill_from_animap("tmdb")
    if result.tmdb.value is not None or not tmdb_key:
        return
    try:
        tmdb = search_tmdb(result.title or name, tmdb_key)
    except ProviderError as exc:
        result.errors.append(f"TMDB: {exc}")
        return
    if tmdb is not None and isinstance(tmdb.get("id"), int):
        result.tmdb.set(tmdb["id"], "TMDB /search/tv")


def _resolve_tvdb(
    result: ResolvedShow, bridges: dict[str, str], tmdb_key: str | None
) -> None:
    """TVDB chain: animap → Kitsu mappings → TMDB ``external_ids``."""
    result.fill_from_animap("tvdb")
    if result.tvdb.value is None:
        raw = bridges.get("thetvdb")
        if raw and raw.isdigit():
            result.tvdb.set(int(raw), "Kitsu mappings (externalSite=thetvdb)")
    if result.tvdb.value is None and result.tmdb.value is not None and tmdb_key:
        try:
            tvdb = get_tmdb_tvdb(int(result.tmdb.value), tmdb_key)
        except ProviderError as exc:
            result.errors.append(f"TMDB external_ids: {exc}")
            return
        if tvdb is not None:
            result.tvdb.set(tvdb, "TMDB /tv/{tmdb_id}/external_ids")


# === 6. YAML rendering ====================================================

# (slot attribute, label shown in output) in display order.
SOURCE_ROWS = (
    ("anilist", "anilist"),
    ("mal", "mal"),
    ("kitsu", "kitsu"),
    ("anidb", "anidb"),
    ("tvdb", "tvdb_show"),
    ("tmdb", "tmdb_show"),
)


def _yaml_value(value: Any, suffix: str = "") -> str:
    return "null" if value is None else f"{value}{suffix}"


def _animap_header(result: ResolvedShow) -> str | None:
    match = result.animap
    if match is None or not match.title:
        return None
    detail = match.title
    if match.year is not None:
        detail += f" ({match.year})"
    route = match.source
    if route.startswith("animap "):
        route = route[len("animap "):]
    return f"# animap: {detail} via {route}"


def _snippet_lines(result: ResolvedShow) -> list[str]:
    """The commented ``mapping.yml`` skeleton (empty without a TMDB id)."""
    if result.tmdb.value is None:
        return []

    def entry(key: str, indent: str) -> list[str]:
        return [f"#{indent}{key}:", f"#{indent}  EPISODES: EPISODES"]

    lines = ["#", f"#   tmdb_show:{result.tmdb.value}:s1:"]
    if result.anidb.value is not None:
        lines += entry(f"anidb:{result.anidb.value}:R", "     ")
    for slot in ("anilist", "mal", "kitsu"):
        value = getattr(result, slot).value
        if value is not None:
            lines += entry(f"{slot}:{value}", "     ")
    if result.tvdb.value is not None:
        lines += entry(f"tvdb_show:{result.tvdb.value}:s1", "     ")
    return lines


def emit_yaml(result: ResolvedShow) -> str:
    """Render the resolved show as a pasteable YAML block + comment header."""
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# Resolved {when} for query: {result.query!r}"]
    if result.title:
        lines.append(f"# Title: {result.title}")
    animap_line = _animap_header(result)
    if animap_line:
        lines.append(animap_line)

    lines += ["#", "# Sources:"]
    for slot, label in SOURCE_ROWS:
        found = getattr(result, slot)
        if found.value is not None:
            lines.append(f"#   {label:<10} {found.value}  ({found.source})")
        else:
            lines.append(f"#   {label:<10} (not found)")

    lines.append("#")
    lines.append("# Suggested mapping.yml snippet (replace EPISODES with the real range):")
    lines += _snippet_lines(result)
    lines.append("")

    # Flat ID-only YAML for piping / pasting.
    lines += [
        f"title: {result.title or result.query}",
        f"tmdb_show: {_yaml_value(result.tmdb.value, ':s1')}",
        f"anidb: {_yaml_value(result.anidb.value, ':R')}",
        f"anilist: {_yaml_value(result.anilist.value)}",
        f"mal: {_yaml_value(result.mal.value)}",
        f"kitsu: {_yaml_value(result.kitsu.value)}",
        f"tvdb_show: {_yaml_value(result.tvdb.value, ':s1')}",
    ]

    if result.errors:
        lines += ["", "# Errors during lookup (printed to stderr too):"]
        lines += [f"#   - {err}" for err in result.errors]
    if result.warnings:
        lines += ["", "# Warnings:"]
        lines += [f"#   - {warning}" for warning in result.warnings]

    return "\n".join(lines) + "\n"


# === 7. CLI ===============================================================


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Look up AniBridge target IDs (tmdb_show, tvdb_show, anidb, "
            "anilist, mal, kitsu) for an anime show name using AniList, "
            "animap.id, Kitsu, and TMDB, then print them as YAML."
        ),
    )
    parser.add_argument(
        "name", help="Anime show name to search for (e.g. 'Overgeared')."
    )
    parser.add_argument(
        "--tmdb-key",
        help=(
            "TMDB v3 API key. Overrides the config file and the "
            "TMDB_API_KEY env var."
        ),
    )
    parser.add_argument(
        "--no-animap",
        action="store_true",
        help=(
            "Disable the animap.id cross-ID source. animap supplies curated "
            "AniDB/TVDB/TMDB ids and is enabled by default."
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


def _force_utf8_streams() -> None:
    """Make stdout/stderr UTF-8 so non-ASCII titles never crash a legacy console.

    Provider titles can contain characters outside the console's default code
    page (e.g. AniList returns ``Sousou no Frieren: ●● no Mahou`` on Windows
    cp1252); without this, ``sys.stdout.write`` raises ``UnicodeEncodeError``.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; return the process exit code."""
    _force_utf8_streams()
    args = build_parser().parse_args(argv)

    tmdb_key = resolve_tmdb_key(args)
    if tmdb_key is None:
        print(
            "Warning: TMDB API key not configured; TMDB is only used as a "
            "fallback. tmdb_show and tvdb_show may still resolve via "
            "animap. Provide --tmdb-key, set [tmdb] api_key in the config "
            "file, or set TMDB_API_KEY to add TMDB lookups.",
            file=sys.stderr,
        )

    result = resolve(args.name, tmdb_key=tmdb_key, use_animap=not args.no_animap)

    if not result.has_any:
        print(f"Error: no ids found for {args.name!r} across any provider.", file=sys.stderr)
        return 1

    sys.stdout.write(emit_yaml(result))
    for err in result.errors:
        print(f"error: {err}", file=sys.stderr)
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())