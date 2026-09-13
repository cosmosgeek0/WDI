# WDI

Combined Grass Desktop and Repocket runtime for Salad, with device signing in
AWS KMS and account credentials confined to the trusted Mumbai controller.
[Grass/README.md](Grass/README.md) describes deployment, recovery and the security
boundary. The existing Honeygain, Bytelixir and website folders are not part of
this GitHub package.

The current image is
`sha256:9ef067c61a3848c08f46ce29fa89413cd4d17c72a5ee92e2cf64b6fa82cc2b55`.
Its complete archive is
`Grass/images/runtime-20260913-9ef067c61a38.tar`. The source archives under
`Grass/images/` and `Repocket/images/` permit rebuilding the pinned clients.

Images use Git LFS. GitHub's ZIP download is configured to include LFS objects.
For a clone, run `git lfs install`, clone this repository, then `git lfs pull`.
A clone containing only LFS pointer files is incomplete. Verify the package:

```sh
(cd Grass && shasum -a 256 -c CHECKSUMS.sha256)
(cd Repocket && shasum -a 256 -c CHECKSUMS.sha256)
```

The encrypted checkpoint and its capture time, identities, AWS dependencies and
hashes are recorded in `Grass/recovery.json`. Preserve the listed Secrets Manager
versions and KMS keys. The new private device keys are not exportable: deleting
the KMS keys cannot be repaired by this ZIP. Provider earnings remain with the
provider; a replacement Salad machine obtains a new runtime and public IP.

Historical encrypted recovery material is retained separately. Neither deleting
local files nor moving keys into KMS revokes a previously copied account token.
The old access-token exposure remains unresolved; see the security notes below
before treating the account itself as fully secured.
