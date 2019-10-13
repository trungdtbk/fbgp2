import unittest
import os
import time
import shutil
import tempfile
import subprocess
import ipaddress

from unittest.mock import Mock
from unittest.mock import patch
from subprocess import TimeoutExpired

from prometheus_client import REGISTRY as reg

from faucet.faucet import Faucet
from faucet.faucet_experimental_api import FaucetExperimentalAPI

from fbgp.fbgp import FlowBasedBGP


class MockFaucetApi(Mock):
    pass


class MockExaBgpConnect(Mock):
    pass


class MockServerConnect(Mock):
    pass


class MockFaucetConnect(Mock):
    pass



class TestFlowBasedBGP(unittest.TestCase):

    FBGP_CONFIG = """
---
routerid: 10.1.1.1

peers:
- peer_ip: 10.0.10.1
  peer_as: 1
  local_as: 65000
- peer_ip: 10.0.20.2
  peer_as: 2
  local_as: 65000
- peer_ip: 10.0.30.1
  peer_as: 2
  local_as: 65000
- peer_ip: 10.0.100.253
  peer_as: 65000
  local_as: 65000
  local_ip: 10.0.100.1

borders:
- routerid: 10.2.2.2
  nexthop: 10.0.30.2
- routerid: 10.3.3.3
  nexthop: 10.0.30.3
"""

    FAUCET_CONFIG = """
vlans:
    vlan10:
        vid: 10
        faucet_vips: ['10.0.10.254/24']
    vlan20:
        vid: 20
        faucet_vips: ['10.0.20.254/24']
    vlan30:
        vid: 30
        faucet_vips: ['10.0.30.254/24']
    vlan100:
        vid: 100
        faucet_vips: ['10.0.100.254/24']
dps:
    s1:
        dp_id: 1
        hardware: 'Open vSwitch'
        interfaces:
            1:
                tagged_vlans: [vlan10, vlan20, vlan30]
            2:
                native_vlan: vlan10
            3:
                native_vlan: vlan100
    s2:
        dp_id: 2
        hardware: 'Open vSwitch'
        interfaces:
            1:
                tagged_vlans: [vlan10, vlan20, vlan30]
            2:
                native_vlan: vlan20
            3:
                native_vlan: vlan20
"""

    faucet = None
    faucet_api = None
    fbgp = None
    local_ip = '10.0.0.253'
    local_as = 65000


    @classmethod
    def setUpClass(cls):
        tempdir = tempfile.mkdtemp()
        with open(os.path.join(tempdir, 'faucet.yaml'), 'w') as f:
            f.write(cls.FAUCET_CONFIG)
            os.environ['FAUCET_CONFIG'] = f.name
        with open(os.path.join(tempdir, 'fbgp.yaml'), 'w') as f:
            f.write(cls.FBGP_CONFIG)
            os.environ['FBGP_CONFIG'] = f.name
        os.environ['FAUCET_LOG'] = os.path.join(tempdir, 'faucet.log')
        os.environ['FAUCET_EXCEPTION_LOG'] = os.path.join(tempdir, 'faucet_exception.log')
        os.environ['FBGP_LOG'] = os.path.join(tempdir, 'fbgp.log')
        os.environ['FBGP_LOG_LEVEL'] = 'DEBUG'
        os.environ['FAUCET_EVENT_SOCK'] = os.path.join(tempdir, 'faucet.sock')
        cls.tempdir = tempdir

        cls.faucet_api = FaucetExperimentalAPI()
        cls.faucet_api_add_route = Mock(wraps=cls.faucet_api)
        cls.faucet = Faucet(dpset=Mock(), faucet_experimental_api=cls.faucet_api)
        cls.faucet.start()

        cls.fbgp = FlowBasedBGP(faucet_experimental_api=cls.faucet_api)

    @classmethod
    def tearDownClass(cls):
        cls.faucet.close()
        if cls.tempdir:
            shutil.rmtree(cls.tempdir)

    def setUp(self):
        with patch('fbgp.fbgp.ExaBgpConnect', MockExaBgpConnect()) as exabgp_connect, \
                patch('fbgp.fbgp.FaucetConnect', MockFaucetConnect()) as faucet_connect, \
                patch('fbgp.fbgp.ServerConnect', MockServerConnect()) as server_connect:

            self.fbgp.initialize()

        self.peers = list(self.fbgp.peers.values())
        for peer in self.peers:
            self.peer_up(peer)

    def tearDown(self):
        self.reset_mocker()

    def generate_peer_state_msg(self, peer_ip, peer_as, state):
        msg = """{ "exabgp": "4.0.1", "time": %s, "type": "state",
            "neighbor": {
                "address": { "local": "%s", "peer": "%s" },
                "asn": { "local": %s, "peer": %s },
            "state": "%s" } }
        """
        return msg % (time.time(), self.local_ip, peer_ip, self.local_as, peer_as, state)

    def generate_update_msg(self, peer_ip, peer_as, prefix, announce=True, **kwargs):
        msg = """{ "exabgp": "4.0.1", "time": %s, "type": "update",
                   "neighbor": {
                        "address": { "local": "%s", "peer": "%s" },
                        "asn": { "local": %s, "peer": %s } , "direction": "receive",
                        "message": {
                            "update": { "attribute": { "origin": "%s", "as-path": %s,
                            "confederation-path": [], "med": %s },
                            "announce": { %s },
                            "withdraw": { %s } }}
                    }
                 }
              """
        peer_ip = str(peer_ip)
        if announce:
            announce = '"ipv4 unicast": { "%s": [ { "nlri": "%s" } ]}' % (peer_ip, prefix)
            withdraw = ''
        else:
            announce = ''
            withdraw = '"ipv4 unicast": [{"nlri": "%s"}]' % prefix
        as_path = kwargs.get('as_path') or [peer_as]
        origin = kwargs.get('origin') or 'igp'
        med = kwargs.get('med') or 0
        return msg % (time.time(), self.local_ip, peer_ip, self.local_as, peer_as,
                      origin, as_path, med, announce, withdraw)

    def reset_mocker(self):
        self.fbgp.exabgp_connect.reset_mock()
        self.fbgp.faucet_connect.reset_mock()
        self.fbgp.server_connect.reset_mock()

    def peer_up(self, peer):
        msg = self.generate_peer_state_msg(peer.peer_ip, peer.peer_as, 'up')
        self.fbgp._process_exabgp_msg(msg)
        self.assertTrue(peer.state=='up')

    def peer_down(self, peer):
        msg = self.generate_peer_state_msg(peer.peer_ip, peer.peer_as, 'down')
        self.fbgp._process_exabgp_msg(msg)
        self.assertTrue(peer.state=='down')
        self.assertTrue(len(peer._rib_in) == 0 and len(peer._rib_out) == 0)

    def peer_announce(self, peer, prefix, **kwargs):
        msg = self.generate_update_msg(peer.peer_ip, peer.peer_as, prefix, True, **kwargs)
        self.fbgp._process_exabgp_msg(msg)

    def peer_withdraw(self, peer, prefix):
        msg = self.generate_update_msg(peer.peer_ip, peer.peer_as, prefix, False)
        self.fbgp._process_exabgp_msg(msg)

    def verify_route_attributes(self, route, **kwargs):
        for attr, value in kwargs.items():
            self.assertTrue(getattr(route, attr) == value)

    def verify_prefix_in_loc_rib(self, prefix, **kwargs):
        prefix = ipaddress.ip_network(prefix)
        self.assertTrue(prefix in self.fbgp.bgp.loc_rib)

    def verify_prefix_in_rib_out(self, peer, prefix, **kwargs):
        prefix = ipaddress.ip_network(prefix)
        self.assertTrue(prefix in peer._rib_out)
        self.verify_route_attributes(peer._rib_out[prefix], **kwargs)

    def verify_best_route(self, prefix, **kwargs):
        prefix = ipaddress.ip_network(prefix)
        self.assertTrue(prefix in self.fbgp.bgp.best_routes)
        self.verify_route_attributes(self.fbgp.bgp.best_routes[prefix], **kwargs)

    def announce_and_verify(self, prefix='1.0.0.0/24', **kwargs):
        first_peer = self.peers[0]
        self.peer_announce(first_peer, prefix, **kwargs)
        self.verify_prefix_in_loc_rib(prefix, **kwargs)
        self.verify_best_route(prefix)
        for peer in self.peers[1:]:
            self.assertEqual(len(peer._rib_in), 0)
            self.verify_prefix_in_rib_out(peer, prefix)
        self.assertTrue(self.fbgp.exabgp_connect.send.call_count==(len(self.fbgp.peers) -1),
                        self.fbgp.exabgp_connect.send.call_count)

    def withdraw_and_verify(self, prefix='1.0.0.0/24'):
        first_peer = self.peers[0]
        self.peer_withdraw(first_peer, prefix)
        self.assertFalse(prefix in self.fbgp.bgp.loc_rib)
        self.assertFalse(prefix in self.fbgp.bgp.best_routes)
        for peer in self.peers:
            self.assertEqual(len(peer._rib_in), 0)
            self.assertFalse(prefix in peer._rib_out)
        self.assertTrue(self.fbgp.exabgp_connect.send.call_count==(len(self.fbgp.peers) -1),
                        self.fbgp.exabgp_connect.send.call_count)

    def test_rcv_exabgp_update(self):
        self.announce_and_verify()
        for test in [self.withdraw_and_verify, self.announce_and_verify]:
            self.reset_mocker()
            test()

    def test_peer_go_down(self):
        """Test when the peer gives us the best route goes down."""
        self.announce_and_verify()
        first_peer = self.peers[0]
        self.peer_down(first_peer)
        for peer in self.peers[1:]:
            self.assertEqual(len(peer._rib_in), 0)
            self.assertEqual(len(peer._rib_out), 0)

    def test_peer_go_up(self):
        """Test when a peer goes down and up again."""
        self.announce_and_verify()
        peer = self.peers[1]
        for func, l in [('peer_down', 0), ('peer_up', 1)]:
            getattr(self, func)(peer)
            self.assertEqual(len(peer._rib_in), 0)
            self.assertEqual(len(peer._rib_out), l)

    def test_recv_inferior_update_msg(self):
        """Test receiving an inferior route."""
        self.announce_and_verify()
        peer = self.peers[1]
        self.peer_announce(peer, '1.0.0.0/24', as_path=[2,2])
        self.verify_best_route('1.0.0.0/24', as_path=[1])
        self.verify_prefix_in_rib_out(self.peers[2], '1.0.0.0/24')

    def test_recv_superior_update_msg(self):
        """Test receiving a superior route."""
        prefix = '1.0.0.0/24'
        self.announce_and_verify(prefix, as_path=[1,1,1])
        self.peer_announce(self.peers[1], prefix, as_path=[2])
        self.verify_best_route(prefix, as_path=[2])
        for peer in self.peers[:1] + self.peers[3:]:
            if peer.is_ibgp():
                as_path = [2]
            else:
                as_path = [65000, 2]
            self.verify_prefix_in_rib_out(peer, prefix, as_path=as_path)
