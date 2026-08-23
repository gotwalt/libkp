"""The TCP session, the protocol-selection handshake and the connection ledger.

Every ``FakeDevice`` binds a fresh ephemeral port, and the ledger is keyed by
``(ip, port)``, so tests do not wait on one another. A test that opens a second
socket to the *same* fake -- or lands on a port the OS handed out less than a
second ago -- pays one ``CONNECTION_COOLDOWN``; the ledger tests below do so on
purpose.
"""

from __future__ import annotations

import asyncio

import pytest
from fake_device import FakeDevice

from libkp.errors import ConnectError, ProtocolRejectedError, TimeoutErrorLibKP
from libkp.session import (
    CONNECTION_COOLDOWN,
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


# -- the connection ledger ---------------------------------------------------

#: Generous bound for an open that must *not* wait: well under the cooldown,
#: well over a localhost dial.
PROMPT = CONNECTION_COOLDOWN / 4


def test_reopening_the_same_peer_waits_out_the_cooldown():
    async def scenario():
        loop = asyncio.get_running_loop()
        async with FakeDevice() as device:
            first = await Session.connect("127.0.0.1", device.port)
            await first.close()
            closed_at = loop.time()
            second = await Session.connect("127.0.0.1", device.port)
            opened_at = loop.time()
            await second.close()
            return opened_at - closed_at

    assert asyncio.run(scenario()) >= CONNECTION_COOLDOWN


def test_a_second_open_beside_an_open_session_also_waits():
    # The cooldown runs from the last *open* as well as the last close: two
    # sockets to one peer must not be opened back to back even if neither is
    # closed in between.
    async def scenario():
        loop = asyncio.get_running_loop()
        async with FakeDevice() as device:
            first = await Session.connect("127.0.0.1", device.port)
            opened_first = loop.time()
            second = await Session.connect("127.0.0.1", device.port)
            opened_second = loop.time()
            await first.close()
            await second.close()
            return opened_second - opened_first

    assert asyncio.run(scenario()) >= CONNECTION_COOLDOWN


def test_different_peers_do_not_wait_on_each_other():
    async def scenario():
        loop = asyncio.get_running_loop()
        async with FakeDevice() as one, FakeDevice() as two:
            started = loop.time()
            first = await Session.connect("127.0.0.1", one.port)
            second = await Session.connect("127.0.0.1", two.port)
            elapsed = loop.time() - started
            await first.close()
            await second.close()
            # Closing `one` must not delay a fresh open to `two`'s neighbour
            # either: only the same (ip, port) is paced.
            async with FakeDevice() as three:
                started = loop.time()
                third = await Session.connect("127.0.0.1", three.port)
                elapsed_after_close = loop.time() - started
                await third.close()
            return elapsed, elapsed_after_close

    elapsed, elapsed_after_close = asyncio.run(scenario())
    assert elapsed < PROMPT
    assert elapsed_after_close < PROMPT


def test_closing_twice_stamps_the_ledger_once():
    # A second close must not push the cooldown out again: the model's close
    # and an `async with` exit can both reach the session.
    async def scenario():
        loop = asyncio.get_running_loop()
        async with FakeDevice() as device:
            session = await Session.connect("127.0.0.1", device.port)
            await session.close()
            closed_at = loop.time()
            await asyncio.sleep(CONNECTION_COOLDOWN / 2)
            await session.close()
            again = await Session.connect("127.0.0.1", device.port)
            waited = loop.time() - closed_at
            await again.close()
            return waited

    waited = asyncio.run(scenario())
    assert CONNECTION_COOLDOWN <= waited < CONNECTION_COOLDOWN + PROMPT


def test_the_snapshot_fetch_goes_through_the_ledger():
    # `fetch_state_snapshot` opens its own socket via `Session.connect`, so a
    # fetch straight after a session closes is held back like any other open.
    # The fake accepts whatever protocol is named and never answers the dump,
    # so the fetch returns an empty snapshot once its short timeout passes.
    from libkp.cbor import fetch_state_snapshot

    async def scenario():
        loop = asyncio.get_running_loop()
        async with FakeDevice() as device:
            session = await Session.connect("127.0.0.1", device.port)
            await session.close()
            closed_at = loop.time()
            snapshot = await fetch_state_snapshot("127.0.0.1", port=device.port, timeout=0.05)
            return loop.time() - closed_at, snapshot

    elapsed, snapshot = asyncio.run(scenario())
    assert elapsed >= CONNECTION_COOLDOWN
    assert not snapshot.is_complete()
