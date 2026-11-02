"""5C2 R1 正向复审：GET 不执行、POST 来源保护、迟到 stop 不改新弹窗。"""
import asyncio,importlib.util,json,pathlib,sys,tempfile
ROOT=pathlib.Path(sys.argv[1]).resolve();sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('review5c2_r1',ROOT/'tests/frontend/browser_original_web_task05c2.py');f=importlib.util.module_from_spec(spec);sys.modules[spec.name]=f;spec.loader.exec_module(f)
from playwright.async_api import async_playwright
async def main():
 result={}
 with tempfile.TemporaryDirectory(prefix='easel-review-5c2-r1-') as temp:
  server=f.Server(pathlib.Path(temp)).start();release=asyncio.Event();held=asyncio.Event();replied=asyncio.Event()
  try:
   async with async_playwright() as p:
    browser=await p.chromium.launch();ctx=await browser.new_context(ignore_https_errors=True);page=await ctx.new_page();await f.login(page,server)
    target=server.origin+'/api/analytics/xiaohongshu';seen=[];before=len(server.fake.analytics_calls)
    page.on('request',lambda r:seen.append(r) if r.url==target else None)
    await page.goto('data:text/html,<a id="go" href="'+target+'">controlled link</a>')
    async with page.expect_response(target) as info:await page.locator('#go').click()
    response=await info.value;payload=await response.json();headers=await seen[-1].all_headers()
    assert response.status==404 and payload.get('code')=='analytics_cache_missing',payload
    assert len(server.fake.analytics_calls)==before
    missing=await ctx.request.post(target,data={});assert missing.status==403
    me=await(await ctx.request.get(server.origin+'/api/auth/me')).json()
    bad=await ctx.request.post(target,data={},headers={'Origin':'https://external.invalid','X-CSRF-Token':me['csrfToken']});assert bad.status==403
    assert len(server.fake.analytics_calls)==before
    proper=await ctx.request.post(target,data={},headers={'Origin':server.origin,'X-CSRF-Token':me['csrfToken']});assert proper.status==200,await proper.text()
    assert len(server.fake.analytics_calls)==before+1
    cached=await ctx.request.get(target);assert cached.status==200,await cached.text();assert len(server.fake.analytics_calls)==before+1
    result['analytics']={'cross_site':headers.get('sec-fetch-site'),'cookie_sent':'__Host-easel_session=' in headers.get('cookie',''),'csrf_present':'x-csrf-token' in headers,'cross_get_status':response.status,'cross_get_execution_delta':0,'post_without_source_status':missing.status,'post_wrong_origin_status':bad.status,'proper_post_status':proper.status,'proper_post_execution_delta':1,'cached_get_status':cached.status,'cached_get_extra_executions':0}
    await page.goto(server.origin,wait_until='domcontentloaded');await page.locator('.sidebar').wait_for();await f.open_accounts(page)
    await f.wait_for(lambda:f.active_runs(server)==0,15,'capacity not released')
    await page.locator('.account-card',has_text='快手').get_by_role('button',name='登录',exact=True).click();await page.locator('.modal .qr-img').wait_for(timeout=20000)
    async def delay(route):
     response=await route.fetch();held.set();await release.wait();await route.fulfill(response=response);replied.set()
    await page.route('**/api/login/kuaishou/stop',delay)
    await page.get_by_role('button',name='停止登录',exact=True).click();await asyncio.wait_for(held.wait(),15)
    await page.get_by_role('button',name='关闭',exact=True).click()
    await page.locator('.account-card',has_text='抖音').get_by_role('button',name='登录',exact=True).click();await page.locator('.modal .qr-img').wait_for(timeout=20000)
    release.set();await asyncio.wait_for(replied.wait(),5);await page.wait_for_timeout(1000)
    modal=await page.locator('.modal').inner_text();qr=await page.locator('.modal .qr-img').count();b=await(await ctx.request.get(server.origin+'/api/login/douyin/status')).json()
    assert '抖音' in modal and '已停止登录' not in modal,modal
    assert qr==1 and b['state']=='qr_ready' and server.manager.state(b['runId'])=='running'
    assert not await page.get_by_role('button',name='停止登录',exact=True).is_disabled()
    result['late_stop']={'new_heading':await page.locator('.modal h3').inner_text(),'old_reply_released':True,'qr_count':qr,'false_stopped_message':False,'new_backend_state':b['state'],'new_run_state':server.manager.state(b['runId']),'new_stop_button_enabled':True}
    await page.get_by_role('button',name='停止登录',exact=True).click();await page.locator('.modal').get_by_text('已停止登录',exact=False).first.wait_for(timeout=15000)
    assert server.manager.state(b['runId'])=='cancelled';result['late_stop']['new_stop_succeeds']=True
    await ctx.close();await browser.close()
  finally:release.set();await server.stop()
 result['temporary_directory_cleaned']=not pathlib.Path(temp).exists();print(json.dumps(result,ensure_ascii=True,indent=2))
asyncio.run(main())
