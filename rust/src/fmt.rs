//! Small formatting helpers shared by examples and tools.

/// Render bytes as a quoted ASCII string when fully printable, else as hex.
pub fn ascii_or_hex(bytes: &[u8]) -> String {
    if !bytes.is_empty() && bytes.iter().all(|b| b.is_ascii_graphic() || *b == b' ') {
        format!("\"{}\"", String::from_utf8_lossy(bytes))
    } else {
        let hex: Vec<String> = bytes.iter().map(|b| format!("{b:02x}")).collect();
        format!("[{}]", hex.join(" "))
    }
}

/// Classic offset / hex / ASCII hexdump, each line prefixed with `indent`.
pub fn hexdump(bytes: &[u8], indent: &str) -> String {
    let mut out = String::new();
    for (i, chunk) in bytes.chunks(16).enumerate() {
        let mut hex = String::new();
        let mut ascii = String::new();
        for (j, b) in chunk.iter().enumerate() {
            if j == 8 {
                hex.push(' ');
            }
            hex.push_str(&format!("{b:02x} "));
            ascii.push(if b.is_ascii_graphic() || *b == b' ' {
                *b as char
            } else {
                '.'
            });
        }
        out.push_str(&format!("{indent}{:04x}  {:<49} {}\n", i * 16, hex, ascii));
    }
    out
}

/// One-line hex (space-separated), for compact logging.
pub fn hex_inline(bytes: &[u8]) -> String {
    bytes
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect::<Vec<_>>()
        .join(" ")
}
