"""SQLite persistence and aggregation for Salad bandwidth samples."""

from __future__ import annotations

import json
import re
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


APP_NAMES = ("packetstream", "proxyrack", "traffmonetizer", "earnfm", "repocket", "bitping", "honeygain")
MINUTE_MS = 60_000
LIVE_SAMPLE_GRACE_MS = 120_000
HONEYGAIN_EVENTS = {
    "start",
    "authenticated",
    "service_starting",
    "connected",
    "disconnected",
    "network_overused",
    "duplicate_device",
    "device_limit_exceeded",
    "unusable_network",
    "authentication_error",
    "api_ping_error",
    "exit",
    "stopping",
    "stopped",
}


def parse_time_ms(value: str) -> int:
    value = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], value, count=1)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(round(parsed.timestamp() * 1000))


class Store:
    def __init__(self, path: Path, raw_retention_days: int = 30, read_only: bool = False):
        self.path = Path(path)
        self.raw_retention_days = raw_retention_days
        if read_only:
            self._lock = threading.RLock()
            self._db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA foreign_keys = ON")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA synchronous = NORMAL")
        self._create_schema()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _create_schema(self) -> None:
        with self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS samples (
                    instance_id TEXT NOT NULL,
                    sample_time_ms INTEGER NOT NULL,
                    sequence INTEGER NOT NULL,
                    receive_time_ms INTEGER NOT NULL,
                    machine_id TEXT NOT NULL,
                    public_ip TEXT NOT NULL DEFAULT '',
                    public_ip_status TEXT NOT NULL DEFAULT 'unknown',
                    interface TEXT NOT NULL,
                    counter_reset INTEGER NOT NULL,
                    eth_rx_total INTEGER NOT NULL,
                    eth_tx_total INTEGER NOT NULL,
                    eth_rx_bytes INTEGER NOT NULL,
                    eth_tx_bytes INTEGER NOT NULL,
                    eth_rx_rate INTEGER NOT NULL DEFAULT 0,
                    eth_tx_rate INTEGER NOT NULL DEFAULT 0,
                    captured_rx_bytes INTEGER NOT NULL,
                    captured_tx_bytes INTEGER NOT NULL,
                    unassigned_rx_bytes INTEGER NOT NULL,
                    unassigned_tx_bytes INTEGER NOT NULL,
                    coverage_percent REAL NOT NULL,
                    organization_name TEXT NOT NULL DEFAULT '',
                    project_name TEXT NOT NULL DEFAULT '',
                    container_group_name TEXT NOT NULL DEFAULT '',
                    container_group_version TEXT NOT NULL DEFAULT '',
                    capture_mode TEXT NOT NULL DEFAULT 'packet',
                    interval_milliseconds INTEGER NOT NULL DEFAULT 1000,
                    partial_interval INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (instance_id, sample_time_ms, sequence)
                );
                CREATE INDEX IF NOT EXISTS samples_time ON samples(sample_time_ms);
                CREATE INDEX IF NOT EXISTS samples_receive_time ON samples(receive_time_ms);

                CREATE TABLE IF NOT EXISTS app_samples (
                    instance_id TEXT NOT NULL,
                    sample_time_ms INTEGER NOT NULL,
                    sequence INTEGER NOT NULL,
                    app_name TEXT NOT NULL,
                    rx_bytes INTEGER NOT NULL,
                    tx_bytes INTEGER NOT NULL,
                    process_count INTEGER NOT NULL,
                    socket_count INTEGER NOT NULL,
                    tcp_socket_count INTEGER NOT NULL DEFAULT 0,
                    tcp_established_count INTEGER NOT NULL DEFAULT 0,
                    udp_socket_count INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL,
                    PRIMARY KEY (instance_id, sample_time_ms, sequence, app_name),
                    FOREIGN KEY (instance_id, sample_time_ms, sequence)
                        REFERENCES samples(instance_id, sample_time_ms, sequence) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS app_samples_time ON app_samples(sample_time_ms);

                CREATE TABLE IF NOT EXISTS instance_segments (
                    instance_id TEXT PRIMARY KEY,
                    machine_id TEXT NOT NULL,
                    first_seen_ms INTEGER NOT NULL,
                    last_seen_ms INTEGER NOT NULL,
                    public_ip TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS minute_rollups (
                    minute_ms INTEGER NOT NULL,
                    instance_id TEXT NOT NULL,
                    machine_id TEXT NOT NULL,
                    sample_count INTEGER NOT NULL,
                    eth_rx_bytes INTEGER NOT NULL,
                    eth_tx_bytes INTEGER NOT NULL,
                    captured_rx_bytes INTEGER NOT NULL,
                    captured_tx_bytes INTEGER NOT NULL,
                    unassigned_rx_bytes INTEGER NOT NULL,
                    unassigned_tx_bytes INTEGER NOT NULL,
                    reset_count INTEGER NOT NULL,
                    partial_interval_count INTEGER NOT NULL DEFAULT 0,
                    eth_rx_rate_sum INTEGER NOT NULL DEFAULT 0,
                    eth_tx_rate_sum INTEGER NOT NULL DEFAULT 0,
                    coverage_sum REAL NOT NULL DEFAULT 0,
                    first_sample_ms INTEGER NOT NULL DEFAULT 0,
                    last_sample_ms INTEGER NOT NULL DEFAULT 0,
                    peak_rate INTEGER NOT NULL DEFAULT 0,
                    peak_time_ms INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (minute_ms, instance_id)
                );
                CREATE INDEX IF NOT EXISTS minute_rollups_time ON minute_rollups(minute_ms);

                CREATE TABLE IF NOT EXISTS minute_app_rollups (
                    minute_ms INTEGER NOT NULL,
                    instance_id TEXT NOT NULL,
                    app_name TEXT NOT NULL,
                    rx_bytes INTEGER NOT NULL,
                    tx_bytes INTEGER NOT NULL,
                    PRIMARY KEY (minute_ms, instance_id, app_name),
                    FOREIGN KEY (minute_ms, instance_id)
                        REFERENCES minute_rollups(minute_ms, instance_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS ingest_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    watermark_ms INTEGER,
                    last_poll_ms INTEGER,
                    last_success_ms INTEGER,
                    last_new_samples INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT ''
                );
                INSERT OR IGNORE INTO ingest_state(singleton) VALUES (1);

                CREATE TABLE IF NOT EXISTS runtime_events (
                    instance_id TEXT NOT NULL,
                    event_time_ms INTEGER NOT NULL,
                    receive_time_ms INTEGER NOT NULL,
                    machine_id TEXT NOT NULL,
                    organization_name TEXT NOT NULL DEFAULT '',
                    project_name TEXT NOT NULL DEFAULT '',
                    container_group_name TEXT NOT NULL DEFAULT '',
                    container_group_version TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL,
                    event TEXT NOT NULL,
                    client_version TEXT NOT NULL DEFAULT '',
                    attempt INTEGER NOT NULL DEFAULT 0,
                    exit_code INTEGER NOT NULL DEFAULT 0,
                    severity TEXT NOT NULL DEFAULT 'default',
                    PRIMARY KEY (instance_id, event_time_ms, kind, event, attempt)
                );
                CREATE INDEX IF NOT EXISTS runtime_events_time ON runtime_events(event_time_ms);
                """
            )
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(app_samples)").fetchall()}
        with self._db:
            for name in ("tcp_socket_count", "tcp_established_count", "udp_socket_count"):
                if name not in columns:
                    self._db.execute(f"ALTER TABLE app_samples ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
        sample_columns = {row[1] for row in self._db.execute("PRAGMA table_info(samples)").fetchall()}
        sample_additions = {
            "organization_name": "TEXT NOT NULL DEFAULT ''",
            "project_name": "TEXT NOT NULL DEFAULT ''",
            "container_group_name": "TEXT NOT NULL DEFAULT ''",
            "container_group_version": "TEXT NOT NULL DEFAULT ''",
            "capture_mode": "TEXT NOT NULL DEFAULT 'packet'",
            "interval_milliseconds": "INTEGER NOT NULL DEFAULT 1000",
            "partial_interval": "INTEGER NOT NULL DEFAULT 0",
            "eth_rx_rate": "INTEGER NOT NULL DEFAULT 0",
            "eth_tx_rate": "INTEGER NOT NULL DEFAULT 0",
        }
        with self._db:
            for name, definition in sample_additions.items():
                if name not in sample_columns:
                    self._db.execute(f"ALTER TABLE samples ADD COLUMN {name} {definition}")
            if "eth_rx_rate" not in sample_columns:
                self._db.execute("UPDATE samples SET eth_rx_rate=eth_rx_bytes")
            if "eth_tx_rate" not in sample_columns:
                self._db.execute("UPDATE samples SET eth_tx_rate=eth_tx_bytes")
        minute_columns = {row[1] for row in self._db.execute("PRAGMA table_info(minute_rollups)").fetchall()}
        minute_additions = {
            "partial_interval_count": "INTEGER NOT NULL DEFAULT 0",
            "eth_rx_rate_sum": "INTEGER NOT NULL DEFAULT 0",
            "eth_tx_rate_sum": "INTEGER NOT NULL DEFAULT 0",
            "coverage_sum": "REAL NOT NULL DEFAULT 0",
            "first_sample_ms": "INTEGER NOT NULL DEFAULT 0",
            "last_sample_ms": "INTEGER NOT NULL DEFAULT 0",
            "peak_rate": "INTEGER NOT NULL DEFAULT 0",
            "peak_time_ms": "INTEGER NOT NULL DEFAULT 0",
        }
        with self._db:
            for name, definition in minute_additions.items():
                if name not in minute_columns:
                    self._db.execute(f"ALTER TABLE minute_rollups ADD COLUMN {name} {definition}")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def ingest(self, log_entry: Dict[str, Any]) -> bool:
        payload = log_entry.get("json_log")
        # Older monitor images forward this native transition without emitting
        # a structured event. Preserve the actual event time and source labels.
        if log_entry.get("text_log") == "Honeygain service disconnected":
            return self._ingest_runtime_event(log_entry, {
                "kind": "honeygain_event", "schema_version": 1,
                "event": "disconnected", "time": log_entry.get("time"),
            })
        if not isinstance(payload, dict):
            return False
        kind = payload.get("kind")
        if kind in ("honeygain_event", "bandwidth_monitor_status"):
            return self._ingest_runtime_event(log_entry, payload)
        if kind != "bandwidth_sample":
            return False
        if payload.get("schema_version") != 1:
            return False
        labels = ((log_entry.get("resource") or {}).get("labels") or {})
        instance_id = str(labels.get("instance_id") or payload.get("instance_id") or "unknown")
        machine_id = str(labels.get("machine_id") or payload.get("machine_id") or "unknown")
        sample_time_ms = parse_time_ms(str(payload["sample_time"]))
        receive_time_ms = parse_time_ms(str(log_entry.get("receive_time") or log_entry.get("time") or payload["sample_time"]))
        sequence = int(payload.get("sequence", 0))
        eth0 = payload.get("eth0") or {}
        captured = payload.get("captured") or {}
        unassigned = payload.get("unassigned") or {}
        public_ip = str(payload.get("public_ip") or "")
        eth_rx_bytes = int(eth0.get("rx_bytes_delta", eth0.get("rx_bytes_per_second", 0)))
        eth_tx_bytes = int(eth0.get("tx_bytes_delta", eth0.get("tx_bytes_per_second", 0)))
        eth_rx_rate = int(eth0.get("rx_bytes_per_second", 0))
        eth_tx_rate = int(eth0.get("tx_bytes_per_second", 0))
        coverage_percent = float(payload.get("coverage_percent", 0.0))
        partial_interval = int(bool(payload.get("partial_interval")))
        row = (
            instance_id,
            sample_time_ms,
            sequence,
            receive_time_ms,
            machine_id,
            public_ip,
            str(payload.get("public_ip_status") or "unknown"),
            str(payload.get("interface") or "eth0"),
            int(bool(payload.get("counter_reset"))),
            int(eth0.get("rx_bytes_total", 0)),
            int(eth0.get("tx_bytes_total", 0)),
            eth_rx_bytes,
            eth_tx_bytes,
            eth_rx_rate,
            eth_tx_rate,
            int(captured.get("rx_bytes", 0)),
            int(captured.get("tx_bytes", 0)),
            int(unassigned.get("rx_bytes", 0)),
            int(unassigned.get("tx_bytes", 0)),
            coverage_percent,
            str(labels.get("organization_name") or ""),
            str(labels.get("project_name") or ""),
            str(labels.get("container_group_name") or ""),
            str(labels.get("container_group_version") or ""),
            str(payload.get("capture_mode") or "packet"),
            int(payload.get("interval_milliseconds") or 1000),
            partial_interval,
        )
        with self._lock, self._db:
            cursor = self._db.execute(
                """
                INSERT OR IGNORE INTO samples(
                    instance_id, sample_time_ms, sequence, receive_time_ms, machine_id,
                    public_ip, public_ip_status, interface, counter_reset, eth_rx_total,
                    eth_tx_total, eth_rx_bytes, eth_tx_bytes, eth_rx_rate, eth_tx_rate, captured_rx_bytes,
                    captured_tx_bytes, unassigned_rx_bytes, unassigned_tx_bytes,
                    coverage_percent, organization_name, project_name, container_group_name,
                    container_group_version, capture_mode, interval_milliseconds,
                    partial_interval
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                row,
            )
            if cursor.rowcount == 0:
                return False
            for app_name, app in (payload.get("applications") or {}).items():
                if app_name not in APP_NAMES or not isinstance(app, dict):
                    continue
                self._db.execute(
                    """
                    INSERT INTO app_samples(
                        instance_id, sample_time_ms, sequence, app_name, rx_bytes, tx_bytes,
                        process_count, socket_count, tcp_socket_count, tcp_established_count,
                        udp_socket_count, state
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        instance_id,
                        sample_time_ms,
                        sequence,
                        app_name,
                        int(app.get("attributed_packet_rx_bytes", 0)),
                        int(app.get("attributed_packet_tx_bytes", 0)),
                        int(app.get("process_count", 0)),
                        int(app.get("socket_count", 0)),
                        int(app.get("tcp_socket_count", 0)),
                        int(app.get("tcp_established_count", 0)),
                        int(app.get("udp_socket_count", 0)),
                        str(app.get("state") or "down"),
                    ),
                )
            self._db.execute(
                """
                INSERT INTO instance_segments(instance_id, machine_id, first_seen_ms, last_seen_ms, public_ip)
                VALUES (?,?,?,?,?)
                ON CONFLICT(instance_id) DO UPDATE SET
                    machine_id=excluded.machine_id,
                    first_seen_ms=min(first_seen_ms, excluded.first_seen_ms),
                    last_seen_ms=max(last_seen_ms, excluded.last_seen_ms),
                    public_ip=CASE WHEN excluded.public_ip <> '' THEN excluded.public_ip ELSE public_ip END
                """,
                (instance_id, machine_id, sample_time_ms, sample_time_ms, public_ip),
            )
            minute_ms = (sample_time_ms // MINUTE_MS) * MINUTE_MS
            peak_rate = eth_rx_rate + eth_tx_rate
            self._db.execute(
                """
                INSERT INTO minute_rollups(
                    minute_ms, instance_id, machine_id, sample_count, eth_rx_bytes, eth_tx_bytes,
                    captured_rx_bytes, captured_tx_bytes, unassigned_rx_bytes, unassigned_tx_bytes,
                    reset_count, partial_interval_count, eth_rx_rate_sum, eth_tx_rate_sum,
                    coverage_sum, first_sample_ms, last_sample_ms, peak_rate, peak_time_ms
                )
                SELECT ?, instance_id, machine_id, 1, eth_rx_bytes, eth_tx_bytes,
                    captured_rx_bytes, captured_tx_bytes, unassigned_rx_bytes, unassigned_tx_bytes,
                    counter_reset, partial_interval, eth_rx_rate, eth_tx_rate,
                    coverage_percent, sample_time_ms, sample_time_ms,
                    ?, sample_time_ms
                FROM samples
                WHERE instance_id=? AND sample_time_ms=? AND sequence=?
                ON CONFLICT(minute_ms, instance_id) DO UPDATE SET
                    machine_id=excluded.machine_id,
                    sample_count=minute_rollups.sample_count + 1,
                    eth_rx_bytes=minute_rollups.eth_rx_bytes + excluded.eth_rx_bytes,
                    eth_tx_bytes=minute_rollups.eth_tx_bytes + excluded.eth_tx_bytes,
                    captured_rx_bytes=minute_rollups.captured_rx_bytes + excluded.captured_rx_bytes,
                    captured_tx_bytes=minute_rollups.captured_tx_bytes + excluded.captured_tx_bytes,
                    unassigned_rx_bytes=minute_rollups.unassigned_rx_bytes + excluded.unassigned_rx_bytes,
                    unassigned_tx_bytes=minute_rollups.unassigned_tx_bytes + excluded.unassigned_tx_bytes,
                    reset_count=minute_rollups.reset_count + excluded.reset_count,
                    partial_interval_count=minute_rollups.partial_interval_count + excluded.partial_interval_count,
                    eth_rx_rate_sum=minute_rollups.eth_rx_rate_sum + excluded.eth_rx_rate_sum,
                    eth_tx_rate_sum=minute_rollups.eth_tx_rate_sum + excluded.eth_tx_rate_sum,
                    coverage_sum=minute_rollups.coverage_sum + excluded.coverage_sum,
                    first_sample_ms=min(minute_rollups.first_sample_ms, excluded.first_sample_ms),
                    last_sample_ms=max(minute_rollups.last_sample_ms, excluded.last_sample_ms),
                    peak_time_ms=CASE
                        WHEN excluded.peak_rate > minute_rollups.peak_rate
                          OR (excluded.peak_rate = minute_rollups.peak_rate
                              AND excluded.peak_time_ms < minute_rollups.peak_time_ms)
                        THEN excluded.peak_time_ms ELSE minute_rollups.peak_time_ms END,
                    peak_rate=max(minute_rollups.peak_rate, excluded.peak_rate)
                WHERE minute_rollups.sample_count < (
                    SELECT count(*) FROM samples s
                    WHERE s.instance_id=excluded.instance_id
                      AND (s.sample_time_ms / 60000) * 60000=excluded.minute_ms
                )
                """,
                (minute_ms, peak_rate, instance_id, sample_time_ms, sequence),
            )
            self._db.execute(
                """
                INSERT INTO minute_app_rollups(minute_ms, instance_id, app_name, rx_bytes, tx_bytes)
                SELECT ?, instance_id, app_name, rx_bytes, tx_bytes
                FROM app_samples
                WHERE instance_id=? AND sample_time_ms=? AND sequence=?
                ORDER BY rowid
                ON CONFLICT(minute_ms, instance_id, app_name) DO UPDATE SET
                    rx_bytes=minute_app_rollups.rx_bytes + excluded.rx_bytes,
                    tx_bytes=minute_app_rollups.tx_bytes + excluded.tx_bytes
                WHERE (SELECT sample_count FROM minute_rollups m
                       WHERE m.minute_ms=excluded.minute_ms AND m.instance_id=excluded.instance_id) =
                      (SELECT count(*) FROM samples s
                       WHERE s.instance_id=excluded.instance_id
                         AND (s.sample_time_ms / 60000) * 60000=excluded.minute_ms)
                """,
                (minute_ms, instance_id, sample_time_ms, sequence),
            )
        return True

    def _ingest_runtime_event(self, log_entry: Dict[str, Any], payload: Dict[str, Any]) -> bool:
        if payload.get("schema_version") != 1:
            return False
        kind = str(payload.get("kind") or "")
        event = str(payload.get("event") or "")
        if kind == "honeygain_event" and event not in HONEYGAIN_EVENTS:
            return False
        if kind == "bandwidth_monitor_status" and event not in ("start", "exit"):
            return False
        labels = ((log_entry.get("resource") or {}).get("labels") or {})
        event_time_ms = parse_time_ms(str(payload.get("time") or log_entry.get("time")))
        receive_time_ms = parse_time_ms(str(log_entry.get("receive_time") or log_entry.get("time") or payload.get("time")))
        row = (
            str(labels.get("instance_id") or "unknown"),
            event_time_ms,
            receive_time_ms,
            str(labels.get("machine_id") or "unknown"),
            str(labels.get("organization_name") or ""),
            str(labels.get("project_name") or ""),
            str(labels.get("container_group_name") or ""),
            str(labels.get("container_group_version") or ""),
            kind,
            event,
            str(payload.get("client_version") or ""),
            int(payload.get("attempt") or 0),
            int(payload.get("exit_code") or 0),
            str(payload.get("severity") or log_entry.get("severity") or "default"),
        )
        with self._lock, self._db:
            if kind == "honeygain_event" and event == "disconnected":
                # New monitors emit both the native line and a structured
                # transition. Prefer that transition over the legacy fallback.
                second_start = (event_time_ms // 1000) * 1000
                same_event = (row[0], second_start, second_start + 1000)
                if row[10]:
                    self._db.execute(
                        "DELETE FROM runtime_events WHERE instance_id=? AND event_time_ms>=? AND event_time_ms<? "
                        "AND kind='honeygain_event' AND event='disconnected' AND client_version=''",
                        same_event,
                    )
                elif self._db.execute(
                    "SELECT 1 FROM runtime_events WHERE instance_id=? AND event_time_ms>=? AND event_time_ms<? "
                    "AND kind='honeygain_event' AND event='disconnected' AND client_version!='' LIMIT 1",
                    same_event,
                ).fetchone():
                    return False
            cursor = self._db.execute(
                """
                INSERT OR IGNORE INTO runtime_events(
                    instance_id, event_time_ms, receive_time_ms, machine_id,
                    organization_name, project_name, container_group_name,
                    container_group_version, kind, event, client_version,
                    attempt, exit_code, severity
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                row,
            )
            return cursor.rowcount > 0

    def ingest_many(self, entries: Iterable[Dict[str, Any]]) -> int:
        return sum(1 for entry in entries if self.ingest(entry))

    def query_events(self, start_ms: int, end_ms: int, limit: int = 1000) -> Dict[str, Any]:
        if end_ms <= start_ms:
            raise ValueError("end must be after start")
        if limit < 1 or limit > 10_000:
            raise ValueError("limit must be between 1 and 10000")
        with self._lock:
            rows = self._db.execute(
                """
                SELECT instance_id, event_time_ms, receive_time_ms, machine_id,
                       organization_name, project_name, container_group_name,
                       container_group_version, kind, event, client_version,
                       attempt, exit_code, severity
                FROM runtime_events
                WHERE event_time_ms >= ? AND event_time_ms < ?
                ORDER BY event_time_ms DESC, instance_id DESC
                LIMIT ?
                """,
                (start_ms, end_ms, limit),
            ).fetchall()
        events = [dict(row) for row in rows]
        counts: Dict[str, int] = {}
        for event in events:
            counts[event["event"]] = counts.get(event["event"], 0) + 1
        return {
            "schema_version": 1,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "event_count": len(events),
            "counts": counts,
            "events": events,
        }

    def watermark_ms(self) -> Optional[int]:
        with self._lock:
            row = self._db.execute("SELECT watermark_ms FROM ingest_state WHERE singleton=1").fetchone()
        return row[0] if row and row[0] is not None else None

    def record_poll(self, watermark_ms: int, new_samples: int, error: str = "", now_ms: Optional[int] = None) -> None:
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        success = None if error else now_ms
        safe_error = error[:500]
        with self._lock, self._db:
            self._db.execute(
                """
                UPDATE ingest_state SET watermark_ms=?, last_poll_ms=?,
                    last_success_ms=coalesce(?, last_success_ms), last_new_samples=?, last_error=?
                WHERE singleton=1
                """,
                (watermark_ms, now_ms, success, new_samples, safe_error),
            )

    def rollup(self, before_ms: Optional[int] = None) -> int:
        if before_ms is None:
            before_ms = (int(time.time() * 1000) // MINUTE_MS) * MINUTE_MS
        with self._lock, self._db:
            count = int(self._db.execute("SELECT count(*) FROM samples WHERE sample_time_ms < ?", (before_ms,)).fetchone()[0])
            self._db.execute(
                """
                INSERT INTO minute_rollups
                    (minute_ms, instance_id, machine_id, sample_count, eth_rx_bytes, eth_tx_bytes,
                     captured_rx_bytes, captured_tx_bytes, unassigned_rx_bytes, unassigned_tx_bytes,
                     reset_count, partial_interval_count, eth_rx_rate_sum, eth_tx_rate_sum,
                     coverage_sum, first_sample_ms, last_sample_ms, peak_rate, peak_time_ms)
                WITH ranked AS (
                    SELECT *, (sample_time_ms / 60000) * 60000 AS minute_ms,
                           row_number() OVER (
                               PARTITION BY (sample_time_ms / 60000), instance_id
                               ORDER BY (eth_rx_rate + eth_tx_rate) DESC,
                                        sample_time_ms ASC, sequence ASC
                           ) AS peak_rank
                    FROM samples WHERE sample_time_ms < ?
                )
                SELECT minute_ms, instance_id, max(machine_id), count(*),
                       sum(eth_rx_bytes), sum(eth_tx_bytes), sum(captured_rx_bytes), sum(captured_tx_bytes),
                       sum(unassigned_rx_bytes), sum(unassigned_tx_bytes), sum(counter_reset), sum(partial_interval),
                       sum(eth_rx_rate), sum(eth_tx_rate), sum(coverage_percent),
                       min(sample_time_ms), max(sample_time_ms),
                       max(CASE WHEN peak_rank = 1 THEN eth_rx_rate + eth_tx_rate END),
                       max(CASE WHEN peak_rank = 1 THEN sample_time_ms END)
                FROM ranked
                GROUP BY minute_ms, instance_id
                ON CONFLICT(minute_ms, instance_id) DO UPDATE SET
                    machine_id=excluded.machine_id, sample_count=excluded.sample_count,
                    eth_rx_bytes=excluded.eth_rx_bytes, eth_tx_bytes=excluded.eth_tx_bytes,
                    captured_rx_bytes=excluded.captured_rx_bytes, captured_tx_bytes=excluded.captured_tx_bytes,
                    unassigned_rx_bytes=excluded.unassigned_rx_bytes, unassigned_tx_bytes=excluded.unassigned_tx_bytes,
                    reset_count=excluded.reset_count, partial_interval_count=excluded.partial_interval_count,
                    eth_rx_rate_sum=excluded.eth_rx_rate_sum, eth_tx_rate_sum=excluded.eth_tx_rate_sum,
                    coverage_sum=excluded.coverage_sum, first_sample_ms=excluded.first_sample_ms,
                    last_sample_ms=excluded.last_sample_ms, peak_rate=excluded.peak_rate,
                    peak_time_ms=excluded.peak_time_ms
                WHERE excluded.sample_count >= minute_rollups.sample_count
                """,
                (before_ms,),
            )
            self._db.execute(
                """
                INSERT INTO minute_app_rollups(minute_ms, instance_id, app_name, rx_bytes, tx_bytes)
                SELECT (sample_time_ms / 60000) * 60000, instance_id, app_name, sum(rx_bytes), sum(tx_bytes)
                FROM app_samples WHERE sample_time_ms < ?
                GROUP BY (sample_time_ms / 60000), instance_id, app_name
                ON CONFLICT(minute_ms, instance_id, app_name) DO UPDATE SET
                    rx_bytes=excluded.rx_bytes, tx_bytes=excluded.tx_bytes
                WHERE (SELECT sample_count FROM minute_rollups m
                       WHERE m.minute_ms=excluded.minute_ms AND m.instance_id=excluded.instance_id) =
                      (SELECT count(*) FROM samples s
                       WHERE s.instance_id=excluded.instance_id
                         AND (s.sample_time_ms / 60000) * 60000=excluded.minute_ms)
                """,
                (before_ms,),
            )
        return count

    def prune(self, now_ms: Optional[int] = None) -> int:
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        cutoff = now_ms - self.raw_retention_days * 86400 * 1000
        self.rollup(before_ms=cutoff)
        with self._lock, self._db:
            cursor = self._db.execute("DELETE FROM samples WHERE sample_time_ms < ?", (cutoff,))
            return max(0, cursor.rowcount)

    def _raw_rows(self, start_ms: int, end_ms: int) -> List[sqlite3.Row]:
        return self._db.execute(
            """
            SELECT * FROM samples WHERE sample_time_ms >= ? AND sample_time_ms < ?
            ORDER BY sample_time_ms, sequence
            """,
            (start_ms, end_ms),
        ).fetchall()

    def _box_stats(self, start_ms: int, end_ms: int, instance_id: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """Aggregate exact per-instance metrics from minute summaries plus uncovered raw edges."""
        rollup_start = ((start_ms + MINUTE_MS - 1) // MINUTE_MS) * MINUTE_MS
        rollup_end = (end_ms // MINUTE_MS) * MINUTE_MS
        stats: Dict[str, Dict[str, Any]] = {}

        def add(
            name: str,
            sample_count: int,
            partial_count: int,
            rx_bytes: int,
            tx_bytes: int,
            rx_rate_sum: int,
            tx_rate_sum: int,
            coverage_sum: float,
            first_ms: int,
            last_ms: int,
            peak_rate: int,
            peak_time_ms: int,
        ) -> None:
            value = stats.setdefault(
                name,
                {
                    "sample_count": 0,
                    "partial_count": 0,
                    "rx_bytes": 0,
                    "tx_bytes": 0,
                    "rx_rate_sum": 0,
                    "tx_rate_sum": 0,
                    "coverage_sum": 0.0,
                    "first_ms": None,
                    "last_ms": None,
                    "peak_rate": 0,
                    "peak_time_ms": None,
                },
            )
            value["sample_count"] += int(sample_count)
            value["partial_count"] += int(partial_count)
            value["rx_bytes"] += int(rx_bytes)
            value["tx_bytes"] += int(tx_bytes)
            value["rx_rate_sum"] += int(rx_rate_sum)
            value["tx_rate_sum"] += int(tx_rate_sum)
            value["coverage_sum"] += float(coverage_sum)
            value["first_ms"] = first_ms if value["first_ms"] is None else min(value["first_ms"], first_ms)
            value["last_ms"] = last_ms if value["last_ms"] is None else max(value["last_ms"], last_ms)
            if peak_rate > value["peak_rate"] or (
                peak_rate == value["peak_rate"]
                and (value["peak_time_ms"] is None or peak_time_ms < value["peak_time_ms"])
            ):
                value["peak_rate"] = int(peak_rate)
                value["peak_time_ms"] = int(peak_time_ms)

        rollup_sql = """
            SELECT instance_id, sample_count, partial_interval_count, eth_rx_bytes, eth_tx_bytes,
                   eth_rx_rate_sum, eth_tx_rate_sum, coverage_sum, first_sample_ms, last_sample_ms,
                   peak_rate, peak_time_ms
            FROM minute_rollups
            WHERE minute_ms >= ? AND minute_ms < ?
        """
        rollup_params: List[Any] = [rollup_start, rollup_end]
        if instance_id is not None:
            rollup_sql += " AND instance_id=?"
            rollup_params.append(instance_id)
        for row in self._db.execute(rollup_sql, rollup_params).fetchall():
            add(
                row["instance_id"], row["sample_count"], row["partial_interval_count"],
                row["eth_rx_bytes"], row["eth_tx_bytes"], row["eth_rx_rate_sum"],
                row["eth_tx_rate_sum"], row["coverage_sum"], row["first_sample_ms"],
                row["last_sample_ms"], row["peak_rate"], row["peak_time_ms"],
            )

        raw_sql = """
            SELECT s.instance_id, s.sample_time_ms, s.sequence, s.partial_interval,
                   s.eth_rx_bytes, s.eth_tx_bytes, s.eth_rx_rate, s.eth_tx_rate,
                   s.coverage_percent
            FROM samples s
            WHERE s.sample_time_ms >= ? AND s.sample_time_ms < ?
              AND NOT EXISTS (
                  SELECT 1 FROM minute_rollups m
                  WHERE m.instance_id=s.instance_id
                    AND m.minute_ms=(s.sample_time_ms / 60000) * 60000
                    AND m.minute_ms >= ? AND m.minute_ms < ?
              )
        """
        raw_params: List[Any] = [start_ms, end_ms, rollup_start, rollup_end]
        if instance_id is not None:
            raw_sql += " AND s.instance_id=?"
            raw_params.append(instance_id)
        for row in self._db.execute(raw_sql, raw_params).fetchall():
            rate = int(row["eth_rx_rate"]) + int(row["eth_tx_rate"])
            add(
                row["instance_id"], 1, row["partial_interval"], row["eth_rx_bytes"],
                row["eth_tx_bytes"], row["eth_rx_rate"], row["eth_tx_rate"],
                row["coverage_percent"], row["sample_time_ms"], row["sample_time_ms"],
                rate, row["sample_time_ms"],
            )
        return stats

    def _box_app_totals(self, start_ms: int, end_ms: int) -> Dict[str, Tuple[int, int]]:
        rollup_start = ((start_ms + MINUTE_MS - 1) // MINUTE_MS) * MINUTE_MS
        rollup_end = (end_ms // MINUTE_MS) * MINUTE_MS
        totals: Dict[str, List[int]] = {}
        for row in self._db.execute(
            """
            SELECT instance_id, sum(rx_bytes) AS rx_bytes, sum(tx_bytes) AS tx_bytes
            FROM minute_app_rollups
            WHERE app_name='honeygain' AND minute_ms >= ? AND minute_ms < ?
            GROUP BY instance_id
            """,
            (rollup_start, rollup_end),
        ).fetchall():
            totals[row["instance_id"]] = [int(row["rx_bytes"] or 0), int(row["tx_bytes"] or 0)]
        for row in self._db.execute(
            """
            SELECT a.instance_id, sum(a.rx_bytes) AS rx_bytes, sum(a.tx_bytes) AS tx_bytes
            FROM app_samples a
            WHERE a.app_name='honeygain' AND a.sample_time_ms >= ? AND a.sample_time_ms < ?
              AND NOT EXISTS (
                  SELECT 1 FROM minute_rollups m
                  WHERE m.instance_id=a.instance_id
                    AND m.minute_ms=(a.sample_time_ms / 60000) * 60000
                    AND m.minute_ms >= ? AND m.minute_ms < ?
              )
            GROUP BY a.instance_id
            """,
            (start_ms, end_ms, rollup_start, rollup_end),
        ).fetchall():
            total = totals.setdefault(row["instance_id"], [0, 0])
            total[0] += int(row["rx_bytes"] or 0)
            total[1] += int(row["tx_bytes"] or 0)
        return {name: (value[0], value[1]) for name, value in totals.items()}

    def query_boxes(self, start_ms: int, end_ms: int, expected_reporters: int = 10) -> Dict[str, Any]:
        if end_ms <= start_ms:
            raise ValueError("end must be after start")
        with self._lock:
            # A snapshot older than the live threshold cannot contribute to
            # current fleet values, so keep this exact-second search bounded.
            snapshot_start_ms = max(start_ms, end_ms - 60_000)
            fleet = self._db.execute(
                """
                SELECT (sample_time_ms / 1000) * 1000 AS sample_time_ms,
                       count(DISTINCT instance_id) AS reporters
                FROM samples
                WHERE sample_time_ms >= ? AND sample_time_ms < ?
                GROUP BY (sample_time_ms / 1000)
                HAVING count(DISTINCT instance_id) >= ?
                ORDER BY sample_time_ms DESC LIMIT 1
                """,
                (snapshot_start_ms, end_ms, expected_reporters),
            ).fetchone()
            if fleet is None:
                fleet = self._db.execute(
                    """
                    SELECT (sample_time_ms / 1000) * 1000 AS sample_time_ms,
                           count(DISTINCT instance_id) AS reporters
                    FROM samples
                    WHERE (sample_time_ms / 1000) = (
                        SELECT max(sample_time_ms / 1000) FROM samples
                        WHERE sample_time_ms >= ? AND sample_time_ms < ?
                    )
                    GROUP BY (sample_time_ms / 1000)
                    """,
                    (snapshot_start_ms, end_ms),
                ).fetchone()
            fleet_snapshot_ms = fleet["sample_time_ms"] if fleet else None
            snapshot_rows: Dict[str, sqlite3.Row] = {}
            if fleet_snapshot_ms is not None:
                for snapshot in self._db.execute(
                    """
                    SELECT instance_id, eth_rx_rate AS eth_rx_bytes, eth_tx_rate AS eth_tx_bytes, sequence
                    FROM samples WHERE sample_time_ms>=? AND sample_time_ms<?
                    ORDER BY instance_id, sample_time_ms DESC, sequence DESC
                    """,
                    (fleet_snapshot_ms, fleet_snapshot_ms + 1000),
                ).fetchall():
                    snapshot_rows.setdefault(snapshot["instance_id"], snapshot)

            selected_stats = self._box_stats(start_ms, end_ms)
            app_totals = self._box_app_totals(start_ms, end_ms)

            def average_rate(value: Optional[Dict[str, Any]]) -> float:
                if not value or not value["sample_count"]:
                    return 0.0
                return float(value["rx_rate_sum"] + value["tx_rate_sum"]) / value["sample_count"]

            boxes = []
            for instance_id, stats in sorted(
                selected_stats.items(), key=lambda item: (-int(item[1]["last_ms"]), item[0])
            ):
                latest = self._db.execute(
                    """
                    SELECT s.*, seg.first_seen_ms AS segment_first_seen_ms,
                           coalesce(nullif(s.public_ip, ''), seg.public_ip, '') AS effective_public_ip,
                           a.rx_bytes AS honeygain_rx_bytes, a.tx_bytes AS honeygain_tx_bytes,
                           a.process_count AS honeygain_process_count,
                           a.socket_count AS honeygain_socket_count,
                           a.tcp_socket_count AS honeygain_tcp_socket_count,
                           a.tcp_established_count AS honeygain_tcp_established_count,
                           a.udp_socket_count AS honeygain_udp_socket_count,
                           a.state AS honeygain_state
                    FROM samples s
                    LEFT JOIN instance_segments seg ON seg.instance_id=s.instance_id
                    LEFT JOIN app_samples a
                      ON a.instance_id=s.instance_id AND a.sample_time_ms=s.sample_time_ms
                     AND a.sequence=s.sequence AND a.app_name='honeygain'
                    WHERE s.instance_id=? AND s.sample_time_ms>=? AND s.sample_time_ms<?
                    ORDER BY s.sample_time_ms DESC, s.sequence DESC LIMIT 1
                    """,
                    (instance_id, start_ms, end_ms),
                ).fetchone()
                segment = self._db.execute(
                    "SELECT machine_id, first_seen_ms, public_ip FROM instance_segments WHERE instance_id=?",
                    (instance_id,),
                ).fetchone()
                first_seen_ms = int(segment["first_seen_ms"] if segment else stats["first_ms"])
                profile_end_ms = int(stats["last_ms"])
                first_profile = self._box_stats(first_seen_ms, first_seen_ms + 300_000, instance_id).get(instance_id)
                last_profile = self._box_stats(max(0, profile_end_ms - 300_000), profile_end_ms + 1, instance_id).get(instance_id)

                first_average = average_rate(first_profile)
                last_average = average_rate(last_profile)
                sample_count = int(stats["sample_count"])
                average_rx = float(stats["rx_rate_sum"]) / sample_count
                average_tx = float(stats["tx_rate_sum"]) / sample_count
                latest_rx = int(latest["eth_rx_rate"] if latest else 0)
                latest_tx = int(latest["eth_tx_rate"] if latest else 0)
                app_rx, app_tx = app_totals.get(instance_id, (0, 0))
                snapshot = snapshot_rows.get(instance_id)
                snapshot_value = None
                if snapshot:
                    snapshot_rx = int(snapshot["eth_rx_bytes"] or 0)
                    snapshot_tx = int(snapshot["eth_tx_bytes"] or 0)
                    snapshot_value = {
                        "time_ms": fleet_snapshot_ms,
                        "rx_bytes_per_second": snapshot_rx,
                        "tx_bytes_per_second": snapshot_tx,
                        "bytes_per_second": snapshot_rx + snapshot_tx,
                    }
                expected = max(1, (int(stats["last_ms"]) - int(stats["first_ms"])) // 1000 + 1)
                box = {
                    "instance_id": instance_id,
                    "machine_id": latest["machine_id"] if latest else (segment["machine_id"] if segment else "unknown"),
                    "public_ip": latest["effective_public_ip"] if latest else (segment["public_ip"] if segment else ""),
                    "public_ip_status": latest["public_ip_status"] if latest else "unknown",
                    "capture_mode": latest["capture_mode"] if latest else "unknown",
                    "source": {
                        "organization": latest["organization_name"] if latest else "",
                        "project": latest["project_name"] if latest else "",
                        "container_group": latest["container_group_name"] if latest else "",
                        "version": latest["container_group_version"] if latest else "",
                    },
                    "first_seen_ms": first_seen_ms,
                    "first_sample_ms": int(stats["first_ms"]),
                    "last_sample_ms": int(stats["last_ms"]),
                    "sample_count": sample_count,
                    "partial_interval_count": int(stats["partial_count"]),
                    "expected_sample_count": expected,
                    "missing_sample_count": max(0, expected - sample_count),
                    "completeness_percent": min(100.0, 100.0 * sample_count / expected),
                    "is_live": snapshot_value is not None and end_ms - fleet_snapshot_ms <= LIVE_SAMPLE_GRACE_MS,
                    "snapshot": snapshot_value,
                    "totals": {"rx_bytes": int(stats["rx_bytes"]), "tx_bytes": int(stats["tx_bytes"])},
                    "observed_average": {
                        "rx_bytes_per_second": average_rx,
                        "tx_bytes_per_second": average_tx,
                        "bytes_per_second": average_rx + average_tx,
                    },
                    "peak": {"bytes_per_second": int(stats["peak_rate"]), "time_ms": stats["peak_time_ms"]},
                    "latest": {
                        "rx_bytes_per_second": latest_rx,
                        "tx_bytes_per_second": latest_tx,
                        "bytes_per_second": latest_rx + latest_tx,
                    },
                    "first_5m_average_bytes_per_second": first_average,
                    "last_5m_average_bytes_per_second": last_average,
                    "slowdown_ratio": (last_average / first_average) if first_average > 0 else None,
                    "average_coverage_percent": float(stats["coverage_sum"]) / sample_count,
                    "honeygain": {
                        "state": latest["honeygain_state"] if latest and latest["honeygain_state"] else "unknown",
                        "process_count": int(latest["honeygain_process_count"] or 0) if latest else 0,
                        "socket_count": int(latest["honeygain_socket_count"] or 0) if latest else 0,
                        "tcp_socket_count": int(latest["honeygain_tcp_socket_count"] or 0) if latest else 0,
                        "tcp_established_count": int(latest["honeygain_tcp_established_count"] or 0) if latest else 0,
                        "udp_socket_count": int(latest["honeygain_udp_socket_count"] or 0) if latest else 0,
                        "rx_bytes": app_rx,
                        "tx_bytes": app_tx,
                    },
                }
                box.update(
                    {
                        "last_ms": box["last_sample_ms"], "samples": box["sample_count"],
                        "rx_bytes": box["totals"]["rx_bytes"], "tx_bytes": box["totals"]["tx_bytes"],
                        "now_rx": box["latest"]["rx_bytes_per_second"],
                        "now_tx": box["latest"]["tx_bytes_per_second"],
                        "honeygain_rx": box["honeygain"]["rx_bytes"],
                        "honeygain_tx": box["honeygain"]["tx_bytes"],
                        "honeygain_sockets": box["honeygain"]["socket_count"],
                        "honeygain_state": box["honeygain"]["state"],
                    }
                )
                boxes.append(box)
        return {
            "schema_version": 2,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "expected_reporter_count": expected_reporters,
            "fleet_snapshot_ms": fleet_snapshot_ms,
            "snapshot_reporter_count": len(snapshot_rows),
            "reporter_count": len(snapshot_rows),
            "boxes": boxes,
        }

    def query_range(self, start_ms: int, end_ms: int, bucket_seconds: int) -> Dict[str, Any]:
        if end_ms <= start_ms:
            raise ValueError("end must be after start")
        if bucket_seconds < 1 or bucket_seconds > 86400:
            raise ValueError("bucket must be between 1 and 86400 seconds")
        bucket_ms = bucket_seconds * 1000
        with self._lock:
            use_rollups = bucket_seconds >= 60 and start_ms % MINUTE_MS == 0 and bucket_ms % MINUTE_MS == 0
            rollup_start = start_ms if use_rollups else end_ms + MINUTE_MS
            rollups = []
            if use_rollups:
                rollups = self._db.execute(
                    """
                    SELECT * FROM minute_rollups
                    WHERE minute_ms >= ? AND minute_ms + 60000 <= ? ORDER BY minute_ms
                    """,
                    (rollup_start, end_ms),
                ).fetchall()
            raw_filter = """
                WHERE s.sample_time_ms >= ? AND s.sample_time_ms < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM minute_rollups m
                      WHERE m.instance_id=s.instance_id
                        AND m.minute_ms=(s.sample_time_ms / 60000) * 60000
                        AND m.minute_ms >= ? AND m.minute_ms + 60000 <= ?
                  )
                """
            raw_params = (start_ms, end_ms, rollup_start, end_ms)
            raw_rows = self._db.execute(
                "SELECT s.* FROM samples s" + raw_filter + " ORDER BY s.sample_time_ms, s.sequence",
                raw_params,
            ).fetchall()
            raw_apps = self._db.execute(
                """
                SELECT s.instance_id, s.sample_time_ms, s.sequence, s.app_name, s.rx_bytes, s.tx_bytes
                FROM app_samples s
                """ + raw_filter,
                raw_params,
            ).fetchall()
            rollup_apps = []
            if rollups:
                rollup_apps = self._db.execute(
                    """
                    SELECT * FROM minute_app_rollups
                    WHERE minute_ms >= ? AND minute_ms + 60000 <= ?
                    """,
                    (rollup_start, end_ms),
                ).fetchall()
            segments = self._db.execute(
                """
                SELECT instance_id, machine_id, first_seen_ms, last_seen_ms, public_ip
                FROM instance_segments WHERE last_seen_ms >= ? AND first_seen_ms < ? ORDER BY first_seen_ms
                """,
                (start_ms, end_ms),
            ).fetchall()
            latest = self._db.execute(
                """
                SELECT instance_id, sample_time_ms, sequence FROM samples
                WHERE sample_time_ms >= ? AND sample_time_ms < ?
                ORDER BY sample_time_ms DESC, sequence DESC LIMIT 1
                """,
                (start_ms, end_ms),
            ).fetchone()
            latest_apps = []
            if latest:
                latest_apps = self._db.execute(
                    """
                    SELECT app_name, process_count, socket_count, state FROM app_samples
                    WHERE instance_id=? AND sample_time_ms=? AND sequence=? ORDER BY app_name
                    """,
                    (latest["instance_id"], latest["sample_time_ms"], latest["sequence"]),
                ).fetchall()

        raw_app_map: Dict[Tuple[str, int, int], Dict[str, Tuple[int, int]]] = {}
        for row in raw_apps:
            key = (row["instance_id"], row["sample_time_ms"], row["sequence"])
            raw_app_map.setdefault(key, {})[row["app_name"]] = (row["rx_bytes"], row["tx_bytes"])
        rollup_app_map: Dict[Tuple[int, str], Dict[str, Tuple[int, int]]] = {}
        for row in rollup_apps:
            rollup_app_map.setdefault((row["minute_ms"], row["instance_id"]), {})[row["app_name"]] = (
                row["rx_bytes"], row["tx_bytes"]
            )

        buckets: Dict[int, Dict[str, Any]] = {}
        for rows, app_map, rolled_up in ((rollups, rollup_app_map, True), (raw_rows, raw_app_map, False)):
            for row in rows:
                timestamp_ms = row["minute_ms"] if rolled_up else row["sample_time_ms"]
                key = (timestamp_ms, row["instance_id"]) if rolled_up else (row["instance_id"], timestamp_ms, row["sequence"])
                bucket = start_ms + ((timestamp_ms - start_ms) // bucket_ms) * bucket_ms
                point = buckets.get(bucket)
                if point is None:
                    point = buckets[bucket] = {
                        "sample_count": 0,
                        "eth_rx": 0,
                        "eth_tx": 0,
                        "captured_rx": 0,
                        "captured_tx": 0,
                        "unassigned_rx": 0,
                        "unassigned_tx": 0,
                        "reset_count": 0,
                        "partial_count": 0,
                        "instances": set(),
                        "apps": {name: [0, 0] for name in APP_NAMES},
                    }
                point["sample_count"] += row["sample_count"] if rolled_up else 1
                point["reset_count"] += row["reset_count"] if rolled_up else row["counter_reset"]
                point["partial_count"] += row["partial_interval_count"] if rolled_up else row["partial_interval"]
                for field in ("eth_rx", "eth_tx", "captured_rx", "captured_tx", "unassigned_rx", "unassigned_tx"):
                    point[field] += row[field + "_bytes"]
                point["instances"].add(row["instance_id"])
                for name, (rx, tx) in app_map.get(key, {}).items():
                    if name in point["apps"]:
                        point["apps"][name][0] += rx
                        point["apps"][name][1] += tx

        totals = {
            "eth0": {"rx_bytes": 0, "tx_bytes": 0},
            "captured": {"rx_bytes": 0, "tx_bytes": 0},
            "unassigned": {"rx_bytes": 0, "tx_bytes": 0},
            "applications": {name: {"rx_bytes": 0, "tx_bytes": 0} for name in APP_NAMES},
        }

        def add_totals(rx: int, tx: int, total: Dict[str, int]) -> Dict[str, Any]:
            total["rx_bytes"] += rx
            total["tx_bytes"] += tx
            return {
                "rx_bytes": rx,
                "tx_bytes": tx,
                "rx_bytes_per_second": rx / bucket_seconds,
                "tx_bytes_per_second": tx / bucket_seconds,
            }

        points = []
        for bucket in sorted(buckets):
            value = buckets[bucket]
            captured_total = value["captured_rx"] + value["captured_tx"]
            unassigned_total = value["unassigned_rx"] + value["unassigned_tx"]
            coverage = 0.0 if captured_total == 0 else max(0.0, 100.0 * (captured_total - min(captured_total, unassigned_total)) / captured_total)
            applications = {
                name: add_totals(*value["apps"][name], totals["applications"][name])
                for name in APP_NAMES
            }
            points.append(
                {
                    "time_ms": bucket,
                    "sample_count": value["sample_count"],
                    "instance_ids": sorted(value["instances"]),
                    "counter_resets": value["reset_count"],
                    "partial_intervals": value["partial_count"],
                    "coverage_percent": coverage,
                    "eth0": add_totals(value["eth_rx"], value["eth_tx"], totals["eth0"]),
                    "captured": add_totals(value["captured_rx"], value["captured_tx"], totals["captured"]),
                    "unassigned": add_totals(value["unassigned_rx"], value["unassigned_tx"], totals["unassigned"]),
                    "applications": applications,
                }
            )

        return {
            "schema_version": 1,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "bucket_seconds": bucket_seconds,
            "points": points,
            "totals": totals,
            "latest_apps": {r["app_name"]: {"process_count": r["process_count"], "socket_count": r["socket_count"], "state": r["state"]} for r in latest_apps},
            "reallocations": [dict(row) for row in segments],
        }

    def status(self) -> Dict[str, Any]:
        with self._lock:
            aggregate = self._db.execute(
                "SELECT count(*) AS count, min(sample_time_ms) AS first_ms, max(sample_time_ms) AS last_ms FROM samples"
            ).fetchone()
            latest = self._db.execute(
                """
                SELECT instance_id, machine_id, sample_time_ms, receive_time_ms, coverage_percent,
                       eth_rx_rate, eth_tx_rate, public_ip_status, organization_name,
                       project_name, container_group_name, container_group_version,
                       capture_mode, interval_milliseconds, partial_interval
                FROM samples ORDER BY sample_time_ms DESC, sequence DESC LIMIT 1
                """
            ).fetchone()
            state = self._db.execute("SELECT * FROM ingest_state WHERE singleton=1").fetchone()
            segment_count = self._db.execute("SELECT count(*) FROM instance_segments").fetchone()[0]
        result: Dict[str, Any] = {
            "sample_count": int(aggregate["count"]),
            "first_sample_ms": aggregate["first_ms"],
            "last_sample_ms": aggregate["last_ms"],
            "segment_count": int(segment_count),
            "database_path": str(self.path),
            "database_size_bytes": self.path.stat().st_size if self.path.exists() else 0,
            "watermark_ms": state["watermark_ms"],
            "last_poll_ms": state["last_poll_ms"],
            "last_success_ms": state["last_success_ms"],
            "last_new_samples": state["last_new_samples"],
            "last_error": state["last_error"],
        }
        if latest:
            result.update(
                {
                    "latest_instance_id": latest["instance_id"],
                    "latest_machine_id": latest["machine_id"],
                    "latest_receive_time_ms": latest["receive_time_ms"],
                    "latest_coverage_percent": latest["coverage_percent"],
                    "latest_rx_bytes_per_second": latest["eth_rx_rate"],
                    "latest_tx_bytes_per_second": latest["eth_tx_rate"],
                    "public_ip_status": latest["public_ip_status"],
                    "capture_mode": latest["capture_mode"],
                    "latest_interval_milliseconds": latest["interval_milliseconds"],
                    "latest_partial_interval": bool(latest["partial_interval"]),
                    "source": {
                        "organization": latest["organization_name"],
                        "project": latest["project_name"],
                        "container_group": latest["container_group_name"],
                        "version": latest["container_group_version"],
                    },
                }
            )
        return result


def compact_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)
