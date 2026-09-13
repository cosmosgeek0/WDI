"""Read-only, bounded independent-fleet experiment reporting.

Network counters and provider account credits have separate units and clocks.
Never infer paid traffic from interfaces, or client acceptance from Salad state.
"""

from __future__ import annotations

import csv
from bisect import bisect_left, bisect_right
from collections import deque
from copy import deepcopy
from datetime import datetime
import io
import hashlib
import json
import math
import re
import threading
import time
import zlib
from pathlib import Path

from dashboard.native_network import instance_run


BUCKET_MS = 600_000
GAP_MS = 180_000
MAX_BYTES = 128 * 1024 * 1024
MAX_ROWS = 20_000
MAX_BUCKETS = 2016  # Two weeks; older observations remain on disk.
MAX_TIMELINE_INTERVALS = 2000
HG_ERROR_CODES = frozenset({"device_limit", "device_already_active", "network_overused", "network_unusable", "authorization_error", "disconnected"})
HG_EXCLUDED_IPS = frozenset({"96.246.178.36"})
STALE_RECORDER = "Recorder is stale or has no observations. Last values are historical, not live."
STALE_HG = "Honeygain worker observer is stale or missing. Running and connected counts are last recorded states."
STALE_CENSUS = "PacketStream restart census is stale, missing, or does not match this test target. The current run boundary is not freshly verified."


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def pick(source, strings=(), numbers=()):
    source = source if isinstance(source, dict) else {}
    result = {key: str(source[key])[:200] if source.get(key) is not None else None for key in strings}
    result.update({key: number(source.get(key)) for key in numbers})
    return result


def clean_hg_error(value):
    # Observer exception messages may contain private URLs or credentials.
    if isinstance(value, str) and value in HG_ERROR_CODES:
        return value
    return "observer_error" if value else None


def clean_ps_error(value):
    if isinstance(value, str) and "PacketStream allows packeting from 1 client per IP address." in value:
        return "duplicate_ip"
    return "client_error" if value else None


def timestamp_ms(value):
    if not isinstance(value, str) or len(value) > 100:
        return None
    try:
        # Salad emits seven fractional digits; older Python accepts six.
        value = re.sub(r"\.(\d+)(?=[+-])", lambda m: "." + m.group(1)[:6].ljust(6, "0"), value.replace("Z", "+00:00"))
        stamp = datetime.fromisoformat(value)
        return int(stamp.timestamp() * 1000) if stamp.tzinfo else None
    except (ValueError, OverflowError, OSError):
        return None


def clean_experiment(raw):
    result = pick(raw, ("id", "started_at", "status", "phase"), ("started_ms",))
    result["ps"] = pick(raw.get("ps"), ("organization", "group"), ("desired", "baseline_provider_bytes"))
    result["hg"] = pick(raw.get("hg"), ("organization", "phase"),
                        ("desired", "baseline_provider_gathering_credits", "baseline_provider_observed_ms", "baseline_started_ms"))
    result["archive_name"] = Path(str(raw.get("archive_path", ""))).name or None
    # Action descriptions are deliberately omitted: raw operational logs may
    # carry credential-bearing URLs. Only the intervention's time/type is public.
    result["interventions"] = [pick(row, ("service", "action", "recorded_at"), ("time_ms",))
                               for row in raw.get("interventions", [])[-100:] if isinstance(row, dict)]
    return result


def clean_observation(raw):
    result = pick(raw, ("recorded_at", "experiment_id", "phase"), ("time_ms",))
    ps = raw.get("ps") or {}
    hg = raw.get("hg") or {}
    result["ps"] = pick(ps, (), ("running", "reporting", "accepted", "monitor_connections", "provider_bytes", "provider_observed_ms",
                                 "network_rx_bytes", "network_tx_bytes", "network_total_bytes", "network_observed_ms", "status_observed_ms"))
    result["ps"]["network_complete"] = ps.get("network_complete") is True
    result["ps"]["workers"] = [{**pick(w, ("machine", "run", "ip", "state", "client_status", "sample_source"),
                                      ("network_bytes", "received_age_ms", "client_status_age_ms", "source_age_ms", "source_granularity_seconds", "last_interval_seconds")),
                                "native_status_only": w.get("native_status_only") is True or w.get("sample_source") in {"native_status_only", "sysfs_via_salad_logs_1s", "native_sysfs_1s"},
                                "error_code": clean_ps_error(w.get("error"))}
                                 for w in ps.get("workers", [])[:50] if isinstance(w, dict)]
    result["ps"]["native_status_only"] = ps.get("native_status_only") is True or any(w["native_status_only"] for w in result["ps"]["workers"])
    result["hg"] = pick(hg, (), ("running", "connected", "desired", "provider_gathering_credits", "provider_observed_ms",
                                 "network_rx_bytes", "network_tx_bytes", "network_total_bytes", "network_observed_ms", "status_observed_ms"))
    result["hg"]["network_complete"] = hg.get("network_complete") is True
    result["hg"]["workers"] = [{**pick(w, ("group", "device", "machine", "run", "ip", "state", "client_status"),
                                      ("credits", "credit_observed_ms", "network_bytes", "received_age_ms", "sample_ms")),
                                "error_code": clean_hg_error(w.get("error"))}
                                 for w in hg.get("workers", []) if isinstance(w, dict)]
    result["collection_error"] = bool(raw.get("errors") or raw.get("error"))
    return result


