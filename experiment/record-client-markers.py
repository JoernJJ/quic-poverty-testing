#!/usr/bin/env python3
"""Timestamp remote transfer markers on the sender while mirroring client output."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

MARKER = re.compile(r"^(QUIC_CLIENT_TRANSFER_START|QUIC_CLIENT_TRANSFER_END):(\d+):(\d+)")
EXPECTED_SAMPLES = 5


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    samples: dict[str, list[dict[str, int]]] = {"start": [], "end": []}
    for line in sys.stdin:
        now_ns = time.time_ns()
        match = MARKER.match(line)
        if not match:
            print(line, end="", flush=True)
            continue
        phase = "start" if match.group(1).endswith("START") else "end"
        samples[phase].append(
            {
                "index": int(match.group(2)),
                "remote_ns": int(match.group(3)),
                "sender_receipt_ns": now_ns,
            }
        )

    Path(args.out).write_text(
        json.dumps(samples, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    for phase, phase_samples in samples.items():
        indices = {sample["index"] for sample in phase_samples}
        if len(phase_samples) != EXPECTED_SAMPLES or indices != set(
            range(1, EXPECTED_SAMPLES + 1)
        ):
            raise SystemExit(f"client transfer {phase} markers are incomplete or duplicated")


if __name__ == "__main__":
    main()
