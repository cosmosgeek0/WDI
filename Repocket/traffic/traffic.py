#!/usr/bin/env python3
"""Count completed INET socket I/O in one traced application process tree."""
import argparse
import ctypes
import fcntl
import ipaddress
import json
import math
import os
import queue
from pathlib import Path
import re
import selectors
import signal
import subprocess
import threading
import time
import uuid

READS = {"read", "readv", "recvfrom", "recvmsg", "recvmmsg"}
WRITES = {"write", "writev", "sendto", "sendmsg", "sendmmsg", "sendfile"}
CALLS = READS | WRITES | {"splice", "io_uring_setup"}
INET = re.compile(r"^\d+<(?:TCP|UDP)(?:v6)?:")
LINE = re.compile(r"^(?:(?:\[pid\s+(\d+)\]|(\d+))\s+)?(\d+\.\d+)\s+(.*)$")
DONE = re.compile(r"^(\w+)\((.*)\)\s+=\s+(-?\d+)(?:\s|$)")


def arguments(text):
    """Split syscall arguments without splitting socket addresses or structures."""
    result, start, stack, quoted, escaped = [], 0, [], False, False
    for n, char in enumerate(text):
        if quoted:
            if char == '"' and not escaped:
                quoted = False
            escaped = char == "\\" and not escaped
        elif char == '"':
            quoted = True
        elif char in "([{<":
            stack.append(char)
        elif char in ")]}>" and not (char == ">" and n and text[n - 1] == "-"):
            if stack and stack[-1] == {")": "(", "]": "[", "}": "{", ">": "<"}[char]:
                stack.pop()
        elif char == "," and not stack:
            result.append(text[start:n].strip())
            start = n + 1
    result.append(text[start:].strip())
    return result


def socket_kind(fd):
    if INET.match(fd):
        return True
    if re.match(r"^\d+<socket:", fd) or re.fullmatch(r"\d+", fd):
        raise ValueError("socket descriptor attribution unavailable")
    return False


def count_line(line):
    match = LINE.match(line)
    if not match:
        if "<TCP" in line or "<UDP" in line:
            raise ValueError("unrecognized socket trace record")
        return 0, 0
    call = match[4]
    # status=successful makes strace emit complete calls after their returns.
    if "unfinished ...>" in call or " resumed>" in call:
        raise ValueError("incomplete syscall record")
    match = DONE.match(call)
    if not match:
        if any(call.startswith(name + "(") for name in CALLS):
            raise ValueError("unrecognized syscall result")
        return 0, 0
    name, raw, returned = match[1], match[2], int(match[3])
    if returned < 0 or name not in CALLS:
        return 0, 0
    if name == "io_uring_setup":
        raise ValueError("io_uring byte accounting is unsupported")
    if returned == 0:
        return 0, 0
    args = arguments(raw)
    if name == "splice":
        return returned * socket_kind(args[0]), returned * socket_kind(args[2])
    if not socket_kind(args[0]):
        return 0, 0
    if name in {"recvfrom", "recvmsg", "recvmmsg"} and "MSG_PEEK" in raw:
        return 0, 0
    if name in {"sendmmsg", "recvmmsg"}:
        lengths = re.findall(r"\bmsg_len=(\d+)", raw)
        if len(lengths) < returned:
            raise ValueError("incomplete batched-datagram lengths")
        returned = sum(map(int, lengths[:returned]))
    return (returned, 0) if name in READS else (0, returned)


def clocks():
    return time.time_ns() // 1000000, time.clock_gettime_ns(time.CLOCK_BOOTTIME) // 1000000


def scope():
    result = {key: os.environ[env] for key, env in (
        ("group", "SALAD_CONTAINER_GROUP_ID"), ("instance", "SALAD_INSTANCE_ID"),
        ("machine", "SALAD_MACHINE_ID"))}
    for value in result.values():
        uuid.UUID(value)
    ticks = Path("/proc/1/stat").read_text().rsplit(") ", 1)[1].split()[19]
    result["runtime"] = Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":" + ticks
    return result


