//! The state routing fold — the one funnel every value passes through on its
//! way into the [`DeviceState`] tree, whichever wire carried it.
//!
//! `spec/state.toml` declares which addresses the tree tracks and how; the
//! generator flattens it into [`generated::STATE_ROUTES`], one [`Route`] per
//! flat address, sorted by address. That table is data only. This module is the
//! hand-written half: [`route`] finds the row for an address, and
//! [`DeviceState::apply_update`] applies the eight rules that turn a row and an
//! [`Update`] into a field write and an event — or into nothing, when the row,
//! the wire, the range or the dump window says so.
//!
//! The rules are applied in a fixed order, because the vectors pin that order:
//! a value dropped by range never marks its address for the dump guard, and a
//! live value deduped as unchanged still does.

use crate::cbor::is_sensitive;
use crate::generated::{self, Field, Kind, Lane, Route, STATE_ROUTES, Wire};
use crate::model::{ApplyOutcome, DeviceEvent, RealtimeStatus};
use crate::state::{Channel, Decoded, DeviceState, Phase, Update};

/// The largest 14-bit value: what a `$01` can carry, and the range of a `u14`
/// row whichever wire the value came in on.
const U14_MAX: u64 = generated::FULL_SCALE as u64;

/// The extended-address boundary: below it a flat address is a `page * 128 +
/// number` pair the stream can name in a `$01`, and an untracked one is still
/// worth a generic event; at or above it there is no page to report.
const EXTENDED_ADDRESS_BASE: u32 = 128 * 128;

/// The routing-table row for a flat address, or `None` if the tree does not
/// track it.
///
/// A binary search: the generator emits the table sorted by address, and
/// [`table_is_sorted_by_address`](tests::table_is_sorted_by_address) holds it
/// to that so this stays correct.
pub fn route(address: u32) -> Option<&'static Route> {
    STATE_ROUTES
        .binary_search_by_key(&address, |r| r.address)
        .ok()
        .map(|i| &STATE_ROUTES[i])
}

/// How many consecutive rows a `multi` row's block spans, counted from its
/// base. The table expands one `span = N` row into `N` entries with the same
/// field, so the span is the run of them. The fold itself no longer needs it
/// — a block at the base is the frame whatever its length — so it remains
/// only for the tests that pin the table's shape.
#[cfg(test)]
fn span(base: &Route) -> usize {
    let start = STATE_ROUTES
        .binary_search_by_key(&base.address, |r| r.address)
        .unwrap_or(STATE_ROUTES.len());
    STATE_ROUTES[start..]
        .iter()
        .take_while(|r| r.field == base.field && r.kind == Kind::Multi)
        .count()
}

/// A value after the row's `kind` has interpreted it — what the tree stores and
/// what an update is compared against for dedupe.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Value {
    /// A `u14`, `u16` or `u7` number, or a `bpm` already divided down.
    Num(u16),
    /// A `bool`.
    Flag(bool),
    /// A `text`.
    Text(String),
    /// A `multi` block decoded as one unit: the meter frame.
    Frame(RealtimeStatus),
}

impl Value {
    fn num(&self) -> Option<u16> {
        match self {
            Value::Num(v) => Some(*v),
            _ => None,
        }
    }

    fn flag(&self) -> Option<bool> {
        match self {
            Value::Flag(on) => Some(*on),
            _ => None,
        }
    }

    fn text(self) -> Option<String> {
        match self {
            Value::Text(t) => Some(t),
            _ => None,
        }
    }

    fn frame(&self) -> Option<RealtimeStatus> {
        match self {
            Value::Frame(f) => Some(*f),
            _ => None,
        }
    }

    /// The number a `ParamChanged` event reports for a numeric row: the value
    /// as stored. A `bool` row reports the wire's own number instead (see
    /// [`DeviceState::write`]), so the three implementations agree on it.
    fn as_u16(&self) -> u16 {
        match self {
            Value::Num(v) => *v,
            Value::Flag(on) => u16::from(*on),
            Value::Text(_) | Value::Frame(_) => 0,
        }
    }
}

/// Does a row's `kind` accept this shape of payload? Page 0 is dual-use — the
/// same numbers are string tags via `$03` and numerics via `$01` — so a row
/// says which face it is, and the other face is untracked.
fn accepts(kind: Kind, decoded: &Decoded) -> bool {
    matches!(
        (kind, decoded),
        (
            Kind::U14 | Kind::U16 | Kind::U7 | Kind::Bool | Kind::Bpm,
            Decoded::Num(_)
        ) | (Kind::Text, Decoded::Text(_))
            | (Kind::Multi, Decoded::Block(_))
    )
}

