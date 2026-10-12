#!/usr/bin/env python3
"""Combine AniBridge mappings and generate CrossWatch custom overrides.

This script walks every ``mappings/<shows|movies>/<Title>/mapping.yml`` file,
merges them into a single ``mappings/mappings.yml`` (the AniBridge combined
format), a minified JSON mirror ``mappings/mappings.min.json``, and emits
``mappings/crosswatch-overrides.json`` (the CrossWatch override format).

The combined YAML/JSON includes a ``$meta`` block at the top describing this
collection (schema version and human-readable description). The
human-readable title for each media item is taken from the parent folder
name — e.g. ``mappings/shows/Overgeared/mapping.yml`` yields override rules
with ``"title": "Overgeared"``.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterator, TypedDict

import yaml

# === Configuration ==========================================================

SCHEMA_URL = (
    "https://raw.githubusercontent.com/anibridge/"
    "anibridge-mappings/refs/heads/main/mappings.schema.json"
)

MAPPINGS_DIR = Path("mappings")
MAPPING_TYPES: tuple[str, ...] = ("shows", "movies")

COMBINED_OUTPUT = MAPPINGS_DIR / "mappings.yml"
COMBINED_MIN_JSON_OUTPUT = MAPPINGS_DIR / "mappings.min.json"
CROSSWATCH_OUTPUT = MAPPINGS_DIR / "crosswatch-overrides.json"

# Metadata prepended to the combined YAML/JSON as a ``$meta`` block.
COMBINED_META: dict[str, str] = {
    "schema_version": "3.0.0",
    "description": "Journey Custom AniBridge Mapping",
}

# === AniBridge key syntax ===================================================

# AniBridge source keys, e.g.:
#   tmdb_show:324502:s1
#   tmdb_movie:16516
SOURCE_RE = re.compile(
    r"^(?P<provider>[a-z0-9]+)_"
    r"(?P<kind>show|movie):"
    r"(?P<id>[^:]+)"
    r"(?::s(?P<season>\d+))?$"
)

# AniBridge target keys, e.g.:
#   anilist:212888
#   mal:64340
#   anidb:20077:R
TARGET_RE = re.compile(
    r"^(?P<namespace>anidb|anilist|mal|simkl|kitsu):"
    r"(?P<id>[^:]+)"
    r"(?::.*)?$"
)

# Providers CrossWatch can match against.
SUPPORTED_MATCH_PROVIDERS: frozenset[str] = frozenset(
    {"tvdb", "tmdb", "imdb", "anidb", "mal", "anilist", "simkl", "kitsu"}
)

# Target namespaces that are only relevant to CrossWatch overrides and must
# not appear in the combined AniBridge YAML/JSON output.
CROSSWATCH_ONLY_TARGET_NAMESPACES: frozenset[str] = frozenset({"kitsu"})

# === Helpers ===============================================================


class _NoAliasDumper(yaml.SafeDumper):
    """PyYAML dumper that never emits anchors (``&id001``) or aliases.

    Swap-source entries reuse the originals' episode-map dicts, so the
    default dumper would emit ``&id001`` / ``*id001`` references — those
    are valid YAML but make diffs and hand-edits harder.
    """

    def ignore_aliases(self, data: Any) -> bool:  # noqa: D401 - PyYAML API
        return True


def is_internal_key(key: Any) -> bool:
    """Return True for AniBridge-internal keys that should not become sources.

    AniBridge reserves top-level keys prefixed with ``$`` (e.g. ``$meta``) for
    schema/configuration metadata. They must not be merged into the combined
    YAML or fed to the override generator.
    """
    return isinstance(key, str) and key.startswith("$")


def parse_range(value: Any) -> tuple[int, int]:
    """Parse an AniBridge episode range such as ``1``, ``"1"``, or ``"1-12"``."""
    # bool is a subclass of int; reject it explicitly.
    if isinstance(value, bool):
        raise ValueError(f"Invalid episode range type: {value!r}")
    if isinstance(value, int):
        start = end = value
    elif isinstance(value, str):
        match = re.fullmatch(r"\s*(\d+)(?:\s*-\s*(\d+))?\s*", value)
        if not match:
            raise ValueError(f"Invalid episode range: {value!r}")
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else start
    else:
        raise ValueError(f"Invalid episode range type: {value!r}")

    if start < 1 or end < start:
        raise ValueError(f"Invalid episode range: {value!r}")

    return start, end


def parse_source_key(source_key: Any, *, origin: Path | None = None) -> dict | None:
    """Parse an AniBridge source key into ``{provider, kind, id, season}``."""
    if not isinstance(source_key, str):
        print(
            f"Warning: source key is not a string: {source_key!r}",
            file=sys.stderr,
        )
        return None
    match = SOURCE_RE.fullmatch(source_key)
    if not match:
        suffix = f" in {origin}" if origin is not None else ""
        print(
            f"Warning: unrecognized AniBridge source key: "
            f"{source_key!r}{suffix}",
            file=sys.stderr,
        )
        return None

    parts = match.groupdict()
    parts["season"] = (
        int(parts["season"]) if parts["season"] is not None else None
    )
    return parts


def format_source_key(parts: dict[str, Any]) -> str:
    """Render a parsed source ``{provider, kind, id, season}`` back to a key.

    Inverse of :func:`parse_source_key`: ``tvdb_show:475617:s1`` ↔
    ``{"provider": "tvdb", "kind": "show", "id": "475617", "season": 1}``.
    """
    key = f"{parts['provider']}_{parts['kind']}:{parts['id']}"
    if parts.get("season") is not None:
        key += f":s{parts['season']}"
    return key


def classify_target(target_key: Any) -> str:
    """Classify an AniBridge target key.

    Returns one of:

    * ``"crosswatch"`` — valid CrossWatch override target namespace
      (``anidb``, ``anilist``, ``mal``, ``simkl``, ``kitsu``).
    * ``"swap"`` — valid AniBridge source reference (``provider_kind:id``
      with optional ``:sN``) that should be promoted to its own source
      entry in the combined output and skipped for CrossWatch overrides.
    * ``"unsupported"`` — malformed or otherwise unrecognised.
    """
    if not isinstance(target_key, str):
        return "unsupported"
    if TARGET_RE.fullmatch(target_key):
        return "crosswatch"
    if SOURCE_RE.fullmatch(target_key):
        return "swap"
    return "unsupported"


def parse_swap_source(target_key: Any) -> dict | None:
    """Parse an AniBridge source-formatted target into its components.

    Returns ``{provider, kind, id, season}`` (with ``season`` as ``int | None``)
    for keys that match :data:`SOURCE_RE`, otherwise ``None``.
    """
    if not isinstance(target_key, str):
        return None
    match = SOURCE_RE.fullmatch(target_key)
    if not match:
        return None
    parts = match.groupdict()
    parts["season"] = (
        int(parts["season"]) if parts["season"] is not None else None
    )
    return parts


def parse_target_key(target_key: Any, *, origin: Path | None = None) -> dict | None:
    """Parse an AniBridge target key into a CrossWatch ``{namespace, id}``.

    Swap-source targets (e.g. ``tvdb_show:475617:s1``) return ``None``
    silently because they are valid AniBridge references rather than
    CrossWatch target namespaces. Truly malformed targets still emit a
    warning so the user is alerted to a typo.
    """
    if not isinstance(target_key, str):
        print(
            f"Warning: target key is not a string: {target_key!r}",
            file=sys.stderr,
        )
        return None
    match = TARGET_RE.fullmatch(target_key)
    if match:
        return match.groupdict()
    # Treat valid AniBridge source references as silent swap targets; only
    # warn when the key is genuinely malformed.
    if SOURCE_RE.fullmatch(target_key):
        return None
    suffix = f" in {origin}" if origin is not None else ""
    print(
        f"Warning: unsupported CrossWatch target namespace in "
        f"{target_key!r}{suffix}; skipping target",
        file=sys.stderr,
    )
    return None


def stable_override_id(identity: dict[str, Any]) -> str:
    """Derive a deterministic CrossWatch override ID from the rule identity."""
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    return f"ovr_{digest}"


# === Output shapes =========================================================


class Override(TypedDict, total=False):
    """Shape of a single CrossWatch override rule."""

    id: str
    created_at: int
    updated_at: int
    enabled: bool
    media_type: str  # "show" | "movie"
    match_provider: str
    match_id: str
    match_season: int | None
    episode_from: int | None
    episode_to: int | None
    episode_start_at: int | None
    target_namespace: str
    target_id: str
    title: str
    note: str


# === Discovery + combining =================================================


def iter_mapping_files() -> Iterator[tuple[Path, str]]:
    """Yield ``(mapping_file, title)`` for every ``<shows|movies>/<Title>``.

    ``title`` is the parent folder name, which is treated as the
    human-readable title for the media.
    """
    for dir_name in MAPPING_TYPES:
        type_dir = MAPPINGS_DIR / dir_name
        if not type_dir.is_dir():
            continue
        for entry in sorted(type_dir.iterdir(), key=lambda p: p.name):
            if not entry.is_dir():
                continue
            mapping_file = entry / "mapping.yml"
            if not mapping_file.is_file():
                print(
                    f"Warning: skipping {entry}: no mapping.yml inside",
                    file=sys.stderr,
                )
                continue
            yield mapping_file, entry.name


def combine_mappings() -> tuple[dict[str, Any], dict[str, str], bool]:
    """Merge every mapping file into a single dict keyed by source key.

    Returns ``(combined, titles, had_error)``:

    * ``combined``: ``source_key -> targets_dict`` (AniBridge combined YAML).
    * ``titles``:   ``source_key -> human-readable title`` (folder name).
    * ``had_error``: ``True`` if any mapping file failed to parse.
    """
    combined: dict[str, Any] = {}
    titles: dict[str, str] = {}
    had_error = False

    for mapping_file, title in iter_mapping_files():
        print(f"Processing {mapping_file}")
        try:
            data = yaml.safe_load(mapping_file.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            print(
                f"Error: failed to parse {mapping_file}: {exc}",
                file=sys.stderr,
            )
            had_error = True
            continue
        except OSError as exc:
            print(
                f"Error: cannot read {mapping_file}: {exc}",
                file=sys.stderr,
            )
            had_error = True
            continue

        if not data:
            print(
                f"Warning: {mapping_file} is empty; skipping",
                file=sys.stderr,
            )
            continue
        if not isinstance(data, dict):
            print(
                f"Error: {mapping_file} must contain a YAML mapping at "
                f"the top level (got {type(data).__name__})",
                file=sys.stderr,
            )
            had_error = True
            continue

        for source_key, targets in data.items():
            if is_internal_key(source_key):
                # Skip AniBridge metadata blocks like `$meta:`.
                continue
            if source_key in combined:
                print(
                    f"Warning: duplicate source key {source_key!r} in "
                    f"{mapping_file} (previous entry kept)",
                    file=sys.stderr,
                )
                continue
            combined[source_key] = targets
            titles[source_key] = title

    return combined, titles, had_error


def expand_swap_sources(
    combined: dict[str, Any],
    titles: dict[str, str],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Expand ``combined`` by generating source entries for swap targets.

    For every source ``S`` in ``combined``, any target that parses as a
    valid AniBridge source but is *not* a CrossWatch target namespace
    (i.e. a swap target like ``tvdb_show:475617:s1``) becomes its own
    source entry. The generated entry mirrors ``S`` with the swap target
    swapped out and the original ``S`` appended as a target.

    Example: given ``tmdb_show:320377:s1`` whose targets include
    ``tvdb_show:475617:s1: 1-12: 1-12``, this function adds a
    ``tvdb_show:475617:s1`` source entry containing the same real
    targets (anidb/anilist/mal/kitsu with their original ranges) plus a
    ``tmdb_show:320377:s1: 1-12: 1-12`` back-reference.

    Existing entries (user-defined or generated earlier in this run) win
    over newly-generated swaps; the same first-occurrence-wins rule used
    by :func:`combine_mappings` applies.
    """
    expanded: dict[str, Any] = dict(combined)
    expanded_titles: dict[str, str] = dict(titles)
    generated = 0

    for source_key, targets in combined.items():
        if not isinstance(targets, dict):
            continue
        title = titles.get(source_key, "")

        # Collect swap targets in this source, in source order.
        swap_targets: list[tuple[str, Any]] = []
        for target_key, episode_map in targets.items():
            if classify_target(target_key) != "swap":
                continue
            swap_key = format_source_key(parse_swap_source(target_key))
            if swap_key == source_key:
                print(
                    f"Warning: source {source_key!r} references itself "
                    f"as a swap target; skipping swap",
                    file=sys.stderr,
                )
                continue
            swap_targets.append((swap_key, episode_map))

        if not swap_targets:
            continue

        # Build the base target dict (CrossWatch targets from the original
        # source, in their original order). Swap targets from the original
        # source are excluded — the generated entry shouldn't carry them
        # because CrossWatch would not emit them as override targets.
        base_targets: dict[str, Any] = {
            other_key: other_map
            for other_key, other_map in targets.items()
            if classify_target(other_key) == "crosswatch"
        }

        for swap_key, episode_map in swap_targets:
            if swap_key in expanded:
                # Existing entry (user-defined or generated earlier) wins.
                continue
            entry = dict(base_targets)
            entry[source_key] = episode_map
            expanded[swap_key] = entry
            expanded_titles[swap_key] = title
            generated += 1
            print(
                f"Generated swap source entry: {swap_key} (from {source_key})",
                file=sys.stderr,
            )

    if generated:
        print(f"Generated {generated} swap source entr{'y' if generated == 1 else 'ies'}")

    return expanded, expanded_titles


