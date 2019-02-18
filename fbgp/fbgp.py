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

from fbgp.bgp import BgpPeer, BgpRouter, Border
from fbgp.policy import Policy
from fbgp.faucet_connect import FaucetConnect
from fbgp.exabgp_connect import ExaBgpConnect

from faucet import faucet_experimental_api
from faucet import faucet

class FlowBasedBGP(app_manager.RyuApp):
    """An application runs on ExaBGP to process BGP routes received from peers."""

    _CONTEXTS = {
        'faucet_experimental_api': faucet_experimental_api.FaucetExperimentalAPI,
        }

    peers = None
    borders = None
    routerid = None
    faucet_connect = None # interface to Faucet
    exabgp_connect = None # interface to exabgp
    server_connect = None # interface to the route controller

    def __init__(self, *args, **kwargs):
        super(FlowBasedBGP, self).__init__(*args, **kwargs)
        self.logger = get_logger('fbgp',
                os.environ.get('FBGP_LOG', None),
                os.environ.get('FBGP_LOG_LEVEL', 'info'))
        self.faucet_api = kwargs['faucet_experimental_api']

    def stop(self):
        self.logger.info('%s is stopping...' % self.__class__.__name__)
        super(FlowBasedBGP, self).stop()

    @set_ev_cls(faucet.EventFaucetExperimentalAPIRegistered)
    def initialize(self, ev=None):
        self.logger.info('Initializing fBGP controller')
        self._load_config()
        self.faucet_connect = FaucetConnect(self._process_faucet_msg)
        self.faucet_connect.start()
        self.exabgp_connect = ExaBgpConnect(self._process_exabgp_msg, self.peers, self.routerid)
        self.exabgp_connect.start()

    def _load_config(self):
        config_file = os.environ.get('FBGP_CONFIG', '/etc/fbgp/fbgp.yaml')
        with open(config_file, 'r') as f:
            config = yaml.safe_load(f.read())
            self.import_policy = {}
            self.export_policy = {}
            self.routerid = ipaddress.ip_address(config['routerid'])
            self.peers = {}
            for peer_conf in config.pop('peers'):
                peer_ip = ipaddress.ip_address(peer_conf['peer_ip'])
                peer = BgpPeer(peer_ip=peer_ip,
                               peer_as=peer_conf['peer_as'],
                               local_ip=peer_conf.get('local_ip', None),
                               local_as=peer_conf.get('local_as', None),
                               peer_port=peer_conf.get('peer_port', 179))
                self.peers[peer_ip] = peer
            self.borders = {}
            for border_conf in config.pop('borders'):
                routerid = ipaddress.ip_address(border_conf['routerid'])
                self.borders[routerid] = Border(
                        routerid=routerid, nexthop=ipaddress.ip_address(border_conf['nexthop']))
            self.bgp = BgpRouter(self.logger, self.borders, self.peers, self.path_change_handler)
            self.logger.info('config loaded')

    def path_change_handler(self, peer, route, withdraw=False):
        # install route to Faucet
        if withdraw:
            self.faucet_api.del_route(
                    route.prefix, route.nexthop, dpid=peer.dp_id, vid=peer.vlan_vid)
        else:
            self.faucet_api.add_route(
                    route.prefix, route.nexthop, dpid=peer.dp_id, vid=peer.vlan_vid)

    def _process_exabgp_msg(self, msg):
        """Process message received from ExaBGP."""
        exabgp_msgs = self.bgp.process_exabgp_msg(msg)
        for msg in exabgp_msgs:
            self.exabgp_connect.send(msg)

    def _process_faucet_msg(self, msg):
        """Process message received from Faucet Controller."""
        dpid = msg['dp_id']
        if 'L2_LEARN' in msg and msg['L2_LEARN']['l3_src_ip'] != 'None':
            l2_learn = msg['L2_LEARN']
            ipa = ipaddress.ip_address(l2_learn['l3_src_ip'])
            vid = l2_learn['vid']
            port_no = l2_learn['port_no']
            if ipa in self.peers:
                peer = self.peers[ipa]
                if peer.is_connected:
                    return
                peer.connected(dpid, vid, port_no)
                self.logger.info('Peer connected: %s' % peer)
            else:
                for border in self.borders.values():
                    if border.nexthop == ipa:
                        border.connected(dpid, vid, port_no)
                        self.logger.info('Border connected: %s' % border)
        elif 'L2_EXPIRE' in msg:
            #TODO: handle expire event
            pass

    def _process_server_msg(self, msg):
        """Process message received from Route Controller."""
        pass
