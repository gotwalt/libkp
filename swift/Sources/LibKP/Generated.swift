// GENERATED FILE — DO NOT EDIT. Edit spec/*.toml and run codegen/generate.py.
// swift-format-ignore-file
import Foundation

public enum Generated {
    public static let specVersion: String = "0.7.0"
    public static let port: UInt16 = 5727
    public static let connectTimeoutSecs: UInt64 = 5
    public static let socketTimeoutSecs: UInt64 = 15
    public static let connectionCooldownMs: UInt64 = 1000
    public static let requestTimeoutMs: UInt64 = 300
    public static let maxInFlightRequests: Int = 16
    public static let dumpSettleMs: UInt64 = 1000
    public static let reconnectDelayMs: UInt64 = 4000
    public static let reconnectMaxDelayMs: UInt64 = 30000
    public static let controlReopenMinGapMs: UInt64 = 30000
    public static let rigLoadSettleMs: UInt64 = 500
    public static let pendingWindowMs: UInt64 = 1500
    public static let rigLoadControllers: [UInt8] = [48, 49, 50, 51, 52, 53, 54]
    public static let handshakeTimeoutMs: UInt64 = 2000
    public static let discoveryHeader: String = "DSCV"
    public static let pollIntervalMs: UInt64 = 500
    public static let pollMacPrefix: String = "MAC#"
    public static let pollPayload: String = "POLL:)"
    public static let pollPlaceholderMac: String = "00:00:00:00:00:00"
    public static let handshakeTerminator: String = "\r\n"
    public static let handshakeListEnd: String = "."
    public static let handshakeAcceptPrefix: String = "+"
    public static let handshakeRejectPrefix: String = "-"
    public static let sessionPreambleLen: Int = 8
    public static let protocolMidi3Stream: String = "{369F50E7-750B-459A-BAEE-85ADD3F3798D}"
    public static let protocolRequestResponse: String = "{2490272E-CD92-4DBA-AE32-E8AF37ED3B0A}"
    public static let protocolCborControl: String = "{774CDB9E-74ED-4740-AF09-AC96B3A69A11}"
    public static let protocolReserved: String = "{77DB6B28-785E-4641-B840-42F0F06A11FC}"
    public static let cborItemTag: UInt64 = 1
    public static let cborSelectorSingle: Int64 = 1
    public static let cborSelectorMulti: Int64 = 2
    public static let cborSelectorString: Int64 = 4
    public static let cborFillerByte: UInt8 = 0xc0
    public static let stateDumpTriggerAddress: UInt32 = 102528
    public static let stateDumpTriggerValue: Int64 = 1
    public static let dumpEndAddress: UInt32 = 100800
    public static let sensitiveAddresses: [UInt32] = [200008, 200009]
    public static let redactedPlaceholder: String = "[redacted]"
    public static let midi3TagContinuation: UInt8 = 0x14
    public static let midi3TagFinal1: UInt8 = 0x15
    public static let midi3TagFinal2: UInt8 = 0x16
    public static let midi3TagFinal3: UInt8 = 0x17
    public static let manufacturerId: [UInt8] = [0x00, 0x20, 0x33]
    public static let productProfiler: UInt8 = 0x00
    public static let productPlayer: UInt8 = 0x02
    public static let deviceOmni: UInt8 = 0x7f
    public static let fullScale: UInt16 = 16383
    public static let fnSingleParam: UInt8 = 0x01
    public static let fnMultiParam: UInt8 = 0x02
    public static let fnStringParam: UInt8 = 0x03
    public static let fnBlob: UInt8 = 0x04
    public static let fnExtParam: UInt8 = 0x06
    public static let fnExtStringParam: UInt8 = 0x07
    public static let fnMorphedMultiParam: UInt8 = 0x08
    public static let fnRenderedStringReply: UInt8 = 0x3c
    public static let fnRequestSingle: UInt8 = 0x41
    public static let fnRequestMulti: UInt8 = 0x42
    public static let fnRequestString: UInt8 = 0x43
    public static let fnRequestExtParam: UInt8 = 0x46
    public static let fnRequestExtString: UInt8 = 0x47
    public static let fnRequestRenderedString: UInt8 = 0x7c
    public static let fnBeacon: UInt8 = 0x7e
    public static let beaconFunction: UInt8 = 0x7e
    public static let beaconSubcommand: UInt8 = 0x40
    public static let beaconDefaultParamSet: UInt8 = 0x02
    public static let beaconFlagInit: UInt8 = 0x01
    public static let beaconFlagSysex: UInt8 = 0x02
    public static let beaconFlagTunemode: UInt8 = 0x20
    public static let effectParamType: UInt8 = 0
    public static let effectParamState: UInt8 = 3
    public static let effectParamMix: UInt8 = 4
    public static let effectParamVolume: UInt8 = 6
    public static let pageStrings: UInt8 = 0x00
    public static let stringRigName: UInt8 = 0x01
    public static let pageRealtime: UInt8 = 0x7c
    public static let meterBlockNumber: UInt8 = 0x4e
    public static let beatPulseNumber: UInt8 = 0x00
    public static let tunerDevianceNumber: UInt8 = 0x0f
    public static let pageRigSettings: UInt8 = 0x04
    public static let tempoNumber: UInt8 = 0x00
    public static let tempoBpmScale: UInt16 = 64
    public static let rigVolumeNumber: UInt8 = 0x01
    public static let ampPage: UInt8 = 0x0a
    public static let ampOnNumber: UInt8 = 0x02
    public static let gainNumber: UInt8 = 0x04
    public static let systemPage: UInt8 = 0x7f
    public static let mainVolumeNumber: UInt8 = 0x00
    public static let headphoneVolumeNumber: UInt8 = 0x01
    public static let monitorVolumeNumber: UInt8 = 0x02
    public static let pageBankPreview: UInt8 = 0x96
    public static let bankSlots: Int = 5
    public static let bankRigNameBase: UInt8 = 0x00
    public static let bankAmpNameBase: UInt8 = 0x05
    public static let bankCabinetNameBase: UInt8 = 0x0a
    public static let pageMorph: UInt8 = 0x00
    public static let morphNumber: UInt8 = 0x77
    public static let morphButtonNumber: UInt8 = 0x50
    public static let morphAddress: UInt32 = 119
    public static let pageTunerNote: UInt8 = 0x7d
    public static let tunerNoteNumber: UInt8 = 0x54
    public static let tunerInTuneCenter: UInt16 = 8192
    public static let tunerInTuneWindow: UInt16 = 350
    public static let meterCount: Int = 11
    public static let currentBankAddress: UInt32 = 100701
    public static let currentRigSlotAddress: UInt32 = 100702
    public static let stringRigAuthor: UInt8 = 0x02
    public static let stringRigDate: UInt8 = 0x03
    public static let stringRigComment: UInt8 = 0x04
    public static let stringAmpName: UInt8 = 0x10
    public static let stringCabinetName: UInt8 = 0x20
    public static let cabinetPage: UInt8 = 0x0c
    public static let cabinetOnNumber: UInt8 = 0x02
    public static let meterPage: UInt8 = 0x7c
    public static let meterFirstNumber: UInt8 = 0x4e
    public static let meterUpdateRateHz: Int = 20
    public static let strobePhaseIndex: Int = 3
    public static let strobeSegmentIndices: [Int] = [0, 1, 2]
    public static let ccWahPedal: UInt8 = 1
    public static let ccPitchPedal: UInt8 = 4
    public static let ccVolumePedal: UInt8 = 7
    public static let ccPanorama: UInt8 = 10
    public static let ccMorphPedal: UInt8 = 11
    public static let ccDelayMix: UInt8 = 68
    public static let ccDelayFeedback: UInt8 = 69
    public static let ccReverbMix: UInt8 = 70
    public static let ccReverbTime: UInt8 = 71
    public static let ccGain: UInt8 = 72
    public static let ccMonitorVolume: UInt8 = 73
    public static let ccToggleAllModules: UInt8 = 16
    public static let ccModuleA: UInt8 = 17
    public static let ccModuleB: UInt8 = 18
    public static let ccModuleC: UInt8 = 19
    public static let ccModuleD: UInt8 = 20
    public static let ccModuleX: UInt8 = 22
    public static let ccModuleMod: UInt8 = 24
    public static let ccModuleDlyNoSpill: UInt8 = 26
    public static let ccModuleDly: UInt8 = 27
    public static let ccModuleRevNoSpill: UInt8 = 28
    public static let ccModuleRev: UInt8 = 29
    public static let ccTapTempo: UInt8 = 30
    public static let ccTunerMode: UInt8 = 31
    public static let ccRotarySpeed: UInt8 = 33
    public static let ccDelayInfinity: UInt8 = 34
    public static let ccFreeze: UInt8 = 35
    public static let ccBankPreselect: UInt8 = 47
    public static let ccUp: UInt8 = 48
    public static let ccDown: UInt8 = 49
    public static let ccLoadSlot1: UInt8 = 50
    public static let ccLoadSlot2: UInt8 = 51
    public static let ccLoadSlot3: UInt8 = 52
    public static let ccLoadSlot4: UInt8 = 53
    public static let ccLoadSlot5: UInt8 = 54
    public static let ccEffectButtonI: UInt8 = 75
    public static let ccEffectButtonIi: UInt8 = 76
    public static let ccEffectButtonIii: UInt8 = 77
    public static let ccEffectButtonIiii: UInt8 = 78
    public static let ccMorphButton: UInt8 = 80
    public static let ccBankSelectMsb: UInt8 = 0
    public static let ccBankSelectLsb: UInt8 = 32
    public static let programChangeStatus: UInt8 = 0xc0
    public static let controlChangeStatus: UInt8 = 0xb0

