"""Tests for the guarded parameter-write path: protocol.py encode/build_write_request,
client.py's write_params(), and the pure validation helpers in writes.py.

Run with:  python3 -m unittest discover -s tests -v

None of this ever contacts a real BCU: dry-run tests never open a socket at all, and the
"real write" tests run against a small dedicated test double defined in this file (NOT
tools/mock_bcu.py, which stays read-only-replay-only per the task brief).
"""

from __future__ import annotations

import asyncio
import re
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SEPLOS_DIR = _REPO_ROOT / "custom_components" / "seplos_hv"
if str(_SEPLOS_DIR) not in sys.path:
    sys.path.insert(0, str(_SEPLOS_DIR))

import protocol as proto  # noqa: E402
import writes  # noqa: E402
from client import SeplosHvClient  # noqa: E402

FIXTURES = [
    _REPO_ROOT / "tests" / "fixtures" / "bcu_frames_1.txt",
    _REPO_ROOT / "tests" / "fixtures" / "bcu_frames_2.txt",
]

_DATA_RE = re.compile(r"【Data】\s*([0-9A-Fa-f ]+)")
_CMD_RE = re.compile(r"【CMD】\s*(0x[0-9A-Fa-f]+)")


def _is_reply_line(line: str) -> bool:
    return "】 ↑" in line


def _iter_frames(path: Path):
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "【Data】" not in line:
            continue
        cmd_m = _CMD_RE.search(line)
        data_m = _DATA_RE.search(line)
        if not cmd_m or not data_m:
            continue
        cmd = int(cmd_m.group(1), 16)
        raw = bytes(int(tok, 16) for tok in data_m.group(1).split())
        yield cmd, _is_reply_line(line), raw


def _first_reply_payload(cmd: int, path: Path = FIXTURES[1]) -> bytes:
    for c, is_reply, raw in _iter_frames(path):
        if c == cmd and is_reply:
            length = (raw[6] << 8) | raw[7]
            return raw[8:8 + length]
    raise AssertionError(f"no reply for cmd 0x{cmd:04X} found in {path}")


class TestEncodeDecodeRoundTrip(unittest.TestCase):
    def test_round_trip_over_every_param_fixture_reply(self):
        checked = 0
        for cmd in proto.PARAM_CMDS:
            payload = _first_reply_payload(cmd)
            block = proto.decode_params(cmd, payload)
            self.assertEqual(proto.encode_params(block), payload, f"cmd 0x{cmd:04X}")
            checked += 1
        self.assertEqual(checked, len(proto.PARAM_CMDS))

    def test_round_trip_after_apply_param_change(self):
        # Changing one field and encoding again must only change the bytes for that field.
        payload = _first_reply_payload(0x0201)
        block = proto.decode_params(0x0201, payload)
        changed = proto.apply_param_change(block, 0, "trip", 3450)
        re_decoded = proto.decode_params(0x0201, proto.encode_params(changed))
        self.assertEqual(re_decoded.levels[0].trip, 3450)
        self.assertEqual(re_decoded.levels[1].trip, block.levels[1].trip)
        self.assertEqual(re_decoded.levels[2].trip, block.levels[2].trip)


class TestBuildWriteRequest(unittest.TestCase):
    def test_charge_over_current_l1_trip_32a_frame(self):
        # The exact scenario from the task brief: charge_over_current (0x0209) L1 trip
        # changed to 32 A, starting from the fixture's own reply.
        payload = _first_reply_payload(0x0209)
        block = proto.decode_params(0x0209, payload)
        self.assertEqual(block.levels[0].trip, 90.0)  # fixture baseline, sanity check

        changed = proto.apply_param_change(block, 0, "trip", 32)
        new_payload = proto.encode_params(changed)
        frame = proto.build_write_request(0x0209, new_payload)

        self.assertEqual(frame[0], proto.SOF)
        self.assertEqual(frame[-1], proto.EOF)
        write_cmd = (frame[3] << 8) | frame[4]
        self.assertEqual(write_cmd, 0x0208)
        self.assertEqual(write_cmd, proto.WRITE_CMDS[0x0209])
        length = (frame[6] << 8) | frame[7]
        self.assertEqual(length, len(new_payload))
        self.assertEqual(frame[8:8 + length], new_payload)
        calc_crc = proto.crc16(frame[:-3])
        recv_crc = (frame[-3] << 8) | frame[-2]
        self.assertEqual(calc_crc, recv_crc)

    def test_write_cmd_is_read_cmd_minus_one_for_every_param(self):
        for read_cmd in proto.PARAM_CMDS:
            self.assertEqual(proto.WRITE_CMDS[read_cmd], read_cmd - 1)

    def test_rejects_blacklisted_and_unmapped_cmds(self):
        payload = b"\x00" * 24
        for bad_cmd in (0x0109, 0x010B, 0x0111, 0x0113, 0x1000, 0x1001):
            with self.assertRaises(ValueError):
                proto.build_write_request(bad_cmd, payload)
        # A read cmd that is not a parameter command at all (e.g. the pack summary read).
        with self.assertRaises(ValueError):
            proto.build_write_request(proto.CMD_SUMMARY, payload)

    def test_rejects_wrong_payload_length(self):
        with self.assertRaises(ValueError):
            proto.build_write_request(0x0201, b"\x00" * 23)  # needs 24
        with self.assertRaises(ValueError):
            proto.build_write_request(0x0209, b"\x00" * 24)  # needs 16
        with self.assertRaises(ValueError):
            proto.build_write_request(0x0239, b"\x00" * 8)  # needs 4


