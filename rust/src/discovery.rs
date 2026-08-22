//! Async UDP discovery: broadcast the poll, collect Profiler replies.

use std::collections::BTreeMap;
use std::net::{IpAddr, Ipv4Addr, SocketAddr, SocketAddrV4};
use std::time::Duration;

use socket2::{Domain, Protocol, SockAddr, Socket, Type};
use tokio::net::UdpSocket;
use tokio::time::{Instant, timeout};

use crate::error::DiscoverError;
use crate::generated;
use crate::protocol::{DISCOVERY_PORT, TagStream, build_poll_request};

/// One raw reply from a candidate device.
#[derive(Debug, Clone)]
pub struct Reply {
    pub from: SocketAddr,
    pub payload: Vec<u8>,
}

impl Reply {
    /// The device's advertised NAME field, if the payload parses.
    pub fn name(&self) -> Option<String> {
        self.field("NAME")
    }

    /// A named 4-char key's value from the reply's tag stream, as text.
    pub fn field(&self, key: &str) -> Option<String> {
        let ts = TagStream::parse(&self.payload).ok()?;
        ts.key_values()
            .into_iter()
            .find(|(k, _)| k == key)
            .map(|(_, v)| String::from_utf8_lossy(&v).into_owned())
    }

    /// The sender's IPv4 address, if it was IPv4.
    pub fn ipv4(&self) -> Option<Ipv4Addr> {
        match self.from.ip() {
            IpAddr::V4(v4) => Some(v4),
            IpAddr::V6(_) => None,
        }
    }
}

/// Whether a packet is a discovery POLL (ours or another client's) rather
/// than a device reply. Polls carry a `POLL` field; replies carry NAME/SER#/….
fn is_poll(pkt: &[u8]) -> bool {
    TagStream::parse(pkt)
        .map(|ts| ts.key_values().iter().any(|(k, _)| k == "POLL"))
        .unwrap_or(false)
}

/// Options controlling a discovery run.
#[derive(Debug, Clone)]
pub struct Options {
    /// Client MAC placed in the poll (all-zero placeholder is fine).
    pub mac: String,
    /// How long to keep listening for replies.
    pub listen_for: Duration,
    /// Re-send the poll every this often while listening.
    pub repeat_every: Duration,
    /// Extra explicit targets to unicast/broadcast the poll to (e.g. a known device IP).
    pub extra_targets: Vec<Ipv4Addr>,
    /// Print targets and every raw packet seen (including our own echoed poll).
    pub verbose: bool,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            mac: generated::POLL_PLACEHOLDER_MAC.to_string(),
            listen_for: Duration::from_secs(3),
            repeat_every: Duration::from_millis(generated::POLL_INTERVAL_MS),
            extra_targets: Vec::new(),
            verbose: false,
        }
    }
}

/// Exclusive owner of the UDP discovery port.
///
/// Acquire one before opening a session and keep it for the session's lifetime.
/// Holding it does two things: it guarantees every reply reaches *this* process —
/// no other socket can bind the port while it is open — and it fails loudly, up
/// front, if the port is already taken, rather than letting discovery quietly come
/// up empty later. See [`DiscoverError::PortUnavailable`].
///
/// [`poll`](Self::poll) may be called as often as needed on a held port, which is
/// what a long-running client wants: re-poll to notice Profilers appearing and
/// disappearing, without ever letting go of the port in between.
///
/// ```no_run
/// # async fn run() -> Result<(), libkp::DiscoverError> {
/// let port = libkp::DiscoveryPort::acquire()?;
/// let replies = port.poll(&libkp::Options::default()).await?;
/// # Ok(()) }
/// ```
///
/// The port is released when the value is dropped.
#[derive(Debug)]
pub struct DiscoveryPort {
    socket: UdpSocket,
    port: u16,
}

impl DiscoveryPort {
    /// Take exclusive ownership of the standard discovery port.
    ///
    /// Returns [`DiscoverError::PortUnavailable`] if another process holds it.
    pub fn acquire() -> Result<Self, DiscoverError> {
        Self::acquire_on(DISCOVERY_PORT)
    }

    /// Take exclusive ownership of `port`. Mostly useful for tests.
    pub fn acquire_on(port: u16) -> Result<Self, DiscoverError> {
        Ok(Self {
            socket: bind_broadcast_socket(port)?,
            port,
        })
    }

    /// The port held.
    pub fn port(&self) -> u16 {
        self.port
    }

