//! `DeviceModel` — a **React-style store** over a Profiler: one handle, one
//! typed immutable [`DeviceState`] snapshot the UI binds to, one event stream,
//! and a command channel for writes.
//!
//! It is the only object in libkp that holds a socket to the device. It owns
//! two links, and never shows either of them to a caller except by name in
//! [`DeviceEvent::ChannelChanged`]:
//!
//! - the **stream** — the MIDI3 session, which carries the meter frame, the
//!   parameter pushes, the strings, and the request/reply lane every
//!   `request_*` method rides on. The model cannot do without it: losing it
//!   is losing the device.
//! - the **control link** — the device's native CBOR channel, opened by
//!   default right after the stream. Its state dump and its live pushes fold
//!   into the same tree, which is how the morph position (a value the stream
//!   never carries) reaches [`DeviceState::morph`]. The link is read-only by
//!   construction: the one item libkp ever writes on it is the dump trigger,
//!   and there is no command queue to write anything else. Its health is
//!   reported, never hidden: [`DeviceState::channels`] says whether it is
//!   open, and [`Connection::Degraded`] says the stream is up without it.
//!
//! Both links feed one funnel: every value, whichever wire carried it, goes
//! through [`DeviceState::apply_update`] under one lock, and a burst of them
//! (one read chunk, the whole dump) republishes the snapshot at most once.
//!
//! Two layers:
//!
//! - [`DeviceState`] (in [`crate::state`]) is the **pure, network-free core**: a
//!   plain-data tree — rig, amp, cabinet, the eight effect slots, tuner, output,
//!   morph, and the latest [`RealtimeStatus`] meter frame. It takes one
//!   already-unframed MIDI message at a time via [`DeviceState::apply`], or one
//!   CBOR item via [`DeviceState::apply_cbor`], and both hand the value to the
//!   same routing fold ([`crate::routes`]), returning an [`ApplyOutcome`] (the
//!   granular [`DeviceEvent`]s produced, plus whether any *slow* field
//!   changed). It does no IO, so unit tests drive it with synthesized messages.
//!
//! - [`DeviceModel`] is the **async handle** (cheap to `Clone`; an `Arc` inside).
//!   [`DeviceModel::connect`] opens the stream, starts its ingest and writer,
//!   kicks off the read-only sync burst, and opens the control link in the
//!   background. Dropping the last handle, or calling
//!   [`close`](DeviceModel::close), closes both links.
//!
//! # This is a store
//!
//! Three access points, mirroring a React store:
//!
//! - [`state`](DeviceModel::state) — `getState`: the current snapshot.
//! - [`subscribe`](DeviceModel::subscribe) — the **store**: a fresh snapshot is
//!   emitted only when *slow* state changed, coalesced to at most once per
//!   ingested chunk.
//! - [`events`](DeviceModel::events) — the granular delta stream (every
//!   [`DeviceEvent`], including the fast ones), for callers that want deltas.
//! - [`status`](DeviceModel::status) — the **fast lane**: poll this per
//!   animation frame for the meter/tuner frame (equals `state().status`).
//!
//! State is classified FAST vs SLOW. **FAST** = the meter [`RealtimeStatus`]
//! block, the beat pulse, and tuner deviance — high-rate, poll them via
//! [`status`](DeviceModel::status). **SLOW** = everything else (rig / amp / cab /
//! effect / output / tempo / morph / tuner-note / connection / channels):
//! these drive the coalesced [`subscribe`](DeviceModel::subscribe) snapshot.
//!
//! # Parameters, requests and actions
//!
//! [`DeviceModel`]'s command methods split into three groups:
//!
//! - **Parameters** (`set_*`) — settable values the device stores. They go out
//!   as 14-bit NRPN `$01` Single Parameter Changes (via
//!   [`crate::nrpn::set_single`]); the device applies the write silently and
//!   does *not* echo it back on the stream, so follow a set with
//!   [`request_param`](DeviceModel::request_param) when [`DeviceModel::state`]
//!   should confirm the new value. The setters are:
//!   [`set_gain`](DeviceModel::set_gain),
//!   [`set_rig_volume`](DeviceModel::set_rig_volume),
//!   [`set_main_volume`](DeviceModel::set_main_volume),
//!   [`set_monitor_volume`](DeviceModel::set_monitor_volume),
//!   [`set_effect_enabled`](DeviceModel::set_effect_enabled),
//!   [`set_effect_mix`](DeviceModel::set_effect_mix),
//!   [`set_tempo_bpm`](DeviceModel::set_tempo_bpm), and the escape hatch
//!   [`set_param`](DeviceModel::set_param).
//! - **Requests** (`request_*`, `refresh*`) — read-only questions with an
//!   answer: each one goes out on the stream's request lane and resolves with
//!   the device's reply, or with [`RequestError::Timeout`] after
//!   [`generated::REQUEST_TIMEOUT_MS`]. At most
//!   [`generated::MAX_IN_FLIGHT_REQUESTS`] are on the wire at once; the rest
//!   queue. The reply folds into the snapshot on its way to the caller.
//! - **Actions** (verbs) — momentary presses and live expression that carry no
//!   stored value. They go out as 7-bit Control Change messages via the
//!   [`crate::control`] vocabulary and are *not* reflected in state:
//!   [`bank`](DeviceModel::bank) (the preselect alone, which loads nothing),
//!   [`tap_tempo`](DeviceModel::tap_tempo),
//!   [`tuner_mode`](DeviceModel::tuner_mode), the buttons and pedals, and the
//!   escape hatch [`send_control`](DeviceModel::send_control).
//! - **Navigation** — the one action with a consequence the device must be
//!   protected from. A rig load is never sent directly: it is *aimed* through
//!   the Navigator ([`navigate_to`](DeviceModel::navigate_to),
//!   [`step_rig`](DeviceModel::step_rig), [`step_bank`](DeviceModel::step_bank),
//!   [`select_slot`](DeviceModel::select_slot)), which sends one load at a
//!   time — the next only [`generated::RIG_LOAD_SETTLE_MS`] after the last —
//!   so a burst of taps costs two loads however long it is, and two loads can
//!   never overlap, which is what wedges the device on a delayed fuse. The
//!   aim shows in [`DeviceState::navigation`] at once; the device's
//!   confirmation is [`DeviceEvent::NavigationSettled`], and an aim it never
//!   confirms (one past the end of its rigs) is
//!   [`DeviceEvent::NavigationDropped`] after [`generated::PENDING_WINDOW_MS`].
//!   Every other road to a rig load is closed:
//!   [`send_control`](DeviceModel::send_control) and
//!   [`send_raw`](DeviceModel::send_raw) refuse the load controllers
//!   ([`generated::RIG_LOAD_CONTROLLERS`]), Program Change and Bank Select
//!   with [`CommandError::RigLoadRequiresNavigator`], before a byte goes out.
//!   The state machine itself is [`nav::NavigatorState`], pinned by the
//!   `navigation.json` vectors so every language runs the same one.
//!
//! Power users reach the full raw vocabulary through
//! [`send_control`](DeviceModel::send_control) and the [`crate::control`]
//! module; any address at all through [`set_param`](DeviceModel::set_param);
//! and any MIDI bytes at all through [`send_raw`](DeviceModel::send_raw) —
//! all but the rig loads, which belong to the Navigator.

