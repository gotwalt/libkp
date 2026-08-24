//! The async [`DeviceModel`] store, driven against the fake device in
//! `tests/common`: both links, the request lane, the Navigator, loss and
//! reconnect, close and drop.
//!
//! Every test opens its own fake on a fresh ephemeral port, so the connection
//! ledger in `libkp::session` never makes one test wait on another. It does
//! space a model's *second* socket to the same fake — the control link opens
//! [`CONNECTION_COOLDOWN`] after the stream, a reconnect a cooldown after the
//! loss — which is the real spacing and the accepted cost of these tests.

mod common;

use std::time::Duration;

use common::{Config, FakeDevice, default_dump, multi_item, wait_for};
use libkp::control::Control;
use libkp::generated;
use libkp::model::{
    Backoff, ChannelError, CommandError, ConnectOptions, ControlPolicy, DeviceEvent, DeviceModel,
    ReconnectPolicy, RequestError, SyncStrategy,
};
use libkp::nrpn::{self, FUNCTION_EXT_PARAM, set_single};
use libkp::params::{BankPreviewField, bank_preview_address};
use libkp::session::CONNECTION_COOLDOWN;
use libkp::state::{Channel, ChannelState, Connection, DeviceState, NavDrop, Navigation};
use tokio::sync::broadcast;
use tokio::time::{Instant, timeout};

/// How long any single wait in these tests may take before it is a failure.
const PATIENCE: Duration = Duration::from_secs(6);

/// The next event matching `pred`, skipping the rest; a lagged receiver just
/// carries on. Panics after [`PATIENCE`].
async fn next_event(
    events: &mut broadcast::Receiver<DeviceEvent>,
    pred: impl Fn(&DeviceEvent) -> bool,
) -> DeviceEvent {
    timeout(PATIENCE, async {
        loop {
            match events.recv().await {
                Ok(event) if pred(&event) => return event,
                Ok(_) | Err(broadcast::error::RecvError::Lagged(_)) => continue,
                Err(broadcast::error::RecvError::Closed) => panic!("event stream closed"),
            }
        }
    })
    .await
    .expect("waited too long for an event")
}

/// Every snapshot the receiver holds right now.
fn drain(snapshots: &mut broadcast::Receiver<DeviceState>) -> Vec<DeviceState> {
    let mut out = Vec::new();
    while let Ok(s) = snapshots.try_recv() {
        out.push(s);
    }
    out
}

/// Options for a model that opens only the stream and asks it nothing —
/// what the lane and loss tests want, so that the fake sees only what the
/// test sends.
fn quiet(fake: &FakeDevice) -> ConnectOptions {
    ConnectOptions {
        control: ControlPolicy::Off,
        sync: SyncStrategy::Off,
        ..fake.options()
    }
}

fn channel_event(channel: Channel, state: ChannelState) -> impl Fn(&DeviceEvent) -> bool {
    move |e| matches!(e, DeviceEvent::ChannelChanged { channel: c, state: s } if *c == channel && *s == state)
}

/// A `$06` Extended Parameter push, as the device sends its position: the
/// 5×7-bit address, then the 5×7-bit value.
fn ext_param(address: u32, value: u64) -> Vec<u8> {
    let mut msg = vec![0xF0, 0x00, 0x20, 0x33, 0x00, 0x00, FUNCTION_EXT_PARAM, 0x00];
    msg.extend(nrpn::ext_encode(u64::from(address), 5));
    msg.extend(nrpn::ext_encode(value, 5));
    msg.push(0xF7);
    msg
}

/// Push the device's position — bank then slot, as two `$06` messages in
/// one write — for the flat rig `index`.
async fn push_position(stream: &common::Connection, index: u16) {
    let slots = index / generated::BANK_SLOTS as u16;
    let mut burst = Vec::new();
    burst.extend(libkp::midi3::frame(&ext_param(
        generated::CURRENT_BANK_ADDRESS,
        u64::from(slots),
    )));
    burst.extend(libkp::midi3::frame(&ext_param(
        generated::CURRENT_RIG_SLOT_ADDRESS,
        u64::from(index % generated::BANK_SLOTS as u16),
    )));
    stream.push_raw(&burst).await;
}

/// The two messages the Navigator puts on the wire for one rig index: the
/// bank preselect, then the slot load that commits it.
fn load_pair(index: u16) -> [Vec<u8>; 2] {
    let slots = index / generated::BANK_SLOTS as u16;
    [
        Control::BankPreselect(slots as u8).message(0),
        Control::LoadSlot((index % generated::BANK_SLOTS as u16) as u8 + 1).message(0),
    ]
}

/// Every navigation event the receiver holds right now.
fn drain_navigation(events: &mut broadcast::Receiver<DeviceEvent>) -> Vec<DeviceEvent> {
    let mut out = Vec::new();
    while let Ok(event) = events.try_recv() {
        if matches!(
            event,
            DeviceEvent::NavigationSettled { .. } | DeviceEvent::NavigationDropped { .. }
        ) {
            out.push(event);
        }
    }
    out
}

// ---------------------------------------------------------------------------
// The control link and the dump
// ---------------------------------------------------------------------------

