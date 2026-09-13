from contextlib import closing
"""Exact per-container minute records, durable storage, and dashboard summaries."""
from datetime import datetime
import ipaddress
from pathlib import Path
import re
import sqlite3
import threading
import time

UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
FIELDS = ("group_id", "instance", "machine", "observer", "seq", "start_ms", "end_ms",
          "rx", "tx", "total_rx", "total_tx", "ip", "state", "error", "final", "valid",
          "received_ms", "record_bytes", "bucket_ms")
ERRORS = {"communication_error": "Cannot communicate with PacketStream",
          "duplicate_ip": "PacketStream permits one client per public IP",
          "non_residential": "PacketStream rejected this IP as non-residential",
          "client_exited": "PacketStream client exited", "client_error": "PacketStream client reported an error; replacing this machine",
          "connection_timeout": "PacketStream did not connect within 60 seconds"}


def instance_run(value):
    parts = value.split("/") if isinstance(value, str) else []
    return "/".join(parts[:2]) if len(parts) >= 2 and all(UUID.fullmatch(p) for p in parts[:2]) else None


def stamp(value):
    if not value:
        return None
    value = re.sub(r"\.(\d+)(?=[+-])", lambda m: "." + m.group(1)[:6].ljust(6, "0"), value.replace("Z", "+00:00"))
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def parse_minute(item, group_id, received_ms=None):
    line = item.get("text_log", "")
    if not line.startswith(("PS_MINUTE_V1 ", "PS_MINUTE_V2 ")):
        return None
    try:
        values = dict(part.split("=", 1) for part in line.split()[1:])
        if not all(UUID.fullmatch(values[k]) for k in ("instance", "machine")):
            return None
        numbers = {key: int(values[key]) for key in
                   ("observer", "seq", "start_ms", "end_ms", "rx", "tx", "total_rx", "total_tx", "final")}
        if any(abs(v) > 2**53 - 1 for v in numbers.values()):
            return None
        if not (0 < numbers["observer"] <= numbers["start_ms"] <= numbers["end_ms"]
                and numbers["seq"] >= 0 and numbers["final"] in (0, 1)):
            return None
        if values["state"] not in ("starting", "running", "error", "exited", "reallocating"):
            return None
        ip = str(ipaddress.ip_address(values["ip"])) if values["ip"] != "-" else None
        result = dict(numbers, group_id=group_id, instance=values["instance"], machine=values["machine"],
                      ip=ip, state=values["state"], error=values.get("error", "none"),
                      valid=int(all(numbers[k] >= 0 for k in ("rx", "tx", "total_rx", "total_tx"))),
                      received_ms=received_ms or int(time.time() * 1000),
                      record_bytes=len(line.encode("utf-8")) + 1,
                      bucket_ms=int(values["bucket_ms"]) if "bucket_ms" in values else None)
        if result["bucket_ms"] is not None and (result["bucket_ms"] % 60000 or result["bucket_ms"] < 0):
            return None
        bucket = result["bucket_ms"]
        if bucket is not None and not bucket <= numbers["start_ms"] <= numbers["end_ms"] <= bucket + 60000:
            result["bucket_ms"] = None
        return result
    except (ValueError, KeyError, TypeError):
        return None


