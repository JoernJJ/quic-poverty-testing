#!/usr/bin/env python3
"""Compare matched idle and one-TCP-flow current-version cells."""

from __future__ import annotations

import argparse
import csv
import math
import statistics

from run_metrics import bootstrap_ci


FIELDS = (
    "implementation",
    "cca",
    "qdisc",
    "cell",
    "idle_n",
    "contention_n",
    "idle_median_ipg_ms",
    "idle_ipg_ci_low_ms",
    "idle_ipg_ci_high_ms",
    "contention_median_ipg_ms",
    "contention_ipg_ci_low_ms",
    "contention_ipg_ci_high_ms",
    "contention_to_idle_ipg_ratio",
    "idle_burst_fraction",
    "contention_burst_fraction",
    "idle_median_drops",
    "contention_median_drops",
    "idle_quic_goodput_mean_mbit_s",
    "idle_quic_goodput_sd_mbit_s",
    "contention_quic_goodput_mean_mbit_s",
    "contention_quic_goodput_sd_mbit_s",
    "contention_tcp_goodput_mean_mbit_s",
    "contention_tcp_goodput_sd_mbit_s",
    "contention_total_goodput_mean_mbit_s",
    "contention_quic_share_mean",
    "contention_quic_share_sd",
    "comparison_scope",
)


def load(path: str) -> dict[str, list[dict[str, str]]]:
    cells: dict[str, list[dict[str, str]]] = {}
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            cells.setdefault(row["cell"], []).append(row)
    return cells


def values(rows: list[dict[str, str]], field: str) -> list[float]:
    result = [float(row[field]) for row in rows]
    if not result or not all(math.isfinite(value) for value in result):
        raise ValueError(f"{field} contains no finite complete sample")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--idle", required=True)
    parser.add_argument("--contention", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    idle = load(args.idle)
    contention = load(args.contention)
    if idle.keys() != contention.keys():
        missing_contention = sorted(idle.keys() - contention.keys())
        missing_idle = sorted(contention.keys() - idle.keys())
        raise SystemExit(
            f"cell mismatch: missing contention={missing_contention}, missing idle={missing_idle}"
        )

    output: list[dict[str, object]] = []
    for cell in sorted(idle):
        idle_rows = idle[cell]
        contention_rows = contention[cell]
        implementation, cca, qdisc, _ = cell.split(":")
        idle_ipg = values(idle_rows, "ipg_median_ms")
        contention_ipg = values(contention_rows, "ipg_median_ms")
        idle_ipg_ci = bootstrap_ci(idle_ipg)
        contention_ipg_ci = bootstrap_ci(contention_ipg)
        idle_quic = values(idle_rows, "goodput_mbit_s")
        contention_quic = values(contention_rows, "goodput_mbit_s")
        contention_tcp = values(contention_rows, "tcp_goodput_mbit_s")
        contention_share = values(contention_rows, "quic_goodput_share")
        total_goodput = [
            quic + tcp for quic, tcp in zip(contention_quic, contention_tcp)
        ]
        idle_ipg_median = statistics.median(idle_ipg)
        contention_ipg_median = statistics.median(contention_ipg)
        output.append(
            {
                "implementation": implementation,
                "cca": cca.upper(),
                "qdisc": qdisc,
                "cell": cell,
                "idle_n": len(idle_rows),
                "contention_n": len(contention_rows),
                "idle_median_ipg_ms": idle_ipg_median,
                "idle_ipg_ci_low_ms": idle_ipg_ci[0],
                "idle_ipg_ci_high_ms": idle_ipg_ci[1],
                "contention_median_ipg_ms": contention_ipg_median,
                "contention_ipg_ci_low_ms": contention_ipg_ci[0],
                "contention_ipg_ci_high_ms": contention_ipg_ci[1],
                "contention_to_idle_ipg_ratio": contention_ipg_median / idle_ipg_median,
                "idle_burst_fraction": statistics.median(values(idle_rows, "burst_fraction")),
                "contention_burst_fraction": statistics.median(values(contention_rows, "burst_fraction")),
                "idle_median_drops": statistics.median(values(idle_rows, "bottleneck_drops")),
                "contention_median_drops": statistics.median(values(contention_rows, "bottleneck_drops")),
                "idle_quic_goodput_mean_mbit_s": statistics.mean(idle_quic),
                "idle_quic_goodput_sd_mbit_s": statistics.pstdev(idle_quic),
                "contention_quic_goodput_mean_mbit_s": statistics.mean(contention_quic),
                "contention_quic_goodput_sd_mbit_s": statistics.pstdev(contention_quic),
                "contention_tcp_goodput_mean_mbit_s": statistics.mean(contention_tcp),
                "contention_tcp_goodput_sd_mbit_s": statistics.pstdev(contention_tcp),
                "contention_total_goodput_mean_mbit_s": statistics.mean(total_goodput),
                "contention_quic_share_mean": statistics.mean(contention_share),
                "contention_quic_share_sd": statistics.pstdev(contention_share),
                "comparison_scope": (
                    "Same frozen current software, commodity hardware, queue, RTT, GSO state "
                    "and sender-host capture; contention adds one long-lived TCP CUBIC flow"
                ),
            }
        )

    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(output)
    print(f"{len(output)} matched contention comparisons -> {args.out}")


if __name__ == "__main__":
    main()