    /// Broadcast the poll and gather replies until the listen window ends.
    ///
    /// Returns one [`Reply`] per distinct source address (last payload wins).
    pub async fn poll(&self, opts: &Options) -> Result<Vec<Reply>, DiscoverError> {
        let socket = &self.socket;
        let poll = build_poll_request(&opts.mac);
        let targets = broadcast_targets(&opts.extra_targets)?;

        if opts.verbose {
            eprintln!("poll targets ({}):", targets.len());
            for t in &targets {
                eprintln!("  {t}:{}", self.port);
            }
        }

        // Fire an initial round immediately.
        send_round(socket, &poll, &targets, self.port).await?;

        let deadline = Instant::now() + opts.listen_for;
        // `checked_add` guards against a very large `repeat_every`; on overflow we
        // simply never re-poll within the window.
        let repoll_at = |from: Instant| from.checked_add(opts.repeat_every).unwrap_or(deadline);
        let mut next_poll = repoll_at(Instant::now());
        // Keyed by source IP: the device answers from a fresh ephemeral port each
        // poll, so keying by full SocketAddr would report one "device" per reply.
        let mut replies: BTreeMap<IpAddr, (SocketAddr, Vec<u8>)> = BTreeMap::new();
        let mut buf = vec![0u8; 2048];

        loop {
            let now = Instant::now();
            if now >= deadline {
                break;
            }
            let recv_window = (deadline - now).min(next_poll.saturating_duration_since(now));

            match timeout(recv_window, socket.recv_from(&mut buf)).await {
                Ok(Ok((n, from))) => {
                    let pkt = &buf[..n];
                    if opts.verbose {
                        eprintln!(
                            "recv {n} bytes from {from}: {}",
                            crate::fmt::hex_inline(pkt)
                        );
                    }
                    // Ignore any POLL packet — our own echoed back, or another
                    // client's broadcast — since recording one as a "device"
                    // would return a random client's IP.
                    if !is_poll(pkt) {
                        replies.insert(from.ip(), (from, pkt.to_vec()));
                    }
                }
                Ok(Err(e)) => return Err(DiscoverError::Recv(e)),
                Err(_elapsed) => {} // window expired; fall through to maybe re-poll
            }

            if Instant::now() >= next_poll {
                send_round(socket, &poll, &targets, self.port).await?;
                next_poll = repoll_at(Instant::now());
            }
        }

        Ok(replies
            .into_values()
            .map(|(from, payload)| Reply { from, payload })
            .collect())
    }
}

/// Acquire the discovery port, poll once, and release it.
///
/// A convenience for one-shot callers such as a CLI. Anything that goes on to open
/// a session should hold a [`DiscoveryPort`] across the session instead, so no
/// other process can take the port midway through.
pub async fn discover(opts: &Options) -> Result<Vec<Reply>, DiscoverError> {
    DiscoveryPort::acquire()?.poll(opts).await
}

async fn send_round(
    socket: &UdpSocket,
    poll: &[u8],
    targets: &[Ipv4Addr],
    port: u16,
) -> Result<(), DiscoverError> {
    // A single unreachable target (e.g. global broadcast denied by a firewall)
    // should not abort the whole sweep; only fail if every send failed.
    let mut sent = 0usize;
    let mut last_err: Option<DiscoverError> = None;
    for ip in targets {
        let addr = SocketAddr::V4(SocketAddrV4::new(*ip, port));
        match socket.send_to(poll, addr).await {
            Ok(_) => sent += 1,
            Err(e) => last_err = Some(DiscoverError::Send { addr, source: e }),
        }
    }
    match (sent, last_err) {
        (0, Some(e)) => Err(e),
        _ => Ok(()),
    }
}

/// Convenience: discover for `listen_for` and return the first device found.
pub async fn find_first(listen_for: Duration) -> Result<Option<Reply>, DiscoverError> {
    let opts = Options {
        listen_for,
        ..Default::default()
    };
    Ok(discover(&opts).await?.into_iter().next())
}

