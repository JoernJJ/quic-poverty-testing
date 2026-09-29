#!/usr/bin/env python3
"""Validate sender-throughput coverage of an estimated QUIC client window."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

EXPECTED_INTERRUPT = "interrupt - the client has terminated"
EXPECTED_MARKER_SAMPLES = 5
COVERAGE_TOLERANCE_S = 0.001
MAX_IPERF_STARTUP_S = 0.5


def load_json(path: str) -> dict[str, object]:
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def marker_samples(markers: dict[str, object], phase: str) -> list[dict[str, int]]:
    raw_samples = markers.get(phase)
    if not isinstance(raw_samples, list) or len(raw_samples) != EXPECTED_MARKER_SAMPLES:
        raise ValueError(f"expected {EXPECTED_MARKER_SAMPLES} {phase} marker samples")
    samples: list[dict[str, int]] = []
    for raw in raw_samples:
        if not isinstance(raw, dict):
            raise ValueError(f"{phase} marker sample is not an object")
        sample = {
            "index": int(raw["index"]),
            "remote_ns": int(raw["remote_ns"]),
            "sender_receipt_ns": int(raw["sender_receipt_ns"]),
        }
        samples.append(sample)
    if {sample["index"] for sample in samples} != set(
        range(1, EXPECTED_MARKER_SAMPLES + 1)
    ):
        raise ValueError(f"{phase} marker indices are incomplete or duplicated")
    return samples


def client_window_s(
    client_time_path: str, client_markers_path: str, iperf_origin_ns: int
) -> tuple[float, float, dict[str, float | int]]:
    timing = load_json(client_time_path)
    markers = load_json(client_markers_path)
    client_start_ns = int(timing["start"])
    client_end_ns = int(timing["end"])
    if client_end_ns <= client_start_ns:
        raise ValueError("invalid client timestamps")

    start_samples = marker_samples(markers, "start")
    end_samples = marker_samples(markers, "end")
    all_samples = start_samples + end_samples
    offset_candidates = [
        sample["sender_receipt_ns"] - sample["remote_ns"] for sample in all_samples
    ]
    # Each candidate is clock offset plus a non-negative delivery delay. The
    # minimum of ten samples is the closest mapping; preflight independently
    # bounds the management-path RTT that limits the residual one-way delay.
    remote_to_sender_offset_ns = min(offset_candidates)
    mapped_start_ns = client_start_ns + remote_to_sender_offset_ns
    mapped_end_ns = client_end_ns + remote_to_sender_offset_ns

    last_start_marker_ns = max(sample["remote_ns"] for sample in start_samples)
    first_end_marker_ns = min(sample["remote_ns"] for sample in end_samples)
    start_boundary_s = (client_start_ns - last_start_marker_ns) / 1e9
    end_boundary_s = (first_end_marker_ns - client_end_ns) / 1e9
    if start_boundary_s < 0 or end_boundary_s < 0:
        raise ValueError("remote client timestamps fall outside their marker boundaries")

    diagnostics: dict[str, float | int] = {
        "remote_to_sender_offset_ns": remote_to_sender_offset_ns,
        "marker_offset_candidate_spread_s": (
            max(offset_candidates) - min(offset_candidates)
        )
        / 1e9,
        "client_start_after_last_start_marker_s": start_boundary_s,
        "first_end_marker_after_client_end_s": end_boundary_s,
        "last_end_marker_receipt_ns": max(
            sample["sender_receipt_ns"] for sample in end_samples
        ),
    }
    return (
        (mapped_start_ns - iperf_origin_ns) / 1e9,
        (mapped_end_ns - iperf_origin_ns) / 1e9,
        diagnostics,
    )


def interval_rows(result: dict[str, object]) -> list[tuple[float, float, float]]:
    intervals = result.get("intervals")
    if not isinstance(intervals, list) or not intervals:
        raise ValueError("iperf3 JSON contains no intervals")

    rows: list[tuple[float, float, float]] = []
    for index, interval in enumerate(intervals):
        if not isinstance(interval, dict):
            raise ValueError(f"interval {index} is not an object")
        summary = interval.get("sum") or interval.get("sum_sent")
        if not isinstance(summary, dict):
            raise ValueError(f"interval {index} has no sum")
        start = float(summary["start"])
        end = float(summary["end"])
        bits_per_second = float(summary["bits_per_second"])
        if not all(math.isfinite(value) for value in (start, end, bits_per_second)):
            raise ValueError(f"interval {index} contains a non-finite value")
        if end <= start or bits_per_second < 0:
            raise ValueError(f"interval {index} has an invalid range or rate")
        if rows and abs(start - rows[-1][1]) > COVERAGE_TOLERANCE_S:
            raise ValueError(f"interval {index} is not contiguous with its predecessor")
        rows.append((start, end, bits_per_second))

    # iperf3 can omit a short zero-byte terminal interval (iperf_api.c,
    # iperf_print_intermediate). Its final sender summary still includes that
    # elapsed time. Anchoring SIGINT to the last *printed* interval shifts the
    # entire client window. Recover silence only when byte accounting proves it.
    final = result.get("end", {}).get("sum_sent")
    if not isinstance(final, dict) or final.get("sender") is not True:
        raise ValueError("iperf3 JSON has no final sender summary")
    final_end = float(final["end"])
    if not math.isfinite(final_end) or final_end < rows[-1][1]:
        raise ValueError("final sender endpoint precedes the reported intervals")
    if final_end > rows[-1][1]:
        summaries = [interval.get("sum") or interval.get("sum_sent") for interval in intervals]
        byte_counts = [summary.get("bytes") for summary in summaries]
        final_bytes = final.get("bytes")
        if (
            any(type(count) is not int or count < 0 for count in byte_counts)
            or type(final_bytes) is not int
            or final_bytes != sum(byte_counts)
            or float(final.get("start", -1)) != 0.0
            or rows[0][0] != 0.0
            or any(summary.get("sender") is not True or summary.get("omitted", False)
                   for summary in summaries)
        ):
            raise ValueError("cannot establish zero bytes in omitted terminal interval")
        rows.append((rows[-1][1], final_end, 0.0))
    return rows


def validate(
    iperf_path: str,
    client_time_path: str,
    client_markers_path: str,
    cross_start_ns: int,
    cross_stop_ns: int,
    lead_s: float,
) -> dict[str, object]:
    result = load_json(iperf_path)
    error = result.get("error")
    if error not in (None, EXPECTED_INTERRUPT):
        raise ValueError(f"unexpected iperf3 terminal error: {error!r}")
    if not isinstance(result.get("end"), dict):
        raise ValueError("iperf3 JSON has no finalized end object")
    if cross_start_ns <= 0 or cross_stop_ns <= cross_start_ns:
        raise ValueError("invalid cross-traffic boundary timestamps")

    intervals = interval_rows(result)
    iperf_origin_ns = cross_stop_ns - round(intervals[-1][1] * 1e9)
    iperf_startup_s = (iperf_origin_ns - cross_start_ns) / 1e9
    if not 0 <= iperf_startup_s <= MAX_IPERF_STARTUP_S:
        raise ValueError(f"implausible iperf3 startup interval: {iperf_startup_s:.6f}s")

    window_start, window_end, marker_diagnostics = client_window_s(
        client_time_path, client_markers_path, iperf_origin_ns
    )
    lead_elapsed = window_start + iperf_startup_s
    if lead_elapsed + COVERAGE_TOLERANCE_S < lead_s:
        raise ValueError(
            f"QUIC window starts after {lead_elapsed:.6f}s, before {lead_s:.6f}s lead completed"
        )
    if int(marker_diagnostics["last_end_marker_receipt_ns"]) > cross_stop_ns:
        raise ValueError("cross traffic stopped before all client end markers arrived")

    duration = window_end - window_start
    overlaps = [
        max(0.0, min(end, window_end) - max(start, window_start))
        for start, end, _ in intervals
    ]
    covered = sum(overlaps)
    if abs(covered - duration) > COVERAGE_TOLERANCE_S:
        raise ValueError(
            f"iperf3 intervals cover {covered:.6f}s of {duration:.6f}s QUIC window"
        )

    weighted_bits = sum(
        bits_per_second * overlap
        for (_, _, bits_per_second), overlap in zip(intervals, overlaps)
    )
    return {
        "interval_count": len(intervals),
        "iperf_span_s": intervals[-1][1] - intervals[0][0],
        "iperf_origin_after_process_start_s": iperf_startup_s,
        "client_window_after_process_start_s": lead_elapsed,
        "quic_window_start_s": window_start,
        "quic_window_end_s": window_end,
        "quic_window_duration_s": duration,
        "covered_s": covered,
        "tcp_goodput_mbit_s": weighted_bits / duration / 1e6,
        "terminal_error": error,
        **marker_diagnostics,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iperf", required=True)
    parser.add_argument("--client-time", required=True)
    parser.add_argument("--client-markers", required=True)
    parser.add_argument("--cross-start-ns", required=True, type=int)
    parser.add_argument("--cross-stop-ns", required=True, type=int)
    parser.add_argument("--lead-s", required=True, type=float)
    parser.add_argument("--out")
    args = parser.parse_args()

    summary = validate(
        args.iperf,
        args.client_time,
        args.client_markers,
        args.cross_start_ns,
        args.cross_stop_ns,
        args.lead_s,
    )
    encoded = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    if args.out:
        Path(args.out).write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
