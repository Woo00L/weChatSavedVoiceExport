import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from favorite_tool.bridge import BridgeConnectionError
from favorite_tool.common import Cancelled
from favorite_tool.service import export_favorites, select_message, write_reports
from test_audio import wav_bytes


def record(i):
    return {"key": str(i).zfill(24), "favorite_id": str(i), "source_id": str(6023526931331132190 + i),
            "session": "other", "sender": "other", "recipient": "self", "data_id": str(i),
            "source_time": 1, "duration_ms": 100, "size": 100, "md5": "", "status": "待导出", "file": ""}


class FakeBridge:
    calls = []
    account = None

    def __init__(self, port, cancel=None):
        self.cancel = cancel

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def state(self):
        return {"db_path": str(self.account), "wxid": "self"}

    def session_names(self):
        return {"other"}

    def voice_messages(self, session, ids):
        return [{"source_id": value, "local_id": 161, "create_time": i + 1, "duration_ms": 100, "size": 100}
                for i, value in enumerate(ids)]

    def voice_data(self, session, local_id, create_time, source_id):
        self.calls.append((local_id, create_time, source_id))
        return wav_bytes()


class ServiceTests(unittest.TestCase):
    def test_duplicate_local_ids_use_exact_server_ids(self):
        wanted = record(1)
        candidates = [{"source_id": record(i)["source_id"], "local_id": 161, "create_time": i + 1} for i in (1, 2)]
        message, error = select_message(wanted, candidates)
        self.assertIsNone(error)
        self.assertEqual(message["source_id"], wanted["source_id"])

    def test_export_resume_and_cancel_preserve_trusted_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            account, output = Path(tmp) / "account", Path(tmp) / "output"
            account.mkdir()
            FakeBridge.account, FakeBridge.calls = account, []
            scan = {"account_dir": str(account), "wxid": "self", "records": [record(1), record(2)]}
            with patch("favorite_tool.service.WeFlowBridge", FakeBridge), patch("favorite_tool.service.resolve_account", return_value=account):
                result = export_favorites(scan, str(output))
                self.assertEqual(result["exported"], 2)
                self.assertEqual(FakeBridge.calls[0][2], record(1)["source_id"])
                cancel = threading.Event()
                def stop_after_first(event):
                    if event["kind"] == "record":
                        cancel.set()
                result = export_favorites(scan, str(output), emit=stop_after_first, cancel=cancel)
                self.assertEqual(result["skipped"], 1)
                self.assertEqual(result["cancelled"], 1)
                result = export_favorites(scan, str(output))
                self.assertEqual(result["skipped"], 2)
                self.assertEqual(result["failed"], 0)
                self.assertEqual(len(FakeBridge.calls), 2)
                self.assertEqual(scan["records"][0]["status"], "待导出")

    def test_changed_existing_audio_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            account, output = Path(tmp) / "account", Path(tmp) / "output"
            account.mkdir()
            FakeBridge.account = account
            scan = {"account_dir": str(account), "wxid": "self", "records": [record(1)]}
            with patch("favorite_tool.service.WeFlowBridge", FakeBridge), patch("favorite_tool.service.resolve_account", return_value=account):
                export_favorites(scan, str(output))
                destination = next((output / "收藏语音").glob("*.wav"))
                destination.write_bytes(b"user-content")
                result = export_favorites(scan, str(output))
                self.assertEqual(result["failed"], 1)
                self.assertEqual(destination.read_bytes(), b"user-content")

    def test_trusted_existing_audio_skips_even_if_source_is_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            account, output = Path(tmp) / "account", Path(tmp) / "output"
            account.mkdir()
            FakeBridge.account = account
            scan = {"account_dir": str(account), "wxid": "self", "records": [record(1)]}
            with patch("favorite_tool.service.WeFlowBridge", FakeBridge), patch("favorite_tool.service.resolve_account", return_value=account):
                self.assertEqual(export_favorites(scan, str(output))["exported"], 1)
            with patch("favorite_tool.service.WeFlowBridge", side_effect=AssertionError("must not query deleted source")):
                result = export_favorites(scan, str(output))
                self.assertEqual(result["skipped"], 1)
                self.assertEqual(result["missing"], 0)

    def test_cancel_interrupts_missing_session_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            account, output = Path(tmp) / "account", Path(tmp) / "output"
            account.mkdir()
            FakeBridge.account = account
            scan = {"account_dir": str(account), "wxid": "self", "records": [record(i) for i in (1, 2, 3)]}
            cancel = threading.Event()

            def stop_after_first(event):
                if event["kind"] == "record":
                    cancel.set()

            with patch("favorite_tool.service.WeFlowBridge", FakeBridge), \
                    patch("favorite_tool.service.resolve_account", return_value=account), \
                    patch.object(FakeBridge, "session_names", return_value=set()):
                result = export_favorites(scan, str(output), emit=stop_after_first, cancel=cancel)
            self.assertEqual(result["missing"], 1)
            self.assertEqual(result["cancelled"], 2)
            self.assertFalse((output / ".收藏导出进行中.lock").exists())

    def test_report_error_does_not_reclassify_published_audio(self):
        for resume in (False, True):
            with self.subTest(resume=resume), tempfile.TemporaryDirectory() as tmp:
                account, output = Path(tmp) / "account", Path(tmp) / "output"
                account.mkdir()
                FakeBridge.account = account
                scan = {"account_dir": str(account), "wxid": "self", "records": [record(1), record(2)]}
                with patch("favorite_tool.service.WeFlowBridge", FakeBridge), \
                        patch("favorite_tool.service.resolve_account", return_value=account):
                    if resume:
                        export_favorites(scan, str(output))
                    attempts = 0

                    def fail_first_report(*args, **kwargs):
                        nonlocal attempts
                        attempts += 1
                        if attempts == 1:
                            raise OSError("temporary report error")
                        return write_reports(*args, **kwargs)

                    with patch("favorite_tool.service.write_reports", side_effect=fail_first_report):
                        with self.assertRaises(OSError):
                            export_favorites(scan, str(output))
                    manifest = json.loads((output / "收藏语音导出结果.json").read_text(encoding="utf-8"))
                    self.assertEqual(manifest["records"][0]["status"], "已验证跳过" if resume else "已导出")
                    self.assertEqual(manifest["records"][1]["status"], "未完成")
                    self.assertTrue(Path(manifest["records"][0]["file"]).is_file())
                    self.assertFalse((output / ".收藏导出进行中.lock").exists())
                    result = export_favorites(scan, str(output))
                    self.assertEqual(result["failed"], 0)
                    self.assertEqual(result["skipped"], 2 if resume else 1)

    def test_connection_loss_stops_remaining_sessions(self):
        class DisconnectingBridge(FakeBridge):
            queried = []

            def session_names(self):
                return {"session1", "session2", "session3"}

            def voice_messages(self, session, ids):
                self.queried.append(session)
                if session == "session2":
                    raise BridgeConnectionError("WeFlow 连接已中断。")
                return super().voice_messages(session, ids)

        with tempfile.TemporaryDirectory() as tmp:
            account, output = Path(tmp) / "account", Path(tmp) / "output"
            account.mkdir()
            DisconnectingBridge.account = account
            DisconnectingBridge.queried = []
            records = [record(i) for i in (1, 2, 3)]
            for i, item in enumerate(records, 1):
                item["session"] = f"session{i}"
            scan = {"account_dir": str(account), "wxid": "self", "records": records}
            with patch("favorite_tool.service.WeFlowBridge", DisconnectingBridge), \
                    patch("favorite_tool.service.resolve_account", return_value=account):
                with self.assertRaises(BridgeConnectionError):
                    export_favorites(scan, str(output))
            self.assertEqual(DisconnectingBridge.queried, ["session1", "session2"])
            manifest = json.loads((output / "收藏语音导出结果.json").read_text(encoding="utf-8"))
            self.assertEqual([item["status"] for item in manifest["records"]], ["已导出", "未完成", "未完成"])


if __name__ == "__main__":
    unittest.main()
