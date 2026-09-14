"""Tests for custom_components/seplos_hv/protocol.py against real captures.

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SEPLOS_DIR = _REPO_ROOT / "custom_components" / "seplos_hv"
# Import protocol.py directly as a flat module (not through the
# custom_components.seplos_hv package) so this test suite never triggers that
# package's __init__.py, which imports homeassistant - unavailable on this
# pip-less dev machine and irrelevant to protocol.py, which is pure stdlib.
if str(_SEPLOS_DIR) not in sys.path:
    sys.path.insert(0, str(_SEPLOS_DIR))

import protocol as proto  # noqa: E402

FIXTURES = [
    _REPO_ROOT / "tests" / "fixtures" / "bcu_frames_1.txt",
    _REPO_ROOT / "tests" / "fixtures" / "bcu_frames_2.txt",
]

_DATA_RE = re.compile(r"【Data】\s*([0-9A-Fa-f ]+)")
_CMD_RE = re.compile(r"【CMD】\s*(0x[0-9A-Fa-f]+)")


def _is_reply_line(line: str) -> bool:
    # The column header itself contains a literal '↑' ('↑-↓'), so match the
    # marker cell specifically rather than just the presence of the glyph.
    return "】 ↑" in line


def _iter_frames(path: Path):
    """Yield (cmd:int, is_reply:bool, raw:bytes) for every 【Data】 line."""
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


def _first_reply_payload(cmd: int, path: Path = FIXTURES[0]) -> bytes:
    for c, is_reply, raw in _iter_frames(path):
        if c == cmd and is_reply:
            length = (raw[6] << 8) | raw[7]
            return raw[8:8 + length]
    raise AssertionError(f"no reply for cmd 0x{cmd:04X} found in {path}")


class TestCrcAndFrames(unittest.TestCase):
    def test_every_fixture_frame_parses_and_crc_checks(self):
        total = 0
        for path in FIXTURES:
            for cmd, is_reply, raw in _iter_frames(path):
                frame = proto.parse_frame(raw)
                self.assertEqual(frame.cmd, cmd)
                total += 1
        self.assertGreater(total, 1000)

    def test_every_request_frame_matches_build_request(self):
        checked = 0
        for path in FIXTURES:
            for cmd, is_reply, raw in _iter_frames(path):
                if is_reply:
                    continue
                if cmd not in proto.ALLOWED_CMDS:
                    continue
                self.assertEqual(raw, proto.build_request(cmd),
                                  f"mismatch rebuilding request for cmd 0x{cmd:04X}")
                checked += 1
        self.assertGreater(checked, 100)

    def test_build_request_rejects_non_whitelisted_cmd(self):
        with self.assertRaises(ValueError):
            proto.build_request(0x0202)

    def test_build_request_rejects_every_even_02xx_and_unknown_ids(self):
        for cmd in (0x0202, 0x0204, 0x0009, 0x000E, 0x0011, 0x0039):
            with self.assertRaises(ValueError):
                proto.build_request(cmd)


class TestDecodeSummary(unittest.TestCase):
    def setUp(self):
        payload = _first_reply_payload(proto.CMD_SUMMARY)
        self.summary = proto.decode_summary(payload)

    def test_voltages(self):
        self.assertAlmostEqual(self.summary.v_pack, 422.2)

    def test_soc_soh_capacity(self):
        # NOTE: SPEC.md says soc=50 / remaining≈299.97; the fixture's every
        # 0x000A reply actually decodes to soc=42 / remaining=250.0 at these
        # offsets. Trusting the fixture per the task instructions.
        self.assertEqual(self.summary.soc, 42)
        self.assertEqual(self.summary.soh, 100)
        self.assertAlmostEqual(self.summary.remaining_ah, 250.0)
        self.assertAlmostEqual(self.summary.full_ah, 600.0)
        self.assertAlmostEqual(self.summary.design_ah, 600.0)

    def test_cell_extremes(self):
        self.assertEqual(self.summary.cell_max_mv, 3416)
        self.assertEqual(self.summary.cell_max_label, "BMU1 C21")
        # NOTE: SPEC.md says min cell 3187; the first captured 0x000A reply
        # decodes to 3188 mV (both 3187 and 3188 occur across the fixtures,
        # index is stable at 18 -> BMU1 C19 either way).
        self.assertEqual(self.summary.cell_min_mv, 3188)
        self.assertEqual(self.summary.cell_min_label, "BMU1 C19")

    def test_max_temp(self):
        self.assertAlmostEqual(self.summary.max_temp_c, 19.8)

    def test_cell_spread_and_as_dict(self):
        self.assertEqual(self.summary.cell_spread_mv,
                          self.summary.cell_max_mv - self.summary.cell_min_mv)
        d = self.summary.as_dict()
        self.assertEqual(d["cell_max_mv"], 3416)
        self.assertIn("cell_spread_mv", d)

    def test_decode_summary_raises_frame_error_on_short_payload(self):
        with self.assertRaises(proto.FrameError):
            proto.decode_summary(b"\x00" * 10)


class TestDecodeStrings(unittest.TestCase):
    def test_bms_serial(self):
        payload = _first_reply_payload(proto.CMD_BMS_SERIAL)
        self.assertEqual(proto.decode_string(payload), "304431553990004P")

    def test_firmware_string(self):
        payload = _first_reply_payload(proto.CMD_FIRMWARE)
        self.assertTrue(proto.decode_string(payload).startswith("HVP-B1018"))


class TestDecodeCells(unittest.TestCase):
    def test_four_modules_of_32_cells(self):
        payload = _first_reply_payload(proto.CMD_CELLS)
        modules = proto.decode_cells(payload)
        self.assertEqual(len(modules), 4)
        for m in modules:
            self.assertEqual(len(m.cells_mv), 32)
        self.assertEqual([m.module_id for m in modules], [1, 2, 3, 4])

    def test_module_cells_properties(self):
        payload = _first_reply_payload(proto.CMD_CELLS)
        modules = proto.decode_cells(payload)
        m = modules[0]
        self.assertEqual(m.spread_mv, m.max_mv - m.min_mv)
        self.assertEqual(m.cells_mv[m.max_index], m.max_mv)
        self.assertEqual(m.cells_mv[m.min_index], m.min_mv)


class TestDecodeTemps(unittest.TestCase):
    def test_bmu1_has_zero_sensors(self):
        payload = _first_reply_payload(proto.CMD_TEMPS)
        temps = proto.decode_temps(payload)
        bmu1 = next(m for m in temps.modules if m.module_id == 1)
        self.assertEqual(len(bmu1.sensors_c), 0)

    def test_other_modules_have_sensors(self):
        payload = _first_reply_payload(proto.CMD_TEMPS)
        temps = proto.decode_temps(payload)
        for m in temps.modules:
            if m.module_id != 1:
                self.assertGreater(len(m.sensors_c), 0)


class TestDecodeStatus(unittest.TestCase):
    def test_status_decodes(self):
        payload = _first_reply_payload(proto.CMD_STATUS)
        status = proto.decode_status(payload)
        self.assertIsInstance(status.relay_word, int)
        self.assertEqual(status.raw, payload)

    def test_status_raises_on_short_payload(self):
        with self.assertRaises(proto.FrameError):
            proto.decode_status(b"\x00" * 5)


class TestDecodeParams(unittest.TestCase):
    # 0x0209/0x020B reply with 16 bytes (2 levels) and 0x0239 with 4 bytes (1
    # bare trip/recover pair, no delays) instead of the common 24-byte/3-level
    # shape - confirmed against the fixture, see decode_params()'s docstring.
    _NONSTANDARD_LEVEL_COUNTS = {0x0209: 2, 0x020B: 2, 0x0239: 1}

    def test_all_whitelisted_param_cmds_decode(self):
        for cmd, (key, unit) in proto.PARAM_CMDS.items():
            payload = _first_reply_payload(cmd, path=FIXTURES[1])
            block = proto.decode_params(cmd, payload)
            self.assertEqual(block.key, key)
            self.assertEqual(block.unit, unit)
            expected = self._NONSTANDARD_LEVEL_COUNTS.get(cmd, 3)
            self.assertEqual(len(block.levels), expected, f"cmd 0x{cmd:04X}")

    def test_cell_over_voltage_is_sane_and_monotonic(self):
        payload = _first_reply_payload(0x0201, path=FIXTURES[1])
        block = proto.decode_params(0x0201, payload)
        trips = [lvl.trip for lvl in block.levels]
        # L1 -> L2 -> L3 trip should move outward (increase for over-voltage)
        self.assertLess(trips[0], trips[2])

    def test_decode_params_rejects_non_param_cmd(self):
        with self.assertRaises(ValueError):
            proto.decode_params(proto.CMD_SUMMARY, b"\x00" * 24)


class TestLabelsAndKelvin(unittest.TestCase):
    def test_cell_label(self):
        self.assertEqual(proto.cell_label(20), "BMU1 C21")
        self.assertEqual(proto.cell_label(18), "BMU1 C19")
        self.assertEqual(proto.cell_label(32), "BMU2 C1")

    def test_temp_label(self):
        self.assertEqual(proto.temp_label(0), "BMU1 T1")
        self.assertEqual(proto.temp_label(64), "BMU2 T1")

    def test_kelvin10_to_c(self):
        self.assertAlmostEqual(proto.kelvin10_to_c(2731), 0.0)
        self.assertAlmostEqual(proto.kelvin10_to_c(2929), 19.8)


class TestFrameParser(unittest.TestCase):
    def test_split_frame_across_two_feeds(self):
        frame_bytes = proto.build_request(proto.CMD_SUMMARY)
        parser = proto.FrameParser()
        mid = len(frame_bytes) // 2
        self.assertEqual(parser.feed(frame_bytes[:mid]), [])
        frames = parser.feed(frame_bytes[mid:])
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].cmd, proto.CMD_SUMMARY)

    def test_concatenated_frames_in_one_feed(self):
        f1 = proto.build_request(proto.CMD_SUMMARY)
        f2 = proto.build_request(proto.CMD_STATUS)
        parser = proto.FrameParser()
        frames = parser.feed(f1 + f2)
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0].cmd, proto.CMD_SUMMARY)
        self.assertEqual(frames[1].cmd, proto.CMD_STATUS)

    def test_garbage_prefix_is_discarded_and_resyncs(self):
        # Leading noise with no 0x9A byte in it at all - unambiguous: it must
        # all be discarded before the real frame is recognised.
        garbage = bytes([0x00, 0xFF, 0x12, 0x34, 0x56, 0x00])
        good = proto.build_request(proto.CMD_CELLS)
        parser = proto.FrameParser()
        frames = parser.feed(garbage + good)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].cmd, proto.CMD_CELLS)

    def test_garbage_containing_a_false_sof_still_resyncs_once_complete(self):
        # A stray 0x9A byte inside the noise is ambiguous until enough bytes
        # are buffered to prove it isn't a real header; feeding it all at
        # once (plenty of trailing data) must still resolve to the one real
        # frame once the false lead is disproven.
        garbage = bytes([0x00, 0xFF, 0x9A, 0x00, 0x01, 0x02, 0x03, 0x04])
        good = proto.build_request(proto.CMD_TEMPS)
        parser = proto.FrameParser()
        frames = parser.feed(garbage + good + good)
        self.assertEqual(len(frames), 2)
        self.assertTrue(all(f.cmd == proto.CMD_TEMPS for f in frames))

    def test_pure_garbage_does_not_raise_and_buffer_is_bounded(self):
        parser = proto.FrameParser()
        garbage = bytes((i * 7 + 3) % 256 for i in range(10_000))
        frames = parser.feed(garbage)  # must not raise
        self.assertEqual(frames, [])
        self.assertLessEqual(len(parser._buf), proto.FrameParser._MAX_BUFFER)

    def test_corrupted_crc_is_skipped_then_resyncs_on_next_good_frame(self):
        good = bytearray(proto.build_request(proto.CMD_TEMPS))
        good[-3] ^= 0xFF  # corrupt CRC high byte
        good2 = proto.build_request(proto.CMD_TEMPS)
        parser = proto.FrameParser()
        frames = parser.feed(bytes(good) + good2)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].cmd, proto.CMD_TEMPS)

    def test_reset_clears_buffer(self):
        parser = proto.FrameParser()
        parser.feed(proto.build_request(proto.CMD_SUMMARY)[:4])
        parser.reset()
        self.assertEqual(len(parser._buf), 0)


if __name__ == "__main__":
    unittest.main()
