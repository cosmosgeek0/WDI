#!/usr/bin/python3
"""Start the official desktop and its single recovery owner; provide restart transport."""
import fcntl,hashlib,json,os,queue,signal,struct,subprocess,threading,time
from pathlib import Path
from session import ready,permission,starting,grass_processes,directory
import ip_policy
import node_auth

RUN=Path('/config/run')
STATUS=Path('/run/wdi/status.json')
STATE=Path('/config/xdg/data/io.getgrass.desktop/store.json')
APP='/usr/bin/grass-desktop'
APP_SHA=Path('/opt/grass-private/app.sha256').read_text().strip()
WORKER='/opt/grass-private/worker.pl'
TRAFFIC='/opt/app-traffic/traffic.py'
EVENTS=queue.Queue(maxsize=128)
EVENT_DROPPED=0

def event(name,**fields):
    global EVENT_DROPPED
    line='GRASS_PRIVATE_EVENT_V1 '+json.dumps(dict(event=name,source_ms=int(time.time()*1000),
        scope='container_runtime',
        group_id=os.environ.get('SALAD_CONTAINER_GROUP_ID'),instance=os.environ.get('SALAD_INSTANCE_ID'),
        machine=os.environ.get('SALAD_MACHINE_ID'),log_dropped=EVENT_DROPPED,**fields),separators=(',',':'))
    if len(line.encode())>65536:
        EVENT_DROPPED+=1
        return
    try:EVENTS.put_nowait(line)
    except queue.Full:EVENT_DROPPED+=1

def load(path):
    try:
        v=json.loads(path.read_text());return v if isinstance(v,dict) else {}
    except (OSError,ValueError):return {}

def resources():
    out={'monotonic_ns':time.monotonic_ns(),'cpu_usage_usec':None,'memory_current_bytes':None,'memory_peak_bytes':None}
    paths={'memory_current_bytes':['/sys/fs/cgroup/memory.current','/sys/fs/cgroup/memory/memory.usage_in_bytes'],
           'memory_peak_bytes':['/sys/fs/cgroup/memory.peak','/sys/fs/cgroup/memory/memory.max_usage_in_bytes']}
    for name,candidates in paths.items():
        for path in candidates:
            try:out[name]=int(Path(path).read_text());break
            except (OSError,ValueError):pass
    try:
        stat=dict(s.split() for s in Path('/sys/fs/cgroup/cpu.stat').read_text().splitlines())
        out['cpu_usage_usec']=int(stat['usage_usec']);out['cpu_max']=Path('/sys/fs/cgroup/cpu.max').read_text().strip()
    except (OSError,ValueError,KeyError):
        try:out['cpu_usage_usec']=int(Path('/sys/fs/cgroup/cpuacct/cpuacct.usage').read_text())//1000
        except (OSError,ValueError):pass
    return out

def terminate(proc):
    # Kill the app's process group even if its leader has already exited.
    for sig in (signal.SIGCONT,signal.SIGTERM):
        try:os.killpg(proc.pid,sig)
        except ProcessLookupError:pass
    try:proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        pass
    # A dead leader does not prove its children have exited.
    try:os.killpg(proc.pid,signal.SIGKILL)
    except ProcessLookupError:pass
    proc.wait()