    public static let functionNames: [UInt8: String] = [0x01: "single-param", 0x02: "multi-param", 0x03: "string-param", 0x04: "blob", 0x06: "ext-param", 0x07: "ext-string-param", 0x08: "morphed-multi-param", 0x3c: "rendered-string-reply", 0x41: "request-single", 0x42: "request-multi", 0x43: "request-string", 0x47: "request-ext-string", 0x7c: "request-rendered-string", 0x7e: "beacon"]
    public static let pageNames: [UInt8: String] = [0x00: "String Tags", 0x04: "Rig Settings", 0x05: "Fixed FX", 0x09: "Input Section", 0x0a: "Amplifier", 0x0b: "Amplifier EQ", 0x0c: "Cabinet", 0x32: "Effect A", 0x33: "Effect B", 0x34: "Effect C", 0x35: "Effect D", 0x38: "Effect X", 0x3a: "Effect MOD", 0x3c: "Effect DLY", 0x3d: "Effect REV", 0x76: "User Scales", 0x7c: "Realtime/Meters", 0x7d: "Looper/Freeze", 0x7f: "System/Global", 0x96: "Bank Preview"]
    public static let effectSlots: [(String, UInt8)] = [("A", 0x32), ("B", 0x33), ("C", 0x34), ("D", 0x35), ("X", 0x38), ("MOD", 0x3a), ("DLY", 0x3c), ("REV", 0x3d)]
    public static let effectParams: [UInt8: String] = [0: "Type", 3: "On/Off", 4: "Mix", 6: "Volume", 7: "Stereo", 8: "Wah Manual / Freq Shifter Delay Pitch", 9: "Wah Peak", 10: "Wah Pedal Range", 12: "Wah Pedal Mode", 13: "Wah Touch Attack / Fuzz Impedance LP", 14: "Wah Touch Release", 15: "Wah Touch Boost / Delay Cross Feedback", 16: "Distortion Drive / Reverb Formant Mix", 17: "Distortion Tone / Reverb Mid Frequency", 18: "Fuzz Octa / Compressor Intensity / Noise Gate Threshold / Auto Swell Compressor", 19: "Compressor Attack / Legacy Delay Bandwidth / Legacy Reverb Bandwidth", 20: "Fuzz Transistor Shape / Modulation Rate / Auto Swell / Widener Tune", 21: "Drive Definition / Fuzz Transistor Tone / Modulation Depth / Micro Pitch Detune / Double Tracker Looseness / Widener Intensity", 22: "Modulation Feedback / Formant Reverb Vowel", 23: "Drive Slim Down / Fuzz Definition / Modulation Crossover / Octaver Low Cut", 24: "Modulation Hyper Chorus Amount", 25: "Modulation Manual / Reverb Formant Offset / Spring Reverb Spectral Balance", 26: "Modulation Peak Spread / Wah Phaser Peak Spread / Reverb Formant Peak", 27: "Modulation Stages / Wah Phaser Stages / Legacy Reverb Room Size", 30: "Rotary Speed (Slow/Fast)", 31: "Rotary Distance", 32: "Rotary Low-High-Balance", 33: "Compressor Squash / Legacy Delay Frequency / Legacy Reverb Mid Frequency", 34: "Graphic EQ Gain 80 Hz", 35: "Graphic EQ Gain 160 Hz", 36: "Graphic EQ Gain 320 Hz", 37: "Graphic EQ Gain 640 Hz", 38: "Graphic EQ Gain 1250 Hz", 39: "Graphic EQ Gain 2500 Hz", 40: "Graphic EQ Gain 5000 Hz", 41: "Graphic EQ Gain 10000 Hz", 42: "Studio/Metal EQ / Metal DS Low Gain / Acoustic Sim Body", 43: "Studio EQ Low Frequency", 44: "Studio/Metal EQ / Metal DS High Gain / Acoustic Sim Sparkle", 45: "Studio EQ High Frequency", 46: "Studio EQ Mid1 / Metal EQ/DS Middle Gain / Acoustic Sim Bronze", 47: "Studio EQ Mid1 / Metal EQ/DS Middle Frequency", 48: "Studio EQ Mid1 Q-Factor", 49: "Studio EQ Mid2 Gain / Acoustic Sim Pickup", 50: "Studio EQ Mid2 Frequency", 51: "Studio EQ Mid2 Q-Factor", 52: "Wah Peak Range", 53: "Ducking", 54: "Mix 2 (Pitch/Octaver/Delay Serial/Crystal Mix / Space Intensity)", 55: "Voice Balance / Delay Balance", 56: "Voice 1 Pitch / Toe Pitch / Transpose Pitch / Quad Voice Pitch 4 / Crystal 1 Pitch", 57: "Voice 2 Pitch / Heel Pitch / Quad Voice Pitch 3 / Wah Formant Pitch Shift / Crystal 2 Pitch", 58: "Pitch Detune", 60: "Smooth Chords", 61: "Pure Tuning", 62: "Voice 1 Interval / Quad Voice 4 Interval", 63: "Voice 2 Interval / Quad Voice 3 Interval", 64: "Key", 65: "Formant Shift Freeze", 66: "Formant Shift Offset", 67: "Equalizer Low Cut", 68: "Equalizer High Cut / Reverb High Cut", 69: "Mix 3 (delay/reverb)", 70: "Mix Pre/Post", 71: "Delay 1 Time / Reverb Room Size / Reverb Attack / Spring Size", 72: "Delay 2 Time / Reverb Predelay Time", 73: "Delay 2 Ratio / Quad Delay 3 Ratio / Rate Flanger/Phaser Oneway", 74: "Quad Delay 2 Ratio / Delay Ratio Serial", 75: "Quad Delay 1 Ratio", 76: "Delay Note Value 1 / Quad Note Value 4", 77: "Delay Note Value 2 / Quad Note Value 3 / Reverb Predelay Note Value", 78: "Quad Note Value 2 / Note Value Serial", 79: "Quad Note Value 1", 80: "To Tempo / Equalizer Steep Low", 81: "Delay Volume 4", 82: "Delay Volume 3", 83: "Delay Volume 2", 84: "Delay Volume 1", 85: "Delay Panorama 4", 86: "Delay Panorama 3", 87: "Delay Panorama 2", 88: "Delay Panorama 1", 89: "Voice Pitch 2 / Crystal Pitch", 90: "Voice Pitch 1", 91: "Voice 3 Interval", 92: "Voice 4 Interval", 93: "Delay Feedback 1 / Reverb Decay Time", 94: "Infinity Feedback", 95: "Infinity", 96: "Feedback 2/Serial / Reverb Low Boost / Echo Reverb Feedback / Ionosphere Buildup", 97: "Delay Feedback Sync", 98: "Delay Low Cut / Reverb Low Decay/Damp", 99: "Delay High Cut / Reverb High Decay/Damp / Fuzz True Impedance", 100: "Delay Cut More / Equalizer Steep High / Full OC HP/LP / Effect Loop (Stage)", 101: "Modulation (delay/reverb)", 102: "Delay Chorus", 103: "Delay Flutter Intensity / Reverb Modulation", 104: "Delay Flutter Rate / Reverb Early Diffusion / Spring Dripstone", 105: "Delay Grit / Reverb Brass / Spring Distortion (Dwell)", 106: "Reverse Mix", 107: "Input Swell", 108: "Smear", 109: "Ducking Pre/Post"]
    public static let nonEffectParams: [NonEffectKey: String] = [
        NonEffectKey(0x04, 0): "Tempo bpm",
        NonEffectKey(0x04, 1): "Rig Volume",
        NonEffectKey(0x04, 2): "Tempo Enable",
        NonEffectKey(0x04, 3): "Panorama",
        NonEffectKey(0x04, 4): "Transpose",
        NonEffectKey(0x04, 64): "Stomps Section On/Off",
        NonEffectKey(0x04, 65): "Stack Section On/Off",
        NonEffectKey(0x04, 66): "Effects Section On/Off",
        NonEffectKey(0x04, 68): "Volume Pedal Location",
        NonEffectKey(0x04, 69): "Volume Pedal Range",
        NonEffectKey(0x04, 71): "Parallel Path Enable",
        NonEffectKey(0x04, 72): "Parallel Path Mix",
        NonEffectKey(0x04, 73): "Rig Spillover Off",
        NonEffectKey(0x04, 74): "DLY+REV Routing",
        NonEffectKey(0x05, 1): "Fixed Transpose On/Off",
        NonEffectKey(0x05, 6): "Fixed Noise Gate On/Off",
        NonEffectKey(0x05, 11): "Fixed Compressor On/Off",
        NonEffectKey(0x05, 16): "Fixed Boost On/Off",
        NonEffectKey(0x05, 21): "Fixed Wah On/Off",
        NonEffectKey(0x05, 26): "Fixed Vintage Chorus On/Off",
        NonEffectKey(0x05, 36): "Fixed Air Chorus On/Off",
        NonEffectKey(0x05, 41): "Fixed Double Tracker On/Off",
        NonEffectKey(0x09, 3): "Noise Gate Intensity",
        NonEffectKey(0x09, 4): "Clean Sense",
        NonEffectKey(0x09, 5): "Distortion Sense",
        NonEffectKey(0x0a, 0): "Amp Model",
        NonEffectKey(0x0a, 2): "On/Off",
        NonEffectKey(0x0a, 3): "Amp Volume",
        NonEffectKey(0x0a, 4): "Gain",
        NonEffectKey(0x0a, 5): "Clean Compensation",
        NonEffectKey(0x0a, 6): "Definition",
        NonEffectKey(0x0a, 7): "Clarity",
        NonEffectKey(0x0a, 8): "Power Sagging",
        NonEffectKey(0x0a, 9): "Pick",
        NonEffectKey(0x0a, 10): "Compressor",
        NonEffectKey(0x0a, 11): "Tube Shape",
        NonEffectKey(0x0a, 12): "Tube Bias",
        NonEffectKey(0x0a, 15): "Direct Mix",
        NonEffectKey(0x0a, 20): "Gain (smoothed follower)",
        NonEffectKey(0x0a, 21): "Bright Cap Intensity",
        NonEffectKey(0x0b, 4): "Bass",
        NonEffectKey(0x0b, 5): "Middle",
        NonEffectKey(0x0b, 6): "Treble",
        NonEffectKey(0x0b, 7): "Presence",
        NonEffectKey(0x0b, 8): "Position Pre/Post",
        NonEffectKey(0x0c, 2): "On/Off",
        NonEffectKey(0x0c, 4): "High Shift",
        NonEffectKey(0x0c, 5): "Low Shift",
        NonEffectKey(0x0c, 6): "Character",
        NonEffectKey(0x0c, 7): "Pure Cabinet",
        NonEffectKey(0x0c, 8): "Kone Imprint Select",
        NonEffectKey(0x0c, 9): "Low Cut",
        NonEffectKey(0x0c, 10): "High Cut",
        NonEffectKey(0x76, 0): "User Scale 1 Step",
        NonEffectKey(0x76, 1): "User Scale 1 Step",
        NonEffectKey(0x76, 2): "User Scale 1 Step",
        NonEffectKey(0x76, 3): "User Scale 1 Step",
        NonEffectKey(0x76, 4): "User Scale 1 Step",
        NonEffectKey(0x76, 5): "User Scale 1 Step",
        NonEffectKey(0x76, 6): "User Scale 1 Step",
        NonEffectKey(0x76, 7): "User Scale 1 Step",
        NonEffectKey(0x76, 8): "User Scale 1 Step",
        NonEffectKey(0x76, 9): "User Scale 1 Step",
        NonEffectKey(0x76, 10): "User Scale 1 Step",
        NonEffectKey(0x76, 11): "User Scale 1 Step",
        NonEffectKey(0x76, 12): "User Scale 2 Step",
        NonEffectKey(0x76, 13): "User Scale 2 Step",
        NonEffectKey(0x76, 14): "User Scale 2 Step",
        NonEffectKey(0x76, 15): "User Scale 2 Step",
        NonEffectKey(0x76, 16): "User Scale 2 Step",
        NonEffectKey(0x76, 17): "User Scale 2 Step",
        NonEffectKey(0x76, 18): "User Scale 2 Step",
        NonEffectKey(0x76, 19): "User Scale 2 Step",
        NonEffectKey(0x76, 20): "User Scale 2 Step",
        NonEffectKey(0x76, 21): "User Scale 2 Step",
        NonEffectKey(0x76, 22): "User Scale 2 Step",
        NonEffectKey(0x76, 23): "User Scale 2 Step",
        NonEffectKey(0x7c, 0): "Tempo/Beat Pulse",
        NonEffectKey(0x7c, 15): "Tuner Deviance",
        NonEffectKey(0x7c, 78): "Tuner Strobe Segment (phase-low)",
        NonEffectKey(0x7c, 79): "Tuner Strobe Segment (phase-mid)",
        NonEffectKey(0x7c, 80): "Tuner Strobe Segment (phase-high)",
        NonEffectKey(0x7c, 81): "Tuner Strobe Phase",
        NonEffectKey(0x7c, 82): "Meter: Stack Level (pre-vol)",
        NonEffectKey(0x7c, 83): "Meter: Stack Power",
        NonEffectKey(0x7c, 84): "Meter: Rig Output Level",
        NonEffectKey(0x7c, 85): "Meter: Rig Output Power",
        NonEffectKey(0x7c, 86): "Meter: (unused v8)",
        NonEffectKey(0x7c, 87): "Meter: Loudness (RMS)",
        NonEffectKey(0x7c, 88): "Meter: (unused v10)",
        NonEffectKey(0x7d, 84): "Tuner Note",
        NonEffectKey(0x7d, 88): "Looper Record/Playback/Overdub",
        NonEffectKey(0x7d, 89): "Looper Stop",
        NonEffectKey(0x7d, 90): "Looper Trigger",
        NonEffectKey(0x7d, 91): "Looper Reverse",
        NonEffectKey(0x7d, 92): "Looper Half Speed",
        NonEffectKey(0x7d, 93): "Looper Cancel/Reactivate Overdub",
        NonEffectKey(0x7d, 94): "Looper Erase Loop",
        NonEffectKey(0x7d, 107): "Module A Freeze",
        NonEffectKey(0x7d, 108): "Module B Freeze",
        NonEffectKey(0x7d, 109): "Module C Freeze",
        NonEffectKey(0x7d, 110): "Module D Freeze",
        NonEffectKey(0x7d, 111): "Module X Freeze",
        NonEffectKey(0x7d, 113): "Module MOD Freeze",
        NonEffectKey(0x7d, 114): "Module DLY Freeze",
        NonEffectKey(0x7d, 115): "Module REV Freeze",
        NonEffectKey(0x7f, 0): "Main Output Volume",
        NonEffectKey(0x7f, 1): "Headphone Output Volume",
        NonEffectKey(0x7f, 2): "Monitor Output Volume",
        NonEffectKey(0x7f, 3): "Direct Output / Send 1 Volume",
        NonEffectKey(0x7f, 4): "S/PDIF Output Volume",
        NonEffectKey(0x7f, 8): "Monitor Cab. Off",
        NonEffectKey(0x7f, 12): "Main Output EQ Bass",
        NonEffectKey(0x7f, 13): "Main Output EQ Middle",
        NonEffectKey(0x7f, 14): "Main Output EQ Treble",
        NonEffectKey(0x7f, 15): "Main Output EQ Presence",
        NonEffectKey(0x7f, 16): "Output Filter Low Cut",
        NonEffectKey(0x7f, 17): "Monitor Output EQ Bass",
        NonEffectKey(0x7f, 18): "Monitor Output EQ Middle",
        NonEffectKey(0x7f, 19): "Monitor Output EQ Treble",
        NonEffectKey(0x7f, 20): "Monitor Output EQ Presence",
        NonEffectKey(0x7f, 21): "Output Filter High Cut",
        NonEffectKey(0x7f, 32): "Aux In >Main",
        NonEffectKey(0x7f, 33): "Aux In >Monitor",
        NonEffectKey(0x7f, 34): "Aux In >Headphone",
        NonEffectKey(0x7f, 36): "Space Intensity",
        NonEffectKey(0x7f, 37): "Space Routing",
        NonEffectKey(0x7f, 38): "Kone Mode",
        NonEffectKey(0x7f, 39): "Kone Bass Boost",
        NonEffectKey(0x7f, 40): "Kone Imprint Select",
        NonEffectKey(0x7f, 41): "Kone Directivity",
        NonEffectKey(0x7f, 42): "Kone Sweetening",
        NonEffectKey(0x7f, 44): "Input Source",
        NonEffectKey(0x7f, 50): "Pure Cabinet Enable",
        NonEffectKey(0x7f, 51): "Pure Cabinet Level (Global)",
        NonEffectKey(0x7f, 52): "Looper Volume",
        NonEffectKey(0x7f, 53): "Looper Location",
        NonEffectKey(0x7f, 59): "Aux >Mono",
        NonEffectKey(0x7f, 126): "Tuner Mode State",
        NonEffectKey(0x96, 0): "Bank Rig Name",
        NonEffectKey(0x96, 1): "Bank Rig Name",
        NonEffectKey(0x96, 2): "Bank Rig Name",
        NonEffectKey(0x96, 3): "Bank Rig Name",
        NonEffectKey(0x96, 4): "Bank Rig Name",
        NonEffectKey(0x96, 5): "Bank Amp Name",
        NonEffectKey(0x96, 6): "Bank Amp Name",
        NonEffectKey(0x96, 7): "Bank Amp Name",
        NonEffectKey(0x96, 8): "Bank Amp Name",
        NonEffectKey(0x96, 9): "Bank Amp Name",
        NonEffectKey(0x96, 10): "Bank Cabinet Name",
        NonEffectKey(0x96, 11): "Bank Cabinet Name",
        NonEffectKey(0x96, 12): "Bank Cabinet Name",
        NonEffectKey(0x96, 13): "Bank Cabinet Name",
        NonEffectKey(0x96, 14): "Bank Cabinet Name",
    ]
    public static let stringTags: [UInt8: String] = [1: "Rig Name", 2: "Rig Author", 3: "Rig Creation Date", 4: "Rig Comment", 10: "Amp Name", 11: "Amp Author", 14: "Amp Location", 15: "Amp Manufacturer", 16: "Amp Comment", 18: "Amp Model", 19: "Amp Channel", 20: "Pickup Type", 21: "Year of Production", 32: "Cabinet Name", 33: "Cabinet Author", 36: "Cabinet Location", 37: "Cabinet Manufacturer", 38: "Microphone Model", 39: "Cabinet Comment", 40: "Microphone Position", 41: "Speaker Configuration", 42: "Cabinet Model", 44: "Speaker Manufacturer", 45: "Speaker Model"]
    public static let page0Numeric: [UInt8: String] = [0x50: "Morph Button", 0x77: "Morph Position"]
    public static let effectTypes: [UInt16: String] = [
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
    ]
    public static let effectCategories: [EffectCategory] = [
        EffectCategory(min: 1, max: 16, name: "Wah"),
        EffectCategory(min: 17, max: 31, name: "Shaper"),
        EffectCategory(min: 32, max: 48, name: "Distortion"),
        EffectCategory(min: 49, max: 63, name: "Dynamics"),
        EffectCategory(min: 64, max: 79, name: "Modulation"),
        EffectCategory(min: 80, max: 95, name: "Phaser & Flanger"),
        EffectCategory(min: 96, max: 111, name: "Equalizer"),
        EffectCategory(min: 112, max: 120, name: "Booster"),
        EffectCategory(min: 121, max: 127, name: "Effect Loop"),
        EffectCategory(min: 128, max: 143, name: "Pitch"),
        EffectCategory(min: 144, max: 175, name: "Delay"),
        EffectCategory(min: 176, max: 207, name: "Reverb"),
    ]
    public static let slotEnableCc: [String: UInt8] = ["A": 17, "B": 18, "C": 19, "D": 20, "X": 22, "MOD": 24, "DLY": 27, "REV": 29]
    public static let meterFields: [MeterField] = [
        MeterField(index: 0, number: 78, id: "strobe_seg_low", name: "Tuner Strobe Segment (phase-low)", render: "strobe"),
        MeterField(index: 1, number: 79, id: "strobe_seg_mid", name: "Tuner Strobe Segment (phase-mid)", render: "strobe"),
        MeterField(index: 2, number: 80, id: "strobe_seg_high", name: "Tuner Strobe Segment (phase-high)", render: "strobe"),
        MeterField(index: 3, number: 81, id: "strobe_phase", name: "Tuner Strobe Phase", render: "strobe"),
        MeterField(index: 4, number: 82, id: "stack_level", name: "Stack Level (pre-rig-volume)", render: "bar"),
        MeterField(index: 5, number: 83, id: "stack_power", name: "Stack Power", render: "extra"),
        MeterField(index: 6, number: 84, id: "rig_out_level", name: "Rig Output Level (post-rig-volume)", render: "bar"),
        MeterField(index: 7, number: 85, id: "rig_out_power", name: "Rig Output Power", render: "extra"),
        MeterField(index: 8, number: 86, id: "unused_v8", name: "(unused)", render: "extra"),
        MeterField(index: 9, number: 87, id: "loudness", name: "Loudness (slow RMS)", render: "bar"),
        MeterField(index: 10, number: 88, id: "unused_v10", name: "(unused)", render: "extra"),
    ]
    /// The state routing table, sorted by address (spec/state.toml).
    public static let stateRoutes: [Route] = [
        Route(address: 1, field: .rigName, slot: nil, kind: .text, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 2, field: .rigAuthor, slot: nil, kind: .text, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 3, field: .rigDate, slot: nil, kind: .text, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 4, field: .rigComment, slot: nil, kind: .text, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 16, field: .ampName, slot: nil, kind: .text, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 32, field: .cabinetName, slot: nil, kind: .text, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 80, field: .morphButton, slot: nil, kind: .bool, lane: .fast, wire: .stream, dedupe: false, request: false),
        Route(address: 119, field: .morphPosition, slot: nil, kind: .u14, lane: .slow, wire: .control, dedupe: true, request: false),
        Route(address: 512, field: .tempoBpm, slot: nil, kind: .bpm, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 513, field: .rigVolume, slot: nil, kind: .u14, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 1282, field: .ampOn, slot: nil, kind: .bool, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 1284, field: .ampGain, slot: nil, kind: .u14, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 1538, field: .cabinetOn, slot: nil, kind: .bool, lane: .slow, wire: .both, dedupe: true, request: false),
        Route(address: 6400, field: .effectType, slot: 0, kind: .u14, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 6403, field: .effectOn, slot: 0, kind: .bool, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 6404, field: .effectMix, slot: 0, kind: .u14, lane: .slow, wire: .both, dedupe: true, request: false),
        Route(address: 6528, field: .effectType, slot: 1, kind: .u14, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 6531, field: .effectOn, slot: 1, kind: .bool, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 6532, field: .effectMix, slot: 1, kind: .u14, lane: .slow, wire: .both, dedupe: true, request: false),
        Route(address: 6656, field: .effectType, slot: 2, kind: .u14, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 6659, field: .effectOn, slot: 2, kind: .bool, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 6660, field: .effectMix, slot: 2, kind: .u14, lane: .slow, wire: .both, dedupe: true, request: false),
        Route(address: 6784, field: .effectType, slot: 3, kind: .u14, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 6787, field: .effectOn, slot: 3, kind: .bool, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 6788, field: .effectMix, slot: 3, kind: .u14, lane: .slow, wire: .both, dedupe: true, request: false),
        Route(address: 7168, field: .effectType, slot: 4, kind: .u14, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 7171, field: .effectOn, slot: 4, kind: .bool, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 7172, field: .effectMix, slot: 4, kind: .u14, lane: .slow, wire: .both, dedupe: true, request: false),
        Route(address: 7424, field: .effectType, slot: 5, kind: .u14, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 7427, field: .effectOn, slot: 5, kind: .bool, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 7428, field: .effectMix, slot: 5, kind: .u14, lane: .slow, wire: .both, dedupe: true, request: false),
        Route(address: 7680, field: .effectType, slot: 6, kind: .u14, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 7683, field: .effectOn, slot: 6, kind: .bool, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 7684, field: .effectMix, slot: 6, kind: .u14, lane: .slow, wire: .both, dedupe: true, request: false),
        Route(address: 7808, field: .effectType, slot: 7, kind: .u14, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 7811, field: .effectOn, slot: 7, kind: .bool, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 7812, field: .effectMix, slot: 7, kind: .u14, lane: .slow, wire: .both, dedupe: true, request: false),
        Route(address: 15872, field: .beatPulse, slot: nil, kind: .bool, lane: .fast, wire: .stream, dedupe: false, request: false),
        Route(address: 15887, field: .tunerDeviance, slot: nil, kind: .u14, lane: .fast, wire: .stream, dedupe: true, request: false),
        Route(address: 15950, field: .status, slot: 0, kind: .multi, lane: .fast, wire: .stream, dedupe: false, request: false),
        Route(address: 15951, field: .status, slot: 1, kind: .multi, lane: .fast, wire: .stream, dedupe: false, request: false),
        Route(address: 15952, field: .status, slot: 2, kind: .multi, lane: .fast, wire: .stream, dedupe: false, request: false),
        Route(address: 15953, field: .status, slot: 3, kind: .multi, lane: .fast, wire: .stream, dedupe: false, request: false),
        Route(address: 15954, field: .status, slot: 4, kind: .multi, lane: .fast, wire: .stream, dedupe: false, request: false),
        Route(address: 15955, field: .status, slot: 5, kind: .multi, lane: .fast, wire: .stream, dedupe: false, request: false),
        Route(address: 15956, field: .status, slot: 6, kind: .multi, lane: .fast, wire: .stream, dedupe: false, request: false),
        Route(address: 15957, field: .status, slot: 7, kind: .multi, lane: .fast, wire: .stream, dedupe: false, request: false),
        Route(address: 15958, field: .status, slot: 8, kind: .multi, lane: .fast, wire: .stream, dedupe: false, request: false),
        Route(address: 15959, field: .status, slot: 9, kind: .multi, lane: .fast, wire: .stream, dedupe: false, request: false),
        Route(address: 15960, field: .status, slot: 10, kind: .multi, lane: .fast, wire: .stream, dedupe: false, request: false),
        Route(address: 16084, field: .tunerNote, slot: nil, kind: .u7, lane: .slow, wire: .stream, dedupe: true, request: false),
        Route(address: 16256, field: .mainVolume, slot: nil, kind: .u14, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 16257, field: .headphoneVolume, slot: nil, kind: .u14, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 16258, field: .monitorVolume, slot: nil, kind: .u14, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 19200, field: .bankRigName, slot: 0, kind: .text, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 19201, field: .bankRigName, slot: 1, kind: .text, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 19202, field: .bankRigName, slot: 2, kind: .text, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 19203, field: .bankRigName, slot: 3, kind: .text, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 19204, field: .bankRigName, slot: 4, kind: .text, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 19205, field: .bankAmpName, slot: 0, kind: .text, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 19206, field: .bankAmpName, slot: 1, kind: .text, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 19207, field: .bankAmpName, slot: 2, kind: .text, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 19208, field: .bankAmpName, slot: 3, kind: .text, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 19209, field: .bankAmpName, slot: 4, kind: .text, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 19210, field: .bankCabinetName, slot: 0, kind: .text, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 19211, field: .bankCabinetName, slot: 1, kind: .text, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 19212, field: .bankCabinetName, slot: 2, kind: .text, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 19213, field: .bankCabinetName, slot: 3, kind: .text, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 19214, field: .bankCabinetName, slot: 4, kind: .text, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 100701, field: .currentBank, slot: nil, kind: .u16, lane: .slow, wire: .both, dedupe: true, request: true),
        Route(address: 100702, field: .currentRigSlot, slot: nil, kind: .u16, lane: .slow, wire: .both, dedupe: true, request: true),
    ]
}

