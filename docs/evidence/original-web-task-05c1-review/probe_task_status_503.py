"""R1 复审：原 build 的技能题卡在一次状态 503 后是否自动恢复轮询。
仅隔离 fixture、假 Run/权限问题，不操作 Docker/用户实例。
"""
import asyncio, importlib.util, json, pathlib, sys, tempfile
ROOT=pathlib.Path(sys.argv[1]).resolve();sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('review_r1_fixture', ROOT/'tests/frontend/browser_original_web_task05c1.py')
f=importlib.util.module_from_spec(spec);sys.modules[spec.name]=f;spec.loader.exec_module(f)
from playwright.async_api import async_playwright
async def main():
 result={}
 with tempfile.TemporaryDirectory(prefix='easel-review-r1-status-') as temp:
  server=f.Server(pathlib.Path(temp));runs=[]
  async def executor(request,context,bind,emit,ask):
   runs.append(context.run_id)
   await ask([{'questionId':'review','question':'是否允许受控操作？','options':[{'label':'允许本次','value':'allow'},{'label':'拒绝本次','value':'deny'}]}])
   emit('token','DONE');return 'DONE'
  server.manager._executor=executor;f.SERVER=server;server.start()
  try:
   async with async_playwright() as p:
    browser=await p.chromium.launch();ctx=await browser.new_context(ignore_https_errors=True);page=await ctx.new_page()
    await f.login(page);await page.get_by_role('button',name='技能库',exact=True).click()
    await page.locator('.skill-card-rawname').filter(has_text='copywriting').first.click()
    await page.locator('.drawer textarea').fill('受控状态503复审');await page.get_by_role('button',name='执行',exact=True).click()
    await page.locator('.question-card[data-pending="false"]').wait_for(timeout=20000)
    await page.get_by_role('button',name='允许本次',exact=True).click()
    assert not await page.get_by_role('button',name='提交',exact=True).is_disabled()
    calls=[];injected=False
    async def once503(route):
     nonlocal injected
     calls.append(route.request.url)
     if not injected:
      injected=True;await route.fulfill(status=503,content_type='application/json',body='{"code":"review_temporary_unavailable"}')
     else: await route.continue_()
    await page.route('**/api/skill/status/copywriting',once503)
    await page.get_by_text('工作区服务暂不可用（503）',exact=False).wait_for(timeout=12000)
    await page.locator('.question-card[data-pending="true"]').wait_for(timeout=3000)
    before=len(calls);await page.wait_for_timeout(4200);after=len(calls)
    healthy=await ctx.request.get(server.origin+'/api/skill/status/copywriting');healthy_body=await healthy.json()
    result={'injected_503_count':1 if injected else 0,'polls_at_failure':before,'polls_after_4_seconds':after,'backend_recovered_status':healthy.status,'backend_question_pending':healthy_body.get('questionPending'),'backend_state':server.manager.state(runs[0]),'visible_question_cards':await page.locator('.question-card').count(),'ui_still_pending':await page.locator('.question-card[data-pending="true"]').count()==1,'submit_still_disabled':await page.get_by_role('button',name='提交',exact=True).is_disabled(),'executions':len(runs)}
    await page.get_by_role('button',name='停止',exact=True).click();await f.wait_for(lambda:server.manager.state(runs[0])=='cancelled',10,'cleanup stop failed')
    await ctx.close();await browser.close()
  finally: await server.stop()
 result['temporary_directory_cleaned']=not pathlib.Path(temp).exists();print(json.dumps(result,ensure_ascii=True,indent=2))
asyncio.run(main())
