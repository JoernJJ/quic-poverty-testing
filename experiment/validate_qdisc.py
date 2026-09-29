#!/usr/bin/env python3
"""Validate the receiver bottleneck qdisc hierarchy against campaign settings."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


def load(path: str) -> list[dict[str, object]]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError(f"{path}: expected a JSON list")
    return value


def one(qdiscs: list[dict[str, object]], *, kind: str, parent: str | None = None) -> dict[str, object]:
    matches = [
        qdisc
        for qdisc in qdiscs
        if qdisc.get("kind") == kind and (parent is None or qdisc.get("parent") == parent)
    ]
    if len(matches) != 1:
        location = f" parent {parent}" if parent else ""
        raise ValueError(f"expected one {kind}{location}, found {len(matches)}")
    return matches[0]


def options(qdisc: dict[str, object]) -> dict[str, object]:
    value = qdisc.get("options")
    if not isinstance(value, dict):
        raise ValueError(f"{qdisc.get('kind')}: missing options")
    return value


def close(actual: float, expected: float, tolerance: float, label: str) -> None:
    if not math.isclose(actual, expected, rel_tol=0.0, abs_tol=tolerance):
        raise ValueError(f"{label}: got {actual}, expected {expected} +/- {tolerance}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ifb", required=True)
    parser.add_argument("--client", required=True)
    parser.add_argument("--rate-mbit", type=int, required=True)
    parser.add_argument("--rtt-ms", type=int, required=True)
    parser.add_argument("--burst-bytes", type=int, required=True)
    parser.add_argument("--bottleneck-limit", type=int, required=True)
    parser.add_argument("--return-limit", type=int, required=True)
    args = parser.parse_args()

    ifb = load(args.ifb)
    client = load(args.client)
    tbf = options(one(ifb, kind="tbf"))
    bottleneck = options(one(ifb, kind="netem", parent="1:1"))
    return_path = options(one(client, kind="netem"))

    close(float(tbf["rate"]), args.rate_mbit * 1_000_000 / 8, 1.0, "TBF byte rate")
    # tc may account for link-layer cell alignment and report 1535 B after a
    # requested 1540 B bucket, as in the released artifact.
    close(float(tbf["burst"]), args.burst_bytes, 16.0, "TBF burst")
    if int(bottleneck["limit"]) != args.bottleneck_limit:
        raise ValueError(
            f"bottleneck netem limit: got {bottleneck['limit']}, expected {args.bottleneck_limit}"
        )
    if int(return_path["limit"]) != args.return_limit:
        raise ValueError(
            f"return netem limit: got {return_path['limit']}, expected {args.return_limit}"
        )

    forward_delay = bottleneck.get("delay")
    return_delay = return_path.get("delay")
    if not isinstance(forward_delay, dict) or not isinstance(return_delay, dict):
        raise ValueError("netem delay options missing")
    expected_half_s = args.rtt_ms / 2 / 1000
    close(float(forward_delay["delay"]), expected_half_s, 1e-9, "forward delay")
    close(float(return_delay["delay"]), expected_half_s, 1e-9, "return delay")

    print(
        f"rate={args.rate_mbit}Mbit/s burst={tbf['burst']}B "
        f"forward_limit={bottleneck['limit']} return_limit={return_path['limit']} "
        f"half_delay={expected_half_s * 1000:g}ms"
    )


if __name__ == "__main__":
    try:
        main()
    except (KeyError, TypeError, ValueError) as error:
        raise SystemExit(str(error)) from None
