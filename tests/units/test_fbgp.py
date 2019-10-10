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
- peer_ip: 10.0.10.2
  peer_as: 2
  local_as: 65000
- peer_ip: 10.0.30.2
  peer_as: 65000
  local_as: 65000

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
                native_vlan: vlan10
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

    def start_controller(self):
        self.proc = subprocess.Popen(['ryu-manager', 'faucet.faucet', 'fbgp.fbgp'], stderr=subprocess.PIPE)
        self.success = True
        try:
            (stdout, stderr) = self.proc.communicate(timeout=5)
            self.success = False
        except:
            self.assertFalse(self.proc.poll())

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

        with patch('fbgp.fbgp.ExaBgpConnect', MockExaBgpConnect()) as exabgp_connect, \
                patch('fbgp.fbgp.FaucetConnect', MockFaucetConnect()) as faucet_connect, \
                patch('fbgp.fbgp.ServerConnect', MockServerConnect()) as server_connect:

            cls.fbgp.initialize()

    @classmethod
    def tearDownClass(cls):
        cls.faucet.close()
        if cls.tempdir:
            shutil.rmtree(cls.tempdir)

    def setUp(self):
        for peer in self.fbgp.peers.values():
            peer.bgp_session_up()

    def tearDown(self):
        pass

    def create_fbgp(self):
        faucet_api = FaucetExperimentalAPI()
        faucet = Faucet(faucet_experimental_api=faucet_api, dpset=None)
        faucet.reload_config(None)
        faucet_api._register(faucet)
        fbgp = FlowBasedBGP(faucet_experimental_api=faucet_api)
        fbgp._load_config()
        return fbgp

    def verify_exabgp_msg_processing(self, fbgp, msg):
        fbgp._process_exabgp_msg(msg)

    def get_exabgp_msg(self, announce=True):
        msg = """
        {
            'neighbor': {
                'address': {'local': '10.0.10.253', 'peer': '10.0.10.1' },
                'asn': {'local': 65000, 'peer': 1 },
                'message': {
                    'update': {
                        'announce': {
                            'ipv4 unicast' : {'10.0.10.1': [{'nlri': '1.0.0.0/24'}, {'nlri': '2.0.0.0/24'}]}
                        },
                        'attribute': { 'as-path': [1, 2, 3] }
                    }
                }
            },
            'type': 'update', 'direction': 'receive'
        }
        """

    def reset_mocker(self):
        self.fbgp.exabgp_connect.reset_mock()
        self.fbgp.faucet_connect.reset_mock()
        self.fbgp.server_connect.reset_mock()

    def test_rcv_update_from_exabgp(self):
        msg = """
        {
            "neighbor": {
                "address": {"local": "10.0.10.253", "peer": "10.0.10.1" },
                "asn": {"local": 65000, "peer": 1 },
                "message": {
                    "update": {
                        "announce": {
                            "ipv4 unicast" : {"10.0.10.1": [{"nlri": "1.0.0.0/24"}]}
                        },
                        "attribute": { "as-path": [1, 2, 3] }
                    }
                }
            },
            "type": "update", "direction": "receive"
        }
        """
        self.fbgp._rcv_exabgp_msg(msg)
        time.sleep(1)
        self.assertEqual(len(self.fbgp.bgp.loc_rib), 1)
        for peer in self.fbgp.peers.values():
            if str(peer.peer_ip) == '10.0.10.1':
                len_rib_in = 1
                len_rib_out = 0
            else:
                len_rib_in = 0
                len_rib_out = 1

            self.assertEqual(len(peer._rib_in), len_rib_in)
            self.assertEqual(len(peer._rib_out), len_rib_out)

        self.assertTrue(self.fbgp.exabgp_connect.send.call_count==(len(self.fbgp.peers) -1))
        #self.assertTrue(self.faucet_api_add_route.call_count==1, self.faucet_api_add_route.call_count)
        self.reset_mocker()
