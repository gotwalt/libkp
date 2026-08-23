//! GENERATED FILE — DO NOT EDIT. Edit spec/*.toml and run codegen/generate.py.
#![allow(clippy::all)]
#![cfg_attr(rustfmt, rustfmt::skip)]

pub const SPEC_VERSION: &str = "0.7.0";

// Transport
pub const PORT: u16 = 5727;
pub const CONNECT_TIMEOUT_SECS: u64 = 5;
pub const SOCKET_TIMEOUT_SECS: u64 = 15;
pub const CONNECTION_COOLDOWN_MS: u64 = 1000;

// Discovery
pub const DISCOVERY_HEADER: &str = "DSCV";
pub const POLL_INTERVAL_MS: u64 = 500;
pub const POLL_MAC_PREFIX: &str = "MAC#";
pub const POLL_PAYLOAD: &str = "POLL:)";
pub const POLL_PLACEHOLDER_MAC: &str = "00:00:00:00:00:00";

// Handshake
pub const HANDSHAKE_TERMINATOR: &str = "\r\n";
pub const HANDSHAKE_LIST_END: &str = ".";
pub const HANDSHAKE_ACCEPT_PREFIX: &str = "+";
pub const HANDSHAKE_REJECT_PREFIX: &str = "-";
pub const SESSION_PREAMBLE_LEN: usize = 8;

// Protocol GUIDs
pub const PROTOCOL_MIDI3_STREAM: &str = "{369F50E7-750B-459A-BAEE-85ADD3F3798D}";
pub const PROTOCOL_REQUEST_RESPONSE: &str = "{2490272E-CD92-4DBA-AE32-E8AF37ED3B0A}";
pub const PROTOCOL_CBOR_CONTROL: &str = "{774CDB9E-74ED-4740-AF09-AC96B3A69A11}";
pub const PROTOCOL_RESERVED: &str = "{77DB6B28-785E-4641-B840-42F0F06A11FC}";

// CBOR channel
pub const CBOR_ITEM_TAG: u64 = 1;
pub const CBOR_SELECTOR_SINGLE: i64 = 1;
pub const CBOR_SELECTOR_MULTI: i64 = 2;
pub const CBOR_SELECTOR_STRING: i64 = 4;
pub const CBOR_FILLER_BYTE: u8 = 0xc0;
pub const STATE_DUMP_TRIGGER_ADDRESS: u32 = 102528;
pub const STATE_DUMP_TRIGGER_VALUE: i64 = 1;
pub const SENSITIVE_ADDRESSES: [u32; 2] = [200008, 200009];
pub const REDACTED_PLACEHOLDER: &str = "[redacted]";

// MIDI3 framing tags
pub const MIDI3_TAG_CONTINUATION: u8 = 0x14;
pub const MIDI3_TAG_FINAL_1: u8 = 0x15;
pub const MIDI3_TAG_FINAL_2: u8 = 0x16;
pub const MIDI3_TAG_FINAL_3: u8 = 0x17;

// SysEx envelope
pub const MANUFACTURER_ID: [u8; 3] = [0x00, 0x20, 0x33];
pub const PRODUCT_PROFILER: u8 = 0x00;
pub const PRODUCT_PLAYER: u8 = 0x02;
pub const DEVICE_OMNI: u8 = 0x7f;
pub const FULL_SCALE: u16 = 16383;

// SysEx function codes
pub const FN_SINGLE_PARAM: u8 = 0x01;
pub const FN_MULTI_PARAM: u8 = 0x02;
pub const FN_STRING_PARAM: u8 = 0x03;
pub const FN_BLOB: u8 = 0x04;
pub const FN_EXT_PARAM: u8 = 0x06;
pub const FN_EXT_STRING_PARAM: u8 = 0x07;
pub const FN_MORPHED_MULTI_PARAM: u8 = 0x08;
pub const FN_RENDERED_STRING_REPLY: u8 = 0x3c;
pub const FN_REQUEST_SINGLE: u8 = 0x41;
pub const FN_REQUEST_MULTI: u8 = 0x42;
pub const FN_REQUEST_STRING: u8 = 0x43;
pub const FN_REQUEST_EXT_PARAM: u8 = 0x46;
pub const FN_REQUEST_EXT_STRING: u8 = 0x47;
pub const FN_REQUEST_RENDERED_STRING: u8 = 0x7c;
pub const FN_BEACON: u8 = 0x7e;

// Beacon
pub const BEACON_FUNCTION: u8 = 0x7e;
pub const BEACON_SUBCOMMAND: u8 = 0x40;
pub const BEACON_DEFAULT_PARAM_SET: u8 = 0x02;
pub const BEACON_FLAG_INIT: u8 = 0x01;
pub const BEACON_FLAG_SYSEX: u8 = 0x02;
pub const BEACON_FLAG_TUNEMODE: u8 = 0x20;

// Effect-slot parameter numbers
pub const EFFECT_PARAM_TYPE: u8 = 0;
pub const EFFECT_PARAM_STATE: u8 = 3;
pub const EFFECT_PARAM_MIX: u8 = 4;
pub const EFFECT_PARAM_VOLUME: u8 = 6;