#[tokio::test]
async fn default_connect_opens_the_control_link_and_folds_the_dump() {
    let fake = FakeDevice::start().await;
    let model = DeviceModel::connect_with(fake.ip(), fake.options())
        .await
        .unwrap();
    let mut events = model.events();
    let mut snapshots = model.subscribe();

    // Right after connect: the stream is up and the control link has not
    // started yet.
    let state = model.state();
    assert_eq!(state.connection, Connection::Connected);
    assert_eq!(state.channels.stream, ChannelState::Open);
    assert_eq!(state.channels.control, ChannelState::Closed);

    // Closed → Connecting → Open, in that order, on the events stream.
    let mut control_states = Vec::new();
    let opened = Instant::now();
    loop {
        let event = next_event(&mut events, |e| {
            matches!(
                e,
                DeviceEvent::ChannelChanged {
                    channel: Channel::Control,
                    ..
                }
            )
        })
        .await;
        if let DeviceEvent::ChannelChanged { state, .. } = event {
            control_states.push(state);
            if state == ChannelState::Open {
                break;
            }
        }
    }
    assert_eq!(
        control_states,
        vec![ChannelState::Connecting, ChannelState::Open]
    );
    // The second socket was spaced by the ledger, not by the model.
    assert!(opened.elapsed() < CONNECTION_COOLDOWN * 2);
    let control = fake.wait_for_control(0).await;
    assert!(
        wait_for(|| control.dump_triggered(), PATIENCE).await,
        "the dump trigger was written"
    );

    // The dump ends with the run at DUMP_END_ADDRESS, and that completes the
    // control sync.
    let done = next_event(&mut events, |e| {
        matches!(
            e,
            DeviceEvent::SyncCompleted {
                source: Channel::Control
            }
        )
    })
    .await;
    assert_eq!(
        done,
        DeviceEvent::SyncCompleted {
            source: Channel::Control
        }
    );

    // The dump folded: the morph, the position and the rig name are in the
    // tree, and the whole burst republished the snapshot exactly once.
    let state = model.state();
    assert_eq!(state.morph, Some(8192));
    assert_eq!(state.current_bank, Some(3));
    assert_eq!(state.current_rig_slot, Some(1));
    assert_eq!(state.rig.name.as_deref(), Some("Fake Rig"));
    assert_eq!(state.connection, Connection::Connected);
    assert_eq!(state.channels.control, ChannelState::Open);
    let with_dump = drain(&mut snapshots)
        .into_iter()
        .filter(|s| s.morph.is_some())
        .count();
    assert_eq!(with_dump, 1, "one snapshot for the whole dump");

    // A live push on the control link after the dump folds as live.
    control
        .push_items(&[libkp::cbor::param_write(generated::MORPH_ADDRESS, 0)])
        .await;
    let morphed = next_event(&mut events, |e| matches!(e, DeviceEvent::MorphChanged(0))).await;
    assert_eq!(morphed, DeviceEvent::MorphChanged(0));
    assert_eq!(model.state().morph, Some(0));

    // The link never wrote anything but the trigger.
    assert_eq!(
        control.raw(),
        libkp::cbor::to_vec(&libkp::cbor::state_dump_request())
    );
    model.close().await;
}

#[tokio::test]
async fn a_dump_without_its_end_marker_settles() {
    let fake = FakeDevice::start_with(Config {
        dump: vec![libkp::cbor::param_write(generated::MORPH_ADDRESS, 100)],
        ..Config::default()
    })
    .await;
    let model = DeviceModel::connect_with(
        fake.ip(),
        ConnectOptions {
            sync: SyncStrategy::Off,
            ..fake.options()
        },
    )
    .await
    .unwrap();
    let mut events = model.events();
    next_event(
        &mut events,
        channel_event(Channel::Control, ChannelState::Open),
    )
    .await;
    let started = Instant::now();
    next_event(&mut events, |e| {
        matches!(
            e,
            DeviceEvent::SyncCompleted {
                source: Channel::Control
            }
        )
    })
    .await;
    let settle = Duration::from_millis(generated::DUMP_SETTLE_MS);
    assert!(started.elapsed() >= settle - Duration::from_millis(50));
    assert_eq!(model.state().morph, Some(100));
    model.close().await;
}

#[tokio::test]
async fn a_rejecting_device_degrades_the_connection_and_the_stream_carries_on() {
    let fake = FakeDevice::start_with(Config::without_cbor()).await;
    let model = DeviceModel::connect_with(
        fake.ip(),
        ConnectOptions {
            sync: SyncStrategy::Off,
            ..fake.options()
        },
    )
    .await
    .unwrap();
    let mut events = model.events();

    next_event(
        &mut events,
        channel_event(Channel::Control, ChannelState::Unavailable),
    )
    .await;
    let degraded = next_event(&mut events, |e| {
        matches!(e, DeviceEvent::ConnectionChanged(Connection::Degraded))
    })
    .await;
    assert_eq!(
        degraded,
        DeviceEvent::ConnectionChanged(Connection::Degraded)
    );
    let state = model.state();
    assert_eq!(state.connection, Connection::Degraded);
    assert_eq!(state.channels.control, ChannelState::Unavailable);
    assert_eq!(state.channels.stream, ChannelState::Open);

    // The stream is unaffected: a push lands, a command goes out.
    let stream = fake.wait_for_stream(0).await;
    stream
        .push(&set_single(
            0,
            0,
            generated::AMP_PAGE,
            generated::GAIN_NUMBER,
            777,
        ))
        .await;
    next_event(&mut events, |e| {
        matches!(e, DeviceEvent::ParamChanged { value: 777, .. })
    })
    .await;
    assert_eq!(model.state().amp.gain, Some(777));
    model.set_gain(1234).await.unwrap();
    assert!(wait_for(|| stream.received().len() == 1, PATIENCE).await);
    assert_eq!(
        stream.received()[0],
        set_single(0, 0x7F, generated::AMP_PAGE, generated::GAIN_NUMBER, 1234)
    );
    model.close().await;
}

