"""
"""

import sys, os, time, traceback
import json, ipaddress
import yaml, socket, collections
import eventlet
eventlet.monkey_patch()

from .cfg import CONF
from .utils import get_logger

from ryu.base import app_manager
from ryu.controller.handler import set_ev_cls
from ryu.lib import hub

from fbgp.bgp import BgpPeer
from fbgp.policy import Policy


class FlowBasedBGP(app_manager.RyuApp):
    """An application runs on ExaBGP to process BGP routes received from peers."""

    _CONTEXTS = {
        }

    peers = None
    faucet_connect = None # interface to Faucet
    exabgp_connect = None # interface to exabgp
    server_connect = None # interface to the route controller

    def __init__(self, *args, **kwargs):
        super(FlowBasedBGP, self).__init__(*args, **kwargs)
        self.logger = get_logger('fbgp', os.environ.get('FBGP_LOG', None), 'info')

    def stop(self):
        self.logger.info('%s is stopping...' % self.__class__.__name__)
        super(FlowBasedBGP, self).stop()

    def initialize(self):
        self.logger.info('Initializing fBGP controller')
        self._load_config()

    def _load_config(self):
        config_file = os.environ.get('FBGP_CONFIG', '/etc/fbgp/fbgp.yaml')
        with open(config_file, 'r') as f:
            config = yaml.safe_load(f.read())
            self.import_policy = {}
            self.export_policy = {}
            self.peers = {}
            for peer_conf in config.pop('peers'):
                peer_ip = ipaddress.ip_address(peer_conf['peer_ip'])
                peer = BgpPeer(peer_ip=peer_ip,
                               peer_as=peer_conf['peer_as'],
                               local_ip=peer_conf.get('local_ip', None),
                               local_as=peer_conf.get('local_as', None),
                               peer_port=peer_conf.get('peer_port', 179))
                self.peers[peer_ip] = peer

    def _process_exabgp_msg(self, msg):
        """Process message received from ExaBGP."""
        pass

    def _process_faucet_msg(self, msg):
        """Process message received from Faucet Controller."""
        pass

    def _process_server_msg(self, msg):
        """Process message received from Route Controller."""
        pass
