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
PERMIT = Path('/config/run/grass-identity.json')
IP_POLICY = Path('/config/run/ip-policy.json')
IP_POLICY_REQUIRED = Path('/opt/grass-private/ip_policy.py').is_file()
MACHINE = Path('/config/run/machine-id')
LEASE_SECONDS = 300
IDENTITY_KEYS = ('wynd:device_id', 'wynd:device_privkey', 'wynd:device_pubkey',
                 'wynd:device_registered_user_id', 'wynd:device_registered_pubkey',
                 'wynd:user_id', 'wynd:browser_id')


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
    if not all(isinstance(values[key], str) and values[key] for key in required):
        raise ValueError('installation_keys_missing')
    try:
        private = base64.b64decode(values['wynd:device_privkey'], validate=True)
        if len(private) != 32:
            raise ValueError('invalid_ed25519_seed')
        # OpenSSL is already in the deployed image. Private bytes travel only
        # through stdin, never command arguments or a temporary key file.
        result = subprocess.run(['openssl', 'pkey', '-inform', 'DER', '-pubout', '-outform', 'DER'],
                                input=bytes.fromhex('302e020100300506032b657004220420') + private,
                                capture_output=True, timeout=5, check=True)
        prefix = bytes.fromhex('302a300506032b6570032100')
        if len(result.stdout) != len(prefix) + 32 or not result.stdout.startswith(prefix):
            raise ValueError('invalid_ed25519_public_key')
        public = result.stdout[len(prefix):]
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
    if enrolling:
        token = decoded(store.get('accessToken'))
        if decoded(store.get('wynd:authenticated')) is not True or not isinstance(token, str) or not token:
            raise ValueError('grass_login_required')
    return dict(account=account, device=values['wynd:device_id'] or None,
                browser=values['wynd:browser_id'],
                public_sha256=hashlib.sha256(public).hexdigest(),
                private_sha256=hashlib.sha256(private).hexdigest())


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


def save(path, value):
    owner = Path('/config').stat() if os.geteuid() == 0 else None
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if owner:
        for directory in missing:
            os.chown(directory, owner.st_uid, owner.st_gid)
    temporary = path.with_name(path.name + '.restore')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    if owner:
        os.fchown(fd, owner.st_uid, owner.st_gid)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, separators=(',', ':'))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def locked():
    """Serialize controller writes with the supervisor's live-start boundary."""
    new_parent = not PERMIT.parent.exists()
    PERMIT.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(PERMIT.with_suffix('.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if os.geteuid() == 0:
            owner = Path('/config').stat()
            if new_parent:
                os.chown(PERMIT.parent, owner.st_uid, owner.st_gid)
            os.fchown(fd, owner.st_uid, owner.st_gid)
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
    session_file = Path(env.get('GRASS_SESSION_FILE', '/run/secrets/grass-session.json'))
    encoded = env.get('GRASS_SESSION_B64') or env.get('GRASS_STATE_B64')
    raw = session_file.read_bytes() if session_file.is_file() else base64.b64decode(encoded, validate=True) if encoded else None
    if raw:
        session_account = decoded(json.loads(raw).get('wynd:user_id'))
        if not isinstance(session_account, str) or not session_account:
            raise ValueError('configured_account_missing')
        if account and account != session_account:
            raise ValueError('configured_account_changed')
        account = session_account
    if account:
        scope['configured_account'] = account
    return scope


def grass_processes():
    result = []
    for entry in Path('/proc').glob('[0-9]*/comm'):
        try:
            if entry.read_text().strip() == 'grass-desktop':
                stat = (entry.parent / 'stat').read_text().rsplit(') ', 1)[1].split()
                if stat[0] != 'Z':
                    result.append(f'{entry.parent.name}:{stat[19]}')
        except FileNotFoundError:
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
    if sequence is None:
        if current:
            raise ValueError('permission_sequence_required')
        sequence = 0
    elif type(sequence) is not int or sequence <= current:
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
    actual = identity(read(STATE), pending=True)
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
                status = read(Path('/config/run/status.json'))
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
            if not compatible(local, store):
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
            MACHINE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            MACHINE.write_text(request['machine_id'])
            if os.geteuid() == 0:
                owner = Path('/config').stat()
                os.chown(MACHINE, owner.st_uid, owner.st_gid)
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