mod core;
mod lane;
mod links;
pub mod nav;
mod supervisor;

use std::net::Ipv4Addr;
use std::sync::Arc;
use std::time::Duration;

use tokio::sync::broadcast;

use crate::control::{self, Control};
use crate::error::SessionError;
use crate::generated::{self, Field};
use crate::nrpn::{
    self, request_extended_param, request_extended_string, request_rendered_string, request_single,
    request_string, set_single,
};
use crate::params::{self, EFFECT_PARAM_MIX, EFFECT_PARAM_STATE};
use crate::state::{
    Channel, ChannelState, Connection, Decoded, DeviceState, NavDrop, Phase, Update,
};

use self::core::{PendingKey, Reply};
use self::supervisor::Shared;

pub use self::nav::{NavAction, NavigatorState};

/// Rig Settings page (holds Tempo bpm at number 0, Rig Volume at number 1).
const PAGE_RIG_SETTINGS: u8 = generated::PAGE_RIG_SETTINGS;
/// Tempo bpm number on [`PAGE_RIG_SETTINGS`]; the value is `bpm * 64`.
const TEMPO_NUMBER: u8 = generated::TEMPO_NUMBER;
/// Rig Volume number on [`PAGE_RIG_SETTINGS`] (14-bit).
const RIG_VOLUME_NUMBER: u8 = generated::RIG_VOLUME_NUMBER;
/// Fixed-point scale of the Tempo bpm parameter: the wire value is `bpm * 64`.
const TEMPO_BPM_SCALE: u16 = generated::TEMPO_BPM_SCALE;
/// Amplifier page (holds On/Off at number 2, Gain at number 4).
const AMP_PAGE: u8 = generated::AMP_PAGE;
/// Gain number on [`AMP_PAGE`] (14-bit).
const GAIN_NUMBER: u8 = generated::GAIN_NUMBER;
/// System/Global page (holds Main/Monitor output volumes).
const SYSTEM_PAGE: u8 = generated::SYSTEM_PAGE;
/// Main Output Volume number on [`SYSTEM_PAGE`] (14-bit).
const MAIN_VOL_NUMBER: u8 = generated::MAIN_VOLUME_NUMBER;
/// Monitor Output Volume number on [`SYSTEM_PAGE`] (14-bit).
const MONITOR_VOL_NUMBER: u8 = generated::MONITOR_VOLUME_NUMBER;
/// The maximum 14-bit NRPN value (0–16383).
const NRPN_MAX: u16 = generated::FULL_SCALE;
/// How many 14-bit values the realtime status block carries.
const METER_COUNT: usize = generated::METER_COUNT;

/// Product byte addressed in outbound SysEx (0x00 = Profiler).
const PRODUCT: u8 = nrpn::PRODUCT_PROFILER;
/// Device byte addressed in outbound SysEx (0x7F = omni).
const DEVICE: u8 = nrpn::DEVICE_OMNI;
/// MIDI channel used for Control Change commands (0 = channel 1).
const CC_CHANNEL: u8 = 0;

/// A decoded realtime status / meter-block frame: the eleven 14-bit values the
/// stream pushes at NRPN `0x7C/78..88` (function `$02`, page `0x7C`, number
/// `0x4E`). The field identities were established by observed experimentation
/// (knob-sweep and pitch-bend correlation against the labelled parameter
/// stream); see the generated `METER_FIELDS` table.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct RealtimeStatus {
    /// The eleven raw 14-bit values (v0..v10) in NRPN order (0x7C/78..88).
    pub raw: [u16; METER_COUNT],
}

impl RealtimeStatus {
    /// Tuner strobe phase (v3): a wrapping 0–16383 phase whose rotation rate
    /// tracks pitch deviance (stationary = in tune).
    pub fn strobe_phase(&self) -> u16 {
        self.raw[generated::STROBE_PHASE_INDEX]
    }

    /// The three tuner strobe display-segment drivers (v0/v1/v2).
    pub fn strobe_segments(&self) -> [u16; 3] {
        generated::STROBE_SEGMENT_INDICES.map(|i| self.raw[i])
    }

    /// Stack tap level (v4): pre-rig-volume amplitude.
    pub fn stack_level(&self) -> u16 {
        self.raw[4]
    }

    /// Rig output level (v6): post-rig-volume amplitude.
    pub fn rig_out_level(&self) -> u16 {
        self.raw[6]
    }

    /// Loudness (v9): slow RMS of the output.
    pub fn loudness(&self) -> u16 {
        self.raw[9]
    }
}

