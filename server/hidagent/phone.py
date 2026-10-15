"""Touch-like gestures on the phone, built from mouse moves.

Over Bluetooth the board's pointer is absolute: `at` puts it right on a screen spot, so taps
need no calibration. Over USB it is relative, so the server tracks where it is (in mouse
units); every gesture starts by slamming it into the top-left corner (`home`), because iOS
moves the pointer slightly differently each time and relative moves would add those errors
up. Coordinates are fractions of the screen (0..1, origin top-left), so they work for any
frame size.
"""

import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path

from . import vision
from .device import Device, DeviceError

log = logging.getLogger(__name__)

CALIBRATION_STOP = 40  # mouse units from the corner where calibration first looks for the pointer
# then it measures these screen spots (aimed with the fit so far), so taps anywhere are interpolated, not extrapolated
CALIBRATION_SPOTS = ((0.8, 0.15), (0.15, 0.85), (0.8, 0.85), (0.5, 0.5))
CALIBRATION_MAX_MISS = 0.05  # screen fraction a spot may sit off the fitted line before the readings count as wrong
HOLD = 0.25             # seconds to rest before lifting, so iOS adds no momentum (the swipe stops where it ends)
TAP_GAP = 0.15          # seconds to stay still after a click, so iOS sees a finished tap, not the start of a drag
NUDGE = 12              # mouse units the pointer is wiggled by to find it on screen
AIM_TOLERANCE = 0.008   # screen fraction; a bigger miss is corrected before pressing
MAX_MISS = 0.06         # bigger misses are still corrected, but not learned from (they may be misreadings)
ABSOLUTE_MAX = 32767    # the absolute pointer's range across each screen axis
GLIDE_MS = 500          # milliseconds an absolute drag takes to cross the full screen height


