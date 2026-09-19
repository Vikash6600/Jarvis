"""
Interactive particle globe — the MARK LIV centrepiece in the blue HUD theme.

A Fibonacci sphere of particles, projected with a little perspective, inside a
set of rotating rings, a live audio ring and three orbiting satellites. Drawn
entirely with QPainter like the avatar and the reactor core, so it needs no GPU.

It is driven by the same inputs as the other centrepieces:
  * `state` / `speaking` / `muted` choose the motion (listening breathes,
    thinking spins fast with shimmering bands, speaking ripples, asleep dims);
  * `amp` (0–1, the smoothed audio level) swells the speaking waves and the
    audio ring, so both follow the real voice and microphone.

And it is interactive: drag to spin it (it keeps the momentum), hover to light
up the particles under the pointer, click to send a ripple.
"""
from __future__ import annotations

import math

from PyQt6.QtCore import QLineF, QPointF, QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QRadialGradient


def _mix(a: QColor, b: QColor, k: float) -> QColor:
    k = max(0.0, min(1.0, k))
    return QColor(int(a.red() + (b.red() - a.red()) * k),
                  int(a.green() + (b.green() - a.green()) * k),
                  int(a.blue() + (b.blue() - a.blue()) * k))


def _alpha(c: QColor, a: float) -> QColor:
    q = QColor(c)
    q.setAlphaF(max(0.0, min(1.0, a)))
    return q