/// A typed change emitted by [`DeviceState::apply`] and broadcast by a
/// [`DeviceModel`].
///
/// `#[non_exhaustive]`: new variants may be added as more of the stream is
/// decoded, so downstream `match`es must include a wildcard arm.
///
/// Not `Copy`: the [`RenderedString`](DeviceEvent::RenderedString) variant owns
/// a `String`. Clone it if you need an owned copy.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub enum DeviceEvent {
    /// The loaded rig changed (its Rig Name was (re)applied).
    RigChanged,
    /// A page-0 string tag was applied (`number` per the string-tag table).
    StringTag {
        /// The string-tag number.
        number: u8,
    },
    /// A bank-preview name was applied (page `0x96`): one of the current bank's
    /// rig / amp / cabinet names. Read [`DeviceState::bank`] for the new value.
    BankPreview {
        /// The Bank Preview number (0..14): 0–4 rig, 5–9 amp, 10–14 cabinet.
        number: u8,
    },
    /// An effect slot's Type, On/Off state or Mix changed.
    EffectChanged {
        /// Index into [`crate::params::effect_slots`] (0 = A … 7 = REV).
        slot: usize,
    },
    /// A numeric parameter changed that the model does not model specially.
    ParamChanged {
        /// Address page.
        page: u8,
        /// Address number.
        number: u8,
        /// 14-bit value.
        value: u16,
    },
    /// A new realtime status / meter frame arrived.
    Status(RealtimeStatus),
    /// The tempo/beat pulse toggled (`on` = value != 0).
    BeatPulse {
        /// Pulse edge state.
        on: bool,
    },
    /// The tempo changed, in whole beats per minute.
    TempoBpm(u16),
    /// The morph position changed (`$01` page 0 / number 0x77): 0 = base,
    /// 16383 = fully morphed.
    ///
    /// Only the control link ever carries this: the position is never sent on
    /// the MIDI3 stream, even while a morph is ramping. See
    /// [`Self::MorphButton`].
    MorphChanged(u16),
    /// The morph button was pressed (`true`) or released (`false`) — momentary,
    /// so nothing about it is stored in the snapshot.
    ///
    /// This is what the MIDI3 stream shows of a morph: *that* one happened, and
    /// what it did to the audio parameters, but never where the fader sits.
    MorphButton(bool),
    /// The tuner pitch deviance changed (`$01` page 0x7C / number 0x0F; 8192 =
    /// in tune).
    TunerDeviance(u16),
    /// The tuner's detected note changed (`$01` page 0x7D / number 0x54; the
    /// low 7 bits are the note index).
    TunerNote(u8),
    /// A rendered-string reply arrived ($3C), carrying a value's exact display
    /// text (e.g. `5.2`, `120 BPM`, `<0.0>`) for the requested address. This is
    /// the reply to [`DeviceModel::request_render`], which also returns it; it
    /// is not stored in the snapshot tree.
    RenderedString {
        /// Address page.
        page: u8,
        /// Address number.
        number: u8,
        /// The 14-bit value the string renders.
        value: u16,
        /// The rendered display text.
        text: String,
    },
    /// The device's current position changed — a `$06` push or reply on the
    /// stream, a CBOR push, or a state-dump item, all landing in the same two
    /// rows. Carries both halves as now stored, so a listener sees the whole
    /// position whichever half moved; a half still unknown is `None`.
    CurrentPosition {
        /// Current bank, 0-based, once known.
        bank: Option<u16>,
        /// Current rig slot within the bank, 0-based, once known.
        slot: Option<u16>,
    },
    /// The model connected to a device: the stream is open. Raised alongside
    /// [`ConnectionChanged`](Self::ConnectionChanged) whenever the connection
    /// comes up, on the first connect and on every reconnect.
    Connected,
    /// The connection is gone: the device closed the stream (and no reconnect
    /// policy is set), or the model was closed or dropped. Raised alongside
    /// [`ConnectionChanged`](Self::ConnectionChanged).
    Disconnected,
    /// [`DeviceState::connection`] moved. Every transition raises this,
    /// including the ones between [`Connection::Connected`] and
    /// [`Connection::Degraded`] that the two events above do not cover.
    ConnectionChanged(Connection),
    /// One of the two links changed state. The only place a channel is named:
    /// an app that wants to show "morph unavailable" watches for
    /// `channel == Control` here, or keys on [`Connection::Degraded`].
    ChannelChanged {
        /// Which link.
        channel: Channel,
        /// Where it is now.
        state: ChannelState,
    },
    /// A sync finished. For [`Channel::Stream`]: the connect-time request
    /// burst's last reply landed or timed out. For [`Channel::Control`]: the
    /// state dump ended — its end marker was folded, or the settle time ran
    /// out.
    SyncCompleted {
        /// Which link's sync.
        source: Channel,
    },
    /// A request went unanswered for [`generated::REQUEST_TIMEOUT_MS`] and was
    /// dropped. It is never retried; the caller got
    /// [`RequestError::Timeout`].
    RequestTimedOut {
        /// The flat address asked for (`page * 128 + number`, or the extended
        /// address).
        address: u32,
    },
    /// The device reported the rig index the Navigator was aiming at: the
    /// move landed. [`DeviceState::navigation`]`.aim` is `None` again, and
    /// the position rows say where the device is.
    NavigationSettled {
        /// The flat rig index that was aimed at and reached.
        index: u16,
    },
    /// The Navigator gave up on an aim without the device reporting it —
    /// the index is past the end of the device's rigs and it stayed put, or
    /// the stream was not there to send the load on. The aim is `None`
    /// again; the device is wherever its position rows say.
    NavigationDropped {
        /// The flat rig index that was aimed at.
        index: u16,
        /// Why.
        reason: NavDrop,
    },
}

/// The result of applying one update to a [`DeviceState`] — one MIDI message,
/// one CBOR item, or one [`Update`] through the funnel:
/// the granular events it produced, plus whether any **slow** (snapshot) field
/// changed.
///
/// The store uses `slow_changed` to decide whether to emit a fresh snapshot:
/// `true` when a rig / amp / cab / effect / output / tempo / morph / tuner-note /
/// connection field moved, `false` for FAST-only traffic (the meter
/// [`RealtimeStatus`] block, the beat pulse, tuner deviance) and for untracked
/// generic params that leave the snapshot unchanged. The granular
/// [`DeviceEvent`]s are still emitted regardless, for [`DeviceModel::events`].
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ApplyOutcome {
    /// The typed deltas this update produced (in order).
    pub events: Vec<DeviceEvent>,
    /// Whether a slow (snapshot-visible) field changed.
    pub slow_changed: bool,
}

impl ApplyOutcome {
    /// An empty outcome: nothing happened, no slow change.
    pub(crate) fn empty() -> Self {
        ApplyOutcome::default()
    }

    /// One event that changed no slow field (FAST lane or untracked generic).
    pub(crate) fn fast(event: DeviceEvent) -> Self {
        ApplyOutcome {
            events: vec![event],
            slow_changed: false,
        }
    }

    /// Events that changed a slow (snapshot-visible) field.
    pub(crate) fn slow(events: Vec<DeviceEvent>) -> Self {
        ApplyOutcome {
            events,
            slow_changed: true,
        }
    }
}

/// Error returned when a command cannot be issued.
///
/// `#[non_exhaustive]`: more failure modes may be added, so downstream `match`es
/// must include a wildcard arm.
#[derive(Debug, thiserror::Error)]
#[non_exhaustive]
pub enum CommandError {
    /// The stream is not open, so there is nothing to write to.
    #[error("device model is disconnected; command channel closed")]
    Disconnected,
    /// An effect-slot name did not match A/B/C/D/X/MOD/DLY/REV.
    #[error("unknown effect slot {0:?}; use A B C D X MOD DLY REV")]
    UnknownSlot(String),
    /// The command would load a rig — a load-slot, up or down controller
    /// ([`generated::RIG_LOAD_CONTROLLERS`]), a Program Change, or a Bank
    /// Select — and nothing was sent. Rig loads go through the Navigator
    /// ([`DeviceModel::navigate_to`] and its conveniences), which is the
    /// only thing that can space them so that two never overlap.
    #[error("rig loads go through the Navigator (navigate_to / step_rig / select_slot)")]
    RigLoadRequiresNavigator,
}

/// Why a request did not come back with a value.
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum RequestError {
    /// The stream is not open — nothing was sent, or the stream ended while
    /// the request was waiting.
    #[error("device model is disconnected; the request lane is closed")]
    Disconnected,
    /// The device did not answer within [`generated::REQUEST_TIMEOUT_MS`]. The
    /// request is dropped and never retried; a
    /// [`DeviceEvent::RequestTimedOut`] was raised.
    #[error("the device did not answer within {}ms", generated::REQUEST_TIMEOUT_MS)]
    Timeout,
    /// The address is one the stream cannot read — the routing table marks it
    /// as the control link's alone (the morph position) — so nothing was sent:
    /// the device would only have stayed silent until the timeout.
    #[error("the address is not readable over the stream")]
    Unreadable,
}

