#!/usr/bin/env python3
"""Combine AniBridge mappings and generate CrossWatch custom overrides.

Walks every ``mappings/<shows|movies>/<Title>/mapping.yml`` and produces:

* ``mappings/mappings.yml``              - combined AniBridge mapping (YAML)
* ``mappings/mappings.min.json``         - minified JSON mirror of the above
* ``mappings/crosswatch-overrides.json`` - CrossWatch override rules

The combined YAML/JSON carry a ``$meta`` block. The human-readable title of
each media item is the parent folder name, e.g.
``mappings/shows/Overgeared/mapping.yml`` -> ``"title": "Overgeared"``.

Pipeline::

    discover files -> load catalog -> expand swap sources
        -> render combined YAML / JSON
        -> generate overrides -> reconcile with previous run -> render JSON
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterator

import yaml

# === Configuration ==========================================================

SCHEMA_URL = (
    "https://raw.githubusercontent.com/anibridge/"
    "anibridge-mappings/refs/heads/main/mappings.schema.json"
)

MAPPING_TYPES: tuple[str, ...] = ("shows", "movies")

COMBINED_META: dict[str, str] = {
    "schema_version": "3.0.0",
    "description": "Journey Custom AniBridge Mapping",
}

# Providers CrossWatch can match against.
SUPPORTED_MATCH_PROVIDERS: frozenset[str] = frozenset(
    {"tvdb", "tmdb", "imdb", "anidb", "mal", "anilist", "simkl", "kitsu"}
)

# Target namespaces only relevant to CrossWatch overrides; stripped from the
# combined AniBridge YAML/JSON.
CROSSWATCH_ONLY_NAMESPACES: frozenset[str] = frozenset({"kitsu"})

# Override fields that change on every run and are ignored when deciding
# whether a rule actually changed.
VOLATILE_FIELDS: frozenset[str] = frozenset({"created_at", "updated_at"})

# Source keys, e.g. ``tmdb_show:324502:s1`` or ``tmdb_movie:16516``.
SOURCE_RE = re.compile(
    r"(?P<provider>[a-z0-9]+)_(?P<kind>show|movie):(?P<id>[^:]+)(?::s(?P<season>\d+))?"
)
# Target keys, e.g. ``anilist:212888``, ``mal:64340``, ``anidb:20077:R``.
TARGET_RE = re.compile(
    r"(?P<namespace>anidb|anilist|mal|simkl|kitsu):(?P<id>[^:]+)(?::.*)?"
)
# Episode ranges: ``1``, ``"1"`` or ``"1-12"``.
RANGE_RE = re.compile(r"\s*(\d+)(?:\s*-\s*(\d+))?\s*")


@dataclass(frozen=True)
class Paths:
    """All input/output locations, derived from a single root directory."""

    root: Path

    @property
    def combined_yaml(self) -> Path:
        return self.root / "mappings.yml"

    @property
    def combined_json(self) -> Path:
        return self.root / "mappings.min.json"

    @property
    def overrides(self) -> Path:
        return self.root / "crosswatch-overrides.json"


# === Diagnostics ============================================================


class Diagnostics:
    """Prints warnings/errors to stderr and counts them for the exit code."""

    def __init__(self) -> None:
        self.warnings = 0
        self.errors = 0

    def warn(self, message: str) -> None:
        self.warnings += 1
        print(f"Warning: {message}", file=sys.stderr)

    def error(self, message: str) -> None:
        self.errors += 1
        print(f"Error: {message}", file=sys.stderr)

    @staticmethod
    def note(message: str) -> None:
        print(message, file=sys.stderr)


# === Key types ==============================================================


@dataclass(frozen=True)
class SourceKey:
    """A parsed AniBridge source key (``provider_kind:id[:sN]``)."""

    provider: str
    kind: str  # "show" | "movie"
    id: str
    season: int | None = None

    @classmethod
    def parse(cls, text: object) -> SourceKey | None:
        if not isinstance(text, str):
            return None
        match = SOURCE_RE.fullmatch(text)
        if match is None:
            return None
        season = match["season"]
        return cls(
            match["provider"],
            match["kind"],
            match["id"],
            int(season) if season is not None else None,
        )

    def __str__(self) -> str:
        key = f"{self.provider}_{self.kind}:{self.id}"
        return key if self.season is None else f"{key}:s{self.season}"


@dataclass(frozen=True)
class TargetKey:
    """A parsed CrossWatch-compatible target key (``namespace:id[:...]``)."""

    namespace: str
    id: str

    @classmethod
    def parse(cls, text: object) -> TargetKey | None:
        if not isinstance(text, str):
            return None
        match = TARGET_RE.fullmatch(text)
        if match is None:
            return None
        return cls(match["namespace"], match["id"])


def parse_target(text: object) -> TargetKey | SourceKey | None:
    """Classify a target key.

    * ``TargetKey``  - a real CrossWatch target namespace.
    * ``SourceKey``  - a "swap" target: a valid AniBridge source reference.
    * ``None``       - malformed / unsupported.
    """
    target = TargetKey.parse(text)
    return target if target is not None else SourceKey.parse(text)


def is_internal_key(key: object) -> bool:
    """AniBridge reserves ``$``-prefixed top-level keys (e.g. ``$meta``)."""
    return isinstance(key, str) and key.startswith("$")


# === Episode ranges =========================================================


def parse_range(value: Any) -> tuple[int, int]:
    """Parse ``1``, ``"1"`` or ``"1-12"`` into an inclusive ``(start, end)``."""
    if isinstance(value, bool):  # bool is an int subclass
        raise ValueError(f"Invalid episode range type: {value!r}")
    if isinstance(value, int):
        start = end = value
    elif isinstance(value, str):
        match = RANGE_RE.fullmatch(value)
        if match is None:
            raise ValueError(f"Invalid episode range: {value!r}")
        start = int(match[1])
        end = int(match[2]) if match[2] else start
    else:
        raise ValueError(f"Invalid episode range type: {value!r}")

    if start < 1 or end < start:
        raise ValueError(f"Invalid episode range: {value!r}")
    return start, end


@dataclass(frozen=True)
class EpisodeSpan:
    """One validated ``source range -> target range`` pair."""

    src_start: int
    src_end: int
    tgt_start: int

    @property
    def is_first_episode_identity(self) -> bool:
        """True for the trivial ``1 -> 1`` mapping."""
        return (self.src_start, self.src_end, self.tgt_start) == (1, 1, 1)


def parse_episode_map(
    episode_map: dict, *, label: str, diag: Diagnostics
) -> tuple[list[EpisodeSpan], bool]:
    """Validate an episode map; return ``(valid_spans, had_error)``.

    Bad entries are reported and skipped so callers can decide whether a
    partial result is acceptable.
    """
    spans: list[EpisodeSpan] = []
    had_error = False
    for source_range, target_range in episode_map.items():
        try:
            src_start, src_end = parse_range(source_range)
            tgt_start, tgt_end = parse_range(target_range)
        except ValueError as exc:
            diag.error(f"{label}: {exc}")
            had_error = True
            continue
        if src_end - src_start != tgt_end - tgt_start:
            diag.error(
                f"{label}: incompatible range lengths: "
                f"{source_range!r} -> {target_range!r}"
            )
            had_error = True
            continue
        spans.append(EpisodeSpan(src_start, src_end, tgt_start))
    return spans, had_error


# === Catalog: discovery, loading, swap expansion ============================


@dataclass(frozen=True)
class Entry:
    """One source key's targets plus where it came from."""

    targets: Any  # normally dict[str, dict]; validated lazily
    title: str
    origin: Path


