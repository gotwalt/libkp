"""GENERATED FILE — DO NOT EDIT. Edit spec/*.toml and run codegen/generate.py."""

SPEC_VERSION = "0.1.0"

PORT = 5727
CONNECT_TIMEOUT_SECS = 5
SOCKET_TIMEOUT_SECS = 15

DISCOVERY_HEADER = "DSCV"
POLL_INTERVAL_MS = 500
POLL_MAC_PREFIX = "MAC#"
POLL_PAYLOAD = "POLL:)"
POLL_PLACEHOLDER_MAC = "00:00:00:00:00:00"

HANDSHAKE_TERMINATOR = "\r\n"
HANDSHAKE_LIST_END = "."
HANDSHAKE_ACCEPT_PREFIX = "+"
HANDSHAKE_REJECT_PREFIX = "-"
SESSION_PREAMBLE_LEN = 8

PROTOCOL_MIDI3_STREAM = "{369F50E7-750B-459A-BAEE-85ADD3F3798D}"
PROTOCOL_REQUEST_RESPONSE = "{2490272E-CD92-4DBA-AE32-E8AF37ED3B0A}"
PROTOCOL_CBOR_CONTROL = "{774CDB9E-74ED-4740-AF09-AC96B3A69A11}"
PROTOCOL_RESERVED = "{77DB6B28-785E-4641-B840-42F0F06A11FC}"

MIDI3_TAG_CONTINUATION = 0x14
MIDI3_TAG_FINAL_1 = 0x15
MIDI3_TAG_FINAL_2 = 0x16
MIDI3_TAG_FINAL_3 = 0x17

MANUFACTURER_ID = (0x00, 0x20, 0x33)
PRODUCT_PROFILER = 0x00
PRODUCT_PLAYER = 0x02
DEVICE_OMNI = 0x7f
FULL_SCALE = 16383

FN_SINGLE_PARAM = 0x01
FN_MULTI_PARAM = 0x02
FN_STRING_PARAM = 0x03
FN_BLOB = 0x04
FN_EXT_PARAM = 0x06
FN_EXT_STRING_PARAM = 0x07
FN_MORPHED_MULTI_PARAM = 0x08
FN_RENDERED_STRING_REPLY = 0x3c
FN_REQUEST_SINGLE = 0x41
FN_REQUEST_MULTI = 0x42
FN_REQUEST_STRING = 0x43
FN_REQUEST_EXT_STRING = 0x47
FN_REQUEST_RENDERED_STRING = 0x7c
FN_BEACON = 0x7e

BEACON_FUNCTION = 0x7e
BEACON_SUBCOMMAND = 0x40
BEACON_DEFAULT_PARAM_SET = 0x02
BEACON_FLAG_INIT = 0x01
BEACON_FLAG_SYSEX = 0x02
BEACON_FLAG_TUNEMODE = 0x20

EFFECT_PARAM_TYPE = 0
EFFECT_PARAM_STATE = 3
EFFECT_PARAM_MIX = 4
EFFECT_PARAM_VOLUME = 6

PAGE_STRINGS = 0x00
STRING_RIG_NAME = 0x01
PAGE_REALTIME = 0x7c
METER_BLOCK_NUMBER = 0x4e
BEAT_PULSE_NUMBER = 0x00
TUNER_DEVIANCE_NUMBER = 0x0f
PAGE_RIG_SETTINGS = 0x04
TEMPO_NUMBER = 0x00
TEMPO_BPM_SCALE = 64
RIG_VOLUME_NUMBER = 0x01
AMP_PAGE = 0x0a
AMP_ON_NUMBER = 0x02
GAIN_NUMBER = 0x04
SYSTEM_PAGE = 0x7f
MAIN_VOLUME_NUMBER = 0x00
MONITOR_VOLUME_NUMBER = 0x02
PAGE_MORPH = 0x00
MORPH_NUMBER = 0x0b
PAGE_TUNER_NOTE = 0x7d
TUNER_NOTE_NUMBER = 0x54
TUNER_IN_TUNE_CENTER = 8192
TUNER_IN_TUNE_WINDOW = 350
METER_COUNT = 11