def distinct_points(points):
    unique = {}
    for at, value, complete in points:
        previous = unique.get(at)
        if previous:
            # Conflicting counters at one source time cannot support a rate.
            value = value if value == previous[1] else None
            complete = complete and previous[2]
        unique[at] = (at, value, complete)
    return sorted(unique.values())


def window_counter(rows, counter, timestamp, start_ms, end_ms):
    points = distinct_points((source[timestamp], number(source.get(counter)), source.get("network_complete") is True)
                             for row in rows for source in (row["ps"],)
                             if number(source.get(timestamp)) is not None and start_ms <= source[timestamp] <= end_ms)
    values = [p[1] for p in points]
    delta = values[-1] - values[0] if len(values) >= 2 and all(v is not None for v in values) and all(b >= a for a, b in zip(values, values[1:])) else None
    return points, delta


def measured_interval(previous=None, current=None, gap_ms=GAP_MS):
    result = {"start_ms": previous[0] if previous else None, "end_ms": current[0] if current else None,
              "delta": None, "per_second": None, "complete": False, "status": "missing"}
    if not previous or not current or previous[1] is None or current[1] is None:
        return result
    elapsed = current[0] - previous[0]
    if elapsed <= 0:
        return result
    if current[1] < previous[1]:
        result["status"] = "counter_reset"
        return result
    result["delta"] = current[1] - previous[1]
    if elapsed > gap_ms:
        result["status"] = "gap"
        return result
    result["complete"] = previous[2] and current[2]
    result["status"] = "complete" if result["complete"] else "incomplete"
    # A partial sum is useful activity evidence, but never full-fleet coverage.
    result["per_second"] = result["delta"] * 1000 / elapsed
    return result


def rate_timeline(rows, started_ms, end_ms, hg_started_ms=None, ps_started_ms=None):
    timeline = {service: {metric: [] for metric in ("network", "provider")} for service in ("ps", "hg")}
    truncated = False
    for service, metric, counter, timestamp in (
        ("ps", "network", "network_total_bytes", "network_observed_ms"),
        ("ps", "provider", "provider_bytes", "provider_observed_ms"),
        ("hg", "network", "network_total_bytes", "network_observed_ms"),
        ("hg", "provider", "provider_gathering_credits", "provider_observed_ms"),
    ):
        start = max(started_ms, hg_started_ms or started_ms) if service == "hg" and metric == "provider" else started_ms
        if service == "ps":
            start = max(start, ps_started_ms or start)
        points = []
        for row in rows:
            source = row.get(service) or {}
            at = number(source.get(timestamp))
            # Missing source timestamps never become recorder-time observations.
            if at is not None and start <= at <= end_ms:
                points.append((at, number(source.get(counter)), metric != "network" or source.get("network_complete") is True))
        points = distinct_points(points)
        truncated = truncated or len(points) - 1 > MAX_TIMELINE_INTERVALS
        points = points[-MAX_TIMELINE_INTERVALS - 1:]
        gap = 420_000 if service == "hg" and metric == "provider" else GAP_MS
        timeline[service][metric] = [measured_interval(a, b, gap) for a, b in zip(points, points[1:])]
    return timeline, truncated


def worker_identity(worker):
    identity = tuple(worker.get(field) for field in ("group", "device", "machine", "run"))
    return identity if any(identity) else None


def worker_sample_time(service, source, worker, row_ms):
    if service == "hg" and number(worker.get("sample_ms")) is not None:
        return worker["sample_ms"]
    if service == "ps":
        sample_age = number(worker.get("source_age_ms"))
        if sample_age is None:
            sample_age = number(worker.get("received_age_ms"))
        base = row_ms
    else:
        sample_age = number(worker.get("received_age_ms"))
        base = number(source.get("status_observed_ms"))
    if sample_age is not None and base is not None:
        return base - sample_age
    return number(source.get("network_observed_ms"))


def worker_intervals(rows, latest, started_ms, now_ms, ps_started_ms=None):
    for service in ("ps", "hg"):
        start = max(started_ms, ps_started_ms or started_ms) if service == "ps" else started_ms
        series = {}
        for row in rows:
            source = row.get(service) or {}
            for worker in source.get("workers", []):
                identity = worker_identity(worker)
                at = worker_sample_time(service, source, worker, row["time_ms"])
                if identity is not None and at is not None and start <= at <= now_ms:
                    series.setdefault(identity, []).append((at, number(worker.get("network_bytes")), True))
        for worker in latest[service]["workers"]:
            points = distinct_points(series.get(worker_identity(worker), []))
            current = points[-1] if points else None
            value = measured_interval(points[-2] if len(points) >= 2 else None, current)
            if worker.get("network_bytes") is None:
                value = measured_interval()
            elif current and now_ms - current[0] > (60_000 if service == "ps" else 120_000):
                value.update(per_second=None, complete=False, status="stale")
            worker["network_interval"] = value
            worker["network_first_ms"] = points[0][0] if points else None
            worker["network_last_ms"] = current[0] if current else None


