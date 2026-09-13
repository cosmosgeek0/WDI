"""Linux controller regression checks; synthetic credentials and no provider calls."""
import base64,contextlib,copy,io,json,os,sys,tempfile,threading,time
from concurrent.futures import ThreadPoolExecutor,TimeoutError as FutureTimeout
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
import grass,session

class Fixture:
 def __init__(self,root,count=1,duplicate=False):
  self.account='test-account';self.groups=[{'id':'group-a','name':'test-a','replicas':count}]
  self.nodes={};self.interrupted=set();self.registrations=0;self.calls=[];self.fail=set();self.inflight=0;self.peak=0;self.lock=threading.Lock()
  secret={'VersionId':'fixture-v1','SecretString':json.dumps({'ssh_private_key':'fixture-only-encryption-input','api_key':'fixture-only'})}
  with patch.object(grass.boto3,'client',return_value=type('Secrets',(),{'get_secret_value':lambda *_a,**_k:secret})()):
   self.v=grass.Vault(Path(root)/'state.sqlite3')
  seed={k:json.dumps(v) for k,v in {'accessToken':'fixture-only-access','refreshToken':'fixture-only-refresh','tokenExpiry':9999999999,'wynd:authenticated':True,'wynd:user_id':self.account}.items()}
  for n in range(count):
   self.v.create_profile(seed)
   self.add(f'instance-{n:03}',f'198.18.{n//250}.{1+n%250}')
  if duplicate:self.nodes['instance-001']['ip']=self.nodes['instance-000']['ip']
 def add(self,id,ip,group='group-a'):
  self.nodes[id]={'id':id,'group':group,'machine_id':'machine-'+id,'state':'running','ssh_ip':'fixture-gateway','ssh_port':22,'ssh_host_key_fingerprint':'fixture','ip':ip,'store':None,'active':False,'permit':False,'until':0,'sequence':0,'runtime':'boot-'+id,'machine':None,'boot_time':time.clock_gettime(time.CLOCK_BOOTTIME)}
 def request(self,url,headers,payload=None):
  if url==grass.API:return {'items':self.groups}
  if url.startswith(grass.API):
   name=url.split('/')[-2];group=next(g for g in self.groups if g['name']==name)
   return {'instances':[{k:n[k] for k in ['id','machine_id','state','ssh_ip','ssh_port','ssh_host_key_fingerprint']} for n in self.nodes.values() if n['group']==group['id']]}
  if 'retrieveUser' in url:return {'result':{'data':{'userId':self.account,'deviceCount':450+self.registrations,'deviceCapOverride':5000}}}
  if 'epochEarnings' in url:return {'result':{'data':{'data':[]}}}
  raise AssertionError(url)
 def exchange(self,i,access,payload,before_send=None):
  with self.lock:self.inflight+=1;self.peak=max(self.peak,self.inflight)
  try:
   if before_send:before_send()
   time.sleep(.006)
   if i['id'] in self.fail:raise TimeoutError('fixture timeout')
   n=self.nodes[i['id']];scope=dict(group=n['group'],instance=n['id'],machine=n['machine_id'],runtime=n['runtime'],configured_account=self.account)
   self.calls.append((i['id'],payload['action']))
   if payload['action']=='inspect':
    renewed=False;l=payload.get('lease')
    if l and l['scope']==scope and l['ip']==n['ip'] and n['store']:
     expected=grass.identity(n['store'],pending=True)
     if dict(expected,device=l['identity']['device'])==l['identity'] and 0<l['until']-time.clock_gettime(time.CLOCK_BOOTTIME)<=session.LEASE_SECONDS and l['sequence']>n['sequence']:
      n['permit']=True;n['until']=l['until'];n['sequence']=l['sequence'];renewed=True
    return dict(scope=scope,store=copy.deepcopy(n['store']),processes=['fixture-process'] if n['active'] else [],public_ip=n['ip'],ip_observed_ms=int(time.time()*1000),machine_id=n['machine'],managed=grass.SOURCE_SHA,restored=n['permit'] and n['until']>time.clock_gettime(time.CLOCK_BOOTTIME),renewed=renewed,sequence=n['sequence'],boot_time=time.clock_gettime(time.CLOCK_BOOTTIME))
   assert payload['scope']==scope
   assert isinstance(payload['sequence'],int) and payload['sequence']>n['sequence']
   n['sequence']=payload['sequence']
   if payload['action']=='hold':n['active']=False;n['permit']=False;return {'held':True}
   assert payload['ip']==n['ip']
   assert 0<payload['until']-time.clock_gettime(time.CLOCK_BOOTTIME)<=session.LEASE_SECONDS
   if payload['action']=='restore':
    assert not n['active'];n['store']=copy.deepcopy(payload['store']);n['machine']=payload['machine_id']
   n['permit']=True;n['until']=payload['until']
   return {'identity':grass.identity(n['store'],pending=True),'scope':scope}
  finally:
   with self.lock:self.inflight-=1
 def native(self):
  # Launch and provider completion happen after the SSH reply. An accepted
  # registration can disappear before the app writes its device ID to disk.
  for n in self.nodes.values():
   if n['until']<=time.clock_gettime(time.CLOCK_BOOTTIME):n['permit']=False
   if not n['permit']:n['active']=False
   elif not n['active']:
    n['active']=True
    if grass.identity(n['store'],pending=True)['device'] is None:
     self.registrations+=1
     if n['id'] in self.interrupted:continue
     n['store'].update({k:json.dumps(v) for k,v in {'wynd:device_id':'fixture-device-'+str(self.registrations),'wynd:device_registered_user_id':self.account,'wynd:device_registered_pubkey':grass.decoded(n['store']['wynd:device_pubkey'])}.items()})
    n['store']['wynd:status']=json.dumps('CONNECTED')
 def cycle(self):
  for g in self.groups:g.setdefault('container',{'environment_variables':{'GRASS_ACCOUNT_ID':self.account}})
  with patch.object(grass,'request',self.request),patch.object(grass,'exchange',self.exchange),ThreadPoolExecutor(max_workers=8) as pool,contextlib.redirect_stdout(io.StringIO()):
   grass.cycle(self.v,[g['name'] for g in self.groups],False,pool)
  self.native()