/// Decode a payload the row accepts into the value it stores, or `None` if the
/// value is out of the row's range — dropped, never truncated into a
/// plausible-looking reading.
fn decode(route: &Route, address: u32, decoded: &Decoded) -> Option<Value> {
    Some(match (route.kind, decoded) {
        (Kind::U14, Decoded::Num(v)) => Value::Num(
            u16::try_from(*v)
                .ok()
                .filter(|v| u64::from(*v) <= U14_MAX)?,
        ),
        (Kind::U16, Decoded::Num(v)) => Value::Num(u16::try_from(*v).ok()?),
        (Kind::U7, Decoded::Num(v)) => Value::Num((*v & 0x7F) as u16),
        (Kind::Bool, Decoded::Num(v)) => Value::Flag(*v != 0),
        (Kind::Bpm, Decoded::Num(v)) => {
            let wire = u16::try_from(*v)
                .ok()
                .filter(|v| u64::from(*v) <= U14_MAX)?;
            Value::Num(wire / generated::TEMPO_BPM_SCALE)
        }
        // A device secret is never stored, whatever row it arrives at; the
        // placeholder is stored in its place so the field still reads as set.
        (Kind::Text, Decoded::Text(t)) => Value::Text(if is_sensitive(address) {
            generated::REDACTED_PLACEHOLDER.to_string()
        } else {
            t.clone()
        }),
        (Kind::Multi, Decoded::Block(values)) => {
            let mut raw = [0u16; generated::METER_COUNT];
            for (dst, src) in raw.iter_mut().zip(values) {
                *dst = *src;
            }
            Value::Frame(RealtimeStatus { raw })
        }
        _ => return None,
    })
}

impl DeviceState {
    /// Fold one [`Update`] into the tree — THE funnel. Every entry point ends
    /// here, and the conformance vectors drive it directly.
    ///
    /// The rules, in order:
    ///
    /// 1. **Lookup.** Find the row for the address. A `multi` row takes any
    ///    [`Decoded::Block`] at its base as one unit (the meter frame),
    ///    zero-filling a short block and cutting a long one at its span; a
    ///    block anywhere else is folded element by element as
    ///    [`Decoded::Num`] updates at `base + i` (the rig-load dump).
    /// 2. **No route.** A numeric on the stream at a page/number address is
    ///    still reported as a FAST `ParamChanged`; anything else untracked is
    ///    silent — a control-channel value, a text, an extended address.
    /// 3. **Wire authority.** A `stream` row refuses the control channel: its
    ///    copies of the meter block, beat pulse, tuner and momentaries are a
    ///    different, unwanted feed. A `control` row accepts the stream, because
    ///    if the morph position ever appeared there it would be real.
    /// 4. **Kind/decoded mismatch** is untracked, exactly as no route.
    /// 5. **Range / decode.** `u14` and `bpm` drop a value past 16383, `u16`
    ///    past 65535; `u7` keeps the low seven bits; `bool` is nonzero = on;
    ///    `bpm` divides by [`generated::TEMPO_BPM_SCALE`]; `text` stores as is,
    ///    except a sensitive address stores the redaction placeholder.
    /// 6. **Dump authority.** While a dump is in progress, a live update marks
    ///    its address touched (changed or not), and a dump item for a touched
    ///    address is dropped: the dump's copy predates the push. Outside a
    ///    dump, a dump item folds like a live one.
    /// 7. **Dedupe.** A row with `dedupe` set is a no-op when it already holds
    ///    the decoded value — no event, no snapshot. The momentaries and the
    ///    meter frame never dedupe: every arrival is the information. Dedupe
    ///    silences the event, not the Navigator: a position row still reports
    ///    the (unchanged) index in [`ApplyOutcome::positions`].
    /// 8. **Store and report.** Write the field, raise the row's event; a
    ///    `fast` row is event only, a `slow` row also flags the snapshot.
    pub fn apply_update(&mut self, u: &Update) -> ApplyOutcome {
        // 1. Lookup — and the block special case.
        let found = route(u.address);
        if let Decoded::Block(values) = &u.decoded {
            let unit = found.filter(|r| r.kind == Kind::Multi && r.slot == Some(0));
            if unit.is_none() {
                return self.apply_block_elements(u, values);
            }
        }
        // 2. No route, and 4. a mismatch, both fall through to the generic
        // stream event; 3. the wire check sits between them so a control-channel
        // copy of a stream row is refused before its shape is even examined.
        let Some(route) = found.filter(|r| !refuses(r.wire, u.source)) else {
            return self.untracked(u);
        };
        if !accepts(route.kind, &u.decoded) {
            return self.untracked(u);
        }
        // 5. Range / decode.
        let Some(value) = decode(route, u.address, &u.decoded) else {
            return ApplyOutcome::empty();
        };
        // 6. Dump authority.
        if self.dump.active {
            match u.phase {
                Phase::Live => {
                    if !self.dump.touched.contains(&u.address) {
                        self.dump.touched.push(u.address);
                    }
                }
                Phase::Dump => {
                    if self.dump.touched.contains(&u.address) {
                        return ApplyOutcome::empty();
                    }
                }
            }
        }
        // 7. Dedupe. A silenced position update still reports the (unchanged)
        // index: the Navigator is confirmed by pushes, not by changes.
        let slot = usize::from(route.slot.unwrap_or(0));
        if route.dedupe && self.read(route.field, slot).as_ref() == Some(&value) {
            return ApplyOutcome {
                positions: self.position_report(route.field),
                ..ApplyOutcome::default()
            };
        }
        // 8. Store and report.
        // What a generic `ParamChanged` reports: the wire's number, clamped to
        // the event's 16 bits, so a `bool` row says what arrived and not the
        // 0/1 it stored — the same as the untracked fallback and the other two
        // implementations.
        let raw = match &u.decoded {
            Decoded::Num(v) => u16::try_from(*v).unwrap_or(u16::MAX),
            _ => 0,
        };
        let Some(events) = self.write(route, slot, value, raw) else {
            return ApplyOutcome::empty();
        };
        let positions = self.position_report(route.field);
        ApplyOutcome {
            events,
            slow_changed: route.lane == Lane::Slow,
            positions,
        }
    }

