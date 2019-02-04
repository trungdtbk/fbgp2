import unittest
import os

from fbgp.fbgp import FlowBasedBGP

class TestFlowBasedBGP(unittest.TestCase):

    def setUp(self):
        self.config = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'sample-config.yaml')
        os.environ['FBGP_CONFIG'] = self.config
        self.fbgp = FlowBasedBGP()

    def test_load_config(self):
        self.fbgp.initialize()
