import hashlib
import hmac
import os
import re
import shutil
import sqlite3
import struct
import tempfile
import time
import xml.etree.ElementTree as ET
from contextlib import closing
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .common import UserError, canonical, check_cancel, numeric_id

SQLITE_HEADER = b"SQLite format 3\0"


def find_favorite_db(account):
    account = Path(account).expanduser().resolve()
    choices = [account / "db_storage/favorite/favorite.db", account / "favorite/favorite.db"]
    for candidate in choices:
        if candidate.is_file():
            return candidate
    raise UserError("此目录下没有找到收藏数据库。请选择包含 db_storage 的微信账号目录。")


def _signature(path):
    if not path.exists():
        return None
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def stable_copy(source, folder, cancel=None):
    source = Path(source)
    paths = [source, Path(str(source) + "-wal")]
    for _ in range(3):
        check_cancel(cancel)
        before = [_signature(p) for p in paths]
        for path, exists in zip(paths, before):
            destination = Path(folder) / path.name
            if exists is not None:
                shutil.copyfile(path, destination)
            else:
                destination.unlink(missing_ok=True)
        after = [_signature(p) for p in paths]
        if before == after:
            return Path(folder) / source.name
        time.sleep(.15)
    raise UserError("收藏数据库正在变化。请先退出微信后重新扫描。")


def _checksum(data, order, initial=(0, 0)):
    if len(data) % 8:
        raise UserError("数据库日志长度无效。")
    first, second = initial
    for left, right in struct.iter_unpack(order + "II", data):
        first = (first + left + second) & 0xFFFFFFFF
        second = (second + right + first) & 0xFFFFFFFF
    return first, second


def committed_wal_pages(wal, page_size):
    """校验 SQLite WAL，仅返回最后一个已提交事务之前的页。"""
    if not wal:
        return [], None
    if len(wal) < 32:
        raise UserError("收藏日志头不完整，请退出微信后重试。")
    magic, version, stored_size = struct.unpack_from(">III", wal)
    if magic not in (0x377F0682, 0x377F0683) or stored_size != page_size or version != 3007000:
        raise UserError("收藏日志格式不受支持。")
    order = "<" if magic == 0x377F0682 else ">"
    checksum = _checksum(wal[:24], order)
    if checksum != struct.unpack_from(">II", wal, 24):
        raise UserError("收藏日志头校验失败。")
    valid, committed_count, database_pages = [], 0, None
    for offset in range(32, len(wal) - page_size - 23, page_size + 24):
        header = wal[offset:offset + 24]
        page_number, commit_size = struct.unpack_from(">II", header)
        if page_number == 0 or header[8:16] != wal[16:24]:
            break
        data = wal[offset + 24:offset + 24 + page_size]
        candidate = _checksum(header[:8] + data, order, checksum)
        if candidate != struct.unpack_from(">II", header, 16):
            break
        checksum = candidate
        valid.append((page_number, data))
        if commit_size:
            if commit_size > 1_000_000:
                raise UserError("收藏日志声明的数据规模异常。")
            committed_count, database_pages = len(valid), commit_size
    return valid[:committed_count], database_pages


def _aes_decrypt(data, key, iv):
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return decryptor.update(data) + decryptor.finalize()


def _mac_key(key, salt, digest):
    return hashlib.pbkdf2_hmac(digest, key, bytes(b ^ 0x3A for b in salt), 2, 32)


def _decode_page(page, number, key, page_size, reserve, mac_key, digest, validate=True):
    start = 16 if number == 1 else 0
    end = page_size - reserve
    iv = page[end:end + 16]
    if validate:
        stored = page[end + 16:end + 16 + hashlib.new(digest).digest_size]
        expected = hmac.new(mac_key, page[start:end + 16] + struct.pack("<I", number), digest).digest()
        if not hmac.compare_digest(stored, expected):
            raise UserError("收藏数据库页校验失败，密钥不匹配或文件已改变。")
    clear = _aes_decrypt(page[start:end], key, iv)
    return (SQLITE_HEADER if number == 1 else b"") + clear + bytes(reserve)


