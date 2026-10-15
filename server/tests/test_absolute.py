import asyncio

import pytest
from test_calibration import FakePhone

from hidagent.device import DeviceError
from hidagent.phone import Phone

MAX = 32767  # the board's absolute pointer range


class AbsoluteBoard:
    """A board paired over Bluetooth: its pointer goes straight to absolute positions."""

    def __init__(self):
        self.pos = None  # screen fraction
        self.down = False
        self.taps, self.strokes, self.holds, self.sent = [], [], [], []
        self.links = 1

    async def send(self, cmd: str) -> None:
        self.sent.append(cmd)
        name, *args = cmd.split()
        if name == "at":
            x, y = int(args[0]) / MAX, int(args[1]) / MAX
            if self.down:
                self.strokes[-1].append((x, y, int(args[2]) if len(args) > 2 else 0))
            self.pos = (x, y)
        elif name == "press":
            self.down = True
            self.strokes.append([(*self.pos, 0)])
        elif name == "release":
            self.down = False
        elif name == "click":
            self.taps.append(self.pos)
        elif name == "stroke":  # the board presses, glides through every point and releases on its own
            hold, x0, y0, *rest = map(int, args)
            self.strokes.append([(x0 / MAX, y0 / MAX, 0)] + [
                (rest[i] / MAX, rest[i + 1] / MAX, rest[i + 2]) for i in range(0, len(rest), 3)])
            self.holds.append(hold)
            self.pos = self.strokes[-1][-1][:2]
        elif name in ("home", "move"):
            raise AssertionError(f"relative command '{cmd}' sent to the absolute pointer")


def make(tmp_path, board=None):
    board = board or AbsoluteBoard()
    return Phone(board, tmp_path / "calibration.json"), board


@pytest.mark.parametrize("target", [(0.1, 0.95), (0.9, 0.05), (0.5, 0.5), (0.0, 1.0)])
def test_tap_lands_on_target_without_calibration(tmp_path, target):
    phone, board = make(tmp_path)
    asyncio.run(phone.tap(*target))
    assert board.taps == [pytest.approx(target, abs=1 / MAX)]
    assert not board.down


def test_precise_tap_needs_no_wiggle(tmp_path):
    phone, board = make(tmp_path)
    phone.screen = FakePhone()  # a mirrored screen is there, but the pointer is where it was sent
    asyncio.run(phone.tap(0.3, 0.7, aim=True))
    assert board.taps == [pytest.approx((0.3, 0.7), abs=1 / MAX)]
    assert [c.split()[0] for c in board.sent] == ["release", "at", "click"]


@pytest.mark.parametrize("hold", [True, False])
def test_drag_is_one_stroke_command_gliding_through_every_point(tmp_path, hold):
    # one command: a slow network between press and release would turn a swipe into a long press
    phone, board = make(tmp_path)
    points = [(0.2, 0.8), (0.5, 0.5), (0.8, 0.2)]
    asyncio.run(phone.drag(points, hold=hold))
    (stroke,) = board.strokes
    assert [(x, y) for x, y, _ in stroke] == [pytest.approx(p, abs=1 / MAX) for p in points]
    assert all(ms > 0 for *_, ms in stroke[1:])  # glided, not jumped: iOS would see a teleport, not a swipe
    assert (board.holds[0] > 0) == hold  # rest before lifting, so lists stop where the swipe ends
    assert [c.split()[0] for c in board.sent] == ["release", "stroke"]


def test_circle_fits_one_stroke(tmp_path):
    import math
    phone, board = make(tmp_path)
    points = [(0.5 + 0.2 * math.cos(i / 4), 0.5 + 0.1 * math.sin(i / 4)) for i in range(25)]
    asyncio.run(phone.drag(points))
    assert len(board.strokes[0]) == 25


def test_drag_releases_the_button_when_the_link_fails_mid_stroke(tmp_path):
    phone, board = make(tmp_path)
    send = board.send

    async def flaky(cmd):
        if cmd.startswith("stroke"):
            board.down = True  # it failed mid-way, button down
            raise DeviceError("err bluetooth dropped 3 reports")
        await send(cmd)

    board.send = flaky
    with pytest.raises(DeviceError):
        asyncio.run(phone.swipe(0.5, 0.9, 0.5, 0.3))
    assert not board.down


def test_calibration_is_not_needed(tmp_path):
    phone, board = make(tmp_path)
    assert asyncio.run(phone.start_calibration()) is True
    assert phone.calibrated
    assert not any(c.split()[0] in ("home", "move") for c in board.sent)


def test_usb_board_without_absolute_pointer_still_uses_calibrated_moves(tmp_path):
    fake = FakePhone()  # refuses "at", like the board on USB
    phone = Phone(fake, tmp_path / "calibration.json", screen=fake)
    assert asyncio.run(phone.start_calibration()) is True
    asyncio.run(phone.point(0.1, 0.95))
    assert fake.at() == (pytest.approx(0.1, abs=0.01), pytest.approx(0.95, abs=0.01))
