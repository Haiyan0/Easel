"""A6 真实 Chromium 浏览器流程（正式前端 + 正式控制面）。

使用仓库既有 Playwright 测试栈驱动两个隔离 context，覆盖邀请登录、会话、
聊天、权限问答、停止、SSE 断线重连、身份切换与刷新恢复。截图只包含登录后
的聊天界面，不包含邀请令牌、Cookie、CSRF 或供应商 Key。

本模块只操作 UI；服务端账本、provider 审计、容器状态由
``tests/integration/multiuser/test_desktop_chat_live.py`` 断言。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

INVITE_SELECTOR = "#invite"
LOGIN_BUTTON = "进入工作区"
NEW_SESSION = re.compile("新建会话")
SEND_BUTTON = "发送"
STOP_BUTTON = "停止"
SUBMIT_ANSWER = "提交回答"
RECONNECT_BUTTON = "重连（只回放）"
LOGOUT_BUTTON = "退出登录"


def launch(playwright, *, pin: str | None = None,
           headless: bool = True) -> Browser:
    """启动真实 Chromium；TLS 信任只限本轮证书 SPKI，不全局忽略错误。"""
    args: list[str] = []
    if pin:
        args.append(f"--ignore-certificate-errors-spki-list={pin}")
    return playwright.chromium.launch(headless=headless, args=args)


def new_context(browser: Browser, origin: str) -> BrowserContext:
    return browser.new_context(base_url=origin, ignore_https_errors=False)


def goto_login(page: Page) -> None:
    page.goto("/", wait_until="domcontentloaded")
    page.wait_for_selector(INVITE_SELECTOR, timeout=20000)


def login(page: Page, invite: str) -> None:
    page.wait_for_selector(INVITE_SELECTOR, timeout=15000)
    page.fill(INVITE_SELECTOR, invite)
    page.get_by_role("button", name=LOGIN_BUTTON).click()
    page.get_by_role("button", name=NEW_SESSION).wait_for(timeout=20000)


def logout(page: Page) -> None:
    page.get_by_role("button", name=LOGOUT_BUTTON).click()
    page.wait_for_selector(INVITE_SELECTOR, timeout=15000)


def create_session(page: Page) -> None:
    page.get_by_role("button", name=NEW_SESSION).click()
    page.get_by_placeholder("输入消息…").wait_for(timeout=20000)


def send(page: Page, text: str) -> None:
    box = page.get_by_placeholder("输入消息…")
    box.fill(text)
    page.get_by_role("button", name=SEND_BUTTON, exact=True).click()


def assistant_articles(page: Page) -> list[str]:
    articles = page.locator("article")
    values: list[str] = []
    for index in range(articles.count()):
        text = articles.nth(index).inner_text()
        if "助手" in text:
            values.append(text)
    return values


def wait_assistant(page: Page, marker: str, timeout: float = 90.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for text in assistant_articles(page):
            if marker in text:
                return text
        page.wait_for_timeout(200)
    raise AssertionError(f"未看到助手消息：{marker}")


def wait_assistant_count(page: Page, marker: str, count: int,
                         timeout: float = 90.0) -> int:
    deadline = time.monotonic() + timeout
    matched = 0
    while time.monotonic() < deadline:
        matched = sum(1 for text in assistant_articles(page)
                      if marker in text)
        if matched >= count:
            return matched
        page.wait_for_timeout(200)
    raise AssertionError(
        f"助手消息数量不足：{marker} {matched}/{count}")


def wait_page_text(page: Page, marker: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker in (page.text_content("body") or ""):
            return
        page.wait_for_timeout(200)
    raise AssertionError(f"页面未出现文本：{marker}")


def wait_notice(page: Page, marker: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        notices = page.locator("[role=status]")
        for index in range(notices.count()):
            if marker in notices.nth(index).inner_text():
                return
        page.wait_for_timeout(200)
    raise AssertionError(f"未出现提示：{marker}")


def wait_notice_any(page: Page, markers: tuple[str, ...],
                    timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        notices = page.locator("[role=status]")
        for index in range(notices.count()):
            text = notices.nth(index).inner_text()
            if any(marker in text for marker in markers):
                return
        page.wait_for_timeout(200)
    raise AssertionError(f"未出现提示：{markers}")


def click_reconnect(page: Page, timeout: float = 20.0) -> bool:
    button = page.get_by_role("button", name=RECONNECT_BUTTON)
    try:
        button.wait_for(timeout=timeout * 1000)
    except Exception:  # noqa: BLE001 - 可能已自动续接完成
        return False
    button.click()
    return True


@dataclass
class QuestionView:
    text: str
    labels: list[str]


def wait_question(page: Page, timeout: float = 60.0) -> QuestionView:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sections = page.locator("section").filter(
            has=page.locator("input[type=radio]"))
        if sections.count() > 0:
            section = sections.first
            labels = [text.strip() for text in
                      section.locator("label").all_inner_texts()
                      if text.strip()]
            return QuestionView(text=section.inner_text(), labels=labels)
        page.wait_for_timeout(200)
    raise AssertionError("未出现权限问答卡片")


def answer(page: Page, label: str, timeout: float = 30.0) -> None:
    section = page.locator("section").filter(
        has=page.locator("input[type=radio]")).first
    section.locator("label").filter(
        has_text=re.compile(re.escape(label))).first.click()
    page.get_by_role("button", name=SUBMIT_ANSWER).click()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if page.locator("section").filter(
                has=page.locator("input[type=radio]")).count() == 0:
            return
        page.wait_for_timeout(150)
    raise AssertionError("权限问答提交后卡片未消失")


def stop(page: Page) -> None:
    page.get_by_role("button", name=STOP_BUTTON, exact=True).click()


def body_text(page: Page) -> str:
    return page.text_content("body") or ""


def screenshot(page: Page, path) -> None:
    page.screenshot(path=str(path), full_page=True)


@dataclass
class SseRecord:
    url: str
    started: float
    ended: float | None = None
    failed: float | None = None

    @property
    def open(self) -> bool:
        return self.ended is None and self.failed is None


@dataclass
class SseTracker:
    """按连接记录浏览器到 jobs SSE 的请求（重连 URL 会带 after 游标）。"""

    records: list[SseRecord] = field(default_factory=list)

    def attach(self, page: Page) -> None:
        def on_request(request) -> None:
            if "/api/chat/jobs/" in request.url and "/stream" in request.url:
                self.records.append(
                    SseRecord(url=request.url, started=time.monotonic()))

        def on_finished(request) -> None:
            for record in reversed(self.records):
                if record.url == request.url and record.open:
                    record.ended = time.monotonic()
                    return

        def on_failed(request) -> None:
            for record in reversed(self.records):
                if record.url == request.url and record.open:
                    record.failed = time.monotonic()
                    return

        page.on("request", on_request)
        page.on("requestfinished", on_finished)
        page.on("requestfailed", on_failed)

    def for_run(self, run_id: str) -> list[SseRecord]:
        return [record for record in self.records if run_id in record.url]

    def open_for_run(self, run_id: str) -> SseRecord | None:
        for record in reversed(self.records):
            if run_id in record.url and record.open:
                return record
        return None


def open_browser():
    """便捷入口：返回 (playwright, browser) 供调用方负责关闭。"""
    playwright = sync_playwright().start()
    return playwright, launch(playwright)


__all__ = [
    "Browser", "BrowserContext", "Page", "QuestionView", "SseRecord",
    "SseTracker", "answer", "assistant_articles", "body_text",
    "click_reconnect", "create_session", "goto_login", "launch", "login",
    "logout", "new_context", "open_browser", "screenshot", "send", "stop",
    "wait_assistant", "wait_notice", "wait_notice_any", "wait_page_text",
    "wait_question",
]
