"""Read TM's published measurements; recompute freshness without touching history."""
import json
from pathlib import Path
import time


def snapshot(source=None):
    path = Path.home() / ".hg1/traffmonetizer/latest.json"
    if source is None and not path.exists():
        return {"provider": "traffmonetizer", "workers": [], "chart_minutes": [],
                "working": None, "running": None, "errors": {"recorder": "Recorder has not published a reading yet"}}
    data = dict(source) if source is not None else json.loads(path.read_text())
    data["workers"] = [dict(worker) for worker in data.get("workers", [])]
    if data.get("credited"):
        data["credited"] = dict(data["credited"])
    now = int(time.time() * 1000)
    age = now - data["generated_ms"]
    data["snapshot_age_ms"] = age
    data["stale"] = not -5000 <= age <= 45000
    inventory_ms = data.get("inventory_ms")
    inventory_fresh = isinstance(inventory_ms, (int, float)) and 0 <= now - inventory_ms <= 90000
    data["inventory_fresh"] = inventory_fresh
    verified = not data["stale"] and inventory_fresh and not data.get("errors", {}).get("recorder")
    for worker in data.get("workers", []):
        sample_age = now - worker["time_ms"] if worker.get("time_ms") is not None else None
        receipt_age = now - worker["received_ms"] if worker.get("received_ms") is not None else None
        status_age = worker.get("status_age_ms")
        statistics_age = worker.get("statistics_age_ms")
        worker.update(sample_age_ms=sample_age, receipt_age_ms=receipt_age)
        worker["fresh"] = bool(not data["stale"] and sample_age is not None and -5000 <= sample_age <= 45000
                               and receipt_age is not None and -5000 <= receipt_age <= 45000)
        worker["statistics_fresh"] = bool(worker["fresh"] and statistics_age is not None and 0 <= statistics_age + max(0, sample_age) <= 60000)
        worker["connection_fresh"] = bool(worker["statistics_fresh"] and status_age is not None and 0 <= status_age + max(0, sample_age) <= 60000)
        worker["working"] = bool(verified and worker["connection_fresh"] and worker.get("state") == "connected" and worker.get("salad_state") == "running")
        if not worker["fresh"]:
            worker["raw_mb_per_minute"] = None
        if not worker["statistics_fresh"]:
            worker["native_recent_bytes"] = worker["native_recent_requests"] = worker["native_recent_ms"] = None
            reason = "Native statistics reply age is missing" if statistics_age is None else "Native statistics reply is stale or unverified"
            if reason not in (worker.get("error") or ""):
                worker["error"] = "; ".join(filter(None, (worker.get("error"), reason)))
    data["working"] = sum(worker["working"] for worker in data.get("workers", [])) if verified else None
    if not inventory_fresh:
        data["running"] = None
    provider = data.get("credited")
    if provider:
        provider["age_ms"] = now - provider["observed_ms"]
        provider["fresh"] = -5000 <= provider["age_ms"] <= 180000 and not data.get("errors", {}).get("provider")
    return data
