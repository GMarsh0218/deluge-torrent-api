# TorrentAPI for Deluge

A daemon-side Deluge 2 plugin that provides a small, authenticated JSON HTTP API.
No Deluge Web UI, separate server process, Flask, or additional runtime dependencies
are required. It uses Twisted, which Deluge already uses.

- Add a torrent using a v1 or hybrid magnet link containing `xt=urn:btih:…`.
- List torrents and poll individual status, progress, transfer speeds, and paths.
- Move a completed torrent's data without losing Deluge's tracking or seeding.
- Set or change the automatic move-on-completion destination before completion.

## Install

Target: Deluge 2.1/2.2 on Python 3.8+. The included egg is built for **Python 3.12**.
Deluge filters eggs by Python version, so build on your server if its Python minor
version differs. Deluge 1.x and pure v2 (`btmh` only) magnets are not supported.

1. Find the Python version used by **deluged**, especially inside Docker.
2. For another Python version, unzip the source and run with that Python:

   ```sh
   python3 -m venv .build-venv
   .build-venv/bin/python -m pip install 'setuptools<81' wheel
   .build-venv/bin/python setup.py bdist_egg
   ```

   On Windows, use `.build-venv\Scripts\python.exe` for the last two commands.
   `bdist_egg` is intentional: Deluge loads plugins as eggs rather than wheels.

3. Copy `dist/TorrentAPI-1.0.0-py3.X.egg` into the **daemon's** config `plugins/`
   directory. On Linux the config is often `~/.config/deluge/`; for a service it is
   the service account's directory, or the directory passed to `deluged -c`.
   In common Docker setups it is `/config/plugins/` inside the container.
4. Restart deluged, then enable **TorrentAPI** under Preferences → Plugins, or:

   ```sh
   deluge-console 'plugin -e TorrentAPI'
   ```

   The console must connect to the daemon where the egg was installed. A new egg
   may require `plugin -r` or a daemon restart before it appears. This is a core-only
   plugin: it has no extra GTK or Web UI preferences panel.
5. Read the generated `torrentapi.conf` in the daemon's config directory. Get
   `api_token` from the second JSON object; keep it private. It is generated on
   first enable and is never printed in logs. The API listens on `127.0.0.1:8190`.

## Configure

Stop deluged before editing `torrentapi.conf`; restart to apply changes. Deluge
uses **two consecutive JSON objects**, rather than one ordinary JSON document:

```json
{
  "file": 1,
  "format": 1
}
{
  "host": "127.0.0.1",
  "port": 8190,
  "api_token": "REPLACE_WITH_AT_LEAST_32_RANDOM_ASCII_CHARACTERS",
  "allowed_paths": ["/srv/torrents"]
}
```

Preserve the generated first object. An empty `api_token` generates a new token
at next enable. An empty `allowed_paths` (the default) allows any path the daemon
can write. Nonempty `allowed_paths` restricts destination paths, including Deluge's
configured default download and completion paths; resolved symlinks must stay
within a listed root. This is an administrative guard, not an OS sandbox: anyone
who can change filesystem symlinks or Deluge's own settings remains trusted.

Paths are absolute paths **on the daemon host**, inside its container if applicable.
Folders must be writable by the daemon user; Deluge creates missing move folders.
Configure Deluge's own default download/completion paths inside the allowed roots.

The bearer token grants access to **all daemon torrents**, regardless of their
Deluge owner. Newly added torrents belong to `localclient`. This is a trusted
administration API, not a per-user API. It intentionally has no delete endpoint.

For remote access, use an authenticated HTTPS reverse proxy or a private SSH/VPN
tunnel. The plugin itself serves HTTP. If binding `0.0.0.0` in Docker, restrict the
host port (for example `127.0.0.1:8190:8190`) and proxy it with TLS. Do not publish
plaintext bearer tokens over the public internet. Configure a reverse-proxy body
limit (64 KiB) and rate limits for network-facing use: JSON size is checked by the
plugin after Twisted receives the request body. No browser CORS access is enabled.
Protect the config directory because the token is stored in its config file.

## Examples

Use your generated token and replace the magnet/hash and host paths below.
All endpoints require `Authorization: Bearer TOKEN`, including health checks.

