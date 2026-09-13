"""Run permanent Grass installations across disposable Salad allocations.

Run only on Mumbai, where the instance role can read wdi/salad-access.
No private keys or session stores are sent to the operator's terminal.
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import fcntl
import hashlib
import io
import ipaddress
import json
import os
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import boto3
import paramiko
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from session import IDENTITY_KEYS, LEASE_SECONDS, compatible, decoded, identity

REGION = 'ap-south-1'
SECRET = 'wdi/salad-access'
API = 'https://api.salad.com/api/public/organizations/cosmosgeeketh/projects/default/containers'
SOURCE_SHA = hashlib.sha256(Path(__file__).with_name('session.py').read_bytes()).hexdigest()


def lease_clock():
    return time.clock_gettime(getattr(time, 'CLOCK_BOOTTIME', time.CLOCK_MONOTONIC))


def emit(event, **values):
    print(json.dumps(dict(event=event, observed_ms=int(time.time()*1000), **values)), flush=True)


def request(url, headers, payload=None):
    headers = dict(headers, **{'User-Agent': 'WDI/1.0', 'Accept': 'application/json'})
    data = None
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(payload).encode()
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers, data=data), timeout=15) as response:
        return json.load(response)


class Vault:
    def __init__(self, path):
        self.path = path
        self.accounts = {}
        self.secrets = boto3.client('secretsmanager', region_name=REGION)
        self.versions = {}
        reply = self.secrets.get_secret_value(SecretId=SECRET)
        self.version = reply['VersionId']
        self.access = json.loads(reply['SecretString'])
        self.versions[self.version] = self.access
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS installations '
                        '(profile TEXT PRIMARY KEY, device TEXT UNIQUE, account TEXT NOT NULL, '
                        'instance TEXT UNIQUE, group_id TEXT, machine TEXT, '
                        'version TEXT NOT NULL, ciphertext BLOB NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS transports '
                        '(machine TEXT PRIMARY KEY, ip TEXT NOT NULL, port INTEGER NOT NULL, '
                        'fingerprint TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS leases '
                        '(instance TEXT PRIMARY KEY, profile TEXT UNIQUE NOT NULL, '
                        'account TEXT NOT NULL, ip TEXT NOT NULL, runtime TEXT NOT NULL, '
                        'until_ms INTEGER NOT NULL, boot_time REAL NOT NULL, '
                        'observed_ms INTEGER NOT NULL DEFAULT 0, UNIQUE(account,ip))')
        if 'observed_ms' not in {r[1] for r in self.db.execute('PRAGMA table_info(leases)')}:
            self.db.execute('ALTER TABLE leases ADD COLUMN observed_ms INTEGER NOT NULL DEFAULT 0')
        with self.db:
            columns = {r[1] for r in self.db.execute('PRAGMA table_info(leases)')}
            for column in ('until_tick', 'observed_tick'):
                if column not in columns:
                    self.db.execute(f'ALTER TABLE leases ADD COLUMN {column} REAL NOT NULL DEFAULT 0')
            self.db.execute('CREATE TABLE IF NOT EXISTS controller '
                            '(id INTEGER PRIMARY KEY CHECK(id=1), sequence INTEGER NOT NULL, boot TEXT NOT NULL)')
            self.db.execute("INSERT OR IGNORE INTO controller VALUES (1,0,'')")
            boot_path = Path('/proc/sys/kernel/random/boot_id')
            boot = (boot_path.read_text().strip() if boot_path.exists() else
                    subprocess.check_output(['sysctl', '-n', 'kern.boottime'], text=True).strip())
            if self.db.execute('SELECT boot FROM controller WHERE id=1').fetchone()[0] != boot:
                # Boot-clock values survive controller restarts, but not host
                # reboots. Fence every possibly issued old lease for one full
                # lifetime only when its persisted host boot epoch changes.
                self.db.execute('UPDATE leases SET until_tick=?,observed_tick=0 WHERE until_ms>0',
                                (lease_clock()+LEASE_SECONDS+50,))
                self.db.execute('UPDATE controller SET boot=? WHERE id=1', (boot,))
        if 'enrollment_started' not in {r[1] for r in self.db.execute('PRAGMA table_info(installations)')}:
            with self.db:
                self.db.execute('ALTER TABLE installations ADD COLUMN enrollment_started INTEGER NOT NULL DEFAULT 0')
                # Old assigned pending profiles may already have reached Grass.
                # A missing reply or device ID is never evidence of no enrollment.
                self.db.execute('UPDATE installations SET enrollment_started=1 '
                                'WHERE device IS NULL AND (instance IS NOT NULL OR '
                                'profile IN (SELECT profile FROM leases))')

    def occupied(self, allocated_ids):
        now = lease_clock()
        with self.db:
            for instance, until in self.db.execute('SELECT instance,until_tick FROM leases').fetchall():
                if instance not in allocated_ids and until <= now:
                    self.db.execute('DELETE FROM leases WHERE instance=? AND until_tick<=?', (instance, now))
        return allocated_ids | {r[0] for r in self.db.execute('SELECT instance FROM leases')}

    def admit(self, scope, profile, account, ip, boot_time, observed_ms, observed_tick=None):
        ip = str(ipaddress.ip_address(ip))
        try:
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                previous = self.db.execute('SELECT profile,account,ip,runtime,until_tick FROM leases '
                                           'WHERE instance=?', (scope['instance'],)).fetchone()
                if (previous and previous[:4] != (profile, account, ip, scope['runtime'])
                        and previous[4] > lease_clock()):
                    raise ValueError('previous_permission_still_leased')
                self.db.execute('INSERT INTO leases '
                                '(instance,profile,account,ip,runtime,until_ms,boot_time,observed_ms,observed_tick) '
                                'VALUES (?,?,?,?,?,0,?,?,?) '
                                'ON CONFLICT(instance) DO UPDATE SET profile=excluded.profile, '
                                'account=excluded.account,ip=excluded.ip,runtime=excluded.runtime,'
                                'boot_time=excluded.boot_time,observed_ms=excluded.observed_ms,'
                                'observed_tick=excluded.observed_tick',
                                (scope['instance'], profile, account, ip, scope['runtime'],boot_time,observed_ms,
                                 lease_clock() if observed_tick is None else observed_tick))
        except sqlite3.IntegrityError:
            raise ValueError('public_ip_or_profile_already_in_use') from None

    def extend(self, instance, profile, runtime, ip):
        # Called in the SSH thread immediately before sending a permission.
        # A failed/lost reply still reserves the identity for the full possible
        # remote lifetime. The extra 50s covers command delivery and shutdown.
        with sqlite3.connect(self.path, timeout=10) as db:
            result = db.execute('UPDATE leases SET until_ms=?,until_tick=MAX(until_tick,?) '
                                'WHERE instance=? AND profile=? AND runtime=? AND ip=? '
                                'AND EXISTS (SELECT 1 FROM installations i WHERE i.profile=leases.profile '
                                'AND i.instance=leases.instance AND i.account=leases.account)',
                                (int(time.time()*1000)+(LEASE_SECONDS+50)*1000,
                                 lease_clock()+LEASE_SECONDS+50,
                                 instance, profile, runtime, ip))
            if result.rowcount != 1:
                raise ValueError('permission_assignment_changed')
            device, started = db.execute('SELECT device,enrollment_started FROM installations '
                                         'WHERE profile=?', (profile,)).fetchone()
            if device is None:
                if started:
                    raise ValueError('enrollment_status_uncertain')
                # Commit before sending any packet that could launch enrollment.
                # A lost reply leaves the profile uncertain across controller restarts.
                db.execute('UPDATE installations SET enrollment_started=1 WHERE profile=?', (profile,))

    def sequence(self):
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute('UPDATE controller SET sequence=sequence+1 WHERE id=1')
            return db.execute('SELECT sequence FROM controller WHERE id=1').fetchone()[0]

    def transport(self, instance):
        # Image replacement may omit SSH fields while the machine's verified
        # gateway remains valid. Inspect checks the new runtime before secrets
        # are sent; both its scope and the SSH host fingerprint must match.
        keys = ('ssh_ip', 'ssh_port', 'ssh_host_key_fingerprint')
        if all(instance.get(k) for k in keys):
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO transports VALUES (?,?,?,?)',
                                (instance['machine_id'], *(instance[k] for k in keys)))
            return instance
        row = self.db.execute('SELECT ip,port,fingerprint FROM transports WHERE machine=?',
                              (instance['machine_id'],)).fetchone()
        return dict(instance, **dict(zip(keys, row))) if row else instance

    def cipher(self, version):
        if version not in self.versions:
            self.versions[version] = json.loads(self.secrets.get_secret_value(
                SecretId=SECRET, VersionId=version)['SecretString'])
        # Domain-separated encryption key from an existing high-entropy secret.
        # Its AWS version is retained with each row so credential rotation does
        # not silently orphan backups. No plaintext master key is stored here.
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                   info=b'WDI Grass installation store v1').derive(
                       self.versions[version]['ssh_private_key'].encode())
        return AESGCM(key)

    def create_profile(self, seed):
        """Persist a new profile from an already server-verified account seed."""
        store = dict(seed)
        for key in IDENTITY_KEYS:
            if key == 'wynd:user_id':
                continue
            if decoded(store.get(key)) not in (None, ''):
                raise ValueError('seed_contains_installation')
            store.pop(key, None)
        private = Ed25519PrivateKey.generate()
        store.update({key: json.dumps(value) for key, value in {
            'wynd:device_privkey': base64.b64encode(private.private_bytes(
                serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                serialization.NoEncryption())).decode(),
            'wynd:device_pubkey': base64.b64encode(private.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode(),
            'wynd:browser_id': str(uuid.uuid4()),
        }.items()})
        store.pop('wynd:status', None)
        payload = dict(store=store, machine_id=uuid.uuid4().hex + '\n',
                       origin={'initiator': 'wdi_profile_create', 'created_ms': int(time.time()*1000)})
        return self.put({}, payload, set(), create=True)

    def get(self, profile):
        row = self.db.execute('SELECT instance,account,version,ciphertext FROM installations '
                              'WHERE profile=?', (profile,)).fetchone()
        if not row:
            return None
        owner, account, version, encrypted = row
        payload = self.cipher(version).decrypt(encrypted[:12], encrypted[12:], f'{account}/{profile}'.encode())
        return owner, json.loads(payload)

    def select(self, account, instance, allocated_ids, store=None):
        if not account:
            raise ValueError('configured_account_missing')
        # A restart retains its assignment. A replacement takes an idle
        # installation. Neither machine IDs nor IPs create installation rows.
        rows = self.db.execute('SELECT profile,device,instance FROM installations WHERE account=? '
                               'ORDER BY instance=? DESC,profile', (account, instance)).fetchall()
        store = store or {}
        public = decoded(store.get('wynd:device_pubkey')) or decoded(store.get('wynd:device_registered_pubkey'))
        device = decoded(store.get('wynd:device_id'))
        if public:
            try:
                public = base64.b64decode(public, validate=True)
                if len(public) != 32:
                    raise ValueError('invalid_public_key')
            except (ValueError, TypeError):
                raise ValueError('installation_keys_invalid') from None
            profile = hashlib.sha256(public).hexdigest()
            rows = [row for row in rows if row[0] == profile]
        elif device:
            rows = [row for row in rows if row[1] == device]
        for profile, _, owner in rows:
            if owner == instance or owner not in allocated_ids:
                return profile
        raise ValueError('saved_installations_allocated_or_leased' if rows else 'no_matching_saved_installation')

    def put(self, scope, payload, allocated_ids, create=False):
        original = identity(payload['store'], pending=True)
        profile, device, account = original['public_sha256'], original['device'], original['account']
        instance = scope.get('instance')
        previous = self.get(profile)
        if not previous and not create:
            raise ValueError('original_installation_backup_missing')
        if previous and previous[0] != instance and previous[0] in allocated_ids:
            raise ValueError('previous_installation_still_allocated')
        if previous:
            saved = identity(previous[1]['store'], pending=True)
            if (dict(original, device=saved['device']) != saved or
                    saved['device'] is not None and device != saved['device']):
                raise ValueError('refusing_to_replace_original_registration')
            if payload['machine_id'] != previous[1]['machine_id']:
                raise ValueError('installation_machine_id_changed')
            if (device is None and previous[0] != instance and
                    self.db.execute('SELECT enrollment_started FROM installations WHERE profile=?',
                                    (profile,)).fetchone()[0]):
                raise ValueError('enrollment_status_uncertain')
        nonce = os.urandom(12)
        encrypted = nonce + self.cipher(self.version).encrypt(
            nonce, json.dumps(payload, separators=(',', ':')).encode(), f'{account}/{profile}'.encode())
        with self.db:
            self.db.execute('INSERT INTO installations '
                            '(profile,device,account,instance,group_id,machine,version,ciphertext) '
                            'VALUES (?,?,?,?,?,?,?,?) '
                            'ON CONFLICT(profile) DO UPDATE SET device=excluded.device, instance=excluded.instance, '
                            'group_id=excluded.group_id,machine=excluded.machine, '
                            'version=excluded.version,ciphertext=excluded.ciphertext',
                            (profile, device, account, instance, scope.get('group'), scope.get('machine'),
                             self.version, encrypted))
        if self.get(profile) != (instance, payload):
            raise ValueError('backup_readback_failed')
        if instance and (not previous or previous[0] != instance):
            emit('profile_assigned', profile=profile, device=device, **scope,
                 previous_instance=previous[0] if previous else None,
                 initiator='wdi_identity_controller', reason='assign_saved_profile_to_allocation')
        return profile


class HostKey(paramiko.MissingHostKeyPolicy):
    def __init__(self, expected):
        # Salad appends an algorithm label, e.g. "SHA256:... (ED25519)".
        self.expected = expected.split()[0]

    def missing_host_key(self, client, hostname, key):
        actual = 'SHA256:' + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')
        if actual != self.expected:
            raise ValueError('salad_ssh_host_key_mismatch')


def exchange(instance, access, payload, before_send=None):
    """Use Salad's interactive SSH gateway; secret input is never shell text."""
    key = paramiko.Ed25519Key.from_private_key(io.StringIO(access['ssh_private_key']))
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(HostKey(instance['ssh_host_key_fingerprint']))
    try:
        client.connect(instance['ssh_ip'], port=instance['ssh_port'], username='root',
                       pkey=key, timeout=15, auth_timeout=15, banner_timeout=15,
                       allow_agent=False, look_for_keys=False)
        channel = client.invoke_shell(width=200, height=24)
        channel.settimeout(20)
        # Disable terminal echo before sending credentials, and canonical input
        # buffering before sending a JSON store larger than the PTY line limit.
        channel.sendall("stty -echo -icanon && printf '\\nWDI_SHELL_READY\\n'\n")
        buffer = b''
        program_sent = False
        sent = False
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            part = channel.recv(65536)
            if not part:
                raise ValueError('salad_ssh_closed_before_result')
            buffer += part
            if len(buffer) > 2_000_000:
                raise ValueError('salad_ssh_response_too_large')
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                line = line.rstrip(b'\r')
                if line == b'WDI_SHELL_READY' and not program_sent:
                    channel.sendall('exec python3 /opt/grass-private/session.py\n')
                    program_sent = True
                elif line == b'WDI_READY' and program_sent and not sent:
                    if before_send:
                        before_send()
                    channel.sendall(json.dumps(payload, separators=(',', ':')) + '\n')
                    sent = True
                elif line.startswith(b'WDI_RESULT ') and sent:
                    reply = json.loads(line[len(b'WDI_RESULT '):])
                    if not reply['ok']:
                        raise ValueError(reply['error'])
                    if payload['action'] == 'inspect':
                        # Capture in the receiving thread, before waiting for
                        # the rest of a large fleet's inspections to complete.
                        reply['result']['controller_received_ms'] = int(time.time()*1000)
                        reply['result']['controller_received_tick'] = lease_clock()
                    return reply['result']
        raise ValueError('salad_ssh_result_timeout')
    finally:
        client.close()


