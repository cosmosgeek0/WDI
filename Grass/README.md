# Grass and Repocket runtime

Grass Desktop 7.6.1 and the pinned Repocket client share one Salad container.
Their process trees, directories, credentials and socket-byte meters are
separate. The controller runs on the existing Mumbai host.

The selected configuration is `wdi-us` / **WDI US**, one CPU, 1024 MB RAM,
64 MB shared memory, batch priority and `country_codes: ["us"]`. These are
Salad's documented resource minimums. The previous live allocation used about
467 MB of memory and zero bytes in `/dev/shm`; 2048/256 MB were inherited limits,
not measured requirements. Capacity and allocation delays remain controlled by
Salad. Account quota currently permits 100 replicas; 200 needs a quota increase.

The published image is:

```text
163092837436.dkr.ecr.ap-south-1.amazonaws.com/grass-desktop@sha256:9ef067c61a3848c08f46ce29fa89413cd4d17c72a5ee92e2cf64b6fa82cc2b55
```

Its archive is `images/runtime-20260913-9ef067c61a38.tar`. `recovery.json` records
the checkpoint's actual counts, source hashes and runtime identities; it is not
a claim that every requested replica has been allocated or earned credit.

## Credentials and authentication

| Location | Material present |
| --- | --- |
| Secrets Manager `wdi/grass-account` | Account ID, access token and refresh-token value |
| AWS KMS | Permanent Ed25519 device keys |
| Mumbai memory | Account token and returned KMS signatures |
| Salad relay | TLS ciphertext, then a short-lived director admission token |
| Grass, UID 1000 | Public device/account/browser IDs and an unregistered allocation-local key |
| Repocket, UID 1001 | Its own API key and private files |

New device keys are generated inside KMS. The original device key was imported
and its public key and signature verified. Its active encrypted record now holds
a KMS reference; prior encrypted recovery copies remain quarantined on Mumbai.
Public IDs and key fingerprints are identifiers, not login credentials.

A pinned patch changes the native desktop's director check-in URL to the local
broker. It does not change the Rust bandwidth transport. The broker admits only
the Grass UID and verifies its local signature. Mumbai constructs the permitted
check-in fields, calls KMS Sign, and terminates TLS directly with
`director.getgrass.io` through the worker's outbound TCP relay. The worker cannot
read the reusable signature. TLS certificates and hostname are verified before
the authorization header is sent.

Only the director's short-lived token and bridge address return to the worker.
The controller checks issuer, IP audience and expiration. The observed token
lifetime was ten seconds; a token may be reused during its validity, and expiry
is not claimed to terminate an established node connection. No data-path proxy
or bandwidth cap was added. The five-second signing minimum limits authentication
abuse, not native transfer speed.

Both apps run without capabilities or future privilege elevation. Separate
UIDs, private directories, an X11 cookie and root-owned control files prevent
ordinary cross-app access. Reads of process names handle arbitrary bytes and
check ownership. Root writes reject directory symlinks. A host administrator
still controls its machine and can disrupt service or take short-lived node
tokens; the controller and its AWS account remain trusted components.

**Historical account exposure is not resolved.** Both known refresh tokens were
revoked and rejected on reuse, but the older account access token still returned
HTTP 200 afterward. Its declared expiry is **2027-09-06 18:24:04 UTC**. A password
change must be followed by a rejection test; it cannot be assumed to invalidate
old access. Grass may need to revoke it server-side. No account password, wallet
passkey, email OTP or wallet private key was changed or exported, and no funds
were moved. The secret's refresh-token value is revoked; runtime telemetry uses
its still-valid access token and does not automatically refresh it.

Keep wallet recovery material in a different secret that this role cannot read.
The runtime policy in `configuration/account-signing-policy.json` permits one
account-secret read and Ed25519 signing only with this account's tagged node keys.
Temporary provisioning and publishing permissions were removed after preparation.
They are not required for steady-state workers. Earlier device-key or account-token copies are not revoked by KMS import.

## Control, recovery and scale

`grass.py` owns identities, allocation assignments, unique account/IP leases and
ordered permissions. Its verified SSH connections preserve machine fingerprints.
A replacement reuses an available saved identity after the old lease expires;
it never fabricates a replacement for an uncertain registration.

`--initialize COUNT` explicitly prepares the target number of identities.
Creation intent and returned KMS ARNs are persisted. The signed registration
matches the pinned native client's `GRASS-REGISTER-v1` protocol. Successful raw
replies are encrypted and committed before parsing. An unattempted request or a
saved successful reply can resume; an uncertain mutation is reported rather
than silently replaced. Only registered KMS profiles are eligible for allocation.
Provisioning uses a separate lock and does not stop earning workers.

Cloudflare rate-limited the initial registration burst on 2026-09-13. The shared
`/var/lib/wdi/grass-api-backoff.json` honors its Retry-After across account API
calls, including restarts. Registration is paced and reuses the same key after
an explicit HTTP 429. `wdi-node-provision.service` completed the rollout's 100-identity preparation at
12:06 UTC on 2026-09-13. All 100 are registered; the owner controls the replica
count in Salad. No additional initialization command is needed for 10 or 100.

