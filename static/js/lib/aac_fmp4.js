/**
 * Minimal ADTS-AAC → fragmented-MP4 remuxer (Route A client transmux).
 *
 * MSE won't accept raw ADTS AAC (`commentary.aac`), only fragmented MP4
 * (`audio/mp4; codecs="mp4a.40.2"`). This parses ADTS frames from a byte
 * stream (fed incrementally from HTTP Range fetches) and emits:
 *   - one INIT segment (ftyp + moov) on the first frame, and
 *   - a MEDIA segment (moof + mdat) per `append()` batch,
 * which append straight into an MSE SourceBuffer. It's a REMUX (container
 * rewrite), not a re-encode — the AAC payloads are copied verbatim.
 *
 * Audio-only, AAC-LC. Works in the browser (window.AacFmp4) and in Node
 * (module.exports) so the muxer can be validated against ffmpeg offline.
 */
(function (root) {
    'use strict';

    // ADTS sampling_frequency_index → Hz.
    var RATES = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050,
                 16000, 12000, 11025, 8000, 7350];
    var SAMPLES_PER_FRAME = 1024;

    // ── byte helpers ──────────────────────────────────────────────────
    function u16(n) { return [(n >> 8) & 0xff, n & 0xff]; }
    function u32(n) { return [(n >>> 24) & 0xff, (n >>> 16) & 0xff, (n >>> 8) & 0xff, n & 0xff]; }
    function str(s) { var a = []; for (var i = 0; i < s.length; i++) a.push(s.charCodeAt(i)); return a; }
    function concat(arrays) {
        var len = 0, i;
        for (i = 0; i < arrays.length; i++) len += arrays[i].length;
        var out = new Uint8Array(len), off = 0;
        for (i = 0; i < arrays.length; i++) { out.set(arrays[i], off); off += arrays[i].length; }
        return out;
    }
    // A box = size(4) + type(4) + payload. payloads are plain arrays / Uint8Arrays.
    function box(type) {
        var payload = [], i;
        for (i = 1; i < arguments.length; i++) {
            var p = arguments[i];
            for (var j = 0; j < p.length; j++) payload.push(p[j]);
        }
        var size = 8 + payload.length;
        return u32(size).concat(str(type)).concat(payload);
    }

    // ── init segment (ftyp + moov) ────────────────────────────────────
    function audioSpecificConfig(aot, freqIndex, channels) {
        // 5 bits AOT, 4 bits freqIndex, 4 bits channelConfig, 3 bits GASpecificConfig(0).
        var b0 = (aot << 3) | (freqIndex >> 1);
        var b1 = ((freqIndex & 1) << 7) | (channels << 3);
        return [b0 & 0xff, b1 & 0xff];
    }
    function descriptor(tag, payload) {
        // ISO descriptor: tag, length (single byte — our descriptors are tiny), payload.
        return [tag, payload.length].concat(payload);
    }
    function esdsBox(cfg) {
        var asc = audioSpecificConfig(cfg.aot, cfg.freqIndex, cfg.channels);
        var dsi = descriptor(0x05, asc);                       // DecoderSpecificInfo
        var dcd = descriptor(0x04,
            [0x40,                                              // objectTypeIndication = AAC
             0x15]                                              // streamType=audio(5)<<2 | upstream(0)<<1 | reserved(1)
            .concat([0x00, 0x00, 0x00])                         // bufferSizeDB
            .concat(u32(0))                                     // maxBitrate
            .concat(u32(0))                                     // avgBitrate
            .concat(dsi));
        var sl = descriptor(0x06, [0x02]);                     // SLConfigDescriptor
        var es = descriptor(0x03, u16(0).concat([0x00]).concat(dcd).concat(sl)); // ES_Descriptor
        return box('esds', u32(0), es);                        // version+flags, then ES_Descriptor
    }
    function mp4aBox(cfg) {
        var inner = [0, 0, 0, 0, 0, 0]                          // reserved (6)
            .concat(u16(1))                                    // data_reference_index
            .concat([0, 0, 0, 0, 0, 0, 0, 0])                  // reserved (8)
            .concat(u16(cfg.channels))                         // channelcount
            .concat(u16(16))                                   // samplesize
            .concat([0, 0, 0, 0])                              // pre_defined + reserved
            .concat(u16(cfg.sampleRate))                       // samplerate (upper 16 of 16.16)
            .concat([0, 0]);
        return box('mp4a', inner, esdsBox(cfg));
    }
    function stblBox(cfg) {
        var stsd = box('stsd', u32(0), u32(1), mp4aBox(cfg));
        var stts = box('stts', u32(0), u32(0));
        var stsc = box('stsc', u32(0), u32(0));
        var stsz = box('stsz', u32(0), u32(0), u32(0));
        var stco = box('stco', u32(0), u32(0));
        return box('stbl', stsd, stts, stsc, stsz, stco);
    }
    function buildInit(cfg) {
        var ts = cfg.sampleRate;
        var ftyp = box('ftyp', str('isom'), u32(1), str('isom'), str('iso5'), str('dash'));
        var mvhd = box('mvhd', u32(0), u32(0), u32(0), u32(ts), u32(0),
            u32(0x00010000), u16(0x0100), u16(0),
            u32(0), u32(0),
            u32(0x00010000), u32(0), u32(0),
            u32(0), u32(0x00010000), u32(0),
            u32(0), u32(0), u32(0x40000000),
            u32(0), u32(0), u32(0), u32(0), u32(0), u32(0),
            u32(2));                                            // next_track_ID
        var tkhd = box('tkhd', [0, 0, 0, 7], u32(0), u32(0), u32(1), u32(0), u32(0),
            u32(0), u32(0), u16(0), u16(0), u16(0x0100), u16(0),
            u32(0x00010000), u32(0), u32(0),
            u32(0), u32(0x00010000), u32(0),
            u32(0), u32(0), u32(0x40000000),
            u32(0), u32(0));                                    // width, height
        var mdhd = box('mdhd', u32(0), u32(0), u32(0), u32(ts), u32(0), u16(0x55c4), u16(0));
        var hdlr = box('hdlr', u32(0), u32(0), str('soun'), u32(0), u32(0), u32(0), str('SoundHandler\0'));
        var smhd = box('smhd', u32(0), u32(0));
        var dref = box('dref', u32(0), u32(1), box('url ', [0, 0, 0, 1]));
        var dinf = box('dinf', dref);
        var minf = box('minf', smhd, dinf, stblBox(cfg));
        var mdia = box('mdia', mdhd, hdlr, minf);
        var trak = box('trak', tkhd, mdia);
        var trex = box('trex', u32(0), u32(1), u32(1), u32(0), u32(0), u32(0));
        var mvex = box('mvex', trex);
        var moov = box('moov', mvhd, trak, mvex);
        return new Uint8Array(ftyp.concat(moov));
    }

    // ── media segment (moof + mdat) ───────────────────────────────────
    function buildSegment(cfg, seq, baseSamples, frames) {
        var i;
        var mfhd = box('mfhd', u32(0), u32(seq));
        var tfhd = box('tfhd', [0, 0x02, 0x00, 0x00], u32(1));   // flags 0x020000 default-base-is-moof, track 1
        // tfdt v1 — 64-bit baseMediaDecodeTime (in timescale = sampleRate units).
        var hi = Math.floor(baseSamples / 0x100000000);
        var lo = baseSamples >>> 0;
        var tfdt = box('tfdt', [1, 0, 0, 0], u32(hi), u32(lo));

        // Per-sample [duration, size] pairs for trun (flags 0x000301 =
        // data-offset(0x1) + sample-duration(0x100) + sample-size(0x200)).
        var samples = [];
        for (i = 0; i < frames.length; i++) samples = samples.concat(u32(SAMPLES_PER_FRAME)).concat(u32(frames[i].length));

        // Sizes, so the trun data_offset (from moof start to mdat payload) is exact.
        // trun box = 8(hdr) + 4(ver/flags) + 4(count) + 4(data_offset) + samples.
        var trunSize = 8 + 4 + 4 + 4 + samples.length;
        var trafSize = 8 + tfhd.length + tfdt.length + trunSize;
        var moofSize = 8 + mfhd.length + trafSize;
        var dataOffset = moofSize + 8;                          // + mdat box header(8)

        var trun = box('trun', [0, 0x00, 0x03, 0x01], u32(frames.length), u32(dataOffset), samples);
        var traf = box('traf', tfhd, tfdt, trun);
        var moof = box('moof', mfhd, traf);
        var mdat = box('mdat', concat(frames));
        return new Uint8Array(moof.concat(mdat));
    }

    // ── ADTS parsing + public API ─────────────────────────────────────
    function create() {
        var cfg = null, init = null, baseSamples = 0, seq = 0;
        var leftover = new Uint8Array(0);

        function parseConfig(b, o) {
            var profile = (b[o + 2] >> 6) & 0x03;                 // 0=Main,1=LC,...
            var freqIndex = (b[o + 2] >> 2) & 0x0f;
            var channels = ((b[o + 2] & 0x01) << 2) | ((b[o + 3] >> 6) & 0x03);
            return { aot: profile + 1, freqIndex: freqIndex, channels: channels,
                     sampleRate: RATES[freqIndex] || 44100 };
        }

        return {
            /** Feed raw ADTS bytes; returns an array of fMP4 segments (Uint8Array)
             *  ready to appendBuffer — the init segment leads the first batch. */
            append: function (chunk) {
                var b = concat([leftover, chunk instanceof Uint8Array ? chunk : new Uint8Array(chunk)]);
                var frames = [], o = 0;
                while (o + 7 <= b.length) {
                    if (b[o] !== 0xff || (b[o + 1] & 0xf0) !== 0xf0) { o++; continue; }   // resync
                    var protectionAbsent = b[o + 1] & 0x01;
                    var headerLen = protectionAbsent ? 7 : 9;
                    var frameLen = ((b[o + 3] & 0x03) << 11) | (b[o + 4] << 3) | ((b[o + 5] >> 5) & 0x07);
                    if (frameLen < headerLen) { o++; continue; }                          // bogus → resync
                    if (o + frameLen > b.length) break;                                   // partial frame → keep for next feed
                    if (!cfg) { cfg = parseConfig(b, o); init = buildInit(cfg); }
                    frames.push(b.subarray(o + headerLen, o + frameLen));
                    o += frameLen;
                }
                leftover = b.subarray(o);
                var out = [];
                if (init) { out.push(init); init = null; }
                if (frames.length) {
                    out.push(buildSegment(cfg, seq++, baseSamples, frames));
                    baseSamples += frames.length * SAMPLES_PER_FRAME;
                }
                return out;
            },
            /** MSE mime for the SourceBuffer. */
            mime: 'audio/mp4; codecs="mp4a.40.2"',
            /** Whole frames consumed so far → seconds (for diagnostics). */
            seconds: function () { return cfg ? baseSamples / cfg.sampleRate : 0; },
            config: function () { return cfg; },
        };
    }

    var api = { create: create };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    if (root) root.AacFmp4 = api;
})(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : null));
