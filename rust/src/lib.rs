//! `libkp` — the Kemper Profiler network protocol.
//!
//! Layers:
//!
//! - [`generated`] — the data-only module (constants + lookup tables) produced
//!   from the shared `spec/`. Never edit it by hand.
//! - [`protocol`] — the tag-stream wire encoding + discovery poll packet.
//! - [`discovery`] — UDP broadcast discovery ([`discovery::discover`]).
//! - [`session`] — TCP connect + the line-based protocol handshake.
//! - [`midi3`] — the 4-byte stream framing that carries MIDI over the session.
//! - [`cbor`] — the device's native CBOR channel and the state-dump snapshot
//!   ([`cbor::StateSnapshot::fetch`]) that reads the current bank and rig slot.
//! - [`nrpn`] — Kemper SysEx/NRPN builders and parsers.
//! - [`control`] — the 7-bit CC / PC / Bank Select control vocabulary.
//! - [`params`] / [`registry`] — offline name and descriptor lookups.
//! - [`state`] / [`model`] — the immutable state tree and the observable store.
//! - [`error`] — error types.
//!
//! Discovery and the TCP session share one port, [`PORT`] (5727).
//!
//! Most callers want [`model::DeviceModel`] — the curated, state-consistent
//! handle. Its commands split into **parameters** (NRPN-backed, tracked in
//! state) and **actions** (CC-backed, momentary). The [`control`] module is the
//! full raw CC vocabulary behind [`model::DeviceModel::send_control`].
//!
//! ```no_run
//! use std::net::Ipv4Addr;
//! use libkp::model::DeviceModel;
//! use libkp::control::Control;
//!
//! # async fn run() -> Result<(), Box<dyn std::error::Error>> {
//! let model = DeviceModel::connect(Ipv4Addr::new(192, 168, 1, 50)).await?;
//! model.set_gain(8192).await?;          // a tracked parameter
//! model.tap_tempo().await?;             // a momentary action
//! model.send_control(Control::Freeze(true)).await?; // any raw control
//! # Ok(())
//! # }
//! ```

pub mod cbor;
pub mod control;
pub mod discovery;
pub mod error;
pub mod fmt;
pub mod generated;
pub mod midi3;
pub mod model;
pub mod nrpn;
pub mod params;
pub mod protocol;
pub mod registry;
pub mod session;
pub mod state;

/// The single UDP/TCP port the Profiler uses for both discovery and sessions.
pub const PORT: u16 = generated::PORT;

/// The version of the shared protocol spec this crate was generated against.
pub const SPEC_VERSION: &str = generated::SPEC_VERSION;

pub use cbor::{StateSnapshot, extract_snapshot, param_write, state_dump_request};
pub use control::{Control, ModuleSlot, program_change, slot_enable_cc};
pub use discovery::{DiscoveryPort, Options, Reply, discover, find_first};
pub use error::{DiscoverError, ParseError, SessionError};
pub use midi3::Unframer;
pub use model::{ApplyOutcome, CommandError, DeviceEvent, DeviceModel, RealtimeStatus};
pub use nrpn::{
    NrpnHeader, beacon, control_change, ext_decode, multi_values, parse_extended_string,
    parse_rendered_string, request_multi, request_rendered_string, request_single, request_string,
    set_single, sysex, u14, u14_split,
};
pub use protocol::{DISCOVERY_PORT, DSCV_HEADER, TagStream, build_poll_request};
pub use registry::{ParamDescriptor, ParamKind, descriptor, format_value};
pub use session::{
    CONNECTION_COOLDOWN, HandshakeOutcome, PROTOCOL_CBOR_CONTROL, PROTOCOL_MIDI3_STREAM,
    PROTOCOL_REQUEST_RESPONSE, Session,
};
pub use state::{Amp, Cabinet, Connection, DeviceState, Effect, Output, Rig, Tuner};