public struct NonEffectKey: Hashable {
    public let page: UInt8
    public let number: UInt8
    public init(_ page: UInt8, _ number: UInt8) { self.page = page; self.number = number }
}

public struct MeterField {
    public let index: Int
    public let number: UInt8
    public let id: String
    public let name: String
    public let render: String
}

public struct EffectCategory {
    public let min: UInt16
    public let max: UInt16
    public let name: String
}

/// One row of the state routing table: a flat address and how the tree folds it.
public struct Route: Hashable, Sendable {
    /// A field of the device-state tree that a routed address writes (spec/state.toml).
    public enum Field: String, CaseIterable, Hashable, Sendable {
        case rigName = "rig_name"
        case rigAuthor = "rig_author"
        case rigDate = "rig_date"
        case rigComment = "rig_comment"
        case ampName = "amp_name"
        case cabinetName = "cabinet_name"
        case morphButton = "morph_button"
        case morphPosition = "morph_position"
        case tempoBpm = "tempo_bpm"
        case rigVolume = "rig_volume"
        case ampOn = "amp_on"
        case ampGain = "amp_gain"
        case cabinetOn = "cabinet_on"
        case effectType = "effect_type"
        case effectOn = "effect_on"
        case effectMix = "effect_mix"
        case beatPulse = "beat_pulse"
        case tunerDeviance = "tuner_deviance"
        case status = "status"
        case tunerNote = "tuner_note"
        case mainVolume = "main_volume"
        case headphoneVolume = "headphone_volume"
        case monitorVolume = "monitor_volume"
        case bankRigName = "bank_rig_name"
        case bankAmpName = "bank_amp_name"
        case bankCabinetName = "bank_cabinet_name"
        case currentBank = "current_bank"
        case currentRigSlot = "current_rig_slot"
    }

