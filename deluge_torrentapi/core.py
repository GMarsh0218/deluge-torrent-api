import logging
import os
import secrets

import deluge.component as component
from deluge.configmanager import ConfigManager
from deluge.plugins.pluginbase import CorePluginBase
from twisted.internet import reactor
from twisted.web.server import Site

from .api import APIRoot, TorrentService

log = logging.getLogger(__name__)
DEFAULTS = {'host': '127.0.0.1', 'port': 8190, 'api_token': '', 'allowed_paths': []}


class Core(CorePluginBase):
    listener = None

    def enable(self):
        self.config = ConfigManager('torrentapi.conf', DEFAULTS)
        if not self.config['api_token']:
            self.config['api_token'] = secrets.token_urlsafe(32)
            self.config.save()
        token = self.config['api_token']
        if not isinstance(token, str) or len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError('TorrentAPI api_token must contain at least 32 non-whitespace ASCII characters.')
        roots = self.config['allowed_paths']
        if not isinstance(roots, list) or any(not isinstance(p, str) or not os.path.isabs(p) for p in roots):
            raise ValueError('TorrentAPI allowed_paths must be a list of absolute directories.')
        service = TorrentService(component.get('Core'), component.get('TorrentManager'), roots)
        site = Site(APIRoot(service, token), timeout=30)
        site.noisy = False
        self.listener = reactor.listenTCP(int(self.config['port']), site, interface=self.config['host'])
        self.service = service
        alerts = component.get('AlertManager')
        alerts.register_handler('storage_moved_alert', service.storage_move_finished)
        alerts.register_handler('storage_moved_failed_alert', service.storage_move_finished)
        log.info('TorrentAPI listening on %s:%s', self.config['host'], self.config['port'])

    def disable(self):
        if self.listener is not None:
            alerts = component.get('AlertManager')
            alerts.deregister_handler('storage_moved_alert', self.service.storage_move_finished)
            alerts.deregister_handler('storage_moved_failed_alert', self.service.storage_move_finished)
            listener, self.listener = self.listener, None
            return listener.stopListening()

    def update(self):
        pass
