#!/usr/bin/env python3
"""Convert a capture into the artifact's `*.pcap.json` row format.

The artifact's `01_preprocessing.py` drives pyshark, which spawns tshark and
parses its XML output; that costs minutes per 80k-packet capture and needs a
Wireshark install.  This reader parses classic pcap and pcapng directly and
emits exactly the rows `run_metrics.py` consumes:

    [relative_ns, raw_ns, frame_len, packet_number]

`frame_len` is the *original* on-wire length from the record header, so a
truncated capture (`tcpdump -s 96`) still supports the wire-feasibility bound.
`packet_number` is left at -1, matching the artifact, because the QUIC packet
number is not readable from an encrypted header.

Selecting the sender's data packets works like the artifact parser: filter on
the server's UDP source port.

Usage
-----
    python pcap_reader.py sender.pcap --sport 4433 \
        --out build/data/quiche-cubic-n1/quiche-cubic-n1-rep1.pcap.json
    python pcap_reader.py --selftest
"""

from __future__ import annotations

import argparse
import io
import json
import os
import struct
import sys

LINKTYPE_ETHERNET = 1

ETH_TYPE_IPV4 = 0x0800
ETH_TYPE_IPV6 = 0x86DD
ETH_TYPE_VLAN = 0x8100
ETH_TYPE_QINQ = 0x88A8

IP_PROTO_UDP = 17

PCAP_MAGIC_US = 0xA1B2C3D4
PCAP_MAGIC_NS = 0xA1B23C4D
PCAPNG_SHB = 0x0A0D0D0A
PCAPNG_IDB = 0x00000001
PCAPNG_EPB = 0x00000006
PCAPNG_BYTE_ORDER_MAGIC = 0x1A2B3C4D


class CaptureError(Exception):
    pass


def parse_udp_ports(frame: bytes, linktype: int) -> tuple[int, int] | None:
    """Return (src_port, dst_port) for an Ethernet-framed UDP datagram."""
    if linktype != LINKTYPE_ETHERNET:
        raise CaptureError(f"unsupported linktype {linktype}; expected Ethernet")
    if len(frame) < 14:
        return None
    ether_type = struct.unpack_from("!H", frame, 12)[0]
    offset = 14
    while ether_type in (ETH_TYPE_VLAN, ETH_TYPE_QINQ):
        if len(frame) < offset + 4:
            return None
        ether_type = struct.unpack_from("!H", frame, offset + 2)[0]
        offset += 4

    if ether_type == ETH_TYPE_IPV4:
        if len(frame) < offset + 20:
            return None
        version_ihl = frame[offset]
        if version_ihl >> 4 != 4:
            return None
        header_len = (version_ihl & 0x0F) * 4
        if frame[offset + 9] != IP_PROTO_UDP:
            return None
        # A fragmented datagram carries no UDP header after the first fragment.
        if struct.unpack_from("!H", frame, offset + 6)[0] & 0x1FFF:
            return None
        udp = offset + header_len
    elif ether_type == ETH_TYPE_IPV6:
        if len(frame) < offset + 40:
            return None
        if frame[offset + 6] != IP_PROTO_UDP:
            return None  # extension headers are not expected on this testbed
        udp = offset + 40
    else:
        return None

    if len(frame) < udp + 4:
        return None
    return struct.unpack_from("!HH", frame, udp)


def iter_pcap(stream: io.BufferedReader):
    """Yield (timestamp_ns, orig_len, frame_bytes, linktype) from classic pcap."""
    header = stream.read(24)
    if len(header) < 24:
        raise CaptureError("truncated pcap file header")
    magic = struct.unpack_from("<I", header, 0)[0]
    if magic in (PCAP_MAGIC_US, PCAP_MAGIC_NS):
        endian = "<"
    else:
        magic = struct.unpack_from(">I", header, 0)[0]
        if magic not in (PCAP_MAGIC_US, PCAP_MAGIC_NS):
            raise CaptureError(f"not a classic pcap file (magic {magic:#x})")
        endian = ">"
    sub_ns = 1 if magic == PCAP_MAGIC_NS else 1000
    linktype = struct.unpack_from(endian + "I", header, 20)[0]

    record = struct.Struct(endian + "IIII")
    while True:
        raw = stream.read(record.size)
        if not raw:
            return
        if len(raw) < record.size:
            raise CaptureError("truncated pcap record header")
        ts_sec, ts_sub, incl_len, orig_len = record.unpack(raw)
        data = stream.read(incl_len)
        if len(data) < incl_len:
            raise CaptureError("truncated pcap record body")
        yield ts_sec * 1_000_000_000 + ts_sub * sub_ns, orig_len, data, linktype


