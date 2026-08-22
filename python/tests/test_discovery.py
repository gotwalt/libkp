"""UDP discovery: the poll, the reply accessors, and the target sweep."""

from __future__ import annotations

import asyncio

import pytest

from libkp.discovery import (
    DiscoveryOptions,
    DiscoveryPort,
    Reply,
    _is_poll,
    broadcast_targets,
    discover,
)
from libkp.errors import PortUnavailableError
from libkp.protocol import build_poll_request, push_field

# A synthetic reply payload in the shape the device answers with.
REPLY_PAYLOAD = bytearray(b"DSCV")
push_field(REPLY_PAYLOAD, b"NAMETest Profiler")
push_field(REPLY_PAYLOAD, b"SER#EXAMPLE0000000")
push_field(REPLY_PAYLOAD, b"VSTRRelease: 0.0.0.00000")
REPLY_PAYLOAD.append(0x00)
REPLY_PAYLOAD = bytes(REPLY_PAYLOAD)


def test_our_own_poll_is_recognised_as_a_poll():
    assert _is_poll(build_poll_request())
    assert not _is_poll(REPLY_PAYLOAD)
    assert not _is_poll(b"")
    assert not _is_poll(b"\xff\xff")


def test_reply_exposes_the_advertised_fields():
    reply = Reply(addr=("192.168.1.50", 5727), payload=REPLY_PAYLOAD)
    assert reply.ip == "192.168.1.50"
    assert reply.name == "Test Profiler"
    assert reply.serial == "EXAMPLE0000000"
    assert reply.version == "Release: 0.0.0.00000"


def test_reply_with_a_malformed_payload_degrades_gracefully():
    reply = Reply(addr=("192.168.1.50", 5727), payload=b"")
    assert reply.tags() is None
    assert reply.name is None
    assert reply.serial is None


def test_broadcast_targets_always_include_the_global_address():
    targets = broadcast_targets(["10.0.0.7"])
    assert targets[0] == "255.255.255.255"
    assert "10.0.0.7" in targets
    assert len(targets) == len(set(targets)), "targets must be de-duplicated"
    assert all(t.count(".") == 3 for t in targets)


def test_broadcast_targets_do_not_repeat_an_extra_that_is_already_present():
    assert broadcast_targets(["255.255.255.255"]).count("255.255.255.255") == 1


def test_discovery_with_no_device_returns_no_replies():
    """Our own echoed poll must not be mistaken for a device."""

    async def scenario():
        options = DiscoveryOptions(
            listen_for=0.2,
            repeat_every=0.05,
            extra_targets=["127.0.0.1"],
            port=54321,
        )
        return await discover(options)

    assert asyncio.run(scenario()) == []


def test_the_discovery_port_is_held_exclusively():
    """A second acquire must fail rather than quietly share the port.

    Sharing is the failure this guards against: the kernel gives an arriving
    reply to exactly one bound socket, so a co-bound listener steals replies
    instead of duplicating them.
    """
    with DiscoveryPort.acquire(54322) as first:
        assert first.port == 54322
        with pytest.raises(PortUnavailableError) as caught:
            DiscoveryPort.acquire(54322)
    assert caught.value.port == 54322
    assert "exclusive" in str(caught.value)


def test_releasing_the_port_lets_it_be_acquired_again():
    DiscoveryPort.acquire(54323).close()
    DiscoveryPort.acquire(54323).close()  # no leak: the first release freed it


def test_closing_a_port_twice_is_harmless():
    port = DiscoveryPort.acquire(54324)
    port.close()
    port.close()


def test_a_held_port_can_be_polled_repeatedly():
    """A long-running client re-polls to notice devices coming and going,
    without ever releasing the port in between."""

    async def scenario():
        options = DiscoveryOptions(listen_for=0.1, repeat_every=0.05, port=54325)
        with DiscoveryPort.acquire(54325) as port:
            return [await port.poll(options), await port.poll(options)]

    assert asyncio.run(scenario()) == [[], []]


def test_discover_releases_the_port_it_acquired():
    """The one-shot helper must not leave the port held behind it."""

    async def scenario():
        await discover(DiscoveryOptions(listen_for=0.1, repeat_every=0.05, port=54326))

    asyncio.run(scenario())
    DiscoveryPort.acquire(54326).close()  # would raise if discover leaked it
