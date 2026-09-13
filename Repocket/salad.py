"""Inspect/stop only the Repocket pilot; run on Mumbai as root via SSM."""
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

import boto3

NAME = "repocket-eval"
GROUP_ID = "a1bae54a-a634-43cc-9f6a-0624e01e1b20"
URL = "https://api.salad.com/api/public/organizations/cosmosgeeketh/projects/default/containers/" + NAME
access = json.loads(boto3.client("secretsmanager", region_name="ap-south-1")
                    .get_secret_value(SecretId="wdi/salad-access")["SecretString"])


def request(path="", method="GET", body=None):
    req = urllib.request.Request(path if path.startswith("https://") else URL + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Salad-Api-Key": access["api_key"],
                                          "Content-Type": "application/merge-patch+json" if method == "PATCH" else "application/json",
                                          "User-Agent": "WDI/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as response:
        body = response.read()
        return json.loads(body) if body else None


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action not in ("status", "logs", "stop"):
        raise SystemExit("usage: salad.py [status|logs|stop]")
    group = request()
    if group["id"] != GROUP_ID:
        raise SystemExit("Pilot group identity changed; refusing to operate on another group")
    if action == "logs":
        now = datetime.now(timezone.utc)
        logs = request("https://api.salad.com/api/public/organizations/cosmosgeeketh/log-entries", "POST",
                       {"start_time": (now - timedelta(hours=1)).isoformat(timespec="seconds"),
                        "end_time": now.isoformat(timespec="seconds"),
                        "query": 'resource.labels.container_group_name = "repocket-eval"',
                        "page_size": 100, "sort_order": "asc"})
        output = json.dumps(logs, indent=2)
        for value in group["container"].get("environment_variables", {}).values():
            if value:
                output = output.replace(value, "[REDACTED]")
        print(output)
        raise SystemExit()
    if action == "stop":
        request("/stop", "POST")
        group = request()
    record = {k: group.get(k) for k in ("id", "name", "version", "pending_change", "create_time", "current_state",
                                       "replicas", "restart_policy", "country_codes")}
    record["container"] = {k: group["container"].get(k) for k in ("image", "resources")}
    record["instances"] = [request("/instances/" + i["id"]) for i in request("/instances")["instances"]]
    record["observed_at"] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(record, indent=2))