class ParticleGlobe:
    N_POINTS = 460
    # (x-radius, y-radius, tilt°, speed, phase) in units of the outer radius
    ORBITS = ((0.833, 0.207, -18, 0.9, 0.0),
              (0.773, 0.300, 38, -0.6, 2.0),
              (0.893, 0.153, 74, 0.45, 4.0))

    def __init__(self, mono_family: str = "Share Tech Mono"):
        self._mono = mono_family
        self.t = 0.0
        self.ry = 0.0
        self.rx = -0.35
        self.vy = 0.0
        self.dragging = False
        self.hover: tuple[float, float] | None = None
        self._ripples: list[float] = []      # start times of click ripples
        self.mode = "listening"
        self.amp = 0.0
        ga = math.pi * (3 - math.sqrt(5))
        n = self.N_POINTS
        pts = []
        for i in range(n):
            y = 1 - (i / (n - 1)) * 2
            r = math.sqrt(max(0.0, 1 - y * y))
            th = ga * i
            x, z = math.cos(th) * r, math.sin(th) * r
            pts.append((x, y, z, math.asin(y), math.atan2(z, x), (i * 37) % 23 == 0))
        self._pts = pts

    # ── input ───────────────────────────────────────────────────────────────
    def drag_by(self, dx: float, dy: float) -> None:
        self.ry += dx * 0.01
        self.rx = max(-1.2, min(1.2, self.rx + dy * 0.01))
        self.vy = dx * 0.5

    def pulse(self) -> None:
        self._ripples.append(self.t)
        self._ripples = self._ripples[-4:]

    # ── animation ───────────────────────────────────────────────────────────
    @staticmethod
    def mode_for(state: str, speaking: bool, muted: bool) -> str:
        if muted:
            return "asleep"
        if speaking:
            return "speaking"
        s = (state or "").upper()
        if s in ("THINKING", "PROCESSING"):
            return "thinking"
        if s in ("SLEEPING", "ASLEEP", "STANDBY"):
            return "asleep"
        return "listening"

    def step(self, dt: float, amp: float, state: str, speaking: bool, muted: bool) -> None:
        dt = max(0.0, min(0.1, dt))
        self.t += dt
        self.amp = amp
        self.mode = self.mode_for(state, speaking, muted)
        spd = {"listening": 0.22, "thinking": 0.95, "speaking": 0.38, "asleep": 0.06}[self.mode]
        if not self.dragging:
            self.vy *= 0.94
            self.ry += (spd + self.vy) * dt
            self.rx += (-0.35 - self.rx) * 0.015
        self._ripples = [r for r in self._ripples if self.t - r < 1.6]

    # ── drawing ─────────────────────────────────────────────────────────────
    def paint(self, p: QPainter, cx: float, cy: float, R: float,
              col: QColor, acc: QColor, bg: QColor, labels: bool = True) -> None:
        t, m, amp = self.t, self.mode, self.amp
        dim = 0.45 if m == "asleep" else 1.0
        white = QColor("#ffffff")

        # atmosphere glow
        glow_a = (0.30 + 0.25 * amp + (0.08 * math.sin(t * 6) if m == "speaking" else 0.0)) * dim
        g = QRadialGradient(cx, cy, R * 0.72)
        g.setColorAt(0.0, _alpha(col, glow_a))
        g.setColorAt(0.55, _alpha(col, glow_a * 0.3))
        g.setColorAt(1.0, _alpha(col, 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawEllipse(QPointF(cx, cy), R * 0.72, R * 0.72)
        p.setBrush(Qt.BrushStyle.NoBrush)

        # crosshair
        p.setPen(QPen(_alpha(col, 0.18 * dim), 1))
        p.drawLine(QLineF(cx - R * 1.1, cy, cx + R * 1.1, cy))
        p.drawLine(QLineF(cx, cy - R * 1.1, cx, cy + R * 1.1))

        # outer circle + tick ring
        p.setPen(QPen(_alpha(col, 0.35 * dim), 1))
        p.drawEllipse(QPointF(cx, cy), R, R)
        ticks = []
        base = t * 0.15
        for i in range(120):
            a = base + i * math.tau / 120
            r0 = R * (0.945 if i % 5 else 0.925)
            ticks.append(QLineF(cx + math.cos(a) * r0, cy + math.sin(a) * r0,
                                cx + math.cos(a) * R * 0.985, cy + math.sin(a) * R * 0.985))
        p.setPen(QPen(_alpha(col, 0.55 * dim), 1.2))
        p.drawLines(ticks)

        if labels and R > 140:
            f = QFont(self._mono, max(7, int(R * 0.03)))
            p.setFont(f)
            p.setPen(QPen(_alpha(QColor("#8aa6e8"), 0.9 * dim), 1))
            for d in range(0, 360, 30):
                a = math.radians(d - 90)
                x, y = cx + math.cos(a) * R * 1.06, cy + math.sin(a) * R * 1.06
                p.drawText(QRectF(x - 20, y - 8, 40, 16), Qt.AlignmentFlag.AlignCenter, f"{d:03d}")

        # segmented rotating arcs + an accent segment
        rate = {"thinking": 2.6, "speaking": 1.6}.get(m, 1.0)
        box = QRectF(cx - R * 0.887, cy - R * 0.887, R * 1.774, R * 1.774)
        rot = -(t * 14 * rate) % 360
        p.setPen(QPen(_alpha(col, 0.85 * dim), max(1.5, R * 0.01)))
        for k in range(4):
            p.drawArc(box, int((rot + k * 90 + 10) * 16), int(70 * 16))
        p.setPen(QPen(_alpha(acc, 0.9 * dim), max(1.5, R * 0.01)))
        p.drawArc(box, int((rot + 84) * 16), int(12 * 16))

        p.setPen(QPen(_alpha(col, 0.5 * dim), 1))
        p.drawEllipse(QPointF(cx, cy), R * 0.753, R * 0.753)

        # audio ring
        bars = []
        nb = 90
        r0 = R * 0.71
        for i in range(nb):
            a = i / nb * math.tau - math.pi / 2
            if m == "speaking":
                v = 0.25 + (0.35 + 0.65 * amp) * (0.5 + 0.5 * math.sin(t * 7 + i * 0.5) * math.sin(t * 3.1 + i * 0.13))
            elif m == "listening":
                v = 0.12 + 0.08 * math.sin(t * 2 + i * 0.3) + amp * 0.8 * (0.5 + 0.5 * math.sin(t * 9 + i * 0.7))
            elif m == "thinking":
                v = 0.35 * (0.5 + 0.5 * math.sin(i * 0.4 - t * 8))
            else:
                v = 0.04
            L = R * (0.01 + v * 0.09)
            bars.append((QLineF(cx + math.cos(a) * r0, cy + math.sin(a) * r0,
                                cx + math.cos(a) * (r0 + L), cy + math.sin(a) * (r0 + L)), v))
        for ln, v in bars:
            p.setPen(QPen(_alpha(col, (0.3 + 0.7 * min(1.0, v)) * dim), max(1.6, R * 0.0085),
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(ln)

        # orbits (back halves first so satellites pass behind the globe)
        sat_speed = {"asleep": 0.15, "thinking": 2.2}.get(m, 1.0)
        sats = []
        for (orx, ory, tilt, sp, ph) in self.ORBITS:
            p.save()
            p.translate(cx, cy)
            p.rotate(tilt)
            pen = QPen(_alpha(col, 0.35 * dim), 1, Qt.PenStyle.CustomDashLine)
            pen.setDashPattern([2, 5])
            p.setPen(pen)
            p.drawEllipse(QPointF(0, 0), R * orx, R * ory)
            p.restore()
            a = t * sp * sat_speed + ph
            lx, ly = math.cos(a) * R * orx, math.sin(a) * R * ory
            rr = math.radians(tilt)
            sats.append((cx + lx * math.cos(rr) - ly * math.sin(rr),
                         cy + lx * math.sin(rr) + ly * math.cos(rr), math.sin(a) > 0))

        def draw_sats(front: bool):
            p.setPen(Qt.PenStyle.NoPen)
            for x, y, fr in sats:
                if fr == front:
                    p.setBrush(_alpha(acc, (1.0 if fr else 0.4) * dim))
                    rad = R * (0.013 if fr else 0.008)
                    p.drawEllipse(QPointF(x, y), rad, rad)
            p.setBrush(Qt.BrushStyle.NoBrush)

        draw_sats(False)

        # the sphere
        Rs = R * 0.583
        cya, sya = math.cos(self.ry), math.sin(self.ry)
        cxa, sxa = math.cos(self.rx), math.sin(self.rx)
        hx, hy = self.hover if self.hover else (-1e9, -1e9)
        hover_r = R * 0.27
        ripple_fronts = [(self.t - r0_) / 1.6 for r0_ in self._ripples]
        out = []
        for (x, y, z, lat, lon, hot) in self._pts:
            if m == "speaking":
                f = 1 + (0.04 + 0.08 * amp) * math.sin(lat * 7 - t * 9) * (0.55 + 0.45 * math.sin(t * 3.3))
            elif m == "listening":
                f = 1 + 0.03 * math.sin(t * 2.2) + amp * 0.05 * math.sin(lat * 5 + t * 6)
            elif m == "thinking":
                f = 1 + 0.02 * math.sin(lon * 6 + t * 6)
            else:
                f = 0.97
            x1 = x * cya + z * sya
            z1 = -x * sya + z * cya
            y2 = y * cxa - z1 * sxa
            z2 = y * sxa + z1 * cxa
            k = 1 / (1 - z2 * 0.18)
            sx, sy = cx + x1 * Rs * f * k, cy + y2 * Rs * f * k
            d = (z2 + 1) / 2
            r = (0.6 + d * 2.1) * (R / 300)
            o = 0.1 + d * 0.9
            c = col
            if m == "thinking":
                o *= 0.5 + 0.5 * math.sin(lon * 5 + t * 7)
            o *= dim
            if hot and m != "asleep":
                fl = 0.5 + 0.5 * math.sin(t * 4 + lon * 3)
                if fl > 0.85 and d > 0.5:
                    c = acc
                    r += 1.2 * (R / 300)
            # click ripple: a band of light sweeping outward over the surface
            for fr in ripple_fronts:
                band = 1 - abs((1 - (y2 + 1) / 2) - fr) * 8
                if band > 0:
                    o = max(o, band * (1 - fr))
                    r += band * 1.5 * (R / 300)
            dist = math.hypot(sx - hx, sy - hy)
            if dist < hover_r:
                b = 1 - dist / hover_r
                r += b * 2.8 * (R / 300)
                o = max(o, b)
                if b > 0.35:
                    c = white
            out.append((z2, sx, sy, max(0.4, r), o, c))
        out.sort(key=lambda q: q[0])
        p.setPen(Qt.PenStyle.NoPen)
        for _z, sx, sy, r, o, c in out:
            p.setBrush(_alpha(c, o))
            p.drawEllipse(QPointF(sx, sy), r, r)
        p.setBrush(Qt.BrushStyle.NoBrush)

        draw_sats(True)

        # click ripples as expanding rings
        for fr in ripple_fronts:
            rad = Rs + (R - Rs) * fr * 1.2
            p.setPen(QPen(_alpha(acc, (1 - fr) * 0.8), 2))
            p.drawEllipse(QPointF(cx, cy), rad, rad)
