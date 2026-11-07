#!/usr/bin/env python3
"""Project a private full manifest into a textless release recipe."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from generator_launcher.text_reconstruction import export_recipe  # noqa: E402


def _read_json(path: str | None):
    if not path:
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, help="private full source manifest")
    parser.add_argument("--english-index", help="normalized local EN export index JSON")
    parser.add_argument("--german-index", help="normalized local DE export index JSON")
    parser.add_argument("--output", required=True, help="textless recipe JSON output")
    args = parser.parse_args(argv)

    manifest = _read_json(args.manifest)
    indexes = {}
    if args.english_index:
        indexes["en"] = _read_json(args.english_index)
    if args.german_index:
        indexes["de"] = _read_json(args.german_index)
    recipe = export_recipe(manifest, indexes)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(recipe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote textless recipe: {output} ({len(recipe['text_reconstruction']['slots'])} text slots)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
