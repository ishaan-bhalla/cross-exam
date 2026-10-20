// Captures microphone audio as mono PCM16 at targetRate, posting chunks of chunkMs.
// If the AudioContext already runs at targetRate this only converts; otherwise each output
// sample is the mean of the input samples it covers (a simple low-pass before decimating).
class PcmCapture extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const { targetRate, chunkMs } = options.processorOptions;
    this.ratio = sampleRate / targetRate;
    this.size = Math.round((targetRate * chunkMs) / 1000);
    this.out = new Int16Array(this.size);
    this.n = 0;
    this.sum = 0;
    this.count = 0;
    this.acc = 0; // input samples still owed to the current output sample
  }

  push(value) {
    const v = Math.max(-1, Math.min(1, value));
    this.out[this.n++] = v < 0 ? v * 0x8000 : v * 0x7fff;
    if (this.n === this.size) {
      this.port.postMessage(this.out.buffer, [this.out.buffer]);
      this.out = new Int16Array(this.size);
      this.n = 0;
    }
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      this.sum += ch[i];
      this.count++;
      this.acc += 1;
      if (this.acc >= this.ratio) {
        this.push(this.sum / this.count);
        this.sum = 0;
        this.count = 0;
        this.acc -= this.ratio;
      }
    }
    return true;
  }
}

registerProcessor("pcm-capture", PcmCapture);
