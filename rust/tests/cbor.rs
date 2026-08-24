//! The CBOR tooling — [`StateSnapshot::fetch`] and [`CborSession`] — driven
//! against the shared fake device, the same way Python's `test_cbor.py` drives
//! `fetch_state_snapshot` and `CborSession` against its fake. Both are built on
//! the one `ControlLink` open-and-ingest path the model's control link uses, so
//! these prove that path reads a real dump and streams later pushes.

mod common;

use std::time::Duration;

use common::{FakeDevice, default_dump, wait_for};
use libkp::cbor::{self, CborSession, StateSnapshot};
use libkp::generated;

const PATIENCE: Duration = Duration::from_secs(6);

/// `StateSnapshot::fetch_with` reads the dump over its own control link: the
/// position, the morph and the rig name, on one socket, the trigger written
/// once, nothing left open.
#[tokio::test]
async fn fetch_reads_the_dump_over_the_control_link() {
    let fake = FakeDevice::start().await;
    let snapshot = StateSnapshot::fetch_with(fake.ip(), fake.port(), Duration::from_secs(2))
        .await
        .unwrap();
    assert!(snapshot.is_complete());
    assert_eq!(snapshot.current_bank, Some(3));
    assert_eq!(snapshot.current_rig_slot, Some(1));
    assert_eq!(snapshot.morph, Some(8192));
    assert_eq!(
        snapshot.string(u32::from(generated::STRING_RIG_NAME)),
        Some("Fake Rig")
    );

    // One control socket, on which the trigger was the only thing written, and
    // nothing left open.
    assert!(
        wait_for(
            || fake.connections().iter().all(|c| c.is_closed()),
            PATIENCE
        )
        .await
    );
    let controls = fake.controls();
    assert_eq!(controls.len(), 1);
    assert_eq!(controls[0].raw(), cbor::to_vec(&cbor::state_dump_request()));
}

/// `CborSession` streams the dump's numeric pairs in document order, then the
/// values pushed after it.
#[tokio::test]
async fn cbor_session_streams_the_dump_and_later_pushes() {
    let fake = FakeDevice::start().await;
    let session = CborSession::connect_to(fake.ip(), fake.port())
        .await
        .unwrap();
    let mut updates = session.subscribe();

    // Every numeric pair the default dump carries, in the order it carries
    // them — the first subscriber inherits the opening burst.
    for expected in cbor::numeric_values(&default_dump()) {
        let got = tokio::time::timeout(PATIENCE, updates.recv())
            .await
            .expect("a dump pair")
            .expect("the session is live");
        assert_eq!(got, expected);
    }

    // A value pushed after the dump streams too.
    let control = fake.wait_for_control(0).await;
    control
        .push_items(&[cbor::param_write(generated::MORPH_ADDRESS, 0)])
        .await;
    let pushed = tokio::time::timeout(PATIENCE, updates.recv())
        .await
        .expect("a pushed pair")
        .expect("the session is live");
    assert_eq!(pushed, (generated::MORPH_ADDRESS, 0));
}
