#!/usr/bin/env python3
"""Run-level pacing metrics and a capture-validity check.

Why this exists
---------------
The anchor artifact (Kempf et al., DOI 10.1145/3730985) ships one packet list
per repetition under

    analysis/build/data/<cell>/<cell>-rep<N>.pcap.json
        rows: [relative_ns, raw_ns, frame_len, packet_number]
    analysis/build/data/<cell>/<cell>-rep<N>.ipg.json
        gaps in milliseconds

The artifact's own `02_plotting.py` concatenates those lists across repetitions,
so every published CDF pools dependent packets from 20 connections.  This script
treats the *run* as the experimental unit: one metric vector per repetition,
then a percentile bootstrap CI of the median across repetitions.

Capture-validity check
----------------------
A start-to-start gap between two frames on a shared link cannot be shorter than
the wire time of the earlier frame:

    (frame_len + 4 FCS + 8 preamble/SFD + 12 IFG) * 8 / link_rate

Gaps below that bound are physically impossible and indicate an observation
point *before* NIC transmission (host/qdisc software timestamps) or a GSO
aggregate counted as one frame.  Calibration against the artifact data: the
in-train gap mode equals the bound to within 1 ns for every implementation
(picoquic 1294 B -> 10544 ns, quiche 1392 B -> 11328 ns, TCP/TLS 1514 B ->
12304 ns), and only ~0.2 % of gaps fall 1-2 ns under it, which is sniffer
rounding.  `TOLERANCE_NS` absorbs exactly that.  A capture whose
`frac_gaps_below_wire_bound` is materially above zero is not wire-equivalent.

The same reducer runs on own measurements (see `pcap_reader.py`, which writes the
same row format), so artifact data and own data are never reduced differently.
It also reads `run-campaign.sh` output directly: successful manifests are grouped
by their recorded cell, while failed or incomplete run directories are excluded.

Usage
-----
    python run_metrics.py                                  # artifact data
    python run_metrics.py --data <artifact-or-campaign-dir> --out build/own.csv
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import os
import random
import statistics
import sys
from dataclasses import asdict, dataclass, fields

from validate_contention import validate as validate_contention

# Burst threshold from Kempf et al. Section 3: gaps below this are
# "back-to-back" and keep the packet train open.
BURST_THRESHOLD_NS = 100_000

# pcap records the frame without the 4-byte FCS; each frame is additionally
# preceded by a 7-byte preamble + 1-byte SFD and followed by a 12-byte IFG.
ETHERNET_OVERHEAD_BYTES = 4 + 8 + 12

# Sniffer timestamp rounding observed in the artifact data (1-2 ns).
TOLERANCE_NS = 2

BOOTSTRAP_SAMPLES = 10000
BOOTSTRAP_SEED = 20260830

# pcap.json column layout, produced by the artifact's 01_preprocessing.py
COL_RELATIVE_NS = 0
COL_FRAME_LEN = 2


@dataclass
class RunMetrics:
    """Metrics for one repetition (one connection)."""

    cell: str
    run: str
    packets: int
    duration_s: float
    wire_mbit_s: float
    frame_len_median: float
    ipg_min_ms: float
    ipg_p10_ms: float
    ipg_median_ms: float
    ipg_p90_ms: float
    burst_fraction: float
    ptl_median: float
    ptl_p95: float
    ptl_max: int
    frac_packets_in_trains_gt5: float
    frac_gaps_below_wire_bound: float
    normalized_ipg_p10: float
    normalized_ipg_median: float
    normalized_ipg_p90: float
    frac_gaps_below_half_pacing_gap: float
    scenario: str = ""
    arm: str = ""
    implementation: str = ""
    congestion_control: str = ""
    sender_qdisc: str = ""
    gso: int = -1
    rate_mbit: float = float("nan")
    rtt_ms: float = float("nan")
    cross_traffic: int = 0
    capture_tstype: str = ""
    capture_wire_equivalent: int = 0
    kernel: str = ""
    goodput_mbit_s: float = float("nan")
    bottleneck_drops: float = float("nan")
    tcp_goodput_mbit_s: float = float("nan")
    quic_goodput_share: float = float("nan")

def quantile(sorted_values: list[float], q: float) -> float:
    """Nearest-rank quantile on an already sorted list."""
    if not sorted_values:
        return float("nan")
    idx = min(len(sorted_values) - 1, max(0, int(round(q * (len(sorted_values) - 1)))))
    return sorted_values[idx]


def packet_trains(gaps_ns: list[int], threshold: int = BURST_THRESHOLD_NS) -> list[int]:
    """Packet-train lengths in packets.

    `gaps_ns[i]` separates packet i and i+1.  A train is a maximal sequence of
    packets whose consecutive gaps are all below `threshold`; an isolated packet
    is a train of length 1.  n gaps describe n+1 packets, so the emitted lengths
    sum to len(gaps_ns) + 1.
    """
    trains: list[int] = []
    current = 1
    for gap in gaps_ns:
        if gap < threshold:
            current += 1
        else:
            trains.append(current)
            current = 1
    trains.append(current)
    return trains


def reduce_run(
    cell: str,
    run: str,
    gaps_ns: list[int],
    frame_lens: list[int] | None,
    link_rate_bit_s: float,
    bottleneck_rate_bit_s: float | None,
) -> RunMetrics:
    """Reduce one run. `frame_lens[i]` is the frame preceding `gaps_ns[i]`."""
    ordered = sorted(gaps_ns)
    trains = packet_trains(gaps_ns)
    total_packets = len(gaps_ns) + 1
    packets_in_long_trains = sum(t for t in trains if t > 5)
    duration_s = sum(gaps_ns) / 1e9

    normalized: list[float] = []
    if frame_lens:
        below_bound = 0
        for gap_ns, length in zip(gaps_ns, frame_lens):
            bound_ns = (length + ETHERNET_OVERHEAD_BYTES) * 8 / link_rate_bit_s * 1e9
            if gap_ns + TOLERANCE_NS < bound_ns:
                below_bound += 1
            if bottleneck_rate_bit_s:
                pacing_gap_ns = (
                    (length + ETHERNET_OVERHEAD_BYTES)
                    * 8
                    / bottleneck_rate_bit_s
                    * 1e9
                )
                normalized.append(gap_ns / pacing_gap_ns)
        frac_below = below_bound / len(gaps_ns)
        len_median = statistics.median(frame_lens)
        wire_mbit = (sum(frame_lens) * 8 / duration_s / 1e6) if duration_s > 0 else float("nan")
    else:
        frac_below = float("nan")
        len_median = float("nan")
        wire_mbit = float("nan")

    normalized_ordered = sorted(normalized)
    norm_p10 = quantile(normalized_ordered, 0.10) if normalized else float("nan")
    norm_median = quantile(normalized_ordered, 0.50) if normalized else float("nan")
    norm_p90 = quantile(normalized_ordered, 0.90) if normalized else float("nan")
    frac_half = (
        sum(value < 0.5 for value in normalized) / len(normalized)
        if normalized
        else float("nan")
    )

    return RunMetrics(
        cell=cell,
        run=run,
        packets=total_packets,
        duration_s=duration_s,
        wire_mbit_s=wire_mbit,
        frame_len_median=len_median,
        ipg_min_ms=ordered[0] / 1e6,
        ipg_p10_ms=quantile(ordered, 0.10) / 1e6,
        ipg_median_ms=quantile(ordered, 0.50) / 1e6,
        ipg_p90_ms=quantile(ordered, 0.90) / 1e6,
        burst_fraction=sum(1 for g in gaps_ns if g < BURST_THRESHOLD_NS) / len(gaps_ns),
        ptl_median=statistics.median(trains),
        ptl_p95=quantile(sorted(map(float, trains)), 0.95),
        ptl_max=max(trains),
        frac_packets_in_trains_gt5=packets_in_long_trains / total_packets,
        frac_gaps_below_wire_bound=frac_below,
        normalized_ipg_p10=norm_p10,
        normalized_ipg_median=norm_median,
        normalized_ipg_p90=norm_p90,
        frac_gaps_below_half_pacing_gap=frac_half,
    )


def bootstrap_ci(values: list[float], samples: int = BOOTSTRAP_SAMPLES) -> tuple[float, float]:
    """Percentile bootstrap 95% CI of the median across runs."""
    if len(values) < 2:
        return (float("nan"), float("nan"))
    rng = random.Random(BOOTSTRAP_SEED)
    n = len(values)
    medians = sorted(statistics.median(rng.choices(values, k=n)) for _ in range(samples))
    return (quantile(medians, 0.025), quantile(medians, 0.975))


def load_run(
    path: str,
    cell: str,
    link_rate_bit_s: float,
    bottleneck_rate_bit_s: float | None,
) -> RunMetrics | None:
    """Load one repetition from `*.pcap.json` (preferred) or `*.ipg.json`."""
    if path.endswith(".pcap.json"):
        run = os.path.basename(path).removesuffix(".pcap.json")
        with open(path, encoding="utf-8") as handle:
            rows = json.load(handle)
        if len(rows) < 2:
            return None
        times = [row[COL_RELATIVE_NS] for row in rows]
        gaps_ns = [times[i + 1] - times[i] for i in range(len(times) - 1)]
        frame_lens = [row[COL_FRAME_LEN] for row in rows[:-1]]
        return reduce_run(
            cell,
            run,
            gaps_ns,
            frame_lens,
            link_rate_bit_s,
            bottleneck_rate_bit_s,
        )

    run = os.path.basename(path).removesuffix(".ipg.json")
    with open(path, encoding="utf-8") as handle:
        gaps_ms = json.load(handle)
    if not gaps_ms:
        return None
    gaps_ns = [round(gap * 1e6) for gap in gaps_ms]
    return reduce_run(
        cell,
        run,
        gaps_ns,
        None,
        link_rate_bit_s,
        bottleneck_rate_bit_s,
    )


def client_duration_s(run_dir: str) -> float:
    for relative in ("client/time.json", "client/logs/time.json"):
        path = os.path.join(run_dir, relative)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as handle:
            timing = json.load(handle)
        duration = (int(timing["end"]) - int(timing["start"])) / 1e9
        if duration > 0:
            return duration
    return float("nan")


def bottleneck_drop_delta(run_dir: str) -> float:
    def leaf_drops(filename: str) -> int:
        with open(os.path.join(run_dir, filename), encoding="utf-8") as handle:
            qdiscs = json.load(handle)
        matches = [
            qdisc
            for qdisc in qdiscs
            if qdisc.get("kind") == "netem" and qdisc.get("parent") == "1:1"
        ]
        if len(matches) != 1:
            raise ValueError(f"{filename}: expected one bottleneck netem parent 1:1")
        return int(matches[0].get("drops", 0))

    try:
        before = leaf_drops("client-qdisc-before.json")
        after = leaf_drops("client-qdisc-after.json")
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return float("nan")
    return float(after - before) if after >= before else float("nan")


def tcp_goodput_during_quic(
    run_dir: str, cross_start_ns: int, cross_stop_ns: int, lead_s: float
) -> float:
    summary = validate_contention(
        os.path.join(run_dir, "cross-traffic.json"),
        os.path.join(run_dir, "client", "time.json"),
        os.path.join(run_dir, "client-transfer-markers.json"),
        cross_start_ns,
        cross_stop_ns,
        lead_s,
    )
    return float(summary["tcp_goodput_mbit_s"])


def enrich_campaign_run(run: RunMetrics, manifest: dict[str, object], run_dir: str) -> None:
    run.scenario = str(manifest.get("scenario", "legacy-n1"))
    run.arm = str(manifest.get("arm", ""))
    run.implementation = str(manifest.get("implementation", ""))
    run.congestion_control = str(manifest.get("congestion_control", ""))
    run.sender_qdisc = str(manifest.get("sender_qdisc", ""))
    run.gso = int(manifest.get("gso", -1))
    run.rate_mbit = float(manifest.get("rate_mbit", float("nan")))
    run.rtt_ms = float(manifest.get("rtt_ms", float("nan")))
    run.cross_traffic = int(manifest.get("cross_traffic", 0))
    run.capture_tstype = str(manifest.get("capture_tstype", ""))
    run.capture_wire_equivalent = int(manifest.get("capture_wire_equivalent", 0))
    run.kernel = str(manifest.get("sender_kernel", manifest.get("kernel", "")))

    duration = client_duration_s(run_dir)
    downloaded = float(manifest.get("downloaded_bytes", float("nan")))
    if math.isfinite(duration) and duration > 0 and math.isfinite(downloaded):
        run.goodput_mbit_s = downloaded * 8 / duration / 1e6
    recorded_drops = manifest.get("bottleneck_drops")
    run.bottleneck_drops = (
        float(recorded_drops)
        if isinstance(recorded_drops, (int, float))
        else bottleneck_drop_delta(run_dir)
    )
    if run.cross_traffic:
        lead_s = float(manifest.get("cross_lead_s", 5))
        try:
            cross_start_ns = int(manifest["cross_start_ns"])
            cross_stop_ns = int(manifest["cross_stop_ns"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(
                f"{run.run}: missing exact cross-traffic boundary timestamp"
            ) from error
        run.tcp_goodput_mbit_s = tcp_goodput_during_quic(
            run_dir, cross_start_ns, cross_stop_ns, lead_s
        )
        total = run.goodput_mbit_s + run.tcp_goodput_mbit_s
        if math.isfinite(total) and total > 0:
            run.quic_goodput_share = run.goodput_mbit_s / total


def load_cells(
    data_dir: str,
    link_rate_bit_s: float,
    default_bottleneck_rate_bit_s: float,
) -> dict[str, list[RunMetrics]]:
    cells: dict[str, list[RunMetrics]] = {}

    # Native campaign layout: <arm>/<run-id>/{manifest.json,<run-id>.pcap.json}.
    # The manifest is authoritative for both cell identity and validity.
    manifests = sorted(glob.glob(os.path.join(data_dir, "*", "manifest.json")))
    if manifests:
        seen: set[tuple[object, ...]] = set()
        for manifest_path in manifests:
            with open(manifest_path, encoding="utf-8") as handle:
                manifest = json.load(handle)
            if manifest.get("status") != "ok":
                continue
            cell = manifest.get("cell")
            if not isinstance(cell, str) or not cell:
                continue
            logical_run = (
                manifest.get("scenario", "legacy-n1"),
                manifest.get("arm"),
                cell,
                manifest.get("gso"),
                manifest.get("repetition"),
            )
            if logical_run in seen:
                print(
                    f"WARN: ignoring duplicate successful logical run: {manifest_path}",
                    file=sys.stderr,
                )
                continue
            run_dir = os.path.dirname(manifest_path)
            paths = sorted(glob.glob(os.path.join(run_dir, "*.pcap.json")))
            if len(paths) != 1:
                print(
                    f"WARN: expected one pcap JSON beside {manifest_path}, found {len(paths)}",
                    file=sys.stderr,
                )
                continue
            rate_mbit = float(manifest.get("rate_mbit", default_bottleneck_rate_bit_s / 1e6))
            run = load_run(paths[0], cell, link_rate_bit_s, rate_mbit * 1e6)
            if run:
                enrich_campaign_run(run, manifest, run_dir)
                cells.setdefault(cell, []).append(run)
                seen.add(logical_run)
        return cells

    # Anchor-artifact layout: <data>/<cell>/<cell>-rep<N>.pcap.json.
    for entry in sorted(os.listdir(data_dir)):
        cell_dir = os.path.join(data_dir, entry)
        if not os.path.isdir(cell_dir):
            continue
        paths = sorted(glob.glob(os.path.join(cell_dir, "*.pcap.json")))
        if not paths:
            paths = sorted(glob.glob(os.path.join(cell_dir, "*.ipg.json")))
        runs = [
            run
            for path in paths
            if (
                run := load_run(
                    path,
                    entry,
                    link_rate_bit_s,
                    default_bottleneck_rate_bit_s,
                )
            )
        ]
        for run in runs:
            run.scenario = "professional-n1"
            run.arm = "professional"
            run.rate_mbit = default_bottleneck_rate_bit_s / 1e6
            run.rtt_ms = 40.0
            run.capture_tstype = "moongen"
            run.capture_wire_equivalent = 1
        if runs:
            cells[entry] = runs
    return cells


def print_summary(cells: dict[str, list[RunMetrics]]) -> None:
    name_width = max(len(c) for c in cells)
    header = (
        f"{'cell':<{name_width}} {'n':>3} {'median IPG ms':>24} "
        f"{'burst frac':>22} {'pkts in PTL>5':>14} {'PTL max':>8} {'sub-wire':>9}"
    )
    print(header)
    print("-" * len(header))
    for cell, runs in cells.items():
        med = [r.ipg_median_ms for r in runs]
        burst = [r.burst_fraction for r in runs]
        long_trains = [r.frac_packets_in_trains_gt5 for r in runs]
        ptl_max = [float(r.ptl_max) for r in runs]
        sub_wire = [r.frac_gaps_below_wire_bound for r in runs]
        med_lo, med_hi = bootstrap_ci(med)
        burst_lo, burst_hi = bootstrap_ci(burst)
        print(
            f"{cell:<{name_width}} {len(runs):>3} "
            f"{statistics.median(med):>8.4f} [{med_lo:.4f},{med_hi:.4f}] "
            f"{statistics.median(burst):>7.3f} [{burst_lo:.3f},{burst_hi:.3f}] "
            f"{statistics.median(long_trains):>13.3f} "
            f"{statistics.median(ptl_max):>8.0f} "
            f"{statistics.median(sub_wire):>9.4f}"
        )


def write_csv(cells: dict[str, list[RunMetrics]], out_path: str) -> None:
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    columns = [f.name for f in fields(RunMetrics)]
    with open(out_path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for runs in cells.values():
            for run in runs:
                writer.writerow(asdict(run))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data",
        default=os.path.join("..", "quic-pacing-paper-main", "analysis", "build", "data"),
        help="artifact <cell> directories or a run-campaign arm directory",
    )
    parser.add_argument("--out", default=os.path.join("build", "run-metrics.csv"))
    parser.add_argument(
        "--link-rate-gbit",
        type=float,
        default=1.0,
        help="capture-link line rate for the wire-feasibility bound",
    )
    parser.add_argument(
        "--bottleneck-rate-mbit",
        type=float,
        default=40.0,
        help="default bottleneck rate for artifact data; campaign manifests override it",
    )
    args = parser.parse_args()

    cells = load_cells(
        args.data,
        args.link_rate_gbit * 1e9,
        args.bottleneck_rate_mbit * 1e6,
    )
    if not cells:
        raise SystemExit(f"no *.pcap.json / *.ipg.json found under {args.data}")
    print_summary(cells)
    write_csv(cells, args.out)
    total = sum(len(runs) for runs in cells.values())
    print(f"\n{len(cells)} cells, {total} runs -> {args.out}")
    print(
        "sub-wire = share of gaps shorter than the preceding frame's wire time at "
        f"{args.link_rate_gbit} Gbit/s (tolerance {TOLERANCE_NS} ns); "
        "materially above zero means the capture is not wire-equivalent."
    )


if __name__ == "__main__":
    main()
