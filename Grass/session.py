"""Restore saved Grass keys before the unchanged desktop starts or enrolls."""
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import ipaddress
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

STATE = Path('/config/xdg/data/io.getgrass.desktop/store.json')
PERMIT = Path('/run/wdi/grass-identity.json')
IP_POLICY = Path('/run/wdi/ip-policy.json')
IP_POLICY_REQUIRED = Path('/opt/grass-private/ip_policy.py').is_file()
MACHINE = Path('/config/run/machine-id')
LEASE_SECONDS = 300
IDENTITY_KEYS = ('wynd:device_id', 'wynd:device_privkey', 'wynd:device_pubkey',
                 'wynd:device_registered_user_id', 'wynd:device_registered_pubkey',
                 'wynd:user_id', 'wynd:browser_id')


def node_store(store):
    """Give native Grass an allocation-local key; its real signatures come from KMS."""
    original = identity(store)
    if not store.get('wdi:kms_key'):
        raise ValueError('kms_migration_required')
    private = os.urandom(32)
    result = subprocess.run(['openssl','pkey','-inform','DER','-pubout','-outform','DER'],
        input=bytes.fromhex('302e020100300506032b657004220420')+private,
        capture_output=True,timeout=5,check=True)
    public = base64.b64encode(result.stdout[-32:]).decode()
    value = {key: json.dumps(decoded(store[key])) for key in IDENTITY_KEYS if key!='wynd:device_privkey'}
    value.update({'wynd:device_privkey':json.dumps(base64.b64encode(private).decode()),
                  'wynd:device_pubkey':json.dumps(public),'wynd:device_registered_pubkey':json.dumps(public),
                  'wdi:identity':json.dumps(original,separators=(',',':'))})
    value.update({key: json.dumps(flag) for key, flag in {
        'wynd:authenticated': False, 'wynd:permissions': True,
        'isConnectButtonActive': True}.items()})
    return value


def require_node_store(store):
    if any(decoded(store.get(key)) not in (None, '', False, 0) for key in (
            'accessToken', 'refreshToken', 'wynd:jwt', 'lastUsedOAuthToken', 'wynd:authenticated')):
        raise ValueError('account_credentials_forbidden_on_worker')
    identity(store)
    if not isinstance(decoded(store.get('wdi:identity')),dict):
        raise ValueError('kms_worker_identity_required')


def decoded(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def identity(store, pending=False):
    """The same key/account checks as Grass 7.6.1's Vse and _M functions."""
    values = {key: decoded(store.get(key)) for key in IDENTITY_KEYS}
    enrolling = pending and values['wynd:device_id'] in (None, '')
    required = (('wynd:device_privkey', 'wynd:device_pubkey', 'wynd:user_id', 'wynd:browser_id')
                if enrolling else IDENTITY_KEYS)
    if store.get('wdi:kms_key'):
        required = tuple(k for k in required if k!='wynd:device_privkey')
    if not all(isinstance(values[key], str) and values[key] for key in required):
        raise ValueError('installation_keys_missing')
    try:
        if store.get('wdi:kms_key') and not values['wynd:device_privkey']:
            public = base64.b64decode(values['wynd:device_pubkey'],validate=True)
            private_hash = store.get('wdi:private_sha256')
            if len(public)!=32 or (private_hash is not None and (not isinstance(private_hash,str)
                    or len(private_hash)!=64 or any(c not in '0123456789abcdef' for c in private_hash))):
                raise ValueError('invalid_kms_identity')
            private = None
        else:
            private = base64.b64decode(values['wynd:device_privkey'], validate=True)
        if private is None:
            pass
        elif len(private) != 32:
            raise ValueError('invalid_ed25519_seed')
        else:
            result = subprocess.run(['openssl', 'pkey', '-inform', 'DER', '-pubout', '-outform', 'DER'],
                                    input=bytes.fromhex('302e020100300506032b657004220420') + private,
                                    capture_output=True, timeout=5, check=True)
            if len(result.stdout) != 44 or not result.stdout.startswith(bytes.fromhex('302a300506032b6570032100')):
                raise ValueError('invalid_ed25519_public_key')
            public = result.stdout[-32:]
            private_hash = hashlib.sha256(private).hexdigest()
    except (ValueError, TypeError, subprocess.SubprocessError):
        raise ValueError('installation_keys_invalid') from None
    public_b64 = base64.b64encode(public).decode()
    registered_public = values['wynd:device_registered_pubkey']
    if (public_b64 != values['wynd:device_pubkey'] or
            registered_public != public_b64 and not (enrolling and registered_public in (None, ''))):
        raise ValueError('installation_keys_mismatch')
    account = values['wynd:user_id']
    registered_account = values['wynd:device_registered_user_id']
    if registered_account != account and not (enrolling and registered_account in (None, '')):
        raise ValueError('installation_account_mismatch')
    observed = dict(account=account, device=values['wynd:device_id'] or None,
                browser=values['wynd:browser_id'],
                public_sha256=hashlib.sha256(public).hexdigest(),
                private_sha256=private_hash)
    reported = decoded(store.get('wdi:identity'))
    if reported is not None:
        if (not isinstance(reported,dict) or set(reported)!=set(observed)
                or any(reported[k]!=observed[k] for k in ('account','device','browser'))
                or any((reported[k] is None and k!='private_sha256') or
                       (reported[k] is not None and (not isinstance(reported[k],str) or len(reported[k])!=64
                        or any(c not in '0123456789abcdef' for c in reported[k])))
                       for k in ('public_sha256','private_sha256'))):
            raise ValueError('invalid_worker_identity')
        return reported
    return observed


def read(path):
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError('invalid_store')
    return value


def compatible(local, original):
    """Missing fields can be restored; conflicting surviving fields cannot."""
    expected = {key: decoded(original.get(key)) for key in IDENTITY_KEYS}
    # Registration markers may survive an interrupted first enrollment.
    for key, source in [('wynd:device_registered_user_id', 'wynd:user_id'),
                        ('wynd:device_registered_pubkey', 'wynd:device_pubkey')]:
        expected[key] = expected[key] or expected[source]
    return all(decoded(local.get(key)) in (None, '', expected[key]) for key in IDENTITY_KEYS)


@contextmanager
def directory(path):
    """Privileged writes must not follow app-controlled directory symlinks."""
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('absolute_state_path_required')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
                if os.geteuid() == 0 and path.is_relative_to('/config'):
                    os.chown(part, 1000, 1000, dir_fd=fd, follow_symlinks=False)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd);fd = child
        yield fd
    finally:
        os.close(fd)


