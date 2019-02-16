import unittest
import tempfile
import os
import ipaddress
import logging

from fbgp.fbgp import FlowBasedBGP
from fbgp.bgp import BgpRouter, BgpPeer

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
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
            f.write(CONFIG)
            os.environ['FBGP_CONFIG'] = f.name
        peers = {}
        for peerip, peeras in [('10.0.1.1', 6510), ('10.0.2.2', 4122)]:
            peerip = ipaddress.ip_address(peerip)
            peers[peerip] = BgpPeer(peeras, peerip)
        borders = {}
        self.bgp = BgpRouter(logging.getLogger(), borders, peers, self.smoke_path_change_handler)
        self.bgp.logger.setLevel('DEBUG')
        for peerip in self.bgp.peers:
            self.bgp.peer_up(peerip)
        self.peerip = ipaddress.ip_address('10.0.1.1')

    def smoke_path_change_handler(self, peer, route, is_withdraw=False):
        pass

    def tearDown(self):
        pass

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
            attribute = {'local-pref': 100, 'as_path': [1,2,3]}
        return self.send_update(announce=announce, attribute=attribute)

    def send_withdraw(self, withdraw=None):
        if withdraw is None:
            withdraw = {'ipv4 unicast': [{'nlri': '1.0.0.0/20'}, {'nlri': '2.0.0.0/21'}]}
        return self.send_update(withdraw=withdraw)

    def test_process_malformed_update(self):
        self.assertEqual(len(self.send_update()), 0)

    def test_process_duplicate(self):
        # duplicate update should have no effect
        self.send_announce()
        self.assertEqual(len(self.send_announce()), 0)

    def test_process_update(self):
        for func in ['send_announce', 'send_withdraw']:
            msgs = getattr(self, func)()
            self.assertGreater(len(msgs), 0, 'No output messages seen')

    def test_bgp_selection(self):
        attribute1 = {'local-pref': 100}
        attribute2 = {'local-pref': 110}
        for attr in [attribute1, attribute2]:
            msgs = self.send_announce(attribute=attr)
            self.assertGreater(len(msgs), 0)
        best_route = self.bgp.best_routes[ipaddress.ip_network('1.0.0.0/20')]
        self.assertTrue(best_route.local_pref == 110)

    def test_peer_state_change(self):
        self.send_announce()
        self.assertGreater(len(self.bgp.peer_down(self.peerip)), 0)