#[tokio::test]
async fn control_policy_off_never_opens_a_second_connection() {
    let fake = FakeDevice::start().await;
    let model = DeviceModel::connect_with(fake.ip(), quiet(&fake))
        .await
        .unwrap();
    let state = model.state();
    assert_eq!(state.connection, Connection::Connected);
    assert_eq!(state.channels.control, ChannelState::Closed);

    // Long enough for the ledger to have let a second open through.
    tokio::time::sleep(CONNECTION_COOLDOWN + Duration::from_millis(300)).await;
    assert_eq!(fake.connections().len(), 1);
    let state = model.state();
    assert_eq!(state.connection, Connection::Connected);
    assert_eq!(state.channels.control, ChannelState::Closed);
    model.close().await;
}

#[tokio::test]
async fn required_control_with_a_rejecting_device_fails_the_connect() {
    let fake = FakeDevice::start_with(Config::without_cbor()).await;
    let result = DeviceModel::connect_with(
        fake.ip(),
        ConnectOptions {
            control: ControlPolicy::Required,
            sync: SyncStrategy::Off,
            ..fake.options()
        },
    )
    .await;
    let err = result
        .err()
        .expect("a required control link that cannot open fails the connect");
    assert!(
        matches!(err, libkp::SessionError::ProtocolRejected { .. }),
        "{err:?}"
    );
    // No session is left open: every socket the fake saw has ended.
    assert!(
        wait_for(
            || {
                let conns = fake.connections();
                !conns.is_empty() && conns.iter().all(|c| c.is_closed())
            },
            PATIENCE
        )
        .await
    );
}

#[tokio::test]
async fn control_eof_is_lost_and_not_reopened() {
    let fake = FakeDevice::start().await;
    let model = DeviceModel::connect_with(
        fake.ip(),
        ConnectOptions {
            sync: SyncStrategy::Off,
            ..fake.options()
        },
    )
    .await
    .unwrap();
    let mut events = model.events();
    next_event(
        &mut events,
        channel_event(Channel::Control, ChannelState::Open),
    )
    .await;

    let control = fake.wait_for_control(0).await;
    control.hang_up().await;
    next_event(
        &mut events,
        channel_event(Channel::Control, ChannelState::Lost),
    )
    .await;
    next_event(&mut events, |e| {
        matches!(e, DeviceEvent::ConnectionChanged(Connection::Degraded))
    })
    .await;
    let state = model.state();
    assert_eq!(state.connection, Connection::Degraded);
    assert_eq!(state.channels.control, ChannelState::Lost);
    assert_eq!(state.channels.stream, ChannelState::Open);

    // Never reopened on its own …
    tokio::time::sleep(CONNECTION_COOLDOWN + Duration::from_millis(300)).await;
    assert_eq!(fake.controls().len(), 1);
    // … and not on request inside the minimum gap either.
    assert!(matches!(
        model.reopen_control().await,
        Err(ChannelError::TooSoon)
    ));
    assert_eq!(fake.controls().len(), 1);
    model.close().await;
}

#[tokio::test]
async fn reopen_control_is_refused_when_the_policy_is_off() {
    let fake = FakeDevice::start().await;
    let model = DeviceModel::connect_with(fake.ip(), quiet(&fake))
        .await
        .unwrap();
    assert!(matches!(
        model.reopen_control().await,
        Err(ChannelError::Off)
    ));
    model.close().await;
}

// ---------------------------------------------------------------------------
// The request lane
// ---------------------------------------------------------------------------

#[tokio::test]
async fn requests_resolve_with_the_reply() {
    let fake = FakeDevice::start().await;
    let gain = u32::from(generated::AMP_PAGE) * 128 + u32::from(generated::GAIN_NUMBER);
    fake.set_value(gain, 5000);
    fake.set_string(u32::from(generated::STRING_RIG_NAME), "Maz 18 Pushed");
    fake.set_value(generated::CURRENT_BANK_ADDRESS, 70_000);
    let preview = bank_preview_address(BankPreviewField::AmpName, 2);
    fake.set_string(preview, "Vox AC30TB");
    fake.set_render(generated::AMP_PAGE, generated::GAIN_NUMBER, 5000, "5.2");

    let model = DeviceModel::connect_with(fake.ip(), quiet(&fake))
        .await
        .unwrap();

    assert_eq!(
        model
            .request_param(generated::AMP_PAGE, generated::GAIN_NUMBER)
            .await,
        Ok(5000)
    );
    assert_eq!(model.state().amp.gain, Some(5000), "the reply folded too");
    assert_eq!(
        model
            .request_string(generated::PAGE_STRINGS, generated::STRING_RIG_NAME)
            .await
            .as_deref(),
        Ok("Maz 18 Pushed")
    );
    assert_eq!(model.state().rig.name.as_deref(), Some("Maz 18 Pushed"));
    // The lane hands back the 35-bit value even where the row drops it.
    assert_eq!(
        model
            .request_ext_param(generated::CURRENT_BANK_ADDRESS)
            .await,
        Ok(70_000)
    );
    assert_eq!(model.state().current_bank, None);
    assert_eq!(
        model.request_ext_string(preview).await.as_deref(),
        Ok("Vox AC30TB")
    );
    assert_eq!(
        model.state().bank.slots[1].amp_name.as_deref(),
        Some("Vox AC30TB")
    );
    assert_eq!(
        model
            .request_render(generated::AMP_PAGE, generated::GAIN_NUMBER, 5000)
            .await
            .as_deref(),
        Ok("5.2")
    );
    model.close().await;
}

