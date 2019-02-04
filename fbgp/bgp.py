"""Implementation of BGP selection algorithm
"""
import ipaddress
import operator
import collections
import traceback

from .policy import Policy

class Route(object):
    """Represent a BGP route to a prefix."""

    def __init__(self, prefix, nexthop, **attributes):
        self.prefix = prefix
        self.nexthop = nexthop
        self.local_pref = attributes.get('local-pref', 100)
        self.as_path = attributes.get('as-path', [])
        self.med = attributes.get('med', 0)
        self.origin = attributes.get('origin', 'incomplete')
        self.community = attributes.get('community')

    def to_exabgp(self, peer_ip=None, is_withdraw=False):
        line = ''
        if peer_ip:
            line = 'neighbour %s' % peer_ip
        if is_withdraw:
            line += ' withdraw route %s' % self.prefix
        else:
            line += ' announce route %s next-hop %s as-path %s' % (
                    self.prefix, self.nexthop, self.as_path)
        return line

    def copy(self):
        """return a copy of this route."""
        return self.__class__(self.prefix, self.nexthop, local_pref=self.local_pref,
                              as_path=self.as_path, med=self.med, origin=self.origin,
                              community=self.community)

    def __hash__(self):
        return hash(frozenset([str(v) for v in self.__dict__.values()]))

    def __eq__(self, other):
        return hash(self) == hash(other)

    def __nq__(self, other):
        return not self.__eq__(other)

    def __str__(self):
        return "<Route %s->%s (local-pref=%s, as-path=%s, " \
                "med=%s, origin=%s, community=%s>" % (
                        self.prefix, self.nexthop, self.local_pref,
                        self.as_path, self.med, self.origin, self.community)
    __repr__ = __str__


class BgpPeer(object):
    """Representation of a BGP peer. It also keeps info about the attachment point."""

    def __init__(self, peer_as, peer_ip, local_as=None, local_ip=None, peer_port=179,
                 dp_id=None, vlan_vid=None, port_no=None):
        self.import_policy = Policy.default() # default accept everything
        self.export_policy = Policy.default() # default accept everything

        self.peer_ip = peer_ip
        self.peer_as = peer_as
        self.local_as = local_as
        self.local_ip = local_ip
        self.peer_port = peer_port

        self.dp_id = dp_id
        self.vlan_vid = vlan_vid
        self.port_no = port_no

        self._rib_in = {} #route received from peer
        self._rib_out = {} #route announced to peer
        self._candidate_routes = {} #possible routes for the peer
        self.state = 'down'

    def bgp_session_up(self):
        """BGP session with the peer is up."""
        self._rib_in = {}
        self._rib_out = {}
        self.state = 'up'

    def bgp_session_down(self):
        """BGP session with the peer is down."""
        self.state = 'down'
        self._rib_in = {}
        self._rib_out = {}

    def connected(self, dp_id, vlan_vid, port_no):
        """The peer is connected to our dataplane."""
        self.dp_id = dp_id
        self.vlan_vid = vlan_vid
        self.port_no = port_no

    def rcv_withdraw(self, prefix):
        """Withdraw a route from this peer."""
        if prefix in self._rib_in:
            return self._rib_in.pop(prefix)
        return

    def rcv_announce(self, prefix, nexthop, **attributes):
        """Process a route announced by this peer."""
        route = Route(prefix, nexthop, **attributes)
        if prefix in self._rib_in and self._rib_in[prefix] == route:
            return
        self._rib_in[prefix] = route
        return self.import_policy.evaluate(route)

    def withdraw(self, route):
        """Withdraw a route previously announced to this peer."""
        if route is None:
            return
        if route.prefix in self._rib_out:
            return self._rib_out.pop(route.prefix)

    def announce(self, route):
        """Announce a route to this peer."""
        if route is None:
            return
        out = self.export_policy.evaluate(route.copy())
        if out:
            out.as_path = [self.local_as] + out.as_path
            self._rib_out[out.prefix] = out
        return out

    def routes(self):
        return self._rib_in.values()

    def __hash__(self):
        return hash((self.peer_as, self.peer_ip, self.local_as, self.local_ip))

    def __eq__(self, other):
        return hash(self) == hash(other)

    def __nq__(self, other):
        return not self == other

    def __str__(self):
        return "BgpPeer(peer_as:%s, peer_ip:%s, " \
                "local_as:%s, local_ip:%s, import: %s, export: %s)" % (
                    self.peer_as, self.peer_ip,
                    self.local_as, self.local_ip,
                    self.import_policy, self.export_policy)
    __repr__ = __str__


