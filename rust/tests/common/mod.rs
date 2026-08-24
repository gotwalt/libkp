//! A fake Profiler for the integration tests: a localhost TCP server that
//! speaks just enough of the transport to drive a [`libkp::model::DeviceModel`]
//! through both of its links.
//!
//! It is multi-connection and protocol-aware. Every accepted socket gets its
//! own [`Connection`] record: the greeting lists the protocols the fake was
//! configured to offer; a selection of one of them is answered `+<name>`, any
//! other with `-NO` and a hang-up, as the device does; then the 8-byte
//! preamble is read and the connection speaks whichever protocol was chosen.
//! On the MIDI3 stream it records every unframed message and answers the
//! request forms from its value tables — `$41`/`$43`/`$46`/`$47`/`$7C` with
//! `$01`/`$03`/`$06`/`$07`/`$3C` — for the addresses it has a value for, and
//! can push any message. On the CBOR channel it records the raw bytes, serves
//! the configured dump in one write when the dump trigger arrives, and can
//! push items. Any connection, or all of them, can be hung up.
//!
//! Lives in `tests/common/` rather than as its own `tests/*.rs` so that it is
//! a module the test binaries share, not a test binary of its own.

#![allow(dead_code)]

use std::collections::HashMap;
use std::net::Ipv4Addr;
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Duration;

use libkp::cbor::{self, Decoder, Value};
use libkp::generated;
use libkp::midi3::{self, Unframer};
use libkp::model::ConnectOptions;
use libkp::nrpn::{
    self, FUNCTION_EXT_PARAM, FUNCTION_EXT_STRING_PARAM, FUNCTION_RENDERED_STRING_REPLY,
    FUNCTION_REQUEST_EXT_PARAM, FUNCTION_REQUEST_EXT_STRING, FUNCTION_REQUEST_RENDERED_STRING,
    FUNCTION_REQUEST_SINGLE, FUNCTION_REQUEST_STRING, FUNCTION_STRING_PARAM, NrpnHeader,
    set_single, sysex, u14, u14_split,
};
use libkp::session::{PROTOCOL_CBOR_CONTROL, PROTOCOL_MIDI3_STREAM};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio::net::tcp::{OwnedReadHalf, OwnedWriteHalf};
use tokio::task::JoinHandle;

/// The reserved protocol GUID a real device lists first in its greeting.
pub const PROTOCOL_RESERVED: &str = generated::PROTOCOL_RESERVED;

/// The request functions the fake counts as requests on the wire.
const REQUEST_FUNCTIONS: [u8; 5] = [
    FUNCTION_REQUEST_SINGLE,
    FUNCTION_REQUEST_STRING,
    FUNCTION_REQUEST_EXT_PARAM,
    FUNCTION_REQUEST_EXT_STRING,
    FUNCTION_REQUEST_RENDERED_STRING,
];

/// What the fake offers and how it answers. Changeable while it runs.
#[derive(Debug, Clone)]
pub struct Config {
    /// The protocol GUIDs listed in the greeting; only these are accepted.
    pub offers: Vec<String>,
    /// Numeric values by flat address, for `$41` and `$46` requests. An
    /// address with no entry is not answered.
    pub values: HashMap<u32, u64>,
    /// String values by flat address, for `$43` and `$47` requests.
    pub strings: HashMap<u32, String>,
    /// Rendered strings by (page, number, value), for `$7C` requests.
    pub renders: HashMap<(u8, u8, u16), String>,
    /// The items served, in one write, when the dump trigger arrives.
    pub dump: Vec<Value>,
}

impl Default for Config {
    /// Offers every protocol a device does, answers nothing (no values), and
    /// serves a short dump: the position, the rig name, the morph, then the
    /// run at [`generated::DUMP_END_ADDRESS`] that ends every real dump.
    fn default() -> Self {
        Config {
            offers: vec![
                PROTOCOL_RESERVED.to_string(),
                PROTOCOL_MIDI3_STREAM.to_string(),
                PROTOCOL_CBOR_CONTROL.to_string(),
            ],
            values: HashMap::new(),
            strings: HashMap::new(),
            renders: HashMap::new(),
            dump: default_dump(),
        }
    }
}

