import argparse
import json
import sys
import threading
from pathlib import Path

from favorite_tool import __version__
from favorite_tool.common import UserError, save_json, safe_error


def self_test():
    import cryptography
    import sqlite3
    import tkinter
    import websocket
    from favorite_tool import audio, bridge, database, gui, service
    root = tkinter.Tk()
    root.withdraw()
    root.update_idletasks()
    root.destroy()
    return {"ok": True, "version": __version__, "tk": tkinter.TkVersion,
            "sqlite": sqlite3.sqlite_version, "cryptography": cryptography.__version__,
            "websocket_client": websocket.__version__, "frozen": bool(getattr(sys, "frozen", False))}


def main():
    parser = argparse.ArgumentParser(description="微信收藏语音工具：本地导出与 WAV 合并")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--self-test", action="store_true", help="验证依赖与窗口运行环境")
    actions.add_argument("--scan", action="store_true", help="扫描当前账号")
    actions.add_argument("--export", action="store_true", help="扫描并导出收藏")
    actions.add_argument("--merge", action="store_true", help="合并文件夹中的现有 WAV")
    parser.add_argument("--account", help="微信账号目录")
    parser.add_argument("--folder", help="合并输入文件夹")
    parser.add_argument("--output", help="导出目录或合并输出文件")
    parser.add_argument("--gap", type=float, default=.5, help="段间静音秒数")
    parser.add_argument("--port", type=int, default=9229)
    parser.add_argument("--result", help="把操作结果保存为 JSON")
    args = parser.parse_args()
    try:
        if args.self_test:
            result = self_test()
        elif args.scan or args.export:
            from favorite_tool.service import scan_favorites, export_favorites
            if not args.account or (args.export and not args.output):
                raise UserError("扫描需要 --account，导出还需要 --output。")
            scan = scan_favorites(args.account, args.port, cancel=threading.Event())
            if args.export:
                result = export_favorites(scan, args.output, args.port, cancel=threading.Event())
            else:
                result = {"ok": True, "total": len(scan["records"]), "scan": scan}
        elif args.merge:
            from favorite_tool.service import list_wav_files, merge_audio
            if not args.folder or not args.output:
                raise UserError("合并需要 --folder 和 --output。")
            result = merge_audio(list_wav_files(args.folder), args.output, args.gap)
        else:
            from favorite_tool.gui import run_gui
            run_gui()
            return 0
    except Exception as exc:
        result = {"ok": False, "error": safe_error(exc)}
        exit_code = 1
    else:
        exit_code = 0
    if args.result:
        save_json(Path(args.result), result)
    if sys.stdout is not None:
        print(json.dumps(result, ensure_ascii=True))
    elif exit_code and not args.result:
        import tkinter
        from tkinter import messagebox
        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror("微信收藏语音工具", result["error"])
        root.destroy()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
