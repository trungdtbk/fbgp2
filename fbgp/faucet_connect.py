import eventlet
eventlet.monkey_patch()

import os
import socket
import json
import logging
import traceback

class FaucetConnect():

    def __init__(self, handler, faucet_sock_path=None):
        self.logger = logging.getLogger('fbgp.faucet_connect')
        self.handler = handler
        self.socket = None
        self.sock_file = None
        self.running = False
        sock_path = faucet_sock_path or os.environ.get('FAUCET_EVENT_SOCK')
        if sock_path:
            self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.socket.connect(sock_path)
            self.sock_file = self.socket.makefile('rb')
            self.logger.info('connected to Faucet event')

    def start(self):
        self.logger.info('start faucet event listener')
        self.running = True
        return eventlet.spawn(self._faucet_event_loop)

    def stop(self):
        self.running = False

    def _faucet_event_loop(self):
        if self.socket:
            while self.running:
                try:
                    data = self.sock_file.readline()
                    if data:
                        data = data.decode('utf-8')
                        self._process_faucet_event(data.strip())
                    else:
                        break
                except:
                    traceback.print_exc()
                    break
            self.socket.close()

    def _process_faucet_event(self, event):
        self.logger.debug('received faucet event: %s' % event)
        try:
            event = json.loads(event)
            if event['version'] != 1:
                return
            if 'L2_LEARN' in event or 'L2_EXPIRE' in event:
                self.handler(event)
        except:
            traceback.print_exc()


