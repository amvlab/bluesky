"""
Tests for per-node shutdown (DELNODE) in the BlueSky server.
"""
from bluesky.network.common import genid, bin2hex
from .conftest import FakeProcess


def addfakenodes(server, count):
    """ Register `count` fake node processes with the server,
        including all per-node bookkeeping. Returns their ids.
    """
    ids = []
    for _ in range(count):
        server.max_group_idx += 1
        node_id = genid(server.server_id[:-1], seqidx=server.max_group_idx)
        server.spawned_processes[node_id] = FakeProcess()
        server.sim_nodes.add(node_id)
        server.avail_nodes.add(node_id)
        ids.append(node_id)
    return ids


def test_delnode_removes_only_target(server):
    """ Deleting one of several nodes only terminates that node,
        removes all its bookkeeping, and announces the removal.
    """
    n1, n2, n3 = addfakenodes(server, 3)
    target = server.spawned_processes[n2]
    survivors = [server.spawned_processes[n1], server.spawned_processes[n3]]

    server.delnode(n2)

    assert target.terminated and not target.killed
    assert all(not p.terminated and not p.killed for p in survivors)
    assert set(server.spawned_processes) == {n1, n3}
    assert server.sim_nodes == {n1, n3}
    assert server.avail_nodes == {n1, n3}
    # Clients are informed through an unsubscribe message for the node id
    assert [b'\x00' + n2] in server.sock_recv.sent
    # The server itself keeps running
    assert server.running


def test_delnode_accepts_hex_string(server):
    """ The node id can also be passed as its hexadecimal string
        representation, as used on the command line.
    """
    n1, n2 = addfakenodes(server, 2)

    server.delnode(bin2hex(n2))

    assert set(server.spawned_processes) == {n1}
    assert [b'\x00' + n2] in server.sock_recv.sent


def test_delnode_unknown_id_is_noop(server):
    """ Deleting an unknown node id is ignored gracefully, and echoed
        back to the caller when a sender is known.
    """
    n1, n2 = addfakenodes(server, 2)
    unknown = genid(server.server_id[:-1], seqidx=42)
    sender = genid('C', seqidx=1)

    server.delnode(unknown, sender)
    server.delnode('definitely-not-hex', sender)

    assert set(server.spawned_processes) == {n1, n2}
    assert server.sim_nodes == {n1, n2}
    assert not server.sock_recv.sent
    # Both rejections were echoed back to the sender
    echoes = [msg for msg in server.sock_send.sent
              if msg[0].startswith(sender + b'ECHO')]
    assert len(echoes) == 2
    assert server.running


def test_delnode_last_node_is_refused(server):
    """ Deleting the last remaining node is refused (use QUIT to stop
        the server), and the refusal is echoed back to the caller.
    """
    (n1,) = addfakenodes(server, 1)
    sender = genid('C', seqidx=1)

    server.delnode(n1, sender)

    proc = server.spawned_processes.get(n1)
    assert proc is not None and not proc.terminated and not proc.killed
    assert server.sim_nodes == {n1}
    assert not server.sock_recv.sent
    echoes = [msg for msg in server.sock_send.sent
              if msg[0].startswith(sender + b'ECHO')]
    assert len(echoes) == 1
    assert server.running


def test_delnode_kill_escalation(server):
    """ A node that does not exit within the bounded wait is killed. """
    n1, n2 = addfakenodes(server, 2)
    proc = server.spawned_processes[n1]
    proc.hang = True

    server.delnode(n1)

    assert proc.terminated
    assert proc.killed
    assert set(server.spawned_processes) == {n2}
    assert [b'\x00' + n1] in server.sock_recv.sent
