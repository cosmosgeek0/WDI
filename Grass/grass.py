"""Run permanent Grass installations across disposable Salad allocations.

Run only on Mumbai, where the instance role can read wdi/salad-access.
No private keys or session stores are sent to the operator's terminal.
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
import fcntl
import grp
import hashlib
import io
import ipaddress
import json
import os
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import boto3
import paramiko
from botocore.config import Config
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from session import IDENTITY_KEYS, LEASE_SECONDS, compatible, decoded, identity, node_store, require_node_store

REGION = 'ap-south-1'
SECRET = 'wdi/salad-access'
ACCOUNT_SECRET = os.environ.get('GRASS_ACCOUNT_SECRET', 'wdi/grass-account')
API = 'https://api.salad.com/api/public/organizations/cosmosgeeketh/projects/default/containers'
SOURCE_SHA = hashlib.sha256(Path(__file__).with_name('session.py').read_bytes()).hexdigest()
API_BACKOFF = Path('/var/lib/wdi/grass-api-backoff.json')
API_BACKOFF_LOCK = threading.Lock()


def lease_clock():
    return time.clock_gettime(getattr(time, 'CLOCK_BOOTTIME', time.CLOCK_MONOTONIC))


def emit(event, **values):
    print(json.dumps(dict(event=event, observed_ms=int(time.time()*1000), **values)), flush=True)


def account_retry_delay():
    try:until=json.loads(API_BACKOFF.read_text()).get('until',0)
    except (OSError,ValueError):until=0
    return max(0,until-time.time())


def request(url, headers, payload=None, on_response=None):
    account_api=url.startswith('https://api.grass.io/')
    if account_api:
        delay=account_retry_delay()
        if delay:
            raise urllib.error.HTTPError(url,429,'provider_cooldown',
                {'Retry-After':str(int(delay)+1)},None)
    headers = dict(headers, **{'User-Agent': 'WDI/1.0', 'Accept': 'application/json'})
    data = None
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(payload).encode()
    try:
        response=urllib.request.urlopen(urllib.request.Request(url, headers=headers, data=data),timeout=15)
    except urllib.error.HTTPError as error:
        if account_api and error.code==429:
            after=error.headers.get('Retry-After','')
            delay=max(60,int(after)) if after.isdigit() else 60
            with API_BACKOFF_LOCK:
                try:previous=json.loads(API_BACKOFF.read_text()).get('until',0)
                except (OSError,ValueError):previous=0
                temporary=API_BACKOFF.with_suffix('.tmp')
                temporary.write_text(json.dumps({'until':max(previous,time.time()+delay),'http':429}))
                temporary.replace(API_BACKOFF)
        raise
    with response:
        raw = response.read(8_000_001)
        if len(raw)>8_000_000:raise ValueError('provider_response_too_large')
        if on_response:on_response(raw)
        return json.loads(raw)


class Vault:
    def __init__(self, path):
        self.path = path
        self.accounts = {}
        self.providers = {}
        self.provider_jobs = {}
        self.authorizers = {}
        self.account_session = None
        self.account_checked = 0
        self.account_lock = threading.Lock()
        self.secrets = boto3.client('secretsmanager', region_name=REGION,
                                   config=Config(connect_timeout=3, read_timeout=5, retries={'max_attempts':1}))
        self.versions = {}
        reply = self.secrets.get_secret_value(SecretId=SECRET)
        self.version = reply['VersionId']
        self.access = json.loads(reply['SecretString'])
        self.versions[self.version] = self.access
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS key_creations (request TEXT PRIMARY KEY,account TEXT NOT NULL,kms_key TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS registration_replies (profile TEXT PRIMARY KEY,version TEXT NOT NULL,ciphertext BLOB NOT NULL)')
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
        if 'kms_key' not in {r[1] for r in self.db.execute('PRAGMA table_info(installations)')}:
            with self.db:
                self.db.execute('ALTER TABLE installations ADD COLUMN kms_key TEXT')

    def occupied(self, allocated_ids):
        now = lease_clock()
        with self.db:
            for instance, until in self.db.execute('SELECT instance,until_tick FROM leases').fetchall():
                if until <= now:
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
        if row and all(not instance.get(k) or instance[k] == v for k,v in zip(keys,row)):
            return dict(instance, **dict(zip(keys, row)))
        return instance

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

    def account(self, account=None):
        with self.account_lock:
            if not self.account_checked or time.monotonic()-self.account_checked >= 60:
                self.account_checked = time.monotonic()
                self.account_session = None
                try:
                    value = json.loads(self.secrets.get_secret_value(SecretId=ACCOUNT_SECRET)['SecretString'])
                    if all(isinstance(value.get(k),str) and value[k] for k in (
                            'account_id','access_token','refresh_token')):
                        self.account_session = value
                except Exception:
                    pass
            if self.account_session is None:
                raise ValueError('account_secret_unavailable')
            if account is not None and self.account_session['account_id'] != account:
                raise ValueError('account_secret_identity_mismatch')
            return self.account_session

    def create_profile(self, seed):
        """Create a non-exportable KMS key, then register its persisted public identity."""
        with closing(boto3.client('kms',region_name=REGION,
                          config=Config(connect_timeout=3,read_timeout=5,retries={'total_max_attempts':1}))) as kms:
            pending = self.db.execute('SELECT request,kms_key FROM key_creations WHERE account=?',
                                      (seed['account_id'],)).fetchone()
            if pending:
                creation,key_id = pending
                if not key_id:raise ValueError('key_creation_reconciliation_required')
            else:
                creation = str(uuid.uuid4())
                with self.db:
                    self.db.execute('INSERT INTO key_creations VALUES (?,?,NULL)',(creation,seed['account_id']))
                key_id = kms.create_key(KeySpec='ECC_NIST_EDWARDS25519',KeyUsage='SIGN_VERIFY',
                    Description='WDI node identity',Tags=[
                        {'TagKey':'wdi-purpose','TagValue':'grass-node-auth'},
                        {'TagKey':'wdi-account','TagValue':seed['account_id']},
                        {'TagKey':'wdi-creation','TagValue':creation}])['KeyMetadata']['Arn']
                with self.db:
                    self.db.execute('UPDATE key_creations SET kms_key=? WHERE request=?',(key_id,creation))
            public = serialization.load_der_public_key(kms.get_public_key(KeyId=key_id)['PublicKey'])
            raw = public.public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
            store = {'wynd:user_id':json.dumps(seed['account_id']),
                     'wynd:device_pubkey':json.dumps(base64.b64encode(raw).decode()),
                     'wynd:browser_id':json.dumps(str(uuid.uuid4())),
                     'wdi:kms_key':key_id}
            payload = dict(store=store,machine_id=uuid.uuid4().hex+'\n',
                           origin={'initiator':'wdi_profile_create','created_ms':int(time.time()*1000)})
            profile = self.put({},payload,set(),create=True)
            with self.db:
                self.db.execute('DELETE FROM key_creations WHERE request=?',(creation,))
            return self.register_profile(profile,seed)

    def register_profile(self, profile, seed):
        """Resume only an unattempted request or a durably saved successful reply."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        value = self.get(profile)[1]
        store = value['store']
        original = identity(store,pending=True)
        if original['account']!=seed['account_id']:raise ValueError('registration_account_mismatch')
        if original['device'] is not None:return profile
        started = self.db.execute('SELECT enrollment_started FROM installations WHERE profile=?',(profile,)).fetchone()[0]
        def preserve(raw):
            nonce=os.urandom(12)
            encrypted=nonce+self.cipher(self.version).encrypt(nonce,raw,('registration/'+profile).encode())
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO registration_replies VALUES (?,?,?)',
                                (profile,self.version,encrypted))
        if started:
            reply=self.db.execute('SELECT version,ciphertext FROM registration_replies WHERE profile=?',(profile,)).fetchone()
            if not reply:raise ValueError('registration_reconciliation_required')
            version,encrypted=reply
            result=json.loads(self.cipher(version).decrypt(encrypted[:12],encrypted[12:],('registration/'+profile).encode()))
        else:
            raw=base64.b64decode(decoded(store['wynd:device_pubkey']),validate=True)
            message=b'GRASS-REGISTER-v1\n'+raw+b'\n'+seed['account_id'].encode()
            with closing(boto3.client('kms',region_name=REGION,
                              config=Config(connect_timeout=3,read_timeout=5,retries={'max_attempts':1}))) as kms:
                signature=kms.sign(KeyId=store['wdi:kms_key'],Message=message,MessageType='RAW',
                                   SigningAlgorithm='ED25519_SHA_512')['Signature']
            Ed25519PublicKey.from_public_bytes(raw).verify(signature,message)
            while True:
                with self.db:
                    self.db.execute('UPDATE installations SET enrollment_started=1 WHERE profile=?',(profile,))
                try:
                    result=request('https://api.grass.io/devices/register',{'Authorization':seed['access_token']},
                                   {'pubkey':base64.b64encode(raw).decode(),'sig':base64.b64encode(signature).decode(),
                                    'platform':'desktop','app_version':'7.6.1','user_agent':'WDI/1.0'},on_response=preserve)
                    break
                except urllib.error.HTTPError as error:
                    if error.code!=429:raise
                    after=error.headers.get('Retry-After','')
                    delay=max(60,int(after)) if after.isdigit() else 60
                    with self.db:
                        self.db.execute('UPDATE installations SET enrollment_started=0 WHERE profile=?',(profile,))
                    emit('registration_rate_limited',profile=profile,retry_after_seconds=delay,existing_key_retained=True)
                    time.sleep(delay)
                    continue
        result=result.get('result') or result
        if not isinstance(result.get('device_id'),str) or not result['device_id']:
            raise ValueError('registration_result_uncertain')
        store.update({'wynd:device_id':json.dumps(result['device_id']),
                      'wynd:device_registered_user_id':store['wynd:user_id'],
                      'wynd:device_registered_pubkey':store['wynd:device_pubkey']})
        self.put({},value,set())
        return profile

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
                               'AND device IS NOT NULL AND kms_key IS NOT NULL '
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
        if previous and any(decoded(previous[1]['store'].get(k)) for k in ('accessToken','refreshToken')):
            raise ValueError('account_session_migration_required')
        keys = (*IDENTITY_KEYS,'wdi:kms_key','wdi:private_sha256')
        payload = dict(payload, store={k:payload['store'][k] for k in keys if k in payload['store']})
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
                            '(profile,device,account,instance,group_id,machine,version,ciphertext,kms_key) '
                            'VALUES (?,?,?,?,?,?,?,?,?) '
                            'ON CONFLICT(profile) DO UPDATE SET device=excluded.device, instance=excluded.instance, '
                            'group_id=excluded.group_id,machine=excluded.machine, '
                            'version=excluded.version,ciphertext=excluded.ciphertext,kms_key=excluded.kms_key',
                            (profile, device, account, instance, scope.get('group'), scope.get('machine'),
                             self.version, encrypted,payload['store'].get('wdi:kms_key')))
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


