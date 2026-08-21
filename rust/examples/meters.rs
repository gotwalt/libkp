//! `meters` — a live full-screen **ratatui** view of a Profiler.
//!
//! Connects (discovering the device if no `--ip` is given), performs the
//! read-only initial rig sync, then drives a full-screen terminal UI from the
//! library's [`DeviceModel`]:
//!
//! - the **slow lane** — rig name / author / tempo / morph, amp and cabinet, and
//!   the eight effect blocks with their on/off state and type — bound to
//!   [`DeviceModel::subscribe`], which emits a fresh snapshot only when
//!   snapshot-visible state actually changes; and
//! - the **fast lane** — the realtime status block (tuner strobe, level bars,
//!   beat pulse) — polled from [`DeviceModel::status`] once per frame.
//!
//! The realtime field identities (strobe phase/segments, the three level taps)
//! come from the shared spec's `METER_FIELDS` table. The strobe drift-rate
//! verdict and the peak-hold decay are computed here from those raw fields.
//!
//! ```text
//! cargo run --example meters -- --ip 192.168.1.50
//! cargo run --example meters -- --all --fps 60
//! ```

use std::collections::VecDeque;
use std::io::{self, Stdout};
use std::net::Ipv4Addr;
use std::time::{Duration, Instant};

use clap::Parser;
use crossterm::event::{self, Event, KeyCode, KeyEventKind, KeyModifiers};
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use crossterm::{execute, ExecutableCommand};
use ratatui::backend::CrosstermBackend;
use ratatui::layout::{Alignment, Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style, Stylize};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Gauge, Paragraph, Wrap};
use ratatui::{Frame, Terminal};
use tokio::sync::broadcast::error::TryRecvError;

use libkp::generated;
use libkp::model::{DeviceEvent, DeviceModel, RealtimeStatus};
use libkp::state::DeviceState;
use libkp::{params, PORT};

/// Number of values in the realtime status block.
const N: usize = generated::METER_COUNT;
/// Full scale of a 14-bit parameter value.
const FULL_SCALE: f64 = generated::FULL_SCALE as f64;

/// Live terminal view of a Kemper Profiler: rig, effect blocks, and meters.
#[derive(Parser, Debug)]
#[command(name = "meters", version, about)]
struct Cli {
    /// Device IPv4 address. If omitted, discovery finds one.
    #[arg(long)]
    ip: Option<Ipv4Addr>,

    /// Start with the raw/extra realtime meter fields visible (toggle with `a`).
    #[arg(long)]
    all: bool,

    /// Redraw cap, in frames per second.
    #[arg(long, default_value_t = 60, value_parser = clap::value_parser!(u32).range(5..=240))]
    fps: u32,
}

type BoxErr = Box<dyn std::error::Error>;

#[tokio::main]
async fn main() {
    if let Err(e) = run(Cli::parse()).await {
        // The guard (if installed) restores the terminal on drop; this covers
        // errors before it exists (e.g. discovery/connect failures).
        eprintln!("\nerror: {e}");
        std::process::exit(1);
    }
}

