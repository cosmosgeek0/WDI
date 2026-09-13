# WDI website

The public WDI dashboard runs on Mumbai from `/opt/wdi/website` through
`cloudflared-salad`. The origin keeps the existing Access audience, issuer and
trusted connector UID; it listens only on its protected Unix socket.

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

The old instance `i-0491397473a219afa` is terminated. Its final encrypted EBS
snapshot is `snap-018b0f9f57148fc2d`. Website histories and legacy collection
cursors were reconciled before the connector moved. The snapshot manifest and
final legacy status files remain under `/var/lib/wdi-website-migration/`.

`salad-public-flow` and `salad-public-flow-publish` maintain the existing anonymous
aggregate. Separate app counters may share a container; overlapping interface
or duplicate app counters are rejected. Their credentials and the Cloudflare
tunnel token are restricted host files, excluded from source control. Restore those credentials and the data before
using `install.sh`; the `cloudflared` ARM64 executable is installed at
`/usr/local/bin/cloudflared`.

Traffic tables show decimal MB to six decimal places (one-byte precision).
A partial window means observations are missing; the approximation marker means
bytes were apportioned across a time boundary. Repocket's credited and pending
account totals come directly from its reporting API, separately from measured
process traffic. The latest provider capture is a dated connected-browser
snapshot; it must not be presented as a continuously collected provider feed.
