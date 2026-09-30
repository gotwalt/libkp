"""Measure what the CBOR control channel pushes while the Profiler sits idle.

The question this answers: can a client watch for "someone started playing"
over CBOR alone, without ever opening the 20 Hz MIDI3 meter stream?

`docs/06` says CBOR carries only the tuner strobe phase out of the meter
block, and `docs/07` says a silent input parks the strobe group at zero. If
the device also stays *quiet* while that value is parked, an idle session
costs the device nothing and the strobe leaving zero is the wake-up edge.

Run it with the device idle (don't touch the guitar), then again while
playing, and compare the rates.

    uv run examples/cbor_idle_rate.py --ip 192.168.7.151 --seconds 60
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import time

from libkp.cbor import CborSession

#: page 0x7C * 128 + 81 — "Tuner Strobe Phase" (METER_FIELDS index 3).
STROBE_PHASE = 124 * 128 + 81
#: page 0x7C * 128 + 0 — the beat pulse, the other known live CBOR feed.
BEAT_PULSE = 124 * 128


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", required=True)
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument(
        "--settle",
        type=float,
        default=5.0,
        help="seconds to discard after connect, so the state dump is not counted",
    )
    args = ap.parse_args()

    session = await CborSession.connect(args.ip)
    updates = session.updates()

    deadline = time.monotonic() + args.settle
    dumped = 0
    while time.monotonic() < deadline:
        try:
            await asyncio.wait_for(updates.get(), timeout=deadline - time.monotonic())
            dumped += 1
        except TimeoutError:
            break
    print(f"discarded {dumped} values from the state dump / settle window\n")

    counts: collections.Counter[int] = collections.Counter()
    strobe_values: list[int] = []
    started = time.monotonic()
    deadline = started + args.seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            address, value = await asyncio.wait_for(updates.get(), timeout=remaining)
        except TimeoutError:
            break
        counts[address] += 1
        if address == STROBE_PHASE:
            strobe_values.append(value)

    elapsed = time.monotonic() - started
    await session.close()

    total = sum(counts.values())
    print(f"=== {total} values in {elapsed:.1f}s = {total / elapsed:.2f}/sec ===\n")
    if not counts:
        print("The channel was SILENT while idle. This is the good outcome:")
        print("an idle CBOR session costs the device no push traffic at all.")
        return

    for address, n in counts.most_common(15):
        label = ""
        if address == STROBE_PHASE:
            label = "  <- tuner strobe phase"
        elif address == BEAT_PULSE:
            label = "  <- beat pulse"
        print(f"  addr {address:>7}  {n:>6} values  {n / elapsed:>7.2f}/sec{label}")

    if strobe_values:
        nonzero = [v for v in strobe_values if v != 0]
        print(
            f"\nstrobe phase: {len(strobe_values)} arrivals, "
            f"{len(nonzero)} non-zero, distinct values {len(set(strobe_values))}"
        )
        print("  all-zero while idle means the strobe is a usable 'someone is playing' edge.")


if __name__ == "__main__":
    asyncio.run(main())
