#!/bin/sh
# Install the existing private origin on Mumbai after restoring its data.
set -eu
[ "$(id -u)" -eq 0 ] || { echo 'Run through AWS SSM as root' >&2; exit 1; }
cd /opt/wdi/website
command -v setfacl >/dev/null
for account in salad salad-ui cloudflared-salad salad-flow; do
    getent group "$account" >/dev/null || groupadd --system "$account"
    id "$account" >/dev/null 2>&1 || useradd --system --gid "$account" --home-dir /nonexistent --shell /usr/sbin/nologin "$account"
done
usermod -a -G salad-ui cloudflared-salad
for path in /home/salad/.hg1/bandwidth.sqlite3 /home/salad/.hg1/packetstream/minutes.sqlite3; do
    [ -f "$path" ] || { echo "Restore the retained database first: $path" >&2; exit 1; }
done
# A standalone WAL snapshot has no shared-memory file for a read-only reader.
# Active writers retain their WAL; dormant snapshots use a rollback journal.
python3 - <<'PYTHON'
from pathlib import Path
import sqlite3
for path in Path('/home/salad/.hg1').rglob('*.sqlite3'):
    if not Path(str(path)+'-wal').exists() and not Path(str(path)+'-shm').exists():
        with sqlite3.connect(path) as db:
            db.execute('PRAGMA journal_mode=DELETE')
PYTHON
chown salad:salad /home/salad
chmod 750 /home/salad
setfacl -m u:salad-ui:--x,u:salad-flow:--x /home/salad
chown -R salad:salad /home/salad/.hg1
chmod -R o-rwx /home/salad/.hg1
setfacl -R -m u:salad-ui:r-X,u:salad-flow:r-X /home/salad/.hg1
find /home/salad/.hg1 -type d -exec setfacl -m d:u:salad-ui:r-x,d:u:salad-flow:r-x {} +
setfacl -m u:salad-ui:rw /home/salad/.hg1/packetstream/provider-observations.jsonl
install -d -m 750 -o salad -g salad-ui /var/lib/salad-ip-evidence
install -d -m 750 -o salad-ui -g salad-ui /var/lib/salad-ip-enrichment
chown -R salad:salad-ui /var/lib/salad-ip-evidence
chown -R salad-ui:salad-ui /var/lib/salad-ip-enrichment
chmod -R g+rX,o-rwx /var/lib/salad-ip-evidence /var/lib/salad-ip-enrichment
install -d -m 755 -o salad-flow -g salad-flow /var/lib/cosmosgeek-public-flow
install -m 644 systemd/*.service /etc/systemd/system/
systemctl daemon-reload
systemd-analyze verify systemd/*.service
# Operator and reason are retained in the journal for this website deployment.
logger -t wdi-website 'Initiator: Codex, owner-authorized website migration; instance: i-0c2e48a7ddc2dd010; action: start/restart website and evidence services'
systemctl enable salad-dashboard salad-ip-evidence salad-ip-enrichment wdi-grass-collector wdi-repocket-collector salad-public-flow salad-public-flow-publish
systemctl restart salad-dashboard salad-ip-evidence salad-ip-enrichment wdi-grass-collector wdi-repocket-collector salad-public-flow salad-public-flow-publish
systemctl enable --now cloudflared-salad
systemctl is-active cloudflared-salad salad-dashboard salad-ip-evidence salad-ip-enrichment wdi-grass-collector wdi-repocket-collector salad-public-flow salad-public-flow-publish
