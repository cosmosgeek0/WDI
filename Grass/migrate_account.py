"""Move a legacy account session to Secrets Manager before sanitizing active device records.

Run on the trusted controller with its service stopped. Default is read-only;
--apply requires operator Secrets Manager write access, not the runtime role.
"""
import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

import boto3
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from grass import ACCOUNT_SECRET, REGION, SECRET, verify_account
from session import IDENTITY_KEYS, decoded, identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=Path('/var/lib/wdi/grass.sqlite3'))
    parser.add_argument('--secret', default=ACCOUNT_SECRET)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    secrets = boto3.client('secretsmanager', region_name=REGION)
    ciphers = {}

    def cipher(version):
        if version not in ciphers:
            access = json.loads(secrets.get_secret_value(SecretId=SECRET, VersionId=version)['SecretString'])
            key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                       info=b'WDI Grass installation store v1').derive(access['ssh_private_key'].encode())
            ciphers[version] = AESGCM(key)
        return ciphers[version]

    with args.database.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        db = sqlite3.connect('file:'+str(args.database.resolve())+'?mode='+('rw' if args.apply else 'ro'), uri=True)
        records, sessions, accounts = [], [], set()
        for profile, account, version, packed in db.execute(
                'SELECT profile,account,version,ciphertext FROM installations'):
            value = json.loads(cipher(version).decrypt(packed[:12], packed[12:], f'{account}/{profile}'.encode()))
            before = identity(value['store'], pending=True)
            safe = dict(value, store={k:value['store'][k] for k in (*IDENTITY_KEYS,'wdi:kms_key','wdi:private_sha256') if k in value['store']})
            if identity(safe['store'], pending=True) != before:
                raise ValueError('migration_would_change_device_identity')
            accounts.add(account)
            if decoded(value['store'].get('accessToken')):
                sessions.append(dict(account_id=account, access_token=decoded(value['store']['accessToken']),
                                     refresh_token=decoded(value['store'].get('refreshToken'))))
            if value != safe:
                records.append((profile, account, version, packed, safe))
        legacy = args.database.with_name('account-session.enc')
        if legacy.exists():
            envelope = json.loads(legacy.read_bytes())
            packed = base64.b64decode(envelope['ciphertext'], validate=True)
            store = json.loads(cipher(envelope['version']).decrypt(
                packed[:12], packed[12:], b'wdi-grass-account-session-v1'))
            sessions.append(dict(account_id=decoded(store['wynd:user_id']),
                                 access_token=decoded(store['accessToken']),
                                 refresh_token=decoded(store['refreshToken'])))
            accounts.add(sessions[-1]['account_id'])
        if len(accounts) != 1:
            raise ValueError('one_account_required_for_this_secret')
        account = next(iter(accounts))
        try:
            reply = secrets.get_secret_value(SecretId=args.secret)
            session = json.loads(reply['SecretString'])
        except ClientError as error:
            if error.response['Error']['Code'] != 'ResourceNotFoundException':
                raise
            unique = {json.dumps(s, sort_keys=True) for s in sessions}
            if len(unique) != 1:
                raise ValueError('choose_and_verify_one_account_session_in_the_secret_first') from None
            session = json.loads(unique.pop())
            reply = None
        if (session.get('account_id') != account or not all(
                isinstance(session.get(k),str) and session[k] for k in ('access_token','refresh_token'))):
            raise ValueError('account_secret_identity_mismatch')
        verify_account(session)  # Read-only; never refresh or revoke a token.
        if not args.apply:
            print(json.dumps(dict(verified=True, secret_exists=reply is not None,
                                  records_to_sanitize=len(records), legacy_session_present=legacy.exists())))
            return
        if reply is None:
            reply = secrets.create_secret(Name=args.secret,
                Description='Grass account session; trusted controller only; never deploy to workers',
                SecretString=json.dumps(session, separators=(',', ':')))
        verified = secrets.get_secret_value(SecretId=args.secret, VersionId=reply['VersionId'])
        if json.loads(verified['SecretString']) != session:
            raise ValueError('account_secret_readback_mismatch')
        verify_account(json.loads(verified['SecretString']))
        if not records and not legacy.exists():
            print(json.dumps(dict(migrated=0, secret_version=verified['VersionId'])))
            return
        recovery = args.database.parent/('before-account-secret-'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime()))
        recovery.mkdir(mode=0o700)
        backup = recovery/args.database.name
        with sqlite3.connect(backup) as snapshot:
            db.backup(snapshot)
            if snapshot.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('recovery_snapshot_invalid')
        with db:
            for profile, account, version, previous, safe in records:
                nonce = os.urandom(12)
                packed = nonce+cipher(version).encrypt(nonce,
                    json.dumps(safe,separators=(',', ':')).encode(), f'{account}/{profile}'.encode())
                changed = db.execute('UPDATE installations SET ciphertext=? WHERE profile=? AND ciphertext=?',
                                     (packed, profile, previous))
                if changed.rowcount != 1:
                    raise ValueError('installation_changed_during_migration')
                stored = db.execute('SELECT ciphertext FROM installations WHERE profile=?', (profile,)).fetchone()[0]
                restored = json.loads(cipher(version).decrypt(stored[:12], stored[12:], f'{account}/{profile}'.encode()))
                if restored != safe:
                    raise ValueError('device_record_readback_mismatch')
        if legacy.exists():
            legacy.rename(recovery/legacy.name)
        print(json.dumps(dict(migrated=len(records), secret_version=verified['VersionId'],
                              recovery=str(recovery), backup_sha256=hashlib.sha256(backup.read_bytes()).hexdigest())))


if __name__ == '__main__':
    main()