    /// What a position row reports for the Navigator once it has folded (or
    /// deduped): the flat rig index, when both halves are known and it fits
    /// sixteen bits. Every other row reports nothing.
    fn position_report(&self, field: Field) -> Vec<u16> {
        match field {
            Field::CurrentBank | Field::CurrentRigSlot => {
                self.current_rig_index().into_iter().collect()
            }
            _ => Vec::new(),
        }
    }

    /// Rule 1's other half: a block that is not the meter frame is the rig-load
    /// dump, consecutive values from the base on, each folded as its own
    /// numeric update with the outcomes merged.
    fn apply_block_elements(&mut self, u: &Update, values: &[u16]) -> ApplyOutcome {
        let mut out = ApplyOutcome::empty();
        for (i, &value) in values.iter().enumerate() {
            let step = self.apply_update(&Update {
                source: u.source,
                phase: u.phase,
                address: u.address + i as u32,
                decoded: Decoded::Num(u64::from(value)),
            });
            out.events.extend(step.events);
            out.slow_changed |= step.slow_changed;
            out.positions.extend(step.positions);
        }
        out
    }

    /// Rule 2: what an untracked update is worth. The stream's numerics at a
    /// page/number address still surface as a generic `ParamChanged`, because
    /// a client watching the delta stream wants to see them go by; everything
    /// else changes nothing and says nothing.
    fn untracked(&self, u: &Update) -> ApplyOutcome {
        match (u.source, &u.decoded) {
            (Channel::Stream, Decoded::Num(value)) if u.address < EXTENDED_ADDRESS_BASE => {
                // A `$06` can carry 35 bits at a page/number address; a value
                // the event cannot hold is dropped, not truncated.
                let Ok(value) = u16::try_from(*value) else {
                    return ApplyOutcome::empty();
                };
                ApplyOutcome::fast(DeviceEvent::ParamChanged {
                    page: (u.address / 128) as u8,
                    number: (u.address % 128) as u8,
                    value,
                })
            }
            _ => ApplyOutcome::empty(),
        }
    }

