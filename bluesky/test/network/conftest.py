"""
Common test fixtures for the network-layer tests.
"""
import subprocess
import pytest

import bluesky.network.server as srvmod
from bluesky.network.server import Server


class FakeSocket:
    """ Stand-in for a zmq socket that records sent messages. """
    def __init__(self):
        self.sent = []

    def send_multipart(self, msg):
        self.sent.append(msg)


class FakeProcess:
    """ Stand-in for a subprocess.Popen node process. """
    def __init__(self, hang=False):
        self.pid = 99999
        self.terminated = False
        self.killed = False
        self.hang = hang

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        if self.hang and not self.killed:
            raise subprocess.TimeoutExpired(cmd='node', timeout=timeout)
        return 0


@pytest.fixture
def server(monkeypatch):
    """ A Server with its network sockets replaced by recording stubs,
        so tests never touch the network or spawn real processes.
    """
    srv = Server(discovery=False)
    srv.sock_recv.close()
    srv.sock_send.close()
    srv.sock_recv = FakeSocket()
    srv.sock_send = FakeSocket()
    # Node processes in these tests are fakes: always take the posix
    # signalling path, so no real signal is ever sent to a real pid.
    monkeypatch.setattr(srvmod.platform, 'system', lambda: 'Linux')
    yield srv