METER_PAGE = 0x7c
METER_FIRST_NUMBER = 0x4e
METER_UPDATE_RATE_HZ = 20
STROBE_PHASE_INDEX = 3
STROBE_SEGMENT_INDICES = (0, 1, 2)

FUNCTION_NAMES = {0x01: "single-param", 0x02: "multi-param", 0x03: "string-param", 0x04: "blob", 0x06: "ext-param", 0x07: "ext-string-param", 0x08: "morphed-multi-param", 0x3c: "rendered-string-reply", 0x41: "request-single", 0x42: "request-multi", 0x43: "request-string", 0x47: "request-ext-string", 0x7c: "request-rendered-string", 0x7e: "beacon"}

PAGE_NAMES = {0x00: "String Tags", 0x04: "Rig Settings", 0x05: "Fixed FX", 0x09: "Input Section", 0x0a: "Amplifier", 0x0b: "Amplifier EQ", 0x0c: "Cabinet", 0x32: "Effect A", 0x33: "Effect B", 0x34: "Effect C", 0x35: "Effect D", 0x38: "Effect X", 0x3a: "Effect MOD", 0x3c: "Effect DLY", 0x3d: "Effect REV", 0x76: "User Scales", 0x7c: "Realtime/Meters", 0x7d: "Looper/Freeze", 0x7f: "System/Global"}

EFFECT_SLOTS = [("A", 0x32), ("B", 0x33), ("C", 0x34), ("D", 0x35), ("X", 0x38), ("MOD", 0x3a), ("DLY", 0x3c), ("REV", 0x3d)]

