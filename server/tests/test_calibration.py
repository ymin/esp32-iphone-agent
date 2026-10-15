import asyncio
import io
import json

import pytest
from PIL import Image, ImageDraw

from hidagent.device import DeviceError
from hidagent.phone import Phone

W, H = 498, 1080  # a mirrored iPhone frame


class FakePhone:
    """A pointer that iOS moves in proportion to mouse units, from an offset start, and a screen showing it."""

    def __init__(self, gain=(1 / 113, 1 / 244), offset=(0.03, 0.01), hidden=lambda x, y: False,
                 shifted=lambda x, y: (x, y)):
        self.gain, self.offset, self.hidden, self.shifted = gain, offset, hidden, shifted
        self.pos = list(offset)  # where iOS shows the pointer, as a screen fraction
        self.links = 1
        self.live = True

    # the board
    async def send(self, cmd: str) -> str:
        name, *args = cmd.split()
        if name == "at":
            raise DeviceError("err absolute pointer needs Bluetooth")  # the board on USB
        if name == "home":
            self.pos = list(self.offset)
        elif name == "move":
            # iOS stops the pointer at the screen edge (seen at about 0.96 when pushed into a corner); the next
            # move starts from there
            self.pos = [min(max(p + g * int(d), 0.0), 0.96) for p, g, d in zip(self.pos, self.gain, args, strict=True)]
        return "ok"

    def at(self) -> tuple[float, float]:
        return self.pos[0], self.pos[1]

    # the mirrored screen
    @property
    def frame(self) -> bytes:
        img = Image.new("RGB", (W, H), (20, 20, 20))
        x, y = self.at()
        x, y = self.shifted(x, y)
        if not self.hidden(x, y):
            ImageDraw.Draw(img).ellipse([x * W - 11, y * H - 11, x * W + 11, y * H + 11], fill=(90, 90, 90))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=75)
        return buf.getvalue()

    async def settled(self, **_):
        return None

    async def fresh(self, sent, timeout):
        return self.frame


def make(tmp_path, fake):
    return Phone(fake, tmp_path / "calibration.json", screen=fake)


def lands(phone, fake, x, y):
    asyncio.run(phone.point(x, y))
    return fake.at()


@pytest.mark.parametrize("target", [(0.1, 0.95), (0.9, 0.05), (0.5, 0.5), (0.9, 0.9), (0.1, 0.1)])
def test_taps_land_on_target_everywhere_after_calibration(tmp_path, target):
    fake = FakePhone()
    phone = make(tmp_path, fake)
    assert asyncio.run(phone.start_calibration()) is True
    x, y = lands(phone, fake, *target)
    assert x == pytest.approx(target[0], abs=0.01) and y == pytest.approx(target[1], abs=0.01)


def test_one_misread_point_barely_moves_far_taps(tmp_path):
    # the pointer is read 3% too far right near the first stop (say, text under it changes its shape)
    fake = FakePhone(shifted=lambda x, y: (x + 0.03, y) if x < 0.5 and y < 0.3 else (x, y))
    phone = make(tmp_path, fake)
    assert asyncio.run(phone.start_calibration()) is True
    x, y = lands(phone, fake, 0.1, 0.95)
    assert x == pytest.approx(0.1, abs=0.015) and y == pytest.approx(0.95, abs=0.015)


def test_a_hidden_point_waits_for_a_click_then_calibration_continues(tmp_path):
    fake = FakePhone(hidden=lambda x, y: y > 0.7)  # e.g. a keyboard: the pointer cannot be found there
    phone = make(tmp_path, fake)
    done = asyncio.run(phone.start_calibration())
    clicks = 0
    while not done:
        clicks += 1
        assert clicks <= 5
        done = asyncio.run(phone.finish_calibration(*fake.at()))  # the user clicks where the pointer is
    assert clicks >= 1
    x, y = lands(phone, fake, 0.1, 0.95)
    assert x == pytest.approx(0.1, abs=0.01) and y == pytest.approx(0.95, abs=0.01)


def test_inconsistent_readings_keep_the_previous_calibration(tmp_path):
    (tmp_path / "calibration.json").write_text(json.dumps({"w": 120, "h": 250, "seen": {"x": [], "y": []}}))
    fake = FakePhone(shifted=lambda x, y: (x, y - 0.3) if y > 0.5 else (x, y))  # the lower half is read wrong
    phone = make(tmp_path, fake)
    with pytest.raises(RuntimeError):
        asyncio.run(phone.start_calibration())
    assert phone.span == {"w": 120, "h": 250}
    assert json.loads((tmp_path / "calibration.json").read_text())["w"] == 120


@pytest.mark.parametrize("target", [(0.898, 0.088), (0.95, 0.5), (0.1, 0.95), (0.5, 0.5)])
def test_precise_tap_lands_on_target_after_drift(tmp_path, target):
    fake = FakePhone()
    phone = make(tmp_path, fake)
    assert asyncio.run(phone.start_calibration()) is True
    fake.offset = (fake.offset[0] + 0.05, fake.offset[1])  # the pointer drifts 5% right of where the server thinks
    asyncio.run(phone.point(*target, aim=True))
    x, y = fake.at()
    assert x == pytest.approx(target[0], abs=0.01) and y == pytest.approx(target[1], abs=0.01)