    /// The value a field currently holds, in the shape [`decode`] produces, so
    /// rule 7 can compare like with like. `None` for a field not yet seen — and
    /// for the momentaries, which store nothing.
    fn read(&self, field: Field, slot: usize) -> Option<Value> {
        let effect =
            |f: fn(&crate::state::Effect) -> Option<Value>| self.effects.get(slot).and_then(f);
        let bank =
            |f: fn(&crate::state::BankSlot) -> Option<Value>| self.bank.slots.get(slot).and_then(f);
        match field {
            Field::RigName => self.rig.name.clone().map(Value::Text),
            Field::RigAuthor => self.rig.author.clone().map(Value::Text),
            Field::RigDate => self.rig.date.clone().map(Value::Text),
            Field::RigComment => self.rig.comment.clone().map(Value::Text),
            Field::AmpName => self.amp.name.clone().map(Value::Text),
            Field::CabinetName => self.cabinet.name.clone().map(Value::Text),
            Field::MorphButton | Field::BeatPulse => None,
            Field::MorphPosition => self.morph.map(Value::Num),
            Field::TempoBpm => self.rig.tempo_bpm.map(Value::Num),
            Field::RigVolume => self.rig.volume.map(Value::Num),
            Field::AmpOn => self.amp.on.map(Value::Flag),
            Field::AmpGain => self.amp.gain.map(Value::Num),
            Field::CabinetOn => self.cabinet.on.map(Value::Flag),
            Field::EffectType => effect(|e| e.kind.map(Value::Num)),
            Field::EffectOn => effect(|e| e.on.map(Value::Flag)),
            Field::EffectMix => effect(|e| e.mix.map(Value::Num)),
            Field::TunerDeviance => self.tuner.deviance.map(Value::Num),
            Field::Status => Some(Value::Frame(self.status)),
            Field::TunerNote => self.tuner.note.map(|n| Value::Num(u16::from(n))),
            Field::MainVolume => self.output.main_volume.map(Value::Num),
            Field::HeadphoneVolume => self.output.headphone_volume.map(Value::Num),
            Field::MonitorVolume => self.output.monitor_volume.map(Value::Num),
            Field::BankRigName => bank(|b| b.rig_name.clone().map(Value::Text)),
            Field::BankAmpName => bank(|b| b.amp_name.clone().map(Value::Text)),
            Field::BankCabinetName => bank(|b| b.cabinet_name.clone().map(Value::Text)),
            Field::CurrentBank => self.current_bank.map(Value::Num),
            Field::CurrentRigSlot => self.current_rig_slot.map(Value::Num),
        }
    }

