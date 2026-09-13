"""Pass the encrypted account credential to an explicit child command, never stdout."""
import base64
import json
import os
import sys
from pathlib import Path

import boto3
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

if len(sys.argv) < 2:
    raise SystemExit("usage: python3 account.py --check | COMMAND [ARG ...]")

saved = json.loads(Path(__file__).with_name("state").joinpath("account.enc").read_text())
purpose = b"wdi/repocket/account-v1"
if (saved["format"], saved["secret_id"], saved["purpose"]) != (1, "wdi/salad-access", purpose.decode()):
    raise SystemExit("Unexpected credential snapshot format")
reply = boto3.client("secretsmanager", region_name="ap-south-1").get_secret_value(
    SecretId=saved["secret_id"], VersionId=saved["secret_version"])
private_key = json.loads(reply["SecretString"])["ssh_private_key"].encode()
key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=purpose).derive(private_key)
account = json.loads(AESGCM(key).decrypt(base64.b64decode(saved["nonce"], validate=True),
                                       base64.b64decode(saved["ciphertext"], validate=True), purpose))
if not isinstance(account.get("RP_API_KEY"), str) or not account["RP_API_KEY"]:
    raise SystemExit("Credential snapshot has no Repocket API key")
if sys.argv[1:] == ["--check"]:
    print("Encrypted Repocket credential is readable.")
else:
    os.execvpe(sys.argv[1], sys.argv[1:], {**os.environ, "RP_API_KEY": account["RP_API_KEY"]})