/// Why the control link could not be reopened.
#[derive(Debug, thiserror::Error)]
pub enum ChannelError {
    /// [`ControlPolicy::Off`]: the model was told never to open it.
    #[error("the control link is off by policy")]
    Off,
    /// Less than [`generated::CONTROL_REOPEN_MIN_GAP_MS`] has passed since the
    /// last attempt to open it. The floor is the device's, not the caller's:
    /// reopening the channel faster than this has wedged devices.
    #[error(
        "the control link was opened less than {}ms ago",
        generated::CONTROL_REOPEN_MIN_GAP_MS
    )]
    TooSoon,
    /// The stream is not open, so there is no session to add a link to.
    #[error("device model is disconnected")]
    Disconnected,
    /// The open was attempted and failed at the transport.
    #[error("the control link could not be opened: {0}")]
    Session(#[source] SessionError),
}

/// Whether, and how insistently, [`DeviceModel::connect_with`] opens the
/// control link alongside the stream.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum ControlPolicy {
    /// Never open it. The model is a MIDI3 client only: no morph position,
    /// [`DeviceState::channels`]`.control` stays [`ChannelState::Closed`], and
    /// the connection is never [`Connection::Degraded`].
    Off,
    /// Open it after the stream, in the background; if it cannot be opened or
    /// is lost, carry on without it and report
    /// [`Connection::Degraded`].
    #[default]
    BestEffort,
    /// Open it before `connect_with` returns, and fail the connect (closing
    /// the stream) if it cannot be opened.
    Required,
}

/// What [`DeviceModel::connect_with`] does to fill the tree once the stream is
/// open.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum SyncStrategy {
    /// Ask nothing. The tree fills only from what the device pushes and from
    /// the control link's dump, if that link is open.
    Off,
    /// Run [`DeviceModel::refresh`] in the background as soon as the stream is
    /// open: every `request = true` row of the routing table — the string
    /// tags, each effect slot's type and state, the bank preview, the
    /// position, and the requested numerics — 46 requests, paced by the lane,
    /// answered by the device within tens of milliseconds. Raises
    /// [`DeviceEvent::SyncCompleted`] for [`Channel::Stream`] when the last
    /// reply lands or times out. This is cheaper for the device than a dump
    /// trigger, which is why there is no dump-based strategy.
    #[default]
    StreamBurst,
}

/// A doubling retry delay: `initial`, then twice that, up to `max`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Backoff {
    /// The first delay after a loss.
    pub initial: Duration,
    /// The ceiling the doubling stops at.
    pub max: Duration,
}

impl Backoff {
    /// The stream's measured-safe reconnect pacing:
    /// [`generated::RECONNECT_DELAY_MS`] (4 s) doubling to
    /// [`generated::RECONNECT_MAX_DELAY_MS`] (30 s). What a long-running app
    /// opts into; nothing faster has been tried against a device.
    pub fn default_stream() -> Self {
        Backoff {
            initial: Duration::from_millis(generated::RECONNECT_DELAY_MS),
            max: Duration::from_millis(generated::RECONNECT_MAX_DELAY_MS),
        }
    }

    /// The delay after `delay`: doubled, capped at `max`.
    pub(crate) fn next(&self, delay: Duration) -> Duration {
        delay.saturating_mul(2).min(self.max)
    }
}

impl Default for Backoff {
    fn default() -> Self {
        Self::default_stream()
    }
}

/// What the model does on its own when a link goes away. Both default to
/// nothing: a lost stream is reported as [`Connection::Disconnected`] and left
/// there, a lost control link as [`Connection::Degraded`].
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct ReconnectPolicy {
    /// Redial the stream after a loss, with this backoff, until it is back or
    /// the model is closed. The whole connect sequence runs again on the same
    /// handle — same receivers, same tree — and the connection reads
    /// [`Connection::Reconnecting`] in between.
    pub stream: Option<Backoff>,
    /// Reopen the control link after it fails or is lost, while the stream is
    /// up, at most once per this interval — floored at
    /// [`generated::CONTROL_REOPEN_MIN_GAP_MS`], whatever is asked.
    pub control_reopen: Option<Duration>,
}

/// Everything [`DeviceModel::connect_with`] can be told. [`Default`] is what
/// [`DeviceModel::connect`] uses: port [`crate::PORT`],
/// [`ControlPolicy::BestEffort`], [`SyncStrategy::StreamBurst`], and no
/// automatic reconnection.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConnectOptions {
    /// The TCP port. A real device only listens on [`crate::PORT`]; the field
    /// exists for fakes and tests.
    pub port: u16,
    /// Whether to open the control link.
    pub control: ControlPolicy,
    /// How to fill the tree at connect.
    pub sync: SyncStrategy,
    /// What to do when a link goes away.
    pub reconnect: ReconnectPolicy,
}

impl Default for ConnectOptions {
    fn default() -> Self {
        ConnectOptions {
            port: generated::PORT,
            control: ControlPolicy::default(),
            sync: SyncStrategy::default(),
            reconnect: ReconnectPolicy::default(),
        }
    }
}

/// An async handle to a live [`DeviceState`] store synced from a Profiler.
///
/// Cheap to clone: all clones share the two links, the state cache, the
/// snapshot and event broadcasts, and the command channel. Dropping the last
/// clone closes both links and raises [`DeviceEvent::Disconnected`], exactly
/// as [`close`](Self::close) does.
///
/// ```no_run
/// use std::net::Ipv4Addr;
/// use libkp::model::DeviceModel;
///
/// # async fn run() -> Result<(), Box<dyn std::error::Error>> {
/// let model = DeviceModel::connect(Ipv4Addr::new(192, 168, 1, 50)).await?;
/// // The store: a fresh snapshot each time slow state changes.
/// let mut snapshots = model.subscribe();
///
/// // Turn the reverb off — a tracked parameter, so the snapshot reflects it.
/// model.set_effect_enabled("REV", false).await?;
///
/// // Re-render whenever the snapshot changes.
/// while let Ok(state) = snapshots.recv().await {
///     if let Some(rev) = state.effect("REV") {
///         println!("REV on = {:?}", rev.on);
///     }
/// }
/// # Ok(())
/// # }
/// ```
#[derive(Clone)]
pub struct DeviceModel {
    handle: Arc<Handle>,
}

/// What the clones share and the last of them tears down. Kept apart from
/// [`Shared`] on purpose: the model's own tasks hold `Shared`, never this, so
/// the last *caller-held* clone going away is what closes the links.
struct Handle {
    shared: Arc<Shared>,
}