#[tokio::test]
async fn an_unsolicited_push_at_the_address_resolves_a_request() {
    let fake = FakeDevice::start().await;
    let model = DeviceModel::connect_with(fake.ip(), quiet(&fake))
        .await
        .unwrap();
    let stream = fake.wait_for_stream(0).await;
    let request = tokio::spawn({
        let model = model.clone();
        async move {
            model
                .request_param(generated::AMP_PAGE, generated::GAIN_NUMBER)
                .await
        }
    });
    assert!(wait_for(|| stream.requests() == 1, PATIENCE).await);
    stream
        .push(&set_single(
            0,
            0,
            generated::AMP_PAGE,
            generated::GAIN_NUMBER,
            42,
        ))
        .await;
    assert_eq!(request.await.unwrap(), Ok(42));
    model.close().await;
}

#[tokio::test]
async fn an_unanswered_request_times_out_and_is_never_retried() {
    let fake = FakeDevice::start().await;
    let model = DeviceModel::connect_with(fake.ip(), quiet(&fake))
        .await
        .unwrap();
    let mut events = model.events();
    let started = Instant::now();
    let result = model
        .request_param(generated::AMP_PAGE, generated::GAIN_NUMBER)
        .await;
    assert_eq!(result, Err(RequestError::Timeout));
    let elapsed = started.elapsed();
    let limit = Duration::from_millis(generated::REQUEST_TIMEOUT_MS);
    assert!(elapsed >= limit - Duration::from_millis(20), "{elapsed:?}");
    let address = u32::from(generated::AMP_PAGE) * 128 + u32::from(generated::GAIN_NUMBER);
    let timed_out = next_event(&mut events, |e| {
        matches!(e, DeviceEvent::RequestTimedOut { .. })
    })
    .await;
    assert_eq!(timed_out, DeviceEvent::RequestTimedOut { address });
    // One request on the wire, and only one.
    let stream = fake.wait_for_stream(0).await;
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert_eq!(stream.requests(), 1);
    model.close().await;
}

#[tokio::test]
async fn the_morph_is_unreadable_without_a_byte_on_the_wire() {
    let fake = FakeDevice::start().await;
    let model = DeviceModel::connect_with(fake.ip(), quiet(&fake))
        .await
        .unwrap();
    let started = Instant::now();
    assert_eq!(
        model
            .request_param(generated::PAGE_MORPH, generated::MORPH_NUMBER)
            .await,
        Err(RequestError::Unreadable)
    );
    assert!(started.elapsed() < Duration::from_millis(100));
    let stream = fake.wait_for_stream(0).await;
    tokio::time::sleep(Duration::from_millis(50)).await;
    assert!(stream.received().is_empty());
    model.close().await;
}

#[tokio::test]
async fn at_most_sixteen_requests_are_on_the_wire_at_once() {
    let fake = FakeDevice::start().await;
    let model = DeviceModel::connect_with(fake.ip(), quiet(&fake))
        .await
        .unwrap();
    let stream = fake.wait_for_stream(0).await;

    // Twenty requests nobody answers, at twenty distinct addresses.
    let total = generated::MAX_IN_FLIGHT_REQUESTS + 4;
    let mut waiting = Vec::new();
    for number in 0..total as u8 {
        let model = model.clone();
        waiting.push(tokio::spawn(async move {
            model.request_param(generated::AMP_PAGE, number).await
        }));
    }
    // Well inside the timeout: exactly the limit have been sent.
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert_eq!(stream.requests(), generated::MAX_IN_FLIGHT_REQUESTS);

    // The rest go out as the first ones time out; none is dropped.
    for handle in waiting {
        assert_eq!(handle.await.unwrap(), Err(RequestError::Timeout));
    }
    assert_eq!(stream.requests(), total);
    model.close().await;
}

#[tokio::test]
async fn refresh_issues_the_forty_six_requests() {
    let fake = FakeDevice::start().await;
    let model = DeviceModel::connect_with(fake.ip(), quiet(&fake))
        .await
        .unwrap();
    let stream = fake.wait_for_stream(0).await;
    assert!(stream.received().is_empty(), "sync off asks nothing");

    // Nothing is answered, so the burst reports the timeouts — but every
    // request went out.
    assert_eq!(model.refresh().await, Err(RequestError::Timeout));
    let received = stream.received();
    assert_eq!(received.len(), 46);
    assert!(
        received.iter().all(|m| matches!(
            m[6],
            nrpn::FUNCTION_REQUEST_SINGLE
                | nrpn::FUNCTION_REQUEST_STRING
                | nrpn::FUNCTION_REQUEST_EXT_PARAM
                | nrpn::FUNCTION_REQUEST_EXT_STRING
        )),
        "the sync must be read-only"
    );
    let count = |f: u8| received.iter().filter(|m| m[6] == f).count();
    assert_eq!(count(nrpn::FUNCTION_REQUEST_STRING), 6);
    assert_eq!(count(nrpn::FUNCTION_REQUEST_SINGLE), 16 + 7);
    assert_eq!(count(nrpn::FUNCTION_REQUEST_EXT_STRING), 15);
    assert_eq!(count(nrpn::FUNCTION_REQUEST_EXT_PARAM), 2);
    model.close().await;
}

