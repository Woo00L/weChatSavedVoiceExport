import io
import tempfile
import threading
import unittest
import wave
from pathlib import Path

from favorite_tool.audio import inspect_wav, merge_wavs
from favorite_tool.common import Cancelled, UserError, numeric_id


def wav_bytes(frames=2400, rate=24000, value=b"\x01\x00"):
    out = io.BytesIO()
    with wave.open(out, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(value * frames)
    return out.getvalue()


class AudioTests(unittest.TestCase):
    def test_duration_uses_sample_rate_not_byte_rate(self):
        self.assertEqual(inspect_wav(wav_bytes(148800))["duration_ms"], 6200)

    def test_truncated_audio_is_rejected(self):
        with self.assertRaises(UserError):
            inspect_wav(wav_bytes()[:-4])

    def test_server_id_never_uses_rounded_float(self):
        self.assertEqual(numeric_id("6023526931331132190"), "6023526931331132190")
        self.assertEqual(numeric_id(float(6023526931331132190)), "")

    def test_merge_keeps_exact_pcm_and_silence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a, b, out = (root / name for name in ("a.wav", "b.wav", "out.wav"))
            a.write_bytes(wav_bytes(2400))
            b.write_bytes(wav_bytes(4800, value=b"\x02\x00"))
            result = merge_wavs([str(b), str(a)], str(out), 0.5)
            self.assertAlmostEqual(result["duration_seconds"], 0.8)
            with wave.open(str(out), "rb") as audio:
                self.assertEqual(audio.readframes(99999), b"\x02\x00" * 4800 + bytes(24000) + b"\x01\x00" * 2400)

    def test_existing_output_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a.wav"
            out = Path(tmp) / "out.wav"
            a.write_bytes(wav_bytes())
            out.write_bytes(b"original")
            with self.assertRaises(UserError):
                merge_wavs([str(a)], str(out), 0)
            self.assertEqual(out.read_bytes(), b"original")

    def test_cancel_leaves_no_completed_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a.wav"
            out = Path(tmp) / "out.wav"
            a.write_bytes(wav_bytes())
            cancel = threading.Event()
            cancel.set()
            with self.assertRaises(Cancelled):
                merge_wavs([str(a)], str(out), 0, cancel=cancel)
            self.assertFalse(out.exists())

    def test_incompatible_parameters_do_not_leave_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b, out = (Path(tmp) / name for name in ("a.wav", "b.wav", "out.wav"))
            a.write_bytes(wav_bytes())
            b.write_bytes(wav_bytes(rate=16000))
            with self.assertRaises(UserError):
                merge_wavs([str(a), str(b)], str(out), .5)
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