with tempfile.TemporaryDirectory(prefix='wdi-fixture-') as root:
 f=Fixture(root,100,duplicate=True);f.cycle();f.cycle()
 assert f.registrations==99 and sum(n['active'] for n in f.nodes.values())==99
 assert f.v.db.execute('SELECT COUNT(*) FROM installations').fetchone()[0]==100
 assert f.v.db.execute('SELECT COUNT(*) FROM leases').fetchone()[0]==99
 assert f.peak==8
 # A duplicate address must not consume the last unused profile.
 f.nodes['instance-001']['ip']='198.19.0.1';f.cycle();f.cycle();assert f.registrations==100
 before={i:grass.identity(n['store']) for i,n in f.nodes.items()}
 f.nodes['instance-001']['ip']=f.nodes['instance-000']['ip'];f.cycle()
 assert not f.nodes['instance-001']['active'] and f.nodes['instance-000']['active']
 assert f.registrations==100
 f.nodes['instance-001']['ip']='198.19.0.1';f.cycle();assert f.nodes['instance-001']['active']
 assert all(grass.identity(n['store'])==before[i] for i,n in f.nodes.items())
 # All inspected accounts with an unreachable admitted node hold newcomers.
 f.fail.add('instance-000');f.add('new-unverified','198.19.0.2');f.cycle()
 assert not f.nodes['new-unverified']['active'] and f.registrations==100
 # Incomplete API inventory aborts before new SSH permissions are issued.
 calls=len(f.calls)
 with patch.object(grass,'request',side_effect=OSError('fixture outage')),ThreadPoolExecutor(max_workers=8) as pool:
  try:grass.cycle(f.v,['test-a'],False,pool);raise AssertionError('inventory failure accepted')
  except OSError:pass
 assert len(f.calls)==calls
 print('PASS: 100 synthetic allocations, 8 concurrent inspections, duplicate IP admission, IP collision pause/resume, inventory/SSH outage')
