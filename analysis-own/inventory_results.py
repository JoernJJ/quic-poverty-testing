#!/usr/bin/env python3
"""Inventory campaign manifests and expose excluded pilots without pooling them."""

from __future__ import annotations

import argparse
import csv
import json
import os
from collections import defaultdict
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    root = Path(args.data)
    campaigns: dict[str, list[dict[str, object]]] = defaultdict(list)
    for path in sorted(root.glob("**/manifest.json")):
        with path.open(encoding="utf-8") as handle:
            manifest = json.load(handle)
        campaign = str(path.parent.parent.relative_to(root))
        campaigns[campaign].append(manifest)
    if not campaigns:
        raise SystemExit(f"no manifest.json files under {root}")

    rows: list[dict[str, object]] = []
    for campaign, manifests in sorted(campaigns.items()):
        statuses = [str(manifest.get("status", "missing")) for manifest in manifests]
        rows.append(
            {
                "campaign": campaign,
                "runs_total": len(manifests),
                "runs_ok": statuses.count("ok"),
                "runs_failed": statuses.count("failed"),
                "arms": ";".join(sorted({str(m.get("arm", "")) for m in manifests})),
                "scenarios": ";".join(
                    sorted({str(m.get("scenario", "legacy-n1")) for m in manifests})
                ),
                "cells": len({str(m.get("cell", "")) for m in manifests}),
                "rates_mbit": ";".join(sorted({str(m.get("rate_mbit", "")) for m in manifests})),
                "rtts_ms": ";".join(sorted({str(m.get("rtt_ms", "")) for m in manifests})),
                "gso_values": ";".join(sorted({str(m.get("gso", "")) for m in manifests})),
                "kernels": ";".join(
                    sorted({str(m.get("sender_kernel", m.get("kernel", ""))) for m in manifests})
                ),
                "capture_types": ";".join(
                    sorted({str(m.get("capture_tstype", "")) for m in manifests})
                ),
            }
        )

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(f"{sum(row['runs_total'] for row in rows)} manifests, {len(rows)} campaigns -> {args.out}")


if __name__ == "__main__":
    main()
