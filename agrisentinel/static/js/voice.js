"use strict";
(() => {
  const $ = (s) => document.querySelector(s);
  class FarmVoice {
    constructor({ask, state, error, requestSpeech}) {
      Object.assign(this, {ask, reportState: state, reportError: error, requestSpeech});
      this.active = false; this.epoch = 0; this.speechEpoch = 0;
      this.sources = new Set(); this.jobs = new Set(); this.queue = [];
      this.playTail = Promise.resolve(); this.ttsActive = 0;
      this.resetInput();
    }
    state(label) { if (this.active) this.reportState(label); }
    resetInput() { this.samples = []; this.started = 0; this.lastSound = 0; }
    async begin() {
      const epoch = ++this.epoch;
      this.active = true; this.muted = false; this.waiting = false;
      this.state("Connecting microphone…");
      this.draw();
      const Context = window.AudioContext || window.webkitAudioContext;
      try {
        if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia || !Context || !window.AudioWorkletNode) {
          throw new Error("Voice mode needs a supported browser over HTTPS or localhost.");
        }
        const context = new Context(); this.context = context;
        await context.resume();
        if (!context.audioWorklet) throw new Error("This browser does not support live audio capture. Use a current version of Chrome, Edge or Safari.");
        const media = navigator.mediaDevices.getUserMedia({audio: {echoCancellation: true, noiseSuppression: true, autoGainControl: true}, video: false});
        // Attach cleanup immediately, even if loading the worklet fails first.
        media.then((stream) => {
          if (!this.active || epoch !== this.epoch) stream.getTracks().forEach((track) => track.stop());
          else this.stream = stream;
        }, () => {});
        const [stream] = await Promise.all([media, context.audioWorklet.addModule("/static/js/voice-capture.js")]);
        if (!this.active || epoch !== this.epoch) { stream.getTracks().forEach((track) => track.stop()); return; }
        this.stream = stream;
        this.input = context.createMediaStreamSource(stream);
        this.micAnalyser = context.createAnalyser(); this.micAnalyser.fftSize = 256;
        this.outputAnalyser = context.createAnalyser(); this.outputAnalyser.fftSize = 256;
        this.outputAnalyser.connect(context.destination);
        this.capture = new AudioWorkletNode(context, "farm-pcm");
        this.silent = context.createGain(); this.silent.gain.value = 0;
        this.input.connect(this.micAnalyser);
        this.input.connect(this.capture); this.capture.connect(this.silent); this.silent.connect(context.destination);
        this.capture.port.onmessage = (event) => this.acceptSamples(event.data);
        this.resetInput(); this.state("Listening");
      } catch (error) {
        if (epoch !== this.epoch) return;
        this.end();
        this.reportError(error.name === "NotAllowedError" ? "Microphone access was denied. Allow access in your browser, or return to chat." : error.message || "The microphone could not be started.");
      }
    }
    acceptSamples(samples) {
      if (!this.active || this.muted || this.waiting || !this.context) return;
      const now = performance.now();
      const rms = Math.sqrt(samples.reduce((sum, value) => sum + value * value, 0) / samples.length);
      this.samples.push(samples);
      if (rms > 0.018) {
        if (!this.started) { this.started = now; this.state("Listening to you…"); }
        this.lastSound = now;
      }
      if (!this.started) { if (this.samples.length > 7) this.samples.shift(); return; }
      $("#voice-send").hidden = false;
      if ((now - this.lastSound > 650 && now - this.started > 400) || now - this.started > 20000) this.finishInput();
    }
    finishInput() {
      if (!this.started || this.waiting || !this.context) return;
      const total = this.samples.reduce((sum, block) => sum + block.length, 0);
      const pcm = new Float32Array(total); let offset = 0;
      for (const block of this.samples) { pcm.set(block, offset); offset += block.length; }
      const wav = FarmVoice.wav(pcm, this.context.sampleRate);
      this.resetInput(); this.waiting = true;
      $("#voice-send").hidden = true;
      this.state("Understanding your question…");
      this.ask(wav);
    }
    static wav(samples, rate) {
      const count = Math.floor(samples.length * 16000 / rate);
      const buffer = new ArrayBuffer(44 + count * 2), view = new DataView(buffer);
      const str = (at, text) => [...text].forEach((c, i) => view.setUint8(at + i, c.charCodeAt(0)));
      str(0, "RIFF"); view.setUint32(4, 36 + count * 2, true); str(8, "WAVE");
      str(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
      view.setUint32(24, 16000, true); view.setUint32(28, 32000, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
      str(36, "data"); view.setUint32(40, count * 2, true);
      // Average source samples over each output interval (anti-alias downsampling).
      for (let i = 0; i < count; i++) {
        const start = Math.floor(i * rate / 16000), end = Math.min(samples.length, Math.max(start + 1, Math.floor((i + 1) * rate / 16000)));
        let sum = 0; for (let j = start; j < end; j++) sum += samples[j];
        const sample = Math.max(-1, Math.min(1, sum / (end - start)));
        view.setInt16(44 + i * 2, sample * (sample < 0 ? 32768 : 32767), true);
      }
      return new Blob([buffer], {type: "audio/wav"});
    }
    toggleMic() {
      this.muted = !this.muted; this.resetInput();
      this.stream?.getTracks().forEach((track) => { track.enabled = !this.muted; });
      $("#voice-mic").setAttribute("aria-pressed", String(!this.muted));
      $("#voice-mic span").textContent = this.muted ? "Resume microphone" : "Pause microphone";
      $("#voice-send").hidden = true;
      if (!this.waiting) this.state(this.muted ? "Microphone paused" : "Listening");
    }
    startReply() {
      this.cancelSpeech(); this.waiting = true; this.resetInput(); this.audioError = null;
      this.audioEnd = this.context?.currentTime || 0;
      this.state("Thinking…");
    }
    enqueue(text, signal) {
      if (!this.active || !text.trim()) return;
      const epoch = this.speechEpoch;
      const job = {text, signal, epoch, chunks: [], done: false, wake: () => {}};
      this.jobs.add(job); this.queue.push(job); this.pump();
      this.playTail = this.playTail.then(() => this.play(job)).catch((error) => {
        if (epoch === this.speechEpoch && error.name !== "AbortError") this.audioError ||= error;
      });
    }
    pump() {
      while (this.queue.length && this.ttsActive < 2) {
        const job = this.queue.shift(); this.ttsActive++;
        this.prepare(job).finally(() => { this.ttsActive--; this.pump(); });
      }
    }
    async prepare(job) {
      let reader;
      try {
        if (job.signal.aborted || job.epoch !== this.speechEpoch) return;
        const response = await this.requestSpeech(job.text, job.signal);
        reader = response.body.getReader();
        while (job.epoch === this.speechEpoch && !job.signal.aborted) {
          const {value, done} = await reader.read();
          if (done) break;
          if (value.length) { job.chunks.push(value); job.wake(); }
        }
      } catch (error) { job.error = error; }
      finally { if (reader) { await reader.cancel().catch(() => {}); reader.releaseLock(); } job.done = true; job.wake(); }
    }
    async play(job) {
      let leftover;
      while (this.active && job.epoch === this.speechEpoch && !job.signal.aborted) {
        if (!job.chunks.length) {
          if (job.done) { if (job.error) throw job.error; break; }
          await new Promise((resolve) => { job.wake = resolve; }); continue;
        }
        let bytes = job.chunks.shift();
        if (leftover !== undefined) { const joined = new Uint8Array(bytes.length + 1); joined[0] = leftover; joined.set(bytes, 1); bytes = joined; leftover = undefined; }
        if (bytes.length % 2) { leftover = bytes[bytes.length - 1]; bytes = bytes.subarray(0, bytes.length - 1); }
        if (!bytes.length) continue;
        const context = this.context;
        const buffer = context.createBuffer(1, bytes.length / 2, 24000), pcm = buffer.getChannelData(0);
        const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
        for (let i = 0; i < pcm.length; i++) pcm[i] = view.getInt16(i * 2, true) / 32768;
        const source = context.createBufferSource(); source.buffer = buffer; source.connect(this.outputAnalyser);
        this.sources.add(source); source.onended = () => this.sources.delete(source);
        const at = Math.max(context.currentTime + 0.08, this.audioEnd);
        source.start(at); this.audioEnd = at + buffer.duration;
        this.state("Speaking");
      }
      this.jobs.delete(job);
    }
    async finishReply(signal) {
      const epoch = this.speechEpoch;
      await this.playTail;
      if (this.audioError) throw this.audioError;
      if (signal.aborted || epoch !== this.speechEpoch) return;
      const ms = Math.max(0, (this.audioEnd - this.context.currentTime) * 1000 + 100);
      await new Promise((resolve) => {
        const finish = () => { clearTimeout(timer); signal.removeEventListener("abort", finish); resolve(); };
        const timer = setTimeout(finish, ms); signal.addEventListener("abort", finish, {once: true});
      });
      if (!signal.aborted && epoch === this.speechEpoch) this.listen();
    }
    listen() { this.waiting = false; this.resetInput(); this.state(this.muted ? "Microphone paused" : "Listening"); }
    cancelSpeech() {
      this.speechEpoch++;
      for (const source of this.sources) { try { source.stop(); } catch (_) {} source.disconnect(); }
      this.sources.clear();
      for (const job of this.jobs) { job.done = true; job.wake(); }
      this.jobs.clear(); this.queue = []; this.playTail = Promise.resolve();
      this.audioEnd = this.context?.currentTime || 0;
    }
    draw() {
      const canvas = $("#voice-canvas"), painter = canvas.getContext("2d");
      if (!painter) return;
      const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      const data = new Uint8Array(128);
      const frame = () => {
        if (!this.active) return;
        const output = this.sources.size > 0;
        const analyser = output ? this.outputAnalyser : this.micAnalyser;
        data.fill(0); if (output || (!this.muted && !this.waiting)) analyser?.getByteFrequencyData(data);
        painter.clearRect(0, 0, 560, 560);
        painter.strokeStyle = output ? "#6b9fff" : "#d8e3da"; painter.lineWidth = 2;
        for (let ring = 0; ring < 3; ring++) {
          painter.globalAlpha = 1 - ring * 0.3; painter.beginPath();
          for (let i = 0; i <= 128; i++) {
            const angle = i / 128 * Math.PI * 2 - Math.PI / 2;
            const energy = data[i % 128] / 255;
            const radius = 150 + ring * 13 + energy * (reduced ? 7 : 44 - ring * 8);
            const x = 280 + Math.cos(angle) * radius, y = 280 + Math.sin(angle) * radius;
            if (!i) painter.moveTo(x, y); else painter.lineTo(x, y);
          }
          painter.closePath(); painter.stroke();
        }
        painter.globalAlpha = 1;
        this.animation = requestAnimationFrame(frame);
      };
      frame();
    }
    end() {
      this.active = false; this.epoch++; this.cancelSpeech(); this.resetInput();
      if (this.animation) cancelAnimationFrame(this.animation);
      this.stream?.getTracks().forEach((track) => track.stop()); this.stream = null;
      this.capture?.disconnect(); this.input?.disconnect(); this.silent?.disconnect();
      if (this.context && this.context.state !== "closed") this.context.close().catch(() => {});
      this.context = null;
    }
  }
  window.FarmVoice = FarmVoice;
})();