# === Override generation ===================================================


def _make_override(
    *,
    media_type: str,
    match_provider: str,
    match_id: str,
    match_season: int | None,
    episode_from: int | None,
    episode_to: int | None,
    episode_start_at: int | None,
    target_namespace: str,
    target_id: str,
    title: str,
    timestamp: int,
    created_at: int | None = None,
    note: str = "",
) -> Override:
    """Build a CrossWatch override dict with a deterministic ID.

    ``created_at`` defaults to ``timestamp`` (current run time). Pass an
    explicit value to preserve a previously-recorded ``created_at`` when
    re-running the script against an existing overrides file.
    """
    identity: dict[str, Any] = {
        "media_type": media_type,
        "match_provider": match_provider,
        "match_id": match_id,
        "match_season": match_season,
        "episode_from": episode_from,
        "episode_to": episode_to,
        "episode_start_at": episode_start_at,
        "target_namespace": target_namespace,
        "target_id": target_id,
    }
    return Override(
        id=stable_override_id(identity),
        created_at=created_at if created_at is not None else timestamp,
        updated_at=timestamp,
        enabled=True,
        media_type=media_type,
        match_provider=match_provider,
        match_id=match_id,
        match_season=match_season,
        episode_from=episode_from,
        episode_to=episode_to,
        episode_start_at=episode_start_at,
        target_namespace=target_namespace,
        target_id=target_id,
        title=title,
        note=note,
    )


