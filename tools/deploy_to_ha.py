#!/usr/bin/env python3
"""Deploy custom_components/seplos_hv to Home Assistant via the HA Vibecode Agent API.

Reads the bearer key from ~/.mcp.json (mcpServers.home-assistant.env) and never prints it.

    python3 tools/deploy_to_ha.py                 # upload all files
    python3 tools/deploy_to_ha.py --dry-run       # list what would be uploaded
    python3 tools/deploy_to_ha.py --restart       # upload, then restart HA
    python3 tools/deploy_to_ha.py --verify        # re-read every file from HA and diff
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "custom_components" / "seplos_hv"
DEST = "custom_components/seplos_hv"
SKIP_DIRS = {"__pycache__"}
SKIP_SUFFIX = {".pyc"}


def load_api() -> tuple[str, str]:
    cfg = json.load(open(Path.home() / ".mcp.json"))
    env = cfg["mcpServers"]["home-assistant"]["env"]
    return env["HA_AGENT_URL"].rstrip("/"), env["HA_AGENT_KEY"]


def call(base: str, key: str, method: str, path: str, body: dict | None = None,
         params: dict | None = None) -> dict:
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": "Bearer " + key,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {path} -> HTTP {e.code}: {e.read().decode()[:300]}")


def local_files() -> list[Path]:
    out = []
    for p in sorted(SRC.rglob("*")):
        if p.is_dir() or p.suffix in SKIP_SUFFIX:
            continue
        if any(part in SKIP_DIRS for part in p.relative_to(SRC).parts):
            continue
        out.append(p)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--restart", action="store_true", help="restart HA after upload")
    ap.add_argument("--verify", action="store_true", help="read back and compare")
    a = ap.parse_args()

    files = local_files()
    if not files:
        raise SystemExit(f"nothing to deploy under {SRC}")
    print(f"{len(files)} files under {SRC}")
    if a.dry_run:
        for p in files:
            print("  ", p.relative_to(SRC))
        return

    base, key = load_api()
    if a.verify:
        bad = 0
        for p in files:
            rel = p.relative_to(SRC).as_posix()
            r = call(base, key, "GET", "/api/files/read", params={"path": f"{DEST}/{rel}"})
            if r.get("content") != p.read_text(encoding="utf-8"):
                bad += 1
                print("  DIFFERS:", rel)
        print("verify:", "all identical" if not bad else f"{bad} differ")
        return

    call(base, key, "POST", "/api/backup/checkpoint",
         params={"user_request": "deploy seplos_hv custom integration"})
    for p in files:
        rel = p.relative_to(SRC).as_posix()
        call(base, key, "POST", "/api/files/write",
             {"path": f"{DEST}/{rel}", "content": p.read_text(encoding="utf-8"),
              "create_backup": False, "commit_message": f"seplos_hv: {rel}"})
        print("  uploaded", rel)
    print("upload complete")
    if a.restart:
        print("restarting Home Assistant ...")
        print(call(base, key, "POST", "/api/system/restart"))


if __name__ == "__main__":
    main()