EFFECT_PARAMS = {0: "Type", 3: "On/Off", 4: "Mix", 6: "Volume", 7: "Stereo", 8: "Wah Manual / Freq Shifter Delay Pitch", 9: "Wah Peak", 10: "Wah Pedal Range", 12: "Wah Pedal Mode", 13: "Wah Touch Attack / Fuzz Impedance LP", 14: "Wah Touch Release", 15: "Wah Touch Boost / Delay Cross Feedback", 16: "Distortion Drive / Reverb Formant Mix", 17: "Distortion Tone / Reverb Mid Frequency", 18: "Fuzz Octa / Compressor Intensity / Noise Gate Threshold / Auto Swell Compressor", 19: "Compressor Attack / Legacy Delay Bandwidth / Legacy Reverb Bandwidth", 20: "Fuzz Transistor Shape / Modulation Rate / Auto Swell / Widener Tune", 21: "Drive Definition / Fuzz Transistor Tone / Modulation Depth / Micro Pitch Detune / Double Tracker Looseness / Widener Intensity", 22: "Modulation Feedback / Formant Reverb Vowel", 23: "Drive Slim Down / Fuzz Definition / Modulation Crossover / Octaver Low Cut", 24: "Modulation Hyper Chorus Amount", 25: "Modulation Manual / Reverb Formant Offset / Spring Reverb Spectral Balance", 26: "Modulation Peak Spread / Wah Phaser Peak Spread / Reverb Formant Peak", 27: "Modulation Stages / Wah Phaser Stages / Legacy Reverb Room Size", 30: "Rotary Speed (Slow/Fast)", 31: "Rotary Distance", 32: "Rotary Low-High-Balance", 33: "Compressor Squash / Legacy Delay Frequency / Legacy Reverb Mid Frequency", 34: "Graphic EQ Gain 80 Hz", 35: "Graphic EQ Gain 160 Hz", 36: "Graphic EQ Gain 320 Hz", 37: "Graphic EQ Gain 640 Hz", 38: "Graphic EQ Gain 1250 Hz", 39: "Graphic EQ Gain 2500 Hz", 40: "Graphic EQ Gain 5000 Hz", 41: "Graphic EQ Gain 10000 Hz", 42: "Studio/Metal EQ / Metal DS Low Gain / Acoustic Sim Body", 43: "Studio EQ Low Frequency", 44: "Studio/Metal EQ / Metal DS High Gain / Acoustic Sim Sparkle", 45: "Studio EQ High Frequency", 46: "Studio EQ Mid1 / Metal EQ/DS Middle Gain / Acoustic Sim Bronze", 47: "Studio EQ Mid1 / Metal EQ/DS Middle Frequency", 48: "Studio EQ Mid1 Q-Factor", 49: "Studio EQ Mid2 Gain / Acoustic Sim Pickup", 50: "Studio EQ Mid2 Frequency", 51: "Studio EQ Mid2 Q-Factor", 52: "Wah Peak Range", 53: "Ducking", 54: "Mix 2 (Pitch/Octaver/Delay Serial/Crystal Mix / Space Intensity)", 55: "Voice Balance / Delay Balance", 56: "Voice 1 Pitch / Toe Pitch / Transpose Pitch / Quad Voice Pitch 4 / Crystal 1 Pitch", 57: "Voice 2 Pitch / Heel Pitch / Quad Voice Pitch 3 / Wah Formant Pitch Shift / Crystal 2 Pitch", 58: "Pitch Detune", 60: "Smooth Chords", 61: "Pure Tuning", 62: "Voice 1 Interval / Quad Voice 4 Interval", 63: "Voice 2 Interval / Quad Voice 3 Interval", 64: "Key", 65: "Formant Shift Freeze", 66: "Formant Shift Offset", 67: "Equalizer Low Cut", 68: "Equalizer High Cut / Reverb High Cut", 69: "Mix 3 (delay/reverb)", 70: "Mix Pre/Post", 71: "Delay 1 Time / Reverb Room Size / Reverb Attack / Spring Size", 72: "Delay 2 Time / Reverb Predelay Time", 73: "Delay 2 Ratio / Quad Delay 3 Ratio / Rate Flanger/Phaser Oneway", 74: "Quad Delay 2 Ratio / Delay Ratio Serial", 75: "Quad Delay 1 Ratio", 76: "Delay Note Value 1 / Quad Note Value 4", 77: "Delay Note Value 2 / Quad Note Value 3 / Reverb Predelay Note Value", 78: "Quad Note Value 2 / Note Value Serial", 79: "Quad Note Value 1", 80: "To Tempo / Equalizer Steep Low", 81: "Delay Volume 4", 82: "Delay Volume 3", 83: "Delay Volume 2", 84: "Delay Volume 1", 85: "Delay Panorama 4", 86: "Delay Panorama 3", 87: "Delay Panorama 2", 88: "Delay Panorama 1", 89: "Voice Pitch 2 / Crystal Pitch", 90: "Voice Pitch 1", 91: "Voice 3 Interval", 92: "Voice 4 Interval", 93: "Delay Feedback 1 / Reverb Decay Time", 94: "Infinity Feedback", 95: "Infinity", 96: "Feedback 2/Serial / Reverb Low Boost / Echo Reverb Feedback / Ionosphere Buildup", 97: "Delay Feedback Sync", 98: "Delay Low Cut / Reverb Low Decay/Damp", 99: "Delay High Cut / Reverb High Decay/Damp / Fuzz True Impedance", 100: "Delay Cut More / Equalizer Steep High / Full OC HP/LP / Effect Loop (Stage)", 101: "Modulation (delay/reverb)", 102: "Delay Chorus", 103: "Delay Flutter Intensity / Reverb Modulation", 104: "Delay Flutter Rate / Reverb Early Diffusion / Spring Dripstone", 105: "Delay Grit / Reverb Brass / Spring Distortion (Dwell)", 106: "Reverse Mix", 107: "Input Swell", 108: "Smear", 109: "Ducking Pre/Post"}

