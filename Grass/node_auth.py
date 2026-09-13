"""Keep the native bandwidth transport; broker only its device check-in."""
import base64
import ctypes
import hashlib
import http.server
import json
import re
import queue
import socket
import socketserver
import select
import sys
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from session import STATE, decoded, permission, read

DIRECTOR = 'https://director.getgrass.io/devices/checkin'
LOCAL = 'http://127.0.0.1:16888/devices/checkin'
CONTROL = '/run/wdi/checkin.sock'
JOBS = queue.Queue(maxsize=1)


def patch_client():
    path = Path('/usr/bin/grass-desktop')
    binary = path.read_bytes()
    if hashlib.sha256(binary).hexdigest() != '36cd15578ac1ac179119d3f115c339f762530af3df82a03b143ab20f30745e10':
        raise ValueError('unrecognised_grass_binary')
    ph = struct.unpack_from('<Q', binary, 32)[0]
    loads = [struct.unpack_from('<IIQQQQQQ',binary,ph+i*56)
             for i in range(struct.unpack_from('<H',binary,56)[0])]
    loads = [p for p in loads if p[0] == 1]
    def offset(value):
        return next(value-p[3]+p[2] for p in loads if p[3] <= value < p[3]+p[5])
    sh = struct.unpack_from('<Q',binary,40)[0]
    relocations = {}
    for i in range(struct.unpack_from('<H',binary,60)[0]):
        section = struct.unpack_from('<IIQQQQIIQQ',binary,sh+i*64)
        if section[1] == 4:
            for pos in range(section[4],section[4]+section[5],section[9]):
                location,info,value = struct.unpack_from('<QQq',binary,pos)
                if info & 0xffffffff == 8:
                    relocations[location] = value
    def word(address):
        return relocations.get(address,struct.unpack_from('<Q',binary,offset(address))[0])
    target = next(k for k,v in relocations.items() if v == binary.index(b'/assets/index-DywfMoEA.js'))
    position, capacity = offset(word(target+16)), word(target+24)
    compressed = binary[position:position+capacity]
    output = ctypes.create_string_buffer(2_000_000)
    size = ctypes.c_size_t(len(output))
    decoder = ctypes.CDLL('libbrotlidec.so.1')
    decoder.BrotliDecoderDecompress.argtypes = [ctypes.c_size_t,ctypes.c_void_p,ctypes.POINTER(ctypes.c_size_t),ctypes.c_void_p]
    if decoder.BrotliDecoderDecompress(len(compressed),compressed,ctypes.byref(size),output) != 1:
        raise ValueError('client_asset_decompression_failed')
    source = output.raw[:size.value]
    if (hashlib.sha256(source).hexdigest() != '9a3bcaf10252dae0402d1f74d32d2a80d84e3653845eb3f8898f11e769bdeb48'
            or source.count(DIRECTOR.encode()) != 1):
        raise ValueError('unrecognised_client_asset')
    source = source.replace(DIRECTOR.encode(),LOCAL.encode())
    encoded = ctypes.create_string_buffer(capacity)
    size = ctypes.c_size_t(capacity)
    encoder = ctypes.CDLL('libbrotlienc.so.1')
    encoder.BrotliEncoderCompress.argtypes = [ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_size_t,ctypes.c_void_p,ctypes.POINTER(ctypes.c_size_t),ctypes.c_void_p]
    if encoder.BrotliEncoderCompress(11,22,1,len(source),source,ctypes.byref(size),encoded) != 1:
        raise ValueError('client_asset_does_not_fit')
    patched = bytearray(binary)
    patched[position:position+size.value] = encoded.raw[:size.value]
    struct.pack_into('<Q',patched,offset(target+24),size.value)
    path.write_bytes(patched)
    Path('/opt/grass-private/app.sha256').write_text(hashlib.sha256(patched).hexdigest()+'\n')


class Checkin(http.server.BaseHTTPRequestHandler):
    def setup(self):
        # Bound request-line/header reads too, before do_POST is entered.
        self.request.settimeout(3)
        def expire():
            try:self.request.shutdown(socket.SHUT_RDWR)
            except OSError:pass
        self.deadline = threading.Timer(45,expire)
        self.deadline.daemon = True
        self.deadline.start()
        super().setup()

    def finish(self):
        self.deadline.cancel()
        super().finish()

    def log_message(self, *_args):
        pass

    def do_POST(self):
        status, response = 503, b'{"error":"node_authorization_unavailable"}'
        try:
            self.connection.settimeout(10)
            permit = permission()
            store = read(STATE)
            original = decoded(store['wdi:identity'])
            if self.path != '/devices/checkin' or self.headers.get('x-wynd-device-id') != original['device']:
                raise ValueError('wrong_node')
            timestamp = self.headers.get('x-wynd-ts','')
            if not timestamp.isdigit() or abs(time.time()*1000-int(timestamp)) > 90_000:
                raise ValueError('stale_local_request')
            match = re.fullmatch(r'Grass-Checkin v=1, sig=([A-Za-z0-9+/=]+)',self.headers.get('Authorization',''))
            signature = base64.b64decode(match[1],validate=True) if match else b''
            public = base64.b64decode(decoded(store['wynd:device_pubkey']),validate=True)
            message = ('CHECKIN1\n'+original['device']+'\n'+timestamp).encode()
            with tempfile.TemporaryDirectory(prefix='checkin-',dir='/run/wdi') as folder:
                root = Path(folder)
                (root/'public').write_bytes(bytes.fromhex('302a300506032b6570032100')+public)
                (root/'signature').write_bytes(signature)
                (root/'message').write_bytes(message)
                verified = subprocess.run(['openssl','pkeyutl','-verify','-pubin','-keyform','DER',
                    '-inkey',str(root/'public'),'-rawin','-in',str(root/'message'),'-sigfile',str(root/'signature')],
                    capture_output=True,timeout=5)
            if len(signature) != 64 or verified.returncode:
                raise ValueError('local_signature_invalid')
            size = int(self.headers.get('Content-Length','0'))
            if not 0 < size <= 16384:
                raise ValueError('invalid_checkin_size')
            body = self.rfile.read(size)
            if not isinstance(json.loads(body),dict):
                raise ValueError('invalid_checkin_body')
            job = dict(body=body,device=original['device'],runtime=permit['runtime'],
                       event=threading.Event(),until=time.monotonic()+30,result=None)
            JOBS.put_nowait(job)
            if job['event'].wait(30) and job['result']:
                status,response = job['result']
            else:
                job['until'] = 0
        except (ValueError,KeyError,TypeError):
            status,response = 401,b'{"error":"node_authorization_rejected"}'
        except (OSError,subprocess.SubprocessError,queue.Full):
            pass
        self.send_response(status)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(response)))
        self.end_headers()
        try:self.wfile.write(response)
        except OSError:pass