    /// Rule 8: the exhaustive set switch. Write `value` into the field the row
    /// names and return the events the row raises. `None` only if the value's
    /// shape does not fit the field — which cannot happen for a row the table
    /// declares consistently, and
    /// [`every_route_stores_and_reads_back`](tests::every_route_stores_and_reads_back)
    /// proves it never does.
    fn write(
        &mut self,
        route: &Route,
        slot: usize,
        value: Value,
        raw: u16,
    ) -> Option<Vec<DeviceEvent>> {
        let number = (route.address % 128) as u8;
        let page = (route.address / 128) as u8;
        let param_changed = |value: &Value| DeviceEvent::ParamChanged {
            page,
            number,
            value: match value {
                Value::Flag(_) => raw,
                other => other.as_u16(),
            },
        };
        let events = match route.field {
            Field::RigName => {
                self.rig.name = Some(value.text()?);
                vec![DeviceEvent::StringTag { number }, DeviceEvent::RigChanged]
            }
            Field::RigAuthor => {
                self.rig.author = Some(value.text()?);
                vec![DeviceEvent::StringTag { number }]
            }
            Field::RigDate => {
                self.rig.date = Some(value.text()?);
                vec![DeviceEvent::StringTag { number }]
            }
            Field::RigComment => {
                self.rig.comment = Some(value.text()?);
                vec![DeviceEvent::StringTag { number }]
            }
            Field::AmpName => {
                self.amp.name = Some(value.text()?);
                vec![DeviceEvent::StringTag { number }]
            }
            Field::CabinetName => {
                self.cabinet.name = Some(value.text()?);
                vec![DeviceEvent::StringTag { number }]
            }
            // The momentaries store nothing: the event is the whole message.
            Field::MorphButton => vec![DeviceEvent::MorphButton(value.flag()?)],
            Field::BeatPulse => vec![DeviceEvent::BeatPulse { on: value.flag()? }],
            Field::MorphPosition => {
                let v = value.num()?;
                self.morph = Some(v);
                vec![DeviceEvent::MorphChanged(v)]
            }
            Field::TempoBpm => {
                let bpm = value.num()?;
                self.rig.tempo_bpm = Some(bpm);
                vec![DeviceEvent::TempoBpm(bpm)]
            }
            Field::RigVolume => {
                self.rig.volume = Some(value.num()?);
                vec![param_changed(&value)]
            }
            Field::AmpOn => {
                self.amp.on = Some(value.flag()?);
                vec![param_changed(&value)]
            }
            Field::AmpGain => {
                self.amp.gain = Some(value.num()?);
                vec![param_changed(&value)]
            }
            Field::CabinetOn => {
                self.cabinet.on = Some(value.flag()?);
                vec![param_changed(&value)]
            }
            Field::EffectType => {
                self.effects.get_mut(slot)?.kind = Some(value.num()?);
                vec![DeviceEvent::EffectChanged { slot }]
            }
            Field::EffectOn => {
                self.effects.get_mut(slot)?.on = Some(value.flag()?);
                vec![DeviceEvent::EffectChanged { slot }]
            }
            Field::EffectMix => {
                self.effects.get_mut(slot)?.mix = Some(value.num()?);
                vec![DeviceEvent::EffectChanged { slot }]
            }
            Field::TunerDeviance => {
                let v = value.num()?;
                self.tuner.deviance = Some(v);
                vec![DeviceEvent::TunerDeviance(v)]
            }
            Field::Status => {
                self.status = value.frame()?;
                vec![DeviceEvent::Status(self.status)]
            }
            Field::TunerNote => {
                let note = value.num()? as u8;
                self.tuner.note = Some(note);
                vec![DeviceEvent::TunerNote(note)]
            }
            Field::MainVolume => {
                self.output.main_volume = Some(value.num()?);
                vec![param_changed(&value)]
            }
            Field::HeadphoneVolume => {
                self.output.headphone_volume = Some(value.num()?);
                vec![param_changed(&value)]
            }
            Field::MonitorVolume => {
                self.output.monitor_volume = Some(value.num()?);
                vec![param_changed(&value)]
            }
            Field::BankRigName => {
                self.bank.slots.get_mut(slot)?.rig_name = Some(value.text()?);
                vec![DeviceEvent::BankPreview { number }]
            }
            Field::BankAmpName => {
                self.bank.slots.get_mut(slot)?.amp_name = Some(value.text()?);
                vec![DeviceEvent::BankPreview { number }]
            }
            Field::BankCabinetName => {
                self.bank.slots.get_mut(slot)?.cabinet_name = Some(value.text()?);
                vec![DeviceEvent::BankPreview { number }]
            }
            // The position rows report both halves as now stored, so a listener
            // sees the whole position whichever half moved.
            Field::CurrentBank => {
                self.current_bank = Some(value.num()?);
                vec![self.current_position()]
            }
            Field::CurrentRigSlot => {
                self.current_rig_slot = Some(value.num()?);
                vec![self.current_position()]
            }
        };
        Some(events)
    }

    fn current_position(&self) -> DeviceEvent {
        DeviceEvent::CurrentPosition {
            bank: self.current_bank,
            slot: self.current_rig_slot,
        }
    }
}