def _emit_movie_override(
    *,
    episode_map: dict,
    origin: Path,
    provider: str,
    source_id: str,
    target_namespace: str,
    target_id: str,
    source_key: Any,
    target_key: Any,
    title: str,
    timestamp: int,
) -> Override | None:
    """Build a single ID-only movie override, validating the episode ranges.

    The CrossWatch movie export format carries no episode coordinates, so any
    range other than ``1-1 -> 1-1`` is collapsed with a warning.
    """
    valid_ranges: list[tuple[int, int, int, int]] = []
    range_error = False

    for source_range, target_range in episode_map.items():
        try:
            source_start, source_end = parse_range(source_range)
            target_start, target_end = parse_range(target_range)
        except ValueError as exc:
            print(
                f"Error: {origin}: {source_key!r} -> {target_key!r}: {exc}",
                file=sys.stderr,
            )
            range_error = True
            continue

        if (source_end - source_start) != (target_end - target_start):
            print(
                f"Error: {origin}: incompatible range lengths for "
                f"{source_key!r} -> {target_key!r}: "
                f"{source_range!r} -> {target_range!r}",
                file=sys.stderr,
            )
            range_error = True
            continue

        valid_ranges.append((source_start, source_end, target_start, target_end))

    if range_error or not valid_ranges:
        return None

    simple_one_to_one = (
        len(valid_ranges) == 1 and valid_ranges[0] == (1, 1, 1, 1)
    )
    if not simple_one_to_one:
        print(
            f"Warning: {origin}: movie mapping {source_key!r} -> "
            f"{target_key!r} contains episode ranges that CrossWatch's "
            f"movie override format cannot preserve. Creating an ID-only "
            f"movie override.",
            file=sys.stderr,
        )

    return _make_override(
        media_type="movie",
        match_provider=provider,
        match_id=source_id,
        match_season=None,
        episode_from=None,
        episode_to=None,
        episode_start_at=None,
        target_namespace=target_namespace,
        target_id=target_id,
        title=title,
        timestamp=timestamp,
    )


