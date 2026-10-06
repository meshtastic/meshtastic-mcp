"""inject_frame's header_options: carried on the injected packet and covered by the channel AAD."""

from meshtastic.protobuf import wire_pb2

from meshtastic_mcp import inject


def test_header_options_ride_the_packet_and_the_aad():
    opts = wire_pb2.HeaderOptions(fragment=(5 << 6) | (1 << 3) | 1).SerializeToString()
    mp = inject._header(
        from_node=0x1234, to_node=0xFFFFFFFF, packet_id=7, ch_hash=0x5A, header_options=opts
    )
    assert mp.header_options == opts
    aad = inject.channel_aad(mp)
    assert aad[1] & 0x02, "the opt flag is set in the AAD's flags byte"
    assert aad.endswith(bytes([len(opts)]) + opts)


def test_no_header_options_leaves_the_aad_as_it_was():
    mp = inject._header(from_node=0x1234, to_node=0xFFFFFFFF, packet_id=7, ch_hash=0x5A)
    assert not mp.header_options
    assert not inject.channel_aad(mp)[1] & 0x02