NON_EFFECT_PARAMS = {
    (0x04, 0): "Tempo bpm",
    (0x04, 1): "Rig Volume",
    (0x04, 2): "Tempo Enable",
    (0x04, 3): "Panorama",
    (0x04, 4): "Transpose",
    (0x04, 64): "Stomps Section On/Off",
    (0x04, 65): "Stack Section On/Off",
    (0x04, 66): "Effects Section On/Off",
    (0x04, 68): "Volume Pedal Location",
    (0x04, 69): "Volume Pedal Range",
    (0x04, 71): "Parallel Path Enable",
    (0x04, 72): "Parallel Path Mix",
    (0x04, 73): "Rig Spillover Off",
    (0x04, 74): "DLY+REV Routing",
    (0x05, 1): "Fixed Transpose On/Off",
    (0x05, 6): "Fixed Noise Gate On/Off",
    (0x05, 11): "Fixed Compressor On/Off",
    (0x05, 16): "Fixed Boost On/Off",
    (0x05, 21): "Fixed Wah On/Off",
    (0x05, 26): "Fixed Vintage Chorus On/Off",
    (0x05, 36): "Fixed Air Chorus On/Off",
    (0x05, 41): "Fixed Double Tracker On/Off",
    (0x09, 3): "Noise Gate Intensity",
    (0x09, 4): "Clean Sense",
    (0x09, 5): "Distortion Sense",
    (0x0a, 0): "Amp Model",
    (0x0a, 2): "On/Off",
    (0x0a, 3): "Amp Volume",
    (0x0a, 4): "Gain",
    (0x0a, 5): "Clean Compensation",
    (0x0a, 6): "Definition",
    (0x0a, 7): "Clarity",
    (0x0a, 8): "Power Sagging",
    (0x0a, 9): "Pick",
    (0x0a, 10): "Compressor",
    (0x0a, 11): "Tube Shape",
    (0x0a, 12): "Tube Bias",
    (0x0a, 15): "Direct Mix",
    (0x0a, 21): "Bright Cap Intensity",
    (0x0b, 4): "Bass",
    (0x0b, 5): "Middle",
    (0x0b, 6): "Treble",
    (0x0b, 7): "Presence",
    (0x0b, 8): "Position Pre/Post",
    (0x0c, 2): "On/Off",
    (0x0c, 4): "High Shift",
    (0x0c, 5): "Low Shift",
    (0x0c, 6): "Character",
    (0x0c, 7): "Pure Cabinet",
    (0x0c, 8): "Kone Imprint Select",
    (0x0c, 9): "Low Cut",
    (0x0c, 10): "High Cut",
    (0x76, 0): "User Scale 1 Step",
    (0x76, 1): "User Scale 1 Step",
    (0x76, 2): "User Scale 1 Step",
    (0x76, 3): "User Scale 1 Step",
    (0x76, 4): "User Scale 1 Step",
    (0x76, 5): "User Scale 1 Step",
    (0x76, 6): "User Scale 1 Step",
    (0x76, 7): "User Scale 1 Step",
    (0x76, 8): "User Scale 1 Step",
    (0x76, 9): "User Scale 1 Step",
    (0x76, 10): "User Scale 1 Step",
    (0x76, 11): "User Scale 1 Step",
    (0x76, 12): "User Scale 2 Step",
    (0x76, 13): "User Scale 2 Step",
    (0x76, 14): "User Scale 2 Step",
    (0x76, 15): "User Scale 2 Step",
    (0x76, 16): "User Scale 2 Step",
    (0x76, 17): "User Scale 2 Step",
    (0x76, 18): "User Scale 2 Step",
    (0x76, 19): "User Scale 2 Step",
    (0x76, 20): "User Scale 2 Step",
    (0x76, 21): "User Scale 2 Step",
    (0x76, 22): "User Scale 2 Step",
    (0x76, 23): "User Scale 2 Step",
    (0x7c, 0): "Tempo/Beat Pulse",
    (0x7c, 15): "Tuner Deviance",
    (0x7c, 78): "Tuner Strobe Segment (phase-low)",
    (0x7c, 79): "Tuner Strobe Segment (phase-mid)",
    (0x7c, 80): "Tuner Strobe Segment (phase-high)",
    (0x7c, 81): "Tuner Strobe Phase",
    (0x7c, 82): "Meter: Stack Level (pre-vol)",
    (0x7c, 83): "Meter: Stack Power",
    (0x7c, 84): "Meter: Rig Output Level",
    (0x7c, 85): "Meter: Rig Output Power",
    (0x7c, 86): "Meter: (unused v8)",
    (0x7c, 87): "Meter: Loudness (RMS)",
    (0x7c, 88): "Meter: (unused v10)",
    (0x7d, 84): "Tuner Note",
    (0x7d, 88): "Looper Record/Playback/Overdub",
    (0x7d, 89): "Looper Stop",
    (0x7d, 90): "Looper Trigger",
    (0x7d, 91): "Looper Reverse",
    (0x7d, 92): "Looper Half Speed",
    (0x7d, 93): "Looper Cancel/Reactivate Overdub",
    (0x7d, 94): "Looper Erase Loop",
    (0x7d, 107): "Module A Freeze",
    (0x7d, 108): "Module B Freeze",
    (0x7d, 109): "Module C Freeze",
    (0x7d, 110): "Module D Freeze",
    (0x7d, 111): "Module X Freeze",
    (0x7d, 113): "Module MOD Freeze",
    (0x7d, 114): "Module DLY Freeze",
    (0x7d, 115): "Module REV Freeze",
    (0x7f, 0): "Main Output Volume",
    (0x7f, 1): "Headphone Output Volume",
    (0x7f, 2): "Monitor Output Volume",
    (0x7f, 3): "Direct Output / Send 1 Volume",
    (0x7f, 4): "S/PDIF Output Volume",
    (0x7f, 8): "Monitor Cab. Off",
    (0x7f, 12): "Main Output EQ Bass",
    (0x7f, 13): "Main Output EQ Middle",
    (0x7f, 14): "Main Output EQ Treble",
    (0x7f, 15): "Main Output EQ Presence",
    (0x7f, 16): "Output Filter Low Cut",
    (0x7f, 17): "Monitor Output EQ Bass",
    (0x7f, 18): "Monitor Output EQ Middle",
    (0x7f, 19): "Monitor Output EQ Treble",
    (0x7f, 20): "Monitor Output EQ Presence",
    (0x7f, 21): "Output Filter High Cut",
    (0x7f, 32): "Aux In >Main",
    (0x7f, 33): "Aux In >Monitor",
    (0x7f, 34): "Aux In >Headphone",
    (0x7f, 36): "Space Intensity",
    (0x7f, 37): "Space Routing",
    (0x7f, 38): "Kone Mode",
    (0x7f, 39): "Kone Bass Boost",
    (0x7f, 40): "Kone Imprint Select",
    (0x7f, 41): "Kone Directivity",
    (0x7f, 42): "Kone Sweetening",
    (0x7f, 44): "Input Source",
    (0x7f, 50): "Pure Cabinet Enable",
    (0x7f, 51): "Pure Cabinet Level (Global)",
    (0x7f, 52): "Looper Volume",
    (0x7f, 53): "Looper Location",
    (0x7f, 59): "Aux >Mono",
    (0x7f, 126): "Tuner Mode State",
}

