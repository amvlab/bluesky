"""
Tests for the client side of per-node shutdown (DELNODE):
the bare-command default to the active node, and active-node failover.
"""
import msgpack

from bluesky.network.common import genid, seqidx2id, IDLEN

GROUP = b'S' + b'grp'


def makenode(seqidx):
    return genid(GROUP, seqidx=seqidx)


def sentmessages(sock):
    """ Decode (dest, topic, data) from messages recorded by a FakeSocket. """
    out = []
    for frame, payload in sock.sent:
        out.append((frame[:IDLEN], frame[IDLEN:-IDLEN],
                    msgpack.unpackb(payload, raw=False)))
    return out


def test_bare_delnode_targets_active_node(client):
    """ DELNODE without argument targets the currently active node. """
    act = makenode(2)
    client.nodes.add(act)
    client.act_id = act

    client.delnode()

    msgs = [m for m in sentmessages(client.sock_send) if m[1] == b'DELNODE']
    assert len(msgs) == 1
    dest, _, data = msgs[0]
    assert data == act
    # Addressed to the server that owns the node
    assert dest == act[:-1] + seqidx2id(0)


def test_bare_delnode_without_active_node(client):
    """ DELNODE without argument and without an active node sends nothing. """
    client.delnode()

    assert not [m for m in sentmessages(client.sock_send) if m[1] == b'DELNODE']


def test_failover_on_active_node_removal(client):
    """ When the active node is removed, another remaining node becomes
        the active node automatically.
    """
    n1, n2 = makenode(1), makenode(2)
    client.nodes.update((n1, n2))
    client.actnode(n1)

    client.nodes.discard(n1)
    client.node_removed.emit(n1)

    assert client.act_id == n2


def test_no_failover_on_inactive_node_removal(client):
    """ Removal of a non-active node leaves the active node unchanged. """
    n1, n2 = makenode(1), makenode(2)
    client.nodes.update((n1, n2))
    client.actnode(n1)

    client.nodes.discard(n2)
    client.node_removed.emit(n2)

    assert client.act_id == n1


def test_failover_when_no_nodes_remain(client):
    """ When the last known node is removed, the client has no active
        node, and the next node that appears becomes active again.
    """
    n1 = makenode(1)
    client.nodes.add(n1)
    client.actnode(n1)

    client.nodes.discard(n1)
    client.node_removed.emit(n1)

    assert client.act_id is None

    n2 = makenode(2)
    client.nodes.add(n2)
    client.node_added.emit(n2)

    assert client.act_id == n2
