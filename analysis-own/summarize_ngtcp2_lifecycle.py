#!/usr/bin/env python3
"""Reduce only the complete, frozen 40-run ngtcp2 lifecycle control (stdlib)."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import random
import re
import statistics


VERSIONS = ("artifact", "current")
COMPLETIONS = ("idle", "streams")
CONDITIONS = [(version, completion) for version in VERSIONS for completion in COMPLETIONS]
RATE_FIELDS = (
    "observed_full_file_goodput_mbit_s",
    "full_file_goodput_upper_bound_mbit_s",
    "client_lifetime_goodput_mbit_s",
)
REQUIRED_FILES = {
    "result.json", "client.json", "client.log", "client-invoke.log", "server.log",
    "sender.pcap", "capture-start.log", "capture-stop.log",
    "sender-qdisc-before.json", "client-qdisc-before.json", "client-qdisc-after.json",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def record(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(value, dict), f"{path}: expected JSON object")
    return value


def sha256(value: object, label: str) -> None:
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
            f"{label}: invalid SHA256")


def integer(value: object, label: str) -> int:
    require(type(value) is int, f"{label}: expected integer")
    return value


def check_cached(data: dict, field: str, expected: float, label: str) -> None:
    value = data[field]
    require(type(value) in (int, float) and math.isfinite(value)
            and math.isclose(value, expected, rel_tol=1e-12, abs_tol=1e-9),
            f"{label}: cached {field} disagrees with bytes/monotonic timestamps")


def verify_manifest(directory: Path) -> dict[str, str]:
    manifest = directory / "SHA256SUMS"
    require(manifest.is_file() and not manifest.is_symlink(), f"{manifest}: missing or symlink")
    hashes = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        require(match is not None, f"{manifest}: malformed checksum entry")
        checksum, name = match.groups()
        require(name not in (".", "..", "SHA256SUMS") and Path(name).name == name
                and name not in hashes, f"{manifest}: unsafe or duplicate filename {name!r}")
        path = directory / name
        require(path.is_file() and not path.is_symlink(), f"{path}: missing file or symlink")
        require(digest(path) == checksum, f"{path}: SHA256 mismatch")
        hashes[name] = checksum
    files = {path.name for path in directory.iterdir() if path.is_file() and path.name != "SHA256SUMS"}
    require(set(hashes) == files, f"{manifest}: manifest does not cover every recorded file")
    require(REQUIRED_FILES <= set(hashes),
            f"{manifest}: missing required records {sorted(REQUIRED_FILES - set(hashes))}")
    require(not any(name.endswith(".qlog") for name in hashes), f"{directory}: control contains qlog")
    return hashes


def load_plan(root: Path) -> tuple[dict, str, list[tuple[int, str, str]]]:
    path = root / "plan.json"
    plan = record(path)
    require(plan["stage"] == "control", f"{path}: only stage=control is accepted; never pool pilot")
    require(plan["study"] == "ngtcp2 CUBIC/default lifecycle control", f"{path}: wrong study")
    require(integer(plan["reps_per_cell"], "reps_per_cell") == 10, f"{path}: expected ten blocks")
    require(integer(plan["payload_bytes"], "payload_bytes") == 100 * 1024 * 1024,
            f"{path}: expected 100 MiB payload")
    require(plan["completion_bracket_limit_ms"] == 50
            and plan["configured_idle_timeout_s_both_peers"] == 30
            and plan["qlog_enabled"] is False, f"{path}: incompatible timing/qlog protocol")
    for field in ("sender_kernel", "client_kernel"):
        require(isinstance(plan[field], str) and bool(plan[field].strip()),
                f"{path}: missing frozen {field}")
    require(plan["settings"]["APP_GSO"] == "0" and plan["settings"]["CAP_TSTYPE"] == "host",
            f"{path}: expected GSO off and host capture timestamps")
    sha256(plan["payload_sha256"], "payload_sha256")
    sha256(plan["driver_sha256"], "driver_sha256")
    payload = root / plan["retained_payload"]
    require(payload.resolve().is_relative_to(root) and payload.is_file()
            and not payload.is_symlink(), f"{path}: missing or unsafe retained payload")
    require(payload.stat().st_size == plan["payload_bytes"]
            and digest(payload) == plan["payload_sha256"], f"{path}: retained payload differs from plan")
    for field in ("client_binary_sha256", "server_binary_sha256"):
        require(set(plan[field]) == set(VERSIONS), f"{path}: incomplete {field}")
        for version in VERSIONS:
            sha256(plan[field][version], f"{field}/{version}")
    require(digest(root / "driver.py") == plan["driver_sha256"], f"{path}: frozen driver SHA256 mismatch")
    seed = integer(plan["seed"], "seed")
    expected_order = []
    for block in range(1, 11):
        conditions = CONDITIONS.copy()
        random.Random(f"{seed}-{block}").shuffle(conditions)
        expected_order.extend((block, version, completion) for version, completion in conditions)
    require(plan["order"] == [list(item) for item in expected_order],
            f"{path}: order does not match complete seeded ten-block design")
    expected_names = {f"block{block:02d}-{version}-{completion}" for block, version, completion in expected_order}
    directories = {p.name for p in root.iterdir() if p.is_dir() and p.name.startswith("block")}
    recorded = {p.parent.name for p in root.glob("*/result.json")}
    require(directories == expected_names and recorded == expected_names,
            f"{root}: missing or extra run directories/results; expected exactly 40 control runs")
    return plan, digest(path), expected_order


def load_run(root: Path, plan: dict, plan_sha: str, block: int, version: str,
             completion: str, run_order: int) -> dict:
    run_id = f"block{block:02d}-{version}-{completion}"
    directory = root / run_id
    require(not directory.is_symlink(), f"{directory}: run directory is a symlink")
    hashes = verify_manifest(directory)
    result = record(directory / "result.json")
    client = record(directory / "client.json")
    require((result["block"], result["version"], result["completion"]) == (block, version, completion),
            f"{run_id}: result identity differs from plan")
    require(result["status"] == client["status"] == "ok"
            and not result.get("error") and not client.get("error")
            and integer(client["returncode"], run_id + " returncode") == 0,
            f"{run_id}: unsuccessful run")
    require(result["client"] == client, f"{run_id}: embedded and standalone client records differ")
    require(client["kernel"] == plan["client_kernel"]
            and client["binary_sha256"] == plan["client_binary_sha256"][version]
            and client["driver_sha256"] == plan["driver_sha256"]
            and client["payload_sha256"] == plan["payload_sha256"]
            and integer(client["downloaded_bytes"], run_id + " bytes") == plan["payload_bytes"],
            f"{run_id}: payload, client executable/driver or kernel differs from plan")
    require(client["completion"] == completion and client["configured_idle_timeout_s"] == 30
            and client["observer_period_s"] == 0.01, f"{run_id}: client protocol differs from plan")
    for label, argv in (("client", client["argv"]), ("server", result["server_argv"])):
        require(isinstance(argv, list) and all(isinstance(arg, str) for arg in argv)
                and "--cc=cubic" in argv and "--timeout=30s" in argv
                and not any(arg.startswith("--qlog") for arg in argv),
                f"{run_id}: {label} invocation violates control protocol")
    require(("--exit-on-all-streams-close" in client["argv"]) == (completion == "streams"),
            f"{run_id}: completion flag differs from condition")
    require(("--max-gso-dgrams=1" if version == "artifact" else "--no-gso") in result["server_argv"],
            f"{run_id}: server GSO setting differs from protocol")
    require(integer(result["capture_kernel_drops"], run_id + " capture drops") == 0,
            f"{run_id}: capture kernel drops are not zero")
    stop_log = (directory / "capture-stop.log").read_text(encoding="utf-8")
    drops = re.findall(r"^(\d+) packets? dropped by kernel$", stop_log, re.M)
    counts = re.findall(r"^(\d+) packets? captured$", stop_log, re.M)
    require(bool(drops) and all(int(value) == 0 for value in drops)
            and bool(counts) and int(counts[-1]) > 0, f"{run_id}: capture log does not prove zero drops/nonzero capture")
    start, end, lower, upper = [integer(client[field], run_id + " " + field) for field in
                                ("start_monotonic_ns", "end_monotonic_ns", "full_file_lower_ns", "full_file_upper_ns")]
    require(end > start and start < lower < upper and upper - lower <= 50_000_000,
            f"{run_id}: invalid positive timing bounds or bracket exceeds 50 ms")
    # The observer may finish just after process exit; do not force upper <= end.
    metrics = {
        "runtime_s": (end - start) / 1e9,
        "full_file_lower_s": (lower - start) / 1e9,
        "full_file_upper_s": (upper - start) / 1e9,
        "completion_bracket_ms": (upper - lower) / 1e6,
    }
    bits_mbit = plan["payload_bytes"] * 8 / 1e6
    metrics.update(
        observed_full_file_goodput_mbit_s=bits_mbit / metrics["full_file_upper_s"],
        full_file_goodput_upper_bound_mbit_s=bits_mbit / metrics["full_file_lower_s"],
        client_lifetime_goodput_mbit_s=bits_mbit / metrics["runtime_s"],
    )
    for field, value in metrics.items():
        check_cached(client, field, value, run_id)
    return {
        "block": block, "version": version, "completion": completion, "run_order": run_order,
        "payload_bytes": plan["payload_bytes"], **metrics,
        "capture_kernel_drops": 0,
        "sender_kernel": plan["sender_kernel"], "client_kernel": client["kernel"],
        "payload_sha256": plan["payload_sha256"],
        "client_binary_sha256": client["binary_sha256"],
        "server_binary_sha256": plan["server_binary_sha256"][version],
        "driver_sha256": plan["driver_sha256"], "project_commit": plan["project_commit"],
        "seed": plan["seed"],
        "source_result_path": str(directory / "result.json"), "source_result_sha256": hashes["result.json"],
        "source_client_path": str(directory / "client.json"), "source_client_sha256": hashes["client.json"],
        "plan_path": str(root / "plan.json"), "plan_sha256": plan_sha,
    }


def summarize(rows: list[dict], version: str, completion: str) -> dict:
    group = [row for row in rows if row["version"] == version and row["completion"] == completion]
    require(len(group) == 10, f"{version}/{completion}: expected ten replicates")
    summary = {"version": version, "completion": completion, "n": len(group)}
    for field in RATE_FIELDS:
        values = [row[field] for row in group]
        summary[field + "_mean"] = statistics.mean(values)
        summary[field + "_population_sd"] = statistics.pstdev(values)
    for field in ("runtime_s", "full_file_lower_s", "full_file_upper_s", "completion_bracket_ms"):
        summary[field + "_mean"] = statistics.mean(row[field] for row in group)
    summary["completion_bracket_ms_max"] = max(row["completion_bracket_ms"] for row in group)
    for field in ("sender_kernel", "client_kernel", "plan_path", "plan_sha256"):
        summary[field] = group[0][field]
    return summary


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="complete raw control directory containing frozen plan.json")
    parser.add_argument("--out-dir", required=True, type=Path, help="derived CSV directory outside raw control")
    args = parser.parse_args()
    root, out = args.root.resolve(), args.out_dir.resolve()
    try:
        require(out != root and root not in out.parents, "--out-dir must be outside the raw control directory")
        plan, plan_sha, order = load_plan(root)
        rows = [load_run(root, plan, plan_sha, block, version, completion, index)
                for index, (block, version, completion) in enumerate(order, 1)]
        rows.sort(key=lambda row: (row["block"], row["version"], row["completion"]))
        summaries = [summarize(rows, version, completion) for version, completion in CONDITIONS]
        out.mkdir(parents=True, exist_ok=True)
        write_csv(out / "ngtcp2-lifecycle-control-runs.csv", rows)
        write_csv(out / "ngtcp2-lifecycle-control-summary.csv", summaries)
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(1, f"lifecycle reduction failed: {exc}\n")
    print(f"40 control runs, four cells -> {out}")
    print("Descriptive grouped-mean comparisons only; no inferential CI or causal interpretation.")
    print("Full-file primary goodput is a conservative lower bound; its upper bound is retained in both CSVs.")
    grouped = {(row["version"], row["completion"]): row for row in summaries}
    comparisons = [(f"same-policy {completion}: current / artifact", ("artifact", completion), ("current", completion))
                   for completion in COMPLETIONS]
    comparisons += [(f"same-version {version}: streams / idle", (version, "idle"), (version, "streams"))
                    for version in VERSIONS]
    for label, before, after in comparisons:
        print(label)
        for field in RATE_FIELDS:
            old, new = grouped[before][field + "_mean"], grouped[after][field + "_mean"]
            print(f"  {field}: difference={new - old:.6f} Mbit/s; ratio={new / old:.6f}")
    print("Provenance: client identity/checksum and all recorded file hashes verified; sender kernel and server "
          "binary identity are plan attestations (the driver checks server SHA before each run).")


if __name__ == "__main__":
    main()