STRING_TAGS = {1: "Rig Name", 2: "Rig Author", 3: "Rig Creation Date", 4: "Rig Comment", 10: "Amp Name", 11: "Amp Author", 14: "Amp Location", 15: "Amp Manufacturer", 16: "Amp Comment", 18: "Amp Model", 19: "Amp Channel", 20: "Pickup Type", 21: "Year of Production", 32: "Cabinet Name", 33: "Cabinet Author", 36: "Cabinet Location", 37: "Cabinet Manufacturer", 38: "Microphone Model", 39: "Cabinet Comment", 40: "Microphone Position", 41: "Speaker Configuration", 42: "Cabinet Model", 44: "Speaker Manufacturer", 45: "Speaker Model"}

PAGE0_NUMERIC = {0x0b: "Morph State"}

EFFECT_TYPES = {
    0: "empty",
    1: "Wah Wah",
    2: "Wah Low Pass",
    3: "Wah High Pass",
    4: "Wah Vowel Filter",
    6: "Wah Phaser",
    7: "Wah Flanger",
    8: "Wah Rate Reducer",
    9: "Wah Ring Modulator",
    10: "Wah Freq Shifter",
    11: "Pedal Pitch",
    12: "Wah Formant Shifter",
    13: "Pedal Vinyl Stop",
    17: "Bit Shaper",
    18: "Octa Shaper",
    19: "Soft Shaper",
    20: "Hard Shaper",
    21: "Wave Shaper",
    32: "Kemper Drive",
    33: "Green Scream",
    34: "Plus DS",
    35: "One DS",
    36: "Muffin",
    37: "Mouse",
    38: "Kemper Fuzz",
    39: "Metal DS",
    42: "Full OC",
    49: "Compressor",
    50: "Auto Swell",
    57: "Noise Gate 2:1",
    58: "Noise Gate 4:1",
    64: "Space",
    65: "Vintage Chorus",
    66: "Hyper Chorus",
    67: "Air Chorus",
    68: "Vibrato",
    69: "Rotary Speaker",
    70: "Tremolo",
    71: "Micro Pitch",
    81: "Phaser",
    82: "Phaser Vibe",
    83: "Phaser Oneway",
    89: "Flanger",
    91: "Flanger Oneway",
    97: "Graphic Equalizer",
    98: "Studio Equalizer",
    99: "Metal Equalizer",
    100: "Acoustic Simulator",
    101: "Stereo Widener",
    102: "Phase Widener",
    103: "Delay Widener",
    104: "Double Tracker",
    113: "Treble Booster",
    114: "Lead Booster",
    115: "Pure Booster",
    116: "Wah Pedal Booster",
    121: "Loop Mono",
    122: "Loop Stereo",
    123: "Loop Distortion",
    129: "Transpose",
    130: "Chromatic Pitch",
    131: "Harmonic Pitch",
    132: "Analog Octaver",
    137: "Dual Chromatic",
    138: "Dual Harmonic",
    139: "Dual Crystal",
    140: "Dual Loop Pitch",
    145: "Legacy Delay",
    146: "Single Delay",
    147: "Dual Delay",
    148: "Two Tap Delay",
    149: "Serial TwoTap Delay",
    150: "Crystal Delay",
    151: "Loop Pitch Delay",
    152: "Freq Shifter Delay",
    161: "Rhythm Delay",
    162: "Melody Chromatic",
    163: "Melody Harmonic",
    164: "Quad Delay",
    165: "Quad Chromatic",
    166: "Quad Harmonic",
    177: "Legacy Reverb",
    178: "Natural Reverb",
    179: "Easy Reverb",
    180: "Echo Reverb",
    181: "Cirrus Reverb",
    182: "Formant Reverb",
    183: "Ionosphere Reverb",
    193: "Spring Reverb",
}

