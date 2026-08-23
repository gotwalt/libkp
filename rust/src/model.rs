//! `DeviceModel` — a **React-style store** over a Profiler's live stream: a
//! typed immutable [`DeviceState`] snapshot bag the UI binds to, plus a command
//! channel for writes.
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
//!   [`DeviceModel::connect`] opens a [`Session`], spawns an ingest task that owns
//!   the socket and an [`Unframer`], and kicks off a read-only initial sync. The
//!   task applies each decoded message to a shared [`DeviceState`].
//!
//! # This is a store
//!
//! Three access points, mirroring a React store:
//!
//! - [`state`](DeviceModel::state) — `getState`: the current snapshot.
//! - [`subscribe`](DeviceModel::subscribe) — the **store**: a fresh snapshot is
//!   emitted only when *slow* state changed, coalesced to at most once per
//!   ingested stream chunk.
//! - [`events`](DeviceModel::events) — the granular delta stream (every
//!   [`DeviceEvent`], including the fast ones), for callers that want deltas.
//! - [`status`](DeviceModel::status) — the **fast lane**: poll this per
//!   animation frame for the meter/tuner frame (equals `state().status`).
//!
//! State is classified FAST vs SLOW. **FAST** = the meter [`RealtimeStatus`]
//! block, the beat pulse, and tuner deviance — high-rate, poll them via
//! [`status`](DeviceModel::status). **SLOW** = everything else (rig / amp / cab /
//! effect / output / tempo / morph / tuner-note / connection): these drive the
//! coalesced [`subscribe`](DeviceModel::subscribe) snapshot.
//!
//! # Parameters vs Actions
//!
//! [`DeviceModel`]'s command methods split into two groups:
//!
//! - **Parameters** (`set_*`) — settable values the device stores. They go out
//!   as 14-bit NRPN `$01` Single Parameter Changes (via
//!   [`crate::nrpn::set_single`]); the device applies the write silently and
//!   does *not* echo it back on the stream, so follow a set with
//!   [`request_param`](DeviceModel::request_param) when [`DeviceModel::state`]
//!   should confirm the new value — the `$41` reply flows through normal
//!   ingest. The setters are:
//!   [`set_gain`](DeviceModel::set_gain),
//!   [`set_rig_volume`](DeviceModel::set_rig_volume),
//!   [`set_main_volume`](DeviceModel::set_main_volume),
//!   [`set_monitor_volume`](DeviceModel::set_monitor_volume),
//!   [`set_effect_enabled`](DeviceModel::set_effect_enabled),
//!   [`set_effect_mix`](DeviceModel::set_effect_mix),
//!   [`set_tempo_bpm`](DeviceModel::set_tempo_bpm), and the escape hatch
//!   [`set_param`](DeviceModel::set_param).
//! - **Actions** (verbs) — momentary presses and live expression that carry no
//!   stored value. They go out as 7-bit Control Change messages via the
//!   [`crate::control`] vocabulary and are *not* reflected in state:
//!   [`select_rig`](DeviceModel::select_rig),
//!   [`rig_up`](DeviceModel::rig_up), [`rig_down`](DeviceModel::rig_down),
//!   [`bank`](DeviceModel::bank), [`tap_tempo`](DeviceModel::tap_tempo),
//!   [`tuner_mode`](DeviceModel::tuner_mode), the buttons and pedals, and the
//!   escape hatch [`send_control`](DeviceModel::send_control).
//!
//! Power users reach the full raw vocabulary through
//! [`send_control`](DeviceModel::send_control) and the [`crate::control`]
//! module; any address at all through [`set_param`](DeviceModel::set_param).

use std::net::Ipv4Addr;
use std::sync::{Arc, RwLock};
use std::time::Duration;

use tokio::sync::{broadcast, mpsc};