/// Create a UDP socket for broadcast discovery, bound **exclusively** to `port`.
///
/// Neither `SO_REUSEADDR` nor `SO_REUSEPORT` is set, and that is deliberate. The
/// device answers a poll only on this port, and the kernel delivers each arriving
/// datagram to just one of the sockets bound to it — a second listener does not
/// see a copy of the reply, it takes it. Sharing the port would make discovery a
/// coin flip; refusing to share makes the clash loud instead, as
/// [`DiscoverError::PortUnavailable`], and holds the port against all comers for
/// as long as the socket is open.
fn bind_broadcast_socket(port: u16) -> Result<UdpSocket, DiscoverError> {
    let socket = Socket::new(Domain::IPV4, Type::DGRAM, Some(Protocol::UDP))
        .map_err(DiscoverError::SocketCreate)?;

    socket
        .set_broadcast(true)
        .map_err(|source| DiscoverError::SocketOption {
            option: "SO_BROADCAST",
            source,
        })?;
    socket
        .set_nonblocking(true)
        .map_err(|source| DiscoverError::SocketOption {
            option: "O_NONBLOCK",
            source,
        })?;

    let bind_addr = SocketAddr::new(IpAddr::V4(Ipv4Addr::UNSPECIFIED), port);
    socket
        .bind(&SockAddr::from(bind_addr))
        .map_err(|source| match source.kind() {
            std::io::ErrorKind::AddrInUse => DiscoverError::PortUnavailable { port, source },
            _ => DiscoverError::Bind {
                addr: bind_addr,
                source,
            },
        })?;

    let std_sock: std::net::UdpSocket = socket.into();
    UdpSocket::from_std(std_sock).map_err(DiscoverError::SocketCreate)
}

/// Broadcast targets: every local IPv4 interface's subnet-broadcast address,
/// plus the global 255.255.255.255, plus any caller-supplied extras.
fn broadcast_targets(extra: &[Ipv4Addr]) -> Result<Vec<Ipv4Addr>, DiscoverError> {
    let mut targets = vec![Ipv4Addr::BROADCAST];

    let ifaces = if_addrs::get_if_addrs().map_err(DiscoverError::Interfaces)?;
    for iface in ifaces {
        if iface.is_loopback() {
            continue;
        }
        if let if_addrs::IfAddr::V4(v4) = iface.addr {
            let bcast = v4.broadcast.unwrap_or_else(|| {
                // ip | !netmask
                let ip = u32::from(v4.ip);
                let mask = u32::from(v4.netmask);
                Ipv4Addr::from(ip | !mask)
            });
            if !targets.contains(&bcast) {
                targets.push(bcast);
            }
        }
    }

    for ip in extra {
        if !targets.contains(ip) {
            targets.push(*ip);
        }
    }
    Ok(targets)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn poll_packets_are_not_treated_as_replies() {
        assert!(is_poll(&build_poll_request("00:00:00:00:00:00")));
        // A reply-shaped payload (NAME field) is not a poll.
        let reply = b"\x0bNAMEKemper\x00";
        assert!(!is_poll(reply));
    }

    #[test]
    fn reply_name_reads_the_name_field() {
        let r = Reply {
            from: "192.168.1.50:5727".parse().unwrap(),
            payload: b"\x0bNAMEKemper\x00".to_vec(),
        };
        assert_eq!(r.name().as_deref(), Some("Kemper"));
        assert_eq!(r.ipv4(), Some(Ipv4Addr::new(192, 168, 1, 50)));
    }

    #[test]
    fn broadcast_targets_include_global() {
        let t = broadcast_targets(&[Ipv4Addr::new(10, 0, 0, 7)]).unwrap();
        assert!(t.contains(&Ipv4Addr::BROADCAST));
        assert!(t.contains(&Ipv4Addr::new(10, 0, 0, 7)));
    }

    /// A second acquire must fail rather than quietly share the port. Sharing is
    /// the failure this guards against: the kernel gives an arriving reply to
    /// exactly one bound socket, so a co-bound listener steals replies instead of
    /// duplicating them.
    #[tokio::test]
    async fn the_port_is_held_exclusively() {
        let first = DiscoveryPort::acquire_on(54341).expect("first acquire");
        assert_eq!(first.port(), 54341);

        match DiscoveryPort::acquire_on(54341) {
            Err(DiscoverError::PortUnavailable { port, .. }) => {
                assert_eq!(port, 54341);
            }
            other => panic!("expected PortUnavailable, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn releasing_the_port_lets_it_be_acquired_again() {
        drop(DiscoveryPort::acquire_on(54342).expect("first acquire"));
        // No leak: the first release freed it.
        drop(DiscoveryPort::acquire_on(54342).expect("second acquire"));
    }

    /// A long-running client re-polls to notice devices coming and going, without
    /// ever releasing the port in between. Our own echoed poll must not be
    /// mistaken for a device.
    #[tokio::test]
    async fn a_held_port_can_be_polled_repeatedly() {
        let port = DiscoveryPort::acquire_on(54343).expect("acquire");
        let opts = Options {
            listen_for: Duration::from_millis(100),
            repeat_every: Duration::from_millis(50),
            extra_targets: vec![Ipv4Addr::LOCALHOST],
            ..Default::default()
        };
        for _ in 0..2 {
            assert!(port.poll(&opts).await.expect("poll").is_empty());
        }
    }
}