CC_WAH_PEDAL = 1
CC_PITCH_PEDAL = 4
CC_VOLUME_PEDAL = 7
CC_PANORAMA = 10
CC_MORPH_PEDAL = 11
CC_DELAY_MIX = 68
CC_DELAY_FEEDBACK = 69
CC_REVERB_MIX = 70
CC_REVERB_TIME = 71
CC_GAIN = 72
CC_MONITOR_VOLUME = 73
CC_TOGGLE_ALL_MODULES = 16
CC_MODULE_A = 17
CC_MODULE_B = 18
CC_MODULE_C = 19
CC_MODULE_D = 20
CC_MODULE_X = 22
CC_MODULE_MOD = 24
CC_MODULE_DLY_NO_SPILL = 26
CC_MODULE_DLY = 27
CC_MODULE_REV_NO_SPILL = 28
CC_MODULE_REV = 29
CC_TAP_TEMPO = 30
CC_TUNER_MODE = 31
CC_ROTARY_SPEED = 33
CC_DELAY_INFINITY = 34
CC_FREEZE = 35
CC_BANK_PRESELECT = 47
CC_UP = 48
CC_DOWN = 49
CC_LOAD_SLOT_1 = 50
CC_LOAD_SLOT_2 = 51
CC_LOAD_SLOT_3 = 52
CC_LOAD_SLOT_4 = 53
CC_LOAD_SLOT_5 = 54
CC_EFFECT_BUTTON_I = 75
CC_EFFECT_BUTTON_II = 76
CC_EFFECT_BUTTON_III = 77
CC_EFFECT_BUTTON_IIII = 78
CC_MORPH_BUTTON = 80
CC_BANK_SELECT_MSB = 0
CC_BANK_SELECT_LSB = 32

SLOT_ENABLE_CC = {"A": 17, "B": 18, "C": 19, "D": 20, "X": 22, "MOD": 24, "DLY": 27, "REV": 29}

PROGRAM_CHANGE_STATUS = 0xc0
CONTROL_CHANGE_STATUS = 0xb0

# (index, number, id, name, render)
METER_FIELDS = [
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
]

