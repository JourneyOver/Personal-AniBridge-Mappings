# PlexAniBridge-Mappings

A personal collection of custom mappings for [PlexAniBridge](https://github.com/eliasbenb/PlexAniBridge), a tool that bridges Plex media libraries with AniList.

## Disclaimer

**This repository is for personal use only.** The mappings provided here are tailored to my specific Plex setup and may not work for other users due to differences in media naming, file structures, or personal preferences. Use at your own risk and verify mappings before applying them.

## Description

PlexAniBridge allows you to sync your Plex watch status with AniList. Custom mappings are used to match Plex media entries to their corresponding AniList IDs when automatic matching fails.

This collection includes mappings for various anime series, organized by show name.

## Usage

1. Clone this repository.
2. Run the `combine_mappings.py` script to generate the combined `mappings.yml` file:
   ```
   python combine_mappings.py
   ```
3. Use the generated `mappings/mappings.yml` in your PlexAniBridge configuration.

## Repository Structure

- `mappings/`: Contains the combined mappings file.
- `shows/`: Individual mapping files for each anime series.
- `movies/`: Individual mapping files for each anime movie.
- `combine_mappings.py`: Script to combine individual mappings into one file.

## Credits

- [PlexAniBridge](https://github.com/eliasbenb/PlexAniBridge) by eliasbenb
- Inspired by mappings from [eliasbenb](https://github.com/eliasbenb/PlexAniBridge-Mappings) and [LuceoEtzio](https://github.com/LuceoEtzio/PlexAniBridge-Custom-Mappings)
