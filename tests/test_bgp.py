import unittest
import tempfile
import os
import ipaddress
import logging

from fbgp.fbgp import FlowBasedBGP
from fbgp.bgp import BgpRouter, BgpPeer
from fbgp.bgp import Route

CONFIG = """
---
peers:
- peer_ip: 10.0.1.1
  peer_as: 6510
- peer_ip: 10.0.2.2
  peer_as: 4122
"""

class TestBGP(unittest.TestCase):

    def setUp(self):
        peers = {}
        borders = {}
        for peerip, peeras in [('10.0.1.1', 6510), ('10.0.2.2', 4122), ('10.10.10.1', 65000)]:
            peerip = ipaddress.ip_address(peerip)
            peers[peerip] = BgpPeer(peeras, peerip, 65000)
            peers[peerip].bgp_session_up()
        self.bgp = BgpRouter(borders, peers, self.smoke_path_change_handler)
        self.peer = peers[ipaddress.ip_address('10.0.1.1')]

    def smoke_path_change_handler(self, peer, route, is_withdraw=False):
        pass

    def tearDown(self):
        pass

    def test_rcv_route(self):
        as_path = [1,2,3,4]
        for i, prefix in enumerate(['1.0.0.0/20', '120.0.0.0/20']):
            as_path = as_path[: len(as_path) - i]
            recv_route = self.peer.rcv_announce(prefix, '10.0.1.1', as_path=as_path)
            best_route, _ = self.bgp.add_route(recv_route)
            self.assertEqual(recv_route.as_path, best_route.as_path)

        recv_route2 = self.peer.rcv_announce('1.0.0.0/20', '10.0.2.2', as_path=[1,2,3,4])
        # this should make no best path change
        best_route, _ = self.bgp.add_route(recv_route2)
        self.assertTrue(best_route is None)
        self.assertEqual(len(self.bgp.best_routes), 2)
        self.assertEqual(len(self.bgp.loc_rib['1.0.0.0/20']), 2)
        self.assertEqual(len(self.bgp.loc_rib['120.0.0.0/20']), 1)

        route = self.peer.rcv_withdraw('1.0.0.0/20')

    def send_update(self, announce=None, withdraw=None, attribute=None):
        if announce is None:
            announce = {}
        if withdraw is None:
            withdraw = {}
        if attribute is None:
            attribute = {}
        return self.bgp.process_update(
                self.peerip, {'announce': announce, 'withdraw': withdraw, 'attribute': attribute})

    def send_announce(self, announce=None, attribute=None):
        if announce is None:
            announce = {'ipv4 unicast': {'10.0.1.1': [{'nlri': '1.0.0.0/20'}, {'nlri': '2.0.0.0/21'}]}}
        if attribute is None:
            attribute = {'local-pref': 100, 'as-path': [1,2,3]}
        return self.send_update(announce=announce, attribute=attribute)

    def send_withdraw(self, withdraw=None):
        if withdraw is None:
            withdraw = {'ipv4 unicast': [{'nlri': '1.0.0.0/20'}, {'nlri': '2.0.0.0/21'}]}
        return self.send_update(withdraw=withdraw)