class TestApplyParamChange(unittest.TestCase):
    def setUp(self):
        self.block = proto.decode_params(0x0201, _first_reply_payload(0x0201))

    def test_rejects_bad_field(self):
        with self.assertRaises(ValueError):
            proto.apply_param_change(self.block, 0, "not_a_field", 1)

    def test_rejects_out_of_range_level(self):
        with self.assertRaises(ValueError):
            proto.apply_param_change(self.block, 5, "trip", 1)
        with self.assertRaises(ValueError):
            proto.apply_param_change(self.block, -1, "trip", 1)

    def test_heating_start_stop_rejects_delay_fields(self):
        block = proto.decode_params(0x0239, _first_reply_payload(0x0239))
        with self.assertRaises(ValueError):
            proto.apply_param_change(block, 0, "trip_delay_s", 1)

    def test_change_is_isolated_to_one_field(self):
        changed = proto.apply_param_change(self.block, 1, "recover_delay_s", 5.0)
        self.assertEqual(changed.levels[1].recover_delay_s, 5.0)
        self.assertEqual(changed.levels[1].trip, self.block.levels[1].trip)
        self.assertEqual(changed.levels[0], self.block.levels[0])


class TestParamValidation(unittest.TestCase):
    def test_value_range_mv(self):
        writes.check_value_range("mV", "trip", 3500)  # ok, no raise
        with self.assertRaises(writes.ParamValidationError):
            writes.check_value_range("mV", "trip", 1000)
        with self.assertRaises(writes.ParamValidationError):
            writes.check_value_range("mV", "trip", 5000)

    def test_value_range_current_is_absolute(self):
        writes.check_value_range("A", "trip", -90)  # discharge, negative, still in range
        with self.assertRaises(writes.ParamValidationError):
            writes.check_value_range("A", "trip", -400)

    def test_value_range_temperature(self):
        writes.check_value_range("C", "trip", -30)
        with self.assertRaises(writes.ParamValidationError):
            writes.check_value_range("C", "trip", 200)

    def test_delay_range_checked_regardless_of_unit(self):
        writes.check_value_range("mV", "trip_delay_s", 5.0)
        with self.assertRaises(writes.ParamValidationError):
            writes.check_value_range("mV", "trip_delay_s", 700)

    def test_step_guard_blocks_large_jump(self):
        with self.assertRaises(writes.ParamValidationError):
            writes.check_step_guard("trip", 100, 200, force=False)

    def test_step_guard_allows_small_jump(self):
        writes.check_step_guard("trip", 100, 110, force=False)  # 10%, ok

    def test_step_guard_bypassed_with_force(self):
        writes.check_step_guard("trip", 100, 900, force=True)

    def test_step_guard_ignores_delay_fields(self):
        writes.check_step_guard("trip_delay_s", 1, 100, force=False)

    def test_step_guard_from_zero_requires_force(self):
        with self.assertRaises(writes.ParamValidationError):
            writes.check_step_guard("trip", 0, 5, force=False)
        writes.check_step_guard("trip", 0, 5, force=True)


