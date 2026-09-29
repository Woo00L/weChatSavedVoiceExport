"""微信收藏语音工具的中文桌面界面。"""

from __future__ import annotations

import copy
import ctypes
import json
import math
import os
from pathlib import Path
import queue
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from datetime import datetime
from typing import Any, Callable

from . import service
from .common import UserError, safe_error


APP_NAME = "微信收藏语音工具"
_PATH_SETTINGS = ("weflow_exe", "account_dir", "output_dir", "merge_folder", "merge_output_dir")


def _settings_path() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "WeChatFavoriteVoiceTool" / "settings.json"


def _clean_settings(values: Any) -> dict[str, Any]:
    """只接受非敏感设置；账号记录及其他未知字段一律忽略。"""
    if not isinstance(values, dict):
        return {}
    clean = {key: values[key] for key in _PATH_SETTINGS if isinstance(values.get(key), str)}
    port = values.get("port")
    if isinstance(port, int) and not isinstance(port, bool) and 1024 <= port <= 65535:
        clean["port"] = port
    gap = values.get("gap_seconds")
    if isinstance(gap, (int, float)) and not isinstance(gap, bool):
        try:
            if math.isfinite(gap) and 0 <= gap <= 10:
                clean["gap_seconds"] = float(gap)
        except OverflowError:
            pass
    return clean


def _read_settings() -> dict[str, Any]:
    path = _settings_path()
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as stream:
        return _clean_settings(json.load(stream))


def _write_settings(values: dict[str, Any]) -> None:
    """在同一文件夹内原子替换，避免异常退出留下半份 JSON。"""
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=".settings-", suffix=".tmp", delete=False,
        ) as stream:
            temporary = stream.name
            json.dump(_clean_settings(values), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


def _normalized_path(value: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(value.strip())))


def _duration(milliseconds: Any) -> str:
    try:
        seconds = max(0, float(milliseconds)) / 1000
        if not math.isfinite(seconds) or seconds == 0:
            return "—"
    except (TypeError, ValueError, OverflowError):
        return "—"
    minutes, seconds = divmod(seconds, 60)
    return f"{int(minutes):02d}:{seconds:04.1f}"


def _source_time(value: Any) -> str:
    try:
        timestamp = float(value)
        if timestamp <= 0 or not math.isfinite(timestamp):
            return "—"
        if timestamp > 1_000_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return "—"