def save(path, value):
    with directory(path.parent) as parent:
        temporary = path.name + '.restore-' + os.urandom(8).hex()
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            if os.geteuid() == 0 and path.is_relative_to('/config'):
                os.fchown(fd, 1000, 1000)
            if path in (IP_POLICY, Path('/run/wdi/provider-status.json')):
                os.fchmod(fd, 0o644)
            with os.fdopen(fd, 'w') as stream:
                if path == MACHINE:
                    stream.write(value)
                else:
                    json.dump(value, stream, separators=(',', ':'))
                stream.flush()
                os.fsync(stream.fileno())
            os.rename(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:pass


@contextmanager
def locked():
    """Serialize controller writes with the supervisor's live-start boundary."""
    PERMIT.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd = os.open(PERMIT.with_suffix('.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def runtime():
    # Salad injects these variables into the app and SSH exec environments.
    # Root SSH cannot read the unprivileged init's environ without ptrace.
    env = os.environ
    ticks = Path('/proc/1/stat').read_text().rsplit(') ', 1)[1].split()[19]
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    scope = {key: env.get(name) for key, name in
             [('group', 'SALAD_CONTAINER_GROUP_ID'), ('machine', 'SALAD_MACHINE_ID'), ('instance', 'SALAD_INSTANCE_ID')]}
    if not all(scope.values()):
        raise ValueError('salad_identity_missing')
    scope['runtime'] = f'{boot}:{ticks}'
    account = env.get('GRASS_ACCOUNT_ID')
    if account:
        scope['configured_account'] = account
    return scope


def grass_processes():
    result = []
    for entry in Path('/proc').glob('[0-9]*/comm'):
        try:
            if entry.read_bytes().strip() == b'grass-desktop' and entry.stat().st_uid == 1000:
                stat = (entry.parent / 'stat').read_bytes().rsplit(b') ', 1)[1].split()
                if stat[0] != b'Z':
                    result.append(f'{entry.parent.name}:{int(stat[19])}')
        except (OSError, ValueError, IndexError):
            pass
    return result


def permission():
    current = runtime()
    permit = read(PERMIT)
    if permit['runtime'] != current:
        raise ValueError('identity_restore_required_for_this_runtime')
    if permit.get('hold'):
        raise ValueError(permit['hold'])
    if time.clock_gettime(time.CLOCK_BOOTTIME) >= permit.get('until', 0):
        raise ValueError('identity_permission_expired')
    require_ip_policy(permit['ip'], current)
    return permit


def require_ip_policy(address, scope):
    if not IP_POLICY_REQUIRED:
        return
    policy = read(IP_POLICY) if IP_POLICY.exists() else {}
    if (policy.get('scope') != scope or policy.get('ip') != address or
            policy.get('allowed') is not True or policy.get('reallocation_requested') or
            time.clock_gettime(time.CLOCK_BOOTTIME) >= policy.get('valid_until', 0)):
        raise ValueError('ip_policy_not_admitted')


def ready(path=None):
    permit = permission()
    current = permit['runtime']
    original = permit['identity']
    store = read(STATE if path is None else path)
    require_node_store(store)
    observed = identity(store, pending=original['device'] is None)
    if current.get('configured_account', observed['account']) != observed['account']:
        raise ValueError('configured_account_changed')
    if observed != original:
        if original['device'] is not None or dict(observed, device=None) != original:
            raise ValueError('saved_installation_changed')
    if observed['device'] is None and (permit.get('enrollment_started') or any(
            decoded(store.get(key)) for key in ('wynd:device_registered_user_id',
                                               'wynd:device_registered_pubkey'))) and not grass_processes():
        raise ValueError('registration_recovery_required')
    return observed


@contextmanager
def starting(path=None):
    with locked():
        if grass_processes():
            raise ValueError('installation_already_running')
        observed = ready(path)
        permit = permission()
        # Persist before Popen: a crash or uncertain launch cannot start a
        # second enrollment. Only a completed registration unblocks it.
        permit['identity'] = observed
        if observed['device'] is None:
            permit['enrollment_started'] = True
        save(PERMIT, permit)
        permission()
        yield observed


def public_ip():
    query = urllib.request.Request('https://api.ipify.org?format=json', headers={'User-Agent': 'WDI/1.0'})
    with urllib.request.urlopen(query, timeout=5) as response:
        address = str(ipaddress.ip_address(json.load(response)['ip']))
    return dict(public_ip=address, ip_source='ipify', ip_observed_ms=int(time.time()*1000))


def ordered_permit(scope, sequence):
    try:
        permit = read(PERMIT)
    except FileNotFoundError:
        permit = {}
    current = permit.get('sequence', 0) if permit.get('runtime') == scope else 0
    if type(sequence) is not int or sequence <= current:
        raise ValueError('stale_permission_sequence')
    permit.update(runtime=scope, sequence=sequence)
    return permit


def grant(scope, expected, ip, until, sequence=None):
    with locked():
        return _grant(scope, expected, ip, until, sequence)


def check_deadline(until):
    # Use an already observed boot-clock deadline, not "receipt time + TTL".
    # A delayed request cannot resurrect an expired permission after suspend.
    remaining = until-time.clock_gettime(time.CLOCK_BOOTTIME) if isinstance(until,(float,int)) else 0
    if not 0 < remaining <= LEASE_SECONDS:
        raise ValueError('invalid_permission_duration')


def _grant(scope, expected, ip, until, sequence=None, permit=None):
    if permit is None:
        permit = ordered_permit(scope, sequence)
    check_deadline(until)
    require_ip_policy(ip, scope)
    store = read(STATE)
    require_node_store(store)
    actual = identity(store)
    if (dict(actual, device=expected['device']) != expected or
            expected['device'] is not None and actual['device'] != expected['device']):
        raise ValueError('saved_installation_changed')
    attempted = permit.get('enrollment_started', False)
    previous = permit.get('identity')
    if previous and (dict(actual, device=previous['device']) != previous or
                     previous['device'] is not None and actual['device'] != previous['device']):
        raise ValueError('saved_installation_changed')
    if actual['device'] is None and attempted and not grass_processes():
        raise ValueError('registration_recovery_required')
    permit.pop('hold', None)
    permit.update(identity=actual, ip=ip, until=until, enrollment_started=attempted)
    save(PERMIT, permit)


def exchange(request):
    if request['action'] == 'restore':
        require_node_store(request['store'])
        if set(request['store']) != set(IDENTITY_KEYS)|{'wdi:identity','wynd:authenticated','wynd:permissions','isConnectButtonActive'}:
            raise ValueError('unexpected_worker_store_fields')
    scope = runtime()
    if request['action'] == 'inspect':
        network = {}
        if request.get('include_ip'):
            try:
                network = public_ip()
            except (OSError, ValueError, KeyError) as error:
                network = dict(public_ip=None, ip_error=type(error).__name__)
        with locked():
            try:
                store = read(STATE)
            except FileNotFoundError:
                store = None
            renewed = False
            renewal_error = None
            lease = request.get('lease')
            if lease and lease['scope'] == scope and network.get('public_ip') == lease['ip']:
                try:
                    _grant(scope, lease['identity'], lease['ip'], lease['until'], lease.get('sequence'))
                    renewed = True
                except (OSError, ValueError, KeyError) as error:
                    renewal_error = str(error) if type(error) is ValueError else type(error).__name__
            try:
                ready()
                restored = True
                blocked = None
            except (OSError, ValueError, KeyError) as error:
                restored = False
                blocked = str(error) if type(error) is ValueError else 'identity_restore_required'
            try:
                status = read(Path('/run/wdi/status.json'))
            except (OSError, ValueError):
                status = {}
            try:
                permit = read(PERMIT)
            except FileNotFoundError:
                permit = {}
            return dict(scope=scope, store=store, processes=grass_processes(), **network,
                        boot_time=time.clock_gettime(time.CLOCK_BOOTTIME),
                        machine_id=MACHINE.read_text() if MACHINE.exists() else None,
                        managed=hashlib.sha256(Path('/opt/grass-private/session.py').read_bytes()).hexdigest(),
                        restored=restored, renewed=renewed, blocked=blocked, renewal_error=renewal_error,
                        sequence=permit.get('sequence', 0) if permit.get('runtime') == scope else 0,
                        enrollment_started=permit.get('enrollment_started', False),
                        ip_policy_required=IP_POLICY_REQUIRED,
                        ip_policy=read(IP_POLICY) if IP_POLICY.exists() else None,
                        fatal_reason=status.get('fatal_reason'))
    if request.get('scope') != scope:
        raise ValueError('runtime_changed')
    if request['action'] == 'hold':
        with locked():
            permit = ordered_permit(scope, request.get('sequence'))
            permit.update(hold='controller_hold', until=0)
            save(PERMIT, permit)
        return {'held': True, 'scope': scope}
    if request['action'] not in ('restore', 'allow'):
        raise ValueError('invalid_action')
    address = public_ip()['public_ip']
    if address != request.get('ip'):
        raise ValueError('public_ip_changed_before_admission')
    with locked():
        require_ip_policy(address, scope)
        permit = ordered_permit(scope, request.get('sequence'))
        if request['action'] == 'allow':
            if request.get('provider') is not None:
                save(Path('/run/wdi/provider-status.json'), request['provider'])
            _grant(scope, request['identity'], address, request['until'], request.get('sequence'))
            return {'identity': ready(), 'scope': scope}
        if grass_processes():
            raise ValueError('installation_already_running')
        store = dict(request['store'])
        original = identity(store, pending=request.get('pending') is True)
        if scope.get('configured_account', original['account']) != original['account']:
            raise ValueError('configured_account_changed')
        # Recover a complete registration from this allocation before applying
        # an older pending backup. Never overwrite the provider's device ID.
        if STATE.exists():
            local = read(STATE)
            if original['device'] is None and decoded(local.get('wynd:device_id')):
                recovered_store = dict(store, **{key: local[key] for key in IDENTITY_KEYS
                                                 if decoded(local.get(key))})
                recovered = identity(recovered_store)
                if dict(recovered, device=None) != original:
                    raise ValueError('local_installation_conflict')
                store = recovered_store
                original = recovered
            if decoded(local.get('wdi:identity')) is not None:
                if identity(local) != original:
                    raise ValueError('local_installation_conflict')
            elif not compatible(local, store):
                # A verified legacy copy can be replaced with its node-only
                # form. Never discard an unrelated surviving installation.
                if identity(local) != original:
                    raise ValueError('local_installation_conflict')
            if original['device'] is None and any(decoded(local.get(key)) for key in (
                    'wynd:device_registered_user_id', 'wynd:device_registered_pubkey')):
                raise ValueError('registration_recovery_required')
        if original['device'] is None and permit.get('enrollment_started'):
            raise ValueError('registration_recovery_required')
        previous = permit.get('identity')
        if previous and (dict(original, device=previous['device']) != previous or
                         previous['device'] is not None and original['device'] != previous['device']):
            raise ValueError('saved_installation_changed')
        store.pop('wynd:status', None)
        check_deadline(request['until'])
        # Reserve the ordering fence durably before changing installation
        # files. A failed restore remains held at this sequence.
        permit.update(identity=original, hold='restore_in_progress', until=0)
        save(PERMIT, permit)
        check_deadline(request['until'])
        save(STATE, store)
        if request.get('machine_id'):
            save(MACHINE, request['machine_id'])
        # This contains only fingerprints and public IDs, never a second key copy.
        _grant(scope, original, address, request['until'], permit=permit)
        return {'identity': ready(), 'scope': scope}


if __name__ == '__main__':
    os.umask(0o077)
    print('\nWDI_READY', flush=True)
    try:
        result = exchange(json.loads(sys.stdin.readline()))
        print('WDI_RESULT ' + json.dumps({'ok': True, 'result': result}), flush=True)
    except (OSError, ValueError, KeyError) as error:
        reason = str(error) if isinstance(error, ValueError) else type(error).__name__
        print('WDI_RESULT ' + json.dumps({'ok': False, 'error': reason}), flush=True)
