#!/usr/bin/env python3
"""Compare matched artifact-version and current-version run-level cells."""

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
    "artifact_n",
    "current_n",
    "artifact_median_ipg_ms",
    "artifact_ipg_ci_low_ms",
    "artifact_ipg_ci_high_ms",
    "current_median_ipg_ms",
    "current_ipg_ci_low_ms",
    "current_ipg_ci_high_ms",
    "current_to_artifact_ipg_ratio",
    "artifact_burst_fraction",
    "current_burst_fraction",
    "artifact_median_drops",
    "current_median_drops",
    "artifact_goodput_mean_mbit_s",
    "artifact_goodput_sd_mbit_s",
    "current_goodput_mean_mbit_s",
    "current_goodput_sd_mbit_s",
    "current_minus_artifact_goodput_mbit_s",
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
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--current", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    artifact = load(args.artifact)
    current = load(args.current)
    if artifact.keys() != current.keys():
        missing_current = sorted(artifact.keys() - current.keys())
        missing_artifact = sorted(current.keys() - artifact.keys())
        raise SystemExit(
            f"cell mismatch: missing current={missing_current}, missing artifact={missing_artifact}"
        )

    output: list[dict[str, object]] = []
    for cell in sorted(artifact):
        artifact_rows = artifact[cell]
        current_rows = current[cell]
        implementation, cca, qdisc, _ = cell.split(":")
        artifact_ipg = values(artifact_rows, "ipg_median_ms")
        current_ipg = values(current_rows, "ipg_median_ms")
        artifact_ipg_ci = bootstrap_ci(artifact_ipg)
        current_ipg_ci = bootstrap_ci(current_ipg)
        artifact_goodput = values(artifact_rows, "goodput_mbit_s")
        current_goodput = values(current_rows, "goodput_mbit_s")
        artifact_ipg_median = statistics.median(artifact_ipg)
        current_ipg_median = statistics.median(current_ipg)
        artifact_goodput_mean = statistics.mean(artifact_goodput)
        current_goodput_mean = statistics.mean(current_goodput)
        output.append(
            {
                "implementation": implementation,
                "cca": cca.upper(),
                "qdisc": qdisc,
                "cell": cell,
                "artifact_n": len(artifact_rows),
                "current_n": len(current_rows),
                "artifact_median_ipg_ms": artifact_ipg_median,
                "artifact_ipg_ci_low_ms": artifact_ipg_ci[0],
                "artifact_ipg_ci_high_ms": artifact_ipg_ci[1],
                "current_median_ipg_ms": current_ipg_median,
                "current_ipg_ci_low_ms": current_ipg_ci[0],
                "current_ipg_ci_high_ms": current_ipg_ci[1],
                "current_to_artifact_ipg_ratio": current_ipg_median / artifact_ipg_median,
                "artifact_burst_fraction": statistics.median(values(artifact_rows, "burst_fraction")),
                "current_burst_fraction": statistics.median(values(current_rows, "burst_fraction")),
                "artifact_median_drops": statistics.median(values(artifact_rows, "bottleneck_drops")),
                "current_median_drops": statistics.median(values(current_rows, "bottleneck_drops")),
                "artifact_goodput_mean_mbit_s": artifact_goodput_mean,
                "artifact_goodput_sd_mbit_s": statistics.pstdev(artifact_goodput),
                "current_goodput_mean_mbit_s": current_goodput_mean,
                "current_goodput_sd_mbit_s": statistics.pstdev(current_goodput),
                "current_minus_artifact_goodput_mbit_s": current_goodput_mean - artifact_goodput_mean,
                "comparison_scope": (
                    "Matched commodity hardware, queue, RTT, GSO state and sender-host capture; "
                    "implementation version and its frozen dependency graph differ"
                ),
            }
        )

    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(output)
    print(f"{len(output)} matched version comparisons -> {args.out}")


if __name__ == "__main__":
    main()