class App:
    """所有 Tk 调用均在主线程；工作线程仅通过队列发布结果。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1060x760")
        self.root.minsize(900, 720)
        self.root.configure(background="#F3F6F7")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._queue: queue.Queue[tuple[int, str, Any]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._busy = False
        self._closing = False
        self._destroyed = False
        self._task_id = 0
        self._task_kind = ""
        self._task_name = ""
        self._on_result: Callable[[Any], None] | None = None
        self._pending_completion: dict[str, Any] | None = None
        self._mutable_controls: list[tk.Widget] = []
        self._scan: dict[str, Any] | None = None
        self._scan_identity: tuple[str, int] | None = None
        self._record_rows: dict[str, str] = {}
        self._display_records: dict[str, dict[str, Any]] = {}
        self._merge_files: list[str] = []
        self._last_merge_path = ""
        self._auto_merge_folder = ""
        self._auto_merge_output = ""
        self._merge_name_stamp = ""
        self._merge_name_serial = 0
        self._suppress_changes = False
        self._saved_preferences: dict[str, Any] = {}

        self.weflow_exe = tk.StringVar()
        self.account_dir = tk.StringVar()
        self.output_dir = tk.StringVar()
        self.port = tk.StringVar(value="9229")
        self.merge_folder = tk.StringVar()
        self.merge_output = tk.StringVar()
        self.gap_seconds = tk.StringVar(value="0.5")
        self.connection_text = tk.StringVar(value="尚未检测连接")
        self.scan_text = tk.StringVar(value="先连接已登录的 WeFlow，再扫描收藏语音。")
        self.record_detail = tk.StringVar(value="选中记录可查看文件路径。")
        self.merge_text = tk.StringVar(value="选择包含 WAV 语音的文件夹。")
        self.status_text = tk.StringVar(value="正在初始化…")

        self._configure_style()
        self._build_ui()
        self.account_dir.trace_add("write", self._account_changed)
        self.port.trace_add("write", self._account_changed)
        self.merge_folder.trace_add("write", self._merge_folder_changed)
        self.root.after(60, self._poll_queue)
        self._start_task("初始化", "initialize", self._initialize_worker, self._on_initialized, persist=False)

    def _configure_style(self) -> None:
        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))
        self.root.option_add("*TCombobox*Listbox.font", ("Microsoft YaHei UI", 10))
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10), foreground="#20343B")
        style.configure("TFrame", background="#F3F6F7")
        style.configure("TLabel", background="#F3F6F7")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 19, "bold"), foreground="#153C44")
        style.configure("Muted.TLabel", foreground="#627780")
        style.configure("Small.TLabel", font=("Microsoft YaHei UI", 9), foreground="#627780")
        style.configure("TLabelframe", background="#F3F6F7", bordercolor="#D9E2E5", relief="solid")
        style.configure("TLabelframe.Label", background="#F3F6F7", foreground="#365A63")
        style.configure("TButton", padding=(12, 4), background="#FFFFFF", bordercolor="#CCDADD")
        style.map("TButton", background=[("active", "#E6F0F1"), ("disabled", "#EEF1F2")], foreground=[("disabled", "#9AA8AD")])
        style.configure("Accent.TButton", background="#117D83", foreground="#FFFFFF", bordercolor="#117D83")
        style.map("Accent.TButton", background=[("disabled", "#BBCFD1"), ("active", "#0D686D")], foreground=[("disabled", "#F5FAFA"), ("!disabled", "#FFFFFF")])
        style.configure("TEntry", fieldbackground="#FFFFFF", padding=5)
        style.configure("TSpinbox", fieldbackground="#FFFFFF", padding=5)
        style.configure("TNotebook", background="#F3F6F7", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(22, 9), background="#E5ECEE")
        style.map("TNotebook.Tab", background=[("selected", "#FFFFFF")], foreground=[("selected", "#0C747A")])
        style.configure("Treeview", background="#FFFFFF", fieldbackground="#FFFFFF", rowheight=29, bordercolor="#D9E2E5")
        style.configure("Treeview.Heading", background="#EAF0F2", padding=(6, 7), font=("Microsoft YaHei UI", 9, "bold"))
        style.map("Treeview", background=[("selected", "#D6EEEF")], foreground=[("selected", "#143F43")])
        style.configure("Horizontal.TProgressbar", background="#117D83", troughcolor="#E1EAED", borderwidth=0)

    def _build_ui(self) -> None:
        shell = ttk.Frame(self.root, padding=(22, 15, 22, 14))
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(1, weight=1)

        header = ttk.Frame(shell)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(header, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text="将本机收藏语音导出为 WAV，也可按顺序合并现有语音。", style="Muted.TLabel").pack(anchor="w", pady=(4, 0))

        notebook = ttk.Notebook(shell)
        notebook.grid(row=1, column=0, sticky="nsew")
        export_tab = ttk.Frame(notebook, padding=(13, 12))
        merge_tab = ttk.Frame(notebook, padding=(13, 12))
        notebook.add(export_tab, text="收藏导出")
        notebook.add(merge_tab, text="合并现有语音")
        self._build_export_tab(export_tab)
        self._build_merge_tab(merge_tab)

        task = ttk.Frame(shell)
        task.grid(row=2, column=0, sticky="ew", pady=(10, 7))
        task.columnconfigure(0, weight=1)
        ttk.Label(task, textvariable=self.status_text).grid(row=0, column=0, sticky="w")
        self.cancel_button = ttk.Button(task, text="取消", command=self._request_cancel, state="disabled", width=9)
        self.cancel_button.grid(row=0, column=1, rowspan=2, padx=(15, 0))
        self.progress = ttk.Progressbar(task, mode="determinate", maximum=100)
        self.progress.grid(row=1, column=0, sticky="ew", pady=(6, 0))

        logs = ttk.LabelFrame(shell, text="运行日志", padding=(8, 4))
        logs.grid(row=3, column=0, sticky="ew")
        logs.columnconfigure(0, weight=1)
        self.log = tk.Text(logs, height=3, wrap="word", state="disabled", relief="flat", borderwidth=0,
                           background="#FFFFFF", foreground="#4D626A", font=("Microsoft YaHei UI", 9), padx=7, pady=4)
        self.log.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(logs, orient="vertical", command=self.log.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=log_scroll.set)

    def _control(self, widget: tk.Widget) -> tk.Widget:
        self._mutable_controls.append(widget)
        return widget

    def _path_row(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar, command: Callable[[], None]) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 9), pady=3)
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=1, sticky="ew", pady=3)
        self._control(entry)
        button = ttk.Button(parent, text="浏览…", command=command, width=7)
        button.grid(row=row, column=2, padx=(8, 0), pady=3)
        self._control(button)

    def _build_export_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(4, weight=1)
        paths = ttk.Frame(parent)
        paths.grid(row=0, column=0, sticky="ew")
        paths.columnconfigure(1, weight=1)
        self._path_row(paths, 0, "WeFlow 程序", self.weflow_exe, self._choose_weflow)
        self._path_row(paths, 1, "微信账号目录", self.account_dir, lambda: self._choose_folder(self.account_dir, "选择微信账号目录"))
        self._path_row(paths, 2, "导出文件夹", self.output_dir, lambda: self._choose_folder(self.output_dir, "选择导出文件夹"))

        connection = ttk.Frame(parent)
        connection.grid(row=1, column=0, sticky="ew", pady=(6, 6))
        self._control(ttk.Button(connection, text="检测连接", command=lambda: self._connect(False))).pack(side="left")
        self._control(ttk.Button(connection, text="启动 WeFlow", command=lambda: self._connect(True))).pack(side="left", padx=(7, 12))
        ttk.Label(connection, text="高级 · CDP 端口", style="Small.TLabel").pack(side="left")
        port_entry = ttk.Entry(connection, textvariable=self.port, width=7)
        port_entry.pack(side="left", padx=(7, 12))
        self._control(port_entry)
        ttk.Label(connection, textvariable=self.connection_text, style="Small.TLabel").pack(side="left", fill="x", expand=True)

        actions = ttk.Frame(parent)
        actions.grid(row=2, column=0, sticky="ew", pady=(2, 5))
        self._control(ttk.Button(actions, text="扫描收藏", command=self._scan_favorites)).pack(side="left")
        self.export_button = ttk.Button(actions, text="导出全部", style="Accent.TButton", command=self._export_favorites)
        self.export_button.pack(side="left", padx=7)
        self._control(ttk.Button(actions, text="打开输出文件夹", command=lambda: self._open_folder(self.output_dir.get()))).pack(side="left")
        ttk.Label(actions, text="仅导出本机已有语音；缺失项会记入报告。", style="Small.TLabel").pack(side="right", padx=(8, 0))
        ttk.Label(parent, textvariable=self.scan_text, style="Muted.TLabel").grid(row=3, column=0, sticky="w", pady=(2, 6))

        table_frame = ttk.Frame(parent)
        table_frame.grid(row=4, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        columns = ("id", "source", "time", "duration", "status", "file")
        self.records = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse", height=6)
        specs = (("id", "收藏 ID", 100), ("source", "来源", 160), ("time", "时间", 148),
                 ("duration", "时长", 70), ("status", "状态", 100), ("file", "音频文件", 245))
        for name, label, width in specs:
            self.records.heading(name, text=label)
            self.records.column(name, width=width, minwidth=60, stretch=name in {"source", "file"}, anchor="w")
        self.records.column("duration", anchor="center")
        self.records.tag_configure("success", foreground="#1B7665")
        self.records.tag_configure("warning", foreground="#8F631E")
        self.records.tag_configure("failure", foreground="#A84E4E")
        self.records.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(table_frame, orient="vertical", command=self.records.yview)
        horizontal = ttk.Scrollbar(table_frame, orient="horizontal", command=self.records.xview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.records.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.records.bind("<<TreeviewSelect>>", self._record_selected)
        ttk.Label(parent, textvariable=self.record_detail, style="Small.TLabel", wraplength=930).grid(row=5, column=0, sticky="w", pady=(6, 0))

    def _build_merge_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)
        paths = ttk.Frame(parent)
        paths.grid(row=0, column=0, sticky="ew")
        paths.columnconfigure(1, weight=1)
        self._path_row(paths, 0, "WAV 文件夹", self.merge_folder, lambda: self._choose_folder(self.merge_folder, "选择现有 WAV 所在文件夹"))
        self._path_row(paths, 1, "合并输出文件", self.merge_output, self._choose_merge_output)

        toolbar = ttk.Frame(parent)
        toolbar.grid(row=1, column=0, sticky="ew", pady=(8, 7))
        self._control(ttk.Button(toolbar, text="刷新列表", command=self._refresh_merge)).pack(side="left")
        ttk.Label(toolbar, textvariable=self.merge_text, style="Muted.TLabel").pack(side="left", padx=12)

        listing = ttk.Frame(parent)
        listing.grid(row=2, column=0, sticky="nsew")
        listing.columnconfigure(0, weight=1)
        listing.rowconfigure(0, weight=1)
        self.merge_list = ttk.Treeview(listing, columns=("order", "name", "path"), show="headings", selectmode="extended", height=7)
        for column, title, width in (("order", "顺序", 55), ("name", "文件名", 280), ("path", "完整路径", 430)):
            self.merge_list.heading(column, text=title)
            self.merge_list.column(column, width=width, minwidth=45, anchor="center" if column == "order" else "w", stretch=column != "order")
        self.merge_list.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(listing, orient="vertical", command=self.merge_list.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(listing, orient="horizontal", command=self.merge_list.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.merge_list.configure(yscrollcommand=scrollbar.set, xscrollcommand=horizontal.set)
        order = ttk.Frame(listing)
        order.grid(row=0, column=2, sticky="ns", padx=(9, 0))
        self.order_buttons: list[ttk.Button] = []
        for title, callback in (("上移", lambda: self._move_merge(-1)), ("下移", lambda: self._move_merge(1)), ("移出列表", self._remove_merge)):
            button = ttk.Button(order, text=title, command=callback, width=8)
            button.pack(pady=(0, 7))
            self.order_buttons.append(button)
        self.merge_list.bind("<<TreeviewSelect>>", lambda _event: self._update_controls())

        bottom = ttk.Frame(parent)
        bottom.grid(row=3, column=0, sticky="ew", pady=(10, 6))
        ttk.Label(bottom, text="语音间隔（秒）").pack(side="left")
        gap = ttk.Spinbox(bottom, from_=0, to=10, increment=0.1, width=6, textvariable=self.gap_seconds)
        gap.pack(side="left", padx=(8, 14))
        self._control(gap)
        self.merge_button = ttk.Button(bottom, text="开始合并", style="Accent.TButton", command=self._merge_audio)
        self.merge_button.pack(side="left")
        self._control(ttk.Button(bottom, text="打开结果文件夹", command=self._open_merge_folder)).pack(side="left", padx=8)
        ttk.Label(parent, text="按列表从上到下合并；默认按文件名排序。移出列表不删除文件，源语音始终保留。", style="Small.TLabel", wraplength=920).grid(row=4, column=0, sticky="w")

    def _append_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", f"{datetime.now():%H:%M:%S}  {message}\n")
        if int(self.log.index("end-1c").split(".")[0]) > 1500:
            self.log.delete("1.0", "201.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _capture_preferences(self) -> dict[str, Any]:
        values: dict[str, Any] = {
            "weflow_exe": self.weflow_exe.get().strip(), "account_dir": self.account_dir.get().strip(),
            "output_dir": self.output_dir.get().strip(), "merge_folder": self.merge_folder.get().strip(),
            "merge_output_dir": str(Path(self.merge_output.get()).parent) if self.merge_output.get().strip() else "",
        }
        try:
            values["port"] = int(self.port.get().strip())
        except ValueError:
            values["port"] = self._saved_preferences.get("port", 9229)
        try:
            values["gap_seconds"] = float(self.gap_seconds.get().strip())
        except ValueError:
            values["gap_seconds"] = self._saved_preferences.get("gap_seconds", 0.5)
        return _clean_settings(values)

    def _initialize_worker(self, emit: Callable[[dict[str, Any]], None], cancel: threading.Event) -> dict[str, Any]:
        try:
            saved = _read_settings()
        except (OSError, ValueError, UnicodeError) as exc:
            saved = {}
            emit({"kind": "log", "message": f"未能读取已有设置，使用默认值：{safe_error(exc)}"})
        try:
            defaults = service.default_paths()
        except Exception as exc:
            defaults = {"output_dir": str(Path.home() / "Documents" / "微信收藏语音")}
            emit({"kind": "log", "message": f"未能自动查找路径，请手动选择：{safe_error(exc)}"})
        return {"defaults": defaults, "saved": saved}

    def _on_initialized(self, result: dict[str, Any]) -> None:
        self._saved_preferences = result["saved"]
        values = {**result["defaults"], **result["saved"]}
        self._suppress_changes = True
        try:
            self.weflow_exe.set(str(values.get("weflow_exe", "")))
            self.account_dir.set(str(values.get("account_dir", "")))
            self.output_dir.set(str(values.get("output_dir", "")))
            self.port.set(str(values.get("port", 9229)))
            self.gap_seconds.set(str(values.get("gap_seconds", 0.5)))
            output = str(values.get("output_dir", ""))
            self._auto_merge_folder = str(Path(output) / "收藏语音") if output else ""
            self.merge_folder.set(str(values.get("merge_folder", self._auto_merge_folder)))
            folder = str(values.get("merge_output_dir", ""))
            self._set_default_merge_output(self.merge_folder.get(), parent_override=folder)
        finally:
            self._suppress_changes = False
        self.status_text.set("就绪")
        self._append_log("已就绪。工具不会修改微信原始数据库或源语音；本机缺失的收藏语音只会报告。")

    def _start_task(self, name: str, kind: str, work: Callable[[Callable[[dict[str, Any]], None], threading.Event], Any],
                    on_result: Callable[[Any], None] | None = None, *, persist: bool = True) -> None:
        if self._busy:
            return
        preferences = self._capture_preferences() if persist else None
        self._busy = True
        self._cancel = threading.Event()
        self._task_id += 1
        task_id = self._task_id
        self._task_name, self._task_kind = name, kind
        self._on_result = on_result
        self._pending_completion = None
        self.status_text.set(f"{name}中…")
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(12)
        self._append_log(f"开始{name}。")
        self._update_controls()

        def emit(event: dict[str, Any]) -> None:
            if isinstance(event, dict):
                self._queue.put((task_id, "event", copy.deepcopy(event)))

        def worker() -> None:
            try:
                if preferences is not None:
                    try:
                        _write_settings(preferences)
                    except OSError as exc:
                        emit({"kind": "log", "message": f"设置未能保存，本次操作仍可继续：{safe_error(exc)}"})
                result = work(emit, self._cancel)
                completion = {"ok": True, "result": result}
            except Exception as exc:
                completion = {"ok": False, "error": safe_error(exc)}
            self._queue.put((task_id, "complete", completion))

        self._thread = threading.Thread(target=worker, name=f"favorite-tool-{kind}", daemon=False)
        self._thread.start()

    def _poll_queue(self) -> None:
        if self._destroyed:
            return
        for _ in range(150):
            try:
                task_id, kind, payload = self._queue.get_nowait()
            except queue.Empty:
                break
            if task_id != self._task_id:
                continue
            if kind == "complete":
                self._pending_completion = payload
            elif kind == "event":
                self._handle_event(payload)
        if self._pending_completion is not None and self._thread is not None and not self._thread.is_alive():
            self._finish_task()
        if not self._destroyed:
            self.root.after(60, self._poll_queue)

    def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind")
        if kind == "log":
            self._append_log(str(event.get("message", "")))
        elif kind == "progress":
            try:
                total = max(0, int(event.get("total", 0)))
                current = max(0, int(event.get("current", 0)))
            except (TypeError, ValueError):
                total, current = 0, 0
            if total:
                self.progress.stop()
                self.progress.configure(mode="determinate", maximum=total, value=min(current, total))
            text = str(event.get("message", "")) or f"{self._task_name}中…"
            if not self._cancel.is_set():
                self.status_text.set(f"{text}  ({current}/{total})" if total else text)
        elif kind == "record" and self._task_kind in {"scan", "export"}:
            record = event.get("record")
            if isinstance(record, dict):
                self._upsert_record(record)

    def _finish_task(self) -> None:
        completion = self._pending_completion or {}
        callback = self._on_result
        self._pending_completion = None
        self._on_result = None
        self._thread = None
        self._busy = False
        self.progress.stop()
        self.progress.configure(mode="determinate")
        try:
            if completion.get("ok"):
                if callback is not None:
                    callback(completion.get("result"))
                else:
                    self.status_text.set("已停止" if self._cancel.is_set() else f"{self._task_name}完成")
            else:
                error = str(completion.get("error", "任务未完成。"))
                self.status_text.set("任务已停止" if self._cancel.is_set() else f"{self._task_name}失败")
                if self._task_kind == "scan":
                    self.scan_text.set("扫描已取消；重新扫描后即可导出。" if self._cancel.is_set() else "扫描未完成，请查看日志并重新扫描。")
                elif self._task_kind == "connect":
                    self.connection_text.set("连接操作已取消" if self._cancel.is_set() else "连接未成功，请查看日志")
                self._append_log(error)
                if not self._closing and not self._cancel.is_set():
                    messagebox.showerror(f"{self._task_name}未完成", error, parent=self.root)
        except Exception as exc:
            self.status_text.set("结果显示失败")
            error = safe_error(exc)
            self._append_log(f"无法显示任务结果：{error}")
            if not self._closing:
                messagebox.showerror("无法显示结果", error, parent=self.root)
        finally:
            self._update_controls()
            if self._closing:
                self._destroy()

    def _update_controls(self) -> None:
        state = "disabled" if self._busy else "normal"
        for widget in self._mutable_controls:
            widget.configure(state=state)
        has_scan = bool(self._scan and self._scan.get("records"))
        self.export_button.configure(state="normal" if not self._busy and has_scan else "disabled")
        self.merge_button.configure(state="normal" if not self._busy and self._merge_files else "disabled")
        selection = bool(self.merge_list.selection())
        for button in self.order_buttons:
            button.configure(state="normal" if not self._busy and selection else "disabled")
        self.cancel_button.configure(state="normal" if self._busy and not self._cancel.is_set() else "disabled")

    def _request_cancel(self) -> None:
        if self._busy and not self._cancel.is_set():
            self._cancel.set()
            self.status_text.set("正在取消，等待当前操作安全结束…")
            self._append_log("已请求取消；已完成的文件会保留。")
            self._update_controls()

    def _valid_port(self) -> int | None:
        try:
            value = int(self.port.get().strip())
            if 1024 <= value <= 65535:
                return value
        except ValueError:
            pass
        messagebox.showerror("端口无效", "CDP 端口应为 1024 到 65535 之间的整数。", parent=self.root)
        return None

    def _account_changed(self, *_args: Any) -> None:
        if self._suppress_changes:
            return
        had_scan = self._scan is not None or bool(self._record_rows)
        self._scan = None
        self._scan_identity = None
        self.connection_text.set("设置已更改，请重新检测连接")
        if had_scan:
            self._clear_records()
            self.scan_text.set("账号目录或端口已更改，请重新扫描收藏。")
        self._update_controls()

    def _clear_records(self) -> None:
        children = self.records.get_children()
        if children:
            self.records.delete(*children)
        self._record_rows.clear()
        self._display_records.clear()
        self.record_detail.set("选中记录可查看文件路径。")

    def _connect(self, launch: bool) -> None:
        if self._busy:
            return
        port = self._valid_port()
        if port is None:
            return
        executable = self.weflow_exe.get().strip()
        if launch and not executable:
            messagebox.showerror("请选择 WeFlow", "先选择 WeFlow 程序路径，再点击“启动 WeFlow”。", parent=self.root)
            return

        def work(emit: Callable[[dict[str, Any]], None], cancel: threading.Event) -> dict[str, Any]:
            if cancel.is_set():
                return {"connected": False, "message": "操作已取消。"}
            return service.connect_weflow(executable, port=port, launch=launch)

        self._start_task("启动 WeFlow" if launch else "检测连接", "connect", work, self._on_connected)

    def _on_connected(self, result: dict[str, Any]) -> None:
        if self._cancel.is_set():
            self.status_text.set("连接操作已结束")
            return
        connected = bool(result.get("connected"))
        message = str(result.get("message", "连接成功" if connected else "尚未连接"))
        directory = result.get("account_dir")
        if connected and directory and (not self.account_dir.get().strip() or _normalized_path(str(directory)) != _normalized_path(self.account_dir.get())):
            self.account_dir.set(str(directory))
        wxid = str(result.get("wxid", ""))
        self.connection_text.set((f"已连接 · {wxid}" if wxid else "已连接") if connected else "尚未连接")
        self.status_text.set("连接成功" if connected else "尚未连接")
        self._append_log(message)
        if not connected:
            messagebox.showinfo("连接状态", message, parent=self.root)

    def _scan_favorites(self) -> None:
        if self._busy:
            return
        port = self._valid_port()
        account = self.account_dir.get().strip()
        if port is None:
            return
        if not account:
            messagebox.showerror("请选择账号目录", "请选择微信账号目录，或先检测 WeFlow 连接以获取路径。", parent=self.root)
            return
        account = _normalized_path(account)
        self._scan = None
        self._scan_identity = None
        self._clear_records()
        self.scan_text.set("正在读取本机收藏语音记录…")

        def work(emit: Callable[[dict[str, Any]], None], cancel: threading.Event) -> dict[str, Any]:
            return service.scan_favorites(account, port, emit, cancel)

        def done(result: dict[str, Any]) -> None:
            if self._cancel.is_set():
                self.scan_text.set("扫描已取消；重新扫描后即可导出。")
                self.status_text.set("扫描已取消")
                return
            self._scan = copy.deepcopy(result)
            self._scan_identity = (account, port)
            for record in result.get("records", []):
                self._upsert_record(record)
            count = len(result.get("records", []))
            self.scan_text.set(f"已找到 {count} 条收藏语音。" if count else "未找到收藏语音。请确认账号目录及收藏内容。")
            self.status_text.set(f"扫描完成 · {count} 条语音")
            self._append_log(f"扫描完成，共 {count} 条收藏语音。")

        self._start_task("扫描收藏", "scan", work, done)

    def _upsert_record(self, record: dict[str, Any]) -> None:
        key = str(record.get("key") or f"{record.get('favorite_id', '')}:{record.get('data_id', '')}:{record.get('source_id', '')}")
        merged = {**self._display_records.get(key, {}), **record}
        self._display_records[key] = copy.deepcopy(merged)
        sender = str(merged.get("sender") or "")
        recipient = str(merged.get("recipient") or "")
        source = " → ".join(value for value in (sender, recipient) if value) or str(merged.get("source_id") or "—")
        status = str(merged.get("status") or "待导出")
        filename = str(merged.get("file") or "")
        tag = "failure" if "失败" in status or "错误" in status else "warning" if "缺失" in status or "不存在" in status or "无法匹配" in status else "success" if "成功" in status or "已导出" in status or "已存在" in status or "已验证跳过" in status else ""
        values = (str(merged.get("favorite_id") or "—"), source, _source_time(merged.get("source_time")),
                  _duration(merged.get("duration_ms")), status, Path(filename).name if filename else "—")
        if key in self._record_rows:
            self.records.item(self._record_rows[key], values=values, tags=(tag,) if tag else ())
        else:
            self._record_rows[key] = self.records.insert("", "end", values=values, tags=(tag,) if tag else ())

    def _record_selected(self, _event: Any = None) -> None:
        selected = self.records.selection()
        if not selected:
            return
        row = selected[0]
        for key, item in self._record_rows.items():
            if item == row:
                record = self._display_records[key]
                self.record_detail.set(str(record.get("file") or record.get("reason") or "此记录尚无导出文件。"))
                break

    def _export_favorites(self) -> None:
        if self._busy or not self._scan:
            return
        port = self._valid_port()
        if port is None:
            return
        identity = (_normalized_path(self.account_dir.get()), port)
        if identity != self._scan_identity:
            self._account_changed()
            messagebox.showinfo("请重新扫描", "账号目录或连接端口已变化，请重新扫描后导出。", parent=self.root)
            return
        output = self.output_dir.get().strip()
        if not output:
            messagebox.showerror("请选择导出文件夹", "请选择用于保存 WAV 和导出报告的文件夹。", parent=self.root)
            return
        output = _normalized_path(output)
        snapshot = copy.deepcopy(self._scan)

        def work(emit: Callable[[dict[str, Any]], None], cancel: threading.Event) -> dict[str, Any]:
            return service.export_favorites(snapshot, output, port, emit, cancel)

        self._start_task("导出收藏", "export", work, self._on_exported)

    def _on_exported(self, result: dict[str, Any]) -> None:
        counts = {key: result.get(key, 0) for key in ("total", "exported", "skipped", "missing", "failed")}
        summary = f"共 {counts['total']} 条 · 导出 {counts['exported']} · 跳过 {counts['skipped']} · 缺失 {counts['missing']} · 失败 {counts['failed']}"
        cancelled = bool(result.get("cancelled"))
        self.scan_text.set(summary)
        self.status_text.set("导出已取消，已完成文件保留" if cancelled else "导出完成")
        self._append_log(summary)
        if result.get("report_csv"):
            self._append_log(f"导出报告：{result['report_csv']}")
        if result.get("output_dir") and self.merge_folder.get().strip() in ("", self._auto_merge_folder):
            self._auto_merge_folder = str(Path(str(result["output_dir"])) / "收藏语音")
            self.merge_folder.set(self._auto_merge_folder)

    def _merge_folder_changed(self, *_args: Any) -> None:
        if self._suppress_changes:
            return
        self._merge_files.clear()
        self._render_merge_list()
        self.merge_text.set("文件夹已更改，请刷新列表。")
        self._set_default_merge_output(self.merge_folder.get())
        self._update_controls()

    def _set_default_merge_output(self, folder: str, *, parent_override: str = "", force: bool = False) -> None:
        current = self.merge_output.get().strip()
        if not force and current and current != self._auto_merge_output:
            return
        if parent_override:
            parent = Path(parent_override)
        elif folder.strip():
            parent = Path(os.path.expanduser(folder.strip())).parent
        else:
            parent = Path.home() / "Documents"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._merge_name_serial = self._merge_name_serial + 1 if stamp == self._merge_name_stamp else 0
        self._merge_name_stamp = stamp
        suffix = f"_{self._merge_name_serial + 1}" if self._merge_name_serial else ""
        self._auto_merge_output = str(parent / f"收藏语音_合并_{stamp}{suffix}.wav")
        self.merge_output.set(self._auto_merge_output)

    def _refresh_merge(self) -> None:
        if self._busy:
            return
        folder = self.merge_folder.get().strip()
        if not folder:
            messagebox.showerror("请选择文件夹", "请选择包含现有 WAV 语音的文件夹。", parent=self.root)
            return
        folder = _normalized_path(folder)

        def work(emit: Callable[[dict[str, Any]], None], cancel: threading.Event) -> list[str]:
            files = service.list_wav_files(folder)
            return sorted((str(path) for path in files if Path(path).suffix.lower() == ".wav"), key=lambda path: (Path(path).name.casefold(), path.casefold()))

        def done(files: list[str]) -> None:
            if self._cancel.is_set():
                self.status_text.set("刷新已取消")
                return
            self._merge_files = files
            self._render_merge_list()
            self._set_default_merge_output(folder)
            self.status_text.set(f"列表已更新 · {len(files)} 个 WAV 文件")
            self._append_log(f"已读取 {len(files)} 个 WAV 文件，按文件名排序。")

        self._start_task("读取 WAV 列表", "list", work, done)

    def _render_merge_list(self, selected: set[int] | None = None) -> None:
        children = self.merge_list.get_children()
        if children:
            self.merge_list.delete(*children)
        for index, path in enumerate(self._merge_files):
            self.merge_list.insert("", "end", iid=f"merge-{index}", values=(index + 1, Path(path).name, path))
        if selected:
            items = [f"merge-{index}" for index in sorted(selected) if index < len(self._merge_files)]
            self.merge_list.selection_set(items)
            if items:
                self.merge_list.see(items[0])
        self.merge_text.set(f"{len(self._merge_files)} 个 WAV 文件 · 可调整合并顺序" if self._merge_files else "列表为空；请刷新文件夹中的 WAV。")

    def _merge_selection(self) -> set[int]:
        return {int(item.split("-")[-1]) for item in self.merge_list.selection()}

    def _move_merge(self, direction: int) -> None:
        if self._busy:
            return
        selected = self._merge_selection()
        indices = sorted(selected, reverse=direction > 0)
        for index in indices:
            target = index + direction
            if 0 <= target < len(self._merge_files) and target not in selected:
                self._merge_files[index], self._merge_files[target] = self._merge_files[target], self._merge_files[index]
                selected.remove(index)
                selected.add(target)
        self._render_merge_list(selected)
        self._update_controls()

    def _remove_merge(self) -> None:
        if self._busy:
            return
        selected = self._merge_selection()
        self._merge_files = [path for index, path in enumerate(self._merge_files) if index not in selected]
        self._render_merge_list()
        self._update_controls()

    def _merge_audio(self) -> None:
        if self._busy or not self._merge_files:
            return
        try:
            gap = float(self.gap_seconds.get().strip())
            if not math.isfinite(gap) or not 0 <= gap <= 10:
                raise ValueError
        except ValueError:
            messagebox.showerror("间隔无效", "语音间隔应为 0 到 10 秒之间的数字。", parent=self.root)
            return
        output = self.merge_output.get().strip()
        if not output:
            messagebox.showerror("请选择输出文件", "请选择合并结果的 WAV 文件路径。", parent=self.root)
            return
        if not Path(output).suffix:
            output += ".wav"
            self.merge_output.set(output)
        if Path(output).suffix.lower() != ".wav":
            messagebox.showerror("文件格式无效", "合并输出文件的扩展名必须为 .wav。", parent=self.root)
            return
        output = _normalized_path(output)
        paths = tuple(self._merge_files)
        if output in {_normalized_path(path) for path in paths}:
            messagebox.showerror("请使用新的输出文件", "合并结果不能覆盖列表中的源语音。请选择另一个文件名。", parent=self.root)
            return

        def work(emit: Callable[[dict[str, Any]], None], cancel: threading.Event) -> dict[str, Any]:
            if Path(output).exists():
                raise UserError("合并输出文件已存在。请使用新的文件名，以保留现有语音。")
            return service.merge_audio(list(paths), output, gap, emit, cancel)

        self._start_task("合并语音", "merge", work, self._on_merged)

    def _on_merged(self, result: dict[str, Any]) -> None:
        if result.get("path"):
            self._last_merge_path = str(result["path"])
            duration = float(result.get("duration_seconds", 0))
            count = result.get("files", len(self._merge_files))
            if isinstance(count, (list, tuple)):
                count = len(count)
            self.status_text.set(f"合并完成 · {count} 个文件 · {duration:.1f} 秒")
            self._append_log(f"合并结果：{self._last_merge_path}")
            self._set_default_merge_output(self.merge_folder.get())
        else:
            self.status_text.set("合并已取消" if self._cancel.is_set() else "合并任务已结束")

    def _choose_weflow(self) -> None:
        if self._busy:
            return
        path = filedialog.askopenfilename(title="选择 WeFlow 程序", parent=self.root,
                                          filetypes=(("Windows 程序", "*.exe"), ("所有文件", "*.*")))
        if path:
            self.weflow_exe.set(path)

    def _choose_folder(self, variable: tk.StringVar, title: str) -> None:
        if self._busy:
            return
        path = filedialog.askdirectory(title=title, parent=self.root, initialdir=variable.get() or None, mustexist=True)
        if path:
            variable.set(path)

    def _choose_merge_output(self) -> None:
        if self._busy:
            return
        current = self.merge_output.get().strip()
        path = filedialog.asksaveasfilename(title="选择合并输出文件（请使用新文件名）", parent=self.root,
                                           initialdir=str(Path(current).parent) if current else None,
                                           initialfile=Path(current).name if current else "收藏语音_合并.wav",
                                           defaultextension=".wav", filetypes=(("WAV 音频", "*.wav"),), confirmoverwrite=False)
        if path:
            self.merge_output.set(path)

    def _open_folder(self, value: str) -> None:
        if self._busy:
            return
        if not value.strip():
            messagebox.showinfo("尚无文件夹", "请先选择输出文件夹。", parent=self.root)
            return
        path = _normalized_path(value)

        def work(emit: Callable[[dict[str, Any]], None], cancel: threading.Event) -> None:
            if cancel.is_set():
                return
            if not Path(path).is_dir():
                raise UserError("文件夹尚不存在。完成导出或合并后再打开，或选择已有文件夹。")
            if not hasattr(os, "startfile"):
                raise UserError("打开文件夹功能仅适用于 Windows。")
            os.startfile(path)

        self._start_task("打开文件夹", "open", work)

    def _open_merge_folder(self) -> None:
        path = self._last_merge_path or self.merge_output.get().strip()
        self._open_folder(str(Path(path).parent) if path else "")

    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._busy:
            self._request_cancel()
            self.status_text.set("正在停止任务，结束后关闭窗口…")
            task_id = self._task_id
            self.root.after(12000, lambda: self._check_close_wait(task_id))
        else:
            self._start_task("保存设置", "close", lambda emit, cancel: None)

    def _check_close_wait(self, task_id: int) -> None:
        if self._destroyed or not self._closing or not self._busy or task_id != self._task_id:
            return
        keep_waiting = messagebox.askyesno(
            "正在安全停止", "取消请求已发送，当前操作仍在结束。\n\n继续等待并在任务结束后关闭窗口？\n选择“否”会返回窗口，取消请求仍然有效。",
            parent=self.root,
        )
        if not keep_waiting:
            self._closing = False
            self.status_text.set("正在取消，等待当前操作安全结束…")

    def _destroy(self) -> None:
        self._destroyed = True
        self.root.destroy()


def run_gui() -> None:
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()
