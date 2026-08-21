"""The TCP session and the protocol-selection handshake."""

from __future__ import annotations

import asyncio

import pytest
from fake_device import FakeDevice

from libkp.errors import ConnectError, ProtocolRejectedError, TimeoutErrorLibKP
from libkp.session import (
    PROTOCOL_MIDI3_STREAM,
    PROTOCOL_RESERVED,
    HandshakeOutcome,
    Session,
    parse_protocol_list,
)

IDLE = 0.2


def test_parses_a_crlf_guid_list_terminated_by_a_dot():
    assert parse_protocol_list(b"{AAA}\r\n{BBB}\r\n.\r\n") == ["{AAA}", "{BBB}"]
    assert parse_protocol_list(b"") == []
    assert parse_protocol_list(b".\r\n") == []
    # Anything after the end marker is ignored.
    assert parse_protocol_list(b"{AAA}\r\n.\r\n{CCC}\r\n") == ["{AAA}"]


def test_response_tail_is_whatever_follows_the_ack_line():
    outcome = HandshakeOutcome(response=b"+{X}\r\n\x14\xf0\x00\x20")
    assert outcome.accepted
    assert outcome.response_tail() == b"\x14\xf0\x00\x20"
    assert HandshakeOutcome(response=b"+{X}").response_tail() == b""
    assert not HandshakeOutcome(response=b"-NO\r\n").accepted


def test_handshake_selects_the_preferred_protocol():
    async def scenario():
        async with FakeDevice() as device:
            session = await Session.connect("127.0.0.1", device.port)
            try:
                outcome = await session.handshake([PROTOCOL_MIDI3_STREAM], IDLE)
                await session.write_session_preamble()
                await asyncio.wait_for(device.saw_preamble.wait(), 2.0)
            finally:
                await session.close()
            return outcome, device.selected

    outcome, selected = asyncio.run(scenario())
    assert outcome.offered == [PROTOCOL_RESERVED, PROTOCOL_MIDI3_STREAM]
    assert outcome.selected == PROTOCOL_MIDI3_STREAM
    assert selected == PROTOCOL_MIDI3_STREAM
    assert outcome.accepted


def test_handshake_falls_back_to_the_first_offered_protocol():
    async def scenario():
        async with FakeDevice(offered=["{ONLY-ONE}"]) as device:
            session = await Session.connect("127.0.0.1", device.port)
            try:
                return await session.handshake([PROTOCOL_MIDI3_STREAM], IDLE)
            finally:
                await session.close()

    outcome = asyncio.run(scenario())
    assert outcome.selected == "{ONLY-ONE}"


def test_handshake_surfaces_a_rejection():
    async def scenario():
        async with FakeDevice(accept=False) as device:
            session = await Session.connect("127.0.0.1", device.port)
            try:
                with pytest.raises(ProtocolRejectedError) as excinfo:
                    await session.handshake([PROTOCOL_MIDI3_STREAM], IDLE)
                return excinfo.value
            finally:
                await session.close()

    error = asyncio.run(scenario())
    assert error.name == PROTOCOL_MIDI3_STREAM
    assert "NO" in str(error)


def test_handshake_without_a_greeting_times_out():
    async def scenario():
        async with FakeDevice(offered=[]) as device:
            session = await Session.connect("127.0.0.1", device.port)
            try:
                with pytest.raises(TimeoutErrorLibKP):
                    await session.handshake([PROTOCOL_MIDI3_STREAM], 0.05)
            finally:
                await session.close()

    asyncio.run(scenario())


def test_response_tail_carries_the_first_stream_burst():
    burst = bytes.fromhex("f000203300000300000141433330f7")

    async def scenario():
        async with FakeDevice(tail_messages=[burst]) as device:
            session = await Session.connect("127.0.0.1", device.port)
            try:
                return await session.handshake([PROTOCOL_MIDI3_STREAM], IDLE)
            finally:
                await session.close()

    from libkp.midi3 import Unframer

    outcome = asyncio.run(scenario())
    assert Unframer().push(outcome.response_tail()) == [burst]


def test_read_once_returns_empty_on_a_quiet_tick():
    async def scenario():
        async with FakeDevice() as device:
            session = await Session.connect("127.0.0.1", device.port)
            try:
                await session.handshake([PROTOCOL_MIDI3_STREAM], IDLE)
                await session.write_session_preamble()
                return await session.read_once(0.05)
            finally:
                await session.close()

    assert asyncio.run(scenario()) == b""


def test_connect_to_a_closed_port_fails():
    async def scenario():
        # Bind and immediately release a port so nothing is listening on it.
        server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()
        with pytest.raises(ConnectError):
            await Session.connect("127.0.0.1", port, timeout=1.0)

    asyncio.run(scenario())


def test_session_reports_its_peer():
    async def scenario():
        async with FakeDevice() as device:
            session = await Session.connect("127.0.0.1", device.port)
            peer = session.peer
            await session.close()
            return peer, device.port

    peer, port = asyncio.run(scenario())
    assert peer == ("127.0.0.1", port)
