#!/usr/bin/env python3
"""One picoquic CUBIC/GSO0 paired software-capture diagnostic, or offline reduction.

Requires preconfigured N1 on the Pi and the sender's kernel-default qdisc.
Neither qdisc nor interface addresses/switch configuration are changed. Only
mirror GRO is changed, then restored. Run as the normal sender user, not root.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import pwd
import re
import shlex
import signal
import struct
import subprocess
import sys
import time
import uuid

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "analysis-own"))
from pcap_reader import iter_capture, parse_udp_ports
from run_metrics import BURST_THRESHOLD_NS, reduce_run
from validate_qdisc import close, one, options

SCOPE = "One paired picoquic CUBIC diagnostic, not a repeated experiment and not wire-accuracy validation."
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def clean_json(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: clean_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean_json(item) for item in value]
    return value


def packets(path):
    """Use the historical signature: original length and captured UDP payload.

    IPv4/UDP checksums and Ethernet headers are intentionally not signatures.
    The fixed 96-byte snaplen is part of the paired acquisition contract.
    """
    result = []
    for timestamp, length, frame, linktype in iter_capture(str(path)):
        ports = parse_udp_ports(frame, linktype)
        if ports is None or ports[0] != 4433:
            continue
        offset = 14
        ether_type = struct.unpack_from("!H", frame, 12)[0]
        while ether_type in (0x8100, 0x88A8):
            ether_type = struct.unpack_from("!H", frame, offset + 2)[0]
            offset += 4
        if ether_type != 0x0800 or len(frame) < offset + 20:
            raise ValueError("diagnostic requires captured IPv4 UDP headers")
        udp = offset + (frame[offset] & 15) * 4
        if len(frame) <= udp + 8:
            raise ValueError("missing captured QUIC payload prefix")
        payload_length = struct.unpack_from("!H", frame, udp + 4)[0] - 8
        prefix = frame[udp + 8:udp + 8 + payload_length]
        result.append((timestamp, length, (length, prefix)))
    if len(result) < 2:
        raise ValueError(f"{path}: fewer than two server UDP packets")
    return result


def reduce_pair(source, destination):
    metadata = json.loads((source / "metadata.json").read_text())
    captures = {name: packets(source / f"{name}.pcap") for name in ("sender", "mirror")}
    signatures = {name: [row[2] for row in rows] for name, rows in captures.items()}
    counts = {name: Counter(values) for name, values in signatures.items()}
    metrics = {}
    gaps = {}
    for name, rows in captures.items():
        gaps[name] = [b[0] - a[0] for a, b in zip(rows, rows[1:])]
        metric = asdict(reduce_run("picoquic:cubic:default", name, gaps[name],
                                   [row[1] for row in rows[:-1]], 1e9, 40e6))
        metric.update(frames_above_1514=sum(row[1] > 1514 for row in rows),
                      negative_gaps=sum(gap < 0 for gap in gaps[name]),
                      zero_gaps=sum(gap == 0 for gap in gaps[name]))
        metrics[name] = metric
    # Only compare gaps with the same unique endpoints adjacent in BOTH files.
    # Missing, reordered, or duplicate signatures must not shift positional pairs.
    mirror_index = {key: index for index, key in enumerate(signatures["mirror"])
                    if counts["mirror"][key] == counts["sender"][key] == 1}
    matched = disagreements = 0
    for index, (left, right) in enumerate(zip(signatures["sender"], signatures["sender"][1:])):
        if left not in mirror_index or right not in mirror_index:
            continue
        other = mirror_index[left]
        if mirror_index[right] != other + 1:
            continue
        matched += 1
        disagreements += ((gaps["sender"][index] < BURST_THRESHOLD_NS)
                          != (gaps["mirror"][other] < BURST_THRESHOLD_NS))
    client = dict(line.split("=", 1) for line in
                  (source / "client-result.env").read_text().splitlines() if "=" in line)
    if int(client["client_rc"]) != 0 or int(client["downloaded_bytes"]) != 104857600:
        raise ValueError("client did not complete the 100 MiB transfer")
    timing = json.loads((source / "time.json").read_text())
    duration = (int(timing["end"]) - int(timing["start"])) / 1e9
    if duration <= 0:
        raise ValueError("client duration must be positive")
    report = {
        "packet_correspondence": {
            "sender_packets": len(captures["sender"]),
            "mirror_packets": len(captures["mirror"]),
            "sender_not_seen_on_mirror": sum((counts["sender"] - counts["mirror"]).values()),
            "mirror_not_seen_on_sender": sum((counts["mirror"] - counts["sender"]).values()),
            "identical_packet_order": signatures["sender"] == signatures["mirror"],
            "duplicate_signature_counts": {name: sum(n - 1 for n in values.values())
                                           for name, values in counts.items()},
            "matching_method": "Original frame length plus captured QUIC payload prefix; excludes checksums potentially changed by TX offload.",
            "matched_adjacent_unique_gaps": matched,
            "burst_classification_disagreement_count": disagreements,
            "burst_classification_disagreement_fraction": disagreements / matched if matched else None,
            "burst_threshold_ns": BURST_THRESHOLD_NS,
        },
        "goodput_mbit_s": 104857600 * 8 / duration / 1e6,
        "capture_kernel_drops": metadata["capture_kernel_drops"],
        "metrics": metrics,
        "scope": SCOPE,
        "source": str(source.resolve()),
        "source_sha256": {name: sha256(source / name) for name in
                          ("sender.pcap", "mirror.pcap", "metadata.json", "client-result.env", "time.json")},
    }
    save(destination / "comparison.json", clean_json(report))
    if any(metadata["capture_kernel_drops"][name] != 0 for name in captures):
        raise ValueError("capture kernel drops are nonzero or unknown")
    if any(metric["frames_above_1514"] or metric["negative_gaps"] for metric in metrics.values()):
        raise ValueError("capture has oversized frames or nonmonotonic chronology; report retained")
    if not matched:
        raise ValueError("no unambiguous matched adjacent packet gaps; report retained")
    return report


def command(argv, *, env=None, timeout=30, check=True):
    result = subprocess.run(argv, env=env, text=True, capture_output=True, timeout=timeout)
    record = {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
    if check and result.returncode:
        raise RuntimeError(f"{shlex.join(map(str, argv))}: {result.stderr.strip() or result.stdout.strip()}")
    return record


def ssh(script, **kwargs):
    return command(["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                    "pi-mgmt", "export PATH=" + SAFE_PATH + "; " + script], **kwargs)


def qdisc_config(rows):
    return [{key: row[key] for key in ("kind", "handle", "root", "parent", "options") if key in row}
            for row in rows]


def require_default(rows):
    roots = [row for row in rows if row.get("root")]
    if len(roots) != 1 or roots[0]["kind"] not in ("mq", "fq_codel"):
        raise ValueError("sender must already have its kernel-default mq/fq_codel qdisc")
    if any(row.get("handle") != "0:" or row["kind"] not in ("mq", "fq_codel") for row in rows):
        raise ValueError("sender has a non-default qdisc; refusing to replace user state")


def require_n1(ifb, client):
    tbf = options(one(ifb, kind="tbf"))
    forward = options(one(ifb, kind="netem", parent="1:1"))
    reverse = options(one(client, kind="netem"))
    close(float(tbf["rate"]), 5000000, 1, "TBF byte rate")
    close(float(tbf["burst"]), 1540, 16, "TBF burst")
    if forward["limit"] != 143 or reverse["limit"] != 1000:
        raise ValueError("N1 requires 143-packet bottleneck and 1000-packet return leaf")
    close(float(forward["delay"]["delay"]), .020, 1e-9, "forward delay")
    close(float(reverse["delay"]["delay"]), .020, 1e-9, "return delay")


def snapshot():
    local = {
        "kernel": ["uname", "-r"],
        "uname": ["uname", "-a"],
        "sender_qdisc": ["tc", "-s", "-j", "qdisc", "show", "dev", "enp7s0"],
        "softnet": ["cat", "/proc/net/softnet_stat"],
        "socket_buffers": ["sysctl", "net.core.rmem_max", "net.core.rmem_default", "net.core.wmem_max", "net.core.wmem_default"],
    }
    for role, iface in (("sender", "enp7s0"), ("mirror", "enp14s0")):
        for name, flag in (("offloads", "-k"), ("stats", "-S"), ("timestamps", "-T"), ("driver", "-i")):
            local[f"{role}_{name}"] = ["ethtool", flag, iface]
        local[f"{role}_link_settings"] = ["ethtool", iface]
        local[f"{role}_link"] = ["ip", "-j", "-d", "link", "show", "dev", iface]
        local[f"{role}_addresses"] = ["ip", "-j", "addr", "show", "dev", iface]
    result = {key: command(argv, check=False) for key, argv in local.items()}
    for key, script in {
        "client_kernel": "uname -a",
        "client_qdisc": "tc -s -j qdisc show dev ifb0",
        "client_return_qdisc": "tc -s -j qdisc show dev eth0",
        "client_ingress_filters": "tc -s -j filter show dev eth0 parent ffff:",
        "client_softnet": "cat /proc/net/softnet_stat",
        "client_offloads": "ethtool -k eth0",
        "client_socket_buffers": "sysctl net.core.rmem_max net.core.rmem_default net.core.wmem_max net.core.wmem_default",
    }.items():
        result[key] = ssh(script, check=False)
    return result


def gro_state(record):
    match = re.search(r"^generic-receive-offload: (on|off)\b", record["stdout"], re.M)
    if record["returncode"] or not match:
        raise ValueError("cannot determine mirror GRO state")
    return match.group(1)


# The remote supervisor has its own bounded lifetime even if management SSH fails.
# Its child gets a private process group; no system-wide process matching is used.
REMOTE_RUN = r'''
import json, os, pathlib, signal, subprocess, sys
root, client, impl = map(pathlib.Path, sys.argv[1:])
def interrupted(signum, frame):
    raise KeyboardInterrupt
for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
    signal.signal(sig, interrupted)
proc = subprocess.Popen([str(client), 'picoquic', 'cubic', str(impl), str(root)], start_new_session=True)
state = {'pid': proc.pid, 'start': pathlib.Path('/proc/%d/stat' % proc.pid).read_text().split()[21]}
(root / 'owned-client.json').write_text(json.dumps(state))
try:
    sys.exit(proc.wait(timeout=180))
finally:
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
'''
REMOTE_STOP = r'''
import json, os, pathlib, signal, sys, time
statefile = pathlib.Path(sys.argv[1]) / 'owned-client.json'
if statefile.exists():
    state = json.loads(statefile.read_text())
    stat = pathlib.Path('/proc/%d/stat' % state['pid'])
    if stat.exists() and stat.read_text().split()[21] == state['start']:
        os.killpg(state['pid'], signal.SIGTERM)
        time.sleep(1)
        if stat.exists() and stat.read_text().split()[21] == state['start']:
            os.killpg(state['pid'], signal.SIGKILL)
'''


class Capture:
    def __init__(self, name, iface, root):
        self.name = name
        self.pidfile = root / f"{name}-capture.pid"
        self.logpath = root / f"{name}-capture.log"
        self.log = self.logpath.open("w")
        self.proc = None
        self.pid = None
        self.stopped = False
        tcpdump = ["tcpdump", "-i", iface, "-j", "host", "--time-stamp-precision=nano",
                   "-s", "96", "-B", "32768", "-n", "-Z", pwd.getpwuid(os.getuid()).pw_name,
                   "-w", str(root / f"{name}.pcap"), "udp port 4433"]
        helper = "import os,pathlib,sys; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); os.execvp(sys.argv[2],sys.argv[2:])"
        try:
            self.proc = subprocess.Popen(["sudo", "-n", "python3", "-c", helper, str(self.pidfile), *tcpdump],
                                         stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
        except BaseException:
            self.log.close()
            raise

    def ready(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"{self.name} capture exited before readiness")
            if self.pidfile.exists():
                self.pid = int(self.pidfile.read_text())
            if self.pid and "listening on" in self.logpath.read_text():
                return
            time.sleep(.05)
        raise RuntimeError(f"{self.name} capture readiness timed out")

    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        was_running = self.proc.poll() is None
        try:
            if was_running:
                if self.pid is None and self.pidfile.exists():
                    self.pid = int(self.pidfile.read_text())
                if self.pid:
                    command(["sudo", "-n", "kill", "-INT", str(self.pid)])
                else:
                    self.proc.terminate()
                try:
                    self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    if self.pid:
                        command(["sudo", "-n", "kill", "-KILL", str(self.pid)], check=False)
                    self.proc.kill()
                    self.proc.wait()
                    raise RuntimeError(f"{self.name} capture failed to flush")
            if not was_running or self.proc.returncode != 0:
                raise RuntimeError(f"{self.name} capture failed (exit {self.proc.returncode})")
        finally:
            self.log.close()

    def drops(self):
        match = re.search(r"^(\d+) packets dropped by kernel$", self.logpath.read_text(), re.M)
        if not match:
            raise ValueError(f"{self.name}: missing capture drop statistics")
        return int(match.group(1))


def load_environment():
    result = subprocess.run(["bash", "-c", 'source "$1"; env -0', "bash", str(PROJECT / "experiment/env.sh")],
                            check=True, capture_output=True)
    environment = dict(item.decode().split("=", 1) for item in result.stdout.split(b"\0") if item)
    required = {"SENDER_IFACE": "enp7s0", "SENDER_IP": "10.0.0.2", "CLIENT_IFACE": "eth0",
                "CLIENT_IP": "10.0.0.1", "CLIENT_SSH": "pi-mgmt", "PORT": "4433", "APP_GSO": "0",
                "RATE_MBIT": "40", "RTT_MS": "40", "BOTTLENECK_LIMIT_PKTS": "143",
                "TESTFILE": "file_100MiB.bin", "TESTFILE_BYTES": "104857600", "TESTCASE": "goodput"}
    for key, expected in required.items():
        if environment[key] != expected:
            raise ValueError(f"{key} must be {expected} for this fixed diagnostic")
    environment["PATH"] = str(Path.home() / ".local/bin") + ":" + str(Path.home() / "bin") + ":" + SAFE_PATH
    return environment


def run_pair(root, metadata):
    env = load_environment()
    impl = Path(env["IMPL_DIR_CURRENT"]) / "picoquic"
    artifact = Path(env["ARTIFACT"])
    remote_project = "/home/joern/PE/paper"
    remote_impl = remote_project + "/quic-implementations-current"
    remote = remote_project + "/results-own/tmp/paired-" + uuid.uuid4().hex
    metadata["remote_run_dir"] = remote
    metadata["wrapper_environment"] = {key: env[key] for key in
        ("SENDER_IP", "CLIENT_IP", "PORT", "TESTFILE", "TESTFILE_BYTES", "TESTCASE", "APP_GSO")}
    metadata["wrapper_environment"].update(POS_CC="cubic", POS_GSO="0", CLIENT_IMPL_DIR=remote_impl)
    captures = []
    server = None
    server_log = None
    gro_before = None
    before_qdisc = None
    remote_created = False
    errors = []
    try:
        command(["sudo", "-n", "true"])
        if (artifact / "local/www" / env["TESTFILE"]).stat().st_size != 104857600:
            raise ValueError("server object is not 100 MiB")
        before = metadata["before"] = snapshot()
        save(root / "metadata.json", metadata)
        before_qdisc = json.loads(before["sender_qdisc"]["stdout"])
        require_default(before_qdisc)
        require_n1(json.loads(before["client_qdisc"]["stdout"]),
                   json.loads(before["client_return_qdisc"]["stdout"]))
        filters = before["client_ingress_filters"]
        if filters["returncode"] or '"ifb0"' not in filters["stdout"] or '"mirred"' not in filters["stdout"]:
            raise ValueError("client ingress redirect to ifb0 is not present")
        occupied = command(["ss", "-H", "-lun", "sport = :4433"])["stdout"]
        if occupied.strip():
            raise ValueError("UDP/4433 already in use; refusing to stop an unrelated process")
        for iface in ("enp7s0", "enp14s0"):
            role = "sender" if iface == "enp7s0" else "mirror"
            if not re.search(r"Speed:\s*1000Mb/s\b", before[f"{role}_link_settings"]["stdout"]):
                raise ValueError(f"{iface} must run at 1 Gbit/s for the fixed wire bound")
            offered = command(["sudo", "-n", "tcpdump", "--list-time-stamp-types", "-i", iface])
            if not re.search(r"^\s*host\s", offered["stdout"] + offered["stderr"], re.M):
                raise ValueError(f"{iface} does not offer host timestamps")
        provenance = root / "provenance"
        provenance.mkdir()
        for path in (Path(__file__), PROJECT / "experiment/env.sh", PROJECT / "experiment/client-run.sh",
                     PROJECT / "analysis-own/pcap_reader.py", PROJECT / "analysis-own/run_metrics.py",
                     impl / "run-server.sh", impl / "run-client.sh", impl / "VERSION"):
            (provenance / path.name).write_bytes(path.read_bytes())
        metadata["server_binary"] = str(impl / "picoquicdemo")
        metadata["server_binary_sha256"] = sha256(impl / "picoquicdemo")
        metadata["version"] = (impl / "VERSION").read_text()
        metadata["runtime"] = {"python": sys.version, "tcpdump": command(["tcpdump", "--version"]),
                               "server_libraries": command(["ldd", str(impl / "picoquicdemo")])}
        for filename, relative in (("pi-client-run.sh", "experiment/client-run.sh"),
                                   ("client-env.sh", "experiment/env.sh"),
                                   ("client-wrapper.sh", "quic-implementations-current/picoquic/run-client.sh"),
                                   ("client-VERSION", "quic-implementations-current/picoquic/VERSION")):
            (provenance / filename).write_text(ssh("cat " + shlex.quote(remote_project + "/" + relative))["stdout"])
        metadata["client_binary_identity"] = ssh("sha256sum " + shlex.quote(remote_impl + "/picoquic/picoquicdemo"))
        metadata["client_runtime"] = ssh("ldd " + shlex.quote(remote_impl + "/picoquic/picoquicdemo"))
        ssh("mkdir " + shlex.quote(remote))
        remote_created = True
        gro_before = gro_state(before["mirror_offloads"])
        metadata["mirror_gro_before"] = gro_before
        save(root / "metadata.json", metadata)
        command(["sudo", "-n", "ethtool", "-K", "enp14s0", "gro", "off"])
        if gro_state(command(["ethtool", "-k", "enp14s0"])) != "off":
            raise ValueError("mirror GRO did not disable")
        server_env = env | {"POS_CC": "cubic", "POS_GSO": "0", "IP": env["SENDER_IP"],
                            "SERVERNAME": env["SENDER_IP"], "CERTS": str(artifact / "local/certs") + "/",
                            "WWW": str(artifact / "local/www") + "/"}
        server_log = (root / "server.log").open("w")
        server = subprocess.Popen(["./run-server.sh"], cwd=impl, env=server_env,
                                  stdout=server_log, stderr=subprocess.STDOUT, start_new_session=True)
        save(root / "server-process.json", {"pid": server.pid, "pgid": server.pid})
        time.sleep(3)
        if server.poll() is not None:
            raise ValueError("server exited before capture")
        for name, iface in (("sender", "enp7s0"), ("mirror", "enp14s0")):
            capture = Capture(name, iface, root)
            captures.append(capture)
            capture.ready()
        remote_env = {**metadata["wrapper_environment"], "PATH": remote_project + "/experiment:" + SAFE_PATH}
        invoke = "env " + " ".join(shlex.quote(f"{key}={value}") for key, value in remote_env.items())
        invoke += " " + shlex.join(["python3", "-c", REMOTE_RUN, remote,
                                    remote_project + "/experiment/client-run.sh", remote_impl])
        result = ssh(invoke, timeout=195, check=False)
        (root / "client-invoke.log").write_text(result["stdout"] + result["stderr"])
        metadata["client_exit"] = result["returncode"]
        if result["returncode"]:
            raise RuntimeError(f"client invocation failed with exit {result['returncode']}")
        if server.poll() is not None:
            raise ValueError("server exited during transfer")
        time.sleep(1)
    except BaseException as error:
        errors.append(f"{type(error).__name__}: {error}")
    finally:
        # Finish all restoration attempts even if one fails; never hide failures.
        for capture in captures:
            try:
                capture.stop()
            except BaseException as error:
                errors.append(str(error))
        if server is not None and server.poll() is None:
            try:
                os.killpg(server.pid, signal.SIGTERM)
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(server.pid, signal.SIGKILL)
                    server.wait()
            except BaseException as error:
                errors.append(f"server cleanup: {error}")
        if server_log is not None:
            server_log.close()
        if gro_before is not None:
            try:
                command(["sudo", "-n", "ethtool", "-K", "enp14s0", "gro", gro_before])
                restored = gro_state(command(["ethtool", "-k", "enp14s0"]))
                metadata["mirror_gro_restored"] = restored
                if restored != gro_before:
                    raise ValueError("mirror GRO restoration mismatch")
            except BaseException as error:
                errors.append(f"GRO restoration: {error}")
        if remote_created:
            try:
                ssh(shlex.join(["python3", "-c", REMOTE_STOP, remote]))
                for local, relative in (("client-result.env", "client-result.env"), ("time.json", "time.json"),
                                        ("client.log", "logs/client.log"), ("owned-client.json", "owned-client.json")):
                    result = ssh("cat " + shlex.quote(remote + "/" + relative), check=False)
                    if result["returncode"]:
                        errors.append(f"cannot retain remote {relative}: {result['stderr'].strip()}")
                    else:
                        (root / local).write_text(result["stdout"])
            except BaseException as error:
                errors.append(f"remote cleanup/evidence: {error}")
        try:
            metadata["after"] = snapshot()
            after_qdisc = json.loads(metadata["after"]["sender_qdisc"]["stdout"])
            metadata["sender_qdisc_unchanged"] = qdisc_config(before_qdisc or []) == qdisc_config(after_qdisc)
            if before_qdisc is not None and not metadata["sender_qdisc_unchanged"]:
                errors.append("sender qdisc changed externally during diagnostic; not overwriting unrelated state")
        except BaseException as error:
            errors.append(f"after snapshots: {error}")
        metadata["capture_kernel_drops"] = {}
        for capture in captures:
            try:
                metadata["capture_kernel_drops"][capture.name] = capture.drops()
            except BaseException as error:
                errors.append(str(error))
        metadata["errors"] = errors
        save(root / "metadata.json", metadata)
    if errors:
        raise RuntimeError("; ".join(errors))
    return reduce_pair(root, root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="new output directory; existing paths refused")
    parser.add_argument("--reduce-only", type=Path, metavar="RETAINED_PAIR", help="offline source directory; never modified")
    args = parser.parse_args()
    root = args.out.absolute()
    root.mkdir(parents=True, exist_ok=False)
    metadata = {"status": "running", "purpose": "Single paired capture diagnostic; excluded from all confirmatory data",
                "scope": SCOPE, "implementation": "picoquic", "cc": "cubic",
                "gso": 0, "payload_bytes": 104857600, "rate_mbit_s": 40, "rtt_ms": 40,
                "bottleneck_limit_pkts": 143, "sender_iface": "enp7s0", "mirror_iface": "enp14s0",
                "timestamp_type": "host on both captures; not hardware timestamps", "capture_wire_equivalent": 0,
                "management_traffic_shares_mirror_iface": True, "mirror_gro_during": "off",
                "sender_qdisc_policy": "Require kernel default; leave unchanged, check configuration afterward",
                "started_ns": time.time_ns()}
    if args.reduce_only:
        metadata = {"status": "running", "mode": "offline-reduction", "scope": SCOPE,
                    "source": str(args.reduce_only.absolute()), "started_ns": time.time_ns()}
    status_path = root / ("reduction-status.json" if args.reduce_only else "metadata.json")
    save(status_path, metadata)
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupted)
    exit_code = 0
    try:
        report = reduce_pair(args.reduce_only.absolute(), root) if args.reduce_only else run_pair(root, metadata)
        metadata["status"] = "ok"
        print(json.dumps({"output": str(root), "packet_correspondence": report["packet_correspondence"]}, indent=2))
    except BaseException as error:
        metadata["status"] = "failed"
        metadata["failure"] = f"{type(error).__name__}: {error}"
        print(metadata["failure"], file=sys.stderr)
        exit_code = 1
    finally:
        metadata["finished_ns"] = time.time_ns()
        save(status_path, metadata)
        comparison_path = root / "comparison.json"
        if not args.reduce_only and comparison_path.exists():
            comparison = json.loads(comparison_path.read_text())
            comparison["source_sha256"]["metadata.json"] = sha256(status_path)
            save(comparison_path, comparison)
        files = sorted(path for path in root.rglob("*") if path.is_file() and path.name != "SHA256SUMS")
        (root / "SHA256SUMS").write_text("".join(f"{sha256(path)}  {path.relative_to(root)}\n" for path in files))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