with tempfile.TemporaryDirectory(prefix='wdi-fixture-') as root:
 f=Fixture(root);f.cycle();f.cycle();old=f.nodes.pop('instance-000');ident=grass.identity(old['store'])
 f.add('replacement','198.19.1.1');f.cycle()
 assert not f.nodes['replacement']['active'] and f.registrations==1
 # Simulate the old holder reaching its already-issued deadline; the native
 # deadline itself is exercised below and in the live pilot outage test.
 old['active']=False
 with f.v.db:f.v.db.execute('UPDATE leases SET until_ms=0,until_tick=0')
 f.cycle();f.cycle();assert grass.identity(f.nodes['replacement']['store'])==ident and f.registrations==1
 # Same-process ownership cannot be cloned into another live allocation.
 f.add('clone','198.19.1.2');f.nodes['clone'].update(store=copy.deepcopy(old['store']),active=True,machine=old['machine'])
 f.cycle();assert not f.nodes['clone']['active'] and f.registrations==1
 print('PASS: absent old instance remains fenced until lease deadline; replacement reuses keys/device; copied live identity rejected')
 # Real local permission code rejects delayed grants and expires in boot time.
 root=Path(root);scope={'group':'fixture','instance':'fixture','machine':'fixture','runtime':'fixture'}
 with patch.object(session,'STATE',root/'store.json'),patch.object(session,'PERMIT',root/'permit.json'),patch.object(session,'runtime',return_value=scope),patch.object(session.os,'geteuid',return_value=1000):
  (root/'store.json').write_text(json.dumps(f.nodes['replacement']['store']))
  session.grant(scope,ident,'198.19.1.1',time.clock_gettime(time.CLOCK_BOOTTIME)+.1)
  assert session.permission()['identity']==ident
  time.sleep(.15)
  try:session.permission();raise AssertionError('expired lease allowed')
  except ValueError as e:assert str(e)=='identity_permission_expired'
  try:session.grant(scope,ident,'198.19.1.1',time.clock_gettime(time.CLOCK_BOOTTIME)-1);raise AssertionError('delayed grant accepted')
  except ValueError:pass
 print('PASS: actual permission expiry and delayed-packet rejection')
with tempfile.TemporaryDirectory(prefix='wdi-fixture-') as root:
 f=Fixture(root,2,duplicate=True)
 f.groups[0]['replicas']=1;f.groups.append({'id':'group-b','name':'test-b','replicas':1});f.nodes['instance-001']['group']='group-b'
 f.cycle();f.cycle();assert f.registrations==1
 status=json.loads(f.v.path.with_name('grass-status.json').read_text())
 assert len(status['groups'])==2 and status['desired_allocations']==2 and status['observed_unique_ips']==1
 assert sum(n['active'] for n in f.nodes.values())==1
 print('PASS: duplicate IP blocked across independent rollout groups')
with tempfile.TemporaryDirectory(prefix='wdi-fixture-') as root:
 f=Fixture(root)
 profile=f.v.db.execute('SELECT profile FROM installations').fetchone()[0]
 store=f.v.get(profile)[1]['store']
 seed={k:v for k,v in store.items() if k not in session.IDENTITY_KEYS or k=='wynd:user_id'}
 nonce=os.urandom(12)
 packed=nonce+f.v.cipher(f.v.version).encrypt(nonce,json.dumps(seed).encode(),b'wdi-grass-account-session-v1')
 envelope=Path(root)/'account.enc'
 envelope.write_text(json.dumps({'version':f.v.version,'ciphertext':base64.b64encode(packed).decode()}))
 with patch.object(sys,'argv',['grass.py','--initialize','5000','--database',str(f.v.path),'--account-session',str(envelope)]),patch.object(grass.boto3,'client',return_value=f.v.secrets),patch.object(grass,'request',f.request):
  try:grass.main();raise AssertionError('provider cap ignored')
  except ValueError as e:assert str(e)=='requested_profiles_exceed_remaining_device_cap'
 assert f.v.db.execute('SELECT COUNT(*) FROM installations').fetchone()[0]==1
 print('PASS: pool initialization respects historical registrations and remaining provider cap')