class Shell:
    def __init__(self, instance, access, program):
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(HostKey(instance['ssh_host_key_fingerprint']))
        setup_timeout = threading.Timer(35, self.client.close)
        setup_timeout.daemon = True
        setup_timeout.start()
        try:
            key = paramiko.Ed25519Key.from_private_key(io.StringIO(access['ssh_private_key']))
            self.client.connect(instance['ssh_ip'], port=instance['ssh_port'], username='root',
                pkey=key, timeout=15, auth_timeout=15, banner_timeout=15,
                allow_agent=False, look_for_keys=False)
            self.channel = self.client.invoke_shell(width=200,height=24)
            self.channel.settimeout(20)
            self.buffer = b''
            self.write("stty -echo -icanon && printf '\\nWDI_SHELL_READY\\n'\n")
            deadline = time.monotonic()+30
            while self.line() != 'WDI_SHELL_READY':
                if time.monotonic()>deadline:
                    raise ValueError('salad_ssh_shell_timeout')
            if program not in ('session.py','node_auth.py relay'):
                raise ValueError('invalid_remote_program')
            self.write('exec python3 /opt/grass-private/'+program+'\n')
            while self.line() != 'WDI_READY':
                if time.monotonic()>deadline:
                    raise ValueError('salad_ssh_program_timeout')
        except Exception:
            self.client.close()
            raise
        finally:
            setup_timeout.cancel()

    def line(self):
        deadline = time.monotonic()+20
        while b'\n' not in self.buffer:
            remaining = deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('salad_ssh_read_timeout')
            self.channel.settimeout(remaining)
            part = self.channel.recv(65536)
            if not part:
                raise ValueError('salad_ssh_closed_before_result')
            self.buffer += part
            if len(self.buffer)>2_000_000:
                raise ValueError('salad_ssh_response_too_large')
        line,self.buffer = self.buffer.split(b'\n',1)
        return line.rstrip(b'\r').decode()

    def send(self, value):
        self.write(json.dumps(value,separators=(',',':'))+'\n')

    def write(self, value):
        data = value.encode()
        deadline = time.monotonic()+20
        while data:
            remaining = deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('salad_ssh_write_timeout')
            self.channel.settimeout(remaining)
            sent = self.channel.send(data)
            if not sent:raise ValueError('salad_ssh_closed_before_send')
            data = data[sent:]

    def close(self):
        self.client.close()


