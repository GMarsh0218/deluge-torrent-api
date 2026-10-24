import io
import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from twisted.internet.defer import Deferred
from twisted.web.test.requesthelper import DummyRequest
from twisted.web.server import NOT_DONE_YET
from deluge_torrentapi.api import APIRoot, APIError, TorrentService, destination

ID = 'a' * 40
MAGNET = 'magnet:?xt=urn:btih:' + ID
TOKEN = 'test-token-' + 'x' * 32

@pytest.fixture
def app():
    torrent = Mock(has_metadata=True, moving_storage_dest_path=None)
    state = {'is_finished': False, 'state': 'Downloading', 'download_location': '/data/incoming',
             'move_completed': False, 'move_completed_path': '/data/done', 'progress': 12.5}
    torrent.get_status.side_effect = lambda *a, **kw: dict(state)
    torrent.set_options.side_effect = state.update
    torrent.move_storage.return_value = True
    manager = SimpleNamespace(torrents={ID: torrent}, save_state=Mock())
    core = Mock()
    core.add_torrent_magnet.return_value = ID
    core.get_config_value.side_effect = {'download_location': '/data/incoming', 'move_completed': False}.get
    return APIRoot(TorrentService(core, manager, ['/data']), TOKEN), torrent, state, core

def call(root, method='GET', path='/v1/torrents', body=None, auth=TOKEN, raw=None, media='application/json'):
    r = DummyRequest([])
    r.method, r.path = method.encode(), path.encode()
    r.content = io.BytesIO(raw if raw is not None else json.dumps(body).encode())
    r.requestHeaders.addRawHeader(b'content-type', media.encode())
    if auth is not None:
        r.requestHeaders.addRawHeader(b'authorization', ('Bearer ' + auth).encode())
    result = root.render(r)
    if result != NOT_DONE_YET:
        r.write(result)
    return r, lambda: json.loads(b''.join(r.written))

@pytest.mark.parametrize('auth', [None, 'wrong'])
def test_auth(app, auth):
    r, data = call(app[0], auth=auth)
    assert r.responseCode == 401 and data()['error']['code'] == 'unauthorized'
    app[1].get_status.assert_not_called()

def test_add_and_status(app):
    r, data = call(app[0], 'POST', body={'magnet': MAGNET, 'download_location': '/data/incoming',
                    'move_completed_path': '/data/done', 'add_paused': True})
    assert r.responseCode == 201 and data()['torrent_id'] == ID
    options = app[3].add_torrent_magnet.call_args.args[1]
    assert options['move_completed'] is True and options['add_paused'] is True
    assert options['owner'] == 'localclient'
    r, data = call(app[0], path='/v1/torrents/' + ID)
    assert r.responseCode == 200 and data()['progress'] == 12.5

@pytest.mark.parametrize('body,code', [({'magnet': 'https://example.com'}, 400),
    ({'magnet': MAGNET, 'download_location': '/etc'}, 403),
    ({'magnet': MAGNET, 'download_location': '../data'}, 400),
    ({'magnet': MAGNET, 'add_paused': 'false'}, 400),
    ({'magnet': MAGNET, 'owner': 'admin'}, 400)])
def test_validation(app, body, code):
    r, _ = call(app[0], 'POST', body=body)
    assert r.responseCode == code
    app[3].add_torrent_magnet.assert_not_called()

def test_default_path_restriction(app):
    app[3].get_config_value.side_effect = {'download_location': '/outside'}.get
    r, _ = call(app[0], 'POST', body={'magnet': MAGNET})
    assert r.responseCode == 403

def test_duplicate(app):
    from deluge.error import AddTorrentError
    app[3].add_torrent_magnet.side_effect = AddTorrentError('duplicate')
    r, data = call(app[0], 'POST', body={'magnet': MAGNET})
    assert r.responseCode == 409 and data()['error']['code'] == 'add_rejected'

def test_deferred(app):
    pending = Deferred()
    app[3].add_torrent_magnet.return_value = pending
    r, _ = call(app[0], 'POST', body={'magnet': MAGNET})
    assert not r.finished
    pending.callback(ID)
    assert r.finished and r.responseCode == 201

def test_move(app):
    path = '/v1/torrents/' + ID + '/move'
    r, data = call(app[0], 'POST', path, {'destination': '/data/done'})
    assert r.responseCode == 409 and data()['error']['code'] == 'not_complete'
    app[1].move_storage.assert_not_called()
    app[2]['is_finished'] = True
    r, data = call(app[0], 'POST', path, {'destination': '/data/done'})
    assert r.responseCode == 202 and data()['status'] == 'move_requested'
    app[1].move_storage.assert_called_once_with('/data/done')
    app[1].moving_storage_dest_path = '/data/done'
    r, _ = call(app[0], 'POST', path, {'destination': '/data/other'})
    assert r.responseCode == 409

def test_failed_move(app):
    app[2]['is_finished'] = True
    app[1].move_storage.return_value = False
    r, data = call(app[0], 'POST', '/v1/torrents/' + ID + '/move', {'destination': '/data/done'})
    assert r.responseCode == 409 and data()['error']['code'] == 'move_rejected'

def test_completion_persistence(app):
    r, data = call(app[0], 'PATCH', '/v1/torrents/' + ID, {'move_completed_path': '/data/done'})
    assert r.responseCode == 200 and data()['move_completed'] is True
    app[0].service.manager.save_state.assert_called_once()
    app[2]['is_finished'] = True
    r, _ = call(app[0], 'PATCH', '/v1/torrents/' + ID, {'move_completed': True})
    assert r.responseCode == 409

@pytest.mark.parametrize('raw,media,code', [(b'{', 'application/json', 400),
    (b'[]', 'application/json', 400), (b'{}', 'text/plain', 415),
    (b'x' * 65537, 'application/json', 413)])
def test_body(app, raw, media, code):
    r, _ = call(app[0], 'POST', raw=raw, media=media)
    assert r.responseCode == code

@pytest.mark.parametrize('method,path,code', [('GET', '/v1/health', 200),
    ('DELETE', '/v1/torrents', 405), ('GET', '/v1/torrents/' + 'b' * 40, 404), ('GET', '/unknown', 404)])
def test_routes(app, method, path, code):
    r, _ = call(app[0], method, path)
    assert r.responseCode == code

def test_pagination(app):
    r, data = call(app[0])
    assert data()['total'] == 1
    with pytest.raises(APIError):
        app[0].service.dispatch('GET', ['v1', 'torrents'], {}, {b'limit': [b'501']})

def test_symlink_escape(tmp_path):
    root = tmp_path / 'allowed'
    root.mkdir()
    (root / 'escape').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(APIError) as exc:
        destination(str(root / 'escape' / 'outside'), [str(root)])
    assert exc.value.status == 403


def test_move_alert_clears_pending_and_ignores_stale_destination(app):
    app[2]['is_finished'] = True
    app[2]['state'] = 'Seeding'
    app[1].moving_storage_dest_path = '/data/previous'
    r, _ = call(app[0], 'POST', '/v1/torrents/' + ID + '/move', {'destination': '/data/done'})
    assert r.responseCode == 202
    assert app[0].service.status(ID)['moving'] is True
    alert = SimpleNamespace(handle=SimpleNamespace(info_hash=lambda: ID))
    app[0].service.storage_move_finished(alert)
    status = app[0].service.status(ID)
    assert status['moving'] is False and status['moving_to'] is None
