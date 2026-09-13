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
selects `cosmosgeeketh/default/repocket-eval`, pinned by the retained deployment
metadata. Its cursor is named by group UUID; the original `cursor.json` and old
deployment snapshot are retained for the historical group.

Current app traffic stays unavailable until an attributed producer is verified.
Neither the current Grass `eth0` sample nor the native Repocket connection logs
supply separate app byte measurements. The collectors preserve original records
and require explicit application attribution before adding current app traffic
to the graph. Old groups remain accessible in history with their original
container measurement scope. Provider credits are independent account readings.

The Grass reader accepts the existing `GRASS_SAMPLE_V2` envelope with cumulative
`rx`/`tx` bytes only when `traffic_scope` is `application`, `application` is `grass`,
and `counter_id` identifies the same measured app-counter lifetime at both
endpoints. Missing attribution, counter changes, resets, clock changes and IP
changes produce gaps. Current group requirements are stored in the existing
traffic database's `measurement_groups` table.

The Repocket reader accepts its existing `RP_MON` minute envelope only when
`traffic_scope` is `application` and `application` is `repocket`; `rx_bytes` and
`tx_bytes` are interval bytes. The existing identity, sequence, interval and
conflict checks still apply. These are reader contracts, not evidence that the
packaged clients emit attributed measurements.

Keep the original snapshot checksum manifest on Mumbai at
`/var/lib/wdi-website-migration/source-manifest.json`. Before any public cutover,
catch up the old host's still-active history writers and reconcile collection
ownership; the initial migration snapshot is not an ongoing mirror.
