# Per-app socket bytes

`traffic.py` follows one launched process tree with strace 6.8. Raw trace data
stays in a pipe with payload output disabled. Both apps use this one implementation,
each with its own process tree and output file; never wrap the combined init.

Copy `traffic.py`, `bin/strace` and `LICENSE.strace` into the image. The tracer is
static Linux AMD64; Python 3 is required. `build-tracer.sh` can download the
hash-checked official source and rebuild it. Artifact hashes and check results
are in `build.json`.

```sh
python3 /opt/app-traffic/traffic.py --application repocket \
  --output /config/run/repocket-traffic.json \
  -- /bin/busybox sh /usr/local/bin/repocket-start
```

The tracer defaults to `strace` on PATH; `--strace` accepts an explicit path.
Use `--application grass` and a separate output file for Grass. Each command
retains its own credential filtering; the runner relays its stdout/stderr.

Counts completed TCP/UDP socket read/write returns, including IPv6, vectored I/O,
batched datagrams, sendfile and splice. Excludes MSG_PEEK, files, pipes, Unix
sockets and unrelated processes. These are socket payload bytes including TLS
framing and local TCP/UDP traffic, not wire headers, retransmits or provider credits.
Unknown descriptors, unsupported io_uring or incomplete traces produce gaps.

The read-only IP source defaults to `/config/run/ip-policy.json` (`--network-state`
to override). Group/instance/machine/runtime must match; runtime is
`boot_id:PID1_start_ticks`. `allowed` must be true, `ip_checked_ms` fresh and
`valid_until` unexpired in CLOCK_BOOTTIME seconds. Otherwise bytes are null.

The atomic output file contains cumulative `rx`/`tx`, `counter_id`, `source_ms`,
`monotonic_ms` (boot-clock milliseconds), `scope`, `ip`, `valid/error`,
`running/final`, `phase/device_id`, `application`, `traffic_scope: application`
and `measurement: socket_io_payload_bytes`. Consumers reject invalid/stale data,
IP or counter changes; an unchanged final snapshot is not a new zero sample.

Repocket also emits format-2 `RP_MON` minute records with group/instance/machine/run,
sequence, start/end/elapsed milliseconds, interval `rx_bytes`/`tx_bytes`, IP,
attribution and `valid/reason/partial/final`. Grass reads the cumulative file.

The runner forwards TERM/INT, then forces shutdown after three seconds.
EXITKILL, parent-death SIGKILL and child reaping prevent tracees outliving it.
Admission, app recovery and Salad allocation remain owned by the caller.
