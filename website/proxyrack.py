#!/usr/bin/env python3
"""Deploy and record one PR group with ten self-monitoring Salad replicas."""
import argparse
import base64
from datetime import datetime, timedelta, timezone
import fcntl
import getpass
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time

from dashboard.native_network import NetworkMinutes, parse_minute, bucket_minutes, saved_minutes

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("PR_DATA_DIR", str(Path.home() / ".hg1/proxyrack")))
HELPER = Path(os.environ.get("PR_KEYCHAIN_HELPER", str(ROOT / ".runtime/keychain-store")))
BASE = "https://api.salad.com/api/public/organizations/ssh1"
GROUPS = "/projects/default/containers"
IMAGE = "docker.io/proxyrack/pop@sha256:7de6719f37b31d5dd354f8cdd3ae601b4ed401832d0935ff460f968e0b6731bc"
UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")


def save(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2))
    os.replace(temp, path)


def salad_api():
    key = subprocess.check_output([str(HELPER), "--read", "--service", "salad-bandwidth-api",
                                   "--account", getpass.getuser()], text=True).strip()

    def request(path, method="GET", body=None):
        if not (path == GROUPS or path.startswith(GROUPS + "/") or path == "/log-entries"):
            raise ValueError("Outside the requested SSH1 deployment")
        media = "application/merge-patch+json" if method == "PATCH" else "application/json"
        config = "header = " + json.dumps("Salad-Api-Key: " + key) + "\nheader = " + json.dumps("Content-Type: " + media) + "\n"
        if body is not None:
            config += "data-binary = " + json.dumps(json.dumps(body)) + "\n"
        result = subprocess.run(["curl", "-sS", "--max-time", "25", "--config", "-", "--request", method,
                                 "--write-out", "\n%{http_code}", BASE + path], input=config, text=True,
                                capture_output=True, timeout=30)
        raw, code = result.stdout.rsplit("\n", 1)
        if result.returncode or not 200 <= int(code) < 300:
            raise RuntimeError("Salad " + method + " " + path + " returned HTTP " + code)
        return json.loads(raw) if raw.strip() else {}
    return request


def peer_api():
    key = subprocess.check_output([str(HELPER), "--read", "--service", "proxyrack-peer-api",
                                   "--account", getpass.getuser()], text=True, stderr=subprocess.DEVNULL).strip()

    def request(method, payload=None):
        if method not in ("balance", "bandwidth", "device/status"):
            raise ValueError("Unsupported ProxyRack operation")
        config = "header = " + json.dumps("Api-Key: " + key) + '\nheader = "Content-Type: application/json"\nheader = "Accept: application/json"\n'
        config += "data-binary = " + json.dumps(json.dumps(payload or {})) + "\n"
        result = subprocess.run(["curl", "-sS", "--max-time", "20", "--config", "-", "--request", "POST",
                                 "--write-out", "\n%{http_code}", "https://peer.proxyrack.com/api/" + method],
                                input=config, text=True, capture_output=True, timeout=25)
        raw, code = result.stdout.rsplit("\n", 1)
        if result.returncode or code not in ("200", "400"):
            try:
                detail = str(json.loads(raw).get("error", "")).replace(key, "[redacted]")[:160]
            except (ValueError, AttributeError):
                detail = ""
            raise RuntimeError("ProxyRack " + method + " returned HTTP " + code + (": " + detail if detail else ""))
        try:
            value = json.loads(raw)
            if isinstance(value, dict) and value.get("error"):
                value["error"] = str(value["error"]).replace(key, "[redacted]")
            return value
        except ValueError:
            raise RuntimeError("ProxyRack returned a non-JSON response") from None
    return request


def configuration():
    return json.loads((DATA / "deployment.json").read_text())


def parsed_time(value):
    value = re.sub(r"\.(\d+)(?=Z$|[+-]\d\d:\d\d$)",
                   lambda match: "." + (match[1] + "000000")[:6], value)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def device_id(config, instance):
    return hashlib.sha256((config["namespace"] + ":" + instance).encode()).hexdigest().upper()


def command():
    encoded = base64.b64encode((ROOT / "pr-worker.js").read_bytes()).decode()
    args = ["/bin/bash", "-c", 'umask 077; printf "%s" "$@" | base64 -d > /tmp/pr-worker.js; exec node /tmp/pr-worker.js',
            "pr-worker", *[encoded[index:index + 900] for index in range(0, len(encoded), 900)]]
    if len(args) > 100 or any(not value or len(value) > 1000 for value in args):
        raise ValueError("Worker command exceeds the verified Salad command envelope")
    return args