class BgpRouter():
    """BGP selection algorithm."""

    def __init__(self, logger, peers):
        self.logger = logger
        self.peers = peers
        self.best_routes = {}
        self.loc_rib = collections.defaultdict(set)

    def _select_best_route(self, routes):
        """Select the  best route based on BGP ranking algorithm."""
        def compare(route1, route2):
            if route1 is None:
                return route2
            if route2 is None:
                return route1

            for attr, op in [('local_pref', operator.gt) , ('as_path', operator.lt),
                             ('med', operator.gt)]:
                val1 = getattr(route1, attr)
                val2 = getattr(route2, attr)
                self.logger.info(val1, val2)
                if op(val1, val2):
                    return route1
                elif op(val2, val1):
                    return route2

            if route1 is not None:
                return route1
            else:
                return route2

        best_route = None
        for route in routes:
            best_route = compare(best_route, route)
        return best_route

    def _del_route(self, route):
        if not route:
            return
        routes = self.loc_rib[route.prefix]
        routes.discard(route)
        best_route = self.best_routes.get(route.prefix, None)
        if route == best_route:
            if len(routes) == 0:
                del self.best_routes[route.prefix]
                return best_route
            else:
                new_best = self._select_best_route(routes)
                if new_best:
                    self.best_routes[new_best.prefix] = new_best
                    return new_best
        return None

    def _add_route(self, new_route):
        if new_route is None:
            return
        prefix = new_route.prefix
        self.loc_rib[prefix].discard(new_route)
        self.loc_rib[prefix].add(new_route)
        best_route = self.best_routes.get(prefix)
        if new_route == best_route: # imply a withdrawal of the current best
            new_best_route = self._select_best_route(self.loc_rib[prefix])
        else:
            new_best_route = self._select_best_route([best_route, new_route])
        if new_best_route:
            self.best_routes[prefix] = new_best_route
            return new_best_route
        return

    def peer_up(self, peer_ip):
        msgs = []
        if peer_ip not in self.peers or self.peers[peer_ip].state == 'up':
            return msgs
        peer = self.peers[peer_ip]
        peer.bgp_session_up()
        for route in self.best_routes.values():
            msgs.extend(self._announce(peer, route))
        return msgs

    def peer_down(self, peer_ip):
        msgs = []
        if peer_ip not in self.peers or self.peers[peer_ip].state == 'down':
            return msgs
        peer = self.peers[peer_ip]
        for route in peer.routes():
            new_best = self._del_route(route)
            if new_best:
                for other_peer in self._other_peers(peer):
                    msgs.extend(self._announce(other_peer, new_best))
        peer.bgp_session_down()
        return msgs

    def _other_peers(self, peer):
        return [other_peer for other_peer in self.peers.values() if other_peer != peer]

    def _announce(self, peer, route):
        msgs = []
        route = peer.announce(route)
        if route:
            msgs.append(route.to_exabgp(peer.peer_ip))
        return msgs

    def _withdraw(self, peer, route):
        msgs = []
        route = peer.withdraw(route)
        if route:
            msgs.extend(route.to_exabgp())
        return msgs

    def process_update(self, peer_ip, update):
        """Process a BGP update received from ExaBGP."""
        try:
            msgs = []
            if peer_ip not in self.peers:
                return []
            peer = self.peers[peer_ip]
            if 'announce' in update and 'ipv4 unicast' in update['announce']:
                attributes = update['attribute']
                for nexthop, nlris in update['announce']['ipv4 unicast'].items():
                    nexthop = ipaddress.ip_address(nexthop)
                    for prefix in nlris:
                        prefix = ipaddress.ip_network(prefix['nlri'])
                        route = peer.rcv_announce(prefix, nexthop, **attributes)
                        new_best = self._add_route(route)
                        if new_best is None:
                            continue
                        self.logger.debug('best path changed: %s' % new_best)
                        for other_peer in self._other_peers(peer):
                            msgs.extend(self._announce(other_peer, new_best))

            if 'withdraw' in update and 'ipv4 unicast' in update['withdraw']:
                for prefix in update['withdraw']['ipv4 unicast']:
                    prefix = ipaddress.ip_network(prefix['nlri'])
                    route = peer.rcv_withdraw(prefix)
                    new_best = self._del_route(route)
                    if new_best:
                        self.logger.debug('best path changed: %s' % new_best)
                    for other_peer in self._other_peers(peer):
                        if new_best:
                            msgs.extend(self._announce(other_peer, new_best))
                        else:
                            msgs.extend(self._withdraw(other_peer, route))
            return msgs
        except Exception as e:
            print(e)
            traceback.print_exc()
        return []
