"""Start ExaBGP as subprocess, communicate with it via netcat to send route update
"""
import eventlet
eventlet.monkey_patch()

import subprocess
import shutil
import time
import traceback
import signal
import os
import sys
import socket, logging
import logging

from threading import Lock
from multiprocessing.connection import Listener

from fbgp.cfg import CONF


class ExaBgpConnect():
    config = """
process send_receive {
    run %s %s;
    encoder json;
}
    """
    peer_config = """
neighbor %s {
    passive;
    connect %s;
    peer-as %s;
    local-address %s;
    local-as %s;
    hold-time 180;
    router-id %s;
    api {
        processes [send_receive];
        neighbor-changes;
        receive {
            parsed;
            update;
        }
        send {
            parsed;
            update;
        }
    }
}
    """

    def __init__(self, handler, peers, routerid):
        self.logger = logging.getLogger('fbgp.exabgp_connect')
        self.handler = handler
        self.peers = peers
        self.routerid = routerid
        self.conn = None
        self.exabgp = None
        self.running = False
        self.recv_queue = eventlet.Queue(128)
        self.lock = Lock()

    def _clean(self):
        pass

    def _run(self):
        try:
            os.remove(self.sock_path)
        except:
            pass
        self.logger.info('starting ExaBGP listener...')
        with Listener(self.sock_path, 'AF_UNIX') as listener:
            self.conn = listener.accept()
            self.logger.info('exabgp_hook connected')
            while self.running:
                try:
                    data = self.conn.recv()
                    if data:
                        self.recv_queue.put(data)
                except:
                    break

    def _process_msg(self):
        while self.running:
            msg = self.recv_queue.get()
            self.lock.acquire()
            try:
                self.handler(msg)
            except Exception as e:
                self.logger.error('Error %s when handling %s' % (e, msg))
                pass
            self.lock.release()

    def start(self):
        self.logger.info('starting ExaBGP...')
        self.running = True
        self.exabgp_cfg_file = os.environ.get('FBGP_EXABGP_CONFIG', '/etc/fbgp/exabgp.conf')
        self.sock_path = os.environ.get('FBGP_EXABGP_SOCK', '/var/log/fbgp/exabgp_hook.sock')
        self.exabgp_hook_log = os.environ.get('FBGP_EXABGP_HOOK_LOG', '/var/log/fbgp/exabgp_hook.log')
        log_level = os.environ.get('FBGP_LOG_LEVEL', 'INFO').upper()
        eventlet.spawn(self._process_msg)
        eventlet.spawn(self._run)
        time.sleep(5) # wait for the listener to start
        self.logger.info('ExaBGP listener started')
        # locate exabgp_hook
        hook = subprocess.run(['which', 'fbgp_exabgp_hook'], stdout=subprocess.PIPE)
        hook_loc = hook.stdout.decode('utf-8').strip()
        with open(self.exabgp_cfg_file, 'w') as f:
            f.write(self.config % (hook_loc, self.sock_path))
            for peer in self.peers.values():
                peer_config = self.peer_config % (
                    peer.peer_ip, peer.peer_port, peer.peer_as,
                    peer.local_ip, peer.local_as, self.routerid)
                f.write(peer_config + '\n')
        self.exabgp = subprocess.Popen(
            ['env', 'exabgp.tcp.bind=' + '0.0.0.0', 'exabgp.tcp.port=' + '9179',
             'exabgp.daemon.daemonize=false', 'exabgp.daemon.user=root',
             'exabgp.log.level=' + log_level, 'exabgp.log.all=true',
             'exabgp.log.destination=' + self.exabgp_hook_log,
             'exabgp', self.exabgp_cfg_file],
             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.logger.info('started ExaBGP subprocess')
        try:
            (dout, derr) = self.exabgp.communicate(timeout=30)
            self.logger.info(derr)
        except subprocess.TimeoutExpired as e:
            self.logger.info('ExaBGP is running')
        except Exception as e:
            returncode = self.exabgp.poll()
            self.logger.error('ExaBGP failed to start, return code: %s, exec: %s' % (returncode, type(e)))
            return None
        return self.exabgp

    def stop(self):
        """stop Exabgp running in the subprocess."""
        self.running = False
        if self.exabgp:
            os.kill(self.exabgp.pid, signal.SIGTERM)
        self._clean()

    def send(self, msg):
        if self.conn:
            self.conn.send(msg)
            self.logger.debug('sent msg <%s> to ExaBGP' % msg)