with tempfile.TemporaryDirectory(prefix='wdi-fixture-') as root:
 f=Fixture(root);original_exchange=f.exchange
 def wrong_account(i,access,payload,before_send=None):
  result=original_exchange(i,access,payload,before_send)
  if payload['action']=='inspect':result['scope']['configured_account']='unconfigured-account'
  return result
 f.exchange=wrong_account;f.cycle()
 assert f.registrations==0 and not f.nodes['instance-000']['active']
 assert f.v.db.execute('SELECT COUNT(*) FROM leases').fetchone()[0]==0
 assert all(action=='inspect' for _,action in f.calls)
 print('PASS: runtime cannot request another account beyond its Salad group configuration')
with tempfile.TemporaryDirectory(prefix='wdi-fixture-') as root:
 from urllib.error import HTTPError
 f=Fixture(root);f.groups[0]['container']={'environment_variables':{'GRASS_ACCOUNT_ID':f.account}}
 def limited(url,headers,payload=None):
  if url.endswith('/instances'):raise HTTPError(url,429,'fixture limit',{'Retry-After':'120'},None)
  return f.request(url,headers,payload)
 class EndPause(BaseException):pass
 pauses=[]
 def pause(seconds):pauses.append(seconds);raise EndPause()
 with patch.object(sys,'argv',['grass.py','--group','test-a','--database',str(f.v.path)]),patch.object(grass.boto3,'client',return_value=f.v.secrets),patch.object(grass,'request',limited),patch.object(grass.time,'sleep',pause),contextlib.redirect_stdout(io.StringIO()):
  try:grass.main()
  except EndPause:pass
 assert pauses==[120] and f.registrations==0
 assert json.loads(f.v.path.with_name('grass-status.json').read_text())['error']=='HTTPError'
 print('PASS: API429 Retry-After pauses admission and exposes stale/error status')
with tempfile.TemporaryDirectory(prefix='wdi-fixture-') as root:
 f=Fixture(root);f.groups=f.groups*100
 try:f.cycle();raise AssertionError('possibly truncated inventory accepted')
 except ValueError as e:assert str(e)=='group_inventory_may_be_truncated'
 assert not f.calls and f.registrations==0
 print('PASS: API inventory boundary fails closed before any credentials are sent')
with tempfile.TemporaryDirectory(prefix='wdi-fixture-') as root:
 f=Fixture(root);f.cycle();f.cycle()
 with f.v.db:f.v.db.execute('UPDATE leases SET boot_time=boot_time-200,observed_ms=observed_ms-200000,observed_tick=observed_tick-200')
 original_exchange=f.exchange;remaining=[]
 def delayed_cycle(i,access,payload,before_send=None):
  def sent():
   if before_send:before_send()
   if payload.get('lease'):remaining.append(payload['lease']['until']-time.clock_gettime(time.CLOCK_BOOTTIME))
  return original_exchange(i,access,payload,sent)
 f.exchange=delayed_cycle;f.cycle()
 assert remaining and all(280<x<=300 for x in remaining),remaining
 assert f.registrations==1
 print('PASS: delayed inspection cycles renew a full bounded grace period rather than an old deadline')

for replacement in (False,True):
 with tempfile.TemporaryDirectory(prefix='wdi-fixture-') as root:
  f=Fixture(root);f.interrupted.add('instance-000');f.cycle()
  assert f.registrations==1 and grass.identity(f.nodes['instance-000']['store'],pending=True)['device'] is None
  old=f.nodes.pop('instance-000');old['active']=False
  node='replacement' if replacement else 'instance-000'
  f.add(node,'198.19.2.1');f.nodes[node]['runtime']='fixture-next-runtime'
  # Even after every prior permission expires, an accepted but unrecorded
  # provider registration cannot be retried from the controller's blank ID.
  real_boot=time.clock_gettime;real_wall=time.time;elapsed=session.LEASE_SECONDS+60
  with patch.object(time,'clock_gettime',side_effect=lambda clock:real_boot(clock)+elapsed),patch.object(time,'time',side_effect=lambda:real_wall()+elapsed):
   f.cycle();f.cycle()
  assert f.registrations==1 and not f.nodes[node]['active'],'interrupted enrollment replayed'
  assert f.nodes[node]['store'] is None,'pending enrollment credentials replayed'