class Phone:
    def __init__(self, device: Device, calibration_file: Path, screen=None):
        self.device, self._file, self.screen = device, calibration_file, screen
        # mouse units needed to cross the full screen width / height
        saved = json.loads(calibration_file.read_text()) if calibration_file.exists() else None
        self.span = {"w": saved["w"], "h": saved["h"]} if saved else None
        self._pos = None  # pointer position in mouse units; None = unknown
        self._link = 0  # device.links when the pointer was last homed
        self.absolute = False  # the board took the last absolute move (it is paired over Bluetooth)
        # where the pointer really landed (seen on screen) for the screen fraction we aimed at, per axis;
        # a straight-line fit through them corrects every later move, so taps get better as the agent works
        self._seen = {axis: deque(map(tuple, (saved or {}).get("seen", {}).get(axis, [])), maxlen=12)
                      for axis in ("x", "y")}

    @property
    def calibrated(self) -> bool:
        return self.absolute or self.span is not None

    async def start_calibration(self) -> bool:
        """Find the pointer at spots spread over the screen and fit the mapping through all of them.

        iOS pointer motion is not exactly proportional and a reading can be a little off, so a fit through
        several far-apart spots beats one through two close ones. Each spot is reached from the corner, so
        errors do not add up. Returns True when calibrated; False when the pointer could not be found at a
        spot, which then waits for the user to click the pointer (finish_calibration).
        """
        if await self._at(0.5, 0.5):
            return True  # the absolute pointer lands where it is sent
        self._calibration = []  # (units x, units y, seen x, seen y) per spot
        return await self._calibrate()

    async def finish_calibration(self, x: float, y: float) -> bool:
        """x, y: where the user saw the pointer at the spot start_calibration() could not find it on its own."""
        self._calibration.append((*self._waiting, x, y))
        return await self._calibrate()

    async def _calibrate(self) -> bool:
        while (units := self._next_spot()) is not None:
            self._pos = None
            await self.device.send("home")
            await self.device.send(f"move {units[0]} {units[1]}")
            found = await self._find_pointer() if self.screen is not None and self.screen.live else None
            if not found:
                log.info("calibration: pointer not found at spot %d, waiting for a click", len(self._calibration) + 1)
                self._waiting = units
                return False
            self._calibration.append((*units, *found))
        fit = [_line([(p[axis], p[axis + 2]) for p in self._calibration]) for axis in (0, 1)]
        worst = max(abs(p[axis + 2] - (a * p[axis] + b)) for p in self._calibration for axis, (a, b) in enumerate(fit))
        if any(a <= 0 for a, _ in fit) or worst > CALIBRATION_MAX_MISS:
            raise RuntimeError("calibration readings disagree; calibrate again on a screen without icons or text")
        self.span = {"w": round(1 / fit[0][0]), "h": round(1 / fit[1][0])}
        for samples in self._seen.values():
            samples.clear()
        for ux, uy, x, y in self._calibration:  # where each spot landed, so moves are corrected for the offset too
            self._seen["x"].append((ux / self.span["w"], x))
            self._seen["y"].append((uy / self.span["h"], y))
        self._save()
        log.info("calibration: pointer at %s, span %s", [(round(x, 3), round(y, 3)) for *_, x, y in self._calibration],
                 self.span)
        return True

    def _next_spot(self) -> tuple[int, int] | None:
        """Mouse units for the next spot to measure, aimed with what was measured so far; None when done."""
        done = len(self._calibration)
        if done == 0:
            return CALIBRATION_STOP, CALIBRATION_STOP
        if done > len(CALIBRATION_SPOTS):
            return None
        units = []
        for axis, target in enumerate(CALIBRATION_SPOTS[done - 1]):
            points = [(p[axis], p[axis + 2]) for p in self._calibration]
            a, b = _line(points) if len({u for u, _ in points}) > 1 else (points[0][1] / points[0][0], 0.0)
            units.append(max(round((target - b) / a), 0) if a > 0 else points[0][0])
        return units[0], units[1]

    async def _find_pointer(self) -> tuple[float, float] | None:
        """Wiggle the pointer and find it anywhere on screen (the span may still be unknown)."""
        await self.screen.settled(quiet=0.2, least=0.3, most=1.5)
        before, sent = self.screen.frame, time.time()
        await self.device.send(f"move {NUDGE} 0")
        after = await self.screen.fresh(sent, 1.0)
        if after:
            await self.screen.settled(quiet=0.15, least=0.1, most=0.6)
            after = self.screen.frame
        await self.device.send(f"move -{NUDGE} 0")
        found = before and after and vision.pointer(before, after, (0.5, 0.5), 0.025, reach=(0.5, 0.5))
        return found if found and 0.03 < found[0] < 0.97 and 0.03 < found[1] < 0.97 else None

    def _save(self) -> None:
        """Calibration plus the observed landings, so the correction survives a restart."""
        self._file.write_text(json.dumps({**self.span, "seen": {k: list(v) for k, v in self._seen.items()}}))

    async def _absolute(self, command: str) -> bool:
        """Send a command for the absolute pointer; False if the board has none (it is on USB)."""
        try:
            await self.device.send(command)
        except DeviceError as e:
            if "needs Bluetooth" not in str(e):
                raise
            self.absolute = False
            return False
        self.absolute = True
        return True

    async def _at(self, x: float, y: float) -> bool:
        return await self._absolute(f"at {_spot(x, y)}")

    async def _stroke(self, points: list[tuple[float, float]], hold: bool) -> bool:
        """The whole drag as one board command (press, glide through the points, rest, release), so a slow
        network between press and release cannot turn a swipe into a long press."""
        await self._release_after_new_link()
        segments = [f"{_spot(x, y)} {max(round(max(abs(x - px), abs(y - py)) * GLIDE_MS), 1)}"
                    for (px, py), (x, y) in zip(points[:-1], points[1:], strict=True)]
        command = f"stroke {round(HOLD * 1000) if hold else 0} {_spot(*points[0])} {' '.join(segments)}"
        try:
            return await self._absolute(command)
        except DeviceError:
            await self.device.send("release")  # the board may have stopped mid-stroke
            raise

    async def _release_after_new_link(self) -> None:
        if self._link != self.device.links:  # new link: a dropped gesture may have left the button down
            await self.device.send("release")
            self._pos, self._link = None, self.device.links

    async def point(self, x: float, y: float, aim: bool = False) -> None:
        await self._release_after_new_link()
        if await self._at(x, y):
            return
        if not self.calibrated:
            raise RuntimeError("pointer not calibrated")
        await self._move_to(self._dx(x), self._dy(y))
        if aim and self.screen is not None:
            await self._aim(x, y)

    async def _aim(self, x: float, y: float) -> None:
        """Closed loop: find the real pointer on screen, fix a miss, and learn the error for later moves."""
        nudge = NUDGE if x < 0.5 else -NUDGE  # toward the middle: a nudge that hits the screen edge moves less
        await self.screen.settled(quiet=0.15, least=0.1, most=0.6)  # pointer at rest
        before, sent = self.screen.frame, time.time()
        await self.device.send(f"move {nudge} 0")
        after = await self.screen.fresh(sent, 0.6)
        if after:
            await self.screen.settled(quiet=0.1, least=0.05, most=0.4)
            after = self.screen.frame
        await self.device.send(f"move {-nudge} 0")
        shift = nudge / self.span["w"]
        found = before and after and (vision.pointer(before, after, (x, y), shift)  # near the target first,
                                      or vision.pointer(before, after, (0.5, 0.5), shift, reach=(0.5, 0.5)))  # anywhere
        if not found:
            log.debug("aim (%.3f, %.3f): pointer not found (%s)", x, y,
                      "no new frame after the nudge" if not after else "no pointer-sized change near the target")
            return
        fx, fy = found
        log.debug("aim (%.3f, %.3f): pointer at (%.3f, %.3f)", x, y, fx, fy)
        if max(abs(fx - x), abs(fy - y)) <= MAX_MISS:  # learn from normal drift only, never from a stray reading
            self._seen["x"].append((self._fix(x, "x"), fx))
            self._seen["y"].append((self._fix(y, "y"), fy))
            self._save()
        if abs(fx - x) <= AIM_TOLERANCE and abs(fy - y) <= AIM_TOLERANCE:
            return
        dx, dy = round((x - fx) * self.span["w"]), round((y - fy) * self.span["h"])  # fix this miss right away
        await self.device.send(f"move {dx} {dy}")
        self._pos = (self._pos[0] + dx, self._pos[1] + dy)

    def _fix(self, t: float, axis: str) -> float:
        """The fraction to aim at so the pointer lands on t, from the line through the observed landings."""
        pts = self._seen[axis]
        if len(pts) < 2:
            return t
        n = len(pts)
        mc, mf = sum(c for c, _ in pts) / n, sum(f for _, f in pts) / n
        var = sum((c - mc) ** 2 for c, _ in pts)
        a = sum((c - mc) * (f - mf) for c, f in pts) / var if var > 1e-4 else 1.0  # landed = a * aimed + b
        a = a if 0.85 < a < 1.15 else 1.0
        b = mf - a * mc
        return (t - b) / a if abs(b) < 0.1 else t

    async def _move_to(self, tx: int, ty: int, rehome: bool = True) -> None:
        if self._pos is None or rehome:
            await self.device.send("home")
            self._pos = (0, 0)
        x, y = self._pos
        self._pos = None  # unknown until the move succeeds
        await self.device.send(f"move {tx - x} {ty - y}")
        self._pos = (tx, ty)

    async def reset(self) -> None:
        """Let go of the button and re-home the pointer, whatever state a gesture left."""
        self._pos = None
        await self.device.send("release")
        await self.device.send("home")
        self._pos = (0, 0)

    async def tap(self, x: float, y: float, aim: bool = False) -> None:
        """aim=True finds the pointer on screen first (slower); worth it when a tap just missed."""
        await self.point(x, y, aim)
        await self.device.send("click")
        await asyncio.sleep(TAP_GAP)

    async def swipe(self, x1: float, y1: float, x2: float, y2: float, hold: bool = True) -> None:
        await self.drag([(x1, y1), (x2, y2)], hold)

    async def drag(self, points: list[tuple[float, float]], hold: bool = True) -> None:
        """Press at the first point, move through the rest, release: one continuous stroke.

        hold=True rests at the end first, so lists and pickers stop exactly there instead of flinging on.
        System gestures (home, back) need the flick, so they pass hold=False.
        """
        if await self._stroke(points, hold):
            return
        await self.point(*points[0])
        await self.device.send("press")
        try:
            for x, y in points[1:]:
                await self._move_to(self._dx(x), self._dy(y), rehome=False)  # never home mid-drag
            if hold:
                await asyncio.sleep(HOLD)
        finally:
            await self.device.send("release")

    async def scroll(self, x: float, y: float, lines: int) -> None:
        await self.point(x, y)
        await self.device.send(f"scroll {lines} 0")

    async def type(self, text: str) -> None:
        await self.device.send(f"type {text}")

    async def key(self, combo: str) -> None:
        await self.device.send(f"key {combo}")

    async def go_home(self) -> None:
        await self.swipe(0.5, 0.995, 0.5, 0.6, hold=False)  # resting here would open the app switcher

    async def go_back(self) -> None:
        await self.swipe(0.005, 0.45, 0.6, 0.45, hold=False)

    def _dx(self, x: float) -> int:
        return round(min(max(self._fix(x, "x"), 0), 1) * self.span["w"])

    def _dy(self, y: float) -> int:
        return round(min(max(self._fix(y, "y"), 0), 1) * self.span["h"])


def _spot(x: float, y: float) -> str:
    """A screen fraction as absolute pointer coordinates."""
    return f"{round(min(max(x, 0), 1) * ABSOLUTE_MAX)} {round(min(max(y, 0), 1) * ABSOLUTE_MAX)}"


def _line(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Least-squares fit seen = a * units + b."""
    n = len(points)
    mu, ms = sum(u for u, _ in points) / n, sum(f for _, f in points) / n
    var = sum((u - mu) ** 2 for u, _ in points)
    a = sum((u - mu) * (f - ms) for u, f in points) / var if var else 0.0
    return a, ms - a * mu
