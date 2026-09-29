#!/usr/bin/env python3
"""Separate CUBIC/default lifecycle control; never resumes historical campaigns.

run: four version/completion conditions per seeded block, with sender capture.
client: Pi-local monotonic process timing and a bounded full-file-size observer.
A full-file observation is not a wire timestamp or exact final-byte arrival time.
"""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time


ROOT = Path(__file__).resolve().parent.parent


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def command(args, *, env=None, timeout=30):
    p = subprocess.run(args, env=env, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError(f"{shlex.join(map(str, args))}: {p.returncode}\n{p.stdout}{p.stderr}")
    return p.stdout


def stop_process(p):
    if p.poll() is None:
        os.killpg(p.pid, signal.SIGTERM)
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()


def client(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    downloads = out / "downloads"
    downloads.mkdir()
    target = downloads / args.filename
    argv = [args.binary, "-q", "--download", str(downloads), "--no-quic-dump",
            "--no-http-dump", "--cc=cubic", "--timeout=30s"]
    if args.completion == "streams":
        argv.append("--exit-on-all-streams-close")
    if args.qlog:
        argv.append(f"--qlog-file={out / 'client.qlog'}")
    argv += [args.sender, str(args.port), f"https://{args.sender}:{args.port}/{args.filename}"]
    result = {"argv": argv, "kernel": os.uname().release,
              "binary_sha256": digest(args.binary), "completion": args.completion,
              "driver_sha256": digest(__file__),
              "configured_idle_timeout_s": 30, "observer_period_s": 0.01,
              "monotonic_resolution_s": time.get_clock_info("monotonic").resolution}
    finished = threading.Event()
    observations = {}
    start_ns = time.monotonic_ns()

    def observe():
        last_incomplete = start_ns
        while True:
            before = time.monotonic_ns()
            try:
                size = target.stat().st_size
            except FileNotFoundError:
                size = 0
            after = time.monotonic_ns()
            if size >= args.bytes:
                observations.update(full_file_lower_ns=last_incomplete, full_file_upper_ns=after)
                return
            last_incomplete = before
            if finished.is_set():
                return
            finished.wait(0.01)

    observer = threading.Thread(target=observe)
    observer.start()
    error = ""
    rc = -1
    try:
        with (out / "client.log").open("w") as log:
            p = subprocess.Popen(argv, cwd=out, stdout=log, stderr=subprocess.STDOUT,
                                 start_new_session=True)
            timed_out = threading.Event()

            def expire():
                timed_out.set()
                try:
                    stop_process(p)
                except ProcessLookupError:
                    pass

            deadline = threading.Timer(180, expire)
            deadline.start()
            try:
                rc = p.wait()
            finally:
                deadline.cancel()
            if timed_out.is_set():
                error = "client exceeded 180-second deadline"
    except Exception as exc:
        error = str(exc)
    finally:
        end_ns = time.monotonic_ns()
        finished.set()
        observer.join()
    result.update(observations)
    result.update(start_monotonic_ns=start_ns, end_monotonic_ns=end_ns, returncode=rc,
                  runtime_s=(end_ns - start_ns) / 1e9,
                  downloaded_bytes=target.stat().st_size if target.exists() else 0)
    result["payload_sha256"] = digest(target) if target.exists() else ""
    if rc != 0 or result["downloaded_bytes"] != args.bytes or result["payload_sha256"] != args.sha256:
        error = error or "client exit, payload size or checksum failed"
    if not observations:
        error = error or "no full-file observation"
    else:
        lower_s = (observations["full_file_lower_ns"] - start_ns) / 1e9
        upper_s = (observations["full_file_upper_ns"] - start_ns) / 1e9
        bracket_ms = (upper_s - lower_s) * 1000
        result.update(full_file_lower_s=lower_s, full_file_upper_s=upper_s,
                      completion_bracket_ms=bracket_ms)
        if lower_s <= 0 or bracket_ms > 50:
            error = error or "invalid full-file bracket or prespecified 50 ms limit exceeded"
        else:
            result.update(client_lifetime_goodput_mbit_s=8 * args.bytes / result["runtime_s"] / 1e6,
                          observed_full_file_goodput_mbit_s=8 * args.bytes / upper_s / 1e6,
                          full_file_goodput_upper_bound_mbit_s=8 * args.bytes / lower_s / 1e6)
    result.update(status="failed" if error else "ok", error=error)
    save(out / "client.json", result)
    # Retain failed bodies. Successful bodies are identical to the preserved server payload.
    if not error:
        target.unlink()
    print(json.dumps(result), flush=True)
    return bool(error)


def run(args):
    raw_env = subprocess.check_output(["bash", "-c", 'source "$1"; env -0', "bash",
                                       str(ROOT / "experiment/env.sh")])
    env = dict(item.split("=", 1) for item in raw_env.decode().split("\0") if "=" in item)
    env.update(APP_GSO="0", CAP_TSTYPE="host", CAPTURE_POINT="sender-host",
               CAPTURE_WIRE_EQUIVALENT="0", CROSS_TRAFFIC="0")
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    ssh = ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", env["CLIENT_SSH"]]

    def remote(argv, timeout=30):
        return command(ssh + ["export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; "
                              + shlex.join(map(str, argv))], timeout=timeout)

    if command(["ss", "-H", "-lun", f"sport = :{env['PORT']}"]).strip():
        raise RuntimeError("experiment UDP port already occupied")
    subprocess.run(["sudo", "-n", "tc", "qdisc", "del", "dev", env["SENDER_IFACE"], "root"],
                   capture_output=True, check=False)
    preflight = subprocess.run(["bash", str(ROOT / "experiment/preflight.sh")], env=env,
                               capture_output=True, text=True, timeout=100)
    (out / "preflight.log").write_text(preflight.stdout + preflight.stderr)
    if preflight.returncode:
        raise RuntimeError("preflight failed; retained preflight.log; no transfers started")
    payload = Path(env["ARTIFACT"]) / "local/www" / env["TESTFILE"]
    expected_sha = digest(payload)
    if payload.stat().st_size != int(env["TESTFILE_BYTES"]):
        raise RuntimeError("server payload size does not match protocol")
    payload_source = payload
    (out / "www").mkdir()
    payload = out / "www" / env["TESTFILE"]
    shutil.copyfile(payload_source, payload)
    if digest(payload) != expected_sha:
        raise RuntimeError("retained payload differs from the source checksum")
    versions = {"artifact": Path(env["IMPL_DIR_ARTIFACT"]) / "ngtcp2",
                "current": Path(env["IMPL_DIR_CURRENT"]) / "ngtcp2"}
    client_versions = {"artifact": env["CLIENT_PROJECT"] + "/quic-implementations/ngtcp2/http_client",
                       "current": env["CLIENT_PROJECT"] + "/quic-implementations-current/ngtcp2/http_client"}
    conditions = [(v, c) for v in versions for c in ["idle", "streams"]]
    order = []
    for rep in range(1, args.reps + 1):
        cells = conditions.copy()
        random.Random(f"{args.seed}-{rep}").shuffle(cells)
        order.extend((rep, v, c) for v, c in cells)
    expected_clients = {v: remote(["sha256sum", binary]).split()[0] for v, binary in client_versions.items()}
    expected_servers = {v: digest(folder / "http_server") for v, folder in versions.items()}
    plan = {"study": "ngtcp2 CUBIC/default lifecycle control", "stage": args.stage,
            "reps_per_cell": args.reps, "seed": args.seed, "order": order,
            "primary_metric": "launch-to-first-observed-complete-file goodput, lower bound from 10ms sampling",
            "completion_bracket_limit_ms": 50, "configured_idle_timeout_s_both_peers": 30,
            "payload_bytes": int(env["TESTFILE_BYTES"]), "payload_sha256": expected_sha,
            "payload_source": str(payload_source), "retained_payload": str(payload.relative_to(out)),
            "sender_kernel": os.uname().release,
            "client_kernel": remote(["uname", "-r"]).strip(), "qlog_enabled": args.stage == "pilot",
            "settings": {k: env[k] for k in ["SENDER_IFACE", "SENDER_IP", "CLIENT_IP", "RATE_MBIT",
                         "RTT_MS", "BOTTLENECK_LIMIT_PKTS", "SOCKET_BUFFER_BYTES", "APP_GSO", "CAP_TSTYPE"]},
            "client_binary_sha256": expected_clients, "server_binary_sha256": expected_servers,
            "driver_sha256": digest(__file__), "project_commit": command(["git", "-C", str(ROOT), "rev-parse", "HEAD"]).strip()}
    save(out / "plan.json", plan)
    (out / "driver.py").write_bytes(Path(__file__).read_bytes())
    rows = []
    for rep, version, completion in order:
        run_id = f"block{rep:02d}-{version}-{completion}"
        run_dir = out / run_id
        run_dir.mkdir()
        remote_dir = env["CLIENT_PROJECT"] + "/results-own/calibration/" + out.name + "/" + run_id
        print(f"RUN {run_id}", flush=True)
        result = {"block": rep, "version": version, "completion": completion,
                  "status": "failed", "remote_directory": remote_dir}
        server = None
        capture_started = False
        try:
            (run_dir / "sender-qdisc-before.json").write_text(command(["tc", "-s", "-j", "qdisc", "show", "dev", env["SENDER_IFACE"]]))
            (run_dir / "client-qdisc-before.json").write_text(remote(["tc", "-s", "-j", "qdisc", "show", "dev", "ifb0"]))
            binary = versions[version] / "http_server"
            if digest(binary) != expected_servers[version]:
                raise RuntimeError("server executable changed after plan freeze")
            argv = [str(binary), "-q", "-d", str(payload.parent), env["SENDER_IP"], env["PORT"],
                    "--cc=cubic", "--timeout=30s", env["ARTIFACT"] + "/local/certs/priv.key",
                    env["ARTIFACT"] + "/local/certs/cert.pem",
                    "--max-gso-dgrams=1" if version == "artifact" else "--no-gso"]
            result["server_argv"] = argv
            with (run_dir / "server.log").open("w") as log:
                server = subprocess.Popen(argv, cwd=run_dir, stdout=log, stderr=subprocess.STDOUT,
                                          start_new_session=True)
            for _ in range(50):
                if server.poll() is not None:
                    raise RuntimeError("server exited before readiness")
                if command(["ss", "-H", "-lun", f"sport = :{env['PORT']}"]).strip():
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("server listener did not become ready")
            (run_dir / "capture-start.log").write_text(command(["bash", str(ROOT / "experiment/capture.sh"), "start", str(run_dir / "sender.pcap")], env=env))
            capture_started = True
            argv = ["python3", env["CLIENT_PROJECT"] + "/experiment/ngtcp2-lifecycle.py", "client",
                    "--binary", client_versions[version], "--out", remote_dir,
                    "--completion", completion, "--sender", env["SENDER_IP"], "--port", env["PORT"],
                    "--filename", env["TESTFILE"], "--bytes", env["TESTFILE_BYTES"], "--sha256", expected_sha]
            if args.stage == "pilot":
                argv.append("--qlog")
            (run_dir / "client-invoke.log").write_text(remote(argv, timeout=200))
            result["status"] = "ok"
        except Exception as exc:
            result["error"] = str(exc)
        finally:
            try:
                if capture_started:
                    (run_dir / "capture-stop.log").write_text(command(["bash", str(ROOT / "experiment/capture.sh"), "stop"], env=env, timeout=25))
            except Exception as exc:
                result.update(status="failed", error=f"capture finalization: {exc}")
            finally:
                if server is not None:
                    stop_process(server)
            for filename in ["client.json", "client.log"] + (["client.qlog"] if args.stage == "pilot" else []):
                copy = subprocess.run(["scp", "-q", env["CLIENT_SSH"] + ":" + remote_dir + "/" + filename, str(run_dir / filename)], capture_output=True, text=True, timeout=45)
                if copy.returncode:
                    result.update(status="failed", error=result.get("error", "") + " missing " + filename)
            (run_dir / "client-qdisc-after.json").write_text(remote(["tc", "-s", "-j", "qdisc", "show", "dev", "ifb0"]))
        if (run_dir / "client.json").exists():
            received = json.loads((run_dir / "client.json").read_text())
            result["client"] = received
            if (received["status"] != "ok" or received["kernel"] != plan["client_kernel"]
                    or received["binary_sha256"] != expected_clients[version]
                    or received["driver_sha256"] != plan["driver_sha256"]):
                result.update(status="failed", error="client validation or frozen identity failed")
        stop_log = (run_dir / "capture-stop.log").read_text() if (run_dir / "capture-stop.log").exists() else ""
        drops = re.findall(r"^(\d+) packets? dropped by kernel$", stop_log, re.M)
        counts = re.findall(r"^(\d+) packets? captured$", stop_log, re.M)
        if not drops or any(int(n) for n in drops) or not counts or int(counts[-1]) == 0:
            result.update(status="failed", error="capture count/drop validation failed")
        result["capture_kernel_drops"] = int(drops[-1]) if drops else None
        save(run_dir / "result.json", result)
        with (run_dir / "SHA256SUMS").open("w") as sums:
            for path in sorted(run_dir.iterdir()):
                if path.is_file() and path.name != "SHA256SUMS":
                    sums.write(f"{digest(path)}  {path.name}\n")
        if result["status"] != "ok":
            raise RuntimeError(f"failed run retained: {run_id}: {result.get('error')}")
        c = result["client"]
        rows.append({"block": rep, "version": version, "completion": completion,
                     **{k: c[k] for k in ["runtime_s", "full_file_lower_s", "full_file_upper_s", "completion_bracket_ms",
                           "client_lifetime_goodput_mbit_s", "observed_full_file_goodput_mbit_s", "full_file_goodput_upper_bound_mbit_s"]}})
        with (out / "runs.csv").open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        print(f"OK {run_id}: file={c['observed_full_file_goodput_mbit_s']:.3f} Mbit/s, lifetime={c['client_lifetime_goodput_mbit_s']:.3f} Mbit/s, bracket={c['completion_bracket_ms']:.3f} ms", flush=True)
    print(f"COMPLETE {len(rows)} runs: {out}", flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    p = sub.add_parser("client")
    for name in ["binary", "out", "sender", "filename", "sha256"]:
        p.add_argument("--" + name, required=True)
    p.add_argument("--completion", choices=["idle", "streams"], required=True)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--bytes", type=int, required=True)
    p.add_argument("--qlog", action="store_true")
    p = sub.add_parser("run")
    p.add_argument("--out", required=True)
    p.add_argument("--stage", choices=["pilot", "control"], required=True)
    p.add_argument("--reps", type=int, required=True)
    p.add_argument("--seed", type=int, default=20260927)
    args = parser.parse_args()
    if args.operation == "client":
        if Path(args.filename).name != args.filename or args.bytes <= 0:
            parser.error("filename must be a basename and payload size positive")
        return client(args)
    if args.reps <= 0:
        parser.error("reps must be positive")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
