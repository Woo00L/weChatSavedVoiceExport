import hashlib
import hmac
import os
import struct
import tempfile
import unittest
import sqlite3
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from favorite_tool.common import UserError
from favorite_tool.database import SQLITE_HEADER, _checksum, _mac_key, committed_wal_pages, decrypt_database, parse_voice_rows, read_favorites


class FavoriteTests(unittest.TestCase):
    def test_plain_database_handles_close_before_temp_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            account = Path(temp)
            database = account / "db_storage/favorite/favorite.db"
            database.parent.mkdir(parents=True)
            connection = sqlite3.connect(database)
            try:
                connection.execute("-- 构造最小测试收藏表\n CREATE TABLE fav_db_item(local_id,server_id,type,content)")
                connection.execute("-- 插入一条测试语音\n INSERT INTO fav_db_item VALUES (1,2,3,?)", ('<favitem><dataitem datatype="3" /></favitem>',))
                connection.commit()
            finally:
                connection.close()
            self.assertEqual(len(read_favorites(account, None, "self")), 1)

    def test_outgoing_and_incoming_use_other_party(self):
        xml = '<favitem><source><fromusr>self</fromusr><tousr>other</tousr><msgid>6023526931331132190</msgid></source><datalist><dataitem datatype="3" dataid="abc"><duration>6200</duration><fullsize>10162</fullsize></dataitem></datalist></favitem>'
        row = parse_voice_rows([(1, 2, 3, xml)], "self")[0]
        self.assertEqual(row["session"], "other")
        self.assertEqual(row["source_id"], "6023526931331132190")
        self.assertEqual(row["duration_ms"], 6200)

    def test_multiple_voices_have_distinct_keys_and_not_parent_ids(self):
        xml = '<favitem><source><msgid>123</msgid></source><dataitem datatype="3" dataid="a" datasourceid="456"/><dataitem datatype="3" dataid="b"/></favitem>'
        rows = parse_voice_rows([(1, 2, 14, xml)], "self")
        self.assertEqual([r["source_id"] for r in rows], ["456", ""])
        self.assertNotEqual(rows[0]["key"], rows[1]["key"])

    def test_group_session_is_retained(self):
        xml = '<favitem><source><fromusr>person</fromusr><tousr>123@chatroom</tousr><msgid>123</msgid></source><dataitem datatype="3"/></favitem>'
        self.assertEqual(parse_voice_rows([(1, 2, 3, xml)], "self")[0]["session"], "123@chatroom")


def encrypted_page(clear, number, key, salt):
    start = 16 if number == 1 else 0
    iv = os.urandom(16)
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    body = cipher.update(clear[start:4016]) + cipher.finalize()
    mac = hmac.new(_mac_key(key, salt, "sha512"), body + iv + struct.pack("<I", number), "sha512").digest()
    return (salt if number == 1 else b"") + body + iv + mac


def wal_data(pages, size=4096):
    salts = os.urandom(8)
    header = struct.pack(">IIII", 0x377F0682, 3007000, size, 0) + salts
    checksum = _checksum(header, "<")
    out = header + struct.pack(">II", *checksum)
    for number, commit, page in pages:
        prefix = struct.pack(">II", number, commit)
        checksum = _checksum(prefix + page, "<", checksum)
        out += prefix + salts + struct.pack(">II", *checksum) + page
    return out


class CipherTests(unittest.TestCase):
    def test_authenticated_decryption_and_committed_wal_growth(self):
        key, salt = os.urandom(32), os.urandom(16)
        page = bytearray(4096)
        page[:16] = SQLITE_HEADER
        page[16:24] = b"\x10\x00\x02\x02\x50\x40\x20\x20"
        second = b"q" * 4016 + bytes(80)
        second_page = encrypted_page(second, 2, key, salt)
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp) / "favorite.db", Path(tmp) / "clear.db"
            source.write_bytes(encrypted_page(page, 1, key, salt))
            Path(str(source) + "-wal").write_bytes(wal_data([(2, 2, second_page)]))
            decrypt_database(source, target, key.hex())
            actual = target.read_bytes()
            self.assertEqual(len(actual), 8192)
            self.assertEqual(actual[4096:], second)
            self.assertEqual(actual[18:20], b"\x01\x01")

    def test_wrong_key_is_rejected(self):
        key, salt = os.urandom(32), os.urandom(16)
        clear = bytearray(4096)
        clear[:16] = SQLITE_HEADER
        clear[16:24] = b"\x10\x00\x02\x02\x50\x40\x20\x20"
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp) / "favorite.db", Path(tmp) / "clear.db"
            source.write_bytes(encrypted_page(clear, 1, key, salt))
            with self.assertRaises(UserError):
                decrypt_database(source, target, os.urandom(32).hex())
            self.assertFalse(target.exists())

    def test_wal_uncommitted_and_torn_tail_ignored(self):
        first, later = b"a" * 4096, b"b" * 4096
        wal = wal_data([(1, 1, first), (1, 0, later)]) + b"torn"
        self.assertEqual(committed_wal_pages(wal, 4096), ([(1, first)], 1))

    def test_corrupt_wal_header_rejected(self):
        wal = bytearray(wal_data([]))
        wal[10] ^= 1
        with self.assertRaises(UserError):
            committed_wal_pages(wal, 4096)


if __name__ == "__main__":
    unittest.main()