impl Config {
    /// A device that does not offer the CBOR control channel.
    pub fn without_cbor() -> Self {
        let mut cfg = Config::default();
        cfg.offers.retain(|p| p != PROTOCOL_CBOR_CONTROL);
        cfg
    }
}

/// The default dump: bank 3 / slot 1 in one run, the rig name, the morph at
/// half, and the end marker.
pub fn default_dump() -> Vec<Value> {
    vec![
        multi_item(generated::CURRENT_BANK_ADDRESS - 1, &[0, 3, 1]),
        string_item(u32::from(generated::STRING_RIG_NAME), "Fake Rig"),
        cbor::param_write(generated::MORPH_ADDRESS, 8192),
        multi_item(generated::DUMP_END_ADDRESS, &[0, 0, 0]),
    ]
}

/// A `tag(1)([2, base, v0, v1, …])` run.
pub fn multi_item(base: u32, values: &[u64]) -> Value {
    let mut fields = vec![
        Value::Uint(generated::CBOR_SELECTOR_MULTI as u64),
        Value::Uint(u64::from(base)),
    ];
    fields.extend(values.iter().map(|v| Value::Uint(*v)));
    Value::Tag(generated::CBOR_ITEM_TAG, Box::new(Value::Array(fields)))
}

/// A `tag(1)([4, addr, "text"])` string.
pub fn string_item(address: u32, text: &str) -> Value {
    Value::Tag(
        generated::CBOR_ITEM_TAG,
        Box::new(Value::Array(vec![
            Value::Uint(generated::CBOR_SELECTOR_STRING as u64),
            Value::Uint(u64::from(address)),
            Value::Text(text.to_string()),
        ])),
    )
}

/// What one accepted socket has done so far.
#[derive(Debug, Default)]
struct ConnState {
    /// The protocol the client asked for.
    selected: Option<String>,
    /// The protocol the connection speaks, once accepted.
    protocol: Option<String>,
    saw_preamble: bool,
    /// Unframed MIDI messages received (MIDI3 connections).
    received: Vec<Vec<u8>>,
    /// Raw bytes received after the preamble (CBOR connections).
    raw: Vec<u8>,
    /// Whether the dump trigger has arrived (CBOR connections).
    dump_triggered: bool,
    /// The client's side of the socket ended.
    closed: bool,
}

/// One accepted connection: what it received, and a way to talk to it.
pub struct Connection {
    id: usize,
    state: Mutex<ConnState>,
    writer: tokio::sync::Mutex<Option<OwnedWriteHalf>>,
}

impl Connection {
    fn state(&self) -> MutexGuard<'_, ConnState> {
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// The order in which the fake accepted it, from 0.
    pub fn id(&self) -> usize {
        self.id
    }

    /// The protocol the client selected, accepted or not.
    pub fn selected(&self) -> Option<String> {
        self.state().selected.clone()
    }

    /// The protocol the connection speaks — `None` until accepted.
    pub fn protocol(&self) -> Option<String> {
        self.state().protocol.clone()
    }

    pub fn is_stream(&self) -> bool {
        self.protocol().as_deref() == Some(PROTOCOL_MIDI3_STREAM)
    }

    pub fn is_control(&self) -> bool {
        self.protocol().as_deref() == Some(PROTOCOL_CBOR_CONTROL)
    }

    pub fn saw_preamble(&self) -> bool {
        self.state().saw_preamble
    }

    /// Every unframed MIDI message received on a stream connection.
    pub fn received(&self) -> Vec<Vec<u8>> {
        self.state().received.clone()
    }

    /// How many of the received messages were requests.
    pub fn requests(&self) -> usize {
        self.state()
            .received
            .iter()
            .filter(|m| m.len() > 6 && REQUEST_FUNCTIONS.contains(&m[6]))
            .count()
    }

    /// Raw bytes received after the preamble on a control connection.
    pub fn raw(&self) -> Vec<u8> {
        self.state().raw.clone()
    }

    pub fn dump_triggered(&self) -> bool {
        self.state().dump_triggered
    }

    /// Whether the client closed its side.
    pub fn is_closed(&self) -> bool {
        self.state().closed
    }