impl Drop for Handle {
    /// The last handle is gone, so nobody can ever read the state again: close
    /// both links and say so. Silently leaving a session open with no owner is
    /// exactly what the device cannot tolerate.
    fn drop(&mut self) {
        self.shared.shutdown();
    }
}

impl DeviceModel {
    /// Connect to `ip:5727` with [`ConnectOptions::default`]: open the stream,
    /// start the read-only sync burst, and open the control link in the
    /// background.
    ///
    /// Returns once the stream is established; the state fills in as the
    /// device's replies and the dump stream back. Subscribe *before* awaiting
    /// fresh events.
    pub async fn connect(ip: Ipv4Addr) -> Result<DeviceModel, SessionError> {
        Self::connect_with(ip, ConnectOptions::default()).await
    }

    /// Connect with explicit [`ConnectOptions`].
    ///
    /// The stream is dialed, handshaken and given its preamble before this
    /// returns — any of those failing is the error. Then, in order:
    /// [`DeviceState::channels`]`.stream` is [`ChannelState::Open`], the
    /// connection is [`Connection::Connected`], the [`DeviceEvent::Connected`]
    /// and [`DeviceEvent::ConnectionChanged`] events and the first snapshot are
    /// broadcast, ingest starts, the sync burst is started (not awaited), and
    /// — unless the policy is [`ControlPolicy::Off`] — the control link is
    /// opened: in the background for [`ControlPolicy::BestEffort`], before
    /// returning for [`ControlPolicy::Required`], whose failure closes the
    /// stream again and is returned.
    ///
    /// The control link is opened after the stream through the same connection
    /// ledger every [`Session`](crate::session::Session) passes, so the two
    /// opens are spaced by [`crate::session::CONNECTION_COOLDOWN`] without any
    /// sleeping here.
    pub async fn connect_with(
        ip: Ipv4Addr,
        opts: ConnectOptions,
    ) -> Result<DeviceModel, SessionError> {
        let shared = supervisor::connect(ip, opts).await?;
        Ok(DeviceModel {
            handle: Arc::new(Handle { shared }),
        })
    }

    /// Close both links, cancel every task, and raise
    /// [`DeviceEvent::Disconnected`] and [`DeviceEvent::ConnectionChanged`].
    ///
    /// Nothing else finishes: the receivers handed out by
    /// [`subscribe`](Self::subscribe) and [`events`](Self::events) stay open
    /// and simply see no more items. Idempotent — a second call does nothing.
    ///
    /// Awaits the sockets closing before it returns — and with them the
    /// connection ledger's close stamp — so a `connect_with` issued straight
    /// after `close()` cannot slip past the ledger before this close is
    /// recorded. (Dropping the last handle instead tears down the same way but
    /// synchronously, without waiting.)
    pub async fn close(&self) {
        self.handle.shared.close().await;
    }

    /// Open the control link again, explicitly, after it failed or was lost.
    ///
    /// Refused with [`ChannelError::Off`] under [`ControlPolicy::Off`], with
    /// [`ChannelError::TooSoon`] inside [`generated::CONTROL_REOPEN_MIN_GAP_MS`]
    /// of the last attempt to open it, and with [`ChannelError::Disconnected`]
    /// when the stream is not open. Resolves once the link is open (or at once
    /// if it already is, or is being opened), or with the transport's error.
    pub async fn reopen_control(&self) -> Result<(), ChannelError> {
        supervisor::reopen_control(&self.handle.shared).await
    }

    /// Subscribe to the **store**: a fresh [`DeviceState`] snapshot each time
    /// *slow* state changes, coalesced to at most one per ingested chunk —
    /// one read of the stream, or the whole state dump. This is the channel a
    /// UI binds to for re-rendering; the FAST meter lane is polled separately
    /// via [`status`](Self::status).
    ///
    /// Joining broadcasts one fresh snapshot to every subscriber, so the new
    /// one starts from the current state rather than waiting for the next
    /// change. After that each subscriber receives every snapshot broadcast (a
    /// lagging slow reader may drop intermediate snapshots, but the latest is
    /// always a complete state, so no information is lost).
    pub fn subscribe(&self) -> broadcast::Receiver<DeviceState> {
        self.handle.shared.core.subscribe()
    }

    /// Subscribe to the granular delta stream — every [`DeviceEvent`], including
    /// the FAST ones (`Status`, `BeatPulse`, `TunerDeviance`). For callers that
    /// want per-message deltas rather than coalesced snapshots.
    pub fn events(&self) -> broadcast::Receiver<DeviceEvent> {
        self.handle.shared.core.events()
    }

    /// A cloned snapshot of the current state under the read lock (`getState`).
    pub fn state(&self) -> DeviceState {
        self.handle.shared.core.state()
    }

    /// The latest FAST meter/tuner frame — poll this per animation frame. Equals
    /// `self.state().status`, but skips cloning the whole tree.
    pub fn status(&self) -> RealtimeStatus {
        self.handle.shared.core.status()
    }

    // ------------------------------------------------------------------
    // Parameters — NRPN `$01`, 14-bit (0–16383), state-tracked.
    // ------------------------------------------------------------------

    /// Set the amp Gain, 0–16383 (NRPN `0x0A/4`). Tracked parameter.
    pub async fn set_gain(&self, v: u16) -> Result<(), CommandError> {
        self.set_param(AMP_PAGE, GAIN_NUMBER, v).await
    }

    /// Set the Rig Volume, 0–16383 (NRPN `0x04/1`). Tracked parameter.
    pub async fn set_rig_volume(&self, v: u16) -> Result<(), CommandError> {
        self.set_param(PAGE_RIG_SETTINGS, RIG_VOLUME_NUMBER, v)
            .await
    }

    /// Set the Main Output Volume, 0–16383 (NRPN `0x7F/0`). Tracked parameter.
    pub async fn set_main_volume(&self, v: u16) -> Result<(), CommandError> {
        self.set_param(SYSTEM_PAGE, MAIN_VOL_NUMBER, v).await
    }

    /// Set the Monitor Output Volume, 0–16383 (NRPN `0x7F/2`). Tracked parameter.
    pub async fn set_monitor_volume(&self, v: u16) -> Result<(), CommandError> {
        self.set_param(SYSTEM_PAGE, MONITOR_VOL_NUMBER, v).await
    }

    /// Turn an effect slot (A/B/C/D/X/MOD/DLY/REV, case-insensitive) on or off
    /// via a `$01` write to number 3. Tracked parameter — folds into the slot's
    /// `on` state. Errors with [`CommandError::UnknownSlot`] on an unknown name.
    pub async fn set_effect_enabled(&self, slot: &str, on: bool) -> Result<(), CommandError> {
        let page = params::effect_slot_page(slot)
            .ok_or_else(|| CommandError::UnknownSlot(slot.to_string()))?;
        self.set_param(page, EFFECT_PARAM_STATE, u16::from(on))
            .await
    }