use crate::control::{self, Control};
use crate::error::SessionError;
use crate::generated;
use crate::midi3::{self, Unframer};
use crate::nrpn::{
    self, PAGE_STRINGS, request_extended_param, request_extended_string, request_rendered_string,
    request_single, request_string, set_single,
};
use crate::params::{self, EFFECT_PARAM_MIX, EFFECT_PARAM_STATE, EFFECT_PARAM_TYPE};
use crate::session::{PROTOCOL_MIDI3_STREAM, Session};
use crate::state::{Connection, DeviceState};

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
/// Read idle gap driving the ingest loop; short so it reacts per packet.
const READ_IDLE: Duration = Duration::from_millis(30);
/// Max bytes per stream read.
const READ_MAX: usize = 64 * 1024;

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
    /// Only a CBOR session ever sees this: the position is never sent on the
    /// MIDI3 stream, even while a morph is ramping. See [`Self::MorphButton`].
    MorphChanged(u16),
    /// The morph button was pressed (`true`) or released (`false`) — momentary,
    /// so nothing about it is stored in the snapshot.
    ///
    /// This is what a MIDI3 client sees of a morph: *that* one happened, and
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
    /// the transient response to [`DeviceModel::request_render`]; it is not
    /// stored in the snapshot tree.
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
    /// The model connected to a device.
    Connected,
    /// The device closed the connection.
    Disconnected,
}

/// The result of applying one update to a [`DeviceState`] — one MIDI message,
/// one CBOR item, or one [`Update`](crate::state::Update) through the funnel:
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
    /// The ingest task has ended, so the command channel is closed.
    #[error("device model is disconnected; command channel closed")]
    Disconnected,
    /// An effect-slot name did not match A/B/C/D/X/MOD/DLY/REV.
    #[error("unknown effect slot {0:?}; use A B C D X MOD DLY REV")]
    UnknownSlot(String),
}

/// An async handle to a live [`DeviceState`] store synced from a Profiler's
/// stream.
///
/// Cheap to clone: all clones share one ingest task, state cache, the snapshot
/// and event broadcasts, and the command channel.
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
    state: Arc<RwLock<DeviceState>>,
    snapshots: broadcast::Sender<DeviceState>,
    events: broadcast::Sender<DeviceEvent>,
    commands: mpsc::Sender<Vec<u8>>,
}

impl DeviceModel {
    /// Connect to `ip:5727`, open the streaming protocol, spawn the ingest task,
    /// and kick off a read-only initial sync (rig strings + effect slots).
    ///
    /// Returns once the session is established; the state fills in as the
    /// device's replies stream back. Subscribe *before* awaiting fresh events.
    pub async fn connect(ip: Ipv4Addr) -> Result<DeviceModel, SessionError> {
        let mut session = Session::connect(ip).await?;
        let outcome = session
            .handshake(&[PROTOCOL_MIDI3_STREAM], READ_IDLE)
            .await?;
        session.write_session_preamble().await?;
        let tail = outcome.response_tail().to_vec();

        let state = Arc::new(RwLock::new(DeviceState::new()));
        // Connecting is a SLOW transition: flip the flag, then broadcast the
        // first snapshot and the granular Connected event.
        write_state(&state).connection = Connection::Connected;
        let (snapshots_tx, _) = broadcast::channel(256);
        let (events_tx, _) = broadcast::channel(1024);
        let (commands_tx, commands_rx) = mpsc::channel(64);
        let _ = events_tx.send(DeviceEvent::Connected);
        let _ = snapshots_tx.send(read_state(&state).clone());

        spawn_ingest(
            session,
            state.clone(),
            snapshots_tx.clone(),
            events_tx.clone(),
            commands_rx,
            tail,
        );

        let model = DeviceModel {
            state,
            snapshots: snapshots_tx,
            events: events_tx,
            commands: commands_tx,
        };
        // Read-only sync so the snapshot populates from the replies.
        let _ = model.refresh_rig().await;
        let _ = model.refresh_bank().await;
        let _ = model.refresh_position().await;
        Ok(model)
    }

    /// Subscribe to the **store**: a fresh [`DeviceState`] snapshot each time
    /// *slow* state changes, coalesced to at most one per ingested stream chunk.
    /// This is the channel a UI binds to for re-rendering; the FAST meter lane is
    /// polled separately via [`status`](Self::status).
    ///
    /// Each subscriber receives every snapshot broadcast after it subscribed (a
    /// lagging slow reader may drop intermediate snapshots, but the latest is
    /// always a complete state, so no information is lost).
    pub fn subscribe(&self) -> broadcast::Receiver<DeviceState> {
        self.snapshots.subscribe()
    }