    /// Push one raw MIDI message, framed, on a stream connection.
    pub async fn push(&self, message: &[u8]) {
        self.write(&midi3::frame(message)).await;
    }

    /// Push already-framed bytes in one write — several messages in one
    /// read on the client's side.
    pub async fn push_raw(&self, bytes: &[u8]) {
        self.write(bytes).await;
    }

    /// Push CBOR items on a control connection, in one write.
    pub async fn push_items(&self, items: &[Value]) {
        let mut bytes = Vec::new();
        for item in items {
            cbor::encode(item, &mut bytes);
        }
        self.write(&bytes).await;
    }

    /// Close the fake's side of the socket: the client reads EOF.
    pub async fn hang_up(&self) {
        if let Some(mut writer) = self.writer.lock().await.take() {
            let _ = writer.shutdown().await;
        }
    }

    async fn write(&self, bytes: &[u8]) {
        if let Some(writer) = self.writer.lock().await.as_mut() {
            let _ = writer.write_all(bytes).await;
        }
    }
}

struct Inner {
    config: Mutex<Config>,
    connections: Mutex<Vec<Arc<Connection>>>,
}

impl Inner {
    fn config(&self) -> Config {
        self.config
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
    }
}

/// The fake device: a listener on `127.0.0.1:0`, accepting any number of
/// connections until it is stopped or dropped.
pub struct FakeDevice {
    port: u16,
    inner: Arc<Inner>,
    accept: JoinHandle<()>,
}

impl FakeDevice {
    /// Start with [`Config::default`].
    pub async fn start() -> FakeDevice {
        Self::start_with(Config::default()).await
    }

    /// Start with an explicit configuration.
    pub async fn start_with(config: Config) -> FakeDevice {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
            .await
            .expect("bind a loopback listener");
        let port = listener.local_addr().expect("local addr").port();
        let inner = Arc::new(Inner {
            config: Mutex::new(config),
            connections: Mutex::new(Vec::new()),
        });
        let accept = tokio::spawn(accept_loop(listener, inner.clone()));
        FakeDevice {
            port,
            inner,
            accept,
        }
    }

    pub fn ip(&self) -> Ipv4Addr {
        Ipv4Addr::LOCALHOST
    }

    pub fn port(&self) -> u16 {
        self.port
    }

    /// [`ConnectOptions::default`] aimed at this fake.
    pub fn options(&self) -> ConnectOptions {
        ConnectOptions {
            port: self.port,
            ..ConnectOptions::default()
        }
    }

    /// Change the configuration for connections from now on.
    pub fn configure(&self, f: impl FnOnce(&mut Config)) {
        f(&mut self.inner.config.lock().unwrap_or_else(|e| e.into_inner()));
    }

    /// Answer `$41`/`$46` requests at `address` with `value`.
    pub fn set_value(&self, address: u32, value: u64) {
        self.configure(|c| {
            c.values.insert(address, value);
        });
    }

    /// Answer `$43`/`$47` requests at `address` with `text`.
    pub fn set_string(&self, address: u32, text: &str) {
        self.configure(|c| {
            c.strings.insert(address, text.to_string());
        });
    }

    /// Answer a `$7C` for (page, number, value) with `text`.
    pub fn set_render(&self, page: u8, number: u8, value: u16, text: &str) {
        self.configure(|c| {
            c.renders.insert((page, number, value), text.to_string());
        });
    }

    /// Every connection accepted so far, in order.
    pub fn connections(&self) -> Vec<Arc<Connection>> {
        self.inner
            .connections
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
    }

    /// The stream connections accepted so far, in order.
    pub fn streams(&self) -> Vec<Arc<Connection>> {
        self.connections()
            .into_iter()
            .filter(|c| c.is_stream())
            .collect()
    }

    /// The control connections accepted so far, in order.
    pub fn controls(&self) -> Vec<Arc<Connection>> {
        self.connections()
            .into_iter()
            .filter(|c| c.is_control())
            .collect()
    }