print('PASS: interrupted first registration remains fenced across replacement and runtime restart')
with tempfile.TemporaryDirectory(prefix='wdi-fixture-') as root:
 f=Fixture(root);f.cycle();f.cycle();old=f.nodes.pop('instance-000')
 f.add('replacement','198.19.3.1');real_wall=time.time
 with patch.object(time,'time',side_effect=lambda:real_wall()+3600):f.cycle()
 assert old['active'] and not f.nodes['replacement']['active'],'wall-clock jump released a still-live lease'
 assert f.v.db.execute('SELECT COUNT(*) FROM leases WHERE instance=?',(old['id'],)).fetchone()[0]==1
 print('PASS: controller wall-clock advance does not release an absent live holder')
with tempfile.TemporaryDirectory(prefix='wdi-fixture-') as root:
 f=Fixture(root);f.cycle();f.cycle()
 f.groups.append({'id':'group-b','name':'test-b','replicas':1});f.add('replacement','198.19.3.2','group-b')
 before=f.v.db.execute('SELECT * FROM leases').fetchall();calls=len(f.calls);original_request=f.request
 def partial(url,headers,payload=None):
  if url.endswith('/test-b/instances'):raise OSError('fixture partial inventory')
  return original_request(url,headers,payload)
 f.request=partial
 try:f.cycle();raise AssertionError('partial inventory accepted')
 except OSError:pass
 assert f.v.db.execute('SELECT * FROM leases').fetchall()==before and len(f.calls)==calls
 assert f.nodes['instance-000']['active'] and not f.nodes['replacement']['active']
 print('PASS: one missing group inventory preserves leases before any runtime mutations')
with tempfile.TemporaryDirectory(prefix='wdi-fixture-') as root:
 f=Fixture(root);f.cycle();f.cycle();store=copy.deepcopy(f.nodes['instance-000']['store']);ident=grass.identity(store)
 root=Path(root);scope={'group':'fixture','instance':'fixture','machine':'fixture','runtime':'fixture','configured_account':f.account}
 with patch.object(session,'STATE',root/'store.json'),patch.object(session,'PERMIT',root/'permit.json'),patch.object(session,'MACHINE',root/'machine-id'),patch.object(session,'runtime',return_value=scope),patch.object(session.os,'geteuid',return_value=1000),patch.object(session,'public_ip',return_value={'public_ip':'198.19.4.1'}),patch.object(session,'grass_processes',return_value=[]):
  (root/'store.json').write_text(json.dumps(store));(root/'machine-id').write_text('original-machine')
  until=time.clock_gettime(time.CLOCK_BOOTTIME)+100
  session.exchange(dict(action='allow',scope=scope,identity=ident,ip='198.19.4.1',until=until,sequence=1))
  session.exchange(dict(action='hold',scope=scope,sequence=3))
  held=(root/'permit.json').read_bytes();saved=(root/'store.json').read_bytes()
  packets=[dict(action='allow',identity=ident),dict(action='restore',store=dict(store,accessToken=json.dumps('stale-token')),machine_id='stale-machine',pending=False)]
  for packet in packets:
   try:session.exchange(dict(packet,scope=scope,ip='198.19.4.1',until=until,sequence=2))
   except ValueError:pass
   assert (root/'permit.json').read_bytes()==held,'delayed permission overrode a newer hold'
   assert (root/'store.json').read_bytes()==saved and (root/'machine-id').read_text()=='original-machine','delayed restore wrote stale state'
   try:session.permission();raise AssertionError('held installation allowed')
   except ValueError:pass
  # A crash after STATE is durable must still consume the higher sequence.
  # The delayed packet is newer than the previous completed operation (3).
  latest=dict(store,accessToken=json.dumps('fixture-latest-token'));actual_save=session.save
  def interrupted_save(path,value):
   actual_save(path,value)
   if path==root/'store.json':raise OSError('fixture failure after store commit')
  newer=dict(action='restore',scope=scope,store=latest,pending=False,ip='198.19.4.1',until=until,sequence=5)
  with patch.object(session,'save',side_effect=interrupted_save):
   try:session.exchange(newer);raise AssertionError('restore interruption did not occur')
   except OSError:pass
  assert session.read(root/'store.json')['accessToken']==latest['accessToken'],'partial restore never wrote the new state'
  partial=(root/'store.json').read_bytes();fence=(root/'permit.json').read_bytes()
  for packet in reversed(packets):
   rejected=False
   try:session.exchange(dict(packet,scope=scope,ip='198.19.4.1',until=until,sequence=4))
   except ValueError:rejected=True
   assert rejected,'lower sequence accepted after a partial restore'
   assert (root/'store.json').read_bytes()==partial,'delayed operation overwrote a partially committed newer restore'
   assert (root/'permit.json').read_bytes()==fence,'delayed operation changed the partial restore fence'
  try:session.permission();raise AssertionError('partial restore enabled native launch')
  except ValueError:pass
  expired=dict(newer,store=dict(store,accessToken=json.dumps('fixture-expired-token')),until=time.clock_gettime(time.CLOCK_BOOTTIME)-1,sequence=6)
  try:session.exchange(expired);raise AssertionError('expired restore accepted')
  except ValueError:pass
  assert (root/'store.json').read_bytes()==partial,'expired restore changed the store before checking its deadline'
 print('PASS: delayed and expired operations cannot override holds or partially committed newer restores')