def main():
    os.umask(0o077)
    def write_events():
        while True:
            try:
                data=(EVENTS.get()+'\n').encode()
                while data:data=data[os.write(1,data):]
            except OSError:pass
    threading.Thread(target=write_events,daemon=True).start()
    stop=threading.Event()
    repocket_key=os.environ.pop('RP_API_KEY',None)
    signal.signal(signal.SIGTERM,lambda *_:stop.set())
    signal.signal(signal.SIGINT,lambda *_:stop.set())
    children=[];bus_pid=None;pipe_fd=None;broker=None;failure=None;network={};next_network=0;next_reallocation=0
    def check_network():
        nonlocal network,next_network
        if time.monotonic()>=next_network:
            previous=(network.get('ip'),network.get('is_datacenter'),network.get('error'))
            network=ip_policy.check();next_network=time.monotonic()+30
            current=(network.get('ip'),network.get('is_datacenter'),network.get('error'))
            if current!=previous:event('ip_classified',policy=network)
    def reject_datacenter():
        nonlocal next_reallocation
        if (network.get('is_datacenter') is not True or network.get('reallocation_requested')
                or network.get('reallocation_rejected')
                or time.clock_gettime(time.CLOCK_BOOTTIME)<network.get('reallocation_retry_at',0)
                or time.monotonic()<next_reallocation):return
        next_reallocation=time.monotonic()+30
        event('reallocation_requested',initiator='container_ip_policy',reason='confirmed_datacenter',ip=network.get('ip'))
        try:
            network.update(ip_policy.reallocate(network))
            event('reallocation_result',policy=network)
        except (OSError,ValueError,KeyError) as error:
            event('reallocation_not_sent',reason=str(error) if type(error) is ValueError else type(error).__name__)
    try:
        if os.geteuid()!=0:
            raise ValueError('isolated_supervisor_requires_root')
        with directory(Path('/run/wdi')) as fd:
            os.fchmod(fd,0o755)
        installation_lock=open('/run/wdi/installation.lock','a')
        fcntl.flock(installation_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        for path in [RUN,Path('/config/xdg/data'),Path('/config/xdg/config'),Path('/config/xdg/cache')]:
            with directory(path) as fd:
                os.fchown(fd,1000,1000)
        worker_sha=Path('/opt/grass-private/worker.sha256').read_text().strip()
        for path,expected in [(APP,APP_SHA),(WORKER,worker_sha)]:
            if hashlib.sha256(Path(path).read_bytes()).hexdigest()!=expected:
                raise ValueError('executable_hash_mismatch')
        if not repocket_key:
            raise ValueError('repocket_login_required')
        # Waiting never exits PID 1 or manufactures a replacement identity.
        waiting=None
        while not stop.is_set():
            check_network()
            reject_datacenter()
            try:
                ready(STATE)
                break
            except (OSError,ValueError,KeyError) as error:
                reason=str(error) if type(error) is ValueError else 'identity_restore_required'
                if reason!=waiting:
                    event('identity_waiting',reason=reason);waiting=reason
                stop.wait(1)
        if stop.is_set():return 0
        broker=node_auth.start()
        grass_env={k:v for k,v in os.environ.items() if k in (
            'PATH','HOME','XDG_DATA_HOME','XDG_CONFIG_HOME','XDG_CACHE_HOME','XDG_RUNTIME_DIR',
            'DISPLAY','GDK_BACKEND','LIBGL_ALWAYS_SOFTWARE','LP_NUM_THREADS','PYTHONUNBUFFERED',
            'SALAD_CONTAINER_GROUP_ID','SALAD_INSTANCE_ID','SALAD_MACHINE_ID','GRASS_ACCOUNT_ID')}
        repocket_env={k:v for k,v in os.environ.items() if k in (
            'SALAD_CONTAINER_GROUP_ID','SALAD_INSTANCE_ID','SALAD_MACHINE_ID')}
        repocket_env.update(PATH='/usr/local/bin:/usr/bin:/bin',HOME='/repocket',TMPDIR='/repocket/tmp',
                            RP_API_KEY=repocket_key)
        def launch(command,public=False,env=None,uid=1000):
            proc=subprocess.Popen(['/usr/bin/setpriv','--reuid='+str(uid),'--regid='+str(uid),
                '--clear-groups','--bounding-set=-all','--no-new-privs','--',*command],stdin=subprocess.DEVNULL,
                stdout=None if public else subprocess.DEVNULL,
                stderr=None if public else subprocess.DEVNULL,start_new_session=True,
                env=grass_env if env is None else env,cwd='/config' if uid==1000 else '/repocket')
            children.append(proc)
            return proc
        def measured(application,command,env=None):
            uid=1001 if application=='repocket' else 1000
            output=Path('/repocket/run/traffic.json') if uid==1001 else RUN/'grass-traffic.json'
            return launch(['/usr/bin/python3',TRAFFIC,'--application',application,
                '--network-state','/run/wdi/ip-policy.json',
                '--output',str(output),'--',*command],public=True,env=env,uid=uid)
        # Require an X11 cookie so the other app cannot control or inspect Grass.
        authority=Path('/run/wdi/Xauthority')
        fields=[b'',b'99',b'MIT-MAGIC-COOKIE-1',os.urandom(16)]
        authority.write_bytes(struct.pack('!H',65535)+b''.join(struct.pack('!H',len(v))+v for v in fields))
        os.chmod(authority,0o600);os.chown(authority,1000,1000)
        grass_env['XAUTHORITY']=str(authority)
        display=launch(['/usr/bin/Xvfb',':99','-screen','0','1024x768x24','-nolisten','tcp','-noreset',
                        '-auth',str(authority)])
        until=time.monotonic()+20
        initial_display_error=None
        while not Path('/tmp/.X11-unix/X99').exists():
            if display.poll() is not None or time.monotonic()>until:
                initial_display_error='virtual_display_failed';terminate(display);break
            if stop.wait(.1):return 0
        bus=subprocess.run(['/usr/bin/setpriv','--reuid=1000','--regid=1000','--clear-groups',
            '--bounding-set=-all','--no-new-privs','--','/usr/bin/dbus-daemon',
            '--session','--fork','--print-address=1','--print-pid=1'],
            capture_output=True,text=True,timeout=10,check=True,env=grass_env)
        address,bus_pid=bus.stdout.strip().splitlines()
        grass_env['DBUS_SESSION_BUS_ADDRESS']=address
        pipe=Path('/run/wdi/restart.fifo');pipe.unlink(missing_ok=True);os.mkfifo(pipe,0o600)
        os.chown(pipe,1000,1000)
        pipe_fd=os.open(pipe,os.O_RDWR|os.O_NONBLOCK)
        def launch_app():
            try:
                with starting(STATE) as restored:
                    proc=measured('grass',['/bin/sh','-c','exec /usr/bin/grass-desktop >/dev/null 2>&1'])
            except (ValueError,KeyError) as error:
                return None,str(error) if type(error) is ValueError else 'identity_restore_required'
            event('app_launch_requested',runner_pid=proc.pid,binary_sha256=APP_SHA,device_id=restored['device'],
                  public_key_sha256=restored['public_sha256'])
            return proc,None
        app=None;repocket=None;repocket_error=None;repocket_retry=0;repocket_failures=0;repocket_started=0
        worker=launch(['/usr/bin/perl',WORKER],public=True);restart_from=None
        worker_paused=False;worker_restarts=0;worker_retry=0;worker_started=time.monotonic()
        display_error=initial_display_error;display_retry=0;buffer=b'';next_sample=0;last_pause=None
        while not stop.wait(.5):
            check_network()
            pause=None
            try:permission()
            except (OSError,ValueError,KeyError) as error:
                pause=str(error) if type(error) is ValueError else 'identity_permission_unavailable'
            at=time.monotonic()
            if display.poll() is not None:
                display_error='virtual_display_exit'
                if app is not None:
                    terminate(app);children.remove(app);app=None
                if not pause and at>=display_retry:
                    terminate(display);children.remove(display)
                    display=launch(['/usr/bin/Xvfb',':99','-screen','0','1024x768x24','-nolisten','tcp','-noreset',
                                    '-auth',str(authority)])
                    display_retry=at+60
                    event('display_restart_requested',pid=display.pid,initiator='supervisor',reason=display_error)
            elif display_error and Path('/tmp/.X11-unix/X99').exists():
                display_error=None
            if not pause and not display_error and app is None:
                app,pause=launch_app()
                if app is not None and restart_from is not None:
                    event('app_restart_launched',old_runner_pid=restart_from,runner_pid=app.pid,
                          initiator='native_worker',reason='native_worker_restart_request',provider_recovery_confirmed=False)
                    restart_from=None
            if not pause and repocket is None and at>=repocket_retry:
                repocket=measured('repocket',['/bin/busybox','sh','/usr/local/bin/repocket-start'],
                                  env=repocket_env)
                event('repocket_launch_requested',runner_pid=repocket.pid,initiator='identity_permission')
                repocket_error=None;repocket_started=at
            if pause:
                if app is not None:
                    old=app.pid;terminate(app);children.remove(app);app=None
                    event('app_paused',old_runner_pid=old,reason=pause,initiator='identity_permission')
                if repocket is not None:
                    old=repocket.pid;terminate(repocket);children.remove(repocket);repocket=None
                    event('repocket_paused',old_runner_pid=old,reason=pause,initiator='identity_permission')
                if worker.poll() is None and not worker_paused:
                    os.killpg(worker.pid,signal.SIGSTOP);worker_paused=True
            elif not display_error:
                if worker_paused and worker.poll() is None:
                    os.killpg(worker.pid,signal.SIGCONT);worker_paused=False
            if pause != last_pause:
                event('permission_state',reason=pause or 'allowed');last_pause=pause
            if pause:reject_datacenter()
            if display_error and worker.poll() is None and not worker_paused:
                os.killpg(worker.pid,signal.SIGSTOP);worker_paused=True
            if repocket is not None and repocket.poll() is not None:
                repocket_error='repocket_or_meter_exited'
                event('repocket_blocked',reason=repocket_error,exit_code=repocket.returncode,
                      initiator='supervisor',reallocation_requested=False)
                terminate(repocket);children.remove(repocket);repocket=None
                repocket_failures+=1;repocket_retry=at+min(300,15*2**min(repocket_failures,5))
            elif repocket is not None and at-repocket_started>=300:
                repocket_failures=0
            try:buffer+=os.read(pipe_fd,4096)
            except BlockingIOError:pass
            if len(buffer)>4096:buffer=b''
            while b'\n' in buffer:
                command,buffer=buffer.split(b'\n',1)
                if not command.startswith(b'restart:app:') or pause or display_error or app is None:continue
                expected=command[len(b'restart:app:'):].decode('ascii',errors='replace')
                native=grass_processes()
                if native != ([] if expected=='missing' else [expected]):continue
                restart_from=app.pid;terminate(app);children.remove(app);app=None
            at=time.monotonic()
            if not pause and not display_error and worker.poll() is not None and at>=worker_retry:
                terminate(worker);children.remove(worker)
                worker=launch(['/usr/bin/perl',WORKER],public=True)
                worker_restarts+=1;worker_retry=at+min(300,30*2**min(worker_restarts,4));worker_started=at
                event('recovery_owner_restarted',pid=worker.pid,attempt=worker_restarts,
                      initiator='supervisor',reason='recovery_worker_exit')
            elif worker.poll() is None and at-worker_started>=300:
                worker_restarts=0
            if at>=next_sample:
                recovery=load(RUN/'grass-recovery.json')
                native=grass_processes()
                value=dict(source_ms=int(time.time()*1000),app_pid=int(native[0].split(':')[0]) if len(native)==1 else None,
                    app_runner_pid=app.pid if app and app.poll() is None else None,
                    repocket_runner_pid=repocket.pid if repocket and repocket.poll() is None else None,
                    repocket_error=repocket_error,display_error=display_error,
                    worker_pid=worker.pid if worker.poll() is None else None,worker_restarts=worker_restarts,
                    paused_reason=pause,worker_sha256=worker_sha,recovery_phase=recovery.get('phase'),resources=resources())
                try:
                    temp=STATUS.with_suffix('.tmp');temp.write_text(json.dumps(value));temp.replace(STATUS)
                except OSError as error:event('runtime_status_unavailable',reason=type(error).__name__)
                event('runtime_sample',**{k:v for k,v in value.items() if k!='source_ms'});next_sample=at+10
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as error:
        failure=str(error) if type(error) is ValueError else type(error).__name__
    finally:
        if broker:
            broker.shutdown();broker.server_close()
        for proc in reversed(children):terminate(proc)
        if bus_pid:
            try:os.kill(int(bus_pid),signal.SIGTERM)
            except ProcessLookupError:pass
        if pipe_fd is not None:
            os.close(pipe_fd);Path('/run/wdi/restart.fifo').unlink(missing_ok=True)
    if failure:
        event('runtime_blocked',reason=failure,initiator='supervisor',reallocation_requested=False)
        try:
            STATUS.write_text(json.dumps(dict(source_ms=int(time.time()*1000),
                app_pid=None,worker_pid=None,paused_reason=failure,fatal_reason=failure)))
        except OSError:pass
        # Persistent configuration failures must not blacklist successive Salad
        # hosts. Keep this allocation inspectable until corrected or stopped.
        while not stop.wait(30):pass
    event('runtime_stopped')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