async fn run(cli: Cli) -> Result<(), BoxErr> {
    let ip = match cli.ip {
        Some(ip) => ip,
        None => find_device()
            .await?
            .ok_or("no device found; pass --ip <addr>")?,
    };

    eprintln!("Connecting to {ip}:{PORT} ...");
    // `connect` opens the stream and issues the read-only initial rig sync
    // (rig/amp/cab names plus every effect slot's Type and On/Off).
    let model = DeviceModel::connect(ip).await?;
    let mut snapshots = model.subscribe();
    let mut events = model.events();

    let mut view = View::new(ip, cli.all);
    view.snapshot = model.state();

    // Everything from here on must go through the guard so the terminal is
    // restored on any exit path — clean return, `?` bail, or a panic.
    let mut term = TerminalGuard::new()?;

    let frame = Duration::from_secs_f64(1.0 / cli.fps as f64);
    let mut next_frame = Instant::now();

    loop {
        // Drain crossterm input non-blockingly, interleaved with the streams.
        // Poll with a small timeout so we never block the render cadence.
        let poll_budget = next_frame.saturating_duration_since(Instant::now());
        if event::poll(poll_budget.min(Duration::from_millis(15)))? {
            match event::read()? {
                Event::Key(k) if k.kind == KeyEventKind::Press => match k.code {
                    KeyCode::Char('q') | KeyCode::Esc => break,
                    KeyCode::Char('c') if k.modifiers.contains(KeyModifiers::CONTROL) => break,
                    KeyCode::Char('a') => view.all = !view.all,
                    _ => {}
                },
                Event::Resize(_, _) => {}
                _ => {}
            }
        }

        // Slow lane: take the newest snapshot the store has published.
        loop {
            match snapshots.try_recv() {
                Ok(s) => view.snapshot = s,
                Err(TryRecvError::Lagged(_)) => continue,
                Err(TryRecvError::Empty) | Err(TryRecvError::Closed) => break,
            }
        }
        // Granular deltas: the beat pulse and the last-parameter line.
        loop {
            match events.try_recv() {
                Ok(ev) => view.ingest_event(&ev),
                Err(TryRecvError::Lagged(_)) => continue,
                Err(TryRecvError::Empty) | Err(TryRecvError::Closed) => break,
            }
        }
        // Fast lane: the meter frame, polled once per rendered frame.
        view.ingest_status(model.status());

        if Instant::now() >= next_frame {
            let rate = view.rate();
            let strobe_rate = view.strobe_rate();
            term.terminal.draw(|f| view.draw(f, rate, strobe_rate))?;
            next_frame += frame;
            // If we fell far behind (e.g. suspended), resync the cadence.
            if next_frame < Instant::now() {
                next_frame = Instant::now() + frame;
            }
        }

        if view.snapshot.connection == libkp::Connection::Disconnected {
            drop(term);
            eprintln!("device closed the connection");
            return Ok(());
        }

        // Yield so the ingest task and timers make progress.
        tokio::task::yield_now().await;
    }

    Ok(())
}

/// Discover a device and return its IPv4.
async fn find_device() -> Result<Option<Ipv4Addr>, BoxErr> {
    eprintln!("No --ip given; discovering...");
    let Some(reply) = libkp::find_first(Duration::from_secs(3)).await? else {
        return Ok(None);
    };
    if let Some(name) = reply.name() {
        eprintln!("Discovered \"{name}\" at {}", reply.from.ip());
    }
    Ok(reply.ipv4())
}

// ---------------------------------------------------------------------------
// terminal guard — always restores raw mode / alt-screen / cursor on drop
// ---------------------------------------------------------------------------

/// Owns the terminal in raw mode on the alternate screen. Its `Drop` restores
/// the terminal on every exit path — a clean return, a `?` bail, or a panic
/// unwinding through `run` — so the shell is never left wrecked.
struct TerminalGuard {
    terminal: Terminal<CrosstermBackend<Stdout>>,
}

impl TerminalGuard {
    fn new() -> io::Result<Self> {
        enable_raw_mode()?;
        let mut stdout = io::stdout();
        execute!(stdout, EnterAlternateScreen)?;
        let mut terminal = Terminal::new(CrosstermBackend::new(stdout))?;
        terminal.hide_cursor()?;
        terminal.clear()?;
        Ok(Self { terminal })
    }
}

impl Drop for TerminalGuard {
    fn drop(&mut self) {
        let _ = disable_raw_mode();
        let _ = self.terminal.backend_mut().execute(LeaveAlternateScreen);
        let _ = self.terminal.show_cursor();
    }
}

// ---------------------------------------------------------------------------
// view state
// ---------------------------------------------------------------------------

struct View {
    ip: Ipv4Addr,
    all: bool,
    /// Latest coalesced store snapshot (the slow lane).
    snapshot: DeviceState,
    /// Latest realtime frame (the fast lane).
    values: [u16; N],
    peaks: [f64; N],
    frames: u64,
    recent: VecDeque<Instant>,
    last_frame: Instant,
    last_param: Option<String>,
    /// Last beat-pulse edge (time, value).
    last_pulse: Option<(Instant, u16)>,
    /// Recent (time, strobe phase) samples for the drift-rate estimate.
    phase_hist: VecDeque<(Instant, u16)>,
}

/// Trailing window for the update-rate estimate.
const RATE_WINDOW: Duration = Duration::from_secs(2);
/// Trailing window for the strobe drift-rate estimate.
const STROBE_WINDOW: Duration = Duration::from_millis(400);
/// |drift| below this (counts/sec) reads as in tune.
const IN_TUNE_DRIFT: f64 = 600.0;

