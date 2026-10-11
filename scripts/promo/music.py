"""Background score for the full promo: an original nocturne in the style of Chopin (his nocturnes are public domain;
this melody and harmony are written here). Piano timbre is synthesised: inharmonic-free partials with per-partial decay,
soft hammer noise on attack, and a short feedback reverb. It is a synthesis, not a recording of a grand piano."""
import wave
import numpy as np

SR = 44100
# (bass midi, chord midi notes) — Eb major → Bb minor → Ab → Bb7 → Eb, one bar = 2.4 s, rubato-free
BARS = [
    (51, [63, 67, 70]), (50, [62, 65, 70]), (56, [60, 63, 68]), (58, [62, 65, 68, 70]),
    (51, [63, 67, 70]), (53, [60, 65, 68]), (56, [63, 68, 72]), (58, [62, 65, 68, 70]),
]
# melody: (bar index, beat offset in bars, midi, length in bars) — a long singing line over the bars
MELODY = [
    (0, 0.00, 75, 0.5), (0, 0.5, 74, 0.25), (0, 0.75, 72, 0.25),
    (1, 0.00, 70, 0.5), (1, 0.5, 72, 0.5),
    (2, 0.00, 75, 0.75), (2, 0.75, 77, 0.25), (2, 0.00, 0, 0),
    (3, 0.00, 75, 0.5), (3, 0.5, 72, 0.5),
    (4, 0.00, 70, 0.5), (4, 0.5, 67, 0.25), (4, 0.75, 70, 0.25),
    (5, 0.00, 72, 0.5), (5, 0.5, 75, 0.5),
    (6, 0.00, 77, 0.75), (6, 0.75, 75, 0.25),
    (7, 0.00, 72, 0.5), (7, 0.5, 70, 0.5),
]


def _mtof(m):
    return 440.0 * 2 ** ((m - 69) / 12)


def _note(freq, dur, vel):
    n = int(SR * (dur + 1.8))
    t = np.arange(n) / SR
    out = np.zeros(n)
    for k in range(1, 9):  # partials: brighter at the attack, darker as the note rings
        amp = vel * (1.0 / k ** 1.25)
        decay = 1.3 + 0.9 * k
        out += amp * np.sin(2 * np.pi * freq * k * t) * np.exp(-decay * t / (1 + 0.15 * dur))
    attack = np.minimum(t / 0.004, 1.0)  # hammer: 4 ms
    hammer = np.random.default_rng(int(freq * 100)).standard_normal(n) * np.exp(-t * 180) * vel * 0.06
    return (out * attack + hammer)


def _add(out, start, sig):
    """Mix a note into the buffer at `start`, clipped to the buffer's end."""
    if start >= len(out):
        return
    n = min(len(sig), len(out) - start)
    out[start:start + n] += sig[:n]


def _reverb(x):
    y = x.copy()
    for delay, gain in ((int(0.031 * SR), 0.32), (int(0.047 * SR), 0.27), (int(0.073 * SR), 0.22)):
        buf = np.zeros_like(x)
        buf[delay:] = x[:-delay] * gain
        y += buf
    tail = np.zeros_like(x)
    tail[int(0.11 * SR):] = y[:-int(0.11 * SR)] * 0.18
    return y + tail


def render(seconds, path, bar=2.4, volume=0.5):
    total = int(SR * seconds)
    out = np.zeros(total + SR * 3)
    rng = np.random.default_rng(7)
    n_bars = int(seconds / bar) + 2
    for b in range(n_bars):
        bass, chord = BARS[b % len(BARS)]
        t0 = b * bar
        # left hand: broken chord, eighths, soft
        for i in range(8):
            m = [bass, bass + 12, chord[i % len(chord)], chord[(i + 1) % len(chord)]][i % 4]
            s = int((t0 + i * bar / 8) * SR)
            _add(out, s, _note(_mtof(m), 0.3, 0.16 + 0.02 * rng.random()))
        # bass note on the bar
        s = int(t0 * SR)
        _add(out, s, _note(_mtof(bass), 1.2, 0.34))
    for bar_i, off, midi, length in MELODY:
        if length == 0:
            continue
        for rep in range(len(BARS) // len(MELODY) + 3):  # the line repeats over the score
            b = bar_i + rep * 8
            t0 = (b + off) * bar
            if t0 >= seconds:
                break
            s = int(t0 * SR)
            dur = length * bar
            _add(out, s, _note(_mtof(midi), dur, 0.42))
    out = out[:total]
    out = _reverb(out)
    fade_in = np.minimum(np.arange(total) / (2.0 * SR), 1.0)
    fade_out = np.clip((total - np.arange(total)) / (3.0 * SR), 0, 1)
    out *= fade_in * fade_out
    peak = np.max(np.abs(out)) or 1.0
    out = out / peak * volume
    stereo = np.stack([out, out * 0.96])
    pcm = (np.clip(stereo.T, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return path


if __name__ == "__main__":
    import sys
    render(float(sys.argv[1]) if len(sys.argv) > 1 else 30, sys.argv[2] if len(sys.argv) > 2 else "nocturne.wav")