def _emit_show_overrides(
    *,
    episode_map: dict,
    origin: Path,
    provider: str,
    source_id: str,
    season: int,
    target_namespace: str,
    target_id: str,
    source_key: Any,
    target_key: Any,
    title: str,
    timestamp: int,
) -> list[Override]:
    """Emit one override per source/target episode-range pair."""
    overrides: list[Override] = []
    for source_range, target_range in episode_map.items():
        try:
            source_start, source_end = parse_range(source_range)
            target_start, target_end = parse_range(target_range)
        except ValueError as exc:
            print(
                f"Error: {origin}: {source_key!r} -> {target_key!r}: {exc}",
                file=sys.stderr,
            )
            continue

        if (source_end - source_start) != (target_end - target_start):
            print(
                f"Error: {origin}: incompatible range lengths for "
                f"{source_key!r} -> {target_key!r}: "
                f"{source_range!r} -> {target_range!r}",
                file=sys.stderr,
            )
            continue

        overrides.append(
            _make_override(
                media_type="show",
                match_provider=provider,
                match_id=source_id,
                match_season=season,
                episode_from=source_start,
                episode_to=source_end,
                episode_start_at=target_start,
                target_namespace=target_namespace,
                target_id=target_id,
                title=title,
                timestamp=timestamp,
            )
        )
    return overrides


