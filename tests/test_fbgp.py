import unittest
import os
import shutil
import tempfile
import subprocess
from subprocess import TimeoutExpired

from fbgp.fbgp import FlowBasedBGP

class TestFlowBasedBGP(unittest.TestCase):

    FBGP_CONFIG = """
---
routerid: 10.1.1.1

peers:
- peer_ip: 10.0.10.1
  peer_as: 6510
- peer_ip: 10.0.10.2
  peer_as: 4122
- peer_ip: 10.0.30.2
  peer_as: 65000

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

    def start_controller(self):
        self.proc = subprocess.Popen(['ryu-manager', 'faucet.faucet', 'fbgp.fbgp'], stderr=subprocess.PIPE)
        self.success = True
        try:
            (stdout, stderr) = self.proc.communicate(timeout=5)
            self.success = False
        except:
            self.assertFalse(self.proc.poll())

    def setUp(self):
        self.success = False
        self.proc = None
        self.tempdir = tempfile.mkdtemp()
        with open(os.path.join(self.tempdir, 'faucet.yaml'), 'w') as f:
            f.write(self.FAUCET_CONFIG)
            os.environ['FAUCET_CONFIG'] = f.name
        with open(os.path.join(self.tempdir, 'fbgp.yaml'), 'w') as f:
            f.write(self.FBGP_CONFIG)
            os.environ['FBGP_CONFIG'] = f.name
        os.environ['FAUCET_LOG'] = os.path.join(self.tempdir, 'faucet.log')
        os.environ['FAUCET_EXCEPTION_LOG'] = os.path.join(self.tempdir, 'faucet_exception.log')
        os.environ['FBGP_LOG'] = os.path.join(self.tempdir, 'fbgp.log')
        os.environ['FAUCET_EVENT_SOCK'] = os.path.join(self.tempdir, 'faucet.sock')

    def tearDown(self):
        if self.success and self.tempdir:
            shutil.rmtree(self.tempdir)
        if self.proc:
            self.proc.kill()

    def test_exec(self):
        self.start_controller()
        self.assertTrue(self.success)