```sh
export TORRENT_API_TOKEN='YOUR_GENERATED_TOKEN'
export TORRENT_API_URL='http://127.0.0.1:8190'

# Check that the plugin is responding.
curl -H "Authorization: Bearer $TORRENT_API_TOKEN" \
  "$TORRENT_API_URL/v1/health"

# Add a magnet and automatically move its selected files when complete.
curl -X POST "$TORRENT_API_URL/v1/torrents" \
  -H "Authorization: Bearer $TORRENT_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"magnet":"magnet:?xt=urn:btih:REPLACE_WITH_40_HEX_HASH","download_location":"/srv/torrents/incoming","move_completed_path":"/srv/torrents/completed","add_paused":false}'
# 201: {"torrent_id":"..."}

# List status; pagination defaults to 100 and supports at most 500 per page.
curl -H "Authorization: Bearer $TORRENT_API_TOKEN" \
  "$TORRENT_API_URL/v1/torrents?limit=100&offset=0"

# Poll one torrent (set TORRENT_ID to the id returned by POST).
export TORRENT_ID='REPLACE_WITH_TORRENT_ID'
curl -H "Authorization: Bearer $TORRENT_API_TOKEN" \
  "$TORRENT_API_URL/v1/torrents/$TORRENT_ID"

# Move an already completed torrent. This starts an asynchronous storage move.
curl -X POST "$TORRENT_API_URL/v1/torrents/$TORRENT_ID/move" \
  -H "Authorization: Bearer $TORRENT_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"destination":"/srv/torrents/archive"}'
# 202: {"torrent_id":"...","destination":"/srv/torrents/archive","status":"move_requested"}

# Before completion, set/change the automatic destination.
curl -X PATCH "$TORRENT_API_URL/v1/torrents/$TORRENT_ID" \
  -H "Authorization: Bearer $TORRENT_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"move_completed_path":"/srv/torrents/completed"}'

# Disable automatic moving for a torrent.
curl -X PATCH "$TORRENT_API_URL/v1/torrents/$TORRENT_ID" \
  -H "Authorization: Bearer $TORRENT_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"move_completed":false}'
```

## HTTP contract

| Method | Endpoint | Result |
| --- | --- | --- |
| GET | `/v1/health` | 200, API health/version |
| POST | `/v1/torrents` | 201, torrent ID |
| GET | `/v1/torrents?limit=100&offset=0` | 200, `torrents`, `total`, `limit`, `offset` |
| GET | `/v1/torrents/{id}` | 200, status object |
| PATCH | `/v1/torrents/{id}` | 200, updated status |
| POST | `/v1/torrents/{id}/move` | 202, move accepted; 200 if already there |

POST add accepts `magnet` (required), `download_location`, `move_completed_path`,
and `add_paused` (boolean). Omitted values use Deluge defaults. Supplying
`move_completed_path` enables native Deluge move-on-completion. Duplicate magnets
return 409 rather than silently modifying an existing torrent.

PATCH accepts `move_completed_path` and/or `move_completed` (boolean). A path by
itself enables moving; `move_completed:false` explicitly disables it. Setting an
automatic move on an already completed torrent returns 409; use `/move` instead.
Deluge persists automatic completion options in its normal torrent state.

A status object includes `torrent_id`, `name`, `state`, `progress` (**0–100**),
`has_metadata`, `is_finished`, `eta` (seconds), byte totals, peer/seed counts,
`download_payload_rate` and `upload_payload_rate` (bytes/sec), `ratio`,
`download_location`, `move_completed`, `move_completed_path`, `message`,
`moving` and `moving_to`. Before metadata arrives, size/name/progress may be
incomplete. Deluge's `is_finished` means all **selected** files are complete.

A 202 move response only means Deluge accepted the request, not that disk I/O
succeeded. Poll status until `moving` is false and `download_location` matches
the requested path. Inspect `state`, `message`, and daemon logs for failures.
A move is rejected while another move is active or the torrent is incomplete.
Existing target file behavior follows Deluge/libtorrent's `dont_replace` mode;
use a fresh destination to avoid collisions. Moves preserve the torrent and
seeding; the API does not manually rename files behind Deluge's back.

Errors have the shape:

```json
{"error":{"code":"not_complete","message":"Torrent has not finished downloading selected files."}}
```

Statuses: 400 invalid input, 401 invalid/missing token, 403 disallowed path,
404 missing torrent/route, 405 unsupported method, 409 torrent conflict or move
rejection, 413 oversized JSON, 415 wrong content type, 500 unexpected server error.

## Development and verification

```sh
python3 -m pip install 'setuptools<81' twisted pytest deluge
python3 -m pytest -q
python3 setup.py bdist_egg
```

The unit suite covers authentication, validation, path restrictions including
symlinks/defaults, synchronous and Deferred adds, conflicts, status, completion
options, and move acceptance. Integration instructions/results are in
`VALIDATION.md`. Tests do not fetch copyrighted material or require trackers.

The plugin uses Deluge's native torrent options and `Torrent.move_storage`,
which exposes immediate rejection while `Core.move_storage` discards that return
value. All torrent operations run on Deluge's Twisted reactor thread.
