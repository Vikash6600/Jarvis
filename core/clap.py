"""
Double-clap detector.

A clap is short, loud, broadband and gone almost at once. Speech, music and a
door knock each fail at least one of those, so a clap here must pass all four:

  1. loud      — well above the adaptive room-noise floor (and an absolute floor)
  2. sudden    — energy jumps several-fold within one 16 ms sub-frame
  3. bright    — a large share of its energy above ~1.5 kHz (a knock or a bass
                 thump is mostly low frequency; voiced speech too)
  4. brief     — falls back below 30 % of its peak within ~130 ms

Two claps 0.15–0.8 s apart, followed by ~0.45 s without a third, is a double
clap. A third clap (applause, a drum beat) cancels it, and a 2 s refractory
period follows every trigger.

Structure mirrors WakeWordDetector: `feed()` is called from the audio callback
and only pushes onto a queue; the analysis runs on its own thread and calls
`on_detect()` from there. Time is measured in samples, not wall-clock, so the
detector is deterministic (and testable) regardless of scheduling jitter.
"""
from __future__ import annotations

import queue
import threading
from typing import Callable

import numpy as np

RATE = 16000
SUB = 256                          # 16 ms sub-frames
SENSITIVITY = {                    # (k × noise floor, absolute rms floor)
    "low": (14.0, 0.10),
    "medium": (9.0, 0.05),
    "high": (5.0, 0.025),
}


class ClapDetector:
    def __init__(self, on_detect: Callable[[], None] | None = None,
                 sensitivity: str = "medium", rate: int = RATE,
                 logger: Callable[[str], None] | None = None):
        self.on_detect = on_detect
        self.rate = rate
        self._log = logger or (lambda m: None)
        self.set_sensitivity(sensitivity)
        self._q: queue.Queue = queue.Queue(maxsize=200)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.reset()
        freqs = np.fft.rfftfreq(SUB, 1.0 / rate)
        self._hf = freqs >= 1500.0
        self._win = np.hanning(SUB).astype(np.float32)

    # ── configuration ────────────────────────────────────────────────────────
    def set_sensitivity(self, level: str) -> None:
        self.sensitivity = level if level in SENSITIVITY else "medium"
        self._k, self._abs = SENSITIVITY[self.sensitivity]

    def reset(self) -> None:
        self._t = 0                 # samples processed
        self._noise = 0.004         # adaptive rms floor
        self._prev = 0.0
        self._pending = bytearray()
        self._event = None          # (onset_t, peak_rms) of a clap being verified
        self._claps: list[int] = []  # confirmed clap onset times (samples)
        self._quiet_until = 0       # refractory end
        self.detections = 0

    # ── threading (same shape as WakeWordDetector) ───────────────────────────
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="clap-detector")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def feed(self, frame) -> None:
        """Called from the audio callback: never blocks, never analyses."""
        try:
            self._q.put_nowait(np.array(frame, copy=True))
        except queue.Full:
            pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                frame = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.process(frame)
            except Exception as e:           # never let the detector die
                self._log(f"clap detector error: {e}")

    # ── analysis ─────────────────────────────────────────────────────────────
    def process(self, frame) -> int:
        """Analyse a block of int16 (or float) mono samples. Returns the number
        of double claps detected in it (and calls on_detect for each)."""
        x = np.asarray(frame).reshape(-1)
        if x.dtype != np.float32:
            x = x.astype(np.float32) / (32768.0 if np.issubdtype(np.asarray(frame).dtype, np.integer) else 1.0)
        found = 0
        for i in range(0, len(x) - SUB + 1, SUB):
            if self._sub(x[i:i + SUB]):
                found += 1
        return found

    def _sub(self, s: np.ndarray) -> bool:
        t = self._t
        self._t += SUB
        rms = float(np.sqrt(np.mean(s * s)) + 1e-9)
        prev, self._prev = self._prev, rms

        # 4. brief: an onset under verification must decay quickly
        if self._event is not None:
            onset, peak = self._event
            peak = max(peak, rms)
            self._event = (onset, peak)
            if rms < 0.30 * peak:
                self._event = None
                self._confirm(onset)
            elif t - onset > int(0.13 * self.rate):
                self._event = None          # sustained sound — not a clap
                self._claps.clear()
            return self._maybe_fire(t)

        loud = rms > max(self._abs, self._k * self._noise)
        sudden = rms > 5.0 * max(prev, self._noise)
        if loud and sudden and t >= self._quiet_until:
            spec = np.abs(np.fft.rfft(s * self._win)) ** 2
            bright = float(spec[self._hf].sum() / (spec.sum() + 1e-12))
            if bright > 0.30:
                self._event = (t, rms)
                return self._maybe_fire(t)
        else:
            # only learn the floor from ordinary audio
            a = 0.02 if rms < self._noise * 3 else 0.002
            self._noise = (1 - a) * self._noise + a * min(rms, 0.05)
        return self._maybe_fire(t)

    def _confirm(self, onset: int) -> None:
        if self._claps and onset - self._claps[-1] < int(0.12 * self.rate):
            return                           # the same clap's tail
        self._claps.append(onset)
        self._claps = self._claps[-3:]
        if len(self._claps) >= 3:            # applause / a rhythm → cancel
            self._claps.clear()
            self._quiet_until = onset + int(0.6 * self.rate)

    def _maybe_fire(self, t: int) -> bool:
        c = self._claps
        if len(c) == 2:
            gap = (c[1] - c[0]) / self.rate
            if not (0.15 <= gap <= 0.8):
                self._claps = c[1:]
                return False
            if (t - c[1]) / self.rate >= 0.45 and self._event is None:
                self._claps = []
                self._quiet_until = t + 2 * self.rate
                self.detections += 1
                self._log("double clap")
                if self.on_detect:
                    try:
                        self.on_detect()
                    except Exception as e:
                        self._log(f"on_detect failed: {e}")
                return True
        elif len(c) == 1 and (t - c[0]) / self.rate > 0.8:
            self._claps = []
        return False