    /// Set an effect slot's dry/wet Mix, 0–16383 (NRPN `<slot>/4`). Tracked
    /// parameter. Errors with [`CommandError::UnknownSlot`] on an unknown name.
    pub async fn set_effect_mix(&self, slot: &str, v: u16) -> Result<(), CommandError> {
        let page = params::effect_slot_page(slot)
            .ok_or_else(|| CommandError::UnknownSlot(slot.to_string()))?;
        self.set_param(page, EFFECT_PARAM_MIX, v).await
    }

    /// Set the Tempo in whole beats per minute (NRPN `0x04/0`). The wire value
    /// is `bpm * 64`, saturating-multiplied and clamped to the 14-bit maximum
    /// (≈255 BPM). Tracked parameter; the momentary counterpart is
    /// [`tap_tempo`](Self::tap_tempo).
    pub async fn set_tempo_bpm(&self, bpm: u16) -> Result<(), CommandError> {
        let value = bpm.saturating_mul(TEMPO_BPM_SCALE).min(NRPN_MAX);
        self.set_param(PAGE_RIG_SETTINGS, TEMPO_NUMBER, value).await
    }

    /// Set an arbitrary numeric parameter (`$01` Single Parameter Change) — the
    /// escape hatch for any address without a named setter. Mutating; the
    /// backbone every parameter setter above routes through.
    pub async fn set_param(&self, page: u8, number: u8, value: u16) -> Result<(), CommandError> {
        self.enqueue(set_single(PRODUCT, DEVICE, page, number, value))
            .await
    }

    // ------------------------------------------------------------------
    // Requests — read-only, through the stream's request lane.
    // ------------------------------------------------------------------

    /// Ask for every value the routing table marks `request = true`: the rig
    /// strings, each effect slot's Type and On/Off, the bank preview, the
    /// position, and the requested numerics (amp on, gain, tempo, rig volume,
    /// the output volumes) — 46 requests, paced by the lane. The replies fold
    /// into the snapshot as they land. This is the connect-time sync under
    /// [`SyncStrategy::StreamBurst`].
    ///
    /// `Ok` once every request was answered; [`RequestError::Timeout`] if any
    /// went unanswered (the others still landed);
    /// [`RequestError::Disconnected`] if the stream went away.
    pub async fn refresh(&self) -> Result<(), RequestError> {
        lane::refresh(&self.handle.shared, |_| true).await
    }

    /// The rig subset of [`refresh`](Self::refresh): the rig strings and every
    /// effect slot's Type and On/Off. Read-only.
    pub async fn refresh_rig(&self) -> Result<(), RequestError> {
        lane::refresh(&self.handle.shared, |row| {
            matches!(
                row.field,
                Field::RigName
                    | Field::RigAuthor
                    | Field::RigDate
                    | Field::RigComment
                    | Field::AmpName
                    | Field::CabinetName
                    | Field::EffectType
                    | Field::EffectOn
            )
        })
        .await
    }

    /// The bank subset of [`refresh`](Self::refresh): the current bank's
    /// five-slot name preview (rig / amp / cabinet names), as extended strings
    /// (`$47`). The `$07` replies fold into [`DeviceState::bank`]. Read-only.
    /// The device also pushes this block unasked on a bank change, so a
    /// controller need only call this once at connect.
    pub async fn refresh_bank(&self) -> Result<(), RequestError> {
        lane::refresh(&self.handle.shared, |row| {
            matches!(
                row.field,
                Field::BankRigName | Field::BankAmpName | Field::BankCabinetName
            )
        })
        .await
    }

    /// The position subset of [`refresh`](Self::refresh): the current bank and
    /// rig slot, as two `$46` extended-parameter requests. The `$06` replies
    /// fold into [`DeviceState::current_bank`] and
    /// [`DeviceState::current_rig_slot`]. Read-only.
    ///
    /// Only needed once, at connect: the device pushes an unsolicited `$06` for
    /// whichever of the two changed on every subsequent rig change, whoever
    /// caused it.
    pub async fn refresh_position(&self) -> Result<(), RequestError> {
        lane::refresh(&self.handle.shared, |row| {
            matches!(row.field, Field::CurrentBank | Field::CurrentRigSlot)
        })
        .await
    }

    /// Request one numeric parameter's current value (function `$41`) and
    /// return it. The device answers with a `$01` at the same address, which
    /// folds into the snapshot on its way here. Read-only: it changes nothing
    /// on the device. This is the read-back to issue after a
    /// [`set_param`](Self::set_param), which the device applies without echoing.
    ///
    /// The reply is whichever value next lands at the address — an unsolicited
    /// push there is equally current and resolves it too.
    /// [`RequestError::Unreadable`] at once, without sending, for an address
    /// the stream cannot read (the morph position).
    pub async fn request_param(&self, page: u8, number: u8) -> Result<u16, RequestError> {
        let address = flat(page, number);
        let reply = self
            .request(
                PendingKey::Num(address),
                request_single(PRODUCT, DEVICE, page, number),
            )
            .await?;
        // A `$01` reply is 14 bits; only a value from another wire could ever
        // be wider, and one that does not fit the stream's word is not the
        // stream's answer.
        // A `$01` reply is a 14-bit word; a wider number at this address can
        // only have come over the control wire, and is not the stream's answer.
        u16::try_from(reply.num())
            .ok()
            .filter(|v| *v <= NRPN_MAX)
            .ok_or(RequestError::Unreadable)
    }

    /// Request one string parameter (function `$43`) and return it — a page-0
    /// string tag such as the Rig Name. The `$03` reply folds into the
    /// snapshot on its way here. Read-only.
    pub async fn request_string(&self, page: u8, number: u8) -> Result<String, RequestError> {
        let address = flat(page, number);
        let reply = self
            .request(
                PendingKey::Text(address),
                request_string(PRODUCT, DEVICE, page, number),
            )
            .await?;
        Ok(reply.text())
    }

    /// Request an extended-address numeric parameter (function `$46`) and
    /// return its `$06` reply — how the device's position is read, at
    /// [`generated::CURRENT_BANK_ADDRESS`] and
    /// [`generated::CURRENT_RIG_SLOT_ADDRESS`]. Read-only.
    pub async fn request_ext_param(&self, address: u32) -> Result<u64, RequestError> {
        let reply = self
            .request(
                PendingKey::Num(address),
                request_extended_param(PRODUCT, DEVICE, address),
            )
            .await?;
        Ok(reply.num())
    }

    /// Request an extended-address string parameter (function `$47`) and
    /// return its `$07` reply — how the bank preview names are read, at
    /// [`crate::params::bank_preview_address`]. Read-only.
    pub async fn request_ext_string(&self, address: u32) -> Result<String, RequestError> {
        let reply = self
            .request(
                PendingKey::Text(address),
                request_extended_string(PRODUCT, DEVICE, address),
            )
            .await?;
        Ok(reply.text())
    }

