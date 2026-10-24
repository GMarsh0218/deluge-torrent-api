"""HTTP transport and service logic. All torrent access stays on the reactor thread."""
import hmac
import json
import logging
import os
import re
from urllib.parse import parse_qs, urlsplit

from twisted.internet.defer import maybeDeferred
from twisted.web.resource import Resource
from twisted.web.server import NOT_DONE_YET

log = logging.getLogger(__name__)
MAX_BODY = 65536
STATUS_KEYS = [
    'name', 'state', 'progress', 'is_finished', 'download_payload_rate',
    'upload_payload_rate', 'eta', 'total_size', 'total_done', 'total_wanted',
    'total_remaining', 'num_peers', 'num_seeds', 'ratio', 'download_location',
    'move_completed', 'move_completed_path', 'message',
]


class APIError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


def reject_unknown(body, allowed):
    unknown = set(body) - set(allowed)
    if unknown:
        raise APIError(400, 'invalid_fields', 'Unknown fields: ' + ', '.join(sorted(unknown)))


def destination(value, roots):
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise APIError(400, 'invalid_path', 'Destination must be a nonempty absolute path.')
    if not os.path.isabs(value):
        raise APIError(400, 'invalid_path', 'Paths must be absolute on the Deluge host.')
    path = os.path.realpath(value)
    if roots:
        allowed = False
        for root in roots:
            root = os.path.realpath(root)
            try:
                allowed = allowed or os.path.commonpath([root, path]) == root
            except ValueError:
                pass
        if not allowed:
            raise APIError(403, 'path_forbidden', 'Destination is outside allowed_paths.')
    return path


def validate_magnet(value):
    if not isinstance(value, str) or len(value) > 16384:
        raise APIError(400, 'invalid_magnet', 'Provide a magnet URI with a BitTorrent v1 hash.')
    try:
        parsed = urlsplit(value)
        hashes = parse_qs(parsed.query).get('xt', [])
        valid = parsed.scheme.lower() == 'magnet' and any(
            re.fullmatch(r'urn:btih:(?:[0-9a-fA-F]{40}|[A-Za-z2-7]{32})', x)
            for x in hashes
        )
    except ValueError:
        valid = False
    if not valid:
        raise APIError(400, 'invalid_magnet', 'Provide a magnet URI with a BitTorrent v1 hash (btih).')
    return value


class TorrentService:
    def __init__(self, core, manager, roots):
        self.core, self.manager, self.roots = core, manager, roots
        self.pending_moves = {}

    def storage_move_finished(self, alert):
        try:
            self.pending_moves.pop(str(alert.handle.info_hash()), None)
        except RuntimeError:
            pass

    def torrent(self, torrent_id):
        if not re.fullmatch(r'[0-9a-fA-F]{40,64}', torrent_id):
            raise APIError(404, 'not_found', 'Torrent not found.')
        try:
            return self.manager.torrents[torrent_id.lower()]
        except KeyError:
            raise APIError(404, 'not_found', 'Torrent not found.')

    def status(self, torrent_id):
        torrent = self.torrent(torrent_id)
        result = dict(torrent.get_status(STATUS_KEYS, update=True))
        torrent_id = torrent_id.lower()
        moving = torrent_id in self.pending_moves or result.get('state') == 'Moving'
        result.update(torrent_id=torrent_id, has_metadata=bool(torrent.has_metadata),
                      moving=moving, moving_to=(self.pending_moves.get(torrent_id) or
                      torrent.moving_storage_dest_path) if moving else None)
        return result

    def dispatch(self, method, parts, body, args):
        if parts == ['v1', 'health']:
            if method != 'GET':
                raise APIError(405, 'method_not_allowed', 'Use GET.')
            return 200, {'status': 'ok', 'api_version': 1}
        if parts == ['v1', 'torrents']:
            if method == 'GET':
                try:
                    limit = int(args.get(b'limit', [b'100'])[0])
                    offset = int(args.get(b'offset', [b'0'])[0])
                    if not 1 <= limit <= 500 or offset < 0:
                        raise ValueError()
                except (ValueError, TypeError):
                    raise APIError(400, 'invalid_pagination', 'limit must be 1..500; offset must be >= 0.')
                ids = sorted(self.manager.torrents)
                return 200, {'torrents': [self.status(t) for t in ids[offset:offset + limit]],
                             'total': len(ids), 'limit': limit, 'offset': offset}
            if method == 'POST':
                return self.add(body)
            raise APIError(405, 'method_not_allowed', 'Use GET or POST.')
        if len(parts) in (3, 4) and parts[:2] == ['v1', 'torrents']:
            torrent_id = parts[2].lower()
            if len(parts) == 3:
                if method == 'GET':
                    return 200, self.status(torrent_id)
                if method == 'PATCH':
                    return self.configure_completion(torrent_id, body)
                raise APIError(405, 'method_not_allowed', 'Use GET or PATCH.')
            if parts[3] == 'move':
                if method != 'POST':
                    raise APIError(405, 'method_not_allowed', 'Use POST.')
                reject_unknown(body, ['destination'])
                dest = destination(body.get('destination'), self.roots)
                torrent = self.torrent(torrent_id)
                status = self.status(torrent_id)
                if not status['has_metadata'] or not status['is_finished']:
                    raise APIError(409, 'not_complete', 'Torrent has not finished downloading selected files.')
                if status['moving']:
                    raise APIError(409, 'move_in_progress', 'A storage move is already in progress.')
                if os.path.realpath(status['download_location']) == dest:
                    return 200, {'torrent_id': torrent_id, 'destination': dest, 'status': 'already_there'}
                if not torrent.move_storage(dest):
                    raise APIError(409, 'move_rejected', 'Deluge could not start the move; check daemon logs and permissions.')
                self.pending_moves[torrent_id] = dest
                return 202, {'torrent_id': torrent_id, 'destination': dest, 'status': 'move_requested'}
        raise APIError(404, 'not_found', 'Endpoint not found.')

    def add(self, body):
        reject_unknown(body, ['magnet', 'download_location', 'move_completed_path', 'add_paused'])
        magnet = validate_magnet(body.get('magnet'))
        # The token grants daemon-wide access; avoid depending on an unrelated RPC session.
        options = {'owner': 'localclient'}
        for field in ('download_location', 'move_completed_path'):
            if field in body:
                options[field] = destination(body[field], self.roots)
        if 'move_completed_path' in body:
            options['move_completed'] = True
        if 'add_paused' in body:
            if not isinstance(body['add_paused'], bool):
                raise APIError(400, 'invalid_boolean', 'add_paused must be true or false.')
            options['add_paused'] = body['add_paused']
        # Validate configured defaults too: do not bypass allowed_paths by omitting a path.
        if self.roots:
            if 'download_location' not in options:
                options['download_location'] = destination(self.core.get_config_value('download_location'), self.roots)
            if 'move_completed' not in options and self.core.get_config_value('move_completed'):
                options['move_completed'] = True
                options['move_completed_path'] = destination(self.core.get_config_value('move_completed_path'), self.roots)
        from deluge.error import AddTorrentError
        d = maybeDeferred(self.core.add_torrent_magnet, magnet, options)

        def added(torrent_id):
            if not torrent_id:
                raise APIError(409, 'add_rejected', 'Deluge did not add the torrent.')
            return 201, {'torrent_id': torrent_id}

        def failed(failure):
            if failure.check(AddTorrentError):
                raise APIError(409, 'add_rejected', 'Deluge rejected the magnet; it may already exist. Check daemon logs.')
            return failure
        d.addCallback(added)
        d.addErrback(failed)
        return d

    def configure_completion(self, torrent_id, body):
        reject_unknown(body, ['move_completed', 'move_completed_path'])
        if not body:
            raise APIError(400, 'invalid_fields', 'Provide move_completed or move_completed_path.')
        torrent = self.torrent(torrent_id)
        options = {}
        if 'move_completed_path' in body:
            options['move_completed_path'] = destination(body['move_completed_path'], self.roots)
            options['move_completed'] = True
        if 'move_completed' in body:
            if not isinstance(body['move_completed'], bool):
                raise APIError(400, 'invalid_boolean', 'move_completed must be true or false.')
            options['move_completed'] = body['move_completed']
        status = self.status(torrent_id)
        if options.get('move_completed') and status['is_finished']:
            raise APIError(409, 'already_complete', 'Use POST /v1/torrents/{id}/move for a completed torrent.')
        if options.get('move_completed') and 'move_completed_path' not in options:
            options['move_completed_path'] = destination(status['move_completed_path'], self.roots)
        torrent.set_options(options)
        self.manager.save_state()
        return 200, self.status(torrent_id)


