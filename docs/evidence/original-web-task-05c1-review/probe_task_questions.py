"""只读审计：真实前端 build + 隔离 fixture RunManager 的技能/画像权限题卡。

运行：Agent Python probe_task_questions.py <实施 worktree>
不运行 Docker，不读用户 workspace/凭证，不覆盖实施证据。
"""
from __future__ import annotations
import asyncio
import importlib.util
import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location(
    "review_task5c1_browser", ROOT / "tests/frontend/browser_original_web_task05c1.py")
fixture = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fixture
spec.loader.exec_module(fixture)
from playwright.async_api import async_playwright

async def main():
    result = {}
    with tempfile.TemporaryDirectory(prefix="easel-review-task5c1-permission-") as temp:
        server = fixture.Server(pathlib.Path(temp))
        requests = []
        runs = []
        async def asking_executor(request, context, bind, emit, ask):
            runs.append((context.task_type, context.run_id))
            await ask([{
                "questionId": "review-permission",
                "header": "受控权限",
                "question": "是否允许本次受控操作？",
                "options": [{"label": "允许本次", "value": "allow"},
                            {"label": "拒绝本次", "value": "deny"}],
            }])
            return "REVIEW-DONE"
        server.manager._executor = asking_executor
        fixture.SERVER = server
        server.start()
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                context = await browser.new_context(ignore_https_errors=True)
                page = await context.new_page()
                page.on("request", lambda request: requests.append(
                    (request.method, request.url.split(server.origin)[-1])))
                await fixture.login(page)
                await page.get_by_role("button", name="技能库", exact=True).click()
                await page.locator(".skill-card-rawname").filter(
                    has_text="copywriting").first.click()
                await page.locator(".drawer textarea").fill("仅用于权限状态复审的假输入")
                await page.get_by_role("button", name="执行", exact=True).click()
                await fixture.wait_for(
                    lambda: bool(runs) and server.manager.state(runs[-1][1]) == "awaiting_permission",
                    15, "技能未进入 awaiting_permission")
                skill_run = runs[-1][1]
                worker_questions = [e for e in server.manager.events(skill_run)["events"]
                                    if e["event"] == "question"]
                skill_status_response = await context.request.get(
                    server.origin + "/api/skill/status/copywriting")
                skill_status = await skill_status_response.json()
                result["skill"] = {
                    "worker_state": server.manager.state(skill_run),
                    "worker_question_events": len(worker_questions),
                    "drawer_visible": await page.locator(".drawer").count(),
                    "question_cards": await page.locator(".question-card").count(),
                    "allow_buttons": await page.get_by_role("button", name="允许本次", exact=True).count(),
                    "public_status": skill_status.get("state"),
                    "public_status_has_question": "question" in skill_status or "questions" in skill_status,
                    "question_api_requests": len([p for _, p in requests if "/question/" in p]),
                }
                await page.get_by_role("button", name="停止", exact=True).click()
                await fixture.wait_for(lambda: server.manager.state(skill_run) == "cancelled",
                                       15, "技能停止未收口")
                await context.close()
                context = await browser.new_context(ignore_https_errors=True)
                page = await context.new_page()
                await fixture.login(page)
                await page.get_by_role("button", name="画像", exact=True).click()
                await page.get_by_role("button", name="新建画像").first.click()
                modal = page.locator(".modal").filter(has_text="配置账号画像")
                await modal.locator("input").first.fill("权限复审画像")
                await modal.get_by_role("button", name="下一步").click()
                await modal.get_by_role("button", name="下一步").click()
                await modal.locator("input").nth(0).fill("受控画像目标")
                await modal.get_by_role("button", name="下一步").click()
                await modal.get_by_role("button", name="生成画像").click()
                await fixture.wait_for(
                    lambda: any(kind == "profile" and server.manager.state(run_id) == "awaiting_permission"
                                for kind, run_id in runs),
                    15, "画像未进入 awaiting_permission")
                profile_run = next(run_id for kind, run_id in runs if kind == "profile")
                profile_questions = [e for e in server.manager.events(profile_run)["events"]
                                      if e["event"] == "question"]
                profile_status_response = await context.request.get(
                    server.origin + "/api/profile/build/status/" + fixture.httpx.URL("https://fake/").copy_with(path="/权限复审画像").raw_path.decode().lstrip("/"))
                profile_status = await profile_status_response.json()
                result["profile"] = {
                    "worker_state": server.manager.state(profile_run),
                    "worker_question_events": len(profile_questions),
                    "wizard_visible": await modal.count(),
                    "question_cards": await page.locator(".question-card").count(),
                    "allow_buttons": await page.get_by_role("button", name="允许本次", exact=True).count(),
                    "stop_buttons": await page.get_by_role("button", name="停止", exact=True).count(),
                    "public_status": profile_status.get("state"),
                    "public_status_has_question": "question" in profile_status or "questions" in profile_status,
                }
                # 仅停止本探针的假 Run，避免 question Future 留到 fixture 清理。
                me_response = await context.request.get(server.origin + "/api/auth/me")
                me = await me_response.json()
                stop_response = await context.request.post(
                    server.origin + "/api/chat/stop",
                    data={"runId": profile_run},
                    headers={"Origin": server.origin, "X-CSRF-Token": me["csrfToken"]})
                result["profile"]["cleanup_stop_status"] = stop_response.status
                await context.close()
                await browser.close()
        finally:
            await server.stop()
        result["execution_count"] = len(runs)
    result["temporary_directory_cleaned"] = not pathlib.Path(temp).exists()
    print(json.dumps(result, ensure_ascii=True, indent=2))

if __name__ == "__main__":
    asyncio.run(main())