/// The strobe drift verdict, derived from the wrap-aware drift rate.
enum Verdict {
    Idle,
    Unknown,
    InTune,
    Sharp,
    Flat,
}

impl View {
    fn new(ip: Ipv4Addr, all: bool) -> Self {
        Self {
            ip,
            all,
            snapshot: DeviceState::new(),
            values: [0; N],
            peaks: [0.0; N],
            frames: 0,
            recent: VecDeque::new(),
            last_frame: Instant::now(),
            last_param: None,
            last_pulse: None,
            phase_hist: VecDeque::new(),
        }
    }

    /// Fold one realtime frame in, tracking peak-hold and strobe history.
    fn ingest_status(&mut self, status: RealtimeStatus) {
        if status.raw != self.values {
            self.frames += 1;
            self.recent.push_back(Instant::now());
        }
        self.values = status.raw;
        for i in 0..N {
            self.peaks[i] = self.peaks[i].max(self.values[i] as f64);
        }
        if self.strobe_active() {
            self.phase_hist
                .push_back((Instant::now(), status.strobe_phase()));
        } else {
            self.phase_hist.clear(); // tuner idle
        }
        self.decay();
    }

    /// Fold one granular device event in.
    fn ingest_event(&mut self, ev: &DeviceEvent) {
        match ev {
            DeviceEvent::BeatPulse { on } => {
                self.last_pulse = Some((Instant::now(), u16::from(*on)));
            }
            DeviceEvent::ParamChanged {
                page,
                number,
                value,
            } => {
                self.last_param = Some(format!(
                    "{} = {value}",
                    params::describe_numeric(*page, *number)
                ));
            }
            DeviceEvent::TempoBpm(bpm) => self.last_param = Some(format!("Tempo = {bpm} BPM")),
            DeviceEvent::MorphChanged(v) => self.last_param = Some(format!("Morph = {v}")),
            DeviceEvent::TunerNote(n) => self.last_param = Some(format!("Tuner Note = {n}")),
            DeviceEvent::EffectChanged { slot } => {
                if let Some(fx) = self.snapshot.effects.get(*slot) {
                    self.last_param = Some(format!("Effect {} changed", fx.slot));
                }
            }
            DeviceEvent::RenderedString { text, .. } => {
                self.last_param = Some(format!("rendered \"{text}\""))
            }
            _ => {}
        }
    }

    /// True when the tuner strobe fields carry any signal.
    fn strobe_active(&self) -> bool {
        let segs = generated::STROBE_SEGMENT_INDICES;
        segs.iter().any(|&i| self.values[i] > 0) || self.values[generated::STROBE_PHASE_INDEX] > 0
    }

    /// Decay the peak-hold markers, frame-rate independently, never below the
    /// current value.
    fn decay(&mut self) {
        let now = Instant::now();
        let dt = (now - self.last_frame).as_secs_f64().min(0.5);
        self.last_frame = now;
        let drop = FULL_SCALE * 0.8 * dt; // a full-scale peak falls in ~1.25 s
        for i in 0..N {
            self.peaks[i] = (self.peaks[i] - drop).max(self.values[i] as f64);
        }
    }

    /// Frames/sec over the trailing [`RATE_WINDOW`].
    fn rate(&mut self) -> f64 {
        let cutoff = Instant::now() - RATE_WINDOW;
        while self.recent.front().is_some_and(|&t| t < cutoff) {
            self.recent.pop_front();
        }
        self.recent.len() as f64 / RATE_WINDOW.as_secs_f64()
    }

    /// Wrap-aware strobe drift rate in counts/sec over the last ~0.4 s.
    /// Positive = phase rising (flat direction), negative = falling (sharp).
    fn strobe_rate(&mut self) -> Option<f64> {
        let now = Instant::now();
        while self
            .phase_hist
            .front()
            .is_some_and(|&(t, _)| now - t > STROBE_WINDOW)
        {
            self.phase_hist.pop_front();
        }
        if self.phase_hist.len() < 3 {
            return None;
        }
        let mut total = 0.0;
        for (&(_, a), &(_, b)) in self.phase_hist.iter().zip(self.phase_hist.iter().skip(1)) {
            // shortest wrapped step from a to b on a 0..16384 circle
            let d = ((b as i32 - a as i32 + 8192).rem_euclid(16384)) - 8192;
            total += d as f64;
        }
        let dt = (self.phase_hist.back()?.0 - self.phase_hist.front()?.0).as_secs_f64();
        (dt > 0.05).then(|| total / dt)
    }