class APIRoot(Resource):
    isLeaf = True

    def __init__(self, service, token):
        super().__init__()
        self.service = service
        self.expected_auth = ('Bearer ' + token).encode('ascii')

    def render(self, request):
        # Never put tokens or request bodies into access logs.
        supplied = request.requestHeaders.getRawHeaders(b'authorization', [])
        if len(supplied) != 1 or not hmac.compare_digest(supplied[0], self.expected_auth):
            request.setHeader(b'WWW-Authenticate', b'Bearer')
            return self.reply(request, 401, {'error': {'code': 'unauthorized', 'message': 'A valid bearer token is required.'}})
        try:
            method = request.method.decode('ascii')
            parts = request.path.decode('utf-8').strip('/').split('/')
            body = {}
            if method in ('POST', 'PATCH'):
                media = request.getHeader('content-type') or ''
                if media.split(';')[0].strip().lower() != 'application/json':
                    raise APIError(415, 'unsupported_media_type', 'Use Content-Type: application/json.')
                raw = request.content.read(MAX_BODY + 1)
                if len(raw) > MAX_BODY:
                    raise APIError(413, 'body_too_large', 'Maximum JSON body size is 64 KiB.')
                try:
                    body = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise APIError(400, 'invalid_json', 'Body must be valid JSON.')
                if not isinstance(body, dict):
                    raise APIError(400, 'invalid_json', 'Body must be a JSON object.')
        except APIError as error:
            return self.error_reply(request, error)
        except (ValueError, UnicodeError):
            return self.error_reply(request, APIError(400, 'invalid_request', 'Invalid request path or method.'))

        disconnected = [False]
        def connection_lost(_failure):
            disconnected[0] = True
            return None
        request.notifyFinish().addErrback(connection_lost)

        def finish(result):
            if not disconnected[0]:
                request.write(self.reply(request, *result))
                request.finish()

        def failed(failure):
            if failure.check(APIError):
                error = failure.value
            else:
                log.error('TorrentAPI request failed: %s', failure.getTraceback())
                error = APIError(500, 'internal_error', 'Operation failed; check daemon logs.')
            finish((error.status, {'error': {'code': error.code, 'message': error.message}}))

        d = maybeDeferred(self.service.dispatch, method, parts, body, request.args)
        d.addCallback(finish)
        d.addErrback(failed)
        return NOT_DONE_YET

    @staticmethod
    def reply(request, status, body):
        data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
        request.setResponseCode(status)
        request.setHeader(b'Content-Type', b'application/json; charset=utf-8')
        request.setHeader(b'Cache-Control', b'no-store')
        request.setHeader(b'X-Content-Type-Options', b'nosniff')
        return data

    def error_reply(self, request, error):
        return self.reply(request, error.status, {'error': {'code': error.code, 'message': error.message}})
