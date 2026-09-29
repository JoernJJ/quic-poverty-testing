#!/usr/bin/env python3
"""Aggregate run-level CSV metrics without treating packets as replicates."""

from __future__ import annotations

import argparse
import csv
import math
import os
import statistics
from collections import defaultdict

from run_metrics import bootstrap_ci

GROUP_FIELDS = (
    "scenario",
    "arm",
    "cell",
    "gso",
    "rate_mbit",
    "rtt_ms",
    "cross_traffic",
    "capture_tstype",
    "capture_wire_equivalent",
)

MEDIAN_FIELDS = (
    "ipg_median_ms",
    "normalized_ipg_median",
    "burst_fraction",
    "frac_packets_in_trains_gt5",
    "ptl_max",
    "frac_gaps_below_wire_bound",
    "bottleneck_drops",
)


def number(row: dict[str, str], field: str) -> float:
    try:
        return float(row[field])
    except (KeyError, TypeError, ValueError):
        return float("nan")


def finite(rows: list[dict[str, str]], field: str) -> list[float]:
    return [value for row in rows if math.isfinite(value := number(row, field))]


def summarize(rows: list[dict[str, str]]) -> dict[str, object]:
    out: dict[str, object] = {field: rows[0].get(field, "") for field in GROUP_FIELDS}
    out["n"] = len(rows)
    for field in MEDIAN_FIELDS:
        values = finite(rows, field)
        out[f"{field}_median"] = statistics.median(values) if values else float("nan")
        low, high = bootstrap_ci(values) if len(values) >= 2 else (float("nan"), float("nan"))
        out[f"{field}_ci_low"] = low
        out[f"{field}_ci_high"] = high
    for field in ("goodput_mbit_s", "tcp_goodput_mbit_s", "quic_goodput_share"):
        values = finite(rows, field)
        out[f"{field}_mean"] = statistics.mean(values) if values else float("nan")
        out[f"{field}_population_sd"] = statistics.pstdev(values) if values else float("nan")
    drops = finite(rows, "bottleneck_drops")
    out["bottleneck_drops_sum"] = sum(drops) if drops else float("nan")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", required=True, help="run-level CSV from run_metrics.py")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    with open(args.runs, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise SystemExit(f"no rows in {args.runs}")

    groups: dict[tuple[str, ...], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[tuple(row.get(field, "") for field in GROUP_FIELDS)].append(row)
    summaries = [summarize(group) for _, group in sorted(groups.items())]

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summaries[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(summaries)
    print(f"{len(rows)} runs, {len(summaries)} cells -> {args.out}")


if __name__ == "__main__":
    main()