def exchange(instance, access, payload, before_send=None):
    """Verified SSH transports device state; account credentials never enter it."""
    if payload['action'] == 'restore':
        require_node_store(payload['store'])
        if set(payload['store']) != set(IDENTITY_KEYS)|{'wdi:identity','wynd:authenticated','wynd:permissions','isConnectButtonActive'}:
            raise ValueError('unexpected_worker_store_fields')
    remote = Shell(instance,access,'session.py')
    try:
        if before_send:
            before_send()
        remote.send(payload)
        line = remote.line()
        if not line.startswith('WDI_RESULT '):
            raise ValueError('invalid_session_reply')
        reply = json.loads(line[len('WDI_RESULT '):])
        if not reply['ok']:
            raise ValueError(reply['error'])
        if payload['action'] == 'inspect':
            reply['result']['controller_received_ms'] = int(time.time()*1000)
            reply['result']['controller_received_tick'] = lease_clock()
        return reply['result']
    finally:
        remote.close()


def verify_account(store):
    account = store.get('account_id')
    if not isinstance(account, str) or not account:
        raise ValueError('configured_account_missing')
    token = store.get('access_token')
    if not isinstance(token, str) or not token:
        raise ValueError('grass_login_required')
    # Inspection must never issue replacement account credentials.
    user = request('https://api.grass.io/retrieveUser', {'Authorization': token})['result']['data']
    if user['userId'] != account:
        raise ValueError('grass_token_account_mismatch')
    return user