def convert_mapping(
    source_key: Any,
    targets: Any,
    *,
    title: str,
    origin: Path,
    timestamp: int,
) -> list[Override]:
    """Convert one AniBridge source entry into CrossWatch overrides.

    ``title`` is the human-readable title for the source media (the
    parent folder name).
    """
    source = parse_source_key(source_key, origin=origin)
    if source is None:
        return []

    provider = source["provider"]
    media_type = source["kind"]
    source_id = source["id"]
    season = source["season"]

    if provider not in SUPPORTED_MATCH_PROVIDERS:
        print(
            f"Warning: unsupported CrossWatch match provider "
            f"{provider!r} in {origin}; skipping {source_key!r}",
            file=sys.stderr,
        )
        return []

    if media_type == "show" and season is None:
        print(
            f"Warning: show mapping {source_key!r} in {origin} "
            f"has no season; CrossWatch show overrides require a season",
            file=sys.stderr,
        )
        return []

    if not isinstance(targets, dict):
        print(
            f"Error: targets for {source_key!r} in {origin} "
            f"are not a mapping (got {type(targets).__name__}); skipping",
            file=sys.stderr,
        )
        return []

    overrides: list[Override] = []
    for target_key, episode_map in targets.items():
        target = parse_target_key(target_key, origin=origin)
        if target is None:
            continue

        if not isinstance(episode_map, dict):
            print(
                f"Warning: episode mappings for {source_key!r} -> "
                f"{target_key!r} in {origin} are not a mapping",
                file=sys.stderr,
            )
            continue

        if not episode_map:
            print(
                f"Warning: empty episode mapping for {source_key!r} -> "
                f"{target_key!r} in {origin}; skipping",
                file=sys.stderr,
            )
            continue

        if media_type == "movie":
            override = _emit_movie_override(
                episode_map=episode_map,
                origin=origin,
                provider=provider,
                source_id=source_id,
                target_namespace=target["namespace"],
                target_id=target["id"],
                source_key=source_key,
                target_key=target_key,
                title=title,
                timestamp=timestamp,
            )
            if override is not None:
                overrides.append(override)
            continue

        overrides.extend(
            _emit_show_overrides(
                episode_map=episode_map,
                origin=origin,
                provider=provider,
                source_id=source_id,
                season=season,  # type: ignore[arg-type]
                target_namespace=target["namespace"],
                target_id=target["id"],
                source_key=source_key,
                target_key=target_key,
                title=title,
                timestamp=timestamp,
            )
        )

    return overrides


