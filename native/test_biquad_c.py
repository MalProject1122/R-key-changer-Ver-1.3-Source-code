import ctypes
import pathlib
import time
import numpy as np

DLL_PATH = pathlib.Path(__file__).with_name("biquad_eq.dll")
lib = ctypes.CDLL(str(DLL_PATH))

lib.eq_create.restype = ctypes.c_void_p
lib.eq_create.argtypes = [ctypes.c_int, ctypes.c_int]
lib.eq_destroy.argtypes = [ctypes.c_void_p]
lib.eq_set_band_db.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_double]
lib.eq_reset.argtypes = [ctypes.c_void_p]
lib.eq_process.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_float),
    ctypes.c_int,
]

sr = 48000
channels = 2
BAND_FREQS = (31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)


class BiquadEqC:
    def __init__(self, sample_rate, channels):
        self._handle = lib.eq_create(sample_rate, channels)
        self.channels = channels

    def set_gains_db(self, gains_db):
        for i, db in enumerate(gains_db):
            lib.eq_set_band_db(self._handle, i, float(db))

    def reset(self):
        lib.eq_reset(self._handle)

    def process(self, block):
        # block: (frames, channels) float32, C-contiguous, processed in place
        block = np.ascontiguousarray(block, dtype=np.float32)
        frames = block.shape[0]
        ptr = block.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        lib.eq_process(self._handle, ptr, frames)
        return block

    def __del__(self):
        if getattr(self, "_handle", None):
            lib.eq_destroy(self._handle)


# --- correctness: compare vs. pure-Python reference biquad cascade ---
def make_peaking_coeffs(f0, gain_db, sr, Q=1.0):
    A = 10 ** (gain_db / 40.0)
    w0 = 2 * np.pi * f0 / sr
    alpha = np.sin(w0) / (2 * Q)
    cosw0 = np.cos(w0)
    b0 = 1 + alpha * A
    b1 = -2 * cosw0
    b2 = 1 - alpha * A
    a0 = 1 + alpha / A
    a1 = -2 * cosw0
    a2 = 1 - alpha / A
    return (b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)


def python_ref_cascade(x, gains_db, sr):
    y = x.astype(np.float64).copy()
    for f0, gain_db in zip(BAND_FREQS, gains_db):
        if gain_db == 0.0:
            continue
        b0, b1, b2, a1, a2 = make_peaking_coeffs(f0, gain_db, sr)
        z1 = z2 = 0.0
        out = np.empty_like(y)
        for n in range(len(y)):
            xn = y[n]
            yn = b0 * xn + z1
            z1 = b1 * xn - a1 * yn + z2
            z2 = b2 * xn - a2 * yn
            out[n] = yn
        y = out
    return y


rng = np.random.default_rng(0)
x = rng.standard_normal((2000, 1)).astype(np.float32) * 0.2
gains = [3.0, -2.0, 0.0, 1.0, 0.0, 6.0, -4.0, 2.0, 0.0, -6.0]

y_ref = python_ref_cascade(x[:, 0], gains, sr)

eq = BiquadEqC(sr, 1)
eq.set_gains_db(gains)
y_c = eq.process(x.copy())[:, 0]

err = np.max(np.abs(y_c.astype(np.float64) - y_ref))
print(f"max abs error vs python reference: {err:.3e}")

# --- streaming continuity (state carries across calls) ---
eq2 = BiquadEqC(sr, 1)
eq2.set_gains_db(gains)
half = 1000
y1 = eq2.process(x[:half].copy())
y2 = eq2.process(x[half:].copy())
y_streamed = np.concatenate([y1, y2])[:, 0]
err_stream = np.max(np.abs(y_streamed.astype(np.float64) - y_ref))
print(f"max abs error streamed (2 calls) vs single-call reference: {err_stream:.3e}")

# --- finite/no-NaN across extreme all-band gains, all-boost and all-cut ---
# (streaming: each iteration feeds a FRESH block, like real continuous audio;
# reprocessing the same output repeatedly would just reapply +12dB N times,
# which is not what happens in the real callback)
for name, g in (("all+12", (12.0,) * 10), ("all-12", (-12.0,) * 10)):
    eq3 = BiquadEqC(sr, channels)
    eq3.set_gains_db(g)
    peak = 0.0
    for _ in range(20):
        block = rng.standard_normal((4096, channels)).astype(np.float32) * 0.2
        out = eq3.process(block)
        peak = max(peak, float(np.abs(out).max()))
    print(f"{name}: finite={np.isfinite(out).all()} max={peak:.3f}")

# --- accuracy check: labeled dB vs actual measured dB at band center (pure tone) ---
def pure_tone_gain_db(freq, gain_db, sr, n=48000):
    t = np.arange(n) / sr
    tone = (0.1 * np.sin(2 * np.pi * freq * t)).astype(np.float32).reshape(-1, 1)
    eq = BiquadEqC(sr, 1)
    eq.set_gains_db([0.0] * 10)
    settle = eq.process(tone.copy())  # flat pass just to warm state (unused)
    eq2 = BiquadEqC(sr, 1)
    gains = [0.0] * 10
    idx = BAND_FREQS.index(freq)
    gains[idx] = gain_db
    eq2.set_gains_db(gains)
    y = eq2.process(tone.copy())
    # skip transient at start
    ref_rms = np.sqrt((tone[n // 2:] ** 2).mean())
    out_rms = np.sqrt((y[n // 2:] ** 2).mean())
    return 20 * np.log10(out_rms / ref_rms)


print()
print("labeled vs measured dB at band center (pure tone, biquad):")
for f0 in BAND_FREQS:
    for target in (6.0, 12.0, -6.0, -12.0):
        measured = pure_tone_gain_db(f0, target, sr)
        print(f"  {f0:6d}Hz target={target:+.0f}dB measured={measured:+.2f}dB")

# --- timing: cascade of 10 bands, stereo, block sizes 1024/2048/4096 ---
print()
eq_bench = BiquadEqC(sr, channels)
eq_bench.set_gains_db([6.0] * 10)
for block_size in (1024, 2048, 4096):
    block = rng.standard_normal((block_size, channels)).astype(np.float32)
    times = []
    for _ in range(200):
        b = block.copy()
        t0 = time.perf_counter()
        eq_bench.process(b)
        times.append(time.perf_counter() - t0)
    budget_ms = block_size / sr * 1000
    mean_ms = np.mean(times) * 1e3
    max_ms = np.max(times) * 1e3
    print(
        f"block={block_size:5d} budget={budget_ms:5.1f}ms  C biquad cascade (stereo, 10band): "
        f"mean={mean_ms:.4f}ms max={max_ms:.4f}ms ({max_ms / budget_ms * 100:.2f}% of budget)"
    )