    /// How a routed value decodes before it is stored.
    public enum Kind: String, CaseIterable, Hashable, Sendable {
        case u14 = "u14"
        case u16 = "u16"
        case u7 = "u7"
        case bool = "bool"
        case text = "text"
        case bpm = "bpm"
        case multi = "multi"
    }

    /// Which update lane a route feeds: FAST (event only) or SLOW (snapshot).
    public enum Lane: String, CaseIterable, Hashable, Sendable {
        case fast = "fast"
        case slow = "slow"
    }

    /// Which channel may write a route: the MIDI3 stream, the CBOR control channel, or both.
    public enum Wire: String, CaseIterable, Hashable, Sendable {
        case stream = "stream"
        case control = "control"
        case both = "both"
    }

    public let address: UInt32
    public let field: Field
    /// The per-slot index for expanded rows: effect slot, bank-preview slot, or
    /// element index within a spanned block.
    public let slot: UInt8?
    public let kind: Kind
    public let lane: Lane
    public let wire: Wire
    public let dedupe: Bool
    public let request: Bool
    public init(
        address: UInt32, field: Field, slot: UInt8?, kind: Kind, lane: Lane, wire: Wire,
        dedupe: Bool, request: Bool
    ) {
        self.address = address; self.field = field; self.slot = slot; self.kind = kind
        self.lane = lane; self.wire = wire; self.dedupe = dedupe; self.request = request
    }
}

