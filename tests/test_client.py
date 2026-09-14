"""End-to-end tests for SeplosHvClient against tools/mock_bcu.py.

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SEPLOS_DIR = _REPO_ROOT / "custom_components" / "seplos_hv"
# Import protocol.py/client.py directly as flat modules (not through the
# custom_components.seplos_hv package) so this test suite never triggers that
# package's __init__.py, which imports homeassistant - unavailable on this
# pip-less dev machine and irrelevant to these two pure-stdlib modules.
if str(_SEPLOS_DIR) not in sys.path:
    sys.path.insert(0, str(_SEPLOS_DIR))

import protocol as proto  # noqa: E402
from client import (  # noqa: E402
    SeplosConnectionError,
    SeplosHvClient,
    SeplosTimeout,
)

sys.path.insert(0, str(_REPO_ROOT / "tools"))
import mock_bcu  # noqa: E402

FIXTURES = [
    str(_REPO_ROOT / "tests" / "fixtures" / "bcu_frames_1.txt"),
    str(_REPO_ROOT / "tests" / "fixtures" / "bcu_frames_2.txt"),
]


class TestSeplosHvClientAgainstMock(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # delay=0 keeps the test suite fast; port 0 -> OS picks an ephemeral one
        self.server = await mock_bcu.serve(0, FIXTURES, delay_ms=0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.client = SeplosHvClient(host="127.0.0.1", port=self.port, timeout=2.0)
        await self.client.connect()

    async def asyncTearDown(self):
        await self.client.close()
        self.server.close()
        await self.server.wait_closed()

    async def test_read_identity(self):
        identity = await self.client.read_identity()
        self.assertEqual(identity["bms_serial"], "304431553990004P")
        self.assertTrue(identity["firmware"].startswith("HVP-B1018"))

    async def test_read_summary(self):
        summary = await self.client.read_summary()
        self.assertAlmostEqual(summary.v_pack, 422.2)
        self.assertEqual(summary.cell_max_label, "BMU1 C21")
        self.assertEqual(summary.cell_min_label, "BMU1 C19")

    async def test_read_status(self):
        status = await self.client.read_status()
        self.assertIsInstance(status.relay_word, int)

    async def test_read_cells(self):
        modules = await self.client.read_cells()
        self.assertEqual(len(modules), 4)
        self.assertTrue(all(len(m.cells_mv) == 32 for m in modules))

    async def test_read_temps(self):
        temps = await self.client.read_temps()
        bmu1 = next(m for m in temps.modules if m.module_id == 1)
        self.assertEqual(len(bmu1.sensors_c), 0)

    async def test_read_params(self):
        params = await self.client.read_params()
        self.assertEqual(len(params), len(proto.PARAM_CMDS))
        self.assertIn("cell_over_voltage", params)
        self.assertEqual(len(params["cell_over_voltage"].levels), 3)

    async def test_sequential_requests_are_serialised(self):
        # Fire several requests concurrently; the client's lock must serialise
        # them so every one still gets the right reply back.
        results = await asyncio.gather(
            self.client.read_summary(),
            self.client.read_status(),
            self.client.read_cells(),
            self.client.read_temps(),
        )
        summary, status, cells, temps = results
        self.assertAlmostEqual(summary.v_pack, 422.2)
        self.assertEqual(len(cells), 4)

    async def test_forbidden_cmd_never_sent_build_request_raises(self):
        with self.assertRaises(ValueError):
            proto.build_request(0x0202)


class TestSeplosHvClientTimeoutAndConnectionErrors(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_when_nothing_listens(self):
        client = SeplosHvClient(host="127.0.0.1", port=1, timeout=0.2)
        with self.assertRaises(SeplosConnectionError):
            await client.connect()

    async def test_timeout_when_server_never_replies(self):
        # A server that accepts connections but never answers -> SeplosTimeout.
        # The handler's own pause only needs to outlast the client's request
        # timeout (0.2s below); keeping it short avoids stalling server
        # teardown, since wait_closed() waits for in-flight handler tasks.
        async def silent_handler(reader, writer):
            await reader.read(4096)
            await asyncio.sleep(0.3)
            writer.close()

        server = await asyncio.start_server(silent_handler, host="127.0.0.1", port=0)
        port = server.sockets[0].getsockname()[1]
        try:
            client = SeplosHvClient(host="127.0.0.1", port=port, timeout=0.2)
            await client.connect()
            with self.assertRaises(SeplosTimeout):
                await client.request(proto.CMD_SUMMARY)
            await client.close()
        finally:
            server.close()
            await server.wait_closed()

    async def test_construction_requires_host_or_serial(self):
        with self.assertRaises(ValueError):
            SeplosHvClient()


if __name__ == "__main__":
    unittest.main()