def deploy():
    api, config = salad_api(), configuration()
    if config.get("name") != "pr" or config.get("replicas") != 10:
        raise RuntimeError("Expected the single PR group with ten replicas")
    groups = {group["name"]: group for group in api(GROUPS).get("items", [])}
    if "pr" in groups:
        group = api(GROUPS + "/pr")
        if group["id"] != config.get("group_id") or group["container"]["environment_variables"].get("PR_NAMESPACE") != config["namespace"]:
            raise RuntimeError("Existing PR group has a different deployment identity")
        if group["container"]["command"] != command():
            raise RuntimeError("Use update to change the existing PR group")
    else:
        if config.get("group_id"):
            raise RuntimeError("The recorded group was deleted; preserve history and initialize a new deployment")
        key = subprocess.check_output([str(HELPER), "--read", "--service", "proxyrack-peer-api",
                                       "--account", getpass.getuser()], text=True).strip()
        group = api(GROUPS, "POST", {"name": "pr", "display_name": "PR", "replicas": 10,
            "autostart_policy": True, "restart_policy": "always", "country_codes": ["us"],
            "container": {"image": IMAGE, "priority": "batch", "resources": {"cpu": 1, "memory": 1024},
                "command": command(), "environment_variables": {"PR_NAMESPACE": config["namespace"],
                    "PR_API_KEY": key, "PR_BANDWIDTH_START": (parsed_time(config["created_at"]).date() - timedelta(days=1)).isoformat()}}})
        config["group_id"] = group["id"]
        save(DATA / "deployment.json", config)
    for _ in range(30):
        group = api(GROUPS + "/pr")
        if group["current_state"]["status"] != "preparing":
            break
        time.sleep(2)
    if group["current_state"]["status"] == "stopped":
        api(GROUPS + "/pr/start", "POST")
    elif group["current_state"]["status"] not in ("deploying", "running"):
        raise RuntimeError("PR is not ready to start: " + group["current_state"]["status"])
    print("PR requested: one group, ten replicas, US, 1 CPU / 1 GB each", flush=True)


def inspect():
    api, config = salad_api(), configuration()
    group = api(GROUPS + "/pr")
    if group["id"] != config["group_id"]:
        raise RuntimeError("Deployment identity changed")
    instances = api(GROUPS + "/pr/instances").get("instances", [])
    print("PR", group["current_state"], [{key: row.get(key) for key in ("id", "machine_id", "state", "ready")} for row in instances])


def update_startup():
    api, config = salad_api(), configuration()
    group = api(GROUPS + "/pr")
    environment = dict(group["container"].get("environment_variables", {}))
    if group["id"] != config["group_id"] or environment.get("PR_NAMESPACE") != config["namespace"]:
        raise RuntimeError("Deployment identity changed")
    if group["replicas"] != 10 or group["container"]["image"] != IMAGE:
        raise RuntimeError("PR image or replica configuration changed")
    environment["PR_BANDWIDTH_START"] = (parsed_time(config["created_at"]).date() - timedelta(days=1)).isoformat()
    if group["container"]["command"] != command() or environment != group["container"]["environment_variables"]:
        api(GROUPS + "/pr", "PATCH", {"container": {"command": command(), "environment_variables": environment}})
    after = api(GROUPS + "/pr")
    if after["container"]["command"] != command() or after["container"]["environment_variables"] != environment:
        raise RuntimeError("Worker update was not confirmed")
    print("PR worker command verified; version", after["version"], flush=True)


def initialize_ledger(meter, config):
    with meter.lock, meter.db:
        meter.db.executescript("""
            CREATE TABLE IF NOT EXISTS pr_events (
                event_id TEXT PRIMARY KEY, group_id TEXT, instance TEXT, machine TEXT,
                observer INTEGER, time_ms INTEGER, received_ms INTEGER, kind TEXT, body TEXT);
            CREATE INDEX IF NOT EXISTS pr_events_current ON pr_events(group_id,instance,kind,time_ms);
            CREATE TABLE IF NOT EXISTS pr_provider (
                group_id TEXT, device TEXT, body TEXT, PRIMARY KEY(group_id,device));
            CREATE TABLE IF NOT EXISTS pr_recovery (
                event_id TEXT PRIMARY KEY, group_id TEXT, instance TEXT, observer INTEGER,
                requested_ms INTEGER, reason TEXT, response_ms INTEGER, http INTEGER,
                accepted INTEGER, departed_ms INTEGER, fleet_connected_ms INTEGER);
        """)
        present = meter.db.execute("SELECT COUNT(*) FROM pr_provider WHERE group_id=?", (config["group_id"],)).fetchone()[0]
        path = DATA / "provider.json"
        if not present and path.exists():
            old = json.loads(path.read_text())
            if old.get("group_id") == config["group_id"]:
                for device, value in old.get("workers", {}).items():
                    meter.db.execute("INSERT OR IGNORE INTO pr_provider VALUES (?,?,?)",
                                     (config["group_id"], device, json.dumps(value)))
                account = {key: value for key, value in old.items() if key not in ("workers", "group_id")}
                meter.db.execute("INSERT OR IGNORE INTO pr_provider VALUES (?,?,?)",
                                 (config["group_id"], "", json.dumps(account)))


def provider_state(meter, group_id):
    with meter.lock:
        return {row[0]: json.loads(row[1]) for row in meter.db.execute(
            "SELECT device,body FROM pr_provider WHERE group_id=?", (group_id,))}


def merge_provider(meter, group_id, device, reading, event=None):
    old = meter.db.execute("SELECT body FROM pr_provider WHERE group_id=? AND device=?", (group_id, device)).fetchone()
    row = json.loads(old[0]) if old else {}
    kind, checked = reading["kind"], reading["checked_ms"]
    attempted = kind + "_attempted_ms"
    if checked < row.get(attempted, 0):
        return
    row[attempted] = checked
    error_field = "error" if kind == "status" else kind + "_error"
    row[error_field] = reading.get("error")
    if not reading.get("error"):
        if kind == "status":
            row.update(status=reading["status"], checked_ms=checked, ip=reading.get("ip"), online=reading.get("online"),
                       status_machine=(event or {}).get("machine"), status_observer=(event or {}).get("observer"))
        elif kind == "bandwidth":
            row.update(reported_value=reading["value"],
                       bandwidth_checked_ms=checked, bandwidth_observed_ms=reading.get("observed_ms"),
                       bandwidth_state=reading.get("provider_state"), bandwidth_source=reading.get("source", "container"))
        elif kind == "balance":
            row.update(balance=reading["balance"], balance_checked_ms=checked)
            row.setdefault("balance_baseline", reading["balance"])
            row.setdefault("balance_baseline_ms", checked)
    meter.db.execute("INSERT OR REPLACE INTO pr_provider VALUES (?,?,?)", (group_id, device, json.dumps(row)))


