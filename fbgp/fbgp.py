"""
"""
import eventlet
eventlet.monkey_patch()

import sys
import os
import time
import traceback
import json
import ipaddress
import yaml
import socket
import collections

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
    current_pathid = 0
    path_mapping = None # mapping between a peer and path, managed by the route server

    def __init__(self, *args, **kwargs):
        super(FlowBasedBGP, self).__init__(*args, **kwargs)
        self.logger = get_logger('fbgp',
                os.environ.get('FBGP_LOG', None),
                os.environ.get('FBGP_LOG_LEVEL', 'info'))
        self.faucet_api = kwargs['faucet_experimental_api']
        self.nexthop_to_pathid = {}
        self.path_mapping = collections.defaultdict(set)
        self.vip_assignment = {}
        self.rcv_msg_q = eventlet.Queue(256)

    def stop(self):
        self.logger.info('%s is stopping...' % self.__class__.__name__)
        super(FlowBasedBGP, self).stop()
        sys.exit()

    def _load_config(self):
        config_file = os.environ.get('FBGP_CONFIG', '/etc/fbgp/fbgp.yaml')
        self.vlans = {}
        for dp in [valve.dp for valve in self.valves.values()]:
            self.vlans.update(dp.vlans)
        with open(config_file, 'r') as f:
            config = yaml.safe_load(f.read())
            self.import_policy = {}
            self.export_policy = {}
            self.routerid = ipaddress.ip_address(config['routerid'])
            self.peers = {}
            for peer_conf in config.pop('peers'):
                peer_ip = ipaddress.ip_address(peer_conf['peer_ip'])
                local_ip = peer_conf.get('local_ip')
                local_ip = ipaddress.ip_address(local_ip) if local_ip else None
                peer = BgpPeer(peer_ip=peer_ip,
                               peer_as=peer_conf['peer_as'],
                               local_ip=local_ip,
                               local_as=peer_conf.get('local_as', None),
                               peer_port=peer_conf.get('peer_port', 179))
                for vlan in self.vlans.values():
                    if vlan.ip_in_vip_subnet(peer_ip):
                        peer.vlan = vlan
                        faucet_vips = vlan.faucet_vips_by_ipv(peer_ip.version)
                        if faucet_vips:
                            peer.faucet_vip = list(faucet_vips)[0]
                self.peers[peer_ip] = peer
            self.borders = {}
            for border_conf in config.pop('borders'):
                routerid = ipaddress.ip_address(border_conf['routerid'])
                self.borders[routerid] = Border(
                        routerid=routerid, nexthop=ipaddress.ip_address(border_conf['nexthop']))
            self.bgp = BgpRouter(self.borders, self.peers, self.path_change_handler)
            self.logger.info('config loaded')

    @set_ev_cls(faucet.EventFaucetExperimentalAPIRegistered)
    def initialize(self, ev=None):
        self.logger.info('Initializing fBGP controller')
        self.valves = self.faucet_api.faucet.valves_manager.valves
        if not self.valves:
            self.logger.error('Exitting...failed to get info from Faucet (Faucet probably has failed)')
            self.stop()
        eventlet.spawn(self._msg_processor)
        self._load_config()
        for name, connector_cls, kwargs in [
                ('faucet_connect', FaucetConnect, {'handler': self._rcv_faucet_msg}),
                ('exabgp_connect', ExaBgpConnect, {'handler': self._rcv_exabgp_msg,
                                                   'peers': self.peers, 'routerid': self.routerid}),
                ('server_connect', ServerConnect, {'handler': self._rcv_server_msg})]:
            connector = connector_cls(**kwargs)
            setattr(self, name, connector)
            self.logger.info('Created connector: %s' % name)
        for name in ['faucet_connect', 'exabgp_connect', 'server_connect']:
            connector = getattr(self, name)
            t = connector.start()
            if t is not None:
                self.logger.info('Connector %s started' % name)
            else:
                self.logger.info('Connector %s failed to start' % name)
                self.stop()

    def _rcv_exabgp_msg(self, msg):
        if msg in ['done', 'error'] or not msg:
            return
        self.rcv_msg_q.put(('exabgp', msg))

    def _rcv_faucet_msg(self, msg):
        self.rcv_msg_q.put(('faucet', msg))

    def _rcv_server_msg(self, msg):
        self.rcv_msg_q.put(('server', msg))

    def _msg_processor(self):
        self.logger.info('Started message processor')
        while True:
            try:
                (source, msg) = self.rcv_msg_q.get()
                if source == 'exabgp':
                    self._process_exabgp_msg(msg)
                elif source == 'faucet':
                    self._process_faucet_msg(msg)
                elif source == 'server':
                    self._process_server_msg(msg)
            except Exception as e:
                self.logger.error('Error when processing msg: %s from %s: %s' % (msg, source, e))

    def _get_pathid(self, nexthop):
        """Return a unique pathid for a nexthop."""
        if nexthop in self.nexthop_to_pathid:
            return self.nexthop_to_pathid[nexthop]
        else:
            self.current_pathid += 1
            self.nexthop_to_pathid[nexthop] = self.current_pathid
            return self.current_pathid

    def _get_vip(self, nexthop, vlan):
        """return vip (extra) if we still have one."""
        if not (nexthop and vlan):
            return
        if (nexthop, vlan) in self.vip_assignment:
            return self.vip_assignment[(nexthop, vlan)]
        used_vips = set(self.vip_assignment.values())
        for vip in vlan.faucet_ext_vips:
            if vip not in used_vips:
                self.vip_assignment[(nexthop, vlan)] = vip
                return vip
        return None

    def _update_fib(self, prefix, nexthop, dpid=None, vid=None, pathid=None, add=True):
        if add:
            self.faucet_api.add_route(prefix, nexthop, dpid=dpid, vid=vid, pathid=pathid)
            self.logger.debug(
                'Added extended FIB rule to datapath: prefix=%s, nexthop=%s, pathid=%s, dpid=%s, vid=%s' % (
                    str(prefix), str(nexthop), pathid, dpid, vid))

        else: # consider if the route is still being used by some peers before deleteing
            #self.faucet_api.del_route(prefix, nexthop, dpid=dpid, vid=vid, pathid=pathid)
            pass

    def _update_mapping(self, vip, pathid, dpid, vid, add=True):
        if add:
            self.faucet_api.add_ext_vip(vip, pathid=pathid, dpid=dpid, vid=vid)
            self.logger.info(
                'Added mapping rule to datapath: vip=%s, pathid=%s, dpid=%s, vid=%s' % (
                    str(vip), pathid, dpid, vid))
        else:
            #TODO: need to check if there is peer using this before deleting
            #self.faucet_api.del_ext_vip(vip, pathid=pathid, dpid=dpid, vid=vid)
            pass

    def path_change_handler(self, peer, route, withdraw=False):
        """handle route advertisement or withdrawal event."""
        msgs = []
        if not route:
            return []
        if withdraw:
            new_best, cur_best = self.bgp.del_route(route)
        else:
            new_best, cur_best = self.bgp.add_route(route)
        if new_best:
            nexthop = new_best.nexthop
            if peer.is_ibgp() and nexthop in self.borders:
                nexthop = self.borders[nexthop].nexthop
            self.logger.info('new best path for %s via %s: %s' % (route.prefix, nexthop, new_best))
            self.logger.debug('previous best path for %s was %s' % (route.prefix, cur_best))
            self._update_fib(new_best.prefix, nexthop, peer.dp_id, peer.vlan_vid)

        for other_peer in self._other_peers(peer):
            if other_peer in self.path_mapping[route.prefix, route.nexthop]:
                gateway = self._get_vip(route.nexthop, other_peer.vlan)
                pathid = self._get_pathid(route.nexthop)
                if withdraw:
                    if new_best:
                        msgs.extend(self.bgp.announce(other_peer, new_best))
                    else:
                        msgs.extend(self.bgp.withdraw(other_peer, route))
                        self._update_mapping(
                            gateway, pathid, other_peer.dp_id, other_peer.vlan_vid, False)
                        self._update_fib(
                            route.prefix, route.nexthop, peer.dp_id, peer.vlan_vid, pathid, False)
                else:
                    msgs.extend(self.bgp.announce(other_peer, route, gateway))
                    self._update_mapping(
                        gateway, pathid, other_peer.dp_id, other_peer.vlan_vid)
                    self._update_fib(
                        route.prefix, route.nexthop, peer.dp_id, peer.vlan_vid, pathid)
            elif new_best and new_best != cur_best:
                if other_peer.is_ibgp():
                    msgs.extend(self.bgp.announce(other_peer, new_best, self.routerid))
                else:
                    msgs.extend(self.bgp.announce(other_peer, new_best))
            elif not new_best and cur_best:
                msgs.extend(self.bgp.withdraw(other_peer, cur_best))
        return msgs

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
                        'nexthop': str(peer.peer_ip), 'pathid': self._get_pathid(peer.peer_ip),
                        'dp_id': peer.dp_id, 'port_no': peer.port_no, 'vlan_vid': peer.vlan_vid}
                self._send_to_server(msg)
            for route in peer.routes():
                self._notify_route_change(peer.peer_ip, route)
        for border in self.borders.values():
            if border.connected and border.dp_id and border.vlan_vid and border.port_no:
                attrs = {'dp': border.dp_id, 'vlan': border.vlan_vid, 'port': border.port_no}
                src = str(self.routerid)
                dst = str(border.routerid)
                self._send_to_server({'msg_type': 'link_up', 'src': src, 'dst': dst, 'attributes': attrs})

    def deregister(self):
        pass

    def _route_by_nexthop(self, prefix, nexthop):
        """return a route for a prefix by its nexthop."""
        for route in self.bgp.loc_rib.get(prefix, []):
            if route.nexthop == nexthop:
                return route
        return None

    def _add_mapping(self, peer_ip, prefix, nexthop, egress=None, pathid=None):
        """create a mapping between a peer and a route.
        egress is None assuming the nexthop is local"""
        if pathid is None and egress is None or self.routerid == egress: # this is the local route
            peer = self.peers[peer_ip]
            self.path_mapping[prefix, nexthop].add(peer)
            vip = self._get_vip(nexthop, peer.vlan)
            if not vip:
                return []
            mypathid = self._get_pathid(nexthop)
            if mypathid != pathid:
                self.logger.error('There must be something wrong, pathids differ')
                return []
            route = self._route_by_nexthop(prefix, nexthop)
            if route:
                self._update_mapping(vip, pathid, peer.dp_id, peer.vlan_vid)
                learned_peer = self.peers[route.learned_from_peer]
                self._update_fib(prefix, nexthop, learned_peer.dp_id, learned_peer.vlan_vid, pathid)
            return self.bgp.announce(peer, route, gateway=vip)
        else:
            #TODO: handle the case when route is remote
            return []

    def _del_mapping(self, peer_ip, prefix, nexthop, egress=None, pathid=None):
        msgs = []
        if egress is None and pathid is None:
            peer = self.peers[peer_ip]
            if peer not in self.path_mapping[prefix, nexthop]:
                return []
            best_route = self.bgp.best_routes.get(prefix)
            if best_route:
                # advertise best route instead
                msgs = self.bgp.announce(peer, best_route)
            else:
                route = self._route_by_nexthop(prefix, nexthop)
                msgs = self.bgp.withdraw(peer, route)
        return msgs

    def _notify_route_change(self, peer_ip, route, withdraw=False):
        """notify the route server about a route."""
        if not route:
            return
        msg_type = 'route_down' if withdraw else 'route_up'
        msg = {
                'msg_type': msg_type, 'peer_ip': str(peer_ip), 'next_hop': str(route.nexthop),
                'prefix': str(route.prefix), 'local_pref': route.local_pref, 'med': route.med,
                'as_path': route.as_path}
        self._send_to_server(msg)

    def _send(self, connector, msg):
        if connector:
            connector.send(msg)
            self.logger.debug('sent a msg to %s: %s' % (connector.__class__.__name__, msg))

    def _send_to_server(self, msg):
        self._send(self.server_connect, msg)

    def _send_to_exabgp(self, msg):
        self._send(self.exabgp_connect, msg)

    def _peer_bgp_up(self, peer):

        def non_best_route(peer, routes):
            for route in routes:
                if peer in self.path_mapping[route.prefix, route.nexthop]:
                    return route
            return None

        def best_route(prefix):
            if prefix in self.bgp.best_routes:
                return self.bgp.best_routes[prefix]
            return None

        if peer.state == 'up':
            return []
        self._send_to_server({
            'msg_type': 'peer_up', 'peer_ip': str(peer.peer_ip), 'peer_as': peer.peer_as,
            'local_ip': str(self.routerid), 'local_as': peer.local_as, 'state': 'up'})
        msgs = []
        peer.bgp_session_up()
        # advertise local subnets
        prefixes = set([str(p.faucet_vip.network) for p in self.peers.values() if p.faucet_vip])
        for prefix in prefixes:
            msgs.extend(self.bgp.announce_prefix(peer, prefix))
        # for each prefix, advertise non-best path if it is configured, otherwise advertise best path
        for prefix, routes in self.bgp.loc_rib.items():
            gateway = None
            pathid = None
            route = non_best_route(peer, routes)
            if route:
                gateway = self._get_vip(route.nexthop, peer.vlan)
                pathid = self._get_pathid(route.nexthop)
                self._update_mapping(gateway, pathid, peer.dp_id, peer.vlan_vid)
                learned_peer = self.peers[route.learned_from_peer]
                self._update_fib(prefix, route.nexthop, learned_peer.dp_id, learned_peer.vlan_vid, pathid)
            else:
                route = best_route(prefix)
            msgs.extend(self.bgp.announce(peer, route, gateway))
        return msgs

    def _border_connected(self, border, dpid, vid, port_no):
        attrs = {'dp': dpid, 'vlan': vid, 'port': port_no}
        src = str(self.routerid)
        dst = str(border.routerid)
        self._send_to_server({'msg_type': 'link_up', 'src': src, 'dst': dst, 'attributes': attrs})
        if border.disconnected():
            border.connected(dpid, vid, port_no)
            self.logger.info('Border %s is connected' % border.routerid)

    def _border_disconnected(self, border):
        src = str(self.routerid)
        dst = str(border.routerid)
        self._send_to_server({'msg_type': 'link_down', 'src': src, 'dst': dst})
        border.disconnected()
        self.logger.info('Border %s is disconnected' % border.routerid)

    def _peer_bgp_down(self, peer):
        if peer.state == 'down':
            return []
        self._send_to_server({'msg_type': 'peer_down', 'peer_ip': str(peer.peer_ip)})
        msgs = []
        for route in peer._rib_in.values():
            msgs.extend(self.path_change_handler(peer, route, True))
        peer.bgp_session_down()
        return msgs

    def _peer_connected(self, peer, dp_id, vlan_vid, port_no):
        if peer.is_connected:
            return []
        peer.connected(dp_id, vlan_vid, port_no)
        self._send_to_server({
            'msg_type': 'nexthop_up', 'routerid': str(self.routerid),
            'nexthop': str(peer.peer_ip), 'pathid': self._get_pathid(peer.peer_ip),
            'dp_id': dp_id, 'port_no': port_no, 'vlan_vid': vlan_vid})
        return []

    def _peer_disconnected(self, peer):
        if not peer.is_connected:
            return []
        peer.disconnected()
        self._send_to_server({
            'msg_type': 'nexthop_down', 'routerid': str(self.routerid),
            'nexthop': str(peer.peer_ip)})
        return []

    def _peer_state_change(self, peer_ip, state, **kwargs):
        peer = self.peers[peer_ip]
        method = None
        kwargs['peer'] = peer
        if state == 'up':
            method = self._peer_bgp_up
        elif state == 'down':
            method = self._peer_bgp_down
        elif state == 'connected':
            method = self._peer_connected
        elif state == 'disconnected':
            method = self._peer_disconnected
        if method:
            return method(**kwargs)
        return []

    def _process_exabgp_msg(self, msg):
        """Process message received from ExaBGP."""
        if msg in ['done', 'error'] or not msg:
            return []
        self.logger.debug('processing msg from exabgp: %r' % msg)
        try:
            msg = json.loads(msg)
            if msg.get('type') == 'notification':
                #TODO: handle notification
                return []
            neighbor = msg.get('neighbor', {})
            if not neighbor or neighbor.get('direction') == 'send':
                return []
            local_ip = ipaddress.ip_address(neighbor['address']['local'])
            peer_ip = ipaddress.ip_address(neighbor['address']['peer'])
            local_as = neighbor['asn']['local']
            peer_as = neighbor['asn']['peer']
            msgs = []
            if msg.get('type') == 'update' and 'update' in neighbor['message']:
                update = neighbor['message']['update']
                msgs = self._process_bgp_update(peer_ip, update)
            elif msg.get('type') == 'state':
                state = 'up' if neighbor['state'] == 'up' else 'down'
                msgs = self._peer_state_change(peer_ip, state)
            for msg in msgs:
                self._send_to_exabgp(msg)
        except Exception as e:
            self.logger.error('Error when processing msg %s: %s' % (msg, e))
            traceback.print_exc()

    def _other_peers(self, peer):
        return [other_peer for other_peer in self.peers.values() if other_peer != peer]

    def _process_bgp_update(self, peer_ip, update):
        """Process a BGP update received from ExaBGP."""
        self.logger.debug('processing update from %s: %s' % (peer_ip, update))
        try:
            msgs = []
            if peer_ip not in self.peers:
                return []
            peer = self.peers[peer_ip]
            if 'announce' in update and 'ipv4 unicast' in update['announce']:
                attributes = update['attribute']
                if 'as-path' not in attributes:
                    if peer.local_as != peer.peer_as:
                        self.logger.error('received malformed update')
                        return []
                    attributes['as-path'] = [peer.peer_as]
                elif peer.local_as in attributes['as-path']: # loop avoidance
                    return []
                for name, attr in [
                        ('origin', 'origin'), ('igp', 'igp'), ('as_path', 'as-path'),
                        ('med', 'med'), ('community', 'communities'),
                        ('local_pref', 'local-preference')]:
                    attributes[name] = update['attribute'].get(attr)

                attributes['internal'] = peer.local_as == peer.peer_as

                for nexthop, nlris in update['announce']['ipv4 unicast'].items():
                    nexthop = ipaddress.ip_address(nexthop)
                    if nexthop == peer.local_ip:
                        continue
                    for prefix in nlris:
                        prefix = ipaddress.ip_network(prefix['nlri'])
                        route = peer.rcv_announce(prefix, nexthop, **attributes)
                        self._notify_route_change(peer_ip, route)
                        msgs.extend(self.path_change_handler(peer, route))
            if 'withdraw' in update and 'ipv4 unicast' in update['withdraw']:
                for prefix in update['withdraw']['ipv4 unicast']:
                    prefix = ipaddress.ip_network(prefix['nlri'])
                    route = peer.rcv_withdraw(prefix)
                    if route:
                        self._notify_route_change(peer_ip, route, True)
                        msgs.extend(self.path_change_handler(peer, route, True))
            return msgs
        except Exception as e:
            self.logger.error('Error when processing update %s: %s' % (update, e))
            traceback.print_exc()
        return []

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
                self.logger.info('Peer %s (ASN: %s) is connected' % (peer.peer_ip, peer.peer_as))
            else:
                for border in self.borders.values():
                    if border.nexthop == ipa:
                        self._border_connected(border, dpid, vid, port_no)
        elif 'L2_EXPIRE' in msg:
            #TODO: handle expire event
            pass

    def _process_server_msg(self, msg):
        """Process message received from Route Controller."""
        self.logger.debug('Process msg from server: %s' % msg)
        msgs = []
        msg_type = msg['msg_type']
        msg = msg['msg']
        try:
            if type(msg) == str:
                msg = json.loads(msg)
            if msg_type == 'server_connected':
                #TODO: process server connected event
                self.logger.info('Connected to server: %s' % msg)
                self.register()
            elif msg_type == 'server_disconnected':
                #TODO: process server disconnected event
                self.logger.info('Disconnected from server: %s' % msg)
                self.deregister()
            elif msg_type == 'server_command':
                #TODO: process server commands
                self.logger.info('Receive msg from server: %s' % msg)
                command = msg.get('command')
                if command in ['add_mapping', 'del_mapping']:
                    routerid = ipaddress.ip_address(msg['routerid'])
                    prefix = ipaddress.ip_network(msg['prefix'])
                    nexthop = ipaddress.ip_address(msg['nexthop'])
                    egress = ipaddress.ip_address(msg['egress'])
                    pathid = int(msg['pathid'])
                    for_peer = msg['for_peer']
                    if for_peer:
                        peer = self.peers[routerid]
                        if command == 'add_mapping':
                            msgs = self._add_mapping(routerid, prefix, nexthop, egress, pathid)
                        elif command == 'del_mapping':
                            msgs = self._del_mapping(routerid, prefix, nexthop, egress, pathid)
                    else:
                        best_route = self.bgp.best_routes.get(prefix)
                        if best_route and best_route.nexthop == nexthop:
                            return
                        route = self._route_by_nexthop(prefix, nexthop)
                        if not route:
                            return
                        self.bgp.best_routes[prefix] = route
                        learned_peer = self.peers[route.learned_from_peer]
                        self._update_fib(route.prefix, route.nexthop, learned_peer.dp_id, learned_peer.vlan_vid)
                        for peer in self.peers.values():
                            if peer.peer_ip == route.learned_from_peer:
                                continue
                            msgs.extend(self.bgp.announce(peer, route))
                elif command == 'add_tunnel':
                    pass
            for msg in msgs:
                self._send_to_exabgp(msg)
        except Exception as e:
            self.logger.error('Error when handling %s: %s' % (msg, e))