    /// Request a parameter value rendered to its exact display text (function
    /// `$7C`) — ask the device for the string a value shows on screen (e.g.
    /// `"5.2"`, `"120 BPM"`, `"<0.0>"`) instead of a generic percentage — and
    /// return it. The `$3C` reply is also raised as
    /// [`DeviceEvent::RenderedString`]; it is not stored in the tree.
    /// Read-only, but costly in device CPU.
    pub async fn request_render(
        &self,
        page: u8,
        number: u8,
        value: u16,
    ) -> Result<String, RequestError> {
        let reply = self
            .request(
                PendingKey::Render {
                    page,
                    number,
                    value,
                },
                request_rendered_string(PRODUCT, DEVICE, page, number, value),
            )
            .await?;
        Ok(reply.text())
    }

    /// One request through the lane, whatever its shape.
    async fn request(&self, key: PendingKey, bytes: Vec<u8>) -> Result<Reply, RequestError> {
        lane::request(&self.handle.shared, key, bytes).await
    }

    // ------------------------------------------------------------------
    // Actions — CC, momentary/expression, NOT stored in state.
    // ------------------------------------------------------------------

    /// Send an arbitrary [`Control`] on the command channel (MIDI channel 1).
    /// The generic entry point behind every action convenience method below.
    ///
    /// Refused with [`CommandError::RigLoadRequiresNavigator`], before a
    /// byte goes out, for anything that loads a rig — [`Control::LoadSlot`],
    /// [`Control::Up`], [`Control::Down`], [`Control::ProgramChange`] and
    /// [`Control::BankSelect`]: those go through
    /// [`navigate_to`](Self::navigate_to) and its conveniences, so that two
    /// loads can never overlap. [`Control::BankPreselect`] alone is allowed;
    /// it loads nothing.
    pub async fn send_control(&self, c: control::Control) -> Result<(), CommandError> {
        if matches!(
            c,
            Control::LoadSlot(_)
                | Control::Up
                | Control::Down
                | Control::ProgramChange(_)
                | Control::BankSelect { .. }
        ) {
            return Err(CommandError::RigLoadRequiresNavigator);
        }
        self.enqueue(c.message(CC_CHANNEL)).await
    }

    // ------------------------------------------------------------------
    // Navigation — rig loads, one at a time, through the Navigator.
    // ------------------------------------------------------------------

    /// Aim at a rig by its flat, 0-based index — the device's own numbering,
    /// and the only address that reaches a rig outside the current bank.
    /// Returns at once; nothing here waits for the device.
    ///
    /// The aim lands immediately in [`DeviceState::navigation`], so a slot
    /// highlight or a position readout answers every tap; only the sending
    /// is rationed. If no load is in flight the pair goes out now — the bank
    /// preselect (CC47) for `index / BANK_SLOTS`, then the slot load
    /// (CC50–54) that commits it. If one is, the aim simply moves, and once
    /// that load has settled ([`generated::RIG_LOAD_SETTLE_MS`]) wherever
    /// the aim ended up is sent — so a burst of taps costs two loads however
    /// long it is, and two loads never overlap.
    ///
    /// Nothing here assumes how many rigs a device has. Aim past the end and
    /// it stays where it is and says so in its position push; the aim is
    /// kept for [`generated::PENDING_WINDOW_MS`] after the move settled and
    /// then dropped with [`DeviceEvent::NavigationDropped`]. A matching
    /// position report, from either wire, retires the aim with
    /// [`DeviceEvent::NavigationSettled`]. With the stream down the aim is
    /// dropped at once, with the same event.
    pub fn navigate_to(&self, index: u16) {
        nav::navigate(&self.handle.shared, index);
    }

    /// Aim `delta` rigs from [`DeviceState::aimed_rig_index`] — the aim if
    /// there is one, else where the device says it is — floored at 0. ±1 is
    /// the next or previous rig, ±[`generated::BANK_SLOTS`] the same slot a
    /// bank over. Ignored while no position is known: there is nothing to
    /// step *from*, and doing nothing beats a guess. A step that lands where
    /// the aim already is sends nothing.
    pub fn step_rig(&self, delta: i32) {
        let Some(current) = self.state().aimed_rig_index() else {
            return;
        };
        let target = (i32::from(current) + delta).clamp(0, i32::from(u16::MAX)) as u16;
        if target != current {
            self.navigate_to(target);
        }
    }

    /// Aim one bank up or down, keeping the slot:
    /// [`step_rig`](Self::step_rig) by ±[`generated::BANK_SLOTS`].
    pub fn step_bank(&self, forward: bool) {
        let slots = generated::BANK_SLOTS as i32;
        self.step_rig(if forward { slots } else { -slots });
    }

    /// Aim at slot `slot` (1..=[`generated::BANK_SLOTS`]) of the bank the
    /// model is aiming at — the aim's bank if there is one, else the
    /// device's own. That is the bank a Bank Up tapped a moment ago is
    /// heading for, which the device's own position would not yet show.
    /// Ignored for a slot out of range, and while no position is known —
    /// there is no bank to name.
    pub fn select_slot(&self, slot: u8) {
        let slots = generated::BANK_SLOTS as u16;
        if slot == 0 || u16::from(slot) > slots {
            return;
        }
        let Some(current) = self.state().aimed_rig_index() else {
            return;
        };
        self.navigate_to(current / slots * slots + u16::from(slot) - 1);
    }

    /// Preselect bank `n` (1-based; CC47). Loads nothing: the preselect is
    /// armed on the device and takes effect with the next slot load, which
    /// only the Navigator sends — and which sends its own preselect. Kept as
    /// the raw action for callers that drive the device's front panel from
    /// afar.
    pub async fn bank(&self, n: u16) -> Result<(), CommandError> {
        self.send_control(Control::BankPreselect(n.saturating_sub(1) as u8))
            .await
    }

    /// Tap the tempo (CC30). Mutating — advances the tap-tempo clock.
    pub async fn tap_tempo(&self) -> Result<(), CommandError> {
        self.send_control(Control::TapTempo).await
    }

    /// Open (`true`) or close (`false`) the tuner (CC31). Mutating.
    pub async fn tuner_mode(&self, open: bool) -> Result<(), CommandError> {
        self.send_control(Control::TunerMode(open)).await
    }

    /// Morph button (CC80): `rise` = rise to the morph target, else fall back.
    /// Mutating — moves the morph state.
    pub async fn morph_button(&self, rise: bool) -> Result<(), CommandError> {
        self.send_control(Control::MorphButton(rise)).await
    }

    /// Set the morph pedal position 0–127 (CC11). Mutating.
    pub async fn morph_pedal(&self, v: u8) -> Result<(), CommandError> {
        self.send_control(Control::MorphPedal(v)).await
    }

