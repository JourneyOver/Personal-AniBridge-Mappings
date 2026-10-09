#!/usr/bin/env python3
"""Combine AniBridge mappings and generate CrossWatch custom overrides.

This script walks every ``mappings/<shows|movies>/<Title>/mapping.yml`` file,
merges them into a single ``mappings/mappings.yml`` (the AniBridge combined
format), and emits ``mappings/crosswatch-overrides.json`` (the CrossWatch
override format).

The human-readable title for each media item is taken from the parent folder
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
CROSSWATCH_OUTPUT = MAPPINGS_DIR / "crosswatch-overrides.json"

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

# === Helpers ===============================================================


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


def parse_target_key(target_key: Any, *, origin: Path | None = None) -> dict | None:
    """Parse an AniBridge target key into a CrossWatch ``{namespace, id}``."""
    if not isinstance(target_key, str):
        print(
            f"Warning: target key is not a string: {target_key!r}",
            file=sys.stderr,
        )
        return None
    match = TARGET_RE.fullmatch(target_key)
    if not match:
        suffix = f" in {origin}" if origin is not None else ""
        print(
            f"Warning: unsupported CrossWatch target namespace in "
            f"{target_key!r}{suffix}; skipping target",
            file=sys.stderr,
        )
        return None
    return match.groupdict()


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
    note: str = "",
) -> Override:
    """Build a CrossWatch override dict with a deterministic ID."""
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
        created_at=timestamp,
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
) -> dict[str, Any]:
    """Build the ``crosswatch-overrides.json`` payload."""
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
            overrides_by_id[override["id"]] = override

    # Preserve insertion order so the output is stable and diff-friendly.
    return {
        "overrides": list(overrides_by_id.values()),
        "updated_at": timestamp,
    }


# === Output writers ========================================================


def write_combined_yaml(combined: dict[str, Any]) -> None:
    """Write the merged AniBridge mapping YAML."""
    MAPPINGS_DIR.mkdir(parents=True, exist_ok=True)

    header = f"# yaml-language-server: $schema={SCHEMA_URL}\n\n"
    body = yaml.dump(
        combined,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )
    COMBINED_OUTPUT.write_text(header + body, encoding="utf-8")
    print(f"Combined mappings written to {COMBINED_OUTPUT}")


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
    write_combined_yaml(combined)

    payload = build_crosswatch_overrides(
        combined,
        titles,
        timestamp=timestamp,
    )
    write_crosswatch_overrides(payload)

    return 1 if had_error else 0


if __name__ == "__main__":
    sys.exit(main())