def save_events(meter, config, events):
    group_id = config["group_id"]
    with meter.lock, meter.db:
        for event in events:
            body = json.dumps(event, sort_keys=True, allow_nan=False)
            event_id = hashlib.sha256((group_id + body).encode()).hexdigest()
            inserted = meter.db.execute("INSERT OR IGNORE INTO pr_events VALUES (?,?,?,?,?,?,?,?,?)",
                (event_id, group_id, event.get("instance"), event.get("machine"), event.get("observer", 0),
                 event["time_ms"], int(time.time() * 1000), event["kind"], body)).rowcount
            if not inserted:
                continue
            if event["kind"] == "provider":
                merge_provider(meter, group_id, event.get("device", ""), event["reading"], event)
            elif event["kind"] == "registered":
                meter.db.execute("INSERT OR IGNORE INTO pr_provider VALUES (?,?,?)", (group_id, event["device"], "{}"))
            elif event["kind"] == "reallocate_requested":
                meter.db.execute("INSERT OR IGNORE INTO pr_recovery VALUES (?,?,?,?,?,?,NULL,NULL,NULL,NULL,NULL)",
                    (event_id, group_id, event["instance"], event["observer"], event["time_ms"], event["reason"]))
            if event["kind"] in ("reallocate_requested", "reallocate_response"):
                request = meter.db.execute("SELECT event_id,requested_ms FROM pr_recovery WHERE group_id=? AND instance=? AND observer=? ORDER BY requested_ms DESC LIMIT 1",
                    (group_id, event["instance"], event["observer"])).fetchone()
                if request:
                    response = meter.db.execute("SELECT body FROM pr_events WHERE group_id=? AND instance=? AND observer=? AND kind='reallocate_response' AND time_ms>=? ORDER BY time_ms DESC LIMIT 1",
                        (group_id, event["instance"], event["observer"], request[1])).fetchone()
                    if response:
                        value = json.loads(response[0])
                        meter.db.execute("UPDATE pr_recovery SET response_ms=?,http=?,accepted=? WHERE event_id=?",
                            (value["time_ms"], value.get("http", 0), int(value.get("accepted") is True), request[0]))


def valid_integer(value, lower=0, upper=2**53 - 1):
    return isinstance(value, int) and not isinstance(value, bool) and lower <= value <= upper


def validate_policy(value):
    if not isinstance(value, dict) or not isinstance(value.get("state"), str):
        raise ValueError("Invalid recovery policy")
    if value["state"] == "registering" and not valid_integer(value.get("registration_ms")):
        raise ValueError("Invalid registration policy")
    if value.get("last_credit_increase_ms") is not None and not valid_integer(value["last_credit_increase_ms"]):
        raise ValueError("Invalid provider progress time")
    errors = value.get("provider_errors", {})
    if not isinstance(errors, dict) or not all(isinstance(kind, str) and (message is None or isinstance(message, str)) for kind, message in errors.items()):
        raise ValueError("Invalid provider policy errors")
    issue = value.get("account_issue")
    if issue is not None and (not isinstance(issue, dict) or not all(isinstance(issue.get(key), str) for key in ("kind", "error"))):
        raise ValueError("Invalid account policy error")
    native = value.get("native_error")
    if native is not None and (not isinstance(native, dict) or not isinstance(native.get("message"), str) or not valid_integer(native.get("time_ms"), 1)):
        raise ValueError("Invalid native policy error")


