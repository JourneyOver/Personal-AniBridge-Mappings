#!/usr/bin/env python3
"""
Script to combine all individual mapping.yml files into combined mappings.yml
"""

import yaml
from pathlib import Path

def combine_mappings():
    base_path = Path("mappings")
    combined = {}
    directories = ["shows", "movies"]

    for dir_name in directories:
        dir_path = base_path / dir_name
        if not dir_path.exists():
            continue
        for yml_file in dir_path.glob("*/mapping.yml"):
            print(f"Processing {yml_file}")
            try:
                with open(yml_file, 'r', encoding='utf-8') as f:
                    data = yaml.safe_load(f)
                    if data:
                        combined.update(data)
            except Exception as e:
                print(f"Error processing {yml_file}: {e}")

    output_file = Path("mappings/mappings.yml")
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write("# yaml-language-server: $schema=https://raw.githubusercontent.com/anibridge/anibridge-mappings/refs/heads/main/mappings.schema.json\n\n")
        yaml.dump(combined, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    print(f"Combined mappings written to {output_file}")

if __name__ == "__main__":
    combine_mappings()