Catalog = dict[str, Entry]


def iter_mapping_files(paths: Paths, diag: Diagnostics) -> Iterator[tuple[Path, str]]:
    """Yield ``(mapping.yml, title)`` for every ``<shows|movies>/<Title>``."""
    for type_name in MAPPING_TYPES:
        type_dir = paths.root / type_name
        if not type_dir.is_dir():
            continue
        for folder in sorted(type_dir.iterdir(), key=lambda p: p.name):
            if not folder.is_dir():
                continue
            mapping_file = folder / "mapping.yml"
            if not mapping_file.is_file():
                diag.warn(f"skipping {folder}: no mapping.yml inside")
                continue
            yield mapping_file, folder.name


def load_catalog(paths: Paths, diag: Diagnostics) -> Catalog:
    """Merge every mapping file into one catalog (first occurrence wins)."""
    catalog: Catalog = {}

    for mapping_file, title in iter_mapping_files(paths, diag):
        print(f"Processing {mapping_file}")
        try:
            data = yaml.safe_load(mapping_file.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            diag.error(f"failed to parse {mapping_file}: {exc}")
            continue
        except OSError as exc:
            diag.error(f"cannot read {mapping_file}: {exc}")
            continue

        if not data:
            diag.warn(f"{mapping_file} is empty; skipping")
            continue
        if not isinstance(data, dict):
            diag.error(
                f"{mapping_file} must contain a YAML mapping at the top level "
                f"(got {type(data).__name__})"
            )
            continue

        for source_key, targets in data.items():
            if is_internal_key(source_key):
                continue
            if not isinstance(source_key, str):
                diag.error(
                    f"{mapping_file}: source key must be a string, got {source_key!r}"
                )
                continue
            if source_key in catalog:
                diag.warn(
                    f"duplicate source key {source_key!r} in {mapping_file} "
                    f"(previous entry from {catalog[source_key].origin} kept)"
                )
                continue
            catalog[source_key] = Entry(targets, title, mapping_file)

    return catalog


def expand_swap_sources(catalog: Catalog, diag: Diagnostics) -> Catalog:
    """Promote swap targets (e.g. ``tvdb_show:475617:s1``) to source entries.

    For every source ``S`` with a swap target ``T``, add a source entry ``T``
    that has the same real targets (anidb/anilist/mal/kitsu...) as ``S`` plus a
    back-reference ``S`` carrying the swap's episode map. Existing entries
    always win over generated ones. Generated entries are not expanded again.
    """
    expanded: Catalog = dict(catalog)
    generated = 0

    for source_key, entry in catalog.items():
        if not isinstance(entry.targets, dict):
            continue

        swaps: list[tuple[str, Any]] = []
        real_targets: dict[str, Any] = {}
        for target_key, episode_map in entry.targets.items():
            parsed = parse_target(target_key)
            if isinstance(parsed, TargetKey):
                real_targets[target_key] = episode_map
            elif isinstance(parsed, SourceKey):
                if str(parsed) == source_key:
                    diag.warn(
                        f"source {source_key!r} references itself as a swap "
                        f"target; skipping swap"
                    )
                    continue
                swaps.append((str(parsed), episode_map))

        for swap_key, episode_map in swaps:
            if swap_key in expanded:
                continue
            expanded[swap_key] = Entry(
                targets={**real_targets, source_key: episode_map},
                title=entry.title,
                origin=entry.origin,
            )
            generated += 1
            diag.note(f"Generated swap source entry: {swap_key} (from {source_key})")

    if generated:
        print(f"Generated {generated} swap source entr{'y' if generated == 1 else 'ies'}")
    return expanded


# === Combined output rendering ==============================================


class _NoAliasDumper(yaml.SafeDumper):
    """YAML dumper that never emits anchors/aliases (``&id001`` / ``*id001``).

    Swap entries reuse the originals' episode-map dicts, so the default dumper
    would emit references, which make diffs and hand-edits harder.
    """

    def ignore_aliases(self, data: Any) -> bool:
        return True


def _public_targets(targets: Any) -> Any:
    """Drop CrossWatch-only targets (e.g. kitsu) from a targets dict."""
    if not isinstance(targets, dict):
        return targets
    return {
        key: value
        for key, value in targets.items()
        if not (
            isinstance(key, str)
            and key.partition(":")[0] in CROSSWATCH_ONLY_NAMESPACES
        )
    }


def _public_payload(catalog: Catalog) -> dict[str, Any]:
    return {key: _public_targets(entry.targets) for key, entry in catalog.items()}


def render_combined_yaml(catalog: Catalog) -> str:
    header = f"# yaml-language-server: $schema={SCHEMA_URL}\n\n"
    body = yaml.dump(
        {"$meta": COMBINED_META, **_public_payload(catalog)},
        Dumper=_NoAliasDumper,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )
    return header + body


def render_combined_json(catalog: Catalog) -> str:
    payload = {"$schema": SCHEMA_URL, "$meta": COMBINED_META, **_public_payload(catalog)}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"


# === CrossWatch overrides ===================================================


@dataclass(frozen=True)
class RuleIdentity:
    """The fields that define *which* rule this is (hashed into its ID)."""

    media_type: str
    match_provider: str
    match_id: str
    match_season: int | None
    episode_from: int | None
    episode_to: int | None
    episode_start_at: int | None
    target_namespace: str
    target_id: str

    def override_id(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return "ovr_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class Override:
    """A single CrossWatch override rule (field order = JSON key order)."""

    id: str
    created_at: int
    updated_at: int
    enabled: bool
    media_type: str
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

    @classmethod
    def create(cls, identity: RuleIdentity, *, title: str, timestamp: int) -> Override:
        return cls(
            id=identity.override_id(),
            created_at=timestamp,
            updated_at=timestamp,
            enabled=True,
            title=title,
            note="",
            **asdict(identity),
        )


class OverrideGenerator:
    """Converts catalog entries into CrossWatch overrides."""

    def __init__(self, diag: Diagnostics, timestamp: int) -> None:
        self.diag = diag
        self.timestamp = timestamp

    def generate(self, catalog: Catalog) -> list[Override]:
        """Convert the whole catalog; duplicate rule IDs collapse to one."""
        by_id: dict[str, Override] = {}
        for source_key, entry in catalog.items():
            for override in self._convert(source_key, entry):
                by_id[override.id] = override
        return list(by_id.values())  # insertion order -> stable diffs

    # -- per source ---------------------------------------------------------

    def _convert(self, source_key: str, entry: Entry) -> list[Override]:
        diag, origin = self.diag, entry.origin

        source = SourceKey.parse(source_key)
        if source is None:
            diag.warn(f"unrecognized AniBridge source key: {source_key!r} in {origin}")
            return []
        if source.provider not in SUPPORTED_MATCH_PROVIDERS:
            diag.warn(
                f"unsupported CrossWatch match provider {source.provider!r} "
                f"in {origin}; skipping {source_key!r}"
            )
            return []
        if source.kind == "show" and source.season is None:
            diag.warn(
                f"show mapping {source_key!r} in {origin} has no season; "
                f"CrossWatch show overrides require a season"
            )
            return []
        if not isinstance(entry.targets, dict):
            diag.error(
                f"targets for {source_key!r} in {origin} are not a mapping "
                f"(got {type(entry.targets).__name__}); skipping"
            )
            return []

        overrides: list[Override] = []
        for target_key, episode_map in entry.targets.items():
            target = parse_target(target_key)
            if isinstance(target, SourceKey):
                continue  # swap target: handled by expand_swap_sources
            if target is None:
                diag.warn(
                    f"unsupported CrossWatch target {target_key!r} in {origin}; "
                    f"skipping target"
                )
                continue

            label = f"{origin}: {source_key!r} -> {target_key!r}"
            if not isinstance(episode_map, dict):
                diag.warn(f"{label}: episode mappings are not a mapping")
                continue
            if not episode_map:
                diag.warn(f"{label}: empty episode mapping; skipping")
                continue

            if source.kind == "movie":
                override = self._movie(source, target, entry.title, episode_map, label)
                if override is not None:
                    overrides.append(override)
            else:
                overrides.extend(self._show(source, target, entry.title, episode_map, label))
        return overrides

    # -- per media type -----------------------------------------------------

    def _movie(
        self,
        source: SourceKey,
        target: TargetKey,
        title: str,
        episode_map: dict,
        label: str,
    ) -> Override | None:
        """One ID-only override; CrossWatch movies carry no episode data."""
        spans, had_error = parse_episode_map(episode_map, label=label, diag=self.diag)
        if had_error or not spans:
            return None

        if not (len(spans) == 1 and spans[0].is_first_episode_identity):
            self.diag.warn(
                f"{label}: movie mapping contains episode ranges that "
                f"CrossWatch's movie override format cannot preserve. "
                f"Creating an ID-only movie override."
            )

        identity = RuleIdentity(
            media_type="movie",
            match_provider=source.provider,
            match_id=source.id,
            match_season=None,
            episode_from=None,
            episode_to=None,
            episode_start_at=None,
            target_namespace=target.namespace,
            target_id=target.id,
        )
        return Override.create(identity, title=title, timestamp=self.timestamp)

    def _show(
        self,
        source: SourceKey,
        target: TargetKey,
        title: str,
        episode_map: dict,
        label: str,
    ) -> list[Override]:
        """One override per valid source/target episode-range pair."""
        spans, _ = parse_episode_map(episode_map, label=label, diag=self.diag)
        return [
            Override.create(
                RuleIdentity(
                    media_type="show",
                    match_provider=source.provider,
                    match_id=source.id,
                    match_season=source.season,
                    episode_from=span.src_start,
                    episode_to=span.src_end,
                    episode_start_at=span.tgt_start,
                    target_namespace=target.namespace,
                    target_id=target.id,
                ),
                title=title,
                timestamp=self.timestamp,
            )
            for span in spans
        ]


# === Reconciling with the previous run ======================================


@dataclass(frozen=True)
class PreviousRun:
    """What the last run wrote to ``crosswatch-overrides.json``."""

    rules: list[dict[str, Any]]
    updated_at: int | None

    @classmethod
    def load(cls, path: Path, diag: Diagnostics) -> PreviousRun:
        """Permissive read: a missing/corrupt file just means "no history"."""
        empty = cls([], None)
        if not path.is_file():
            return empty
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            diag.warn(
                f"could not read existing overrides from {path}: {exc}; treating as empty"
            )
            return empty
        if not isinstance(data, dict) or not isinstance(data.get("overrides"), list):
            return empty

        rules = [
            rule
            for rule in data["overrides"]
            if isinstance(rule, dict) and isinstance(rule.get("id"), str)
        ]
        updated_at = data.get("updated_at")
        return cls(rules, updated_at if isinstance(updated_at, int) else None)


def _content(rule: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in rule.items() if k not in VOLATILE_FIELDS}


def reconcile(
    overrides: list[Override],
    previous: PreviousRun,
    *,
    timestamp: int,
    refresh_timestamps: bool,
) -> dict[str, Any]:
    """Build the overrides payload, keeping timestamps stable where possible.

    * ``created_at`` is always carried over for rules seen before.
    * ``updated_at`` is carried over too, unless the rule's content changed
      (or ``refresh_timestamps`` is set), so re-running on unchanged input
      yields a byte-identical file instead of a noisy all-lines diff.
    * The top-level ``updated_at`` only moves when something actually changed.
    """
    previous_by_id = {rule["id"]: rule for rule in previous.rules}
    result: list[Override] = []

    for override in overrides:
        old = previous_by_id.get(override.id)
        if old is not None:
            if isinstance(old.get("created_at"), int):
                override = replace(override, created_at=old["created_at"])
            if (
                not refresh_timestamps
                and isinstance(old.get("updated_at"), int)
                and _content(old) == _content(asdict(override))
            ):
                override = replace(override, updated_at=old["updated_at"])
        result.append(override)

    rules = [asdict(o) for o in result]
    unchanged = (
        not refresh_timestamps
        and previous.updated_at is not None
        and rules == previous.rules
    )
    return {
        "overrides": rules,
        "updated_at": previous.updated_at if unchanged else timestamp,
    }


def render_overrides(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


# === I/O ====================================================================


def write_text_atomic(path: Path, text: str) -> None:
    """Write via a temp file + rename so a crash never leaves a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    tmp.replace(path)


# === Entry point ============================================================


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("mappings"),
        help="mappings directory (default: %(default)s)",
    )
    parser.add_argument(
        "--refresh-timestamps",
        action="store_true",
        help="bump updated_at on every override, even unchanged ones",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    paths = Paths(args.root)
    diag = Diagnostics()
    timestamp = int(time.time())

    catalog = expand_swap_sources(load_catalog(paths, diag), diag)

    write_text_atomic(paths.combined_yaml, render_combined_yaml(catalog))
    print(f"Combined mappings written to {paths.combined_yaml}")

    write_text_atomic(paths.combined_json, render_combined_json(catalog))
    print(f"Combined minified JSON written to {paths.combined_json}")

    overrides = OverrideGenerator(diag, timestamp).generate(catalog)
    payload = reconcile(
        overrides,
        PreviousRun.load(paths.overrides, diag),
        timestamp=timestamp,
        refresh_timestamps=args.refresh_timestamps,
    )
    write_text_atomic(paths.overrides, render_overrides(payload))
    print(f"CrossWatch overrides written to {paths.overrides} ({len(overrides)} rules)")

    print(
        f"Done: {len(catalog)} sources, {diag.warnings} warning(s), "
        f"{diag.errors} error(s)"
    )
    return 1 if diag.errors else 0


if __name__ == "__main__":
    sys.exit(main())