def parse_entries(items, config, received_ms):
    records, events, rejected, ordered = [], [], 0, []
    for item in items:
        try:
            if not isinstance(item, dict) or not isinstance(item.get("text_log", ""), str):
                raise ValueError("Invalid source log")
            if not item.get("text_log", "").startswith(("PR_MINUTE_V1 ", "PR_MINUTE_V2 ", "PR_EVENT_V1 ")):
                continue
            source_time = parsed_time(item["time"])
            if source_time.tzinfo is None:
                raise ValueError("Missing source timezone")
            ordered.append((source_time, item))
        except (ValueError, KeyError, TypeError, OverflowError):
            rejected += 1
    for _, item in sorted(ordered, key=lambda entry: entry[0]):
        line = item["text_log"]
        try:
            modern = line.startswith("PR_MINUTE_V2 ")
            if modern or line.startswith("PR_EVENT_V1 "):
                value = json.loads(line.split(" ", 1)[1])
            else:
                value = dict(part.split("=", 1) for part in line.split()[1:])
            if not isinstance(value, dict):
                raise ValueError("Invalid source record")
            # Python accepts NaN/Infinity in JSON; neither can enter durable evidence.
            json.dumps(value, allow_nan=False)
            if not all(isinstance(value.get(key), str) and UUID.fullmatch(value[key]) for key in ("instance", "machine")):
                raise ValueError("Invalid instance identity")
            if value.get("device") != device_id(config, value["instance"]):
                continue
            if line.startswith("PR_EVENT_V1 "):
                if not isinstance(value.get("kind"), str) or not valid_integer(value.get("time_ms"), 1, received_ms + 30000):
                    raise ValueError("Invalid event time")
                if not valid_integer(value.get("observer"), 1, value["time_ms"]) or not valid_integer(value.get("event_seq")):
                    raise ValueError("Invalid event run")
                if value["kind"] == "provider":
                    reading = value["reading"]
                    if not isinstance(reading, dict) or reading.get("kind") not in ("status", "bandwidth") or not valid_integer(reading.get("checked_ms"), value["observer"], value["time_ms"] + 10000):
                        raise ValueError("Invalid provider record")
                    if reading.get("error") is not None and not isinstance(reading["error"], str):
                        raise ValueError("Invalid provider error")
                    if reading["kind"] == "bandwidth" and not reading.get("error"):
                        if isinstance(reading["value"], bool) or not isinstance(reading["value"], (int, float)) or not math.isfinite(reading["value"]) or reading["value"] < 0:
                            raise ValueError("Invalid provider amount")
                        if reading.get("observed_ms") is not None and not valid_integer(reading["observed_ms"], 0, reading["checked_ms"] + 10000):
                            raise ValueError("Invalid provider observation time")
                        if reading.get("provider_state") is not None and not isinstance(reading["provider_state"], str):
                            raise ValueError("Invalid provider bandwidth state")
                    if reading["kind"] == "status" and not reading.get("error"):
                        if not isinstance(reading.get("status"), str) or not isinstance(reading.get("online"), bool):
                            raise ValueError("Invalid provider connection state")
                        if reading.get("ip") is not None:
                            ipaddress.ip_address(reading["ip"])
                elif value["kind"] in ("reallocate_requested", "reallocate_response"):
                    if not isinstance(value.get("reason"), str):
                        raise ValueError("Invalid recovery reason")
                    if value["kind"] == "reallocate_response" and (not valid_integer(value.get("http"), 0, 599) or not isinstance(value.get("accepted"), bool)):
                        raise ValueError("Invalid recovery response")
                elif value["kind"] == "policy":
                    validate_policy(value["policy"])
                events.append(value)
                continue
            core = {key: value[key] for key in ("instance", "machine", "observer", "seq", "start_ms", "end_ms", "rx", "tx", "total_rx", "total_tx", "state", "error", "final")}
            core["ip"] = value.get("ip", "-")
            if modern:
                core["bucket_ms"] = value["bucket_ms"]
            converted = dict(item, text_log="PS_MINUTE_V2 " + " ".join(key + "=" + str(amount) for key, amount in core.items()))
            record = parse_minute(converted, config["group_id"], received_ms)
            if not record or record["end_ms"] > received_ms + 30000:
                raise ValueError("Invalid minute record")
            record["record_bytes"] = len(line.encode()) + 1
            if modern and value.get("valid") != 1:
                record["valid"] = 0
            records.append(record)
            if modern:
                validate_policy(value["policy"])
                if not valid_integer(value.get("event_seq")):
                    raise ValueError("Invalid policy sequence")
                events.append({"kind": "policy", "time_ms": value["end_ms"], "instance": value["instance"],
                    "machine": value["machine"], "observer": value["observer"], "event_seq": value["event_seq"],
                    "device": value["device"], "policy": value["policy"]})
        except (ValueError, KeyError, TypeError, OverflowError):
            rejected += 1
    return records, events, rejected


def read_logs(api, start, end):
    calls, deadline = 0, time.monotonic() + 45
    def fetch(begin, finish):
        nonlocal calls
        calls += 1
        if calls > 128 or time.monotonic() > deadline:
            raise RuntimeError("PR log catch-up exceeded this poll's bounded window")
        items = api("/log-entries", "POST", {"query": 'resource.type = "container" and resource.labels.project_name = "default" and resource.labels.container_group_name = "pr" and (text_log startswith "PR_MINUTE_V1 " or text_log startswith "PR_MINUTE_V2 " or text_log startswith "PR_EVENT_V1 ")',
            "start_time": begin.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "end_time": finish.isoformat(timespec="milliseconds").replace("+00:00", "Z"), "page_size": 100, "sort_order": "asc"}).get("items")
        if not isinstance(items, list):
            raise RuntimeError("Invalid Salad log response")
        if len(items) < 100:
            return items
        if finish - begin <= timedelta(milliseconds=1):
            raise RuntimeError("PR log page remains capped at one millisecond")
        middle = begin + (finish - begin) / 2
        return fetch(begin, middle) + fetch(middle, finish)
    return fetch(start, end)


def provider_observed_ms(text, checked):
    if not isinstance(text, str):
        return None
    if re.search(r"updated\s+(?:just now|a few seconds ago)", text, re.I):
        return checked
    match = re.search(r"updated\s+(\d+)\s+(second|minute|hour|day)s?\s+ago", text or "", re.I)
    return checked - int(match[1]) * {"second": 1000, "minute": 60000, "hour": 3600000, "day": 86400000}[match[2].lower()] if match else None


