import hashlib
import io
import math
import os
import tempfile
import wave
from pathlib import Path

from .common import UserError, canonical, check_cancel, emit_event


def inspect_wav(value):
    """读取全部 PCM，检测截断而不只相信 WAV 头。"""
    source = io.BytesIO(value) if isinstance(value, bytes) else str(value)
    digest = hashlib.sha256()
    try:
        with wave.open(source, "rb") as audio:
            channels, width, rate, frames, compression, _ = audio.getparams()
            if not (1 <= channels <= 8 and width in (1, 2, 3, 4) and 1000 <= rate <= 384000 and frames > 0 and compression == "NONE"):
                raise UserError("WAV 音频参数不受支持。")
            count = 0
            while data := audio.readframes(65536):
                count += len(data)
                digest.update(data)
            if count != frames * channels * width:
                raise UserError("WAV 音频数据不完整，可能被截断。")
            return {"channels": channels, "width": width, "rate": rate, "frames": frames,
                    "duration_ms": frames / rate * 1000, "pcm_sha256": digest.hexdigest()}
    except (wave.Error, EOFError) as exc:
        raise UserError("不是可读取的未压缩 WAV 文件。") from exc


def verify_duration(info, duration_ms):
    if duration_ms and abs(info["duration_ms"] - duration_ms) > max(1500, duration_ms * .25):
        raise UserError(f"音频时长与收藏不符（实际 {info['duration_ms']/1000:.2f} 秒，收藏 {duration_ms/1000:.2f} 秒）。")


def publish_new(temp, destination):
    """发布完整文件；绝不替换并发创建的同名目标。"""
    try:
        os.link(temp, destination)
    except FileExistsError as exc:
        raise UserError("输出文件已经存在，请改用新文件名或新目录。") from exc
    except OSError:
        if os.name != "nt":
            raise
        try:
            os.rename(temp, destination)
        except FileExistsError as exc:
            raise UserError("输出文件已经存在，请改用新文件名或新目录。") from exc


def write_verified_wav(destination, data, duration_ms=0):
    info = inspect_wav(data)
    verify_duration(info, duration_ms)
    destination = Path(destination)
    if destination.exists():
        raise UserError("同名文件已经存在且尚未验证，未覆盖。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".音频-", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if Path(temp_name).read_bytes() != data:
            raise UserError("音频写入校验失败。")
        publish_new(temp_name, destination)
    finally:
        Path(temp_name).unlink(missing_ok=True)
    return {**info, "sha256": hashlib.sha256(data).hexdigest()}


def list_wav_files(folder):
    folder = Path(folder)
    if not folder.is_dir():
        raise UserError("请选择存在的语音文件夹。")
    return [str(path) for path in sorted(folder.iterdir(), key=lambda path: path.name.casefold())
            if path.is_file() and path.suffix.lower() == ".wav"]


def merge_wavs(paths, output_path, gap_seconds=.5, emit=None, cancel=None):
    if not paths:
        raise UserError("文件夹中没有 WAV，或尚未选择要合并的文件。")
    try:
        gap_seconds = float(gap_seconds)
    except (TypeError, ValueError) as exc:
        raise UserError("段间静音必须是 0 至 10 秒的数字。") from exc
    if not math.isfinite(gap_seconds) or not 0 <= gap_seconds <= 10:
        raise UserError("段间静音必须在 0 至 10 秒之间。")
    check_cancel(cancel)
    files = [Path(path).resolve() for path in paths]
    target = Path(output_path).resolve()
    if target.suffix.lower() != ".wav":
        raise UserError("合并文件名必须以 .wav 结尾。")
    if canonical(target) in {canonical(path) for path in files}:
        raise UserError("合并输出不能与任何输入文件相同。")
    if target.exists():
        raise UserError("合并文件已经存在，请选择新文件名。")
    signatures = [(p.stat().st_size, p.stat().st_mtime_ns) for p in files]
    metadata = []
    for i, path in enumerate(files):
        check_cancel(cancel)
        info = inspect_wav(path)
        metadata.append(info)
        emit_event(emit, "progress", current=i + 1, total=len(files), message="检查合并输入")
    params = tuple(metadata[0][key] for key in ("channels", "width", "rate"))
    if any(tuple(info[key] for key in ("channels", "width", "rate")) != params for info in metadata):
        raise UserError("文件的采样率、声道或位深不一致，未合并。请先转换成一致的 WAV 格式。")
    channels, width, rate = params
    gap_frames = round(gap_seconds * rate)
    gap = (b"\x80" if width == 1 else b"\0") * gap_frames * channels * width
    frames = sum(info["frames"] for info in metadata) + gap_frames * (len(files) - 1)
    if frames * channels * width + 44 >= 0xFFFFFFFF:
        raise UserError("合并结果超过 WAV 的 4 GB 上限，请分批合并。")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".合并-", suffix=".wav.tmp", dir=target.parent)
    os.close(fd)
    digest = hashlib.sha256()
    try:
        with wave.open(temp_name, "wb") as merged:
            merged.setnchannels(channels)
            merged.setsampwidth(width)
            merged.setframerate(rate)
            for index, path in enumerate(files):
                check_cancel(cancel)
                if (path.stat().st_size, path.stat().st_mtime_ns) != signatures[index]:
                    raise UserError("合并期间输入文件发生变化，请刷新文件列表后重试。")
                if index:
                    merged.writeframesraw(gap)
                    digest.update(gap)
                source_hash = hashlib.sha256()
                with wave.open(str(path), "rb") as audio:
                    while data := audio.readframes(65536):
                        check_cancel(cancel)
                        merged.writeframesraw(data)
                        digest.update(data)
                        source_hash.update(data)
                if source_hash.hexdigest() != metadata[index]["pcm_sha256"]:
                    raise UserError("输入音频在合并期间改变，已停止。")
                emit_event(emit, "progress", current=index + 1, total=len(files), message="合并音频")
        check_cancel(cancel)
        verified = inspect_wav(temp_name)
        if verified["frames"] != frames or verified["pcm_sha256"] != digest.hexdigest():
            raise UserError("合并音频完整性校验失败。")
        check_cancel(cancel)
        publish_new(temp_name, target)
        emit_event(emit, "log", message=f"合并完成：{len(files)} 个文件，时长 {frames/rate:.2f} 秒。")
        return {"files": len(files), "duration_seconds": frames / rate, "path": str(target)}
    finally:
        Path(temp_name).unlink(missing_ok=True)