#[tokio::test]
async fn connect_runs_the_burst_in_the_background_and_reports_it_done() {
    let fake = FakeDevice::start().await;
    let model = DeviceModel::connect_with(
        fake.ip(),
        ConnectOptions {
            control: ControlPolicy::Off,
            ..fake.options()
        },
    )
    .await
    .unwrap();
    let mut events = model.events();
    let stream = fake.wait_for_stream(0).await;
    assert!(wait_for(|| stream.requests() == 46, PATIENCE).await);
    let done = next_event(&mut events, |e| {
        matches!(
            e,
            DeviceEvent::SyncCompleted {
                source: Channel::Stream
            }
        )
    })
    .await;
    assert_eq!(
        done,
        DeviceEvent::SyncCompleted {
            source: Channel::Stream
        }
    );
    model.close().await;
}

#[tokio::test]
async fn a_stream_read_chunk_republishes_the_snapshot_once() {
    let fake = FakeDevice::start().await;
    let model = DeviceModel::connect_with(fake.ip(), quiet(&fake))
        .await
        .unwrap();
    let mut snapshots = model.subscribe();
    let mut events = model.events();
    drain(&mut snapshots);
    let stream = fake.wait_for_stream(0).await;

    // Three slow changes in one write: one read, one snapshot.
    let mut burst = Vec::new();
    burst.extend(libkp::midi3::frame(&set_single(
        0,
        0,
        generated::AMP_PAGE,
        generated::GAIN_NUMBER,
        1,
    )));
    burst.extend(libkp::midi3::frame(&set_single(
        0,
        0,
        generated::PAGE_RIG_SETTINGS,
        generated::TEMPO_NUMBER,
        7680,
    )));
    burst.extend(libkp::midi3::frame(&set_single(0, 0, 0x3D, 0, 179)));
    stream.push_raw(&burst).await;
    next_event(&mut events, |e| {
        matches!(e, DeviceEvent::EffectChanged { slot: 7 })
    })
    .await;
    let published = drain(&mut snapshots);
    assert_eq!(published.len(), 1);
    assert_eq!(published[0].amp.gain, Some(1));
    assert_eq!(published[0].rig.tempo_bpm, Some(120));
    assert_eq!(published[0].effects[7].kind, Some(179));
    model.close().await;
}

// ---------------------------------------------------------------------------
// The Navigator
// ---------------------------------------------------------------------------

#[tokio::test]
async fn a_burst_of_taps_costs_two_loads_spaced_by_the_settle() {
    let fake = FakeDevice::start().await;
    let model = DeviceModel::connect_with(fake.ip(), quiet(&fake))
        .await
        .unwrap();
    let mut events = model.events();
    let stream = fake.wait_for_stream(0).await;

    // Three taps before anything could settle: the first goes out at once,
    // the aim moves on to the last.
    model.navigate_to(14);
    model.navigate_to(15);
    model.navigate_to(16);
    assert_eq!(
        model.state().navigation,
        Navigation {
            aim: Some(16),
            in_flight: true
        }
    );
    assert!(wait_for(|| stream.received().len() >= 2, PATIENCE).await);
    let first_at = Instant::now();
    assert_eq!(stream.received(), load_pair(14));

    // The settle sends the final aim, and nothing in between.
    assert!(wait_for(|| stream.received().len() >= 4, PATIENCE).await);
    let settle = Duration::from_millis(generated::RIG_LOAD_SETTLE_MS);
    let gap = first_at.elapsed();
    assert!(gap >= settle - Duration::from_millis(20), "{gap:?}");
    let received = stream.received();
    assert_eq!(received.len(), 4);
    assert_eq!(&received[2..], &load_pair(16));

    // The device never confirms, so the second settle opens the window and
    // sends nothing more: exactly two loads for three taps.
    tokio::time::sleep(settle + Duration::from_millis(100)).await;
    assert_eq!(stream.received().len(), 4);
    assert_eq!(
        model.state().navigation,
        Navigation {
            aim: Some(16),
            in_flight: false
        }
    );
    assert!(drain_navigation(&mut events).is_empty());
    model.close().await;
}

#[tokio::test]
async fn a_matching_position_on_the_stream_settles_the_aim() {
    let fake = FakeDevice::start().await;
    let model = DeviceModel::connect_with(fake.ip(), quiet(&fake))
        .await
        .unwrap();
    let mut events = model.events();
    let mut snapshots = model.subscribe();
    drain(&mut snapshots);
    let stream = fake.wait_for_stream(0).await;

    model.navigate_to(14);
    // The aim is a slow change: the snapshot shows it before the wire does.
    let aimed = drain(&mut snapshots).pop().expect("a snapshot for the aim");
    assert_eq!(aimed.navigation.aim, Some(14));
    assert_eq!(aimed.aimed_rig_index(), Some(14));
    assert!(wait_for(|| stream.received().len() == 2, PATIENCE).await);

    // A report of somewhere else keeps the aim; the aimed index does.
    push_position(&stream, 13).await;
    next_event(&mut events, |e| {
        matches!(e, DeviceEvent::CurrentPosition { slot: Some(3), .. })
    })
    .await;
    assert_eq!(model.state().navigation.aim, Some(14));
    push_position(&stream, 14).await;
    let settled = next_event(&mut events, |e| {
        matches!(e, DeviceEvent::NavigationSettled { .. })
    })
    .await;
    assert_eq!(settled, DeviceEvent::NavigationSettled { index: 14 });
    let state = model.state();
    assert_eq!(state.navigation.aim, None);
    assert_eq!(state.current_rig_index(), Some(14));
    assert_eq!(state.aimed_rig_index(), Some(14));

    // An early confirmation does not shorten the flight: the next aim waits
    // for the settle, and the confirmed one is not sent again.
    assert!(state.navigation.in_flight);
    model.navigate_to(15);
    assert_eq!(stream.received().len(), 2);
    assert!(wait_for(|| stream.received().len() == 4, PATIENCE).await);
    assert_eq!(&stream.received()[2..], &load_pair(15));
    model.close().await;
}

