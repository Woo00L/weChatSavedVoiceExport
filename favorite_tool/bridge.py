import base64
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

import websocket

from .common import UserError, check_cancel


class BridgeConnectionError(UserError):
    """本机连接故障，不能按单条源消息失败继续重试。"""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise UserError("本机接口返回了重定向，已拒绝连接。")


def validate_port(port):
    try:
        port = int(port)
    except (TypeError, ValueError) as exc:
        raise UserError("接口端口必须是整数。") from exc
    if not 1024 <= port <= 65535:
        raise UserError("接口端口必须在 1024 至 65535 之间。")
    return port


def targets(port):
    port = validate_port(port)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(f"http://127.0.0.1:{port}/json/list", timeout=3) as response:
            value = json.loads(response.read(1024 * 1024))
        if not isinstance(value, list):
            raise ValueError()
        return value
    except UserError:
        raise
    except Exception as exc:
        raise UserError("未连接到 WeFlow。请使用“启动 WeFlow”，并在 WeFlow 中手动解锁。若 WeFlow 已启动，请先正常退出后再启动。") from exc


class WeFlowBridge:
    def __init__(self, port=9229, cancel=None):
        self.port = validate_port(port)
        self.cancel = cancel
        self.sequence = 0
        check_cancel(cancel)
        choices = targets(self.port)
        target = next((p for p in choices if p.get("type") == "page" and str(p.get("title", "")).lower() == "weflow"), None)
        if not target:
            raise UserError("指定端口没有发现 WeFlow 主窗口，请检查所选程序和端口。")
        address = target.get("webSocketDebuggerUrl", "")
        parsed = urllib.parse.urlparse(address)
        if parsed.scheme != "ws" or parsed.hostname != "127.0.0.1" or parsed.port != self.port or not parsed.path.startswith("/devtools/page/"):
            raise UserError("WeFlow 接口不是预期的本机地址，已拒绝连接。")
        try:
            self.socket = websocket.create_connection(address, timeout=3, suppress_origin=True,
                                                      http_no_proxy=["127.0.0.1", "localhost"])
        except Exception as exc:
            raise UserError("无法打开 WeFlow 本机连接，请重新启动 WeFlow。") from exc
        self.socket.settimeout(.5)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.socket.close(timeout=1)

    def evaluate(self, expression, timeout=120):
        check_cancel(self.cancel)
        self.sequence += 1
        identifier = self.sequence
        try:
            self.socket.send(json.dumps({"id": identifier, "method": "Runtime.evaluate", "params": {
                "expression": expression, "awaitPromise": True, "returnByValue": True}}))
        except websocket.WebSocketException as exc:
            raise BridgeConnectionError("WeFlow 连接已中断。") from exc
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            check_cancel(self.cancel)
            try:
                raw = self.socket.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except websocket.WebSocketException as exc:
                raise BridgeConnectionError("WeFlow 连接已中断，请重新连接后继续。") from exc
            if not raw:
                raise BridgeConnectionError("WeFlow 已关闭连接。")
            try:
                response = json.loads(raw)
            except (ValueError, TypeError) as exc:
                raise BridgeConnectionError("WeFlow 返回无效响应。") from exc
            if not isinstance(response, dict):
                raise BridgeConnectionError("WeFlow 返回无效响应。")
            if response.get("id") != identifier:
                continue
            result = response.get("result", {})
            if response.get("error") or result.get("exceptionDetails"):
                raise BridgeConnectionError("WeFlow 接口调用失败，当前版本可能不兼容。")
            return result.get("result", {}).get("value")
        raise BridgeConnectionError("WeFlow 响应超时，已完成的结果保留，可稍后重试。")

    def state(self, include_key=False):
        result = self.evaluate("""(async()=>{
          const api=window.electronAPI;
          if(!api?.chat?.connect || !api?.chat?.getAllVoiceMessages || !api?.chat?.getVoiceData || !api?.config?.get)
            return {ok:false,reason:'api'};
          const connected=await api.chat.connect();
          if(!connected?.success)return {ok:false,reason:'locked'};
          return {ok:true,wxid:await api.config.get('myWxid'),db_path:await api.config.get('dbPath')"""
          + (",key:await api.config.get('decryptKey')" if include_key else "") + "};})()", timeout=25)
        if not isinstance(result, dict) or not result.get("ok"):
            if isinstance(result, dict) and result.get("reason") == "api":
                raise UserError("此 WeFlow 版本缺少所需接口。当前已适配本机 4.5.1。")
            raise UserError("请先在 WeFlow 中解锁并配置有效账号，确认能查看聊天记录后再点击“检测连接”。")
        if not isinstance(result.get("wxid"), str) or not isinstance(result.get("db_path"), str):
            raise UserError("WeFlow 没有提供有效账号目录，请检查其设置。")
        return result

    def session_names(self):
        result = self.evaluate("""(async()=>{const r=await window.electronAPI.chat.getSessions();
          return r?.success?{ok:true,sessions:r.sessions.map(s=>s.username)}:{ok:false};})()""")
        if not isinstance(result, dict) or not result.get("ok") or not isinstance(result.get("sessions"), list):
            raise UserError("无法读取 WeFlow 的本地会话列表。")
        return set(result["sessions"])

    def voice_messages(self, session, identifiers):
        arguments = json.dumps([session, identifiers], ensure_ascii=True)
        expression = """(async()=>{
          const [session,ids]=ARGS;
          const r=await window.electronAPI.chat.getAllVoiceMessages(session);
          if(!r?.success||!Array.isArray(r.messages))return {ok:false};
          const wanted=new Set(ids);
          return {ok:true,messages:r.messages.filter(m=>typeof m.serverIdRaw==='string'&&wanted.has(m.serverIdRaw)).map(m=>{
            const raw=m.rawContent||'';
            const duration=/\\bvoicelength\\s*=\\s*["'](\\d+)["']/.exec(raw)?.[1];
            const size=/\\blength\\s*=\\s*["'](\\d+)["']/.exec(raw)?.[1];
            return {source_id:m.serverIdRaw,local_id:m.localId,create_time:m.createTime,
              duration_ms:duration?Number(duration):0,size:size?Number(size):0};
          })};
        })()""".replace("ARGS", arguments)
        result = self.evaluate(expression)
        if not isinstance(result, dict) or not result.get("ok") or not isinstance(result.get("messages"), list):
            raise UserError("无法读取源会话语音。")
        return result["messages"]

    def voice_data(self, session, local_id, create_time, source_id):
        # 第四参数为完整服务端 ID；绝不使用浮点数或发送者替代。
        args = json.dumps([session, int(local_id), int(create_time), str(source_id)])
        result = self.evaluate(f"(async()=>await window.electronAPI.chat.getVoiceData(...{args}))()")
        if not isinstance(result, dict) or not result.get("success") or not isinstance(result.get("data"), str):
            raise UserError("本地语音数据不可用，或 WeFlow 未能解码。")
        try:
            return base64.b64decode(result["data"], validate=True)
        except ValueError as exc:
            raise UserError("WeFlow 返回的音频编码无效。") from exc


def _weflow_running():
    if os.name != "nt":
        return False
    proc = subprocess.run(["tasklist", "/FI", "IMAGENAME eq WeFlow.exe", "/FO", "CSV", "/NH"],
                          capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
    return b'"weflow.exe"' in proc.stdout.lower()


def launch_weflow(executable, port=9229):
    port = validate_port(port)
    try:
        targets(port)
        return
    except UserError:
        pass
    path = Path(executable).expanduser().resolve()
    if not path.is_file() or path.name.lower() != "weflow.exe":
        raise UserError("请选择本机的 WeFlow.exe 程序。")
    if _weflow_running():
        raise UserError("WeFlow 已经运行但未开放本机连接。请从托盘正常退出 WeFlow，再点击“启动 WeFlow”。工具不会强制关闭它。")
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    subprocess.Popen([str(path), "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={port}"],
                     cwd=str(path.parent), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            targets(port)
            return
        except UserError:
            time.sleep(.5)
    raise UserError("WeFlow 正在启动。请完成手动解锁后再点击“检测连接”。")
