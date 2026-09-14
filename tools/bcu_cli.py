#!/usr/bin/env python3
"""Bench CLI for a Seplos HV BCU - read-only, prints identity/summary/status/
cells/temps (and optionally protection parameters) as tables or JSON.

Runs standalone::

    python3 tools/bcu_cli.py --host 192.168.1.50 --port 8899
    python3 tools/bcu_cli.py --serial /dev/ttyUSB0
    python3 tools/bcu_cli.py --host 127.0.0.1 --json
    python3 tools/bcu_cli.py --host 127.0.0.1 --params
    python3 tools/bcu_cli.py --host 127.0.0.1 --watch 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SEPLOS_DIR = _REPO_ROOT / "custom_components" / "seplos_hv"
# Import protocol.py/client.py directly as flat modules (not through the
# custom_components.seplos_hv package) so this bench CLI runs standalone
# without homeassistant installed - it never triggers that package's
# __init__.py, which imports homeassistant.
if str(_SEPLOS_DIR) not in sys.path:
    sys.path.insert(0, str(_SEPLOS_DIR))

from client import (  # noqa: E402
    SeplosConnectionError,
    SeplosHvClient,
    SeplosTimeout,
)
from protocol import ModuleCells, PackSummary, Status, Temperatures  # noqa: E402


def _print_identity(identity: dict[str, str]) -> None:
    print("=== Identity ===")
    for k, v in identity.items():
        print(f"  {k:12} {v}")


def _print_summary(s: PackSummary) -> None:
    print("=== Pack summary ===")
    print(f"  v_pack        {s.v_pack:.1f} V")
    print(f"  v_collect     {s.v_collect:.1f} V")
    print(f"  v_load        {s.v_load:.1f} V")
    print(f"  current       {s.current:.2f} A")
    print(f"  soc / soh     {s.soc}% / {s.soh}%")
    print(f"  remaining_ah  {s.remaining_ah:.2f} Ah")
    print(f"  full_ah       {s.full_ah:.2f} Ah")
    print(f"  design_ah     {s.design_ah:.2f} Ah")
    print(f"  cell max/min  {s.cell_max_mv} mV @ {s.cell_max_label}   "
          f"{s.cell_min_mv} mV @ {s.cell_min_label}   spread {s.cell_spread_mv} mV")
    print(f"  temp max/min  {s.max_temp_c:.1f} C @ {s.max_temp_label}   "
          f"{s.min_temp_c:.1f} C @ {s.min_temp_label}")
    print(f"  limits        {s.limits}")


def _print_status(st: Status) -> None:
    print("=== Status ===")
    print(f"  relay_word    0x{st.relay_word:08X}")
    print(f"  current_limiting={st.current_limiting}  charge={st.charge_relay}  "
          f"discharge={st.discharge_relay}  precharge={st.precharge_relay}  "
          f"negative={st.negative_relay}  heating={st.heating_relay}")
    print(f"  byte27={st.byte27}  byte30={st.byte30}  byte36={st.byte36}")


def _print_cells(modules: list[ModuleCells]) -> None:
    print("=== Cell voltages ===")
    for m in modules:
        print(f"  BMU{m.module_id}: {len(m.cells_mv)} cells   "
              f"min {m.min_mv} mV (C{m.min_index + 1})   "
              f"max {m.max_mv} mV (C{m.max_index + 1})   "
              f"spread {m.spread_mv} mV")
        for row_start in range(0, len(m.cells_mv), 8):
            row = m.cells_mv[row_start:row_start + 8]
            print("      " + " ".join(f"{v:>5}" for v in row))


def _print_temps(t: Temperatures) -> None:
    print("=== Temperatures ===")
    print("  BCU: " + "  ".join(f"{v:.1f}C" for v in t.bcu_c))
    for m in t.modules:
        if not m.sensors_c:
            print(f"  BMU{m.module_id}: NO SENSORS REPORTED")
        else:
            print(f"  BMU{m.module_id}: " + "  ".join(f"{v:.1f}C" for v in m.sensors_c))


def _print_params(params: dict) -> None:
    print("=== Protection parameters ===")
    print(f"  {'key':28} {'unit':7} {'L1 trip':>10}{'L1 rec':>10}"
          f"{'L2 trip':>10}{'L2 rec':>10}{'L3 trip':>10}{'L3 rec':>10}")
    for key, block in params.items():
        vals = []
        for lvl in block.levels:
            vals.append(f"{lvl.trip:>10.1f}{lvl.recover:>10.1f}")
        print(f"  {key:28} {block.unit:7} " + "".join(vals))


def _summary_to_dict(s: PackSummary) -> dict:
    return s.as_dict()


def _status_to_dict(st: Status) -> dict:
    d = asdict(st)
    d["raw"] = st.raw.hex()
    return d


def _cells_to_list(modules: list[ModuleCells]) -> list[dict]:
    return [
        {
            "module_id": m.module_id,
            "cells_mv": m.cells_mv,
            "min_mv": m.min_mv,
            "max_mv": m.max_mv,
            "spread_mv": m.spread_mv,
        }
        for m in modules
    ]


def _temps_to_dict(t: Temperatures) -> dict:
    return {
        "bcu_c": t.bcu_c,
        "modules": [asdict(m) for m in t.modules],
    }


def _params_to_dict(params: dict) -> dict:
    return {key: asdict(block) for key, block in params.items()}


async def _one_pass(client: SeplosHvClient, args: argparse.Namespace) -> dict:
    identity = await client.read_identity()
    summary = await client.read_summary()
    status = await client.read_status()
    cells = await client.read_cells()
    temps = await client.read_temps()
    params = await client.read_params() if args.params else None

    if args.json:
        doc = {
            "identity": identity,
            "summary": _summary_to_dict(summary),
            "status": _status_to_dict(status),
            "cells": _cells_to_list(cells),
            "temps": _temps_to_dict(temps),
        }
        if params is not None:
            doc["params"] = _params_to_dict(params)
        return doc

    _print_identity(identity)
    _print_summary(summary)
    _print_status(status)
    _print_cells(cells)
    _print_temps(temps)
    if params is not None:
        _print_params(params)
    return {}


async def _main_async(args: argparse.Namespace) -> int:
    if args.serial:
        client = SeplosHvClient(serial_port=args.serial, baudrate=args.baudrate, timeout=args.timeout)
    else:
        client = SeplosHvClient(host=args.host, port=args.port, timeout=args.timeout)

    try:
        await client.connect()
    except SeplosConnectionError as exc:
        print(f"connect failed: {exc}", file=sys.stderr)
        return 1

    try:
        if args.watch is not None:
            while True:
                doc = await _one_pass(client, args)
                if args.json:
                    print(json.dumps(doc, indent=2))
                print(f"--- next read in {args.watch}s (Ctrl-C to stop) ---")
                await asyncio.sleep(args.watch)
        else:
            doc = await _one_pass(client, args)
            if args.json:
                print(json.dumps(doc, indent=2))
    except (SeplosTimeout, SeplosConnectionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    finally:
        await client.close()
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    transport = ap.add_mutually_exclusive_group()
    transport.add_argument("--host", help="TCP host/IP of the gateway")
    transport.add_argument("--serial", help="serial device path (e.g. /dev/ttyUSB0)")
    ap.add_argument("--port", type=int, default=8899, help="TCP port (default 8899)")
    ap.add_argument("--baudrate", type=int, default=57600, help="serial baud rate (default 57600)")
    ap.add_argument("--timeout", type=float, default=1.5, help="per-request timeout in seconds")
    ap.add_argument("--json", action="store_true", help="dump everything as one JSON document")
    ap.add_argument("--params", action="store_true", help="also read the protection parameter blocks")
    ap.add_argument("--watch", type=float, nargs="?", const=5.0, default=None, metavar="SECONDS",
                     help="loop, reading every N seconds (default 5 if given with no value)")
    args = ap.parse_args()

    if not args.host and not args.serial:
        args.host = "127.0.0.1"

    sys.exit(asyncio.run(_main_async(args)))


if __name__ == "__main__":
    main()
