"""Anycast group tools: set_group / list_groups / remove_group and send_text(to_group=...)."""

import base64
import contextlib

import pytest
from meshtastic import anycast

from meshtastic_mcp import admin


class FakeNode:
    def __init__(self):
        self.groups = {}

    def setGroup(self, name, pub, priv, uplink=False):
        self.groups[name] = (pub, priv, uplink)

    def deleteGroup(self, name):
        del self.groups[name]

    def listGroups(self):
        return [
            {"name": n, "id": anycast.group_id(pub), "member": priv is not None, "uplink": up}
            for n, (pub, priv, up) in self.groups.items()
        ]

    def groupId(self, name):
        return anycast.group_id(self.groups[name][0]) if name in self.groups else None


class FakeIface:
    def __init__(self, node, answer=None):
        self.localNode = node
        self.sent = []
        self.answer = answer

    def sendData(self, data, dest, **kw):
        self.sent.append((data, dest, kw))
        if kw.get("onResponse") and self.answer:
            kw["onResponse"](self.answer)

        class P:
            id = 0x1234

        return P()


@pytest.fixture
def fake(monkeypatch):
    node = FakeNode()
    state = {"iface": FakeIface(node)}

    @contextlib.contextmanager
    def fake_connect(port=None, linger_s=0.0, **kwargs):
        yield state["iface"]

    monkeypatch.setattr(admin, "connect", fake_connect)
    return state


def test_set_group_without_a_key_generates_one_and_makes_the_node_a_member(fake):
    r = admin.set_group("gw", uplink=True)
    pub = base64.b64decode(r["public_key"])
    priv = base64.b64decode(r["private_key"])
    assert anycast.public_key(priv) == pub
    assert r["member"] is True
    assert r["id"] == f"!{anycast.group_id(pub):08x}"
    listed = admin.list_groups()["groups"]
    assert listed == [{"name": "gw", "id": r["id"], "member": True, "uplink": True}]


def test_set_group_with_a_public_key_only_is_a_sender(fake):
    _, pub = anycast.generate_keypair()
    r = admin.set_group("gw", public_key=base64.b64encode(pub).decode())
    assert r["member"] is False
    assert "private_key" not in r
    admin.remove_group("gw")
    assert admin.list_groups()["groups"] == []


def test_send_text_to_a_group_reports_the_member_that_answered(fake):
    _, pub = anycast.generate_keypair()
    admin.set_group("gw", public_key=base64.b64encode(pub).decode())
    fake["iface"].answer = {
        "from": 0x3E3E3E02,
        "decoded": {"routing": {"errorReason": "NONE"}},
        "ackProofStatus": "ACK_PROOF_VALID",
    }
    r = admin.send_text("hi", to_group="gw", want_ack=True, ack_timeout_s=1)
    _, dest, kw = fake["iface"].sent[0]
    assert dest == anycast.group_id(pub)
    assert kw["anycast"] is True
    assert r["answered_by"] == "!3e3e3e02"
    assert r["ack_proof_status"] == "ACK_PROOF_VALID"


def test_send_text_to_an_unknown_group_fails(fake):
    with pytest.raises(admin.AdminError):
        admin.send_text("hi", to_group="nope")
