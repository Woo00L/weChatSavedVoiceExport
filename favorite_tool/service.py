import copy
import csv
import hashlib
import io
import json
import os
import re
import threading
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from .audio import inspect_wav, list_wav_files, merge_wavs, verify_duration, write_verified_wav
from .bridge import BridgeConnectionError, WeFlowBridge, launch_weflow
from .common import Cancelled, UserError, atomic_write, canonical, check_cancel, emit_event, file_hash, safe_error, safe_name, save_json
from .database import find_favorite_db, read_favorites


def clean_wxid(value):
    return re.sub(r"_[0-9a-fA-F]{4}$", "", value) if value.startswith("wxid_") and value.count("_") >= 2 else value


def resolve_account(db_path, wxid):
    root = Path(db_path).expanduser().resolve()
    candidates = [root, root.parent] if root.name == "db_storage" else [root]
    if root.is_dir():
        candidates += [p for p in root.iterdir() if p.is_dir() and (p.name == wxid or clean_wxid(p.name) == clean_wxid(wxid))]
    valid = []
    for candidate in candidates:
        try:
            find_favorite_db(candidate)
        except UserError:
            continue
        if canonical(candidate) not in {canonical(p) for p in valid}:
            valid.append(candidate)
    if len(valid) != 1:
        raise UserError("无法从 WeFlow 设置唯一确定账号目录，请检查 WeFlow 数据目录设置。")
    return valid[0]


