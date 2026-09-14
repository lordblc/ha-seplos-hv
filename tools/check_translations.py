#!/usr/bin/env python3
"""Cross-check every entity translation_key used in custom_components/seplos_hv
against strings.json, translations/en.json and translations/nb.json.

Static ``translation_key="..."`` / ``translation_key='...'`` literals (in
EntityDescription instances and as ``_attr_translation_key = "..."``) are
found by regex. A couple of keys are built dynamically from an f-string
(``f"param_{kind}"`` in sensor.py, where ``kind`` loops over "trip" and
"recover") — those cannot be resolved by regex, so they are listed explicitly
in DYNAMIC_KEYS below, next to the source line that builds them.

Usage: python3 tools/check_translations.py
Exits non-zero (and prints what is missing) if any key used in code is absent
from any of the three translation files, under entity.<platform>.<key>.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "seplos_hv"

# platform -> source file
PLATFORM_FILES = {
    "sensor": COMPONENT / "sensor.py",
    "binary_sensor": COMPONENT / "binary_sensor.py",
}

# Keys built at runtime from an f-string, which the regex below cannot see.
# (file, dynamic expression) -> concrete keys it actually produces.
DYNAMIC_KEYS: dict[str, list[str]] = {
    "sensor": ["param_trip", "param_recover"],  # sensor.py: f"param_{kind}", kind in ("trip", "recover")
}

TRANSLATION_KEY_RE = re.compile(r"""translation_key\s*=\s*["']([A-Za-z0-9_]+)["']""")

TRANSLATION_FILES = {
    "strings.json": COMPONENT / "strings.json",
    "translations/en.json": COMPONENT / "translations" / "en.json",
    "translations/nb.json": COMPONENT / "translations" / "nb.json",
}


def used_keys_for(platform: str, path: Path) -> set[str]:
    """Static translation_key literals plus this platform's known dynamic keys."""
    text = path.read_text(encoding="utf-8")
    keys = set(TRANSLATION_KEY_RE.findall(text))
    keys.update(DYNAMIC_KEYS.get(platform, []))
    return keys


def defined_keys_for(platform: str, doc: dict) -> set[str]:
    """Keys defined under entity.<platform> in a strings/translations document."""
    return set(doc.get("entity", {}).get(platform, {}).keys())


def main() -> int:
    docs: dict[str, dict] = {}
    for label, path in TRANSLATION_FILES.items():
        if not path.exists():
            print(f"MISSING FILE: {path}")
            return 1
        docs[label] = json.loads(path.read_text(encoding="utf-8"))

    ok = True
    for platform, src_path in PLATFORM_FILES.items():
        if not src_path.exists():
            print(f"MISSING FILE: {src_path}")
            ok = False
            continue

        used = used_keys_for(platform, src_path)
        print(f"\n[{platform}] {len(used)} translation_key(s) used in {src_path.name}")

        for label, doc in docs.items():
            defined = defined_keys_for(platform, doc)
            missing = sorted(used - defined)
            extra = sorted(defined - used)
            if missing:
                ok = False
                print(f"  MISSING in {label}: {missing}")
            else:
                print(f"  OK in {label} ({len(defined)} defined)")
            if extra:
                print(f"  (unused-but-defined in {label}, not an error: {extra})")

    print()
    if ok:
        print("All translation_key usages are covered in strings.json, en.json and nb.json.")
        return 0
    print("FAILED: some translation_key usages are not covered. See MISSING lines above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
