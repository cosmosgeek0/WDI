# WDI website

The existing dashboard runs as a private Unix-socket origin on Mumbai from
`/opt/wdi/website`. The public Cloudflare connector remains on `mumbai-old` until
an owner-authorized cutover. The origin keeps the existing Access audience,
issuer and trusted connector UID; it has no TCP listener.

Run commands and the retained data paths are in the root `AGENTS.md`.
`install.sh` installs the website units after the histories have been restored.
It does not deploy or restart Grass, Repocket or other Salad applications.

`collectors/grass.py` and `collectors/repocket.py` are the existing collectors,
moved from the old host and adapted to read the AWS instance-role secret in
memory. Grass reads the group selected by `/etc/wdi/grass.env`. Repocket's unit
selects `cosmosgeeketh/default/wdi-grass-continuity`, pinned by the retained deployment
metadata. Its cursor is named by group UUID; the original `cursor.json` and old
deployment snapshot are retained for the historical group.

The combined image uses `Repocket/traffic/traffic.py` separately around each
application process tree. It counts completed TCP/UDP socket-I/O payload bytes,
including application protocol traffic. It excludes unrelated processes, files
and Unix-domain sockets; it does not measure packet headers or provider credits.
Shared container counters remain in original Grass samples as `container_rx`,
`container_tx` and `container_counter_source_ms`, outside both app series.

Counter failures remain gaps. Native health observations are retained even when
app bytes or their IP binding are unavailable. Old groups remain accessible in
history with their original container measurement scope. The IP view keeps both
app observations on one allocation and counts its recorded cost once.

The Grass reader accepts the existing `GRASS_SAMPLE_V2` envelope with cumulative
`rx`/`tx` bytes only when `traffic_scope` is `application`, `application` is `grass`,
and `counter_id` identifies the same measured app-counter lifetime at both
endpoints. Missing attribution, counter changes, resets, clock changes and IP
changes produce gaps. Null counters and `valid=false` preserve the health sample
without supplying traffic. Current group requirements are stored in the existing
traffic database's `measurement_groups` table.

The Repocket reader accepts its existing `RP_MON` minute envelope only when
`traffic_scope` is `application` and `application` is `repocket`; `rx_bytes` and
`tx_bytes` are interval bytes. The existing identity, sequence, interval and
conflict checks still apply. Source, collector receipt and provider indexing times
retain their separate meanings.

The IP check runs inside the existing container runtime through
`Grass/ip_policy.py`. A matching-IP, explicit datacenter classification triggers
the container's own reallocation request; inconclusive lookup results do not
establish residential status. The website retains `ip_classified` and
`reallocation_*` records from `GRASS_PRIVATE_EVENT_V1` in the existing Grass event
ledger. These container events keep their source identities and do not invent
an application run or any traffic. The website has no reallocation authority.

Keep the original snapshot checksum manifest on Mumbai at
`/var/lib/wdi-website-migration/source-manifest.json`. Before any public cutover,
catch up the old host's still-active history writers and reconcile collection
ownership; the initial migration snapshot is not an ongoing mirror.