    /// The in-tune / sharp / flat / idle verdict from the strobe drift rate.
    fn verdict(&self, strobe_rate: Option<f64>) -> Verdict {
        match (self.strobe_active(), strobe_rate) {
            (false, _) => Verdict::Idle,
            (true, None) => Verdict::Unknown,
            (true, Some(r)) if r.abs() < IN_TUNE_DRIFT => Verdict::InTune,
            (true, Some(r)) if r < 0.0 => Verdict::Sharp,
            (true, Some(_)) => Verdict::Flat,
        }
    }

    // -- rendering ----------------------------------------------------------

    /// Draw the whole UI. Pure over the current view state plus the two derived
    /// rates, so it is exercisable without a live socket.
    fn draw(&self, f: &mut Frame, rate: f64, strobe_rate: Option<f64>) {
        let area = f.area();
        // Vertical stack: header, chain, effect blocks, tuner, meters, footer.
        let bars = self.visible_bars();
        let rows = Layout::default()
            .direction(Direction::Vertical)
            .constraints([
                Constraint::Length(5),               // header (3 lines + border)
                Constraint::Length(4),               // signal chain (2 + border)
                Constraint::Length(4),               // effect blocks (2 + border)
                Constraint::Length(4),               // tuner (2 + border)
                Constraint::Length(2 + bars as u16), // meters (bars + border)
                Constraint::Min(3),                  // footer (grows when tall)
            ])
            .split(area);

        self.draw_header(f, rows[0], rate);
        self.draw_chain(f, rows[1]);
        self.draw_blocks(f, rows[2]);
        self.draw_tuner(f, rows[3], strobe_rate);
        self.draw_meters(f, rows[4]);
        self.draw_footer(f, rows[5]);
    }