#[tokio::test]
async fn a_position_from_the_control_link_settles_the_aim_too() {
    let fake = FakeDevice::start().await;
    let model = DeviceModel::connect_with(
        fake.ip(),
        ConnectOptions {
            sync: SyncStrategy::Off,
            ..fake.options()
        },
    )
    .await
    .unwrap();
    let mut events = model.events();
    // Let the dump land first: it reports bank 3 / slot 1, which is nobody's
    // aim yet.
    next_event(&mut events, |e| {
        matches!(
            e,
            DeviceEvent::SyncCompleted {
                source: Channel::Control
            }
        )
    })
    .await;
    assert_eq!(model.state().current_rig_index(), Some(16));

    model.navigate_to(14);
    let control = fake.wait_for_control(0).await;
    control
        .push_items(&[
            libkp::cbor::param_write(generated::CURRENT_BANK_ADDRESS, 2),
            libkp::cbor::param_write(generated::CURRENT_RIG_SLOT_ADDRESS, 4),
        ])
        .await;
    let settled = next_event(&mut events, |e| {
        matches!(e, DeviceEvent::NavigationSettled { .. })
    })
    .await;
    assert_eq!(settled, DeviceEvent::NavigationSettled { index: 14 });
    assert_eq!(model.state().navigation.aim, None);
    model.close().await;
}

#[tokio::test]
async fn an_aim_past_the_end_is_dropped_after_the_window_and_may_be_sent_again() {
    let fake = FakeDevice::start().await;
    let model = DeviceModel::connect_with(fake.ip(), quiet(&fake))
        .await
        .unwrap();
    let mut events = model.events();
    let stream = fake.wait_for_stream(0).await;

    let aimed_at = Instant::now();
    model.navigate_to(999);
    assert!(wait_for(|| stream.received().len() == 2, PATIENCE).await);
    // The device stays put and says so; that is not a confirmation.
    push_position(&stream, 16).await;
    next_event(&mut events, |e| {
        matches!(e, DeviceEvent::CurrentPosition { slot: Some(1), .. })
    })
    .await;
    assert_eq!(model.state().navigation.aim, Some(999));

    let dropped = next_event(&mut events, |e| {
        matches!(e, DeviceEvent::NavigationDropped { .. })
    })
    .await;
    assert_eq!(
        dropped,
        DeviceEvent::NavigationDropped {
            index: 999,
            reason: NavDrop::Unconfirmed
        }
    );
    let after = Duration::from_millis(generated::RIG_LOAD_SETTLE_MS + generated::PENDING_WINDOW_MS);
    let elapsed = aimed_at.elapsed();
    assert!(elapsed >= after - Duration::from_millis(20), "{elapsed:?}");
    let state = model.state();
    assert_eq!(state.navigation, Navigation::default());
    assert_eq!(
        state.aimed_rig_index(),
        Some(16),
        "back to the device's own"
    );
    assert_eq!(stream.received().len(), 2, "never re-sent while aimed");

    // A drop forgets the sent index, so the same aim goes out again.
    model.navigate_to(999);
    assert!(wait_for(|| stream.received().len() == 4, PATIENCE).await);
    assert_eq!(&stream.received()[2..], &load_pair(999));
    model.close().await;
}

#[tokio::test]
async fn rig_loads_are_refused_outside_the_navigator_before_any_byte() {
    let fake = FakeDevice::start().await;
    let model = DeviceModel::connect_with(fake.ip(), quiet(&fake))
        .await
        .unwrap();
    let stream = fake.wait_for_stream(0).await;

    for control in [
        Control::LoadSlot(1),
        Control::Up,
        Control::Down,
        Control::ProgramChange(3),
        Control::BankSelect { msb: 0, lsb: 1 },
    ] {
        assert!(
            matches!(
                model.send_control(control).await,
                Err(CommandError::RigLoadRequiresNavigator)
            ),
            "{control:?}"
        );
    }
    assert!(matches!(
        model.send_raw(&[0xC0, 5]).await,
        Err(CommandError::RigLoadRequiresNavigator)
    ));
    for controller in generated::RIG_LOAD_CONTROLLERS {
        assert!(matches!(
            model.send_raw(&[0xB0, controller, 1]).await,
            Err(CommandError::RigLoadRequiresNavigator)
        ));
    }
    // The preselect alone loads nothing, and goes.
    model.bank(3).await.unwrap();
    model
        .send_raw(&Control::BankPreselect(4).message(0))
        .await
        .unwrap();
    assert!(wait_for(|| stream.received().len() == 2, PATIENCE).await);
    tokio::time::sleep(Duration::from_millis(50)).await;
    assert_eq!(
        stream.received(),
        vec![
            Control::BankPreselect(2).message(0),
            Control::BankPreselect(4).message(0)
        ]
    );
    model.close().await;
}

