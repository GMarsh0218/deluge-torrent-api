# Validation

Verified with Deluge 2.2.0, Python 3.12, libtorrent 2.1.1, and Twisted 26.4.0
on Linux. Deluge 2.1 is an intended compatibility target but was not run here.
Windows/macOS and other Python versions have not been integration-tested.

`python -m pytest -q`: **25 passed**.

`python tests/integration.py`, after `python setup.py bdist_egg`, starts an
isolated daemon using a temporary config and the built egg. It checks:

- The egg is discovered and its HTTP listener starts in real deluged.
- Missing authentication returns 401; authenticated health returns 200.
- Adding a magnet returns 201 and the ID can be queried.
- Duplicate magnets and incomplete moves return 409.
- A completion destination can be configured through PATCH.
- Paths outside the allowlist return 403.
- A locally generated original test file is added through authenticated native
  RPC solely to provide a complete fixture without downloading from peers.
- The HTTP API moves that completed torrent, reports completion, preserves
  exact file contents, removes the original file, and leaves the torrent seeding.

Automatic movement on a *fresh network download* was not exercised; this uses
Deluge's native `move_completed`/`move_completed_path` mechanism. Its API wiring
is covered by the unit suite and live configuration check.

## Reproducing the isolated test

Use a disposable environment with Deluge and libtorrent installed. On the test
host, Deluge's existing RPC certificate-generation code required an older
pyOpenSSL, and its plugin loader required setuptools' legacy `pkg_resources`:

```sh
python3 -m venv .test-venv
.test-venv/bin/python -m pip install \
  'deluge==2.2.0' 'libtorrent==2.1.1' 'twisted==26.4.0' \
  'setuptools<81' 'pyopenssl==23.3.0' 'service_identity==24.2.0' pytest wheel
.test-venv/bin/python -m pytest -q
.test-venv/bin/python setup.py bdist_egg
.test-venv/bin/python tests/integration.py
```

These are test-environment workarounds for upstream Deluge, not new plugin
runtime requirements. Installing the egg into an existing working daemon does
not require changing that daemon's dependencies. The tests use private,
trackerless fixtures and disable DHT, LSD, UPnP and NAT-PMP on the test daemon.