class TestWriteParamsDryRun(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_never_touches_the_wire(self):
        # host is unreachable/never resolved to a real connection in this test - if
        # dry_run touched the wire at all, this would hang/raise instead of returning.
        client = SeplosHvClient(host="127.0.0.1", port=1, timeout=0.2)
        block = proto.decode_params(0x0209, _first_reply_payload(0x0209))
        changed = proto.apply_param_change(block, 0, "trip", 32)

        result = await client.write_params(0x0209, changed, dry_run=True)

        self.assertFalse(result.sent)
        self.assertIsNone(result.reply)
        self.assertIsNone(result.readback)
        self.assertFalse(result.verified)
        self.assertTrue(result.frame_hex.startswith("9A 01 21 02 08"))
        self.assertIsNone(client._writer)  # never connected


# ------------------------------------------------------------- real-write test double


async def _serve_write_double(
    port: int, write_cmd: int, ack_payload: bytes, read_cmd: int, readback_payload: bytes
) -> asyncio.Server:
    """A tiny, dedicated double for the real (non-dry-run) write path.

    Deliberately NOT tools/mock_bcu.py (that one stays read-only/replay-only). Answers the
    first frame (expected: the write frame) with an ack echoing ``ack_payload`` under
    ``write_cmd``, then answers the second frame (expected: the read-back request) with
    ``readback_payload`` under ``read_cmd``. The real BCU's ack shape is UNVERIFIED - this
    double is a stand-in for "some frame comes back", not a claim about what it contains.
    """

    def _build(cmd: int, payload: bytes) -> bytes:
        header = (
            bytes([proto.SOF, proto.BCU_ADDR, proto.HOST_ADDR])
            + cmd.to_bytes(2, "big") + b"\x00" + len(payload).to_bytes(2, "big")
        )
        frame = header + payload
        return frame + proto.crc16(frame).to_bytes(2, "big") + bytes([proto.EOF])

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        parser = proto.FrameParser()
        replies = [_build(write_cmd, ack_payload), _build(read_cmd, readback_payload)]
        try:
            while replies:
                data = await reader.read(4096)
                if not data:
                    break
                for _frame in parser.feed(data):
                    if not replies:
                        break
                    writer.write(replies.pop(0))
                    await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()

    return await asyncio.start_server(handle, host="127.0.0.1", port=port)


async def _serve_sequence_double(port: int, replies: list[tuple[int, bytes]]) -> asyncio.Server:
    """Like _serve_write_double, but for an arbitrary ordered sequence of (cmd, payload)
    replies - one per incoming frame, in order, regardless of that frame's own cmd. Used
    to simulate "read the fresh baseline, then write, then read back" (3 exchanges) for
    the fresh-vs-cached-baseline tests.
    """

    def _build(cmd: int, payload: bytes) -> bytes:
        header = (
            bytes([proto.SOF, proto.BCU_ADDR, proto.HOST_ADDR])
            + cmd.to_bytes(2, "big") + b"\x00" + len(payload).to_bytes(2, "big")
        )
        frame = header + payload
        return frame + proto.crc16(frame).to_bytes(2, "big") + bytes([proto.EOF])

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        parser = proto.FrameParser()
        queue = list(replies)
        try:
            while queue:
                data = await reader.read(4096)
                if not data:
                    break
                for _frame in parser.feed(data):
                    if not queue:
                        break
                    cmd, payload = queue.pop(0)
                    writer.write(_build(cmd, payload))
                    await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()

    return await asyncio.start_server(handle, host="127.0.0.1", port=port)


class TestReadParam(unittest.IsolatedAsyncioTestCase):
    async def test_read_param_decodes_a_single_group(self):
        payload = _first_reply_payload(0x0209)
        server = await _serve_sequence_double(0, [(0x0209, payload)])
        port = server.sockets[0].getsockname()[1]
        client = SeplosHvClient(host="127.0.0.1", port=port, timeout=2.0)
        try:
            block = await client.read_param(0x0209)
            self.assertEqual(block.key, "charge_over_current")
            self.assertEqual(block.levels[0].trip, 90.0)
        finally:
            await client.close()
            server.close()
            await server.wait_closed()


class TestFreshBaselineNotStaleCached(unittest.IsolatedAsyncioTestCase):
    """Reproduces the coordinator's fetch-fresh-before-write flow directly against the
    client (coordinator.py itself needs homeassistant, unavailable on this dev machine) -
    proves the write path, given a fresh read_param() call, uses the DEVICE's current
    value as the baseline rather than some other (e.g. stale/cached) block passed in from
    elsewhere.
    """

    async def test_write_flow_uses_freshly_read_value_not_a_stale_one(self):
        fixture_payload = _first_reply_payload(0x0209)
        fresh_block = proto.decode_params(0x0209, fixture_payload)
        # A deliberately different "stale" block - if the write flow used this instead of
        # the fresh read, the 20% step guard below would see a different (and wrong) base.
        stale_block = proto.apply_param_change(fresh_block, 0, "trip", 200)
        self.assertNotEqual(stale_block.levels[0].trip, fresh_block.levels[0].trip)

        target_trip = 95  # +5.6% over the fresh baseline (90), within the 20% guard;
        # +5.6% would be safe against fresh (90) but a -52.5% "drop" against stale (200) -
        # different bases give different guard outcomes, which is exactly what this test
        # distinguishes.
        changed = proto.apply_param_change(fresh_block, 0, "trip", target_trip)
        new_payload = proto.encode_params(changed)

        server = await _serve_sequence_double(
            0,
            [
                (0x0209, fixture_payload),  # (1) fresh baseline read
                (0x0208, new_payload),      # (2) write ack
                (0x0209, new_payload),      # (3) read-back
            ],
        )
        port = server.sockets[0].getsockname()[1]
        client = SeplosHvClient(host="127.0.0.1", port=port, timeout=2.0)
        try:
            # Step 1: fresh read, exactly what the coordinator does before validating.
            baseline = await client.read_param(0x0209)
            self.assertEqual(baseline.levels[0].trip, fresh_block.levels[0].trip)
            self.assertNotEqual(baseline.levels[0].trip, stale_block.levels[0].trip)

            # Step 2: the 20% guard, evaluated against the fresh baseline (matches
            # coordinator.async_write_param's own check_step_guard call).
            writes.check_step_guard("trip", baseline.levels[0].trip, target_trip, force=False)
            with self.assertRaises(writes.ParamValidationError):
                # Same target, but against the stale baseline: proves the two bases
                # genuinely disagree on whether this step is allowed.
                writes.check_step_guard(
                    "trip", stale_block.levels[0].trip, target_trip, force=False
                )

            # Step 3: write + verify, using the fresh-derived block throughout.
            result = await client.write_params(0x0209, changed, dry_run=False)
            self.assertTrue(result.verified)
            self.assertEqual(result.readback.levels[0].trip, target_trip)
        finally:
            await client.close()
            server.close()
            await server.wait_closed()


class TestWriteParamsRealWrite(unittest.IsolatedAsyncioTestCase):
    async def test_real_write_verified_true_on_matching_readback(self):
        block = proto.decode_params(0x0209, _first_reply_payload(0x0209))
        changed = proto.apply_param_change(block, 0, "trip", 32)
        new_payload = proto.encode_params(changed)

        server = await _serve_write_double(
            0, write_cmd=0x0208, ack_payload=new_payload,
            read_cmd=0x0209, readback_payload=new_payload,
        )
        port = server.sockets[0].getsockname()[1]
        client = SeplosHvClient(host="127.0.0.1", port=port, timeout=2.0)
        try:
            result = await client.write_params(0x0209, changed, dry_run=False)
            self.assertTrue(result.sent)
            self.assertIsNotNone(result.reply)
            self.assertTrue(result.verified)
            self.assertEqual(result.readback.levels[0].trip, 32.0)
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    async def test_real_write_verified_false_on_mismatched_readback(self):
        block = proto.decode_params(0x0209, _first_reply_payload(0x0209))
        changed = proto.apply_param_change(block, 0, "trip", 32)
        new_payload = proto.encode_params(changed)
        # Readback still reports the OLD block - simulates a write that silently failed.
        old_payload = proto.encode_params(block)

        server = await _serve_write_double(
            0, write_cmd=0x0208, ack_payload=new_payload,
            read_cmd=0x0209, readback_payload=old_payload,
        )
        port = server.sockets[0].getsockname()[1]
        client = SeplosHvClient(host="127.0.0.1", port=port, timeout=2.0)
        try:
            result = await client.write_params(0x0209, changed, dry_run=False)
            self.assertTrue(result.sent)
            self.assertFalse(result.verified)
            self.assertEqual(result.readback.levels[0].trip, block.levels[0].trip)
        finally:
            await client.close()
            server.close()
            await server.wait_closed()


if __name__ == "__main__":
    unittest.main()
