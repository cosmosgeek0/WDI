# Repocket in WDI

The pinned Repocket client runs beside Grass in the combined image documented in
[Grass/README.md](../Grass/README.md). Its source archive and original build inputs
remain here so the launcher can be reproduced.

Repocket runs as UID 1001 with a private home, environment and files. Its API key
remains in the Salad group environment; this is the owner's accepted trade-off.
Grass account credentials and permanent device keys are not supplied to it.

`traffic/traffic.py` wraps its process tree with the pinned static tracer and
reports successful TCP/UDP socket-I/O payload bytes, including protocol traffic.
It does not duplicate container-wide counters or equate bytes with earnings.
Application output is filtered by the launcher; bounded logging prevents stdout
backpressure from stalling the tracer. The combined supervisor retries failures
with backoff while preserving the other application.

`state/account.enc` is encrypted recovery data. Its recorded Secrets Manager
version is required for decryption on the trusted AWS host. Do not paste or save
the decrypted value in source or shell arguments. The current group binding is
in `Grass/configuration/repocket-deployment.json`; historical website rows and
per-group cursors remain on Mumbai.
