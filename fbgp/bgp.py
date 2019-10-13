"""Implementation of BGP selection algorithm
"""
from fbgp.policy import Policy

import logging
import json
import ipaddress
import operator
import collections
import traceback

ORIGIN_IGP = 0
ORIGIN_EGP = 1
ORIGIN_INCOMPLETE = 2

class Route:
    """Represent a BGP route to a prefix."""

    def __init__(self, prefix, nexthop, as_path, origin, **others):
        self.prefix = prefix
        self.nexthop = nexthop
        self.as_path = as_path
        self.origin = origin
        self.local_pref = others.get('local_pref') or 100
        self.med = others.get('med') or 0
        self.community = others.get('community')

        self.local = others.get('local', False) # locally originated

        self.from_as = others.get('from_as')
        self.from_peer = others.get('from_peer')
        self.from_ibgp = others.get('from_ibgp', False)


    def to_exabgp(self, peer=None, is_withdraw=False, gw=None):
        line = ''
        if peer:
            line = 'neighbor %s' % peer.peer_ip
        if is_withdraw:
            line += ' withdraw route %s' % self.prefix
        else:
            gateway = gw or peer.faucet_vip.ip
            line += ' announce route %s next-hop %s as-path %s' % (
                self.prefix, gateway, self.as_path)
        for name, attr in [
                ('origin', 'origin'), ('med', 'med'), ('local_pref', 'local-preference'),
                ('community', 'community')]:
            if getattr(self, name) is not None:
                line += ' %s %s' % (attr, getattr(self, name))
        return line

    def copy(self):
        """return a copy of this route."""
        return self.__class__(self.prefix, self.nexthop, self.as_path, self.origin,
                              local_pref=self.local_pref, med=self.med,
                              community=self.community, local=self.local,
                              from_as=self.from_as, from_peer=self.from_peer,
                              from_ibgp=self.from_ibgp)

    def __hash__(self):
        return hash(frozenset([str(v) for v in self.__dict__.values()]))

    def __eq__(self, other):
        return hash(self) == hash(other)

    def __nq__(self, other):
        return not self.__eq__(other)

    def __str__(self):
        return "<Route %s->%s (local-pref=%s, as-path=%s, " \
                "med=%s, origin=%s, community=%s, from_peer=%s>" \
                "from_as=%s, from_ibgp=%s, local=%s>" % (
                    self.prefix, self.nexthop, self.local_pref,
                    self.as_path, self.med, self.origin, self.community,
                    self.from_peer, self.from_as, self.from_ibgp, self.local)
    __repr__ = __str__


class Border:
    """Represent other border routers."""

    def __init__(self, routerid, nexthop, dp_id=None, vlan_vid=None, port_no=None):
        """initialize a sdn-enabled border router."""
        self.routerid = routerid
        self.nexthop = nexthop
        self.dp_id = dp_id
        self.vlan_vid = vlan_vid
        self.port_no = port_no
        self.is_connected = False

    def connected(self, dp_id, vlan_vid, port_no):
        self.dp_id = dp_id
        self.vlan_vid = vlan_vid
        self.port_no = port_no
        self.is_connected = True

    def disconnected(self):
        self.is_connected = False

    def __str__(self):
        return 'Border(routerid=%s, nexthop=%s, dpid=%s, vid=%s, port_no=%s)' % (
            self.routerid, self.nexthop, self.dp_id, self.vlan_vid, self.port_no)


class BgpPeer:
    """Representation of a BGP peer. It also keeps info about the attachment point."""

    def __init__(self, peer_as, peer_ip, local_as=None, local_ip=None, peer_port=179,
                 dp_id=None, vlan_vid=None, port_no=None, vlan=None):
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
        self.vlan = vlan
        self.faucet_vip = None

        self._rib_in = {} #route received from peer
        self._rib_out = {} #route announced to peer
        self._candidate_routes = {} #possible routes for the peer
        self.state = 'down'
        self.is_connected = False
        self.ibgp = self.local_as == self.peer_as

    def is_ibgp(self):
        return self.ibgp

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
        self.is_connected = True

    def disconnected(self):
        """The peer is disconnected physically."""
        self.is_connected = False

    def rcv_withdraw(self, prefix):
        """Withdraw a route from this peer."""
        if prefix in self._rib_in:
            return self._rib_in.pop(prefix)
        return

    def rcv_announce(self, prefix, nexthop, as_path, origin, **others):
        """Process a route announced by this peer."""
        attributes = dict(
            from_as=self.peer_as,
            from_peer=self.peer_ip,
            from_ibgp=self.ibgp
        )
        attributes.update(others)
        route = Route(prefix, nexthop, as_path, origin, **attributes)
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
        # if the peer is internal, announce all external routes but no internal ones
        # if the peer is external, announce all routes if the peer not in the as path
        if route is None:
            return
        if ((self.ibgp and not route.from_ibgp) or
                (not self.ibgp and self.peer_as not in route.as_path[:1])):
            out = self.export_policy.evaluate(route.copy())
        else:
            out = None
        if out:
            if self.local_as != self.peer_as:
                out.as_path = [self.local_as] + out.as_path
                out.local_pref = None
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

    def __init__(self, borders, peers, path_change_handler):
        self.logger = logging.getLogger('fbgp.bgp')
        self.borders = borders
        self.peers = peers
        self.notify_path_change = path_change_handler
        self.best_routes = {}
        self.loc_rib = collections.defaultdict(set)

    def _select_best_route(self, routes):
        """Select the  best route based on BGP ranking algorithm."""
        def compare(route1, route2):
            if route1 is None:
                return route2
            if route2 is None:
                return route1

            for attr, op in [('local_pref', operator.gt),
                             ('local', operator.gt),
                             ('as_path', operator.lt),
                             ('origin', operator.lt),
                             ('med', operator.gt),
                             ('from_ibgp', operator.lt),
                             ('from_peer', operator.lt)]:
                if attr == 'med' and route1.from_as != route2.from_as:
                    # do not compare med from two different ases
                    continue
                val1 = getattr(route1, attr)
                val2 = getattr(route2, attr)
                if isinstance(val1, list) and isinstance(val2, list):
                    val1 = len(val1)
                    val2 = len(val2)
                if op(val1, val2):
                    return route1
                if op(val2, val1):
                    return route2

            if not route1.internal:
                return route1
            if not route2.internal:
                return route2
            return route1

        best_route = None
        for route in routes:
            best_route = compare(best_route, route)
        return best_route

    def del_route(self, route):
        prefix = route.prefix

        best_route = self.best_routes.get(prefix)
        routes = self.loc_rib[prefix]
        routes.discard(route)

        new_best = self._select_best_route(routes)
        if new_best:
            self.best_routes[prefix] = new_best
        else:
            del self.best_routes[prefix]

        if len(self.loc_rib[prefix]) == 0:
            del self.loc_rib[prefix]

        return new_best, best_route

    def add_route(self, route):
        prefix = route.prefix
        self.loc_rib[prefix].add(route)

        best_route = self.best_routes.get(prefix)
        new_best = self._select_best_route([best_route, route])

        if new_best and new_best != best_route:
            self.best_routes[prefix] = new_best
        else:
            new_best = None

        return new_best, best_route

    @staticmethod
    def announce(peer, route, gateway=None):
        msgs = []
        route = peer.announce(route)
        if route:
            msgs.append(route.to_exabgp(peer, gw=gateway))
        return msgs

    @staticmethod
    def withdraw(peer, route):
        msgs = []
        route = peer.withdraw(route)
        if route:
            msgs.append(route.to_exabgp(peer, is_withdraw=True))
        return msgs