#[tokio::test]
async fn stepping_moves_the_aim_and_needs_a_position_to_step_from() {
    let fake = FakeDevice::start().await;
    let model = DeviceModel::connect_with(fake.ip(), quiet(&fake))
        .await
        .unwrap();
    let mut events = model.events();
    let stream = fake.wait_for_stream(0).await;

    // Nothing to step from yet: nothing happens.
    model.step_rig(1);
    model.step_bank(true);
    model.select_slot(2);
    tokio::time::sleep(Duration::from_millis(50)).await;
    assert!(stream.received().is_empty());
    assert_eq!(model.state().navigation, Navigation::default());

    push_position(&stream, 16).await;
    next_event(&mut events, |e| {
        matches!(e, DeviceEvent::CurrentPosition { slot: Some(1), .. })
    })
    .await;
    // A step from the device's position goes out; the next steps go from
    // the aim, not from where the device still is.
    model.step_rig(-1);
    assert!(wait_for(|| stream.received().len() == 2, PATIENCE).await);
    assert_eq!(stream.received(), load_pair(15));
    model.step_bank(true);
    assert_eq!(model.state().aimed_rig_index(), Some(20));
    model.select_slot(3);
    assert_eq!(model.state().aimed_rig_index(), Some(22));
    // Out of range slots and a step to where the aim already is do nothing.
    model.select_slot(0);
    model.select_slot(generated::BANK_SLOTS as u8 + 1);
    model.step_rig(0);
    assert_eq!(model.state().aimed_rig_index(), Some(22));
    // The floor is 0.
    model.step_rig(-100);
    assert_eq!(model.state().aimed_rig_index(), Some(0));
    model.navigate_to(22);

    // One settle later the final aim is the second and last load.
    assert!(wait_for(|| stream.received().len() == 4, PATIENCE).await);
    assert_eq!(&stream.received()[2..], &load_pair(22));
    tokio::time::sleep(Duration::from_millis(generated::RIG_LOAD_SETTLE_MS + 100)).await;
    assert_eq!(stream.received().len(), 4);
    model.close().await;
}

#[tokio::test]
async fn a_stream_loss_forgets_the_aim_without_a_word() {
    let fake = FakeDevice::start().await;
    let model = DeviceModel::connect_with(fake.ip(), quiet(&fake))
        .await
        .unwrap();
    let mut events = model.events();
    let stream = fake.wait_for_stream(0).await;

    model.navigate_to(14);
    assert!(wait_for(|| stream.received().len() == 2, PATIENCE).await);
    stream.hang_up().await;
    next_event(&mut events, |e| matches!(e, DeviceEvent::Disconnected)).await;
    assert_eq!(model.state().navigation, Navigation::default());

    // The timers were cancelled with the life: nothing fires late.
    tokio::time::sleep(Duration::from_millis(
        generated::RIG_LOAD_SETTLE_MS + generated::PENDING_WINDOW_MS + 100,
    ))
    .await;
    assert!(drain_navigation(&mut events).is_empty());

    // With no stream to send on, an aim is dropped at once, and says so.
    model.navigate_to(3);
    assert_eq!(
        drain_navigation(&mut events),
        vec![DeviceEvent::NavigationDropped {
            index: 3,
            reason: NavDrop::Unconfirmed
        }]
    );
    assert_eq!(model.state().navigation, Navigation::default());
    model.close().await;
}

// ---------------------------------------------------------------------------
// Loss, reconnect, close, drop
// ---------------------------------------------------------------------------

#[tokio::test]
async fn a_stream_hang_up_disconnects_by_default() {
    let fake = FakeDevice::start().await;
    let model = DeviceModel::connect_with(fake.ip(), quiet(&fake))
        .await
        .unwrap();
    let mut events = model.events();
    let mut snapshots = model.subscribe();
    let stream = fake.wait_for_stream(0).await;
    stream.hang_up().await;

    let gone = next_event(&mut events, |e| matches!(e, DeviceEvent::Disconnected)).await;
    assert_eq!(gone, DeviceEvent::Disconnected);
    let changed = next_event(&mut events, |e| {
        matches!(e, DeviceEvent::ConnectionChanged(Connection::Disconnected))
    })
    .await;
    assert_eq!(
        changed,
        DeviceEvent::ConnectionChanged(Connection::Disconnected)
    );
    let state = model.state();
    assert_eq!(state.connection, Connection::Disconnected);
    assert_eq!(state.channels.stream, ChannelState::Lost);
    let last = drain(&mut snapshots).pop().expect("a final snapshot");
    assert_eq!(last.connection, Connection::Disconnected);

    // The lane is closed; the receivers are not.
    assert_eq!(
        model
            .request_param(generated::AMP_PAGE, generated::GAIN_NUMBER)
            .await,
        Err(RequestError::Disconnected)
    );
    assert!(matches!(
        model.set_gain(1).await,
        Err(libkp::CommandError::Disconnected)
    ));
    assert!(matches!(
        events.try_recv(),
        Err(broadcast::error::TryRecvError::Empty)
    ));
    model.close().await;
}