// Well-known addresses (keyed on by the state / model layer)
pub const PAGE_STRINGS: u8 = 0x00;
pub const STRING_RIG_NAME: u8 = 0x01;
pub const PAGE_REALTIME: u8 = 0x7c;
pub const METER_BLOCK_NUMBER: u8 = 0x4e;
pub const BEAT_PULSE_NUMBER: u8 = 0x00;
pub const TUNER_DEVIANCE_NUMBER: u8 = 0x0f;
pub const PAGE_RIG_SETTINGS: u8 = 0x04;
pub const TEMPO_NUMBER: u8 = 0x00;
pub const TEMPO_BPM_SCALE: u16 = 64;
pub const RIG_VOLUME_NUMBER: u8 = 0x01;
pub const AMP_PAGE: u8 = 0x0a;
pub const AMP_ON_NUMBER: u8 = 0x02;
pub const GAIN_NUMBER: u8 = 0x04;
pub const SYSTEM_PAGE: u8 = 0x7f;
pub const MAIN_VOLUME_NUMBER: u8 = 0x00;
pub const HEADPHONE_VOLUME_NUMBER: u8 = 0x01;
pub const MONITOR_VOLUME_NUMBER: u8 = 0x02;
pub const PAGE_BANK_PREVIEW: u8 = 0x96;
pub const BANK_SLOTS: usize = 5;
pub const BANK_RIG_NAME_BASE: u8 = 0x00;
pub const BANK_AMP_NAME_BASE: u8 = 0x05;
pub const BANK_CABINET_NAME_BASE: u8 = 0x0a;
pub const PAGE_MORPH: u8 = 0x00;
pub const MORPH_NUMBER: u8 = 0x77;
pub const MORPH_BUTTON_NUMBER: u8 = 0x50;
pub const MORPH_ADDRESS: u32 = 119;
pub const PAGE_TUNER_NOTE: u8 = 0x7d;
pub const TUNER_NOTE_NUMBER: u8 = 0x54;
pub const TUNER_IN_TUNE_CENTER: u16 = 8192;
pub const TUNER_IN_TUNE_WINDOW: u16 = 350;
pub const METER_COUNT: usize = 11;
pub const CURRENT_BANK_ADDRESS: u32 = 100701;
pub const CURRENT_RIG_SLOT_ADDRESS: u32 = 100702;
pub const STRING_RIG_AUTHOR: u8 = 0x02;
pub const STRING_RIG_DATE: u8 = 0x03;
pub const STRING_RIG_COMMENT: u8 = 0x04;
pub const STRING_AMP_NAME: u8 = 0x0a;
pub const STRING_CABINET_NAME: u8 = 0x20;
pub const CABINET_PAGE: u8 = 0x0c;
pub const CABINET_ON_NUMBER: u8 = 0x02;

// Meter block
pub const METER_PAGE: u8 = 0x7c;
pub const METER_FIRST_NUMBER: u8 = 0x4e;
pub const METER_UPDATE_RATE_HZ: u32 = 20;
pub const STROBE_PHASE_INDEX: usize = 3;
pub const STROBE_SEGMENT_INDICES: [usize; 3] = [0, 1, 2];

// Tables
#[rustfmt::skip]
pub static FUNCTION_NAMES: &[(u8, &str)] = &[
    (0x01, "single-param"),
    (0x02, "multi-param"),
    (0x03, "string-param"),
    (0x04, "blob"),
    (0x06, "ext-param"),
    (0x07, "ext-string-param"),
    (0x08, "morphed-multi-param"),
    (0x3c, "rendered-string-reply"),
    (0x41, "request-single"),
    (0x42, "request-multi"),
    (0x43, "request-string"),
    (0x47, "request-ext-string"),
    (0x7c, "request-rendered-string"),
    (0x7e, "beacon"),
];

#[rustfmt::skip]
pub static PAGE_NAMES: &[(u8, &str)] = &[
    (0x00, "String Tags"),
    (0x04, "Rig Settings"),
    (0x05, "Fixed FX"),
    (0x09, "Input Section"),
    (0x0a, "Amplifier"),
    (0x0b, "Amplifier EQ"),
    (0x0c, "Cabinet"),
    (0x32, "Effect A"),
    (0x33, "Effect B"),
    (0x34, "Effect C"),
    (0x35, "Effect D"),
    (0x38, "Effect X"),
    (0x3a, "Effect MOD"),
    (0x3c, "Effect DLY"),
    (0x3d, "Effect REV"),
    (0x76, "User Scales"),
    (0x7c, "Realtime/Meters"),
    (0x7d, "Looper/Freeze"),
    (0x7f, "System/Global"),
    (0x96, "Bank Preview"),
];

#[rustfmt::skip]
pub static EFFECT_SLOTS: &[(&str, u8)] = &[
    ("A", 0x32),
    ("B", 0x33),
    ("C", 0x34),
    ("D", 0x35),
    ("X", 0x38),
    ("MOD", 0x3a),
    ("DLY", 0x3c),
    ("REV", 0x3d),
];

