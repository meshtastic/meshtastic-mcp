"""Node records as list_nodes returns them."""

from meshtastic_mcp.info import _node_record


def test_has_ratchet_comes_from_the_node_flags() -> None:
    assert _node_record({"num": 1, "hasRatchet": True})["has_ratchet"] is True
    assert _node_record({"num": 2})["has_ratchet"] is False
