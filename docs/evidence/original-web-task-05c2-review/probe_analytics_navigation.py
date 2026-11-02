"""隔离复审：跨站顶层 GET 是否在无 Origin/CSRF 时启动 analytics Run。"""
import asyncio,importlib.util,json,pathlib,sys,tempfile
ROOT=pathlib.Path(sys.argv[1]).resolve();sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('review5c2',ROOT/'tests/frontend/browser_original_web_task05c2.py');f=importlib.util.module_from_spec(spec);sys.modules[spec.name]=f;spec.loader.exec_module(f)
from playwright.async_api import async_playwright
async def main():
 result={}
 with tempfile.TemporaryDirectory(prefix='easel-review-5c2-csrf-') as temp:
  server=f.Server(pathlib.Path(temp)).start()
  try:
   async with async_playwright() as p:
    browser=await p.chromium.launch();ctx=await browser.new_context(ignore_https_errors=True);page=await ctx.new_page();await f.login(page,server)
    cookies=await ctx.cookies();session=next(x for x in cookies if x['name']=='__Host-easel_session');result['session_cookie_attributes']={k:session[k] for k in ('httpOnly','secure','sameSite')}
    target=server.origin+'/api/analytics/xiaohongshu';seen=[]
    page.on('request',lambda r:seen.append(r) if r.url==target else None)
    before=len(server.fake.analytics_calls)
    # data: 文档为 opaque/cross-site，不加载第三方站点或用户内容。
    await page.goto('data:text/html,<title>External controlled page</title><a id="go" href="'+target+'">Open controlled link</a>')
    await page.locator('#go').click();await page.wait_for_url(target)
    await f.wait_for(lambda:len(server.fake.analytics_calls)>before,15,'analytics execution absent')
    request=seen[-1];headers=await request.all_headers()
    result.update({'navigation_status':(await page.locator('body').inner_text())[:0] or 'completed','cross_site_header':headers.get('sec-fetch-site'),'fetch_mode':headers.get('sec-fetch-mode'),'origin_present':'origin' in headers,'csrf_present':'x-csrf-token' in headers,'session_cookie_sent':'__Host-easel_session=' in headers.get('cookie',''),'analytics_executions':len(server.fake.analytics_calls)-before})
    await ctx.close();await browser.close()
  finally:await server.stop()
 result['temporary_directory_cleaned']=not pathlib.Path(temp).exists();print(json.dumps(result,ensure_ascii=True,indent=2))
asyncio.run(main())