    /// A titled, rounded block in the app's accent frame color.
    fn panel(title: &str) -> Block<'_> {
        Block::default()
            .borders(Borders::ALL)
            .border_type(BorderType::Rounded)
            .border_style(Style::default().fg(Color::Rgb(129, 140, 248)))
            .title(Span::styled(
                format!(" {title} "),
                Style::default().fg(Color::Rgb(129, 140, 248)).bold(),
            ))
    }

    fn draw_header(&self, f: &mut Frame, area: Rect, rate: f64) {
        let rig = &self.snapshot.rig;
        let name = rig.name.as_deref().unwrap_or("(no rig yet)");

        let mut meta: Vec<Span> = Vec::new();
        if let Some(a) = rig.author.as_deref().filter(|s| !s.is_empty()) {
            meta.push(Span::styled(format!("by {a}"), Style::default().dim()));
            meta.push(Span::raw("   "));
        }
        if let Some(bpm) = rig.tempo_bpm {
            meta.push(Span::styled(format!("{bpm} BPM"), Style::default().fg(Color::Yellow)));
            meta.push(Span::raw("   "));
        }
        if let Some(m) = self.snapshot.morph {
            meta.push(Span::styled(
                format!("morph {:.0}%", m as f64 / FULL_SCALE * 100.0),
                Style::default().fg(Color::Magenta),
            ));
        }

        let conn = self.snapshot.connection == libkp::Connection::Connected;
        let (dot, dot_style, label) = if conn {
            ("●", Style::default().fg(Color::Green), "connected")
        } else {
            ("○", Style::default().dim(), "disconnected")
        };
        let status_line = Line::from(vec![
            Span::styled(dot, dot_style),
            Span::raw(" "),
            Span::styled(format!("{label}  "), Style::default().dim()),
            Span::styled(
                format!("{}:{PORT}   ", self.ip),
                Style::default().fg(Color::Cyan),
            ),
            Span::styled(
                format!("{} frames ({rate:.0}/s)", self.frames),
                Style::default().dim(),
            ),
        ]);

        let lines = vec![
            Line::from(Span::styled(
                name.to_string(),
                Style::default()
                    .fg(Color::White)
                    .add_modifier(Modifier::BOLD),
            )),
            Line::from(meta),
            status_line,
        ];
        let block = Self::panel("RIG");
        f.render_widget(Paragraph::new(lines).block(block), area);
    }

    fn draw_chain(&self, f: &mut Frame, area: Rect) {
        let amp = &self.snapshot.amp;
        let cab = &self.snapshot.cabinet;

        let (amp_dot, amp_style) = on_dot(amp.on);
        let gain = amp
            .gain
            .map(|g| format!("  gain {:.0}%", g as f64 / FULL_SCALE * 100.0))
            .unwrap_or_default();
        let amp_line = Line::from(vec![
            Span::styled(amp_dot, amp_style),
            Span::styled("  AMP  ", Style::default().bold()),
            Span::styled(
                amp.name.as_deref().unwrap_or("—").to_string(),
                Style::default().fg(Color::White),
            ),
            Span::styled(gain, Style::default().dim()),
        ]);

        let (cab_dot, cab_style) = on_dot(cab.on);
        let cab_line = Line::from(vec![
            Span::styled(cab_dot, cab_style),
            Span::styled("  CAB  ", Style::default().bold()),
            Span::styled(
                cab.name.as_deref().unwrap_or("—").to_string(),
                Style::default().fg(Color::White),
            ),
        ]);

        let block = Self::panel("SIGNAL CHAIN");
        f.render_widget(
            Paragraph::new(vec![amp_line, cab_line]).block(block),
            area,
        );
    }

    /// The eight effect blocks as a responsive grid: two rows of four compact
    /// cells, each a colored dot + slot label + effect type. No per-cell borders
    /// (the single panel frame carries the chrome), so it stays legible at 80×24.
    fn draw_blocks(&self, f: &mut Frame, area: Rect) {
        let block = Self::panel("EFFECT BLOCKS");
        let inner = block.inner(area);
        f.render_widget(block, area);

        let rows = Layout::default()
            .direction(Direction::Vertical)
            .constraints([Constraint::Length(1), Constraint::Length(1)])
            .split(inner);

        for (r, chunk) in self.snapshot.effects.chunks(4).enumerate() {
            if r >= rows.len() {
                break;
            }
            let cells = Layout::default()
                .direction(Direction::Horizontal)
                .constraints([Constraint::Ratio(1, 4); 4])
                .split(rows[r]);
            for (c, fx) in chunk.iter().enumerate() {
                self.draw_effect_cell(f, cells[c], fx);
            }
        }
    }

    fn draw_effect_cell(&self, f: &mut Frame, area: Rect, fx: &libkp::Effect) {
        let (dot, dot_style) = on_dot(fx.on);
        let empty = fx.is_empty();
        let label = match (empty, fx.type_name(), fx.kind) {
            (true, _, _) | (_, _, None) => "—".to_string(),
            (_, Some(n), _) => n.to_string(),
            (_, None, Some(k)) => format!("type {k}"),
        };
        let name_style = if empty || fx.on != Some(true) {
            Style::default().dim()
        } else {
            Style::default().fg(Color::White)
        };
        let line = Line::from(vec![
            Span::styled(dot, dot_style),
            Span::raw(" "),
            Span::styled(format!("{:<3}", fx.slot), Style::default().bold()),
            Span::raw(" "),
            Span::styled(label, name_style),
        ]);
        f.render_widget(Paragraph::new(line).wrap(Wrap { trim: true }), area);
    }

    /// The tuner strobe row: a drifting marker plus an in-tune verdict derived
    /// from the strobe-phase drift rate.
    fn draw_tuner(&self, f: &mut Frame, area: Rect, strobe_rate: Option<f64>) {
        let block = Self::panel("TUNER");
        let inner = block.inner(area);
        f.render_widget(block, area);

        let verdict = self.verdict(strobe_rate);
        let (glyph, vstyle, marker_style) = match verdict {
            Verdict::Idle => (
                "idle",
                Style::default().dim(),
                Style::default().dim(),
            ),
            Verdict::Unknown => (
                "…",
                Style::default().dim(),
                Style::default().dim(),
            ),
            Verdict::InTune => (
                "● in tune",
                Style::default().fg(Color::Green).bold(),
                Style::default().fg(Color::Green),
            ),
            Verdict::Sharp => (
                "← sharp",
                Style::default().fg(Color::Red).bold(),
                Style::default().fg(Color::Red),
            ),
            Verdict::Flat => (
                "→ flat",
                Style::default().fg(Color::Cyan).bold(),
                Style::default().fg(Color::Cyan),
            ),
        };

        // Split the inner area: strobe strip on top, verdict + note below.
        let parts = Layout::default()
            .direction(Direction::Vertical)
            .constraints([Constraint::Length(1), Constraint::Length(1)])
            .split(inner);

        let active = self.strobe_active();
        let width = parts[0].width.max(1) as usize;
        let phase = self.values[generated::STROBE_PHASE_INDEX];
        let pos = ((phase as f64 / FULL_SCALE) * (width.saturating_sub(1)) as f64) as usize;
        let mut spans: Vec<Span> = Vec::with_capacity(width);
        for i in 0..width {
            if active && i == pos {
                spans.push(Span::styled("◆", marker_style));
            } else {
                spans.push(Span::styled("·", Style::default().dim()));
            }
        }
        f.render_widget(Paragraph::new(Line::from(spans)), parts[0]);

        let mut verdict_spans = vec![Span::styled(glyph, vstyle)];
        if let Some(n) = self.snapshot.tuner.note {
            verdict_spans.push(Span::raw("     "));
            verdict_spans.push(Span::styled(
                note_name(n),
                Style::default().fg(Color::White).bold(),
            ));
        }
        f.render_widget(Paragraph::new(Line::from(verdict_spans)), parts[1]);
    }

    /// The level bars — the three taps by default, all eleven with `--all`.
    fn draw_meters(&self, f: &mut Frame, area: Rect) {
        let block = Self::panel("METERS");
        let inner = block.inner(area);
        f.render_widget(block, area);

        let count = self.visible_bars();
        if count == 0 || inner.height == 0 {
            return;
        }
        let rows = Layout::default()
            .direction(Direction::Vertical)
            .constraints(vec![Constraint::Length(1); count])
            .split(inner);

        let mut r = 0usize;
        for &(index, _number, _id, name, render) in params::meter_fields() {
            let is_bar = render == "bar";
            if !is_bar && !self.all {
                continue;
            }
            if r >= rows.len() {
                break;
            }
            self.draw_bar_row(f, rows[r], index, name);
            r += 1;
        }
    }

    fn draw_bar_row(&self, f: &mut Frame, area: Rect, index: usize, name: &str) {
        // label | gauge | value, laid out horizontally.
        let cols = Layout::default()
            .direction(Direction::Horizontal)
            .constraints([
                Constraint::Length(16),
                Constraint::Min(10),
                Constraint::Length(7),
            ])
            .split(area);

        let frac = (self.values[index] as f64 / FULL_SCALE).clamp(0.0, 1.0);
        let peak_frac = (self.peaks[index] / FULL_SCALE).clamp(0.0, 1.0);
        let color = level_color(frac);

        let label = Paragraph::new(Line::from(Span::styled(
            short_label(name),
            Style::default().dim(),
        )));
        f.render_widget(label, cols[0]);

        let gauge = Gauge::default()
            .gauge_style(Style::default().fg(color))
            .ratio(frac)
            .label(peak_label(peak_frac));
        f.render_widget(gauge, cols[1]);

        let value = Paragraph::new(Line::from(Span::styled(
            format!("{:>5}", self.values[index]),
            Style::default().fg(Color::White),
        )))
        .alignment(Alignment::Right);
        f.render_widget(value, cols[2]);
    }

    fn draw_footer(&self, f: &mut Frame, area: Rect) {
        let block = Self::panel("STATUS");
        let inner = block.inner(area);
        f.render_widget(block, area);

        // Tempo indicator: flash for 150 ms after a rising pulse edge.
        let tempo = match self.last_pulse {
            Some((t, v)) if t.elapsed() < Duration::from_millis(150) && v != 0 => {
                Span::styled("♩ ●", Style::default().fg(Color::LightYellow).bold())
            }
            Some(_) => Span::styled("♩ ·", Style::default().dim()),
            None => Span::styled("♩ (no pulse seen)", Style::default().dim()),
        };
        let tempo_line = Line::from(vec![
            Span::styled("tempo   ", Style::default().dim()),
            tempo,
        ]);

        let param_line = Line::from(vec![
            Span::styled("param   ", Style::default().dim()),
            Span::styled(
                self.last_param.as_deref().unwrap_or("(none)").to_string(),
                Style::default().fg(Color::White),
            ),
        ]);

        let help = Line::from(Span::styled(
            "q / Ctrl-C quit    a toggle raw fields    play into the device to see meters move",
            Style::default().dim(),
        ));

        f.render_widget(
            Paragraph::new(vec![tempo_line, param_line, Line::raw(""), help])
                .wrap(Wrap { trim: true }),
            inner,
        );
    }

    /// How many bar rows are visible right now.
    fn visible_bars(&self) -> usize {
        if self.all {
            N
        } else {
            params::meter_fields()
                .iter()
                .filter(|(_, _, _, _, render)| *render == "bar")
                .count()
        }
    }
}