def provider_status(vault, profile, store, observation, pool):
    """Fetch bounded telemetry asynchronously; provider latency cannot delay grants."""
    context = dict(scope=observation['scope'], ip=observation['public_ip'],
                   device=decoded(store['wynd:device_id']),
                   process_identity=observation['processes'][0] if len(observation['processes']) == 1 else None)
    job = vault.provider_jobs.get(profile)
    if job is not None and job.done():
        vault.providers[profile] = job.result()
        del vault.provider_jobs[profile]
    previous = vault.providers.get(profile, {})
    at = int(time.time()*1000)
    same = all(previous.get(k) == v for k,v in context.items())
    if profile not in vault.provider_jobs and (not same or at>=previous.get('retry_ms',0)):
        def fetch():
            observed = int(time.time()*1000)
            result = dict(context, observed_ms=observed, retry_ms=observed+60_000, data=None, http=None)
            query = urllib.parse.urlencode({'input':json.dumps(
                {'deviceId':context['device'], 'ipAddress':context['ip']}, separators=(',', ':'))})
            try:
                token = vault.account(decoded(store['wynd:user_id']))['access_token']
                data = request('https://api.grass.io/retrieveDevice?'+query,
                               {'Authorization':token})['result']['data']
                result.update(http=200, data={k:data.get(k) for k in (
                    'deviceId', 'ipAddress', 'lastConnectedAt', 'isConnected', 'ipScore')})
            except urllib.error.HTTPError as error:
                after = error.headers.get('Retry-After', '')
                if after.isdigit():
                    result['retry_ms'] = observed+max(60,min(3600,int(after)))*1000
                result.update(http=error.code,error='provider_http_'+str(error.code))
            except Exception:
                result['error'] = 'provider_status_unavailable'
            return result
        vault.provider_jobs[profile] = pool.submit(fetch)
    return previous if same else dict(context,observed_ms=at,data=None,http=None,error='provider_check_pending')