/// Rule 3: does a row's `wire` refuse this channel? Only a `stream` row
/// refuses anything — the control channel's copy of it. A `control` row takes
/// the stream too: the morph position never appears there, but if it did it
/// would be real.
fn refuses(wire: Wire, source: Channel) -> bool {
    matches!((wire, source), (Wire::Stream, Channel::Control))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// [`route`] is a binary search, which is only a lookup if the generator
    /// kept its promise to sort the table.
    #[test]
    fn table_is_sorted_by_address() {
        assert!(
            STATE_ROUTES.windows(2).all(|w| w[0].address < w[1].address),
            "STATE_ROUTES must be strictly ascending by address"
        );
        assert_eq!(
            route(generated::CURRENT_BANK_ADDRESS).map(|r| r.field),
            Some(Field::CurrentBank)
        );
        assert!(route(102_405).is_none());
    }

    /// The meter row spans the eleven meter values, and nothing else is a
    /// `multi` row: the one place a block is a unit.
    #[test]
    fn meter_block_span_comes_from_the_table() {
        let base = route(
            u32::from(generated::PAGE_REALTIME) * 128 + u32::from(generated::METER_BLOCK_NUMBER),
        )
        .expect("meter base row");
        assert_eq!(base.kind, Kind::Multi);
        assert_eq!(base.slot, Some(0));
        assert_eq!(span(base), generated::METER_COUNT);
        assert_eq!(
            STATE_ROUTES
                .iter()
                .filter(|r| r.kind == Kind::Multi)
                .count(),
            generated::METER_COUNT
        );
    }

    /// Every row's `kind` fits its `field`: a value decoded by the row stores,
    /// reads back equal, and raises at least one event. This is what lets
    /// [`DeviceState::write`] treat a shape mismatch as impossible.
    #[test]
    fn every_route_stores_and_reads_back() {
        for r in STATE_ROUTES {
            // A `multi` row is written by its base as one frame.
            if r.kind == Kind::Multi && r.slot != Some(0) {
                continue;
            }
            let decoded = match r.kind {
                Kind::Text => Decoded::Text("x".into()),
                Kind::Multi => Decoded::Block(vec![7; span(r)]),
                _ => Decoded::Num(1),
            };
            let value = decode(r, r.address, &decoded)
                .unwrap_or_else(|| panic!("{:?} at {} did not decode", r.field, r.address));
            let mut st = DeviceState::new();
            let slot = usize::from(r.slot.unwrap_or(0));
            let events = st
                .write(r, slot, value.clone(), value.as_u16())
                .unwrap_or_else(|| panic!("{:?} at {} did not store", r.field, r.address));
            assert!(!events.is_empty(), "{:?} raised nothing", r.field);
            // The momentaries are the two fields that hold nothing.
            if matches!(r.field, Field::MorphButton | Field::BeatPulse) {
                assert_eq!(st.read(r.field, slot), None);
            } else {
                assert_eq!(
                    st.read(r.field, slot),
                    Some(value),
                    "{:?} read back",
                    r.field
                );
            }
        }
    }

    /// Only a `stream` row refuses a channel; `both` and `control` take either.
    #[test]
    fn wire_authority_refuses_only_the_control_copy() {
        assert!(refuses(Wire::Stream, Channel::Control));
        assert!(!refuses(Wire::Stream, Channel::Stream));
        assert!(!refuses(Wire::Control, Channel::Stream));
        assert!(!refuses(Wire::Control, Channel::Control));
        assert!(!refuses(Wire::Both, Channel::Stream));
        assert!(!refuses(Wire::Both, Channel::Control));
    }

    /// The rules run in their stated order. A value dropped by range (rule 5)
    /// never reaches the dump guard (rule 6); a value the guard sees marks its
    /// address even when dedupe (rule 7) then swallows it.
    #[test]
    fn rule_order_range_then_dump_then_dedupe() {
        let live = |value: u64| Update {
            source: Channel::Control,
            phase: Phase::Live,
            address: generated::CURRENT_BANK_ADDRESS,
            decoded: Decoded::Num(value),
        };
        let dump = |value: u64| Update {
            phase: Phase::Dump,
            ..live(value)
        };

        // Out of range: not marked, so the dump item lands.
        let mut st = DeviceState::new();
        st.begin_dump();
        assert_eq!(st.apply_update(&live(70_000)), ApplyOutcome::empty());
        assert!(st.apply_update(&dump(2)).slow_changed);
        assert_eq!(st.current_bank, Some(2));
        st.end_dump();

        // Deduped: still marked, so the dump item is refused.
        let mut st = DeviceState::new();
        assert!(st.apply_update(&live(3)).slow_changed);
        st.begin_dump();
        assert_eq!(st.apply_update(&live(3)), ApplyOutcome::empty());
        assert_eq!(st.apply_update(&dump(2)), ApplyOutcome::empty());
        st.end_dump();
        assert_eq!(st.current_bank, Some(3));
        // Once the dump is over, its items are as live as any.
        assert!(st.apply_update(&dump(2)).slow_changed);
        assert_eq!(st.current_bank, Some(2));
    }

    /// A secret arriving at a text row is stored as the placeholder, never as
    /// itself. No row today sits at a sensitive address, so this drives the
    /// decoder directly with the rig-name row standing in.
    #[test]
    fn sensitive_text_is_redacted_before_it_is_stored() {
        let row = route(u32::from(generated::STRING_RIG_NAME)).expect("rig name row");
        let secret = Decoded::Text("hunter2".into());
        assert_eq!(
            decode(row, generated::SENSITIVE_ADDRESSES[0], &secret),
            Some(Value::Text(generated::REDACTED_PLACEHOLDER.to_string()))
        );
        assert_eq!(
            decode(row, row.address, &secret),
            Some(Value::Text("hunter2".to_string()))
        );
    }
}