#[rustfmt::skip]
pub static EFFECT_PARAMS: &[(u8, &str)] = &[
    (0, "Type"),
    (3, "On/Off"),
    (4, "Mix"),
    (6, "Volume"),
    (7, "Stereo"),
    (8, "Wah Manual / Freq Shifter Delay Pitch"),
    (9, "Wah Peak"),
    (10, "Wah Pedal Range"),
    (12, "Wah Pedal Mode"),
    (13, "Wah Touch Attack / Fuzz Impedance LP"),
    (14, "Wah Touch Release"),
    (15, "Wah Touch Boost / Delay Cross Feedback"),
    (16, "Distortion Drive / Reverb Formant Mix"),
    (17, "Distortion Tone / Reverb Mid Frequency"),
    (18, "Fuzz Octa / Compressor Intensity / Noise Gate Threshold / Auto Swell Compressor"),
    (19, "Compressor Attack / Legacy Delay Bandwidth / Legacy Reverb Bandwidth"),
    (20, "Fuzz Transistor Shape / Modulation Rate / Auto Swell / Widener Tune"),
    (21, "Drive Definition / Fuzz Transistor Tone / Modulation Depth / Micro Pitch Detune / Double Tracker Looseness / Widener Intensity"),
    (22, "Modulation Feedback / Formant Reverb Vowel"),
    (23, "Drive Slim Down / Fuzz Definition / Modulation Crossover / Octaver Low Cut"),
    (24, "Modulation Hyper Chorus Amount"),
    (25, "Modulation Manual / Reverb Formant Offset / Spring Reverb Spectral Balance"),
    (26, "Modulation Peak Spread / Wah Phaser Peak Spread / Reverb Formant Peak"),
    (27, "Modulation Stages / Wah Phaser Stages / Legacy Reverb Room Size"),
    (30, "Rotary Speed (Slow/Fast)"),
    (31, "Rotary Distance"),
    (32, "Rotary Low-High-Balance"),
    (33, "Compressor Squash / Legacy Delay Frequency / Legacy Reverb Mid Frequency"),
    (34, "Graphic EQ Gain 80 Hz"),
    (35, "Graphic EQ Gain 160 Hz"),
    (36, "Graphic EQ Gain 320 Hz"),
    (37, "Graphic EQ Gain 640 Hz"),
    (38, "Graphic EQ Gain 1250 Hz"),
    (39, "Graphic EQ Gain 2500 Hz"),
    (40, "Graphic EQ Gain 5000 Hz"),
    (41, "Graphic EQ Gain 10000 Hz"),
    (42, "Studio/Metal EQ / Metal DS Low Gain / Acoustic Sim Body"),
    (43, "Studio EQ Low Frequency"),
    (44, "Studio/Metal EQ / Metal DS High Gain / Acoustic Sim Sparkle"),
    (45, "Studio EQ High Frequency"),
    (46, "Studio EQ Mid1 / Metal EQ/DS Middle Gain / Acoustic Sim Bronze"),
    (47, "Studio EQ Mid1 / Metal EQ/DS Middle Frequency"),
    (48, "Studio EQ Mid1 Q-Factor"),
    (49, "Studio EQ Mid2 Gain / Acoustic Sim Pickup"),
    (50, "Studio EQ Mid2 Frequency"),
    (51, "Studio EQ Mid2 Q-Factor"),
    (52, "Wah Peak Range"),
    (53, "Ducking"),
    (54, "Mix 2 (Pitch/Octaver/Delay Serial/Crystal Mix / Space Intensity)"),
    (55, "Voice Balance / Delay Balance"),
    (56, "Voice 1 Pitch / Toe Pitch / Transpose Pitch / Quad Voice Pitch 4 / Crystal 1 Pitch"),
    (57, "Voice 2 Pitch / Heel Pitch / Quad Voice Pitch 3 / Wah Formant Pitch Shift / Crystal 2 Pitch"),
    (58, "Pitch Detune"),
    (60, "Smooth Chords"),
    (61, "Pure Tuning"),
    (62, "Voice 1 Interval / Quad Voice 4 Interval"),
    (63, "Voice 2 Interval / Quad Voice 3 Interval"),
    (64, "Key"),
    (65, "Formant Shift Freeze"),
    (66, "Formant Shift Offset"),
    (67, "Equalizer Low Cut"),
    (68, "Equalizer High Cut / Reverb High Cut"),
    (69, "Mix 3 (delay/reverb)"),
    (70, "Mix Pre/Post"),
    (71, "Delay 1 Time / Reverb Room Size / Reverb Attack / Spring Size"),
    (72, "Delay 2 Time / Reverb Predelay Time"),
    (73, "Delay 2 Ratio / Quad Delay 3 Ratio / Rate Flanger/Phaser Oneway"),
    (74, "Quad Delay 2 Ratio / Delay Ratio Serial"),
    (75, "Quad Delay 1 Ratio"),
    (76, "Delay Note Value 1 / Quad Note Value 4"),
    (77, "Delay Note Value 2 / Quad Note Value 3 / Reverb Predelay Note Value"),
    (78, "Quad Note Value 2 / Note Value Serial"),
    (79, "Quad Note Value 1"),
    (80, "To Tempo / Equalizer Steep Low"),
    (81, "Delay Volume 4"),
    (82, "Delay Volume 3"),
    (83, "Delay Volume 2"),
    (84, "Delay Volume 1"),
    (85, "Delay Panorama 4"),
    (86, "Delay Panorama 3"),
    (87, "Delay Panorama 2"),
    (88, "Delay Panorama 1"),
    (89, "Voice Pitch 2 / Crystal Pitch"),
    (90, "Voice Pitch 1"),
    (91, "Voice 3 Interval"),
    (92, "Voice 4 Interval"),
    (93, "Delay Feedback 1 / Reverb Decay Time"),
    (94, "Infinity Feedback"),
    (95, "Infinity"),
    (96, "Feedback 2/Serial / Reverb Low Boost / Echo Reverb Feedback / Ionosphere Buildup"),
    (97, "Delay Feedback Sync"),
    (98, "Delay Low Cut / Reverb Low Decay/Damp"),
    (99, "Delay High Cut / Reverb High Decay/Damp / Fuzz True Impedance"),
    (100, "Delay Cut More / Equalizer Steep High / Full OC HP/LP / Effect Loop (Stage)"),
    (101, "Modulation (delay/reverb)"),
    (102, "Delay Chorus"),
    (103, "Delay Flutter Intensity / Reverb Modulation"),
    (104, "Delay Flutter Rate / Reverb Early Diffusion / Spring Dripstone"),
    (105, "Delay Grit / Reverb Brass / Spring Distortion (Dwell)"),
    (106, "Reverse Mix"),
    (107, "Input Swell"),
    (108, "Smear"),
    (109, "Ducking Pre/Post"),
];