def honeygain_minutes(rows, started_ms, end_ms, desired, hours=24, include_nodes=True, selected_ip=None):
    end = int(end_ms)//60000*60000
    start = end-(hours*60-1)*60000
    series = {}
    for row in rows:
        source = row.get("hg") or {}
        for worker in source.get("workers", []):
            at = worker_sample_time("hg", source, worker, row["time_ms"])
            value = number(worker.get("network_bytes"))
            identity = worker_identity(worker)
            if identity is not None and at is not None and max(started_ms,start-GAP_MS) <= at <= end_ms:
                points = series.setdefault(identity, {})
                point = value, worker.get("ip")
                points[at] = point if at not in points or points[at] == point else (None, None)
    buckets = {}
    for identity, values in series.items():
        points=[(at,*point) for at,point in sorted(values.items())]
        for previous, current in zip(points,points[1:]):
            if previous[2] != current[2] or selected_ip is not None and current[2] != selected_ip:
                continue
            interval = measured_interval((*previous[:2],True),(*current[:2],True))
            if interval["per_second"] is None:
                continue
            first,last = interval["start_ms"],interval["end_ms"]
            for minute in range(max(start,int(first)//60000*60000),min(end,int(last-1)//60000*60000)+1,60000):
                overlap = min(last,minute+60000)-max(first,minute)
                if overlap <= 0:
                    continue
                contribution = buckets.setdefault(minute,{}).setdefault(identity+(current[2],),{"instance":"/".join(x or "" for x in identity),"ip":current[2],"mb":0,"covered_ms":0})
                contribution["mb"] += interval["delta"]*overlap/(last-first)/1e6
                contribution["covered_ms"] += overlap
    result=[]
    for minute in (range(start,end+1,60000) if hours == 24 else sorted(buckets)):
        nodes=sorted(buckets.get(minute,{}).values(),key=lambda n:(-n["mb"],n["instance"]))
        coverage=[node.pop("covered_ms") for node in nodes]
        groups={key[0] for key in buckets.get(minute,{}) if key[0]}
        point = {"minute_ms":minute,"mb":sum(n["mb"] for n in nodes) if nodes else None,
                 "partial":True,"estimated":True,"coverage_complete":bool(coverage) and desired is not None and len(groups)>=desired and all(ms>=60000 for ms in coverage),"count":len(nodes)}
        if include_nodes:
            point["nodes"] = nodes[:20]
        result.append(point)
    return result


def measured_delta(points, start, end, *, final=False, gap_ms=GAP_MS):
    selected = points[bisect_left(points, (start,)):bisect_right(points, (end, float("inf"), True))]
    result = {"delta": None, "per_10_min": None, "coverage_seconds": 0, "status": "missing", "change_percent": None}
    if len(selected) < 2:
        return result
    elapsed = selected[-1][0] - selected[0][0]
    result["coverage_seconds"] = elapsed / 1000
    if any(b[1] < a[1] for a, b in zip(selected, selected[1:])):
        result["status"] = "counter_reset"
        return result
    result["delta"] = selected[-1][1] - selected[0][1]
    if any(b[0] - a[0] > gap_ms for a, b in zip(selected, selected[1:])):
        result["status"] = "gap"
    elif any(not point[2] for point in selected):
        result["status"] = "incomplete"
    elif elapsed < BUCKET_MS * .9 or selected[0][0] - start >= 60_000 or end - selected[-1][0] >= 60_000 or final:
        result["status"] = "partial"
    else:
        result["status"] = "complete"
        result["per_10_min"] = result["delta"] * BUCKET_MS / elapsed
    return result


def rate_buckets(rows, started_ms, end_ms):
    sources = {"ps_credited": {}, "ps_network": {}, "hg_credits": {}, "hg_network": {}}
    for row in rows:
        for metric, service, counter, timestamp, divisor in (
            ("ps_credited", "ps", "provider_bytes", "provider_observed_ms", 1_000_000),
            ("ps_network", "ps", "network_total_bytes", "network_observed_ms", 1_000_000),
            ("hg_credits", "hg", "provider_gathering_credits", "provider_observed_ms", 1),
            ("hg_network", "hg", "network_total_bytes", "network_observed_ms", 1_000_000),
        ):
            source = row.get(service) or {}
            value = number(source.get(counter))
            at = number(source.get(timestamp))
            if metric.endswith("_network") and at is None:
                at = number(row.get("time_ms"))
            if value is not None and at is not None and started_ms <= at <= end_ms:
                # Repeated stale readings are one observation; retain any
                # incomplete same-time sample without manufacturing zero growth.
                points = sources[metric]
                previous = points.get(at)
                complete = not metric.endswith("_network") or source.get("network_complete") is True
                points[at] = (at, value / divisor, complete and (previous[2] if previous else True))
    for key, points in sources.items():
        sources[key] = sorted(points.values())
    count = min(MAX_BUCKETS, max(1, math.ceil((end_ms - started_ms) / BUCKET_MS)))
    first_index = max(0, math.ceil((end_ms - started_ms) / BUCKET_MS) - count)
    buckets = []
    for index in range(first_index, first_index + count):
        start = started_ms + index * BUCKET_MS
        end = min(end_ms, start + BUCKET_MS)
        bucket = {"start_ms": start, "end_ms": end, "open": end - start < BUCKET_MS}
        for metric, points in sources.items():
            bucket[metric] = measured_delta(points, start, end, final=bucket["open"], gap_ms=420_000 if metric == "hg_credits" else GAP_MS)
            current = bucket[metric]
            previous = buckets[-1][metric] if buckets else None
            if previous and current["per_10_min"] is not None and previous["per_10_min"]:
                current["change_percent"] = 100 * (current["per_10_min"] / previous["per_10_min"] - 1)
        buckets.append(bucket)
    return buckets


class LedgerRows:
    """Replay validated records without retaining their decoded object graphs."""
    def __init__(self, entries):
        self.entries = entries

    def __iter__(self):
        for _, row in self.entries:
            if row is not None:
                yield json.loads(zlib.decompress(row))


class FleetTestView:
    def __init__(self, root):
        self.root = Path(root)
        self._cache = {}
        self._cache_lock = threading.Lock()
        self._hg_sources = (None, {})
        self._ledgers = {}

    def ledger(self, path, bounded=True):
        """Cache compressed lines; stream reads and verify the prefix on append."""
        stat = path.stat()
        signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        cache_key = (path, bounded)
        saved = self._ledgers.get(cache_key)
        if saved and saved["signature"] == signature:
            return saved["result"]
        limit = MAX_BYTES if bounded else stat.st_size
        with path.open("rb") as handle:
            def digest(start, end):
                handle.seek(start)
                value = hashlib.sha256()
                for position in range(start, end, 1024 * 1024):
                    length = min(1024 * 1024, end - position)
                    chunk = handle.read(length)
                    if len(chunk) != length:
                        raise OSError("Ledger changed while reading")
                    value.update(chunk)
                return value.digest()
            append = (saved and saved["signature"][:2] == signature[:2]
                      and (saved["entries"] or saved["offset"] == 0)
                      and saved["signature"][2] < stat.st_size <= saved["offset"] + limit
                      and digest(saved["start"], saved["offset"]) == saved["digest"])
            cutoff = max(0, stat.st_size - limit)
            entries = deque(((at, row) for at, row in saved["entries"] if at >= cutoff) if append else (),
                            maxlen=MAX_ROWS if bounded else None)
            count = len(entries)
            handle.seek(saved["offset"] if append else max(0, cutoff - 1))
            if not append and cutoff and handle.read(1) != b"\n":
                handle.readline(stat.st_size - cutoff)
            position = handle.tell()
            new_start = position
            pending = False
            while position < stat.st_size:
                line = handle.readline(stat.st_size - position)
                if not line:
                    raise OSError("Ledger changed while reading")
                if not line.endswith(b"\n"):
                    if handle.tell() != stat.st_size:
                        raise OSError("Ledger changed while reading")
                    pending = True
                    break
                entries.append((position, zlib.compress(line, 1)))
                count += 1
                position += len(line)
            truncated = bounded and (stat.st_size > MAX_BYTES or count > MAX_ROWS
                                     or bool(append and saved["result"][2]))
            entries = list(entries)
            # Apply byte/row retention before decoding, including on cold reads.
            for index, (at, row) in enumerate(entries):
                if at >= new_start:
                    try:
                        if not isinstance(json.loads(zlib.decompress(row)), dict):
                            raise ValueError("Invalid observation")
                    except ValueError:
                        entries[index] = (at, None)
            start = entries[0][0] if entries else position
            checksum = digest(start, position)
        result = (LedgerRows(entries), sum(row is None for _, row in entries) + pending, truncated)
        if cache_key not in self._ledgers and len(self._ledgers) >= 4:
            self._ledgers.pop(next(iter(self._ledgers)))
        self._ledgers[cache_key] = dict(signature=signature, start=start, offset=position, digest=checksum,
                                       entries=entries, result=result)
        return result

    @staticmethod
    def read_json(path):
        with path.open("rb") as handle:
            data = handle.read(128 * 1024 + 1)
        if len(data) > 128 * 1024:
            raise ValueError("Experiment metadata is too large")
        raw = json.loads(data)
        if not isinstance(raw, dict):
            raise ValueError("Experiment metadata is invalid")
        return raw

    def service_windows(self, experiment, now_ms, archived=False):
        start = experiment["started_ms"] or now_ms
        windows = {service: {"start_ms": start, "basis": "test", "observed_ms": None,
                             "stale": False if archived or service == "hg" else True,
                             "state": "archived" if archived else "unknown"} for service in ("ps", "hg")}
        if archived:
            return windows, None
        try:
            census = self.read_json(self.root.parent / "packetstream" / "native-census.json")
            target = experiment["ps"]
            if not target.get("organization") or not target.get("group") or census.get("group") != f'{target["organization"]}/default/{target["group"]}':
                return windows, None
            group_state = census.get("group_state") or {}
            restarted = timestamp_ms(group_state.get("start_time"))
            observed = timestamp_ms(census.get("last_success_at") or census.get("checked_at"))
            if restarted is None or restarted > now_ms or observed is None or restarted > observed:
                return windows, None
            state = group_state.get("status")
            if state not in {"running", "stopped", "stopping", "pending", "starting", "allocating", "creating", "deploying", "failed", "succeeded"}:
                state = "unknown"
            windows["ps"].update(start_ms=max(start, restarted), basis="native_restart" if restarted > start else "test",
                                 observed_ms=observed, stale=not 0 <= now_ms - observed <= 90_000, state=state)
            nodes = census.get("nodes")
            if not isinstance(nodes, list):
                return windows, None
            runs = set()
            for node in nodes[:50]:
                if not isinstance(node, dict):
                    continue
                machine, instance = node.get("machine"), node.get("instance_id")
                if all(isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value) for value in (machine, instance)):
                    runs.add(machine + "/" + instance)
            return windows, {"observed_ms": observed, "runs": runs}
        except (OSError, ValueError, TypeError, AttributeError):
            return windows, None

    def archives(self):
        result = []
        archive_root = self.root / "archives"
        if not archive_root.is_dir():
            return result
        for child in sorted(archive_root.iterdir(), reverse=True)[:100]:
            if child.is_symlink() or not child.is_dir() or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", child.name):
                continue
            try:
                metadata = clean_experiment(self.read_json(child / "current.json"))
                result.append({"id": child.name, "started_ms": metadata["started_ms"], "status": metadata["status"]})
            except (OSError, ValueError):
                continue
        return result

    def snapshot(self, now_ms=None, archive=None, include_observations=False, honeygain_only=False, chart_hours=24):
        with self._cache_lock:
            if now_ms is not None or archive or include_observations:
                return self._snapshot(now_ms, archive, include_observations, honeygain_only, chart_hours)
            record, signature = None, None
            if honeygain_only:
                try:
                    record = self.read_json(self.root / "latest.json")
                    source = self.root.parent / "hg-fleet" / "observations.jsonl"
                    signature = (record.get("experiment_id"), json.dumps(record.get("hg"), sort_keys=True),
                                 bool(record.get("errors") or record.get("error")),
                                 (self.root / "current.json").stat().st_mtime_ns,
                                 source.stat().st_mtime_ns if source.exists() else None)
                except (OSError, ValueError):
                    record = None
            cache_key = honeygain_only if chart_hours == 24 else (honeygain_only, chart_hours)
            cached = self._cache.get(cache_key)
            if cached is None or cached[2] != signature or time.monotonic() - cached[0] >= (60 if record else 5):
                cached = (time.monotonic(), self._snapshot(honeygain_only=honeygain_only, chart_hours=chart_hours), signature)
                self._cache[cache_key] = cached
            value = cached[1]
            if not record or not value.get("latest") or record.get("experiment_id") != value["experiment"]["id"]:
                return value
            result = dict(value)
            result["latest"] = deepcopy(value["latest"])
            now = int(time.time() * 1000)
            elapsed = max(0, now - value["generated_ms"])
            latest = result["latest"]
            latest["time_ms"] = number(record.get("time_ms"))
            latest["recorded_at"] = record.get("recorded_at")
            result["recorder_age_ms"] = max(0, now - latest["time_ms"]) if latest["time_ms"] is not None else None
            result["recorder_stale"] = result["recorder_age_ms"] is None or result["recorder_age_ms"] > GAP_MS
            hg = latest["hg"]
            for field in ("status_age_ms", "provider_age_ms"):
                if hg.get(field) is not None:
                    hg[field] += elapsed
            hg["status_stale"] = hg["status_age_ms"] is None or hg["status_age_ms"] > 120_000
            for worker in hg["workers"]:
                if worker.get("received_age_ms") is not None:
                    worker["received_age_ms"] += elapsed
                interval = worker.get("network_interval") or {}
                if interval.get("end_ms") is not None and now - interval["end_ms"] > 120_000:
                    interval.update(per_second=None, complete=False, status="stale")
            result["service_windows"], _ = self.service_windows(value["experiment"], now)
            warnings = [w for w in value["warnings"] if w not in (STALE_RECORDER, STALE_HG, STALE_CENSUS)]
            if result["service_windows"]["ps"]["stale"]:
                warnings.insert(0, STALE_CENSUS)
            if result["recorder_stale"]:
                warnings.append(STALE_RECORDER)
            if hg["status_stale"]:
                warnings.append(STALE_HG)
            result["warnings"] = warnings
            return result

    def native_rows(self, experiment, start, end, identities=None):
        selected = {}
        for row in identities or []:
            if row.get("group") and row.get("run"):
                selected.setdefault((row["group"], row["run"]), set()).add((row.get("machine"), row.get("device")))
        for observed, packed in self._hg_sources[1].items():
            if observed < start-GAP_MS:
                continue
            workers = []
            for original in json.loads(zlib.decompress(packed)).values():
                if original.get("organization") != experiment["hg"].get("organization"):
                    continue
                if identities is not None:
                    matches = {identity for identity in selected.get((original.get("group"), original.get("instance")), ())
                               if identity[0] is None or identity[0] == original.get("machine")}
                    source_identity = original.get("machine"), original.get("device")
                    if source_identity in matches:
                        recorded_identity = source_identity
                    elif len(matches) == 1:
                        recorded_identity = next(iter(matches))
                    else:
                        continue
                sample = original.get("sample") or {}
                rx, tx = number(sample.get("rx_total")), number(sample.get("tx_total"))
                worker = pick(original, ("group", "device", "machine", "instance", "ip"), ("sample_ms",))
                worker["run"] = worker.pop("instance")
                if identities is not None:
                    # Keep the test's identity when supplemental metadata is
                    # richer, so the same counter is not counted in two series.
                    worker["machine"], worker["device"] = recorded_identity
                worker["network_bytes"] = rx+tx if rx is not None and tx is not None and rx>=0 and tx>=0 and worker["ip"] not in HG_EXCLUDED_IPS else None
                workers.append(worker)
            yield {"time_ms": observed, "hg": {"workers": workers}}


    def _snapshot(self, now_ms=None, archive=None, include_observations=False, honeygain_only=False, chart_hours=24):
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        directory = self.root
        if archive is not None:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", archive):
                raise ValueError("Invalid archive identifier")
            directory = self.root / "archives" / archive
            if directory.is_symlink() or directory.resolve().parent != (self.root / "archives").resolve():
                raise ValueError("Invalid archive identifier")
        try:
            experiment = clean_experiment(self.read_json(directory / "current.json"))
        except FileNotFoundError:
            return {"experiment": None, "warnings": ["Independent test recorder has not started."], "archives": self.archives()}
        rows, invalid, truncated = [], 0, False
        previous_hg = None
        source_path = self.root.parent / "hg-fleet" / "observations.jsonl"
        if source_path.exists() and self._hg_sources[0] != source_path.stat().st_mtime_ns:
            sources = {}
            source_stamp = source_path.stat().st_mtime_ns
            source_rows, _, _ = self.ledger(source_path, bounded=False)
            for source in source_rows:
                try:
                    workers = {w["group"]: dict(w, sample_ms=timestamp_ms((w.get("sample") or {}).get("time")))
                               for w in source.get("workers", [])}
                    sources[source["time_ms"]] = zlib.compress(json.dumps(workers).encode(), 1)
                except (ValueError, KeyError):
                    continue
            self._hg_sources = (source_stamp, sources)
        path = directory / "observations.jsonl"
        if path.exists():
            raw_rows, invalid, truncated = self.ledger(path)
            source_at, source_workers = object(), {}
            for raw in raw_rows:
                try:
                    if raw.get("experiment_id") == experiment["id"]:
                        hg = raw["hg"] = raw.get("hg") or {}
                        hg["workers"] = [dict(w) for w in hg.get("workers", [])]
                        if source_at != hg.get("status_observed_ms"):
                            source_at = hg.get("status_observed_ms")
                            packed = self._hg_sources[1].get(source_at)
                            source_workers = json.loads(zlib.decompress(packed)) if packed is not None else {}
                        for worker in hg.get("workers", []):
                            original = source_workers.get(worker.get("group"), {})
                            sample = original.get("sample") or {}
                            if (original.get("machine") == worker.get("machine")
                                    and (not worker.get("run") or original.get("instance") == worker["run"])
                                    and (not worker.get("ip") or not original.get("ip") or original["ip"] == worker["ip"])
                                    and number(sample.get("rx_total")) is not None and number(sample.get("tx_total")) is not None
                                    and sample["rx_total"] + sample["tx_total"] == worker.get("network_bytes")):
                                worker.update(sample_ms=original.get("sample_ms"), run=original.get("instance"))
                        if honeygain_only:
                            if rows and hg == previous_hg:
                                rows[-1]["time_ms"] = raw["time_ms"]
                                rows[-1]["recorded_at"] = raw.get("recorded_at")
                                continue
                            previous_hg = hg
                            raw["ps"] = {}
                        row = clean_observation(raw)
                        if row["time_ms"] is not None and row["time_ms"] >= (experiment["started_ms"] or 0):
                            rows.append(row)
                except (ValueError, TypeError, AttributeError):
                    invalid += 1
        rows.sort(key=lambda row: row["time_ms"])
        excluded_last, excluded_bytes = {}, 0
        for row in rows:
            hg = row["hg"]
            for worker in hg["workers"]:
                if worker.get("ip") not in HG_EXCLUDED_IPS:
                    continue
                identity, value = worker_identity(worker), worker.get("network_bytes")
                if identity is not None and value is not None:
                    previous = excluded_last.get(identity, value)
                    excluded_bytes += max(0, value - previous)
                    excluded_last[identity] = value
                worker["network_bytes"] = None
            if excluded_bytes and hg["network_total_bytes"] is not None:
                hg["network_total_bytes"] = max(0, hg["network_total_bytes"] - excluded_bytes)
                hg["network_rx_bytes"] = hg["network_tx_bytes"] = None
                hg["network_complete"] = False
            if truncated:
                hg["network_total_bytes"] = None
                hg["network_complete"] = False
        # Keep ledger source ages unchanged while advancing the live view clock.
        latest = deepcopy(rows[-1]) if rows else None
        if latest:
            latest["hg"]["workers"] = [w for w in latest["hg"]["workers"] if w.get("ip") not in HG_EXCLUDED_IPS]
        age = max(0, now_ms - latest["time_ms"]) if latest else None
        stale = age is None or age > GAP_MS
        start = experiment["started_ms"] or now_ms
        end = latest["time_ms"] if archive and latest else now_ms
        windows, census = self.service_windows(experiment, now_ms, archived=bool(archive))
        ps_start = windows["ps"]["start_ms"]
        warnings = []
        if not archive and windows["ps"]["stale"]:
            warnings.append(STALE_CENSUS)
        if stale and not archive:
            warnings.append(STALE_RECORDER)
        if invalid:
            warnings.append("An incomplete or invalid ledger line was skipped; the next refresh will retry.")
        if truncated:
            warnings.append("Showing the bounded ledger tail. Older records remain in the saved ledger.")
        if latest and latest["collection_error"]:
            warnings.append("A collector reported an error. Inspect its local recorder log.")
        timeline, timeline_truncated = rate_timeline(rows, start, max(start, end), experiment["hg"].get("baseline_provider_observed_ms"), ps_start)
        if timeline_truncated:
            warnings.append("The activity graph shows the latest 2,000 measured intervals per source. Older observations remain in history.")
        summary = {"ps_credited_mb": None, "ps_network_mb": None, "hg_gathering_credits": None, "hg_network_mb": None}
        if latest:
            ps, hg = latest["ps"], latest["hg"]
            ps["inventory_status"] = "ledger"
            if census and (census["observed_ms"] >= (ps["status_observed_ms"] or 0) or latest["time_ms"] < ps_start):
                current_workers = [w for w in ps["workers"] if instance_run(w.get("run")) in census["runs"] and latest["time_ms"] >= ps_start]
                if len(current_workers) != len(ps["workers"]):
                    ps["workers"] = current_workers
                    ps["inventory_status"] = "awaiting_current_run"
                    for field in ("running", "accepted", "reporting", "monitor_connections", "network_rx_bytes", "network_tx_bytes", "network_total_bytes", "network_observed_ms"):
                        ps[field] = None
                    ps["network_complete"] = False
                    warnings.append("PacketStream restarted or changed instances. Older container rows are hidden while the recorder catches up with the current census.")
                else:
                    ps["inventory_status"] = "matched"
            if ps_start > start:
                for worker in ps["workers"]:
                    client_age = worker.get("client_status_age_ms")
                    if client_age is not None and latest["time_ms"] - client_age < ps_start:
                        worker.update(client_status="unknown", client_status_age_ms=None, error_code=None)
            worker_intervals(rows, latest, start, end, ps_start)
            ps["status_age_ms"] = max(0, now_ms - ps["status_observed_ms"]) if ps["status_observed_ms"] is not None else None
            ps["status_stale"] = ps["status_age_ms"] is None or ps["status_age_ms"] > 90_000
            if ps["status_stale"] and not archive:
                warnings.append("PacketStream worker census is stale or missing. Running and accepted counts are last recorded states.")
            hg["status_age_ms"] = max(0, now_ms - hg["status_observed_ms"]) if hg["status_observed_ms"] is not None else None
            hg["status_stale"] = hg["status_age_ms"] is None or hg["status_age_ms"] > 120_000
            if hg["status_stale"] and not archive:
                warnings.append(STALE_HG)
            # Source ages were captured at different times. Advance them to the
            # current snapshot clock; a healthy fleet recorder cannot freshen an
            # old Honeygain observer or an old PacketStream client sample.
            for service, elapsed in ((ps, age), (hg, hg["status_age_ms"])):
                for worker in service["workers"]:
                    for field in ("received_age_ms", "client_status_age_ms", "source_age_ms"):
                        if worker.get(field) is not None and elapsed is not None:
                            worker[field] += elapsed
                    if service is ps:
                        client_age = worker.get("client_status_age_ms")
                        worker["client_status_stale"] = stale or ps["status_stale"] or client_age is None or client_age > (240_000 if worker.get("native_status_only") else 120_000)
            for service, timestamp in ((ps, "provider_observed_ms"), (hg, "provider_observed_ms")):
                service["provider_age_ms"] = max(0, now_ms - service[timestamp]) if service[timestamp] is not None else None
            baseline, current = experiment["ps"].get("baseline_provider_bytes"), ps["provider_bytes"]
            paid_values = [r["ps"]["provider_bytes"] for r in rows if r["ps"]["provider_bytes"] is not None
                           and (r["ps"]["provider_observed_ms"] or 0) >= ps_start]
            paid_reset = any(b < a for a, b in zip(paid_values, paid_values[1:]))
            if baseline is not None and current is not None and current >= baseline and not paid_reset and (ps["provider_observed_ms"] or 0) >= start:
                summary["ps_credited_mb"] = (current - baseline) / 1_000_000
            if paid_reset:
                warnings.append("PacketStream's account counter decreased during this window. Its total delta is unknown.")
            summary["ps_network_mb"] = ps["network_total_bytes"] / 1_000_000 if ps["network_total_bytes"] is not None else None
            if ps_start > start:
                paid_points, paid_delta = window_counter(rows, "provider_bytes", "provider_observed_ms", ps_start, end)
                network_points, network_delta = window_counter(rows, "network_total_bytes", "network_observed_ms", ps_start, end)
                windows["ps"].update(provider_baseline_ms=paid_points[0][0] if paid_points else None,
                                     provider_baseline_bytes=paid_points[0][1] if paid_points else None,
                                     network_baseline_ms=network_points[0][0] if network_points else None)
                paid_fresh = paid_points and 0 <= now_ms - paid_points[-1][0] <= GAP_MS and not windows["ps"]["stale"]
                summary["ps_credited_mb"] = paid_delta / 1_000_000 if paid_delta is not None and paid_fresh else None
                summary["ps_network_mb"] = network_delta / 1_000_000 if network_delta is not None else None
                ps["network_complete"] = ps["network_complete"] and network_delta is not None and all(p[2] for p in network_points)
            summary["hg_network_mb"] = hg["network_total_bytes"] / 1_000_000 if hg["network_total_bytes"] is not None else None
            hg_baseline = experiment["hg"].get("baseline_provider_gathering_credits")
            hg_baseline_at = experiment["hg"].get("baseline_provider_observed_ms")
            explicit_hg_baseline = hg_baseline is not None and hg_baseline_at is not None
            hg_readings = sorted({r["hg"]["provider_observed_ms"]: r["hg"]["provider_gathering_credits"] for r in rows
                                 if r["hg"]["provider_gathering_credits"] is not None
                                 and (r["hg"]["provider_observed_ms"] or 0) >= (hg_baseline_at if explicit_hg_baseline else start)}.items())
            if explicit_hg_baseline:
                # The explicit reading is the independent Honeygain baseline;
                # never fall back to startup credits if later readings are absent.
                valid_hg = [hg_baseline] + [value for at, value in hg_readings if at > hg_baseline_at]
            else:
                valid_hg = [value for at, value in hg_readings]
            if len(valid_hg) >= 2 and all(b >= a for a, b in zip(valid_hg, valid_hg[1:])):
                summary["hg_gathering_credits"] = valid_hg[-1] - valid_hg[0]
            # Pad missing inventory without manufacturing running clients.
            for service in ("ps", "hg"):
                wanted = min(50, int(experiment[service].get("desired") or 10))
                while len(latest[service]["workers"]) < wanted:
                    latest[service]["workers"].append({"state": "unknown", "client_status": "unknown"})
        result = {"experiment": experiment, "latest": latest, "summary": summary, "observation_count": len(rows),
                  "recorder_age_ms": age, "recorder_stale": stale, "invalid_lines": invalid, "truncated": truncated,
                  "warnings": warnings, "archives": self.archives(), "archived": bool(archive),
                  "service_windows": windows,
                  "timeline": timeline, "timeline_truncated": timeline_truncated,
                  "hg_minutes": honeygain_minutes(rows, start, end, experiment["hg"].get("desired")),
                  "buckets": rate_buckets(rows, start, max(start, end)), "generated_ms": now_ms}
        if chart_hours > 24 and self._hg_sources[1]:
            result["retained_hg_minutes"] = honeygain_minutes(self.native_rows(experiment, max(start, end-chart_hours*3600000), end), start, end, experiment["hg"].get("desired"), chart_hours, include_nodes=False)
        if include_observations:
            result["observations"] = rows
        return result


def fleet_csv(snapshot):
    output = io.StringIO(newline="")
    fields = ["start_ms", "end_ms", "ps_network_mb", "ps_network_status", "ps_credited_mb", "ps_credited_mb_per_10_min",
              "ps_credited_change_percent", "ps_credited_coverage_seconds", "ps_credited_status", "hg_network_mb", "hg_network_status", "hg_gathering_credits", "hg_status"]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for bucket in snapshot.get("buckets", []):
        writer.writerow({"start_ms": bucket["start_ms"], "end_ms": bucket["end_ms"],
                         "ps_network_mb": bucket["ps_network"]["delta"], "ps_network_status": bucket["ps_network"]["status"],
                         "ps_credited_mb": bucket["ps_credited"]["delta"], "ps_credited_mb_per_10_min": bucket["ps_credited"]["per_10_min"],
                         "ps_credited_change_percent": bucket["ps_credited"]["change_percent"],
                         "ps_credited_coverage_seconds": bucket["ps_credited"]["coverage_seconds"],
                         "ps_credited_status": bucket["ps_credited"]["status"],
                         "hg_network_mb": bucket["hg_network"]["delta"], "hg_network_status": bucket["hg_network"]["status"],
                         "hg_gathering_credits": bucket["hg_credits"]["delta"], "hg_status": bucket["hg_credits"]["status"]})
    return output.getvalue().encode("utf-8")
