# Repocket component of the current combined runtime

The combined container runs Repocket 1.2.0 / SDK 2.0.1 alongside Grass. Deployment
and complete recovery commands are in [Grass/README.md](../Grass/README.md).

`start.sh` is the native launcher and credential-log filter. `traffic/traffic.py`
and its pinned static strace binary measure this app's process tree; Grass uses a
separate instance of the same meter. Neither meter treats shared container bytes
or provider account credits as application traffic.

`images/repocket-1.2.0-filtered-45cd81b05271.tar` is the exact build input consumed
by `Grass/build.sh`: native binary, filtered launcher and BusyBox. It is retained
because the combined Dockerfile copies those files from it. Its manifest digest
is `sha256:45cd81b05271de652259fdbed044505b41fe770755192c4b99eb370c30df0647`.
`images/build-inputs-amd64.tar`, `Dockerfile` and `build.sh` reproduce this input
from the pinned upstream images. They contain no account credential.

```sh
cd Repocket
shasum -a 256 -c CHECKSUMS.sha256
./build.sh images/build-inputs-amd64.tar
```

`state/account.enc` contains the current account key encrypted with AES-256-GCM
and HKDF-SHA256 using the recorded AWS `wdi/salad-access` SSH-key version. On an
AWS host with that secret access, boto3 and cryptography:

```sh
python3 account.py --check
```

For deployment, `account.py COMMAND ...` decrypts the key and supplies `RP_API_KEY`
in the child environment; it does not print it. The combined supervisor passes
the key only to the Repocket launcher. Keep the AWS secret version: a new key
cannot decrypt an existing backup. No old Salad pilot helper, configuration,
earning observations, or historical test files are needed or included here.

[traffic/README.md](traffic/README.md) defines the byte-source contract and how to
rebuild the pinned tracer. Its license is included next to the binary.