def verify_account(store):
    account = decoded(store.get('wynd:user_id'))
    if not isinstance(account, str) or not account:
        raise ValueError('configured_account_missing')
    if decoded(store.get('wynd:authenticated')) is not True:
        raise ValueError('grass_login_required')
    token = decoded(store.get('accessToken'))
    if not isinstance(token, str) or not token:
        raise ValueError('grass_login_required')
    renewed = None
    try:
        user = request('https://api.grass.io/retrieveUser', {'Authorization': token})['result']['data']
    except urllib.error.HTTPError as error:
        refresh = decoded(store.get('refreshToken'))
        if error.code != 401 or not isinstance(refresh, str) or not refresh:
            raise
        renewed = request('https://api.grass.io/refreshToken', {}, {'refreshToken': refresh})['result']['data']
        user = request('https://api.grass.io/retrieveUser',
                       {'Authorization': renewed['accessToken']})['result']['data']
    if user['userId'] != account:
        raise ValueError('grass_token_account_mismatch')
    if renewed:
        store['accessToken'] = json.dumps(renewed['accessToken'])
        store['refreshToken'] = json.dumps(renewed['refreshToken'])
        # This is the expiry update used by the official desktop after refresh.
        store['tokenExpiry'] = json.dumps(int(time.time()) + 365*86400)
    return user