def network(path, expected, wall, boot):
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or not isinstance(value.get("scope"), dict):
            raise ValueError("IP gate unavailable")
        if any(value.get("scope", {}).get(k) != v for k, v in expected.items()):
            raise ValueError("IP gate runtime mismatch")
        if not all(type(value.get(k)) in (int, float) and math.isfinite(value[k])
                   for k in ("valid_until", "ip_checked_ms")):
            raise ValueError("IP gate timing unavailable")
        if value.get("allowed") is not True or boot / 1000 >= value["valid_until"]:
            raise ValueError("IP gate not admitted or expired")
        if not -10000 <= wall - value["ip_checked_ms"] <= 90000:
            raise ValueError("IP observation is stale")
        try:
            address = str(ipaddress.ip_address(value["ip"]))
        except ValueError:
            raise ValueError("IP gate address invalid") from None
        return address, None
    except (OSError, ValueError, KeyError, TypeError) as error:
        return None, str(error) if isinstance(error, ValueError) else "IP gate unavailable"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--application", choices=("repocket", "grass"), default="repocket")
    parser.add_argument("--network-state", type=Path, default=Path("/config/run/ip-policy.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--strace", default="strace")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("an application command is required after --")
    os.umask(0o077)
    identity = scope()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    lock = args.output.with_name(args.output.name + ".lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    started, start_boot = clocks()
    counter_id = str(uuid.uuid4())
    seq, rx, tx = 0, 0, 0
    last_wall, last_boot, last_rx, last_tx = started, start_boot, 0, 0
    interval_ip, gate_error = network(args.network_state, identity, started, start_boot)
    interval_error = gate_error
    accounting_error = None
    phase, device = "starting", None
    stopped = False
    stop_deadline = None
    killed = False
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "cannot own traced child reaping")
    parent_pid = os.getpid()

    def tracer_parent_death():
        # Kill the tracer if this runner is killed; EXITKILL covers its tracees.
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent_pid:
            os._exit(125)

    trace_read, trace_write = os.pipe()
    tracer = subprocess.Popen(
        [args.strace, "-f", "-qq", "--kill-on-exit", "--seccomp-bpf", "-ttt", "-s", "0", "-yy", "-v",
         "-e", "status=successful", "-e", "trace=" + ",".join(sorted(CALLS)),
         "-o", "/proc/self/fd/" + str(trace_write), "--", *command],
        pass_fds=(trace_write,), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=True, preexec_fn=tracer_parent_death)
    os.close(trace_write)
    # Container-log backpressure must never stop draining the syscall pipe.
    logs = queue.Queue(maxsize=256)
    log_dropped = 0
    def write_logs():
        for line in iter(logs.get, None):
            try:
                data=(line+'\n').encode()
                while data:data=data[os.write(1,data):]
            except OSError:pass
    writer = threading.Thread(target=write_logs,daemon=True)
    writer.start()
    def log(line):
        nonlocal log_dropped
        if len(line.encode())>65536:
            log_dropped+=1
            return
        try:logs.put_nowait(line)
        except queue.Full:log_dropped+=1

    def stop(signum, frame):
        nonlocal stopped, stop_deadline
        stopped = True
        if stop_deadline is None:
            stop_deadline = time.monotonic() + 3
        try:
            os.killpg(tracer.pid, signum)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    poller = selectors.DefaultSelector()
    poller.register(trace_read, selectors.EVENT_READ, "trace")
    poller.register(tracer.stdout, selectors.EVENT_READ, "app")
    os.set_blocking(trace_read, False)
    os.set_blocking(tracer.stdout.fileno(), False)
    buffers = {"trace": b"", "app": b""}
    next_minute = (started // 60000 + 1) * 60000
    next_publish, next_tick = start_boot, start_boot

    def emit(kind, wall, **extra):
        nonlocal seq
        if args.application != "repocket":
            return
        row = dict(format=2, revision=12, service="repocket", application="repocket",
                   traffic_scope="application", measurement="socket_io_payload_bytes",
                   group_id=identity["group"], instance=identity["instance"],
                   machine=identity["machine"], run=identity["instance"] + ":" + str(started),
                   started_ms=started, time_ms=wall, seq=seq, kind=kind,
                   counter_id=counter_id, phase=phase, device_id=device, log_dropped=log_dropped, **extra)
        seq += 1
        log("RP_MON " + json.dumps(row, separators=(",", ":")))

    emit("start", started, ip=interval_ip, reason=gate_error or "socket-I/O tracer started")
    while True:
        if stop_deadline is not None and time.monotonic() >= stop_deadline and not killed:
            try:
                os.killpg(tracer.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            killed = True
        for key, _ in poller.select(timeout=0.2):
            chunks = []
            # Bound each drain pass so a continuous stream cannot defer shutdown.
            for _ in range(16):
                try:
                    data = os.read(key.fd, 65536)
                except BlockingIOError:
                    break
                if not data:
                    poller.unregister(key.fileobj)
                    break
                chunks.append(data)
            stream = key.data
            buffers[stream] += b"".join(chunks)
            while b"\n" in buffers[stream]:
                line, buffers[stream] = buffers[stream].split(b"\n", 1)
                line = line.decode("utf-8", errors="replace")
                if stream == "trace":
                    try:
                        add_rx, add_tx = count_line(line)
                        rx += add_rx
                        tx += add_tx
                    except ValueError as error:
                        accounting_error = str(error)
                else:
                    # The command must already apply its app's credential-log filtering.
                    log(line)
                    native = re.search(r"peer (\w+);.*device_id=([a-zA-Z0-9]*);", line)
                    if native and args.application == "repocket":
                        phase = "connected" if native[1] == "connected" else "connecting"
                        device = native[2] or device
                    if line.startswith("strace:") and any(s in line for s in ("Operation not permitted", "Permission denied", "invalid", "Can't", "Cannot")):
                        accounting_error = "strace could not observe the application"
            if len(buffers[stream])>2_000_000:
                buffers[stream]=b''
                if stream=='trace':accounting_error='oversized syscall record'
                else:log_dropped+=1
        wall, boot = clocks()
        ip, gate_error = network(args.network_state, identity, wall, boot)
        interval_error = interval_error or gate_error or ("IP boundary" if ip != interval_ip else None)
        if abs((wall - last_wall) - (boot - last_boot)) > 5000:
            interval_error = "clock boundary"
        final = tracer.poll() is not None and not poller.get_map()
        if final and buffers["trace"].strip():
            accounting_error = "truncated syscall record"
        error = accounting_error or gate_error or ("producer stopping" if stopped else None)
        if tracer.poll() not in (None, 0):
            error = error or "application or tracer failed"
        if final:
            phase = "stopped"
        if boot >= next_publish or final:
            value = dict(traffic_scope="application", application=args.application,
                         measurement="socket_io_payload_bytes", counter_id=counter_id,
                         scope=identity, source_ms=wall, monotonic_ms=boot,
                         rx=rx if not error else None, tx=tx if not error else None,
                         valid=not error, error=error, final=final, log_dropped=log_dropped,
                         running=tracer.poll() is None,
                         ip=ip, phase=phase, device_id=device)
            temporary = args.output.with_name(args.output.name + "." + str(os.getpid()) + ".tmp")
            temporary.write_text(json.dumps(value, separators=(",", ":")))
            temporary.replace(args.output)
            next_publish = boot + 1000
        if wall >= next_minute or final:
            failure = accounting_error or interval_error
            if final and (tracer.returncode != 0 or stopped):
                failure = failure or "application or tracer interrupted"
            if wall > last_wall:
                emit("minute", wall, start_ms=last_wall, end_ms=wall,
                     elapsed_ms=wall - last_wall, rx_bytes=rx - last_rx,
                     tx_bytes=tx - last_tx, ip=interval_ip, valid=failure is None,
                     reason=failure, partial=(last_wall == started and started % 60000 != 0) or final, final=final)
            last_wall, last_boot, last_rx, last_tx = wall, boot, rx, tx
            interval_ip, interval_error = ip, gate_error
            next_minute = (wall // 60000 + 1) * 60000
        elif boot >= next_tick:
            emit("tick", wall, ip=ip, reason=error)
            next_tick = boot + 20000
        if final:
            break
    os.close(trace_read)
    tracer.stdout.close()
    result = tracer.wait(timeout=2)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            child, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            try:logs.put_nowait(None)
            except queue.Full:pass
            writer.join(timeout=2)
            return result if result >= 0 else 128 - result
        if child == 0:
            time.sleep(0.02)
    raise RuntimeError("traced child reaping did not finish")


if __name__ == "__main__":
    raise SystemExit(main())