    /// Delay + Reverb Freeze (CC35): `on` engages the freeze. Mutating.
    pub async fn freeze(&self, on: bool) -> Result<(), CommandError> {
        self.send_control(Control::Freeze(on)).await
    }

    /// Rotary speaker speed (CC33): `fast` = fast, else slow. Mutating.
    pub async fn rotary_fast(&self, fast: bool) -> Result<(), CommandError> {
        self.send_control(Control::RotaryFast(fast)).await
    }

    /// Delay Infinity (CC34): `on` holds the delay indefinitely. Mutating.
    pub async fn delay_infinity(&self, on: bool) -> Result<(), CommandError> {
        self.send_control(Control::DelayInfinity(on)).await
    }

    /// Toggle every module A–REV on/off (CC16). Mutating.
    pub async fn toggle_all_modules(&self) -> Result<(), CommandError> {
        self.send_control(Control::ToggleAllModules).await
    }

    /// Press Effect Button `n` (I–IIII, clamped to 1..=4; CC75–78). Mutating.
    pub async fn effect_button(&self, n: u8) -> Result<(), CommandError> {
        self.send_control(Control::EffectButton(n)).await
    }

    /// Set the wah pedal position 0–127 (CC1). Mutating.
    pub async fn wah_pedal(&self, v: u8) -> Result<(), CommandError> {
        self.send_control(Control::WahPedal(v)).await
    }

    /// Set the pitch pedal position 0–127 (CC4). Mutating.
    pub async fn pitch_pedal(&self, v: u8) -> Result<(), CommandError> {
        self.send_control(Control::PitchPedal(v)).await
    }

    /// Set the volume pedal position 0–127 (CC7). Mutating.
    pub async fn volume_pedal(&self, v: u8) -> Result<(), CommandError> {
        self.send_control(Control::VolumePedal(v)).await
    }

    /// Set the panorama 0–127 (CC10). Mutating.
    pub async fn panorama(&self, v: u8) -> Result<(), CommandError> {
        self.send_control(Control::Panorama(v)).await
    }

    /// Write raw, pre-framing MIDI bytes to the stream — any message at all,
    /// framed by the writer like everything else. The escape hatch beneath
    /// [`send_control`](Self::send_control) and [`set_param`](Self::set_param);
    /// nothing is tracked, and only one thing is checked: bytes that would
    /// load a rig — a Program Change (status `0xC0..=0xCF`), or a Control
    /// Change whose controller is one of
    /// [`generated::RIG_LOAD_CONTROLLERS`] — are refused with
    /// [`CommandError::RigLoadRequiresNavigator`] before anything is
    /// written. Every status byte in the buffer is looked at, so a load
    /// cannot ride in behind another message.
    pub async fn send_raw(&self, midi: &[u8]) -> Result<(), CommandError> {
        if loads_a_rig(midi) {
            return Err(CommandError::RigLoadRequiresNavigator);
        }
        self.enqueue(midi.to_vec()).await
    }

    /// Send the bidirectional beacon (function `$7E`): ask the device to stream
    /// its default parameter set, with the tuner if `tuner`, and to keep doing
    /// so for `lease_secs`. Re-send within half the lease to keep it alive;
    /// `init` marks the first beacon of a session.
    pub async fn send_beacon(
        &self,
        init: bool,
        tuner: bool,
        lease_secs: u8,
    ) -> Result<(), CommandError> {
        self.enqueue(nrpn::beacon(
            init,
            tuner,
            lease_secs,
            generated::BEACON_DEFAULT_PARAM_SET,
            PRODUCT,
        ))
        .await
    }

    /// Fold one CBOR value into the tree as a live control-channel push.
    ///
    /// Kept only for callers that fed the model from their own
    /// [`CborSession`](crate::cbor::CborSession); the model's own control link
    /// folds the same values itself, so there is nothing left to hand it.
    #[doc(hidden)]
    #[deprecated(
        since = "0.2.0",
        note = "the model opens and folds its own control link; see ConnectOptions::control"
    )]
    pub fn apply_cbor(&self, address: u32, value: i64) {
        let Ok(value) = u64::try_from(value) else {
            return;
        };
        let shared = &self.handle.shared;
        links::fold_updates(
            shared,
            shared.core.epoch(),
            &[Update {
                source: Channel::Control,
                phase: Phase::Live,
                address,
                decoded: Decoded::Num(value),
            }],
        );
    }

    /// Enqueue raw (pre-framing) MIDI bytes for the stream's writer.
    async fn enqueue(&self, bytes: Vec<u8>) -> Result<(), CommandError> {
        self.handle.shared.enqueue(bytes).await
    }

    /// Test hook: forget when the control link was last attempted, so
    /// [`reopen_control`](Self::reopen_control) is not refused as
    /// [`ChannelError::TooSoon`]. The reopen gap
    /// ([`generated::CONTROL_REOPEN_MIN_GAP_MS`]) is far longer than any test
    /// can wait, and there is no other way to reach the success path.
    #[doc(hidden)]
    pub fn clear_control_attempt_for_tests(&self) {
        self.handle.shared.clear_control_attempt();
    }
}

/// The flat address of a page/number pair.
fn flat(page: u8, number: u8) -> u32 {
    u32::from(page) * 128 + u32::from(number)
}

/// Whether raw MIDI bytes would load a rig: a Program Change status, or a
/// Control Change on one of [`generated::RIG_LOAD_CONTROLLERS`]. Every
/// status byte is examined, wherever it sits in the buffer; the data bytes
/// between them are all below `0x80` and cannot be mistaken for one.
fn loads_a_rig(midi: &[u8]) -> bool {
    midi.iter().enumerate().any(|(i, &b)| match b & 0xF0 {
        generated::PROGRAM_CHANGE_STATUS => true,
        generated::CONTROL_CHANGE_STATUS => midi
            .get(i + 1)
            .is_some_and(|c| generated::RIG_LOAD_CONTROLLERS.contains(c)),
        _ => false,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn raw_bytes_that_load_a_rig_are_recognised() {
        assert!(loads_a_rig(&[0xC0, 5]));
        assert!(loads_a_rig(&[0xCF, 0]));
        for controller in generated::RIG_LOAD_CONTROLLERS {
            assert!(loads_a_rig(&[0xB0, controller, 1]));
        }
        // A load riding in behind a harmless message is still a load.
        assert!(loads_a_rig(&[0xB0, 0x1E, 1, 0xB0, 0x32, 1]));
        // The preselect loads nothing, and neither does anything else.
        assert!(!loads_a_rig(&Control::BankPreselect(2).message(0)));
        assert!(!loads_a_rig(&Control::TapTempo.message(0)));
        assert!(!loads_a_rig(&nrpn::set_single(0, 0x7F, 0x0A, 4, 100)));
        assert!(!loads_a_rig(&[]));
    }
}
