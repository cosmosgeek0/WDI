"""Import device signing keys into KMS and verify them before retiring active copies."""
import base64
import json
import os
import time
import ssl
import io
import http.client
import threading
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from session import decoded, identity


def provision(vault, profile):
    import boto3
    from grass import REGION
    kms = boto3.client('kms',region_name=REGION)
    previous = vault.get(profile)
    if previous is None:
        raise ValueError('unknown_device_profile')
    owner,value = previous
    store = value['store']
    original = identity(store,pending=True)
    if store.get('wdi:kms_key'):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        message=b'WDI verifies active node signing permission'
        signature=kms.sign(KeyId=store['wdi:kms_key'],Message=message,MessageType='RAW',
                           SigningAlgorithm='ED25519_SHA_512')['Signature']
        Ed25519PublicKey.from_public_bytes(base64.b64decode(decoded(store['wynd:device_pubkey']))).verify(signature,message)
        kms.close()
        return store['wdi:kms_key']
    seed = base64.b64decode(decoded(store['wynd:device_privkey']),validate=True)
    row = vault.db.execute('SELECT kms_key FROM installations WHERE profile=?',(profile,)).fetchone()
    key_id = row[0]
    if not key_id:
        reply = kms.create_key(KeySpec='ECC_NIST_EDWARDS25519',KeyUsage='SIGN_VERIFY',Origin='EXTERNAL',
            Description='WDI Grass device '+str(original['device']),
            Tags=[{'TagKey':'wdi-purpose','TagValue':'grass-node-auth'},
                  {'TagKey':'wdi-profile','TagValue':profile}])
        key_id = reply['KeyMetadata']['Arn']
        with vault.db:
            vault.db.execute('UPDATE installations SET kms_key=? WHERE profile=?',(key_id,profile))
    state = kms.describe_key(KeyId=key_id)['KeyMetadata']['KeyState']
    if state == 'PendingImport':
        wrapping = kms.get_parameters_for_import(KeyId=key_id,
            WrappingAlgorithm='RSAES_OAEP_SHA_256',WrappingKeySpec='RSA_4096')
        public = serialization.load_der_public_key(wrapping['PublicKey'])
        encrypted = public.encrypt(bytes.fromhex('302e020100300506032b657004220420')+seed,
                                   padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=None))
        kms.import_key_material(KeyId=key_id,ImportToken=wrapping['ImportToken'],EncryptedKeyMaterial=encrypted,
                                ExpirationModel='KEY_MATERIAL_DOES_NOT_EXPIRE')
    public = serialization.load_der_public_key(kms.get_public_key(KeyId=key_id)['PublicKey'])
    expected = base64.b64decode(decoded(store['wynd:device_pubkey']),validate=True)
    if public.public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw) != expected:
        raise ValueError('kms_device_public_key_mismatch')
    message = b'WDI verifies imported Grass device key'
    signature = kms.sign(KeyId=key_id,Message=message,MessageType='RAW',SigningAlgorithm='ED25519_SHA_512')['Signature']
    public.verify(signature,message)
    safe = dict(store)
    safe.pop('wynd:device_privkey')
    safe.update({'wdi:kms_key':key_id,'wdi:private_sha256':original['private_sha256']})
    if identity(safe,pending=True) != original:
        raise ValueError('kms_migration_changed_identity')
    value = dict(value,store=safe)
    row = vault.db.execute('SELECT account,version,ciphertext FROM installations WHERE profile=?',(profile,)).fetchone()
    account,version,encrypted = row
    recovery = vault.path.parent/'key-recovery'
    recovery.mkdir(mode=0o700,exist_ok=True)
    record = recovery/(profile+'.json')
    if not record.exists():
        with tempfile.NamedTemporaryFile(mode='w',dir=recovery,delete=False) as backup:
            temp = Path(backup.name)
            json.dump(dict(profile=profile,account=account,version=version,
                ciphertext=base64.b64encode(encrypted).decode()),backup)
            backup.flush();os.fsync(backup.fileno())
        temp.replace(record)
        directory = os.open(recovery,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    archived = json.loads(record.read_bytes())
    raw = base64.b64decode(archived['ciphertext'],validate=True)
    recovered = json.loads(vault.cipher(archived['version']).decrypt(
        raw[:12],raw[12:],f'{account}/{profile}'.encode()))
    if (archived['profile']!=profile or archived['account']!=account
            or identity(recovered['store'],pending=True)!=original
            or recovered['machine_id']!=value['machine_id']):
        raise ValueError('kms_recovery_identity_mismatch')
    directory=os.open(recovery,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(directory)
    finally:os.close(directory)
    with vault.db:
        nonce = os.urandom(12)
        packed = nonce+vault.cipher(vault.version).encrypt(nonce,json.dumps(value,separators=(',',':')).encode(),f'{account}/{profile}'.encode())
        vault.db.execute('UPDATE installations SET version=?,ciphertext=? WHERE profile=?',(vault.version,packed,profile))
    if vault.get(profile)!=(owner,value):
        raise ValueError('kms_migration_readback_failed')
    return key_id


def tls_checkin(remote, authorization, device, timestamp, body):
    """Validate Grass TLS on Mumbai; remote relays cannot read the signature."""
    incoming,outgoing = ssl.MemoryBIO(),ssl.MemoryBIO()
    deadline = time.monotonic()+25
    tls = ssl.create_default_context().wrap_bio(incoming,outgoing,server_side=False,
                                               server_hostname='director.getgrass.io')
    def pump():
        if time.monotonic()>deadline:
            raise TimeoutError('director_tls_exchange_timeout')
        remote.send({'action':'tls','data':base64.b64encode(outgoing.read()).decode()})
        line = remote.line()
        if not line.startswith('WDI_RESULT '):raise ValueError('invalid_auth_relay')
        reply = json.loads(line[len('WDI_RESULT '):])
        data = base64.b64decode(reply['data'],validate=True)
        if len(data)>65536:raise ValueError('oversized_auth_record')
        if data:incoming.write(data)
        else:incoming.write_eof()
    while True:
        try:tls.do_handshake();break
        except (ssl.SSLWantReadError,ssl.SSLWantWriteError):pump()
    headers = ('POST /devices/checkin HTTP/1.1\r\nHost: director.getgrass.io\r\n'
               'Content-Type: application/json\r\nConnection: close\r\n'
               'User-Agent: WDI/1.0\r\nAuthorization: '+authorization+'\r\n'
               'x-wynd-device-id: '+device+'\r\nx-wynd-ts: '+str(timestamp)+'\r\n'
               'Content-Length: '+str(len(body))+'\r\n\r\n').encode()
    tls.write(headers+body)
    pump()
    raw = b''
    while True:
        try:
            data = tls.read(65536)
            if not data:break
            raw += data
            if len(raw)>100000:raise ValueError('oversized_director_response')
        except (ssl.SSLWantReadError,ssl.SSLWantWriteError):pump()
        except (ssl.SSLZeroReturnError,ssl.SSLEOFError):break
    class ResponseSocket:
        def makefile(self,*_args,**_kwargs):return io.BytesIO(raw)
    response = http.client.HTTPResponse(ResponseSocket())
    response.begin()
    data = response.read(65537)
    if len(data)>65536:raise ValueError('oversized_director_response')
    return response.status,data


def authorize(vault, instance, profile, scope, address):
    """One authenticated control connection per allocation; no bandwidth proxy."""
    from grass import Shell, REGION, emit, lease_clock
    import boto3
    stored = vault.get(profile)[1]['store']
    original = identity(stored)
    key_id = stored.get('wdi:kms_key')
    if not key_id:raise ValueError('kms_migration_required')
    previous = vault.authorizers.get(instance['id'])
    if (previous and previous['scope']==scope and previous['key']==key_id
            and previous['address']==address and previous['until']>lease_clock()
            and previous['thread'].is_alive()):
        previous['until'] = lease_clock()+300
        return
    if previous:
        previous['until']=0
        if previous.get('remote'):previous['remote'].close()
    state = dict(scope=scope,key=key_id,address=address,until=lease_clock()+300,error=None,last_sign=0)
    def run():
        from botocore.config import Config
        kms = boto3.client('kms',region_name=REGION,
                           config=Config(connect_timeout=3,read_timeout=5,retries={'max_attempts':1}))
        remote = None
        while lease_clock()<state['until']:
            try:
                remote = Shell(instance,vault.access,'node_auth.py relay')
                state['remote'] = remote
                while lease_clock()<state['until']:
                    remote.send({'action':'poll','runtime':scope})
                    line = remote.line()
                    if not line.startswith('WDI_RESULT '):raise ValueError('invalid_auth_relay')
                    request = json.loads(line[len('WDI_RESULT '):])
                    if request.get('wait'):continue
                    body = base64.b64decode(request['body'],validate=True)
                    data = json.loads(body)
                    if (request['runtime']!=scope or request['device']!=original['device']
                            or data.get('browser_id')!=original['browser'] or len(body)>16384
                            or any(data.get(k)!=v for k,v in {'device_type':'desktop','os':'linux',
                                'version':'7.6.1','extension_id':'desktop'}.items())
                            or lease_clock()>=state['until']):
                        raise ValueError('node_checkin_identity_changed')
                    expected_fields={'browser_id','user_agent','device_type','os','version','extension_id'}
                    if (set(data)!=expected_fields or not isinstance(data['user_agent'],str)
                            or not 0<len(data['user_agent'])<=1024):
                        raise ValueError('unexpected_checkin_fields')
                    body=json.dumps(dict(browser_id=original['browser'],user_agent=data['user_agent'],
                        device_type='desktop',os='linux',version='7.6.1',extension_id='desktop'),
                        separators=(',',':')).encode()
                    if time.monotonic()-state['last_sign']<5:
                        raise ValueError('node_checkin_rate_limited')
                    state['last_sign']=time.monotonic()
                    timestamp = int(time.time()*1000)
                    message = f"CHECKIN1\n{original['device']}\n{timestamp}".encode()
                    signature = kms.sign(KeyId=key_id,Message=message,MessageType='RAW',
                                         SigningAlgorithm='ED25519_SHA_512')['Signature']
                    if lease_clock()>=state['until']:
                        raise ValueError('node_authorization_expired')
                    status,result = tls_checkin(remote,'Grass-Checkin v=1, sig='+base64.b64encode(signature).decode(),
                                                original['device'],timestamp,body)
                    if status==201:
                        admission = json.loads(result)
                        token = admission['token']
                        claim = token.split('.')[1]
                        claims = json.loads(base64.urlsafe_b64decode(claim+'='*((-len(claim))%4)))
                        audience = claims.get('aud')
                        if (address not in (audience if isinstance(audience,list) else [audience])
                                or claims.get('iss')!='director-server'
                                or type(claims.get('exp')) is not int or type(claims.get('iat')) is not int
                                or not 0<claims['exp']-claims['iat']<=30
                                or abs(time.time()-claims['iat'])>30 or claims['exp']<=time.time()):
                            raise ValueError('director_token_scope_mismatch')
                        result = json.dumps({'bridge_ip':admission['bridge_ip'],'token':token},
                                            separators=(',',':')).encode()
                    else:
                        # Never forward diagnostic text that could echo an authorization header.
                        result = b'{"error":"director_rejected_node_authentication"}'
                    if lease_clock()>=state['until']:
                        raise ValueError('node_authorization_expired')
                    remote.send({'action':'finish','status':status,'body':base64.b64encode(result).decode()})
                    if not remote.line().startswith('WDI_RESULT '):raise ValueError('auth_delivery_failed')
                    emit('node_authentication',instance=instance['id'],device=original['device'],http=status,
                         signature_exposed_to_worker=False)
                    state['error']=None
            except Exception as error:
                reason = str(error) if type(error) is ValueError else type(error).__name__
                if reason!=state['error']:
                    emit('node_authentication_unavailable',instance=instance['id'],reason=reason)
                    state['error']=reason
                if lease_clock()<state['until']:time.sleep(3)
            finally:
                if remote:remote.close();remote=None
                state['remote'] = None
        kms.close()
    state['thread']=threading.Thread(target=run,daemon=True)
    vault.authorizers[instance['id']]=state
    state['thread'].start()


if __name__ == '__main__':
    import fcntl
    from grass import Vault
    path=Path('/var/lib/wdi/grass.sqlite3')
    os.umask(0o077)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        vault=Vault(path)
        for profile, in vault.db.execute('SELECT profile FROM installations').fetchall():
            key=provision(vault,profile)
            print(json.dumps({'profile':profile,'kms_key':key,'verified':True}),flush=True)