#[tokio::test]
async fn a_lost_stream_reconnects_with_backoff_on_the_same_receivers() {
    let fake = FakeDevice::start().await;
    let model = DeviceModel::connect_with(
        fake.ip(),
        ConnectOptions {
            reconnect: ReconnectPolicy {
                stream: Some(Backoff {
                    initial: Duration::from_millis(50),
                    max: Duration::from_millis(100),
                }),
                control_reopen: None,
            },
            ..quiet(&fake)
        },
    )
    .await
    .unwrap();
    let mut events = model.events();
    let stream = fake.wait_for_stream(0).await;
    let lost_at = Instant::now();
    stream.hang_up().await;

    let reconnecting = next_event(&mut events, |e| {
        matches!(
            e,
            DeviceEvent::ConnectionChanged(Connection::Reconnecting { .. })
        )
    })
    .await;
    assert_eq!(
        reconnecting,
        DeviceEvent::ConnectionChanged(Connection::Reconnecting { attempt: 1 })
    );
    assert_eq!(
        model.state().connection,
        Connection::Reconnecting { attempt: 1 }
    );

    // The same receivers see the new life come up.
    next_event(&mut events, |e| matches!(e, DeviceEvent::Connected)).await;
    next_event(&mut events, |e| {
        matches!(e, DeviceEvent::ConnectionChanged(Connection::Connected))
    })
    .await;
    // Spaced by the ledger: a full cooldown after the loss.
    assert!(lost_at.elapsed() >= CONNECTION_COOLDOWN);
    assert_eq!(fake.streams().len(), 2);
    let state = model.state();
    assert_eq!(state.connection, Connection::Connected);
    assert_eq!(state.channels.stream, ChannelState::Open);

    // And the new life works: a command reaches the second socket.
    let second = fake.wait_for_stream(1).await;
    model.set_gain(9).await.unwrap();
    assert!(wait_for(|| second.received().len() == 1, PATIENCE).await);
    model.close().await;
}

#[tokio::test]
async fn close_is_idempotent_and_closes_both_links() {
    let fake = FakeDevice::start().await;
    let model = DeviceModel::connect_with(
        fake.ip(),
        ConnectOptions {
            sync: SyncStrategy::Off,
            ..fake.options()
        },
    )
    .await
    .unwrap();
    let mut events = model.events();
    next_event(
        &mut events,
        channel_event(Channel::Control, ChannelState::Open),
    )
    .await;

    model.close().await;
    let gone = next_event(&mut events, |e| matches!(e, DeviceEvent::Disconnected)).await;
    assert_eq!(gone, DeviceEvent::Disconnected);
    // The last word of a close is the connection transition.
    next_event(&mut events, |e| {
        matches!(e, DeviceEvent::ConnectionChanged(Connection::Disconnected))
    })
    .await;
    let state = model.state();
    assert_eq!(state.connection, Connection::Disconnected);
    assert_eq!(state.channels.stream, ChannelState::Closed);
    assert_eq!(state.channels.control, ChannelState::Closed);
    assert!(
        wait_for(
            || fake.connections().iter().all(|c| c.is_closed()),
            PATIENCE
        )
        .await,
        "both sockets closed"
    );

    // A second close says nothing.
    model.close().await;
    assert!(matches!(
        events.try_recv(),
        Err(broadcast::error::TryRecvError::Empty)
    ));
}

#[tokio::test]
async fn dropping_the_last_handle_disconnects() {
    let fake = FakeDevice::start().await;
    let model = DeviceModel::connect_with(fake.ip(), quiet(&fake))
        .await
        .unwrap();
    let mut events = model.events();
    let other = model.clone();

    // One of two handles going is nothing.
    drop(model);
    tokio::time::sleep(Duration::from_millis(50)).await;
    assert!(matches!(
        events.try_recv(),
        Err(broadcast::error::TryRecvError::Empty)
    ));
    assert_eq!(other.state().connection, Connection::Connected);

    drop(other);
    let gone = next_event(&mut events, |e| matches!(e, DeviceEvent::Disconnected)).await;
    assert_eq!(gone, DeviceEvent::Disconnected);
    assert!(
        wait_for(
            || fake.connections().iter().all(|c| c.is_closed()),
            PATIENCE
        )
        .await,
        "the socket closed"
    );
}

#[tokio::test]
async fn subscribing_republishes_the_current_state() {
    let fake = FakeDevice::start().await;
    let model = DeviceModel::connect_with(fake.ip(), quiet(&fake))
        .await
        .unwrap();
    let mut first = model.subscribe();
    let seeded = first.try_recv().expect("a fresh snapshot on joining");
    assert_eq!(seeded.connection, Connection::Connected);
    // Everyone gets it, not just the newcomer.
    let mut second = model.subscribe();
    assert!(first.try_recv().is_ok());
    assert!(second.try_recv().is_ok());
    model.close().await;
}

#[tokio::test]
#[allow(deprecated)]
async fn the_apply_cbor_shim_still_folds_through_the_funnel() {
    let fake = FakeDevice::start().await;
    let model = DeviceModel::connect_with(fake.ip(), quiet(&fake))
        .await
        .unwrap();
    let mut events = model.events();
    let mut snapshots = model.subscribe();
    drain(&mut snapshots);
    model.apply_cbor(generated::MORPH_ADDRESS, 4096);
    assert_eq!(events.try_recv().unwrap(), DeviceEvent::MorphChanged(4096));
    assert_eq!(drain(&mut snapshots).len(), 1);
    assert_eq!(model.state().morph, Some(4096));
    model.close().await;
}

/// The default dump the fake serves is a real dump's shape: it ends with the
/// run at `DUMP_END_ADDRESS`.
#[test]
fn the_fake_dump_ends_with_the_marker() {
    let dump = default_dump();
    assert_eq!(
        dump.last(),
        Some(&multi_item(generated::DUMP_END_ADDRESS, &[0, 0, 0]))
    );
}
