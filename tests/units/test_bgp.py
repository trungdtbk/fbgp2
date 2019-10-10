import unittest
import tempfile
import os
import ipaddress
import logging

from unittest.mock import Mock

from fbgp.bgp import BgpRouter, BgpPeer
from fbgp.bgp import Route



class TestBGP(unittest.TestCase):

    def setUp(self):
        self.external_peers = []
        self.internal_peers = []

        borders = {}
        peers = {}
        for peerip, peeras in [('10.0.0.1', 1), ('10.0.0.2', 2), ('10.0.0.3', 3)]:
            peerip = ipaddress.ip_address(peerip)
            peers[peerip] = BgpPeer(peeras, peerip, 65000)
            peers[peerip].bgp_session_up()
            self.external_peers.append(peers[peerip])

        for peerip, peeras in [('10.0.10.1', 65000), ('10.0.10.2', 65000)]:
            peerip = ipaddress.ip_address(peerip)
            peers[peerip] = BgpPeer(peeras, peerip, 65000)
            peers[peerip].bgp_session_up()
            self.internal_peers.append(peers[peerip])

        self.mock_path_change_handler = Mock()
        self.bgp = BgpRouter(borders, peers, self.mock_path_change_handler)

        self.prefix = ipaddress.ip_network('1.0.0.0/24')
        self.as_path = [1, 2, 3]

    def tearDown(self):
        pass

    def test_add_route(self):
        as_path = list(self.as_path)
        for peer in self.external_peers:
            route = peer.rcv_announce(self.prefix, peer.peer_ip, list(as_path), origin=2)
            new_best, cur_best = self.bgp.add_route(route)
            self.assertTrue(new_best)
            self.assertEqual(new_best.nexthop, peer.peer_ip)
            self.assertEqual(new_best.as_path, as_path)
            self.assertEqual(new_best.from_as, peer.peer_as)
            as_path.pop(0)

        self.assertEqual(len(self.bgp.loc_rib), 1)
        self.assertEqual(len(self.bgp.loc_rib[self.prefix]), 3)

    def test_del_route(self):
        as_path = list(self.as_path)
        for peer in self.external_peers:
            route = peer.rcv_announce(self.prefix, peer.peer_ip, list(as_path), origin=2)
            new_best, cur_best = self.bgp.add_route(route)
            as_path.pop(0)

        for idx, peer in enumerate(self.external_peers[1:][::-1]):
            route = peer.rcv_withdraw(self.prefix)
            new_best, cur_best = self.bgp.del_route(route)
            self.assertTrue(len(peer._rib_in) == 0)
            self.assertTrue(new_best)
            next_peer = self.external_peers[len(self.external_peers) - idx - 2]
            self.assertEqual(new_best.nexthop, next_peer.peer_ip)
            self.assertEqual(new_best.from_as, next_peer.peer_as)

        route = self.external_peers[0].rcv_withdraw(self.prefix)
        new_best, cur_best = self.bgp.del_route(route)
        self.assertFalse(new_best)

        self.assertEqual(len(self.bgp.loc_rib), 0)

    def test_del_route_no_exist(self):
        route = self.external_peers[0].rcv_withdraw(self.prefix)
        self.assertFalse(route)

    def test_duplicate_announce(self):
        peer = self.external_peers[0]
        route = peer.rcv_announce(self.prefix, peer.peer_ip, [1], origin=2)
        new_best, cur_best = self.bgp.add_route(route)
        self.assertTrue(new_best)
        self.assertFalse(cur_best)

        route = peer.rcv_announce(self.prefix, peer.peer_ip, [1], origin=2)
        self.assertTrue(route is None)

    def test_bgp_best_path_no_change(self):
        peer = self.external_peers[0]
        route = peer.rcv_announce(self.prefix, peer.peer_ip, [1], 1)
        new_best, cur_best = self.bgp.add_route(route)
        self.assertTrue(new_best)
        self.assertFalse(cur_best)

        peer = self.external_peers[1]
        route = peer.rcv_announce(self.prefix, peer.peer_ip, [2,1], 1)
        new_best, cur_best = self.bgp.add_route(route)
        self.assertFalse(new_best)
        self.assertTrue(cur_best)

    def test_bgp_best_path_local_pref(self):
        pref = 100
        for peer in self.internal_peers:
            route = peer.rcv_announce(self.prefix, peer.peer_ip, [6500, 1], 1, local_pref=pref)
            new_best, cur_best = self.bgp.add_route(route)
            self.assertTrue(new_best)
            self.assertTrue(new_best.local_pref == pref)
            pref += 1