def iter_pcapng(stream: io.BufferedReader):
    """Yield (timestamp_ns, orig_len, frame_bytes, linktype) from pcapng."""
    endian = "<"
    interfaces: list[tuple[int, int]] = []  # (linktype, ts_units_per_second)

    while True:
        head = stream.read(8)
        if not head:
            return
        if len(head) < 8:
            raise CaptureError("truncated pcapng block header")
        block_type = struct.unpack_from(endian + "I", head, 0)[0]
        if block_type == PCAPNG_SHB:
            body_head = stream.read(4)
            if struct.unpack_from("<I", body_head, 0)[0] != PCAPNG_BYTE_ORDER_MAGIC:
                endian = ">"
            block_len = struct.unpack_from(endian + "I", head, 4)[0]
            # 4 body bytes consumed; skip the remaining body and trailing length
            stream.read(block_len - 12)
            continue

        block_len = struct.unpack_from(endian + "I", head, 4)[0]
        if block_len < 12:
            raise CaptureError(f"invalid pcapng block length {block_len}")
        body = stream.read(block_len - 12)
        stream.read(4)  # trailing block length

        if block_type == PCAPNG_IDB:
            linktype = struct.unpack_from(endian + "H", body, 0)[0]
            units = 1_000_000  # if_tsresol default: microseconds
            offset = 8
            while offset + 4 <= len(body):
                code, length = struct.unpack_from(endian + "HH", body, offset)
                value = body[offset + 4: offset + 4 + length]
                if code == 0:
                    break
                if code == 9 and length >= 1:  # if_tsresol
                    raw = value[0]
                    units = (1 << (raw & 0x7F)) if raw & 0x80 else 10 ** raw
                offset += 4 + ((length + 3) & ~3)
            interfaces.append((linktype, units))

        elif block_type == PCAPNG_EPB:
            iface_id, ts_high, ts_low, cap_len, orig_len = struct.unpack_from(
                endian + "IIIII", body, 0
            )
            if iface_id >= len(interfaces):
                raise CaptureError(f"EPB references unknown interface {iface_id}")
            linktype, units = interfaces[iface_id]
            ticks = (ts_high << 32) | ts_low
            yield (
                ticks * 1_000_000_000 // units,
                orig_len,
                bytes(body[20: 20 + cap_len]),
                linktype,
            )


def iter_capture(path: str):
    with open(path, "rb") as stream:
        magic = stream.read(4)
        stream.seek(0)
        if len(magic) < 4:
            raise CaptureError("file too short")
        first = struct.unpack("<I", magic)[0]
        if first == PCAPNG_SHB:
            yield from iter_pcapng(stream)
        else:
            yield from iter_pcap(stream)


def build_rows(path: str, sport: int | None = None, dport: int | None = None) -> list[list[int]]:
    """Rows in the artifact's pcap.json format for the selected direction."""
    rows: list[list[int]] = []
    base_ns: int | None = None
    for ts_ns, orig_len, frame, linktype in iter_capture(path):
        if sport is not None or dport is not None:
            ports = parse_udp_ports(frame, linktype)
            if ports is None:
                continue
            src, dst = ports
            if sport is not None and src != sport:
                continue
            if dport is not None and dst != dport:
                continue
        if base_ns is None:
            base_ns = ts_ns
        rows.append([ts_ns - base_ns, ts_ns, orig_len, -1])
    return rows


# --------------------------------------------------------------------------- #
# self-test: synthesizes captures in memory, so it needs no tshark/pyshark
# --------------------------------------------------------------------------- #

def _frame(sport: int, dport: int, payload_len: int) -> bytes:
    udp = struct.pack("!HHHH", sport, dport, 8 + payload_len, 0) + b"\x00" * payload_len
    ipv4 = struct.pack(
        "!BBHHHBBH4s4s",
        0x45, 0, 20 + len(udp), 1, 0, 64, IP_PROTO_UDP, 0,
        bytes([10, 0, 0, 2]), bytes([10, 0, 0, 1]),
    )
    return b"\x02" * 6 + b"\x03" * 6 + struct.pack("!H", ETH_TYPE_IPV4) + ipv4 + udp


def _write_pcap(path: str, packets: list[tuple[int, bytes]], nano: bool) -> None:
    magic = PCAP_MAGIC_NS if nano else PCAP_MAGIC_US
    with open(path, "wb") as handle:
        handle.write(struct.pack("<IHHiIII", magic, 2, 4, 0, 0, 65535, LINKTYPE_ETHERNET))
        for ts_ns, frame in packets:
            sub = ts_ns % 1_000_000_000
            handle.write(
                struct.pack(
                    "<IIII",
                    ts_ns // 1_000_000_000,
                    sub if nano else sub // 1000,
                    len(frame),
                    len(frame),
                )
            )
            handle.write(frame)


