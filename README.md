# PlexAniBridge-Mappings

A personal collection of custom mappings for [PlexAniBridge](https://github.com/eliasbenb/PlexAniBridge), a tool that bridges Plex media libraries with AniList.

## Disclaimer

**This repository is for personal use only.** The mappings provided here are tailored to my specific Plex setup and may not work for other users. Use at your own risk and verify mappings before applying them.

## Description

PlexAniBridge allows you to sync your Plex watch status with AniList. Custom mappings are used to match Plex media entries to their corresponding AniList IDs when automatic matching fails.

This collection includes mappings for various anime series, organized by show name.

## Usage

1. Clone this repository.
2. Run `scripts/combine_mappings.py` to generate the combined mapping files:
   ```
   python scripts/combine_mappings.py
   ```
3. Use the generated `mappings/mappings.yml` (or its minified JSON mirror
   `mappings/mappings.min.json`) in your PlexAniBridge configuration.

   Both scripts are designed to be run from the repo root (their relative
   paths assume `mappings/` is a sibling of `scripts/`).

### Looking up IDs for a new show

`scripts/lookup_ids.py` resolves an anime show name to the six IDs the
mapping files use (`tmdb_show`, `tvdb_show`, `anidb`, `anilist`, `mal`,
`kitsu`):

```
python scripts/lookup_ids.py "Overgeared"
```

It queries AniList (canonical anchor), Kitsu (which exposes AniDB and TVDB
ids via its `/anime/{id}/mappings` bridge in one call), and TMDB (which
provides the TV series id and, via `/tv/{id}/external_ids`, a TVDB
fallback). TMDB requires a v3 API key, resolved in this order:
`--tmdb-key` flag → `[tmdb] api_key` in the `config.ini` next to
`lookup_ids.py` (copy from `config.ini.example`; override with
`--config <path>`) → `TMDB_API_KEY` env var. The script prints a
pasteable YAML block; you fill in the episode ranges and drop it into a
per-show `mapping.yml`.

The AniDB id is resolved through a two-provider fallback chain
(AniList → Kitsu) and the TVDB id through a two-provider chain
(Kitsu → TMDB). If AniList doesn't return a MAL id (it usually does,
via `idMal`) there is currently no fallback — the previous Jikan-based
fallback was removed because the v4 API is unreliable from many
networks. `tmdb_show` and `tvdb_show` are emitted with the `:s1` season
suffix, `anidb` with the `:R` AniDB resource suffix, so the lines match
the existing `mapping.yml` format and paste directly.

## Repository Structure

- `mappings/`: Contains the combined mappings files (`mappings.yml`,
  `mappings.min.json`, and `crosswatch-overrides.json`).
- `mappings/shows/`: Individual mapping files for each anime series.
- `mappings/movies/`: Individual mapping files for each anime movie.
- `scripts/`: Utility scripts.
  - `combine_mappings.py`: Combines per-show `mapping.yml` files into the
    repo-wide `mappings.yml` / `mappings.min.json` / `crosswatch-overrides.json`.
  - `lookup_ids.py`: Resolves a show name to the six AniBridge target IDs.
  - `config.ini.example`: Template for the optional `config.ini` (TMDB key).

## Credits

- [PlexAniBridge](https://github.com/eliasbenb/PlexAniBridge) by eliasbenb
- Inspired by mappings from [eliasbenb](https://github.com/eliasbenb/PlexAniBridge-Mappings) and [LuceoEtzio](https://github.com/LuceoEtzio/PlexAniBridge-Custom-Mappings)
