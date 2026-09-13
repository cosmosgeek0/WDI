# Repocket

Native Repocket 1.2.0 / SDK 2.0.1, plus the credential-log filter needed by this
release. `traffic/` contains the per-app byte producer used by the combined
Grass image. The combined build and app admission belong to `../Grass`.

## Use the saved standalone image

```sh
docker load --input images/repocket-1.2.0-filtered-45cd81b05271.tar
```

Loaded tag: `wdi-repocket:1.2.0-filtered-45cd81b05271`.
The registry digest, build inputs, archive hashes and completed checks are in
`baseline.json`. `./build.sh` rebuilds from `images/build-inputs-amd64.tar` into
`wdi-repocket:rebuild`; build metadata may change its digest. The saved archive
is the exact tested release. Check it with `shasum -a 256 -c CHECKSUMS.sha256`.

## Credentials and identity

Only `RP_API_KEY` is required. The upstream client prints it as SDK `user_id`,
even with `RUST_LOG=off`; `start.sh` filters both output streams before logging.

`state/account.enc` stores the existing key encrypted with AES-256-GCM and
HKDF-SHA256, using the recorded version of the AWS `wdi/salad-access` SSH key.
Keep that AWS secret version for recovery. No plaintext secret or decryption
key is packaged. Images and encrypted state are excluded from Git/build context.

On an authorized host with AWS secret access, Python 3, boto3 and cryptography:

```sh
python3 account.py --check
```

For an authorized Linux AMD64 Docker target with no other Repocket on its IP:

```sh
python3 account.py docker run --name repocket --env RP_API_KEY wdi-repocket:1.2.0-filtered-45cd81b05271
```

The helper passes the key through the child environment. The saved state restores
account access; the observed Salad move produced a new provider device ID.

## Operation and integration

The standalone helper on Mumbai is `/opt/wdi/integrations/repocket/salad.py`.
Run it through AWS SSM with `status`, `logs` or `stop`; it targets only
`repocket-eval`. Its stored configuration and stop-timer snapshots are in
`configuration/`, with original source timestamps. The private ECR pull token
expires after 12 hours and must be refreshed for a later pull when required.

`../Grass/build.sh` consumes the saved image and `traffic/` producer. It copies
only the native binary, filtered launcher and BusyBox into the Grass image.
The client requires glibc >= 2.28, libgcc, standard C libraries and CA certificates.
See [traffic/README.md](traffic/README.md) for the byte-source contract.

Historical pilot readings, IDs and operation events remain in `evidence/`.
The initial sample displayed 0 B / $0; it establishes neither profitability nor
Windows/Docker earning equality. Windows/Wine was not tested. The owner's
$1.38/day for ten devices remains an unverified total.