    /// Wait until at least `n` connections have been accepted *and* have
    /// completed their handshake, then return them.
    pub async fn wait_for_connections(&self, n: usize) -> Vec<Arc<Connection>> {
        let ready = wait_for(
            || {
                let conns = self.connections();
                conns.len() >= n && conns.iter().take(n).all(|c| c.selected().is_some())
            },
            Duration::from_secs(5),
        )
        .await;
        assert!(
            ready,
            "expected {n} connections, got {}",
            self.connections().len()
        );
        self.connections()
    }

    /// Wait for the `n`th stream connection (0-based) to be up.
    pub async fn wait_for_stream(&self, n: usize) -> Arc<Connection> {
        let ready = wait_for(
            || self.streams().len() > n && self.streams()[n].saw_preamble(),
            Duration::from_secs(5),
        )
        .await;
        assert!(ready, "stream connection {n} never came up");
        self.streams()[n].clone()
    }

    /// Wait for the `n`th control connection (0-based) to be up.
    pub async fn wait_for_control(&self, n: usize) -> Arc<Connection> {
        let ready = wait_for(
            || self.controls().len() > n && self.controls()[n].saw_preamble(),
            Duration::from_secs(5),
        )
        .await;
        assert!(ready, "control connection {n} never came up");
        self.controls()[n].clone()
    }

    /// Hang up every connection.
    pub async fn hang_up_all(&self) {
        for conn in self.connections() {
            conn.hang_up().await;
        }
    }

    /// Stop accepting and hang up everything.
    pub async fn stop(&self) {
        self.accept.abort();
        self.hang_up_all().await;
    }
}

impl Drop for FakeDevice {
    fn drop(&mut self) {
        self.accept.abort();
    }
}