def _write_pcapng(path: str, packets: list[tuple[int, bytes]]) -> None:
    with open(path, "wb") as handle:
        shb_body = struct.pack("<IHHq", PCAPNG_BYTE_ORDER_MAGIC, 1, 0, -1)
        handle.write(struct.pack("<II", PCAPNG_SHB, 12 + len(shb_body)))
        handle.write(shb_body)
        handle.write(struct.pack("<I", 12 + len(shb_body)))

        # if_tsresol = 9 -> nanoseconds
        idb_body = struct.pack("<HHI", LINKTYPE_ETHERNET, 0, 65535) + struct.pack(
            "<HHBBBB", 9, 1, 9, 0, 0, 0
        ) + struct.pack("<HH", 0, 0)
        handle.write(struct.pack("<II", PCAPNG_IDB, 12 + len(idb_body)))
        handle.write(idb_body)
        handle.write(struct.pack("<I", 12 + len(idb_body)))

        for ts_ns, frame in packets:
            padded = frame + b"\x00" * (-len(frame) % 4)
            body = struct.pack(
                "<IIIII", 0, ts_ns >> 32, ts_ns & 0xFFFFFFFF, len(frame), len(frame)
            ) + padded
            handle.write(struct.pack("<II", PCAPNG_EPB, 12 + len(body)))
            handle.write(body)
            handle.write(struct.pack("<I", 12 + len(body)))


def selftest() -> int:
    import tempfile

    base = 1_700_000_000_000_000_000
    server = [(base + i * 11_328, _frame(4433, 55000, 1350)) for i in range(5)]
    client = [(base + 5_000 + i * 11_328, _frame(55000, 4433, 20)) for i in range(3)]
    packets = sorted(server + client)
    expected_len = len(server[0][1])
    failures = 0

    with tempfile.TemporaryDirectory() as tmp:
        cases = {
            "pcap-nano": (os.path.join(tmp, "n.pcap"), lambda p: _write_pcap(p, packets, True)),
            "pcap-micro": (os.path.join(tmp, "u.pcap"), lambda p: _write_pcap(p, packets, False)),
            "pcapng": (os.path.join(tmp, "c.pcapng"), lambda p: _write_pcapng(p, packets)),
        }
        for name, (path, writer) in cases.items():
            writer(path)

            everything = build_rows(path)
            if len(everything) != len(packets):
                print(f"FAIL {name}: read {len(everything)} of {len(packets)} packets")
                failures += 1

            rows = build_rows(path, sport=4433)
            if len(rows) != len(server):
                print(f"FAIL {name}: sport filter kept {len(rows)}, expected {len(server)}")
                failures += 1
                continue
            if rows[0][0] != 0:
                print(f"FAIL {name}: first relative timestamp {rows[0][0]} != 0")
                failures += 1
            if any(row[2] != expected_len for row in rows):
                print(f"FAIL {name}: frame lengths {[r[2] for r in rows]}")
                failures += 1
            gaps = [rows[i + 1][0] - rows[i][0] for i in range(len(rows) - 1)]
            tolerance = 0 if name != "pcap-micro" else 1000
            if any(abs(gap - 11_328) > tolerance for gap in gaps):
                print(f"FAIL {name}: gaps {gaps} not ~11328 ns")
                failures += 1
            if not failures:
                print(f"ok   {name}: {len(rows)} server packets, gaps {gaps} ns")

    print("selftest failures:", failures)
    return 1 if failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", nargs="?", help="pcap or pcapng file")
    parser.add_argument("--sport", type=int, help="keep only this UDP source port")
    parser.add_argument("--dport", type=int, help="keep only this UDP destination port")
    parser.add_argument("--out", help="output *.pcap.json path")
    parser.add_argument(
        "--max-frame",
        type=int,
        help="fail if any frame exceeds this on-wire length; 1514 detects a "
             "pre-segmentation offload aggregate in a GSO-disabled run",
    )
    parser.add_argument(
        "--min-packets",
        type=int,
        help="fail if fewer than this many packets matched the filter",
    )
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()

    if args.selftest:
        sys.exit(selftest())
    if not args.capture:
        parser.error("capture path required (or --selftest)")

    rows = build_rows(args.capture, args.sport, args.dport)
    if not rows:
        raise SystemExit("no packets matched the filter")
    duration_s = rows[-1][0] / 1e9
    print(
        f"{len(rows)} packets, {duration_s:.3f} s, "
        f"median frame {sorted(r[2] for r in rows)[len(rows) // 2]} B"
    )

    problems = []
    if args.max_frame is not None:
        oversized = [row[2] for row in rows if row[2] > args.max_frame]
        if oversized:
            problems.append(
                f"{len(oversized)} frames exceed {args.max_frame} B "
                f"(largest {max(oversized)} B): capture is not wire-equivalent"
            )
    if args.min_packets is not None and len(rows) < args.min_packets:
        problems.append(f"only {len(rows)} packets, expected at least {args.min_packets}")
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w") as handle:
            json.dump(rows, handle)
        print(f"-> {args.out}")

    # The *.pcap.json is written even for a failed run: a failure must still be
    # analysable. The exit status is what the campaign driver gates on.
    if problems:
        for problem in problems:
            print(f"FAIL: {problem}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