    /// Subscribe to the granular delta stream — every [`DeviceEvent`], including
    /// the FAST ones (`Status`, `BeatPulse`, `TunerDeviance`). For callers that
    /// want per-message deltas rather than coalesced snapshots.
    pub fn events(&self) -> broadcast::Receiver<DeviceEvent> {
        self.events.subscribe()
    }

    /// A cloned snapshot of the current state under the read lock (`getState`).
    pub fn state(&self) -> DeviceState {
        read_state(&self.state).clone()
    }

    /// The latest FAST meter/tuner frame — poll this per animation frame. Equals
    /// `self.state().status`, but skips cloning the whole tree.
    pub fn status(&self) -> RealtimeStatus {
        read_state(&self.state).status
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

    /// Re-request the rig strings and every effect slot's Type/State (read-only).
    /// Also the initial sync run at connect time. Neither a parameter nor an
    /// action: it only issues value *requests* and changes nothing on the device.
    pub async fn refresh_rig(&self) -> Result<(), CommandError> {
        // Rig Name, Author, Comment, Date, Amp Name, Cab Name.
        for tag in [1u8, 2, 4, 3, 10, 32] {
            self.enqueue(request_string(PRODUCT, DEVICE, PAGE_STRINGS, tag))
                .await?;
        }
        for (_, page) in params::effect_slots() {
            self.enqueue(request_single(PRODUCT, DEVICE, *page, EFFECT_PARAM_TYPE))
                .await?;
            self.enqueue(request_single(PRODUCT, DEVICE, *page, EFFECT_PARAM_STATE))
                .await?;
        }
        Ok(())
    }

    /// Request the current bank's five-slot name preview (rig / amp / cabinet
    /// names) as extended strings (`$47`). The `$07` replies fold into
    /// [`DeviceState::bank`]. Read-only: it changes nothing on the device. The
    /// device also pushes this block unasked on a bank change, so a controller
    /// need only call this once at connect.
    pub async fn refresh_bank(&self) -> Result<(), CommandError> {
        use params::BankPreviewField::{AmpName, CabinetName, RigName};
        for field in [RigName, AmpName, CabinetName] {
            for slot in 1..=params::BANK_SLOTS as u8 {
                let addr = params::bank_preview_address(field, slot);
                self.enqueue(request_extended_string(PRODUCT, DEVICE, addr))
                    .await?;
            }
        }
        Ok(())
    }

    /// Ask the device where it is: the current bank and rig slot, as two `$46`
    /// extended-parameter requests. The `$06` replies fold into
    /// [`DeviceState::current_bank`] and [`DeviceState::current_rig_slot`].
    /// Read-only.
    ///
    /// Only needed once, at connect: the device pushes an unsolicited `$06` for
    /// whichever of the two changed on every subsequent rig change, whoever
    /// caused it.
    pub async fn refresh_position(&self) -> Result<(), CommandError> {
        for address in [
            generated::CURRENT_BANK_ADDRESS,
            generated::CURRENT_RIG_SLOT_ADDRESS,
        ] {
            self.enqueue(request_extended_param(PRODUCT, DEVICE, address))
                .await?;
        }
        Ok(())
    }

    /// Fold one value from a [`CborSession`](crate::cbor::CborSession) into this
    /// model's state tree, emitting whatever events it raises and republishing
    /// the snapshot.
    ///
    /// The two channels are one event universe in two wire formats, so a client
    /// holding both hands the CBOR side's pushes here and reads a single tree.
    /// This is how the morph position reaches a model whose own session cannot
    /// carry it.
    pub fn apply_cbor(&self, address: u32, value: i64) {
        let outcome = write_state(&self.state).apply_cbor(address, value);
        for event in outcome.events {
            let _ = self.events.send(event);
        }
        if outcome.slow_changed {
            let _ = self.snapshots.send(read_state(&self.state).clone());
        }
    }

    /// Fold a [`StateSnapshot`](crate::cbor::StateSnapshot)'s current bank and
    /// rig slot into the state tree, emitting a
    /// [`DeviceEvent::CurrentPosition`] and broadcasting a fresh snapshot.
    ///
    /// For a client that already holds a `StateSnapshot` — read over the CBOR
    /// channel *before* this streaming session opened — this seeds the tree
    /// without waiting for a reply. A session that is already up should call
    /// [`refresh_position`](Self::refresh_position) instead and let the device
    /// answer. Only the `Some` fields overwrite; a `None` leaves the current
    /// value untouched.
    pub fn set_current_position(&self, bank: Option<u16>, slot: Option<u16>) {
        {
            let mut st = write_state(&self.state);
            if bank.is_some() {
                st.current_bank = bank;
            }
            if slot.is_some() {
                st.current_rig_slot = slot;
            }
        }
        let _ = self
            .events
            .send(DeviceEvent::CurrentPosition { bank, slot });
        let _ = self.snapshots.send(read_state(&self.state).clone());
    }

    /// Request one numeric parameter's current value (function `$41`). The
    /// device answers with a `$01` message on the same stream, which the ingest
    /// task folds into the snapshot. Read-only: it changes nothing on the
    /// device. This is the read-back to issue after a
    /// [`set_param`](Self::set_param), which the device applies without echoing.
    pub async fn request_param(&self, page: u8, number: u8) -> Result<(), CommandError> {
        self.enqueue(request_single(PRODUCT, DEVICE, page, number))
            .await
    }

    /// Request a parameter value rendered to its exact display text (function
    /// `$7C`) — ask the device for the string a value shows on screen (e.g.
    /// `"5.2"`, `"120 BPM"`, `"<0.0>"`) instead of a generic percentage.
    /// Read-only: it changes nothing on the device.
    ///
    /// This is a **streaming request/response**, consistent with the store
    /// model: it enqueues the request and returns immediately — there is no
    /// blocking correlation. Watch [`events`](Self::events) for the matching
    /// [`DeviceEvent::RenderedString`] carrying the rendered `text`.
    pub async fn request_render(
        &self,
        page: u8,
        number: u8,
        value: u16,
    ) -> Result<(), CommandError> {
        self.enqueue(request_rendered_string(
            PRODUCT, DEVICE, page, number, value,
        ))
        .await
    }

    // ------------------------------------------------------------------
    // Actions — CC, momentary/expression, NOT stored in state.
    // ------------------------------------------------------------------

    /// Send an arbitrary [`Control`] on the command channel ([`CC_CHANNEL`]).
    /// The generic entry point behind every action convenience method below.
    pub async fn send_control(&self, c: control::Control) -> Result<(), CommandError> {
        self.enqueue(c.message(CC_CHANNEL)).await
    }

    /// Select rig slot 1–5 in the current bank (CC50–54). Changes the rig.
    pub async fn select_rig(&self, rig: u8) -> Result<(), CommandError> {
        self.send_control(Control::LoadSlot(rig)).await
    }

    /// Step to the next rig (CC48). Changes the rig.
    pub async fn rig_up(&self) -> Result<(), CommandError> {
        self.send_control(Control::Up).await
    }

    /// Step to the previous rig (CC49). Changes the rig.
    pub async fn rig_down(&self) -> Result<(), CommandError> {
        self.send_control(Control::Down).await
    }

    /// Preselect bank `n` (1-based; CC47). Takes effect with the next rig.
    pub async fn bank(&self, n: u16) -> Result<(), CommandError> {
        self.send_control(Control::BankPreselect(n.saturating_sub(1) as u8))
            .await
    }

    /// Load a rig by its flat, 0-based index — the device's own numbering, and
    /// the only address that reaches a rig outside the current bank.
    ///
    /// Sent as the documented pair: the absolute bank preselect (CC47) followed
    /// by the slot load (CC50–54) that commits it. The index divides by
    /// [`generated::BANK_SLOTS`], so index 123 is bank 25, slot 4.
    ///
    /// Nothing here assumes how many banks a device has. Aim past the end and
    /// the device simply stays where it is — and says so in the `$06` position
    /// push that follows, so
    /// [`DeviceState::current_rig_index`](crate::state::DeviceState::current_rig_index)
    /// always reflects where it actually landed, not where this aimed.
    pub async fn select_rig_index(&self, index: u16) -> Result<(), CommandError> {
        let slots = generated::BANK_SLOTS as u16;
        self.bank(index / slots + 1).await?;
        self.select_rig((index % slots) as u8 + 1).await
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

    /// Enqueue raw (pre-framing) MIDI bytes for the ingest task to write.
    async fn enqueue(&self, bytes: Vec<u8>) -> Result<(), CommandError> {
        self.commands
            .send(bytes)
            .await
            .map_err(|_| CommandError::Disconnected)
    }
}

/// Spawn the task that owns the socket: reads and decodes the stream into the
/// shared state, drives the coalesced snapshot store + granular event broadcast,
/// and drains the command channel to the wire.
fn spawn_ingest(
    mut session: Session,
    state: Arc<RwLock<DeviceState>>,
    snapshots: broadcast::Sender<DeviceState>,
    events: broadcast::Sender<DeviceEvent>,
    mut commands: mpsc::Receiver<Vec<u8>>,
    tail: Vec<u8>,
) {
    tokio::spawn(async move {
        let mut unframer = Unframer::new();
        // Decode anything that rode in on the handshake acceptance tail.
        apply_chunk(&state, &snapshots, &events, unframer.push(&tail));
        loop {
            tokio::select! {
                read = session.read_once(READ_IDLE, READ_MAX) => match read {
                    Ok(chunk) => {
                        apply_chunk(&state, &snapshots, &events, unframer.push(&chunk));
                    }
                    // A closed/errored socket is a SLOW Disconnected transition.
                    Err(_) => {
                        disconnect(&state, &snapshots, &events);
                        break;
                    }
                },
                cmd = commands.recv() => match cmd {
                    Some(bytes) => {
                        if session.write_all(&midi3::frame(&bytes)).await.is_err() {
                            disconnect(&state, &snapshots, &events);
                            break;
                        }
                    }
                    // All handles dropped: nothing more can be requested.
                    None => break,
                },
            }
        }
    });
}

/// Apply every message in one ingested chunk: broadcast each granular event, and
/// — if any message changed a slow field — emit exactly one coalesced snapshot.
fn apply_chunk(
    state: &RwLock<DeviceState>,
    snapshots: &broadcast::Sender<DeviceState>,
    events: &broadcast::Sender<DeviceEvent>,
    msgs: Vec<Vec<u8>>,
) {
    let mut slow_changed = false;
    for msg in msgs {
        let outcome = write_state(state).apply(&msg);
        for ev in outcome.events {
            let _ = events.send(ev);
        }
        slow_changed |= outcome.slow_changed;
    }
    if slow_changed {
        let _ = snapshots.send(read_state(state).clone());
    }
}

/// Record the Disconnected transition: flip the flag, then broadcast a final
/// snapshot (a SLOW change) and the granular Disconnected event.
fn disconnect(
    state: &RwLock<DeviceState>,
    snapshots: &broadcast::Sender<DeviceState>,
    events: &broadcast::Sender<DeviceEvent>,
) {
    write_state(state).connection = Connection::Disconnected;
    let _ = events.send(DeviceEvent::Disconnected);
    let _ = snapshots.send(read_state(state).clone());
}

/// Acquire the write guard, recovering from a poisoned lock rather than panicking.
fn write_state(state: &RwLock<DeviceState>) -> std::sync::RwLockWriteGuard<'_, DeviceState> {
    match state.write() {
        Ok(g) => g,
        Err(poisoned) => poisoned.into_inner(),
    }
}

/// Acquire the read guard, recovering from a poisoned lock rather than panicking.
fn read_state(state: &RwLock<DeviceState>) -> std::sync::RwLockReadGuard<'_, DeviceState> {
    match state.read() {
        Ok(g) => g,
        Err(poisoned) => poisoned.into_inner(),
    }
}
