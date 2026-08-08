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

    def setsockopt(self, opt, value):
        pass


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


@pytest.fixture
def client():
    """ A Client with its network sockets replaced by recording stubs.

        Client is an Entity singleton, so every test gets the same
        instance: reset its state and signal connections here.
    """
    from bluesky.network.client import Client
    cl = Client()
    if not isinstance(cl.sock_recv, FakeSocket):
        cl.sock_recv.close()
        cl.sock_send.close()
    cl.sock_recv = FakeSocket()
    cl.sock_send = FakeSocket()
    cl.nodes.clear()
    cl.servers.clear()
    cl.acttopics.clear()
    cl.act_id = None
    # Restore the signal connections of a freshly-started client
    for sig, slot in ((cl.node_added, cl.actnode),
                      (cl.node_removed, cl._actnode_removed)):
        try:
            sig.disconnect(slot)
        except Exception:
            pass
        sig.connect(slot)
    yield cl