class NetworkMinutes:
    def __init__(self, path, read_only=False):
        self.path = Path(path)
        self._chart_cache = None
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        target = self.path.resolve().as_uri() + "?mode=ro" if read_only else self.path
        self.db = sqlite3.connect(target, uri=read_only, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        if read_only:
            return
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS minutes (
                group_id TEXT, instance TEXT, machine TEXT, observer INTEGER, seq INTEGER,
                start_ms INTEGER, end_ms INTEGER, rx INTEGER, tx INTEGER,
                total_rx INTEGER, total_tx INTEGER, ip TEXT, state TEXT, error TEXT,
                final INTEGER, valid INTEGER, received_ms INTEGER, record_bytes INTEGER, bucket_ms INTEGER,
                PRIMARY KEY(group_id, instance, observer, seq));
            CREATE INDEX IF NOT EXISTS minutes_received ON minutes(group_id, received_ms);
            CREATE TABLE IF NOT EXISTS countries (ip TEXT PRIMARY KEY, country TEXT);
            CREATE TABLE IF NOT EXISTS collector (group_id TEXT PRIMARY KEY, time_ms INTEGER,
                cursor TEXT, error TEXT, inserted INTEGER);
        """)
        if "bucket_ms" not in {row[1] for row in self.db.execute("PRAGMA table_info(minutes)")}:
            self.db.execute("ALTER TABLE minutes ADD COLUMN bucket_ms INTEGER")
        self.db.commit()

    def ingest(self, records):
        inserted = 0
        statement = "INSERT OR IGNORE INTO minutes VALUES (" + ",".join("?" * len(FIELDS)) + ")"
        with self.lock, self.db:
            for row in records:
                values = tuple(row.get(k) for k in FIELDS)
                result = self.db.execute(statement, values)
                inserted += result.rowcount
                if not result.rowcount:
                    key = (row["group_id"], row["instance"], row["observer"], row["seq"])
                    old = self.db.execute(
                        "SELECT * FROM minutes WHERE group_id=? AND instance=? AND observer=? AND seq=?", key).fetchone()
                    if any(old[k] != row.get(k) for k in FIELDS if k not in ("received_ms", "record_bytes", "valid")):
                        self.db.execute("UPDATE minutes SET valid=0 WHERE group_id=? AND instance=? AND observer=? AND seq=?",
                                        key)
        return inserted

    def collector_status(self, group_id, cursor=None, error=None, inserted=0):
        with self.lock:
            if cursor is None:
                row = self.db.execute("SELECT * FROM collector WHERE group_id=?", (group_id,)).fetchone()
                return dict(row) if row else {}
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO collector VALUES (?,?,?,?,?)",
                                (group_id, int(time.time() * 1000), cursor, error, inserted))

    def latest(self, group_id):
        with self.lock:
            rows = self.db.execute("""
                SELECT m.* FROM minutes m JOIN (
                    SELECT instance, MAX(observer) observer FROM minutes WHERE group_id=? GROUP BY instance
                ) r ON m.instance=r.instance AND m.observer=r.observer
                WHERE m.group_id=? AND m.seq=(SELECT MAX(seq) FROM minutes
                    WHERE group_id=m.group_id AND instance=m.instance AND observer=m.observer)
            """, (group_id, group_id)).fetchall()
            return {row["instance"]: dict(row) for row in rows}

    def minute(self, group_id, minute_ms):
        with self.lock:
            rows=self.db.execute("""SELECT m.*, c.country FROM minutes m LEFT JOIN countries c ON m.ip=c.ip
                WHERE m.group_id=? AND m.seq>0 AND m.valid=1 AND m.end_ms>=? AND m.start_ms<?""",
                (group_id,minute_ms,minute_ms+60000)).fetchall()
        points=bucket_minutes(rows,minute_ms,minute_ms)
        return points[0] if points else {"minute_ms":minute_ms,"mb":None,"count":0,"nodes":[]}

    def snapshot(self, census, now_ms=None):
        now = now_ms or int(time.time() * 1000)
        group_id = census.get("group_id", "")
        census_at = stamp(census.get("last_success_at") or census.get("checked_at"))
        census_fresh = census_at is not None and 0 <= now - census_at < 120_000 and not census.get("poll_error")
        current = {row["instance_id"]: row for row in census.get("nodes", [])}
        desired = int(census.get("desired_replicas") or len(current))
        end = now // 60000 * 60000
        start = end - 1439 * 60000
        nodes = []
        with self.lock, self.db:
            self.db.execute("BEGIN")
            latest = self.latest(group_id)
            signature = (self.db.execute("PRAGMA data_version").fetchone()[0], self.db.total_changes, group_id, end, desired)
            if self._chart_cache is None or self._chart_cache[0] != signature:
                rows=self.db.execute("SELECT * FROM minutes WHERE group_id=? AND valid=1 AND seq>0 AND end_ms>=? AND start_ms<?",(group_id,start,end+60000)).fetchall()
                points={p["minute_ms"]:p for p in bucket_minutes(rows,start,end)}
                candles=[]
                for minute in range(start,end+1,60000):
                    point=points.get(minute) or {"minute_ms":minute,"mb":None,"count":0,"nodes":[],
                                                "estimated":False,"coverage_complete":False,"partial":True}
                    point["coverage_complete"] &= desired>0 and point["count"]>=desired
                    point["partial"] |= point["count"]<desired or minute+60000>now
                    candles.append(point)
                self._chart_cache = (signature, candles)
            candles = self._chart_cache[1]
            for instance, member in current.items():
                last = latest.get(instance)
                sample_age = now - last["end_ms"] if last else None
                sample_fresh = sample_age is not None and -10_000 <= sample_age <= 150_000
                observer = last["observer"] if last else None
                client = (member or {}).get("client_status") or {}
                client_at = stamp(client.get("source_time"))
                client_fresh = census_fresh and client_at is not None and -10_000 <= now - client_at <= 240_000
                state = client.get("state") if client_fresh else None
                error = client.get("message") if state == "error" else None
                if sample_fresh and (not client_at or last["end_ms"] >= client_at):
                    state = "stopped" if last["final"] and last["state"] == "running" else last["state"]
                    error = ERRORS.get(last["error"], last["error"]) if last["error"] != "none" else None
                salad_state = member.get("salad_state") if member else "previous"
                if not census_fresh:
                    label = "Status feed stale"
                elif state == "running":
                    label = "Running"
                elif state == "stopped":
                    label = "Stopped"
                elif state == "reallocating":
                    label = "Reallocating"
                elif state in ("error", "exited"):
                    label = "Error"
                elif salad_state != "running":
                    label = (salad_state or "starting").capitalize()
                else:
                    label = "Checking client"
                total, captured, latest_rate, first_at, last_at = None, 0, None, None, None
                gaps = False
                if last:
                    stats = self.db.execute("""
                        SELECT SUM(CASE WHEN valid=1 THEN rx+tx ELSE 0 END) recorded,
                               MIN(valid) valid, COUNT(CASE WHEN seq>0 THEN 1 END) count,
                               MIN(start_ms) first, MAX(end_ms) last, MAX(seq) seq
                        FROM minutes WHERE group_id=? AND instance=? AND observer=?
                    """, (group_id, instance, observer)).fetchone()
                    first_at, last_at = stats["first"], stats["last"]
                    gaps = stats["count"] < stats["seq"] or not stats["valid"]
                    total = (last["total_rx"] + last["total_tx"]) / 1e6 if stats["valid"] else stats["recorded"] / 1e6
                    captured = stats["count"]
                    seconds = (last["end_ms"] - last["start_ms"]) / 1000
                    if last["seq"] > 0 and last["valid"] and seconds > 0:
                        latest_rate = (last["rx"] + last["tx"]) / 1e6 * 60 / seconds
                nodes.append({"id": instance + "/" + str(observer or "pending"), "instance": instance,
                              "machine": member["machine"],
                              "current": True, "ip": last["ip"] if last else None,
                              "status": label, "salad_state": salad_state, "client_state": state, "error": error,
                              "start_ms": observer, "end_ms": last_at,
                              "elapsed_seconds": (last_at - first_at) / 1000 if first_at is not None else None,
                              "total_mb": total, "latest_mb_per_minute": latest_rate,
                              "minutes_recorded": captured, "metrics_fresh": bool(sample_fresh and not last["final"]),
                              "has_gaps": gaps, "sample_age_ms": sample_age})
            disk_minute = end - 60000
            saved = self.db.execute("""
                SELECT COUNT(*) records, COALESCE(SUM(record_bytes),0) bytes FROM minutes
                WHERE group_id=? AND received_ms>=? AND received_ms<?
            """, (group_id, disk_minute, disk_minute + 60000)).fetchone()
            totals = self.db.execute("SELECT COUNT(*) records, COALESCE(SUM(record_bytes),0) bytes, SUM(CASE WHEN valid=1 THEN rx+tx ELSE 0 END) network_bytes, MIN(start_ms) started_ms FROM minutes WHERE group_id=?", (group_id,)).fetchone()
        file_size = sum(path.stat().st_size for path in (self.path, Path(str(self.path) + "-wal")) if path.exists())
        return {"time_ms": now, "group": census.get("group"), "desired": desired, "assigned": len(current),
                "allocating": max(0, desired - len(current)), "inventory_fresh": census_fresh,
                "inventory_age_ms": now - census_at if census_at else None,
                "running": sum(n["status"] == "Running" for n in nodes),
                "errors": sum(n["status"] == "Error" for n in nodes),
                "nodes": nodes, "candles": candles, "collector": self.collector_status(group_id),
                "total_mb": totals["network_bytes"]/1e6 if totals["records"] else None, "started_ms": totals["started_ms"],
                "disk": {"minute_ms": disk_minute, "minute_bytes": saved["bytes"], "minute_records": saved["records"],
                         "total_record_bytes": totals["bytes"], "total_records": totals["records"],
                         "database_bytes": file_size,
                         "basis": "Telemetry payload bytes saved; excludes database/index overhead."}}


def saved_minutes(path, now_ms=None, group_id=None):
    """Read minute records from a provider's existing database without changing it."""
    now_ms = now_ms or int(time.time()*1000)
    first = now_ms//60000*60000 - 1439*60000
    if not Path(path).exists():
        return []
    with closing(sqlite3.connect("file:"+str(path)+"?mode=ro",uri=True)) as db:
        db.row_factory=sqlite3.Row
        query="SELECT * FROM minutes WHERE valid=1 AND seq>0 AND end_ms>=?"
        params=[first]
        if group_id is not None:
            query+=" AND group_id=?"
            params.append(group_id)
        rows=db.execute(query+" ORDER BY end_ms",params).fetchall()
    return bucket_minutes(rows,first,now_ms//60000*60000)


def bucket_minutes(rows, first, last, *, include_nodes=True):
    buckets={}
    for row in rows:
        start,end=row["start_ms"],row["end_ms"]
        amount=(row["rx"]+row["tx"])/1e6
        if end<start:
            continue
        exact=row["bucket_ms"]
        splits=[(exact,amount,False)] if exact is not None else []
        if exact is None and end==start:
            splits=[(end//60000*60000,amount,True)]
        elif exact is None:
            for minute in range(max(first,start//60000*60000),min(last,max(start,end-1)//60000*60000)+1,60000):
                overlap=min(end,minute+60000)-max(start,minute)
                if overlap>0:splits.append((minute,amount*overlap/(end-start),True))
        for minute,mb,estimated in splits:
            if not first<=minute<=last:
                continue
            bucket=buckets.setdefault(minute,{"minute_ms":minute,"mb":0,"estimated":False,"partial":False,"nodes":{}})
            bucket["mb"]+=mb;bucket["estimated"]|=estimated
            bucket["partial"]|=estimated or start!=minute or end!=minute+60000
            key=(row["instance"],row["machine"],row["ip"])
            if not include_nodes:
                bucket["nodes"][key] = None
                continue
            node=bucket["nodes"].setdefault(key,{"instance":row["instance"],"machine":row["machine"],"ip":row["ip"],"country":row["country"] if "country" in row.keys() else None,"mb":0,"coverage":[]})
            node["mb"]+=mb
            node["coverage"].append((max(start,minute),min(end,minute+60000)))
    result=[]
    for minute,bucket in sorted(buckets.items()):
        if not include_nodes:
            result.append({"minute_ms": minute, "mb": bucket["mb"], "estimated": bucket["estimated"],
                           "partial": True, "coverage_complete": False, "count": len(bucket["nodes"])})
            continue
        nodes=sorted(bucket["nodes"].values(),key=lambda n:(-n["mb"],n["instance"]))
        covered=bool(nodes)
        for node in nodes:
            through=minute
            for begin,end in sorted(node.pop("coverage")):
                if begin>through:break
                through=max(through,end)
            covered &= through>=minute+60000
        result.append(dict(bucket,count=len(nodes),nodes=nodes[:20],coverage_complete=covered))
    return result


def retained_minutes(path, service, first, last, group_id=None, test_start=0, cache=None, ip=None, identities=None):
    """Read validated saved traffic without changing recorders or inventing empty minutes."""
    path = Path(path)
    if not path.exists():
        return []
    if service in ("ps", "proxyrack", "rp", "pawns") and not group_id:
        raise ValueError("Current traffic group is unavailable")
    first = max(first, int(test_start or 0) // 60000 * 60000)
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        params = {"first": first, "end": last + 60000, "group": group_id, "test": int(test_start or 0)}
        if service == 'earnapp' and db.execute("SELECT 1 FROM sqlite_master WHERE name='minute_nodes'").fetchone():
            params['ip'] = ip
            return [dict(row) for row in db.execute("""SELECT minute_ms,SUM(bytes)/1e6 mb,
                COUNT(DISTINCT group_id||'/'||instance||'/'||observer) count,MAX(estimated) estimated,
                1 partial,0 coverage_complete FROM minute_nodes WHERE minute_ms>=:first AND minute_ms<:end
                AND (:group IS NULL OR group_id=:group) AND (:ip IS NULL OR ip=:ip)
                GROUP BY minute_ms ORDER BY minute_ms""", params)]
        scope = ""
        if ip is not None:
            params["selected_ip"] = ip
            column = "instance" if service in ("ps", "proxyrack", "pawns", "earnapp") else "run"
            values = sorted({row.get(column) for row in identities or [] if row.get(column)})
            if identities is not None and not identities:
                return []
            identities_sql = []
            if values:
                params.update({"identity" + str(i): value for i, value in enumerate(values)})
                identities_sql.append(column + " IN (" + ",".join(":identity" + str(i) for i in range(len(values))) + ")")
            # A missing run does not erase attributed traffic. Match the saved
            # partial identity exactly, within the existing group/IP boundary.
            fallback_fields = [field for field in ("instance", "machine", "group_id") if field != column
                               and (field != "group_id" or service not in ("tm", "earnfm"))]
            partials = {tuple(row.get(field) for field in fallback_fields) for row in identities or [] if not row.get(column)}
            for index, partial in enumerate(partials):
                clauses = ["NULLIF(" + column + ",'') IS NULL"]
                for field, value in zip(fallback_fields, partial):
                    name = "partial_" + str(index) + "_" + field
                    params[name] = value
                    source = "json_extract(body,'$.machine')" if service == "earnapp" and field == "machine" else field
                    clauses.append(source + " IS :" + name)
                identities_sql.append("(" + " AND ".join(clauses) + ")")
            if identities_sql:
                scope = " AND (" + " OR ".join(identities_sql) + ")"
            scope += " AND " + ("json_extract(body,'$.ip')" if service == "earnapp" else "ip") + "=:selected_ip"
        if service == 'grass' and ip is None and db.execute("SELECT 1 FROM sqlite_master WHERE name='minute_nodes'").fetchone():
            return [dict(row) for row in db.execute("""SELECT minute_ms,MAX(0,SUM(bytes))/1e6 mb,
                COUNT(DISTINCT group_id||'/'||instance||'/'||machine) count,MAX(estimated>0) estimated,
                1 partial,0 coverage_complete FROM minute_nodes WHERE minute_ms>=:first AND minute_ms<:end
                AND coverage_ms>0 AND (:group IS NULL OR group_id=:group) GROUP BY minute_ms ORDER BY minute_ms""", params)]
        if service in ("ps", "proxyrack", "pawns"):
            rows = db.execute("""SELECT * FROM minutes WHERE group_id=:group AND valid=1 AND seq>0
                AND end_ms>=:first AND start_ms<:end""" + scope, params)
        elif service == "rp":
            saved = db.execute("""SELECT start_ms,end_ms,rx_bytes rx,tx_bytes tx,instance,machine,ip,
                json_extract(body,'$.partial') partial,json_extract(body,'$.final') final FROM events
                WHERE group_id=:group AND kind='minute' AND valid=1 AND end_ms>=:first AND start_ms<:end""" + scope, params)
            def repocket_rows():
                for value in saved:
                    row = dict(value, bucket_ms=None)
                    bucket = round(row["start_ms"] / 60000) * 60000
                    if not row["partial"] and not row["final"] and 59000 <= row["end_ms"]-row["start_ms"] <= 61000 and abs(row["start_ms"]-bucket) <= 1000:
                        row.update(start_ms=bucket, end_ms=bucket+60000, bucket_ms=bucket)
                    yield row
            rows = repocket_rows()
        elif service == "earnfm":
            binding = db.execute("SELECT value FROM collector WHERE name='group_id'").fetchone()
            if binding is None or binding[0] != group_id:
                raise ValueError("EarnFM ledger does not match the current test")
            rows = db.execute("""SELECT time_ms start_ms,time_ms+60000 end_ms,rx,tx,instance,machine,ip,
                CASE WHEN estimated THEN NULL ELSE time_ms END bucket_ms FROM minute_cache
                WHERE time_ms>=:first AND time_ms<:end""" + scope, params)
        elif service == "tm" and ip is not None:
            rows = db.execute("""SELECT CAST(time_ms/60000 AS INTEGER)*60000 start_ms,
                CAST(time_ms/60000 AS INTEGER)*60000+60000 end_ms,SUM(rx) rx,SUM(tx) tx,
                instance,machine,ip,NULL bucket_ms FROM samples
                WHERE time_ms>=:first AND time_ms<:end AND start_ms>=:test
                AND rx IS NOT NULL AND tx IS NOT NULL""" + scope + " GROUP BY 1,run,instance,machine,ip", params)
        elif service == "tm":
            # TM inserts immutable traffic amounts; duplicate updates only fill receipt clocks.
            cache = cache if cache is not None else {}
            high = db.execute("SELECT COALESCE(MAX(rowid),0) FROM samples").fetchone()[0]
            if high < cache.get("cursor", 0):
                cache.clear()
            saved = cache.setdefault("rows", {})
            params.update(cursor=cache.get("cursor", 0), high=high)
            added = db.execute("""SELECT CAST(time_ms/60000 AS INTEGER)*60000 minute,
                SUM(rx) rx,SUM(tx) tx,instance,machine,ip,run FROM samples NOT INDEXED
                WHERE rowid>:cursor AND rowid<=:high AND time_ms>=:first
                AND start_ms>=:test AND rx IS NOT NULL AND tx IS NOT NULL
                GROUP BY CAST(time_ms/60000 AS INTEGER),run,instance,machine,ip""", params)
            for row in added:
                key = row["minute"], row["run"], row["instance"], row["machine"], row["ip"]
                item = saved.setdefault(key, {"start_ms": row["minute"], "end_ms": row["minute"]+60000,
                    "instance": row["instance"], "machine": row["machine"], "ip": row["ip"], "rx": 0, "tx": 0, "bucket_ms": None})
                item["rx"] += row["rx"]
                item["tx"] += row["tx"]
            cache["cursor"] = high
            for key in list(saved):
                if key[0] < first:
                    del saved[key]
            rows = saved.values()
        elif service == "earnapp":
            rows = db.execute("""SELECT start_ms,end_ms,instance,json_extract(body,'$.machine') machine,
                json_extract(body,'$.ip') ip,json_extract(body,'$.rx') rx,json_extract(body,'$.tx') tx,NULL bucket_ms
                FROM samples WHERE (:group IS NULL OR group_id=:group) AND seq>0 AND COALESCE(json_extract(body,'$.valid'),1)=1
                AND end_ms>start_ms AND end_ms>=:first AND start_ms<:end""" + scope, params)
        elif service == "grass":
            rows = db.execute("""SELECT start_ms,source_ms end_ms,instance,machine,ip,rx,tx,NULL bucket_ms
                FROM intervals WHERE valid=1 AND duration_ms<=90000 AND duration_ms>0
                AND source_ms>=:first AND start_ms<:end""" + scope, params)
        else:
            raise ValueError("Unknown native traffic service")
        return bucket_minutes(rows, first, last, include_nodes=False)