with tempfile.TemporaryDirectory(prefix='wdi-fixture-') as root:
 f=Fixture(root);f.cycle();f.cycle()
 before=f.v.db.execute('SELECT * FROM leases').fetchall()
 with patch.object(grass.boto3,'client',return_value=f.v.secrets):reader=grass.Vault(f.v.path)
 assert reader.db.execute('SELECT * FROM leases').fetchall()==before,'opening a reader changed lease deadlines or observations'
 reader.db.close()
 print('PASS: opening the same-boot vault does not change lease or observation timing')
with tempfile.TemporaryDirectory(prefix='wdi-fixture-') as root:
 f=Fixture(root);profile=f.v.db.execute('SELECT profile FROM installations').fetchone()[0]
 pending=copy.deepcopy(f.v.get(profile)[1]['store']);root=Path(root)
 scope={'group':'fixture','instance':'fixture','machine':'fixture','runtime':'fixture','configured_account':f.account}
 processes=[];attempted=threading.Event();native_wrote=threading.Event();owner=threading.current_thread();actual_save=session.save
 def racing_save(path,value):
  if path==root/'store.json' and threading.current_thread()!=owner:
   assert native_wrote.wait(3),'native registration write never reached'
  actual_save(path,value)
 with patch.object(session,'STATE',root/'store.json'),patch.object(session,'PERMIT',root/'permit.json'),patch.object(session,'MACHINE',root/'machine-id'),patch.object(session,'runtime',return_value=scope),patch.object(session.os,'geteuid',return_value=1000),patch.object(session,'public_ip',return_value={'public_ip':'198.19.4.2'}),patch.object(session,'grass_processes',side_effect=lambda:list(processes)),patch.object(session,'save',side_effect=racing_save),ThreadPoolExecutor(max_workers=1) as pool:
  packet=dict(action='restore',scope=scope,store=pending,pending=True,ip='198.19.4.2',until=time.clock_gettime(time.CLOCK_BOOTTIME)+100,sequence=1)
  session.exchange(packet)
  def restore_during_start():
   attempted.set()
   try:return session.exchange(dict(packet,sequence=2))
   except ValueError:return None
  with session.starting(root/'store.json'):
   future=pool.submit(restore_during_start);assert attempted.wait(2)
   try:future.result(timeout=.1)
   except FutureTimeout:pass
   # This is the native provider result becoming durable after launch. A
   # restore that passed its process check earlier must not overwrite it.
   completed=dict(pending,**{k:json.dumps(v) for k,v in {'wynd:device_id':'fixture-race-device','wynd:device_registered_user_id':f.account,'wynd:device_registered_pubkey':grass.decoded(pending['wynd:device_pubkey'])}.items()})
   actual_save(root/'store.json',completed);processes.append('fixture-native:1');native_wrote.set()
  future.result(timeout=3)
  assert session.read(root/'store.json')==completed,'overlapping restore clobbered native registration'
  processes.clear()
  with session.starting(root/'store.json') as resumed:assert resumed['device']=='fixture-race-device'
 print('PASS: restore overlapping native startup cannot erase the completed provider registration')
