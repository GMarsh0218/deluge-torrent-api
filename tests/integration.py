"""Isolated daemon test. Run from the project root after building its egg."""
import pathlib
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import shutil

from deluge.config import Config


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main():
    project = pathlib.Path(__file__).resolve().parents[1]
    eggs = sorted((project / 'dist').glob('TorrentAPI-*-py%d.%d.egg' % sys.version_info[:2]))
    if not eggs:
        raise SystemExit('Build with this Python first: python setup.py bdist_egg')
    with tempfile.TemporaryDirectory(prefix='torrentapi-test-') as temp:
        base = pathlib.Path(temp)
        (base / 'plugins').mkdir()
        shutil.copy2(eggs[-1], base / 'plugins')
        http_port, rpc_port = free_port(), free_port()
        token = secrets.token_urlsafe(32)
        core = Config('core.conf', config_dir=temp)
        for key, value in {'enabled_plugins': ['TorrentAPI'], 'dht': False, 'upnp': False,
                           'natpmp': False, 'lsd': False,
                           'download_location': str(base / 'downloads')}.items():
            core[key] = value
        core.save()
        api = Config('torrentapi.conf', config_dir=temp)
        for key, value in {'api_token': token, 'host': '127.0.0.1', 'port': http_port,
                           'allowed_paths': [temp]}.items():
            api[key] = value
        api.save()
        with (base / 'daemon.log').open('w+') as log:
            daemon = subprocess.Popen([sys.executable, '-c',
                'from deluge.core.daemon_entry import start_daemon; start_daemon()',
                '-d', '-c', temp, '-p', str(rpc_port), '-L', 'info'], stdout=log, stderr=log)
            try:
                for _ in range(100):
                    try:
                        urllib.request.urlopen('http://127.0.0.1:%d/v1/health' % http_port, timeout=1)
                    except urllib.error.HTTPError as error:
                        if error.code == 401:
                            break
                        raise
                    except OSError:
                        if daemon.poll() is not None:
                            raise RuntimeError('Daemon exited before listening')
                        time.sleep(.1)
                else:
                    raise RuntimeError('Daemon did not start')
                subprocess.run([sys.executable, str(project / 'tests/daemon_checks.py'),
                    temp, token, str(http_port), str(rpc_port)], check=True, timeout=45)
            except BaseException:
                log.flush()
                log.seek(0)
                print(log.read(), file=sys.stderr)
                raise
            finally:
                daemon.terminate()
                try:
                    daemon.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.wait()


if __name__ == '__main__':
    main()