class Control(socketserver.StreamRequestHandler):
    def handle(self):
        job = None
        try:
            self.request.settimeout(35)
            scope = json.loads(self.rfile.readline(16385))['runtime']
            if scope != permission()['runtime']:
                raise ValueError('wrong_runtime')
            try:
                job = JOBS.get(timeout=8)
            except queue.Empty:
                self.wfile.write(b'{"wait":true}\n');return
            if (job['runtime'] != scope or job['until']<time.monotonic()
                    or scope != permission()['runtime']):
                raise ValueError('expired_checkin')
            self.wfile.write((json.dumps(dict(body=base64.b64encode(job['body']).decode(),
                device=job['device'],runtime=scope))+'\n').encode())
            reply = json.loads(self.rfile.readline(100001))
            body = base64.b64decode(reply['body'],validate=True)
            if len(body)>65536 or type(reply['status']) is not int or not 100<=reply['status']<=599:
                raise ValueError('invalid_auth_reply')
            if permission()['runtime'] != scope:
                raise ValueError('permission_changed')
            job['result'] = reply['status'],body
        except (OSError,ValueError,KeyError,TypeError):
            pass
        finally:
            if job:
                job['event'].set()


class LocalServer(http.server.HTTPServer):
    def verify_request(self, connection, address):
        # Linux records the connecting socket's UID. Reject the other app
        # before it can occupy the HTTP parser or the single check-in queue.
        if address[0]!='127.0.0.1':return False
        peer = f'0100007F:{address[1]:04X}'
        local = f'0100007F:{connection.getsockname()[1]:04X}'
        try:
            with open('/proc/net/tcp') as table:
                for line in table:
                    fields = line.split()
                    if len(fields)>7 and fields[1:4] == [peer,local,'01']:
                        return fields[7] == '1000'
        except OSError:
            pass
        return False


class Broker:
    def __init__(self):
        Path(CONTROL).unlink(missing_ok=True)
        self.control = socketserver.UnixStreamServer(CONTROL,Control)
        self.http = LocalServer(('127.0.0.1',16888),Checkin)
        for server in (self.control,self.http):
            threading.Thread(target=server.serve_forever,daemon=True).start()

    def shutdown(self):
        self.http.shutdown();self.control.shutdown()

    def server_close(self):
        self.http.server_close();self.control.server_close()
        Path(CONTROL).unlink(missing_ok=True)


def relay():
    """The untrusted worker moves ciphertext only; TLS ends on Mumbai and Grass."""
    control = upstream = None
    print('\nWDI_READY',flush=True)
    try:
        for line in sys.stdin:
            request = json.loads(line)
            action = request['action']
            if action == 'poll':
                control = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
                control.settimeout(35)
                control.connect(CONTROL)
                control.sendall((json.dumps({'runtime':request['runtime']})+'\n').encode())
                raw = control.makefile('rb').readline(100001)
                result = json.loads(raw)
                if result.get('wait'):
                    control.close();control=None
                else:
                    upstream = socket.create_connection(('director.getgrass.io',443),timeout=15)
            elif action == 'tls' and upstream is not None:
                data = base64.b64decode(request['data'],validate=True)
                if len(data)>65536:raise ValueError('oversized_tls_record')
                if data:upstream.sendall(data)
                chunks = [upstream.recv(65536)]
                while chunks[-1] and sum(map(len,chunks))<65536 and select.select([upstream],[],[],.01)[0]:
                    chunks.append(upstream.recv(65536-sum(map(len,chunks))))
                result = {'data':base64.b64encode(b''.join(chunks)).decode()}
            elif action == 'finish' and control is not None:
                control.sendall((json.dumps({'status':request['status'],'body':request['body']})+'\n').encode())
                control.close();control=None
                if upstream:upstream.close();upstream=None
                result = {'finished':True}
            else:
                raise ValueError('invalid_auth_relay_action')
            print('WDI_RESULT '+json.dumps(result,separators=(',',':')),flush=True)
    finally:
        if upstream:upstream.close()
        if control:control.close()


def start():
    return Broker()


if __name__ == '__main__':
    if sys.argv[1:] == ['relay']:
        relay()
    elif not sys.argv[1:]:
        patch_client()
    else:
        raise SystemExit('invalid action')
