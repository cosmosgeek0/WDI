# Grass + Repocket

Official Grass Desktop 7.6.1 and Repocket 1.2.0 in one Linux AMD64 container.
The Mumbai controller preserves Grass installation identities across replacements.

## Image and configuration

- `configuration/salad-group.json` is the combined pilot configuration:
  image reference and digest, group, account ID, resources and runtime settings.
  Credentials are replaced with recovery references. This provider response
  snapshot is not an API update request.
- `configuration/grass.env` and `grass.service` are the installed controller settings.
- The original standalone release remains in
  `images/grass-desktop-fleet-20260913-4c2ccc3cff1b.tar`, with its source in
  `images/standalone-source.tar.gz` and configuration in
  `configuration/standalone-salad-group.json`.
- `images/base-7.6.1-v3.tar` is the pinned input needed to rebuild it.
- `state/grass.sqlite3` and `state/account-session.enc` are encrypted recovery
  snapshots. The live database on Mumbai remains authoritative.
- `evidence/` contains the original live test records with their timestamps.

The container implementation is `session.py`, `supervisor.py`, `worker.pl` and
`worker.sha256`, with `ip_policy.py` handling the IP check inside the existing
supervisor. The controller is `grass.py`, which also uses `session.py`.
Repocket supplies its filtered launcher and the shared measurement runner from
`../Repocket/traffic/`. Each app has a separate traced process tree and counter.

## Run and recover

The existing controller runs as `grass.service` from `/opt/wdi` on Mumbai
(`i-0c2e48a7ddc2dd010`, `ap-south-1`). Through AWS SSM, run
`python3 /opt/wdi/grass.py --status` to see its last observed allocations, IPs,
device IDs, provider count and data age. Moving this local folder does not change
the deployed paths. `GRASS_GROUP` in `/etc/wdi/grass.env` selects managed groups.

The Salad group supplies `GRASS_ACCOUNT_ID` and the runtime Repocket credential.
The supervisor removes that credential from its environment and passes it only
to Repocket. Its encrypted recovery copy is `../Repocket/state/account.enc`.
The controller restores the Grass
login and saved installation over verified SSH after checking identity and IP
ownership. It reads Salad API/SSH credentials from `wdi/salad-access` through
the `mumbai-clean` instance role. ECR pull tokens are supplied at deployment time;
`ecr-pull-policy.json` records the scoped read permission.

Keep `/var/lib/wdi/grass.sqlite3` when changing containers. Account tokens are in
`/var/lib/wdi/account-session.enc`; profile credentials are encrypted in the
database. Recovery requires the recorded AWS Secrets Manager SSH-key versions.
The packaged snapshots contain no decryption keys and must not overwrite an
active controller database. Keep `state/` and `images/` out of Git.

Each installation and observed IP has one admitted owner. Permission expires
within five minutes without renewal, pausing Grass locally. Uncertain first
registration is held for recovery, not repeated. Delayed commands cannot undo
a newer hold. The worker permits two local app recovery attempts per failure
episode; five healthy minutes reset the budget. Exhaustion preserves the
allocation. Salad itself can still replace a host or restart a group on updates.

The supervisor checks its public IP before admission and every 30 seconds.
IPQuery's explicit `risk.is_datacenter=true` holds both apps and requests its own
reallocation through Salad IMDS. A failed or unknown lookup holds locally. False
means the supplier did not detect a datacenter; it does not prove residential.
`/config/run/ip-policy.json` records the source, address, timing and decision.

Grass reads `/config/run/grass-traffic.json` and emits `GRASS_SAMPLE_V2` records;
Repocket reads the same IP policy and emits `RP_MON` intervals. Both count their
own successful TCP/UDP socket I/O, including descendants and short connections.
These are socket payload bytes, including TLS framing and local TCP/UDP, not
wire bytes or provider-credited traffic. Shared interface bytes stay separate.
Missing, stale, reset or misattributed counters produce unavailable intervals.
The runner's shutdown kills and reaps its traced processes within the supervisor's
stop deadline. Normal tracing stops do not trigger Grass recovery.

`--initialize N` prepares a total pool of N profiles from the encrypted account
session, subject to the provider cap. It does not register them. Stop the
controller before changing the pool, then start it again; preserve existing keys.

## Load, build and verify

Run these commands from this folder as needed:

```sh
# Load the preserved standalone artifact; its saved tag is wdi-grass:candidate.
docker load --input images/grass-desktop-fleet-20260913-4c2ccc3cff1b.tar

# Build the combined image from the saved Grass and Repocket inputs.
./build.sh images/base-7.6.1-v3.tar

# Check saved file integrity.
shasum -a 256 -c CHECKSUMS.sha256
```

The combined build tag is `wdi-grass:combined`; rebuilding may change its digest.
Loading an image supplies no credentials or launch permission. Runtime admission
still requires the controller. On Linux, `python3 test_fleet.py` runs the existing
synthetic checks; it needs boto3, Paramiko, cryptography and OpenSSL.

The saved standalone release passed 100 synthetic allocations and live tests on one replica.
Replacement and pause/resume retained device `DV01M2BK8GZMEFV96GA1S9K` and provider
count 451/5000. Provider reporting lagged the native restart by several minutes.
These are historical results; 100 live replicas and credited earnings were not
verified. Original records remain in `evidence/` and `/var/lib/wdi` on Mumbai.

The combined version-8 pilot was verified on one replica with both apps connected.
Grass retained the same device and count 451. Both meters returned valid distinct
counters, and the new-Mumbai website stored valid intervals for each app under
the same container instance. The final `combined_pilot_verified` evidence includes
source hashes and measurements. No 10/50-replica scaling or public cutover was done.