with tempfile.TemporaryDirectory(prefix='wdi-fixture-') as root:
 f=Fixture(root);profile=f.v.db.execute('SELECT profile FROM installations').fetchone()[0]
 pending=f.v.get(profile)[1]['store'];root=Path(root)
 scope={'group':'fixture','instance':'fixture','machine':'fixture','runtime':'fixture','configured_account':f.account}
 with patch.object(session,'STATE',root/'store.json'),patch.object(session,'PERMIT',root/'permit.json'),patch.object(session,'MACHINE',root/'machine-id'),patch.object(session,'runtime',return_value=scope),patch.object(session.os,'geteuid',return_value=1000),patch.object(session,'public_ip',return_value={'public_ip':'198.19.4.3'}),patch.object(session,'grass_processes',return_value=[]):
  packet=dict(action='restore',scope=scope,store=pending,pending=True,ip='198.19.4.3',until=time.clock_gettime(time.CLOCK_BOOTTIME)+100,sequence=1)
  session.exchange(packet)
  try:
   with session.starting(root/'store.json'):raise OSError('fixture uncertain Popen result')
  except OSError:pass
  launched=False
  try:
   with session.starting(root/'store.json'):launched=True
  except ValueError:pass
  assert not launched,'uncertain first launch replayed enrollment'
  try:session.exchange(dict(packet,sequence=2))
  except ValueError:pass
  try:
   with session.starting(root/'store.json'):launched=True
  except ValueError:pass
  assert not launched,'renewed restore erased the enrollment-attempt fence'
 print('PASS: an interrupted first native launch cannot be replayed by restart or repeated restore')

with tempfile.TemporaryDirectory(prefix='wdi-ip-policy-') as root:
 f=Fixture(root);original_exchange=f.exchange;admitted=False
 def policy_exchange(instance,access,payload,before_send=None):
  result=original_exchange(instance,access,payload,before_send)
  if payload['action']=='inspect':
   result.update(ip_policy_required=True,ip_policy={'scope':result['scope'],
       'ip':result['public_ip'],'allowed':admitted,'valid_until':result['boot_time']+60})
  return result
 f.exchange=policy_exchange
 f.cycle()
 assert f.registrations==0 and not f.nodes['instance-000']['active']
 assert f.v.db.execute('SELECT enrollment_started FROM installations').fetchone()[0]==0
 admitted=True;f.cycle();f.cycle()
 assert f.registrations==1 and f.nodes['instance-000']['active']
 expected=grass.identity(f.nodes['instance-000']['store'])
 admitted=False;f.cycle()
 assert f.registrations==1 and not f.nodes['instance-000']['active']
 assert grass.identity(f.nodes['instance-000']['store'])==expected
 print('PASS: IP admission blocks first enrollment; later rejection holds the same installation')

with tempfile.TemporaryDirectory(prefix='wdi-ip-permission-') as root:
 root=Path(root);f=Fixture(root);f.cycle();f.cycle()
 store=f.nodes['instance-000']['store'];original=grass.identity(store)
 scope={'group':'fixture','instance':'fixture','machine':'fixture','runtime':'fixture'}
 address='198.18.0.1';state=root/'store.json';state.write_text(json.dumps(store))
 with patch.object(session,'STATE',state),patch.object(session,'PERMIT',root/'permit.json'),patch.object(session,'IP_POLICY',root/'network.json'),patch.object(session,'IP_POLICY_REQUIRED',True),patch.object(session,'runtime',return_value=scope),patch.object(session.os,'geteuid',return_value=1000):
  deadline=time.clock_gettime(time.CLOCK_BOOTTIME)+60
  try:session.grant(scope,original,address,deadline);raise AssertionError('Missing policy admitted')
  except ValueError as e:assert str(e)=='ip_policy_not_admitted'
  policy=dict(scope=scope,ip=address,allowed=True,valid_until=deadline)
  session.IP_POLICY.write_text(json.dumps(policy));session.grant(scope,original,address,deadline)
  assert session.permission()['identity']==original
  for change in ({'allowed':False},{'ip':'198.18.0.2'},{'valid_until':0},{'reallocation_requested':True}):
   session.IP_POLICY.write_text(json.dumps(dict(policy,**change)))
   try:session.permission();raise AssertionError('Rejected IP policy remained permitted')
   except ValueError as e:assert str(e)=='ip_policy_not_admitted'
 print('PASS: runtime launch permission requires current admitted IP policy')