def cycle(vault, names, capture_only, pool, telemetry_pool):
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
    for instance, authorizer in list(vault.authorizers.items()):
        if instance not in ids:
            authorizer['until'] = 0
            del vault.authorizers[instance]
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
            if lease and lease['until_tick']>lease_clock():
                unknown_accounts.add(lease['account'])
            continue
        packet = {'action':'inspect', 'include_ip':True}
        hook = None
        if not capture_only and lease and lease['observed_tick']>0 and lease_clock()-lease['observed_tick']<LEASE_SECONDS:
            saved = vault.get(lease['profile'])
            if saved and saved[0]==i['id'] and saved[1]['store'].get('wdi:kms_key'):
                scope = dict(group=i['group_id'],instance=i['id'],machine=i['machine_id'],
                             runtime=lease['runtime'],configured_account=i['configured_account'])
                packet['lease'] = dict(scope=scope,identity=identity(saved[1]['store']),ip=lease['ip'],
                                       sequence=vault.sequence())
                def renew(p=packet,l=lease,s=scope):
                    p['lease']['until'] = l['boot_time']+lease_clock()-l['observed_tick']+LEASE_SECONDS
                    vault.extend(s['instance'],l['profile'],s['runtime'],l['ip'])
                hook = renew
        jobs[pool.submit(exchange, i, vault.access, packet, hook)] = i
    for job in as_completed(jobs):
        i = jobs[job]
        try:
            obs = job.result()
            expected = dict(group=i['group_id'], instance=i['id'], machine=i['machine_id'])
            if i['configured_account']:
                expected['configured_account'] = i['configured_account']
            if any(obs['scope'].get(k) != value for k, value in expected.items()):
                raise ValueError('salad_runtime_identity_mismatch')
            if (not isinstance(obs.get('store'),(dict,type(None))) or not isinstance(obs.get('processes'),list)
                    or type(obs.get('restored')) is not bool or not isinstance(obs.get('boot_time'),(int,float))
                    or not isinstance(obs['scope'].get('runtime'),str)):
                raise ValueError('invalid_worker_observation')
            observed[i['id']] = obs
            lease = leases.get(i['id'])
            if not capture_only and lease and obs.get('public_ip') == lease['ip']:
                saved = vault.get(lease['profile'])
                if saved and identity(obs['store']) == identity(saved[1]['store']):
                    if obs.get('renewed'):
                        from kms_nodes import authorize
                        authorize(vault,i,lease['profile'],obs['scope'],lease['ip'])
                    # Start telemetry while other inspections finish, so its
                    # result can travel with this cycle's permission update.
                    provider_status(vault,lease['profile'],saved[1]['store'],obs,telemetry_pool)
            if lease and not capture_only and not obs.get('renewed'):
                # Keep the possible lifetime of every packet already sent.
                # Stop blind renewals after rejection so changed-IP/runtime
                # reservations can expire without shortening a concurrent grant.
                with vault.db:
                    vault.db.execute('UPDATE leases SET observed_tick=0 WHERE instance=? AND runtime=?',
                                     (i['id'], lease['runtime']))
                if not obs.get('public_ip') and lease['until_tick']>lease_clock():
                    unknown_accounts.add(lease['account'])
        except Exception as error:
            report[i['id']]['error'] = str(error) if type(error) is ValueError else type(error).__name__
            if i['id'] in leases and leases[i['id']]['until_tick']>lease_clock():
                unknown_accounts.add(leases[i['id']]['account'])
    actions = {}
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
            if capture_only:
                profile = local['public_sha256']
            else:
                assignment=vault.db.execute('SELECT profile FROM installations WHERE account=? '
                    'AND (instance=? OR (machine=? AND group_id=?)) ORDER BY instance=? DESC LIMIT 1',
                    (account,i['id'],i['machine_id'],i['group_id'],i['id'])).fetchone()
                profile=assignment[0] if assignment else vault.select(account,i['id'],occupied)
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
                    verify_account(vault.account(account))
                if saved and saved[1]['store'].get('wdi:kms_key'):
                    if local != identity(saved[1]['store']):
                        raise ValueError('worker_identity_changed')
                    value = dict(saved[1],machine_id=obs['machine_id'])
                else:
                    value = dict(saved[1] if saved else {'origin':scope},
                                 store={k:store[k] for k in IDENTITY_KEYS if k in store},
                                 machine_id=obs['machine_id'])
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
                raise ValueError('trusted_registration_required')
            if account in unknown_accounts and not (lease and lease['ip'] == obs['public_ip']):
                raise ValueError('existing_account_ip_inventory_incomplete')
            vault.admit(scope, profile, account, obs['public_ip'], obs['boot_time'],
                        obs.get('controller_received_ms',int(time.time()*1000)),
                        obs.get('controller_received_tick',lease_clock()))
            if local and (obs['processes'] or obs['restored']):
                payload = dict(action='allow', scope=scope, identity=original,
                               ip=obs['public_ip'], until=obs['boot_time']+LEASE_SECONDS)
            else:
                value = saved[1]
                vault.put(scope, value, occupied)
                payload = dict(action='restore', scope=scope,
                               ip=obs['public_ip'], until=obs['boot_time']+LEASE_SECONDS,
                               store=node_store(value['store']), machine_id=value['machine_id'])
            if payload['action'] == 'allow':
                payload['provider'] = provider_status(vault, profile, saved[1]['store'], obs, telemetry_pool)
            # Decision order survives asynchronous SSH delivery. A queued old
            # grant cannot acquire a newer sequence after a later hold is sent.
            payload['sequence'] = vault.sequence()
            def hook(s=scope, p=profile, o=obs, packet=payload, store=saved[1]['store']):
                packet['until'] = o['boot_time']+lease_clock()-o['controller_received_tick']+LEASE_SECONDS
                vault.extend(s['instance'],p,s['runtime'],o['public_ip'])
                if packet['action']=='allow':
                    packet['provider']=provider_status(vault,p,store,o,telemetry_pool)
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
            if action in ('restore','allow'):
                from kms_nodes import authorize
                authorize(vault,i,row['profile'],observed[i['id']]['scope'],row['ip'])
            elif i['id'] in vault.authorizers:
                vault.authorizers[i['id']]['until'] = 0
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
        saved = vault.db.execute('SELECT profile FROM installations WHERE account=? LIMIT 1', (account,)).fetchone()
        value = vault.get(saved[0])[1]['store'] if saved else None
        if not value:
            continue
        checked = int(time.time()*1000)
        try:
            session = vault.account(account)
            user = verify_account(session)
            vault.accounts[account] = dict(checked_ms=checked, observed_ms=checked,
                epoch_earnings=previous.get('epoch_earnings',{}),
                registered_devices=user.get('deviceCount'), device_cap=user.get('deviceCapOverride'), error=None)
            token = session['access_token']
            points = dict(previous.get('epoch_earnings',{}),checked_ms=checked)
            try:
                query = urllib.parse.urlencode({'input':json.dumps({'limit':1,'isLatestOnly':True})})
                epochs = request('https://api.grass.io/epochEarnings?'+query,{'Authorization':token})['result']['data']['data']
                if not isinstance(epochs,list) or not epochs or epochs[0].get('userId')!=account:
                    raise ValueError('epoch_account_mismatch')
                points.update(observed_ms=int(time.time()*1000),error=None,
                    record={k:epochs[0].get(k) for k in ('userId','epochId','epochName','stage','startDate','endDate',
                        'modified','proxyPoints','totalPoints','rewardPoints','referralPoints','totalUptime')})
            except Exception as error:
                points['error']=type(error).__name__
            vault.accounts[account]['epoch_earnings']=points
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
            vault.accounts[account]['epoch_earnings'] = dict(previous.get('epoch_earnings',{}),
                checked_ms=checked,error='account_observation_unavailable')
    rows = list(report.values())
    status = dict(observed_ms=int(time.time()*1000), cycle_seconds=round(time.time()-started,3),
                  groups=names, desired_allocations=sum(g['replicas'] for g in selected.values()),
                  allocations=len(rows), running=sum(r['state']=='running' for r in rows),
                  grass_running=sum(bool(r.get('processes')) for r in rows),
                  grass_process_observed=sum('processes' in r for r in rows),
                  grass_process_unknown=sum('processes' not in r for r in rows),
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
    publication = dict(schema_version=1,published_ms=status['observed_ms'],
        groups=[dict(id=g['id'],name=g['name'],account_id=g['container']['environment_variables']['GRASS_ACCOUNT_ID'])
                for g in selected.values() if g.get('container',{}).get('environment_variables',{}).get('GRASS_ACCOUNT_ID')],
        accounts={account:{k:value.get(k) for k in ('checked_ms','observed_ms','registered_devices',
                  'device_cap','error','epoch_earnings')} for account,value in vault.accounts.items()})
    public_path=Path('/run/wdi/grass-account-observation.json')
    public_path.parent.mkdir(mode=0o750,parents=True,exist_ok=True)
    os.chown(public_path.parent,-1,grp.getgrnam('salad').gr_gid)
    os.chmod(public_path.parent,0o750)
    public_temp=public_path.with_suffix('.tmp')
    public_temp.write_text(json.dumps(publication,separators=(',',':'))+'\n')
    os.chown(public_temp,-1,grp.getgrnam('salad').gr_gid)
    os.chmod(public_temp,0o640)
    public_temp.replace(public_path)
    for row in rows:
        if row['error']:
            emit('identity_blocked', instance=row['instance'], reason=row['error'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--group', action='append', help='Existing group names, comma-separated; repeat for batches')
    parser.add_argument('--initialize', type=int, metavar='COUNT',
                        help='Explicitly prepare and register this total on the trusted controller')
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
    suffix='.provision.lock' if args.initialize is not None else '.lock'
    with args.database.with_suffix(suffix).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        vault = Vault(args.database)
        if args.initialize is not None:
            while account_retry_delay():
                delay=account_retry_delay()
                emit('profile_capacity_waiting',retry_after_seconds=int(delay)+1,target=args.initialize)
                time.sleep(min(60,delay))
            seed = vault.account()
            user = verify_account(seed)
            account = seed['account_id']
            from kms_nodes import provision
            rows=vault.db.execute('SELECT profile,device,enrollment_started FROM installations WHERE account=?',
                                  (account,)).fetchall()
            cap,registered=user.get('deviceCapOverride'),user.get('deviceCount')
            if not isinstance(cap,int) or not isinstance(registered,int):
                raise ValueError('provider_device_cap_unavailable')
            needed=max(0,args.initialize-len(rows))+sum(device is None and not begun for _,device,begun in rows)
            if needed>max(0,cap-registered):
                raise ValueError('requested_profiles_exceed_remaining_device_cap')
            for profile,device,_begun in rows:
                if not vault.get(profile)[1]['store'].get('wdi:kms_key'):
                    raise ValueError('run_legacy_key_migration_before_initializing_capacity')
                provision(vault,profile)
                if device is None:
                    vault.register_profile(profile,seed)
            count=vault.db.execute('SELECT COUNT(*) FROM installations WHERE account=? AND device IS NOT NULL AND kms_key IS NOT NULL',
                                   (account,)).fetchone()[0]
            with vault.db:
                vault.db.execute('DELETE FROM key_creations WHERE kms_key IN (SELECT kms_key FROM installations)')
            initial=count
            while count<args.initialize:
                prepared_at=time.monotonic()
                vault.create_profile(seed)
                count=vault.db.execute('SELECT COUNT(*) FROM installations WHERE account=? AND device IS NOT NULL AND kms_key IS NOT NULL',(account,)).fetchone()[0]
                if count%10==0 or count==args.initialize:
                    emit('profile_capacity',profiles=count,target=args.initialize)
                time.sleep(max(0,3-(time.monotonic()-prepared_at)))
            emit('profiles_initialized', account=account, profiles=count,
                 newly_prepared=count-initial,
                 provider_registered_devices=user.get('deviceCount'),
                 registration_requested=needed>0)
            return
        failures = 0
        with ThreadPoolExecutor(max_workers=32) as pool, ThreadPoolExecutor(max_workers=8) as telemetry_pool:
            while True:
                try:
                    cycle(vault, names, args.capture, pool, telemetry_pool)
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
