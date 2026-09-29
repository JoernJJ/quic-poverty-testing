#!/usr/bin/env python3
"""Build the descriptive professional-versus-commodity comparison table."""

from __future__ import annotations

import argparse
import csv
import math
import statistics

from run_metrics import bootstrap_ci

MAPPINGS = (
    ("picoquic", "BBR", "default", "picoquic-bbr", "picoquic:bbr:default"),
    ("picoquic", "CUBIC", "default", "picoquic-cubic", "picoquic:cubic:default"),
    ("ngtcp2", "CUBIC", "default", "ngtcp2-cubic", "ngtcp2:cubic:default"),
    ("ngtcp2", "BBR", "default", "ngtcp2-bbr", "ngtcp2:bbr:default"),
    (
        "quiche",
        "CUBIC",
        "default",
        "quiche-gso-txtime-cubic-gso_unpaced",
        "quiche:cubic:default",
    ),
    (
        "quiche",
        "CUBIC",
        "fq",
        "quiche-gso-txtime-cubic-fq-gso_disabled-spurious_fixed",
        "quiche:cubic:fq",
    ),
)

GOODPUT = {
    ("picoquic", "CUBIC", "default"): (37.09, 0.03),
    ("ngtcp2", "CUBIC", "default"): (15.93, 0.00),
    ("quiche", "CUBIC", "default"): (34.67, 0.64),
    ("quiche", "CUBIC", "fq"): (31.71, 0.08),
}

DROPS = {
    ("picoquic", "CUBIC", "default"): (861.45, 99.53),
    ("ngtcp2", "CUBIC", "default"): (503.45, 7.39),
    ("quiche", "CUBIC", "default"): (687.15, 338.12),
    ("quiche", "CUBIC", "fq"): (160.80, 39.17),
}

FIELDS = (
    "implementation",
    "cca",
    "qdisc",
    "anchor_cell",
    "own_cell",
    "anchor_n",
    "own_n",
    "anchor_median_ipg_ms",
    "anchor_ipg_ci_low_ms",
    "anchor_ipg_ci_high_ms",
    "own_median_ipg_ms",
    "own_ipg_ci_low_ms",
    "own_ipg_ci_high_ms",
    "own_to_anchor_ipg_ratio",
    "anchor_burst_fraction",
    "own_burst_fraction",
    "anchor_ptl_gt5_share",
    "own_ptl_gt5_share",
    "anchor_median_run_ptl_max",
    "own_median_run_ptl_max",
    "anchor_goodput_mean_mbit_s",
    "anchor_goodput_sd_mbit_s",
    "own_goodput_mean_mbit_s",
    "own_goodput_sd_mbit_s",
    "anchor_bottleneck_drops_mean",
    "anchor_bottleneck_drops_sd",
    "own_bottleneck_drops_mean",
    "own_bottleneck_drops_sd",
    "comparison_scope",
)


def load(path: str) -> dict[str, list[dict[str, str]]]:
    cells: dict[str, list[dict[str, str]]] = {}
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            cells.setdefault(row["cell"], []).append(row)
    return cells


def values(rows: list[dict[str, str]], field: str) -> list[float]:
    return [float(row[field]) for row in rows]


def scope(implementation: str, qdisc: str) -> str:
    common = (
        "Descriptive only: professional optical-tap capture versus local sender-host capture; "
        "bottleneck queue implementations and endpoint hardware differ"
    )
    if implementation in {"picoquic", "ngtcp2"} or qdisc == "default":
        return common + "; professional default GSO enabled versus local GSO disabled"
    return common + "; both disable GSO, but quiche rollback controls may differ"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--anchor", required=True)
    parser.add_argument("--own", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    anchor = load(args.anchor)
    own = load(args.own)
    output: list[dict[str, object]] = []
    for implementation, cca, qdisc, anchor_cell, own_cell in MAPPINGS:
        anchor_rows = anchor[anchor_cell]
        own_rows = own.get(own_cell) or own[f"{own_cell}:gso0"]
        anchor_ipg = values(anchor_rows, "ipg_median_ms")
        own_ipg = values(own_rows, "ipg_median_ms")
        anchor_ci = bootstrap_ci(anchor_ipg)
        own_ci = bootstrap_ci(own_ipg)
        professional_goodput = GOODPUT.get((implementation, cca, qdisc), (math.nan, math.nan))
        professional_drops = DROPS.get((implementation, cca, qdisc), (math.nan, math.nan))
        own_goodput = values(own_rows, "goodput_mbit_s")
        output.append(
            {
                "implementation": implementation,
                "cca": cca,
                "qdisc": qdisc,
                "anchor_cell": anchor_cell,
                "own_cell": own_cell,
                "anchor_n": len(anchor_rows),
                "own_n": len(own_rows),
                "anchor_median_ipg_ms": statistics.median(anchor_ipg),
                "anchor_ipg_ci_low_ms": anchor_ci[0],
                "anchor_ipg_ci_high_ms": anchor_ci[1],
                "own_median_ipg_ms": statistics.median(own_ipg),
                "own_ipg_ci_low_ms": own_ci[0],
                "own_ipg_ci_high_ms": own_ci[1],
                "own_to_anchor_ipg_ratio": statistics.median(own_ipg) / statistics.median(anchor_ipg),
                "anchor_burst_fraction": statistics.median(values(anchor_rows, "burst_fraction")),
                "own_burst_fraction": statistics.median(values(own_rows, "burst_fraction")),
                "anchor_ptl_gt5_share": statistics.median(values(anchor_rows, "frac_packets_in_trains_gt5")),
                "own_ptl_gt5_share": statistics.median(values(own_rows, "frac_packets_in_trains_gt5")),
                "anchor_median_run_ptl_max": statistics.median(values(anchor_rows, "ptl_max")),
                "own_median_run_ptl_max": statistics.median(values(own_rows, "ptl_max")),
                "anchor_goodput_mean_mbit_s": professional_goodput[0],
                "anchor_goodput_sd_mbit_s": professional_goodput[1],
                "own_goodput_mean_mbit_s": statistics.mean(own_goodput),
                "own_goodput_sd_mbit_s": statistics.pstdev(own_goodput),
                "anchor_bottleneck_drops_mean": professional_drops[0],
                "anchor_bottleneck_drops_sd": professional_drops[1],
                "own_bottleneck_drops_mean": statistics.mean(values(own_rows, "bottleneck_drops")),
                "own_bottleneck_drops_sd": statistics.pstdev(values(own_rows, "bottleneck_drops")),
                "comparison_scope": scope(implementation, qdisc),
            }
        )

    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(output)
    print(f"{len(output)} descriptive comparisons -> {args.out}")


if __name__ == "__main__":
    main()