#[rustfmt::skip]
pub static NON_EFFECT_PARAMS: &[(u8, u8, &str)] = &[
    (0x04, 0, "Tempo bpm"),
    (0x04, 1, "Rig Volume"),
    (0x04, 2, "Tempo Enable"),
    (0x04, 3, "Panorama"),
    (0x04, 4, "Transpose"),
    (0x04, 64, "Stomps Section On/Off"),
    (0x04, 65, "Stack Section On/Off"),
    (0x04, 66, "Effects Section On/Off"),
    (0x04, 68, "Volume Pedal Location"),
    (0x04, 69, "Volume Pedal Range"),
    (0x04, 71, "Parallel Path Enable"),
    (0x04, 72, "Parallel Path Mix"),
    (0x04, 73, "Rig Spillover Off"),
    (0x04, 74, "DLY+REV Routing"),
    (0x05, 1, "Fixed Transpose On/Off"),
    (0x05, 6, "Fixed Noise Gate On/Off"),
    (0x05, 11, "Fixed Compressor On/Off"),
    (0x05, 16, "Fixed Boost On/Off"),
    (0x05, 21, "Fixed Wah On/Off"),
    (0x05, 26, "Fixed Vintage Chorus On/Off"),
    (0x05, 36, "Fixed Air Chorus On/Off"),
    (0x05, 41, "Fixed Double Tracker On/Off"),
    (0x09, 3, "Noise Gate Intensity"),
    (0x09, 4, "Clean Sense"),
    (0x09, 5, "Distortion Sense"),
    (0x0a, 0, "Amp Model"),
    (0x0a, 2, "On/Off"),
    (0x0a, 3, "Amp Volume"),
    (0x0a, 4, "Gain"),
    (0x0a, 5, "Clean Compensation"),
    (0x0a, 6, "Definition"),
    (0x0a, 7, "Clarity"),
    (0x0a, 8, "Power Sagging"),
    (0x0a, 9, "Pick"),
    (0x0a, 10, "Compressor"),
    (0x0a, 11, "Tube Shape"),
    (0x0a, 12, "Tube Bias"),
    (0x0a, 15, "Direct Mix"),
    (0x0a, 20, "Gain (smoothed follower)"),
    (0x0a, 21, "Bright Cap Intensity"),
    (0x0b, 4, "Bass"),
    (0x0b, 5, "Middle"),
    (0x0b, 6, "Treble"),
    (0x0b, 7, "Presence"),
    (0x0b, 8, "Position Pre/Post"),
    (0x0c, 2, "On/Off"),
    (0x0c, 4, "High Shift"),
    (0x0c, 5, "Low Shift"),
    (0x0c, 6, "Character"),
    (0x0c, 7, "Pure Cabinet"),
    (0x0c, 8, "Kone Imprint Select"),
    (0x0c, 9, "Low Cut"),
    (0x0c, 10, "High Cut"),
    (0x76, 0, "User Scale 1 Step"),
    (0x76, 1, "User Scale 1 Step"),
    (0x76, 2, "User Scale 1 Step"),
    (0x76, 3, "User Scale 1 Step"),
    (0x76, 4, "User Scale 1 Step"),
    (0x76, 5, "User Scale 1 Step"),
    (0x76, 6, "User Scale 1 Step"),
    (0x76, 7, "User Scale 1 Step"),
    (0x76, 8, "User Scale 1 Step"),
    (0x76, 9, "User Scale 1 Step"),
    (0x76, 10, "User Scale 1 Step"),
    (0x76, 11, "User Scale 1 Step"),
    (0x76, 12, "User Scale 2 Step"),
    (0x76, 13, "User Scale 2 Step"),
    (0x76, 14, "User Scale 2 Step"),
    (0x76, 15, "User Scale 2 Step"),
    (0x76, 16, "User Scale 2 Step"),
    (0x76, 17, "User Scale 2 Step"),
    (0x76, 18, "User Scale 2 Step"),
    (0x76, 19, "User Scale 2 Step"),
    (0x76, 20, "User Scale 2 Step"),
    (0x76, 21, "User Scale 2 Step"),
    (0x76, 22, "User Scale 2 Step"),
    (0x76, 23, "User Scale 2 Step"),
    (0x7c, 0, "Tempo/Beat Pulse"),
    (0x7c, 15, "Tuner Deviance"),
    (0x7c, 78, "Tuner Strobe Segment (phase-low)"),
    (0x7c, 79, "Tuner Strobe Segment (phase-mid)"),
    (0x7c, 80, "Tuner Strobe Segment (phase-high)"),
    (0x7c, 81, "Tuner Strobe Phase"),
    (0x7c, 82, "Meter: Stack Level (pre-vol)"),
    (0x7c, 83, "Meter: Stack Power"),
    (0x7c, 84, "Meter: Rig Output Level"),
    (0x7c, 85, "Meter: Rig Output Power"),
    (0x7c, 86, "Meter: (unused v8)"),
    (0x7c, 87, "Meter: Loudness (RMS)"),
    (0x7c, 88, "Meter: (unused v10)"),
    (0x7d, 84, "Tuner Note"),
    (0x7d, 88, "Looper Record/Playback/Overdub"),
    (0x7d, 89, "Looper Stop"),
    (0x7d, 90, "Looper Trigger"),
    (0x7d, 91, "Looper Reverse"),
    (0x7d, 92, "Looper Half Speed"),
    (0x7d, 93, "Looper Cancel/Reactivate Overdub"),
    (0x7d, 94, "Looper Erase Loop"),
    (0x7d, 107, "Module A Freeze"),
    (0x7d, 108, "Module B Freeze"),
    (0x7d, 109, "Module C Freeze"),
    (0x7d, 110, "Module D Freeze"),
    (0x7d, 111, "Module X Freeze"),
    (0x7d, 113, "Module MOD Freeze"),
    (0x7d, 114, "Module DLY Freeze"),
    (0x7d, 115, "Module REV Freeze"),
    (0x7f, 0, "Main Output Volume"),
    (0x7f, 1, "Headphone Output Volume"),
    (0x7f, 2, "Monitor Output Volume"),
    (0x7f, 3, "Direct Output / Send 1 Volume"),
    (0x7f, 4, "S/PDIF Output Volume"),
    (0x7f, 8, "Monitor Cab. Off"),
    (0x7f, 12, "Main Output EQ Bass"),
    (0x7f, 13, "Main Output EQ Middle"),
    (0x7f, 14, "Main Output EQ Treble"),
    (0x7f, 15, "Main Output EQ Presence"),
    (0x7f, 16, "Output Filter Low Cut"),
    (0x7f, 17, "Monitor Output EQ Bass"),
    (0x7f, 18, "Monitor Output EQ Middle"),
    (0x7f, 19, "Monitor Output EQ Treble"),
    (0x7f, 20, "Monitor Output EQ Presence"),
    (0x7f, 21, "Output Filter High Cut"),
    (0x7f, 32, "Aux In >Main"),
    (0x7f, 33, "Aux In >Monitor"),
    (0x7f, 34, "Aux In >Headphone"),
    (0x7f, 36, "Space Intensity"),
    (0x7f, 37, "Space Routing"),
    (0x7f, 38, "Kone Mode"),
    (0x7f, 39, "Kone Bass Boost"),
    (0x7f, 40, "Kone Imprint Select"),
    (0x7f, 41, "Kone Directivity"),
    (0x7f, 42, "Kone Sweetening"),
    (0x7f, 44, "Input Source"),
    (0x7f, 50, "Pure Cabinet Enable"),
    (0x7f, 51, "Pure Cabinet Level (Global)"),
    (0x7f, 52, "Looper Volume"),
    (0x7f, 53, "Looper Location"),
    (0x7f, 59, "Aux >Mono"),
    (0x7f, 126, "Tuner Mode State"),
    (0x96, 0, "Bank Rig Name"),
    (0x96, 1, "Bank Rig Name"),
    (0x96, 2, "Bank Rig Name"),
    (0x96, 3, "Bank Rig Name"),
    (0x96, 4, "Bank Rig Name"),
    (0x96, 5, "Bank Amp Name"),
    (0x96, 6, "Bank Amp Name"),
    (0x96, 7, "Bank Amp Name"),
    (0x96, 8, "Bank Amp Name"),
    (0x96, 9, "Bank Amp Name"),
    (0x96, 10, "Bank Cabinet Name"),
    (0x96, 11, "Bank Cabinet Name"),
    (0x96, 12, "Bank Cabinet Name"),
    (0x96, 13, "Bank Cabinet Name"),
    (0x96, 14, "Bank Cabinet Name"),
];

