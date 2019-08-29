"""This module is an interface to the Route Controller. It provides APIs to send
network events to the route controller and to receive control commands.
"""
import eventlet
eventlet.monkey_patch()

import logging
import json
import time
import os

from twisted.internet import reactor, protocol
from twisted.internet.protocol import ReconnectingClientFactory
from twisted.protocols.basic import LineReceiver

logger = logging.getLogger('fbgp.server_connect')

class RouteServerProtocol(LineReceiver):

    delimiter = b'\n'

    def __init__(self, handler):
        self.handler = handler

    def connectionMade(self):
        self.handler({'msg_type': 'server_connected', 'msg': self.transport.getPeer()})

    def connectionLost(self, reason):
        self.handler({'msg_type': 'server_disconnected', 'msg': reason.getErrorMessage()})

    def lineReceived(self, raw):
        if type(raw) == bytes:
            raw = raw.decode('utf-8')
        self.handler({'msg_type': 'server_command', 'msg': raw})

    def send(self, msg):
        self.sendLine(msg)


class ServerConnect(ReconnectingClientFactory):

    def __init__(self, handler):
        self.proto = None
        self.running = False
        self.handler = handler
        self.server_addr = os.environ.get('FBGP_SERVER_ADDR') or 'localhost'
        self.server_port = int(os.environ.get('FBGP_SERVER_PORT') or 9999)

    def send(self, data):
        """Send data (string or dict) to the route server."""
        if not self.proto:
            return False
        if isinstance(data, dict):
            msg = json.dumps(data)
        else:
            msg = str(data)
        reactor.callFromThread(lambda: self.proto.send(msg.encode('utf-8'))) #pylint: disable=no-member
        return True

    def start(self):
        reactor.connectTCP(self.server_addr, self.server_port, self, timeout=10) #pylint: disable=no-member
        t = eventlet.spawn(reactor.run) #pylint: disable=no-member
        eventlet.sleep(0)
        return t

    def stop(self):
        reactor.stop() #pylint: disable=no-member

    def clientConntionFailed(self, connector, reason):
        logger.error('Failed to connect to gRCP server: %s' % reason)
        ReconnectingClientFactory.clientConnectionFailed(self, connector, reason)

    def clientConnectionLost(self, connector, reason):
        logger.error('Lost connection to gRCP server: %s' % reason)
        ReconnectingClientFactory.clientConnectionLost(self, connector, reason)


    def buildProtocol(self, addr):
        logger.info('Connected to gRCP server: %s' % addr)
        self.resetDelay()
        self.proto = RouteServerProtocol(self.handler)
        return self.proto
