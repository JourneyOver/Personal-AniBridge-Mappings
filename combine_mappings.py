#!/usr/bin/env python3
"""Combine individual mapping.yml files into combined mappings.yml"""

import sys
from pathlib import Path

import yaml

SCHEMA_URL = "https://raw.githubusercontent.com/anibridge/anibridge-mappings/refs/heads/main/mappings.schema.json"
MAPPINGS_DIR = Path("mappings")
MAPPING_TYPES = ("shows", "movies")


def combine_mappings() -> None:
    combined: dict = {}
    seen_in: dict[str, Path] = {}
    had_error = False

    for dir_name in MAPPING_TYPES:
        dir_path = MAPPINGS_DIR / dir_name
        if not dir_path.is_dir():
            continue
        for yml_file in sorted(dir_path.glob("*/mapping.yml")):
            print(f"Processing {yml_file}")
            try:
                data = yaml.safe_load(yml_file.read_text(encoding="utf-8"))
                if not data:
                    continue
                for key in data:
                    if key in seen_in:
                        print(f"Warning: duplicate key '{key}' in {yml_file} (first seen in {seen_in[key]})")
                    seen_in[key] = yml_file
                combined.update(data)
            except Exception as e:
                print(f"Error processing {yml_file}: {e}", file=sys.stderr)
                had_error = True

    output_file = MAPPINGS_DIR / "mappings.yml"
    header = f"# yaml-language-server: $schema={SCHEMA_URL}\n\n"
    output_file.write_text(
        header + yaml.dump(combined, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"Combined mappings written to {output_file}")
    sys.exit(1 if had_error else 0)


if __name__ == "__main__":
    combine_mappings()