#[rustfmt::skip]
pub static STRING_TAGS: &[(u8, &str)] = &[
    (1, "Rig Name"),
    (2, "Rig Author"),
    (3, "Rig Creation Date"),
    (4, "Rig Comment"),
    (10, "Amp Name"),
    (11, "Amp Author"),
    (14, "Amp Location"),
    (15, "Amp Manufacturer"),
    (16, "Amp Comment"),
    (18, "Amp Model"),
    (19, "Amp Channel"),
    (20, "Pickup Type"),
    (21, "Year of Production"),
    (32, "Cabinet Name"),
    (33, "Cabinet Author"),
    (36, "Cabinet Location"),
    (37, "Cabinet Manufacturer"),
    (38, "Microphone Model"),
    (39, "Cabinet Comment"),
    (40, "Microphone Position"),
    (41, "Speaker Configuration"),
    (42, "Cabinet Model"),
    (44, "Speaker Manufacturer"),
    (45, "Speaker Model"),
];

#[rustfmt::skip]
pub static PAGE0_NUMERIC: &[(u8, &str)] = &[
    (0x50, "Morph Button"),
    (0x77, "Morph Position"),
];

#[rustfmt::skip]
pub static EFFECT_TYPES: &[(u16, &str)] = &[
    (0, "empty"),
    (1, "Wah Wah"),
    (2, "Wah Low Pass"),
    (3, "Wah High Pass"),
    (4, "Wah Vowel Filter"),
    (6, "Wah Phaser"),
    (7, "Wah Flanger"),
    (8, "Wah Rate Reducer"),
    (9, "Wah Ring Modulator"),
    (10, "Wah Freq Shifter"),
    (11, "Pedal Pitch"),
    (12, "Wah Formant Shifter"),
    (13, "Pedal Vinyl Stop"),
    (17, "Bit Shaper"),
    (18, "Octa Shaper"),
    (19, "Soft Shaper"),
    (20, "Hard Shaper"),
    (21, "Wave Shaper"),
    (32, "Kemper Drive"),
    (33, "Green Scream"),
    (34, "Plus DS"),
    (35, "One DS"),
    (36, "Muffin"),
    (37, "Mouse"),
    (38, "Kemper Fuzz"),
    (39, "Metal DS"),
    (42, "Full OC"),
    (49, "Compressor"),
    (50, "Auto Swell"),
    (57, "Noise Gate 2:1"),
    (58, "Noise Gate 4:1"),
    (64, "Space"),
    (65, "Vintage Chorus"),
    (66, "Hyper Chorus"),
    (67, "Air Chorus"),
    (68, "Vibrato"),
    (69, "Rotary Speaker"),
    (70, "Tremolo"),
    (71, "Micro Pitch"),
    (81, "Phaser"),
    (82, "Phaser Vibe"),
    (83, "Phaser Oneway"),
    (89, "Flanger"),
    (91, "Flanger Oneway"),
    (97, "Graphic Equalizer"),
    (98, "Studio Equalizer"),
    (99, "Metal Equalizer"),
    (100, "Acoustic Simulator"),
    (101, "Stereo Widener"),
    (102, "Phase Widener"),
    (103, "Delay Widener"),
    (104, "Double Tracker"),
    (113, "Treble Booster"),
    (114, "Lead Booster"),
    (115, "Pure Booster"),
    (116, "Wah Pedal Booster"),
    (121, "Loop Mono"),
    (122, "Loop Stereo"),
    (123, "Loop Distortion"),
    (129, "Transpose"),
    (130, "Chromatic Pitch"),
    (131, "Harmonic Pitch"),
    (132, "Analog Octaver"),
    (137, "Dual Chromatic"),
    (138, "Dual Harmonic"),
    (139, "Dual Crystal"),
    (140, "Dual Loop Pitch"),
    (145, "Legacy Delay"),
    (146, "Single Delay"),
    (147, "Dual Delay"),
    (148, "Two Tap Delay"),
    (149, "Serial TwoTap Delay"),
    (150, "Crystal Delay"),
    (151, "Loop Pitch Delay"),
    (152, "Freq Shifter Delay"),
    (161, "Rhythm Delay"),
    (162, "Melody Chromatic"),
    (163, "Melody Harmonic"),
    (164, "Quad Delay"),
    (165, "Quad Chromatic"),
    (166, "Quad Harmonic"),
    (177, "Legacy Reverb"),
    (178, "Natural Reverb"),
    (179, "Easy Reverb"),
    (180, "Echo Reverb"),
    (181, "Cirrus Reverb"),
    (182, "Formant Reverb"),
    (183, "Ionosphere Reverb"),
    (193, "Spring Reverb"),
];

