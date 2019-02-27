"""
"""
import eventlet
eventlet.monkey_patch()

import sys, os, time, traceback
import json, ipaddress
import yaml, socket, collections

from ryu.base import app_manager
from ryu.controller.handler import set_ev_cls
from ryu.lib import hub

from fbgp.cfg import CONF
from fbgp.utils import get_logger
from fbgp.bgp import BgpPeer, BgpRouter, Border
from fbgp.policy import Policy
from fbgp.faucet_connect import FaucetConnect
from fbgp.exabgp_connect import ExaBgpConnect
from fbgp.server_connect import ServerConnect

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
        for name, connector_cls, kwargs in [
                ('faucet_connect', FaucetConnect, {'handler': self._process_faucet_msg}),
                ('exabgp_connect', ExaBgpConnect, {'handler': self._process_exabgp_msg,
                                                   'peers': self.peers, 'routerid': self.routerid}),
                ('server_connect', ServerConnect, {'handler': self._process_server_msg})]:
            connector = connector_cls(**kwargs)
            connector.start()
            setattr(self, name, connector)

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

    def register(self):
        self._send_to_server({
            'msg_type': 'router_up', 'routerid': str(self.routerid), 'state': 'up'})

        for peer in self.peers.values():
            msg = {'msg_type': 'peer_up', 'peer_ip': str(peer.peer_ip), 'peer_as': peer.peer_as,
                   'local_ip': str(self.routerid), 'local_as': peer.local_as, 'state': peer.state}
            self._send_to_server(msg)
            if peer.is_connected:
                msg = {
                        'msg_type': 'nexthop_up', 'routerid': str(self.routerid),
                        'nexthop': str(peer.peer_ip), 'pathid': None, 'dp_id': peer.dp_id,
                        'port_no': peer.port_no, 'vlan_vid': peer.vlan_vid}
                self._send_to_server(msg)

    def deregister(self):
        pass

    def _send(self, connector, msg):
        if connector:
            connector.send(msg)
            self.logger.debug('sent a msg to server: %s' % msg)

    def _send_to_server(self, msg):
        self._send(self.server_connect, msg)

    def _send_to_exabgp(self, msg):
        self._send(self.exabgp_connect, msg)

    def _peer_state_change(self, peer_ip, state, **kwargs):
        peer = self.peers[peer_ip]
        msg_type = None
        method = None
        msg = {}
        if state == 'up':
            msg = {'msg_type': 'peer_up', 'peer_ip': str(peer_ip), 'peer_as': peer.peer_as,
                   'local_ip': str(self.routerid), 'local_as': peer.local_as, 'state': 'up'}
            method = None if peer.state == 'up' else self.bgp.peer_up
            kwargs['peer_ip'] = peer_ip
        elif state == 'down':
            msg = {'msg_type': 'peer_down', 'peer_ip': str(peer_ip)}
            method = None if peer.state =='down' else self.bgp.peer_down
            kwargs['peer_ip'] = peer_ip
        elif state == 'connected':
            msg = {
                    'msg_type': 'nexthop_up', 'routerid': str(self.routerid), 'nexthop': str(peer_ip),
                    'pathid': None, 'dp_id': kwargs['dp_id'], 'port_no': kwargs['port_no'],
                    'vlan_vid': kwargs['vlan_vid']}
            method = None if peer.is_connected else peer.connected
        elif state == 'disconnected':
            msg = {'msg_type': 'nexthop_down', 'routerid': str(self.routerid), 'nexthop': str(peer_ip)}
            method = None if not peer.is_connected else peer.disconnected
        if method and msg:
            self._send_to_server(msg)
            return method(**kwargs)
        return []

    def _process_exabgp_msg(self, msg):
        """Process message received from ExaBGP."""
        self.logger.debug('processing msg from exabgp: %r' % msg)
        if msg in ['done', 'error']:
            return []
        try:
            msg = json.loads(msg)
            if msg.get('type') == 'notification':
                #TODO: handle notification
                return []
            neighbor = msg.get('neighbor', {})
            if not neighbor:
                return []
            local_ip = ipaddress.ip_address(neighbor['address']['local'])
            peer_ip = ipaddress.ip_address(neighbor['address']['peer'])
            local_as = neighbor['asn']['local']
            peer_as = neighbor['asn']['peer']
            msgs = []
            if msg.get('type') == 'update' and 'update' in neighbor['message']:
                update = neighbor['message']['update']
                msgs = self.bgp.process_update(peer_ip, update)
            elif msg.get('type') == 'state':
                state = 'up' if neighbor['state'] == 'connected' else 'down'
                msgs = self._peer_state_change(peer_ip, state)
            for msg in msgs:
                self._send_to_exabgp(msg)
        except Exception as e:
            print(msg)
            traceback.print_exc()

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
                self._peer_state_change(ipa, 'connected', dp_id=dpid, port_no=port_no, vlan_vid=vid)
                self.logger.info('Peer %s (%s) is connected' % (peer.peer_ip, peer.peer_as))
            else:
                for border in self.borders.values():
                    if border.nexthop == ipa:
                        border.connected(dpid, vid, port_no)
                        self.logger.info('Border %s is connected' % border.routerid)
        elif 'L2_EXPIRE' in msg:
            #TODO: handle expire event
            pass

    def _process_server_msg(self, msg):
        """Process message received from Route Controller."""
        msg_type = msg['msg_type']
        if msg_type == 'server_connected':
            #TODO: process server connected event
            self.logger.info('Connected to server: %s' % msg['msg'])
            self.register()
        elif msg_type == 'server_disconnected':
            #TODO: process server disconnected event
            self.logger.info('Disconnected from server: %s' % msg['msg'])
            self.deregister()
        elif msg_type == 'server_command':
            #TODO: process server commands
            self.logger.info('Receive msg from server: %s' % msg['msg'])