def lookup_countries(meter):
    with meter.lock:
        ips = [row[0] for row in meter.db.execute("SELECT DISTINCT ip FROM minutes WHERE ip IS NOT NULL AND ip NOT IN (SELECT ip FROM countries) LIMIT 100")]
    if not ips:
        return
    response = subprocess.run(["curl", "-fsS", "--max-time", "5", "-H", "Content-Type: application/json",
        "--data-binary", "@-", "https://api.country.is/"], input=json.dumps(ips), text=True, capture_output=True, timeout=7)
    if response.returncode:
        return
    values = json.loads(response.stdout)
    if not isinstance(values, list):
        return
    with meter.lock, meter.db:
        for row in values:
            if isinstance(row, dict) and row.get("ip") in ips and (row.get("country") is None or isinstance(row["country"], str) and re.fullmatch(r"[A-Z]{2}", row["country"])):
                meter.db.execute("INSERT OR REPLACE INTO countries VALUES (?,?)", (row["ip"], row.get("country")))


def provider_loop(config, members, meter, stopped):
    api = None
    while not stopped.is_set():
        try:
            if api is None:
                api = peer_api()
            states = provider_state(meter, config["group_id"])
            current = set(members.get("devices", []))
            now = int(time.time() * 1000)
            account = states.get("", {})
            jobs = []
            if now - account.get("balance_attempted_ms", 0) >= 600000:
                jobs.append((0, "balance", ""))
            if members.get("loaded"):
                for device, row in states.items():
                    if device and device not in current and now - row.get("bandwidth_attempted_ms", 0) >= 3600000:
                        jobs.append((row.get("bandwidth_attempted_ms", 0), "bandwidth", device))
            if not jobs:
                stopped.wait(5)
                continue
            _, kind, device = min(jobs)
            payload = {"device_id": device, "date_start": (parsed_time(config["created_at"]).date() - timedelta(days=1)).isoformat(),
                       "date_end": datetime.now(timezone.utc).date().isoformat()} if device else {}
            reading = {"kind": kind, "checked_ms": now, "source": "recorder"}
            try:
                response = api(kind, payload)
                if response.get("status") != "success":
                    raise RuntimeError(response.get("error", "Provider has not returned a reading"))
                data = response["data"]
                reading["checked_ms"] = int(time.time() * 1000)
                if kind == "balance":
                    balance = data["balance"]
                    if not isinstance(balance, (str, int, float)) or isinstance(balance, bool) or isinstance(balance, (int, float)) and not math.isfinite(balance):
                        raise ValueError("Provider returned an invalid account balance")
                    reading["balance"] = balance
                else:
                    values = data.get("bandwidth")
                    if not isinstance(values, dict) or not values or not all(isinstance(day, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)
                            and payload["date_start"] <= day <= payload["date_end"] and isinstance(value, (int, float))
                            and not isinstance(value, bool) and math.isfinite(value) and value >= 0 for day, value in values.items()):
                        raise ValueError("Provider returned invalid bandwidth values")
                    amount = sum(values.values())
                    if not math.isfinite(amount):
                        raise ValueError("Provider returned an invalid bandwidth total")
                    reading.update(value=amount, days=values, date_start=payload["date_start"], date_end=payload["date_end"], provider_state=data.get("state"),
                                   observed_ms=provider_observed_ms(data.get("state"), reading["checked_ms"]))
            except Exception as failure:
                reading["error"] = str(failure)[:200]
                reading["checked_ms"] = int(time.time() * 1000)
            save_events(meter, config, [{"kind": "provider", "time_ms": reading["checked_ms"], "device": device, "reading": reading, "source": "recorder"}])
        except Exception as failure:
            print("PR account recording:", type(failure).__name__, str(failure)[:160], flush=True)
            api = None
        # Current devices report from their containers; reserve API capacity for registration.
        stopped.wait(90)