/// One category block of the effect Type value space: an inclusive range.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct EffectCategory {
    pub min: u16,
    pub max: u16,
    pub name: &'static str,
}
#[rustfmt::skip]
pub static EFFECT_CATEGORIES: &[EffectCategory] = &[
    EffectCategory { min: 1, max: 16, name: "Wah" },
    EffectCategory { min: 17, max: 31, name: "Shaper" },
    EffectCategory { min: 32, max: 48, name: "Distortion" },
    EffectCategory { min: 49, max: 63, name: "Dynamics" },
    EffectCategory { min: 64, max: 79, name: "Modulation" },
    EffectCategory { min: 80, max: 95, name: "Phaser & Flanger" },
    EffectCategory { min: 96, max: 111, name: "Equalizer" },
    EffectCategory { min: 112, max: 120, name: "Booster" },
    EffectCategory { min: 121, max: 127, name: "Effect Loop" },
    EffectCategory { min: 128, max: 143, name: "Pitch" },
    EffectCategory { min: 144, max: 175, name: "Delay" },
    EffectCategory { min: 176, max: 207, name: "Reverb" },
];

pub const CC_WAH_PEDAL: u8 = 1;
pub const CC_PITCH_PEDAL: u8 = 4;
pub const CC_VOLUME_PEDAL: u8 = 7;
pub const CC_PANORAMA: u8 = 10;
pub const CC_MORPH_PEDAL: u8 = 11;
pub const CC_DELAY_MIX: u8 = 68;
pub const CC_DELAY_FEEDBACK: u8 = 69;
pub const CC_REVERB_MIX: u8 = 70;
pub const CC_REVERB_TIME: u8 = 71;
pub const CC_GAIN: u8 = 72;
pub const CC_MONITOR_VOLUME: u8 = 73;
pub const CC_TOGGLE_ALL_MODULES: u8 = 16;
pub const CC_MODULE_A: u8 = 17;
pub const CC_MODULE_B: u8 = 18;
pub const CC_MODULE_C: u8 = 19;
pub const CC_MODULE_D: u8 = 20;
pub const CC_MODULE_X: u8 = 22;
pub const CC_MODULE_MOD: u8 = 24;
pub const CC_MODULE_DLY_NO_SPILL: u8 = 26;
pub const CC_MODULE_DLY: u8 = 27;
pub const CC_MODULE_REV_NO_SPILL: u8 = 28;
pub const CC_MODULE_REV: u8 = 29;
pub const CC_TAP_TEMPO: u8 = 30;
pub const CC_TUNER_MODE: u8 = 31;
pub const CC_ROTARY_SPEED: u8 = 33;
pub const CC_DELAY_INFINITY: u8 = 34;
pub const CC_FREEZE: u8 = 35;
pub const CC_BANK_PRESELECT: u8 = 47;
pub const CC_UP: u8 = 48;
pub const CC_DOWN: u8 = 49;
pub const CC_LOAD_SLOT_1: u8 = 50;
pub const CC_LOAD_SLOT_2: u8 = 51;
pub const CC_LOAD_SLOT_3: u8 = 52;
pub const CC_LOAD_SLOT_4: u8 = 53;
pub const CC_LOAD_SLOT_5: u8 = 54;
pub const CC_EFFECT_BUTTON_I: u8 = 75;
pub const CC_EFFECT_BUTTON_II: u8 = 76;
pub const CC_EFFECT_BUTTON_III: u8 = 77;
pub const CC_EFFECT_BUTTON_IIII: u8 = 78;
pub const CC_MORPH_BUTTON: u8 = 80;
pub const CC_BANK_SELECT_MSB: u8 = 0;
pub const CC_BANK_SELECT_LSB: u8 = 32;