def default_paths():
    personal = Path.home() / "Documents"
    exe_candidates = [Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Programs/WeFlow/WeFlow.exe"]
    for base in (os.environ.get("ProgramFiles", "C:/Program Files"), os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")):
        exe_candidates.append(Path(base) / "WeFlow/WeFlow.exe")
    for drive in ("E:/", "D:/", "C:/"):
        exe_candidates += [Path(drive) / "dev/weFlow/WeFlow.exe", Path(drive) / "WeFlow/WeFlow.exe"]
    account_dirs = []
    for root in [personal / "xwechat_files", *(Path(drive) / "xwechat_files" for drive in ("E:/", "D:/", "C:/"))]:
        if root.is_dir():
            for path in root.iterdir():
                if path.is_dir() and (path / "db_storage/favorite/favorite.db").is_file():
                    account_dirs.append(path)
    account = str(account_dirs[0]) if len(account_dirs) == 1 else ""
    return {"weflow_exe": next((str(p) for p in exe_candidates if p.is_file()), ""),
            "account_dir": account, "output_dir": str(personal / "微信收藏语音工具导出")}


def connect_weflow(executable, port=9229, launch=False):
    if launch:
        launch_weflow(executable, port)
    with WeFlowBridge(port) as bridge:
        state = bridge.state()
    account = resolve_account(state["db_path"], state["wxid"])
    return {"connected": True, "wxid": clean_wxid(state["wxid"]), "account_dir": str(account),
            "message": "已连接 WeFlow，账号与本地目录可用。"}


def scan_favorites(account_dir, port=9229, emit=None, cancel=None):
    check_cancel(cancel)
    emit_event(emit, "log", message="连接 WeFlow 并检查当前账号。")
    with WeFlowBridge(port, cancel) as bridge:
        state = bridge.state(include_key=True)
    account = resolve_account(state["db_path"], state["wxid"])
    if canonical(account_dir) != canonical(account):
        state.pop("key", None)
        raise UserError("所选账号目录与 WeFlow 当前账号不一致。请切换 WeFlow 账号，或使用检测连接得到的目录。")
    wxid = clean_wxid(state["wxid"])
    emit_event(emit, "log", message="读取当前收藏快照并校验数据库。")
    key = state.pop("key", None)
    try:
        records = read_favorites(account, key, wxid, cancel)
    finally:
        key = None
    result = {"account_dir": str(account), "wxid": wxid, "source_db": str(find_favorite_db(account)),
              "scanned_at": datetime.now().isoformat(timespec="seconds"), "records": records}
    emit_event(emit, "log", message=f"扫描完成：发现 {len(records)} 条收藏语音。")
    return result


def select_message(record, candidates):
    exact = [m for m in candidates if isinstance(m.get("source_id"), str) and m["source_id"] == record["source_id"]]
    unique = {(m.get("local_id"), m.get("create_time"), m.get("duration_ms"), m.get("size")): m for m in exact}
    if not unique:
        return None, "本地聊天中无精确对应的源语音"
    if len(unique) != 1:
        return None, "源消息匹配不唯一，未冒险选择"
    message = next(iter(unique.values()))
    if not isinstance(message.get("local_id"), int) or not isinstance(message.get("create_time"), int) or message["create_time"] <= 0:
        return None, "源语音消息定位信息不完整"
    if message.get("size") and record.get("size") and message["size"] != record["size"]:
        return None, "源语音大小与收藏记录不符"
    if message.get("duration_ms") and record.get("duration_ms") and abs(message["duration_ms"] - record["duration_ms"]) > 1000:
        return None, "源语音时长与收藏记录不符"
    return message, None


def _record_identity(record):
    return {field: record.get(field) for field in ("key", "source_id", "session", "duration_ms", "size", "md5", "data_id")}


def _csv_value(value):
    value = str(value or "")
    if value.startswith(("=", "+", "-", "@", "\t", "\r", "\n")) or (value.isdigit() and len(value) > 15):
        return "'" + value
    return value


def write_reports(output, scan, records, verified=None):
    report = {"schema": 1, "account_dir": canonical(scan["account_dir"]), "wxid": scan["wxid"],
              "updated_at": datetime.now().isoformat(timespec="seconds"), "records": records,
              "verified_records": list((verified or {}).values())}
    save_json(output / "收藏语音导出结果.json", report)
    columns = [("收藏ID", "favorite_id"), ("源消息ID", "source_id"), ("源会话", "session"),
               ("时长毫秒", "duration_ms"), ("原始字节数", "size"), ("原始MD5", "md5"),
               ("状态", "status"), ("原因", "reason"), ("导出文件", "file"), ("WAV校验", "sha256")]
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow([label for label, _ in columns])
    for record in records:
        writer.writerow([_csv_value(record.get(field)) for _, field in columns])
    atomic_write(output / "收藏语音清单.csv", buffer.getvalue().encode("utf-8-sig"))


def export_favorites(scan, output_dir, port=9229, emit=None, cancel=None):
    cancel = cancel or threading.Event()
    check_cancel(cancel)
    if not scan or not isinstance(scan.get("records"), list) or not scan["records"]:
        raise UserError("请先扫描收藏，确认有语音记录。")
    output = Path(output_dir).expanduser().resolve()
    account = Path(scan["account_dir"]).resolve()
    if output == account or account in output.parents:
        raise UserError("导出目录不能位于微信账号原始数据目录内部。")
    output.mkdir(parents=True, exist_ok=True)
    # 防止同一输出目录上的并发任务交错覆盖结果清单。
    lock_path = output / ".收藏导出进行中.lock"
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise UserError("此目录存在进行中的导出标记。请先结束另一任务；若上次异常退出，请确认无任务运行后删除 .收藏导出进行中.lock。") from exc
    os.close(lock_fd)
    records = copy.deepcopy(scan["records"])
    previous = {}
    try:
        manifest_path = output / "收藏语音导出结果.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                raise UserError("已有结果清单无法读取，请选择新目录，避免覆盖历史结果。") from exc
            if manifest.get("schema") != 1 or manifest.get("account_dir") != canonical(account) or manifest.get("wxid") != scan["wxid"]:
                raise UserError("此目录属于其他账号或旧版清单，请选择新的导出目录。")
            previous = {r["key"]: r for r in manifest.get("verified_records", manifest.get("records", []))
                        if isinstance(r, dict) and "key" in r and r.get("status") in ("已导出", "已验证跳过")}
        elif (output / "收藏语音清单.csv").exists():
            raise UserError("此目录已有其他格式的收藏清单，请选择新目录保留原结果。")
        for record in records:
            record.update(status="待导出", reason="", file="")
            record.pop("sha256", None)
        folder = output / "收藏语音"
        folder.mkdir(exist_ok=True)
        completed = 0

        def finish(record, status, reason="", **values):
            nonlocal completed
            record.update(status=status, reason=reason, **values)
            completed += 1
            if status in ("已导出", "已验证跳过"):
                previous[record["key"]] = copy.deepcopy(record)
            write_reports(output, scan, records, previous)
            emit_event(emit, "record", record=copy.deepcopy(record))
            emit_event(emit, "progress", current=completed, total=len(records), message=f"{status}：收藏 {record['favorite_id']}")

        try:
            groups = defaultdict(list)
            destinations = {}
            for record in records:
                check_cancel(cancel)
                destination = folder / f"收藏_{safe_name(record['favorite_id']).zfill(4)}_{safe_name(record['source_id'])}_{record['key'][:8]}.wav"
                destinations[record["key"]] = destination
                prior = previous.get(record["key"])
                if destination.exists():
                    try:
                        if (not prior or _record_identity(prior) != _record_identity(record)
                                or canonical(prior.get("file", "")) != canonical(destination)
                                or prior.get("status") not in ("已导出", "已验证跳过")
                                or file_hash(destination) != prior.get("sha256")):
                            raise UserError("同名文件无法从已有清单和哈希验证，未覆盖。")
                        info = inspect_wav(destination)
                        verify_duration(info, record["duration_ms"])
                    except (UserError, OSError) as exc:
                        finish(record, "失败", safe_error(exc))
                    else:
                        finish(record, "已验证跳过", file=str(destination), sha256=prior["sha256"], wav_duration_ms=round(info["duration_ms"]))
                else:
                    groups[record.get("session", "")].append(record)
            if groups:
                _export_pending(groups, destinations, scan, account, bridge_port=port,
                                finish=finish, cancel=cancel)
        except Cancelled:
            for record in records:
                if record["status"] == "待导出":
                    record["status"] = "已取消"
                    emit_event(emit, "record", record=copy.deepcopy(record))
        except Exception:
            for record in records:
                if record["status"] == "待导出":
                    record.update(status="未完成", reason="连接或任务中断，可重新导出继续")
                    emit_event(emit, "record", record=copy.deepcopy(record))
            raise
        finally:
            write_reports(output, scan, records, previous)
        counts = Counter(r["status"] for r in records)
        summary = {"total": len(records), "exported": counts["已导出"], "skipped": counts["已验证跳过"],
                   "missing": counts["本地缺失"], "failed": counts["失败"] + counts["无法匹配"],
                   "cancelled": counts["已取消"], "report_csv": str(output / "收藏语音清单.csv"), "output_dir": str(output)}
        emit_event(emit, "log", message=f"导出结束：新增 {summary['exported']}，已验证跳过 {summary['skipped']}，本地缺失 {summary['missing']}，失败 {summary['failed']}，取消 {summary['cancelled']}。")
        return summary
    finally:
        lock_path.unlink(missing_ok=True)


def _export_pending(groups, destinations, scan, account, bridge_port, finish, cancel):
    with WeFlowBridge(bridge_port, cancel) as bridge:
        state = bridge.state()
        current = resolve_account(state["db_path"], state["wxid"])
        if canonical(current) != canonical(account) or clean_wxid(state["wxid"]) != scan["wxid"]:
            raise UserError("WeFlow 账号已变化，请重新扫描。")
        sessions = bridge.session_names()
        for session, group in groups.items():
            check_cancel(cancel)
            if not session or session not in sessions:
                for record in group:
                    check_cancel(cancel)
                    finish(record, "本地缺失", "本地会话列表中找不到对应源会话")
                continue
            try:
                candidates = bridge.voice_messages(session, [r["source_id"] for r in group if r["source_id"]])
            except Cancelled:
                raise
            except BridgeConnectionError:
                raise
            except UserError as exc:
                for record in group:
                    check_cancel(cancel)
                    finish(record, "失败", safe_error(exc))
                continue
            for record in group:
                check_cancel(cancel)
                if not record["source_id"]:
                    finish(record, "无法匹配", "收藏没有可精确验证的源消息 ID")
                    continue
                message, reason = select_message(record, candidates)
                if message is None:
                    finish(record, "本地缺失" if reason.startswith("本地聊天") else "无法匹配", reason)
                    continue
                destination = destinations[record["key"]]
                try:
                    data = bridge.voice_data(session, message["local_id"], message["create_time"], record["source_id"])
                    check_cancel(cancel)
                    info = write_verified_wav(destination, data, record["duration_ms"])
                except Cancelled:
                    raise
                except BridgeConnectionError:
                    raise
                except Exception as exc:
                    finish(record, "失败", safe_error(exc))
                else:
                    finish(record, "已导出", file=str(destination), sha256=info["sha256"],
                           wav_duration_ms=round(info["duration_ms"]), source_local_id=message["local_id"], source_message_time=message["create_time"])


def merge_audio(paths, output_path, gap_seconds=.5, emit=None, cancel=None):
    return merge_wavs(paths, output_path, gap_seconds, emit, cancel)
