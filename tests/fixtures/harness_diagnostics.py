"""Task 1 专用假 ACP 子进程：确定性产生阶段错误、协议错误与 stderr 压力。

所有参数只通过 argv 传入；不读取真实凭证。审计文件只记录方法名和受控标记，
不写入 prompt、环境变量或任何 Run 凭证。
"""
import json
import sys


def option(name, default=None):
    flag = "--" + name
    arguments = sys.argv[1:]
    if flag in arguments:
        return arguments[arguments.index(flag) + 1]
    return default


MODE = option("mode", "normal")
AUDIT = option("audit", "")
STDERR_BYTES = int(option("stderr-bytes", "0") or 0)
STATUS = option("status", "")
ERROR_CODE = option("error-code", "")
ERROR_DATA = option("error-data", "")
ERROR_MESSAGE = option("error-message", "fixture failure")
SESSION_ID = "diag-session"


def record(entry):
    if not AUDIT:
        return
    with open(AUDIT, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")


def send(message):
    print(json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False),
          flush=True)


def receive():
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    message = json.loads(line)
    record({"method": message.get("method")})
    return message


def reply(message, result):
    send({"id": message["id"], "result": result})


def fail(message):
    error = {"code": -32000, "message": ERROR_MESSAGE}
    data = {}
    if ERROR_DATA:
        # 原样注入结构化证据，包括非法 status 字符串；fixture 不做 int 转换。
        parsed = json.loads(ERROR_DATA)
        if isinstance(parsed, dict):
            data = parsed
    if STATUS:
        data["status"] = int(STATUS)
    if ERROR_CODE:
        data["code"] = ERROR_CODE
    if data:
        error["data"] = data
    send({"id": message["id"], "error": error})


def burst_stderr(total):
    """无换行大输出：验证父进程按块排空而不是按行读取。"""
    if total <= 0:
        return
    chunk = b"x" * 4096
    remaining = total
    while remaining:
        piece = chunk[:min(4096, remaining)]
        sys.stderr.buffer.write(piece)
        remaining -= len(piece)
    sys.stderr.buffer.flush()
    record({"method": "fixture/stderr-complete", "bytes": total})


burst_stderr(STDERR_BYTES)

if MODE == "stderr-exit":
    # 写完 stderr 立即退出：父进程必须仍能排空并清理任务。
    sys.exit(0)

if MODE == "malformed-idle":
    # 等测试发出第一帧后制造协议错误，确保没有待回复调用掩盖阶段。
    receive()
    print("not-json", flush=True)
    sys.stdin.read()
    sys.exit(0)

if MODE == "hang-stderr":
    # 收到第一帧后阻塞；测试取消调用并关闭连接，验证任务清理。
    receive()
    sys.stdin.read()
    sys.exit(0)

while True:
    message = receive()
    method = message.get("method")
    if method == "probe-error":
        # 可选诊断字段解析的专用帧；后续 probe-ok 必须仍能被 reader 消费。
        fail(message)
        continue
    if method == "probe-ok":
        reply(message, {"ok": True})
        continue
    if method == "initialize":
        if MODE == "initialize-error":
            fail(message)
            continue
        reply(message, {"protocolVersion": 1, "agentCapabilities": {}})
    elif method in ("session/new", "session/load", "session/resume"):
        if MODE == "session-error":
            fail(message)
            continue
        reply(message, {
            "sessionId": SESSION_ID,
            "configOptions": [{"category": "model", "id": "model",
                               "type": "select"}],
        })
    elif method == "session/set_config_option":
        if MODE == "model-error":
            fail(message)
            continue
        reply(message, {})
    elif method == "session/prompt":
        if MODE == "prompt-error":
            fail(message)
            continue
        if MODE == "exit-at-prompt":
            sys.exit(0)
        for update, text in (("agent_thought_chunk", "思考"),
                             ("agent_message_chunk", "你好")):
            send({"method": "session/update", "params": {
                "sessionId": SESSION_ID,
                "update": {"sessionUpdate": update,
                           "content": {"type": "text", "text": text}}}})
        reply(message, {"stopReason": "end_turn"})
    elif method == "session/cancel":
        if "id" in message:
            reply(message, {})
    elif "id" in message:
        reply(message, {})
