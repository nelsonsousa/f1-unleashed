/**
 * TV-signal capture + Sync.
 *
 * Phase 1 of the audio/video sync feature:
 *   1. "TV signal" button picks an audio input device (= BlackHole-2ch)
 *      and starts capturing its PCM into a rolling buffer.
 *   2. "Sync" button uploads the latest 10 s window of captured audio to
 *      the backend, which cross-correlates it against the in-playback
 *      commentary.aac and returns the time delta between data clock and
 *      TV broadcast.
 *
 *   The data clock typically RUNS AHEAD of the TV (= broadcast lag).
 *   Sync rewinds the data playback to wait for the TV stream.
 *   If the TV is ahead (= delta < 0), we don't fast-forward — the user
 *   is warned to pause the TV manually.
 *
 *   Browser-side does only the capture + chunk upload; the heavy FFT
 *   correlation runs server-side in `audio_sync.py`.
 */

(function () {
    const CAPTURE_RATE = 8000;          // server expects 8 kHz mono int16
    const PROBE_DURATION_S = 10;        // upload last N seconds
    const RING_DURATION_S = 30;         // keep up to this much in memory

    const state = {
        audioCtx: null,
        mediaStream: null,
        sourceNode: null,
        workletNode: null,
        deviceId: null,
        ringBuffer: null,               // Float32Array, length = RING_DURATION_S * CAPTURE_RATE
        ringWritePos: 0,
        ringSamplesWritten: 0,
        active: false,
    };

    // ── Status / UI plumbing ──

    function setLight(cls) {
        const light = document.getElementById('tvSignalLight');
        if (!light) return;
        light.classList.remove('live', 'error');
        if (cls) light.classList.add(cls);
    }

    function setStatus(text, cls) {
        const span = document.getElementById('syncStatus');
        if (!span) return;
        span.textContent = text || '';
        span.classList.remove('warn', 'ok', 'error');
        if (cls) span.classList.add(cls);
    }

    function setSyncBtnEnabled(enabled) {
        const btn = document.getElementById('syncBtn');
        if (btn) btn.disabled = !enabled;
    }

    // ── Audio capture ──

    async function listAudioInputs() {
        // Trigger a permission prompt so device labels are populated
        // (Firefox suppresses labels until the user grants mic access).
        try {
            const tmp = await navigator.mediaDevices.getUserMedia({ audio: true });
            tmp.getTracks().forEach(t => t.stop());
        } catch (e) {
            throw new Error('Microphone permission denied');
        }
        const devices = await navigator.mediaDevices.enumerateDevices();
        return devices.filter(d => d.kind === 'audioinput');
    }

    async function pickDeviceSimple(inputs) {
        // Phase 1: a one-shot prompt() with the device list. Phase 3 can
        // upgrade to a styled picker overlay.
        if (!inputs.length) return null;
        const lines = inputs.map((d, i) => `${i + 1}. ${d.label || 'Unknown'}`).join('\n');
        const choice = window.prompt(
            'Pick an audio input for TV signal:\n\n' + lines + '\n\nEnter the number:',
            String(inputs.findIndex(d => /blackhole/i.test(d.label)) + 1 || 1),
        );
        if (!choice) return null;
        const idx = parseInt(choice, 10) - 1;
        if (idx < 0 || idx >= inputs.length) return null;
        return inputs[idx];
    }

    async function startCapture(deviceId) {
        // 1 channel @ CAPTURE_RATE is enough for cross-correlation.
        state.audioCtx = new (window.AudioContext || window.webkitAudioContext)({
            sampleRate: CAPTURE_RATE,
        });
        state.mediaStream = await navigator.mediaDevices.getUserMedia({
            audio: {
                deviceId: { exact: deviceId },
                channelCount: 1,
                echoCancellation: false,
                noiseSuppression: false,
                autoGainControl: false,
            },
        });
        state.sourceNode = state.audioCtx.createMediaStreamSource(state.mediaStream);

        // Allocate the rolling buffer.
        const ringLen = RING_DURATION_S * CAPTURE_RATE;
        state.ringBuffer = new Float32Array(ringLen);
        state.ringWritePos = 0;
        state.ringSamplesWritten = 0;

        // ScriptProcessorNode is deprecated but its replacement
        // (AudioWorklet) needs a separate file. ScriptProcessor still
        // ships in every browser, and we only need ~8 k samples/s.
        const bufSize = 1024;
        state.workletNode = state.audioCtx.createScriptProcessor(bufSize, 1, 1);
        state.workletNode.onaudioprocess = function (e) {
            const input = e.inputBuffer.getChannelData(0);
            const ring = state.ringBuffer;
            let pos = state.ringWritePos;
            for (let i = 0; i < input.length; i++) {
                ring[pos++] = input[i];
                if (pos >= ring.length) pos = 0;
            }
            state.ringWritePos = pos;
            state.ringSamplesWritten = Math.min(
                state.ringSamplesWritten + input.length,
                ring.length,
            );
        };
        state.sourceNode.connect(state.workletNode);
        // The worklet must connect to destination for onaudioprocess to fire.
        // We mute it via a zero-gain so we don't echo TV audio back out.
        const mute = state.audioCtx.createGain();
        mute.gain.value = 0;
        state.workletNode.connect(mute);
        mute.connect(state.audioCtx.destination);

        state.active = true;
        state.deviceId = deviceId;
    }

    function stopCapture() {
        if (state.workletNode) { state.workletNode.disconnect(); state.workletNode = null; }
        if (state.sourceNode)  { state.sourceNode.disconnect();  state.sourceNode = null;  }
        if (state.mediaStream) {
            state.mediaStream.getTracks().forEach(t => t.stop());
            state.mediaStream = null;
        }
        if (state.audioCtx)    { state.audioCtx.close();         state.audioCtx = null;    }
        state.ringBuffer = null;
        state.active = false;
    }

    function readLatestPCM(seconds) {
        // Return the last N seconds of captured PCM as an Int16Array.
        if (!state.ringBuffer) return null;
        const want = seconds * CAPTURE_RATE;
        if (state.ringSamplesWritten < want) return null;
        const ring = state.ringBuffer;
        const out = new Int16Array(want);
        // Read from (ringWritePos - want), wrapping.
        let start = state.ringWritePos - want;
        if (start < 0) start += ring.length;
        for (let i = 0; i < want; i++) {
            const v = ring[(start + i) % ring.length];
            // Clamp + convert float32 [-1, 1] → int16.
            const s = Math.max(-1, Math.min(1, v));
            out[i] = (s < 0 ? s * 0x8000 : s * 0x7FFF) | 0;
        }
        return out;
    }

    // ── Sync trigger ──

    async function triggerSync() {
        if (!state.active) {
            setStatus('Start TV signal first', 'warn');
            return;
        }
        const pcm = readLatestPCM(PROBE_DURATION_S);
        if (!pcm) {
            setStatus('Need ' + PROBE_DURATION_S + 's of audio', 'warn');
            return;
        }
        const sessionId = (window.SESSION_CONFIG || {}).sessionId
            || (window.SESSION_CONFIG || {}).sessionKey
            || null;
        if (!sessionId) {
            setStatus('No session id', 'error');
            return;
        }
        // getCurrentOffset() returns SECONDS (= elapsed from session start).
        // seekToOffset() takes SECONDS too. We send ms to the server for
        // backend convenience but stay in seconds on the client.
        const dataClockSec = messageBus.getCurrentOffset
            ? messageBus.getCurrentOffset()
            : 0;
        const audioEl = window.f1audioElement || null;
        const audioCurrentSec = (audioEl && !isNaN(audioEl.currentTime))
            ? audioEl.currentTime : null;
        console.log('[tv_sync] audio.currentTime =', audioCurrentSec,
                    'dataClock(s) =', dataClockSec);
        setStatus('Matching…');

        try {
            const url = `/api/v1/livetiming/audio-sync-probe/${encodeURIComponent(sessionId)}`
                + `?data_offset_ms=${Math.round(dataClockSec * 1000)}`
                + `&sample_rate=${CAPTURE_RATE}`
                + (audioCurrentSec !== null ? `&audio_current_s=${audioCurrentSec.toFixed(3)}` : '');
            const resp = await fetch(
                url,
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/octet-stream' },
                    body: pcm.buffer,
                },
            );
            if (!resp.ok) {
                const txt = await resp.text().catch(() => '');
                setStatus('Probe failed: ' + (txt || resp.status), 'error');
                return;
            }
            const result = await resp.json();
            applySync(result, dataClockSec);
        } catch (e) {
            setStatus('Probe error: ' + e.message, 'error');
        }
    }

    function applySync(result, dataClockSec) {
        // result.delta_ms > 0 → audio is AHEAD of TV → shift audio later.
        // result.delta_ms < 0 → audio is BEHIND TV → shift audio earlier.
        // Audio-only adjustment: the data clock is untouched. Future
        // phases will sync the data clock independently via OCR of the
        // TV stream.
        const conf = result.confidence || 0;
        const deltaMs = result.delta_ms;
        // Surface backend diagnostics to the console for tuning.
        console.log('[tv_sync] sync result:', result);
        if (deltaMs === null || deltaMs === undefined) {
            setStatus(`No match (conf=${conf.toFixed(1)})`, 'error');
            return;
        }
        if (conf < 3.0) {
            const dx = result.diagnostics || {};
            const tgt = result.target_combined_s;
            const mat = result.matched_combined_s;
            setStatus(
                `Low conf ${conf.toFixed(1)} | rms=${dx.probe_rms} mean=${dx.probe_mean} std=${dx.probe_std} `
                + `ref=${dx.ref_window_s}s peak=${dx.corr_peak} base=${dx.corr_baseline} `
                + `tgt=${tgt}s mat=${mat}s`,
                'warn',
            );
            return;
        }
        const deltaSec = deltaMs / 1000;
        console.log('[tv_sync] adjustAudioOffset', deltaSec,
                    '(audio shifts', deltaSec > 0 ? 'later' : 'earlier',
                    'by', Math.abs(deltaSec).toFixed(2), 's)');
        if (typeof window.adjustAudioOffset !== 'function') {
            setStatus('Audio offset API missing', 'error');
            return;
        }
        window.adjustAudioOffset(deltaSec);
        const sign = deltaSec >= 0 ? '−' : '+';
        const mag = Math.abs(deltaSec).toFixed(2);
        setStatus(`Audio ${sign}${mag}s (conf ${conf.toFixed(1)})`, 'ok');
    }

    // ── Button handlers (exported via window) ──

    window.toggleTvSignal = async function toggleTvSignal() {
        if (state.active) {
            stopCapture();
            setLight(null);
            setStatus('');
            setSyncBtnEnabled(false);
            return;
        }
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            setStatus('No media APIs', 'error');
            setLight('error');
            return;
        }
        setStatus('Requesting…');
        try {
            const inputs = await listAudioInputs();
            const pick = await pickDeviceSimple(inputs);
            if (!pick) {
                setStatus('Cancelled');
                return;
            }
            await startCapture(pick.deviceId);
            setLight('live');
            setStatus(`Capturing: ${pick.label || 'audio'}`, 'ok');
            setSyncBtnEnabled(true);
        } catch (e) {
            stopCapture();
            setLight('error');
            setStatus(e.message || 'Capture failed', 'error');
        }
    };

    window.triggerSync = triggerSync;

    // Initialise button states.
    setSyncBtnEnabled(false);
})();