#[rustfmt::skip]
pub static SLOT_ENABLE_CC: &[(&str, u8)] = &[
    ("A", 17),
    ("B", 18),
    ("C", 19),
    ("D", 20),
    ("X", 22),
    ("MOD", 24),
    ("DLY", 27),
    ("REV", 29),
];

pub const PROGRAM_CHANGE_STATUS: u8 = 0xc0;
pub const CONTROL_CHANGE_STATUS: u8 = 0xb0;

/// One realtime status field: (index, number, id, name, render).
#[rustfmt::skip]
pub static METER_FIELDS: &[(usize, u8, &str, &str, &str)] = &[
    (0, 78, "strobe_seg_low", "Tuner Strobe Segment (phase-low)", "strobe"),
    (1, 79, "strobe_seg_mid", "Tuner Strobe Segment (phase-mid)", "strobe"),
    (2, 80, "strobe_seg_high", "Tuner Strobe Segment (phase-high)", "strobe"),
    (3, 81, "strobe_phase", "Tuner Strobe Phase", "strobe"),
    (4, 82, "stack_level", "Stack Level (pre-rig-volume)", "bar"),
    (5, 83, "stack_power", "Stack Power", "extra"),
    (6, 84, "rig_out_level", "Rig Output Level (post-rig-volume)", "bar"),
    (7, 85, "rig_out_power", "Rig Output Power", "extra"),
    (8, 86, "unused_v8", "(unused)", "extra"),
    (9, 87, "loudness", "Loudness (slow RMS)", "bar"),
    (10, 88, "unused_v10", "(unused)", "extra"),
];

/// A field of the device-state tree that a routed address writes (spec/state.toml).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Field {
    RigName,
    RigAuthor,
    RigDate,
    RigComment,
    AmpName,
    CabinetName,
    MorphButton,
    MorphPosition,
    TempoBpm,
    RigVolume,
    AmpOn,
    AmpGain,
    CabinetOn,
    EffectType,
    EffectOn,
    EffectMix,
    BeatPulse,
    TunerDeviance,
    Status,
    TunerNote,
    MainVolume,
    HeadphoneVolume,
    MonitorVolume,
    BankRigName,
    BankAmpName,
    BankCabinetName,
    CurrentBank,
    CurrentRigSlot,
}

/// How a routed value decodes before it is stored.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Kind {
    U14,
    U16,
    U7,
    Bool,
    Text,
    Bpm,
    Multi,
}

/// Which update lane a route feeds: FAST (event only) or SLOW (snapshot).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Lane {
    Fast,
    Slow,
}

/// Which channel may write a route: the MIDI3 stream, the CBOR control channel, or both.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Wire {
    Stream,
    Control,
    Both,
}

/// One row of the state routing table: a flat address and how the tree folds it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Route {
    pub address: u32,
    pub field: Field,
    /// The per-slot index for expanded rows: effect slot, bank-preview slot, or
    /// element index within a spanned block.
    pub slot: Option<u8>,
    pub kind: Kind,
    pub lane: Lane,
    pub wire: Wire,
    pub dedupe: bool,
    pub request: bool,
}