def decrypt_database(source, destination, key_text, cancel=None):
    data = Path(source).read_bytes()
    if len(data) < 1024:
        raise UserError("收藏数据库过小或不完整。")
    if data.startswith(SQLITE_HEADER):
        # 只打开临时快照，SQLite backup 会合并快照中正常的 WAL。
        with closing(sqlite3.connect(str(source))) as src, closing(sqlite3.connect(str(destination))) as dst:
            src.backup(dst)
        return
    key_text = str(key_text or "").strip()
    if not key_text:
        raise UserError("WeFlow 未提供可用数据库密钥，请在 WeFlow 中解锁并连接账号。")
    raw_text = re.sub(r"^x'([0-9a-fA-F]{64})'$", r"\1", key_text)
    keys = [bytes.fromhex(raw_text)] if re.fullmatch(r"[0-9a-fA-F]{64}", raw_text) else []
    keys.append(key_text.encode())
    salt = data[:16]
    selected = None
    for raw in keys:
        candidates = []
        if len(raw) == 32:
            candidates.append(raw)
        for count, digest in ((256000, "sha512"), (64000, "sha1"), (64000, "sha512"), (4000, "sha1")):
            check_cancel(cancel)
            candidates.append(hashlib.pbkdf2_hmac(digest, raw, salt, count, 32))
        for key in candidates:
            for page_size in (4096, 8192, 1024, 2048):
                if len(data) % page_size:
                    continue
                for reserve, digest in ((80, "sha512"), (48, "sha1")):
                    mac_key = _mac_key(key, salt, digest)
                    try:
                        clear = _decode_page(data[:page_size], 1, key, page_size, reserve, mac_key, digest)
                    except (UserError, ValueError):
                        continue
                    if clear[16:18] == struct.pack(">H", page_size) and clear[20] == reserve and clear[21:24] == b"\x40\x20\x20":
                        selected = key, page_size, reserve, mac_key, digest
                        break
                if selected:
                    break
            if selected:
                break
        if selected:
            break
    if not selected:
        raise UserError("收藏数据库密钥或加密格式不匹配。请确认 WeFlow 当前账号与选择的目录一致。")
    key, size, reserve, mac_key, digest = selected
    wal_path = Path(str(source) + "-wal")
    wal_pages, committed_size = committed_wal_pages(wal_path.read_bytes() if wal_path.exists() else b"", size)
    pages = committed_size or len(data) // size
    output = bytearray(pages * size)
    latest = dict(wal_pages)
    for number in range(1, pages + 1):
        check_cancel(cancel)
        page = latest.get(number, data[(number - 1) * size:number * size])
        if len(page) != size:
            raise UserError("已提交收藏日志缺少所需数据页。")
        output[(number - 1) * size:number * size] = _decode_page(page, number, key, size, reserve, mac_key, digest)
    # 临时明文副本无 WAL；禁用该副本的 WAL 模式，原始数据库保持不变。
    output[18:20] = b"\x01\x01"
    Path(destination).write_bytes(output)


def _number(text):
    try:
        return max(0, int(text or 0))
    except (ValueError, TypeError):
        return 0


def parse_voice_rows(rows, wxid):
    records = []
    for favorite_id, server_id, favorite_type, content in rows:
        try:
            root = ET.fromstring(content)
        except (ET.ParseError, TypeError):
            continue
        root_source = root.find("source")
        for index, item in enumerate(root.findall(".//dataitem")):
            if item.get("datatype") != "3":
                continue
            own_source = item.find("dataitemsource")
            source = own_source if own_source is not None else root_source if int(favorite_type) == 3 else None
            values = {child.tag: child.text or "" for child in source} if source is not None else {}
            voice = {child.tag: child.text or "" for child in item}
            source_id = (numeric_id(values.get("msgid")) or numeric_id(values.get("sourceid"))
                         or numeric_id(item.get("datasourceid")))
            sender = values.get("fromusr", "")
            recipient = values.get("tousr", "")
            chatroom = values.get("realchatname", "")
            session = next((value for value in (chatroom, sender, recipient) if value.endswith("@chatroom")), "")
            if not session:
                session = recipient if sender == wxid else sender
            data_id = item.get("dataid", "")
            key = hashlib.sha256(f"{favorite_id}:{data_id}:{index}".encode()).hexdigest()[:24]
            records.append({"key": key, "favorite_id": str(favorite_id), "favorite_server_id": str(server_id),
                            "source_id": source_id, "sender": sender, "recipient": recipient,
                            "source_time": _number(values.get("createtime")), "duration_ms": _number(voice.get("duration")),
                            "size": _number(voice.get("fullsize")), "md5": voice.get("fullmd5", ""),
                            "data_id": data_id, "session": session, "status": "待导出", "file": ""})
    return records


def read_favorites(account, key, wxid, cancel=None):
    source = find_favorite_db(account)
    with tempfile.TemporaryDirectory(prefix="微信收藏扫描-") as temp:
        snapshot = stable_copy(source, temp, cancel)
        clear = Path(temp) / "favorite-clear.db"
        decrypt_database(snapshot, clear, key, cancel)
        key = None
        try:
            with closing(sqlite3.connect(clear.as_uri() + "?mode=ro", uri=True)) as connection:
                connection.execute("-- 禁止意外写入临时数据库\n PRAGMA query_only=ON")
                check = connection.execute("-- 完整性校验\n PRAGMA integrity_check").fetchall()
                if check != [("ok",)]:
                    raise UserError("收藏数据库完整性校验失败，未继续导出。")
                rows = connection.execute("-- 只读获取收藏元数据，不读取其他聊天表\n SELECT local_id, server_id, type, content FROM fav_db_item ORDER BY local_id").fetchall()
        except sqlite3.Error as exc:
            raise UserError("无法读取收藏数据表，可能是数据库格式不兼容。") from exc
        check_cancel(cancel)
        return parse_voice_rows(rows, wxid)