def build_crosswatch_overrides(
    combined: dict[str, Any],
    titles: dict[str, str],
    *,
    timestamp: int,
    existing_created_at: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Build the ``crosswatch-overrides.json`` payload.

    ``existing_created_at`` is an ``{override_id: created_at}`` map read
    from a prior run's ``crosswatch-overrides.json``. When present, the
    corresponding overrides preserve their original ``created_at`` so
    re-running the script only refreshes ``updated_at``. New overrides
    (and overrides whose deterministic ID no longer matches anything in
    the previous run) are stamped with ``timestamp``.
    """
    if existing_created_at is None:
        existing_created_at = {}
    overrides_by_id: dict[str, Override] = {}

    for source_key, targets in combined.items():
        title = titles.get(source_key, "")
        for override in convert_mapping(
            source_key,
            targets,
            title=title,
            origin=COMBINED_OUTPUT,
            timestamp=timestamp,
        ):
            preserved = existing_created_at.get(override["id"])
            if preserved is not None:
                override["created_at"] = preserved
            # updated_at was already set to `timestamp` by _make_override.
            overrides_by_id[override["id"]] = override

    # Preserve insertion order so the output is stable and diff-friendly.
    return {
        "overrides": list(overrides_by_id.values()),
        "updated_at": timestamp,
    }


def read_existing_created_at(path: Path) -> dict[str, int]:
    """Load ``{override_id: created_at}`` from an existing overrides file.

    Returns an empty dict if the file is missing or malformed; in either
    case the next write becomes the authoritative ``created_at`` for
    every rule. The read is intentionally permissive: a corrupt or
    hand-edited overrides file should not block regeneration.
    """
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(
            f"Warning: could not read existing overrides from {path}: "
            f"{exc}; treating as empty",
            file=sys.stderr,
        )
        return {}
    if not isinstance(data, dict):
        return {}
    overrides = data.get("overrides", [])
    if not isinstance(overrides, list):
        return {}
    result: dict[str, int] = {}
    for entry in overrides:
        if not isinstance(entry, dict):
            continue
        override_id = entry.get("id")
        created = entry.get("created_at")
        if isinstance(override_id, str) and isinstance(created, int):
            result[override_id] = created
    return result


# === Output writers ========================================================


def _strip_crosswatch_only_targets(
    combined: dict[str, Any],
) -> dict[str, Any]:
    """Return a copy of ``combined`` with CrossWatch-only target keys removed.

    Targets whose namespace is in :data:`CROSSWATCH_ONLY_TARGET_NAMESPACES`
    (currently just ``kitsu``) are kept in the in-memory ``combined`` dict so
    the CrossWatch override generator can still see them, but are stripped
    from the AniBridge-combined YAML/JSON output.
    """
    filtered: dict[str, Any] = {}
    for source_key, targets in combined.items():
        if not isinstance(targets, dict):
            filtered[source_key] = targets
            continue
        filtered[source_key] = {
            target_key: episode_map
            for target_key, episode_map in targets.items()
            if not (
                isinstance(target_key, str)
                and target_key.split(":", 1)[0]
                in CROSSWATCH_ONLY_TARGET_NAMESPACES
            )
        }
    return filtered


def write_combined_yaml(combined: dict[str, Any]) -> None:
    """Write the merged AniBridge mapping YAML.

    The output starts with a ``yaml-language-server`` schema comment and a
    ``$meta`` block describing this collection, followed by the merged
    ``source_key -> targets`` mapping. Targets whose namespace is in
    :data:`CROSSWATCH_ONLY_TARGET_NAMESPACES` are omitted.
    """
    MAPPINGS_DIR.mkdir(parents=True, exist_ok=True)

    payload: dict[str, Any] = {
        "$meta": COMBINED_META,
        **_strip_crosswatch_only_targets(combined),
    }

    header = f"# yaml-language-server: $schema={SCHEMA_URL}\n\n"
    body = yaml.dump(
        payload,
        Dumper=_NoAliasDumper,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )
    COMBINED_OUTPUT.write_text(header + body, encoding="utf-8")
    print(f"Combined mappings written to {COMBINED_OUTPUT}")


def write_combined_min_json(combined: dict[str, Any]) -> None:
    """Write a minified JSON mirror of the combined AniBridge mapping.

    Mirrors the structure of :func:`write_combined_yaml` (including the
    ``$meta`` block) plus the ``$schema`` declaration, but emits compact
    JSON without indentation. Targets whose namespace is in
    :data:`CROSSWATCH_ONLY_TARGET_NAMESPACES` are omitted.
    """
    MAPPINGS_DIR.mkdir(parents=True, exist_ok=True)

    payload: dict[str, Any] = {
        "$schema": SCHEMA_URL,
        "$meta": COMBINED_META,
        **_strip_crosswatch_only_targets(combined),
    }
    COMBINED_MIN_JSON_OUTPUT.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=False,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Combined minified JSON written to {COMBINED_MIN_JSON_OUTPUT}")


def write_crosswatch_overrides(payload: dict[str, Any]) -> None:
    """Write the CrossWatch overrides JSON."""
    MAPPINGS_DIR.mkdir(parents=True, exist_ok=True)
    CROSSWATCH_OUTPUT.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(
        f"CrossWatch overrides written to {CROSSWATCH_OUTPUT} "
        f"({len(payload['overrides'])} rules)",
    )


# === Entry point ===========================================================


def main() -> int:
    """Run the combine + override pipeline; return the process exit code."""
    timestamp = int(time.time())

    combined, titles, had_error = combine_mappings()
    expanded, expanded_titles = expand_swap_sources(combined, titles)

    write_combined_yaml(expanded)
    write_combined_min_json(expanded)

    existing_created_at = read_existing_created_at(CROSSWATCH_OUTPUT)
    payload = build_crosswatch_overrides(
        expanded,
        expanded_titles,
        timestamp=timestamp,
        existing_created_at=existing_created_at,
    )
    write_crosswatch_overrides(payload)

    return 1 if had_error else 0


if __name__ == "__main__":
    sys.exit(main())