/// The state routing table, sorted by address (spec/state.toml).
#[rustfmt::skip]
pub static STATE_ROUTES: &[Route] = &[
    Route { address: 1, field: Field::RigName, slot: None, kind: Kind::Text, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 2, field: Field::RigAuthor, slot: None, kind: Kind::Text, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 3, field: Field::RigDate, slot: None, kind: Kind::Text, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 4, field: Field::RigComment, slot: None, kind: Kind::Text, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 10, field: Field::AmpName, slot: None, kind: Kind::Text, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 32, field: Field::CabinetName, slot: None, kind: Kind::Text, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 80, field: Field::MorphButton, slot: None, kind: Kind::Bool, lane: Lane::Fast, wire: Wire::Stream, dedupe: false, request: false },
    Route { address: 119, field: Field::MorphPosition, slot: None, kind: Kind::U14, lane: Lane::Slow, wire: Wire::Control, dedupe: true, request: false },
    Route { address: 512, field: Field::TempoBpm, slot: None, kind: Kind::Bpm, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 513, field: Field::RigVolume, slot: None, kind: Kind::U14, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 1282, field: Field::AmpOn, slot: None, kind: Kind::Bool, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 1284, field: Field::AmpGain, slot: None, kind: Kind::U14, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 1538, field: Field::CabinetOn, slot: None, kind: Kind::Bool, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: false },
    Route { address: 6400, field: Field::EffectType, slot: Some(0), kind: Kind::U14, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 6403, field: Field::EffectOn, slot: Some(0), kind: Kind::Bool, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 6404, field: Field::EffectMix, slot: Some(0), kind: Kind::U14, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: false },
    Route { address: 6528, field: Field::EffectType, slot: Some(1), kind: Kind::U14, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 6531, field: Field::EffectOn, slot: Some(1), kind: Kind::Bool, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 6532, field: Field::EffectMix, slot: Some(1), kind: Kind::U14, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: false },
    Route { address: 6656, field: Field::EffectType, slot: Some(2), kind: Kind::U14, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 6659, field: Field::EffectOn, slot: Some(2), kind: Kind::Bool, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 6660, field: Field::EffectMix, slot: Some(2), kind: Kind::U14, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: false },
    Route { address: 6784, field: Field::EffectType, slot: Some(3), kind: Kind::U14, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 6787, field: Field::EffectOn, slot: Some(3), kind: Kind::Bool, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 6788, field: Field::EffectMix, slot: Some(3), kind: Kind::U14, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: false },
    Route { address: 7168, field: Field::EffectType, slot: Some(4), kind: Kind::U14, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 7171, field: Field::EffectOn, slot: Some(4), kind: Kind::Bool, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 7172, field: Field::EffectMix, slot: Some(4), kind: Kind::U14, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: false },
    Route { address: 7424, field: Field::EffectType, slot: Some(5), kind: Kind::U14, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 7427, field: Field::EffectOn, slot: Some(5), kind: Kind::Bool, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 7428, field: Field::EffectMix, slot: Some(5), kind: Kind::U14, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: false },
    Route { address: 7680, field: Field::EffectType, slot: Some(6), kind: Kind::U14, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 7683, field: Field::EffectOn, slot: Some(6), kind: Kind::Bool, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 7684, field: Field::EffectMix, slot: Some(6), kind: Kind::U14, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: false },
    Route { address: 7808, field: Field::EffectType, slot: Some(7), kind: Kind::U14, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 7811, field: Field::EffectOn, slot: Some(7), kind: Kind::Bool, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 7812, field: Field::EffectMix, slot: Some(7), kind: Kind::U14, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: false },
    Route { address: 15872, field: Field::BeatPulse, slot: None, kind: Kind::Bool, lane: Lane::Fast, wire: Wire::Stream, dedupe: false, request: false },
    Route { address: 15887, field: Field::TunerDeviance, slot: None, kind: Kind::U14, lane: Lane::Fast, wire: Wire::Stream, dedupe: true, request: false },
    Route { address: 15950, field: Field::Status, slot: Some(0), kind: Kind::Multi, lane: Lane::Fast, wire: Wire::Stream, dedupe: false, request: false },
    Route { address: 15951, field: Field::Status, slot: Some(1), kind: Kind::Multi, lane: Lane::Fast, wire: Wire::Stream, dedupe: false, request: false },
    Route { address: 15952, field: Field::Status, slot: Some(2), kind: Kind::Multi, lane: Lane::Fast, wire: Wire::Stream, dedupe: false, request: false },
    Route { address: 15953, field: Field::Status, slot: Some(3), kind: Kind::Multi, lane: Lane::Fast, wire: Wire::Stream, dedupe: false, request: false },
    Route { address: 15954, field: Field::Status, slot: Some(4), kind: Kind::Multi, lane: Lane::Fast, wire: Wire::Stream, dedupe: false, request: false },
    Route { address: 15955, field: Field::Status, slot: Some(5), kind: Kind::Multi, lane: Lane::Fast, wire: Wire::Stream, dedupe: false, request: false },
    Route { address: 15956, field: Field::Status, slot: Some(6), kind: Kind::Multi, lane: Lane::Fast, wire: Wire::Stream, dedupe: false, request: false },
    Route { address: 15957, field: Field::Status, slot: Some(7), kind: Kind::Multi, lane: Lane::Fast, wire: Wire::Stream, dedupe: false, request: false },
    Route { address: 15958, field: Field::Status, slot: Some(8), kind: Kind::Multi, lane: Lane::Fast, wire: Wire::Stream, dedupe: false, request: false },
    Route { address: 15959, field: Field::Status, slot: Some(9), kind: Kind::Multi, lane: Lane::Fast, wire: Wire::Stream, dedupe: false, request: false },
    Route { address: 15960, field: Field::Status, slot: Some(10), kind: Kind::Multi, lane: Lane::Fast, wire: Wire::Stream, dedupe: false, request: false },
    Route { address: 16084, field: Field::TunerNote, slot: None, kind: Kind::U7, lane: Lane::Slow, wire: Wire::Stream, dedupe: true, request: false },
    Route { address: 16256, field: Field::MainVolume, slot: None, kind: Kind::U14, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 16257, field: Field::HeadphoneVolume, slot: None, kind: Kind::U14, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 16258, field: Field::MonitorVolume, slot: None, kind: Kind::U14, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 19200, field: Field::BankRigName, slot: Some(0), kind: Kind::Text, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 19201, field: Field::BankRigName, slot: Some(1), kind: Kind::Text, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 19202, field: Field::BankRigName, slot: Some(2), kind: Kind::Text, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 19203, field: Field::BankRigName, slot: Some(3), kind: Kind::Text, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 19204, field: Field::BankRigName, slot: Some(4), kind: Kind::Text, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 19205, field: Field::BankAmpName, slot: Some(0), kind: Kind::Text, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 19206, field: Field::BankAmpName, slot: Some(1), kind: Kind::Text, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 19207, field: Field::BankAmpName, slot: Some(2), kind: Kind::Text, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 19208, field: Field::BankAmpName, slot: Some(3), kind: Kind::Text, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 19209, field: Field::BankAmpName, slot: Some(4), kind: Kind::Text, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 19210, field: Field::BankCabinetName, slot: Some(0), kind: Kind::Text, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 19211, field: Field::BankCabinetName, slot: Some(1), kind: Kind::Text, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 19212, field: Field::BankCabinetName, slot: Some(2), kind: Kind::Text, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 19213, field: Field::BankCabinetName, slot: Some(3), kind: Kind::Text, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 19214, field: Field::BankCabinetName, slot: Some(4), kind: Kind::Text, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 100701, field: Field::CurrentBank, slot: None, kind: Kind::U16, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
    Route { address: 100702, field: Field::CurrentRigSlot, slot: None, kind: Kind::U16, lane: Lane::Slow, wire: Wire::Both, dedupe: true, request: true },
];

