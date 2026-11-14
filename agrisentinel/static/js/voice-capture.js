/* PCM capture runs on the audio thread; no MediaRecorder decoding round trip. */
class FarmPCM extends AudioWorkletProcessor {
  constructor() { super(); this.chunk = new Float32Array(2048); this.used = 0; }
  process(inputs) {
    const input = inputs[0]?.[0];
    if (!input) return true;
    for (const sample of input) {
      this.chunk[this.used++] = sample;
      if (this.used === this.chunk.length) {
        this.port.postMessage(this.chunk, [this.chunk.buffer]);
        this.chunk = new Float32Array(2048); this.used = 0;
      }
    }
    return true;
  }
}
registerProcessor("farm-pcm", FarmPCM);