def cycle(vault, names, capture_only, pool):
    """Inspect concurrently; serialize identity/IP decisions in one database."""
    started = time.time()
    headers = {'Salad-Api-Key': vault.access['api_key']}
    groups = request(API, headers)['items']
    if len(groups) >= 100:
        raise ValueError('group_inventory_may_be_truncated')
    selected = {g['id']: g for g in groups if g['name'] in names}
    if {g['name'] for g in selected.values()} != set(names):
        raise ValueError('configured_group_missing')
    ids, managed = set(), []
    jobs = {pool.submit(request, API+'/'+g['name']+'/instances', headers): g for g in groups}
    try:
        for job in as_completed(jobs):
            group = jobs[job]
            instances = job.result()['instances']
            if len(instances) >= 1000:
                raise ValueError('instance_inventory_may_be_truncated')
            ids.update(i['id'] for i in instances)
            if group['id'] in selected:
                account = group.get('container',{}).get('environment_variables',{}).get('GRASS_ACCOUNT_ID')
                if not account and not capture_only:
                    raise ValueError('group_account_configuration_missing')
                for i in instances:
                    i = vault.transport(i)
                    i['group_id'] = group['id']
                    i['configured_account'] = account
                    managed.append(i)
    except Exception:
        for job in jobs:job.cancel()
        raise
    occupied = vault.occupied(ids)
    leases = {r[0]: dict(zip(('instance','profile','account','ip','runtime','until_ms','boot_time','observed_ms',
                            'until_tick','observed_tick'), r))
              for r in vault.db.execute('SELECT * FROM leases')}
    report, observed, unknown_accounts = {}, {}, set()
    jobs = {}
    for i in managed:
        row = report[i['id']] = {'instance':i['id'], 'machine':i['machine_id'],
                                'group':i['group_id'], 'state':i['state'], 'error':None,
                                'account':i['configured_account']}
        if i['state'] != 'running':
            continue
        lease = leases.get(i['id'])
        if not i.get('ssh_ip'):
            row['error'] = 'ssh_unavailable'
            if lease:
                unknown_accounts.add(lease['account'])
            continue
        payload, hook = {'action':'inspect', 'include_ip':True}, None
        if (lease and lease['observed_tick'] and not capture_only
                and lease['account'] == i['configured_account']):
            saved = vault.get(lease['profile'])
            original = identity(saved[1]['store'], pending=True) if saved and saved[0] == i['id'] else None
            if original and original['device'] is not None:
                scope = dict(group=i['group_id'], instance=i['id'], machine=i['machine_id'],
                             runtime=lease['runtime'], configured_account=i['configured_account'])
                payload['lease'] = dict(scope=scope, identity=original,
                                        ip=lease['ip'], until=0, sequence=vault.sequence())
                def renew(l=lease, packet=payload):
                    elapsed=lease_clock()-l['observed_tick']
                    if elapsed < 0:
                        raise ValueError('controller_clock_moved_backwards')
                    packet['lease']['until']=l['boot_time']+elapsed+LEASE_SECONDS-10
                    vault.extend(l['instance'],l['profile'],l['runtime'],l['ip'])
                hook=renew
        jobs[pool.submit(exchange, i, vault.access, payload, hook)] = i
    for job in as_completed(jobs):
        i = jobs[job]
        try:
            obs = job.result()
            expected = dict(group=i['group_id'], instance=i['id'], machine=i['machine_id'])
            if i['configured_account']:
                expected['configured_account'] = i['configured_account']
            if any(obs['scope'].get(k) != value for k, value in expected.items()):
                raise ValueError('salad_runtime_identity_mismatch')
            observed[i['id']] = obs
            lease = leases.get(i['id'])
            if lease and not capture_only and not obs.get('renewed'):
                # Keep the possible lifetime of every packet already sent.
                # Stop blind renewals after rejection so changed-IP/runtime
                # reservations can expire without shortening a concurrent grant.
                with vault.db:
                    vault.db.execute('UPDATE leases SET observed_tick=0 WHERE instance=? AND runtime=?',
                                     (i['id'], lease['runtime']))
                if not obs.get('public_ip'):
                    unknown_accounts.add(lease['account'])
        except Exception as error:
            report[i['id']]['error'] = str(error) if type(error) is ValueError else type(error).__name__
            if i['id'] in leases:
                unknown_accounts.add(leases[i['id']]['account'])
    actions, verified = {}, {}
    # Keep current owners ahead of new contenders; SQL also rejects duplicate
    # IPs/profiles when several replacements arrive in the same inspection.
    managed.sort(key=lambda i: (i['id'] not in leases, i['id']))
    for i in managed:
        obs = observed.get(i['id'])
        if not obs:
            continue
        scope, row = obs['scope'], report[i['id']]
        store, local_error = obs['store'], None
        account = scope.get('configured_account') or decoded((store or {}).get('wynd:user_id'))
        row.update(account=account, ip=obs.get('public_ip'), ip_observed_ms=obs.get('ip_observed_ms'),
                   runtime=scope['runtime'], processes=obs['processes'], restored=obs['restored'],
                   native_status=decoded((store or {}).get('wynd:status')), native_status_is_heartbeat=False)
        try:
            if obs.get('fatal_reason'):
                row['fatal_reason'] = obs['fatal_reason']
                raise ValueError('runtime_blocked')
            local = None
            if store:
                try:
                    local = identity(store, pending=not capture_only)
                except ValueError as error:
                    local_error = error
            if (capture_only or obs['processes']) and not local:
                raise local_error or ValueError('running_installation_has_no_saved_keys')
            if local and local['account'] != account:
                raise ValueError('configured_account_changed')
            profile = local['public_sha256'] if local else vault.select(account, i['id'], occupied, store)
            saved = vault.get(profile)
            if saved and saved[0] != i['id'] and saved[0] in occupied:
                raise ValueError('previous_installation_still_allocated_or_leased')
            if not saved and not capture_only:
                raise ValueError('unrecognised_installation_not_adopted')
            if (not capture_only and local and local['device'] is None
                    and (obs['processes'] or obs['restored'])):
                with vault.db:
                    vault.db.execute('UPDATE installations SET enrollment_started=1 '
                                     'WHERE profile=? AND device IS NULL', (profile,))
            if (local and local['device'] is None and saved and
                    identity(saved[1]['store'], pending=True)['device'] is not None):
                if not compatible(store, saved[1]['store']):
                    raise ValueError('local_installation_conflict')
                if obs['processes']:
                    raise ValueError('saved_installation_changed')
                # A complete backup repairs missing native fields without
                # replacing it with an apparently unregistered local identity.
                local = None
            if local_error and (not saved or not compatible(store, saved[1]['store'])):
                raise local_error
            if local:
                if not saved:
                    verify_account(store)
                value = dict(saved[1] if saved else {'origin':scope}, store=store, machine_id=obs['machine_id'])
                if saved != (i['id'], value):
                    vault.put(scope, value, occupied, create=capture_only)
                saved = (i['id'], value)
            original = identity(saved[1]['store'], pending=True)
            row.update(profile=profile, device=original['device'])
            if capture_only:
                continue
            if obs['managed'] != SOURCE_SHA:
                raise ValueError('runtime_image_not_updated')
            if not obs.get('public_ip'):
                raise ValueError('public_ip_unavailable')
            if obs.get('ip_policy_required'):
                policy = obs.get('ip_policy') or {}
                row['ip_policy'] = policy
                if (policy.get('scope') != scope or policy.get('ip') != obs['public_ip'] or
                        policy.get('allowed') is not True or policy.get('reallocation_requested') or
                        obs['boot_time'] >= policy.get('valid_until', 0)):
                    raise ValueError('ip_policy_not_admitted')
            lease = leases.get(i['id'])
            if original['device'] is None:
                if vault.db.execute('SELECT enrollment_started FROM installations WHERE profile=?', (profile,)).fetchone()[0]:
                    row['enrollment_status'] = 'uncertain'
                    # An already-running first attempt can finish under its
                    # existing deadline. Never send it another launch permission.
                    if (obs['processes'] and lease and lease['profile'] == profile
                            and lease['runtime'] == scope['runtime'] and lease['ip'] == obs['public_ip']
                            and lease['account'] == account and lease['until_tick'] > lease_clock()):
                        continue
                    raise ValueError('enrollment_status_uncertain')
            if account in unknown_accounts and not (lease and lease['ip'] == obs['public_ip']):
                raise ValueError('existing_account_ip_inventory_incomplete')
            vault.admit(scope, profile, account, obs['public_ip'], obs['boot_time'],
                        obs.get('controller_received_ms',int(time.time()*1000)),
                        obs.get('controller_received_tick',lease_clock()))
            if obs.get('renewed'):
                continue
            if local and (obs['processes'] or obs['restored']):
                payload = dict(action='allow', scope=scope, identity=original,
                               ip=obs['public_ip'], until=obs['boot_time']+LEASE_SECONDS)
            else:
                value = saved[1]
                key = (account, hashlib.sha256(str(value['store'].get('accessToken')).encode()).hexdigest())
                if key not in verified:
                    verify_account(value['store'])
                    verified[key] = {k:value['store'][k] for k in ('accessToken','refreshToken','tokenExpiry') if k in value['store']}
                value['store'].update(verified[key])
                vault.put(scope, value, occupied)
                payload = dict(action='restore', scope=scope, pending=original['device'] is None,
                               ip=obs['public_ip'], until=obs['boot_time']+LEASE_SECONDS, **value)
            # Decision order survives asynchronous SSH delivery. A queued old
            # grant cannot acquire a newer sequence after a later hold is sent.
            payload['sequence'] = vault.sequence()
            hook = lambda s=scope, p=profile, ip=obs['public_ip']: vault.extend(s['instance'],p,s['runtime'],ip)
            actions[pool.submit(exchange, i, vault.access, payload, hook)] = (i, payload['action'])
        except Exception as error:
            row['error'] = str(error) if type(error) is ValueError else type(error).__name__
            # Pausing revokes local launch permission; it never discards keys,
            # reallocates a node, or releases an uncertain remote lease early.
            if not capture_only and obs['managed'] == SOURCE_SHA:
                packet = dict(action='hold',scope=scope,sequence=vault.sequence())
                actions[pool.submit(exchange, i, vault.access, packet)] = (i,'hold')
    for job in as_completed(actions):
        i, action = actions[job]
        row = report[i['id']]
        try:
            reply = job.result()
            row['action'] = action
            if action == 'restore':
                row['restored'] = True
                emit('identity_restored', instance=i['id'], machine=i['machine_id'], group=i['group_id'],
                     device=reply['identity']['device'], profile=reply['identity']['public_sha256'],
                     initiator='wdi_identity_controller', reason='restore_saved_identity_after_ip_admission')
        except Exception as error:
            row['error'] = str(error) if type(error) is ValueError else type(error).__name__
    # One account-wide request per minute, rather than one per node.
    accounts = {g.get('container',{}).get('environment_variables',{}).get('GRASS_ACCOUNT_ID') for g in selected.values()} - {None}
    for account in accounts:
        previous = vault.accounts.get(account, {})
        if time.time()*1000 - previous.get('checked_ms',0) < 60_000:
            continue
        value = next((o['store'] for o in observed.values() if o.get('store') and
                      decoded(o['store'].get('wynd:user_id')) == account), None)
        if not value:
            saved = vault.db.execute('SELECT profile FROM installations WHERE account=? LIMIT 1', (account,)).fetchone()
            if saved:
                value = vault.get(saved[0])[1]['store']
        if not value:
            continue
        checked = int(time.time()*1000)
        try:
            user = verify_account(value)
            vault.accounts[account] = dict(checked_ms=checked, observed_ms=checked,
                registered_devices=user.get('deviceCount'), device_cap=user.get('deviceCapOverride'), error=None)
            vault.accounts[account]['account_total_points'] = user.get('totalPoints')
            vault.accounts[account]['account_network_points'] = user.get('proxyPoints')
            token = decoded(value['accessToken'])
            try:
                usage = request('https://api.grass.io/activeIps', {'Authorization':token})['result']['data']
                managed_ips = {r.get('ip') for r in report.values() if r.get('account') == account}
                today = time.strftime('%Y-%m-%d',time.gmtime())
                vault.accounts[account]['ip_usage'] = [
                    {k:r.get(k) for k in ('ipAddress','date','totalUptime','aggUptime','ipScore','multiplier')}
                    for r in usage if r.get('ipAddress') in managed_ips and r.get('date') == today]
                vault.accounts[account]['usage_source'] = 'Grass activeIps; UTC day; totalUptime in seconds'
            except Exception as error:
                vault.accounts[account]['usage_error'] = type(error).__name__
        except Exception as error:
            vault.accounts[account] = dict(previous, checked_ms=checked, error=type(error).__name__)
    rows = list(report.values())
    status = dict(observed_ms=int(time.time()*1000), cycle_seconds=round(time.time()-started,3),
                  groups=names, desired_allocations=sum(g['replicas'] for g in selected.values()),
                  allocations=len(rows), running=sum(r['state']=='running' for r in rows),
                  grass_running=sum(bool(r.get('processes')) for r in rows),
                  observed_unique_ips=len({r['ip'] for r in rows if r.get('ip')}),
                  profiles=vault.db.execute('SELECT COUNT(*) FROM installations').fetchone()[0],
                  reserved_after_instance_loss=[dict(instance=r[0],
                      until_ms=int(time.time()*1000+max(0,r[1]-lease_clock())*1000))
                      for r in vault.db.execute('SELECT instance,until_tick FROM leases') if r[0] not in ids],
                  accounts=vault.accounts, instances=rows, error=None)
    path = vault.path.with_name('grass-status.json')
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(status, separators=(',',':'))+'\n')
    temp.replace(path)
    for row in rows:
        if row['error']:
            emit('identity_blocked', instance=row['instance'], reason=row['error'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--group', action='append', help='Existing group names, comma-separated; repeat for batches')
    parser.add_argument('--initialize', type=int, metavar='COUNT',
                        help='Explicitly prepare this total number of permanent profiles once')
    parser.add_argument('--account-session', type=Path, default=Path('/var/lib/wdi/account-session.enc'),
                        help='Encrypted account session for one-time setup')
    parser.add_argument('--capture', action='store_true', help='Import existing installations once; does not register devices')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--status', action='store_true', help='Show the last measured fleet state without contacting providers')
    parser.add_argument('--database', type=Path, default=Path('/var/lib/wdi/grass.sqlite3'))
    args = parser.parse_args()
    if args.status:
        status = json.loads(args.database.with_name('grass-status.json').read_text())
        status['age_seconds'] = round((time.time()*1000-status['observed_ms'])/1000,1)
        print(json.dumps(status, indent=2))
        return
    names = list(dict.fromkeys(n for group in args.group or [] for n in group.split(',')))
    if args.initialize is not None and (args.initialize < 1 or args.group or args.capture):
        parser.error('--initialize needs a positive count and cannot run allocation management')
    if args.initialize is None and (not names or any(not n or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in n) for n in names)):
        parser.error('invalid Salad group name')
    os.umask(0o077)
    args.database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with args.database.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        vault = Vault(args.database)
        if args.initialize is not None:
            envelope = json.loads(args.account_session.read_bytes())
            packed = base64.b64decode(envelope['ciphertext'], validate=True)
            seed = json.loads(vault.cipher(envelope['version']).decrypt(
                packed[:12], packed[12:], b'wdi-grass-account-session-v1'))
            user = verify_account(seed)
            account = decoded(seed['wynd:user_id'])
            count = vault.db.execute('SELECT COUNT(*) FROM installations WHERE account=?', (account,)).fetchone()[0]
            pending = vault.db.execute('SELECT COUNT(*) FROM installations WHERE account=? AND device IS NULL', (account,)).fetchone()[0]
            cap, registered = user.get('deviceCapOverride'), user.get('deviceCount')
            if not isinstance(cap, int) or not isinstance(registered, int):
                raise ValueError('provider_device_cap_unavailable')
            if max(0,args.initialize-count)+pending > max(0,cap-registered):
                raise ValueError('requested_profiles_exceed_remaining_device_cap')
            for _ in range(max(0, args.initialize - count)):
                vault.create_profile(seed)
            emit('profiles_initialized', account=account, profiles=max(count, args.initialize),
                 newly_prepared=max(0, args.initialize-count),
                 provider_registered_devices=user.get('deviceCount'),
                 registration_requested=False)
            return
        failures = 0
        with ThreadPoolExecutor(max_workers=8) as pool:
            while True:
                try:
                    cycle(vault, names, args.capture, pool)
                    failures = 0
                    pause = 30
                except Exception as error:
                    failures += 1
                    reason = str(error) if type(error) is ValueError else type(error).__name__
                    emit('inventory_unavailable', reason=reason)
                    path = args.database.with_name('grass-status.json')
                    status = json.loads(path.read_text()) if path.exists() else {'observed_ms':0}
                    status.update(error=reason, checked_ms=int(time.time()*1000))
                    temp = path.with_suffix('.tmp');temp.write_text(json.dumps(status)+'\n');temp.replace(path)
                    pause = min(300, 15*2**min(failures,5))
                    if isinstance(error, urllib.error.HTTPError):
                        after = error.headers.get('Retry-After','')
                        if after.isdigit():
                            pause = max(pause, min(3600,int(after)))
                    if args.once:
                        raise SystemExit(1)
                if args.once:
                    return
                time.sleep(pause)


if __name__ == '__main__':
    main()
