import hashlib
import json
import os
import re
import tempfile
from pathlib import Path


class UserError(Exception):
    """可直接展示的错误；不得包含密钥或完整聊天内容。"""


class Cancelled(UserError):
    pass


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled("任务已取消，已完成的文件保留。")


def emit_event(emit, kind, **values):
    if emit:
        emit({"kind": kind, **values})


def canonical(path):
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".写入-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def save_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while data := stream.read(1024 * 1024):
            digest.update(data)
    return digest.hexdigest()


def safe_name(value):
    return re.sub(r"[^a-zA-Z0-9_-]", "_", str(value))[:100] or "unknown"


def numeric_id(value):
    """避免把浮点数或不完整 ID 当作精确消息身份。"""
    if isinstance(value, float) or isinstance(value, bool):
        return ""
    value = str(value or "").strip()
    if not re.fullmatch(r"-?\d{1,20}", value):
        return ""
    number = int(value)
    if -(1 << 63) <= number < 0:
        number += 1 << 64
    return str(number) if 0 < number < (1 << 64) else ""


def safe_error(error):
    if isinstance(error, UserError):
        return str(error)
    if isinstance(error, PermissionError):
        return "文件无法访问，请检查目录权限或是否被其他程序占用。"
    if isinstance(error, OSError):
        return f"文件或连接操作失败（系统代码 {getattr(error, 'winerror', None) or error.errno or '未知'}）。"
    return f"操作失败（{type(error).__name__}），原文件未修改。"
