"""隔离真实build：旧平台停止回包是否污染随后打开的新平台二维码。"""
import asyncio,importlib.util,json,pathlib,sys,tempfile
ROOT=pathlib.Path(sys.argv[1]).resolve();sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('review5c2_stop',ROOT/'tests/frontend/browser_original_web_task05c2.py');f=importlib.util.module_from_spec(spec);sys.modules[spec.name]=f;spec.loader.exec_module(f)
from playwright.async_api import async_playwright
async def main():
 result={}
 with tempfile.TemporaryDirectory(prefix='easel-review-5c2-stop-') as temp:
  server=f.Server(pathlib.Path(temp)).start();release=asyncio.Event();held=asyncio.Event()
  try:
   async with async_playwright() as p:
    browser=await p.chromium.launch();ctx=await browser.new_context(ignore_https_errors=True);page=await ctx.new_page();await f.login(page,server);await f.open_accounts(page)
    await page.locator('.account-card',has_text='小红书').get_by_role('button',name='登录',exact=True).click();await page.locator('.modal .qr-img').wait_for(timeout=20000)
    async def hold_stop(route):
     response=await route.fetch();held.set();await release.wait();await route.fulfill(response=response)
    await page.route('**/api/login/xiaohongshu/stop',hold_stop)
    await page.get_by_role('button',name='停止登录',exact=True).click();await asyncio.wait_for(held.wait(),15)
    await page.get_by_role('button',name='关闭',exact=True).click()
    await page.locator('.account-card',has_text='抖音').get_by_role('button',name='登录',exact=True).click();await page.locator('.modal .qr-img').wait_for(timeout=20000)
    before=await page.locator('.modal').inner_text();before_qr=await page.locator('.modal .qr-img').count();release.set()
    await page.locator('.modal').get_by_text('已停止登录',exact=False).first.wait_for(timeout=5000)
    b_status=await ctx.request.get(server.origin+'/api/login/douyin/status');b=await b_status.json()
    result={'new_modal_heading':await page.locator('.modal h3').inner_text(),'new_modal_had_qr':before_qr==1,'before_claimed_stopped':'已停止登录' in before,'after_claims_stopped':'已停止登录' in await page.locator('.modal').inner_text(),'new_qr_after_late_old_stop':await page.locator('.modal .qr-img').count(),'new_backend_login_state':b.get('state'),'new_backend_run_state':server.manager.state(b['runId']) if b.get('runId') else None,'login_starts':server.fake.login_starts}
    me=await(await ctx.request.get(server.origin+'/api/auth/me')).json()
    cleanup=await ctx.request.post(server.origin+'/api/login/douyin/stop',data={'runId':b['runId']},headers={'Origin':server.origin,'X-CSRF-Token':me['csrfToken']});result['cleanup_stop_status']=cleanup.status
    await ctx.close();await browser.close()
  finally:release.set();await server.stop()
 result['temporary_directory_cleaned']=not pathlib.Path(temp).exists();print(json.dumps(result,ensure_ascii=True,indent=2))
asyncio.run(main())