/// Poll `predicate` every few milliseconds until it holds or `timeout`
/// elapses; whether it held.
pub async fn wait_for(mut predicate: impl FnMut() -> bool, timeout: Duration) -> bool {
    let deadline = tokio::time::Instant::now() + timeout;
    while tokio::time::Instant::now() < deadline {
        if predicate() {
            return true;
        }
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    predicate()
}

async fn accept_loop(listener: TcpListener, inner: Arc<Inner>) {
    let mut next_id = 0usize;
    while let Ok((stream, _)) = listener.accept().await {
        let _ = stream.set_nodelay(true);
        let (reader, writer) = stream.into_split();
        let conn = Arc::new(Connection {
            id: next_id,
            state: Mutex::new(ConnState::default()),
            writer: tokio::sync::Mutex::new(Some(writer)),
        });
        next_id += 1;
        inner
            .connections
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push(conn.clone());
        tokio::spawn(serve(inner.clone(), conn, reader));
    }
}

/// One connection, greeting to EOF.
async fn serve(inner: Arc<Inner>, conn: Arc<Connection>, mut reader: OwnedReadHalf) {
    let config = inner.config();
    let greeting: String = config
        .offers
        .iter()
        .map(|p| format!("{p}\r\n"))
        .collect::<String>()
        + ".\r\n";
    conn.write(greeting.as_bytes()).await;

    let Some(line) = read_line(&mut reader).await else {
        conn.state().closed = true;
        return;
    };
    conn.state().selected = Some(line.clone());
    if !config.offers.contains(&line) {
        conn.write(b"-NO\r\n").await;
        conn.hang_up().await;
        conn.state().closed = true;
        return;
    }
    conn.write(format!("+{line}\r\n").as_bytes()).await;
    conn.state().protocol = Some(line.clone());

    let mut preamble = [0u8; generated::SESSION_PREAMBLE_LEN];
    if reader.read_exact(&mut preamble).await.is_err() {
        conn.state().closed = true;
        return;
    }
    conn.state().saw_preamble = preamble.iter().all(|b| *b == 0);

    if line == PROTOCOL_CBOR_CONTROL {
        serve_control(inner, conn, reader).await;
    } else {
        serve_stream(inner, conn, reader).await;
    }
}

/// Read up to and including the first CRLF; the line without it.
async fn read_line(reader: &mut OwnedReadHalf) -> Option<String> {
    let mut line = Vec::new();
    loop {
        let byte = reader.read_u8().await.ok()?;
        line.push(byte);
        if line.ends_with(b"\r\n") {
            line.truncate(line.len() - 2);
            return Some(String::from_utf8_lossy(&line).to_string());
        }
    }
}

/// The MIDI3 side: unframe, record, answer what the tables can.
async fn serve_stream(inner: Arc<Inner>, conn: Arc<Connection>, mut reader: OwnedReadHalf) {
    let mut unframer = Unframer::new();
    let mut chunk = [0u8; 4096];
    loop {
        let n = match reader.read(&mut chunk).await {
            Ok(0) | Err(_) => break,
            Ok(n) => n,
        };
        let messages = unframer.push(&chunk[..n]);
        let config = inner.config();
        for message in messages {
            let reply = reply_for(&config, &message);
            conn.state().received.push(message);
            if let Some(reply) = reply {
                conn.push(&reply).await;
            }
        }
    }
    conn.state().closed = true;
}

/// The CBOR side: record, and serve the dump when the trigger arrives.
async fn serve_control(inner: Arc<Inner>, conn: Arc<Connection>, mut reader: OwnedReadHalf) {
    let mut decoder = Decoder::new();
    let mut chunk = [0u8; 4096];
    let trigger = cbor::state_dump_request();
    loop {
        let n = match reader.read(&mut chunk).await {
            Ok(0) | Err(_) => break,
            Ok(n) => n,
        };
        conn.state().raw.extend_from_slice(&chunk[..n]);
        for item in decoder.push(&chunk[..n]) {
            if item == trigger {
                conn.state().dump_triggered = true;
                let dump = inner.config().dump;
                conn.push_items(&dump).await;
            }
        }
    }
    conn.state().closed = true;
}

/// The reply a request draws from the tables, if any.
fn reply_for(config: &Config, msg: &[u8]) -> Option<Vec<u8>> {
    let (header, vals) = NrpnHeader::parse(msg)?;
    let flat = u32::from(header.page) * 128 + u32::from(header.number);
    match header.function {
        FUNCTION_REQUEST_SINGLE => {
            let value = *config.values.get(&flat)?;
            Some(set_single(0, 0, header.page, header.number, value as u16))
        }
        FUNCTION_REQUEST_STRING => {
            let text = config.strings.get(&flat)?;
            let mut payload = text.as_bytes().to_vec();
            payload.push(0);
            Some(sysex(
                0,
                0,
                FUNCTION_STRING_PARAM,
                header.page,
                header.number,
                &payload,
            ))
        }
        FUNCTION_REQUEST_EXT_PARAM => {
            let address = nrpn::ext_decode(msg.get(8..13)?) as u32;
            let value = *config.values.get(&address)?;
            let mut reply = vec![0xF0, 0x00, 0x20, 0x33, 0x00, 0x00, FUNCTION_EXT_PARAM, 0x00];
            reply.extend(nrpn::ext_encode(u64::from(address), 5));
            reply.extend(nrpn::ext_encode(value, 5));
            reply.push(0xF7);
            Some(reply)
        }
        FUNCTION_REQUEST_EXT_STRING => {
            let address = nrpn::ext_decode(msg.get(8..13)?) as u32;
            let text = config.strings.get(&address)?;
            let mut reply = vec![
                0xF0,
                0x00,
                0x20,
                0x33,
                0x00,
                0x00,
                FUNCTION_EXT_STRING_PARAM,
                0x00,
            ];
            reply.extend(nrpn::ext_encode(u64::from(address), 5));
            reply.extend_from_slice(text.as_bytes());
            reply.push(0);
            reply.push(0xF7);
            Some(reply)
        }
        FUNCTION_REQUEST_RENDERED_STRING => {
            if vals.len() < 2 {
                return None;
            }
            let value = u14(vals[0], vals[1]);
            let text = config.renders.get(&(header.page, header.number, value))?;
            let (msb, lsb) = u14_split(value);
            let mut payload = vec![msb, lsb];
            payload.extend_from_slice(text.as_bytes());
            payload.push(0);
            Some(sysex(
                0,
                0,
                FUNCTION_RENDERED_STRING_REPLY,
                header.page,
                header.number,
                &payload,
            ))
        }
        _ => None,
    }
}