/// On/off/unknown state as a colored dot.
fn on_dot(on: Option<bool>) -> (&'static str, Style) {
    match on {
        Some(true) => ("●", Style::default().fg(Color::Green)),
        Some(false) => ("○", Style::default().dim()),
        None => ("·", Style::default().fg(Color::DarkGray)),
    }
}

/// Green→yellow→red by level.
fn level_color(frac: f64) -> Color {
    if frac < 0.6 {
        Color::Green
    } else if frac < 0.85 {
        Color::Yellow
    } else {
        Color::Red
    }
}

/// A tiny peak-hold marker label for the gauge (a caret at the held position).
fn peak_label(peak_frac: f64) -> String {
    if peak_frac > 0.01 {
        format!("▲ {:.0}%", peak_frac * 100.0)
    } else {
        String::new()
    }
}

/// Trim the spec's long field name down to something that fits the label column.
fn short_label(name: &str) -> String {
    let base = name.strip_prefix("Meter: ").unwrap_or(name);
    base.split(" (").next().unwrap_or(base).to_lowercase()
}

/// The twelve-tone name for a tuner note index.
fn note_name(n: u8) -> String {
    const NAMES: [&str; 12] = [
        "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B",
    ];
    NAMES[(n % 12) as usize].to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::backend::TestBackend;

    /// The draw function must render without a live socket, at a variety of
    /// sizes, both with and without the raw fields — exercising every panel.
    fn render_at(view: &View, w: u16, h: u16) {
        let backend = TestBackend::new(w, h);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal
            .draw(|f| view.draw(f, 42.0, Some(-1200.0)))
            .unwrap();
    }

    fn sample_view(all: bool) -> View {
        let mut view = View::new(Ipv4Addr::new(10, 0, 0, 1), all);
        // Populate a plausible snapshot so every panel has content.
        view.snapshot.rig.name = Some("Test Rig".into());
        view.snapshot.rig.author = Some("Author".into());
        view.snapshot.rig.tempo_bpm = Some(120);
        view.snapshot.morph = Some(8192);
        view.snapshot.amp.name = Some("Amp Model".into());
        view.snapshot.amp.on = Some(true);
        view.snapshot.amp.gain = Some(9000);
        view.snapshot.cabinet.name = Some("Cab Model".into());
        view.snapshot.effects[0].kind = Some(33);
        view.snapshot.effects[0].on = Some(true);
        view.snapshot.tuner.note = Some(45);
        let mut raw = [0u16; N];
        raw[3] = 4096; // strobe phase
        raw[4] = 9000; // stack level
        raw[6] = 15000; // rig out level
        raw[9] = 3000; // loudness
        view.ingest_status(RealtimeStatus { raw });
        view
    }

    #[test]
    fn draws_at_many_sizes_without_panic() {
        for all in [false, true] {
            let view = sample_view(all);
            render_at(&view, 80, 24); // standard
            render_at(&view, 120, 40); // large
            render_at(&view, 40, 20); // narrow
            render_at(&view, 200, 60); // very large
        }
    }

    #[test]
    fn verdict_tracks_drift_direction() {
        let view = sample_view(false); // strobe active
        assert!(matches!(view.verdict(Some(-5000.0)), Verdict::Sharp));
        assert!(matches!(view.verdict(Some(5000.0)), Verdict::Flat));
        assert!(matches!(view.verdict(Some(0.0)), Verdict::InTune));
        assert!(matches!(view.verdict(None), Verdict::Unknown));

        let idle = View::new(Ipv4Addr::new(10, 0, 0, 1), false); // no strobe
        assert!(matches!(idle.verdict(Some(0.0)), Verdict::Idle));
    }
}
