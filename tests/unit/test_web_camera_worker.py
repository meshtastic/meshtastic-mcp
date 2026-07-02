"""Out-of-process camera worker + the reader that adapts it to MJPEG.

cv2's macOS backend can SIGSEGV the host; capture runs in a child so a crash
kills only the worker. These tests exercise the worker (with a fake cv2 — no real
camera) and the binary frame protocol the server reads, end to end.
"""

from __future__ import annotations

import asyncio
import io
import json
import struct
import sys

from meshtastic_mcp.web import camera_worker as cw
from meshtastic_mcp.web.services import camera_stream as cs


# --- a fake cv2 so the worker runs without a camera --------------------------
class _FakeBuf:
    def __init__(self, b: bytes) -> None:
        self._b = b

    def tobytes(self) -> bytes:
        return self._b


class _FakeCap:
    def __init__(self, frames: list[int]) -> None:
        self._frames = list(frames)
        self._open = True

    def isOpened(self) -> bool:
        return self._open

    def read(self):
        if self._frames:
            return True, self._frames.pop(0)
        return False, None

    def get(self, prop):
        return {3: 640.0, 4: 480.0}.get(prop, 0.0)

    def release(self) -> None:
        self._open = False


class _FakeLogging:
    LOG_LEVEL_SILENT = 0

    @staticmethod
    def setLogLevel(_lvl):
        pass


class _FakeUtils:
    logging = _FakeLogging


class _FakeCv2:
    CAP_PROP_FRAME_WIDTH = 3
    CAP_PROP_FRAME_HEIGHT = 4
    utils = _FakeUtils

    def __init__(self, frames_by_index: dict[int, list[int]]) -> None:
        self._frames = frames_by_index

    def VideoCapture(self, index):
        return _FakeCap(list(self._frames.get(index, [])))

    def imencode(self, _ext, frame):
        return True, _FakeBuf(b"JPG" + bytes([frame]))


def _install_fake_cv2(monkeypatch, frames_by_index):
    monkeypatch.setitem(sys.modules, "cv2", _FakeCv2(frames_by_index))


def test_worker_probe_reports_resolution(monkeypatch, capsys):
    _install_fake_cv2(monkeypatch, {0: [7]})  # one frame available
    rc = cw.main(["x", "probe", "0"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out == {"ok": True, "width": 640, "height": 480}


def test_worker_probe_unopenable(monkeypatch, capsys):
    _install_fake_cv2(monkeypatch, {})  # index 5 has no frames → read fails
    cw.main(["x", "probe", "5"])
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["error"]


def test_worker_probe_many(monkeypatch, capsys):
    _install_fake_cv2(monkeypatch, {0: [1], 2: [2]})
    cw.main(["x", "probe-many", "0,1,2"])
    out = json.loads(capsys.readouterr().out)
    assert out["cv2"] is True
    assert out["results"]["0"]["ok"] is True
    assert out["results"]["1"]["ok"] is False  # nothing on index 1
    assert out["results"]["2"]["ok"] is True


def test_worker_missing_opencv_reports_cv2_false(monkeypatch, capsys):
    # Simulate the [ui] extra not installed: importing cv2 raises.
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "cv2":
            raise ImportError("No module named 'cv2'")
        return real_import(name, *a, **k)

    monkeypatch.delitem(sys.modules, "cv2", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    rc = cw.main(["x", "probe-many", ""])
    out = json.loads(capsys.readouterr().out)
    assert rc == 4 and out["cv2"] is False


def test_worker_stream_emits_framed_jpegs(monkeypatch):
    """_stream writes MAGIC+len+jpeg per frame; reading them back yields the
    exact payloads — the contract the server's reader relies on."""
    _install_fake_cv2(monkeypatch, {0: [10, 20, 30]})
    buf = io.BytesIO()

    class _Out:
        buffer = buf

    monkeypatch.setattr(sys, "stdout", _Out())
    monkeypatch.setattr(cw, "FPS", 1000.0)  # don't sleep in the test
    rc = cw._stream(_FakeCv2({0: [10, 20, 30]}), 0)
    assert rc == 0

    payloads = asyncio.run(_decode(buf.getvalue()))
    assert payloads == [b"JPG\x0a", b"JPG\x14", b"JPG\x1e"]


async def _decode(data: bytes) -> list[bytes]:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return [f async for f in cs._read_frames(reader)]


def test_read_frames_stops_on_truncation():
    # A header promising 100 bytes but only 3 present → clean stop, no hang/garbage.
    data = cs.MAGIC + struct.pack(">I", 100) + b"abc"
    assert asyncio.run(_decode(data)) == []


def test_read_frames_stops_on_desync():
    data = b"XXXX" + struct.pack(">I", 3) + b"abc"  # wrong magic
    assert asyncio.run(_decode(data)) == []