Existing lease renewal is carried in inspection requests. Provider lookups run
outside the permission path, so missing account telemetry does not stop native
traffic. Missing, stale and failed provider observations remain unavailable.
The private, credential-free `/run/wdi/grass-account-observation.json` supplies
account counts and original epoch fields to the website's `salad` user, with
source timestamps preserved. The website never receives Grass account tokens.

Native failure recovery remains bounded and visible. Repocket retries with
backoff; a display or watchdog failure does not permanently stop the other app.
Restart commands identify the observed process, and termination includes
surviving process-group descendants. Log queues are bounded so a blocked log
sink does not stop tracing or recovery. Dropped logs are counted.

The existing IP policy checks public IP periodically, caches classification of
an unchanged IP, holds on unknown classification and reallocates confirmed
datacenter addresses. Definitive reallocation rejection can recover after an IP
change; uncertain submitted moves remain held. A false datacenter flag is not
proof of a residential connection. IP lookup interruptions and strict admission
checks can temporarily pause work; no zero-downtime guarantee is claimed.

`GRASS_SAMPLE_V2` and `RP_MON` count successful application process-tree socket
I/O, including protocol bytes. They are not shared `eth0` counters, physical wire
bytes, provider-delivered data or a promise of paid demand. Historical group IDs,
cursors, counters and provider identities retain their original meaning.

## Build and recovery

Build with the complete Git LFS archives available:

```sh
./Grass/build.sh Grass/images/base-7.6.1-v3.tar \
  Repocket/images/repocket-1.2.0-filtered-45cd81b05271.tar
```

The output is `wdi-grass:node-auth`. The build validates the pinned ELF, embedded
JS asset and static tracer before changing the single director URL. For exact
recovery, load the published archive instead of rebuilding:

```sh
docker load -i Grass/images/runtime-20260913-9ef067c61a38.tar
```

AWS account `163092837436`, region `ap-south-1`, host `i-0c2e48a7ddc2dd010`,
SSH alias `mumbai`, role `mumbai-clean`. Use unprivileged SSH for inspection and
AWS SSM `AWS-RunShellScript`, CLI profile `default`, for authorized root work.

Keep `wdi/salad-access` and every version listed in `recovery.json`: its SSH-key
material derives the encrypted checkpoint key. Keep the separate account secret
and all referenced KMS keys. The account-secret schema is:

```json
{"account_id":"ACCOUNT_ID","access_token":"ACCESS_TOKEN","refresh_token":"REFRESH_TOKEN"}
```

Populate it through an authorized Secrets Manager workflow on the trusted host;
never put values in source, shell arguments, logs or Salad configuration.

On a replacement trusted host, install Python, boto3, paramiko, cryptography and
OpenSSL. Stop the existing controller before restoring the saved database to
`/var/lib/wdi/grass.sqlite3`; keep that directory 0700 and its files 0600, owned by
root. Use SQLite's backup API for live snapshots, retain the encryption versions,
and check `PRAGMA quick_check`. Do not restore over a running writer or start two
allocation controllers.

Install `grass.py`, `session.py`, `kms_nodes.py` and `migrate_account.py` in
`/opt/wdi`; install `grass.service` and `configuration/grass.env`. The `salad`
group is required for the private website observation. Apply the scoped runtime
IAM policy to the replacement host and preserve the KMS key tags and policies.
Then reload systemd and enable/start `grass.service`.

For a historical checkpoint containing account tokens, stop the controller and
run `migrate_account.py` first in preview mode, then with `--apply`. For historical
private device keys, run `kms_nodes.py` with the required temporary import rights
while the controller is stopped. Current checkpoints already contain KMS
references. Do not deploy the historical combined image with an account session.

A new Salad group needs fresh private-registry authentication and the two runtime
environment values shown in `configuration/salad-group.json`; recover the
Repocket value from its encrypted checkpoint on AWS. Salad copies a prepared
image into its own storage, so existing groups distribute that retained image to
new nodes. A private image-source update requires fresh registry authentication.

## Verification and limits

Thirty-four independent, bounded reviews were completed, followed by critiques
of their repairs. Tests covered node-only serialization, registration retry and
saved-reply recovery, KMS signatures, the full TLS broker path, wrong-host
certificate rejection and cross-UID admission. A native Linux meter test sent and
received exactly 8,388,608 bytes while stdout was deliberately blocked; traffic
completed and dropped logs were counted. The final image's source hashes match
the reviewed checkout. The live U.S. allocation preserved device `DV01M2BK8GZMEFV96GA1S9K`,
with IP `68.12.46.69`, provider confirmation and score 100. Both native app meters
were valid and increasing; neither app had a runtime error. Live identities and
checkpoint observations are recorded in `recovery.json`.

This evidence does not certify every provider endpoint, eliminate host compromise,
guarantee paid traffic or resolve the historical access-token exposure.

References: [Salad resources](https://docs.salad.com/reference/saladcloud-api/container-groups/create-container-group),
[Salad image distribution](https://docs.salad.com/container-engine/explanation/infrastructure-platform/container-registries),
[AWS KMS signing](https://docs.aws.amazon.com/kms/latest/APIReference/API_Sign.html),
[Secrets Manager guidance](https://docs.aws.amazon.com/secretsmanager/latest/userguide/best-practices.html).
