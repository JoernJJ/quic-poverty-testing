"""Regression coverage for iperf's omitted zero-byte terminal interval."""

import json
import tempfile
import unittest
from pathlib import Path

from validate_contention import validate


class TerminalIntervalTests(unittest.TestCase):
    def validate_window(self, *, explicit_tail=False, unreported_bytes=0):
        origin_ns = 1_200_000_000
        interval_sums = [
            {"start": 0.0, "end": 5.0, "bits_per_second": 8_000_000,
             "bytes": 5_000_000, "sender": True},
            {"start": 5.0, "end": 10.0, "bits_per_second": 16_000_000,
             "bytes": 10_000_000, "sender": True},
        ]
        if explicit_tail:
            interval_sums.append({"start": 10.0, "end": 10.1,
                                  "bits_per_second": 0, "bytes": 0, "sender": True})
        iperf = {
            "intervals": [{"sum": row} for row in interval_sums],
            "end": {"sum_sent": {"start": 0.0, "end": 10.1,
                                 "bytes": 15_000_000 + unreported_bytes,
                                 "sender": True}},
        }
        # Client spans a rate transition and ends inside the empty terminal tail.
        timing = {"start": origin_ns + 5_100_000_000,
                  "end": origin_ns + 10_050_000_000}
        markers = {
            phase: [{"index": i, "remote_ns": origin_ns + relative_ns + i,
                     "sender_receipt_ns": origin_ns + relative_ns + i}
                    for i in range(1, 6)]
            for phase, relative_ns in (("start", 5_090_000_000),
                                       ("end", 10_060_000_000))
        }
        with tempfile.TemporaryDirectory() as tmp:
            paths = [Path(tmp) / name for name in ("iperf.json", "time.json", "markers.json")]
            for path, value in zip(paths, (iperf, timing, markers)):
                path.write_text(json.dumps(value), encoding="utf-8")
            return validate(*(str(path) for path in paths),
                            cross_start_ns=1_000_000_000,
                            cross_stop_ns=11_300_000_000, lead_s=5.0)

    def test_omitted_empty_tail_preserves_window_and_throughput(self):
        omitted = self.validate_window()
        explicit = self.validate_window(explicit_tail=True)
        self.assertAlmostEqual(omitted["quic_window_start_s"], 5.1)
        self.assertAlmostEqual(omitted["quic_window_end_s"], 10.05)
        expected_rate = 16.0 * 4.9 / 4.95
        self.assertAlmostEqual(omitted["tcp_goodput_mbit_s"], expected_rate)
        self.assertAlmostEqual(omitted["tcp_goodput_mbit_s"],
                               explicit["tcp_goodput_mbit_s"])

    def test_unaccounted_bytes_cannot_be_replaced_with_silence(self):
        with self.assertRaises(ValueError):
            self.validate_window(unreported_bytes=1)


if __name__ == "__main__":
    unittest.main()
