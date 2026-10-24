import base64, json, pathlib, subprocess, sys, time, urllib.request, urllib.error
import libtorrent as lt

base=pathlib.Path(sys.argv[1]).resolve()
token=sys.argv[2]
url='http://127.0.0.1:'+sys.argv[3]
def api(method,path,body=None,auth=True):
    r=urllib.request.Request(url+path, data=json.dumps(body).encode() if body is not None else None, method=method,
        headers={'Content-Type':'application/json', **({'Authorization':'Bearer '+token} if auth else {})})
    try:
        with urllib.request.urlopen(r, timeout=5) as response:
            return response.status,json.load(response)
    except urllib.error.HTTPError as e:
        return e.code,json.load(e)
assert api('GET','/v1/health',auth=False)[0]==401
assert api('GET','/v1/health')[0]==200
magnet='magnet:?xt=urn:btih:'+'1'*40
code,body=api('POST','/v1/torrents',{'magnet':magnet, 'download_location':str(base/'downloads')})
print('magnet add',code,body);assert code==201
id=body['torrent_id']
assert api('GET','/v1/torrents/'+id)[0]==200
assert api('POST','/v1/torrents',{'magnet':magnet})[0]==409
assert api('POST','/v1/torrents/'+id+'/move',{'destination':str(base/'done')})[0]==409
assert api('PATCH','/v1/torrents/'+id,{'move_completed_path':str(base/'automatic')})[0]==200
assert api('POST','/v1/torrents',{'magnet':magnet,'download_location':'/outside'})[0]==403

incoming=base/'downloads';incoming.mkdir(exist_ok=True)
file=incoming/'fixture.txt';file.write_bytes(b'TorrentAPI integration fixture\n'*4096)
fs=lt.file_storage();fs.add_file(file.name,file.stat().st_size)
t=lt.create_torrent(fs, 16384);t.set_priv(True);lt.set_piece_hashes(t,str(incoming))
metadata=lt.bencode(t.generate()); torrent_id=str(lt.torrent_info(metadata).info_hash())
(base/'fixture.torrent').write_bytes(metadata)
# Use a local authenticated Deluge RPC connection only to inject metadata for
# our generated original fixture. No tracker or external download is needed.
script='''
import base64, pathlib, sys
from twisted.internet import task, defer
from deluge.ui.client import client
@defer.inlineCallbacks
def main(reactor):
    base=pathlib.Path(sys.argv[1])
    auth=next(x.split(':') for x in (base/'auth').read_text().splitlines() if x.startswith('localclient:'))
    yield client.connect('127.0.0.1',int(sys.argv[2]),auth[0],auth[1])
    result=yield client.core.add_torrent_file('fixture.torrent',base64.b64encode((base/'fixture.torrent').read_bytes()).decode(),{'download_location':str(base/'downloads'),'owner':'localclient','move_completed':False,'add_paused':False,'auto_managed':False})
    print(result)
    yield client.core.resume_session()
    yield client.core.resume_torrent(result)
    yield client.core.force_recheck([result])
    yield client.disconnect()
task.react(main)
'''
subprocess.run([sys.executable,'-c',script,str(base),sys.argv[4]],check=True,capture_output=True,text=True)
for _ in range(100):
    code,status=api('GET','/v1/torrents/'+torrent_id)
    if status.get('is_finished'):break
    time.sleep(.1)
else: raise AssertionError(status)
print('complete fixture',status['name'],status['is_finished'])
assert api('POST','/v1/torrents/'+torrent_id+'/move',{'destination':str(base/'done')})[0]==202
for _ in range(100):
    code,status=api('GET','/v1/torrents/'+torrent_id)
    if not status['moving'] and status['download_location']==str(base/'done'):break
    time.sleep(.1)
else:raise AssertionError(status)
assert (base/'done'/'fixture.txt').read_bytes()==b'TorrentAPI integration fixture\n'*4096
assert not file.exists()
print('move verified',status['download_location'],status['state'])
print('REAL DAEMON CHECK PASSED')
