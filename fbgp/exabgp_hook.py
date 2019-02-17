#!/usr/bin/env python
import socket, sys, os, traceback
import time
import eventlet
eventlet.monkey_patch()

from multiprocessing.connection import Client

from sys import stdin, stdout

class ExabgpHook():

    def __init__(self, sock_path):
        self.conn = None
        self.sock_path = sock_path
        self.running = False

    def run_forever(self):
        self.conn = Client(self.sock_path, 'AF_UNIX')
        self.running = True
        eventlet.spawn(self.recv_from_fbgp_loop)
        self.recv_from_exabgp_loop()

    def recv_from_exabgp_loop(self):
        while self.running:
            line = stdin.readline().strip()
            self.conn.send(line)
            # have no idea why the below line is important,
            # without it no data is received from conn
            time.sleep(0)

    def recv_from_fbgp_loop(self):
        while self.running:
            try:
                data = self.conn.recv()
                stdout.write(data + '\n')
                stdout.flush()
            except:
                self.conn.close()
                break

def main():
    exabgp = ExabgpHook(sys.argv[1])
    exabgp.run_forever()

if __name__ == '__main__':
    main()
