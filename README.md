# WDI — current Grass + Repocket runtime

This package reproduces the combined container currently deployed on Salad and
restores its saved Grass installation identity. Start with
[Grass/README.md](Grass/README.md) for the complete AWS, image, controller, and
Salad startup commands. No previous standalone Grass release or historical test
reports are included. Honeygain, Bytelixir, the website, and AGENTS.md are excluded
from this GitHub package.

## Required files

| Keep | Reason |
| --- | --- |
| `Grass/images/grass-repocket-combined-20260913-1b52f43837f6.tar` | Exact deployed combined image; load it to avoid a rebuild changing the digest. |
| `Grass/images/base-7.6.1-v3.tar` | Pinned Grass binary, Linux libraries and base filesystem consumed by the current Dockerfile. |
| `Repocket/images/repocket-1.2.0-filtered-45cd81b05271.tar` | Pinned Repocket binary/launcher and BusyBox consumed by the combined build. |
| `Repocket/images/build-inputs-amd64.tar` | Exact upstream inputs needed to rebuild that Repocket launcher image from its Dockerfile. |
| Grass and Repocket source/build files | The controller, container startup, IP policy, native launcher and separate app byte meters. |
| `Grass/state/grass.sqlite3` | Current encrypted Grass installation keys; preserves the existing provider device identity. |
| `Grass/state/account-session.enc`, `Repocket/state/account.enc` | Encrypted credentials needed to restore account access. |
| `Grass/configuration/`, `Grass/recovery.json`, checksum files | Current launch settings, required AWS secret version, checkpoint time and integrity checks. |

The large archives are assets of the private
[recovery-20260913 release](https://github.com/cosmosgeek0/WDI/releases/tag/recovery-20260913).
Download all four `.tar` assets while signed into GitHub and place them at the
paths above. The local WDI folder already contains them. GitHub's normal source
ZIP alone does not contain release assets; source plus these four assets is the
complete recovery package. Encrypted state and the static tracer are committed.

From WDI, check the complete download:

```sh
(cd Grass && shasum -a 256 -c CHECKSUMS.sha256)
(cd Repocket && shasum -a 256 -c CHECKSUMS.sha256)
```

On Linux, `sha256sum -c CHECKSUMS.sha256` is equivalent.

## Recovery boundary

The saved checkpoint was read from the live controller and its encrypted Grass
profile and Repocket credential were decrypted successfully using the recorded
AWS secret version. The Repocket credential also matched the live Salad group.
The combined archive manifest matches the deployed digest exactly.

**AWS access is still required.** Keep Secrets Manager `wdi/salad-access`, including
the version recorded in `Grass/recovery.json`, in account `163092837436`, region
`ap-south-1`. No plaintext Salad API key or SSH private key is stored here. If that
AWS secret is deleted too, this encrypted backup cannot restore those identities.

Recovery preserves this software/configuration and the saved Grass device keys.
A destroyed Salad allocation gets a new instance, machine and public IP;
Repocket may get a new provider device ID. Provider-side earnings remain at the
provider. The saved state is a dated checkpoint, not a continuously updated backup.