def make_view(meter, config, inventory, cloud_error=None):
    now = int(time.time() * 1000)
    group_id = config["group_id"]
    states = provider_state(meter, group_id)
    with meter.lock:
        stats = {(row["instance"], row["machine"], row["observer"]): row for row in meter.db.execute("""
            SELECT instance, machine, observer, SUM(rx+tx) bytes,
                   SUM(end_ms-start_ms) duration_ms, COUNT(*) samples
            FROM minutes WHERE group_id=? AND valid=1 AND seq>0 GROUP BY instance,machine,observer""", (group_id,))}
        total_mb = sum(row["bytes"] for row in stats.values()) / 1e6 if stats else None
        first = now // 60000 * 60000 - 1439 * 60000
        rows = meter.db.execute("SELECT m.*,c.country FROM minutes m LEFT JOIN countries c ON m.ip=c.ip WHERE group_id=? AND valid=1 AND seq>0 AND end_ms>=? ORDER BY end_ms", (group_id, first)).fetchall()
        minutes = bucket_minutes(rows, first, now // 60000 * 60000)
        latest = meter.latest(group_id)
        workers = []
        for member in inventory.get("instances", []):
            instance = member["id"]
            device = device_id(config, instance)
            running = inventory.get("status") != "stopped" and member["state"] == "running"
            row = latest.get(instance)
            if row and row["machine"] != member["machine_id"]:
                row = None
            age = now - row["end_ms"] if row else None
            fresh = bool(not cloud_error and row and row["valid"] and not row["final"] and -10000 <= age <= 150000)
            details = states.get(device, {})
            p_age = now - details["checked_ms"] if details.get("checked_ms") else None
            provider_fresh = bool(row and details.get("status_machine") == row["machine"] and details.get("status_observer") == row["observer"]
                                  and p_age is not None and 0 <= p_age <= 900000 and not details.get("error"))
            provider_online = details.get("online") if provider_fresh else None
            saved = meter.db.execute("SELECT body FROM pr_events WHERE group_id=? AND instance=? AND machine=? AND kind='policy' ORDER BY time_ms DESC LIMIT 1",
                                     (group_id, instance, member["machine_id"])).fetchone()
            latest_policy = json.loads(saved[0]) if saved else None
            policy = latest_policy.get("policy") if latest_policy and row and latest_policy.get("observer") == row["observer"] and -10000 <= now - latest_policy["time_ms"] <= 150000 else None
            status = "Status unavailable" if cloud_error else "Stopped" if inventory.get("status") == "stopped" else member["state"].capitalize() if member["state"] != "running" else "Checking client"
            if not cloud_error and running and row and -10000 <= age <= 150000:
                status = "Reallocating" if row["state"] == "reallocating" else "Stopped" if row["final"] else "Connected (control)" if fresh and row["state"] == "running" else "Registration pending" if row["error"] == "registration_pending" else row["state"].capitalize()
                if not row["valid"] and not row["final"]:
                    status = "Measurement invalid"
            if running and provider_online is False and fresh and row["state"] == "running":
                status = "Provider offline"
            connected = running and fresh and row["state"] == "running" and row["error"] == "none" and provider_online is not False
            seconds = (row["end_ms"] - row["start_ms"]) / 1000 if row else None
            amount = (row["rx"] + row["tx"]) / 1e6 if row and row["valid"] and row["seq"] > 0 else None
            recorded = stats.get((instance, member["machine_id"], row["observer"])) if row else None
            workers.append({"name": "PR-" + instance[:8], "instance": instance, "salad_instance": instance,
                "machine": member["machine_id"], "device_id": device, "salad_state": member["state"],
                "status": status, "client_state": row["state"] if fresh else None, "fresh": fresh, "connected": bool(connected),
                "client_error": row["error"] if row and row["error"] != "none" else None,
                "provider_status": details.get("status", "Not checked"), "provider_online": provider_online,
                "provider_age_ms": p_age, "provider_checked_ms": details.get("checked_ms"), "provider_error": details.get("error"),
                "credited_mb": None, "provider_bandwidth_value": details.get("reported_value", details["credited_mb"] / 1000 if details.get("credited_mb") is not None else None), "bandwidth_state": details.get("bandwidth_error") or details.get("bandwidth_state"),
                "bandwidth_checked_ms": details.get("bandwidth_checked_ms"), "bandwidth_observed_ms": details.get("bandwidth_observed_ms"),
                "bandwidth_error": details.get("bandwidth_error"),
                "ip": row["ip"] if row else None, "country": next((point[0] for point in meter.db.execute("SELECT country FROM countries WHERE ip=?", (row["ip"],))), None) if row else None,
                "network_mb": recorded["bytes"] / 1e6 if recorded else None,
                "recorded_duration_ms": recorded["duration_ms"] if recorded else None,
                "interval_mb": amount, "interval_seconds": seconds,
                "mb_per_minute": amount * 60 / seconds if fresh and running and amount is not None and seconds else None,
                "sample_age_ms": age, "last_sample_ms": row["end_ms"] if row else None,
                "recorded_ms": row["received_ms"] if row else None, "samples": recorded["samples"] if recorded else 0,
                "policy": policy, "group_url": "https://portal.salad.com/organizations/ssh1/projects/default/containers/pr/instances/" + instance})
        if not cloud_error and inventory.get("checked_ms"):
            ids = {member["id"] for member in inventory.get("instances", [])}
            with meter.db:
                for recovery in meter.db.execute("SELECT event_id,instance,departed_ms FROM pr_recovery WHERE group_id=? AND fleet_connected_ms IS NULL AND requested_ms<=?", (group_id, inventory["checked_ms"])).fetchall():
                    if recovery[1] not in ids and recovery[2] is None:
                        meter.db.execute("UPDATE pr_recovery SET departed_ms=? WHERE event_id=?", (inventory["checked_ms"], recovery[0]))
                    if recovery[1] not in ids and len(workers) == config["replicas"] and all(worker["connected"] for worker in workers):
                        meter.db.execute("UPDATE pr_recovery SET fleet_connected_ms=? WHERE event_id=?", (now, recovery[0]))
        recovery = [dict(row) for row in meter.db.execute("SELECT * FROM pr_recovery WHERE group_id=? ORDER BY requested_ms DESC LIMIT 20", (group_id,))]
    for point in minutes:
        point["partial"] |= point["count"] < config["replicas"]
    reported = [row.get("reported_value", row["credited_mb"] / 1000 if row.get("credited_mb") is not None else None) for device, row in states.items() if device]
    reported = [value for value in reported if value is not None]
    account = states.get("", {})
    return {"generated_ms": now, "inventory_ms": inventory.get("checked_ms"), "organization": "SSH1", "group": "PR",
        "requested": config["replicas"], "group_id": group_id, "workers": workers,
        "reporting": sum(worker["fresh"] for worker in workers), "online": sum(worker["connected"] for worker in workers),
        "network_mb": total_mb, "total_recorded_mb": total_mb,
        "credited_mb": None, "provider_bandwidth_value": sum(reported) if reported else None, "provider_bandwidth_unit": None,
        "credited_devices": sum(worker["provider_bandwidth_value"] is not None for worker in workers),
        "credited_total_devices": len(reported), "balance": account.get("balance"), "balance_checked_ms": account.get("balance_checked_ms"),
        "balance_error": account.get("balance_error"), "balance_baseline": account.get("balance_baseline"),
        "balance_baseline_ms": account.get("balance_baseline_ms"), "minutes": minutes, "recent_recovery": recovery,
        "cloud_error": cloud_error, "started_at": config["created_at"], "schema_version": 2}


def decorate_snapshot(result, now_ms=None):
    """Expire live claims and describe the same evidence for every PR page."""
    now = int(time.time() * 1000) if now_ms is None else now_ms
    def recent(value, limit):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and -10000 <= now - value <= limit
    def timestamp(value):
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z") if valid_integer(value, 1, 253402300799999) else "unknown"
    known = recent(result.get("generated_ms"), 90000) and recent(result.get("inventory_ms"), 90000) and not result.get("cloud_error")
    if not known:
        result["cloud_error"] = result.get("cloud_error") or "PR recorder or inventory is stale."
    for worker in result.get("workers", []):
        worker["sample_age_ms"] = now - worker["last_sample_ms"] if valid_integer(worker.get("last_sample_ms"), 1) else None
        worker["provider_age_ms"] = now - worker["provider_checked_ms"] if valid_integer(worker.get("provider_checked_ms"), 1) else None
        if not known or not recent(worker.get("last_sample_ms"), 150000):
            worker.update(connected=None, fresh=False, client_state=None, mb_per_minute=None, status="Unverified", policy=None)
        if not recent(worker.get("provider_checked_ms"), 900000) or worker.get("provider_error"):
            worker["provider_online"] = None
        worker["bandwidth_fresh"] = recent(worker.get("bandwidth_observed_ms"), 900000) and not worker.get("bandwidth_error")
        notes = []
        if worker.get("provider_bandwidth_value") is not None:
            notes.append("Provider counter: " + str(worker["provider_bandwidth_value"]) + " for this device (unit unverified; "
                         + ("fresh" if worker["bandwidth_fresh"] else "cached/stale") + "; source " + timestamp(worker.get("bandwidth_observed_ms"))
                         + "; checked " + timestamp(worker.get("bandwidth_checked_ms")) + ").")
        else:
            notes.append("Provider bandwidth is not reported yet.")
        if worker.get("client_error"):
            notes.append({"no_credited_growth": "no provider counter increase", "credit_progress_unverified": "provider counter progress unverified"}.get(worker["client_error"], worker["client_error"].replace("_", " ")))
        policy = worker.get("policy")
        if policy is not None:
            try:
                validate_policy(policy)
            except (ValueError, TypeError):
                policy = worker["policy"] = None
                notes.append("Recovery policy record is malformed.")
        if policy:
            if policy["state"] == "reallocating":
                notes.append("Reallocation requested; waiting for Salad.")
            elif policy.get("account_issue"):
                issue = policy["account_issue"]
                notes.append("Account " + issue["kind"].replace("_", " ") + ": " + issue["error"] + ". Retrying with cooldown; keeping this node.")
            elif policy["state"] == "registering":
                notes.append("Registration retries are bounded to " + str(policy["registration_ms"] // 60000) + " min.")
            elif policy["state"] != "exited":
                notes.append("Healthy idle nodes stay running; traffic and earnings do not trigger replacement.")
                growth = policy.get("last_credit_increase_ms")
                notes.append("Last provider counter increase %.1f min ago." % (max(0, now - growth) / 60000) if isinstance(growth, (int, float)) else "A provider counter increase has not yet been verified in this run.")
            for kind, message in policy.get("provider_errors", {}).items():
                if message:
                    notes.append("Provider " + kind + ": " + message)
            native = policy.get("native_error")
            if native:
                notes.append("Last native error: " + native["message"] + " (%.1f min ago)." % (max(0, now - native["time_ms"]) / 60000))
        else:
            notes.append("Waiting for the in-container recovery policy." if worker.get("fresh") else "Waiting for a fresh native sample.")
        for field, kind in (("provider_error", "status"), ("bandwidth_error", "bandwidth")):
            if worker.get(field) and not (policy or {}).get("provider_errors", {}).get(kind):
                notes.append("Provider " + kind + ": " + worker[field])
        worker["status_note"] = " · ".join(notes)
    result["credited_fresh_current_devices"] = sum(worker["bandwidth_fresh"] for worker in result.get("workers", [])) if known else None
    result["online"] = sum(worker.get("connected") is True for worker in result.get("workers", [])) if known else None
    result["reporting"] = sum(worker.get("fresh") is True for worker in result.get("workers", [])) if known else None
    coverage = "%s/%s current readings are fresh." % (result["credited_fresh_current_devices"], result.get("requested", 10)) if known else "Current-device provider coverage is unknown while inventory is stale."
    result["credit_note"] = ("Connected confirms the native control link, not paid traffic. Raw network RX + TX includes relaying, registration and health checks. "
        "The original provider counter is shown without a unit conversion; the API unit is awaiting reference verification. It includes current and replaced devices; " + coverage + " Earnings attributable to this group are not reported.")
    if result.get("balance") is not None:
        result["credit_note"] += " Account balance " + str(result["balance"]) + " (checked " + timestamp(result.get("balance_checked_ms")) + "; " + ("fresh" if recent(result.get("balance_checked_ms"), 900000) and not result.get("balance_error") else "stale") + ") covers the whole account."
    if result.get("balance_error"):
        result["credit_note"] += " Account reading error: " + result["balance_error"] + "."
    if result.get("recent_recovery"):
        action = result["recent_recovery"][0]
        response = "accepted by Salad" if action.get("accepted") else "latest response HTTP " + str(action["http"]) if action.get("http") is not None else "awaiting Salad response"
        outcome = "; fleet returned to %s confirmed control connections" % result["requested"] if action.get("fleet_connected_ms") else "; old instance left the fleet; replacement confirmation pending" if action.get("departed_ms") else "; old instance departure pending"
        result["credit_note"] += " Latest automatic reallocation: " + {"no_credited_growth": "no provider counter increase", "credit_progress_unverified": "provider counter progress unverified"}.get(action["reason"], action["reason"].replace("_", " ")) + " · " + response + outcome + "."
    return result


def public_snapshot(data_dir=DATA, now_ms=None):
    """Read the precomputed PR view; legacy views get their existing ledger history."""
    result = json.loads((Path(data_dir) / "view.json").read_text())
    if result.get("schema_version") != 2:
        result["minutes"] = saved_minutes(Path(data_dir) / "traffic.sqlite3", now_ms=now_ms, group_id=result["group_id"])
    return decorate_snapshot(result, now_ms)


def observe(once=False):
    config = configuration()
    lock = (DATA / "observer.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    meter = NetworkMinutes(DATA / "traffic.sqlite3")
    initialize_ledger(meter, config)
    api = salad_api()
    stopped, members = threading.Event(), {}
    if not once:
        threading.Thread(target=provider_loop, args=(config, members, meter, stopped), daemon=True).start()
    saved = meter.collector_status(config["group_id"])
    cursor = parsed_time(saved["cursor"]) if saved.get("cursor") else parsed_time(config["created_at"])
    cursor_path = DATA / "cursor.json"
    if not saved.get("cursor") and cursor_path.exists():
        old = json.loads(cursor_path.read_text())
        if old.get("group_id") == config["group_id"]:
            cursor = parsed_time(old["cursor"])
    inventory, catchup_seconds, next_geo = {}, 3600, 0
    try:
        while True:
            began = time.monotonic()
            error = None
            try:
                group = api(GROUPS + "/pr")
                if group["id"] != config["group_id"]:
                    raise RuntimeError("Deployment identity changed")
                instances = api(GROUPS + "/pr/instances").get("instances")
                if not isinstance(instances, list) or any(not isinstance(member, dict)
                        or not isinstance(member.get("id"), str) or not UUID.fullmatch(member["id"])
                        or "machine_id" not in member or member["machine_id"] is not None
                        and (not isinstance(member["machine_id"], str) or not UUID.fullmatch(member["machine_id"]))
                        or not isinstance(member.get("state"), str) for member in instances):
                    raise RuntimeError("Invalid Salad instance inventory")
                inventory = {"status": group["current_state"]["status"], "instances": instances,
                             "checked_ms": int(time.time() * 1000)}
                now = datetime.now(timezone.utc)
                end = min(now, cursor + timedelta(seconds=catchup_seconds))
                entries = read_logs(api, cursor - timedelta(minutes=10), end)
                records, events, rejected = parse_entries(entries, config, int(time.time() * 1000))
                inserted = meter.ingest(records)
                save_events(meter, config, events)
                if rejected:
                    error = "PR rejected malformed source records: " + str(rejected)
                cursor = end
                meter.collector_status(config["group_id"], cursor.isoformat(), error=error, inserted=inserted)
                save(cursor_path, {"group_id": config["group_id"], "cursor": cursor.isoformat()})
                catchup_seconds = min(3600, catchup_seconds * 2)
                if not once and time.monotonic() >= next_geo:
                    next_geo = time.monotonic() + 300
                    try:
                        lookup_countries(meter)
                    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired) as failure:
                        print("PR country lookup:", type(failure).__name__, flush=True)
            except Exception as failure:
                error = str(failure)[:220]
                if "bounded window" in error:
                    catchup_seconds = max(60, catchup_seconds // 2)
                print("PR recording:", error, flush=True)
            view = make_view(meter, config, inventory, error)
            view["backfilling"] = cursor < datetime.now(timezone.utc) - timedelta(minutes=2)
            view["cursor_ms"] = int(cursor.timestamp() * 1000)
            members.update(devices=[worker["device_id"] for worker in view["workers"]], loaded=bool(inventory.get("checked_ms")))
            decorate_snapshot(view)
            save(DATA / "view.json", view)
            if once:
                return view
            stopped.wait(max(1, 15 - (time.monotonic() - began)))
    finally:
        stopped.set()
        if once:
            meter.db.close()
            lock.close()


def main():
    os.umask(0o077)
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["deploy", "status", "observe", "update"])
    parser.add_argument("--once", action="store_true", help="Record one Salad cycle without starting the provider account reader")
    args = parser.parse_args()
    if args.action == "deploy":
        deploy()
    elif args.action == "status":
        inspect()
    elif args.action == "update":
        update_startup()
    else:
        observe(args.once)


if __name__ == "__main__":
    main()
