"""Task 3 原 Web 共用认证/会话数据层浏览器验收。

使用真实 ``npm run build`` 产物和隔离的假 control/local API，不连接用户
容器、真实 Key 或真实供应商。两个 Chromium context 验证原 Sidebar /
ChatPage、身份切换、迟到响应、问题卡、停止、刷新重连与 local 回归。
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import mimetypes
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "web" / "frontend" / "dist"
EVIDENCE = ROOT / "docs" / "evidence" / "original-web-task-03" / "browser"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAusB9Y9Z0mQAAAAASUVORK5CYII=")

USERS = {
    "A": {
        "userId": "user-a",
        "workspaceId": "workspace-a",
        "role": "admin",
        "csrfToken": "csrf-a",
        "capabilities": ["workspace", "admin"],
    },
    "B": {
        "userId": "user-b",
        "workspaceId": "workspace-b",
        "role": "member",
        "csrfToken": "csrf-b",
        "capabilities": ["workspace"],
    },
}


def identity(user: str) -> dict:
    return {**USERS[user]}


class FakeState:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.lock = threading.RLock()
        self.requests: list[tuple[str, str]] = []
        self.invites = {
            "invite-a": "A", "invite-a2": "A", "invite-a3": "A",
            "invite-a4": "A", "invite-a5": "A", "invite-a6": "A",
            "invite-a7": "A", "invite-a8": "A", "invite-a9": "A",
            "invite-a10": "A", "invite-a11": "A", "invite-a12": "A",
            "invite-a13": "A", "invite-a14": "A", "invite-a15": "A",
            "invite-a16": "A", "invite-a17": "A", "invite-a18": "A",
            "invite-a19": "A", "invite-a20": "A", "invite-a21": "A",
            "invite-a22": "A",
            "invite-b": "B", "invite-b2": "B", "invite-b3": "B",
            "invite-b4": "B", "invite-b5": "B",
        }
        self.sessions: dict[str, list[dict]] = {
            "A": [
                self._session("s-a1", "A 会话", [
                    {"role": "user", "content": "A 私有问题"},
                    {"role": "assistant", "content": "A 私有回答"},
                ]),
                self._session("s-a2", "A 第二会话", []),
            ],
            "B": [
                self._session("s-b1", "B 会话", [
                    {"role": "user", "content": "B 私有问题"},
                    {"role": "assistant", "content": "B 私有回答"},
                ]),
            ],
        }
        self.next_session = 1
        self.runs: dict[str, dict] = {}
        self.next_run = 1
        self.turn_runs: dict[tuple[str, str, str], str] = {}
        self.hold_a_messages = False
        self.a_messages_waiting = threading.Event()
        self.a_messages_release = threading.Event()
        self.hold_a_settings = False
        self.a_settings_waiting = threading.Event()
        self.a_settings_release = threading.Event()
        self.identity_status = 200
        self.fail_next_chat = False
        self.hold_sessions = False
        self.sessions_waiting = threading.Event()
        self.sessions_release = threading.Event()
        self.hold_admission = False
        self.admission_waiting = threading.Event()
        self.admission_release = threading.Event()
        self.fail_next_chat_401 = False
        self.hold_chat_401 = False
        self.chat_401_waiting = threading.Event()
        self.chat_401_release = threading.Event()
        self.fail_next_question_status_401 = False
        self.fail_next_answer_503 = False
        self.fail_next_answer_403 = False
        self.fail_next_answer_409 = False
        self.fail_next_runs_401 = False
        self.stream_afters: list[int] = []
        self.question_run_ids: list[str] = []
        self.chat_turn_ids: list[tuple[str, str, str]] = []
        self.post_chat_count = 0
        self.stop_count = 0

    def _session(self, sid: str, title: str, messages: list[dict]) -> dict:
        return {
            "id": sid, "workspaceId": "workspace",
            "title": title, "archived": False,
            "messages": messages, "created": int(time.time()),
            "deleted": False,
        }

    def record(self, method: str, path: str) -> None:
        with self.lock:
            self.requests.append((method, path))

    def user_sessions(self, user: str) -> list[dict]:
        return [item for item in self.sessions[user] if not item["deleted"]]

    def find_session(self, user: str, sid: str) -> dict | None:
        return next((item for item in self.user_sessions(user)
                     if item["id"] == sid), None)

    def active_runs(self, user: str, sid: str) -> list[dict]:
        return [run for run in self.runs.values()
                if run["user"] == user and run["session"] == sid
                and not run["terminal"]]

    def create_run(self, user: str, sid: str, turn_id: str, text: str,
                   script: str) -> dict:
        with self.lock:
            key = (user, sid, turn_id)
            if key in self.turn_runs:
                return self.runs[self.turn_runs[key]]
            run_id = f"run-{self.next_run}"
            self.next_run += 1
            run = {
                "id": run_id, "user": user, "session": sid,
                "turnId": turn_id, "text": text, "script": script,
                "events": [], "next_event": 1, "terminal": False,
                "state": "accepted", "answered": False, "stopped": False,
                "asked": False, "release": threading.Event(),
                "drop_ready": False, "drop_done": False,
                "resume": threading.Event(),
                "condition": threading.Condition(self.lock),
            }
            self.runs[run_id] = run
            self.turn_runs[key] = run_id
            return run


def add_event(run: dict, event: str, data: object) -> None:
    with run["condition"]:
        if run["terminal"]:
            return
        run["events"].append({
            "id": run["next_event"], "event": event,
            "data": json.dumps(data, ensure_ascii=False),
        })
        run["next_event"] += 1
        run["condition"].notify_all()


def finish_run(run: dict, owner: FakeState, state: str,
               assistant: str | None = None) -> None:
    with run["condition"]:
        if run["terminal"]:
            return
        run["state"] = state
        run["events"].append({
            "id": run["next_event"], "event": "state",
            "data": json.dumps({"state": state}, ensure_ascii=False),
        })
        run["next_event"] += 1
        run["terminal"] = True
        if assistant is not None:
            session = owner.find_session(run["user"], run["session"])
            if session is not None:
                session["messages"].append(
                    {"role": "user", "content": run["text"]})
                session["messages"].append(
                    {"role": "assistant", "content": assistant})
        run["condition"].notify_all()


def run_script(run: dict, owner: FakeState) -> None:
    script = run["script"]
    add_event(run, "state", {"state": "running"})
    if script == "drop":
        add_event(run, "thinking", "思考唯一内容")
        add_event(run, "activity", "活动唯一内容")
    elif script != "drop_question":
        add_event(run, "thinking", "我先分析一下")
        add_event(run, "activity", "正在生成回答")
    if script == "drop_question":
        with run["condition"]:
            run["asked"] = True
            run["state"] = "awaiting_permission"
            run["events"].append({
                "id": run["next_event"], "event": "state",
                "data": json.dumps({"state": "awaiting_permission"}),
            })
            run["next_event"] += 1
            run["events"].append({
                "id": run["next_event"], "event": "question",
                "data": json.dumps(json.dumps({
                    "id": f"q-{run['id']}",
                    "questions": [{
                        "questionId": "permission",
                        "header": "执行授权",
                        "question": "允许本次执行吗？",
                        "options": [
                            {"label": "允许本次", "value": "qopt_allow"},
                            {"label": "拒绝", "value": "qopt_deny"},
                        ],
                    }],
                }, ensure_ascii=False)),
            })
            run["next_event"] += 1
            run["drop_ready"] = True
            run["condition"].notify_all()
        run["resume"].wait(60)
        if run["terminal"]:
            return
        add_event(run, "token", "问题后回答")
        finish_run(run, owner, "done", "问题后回答")
        return
    if script == "drop":
        with run["condition"]:
            run["drop_ready"] = True
            run["condition"].notify_all()
        run["resume"].wait(60)
        if run["terminal"]:
            return
        add_event(run, "token", "断线后的回答")
        finish_run(run, owner, "done", "断线后的回答")
        return
    if script == "question":
        with run["condition"]:
            if run["terminal"]:
                return
            run["asked"] = True
            run["state"] = "awaiting_permission"
            run["events"].append({
                "id": run["next_event"], "event": "state",
                "data": json.dumps({"state": "awaiting_permission"}),
            })
            run["next_event"] += 1
            run["events"].append({
                "id": run["next_event"], "event": "question",
                "data": json.dumps(json.dumps({
                    "id": f"q-{run['id']}",
                    "questions": [{
                        "questionId": "permission",
                        "header": "执行授权",
                        "question": "允许本次执行吗？",
                        "options": [
                            {"label": "允许本次", "value": "qopt_allow"},
                            {"label": "拒绝", "value": "qopt_deny"},
                        ],
                    }],
                }, ensure_ascii=False)),
            })
            run["next_event"] += 1
            run["condition"].notify_all()
            run["condition"].wait_for(
                lambda: run["answered"] or run["stopped"]
                or run["terminal"], timeout=60)
        if run["stopped"]:
            return
        if not run["terminal"]:
            add_event(run, "token", "已按你的选择继续。")
            finish_run(run, owner, "done", "已按你的选择继续。")
        return
    if script in ("held", "slow"):
        if script == "held":
            run["release"].wait(60)
        else:
            with run["condition"]:
                run["condition"].wait_for(
                    lambda: run["stopped"] or run["terminal"], timeout=60)
        if run["stopped"] or run["terminal"]:
            return
        add_event(run, "token", "延迟完成。")
        finish_run(run, owner, "done", "延迟完成。")
        return
    time.sleep(1.0)
    add_event(run, "token", "你好")
    finish_run(run, owner, "done", "你好")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> FakeState:
        return self.server.state

    def log_message(self, *args) -> None:  # noqa: D401 - quiet test server
        return

    # ---- helpers ----
    def _json(self, status: int, payload: object,
              headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, OSError):
            return

    def _empty(self, status: int = 204) -> None:
        self.send_response(status)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _cookie_user(self) -> str | None:
        cookie = self.headers.get("Cookie") or ""
        match = re.search(r"(?:^|;\s*)easel_test_session=([^;]+)", cookie)
        if not match:
            return None
        candidate = match.group(1)
        return candidate if candidate in ("A", "B") else None

    def _require_user(self) -> str | None:
        user = self._cookie_user()
        if user is None:
            self._json(401, {"code": "unauthorized"})
            return None
        return user

    def _require_csrf(self, user: str) -> bool:
        supplied = self.headers.get("X-CSRF-Token") or ""
        if supplied != USERS[user]["csrfToken"]:
            self._json(403, {"code": "csrf_invalid"})
            return False
        return True

    def _query(self) -> dict[str, list[str]]:
        return parse_qs(urlparse(self.path).query)

    def _static(self) -> bool:
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            file_path = DIST / "index.html"
        elif path == "/static/easel-icon-transparent.png":
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(PNG)))
            self.end_headers()
            self.wfile.write(PNG)
            return True
        else:
            relative = path.lstrip("/")
            file_path = (DIST / relative).resolve()
            if DIST.resolve() not in file_path.parents:
                return False
        if not file_path.is_file():
            return False
        body = file_path.read_bytes()
        content_type = mimetypes.guess_type(str(file_path))[0] \
            or "application/octet-stream"
        if file_path.suffix == ".js":
            content_type = "text/javascript; charset=utf-8"
        elif file_path.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True

    def _settings_models(self, user: str) -> dict:
        channel = {
            "configured": False, "source": "none", "version": 1,
            "mode": "none", "provider": None, "model": None,
            "base_url": None, "protocol": None, "options": {},
            "broker_adapter": None, "broker_supported": False,
            "override_allowed": True,
        }
        channels = {name: dict(channel) for name in
                    ("speech", "image", "video", "music", "chat")}
        channels["speech"].update({
            "configured": True, "source": "admin", "mode": "managed-model",
            "provider": "provider", "model": f"{user}-model",
            "allowed_models": [f"{user}-model"],
            "broker_adapter": "openai", "broker_supported": True,
            "override_allowed": True,
        })
        return {"version": 1, "channels": channels, "providers": {}}

    # ---- HTTP ----
    def do_GET(self) -> None:  # noqa: N802
        self.state.record("GET", self.path)
        parsed = urlparse(self.path)
        path = parsed.path
        if not path.startswith("/api/"):
            if not self._static():
                self._json(404, {"code": "not_found"})
            return
        if path == "/api/runtime":
            self._json(200, {"mode": self.state.mode})
            return
        if path == "/api/auth/me":
            if self.state.identity_status != 200:
                self._json(self.state.identity_status,
                           {"code": "service_unavailable"})
                return
            user = self._require_user()
            if user is not None:
                self._json(200, identity(user))
            return
        if path == "/api/status":
            self._json(200, {
                "gateway": True, "transport": "api", "runtime": "openclaw",
                "skills": [], "personas": [],
            })
            return
        if path == "/api/settings/bootstrap":
            self._json(200, {"modelConfigured": True})
            return
        if path == "/api/personas":
            self._json(200, [])
            return
        if path == "/api/upload/limits":
            self._json(200, {"max_mb": 50})
            return
        if path == "/api/settings/models":
            user = self._require_user()
            if user is None:
                return
            if user == "A" and self.state.hold_a_settings:
                self.state.a_settings_waiting.set()
                self.state.a_settings_release.wait(30)
            self._json(200, self._settings_models(user))
            return
        if path == "/api/settings/harness":
            if self._require_user() is None:
                return
            self._json(200, {
                "locked": False, "configured": False, "runtime": None,
                "revision": None, "model": None, "sandbox": None,
                "timeout": None, "codexAutoApprove": None,
                "unavailableReason": None,
            })
            return
        if path == "/api/admin/managed-config":
            if self._require_user() is None:
                return
            self._json(200, {"version": 1, "defaults": {}})
            return
        if path == "/api/admin/users":
            if self._require_user() is None:
                return
            self._json(200, {"users": [{
                "userId": USERS["A"]["userId"],
                "workspaceId": USERS["A"]["workspaceId"],
                "displayName": "成员 A", "role": "admin",
                "disabled": False, "createdAt": 1,
            }], "nextOffset": None})
            return
        if path == "/api/admin/invites":
            if self._require_user() is None:
                return
            self._json(200, {"invites": [], "nextOffset": None})
            return
        if path == "/api/sessions":
            user = self._require_user()
            if user is None:
                return
            if user == "A" and self.state.hold_sessions:
                self.state.hold_sessions = False
                self.state.sessions_waiting.set()
                self.state.sessions_release.wait(30)
            query = self._query()
            limit = int((query.get("limit") or ["100"])[0])
            offset = int((query.get("offset") or ["0"])[0])
            include = (query.get("include_archived") or ["false"])[0] == "true"
            items = [item for item in self.state.user_sessions(user)
                     if include or not item["archived"]]
            page = items[offset:offset + limit]
            next_offset = offset + len(page) if offset + len(page) < len(items) \
                else None
            self._json(200, {
                "sessions": [{
                    "id": item["id"],
                    "workspaceId": USERS[user]["workspaceId"],
                    "title": item["title"], "archived": item["archived"],
                } for item in page],
                "nextOffset": next_offset,
            })
            return
        match = re.fullmatch(r"/api/sessions/([^/]+)/messages", path)
        if match:
            user = self._require_user()
            if user is None:
                return
            sid = match.group(1)
            if user == "A" and self.state.hold_a_messages:
                self.state.a_messages_waiting.set()
                self.state.a_messages_release.wait(30)
            session = self.state.find_session(user, sid)
            if session is None:
                self._json(404, {"code": "not_found"})
                return
            query = self._query()
            limit = int((query.get("limit") or ["500"])[0])
            offset = int((query.get("offset") or ["0"])[0])
            rows = session["messages"]
            self._json(200, {
                "messages": rows[offset:offset + limit],
                "total": len(rows),
            })
            return
        match = re.fullmatch(r"/api/sessions/([^/]+)/runs", path)
        if match:
            user = self._require_user()
            if user is None:
                return
            sid = match.group(1)
            if self.state.fail_next_runs_401:
                self.state.fail_next_runs_401 = False
                self._json(401, {"code": "unauthorized"})
                return
            if self.state.find_session(user, sid) is None:
                self._json(404, {"code": "not_found"})
                return
            runs = self.state.active_runs(user, sid)
            self._json(200, {
                "runs": [{
                    "runId": run["id"], "conversationId": run["session"],
                    "state": run["state"],
                } for run in runs],
                "nextOffset": None,
            })
            return
        match = re.fullmatch(r"/api/chat/jobs/([^/]+)/stream", path)
        if match:
            user = self._require_user()
            if user is None:
                return
            run = self.state.runs.get(match.group(1))
            if run is None or run["user"] != user:
                self._json(404, {"code": "run_not_found"})
                return
            after = int((self._query().get("after") or ["0"])[0])
            with self.state.lock:
                self.state.stream_afters.append(after)
            self.send_response(200)
            self.send_header("Content-Type",
                             "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                while True:
                    with run["condition"]:
                        pending = [event for event in run["events"]
                                   if event["id"] > after]
                        terminal = run["terminal"]
                        drop = (run["drop_ready"] and not run["drop_done"]
                                and not pending)
                        if drop:
                            run["drop_done"] = True
                    if pending:
                        for event in pending:
                            frame = (
                                f"id: {event['id']}\n"
                                f"event: {event['event']}\n"
                                f"data: {event['data']}\n\n"
                            ).encode("utf-8")
                            self.wfile.write(frame)
                            self.wfile.flush()
                            after = event["id"]
                        continue
                    if drop or terminal:
                        break
                    with run["condition"]:
                        run["condition"].wait(0.1)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            return
        self._json(404, {"code": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        self.state.record("POST", self.path)
        path = urlparse(self.path).path
        if path == "/api/auth/exchange":
            body = self._body()
            token = body.get("token")
            user = self.state.invites.pop(token, None) \
                if isinstance(token, str) else None
            if user is None:
                self._json(401, {"code": "invalid_invite"})
                return
            cookie = (
                f"easel_test_session={user}; Path=/; HttpOnly; SameSite=Lax"
            )
            self._json(200, identity(user), {"Set-Cookie": cookie})
            return
        if path == "/api/auth/logout":
            user = self._require_user()
            if user is None:
                return
            if not self._require_csrf(user):
                return
            self._json(204, {}, {
                "Set-Cookie": "easel_test_session=; Path=/; Max-Age=0",
            })
            return
        if path == "/api/sessions":
            user = self._require_user()
            if user is None:
                return
            if not self._require_csrf(user):
                return
            body = self._body()
            title = body.get("title")
            if not isinstance(title, str) or not title:
                self._json(400, {"code": "invalid_argument"})
                return
            with self.state.lock:
                sid = f"s-new-{self.state.next_session}"
                self.state.next_session += 1
                session = self.state._session(sid, title, [])
                self.state.sessions[user].insert(0, session)
            self._json(201, {
                "id": session["id"], "title": session["title"],
                "archived": session["archived"],
                "workspaceId": USERS[user]["workspaceId"],
            })
            return
        if path == "/api/chat":
            user = self._require_user()
            if user is None:
                return
            if not self._require_csrf(user):
                return
            body = self._body()
            sid = body.get("sessionId")
            turn_id = body.get("turnId")
            text = body.get("message")
            if (not isinstance(sid, str) or not isinstance(turn_id, str)
                    or not isinstance(text, str)
                    or self.state.find_session(user, sid) is None):
                self._json(400, {"code": "invalid_argument"})
                return
            if self.state.hold_chat_401 and "迟到401" in text:
                self.state.hold_chat_401 = False
                self.state.chat_401_waiting.set()
                self.state.chat_401_release.wait(30)
                self._json(401, {"code": "unauthorized"})
                return
            if self.state.fail_next_chat_401:
                self.state.fail_next_chat_401 = False
                self._json(401, {"code": "unauthorized"})
                return
            script = "default"
            if "需要问题" in text:
                script = "question"
            elif "断线问题" in text:
                script = "drop_question"
            elif "断线思考" in text:
                script = "drop"
            elif "延迟" in text:
                script = "held"
            elif "停止" in text:
                script = "slow"
            with self.state.lock:
                self.state.chat_turn_ids.append((user, sid, turn_id))
                fail_once = self.state.fail_next_chat
                self.state.fail_next_chat = False
            if fail_once:
                self._json(503, {"code": "service_unavailable"})
                return
            with self.state.lock:
                self.state.post_chat_count += 1
                existing = self.state.turn_runs.get((user, sid, turn_id))
            if existing is None:
                run = self.state.create_run(user, sid, turn_id, text, script)
                threading.Thread(
                    target=run_script, args=(run, self.state),
                    daemon=True).start()
                duplicate = False
            else:
                run = self.state.runs[existing]
                duplicate = True
            if self.state.hold_admission and "延迟接纳" in text:
                self.state.hold_admission = False
                self.state.admission_waiting.set()
                self.state.admission_release.wait(30)
            self._json(202, {
                "runId": run["id"], "state": run["state"],
                "duplicate": duplicate,
            })
            return
        if path == "/api/chat/stop":
            user = self._require_user()
            if user is None:
                return
            if not self._require_csrf(user):
                return
            body = self._body()
            run = self.state.runs.get(body.get("runId"))
            if run is None or run["user"] != user:
                self._json(404, {"code": "run_not_found"})
                return
            with run["condition"]:
                if run["terminal"]:
                    self._json(200, {"stopped": False})
                    return
                run["stopped"] = True
                self.state.stop_count += 1
            finish_run(run, self.state, "cancelled", "（已停止）")
            self._json(200, {"stopped": True})
            return
        if path == "/api/chat/question/status":
            user = self._require_user()
            if user is None:
                return
            if self.state.fail_next_question_status_401:
                self.state.fail_next_question_status_401 = False
                self._json(401, {"code": "unauthorized"})
                return
            body = self._body()
            run = self.state.runs.get(body.get("runId"))
            if run is None or run["user"] != user:
                self._json(404, {"code": "run_not_found"})
                return
            with self.state.lock:
                self.state.question_run_ids.append(run["id"])
            questions = {}
            for question_id in body.get("questionIds") or []:
                if run["answered"]:
                    status = "answered"
                elif run["asked"] and not run["terminal"]:
                    status = "pending"
                else:
                    status = "not_found"
                questions[str(question_id)] = {"status": status}
            self._json(200, {"questions": questions})
            return
        if path == "/api/chat/question/answer":
            user = self._require_user()
            if user is None:
                return
            if not self._require_csrf(user):
                return
            body = self._body()
            run = self.state.runs.get(body.get("runId"))
            if run is None or run["user"] != user:
                self._json(404, {"code": "run_not_found"})
                return
            with run["condition"]:
                fail_503 = self.state.fail_next_answer_503
                fail_403 = self.state.fail_next_answer_403
                fail_409 = self.state.fail_next_answer_409
                self.state.fail_next_answer_503 = False
                self.state.fail_next_answer_403 = False
                self.state.fail_next_answer_409 = False
            if fail_503:
                self._json(503, {"code": "service_unavailable"})
                return
            if fail_403:
                self._json(403, {"code": "forbidden"})
                return
            if fail_409:
                self._json(409, {"code": "question_expired"})
                return
            with run["condition"]:
                if run["terminal"] or run["answered"] or not run["asked"]:
                    self._json(409, {"code": "question_expired"})
                    return
                run["answered"] = True
                run["condition"].notify_all()
            self._json(200, {"ok": True})
            return
        if path == "/api/chat/stream":
            body = self._body()
            text = body.get("message") or ""
            self.send_response(200)
            self.send_header("Content-Type",
                             "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            frames = [
                ("thinking", json.dumps("本地思考", ensure_ascii=False)),
                ("activity", json.dumps("本地执行", ensure_ascii=False)),
                ("token", json.dumps(
                    f"本地回答：{text}", ensure_ascii=False)),
                ("done", json.dumps({"sessionKey": "local-1"})),
            ]
            try:
                for index, (event, data) in enumerate(frames, start=1):
                    self.wfile.write(
                        f"id: {index}\nevent: {event}\ndata: {data}\n\n"
                        .encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            return
        self._json(404, {"code": "not_found"})

    def do_PATCH(self) -> None:  # noqa: N802
        self.state.record("PATCH", self.path)
        path = urlparse(self.path).path
        match = re.fullmatch(r"/api/sessions/([^/]+)", path)
        if not match:
            self._json(404, {"code": "not_found"})
            return
        user = self._require_user()
        if user is None:
            return
        if not self._require_csrf(user):
            return
        session = self.state.find_session(user, match.group(1))
        if session is None:
            self._json(404, {"code": "not_found"})
            return
        body = self._body()
        if isinstance(body.get("title"), str):
            session["title"] = body["title"]
        if isinstance(body.get("archived"), bool):
            session["archived"] = body["archived"]
        self._json(200, {
            "id": session["id"], "title": session["title"],
            "archived": session["archived"],
        })

    def do_DELETE(self) -> None:  # noqa: N802
        self.state.record("DELETE", self.path)
        path = urlparse(self.path).path
        match = re.fullmatch(r"/api/sessions/([^/]+)", path)
        if not match:
            self._json(404, {"code": "not_found"})
            return
        user = self._require_user()
        if user is None:
            return
        if not self._require_csrf(user):
            return
        session = self.state.find_session(user, match.group(1))
        if session is None:
            self._json(404, {"code": "not_found"})
            return
        session["deleted"] = True
        self._empty(204)


class QuietHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionAbortedError, ConnectionResetError,
                               BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class Server:
    def __init__(self, mode: str) -> None:
        self.state = FakeState(mode)
        self.httpd = QuietHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.state = self.state
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True)

    @property
    def origin(self) -> str:
        host, port = self.httpd.server_address
        return f"http://127.0.0.1:{port}"

    def start(self) -> "Server":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def hold_a_messages(self) -> None:
        with self.state.lock:
            self.state.hold_a_messages = True

    def release_a_messages(self) -> None:
        self.state.a_messages_release.set()

    def hold_a_settings(self) -> None:
        with self.state.lock:
            self.state.hold_a_settings = True

    def release_a_settings(self) -> None:
        self.state.a_settings_release.set()

    def set_identity_status(self, status: int) -> None:
        with self.state.lock:
            self.state.identity_status = status

    def hold_sessions_once(self) -> None:
        with self.state.lock:
            self.state.hold_sessions = True
            self.state.sessions_waiting.clear()
            self.state.sessions_release.clear()

    def release_sessions(self) -> None:
        self.state.sessions_release.set()

    def hold_admission_once(self) -> None:
        with self.state.lock:
            self.state.hold_admission = True
            self.state.admission_waiting.clear()
            self.state.admission_release.clear()

    def release_admission(self) -> None:
        self.state.admission_release.set()

    def fail_chat_401_once(self) -> None:
        with self.state.lock:
            self.state.fail_next_chat_401 = True

    def hold_chat_401_once(self) -> None:
        with self.state.lock:
            self.state.hold_chat_401 = True
            self.state.chat_401_waiting.clear()
            self.state.chat_401_release.clear()

    def release_chat_401(self) -> None:
        self.state.chat_401_release.set()

    def fail_question_status_401_once(self) -> None:
        with self.state.lock:
            self.state.fail_next_question_status_401 = True

    def fail_answer_503_once(self) -> None:
        with self.state.lock:
            self.state.fail_next_answer_503 = True

    def fail_answer_403_once(self) -> None:
        with self.state.lock:
            self.state.fail_next_answer_403 = True

    def fail_answer_409_once(self) -> None:
        with self.state.lock:
            self.state.fail_next_answer_409 = True

    def fail_runs_401_once(self) -> None:
        with self.state.lock:
            self.state.fail_next_runs_401 = True

    def abandon_runs(self, user: str, sid: str | None = None) -> None:
        with self.state.lock:
            for run in self.state.runs.values():
                if run["user"] != user:
                    continue
                if sid is not None and run["session"] != sid:
                    continue
                if run["terminal"]:
                    continue
                run["stopped"] = True
                run["terminal"] = True
                run["release"].set()
                run["resume"].set()
                run["condition"].notify_all()

    def seed_sessions(self, user: str, count: int) -> list[str]:
        ids: list[str] = []
        with self.state.lock:
            for index in range(count):
                sid = f"s-seed-{user}-{self.state.next_session}"
                self.state.next_session += 1
                self.state.sessions[user].append(self.state._session(
                    sid, f"分页会话 {index}", []))
                ids.append(sid)
        return ids

    def add_long_session(self, user: str, count: int = 501) -> str:
        with self.state.lock:
            sid = f"s-long-{user}-{self.state.next_session}"
            self.state.next_session += 1
            messages = []
            for index in range(count):
                role = "user" if index % 2 == 0 else "assistant"
                messages.append({"role": role,
                                 "content": f"长历史第 {index} 条"})
            self.state.sessions[user].insert(0, self.state._session(
                sid, "长历史会话", messages))
            return sid

    def remove_sessions(self, user: str, ids: list[str]) -> None:
        with self.state.lock:
            self.state.sessions[user] = [
                item for item in self.state.sessions[user]
                if item["id"] not in set(ids)]

    def request_paths(self) -> list[str]:
        with self.state.lock:
            return [path for _method, path in self.state.requests]


async def wait_for(predicate, timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if inspect.isawaitable(value):
            value = await value
        if value:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(message)


async def login(page, origin: str, invite: str) -> None:
    await page.goto(origin, wait_until="domcontentloaded")
    await page.fill("#invite", invite)
    await page.get_by_role("button", name="进入工作区").click()
    await page.locator(".sidebar").wait_for(timeout=15000)


async def logout(page) -> None:
    await page.get_by_role("button", name="退出").click()
    await page.locator("#invite").wait_for(timeout=15000)


async def select_session(page, title: str) -> None:
    await page.locator(".session-item", has_text=title).click()
    await page.locator(".session-item.active", has_text=title).wait_for(
        timeout=15000)
    await page.locator(".chat-input").wait_for(timeout=15000)


async def scenario_control_layout(browser, server: Server,
                                  results: dict, failures: list,
                                  screenshots: list) -> None:
    context = await browser.new_context()
    page = await context.new_page()
    try:
        await page.goto(server.origin, wait_until="domcontentloaded")
        await page.locator("#invite").wait_for(timeout=15000)
        assert await page.locator(".sidebar").count() == 0
        assert await page.locator(".chat-page").count() == 0
        assert "A 私有问题" not in (await page.text_content("body") or "")
        before = server.request_paths()
        assert not any(path.startswith("/api/sessions")
                       for path in before), before

        await page.fill("#invite", "invite-a")
        await page.get_by_role("button", name="进入工作区").click()
        await page.locator(".sidebar").wait_for(timeout=15000)
        await page.locator(".chat-page").wait_for(timeout=15000)
        body = await page.text_content("body") or ""
        for label in ("工作台", "对话", "技能库", "内容库", "账号", "画像"):
            assert label in body, label
        assert "A 会话" in body
        assert "A 私有问题" in body
        assert "A 私有回答" in body
        assert not any(path.startswith("/api/upload/limits")
                       for path in server.request_paths())
        results["control_original_layout"] = True
        await page.screenshot(path=str(EVIDENCE / "01-control-layout.png"))

        await page.get_by_role("button", name="技能库").click()
        await page.get_by_text("暂未接通").first.wait_for(timeout=10000)
        paths = server.request_paths()
        assert not any(path.startswith("/api/skills") for path in paths), paths
        results["control_unavailable_menu"] = True
        await page.screenshot(path=str(
            EVIDENCE / "02-control-unavailable.png"))

        await page.get_by_role("button", name="设置").click()
        await page.get_by_role("heading", name="个人设置").wait_for(
            timeout=15000)
        await page.get_by_role("tab", name="全体默认").click()
        await page.get_by_role("heading", name="全体默认").wait_for(
            timeout=15000)
        paths = server.request_paths()
        assert any(path.startswith("/api/settings/models") for path in paths)
        assert not any(path.startswith("/api/env/tools") for path in paths)
        assert not any(path.startswith("/api/settings/bootstrap")
                       for path in paths)
        results["control_settings_bridge"] = True
        await page.screenshot(path=str(
            EVIDENCE / "03-control-settings.png"))
    except Exception as error:  # noqa: BLE001
        failures.append(f"control-layout: {error!r}")
    finally:
        await context.close()


async def scenario_control_chat(browser, server: Server,
                                results: dict, failures: list,
                                screenshots: list) -> None:
    context = await browser.new_context()
    page = await context.new_page()
    try:
        await login(page, server.origin, "invite-a2")
        await select_session(page, "A 第二会话")
        await page.locator(".chat-input").fill("正常问题")
        await page.locator(".send-btn").click()
        await page.get_by_text("我先分析一下").first.wait_for(timeout=15000)
        await page.get_by_text("正在生成回答").first.wait_for(timeout=15000)
        await page.get_by_text("你好", exact=True).last.wait_for(
            timeout=15000)
        await page.screenshot(path=str(EVIDENCE / "04-control-chat.png"))
        results["control_text_thinking_activity"] = True
    except Exception as error:  # noqa: BLE001
        failures.append(f"control-chat: {error!r}")
    finally:
        await context.close()


async def scenario_question_and_stop(browser, server: Server,
                                     results: dict, failures: list,
                                     screenshots: list) -> None:
    context = await browser.new_context()
    page = await context.new_page()
    try:
        await login(page, server.origin, "invite-a3")
        await select_session(page, "A 第二会话")
        await page.locator(".chat-input").fill("需要问题的问题")
        await page.locator(".send-btn").click()
        await page.get_by_text("允许本次", exact=True).wait_for(timeout=15000)
        await page.get_by_role("button", name="允许本次").click()
        await page.get_by_role("button", name="提交").click()
        await page.get_by_text("已按你的选择继续。", exact=True).last.wait_for(
            timeout=15000)
        results["control_question_card"] = True

        await page.locator(".chat-input").fill("停止本轮")
        await page.locator(".send-btn").click()
        await page.locator('button[title="停止生成"]').wait_for(
            timeout=15000)
        await page.locator('button[title="停止生成"]').click()
        await wait_for(
            lambda: server.state.stop_count >= 1, 10,
            "停止请求未到达")
        await page.get_by_text("（已停止）", exact=True).last.wait_for(
            timeout=15000)
        results["control_stop"] = True
        await page.screenshot(path=str(EVIDENCE / "05-control-stop.png"))

        await page.locator(".chat-input").fill("继续内容")
        await page.locator(".send-btn").click()
        await page.get_by_text("继续内容", exact=True).last.wait_for(
            timeout=15000)
        await wait_for(
            lambda: any(run["text"] == "继续内容" and run["terminal"]
                        for run in server.state.runs.values()), 15,
            "停止后继续未完成")
        results["control_continue_after_stop"] = True
    except Exception as error:  # noqa: BLE001
        failures.append(f"question-stop: {error!r}")
    finally:
        await context.close()


async def scenario_refresh_reconnect(browser, server: Server,
                                     results: dict, failures: list,
                                     screenshots: list) -> None:
    context = await browser.new_context()
    page = await context.new_page()
    try:
        await login(page, server.origin, "invite-a4")
        await select_session(page, "A 第二会话")
        await page.locator(".chat-input").fill("延迟完成")
        await page.locator(".send-btn").click()
        await page.get_by_text("正在生成回答").first.wait_for(timeout=15000)
        posts_before = server.state.post_chat_count
        await page.reload(wait_until="domcontentloaded")
        await page.locator(".sidebar").wait_for(timeout=15000)
        await select_session(page, "A 第二会话")
        await page.get_by_text("正在生成回答").first.wait_for(timeout=15000)
        assert server.state.post_chat_count == posts_before, (
            "刷新后不得重发 prompt")
        held = next(run for run in server.state.runs.values()
                    if run["text"] == "延迟完成")
        held["release"].set()
        await page.get_by_text("延迟完成。", exact=True).last.wait_for(
            timeout=15000)
        assert server.state.post_chat_count == posts_before
        results["control_refresh_reconnect"] = True
        await page.screenshot(path=str(
            EVIDENCE / "06-control-reconnect.png"))
    except Exception as error:  # noqa: BLE001
        failures.append(f"refresh-reconnect: {error!r}")
    finally:
        await context.close()


async def scenario_cross_identity(browser, server: Server,
                                  results: dict, failures: list,
                                  screenshots: list) -> None:
    context = await browser.new_context()
    page = await context.new_page()
    try:
        await login(page, server.origin, "invite-a5")
        await select_session(page, "A 第二会话")
        with server.state.lock:
            posts_before = server.state.post_chat_count
        await page.locator(".chat-input").fill("延迟的 A 任务")
        await page.locator(".send-btn").click()
        await wait_for(
            lambda: server.state.post_chat_count == posts_before + 1, 10,
            "cross-identity 未派发新 Run")
        await page.get_by_text("正在生成回答").first.wait_for(timeout=15000)
        await page.locator(".chat-input").fill("A 切换后的草稿")
        await logout(page)
        keys = await page.evaluate(
            "() => [...Object.keys(localStorage), "
            "...Object.keys(sessionStorage)]")
        assert not any("user-a" in key or "workspace-a" in key
                       for key in keys), keys

        await page.fill("#invite", "invite-b")
        await page.get_by_role("button", name="进入工作区").click()
        await page.locator(".sidebar").wait_for(timeout=15000)
        await page.get_by_text("B 会话", exact=True).wait_for(timeout=15000)
        runs = [(run["id"], run["text"], run["state"], run["terminal"])
                for run in server.state.runs.values()]
        assert any(run["text"] == "延迟的 A 任务"
                   for run in server.state.runs.values()), runs
        held = next(run for run in server.state.runs.values()
                    if run["text"] == "延迟的 A 任务")
        held["release"].set()
        await asyncio.sleep(1.0)
        body = await page.text_content("body") or ""
        assert "B 私有回答" in body
        assert "A 私有回答" not in body
        assert "A 切换后的草稿" not in body
        assert "延迟的 A 任务" not in body
        assert await page.locator(".question-card").count() == 0
        assert await page.locator(".chat-input").input_value() == ""
        results["control_cross_identity_late_results"] = True
        await page.screenshot(path=str(EVIDENCE / "07-control-identity.png"))
    except Exception as error:  # noqa: BLE001
        failures.append(f"cross-identity: {error!r}")
    finally:
        await context.close()


async def scenario_cross_tab(browser, server: Server,
                             results: dict, failures: list,
                             screenshots: list) -> None:
    context = await browser.new_context()
    page1 = await context.new_page()
    page2 = await context.new_page()
    try:
        await login(page1, server.origin, "invite-a6")
        await page2.goto(server.origin, wait_until="domcontentloaded")
        await page2.locator(".sidebar").wait_for(timeout=15000)
        await logout(page1)
        await page2.locator("#invite").wait_for(timeout=15000)
        await page2.fill("#invite", "invite-b2")
        await page2.get_by_role("button", name="进入工作区").click()
        await page2.get_by_text("B 会话", exact=True).wait_for(timeout=15000)
        await page1.locator(".sidebar").first.wait_for(timeout=15000)
        await page1.get_by_text("B 会话", exact=True).wait_for(timeout=15000)
        body1 = await page1.text_content("body") or ""
        body2 = await page2.text_content("body") or ""
        assert "A 私有回答" not in body1 and "A 私有回答" not in body2
        results["control_cross_tab_identity"] = True
        await page1.screenshot(path=str(
            EVIDENCE / "08-cross-tab-tab1.png"))
        await page2.screenshot(path=str(
            EVIDENCE / "09-cross-tab-tab2.png"))
    except Exception as error:  # noqa: BLE001
        failures.append(f"cross-tab: {error!r}")
    finally:
        await context.close()


async def scenario_delayed_history(browser, server: Server,
                                   results: dict, failures: list,
                                   screenshots: list) -> None:
    context = await browser.new_context()
    page = await context.new_page()
    try:
        server.hold_a_messages()
        await page.goto(server.origin, wait_until="domcontentloaded")
        await page.fill("#invite", "invite-a7")
        await page.get_by_role("button", name="进入工作区").click()
        await page.locator(".sidebar").wait_for(timeout=15000)
        await wait_for(
            lambda: server.state.a_messages_waiting.is_set(), 10,
            "A 历史请求未挂起")
        await logout(page)
        await page.fill("#invite", "invite-b3")
        await page.get_by_role("button", name="进入工作区").click()
        await page.locator(".sidebar").wait_for(timeout=15000)
        await page.get_by_text("B 会话", exact=True).wait_for(timeout=15000)
        server.release_a_messages()
        await asyncio.sleep(1.0)
        body = await page.text_content("body") or ""
        assert "B 私有回答" in body
        assert "A 私有回答" not in body
        results["control_delayed_history"] = True
        await page.screenshot(path=str(
            EVIDENCE / "10-delayed-history.png"))
    except Exception as error:  # noqa: BLE001
        failures.append(f"delayed-history: {error!r}")
    finally:
        server.release_a_messages()
        await context.close()


async def scenario_local_mode(browser, server: Server,
                              results: dict, failures: list,
                              screenshots: list) -> None:
    context = await browser.new_context()
    await context.add_init_script(
        "localStorage.setItem('easel_sessions', JSON.stringify([{"
        "id:'local-1',title:'本地会话',created:1,messages:["
        "{role:'user',content:'旧问题'},"
        "{role:'assistant',content:'旧回答'}]}]));"
        "localStorage.setItem('easel_active_session','local-1');"
        "localStorage.setItem('easel_onboarding_seen','1');")
    page = await context.new_page()
    try:
        await page.goto(server.origin, wait_until="domcontentloaded")
        await page.locator(".sidebar").wait_for(timeout=15000)
        await page.get_by_role("button", name="对话", exact=True).click()
        await page.locator(".chat-page").wait_for(timeout=15000)
        await page.get_by_text("旧问题", exact=True).wait_for(timeout=15000)
        await page.get_by_text("旧回答", exact=True).wait_for(timeout=15000)
        await page.locator(".chat-input").fill("本地的新问题")
        await page.locator(".send-btn").click()
        await page.get_by_text("本地回答：本地的新问题", exact=True).last.wait_for(
            timeout=15000)
        paths = server.request_paths()
        assert any(path.startswith("/api/upload/limits") for path in paths), paths
        results["local_original_chat"] = True
        await page.screenshot(path=str(EVIDENCE / "11-local-mode.png"))
    except Exception as error:  # noqa: BLE001
        failures.append(f"local-mode: {error!r}")
    finally:
        await context.close()


async def scenario_selection_interleaving(browser, server: Server,
                                          results: dict, failures: list,
                                          screenshots: list) -> None:
    context = await browser.new_context()
    page = await context.new_page()
    try:
        await login(page, server.origin, "invite-a12")
        await select_session(page, "A 会话")
        server.hold_sessions_once()
        await page.evaluate(
            "() => { const ch = new BroadcastChannel("
            "'easel-control:user-a:workspace-a');"
            " ch.postMessage({type:'sessions-changed'}); ch.close(); }")
        await wait_for(
            lambda: server.state.sessions_waiting.is_set(), 10,
            "目录刷新未挂起")
        await page.locator(".session-item", has_text="A 第二会话").click()
        await page.locator(".session-item.active",
                           has_text="A 第二会话").wait_for(timeout=15000)
        server.release_sessions()
        await asyncio.sleep(0.8)
        active_text = await page.locator(
            ".session-item.active").first.text_content() or ""
        assert "A 第二会话" in active_text, active_text
        results["control_selection_interleaving"] = True
        await page.screenshot(path=str(
            EVIDENCE / "17-selection-interleaving.png"))
    except Exception as error:  # noqa: BLE001
        failures.append(f"selection-interleaving: {error!r}")
    finally:
        server.release_sessions()
        await context.close()


async def scenario_admission_ordering(browser, server: Server,
                                      results: dict, failures: list,
                                      screenshots: list) -> None:
    context = await browser.new_context()
    page = await context.new_page()
    try:
        await login(page, server.origin, "invite-a13")
        await select_session(page, "A 第二会话")
        server.hold_admission_once()
        await page.locator(".chat-input").fill("延迟接纳")
        await page.locator(".send-btn").click()
        await wait_for(
            lambda: server.state.admission_waiting.is_set(), 10,
            "接纳响应未挂起")
        await page.locator(".session-item", has_text="A 会话").click()
        await page.locator(".session-item.active",
                           has_text="A 会话").wait_for(timeout=15000)
        server.release_admission()
        await asyncio.sleep(0.8)
        active_text = await page.locator(
            ".session-item.active").first.text_content() or ""
        assert "A 会话" in active_text, active_text
        assert "我先分析一下" not in (
            await page.text_content("body") or "")

        await page.locator(".session-item", has_text="A 第二会话").click()
        await page.locator(".session-item.active",
                           has_text="A 第二会话").wait_for(timeout=15000)
        await page.get_by_text("我先分析一下").first.wait_for(timeout=15000)
        held = next(run for run in server.state.runs.values()
                    if run["text"] == "延迟接纳")
        held["release"].set()
        await page.get_by_text("延迟完成。", exact=True).last.wait_for(
            timeout=15000)
        results["control_admission_ordering"] = True
        await page.screenshot(path=str(
            EVIDENCE / "18-admission-ordering.png"))
    except Exception as error:  # noqa: BLE001
        failures.append(f"admission-ordering: {error!r}")
    finally:
        for run in server.state.runs.values():
            if run["text"] == "延迟接纳":
                run["release"].set()
        await context.close()


async def scenario_identity_unavailable(browser, server: Server,
                                        results: dict, failures: list,
                                        screenshots: list) -> None:
    context = await browser.new_context()
    await context.add_init_script(
        "localStorage.setItem('easel_sessions', JSON.stringify([{"
        "id:'private-a',title:'A 私有缓存',created:1,messages:["
        "{role:'user',content:'A 私有缓存内容'}]}]));")
    page = await context.new_page()
    try:
        server.set_identity_status(503)
        with server.state.lock:
            before = len(server.state.requests)
        await page.goto(server.origin, wait_until="domcontentloaded")
        await page.get_by_text("服务暂不可用").first.wait_for(timeout=15000)
        assert await page.locator(".sidebar").count() == 0
        assert await page.locator(".chat-page").count() == 0
        body = await page.text_content("body") or ""
        assert "A 私有缓存内容" not in body
        with server.state.lock:
            paths = [path for _method, path
                     in server.state.requests[before:]]
        assert not any(path.startswith("/api/sessions")
                       for path in paths), paths
        results["control_identity_unavailable"] = True
        await page.screenshot(path=str(
            EVIDENCE / "12-identity-unavailable.png"))
    except Exception as error:  # noqa: BLE001
        failures.append(f"identity-unavailable: {error!r}")
    finally:
        server.set_identity_status(200)
        await context.close()


async def scenario_session_crud(browser, server: Server,
                                results: dict, failures: list,
                                screenshots: list) -> None:
    context = await browser.new_context()
    page = await context.new_page()
    sid = None
    try:
        await login(page, server.origin, "invite-a8")
        await page.get_by_role("button", name="新对话").click()
        item = page.locator(".session-item", has_text="新对话").first
        await item.wait_for(timeout=15000)
        with server.state.lock:
            created = next((row for row in server.state.sessions["A"]
                            if row["title"] == "新对话"), None)
        assert created is not None
        sid = created["id"]

        await item.hover()
        await item.locator('button[title="重命名"]').click()
        rename = page.locator(".session-rename-input")
        await rename.wait_for(timeout=15000)
        await rename.fill("改名会话")
        await rename.press("Enter")
        await page.locator(".session-item", has_text="改名会话").first.wait_for(
            timeout=15000)

        renamed = page.locator(".session-item",
                               has_text="改名会话").first
        await renamed.hover()
        await renamed.locator('button[title="归档"]').click()
        await wait_for(
            lambda: server.state.find_session("A", sid)["archived"], 10,
            "归档未生效")
        await page.get_by_text("已归档 · 1").click()
        archived = page.locator(".session-item",
                                has_text="改名会话").first
        await archived.wait_for(timeout=15000)

        page.on("dialog", lambda dialog:
                asyncio.ensure_future(dialog.accept()))
        await archived.hover()
        await archived.locator('button[title="删除"]').click()
        await wait_for(
            lambda: server.state.find_session("A", sid) is None, 10,
            "删除未生效")
        results["control_session_crud"] = True
        await page.screenshot(path=str(
            EVIDENCE / "13-session-crud.png"))
    except Exception as error:  # noqa: BLE001
        failures.append(f"session-crud: {error!r}")
    finally:
        if sid is not None:
            server.remove_sessions("A", [sid])
        await context.close()


async def scenario_history_pagination(browser, server: Server,
                                      results: dict, failures: list,
                                      screenshots: list) -> None:
    context = await browser.new_context()
    page = await context.new_page()
    sid = None
    try:
        sid = server.add_long_session("A", 501)
        await login(page, server.origin, "invite-a9")
        await select_session(page, "长历史会话")
        await page.get_by_text("长历史第 500 条", exact=True).wait_for(
            timeout=30000)
        older = page.get_by_role("button", name="加载更早历史")
        await older.wait_for(timeout=15000)
        await older.click()
        await page.get_by_text("长历史第 0 条", exact=True).wait_for(
            timeout=15000)
        results["control_history_pagination"] = True
        await page.screenshot(path=str(
            EVIDENCE / "14-history-pagination.png"))
    except Exception as error:  # noqa: BLE001
        failures.append(f"history-pagination: {error!r}")
    finally:
        if sid is not None:
            server.remove_sessions("A", [sid])
        await context.close()


async def scenario_failure_preserves(browser, server: Server,
                                     results: dict, failures: list,
                                     screenshots: list) -> None:
    context = await browser.new_context()
    page = await context.new_page()
    try:
        await login(page, server.origin, "invite-a10")
        await page.get_by_text("A 私有回答", exact=True).wait_for(
            timeout=15000)
        with server.state.lock:
            before = len(server.state.chat_turn_ids)
            server.state.fail_next_chat = True
        await page.locator(".chat-input").fill("失败发送")
        await page.locator(".send-btn").click()
        await page.get_by_text("发送失败", exact=False).first.wait_for(
            timeout=15000)
        assert "A 私有回答" in (await page.text_content("body") or "")
        retry = page.get_by_role("button", name="重试同一请求")
        await retry.wait_for(timeout=15000)
        await retry.click()
        await wait_for(
            lambda: len(server.state.chat_turn_ids) >= before + 2, 15,
            "重试请求未到达")
        with server.state.lock:
            first = server.state.chat_turn_ids[before]
            second = server.state.chat_turn_ids[before + 1]
        assert first[2] == second[2], (first, second)
        await wait_for(
            lambda: any(run["text"] == "失败发送" and run["terminal"]
                        for run in server.state.runs.values()), 15,
            "重试未完成")
        assert "A 私有回答" in (await page.text_content("body") or "")
        results["control_failure_preserves_completed"] = True
        await page.screenshot(path=str(
            EVIDENCE / "15-failure-preserves.png"))
    except Exception as error:  # noqa: BLE001
        failures.append(f"failure-preserves: {error!r}")
    finally:
        with server.state.lock:
            server.state.fail_next_chat = False
        await context.close()


async def scenario_settings_delayed(browser, server: Server,
                                    results: dict, failures: list,
                                    screenshots: list) -> None:
    context = await browser.new_context()
    page = await context.new_page()
    try:
        server.hold_a_settings()
        await login(page, server.origin, "invite-a11")
        await page.get_by_role("button", name="设置").click()
        await page.get_by_role("heading", name="个人设置").wait_for(
            timeout=15000)
        await wait_for(
            lambda: server.state.a_settings_waiting.is_set(), 10,
            "A 设置请求未挂起")
        await logout(page)
        await page.fill("#invite", "invite-b4")
        await page.get_by_role("button", name="进入工作区").click()
        await page.locator(".sidebar").wait_for(timeout=15000)
        await page.get_by_role("button", name="设置").click()
        await page.get_by_role("heading", name="个人设置").wait_for(
            timeout=15000)
        await page.get_by_text("B-model", exact=False).first.wait_for(
            timeout=15000)
        server.release_a_settings()
        await asyncio.sleep(1.0)
        body = await page.text_content("body") or ""
        assert "B-model" in body
        assert "A-model" not in body
        results["control_settings_delayed"] = True
        await page.screenshot(path=str(
            EVIDENCE / "16-settings-delayed.png"))
    except Exception as error:  # noqa: BLE001
        failures.append(f"settings-delayed: {error!r}")
    finally:
        with server.state.lock:
            server.state.hold_a_settings = False
        server.release_a_settings()
        await context.close()


async def scenario_r1_submit_401(browser, server: Server,
                                 results: dict, failures: list,
                                 screenshots: list) -> None:
    context = await browser.new_context()
    page = await context.new_page()
    try:
        await login(page, server.origin, "invite-a14")
        await select_session(page, "A 第二会话")
        server.fail_chat_401_once()
        await page.locator(".chat-input").fill("提交401")
        await page.locator(".send-btn").click()
        await page.get_by_text("登录已失效", exact=False).first.wait_for(
            timeout=15000)
        await page.locator("#invite").wait_for(timeout=15000)
        assert await page.locator(".sidebar").count() == 0
        body = await page.text_content("body") or ""
        assert "发送中" not in body
        results["r1_submit_401"] = True
        await page.screenshot(path=str(EVIDENCE / "19-r1-submit-401.png"))
    except Exception as error:  # noqa: BLE001
        failures.append(f"r1-submit-401: {error!r}")
    finally:
        await context.close()


async def scenario_r1_question_status_401(browser, server: Server,
                                          results: dict, failures: list,
                                          screenshots: list) -> None:
    context = await browser.new_context()
    page = await context.new_page()
    try:
        await login(page, server.origin, "invite-a15")
        await select_session(page, "A 第二会话")
        server.fail_question_status_401_once()
        await page.locator(".chat-input").fill("需要问题")
        await page.locator(".send-btn").click()
        await page.get_by_text("登录已失效", exact=False).first.wait_for(
            timeout=15000)
        await page.locator("#invite").wait_for(timeout=15000)
        assert await page.locator(".sidebar").count() == 0
        body = await page.text_content("body") or ""
        assert "发送中" not in body
        results["r1_question_status_401"] = True
        await page.screenshot(path=str(EVIDENCE / "20-r1-question-401.png"))
    except Exception as error:  # noqa: BLE001
        failures.append(f"r1-question-status-401: {error!r}")
    finally:
        server.abandon_runs("A", "s-a2")
        await context.close()


async def scenario_r1_initial_history_401(browser, server: Server,
                                          results: dict, failures: list,
                                          screenshots: list) -> None:
    context = await browser.new_context()
    page = await context.new_page()
    try:
        server.fail_runs_401_once()
        await page.goto(server.origin, wait_until="domcontentloaded")
        await page.fill("#invite", "invite-a16")
        await page.get_by_role("button", name="进入工作区").click()
        await page.get_by_text("登录已失效", exact=False).first.wait_for(
            timeout=15000)
        await page.locator("#invite").wait_for(timeout=15000)
        assert await page.locator(".sidebar").count() == 0
        body = await page.text_content("body") or ""
        assert "发送中" not in body
        results["r1_initial_history_401"] = True
        await page.screenshot(path=str(
            EVIDENCE / "21-r1-initial-history-401.png"))
    except Exception as error:  # noqa: BLE001
        failures.append(f"r1-initial-history-401: {error!r}")
    finally:
        await context.close()


async def scenario_r1_late_401_keeps_b(browser, server: Server,
                                       results: dict, failures: list,
                                       screenshots: list) -> None:
    context = await browser.new_context()
    page = await context.new_page()
    try:
        await login(page, server.origin, "invite-a17")
        await select_session(page, "A 第二会话")
        server.hold_chat_401_once()
        await page.locator(".chat-input").fill("迟到401")
        await page.locator(".send-btn").click()
        await wait_for(
            lambda: server.state.chat_401_waiting.is_set(), 10,
            "迟到 401 请求未挂起")
        await logout(page)
        await page.fill("#invite", "invite-b5")
        await page.get_by_role("button", name="进入工作区").click()
        await page.locator(".sidebar").wait_for(timeout=15000)
        await page.get_by_text("B 会话", exact=True).wait_for(timeout=15000)
        server.release_chat_401()
        await asyncio.sleep(1.0)
        assert await page.locator("#invite").count() == 0
        assert await page.locator(".sidebar").count() == 1
        body = await page.text_content("body") or ""
        assert "B 私有回答" in body
        assert "A 私有回答" not in body
        results["r1_late_401_keeps_b"] = True
        await page.screenshot(path=str(EVIDENCE / "22-r1-late-401.png"))
    except Exception as error:  # noqa: BLE001
        failures.append(f"r1-late-401: {error!r}")
    finally:
        server.release_chat_401()
        await context.close()


async def scenario_r1_answer_503_retry(browser, server: Server,
                                       results: dict, failures: list,
                                       screenshots: list) -> None:
    context = await browser.new_context()
    page = await context.new_page()
    try:
        await login(page, server.origin, "invite-a18")
        await select_session(page, "A 第二会话")
        await page.locator(".chat-input").fill("需要问题")
        await page.locator(".send-btn").click()
        await page.get_by_text("允许本次", exact=True).wait_for(timeout=15000)
        await page.get_by_role("button", name="允许本次").click()
        server.fail_answer_503_once()
        with server.state.lock:
            posts = server.state.post_chat_count
        await page.get_by_role("button", name="提交").click()
        await page.get_by_text("回答提交失败", exact=False).first.wait_for(
            timeout=15000)
        assert await page.locator(".question-card").count() == 1, (
            "503 后题卡被移除")
        assert await page.locator(
            ".question-card__option--selected").count() >= 1, (
            "503 后已选答案丢失")
        run = server.state.runs[server.state.question_run_ids[-1]]
        assert run["text"] == "需要问题", run
        assert run["answered"] is False, run
        assert server.state.post_chat_count == posts, (
            server.state.post_chat_count, posts)
        await page.get_by_role("button", name="提交").click()
        await wait_for(
            lambda: run["answered"] is True, 10,
            f"重试未提交 {run['id']}")
        await page.wait_for_function(
            "() => document.querySelectorAll('.question-card').length === 0",
            timeout=15000)
        await page.get_by_text("已按你的选择继续。", exact=True).last.wait_for(
            timeout=15000)
        assert await page.locator(".question-card").count() == 0, (
            "成功后题卡未移除")
        assert server.state.post_chat_count == posts, (
            server.state.post_chat_count, posts)
        results["r1_answer_503_retry"] = True
        await page.screenshot(path=str(EVIDENCE / "23-r1-answer-retry.png"))
    except Exception as error:  # noqa: BLE001
        failures.append(f"r1-answer-503-retry: {error!r}")
    finally:
        server.abandon_runs("A", "s-a2")
        await context.close()


async def scenario_r1_answer_403(browser, server: Server,
                                 results: dict, failures: list,
                                 screenshots: list) -> None:
    context = await browser.new_context()
    page = await context.new_page()
    try:
        await login(page, server.origin, "invite-a20")
        await select_session(page, "A 第二会话")
        await page.locator(".chat-input").fill("需要问题")
        await page.locator(".send-btn").click()
        await page.get_by_text("允许本次", exact=True).wait_for(timeout=15000)
        await page.get_by_role("button", name="允许本次").click()
        server.fail_answer_403_once()
        await page.get_by_role("button", name="提交").click()
        await page.get_by_text("403 权限不足", exact=False).first.wait_for(
            timeout=15000)
        assert await page.locator(".question-card").count() == 1
        assert await page.locator("#invite").count() == 0
        run = server.state.runs[server.state.question_run_ids[-1]]
        assert run["answered"] is False
        await page.get_by_role("button", name="提交").click()
        await wait_for(lambda: run["answered"] is True, 10, "403 后重试失败")
        await page.wait_for_function(
            "() => document.querySelectorAll('.question-card').length === 0",
            timeout=15000)
        results["r1_answer_403"] = True
        await page.screenshot(path=str(EVIDENCE / "25-r1-answer-403.png"))
    except Exception as error:  # noqa: BLE001
        failures.append(f"r1-answer-403: {error!r}")
    finally:
        server.abandon_runs("A", "s-a2")
        await context.close()


async def scenario_r1_answer_409(browser, server: Server,
                                 results: dict, failures: list,
                                 screenshots: list) -> None:
    context = await browser.new_context()
    page = await context.new_page()
    try:
        await login(page, server.origin, "invite-a21")
        await select_session(page, "A 第二会话")
        await page.locator(".chat-input").fill("需要问题")
        await page.locator(".send-btn").click()
        await page.get_by_text("允许本次", exact=True).wait_for(timeout=15000)
        await page.get_by_role("button", name="允许本次").click()
        server.fail_answer_409_once()
        await page.get_by_role("button", name="提交").click()
        await page.get_by_text("已过期或已回答", exact=False).first.wait_for(
            timeout=15000)
        await page.wait_for_function(
            "() => document.querySelectorAll('.question-card').length === 0",
            timeout=15000)
        assert await page.locator("#invite").count() == 0
        results["r1_answer_409"] = True
        await page.screenshot(path=str(EVIDENCE / "26-r1-answer-409.png"))
    except Exception as error:  # noqa: BLE001
        failures.append(f"r1-answer-409: {error!r}")
    finally:
        server.abandon_runs("A", "s-a2")
        await context.close()


async def scenario_r1_question_cursor(browser, server: Server,
                                      results: dict, failures: list,
                                      screenshots: list) -> None:
    context = await browser.new_context()
    page = await context.new_page()
    try:
        await login(page, server.origin, "invite-a22")
        await select_session(page, "A 第二会话")
        with server.state.lock:
            afters = len(server.state.stream_afters)
        await page.locator(".chat-input").fill("断线问题")
        await page.locator(".send-btn").click()
        await page.get_by_text("允许本次", exact=True).wait_for(timeout=15000)
        posts_after_send = server.state.post_chat_count
        reconnect = page.get_by_role("button", name="重连（只回放）")
        await reconnect.wait_for(timeout=15000)
        await reconnect.click()
        await wait_for(
            lambda: (len(server.state.stream_afters) > afters
                     and server.state.stream_afters[-1] == 3), 10,
            "题卡后重连未从已消费游标继续")
        run = [run for run in server.state.runs.values()
               if run["text"] == "断线问题"][-1]
        run["resume"].set()
        await page.get_by_text("问题后回答", exact=True).last.wait_for(
            timeout=15000)
        assert server.state.post_chat_count == posts_after_send
        results["r1_question_cursor"] = True
        await page.screenshot(path=str(
            EVIDENCE / "27-r1-question-cursor.png"))
    except Exception as error:  # noqa: BLE001
        failures.append(f"r1-question-cursor: {error!r}")
    finally:
        for run in server.state.runs.values():
            if run["text"] == "断线问题":
                run["resume"].set()
        await context.close()


async def scenario_r1_thinking_cursor(browser, server: Server,
                                      results: dict, failures: list,
                                      screenshots: list) -> None:
    context = await browser.new_context()
    page = await context.new_page()
    try:
        await login(page, server.origin, "invite-a19")
        await select_session(page, "A 第二会话")
        with server.state.lock:
            posts_before = server.state.post_chat_count
            afters = len(server.state.stream_afters)
        await page.locator(".chat-input").fill("断线思考")
        await page.locator(".send-btn").click()
        await page.get_by_text("思考唯一内容", exact=True).wait_for(
            timeout=15000)
        await page.get_by_text("活动唯一内容", exact=True).wait_for(
            timeout=15000)
        posts_after_send = server.state.post_chat_count
        assert posts_after_send == posts_before + 1
        reconnect = page.get_by_role("button", name="重连（只回放）")
        await reconnect.wait_for(timeout=15000)
        await reconnect.click()
        await wait_for(
            lambda: (len(server.state.stream_afters) > afters
                     and server.state.stream_afters[-1] == 3), 10,
            "重连未从已消费游标继续")
        body = await page.text_content("body") or ""
        assert body.count("思考唯一内容") == 1, body
        assert body.count("活动唯一内容") == 1, body
        run = [run for run in server.state.runs.values()
               if run["text"] == "断线思考"][-1]
        run["resume"].set()
        await page.get_by_text("断线后的回答", exact=True).last.wait_for(
            timeout=15000)
        assert server.state.post_chat_count == posts_after_send
        results["r1_thinking_cursor"] = True
        await page.screenshot(path=str(
            EVIDENCE / "24-r1-thinking-cursor.png"))
    except Exception as error:  # noqa: BLE001
        failures.append(f"r1-thinking-cursor: {error!r}")
    finally:
        for run in server.state.runs.values():
            if run["text"] == "断线思考":
                run["resume"].set()
        await context.close()


async def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    if not (DIST / "index.html").is_file():
        print("缺少构建产物，请先 npm --prefix web/frontend run build")
        return 2
    from playwright.async_api import async_playwright

    control = Server("control").start()
    local = Server("local").start()
    results: dict[str, bool] = {}
    failures: list[str] = []
    screenshots: list[str] = []
    browser = None
    version = ""
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            version = browser.version
            control_scenarios = [
                scenario_control_layout, scenario_control_chat,
                scenario_question_and_stop, scenario_refresh_reconnect,
                scenario_cross_identity, scenario_cross_tab,
                scenario_delayed_history, scenario_selection_interleaving,
                scenario_admission_ordering, scenario_identity_unavailable,
                scenario_session_crud, scenario_history_pagination,
                scenario_failure_preserves, scenario_settings_delayed,
                scenario_r1_submit_401, scenario_r1_question_status_401,
                scenario_r1_initial_history_401,
                scenario_r1_late_401_keeps_b,
                scenario_r1_answer_503_retry, scenario_r1_answer_403,
                scenario_r1_answer_409, scenario_r1_question_cursor,
                scenario_r1_thinking_cursor,
            ]
            for scenario in control_scenarios:
                await scenario(
                    browser, control, results, failures, screenshots)
                # 每个场景独立收尾：不把上一场景的活跃 Run 带入下一场景。
                control.abandon_runs("A")
            await scenario_local_mode(
                browser, local, results, failures, screenshots)
            await browser.close()
            browser = None
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                pass
        control.stop()
        local.stop()

    print("BROWSER_VERSION", version)
    print("TASK3_BROWSER_RESULTS",
          json.dumps(results, ensure_ascii=False, sort_keys=True))
    print("FAILURES", json.dumps(failures, ensure_ascii=False))
    print("SCREENSHOTS", json.dumps(
        [str(Path(item).relative_to(ROOT)) for item in EVIDENCE.iterdir()
         if item.suffix == ".png"], ensure_ascii=False))
    expected = {
        "control_original_layout", "control_unavailable_menu",
        "control_settings_bridge", "control_text_thinking_activity",
        "control_question_card", "control_stop",
        "control_continue_after_stop", "control_refresh_reconnect",
        "control_cross_identity_late_results",
        "control_cross_tab_identity", "control_delayed_history",
        "control_selection_interleaving", "control_admission_ordering",
        "control_identity_unavailable", "control_session_crud",
        "control_history_pagination", "control_failure_preserves_completed",
        "control_settings_delayed", "r1_submit_401",
        "r1_question_status_401", "r1_initial_history_401",
        "r1_late_401_keeps_b", "r1_answer_503_retry",
        "r1_answer_403", "r1_answer_409", "r1_question_cursor",
        "r1_thinking_cursor", "local_original_chat",
    }
    return 0 if not failures and expected <= set(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
