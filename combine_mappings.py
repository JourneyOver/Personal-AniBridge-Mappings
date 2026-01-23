#!/usr/bin/env python3
"""
Script to combine all individual mapping.yml files into combined mappings.yml
"""

import yaml
from pathlib import Path

def combine_mappings():
    # Base path to the mappings directory
    base_path = Path("mappings")

    # Dictionary to hold combined mappings
    combined = {}

    # Directories to process
    directories = ["shows", "movies"]

    for dir_name in directories:
        dir_path = base_path / dir_name
        if dir_path.exists():
            # Find all mapping.yml files in subdirectories
            for yml_file in dir_path.glob("*/mapping.yml"):
                print(f"Processing {yml_file}")
                try:
                    with open(yml_file, 'r', encoding='utf-8') as f:
                        data = yaml.safe_load(f)
                        if data:
                            combined.update(data)
                except Exception as e:
                    print(f"Error processing {yml_file}: {e}")
        else:
            print(f"Directory {dir_path} does not exist, skipping.")

    # Write the combined mappings to mappings.yaml
    output_file = Path("mappings/mappings.yml")
    with open(output_file, 'w', encoding='utf-8') as f:
        # Add the schema comment at the top
        f.write("# yaml-language-server: $schema=https://raw.githubusercontent.com/anibridge/anibridge-mappings/refs/heads/main/mappings.schema.json\n\n")
        yaml.dump(combined, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    print(f"Combined mappings written to {output_file}")

if __name__ == "__main__":
    combine_mappings()
