"""5C3 R2 正向复审：明确改图、确认取消不提交、刷新仍选择并提交B。"""
import asyncio,importlib.util,json,pathlib,sys,tempfile
ROOT=pathlib.Path(sys.argv[1]).resolve();sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('review5c3_media',ROOT/'tests/frontend/browser_original_web_task05c3.py');f=importlib.util.module_from_spec(spec);sys.modules[spec.name]=f;spec.loader.exec_module(f)
from playwright.async_api import async_playwright
async def main():
 result={}
 with tempfile.TemporaryDirectory(prefix='easel-review-5c3-intent-media-') as temp:
  server=f.Server(pathlib.Path(temp),publish_mode='automated')
  (server.outputs/'project'/'other.png').write_bytes(f.PNG_1PX)
  server.start();posts=[]
  try:
   async with async_playwright() as p:
    browser=await p.chromium.launch();ctx=await browser.new_context(ignore_https_errors=True);page=await ctx.new_page()
    dialogs=[];accept_next=True
    def on_dialog(dialog):
     dialogs.append(dialog.message)
     asyncio.ensure_future(dialog.accept() if accept_next else dialog.dismiss())
    page.on('dialog',on_dialog)
    await f.login(page,server);await f.open_publish(page);await page.locator('.publish-editor').wait_for(timeout=15000)
    await page.locator('.publish-editor input.field').first.fill('仅改素材的意图复审')
    await page.locator('.publish-editor textarea.field').first.fill('两次请求保持标题正文话题一致')
    if await f._chip_active(page,'抖音'):await page.get_by_role('button',name='抖音',exact=True).click()
    if not await f._chip_active(page,'小红书'):await page.get_by_role('button',name='小红书',exact=True).click()
    await page.get_by_role('button',name='选择媒体').click();await f.wait_for(lambda:f.media_cell_ready(page,'project/other.png'),10,'other image absent')
    first=await f.media_cell_index(page,'project/cover.png');second=await f.media_cell_index(page,'project/other.png')
    await page.locator('.media-cell').nth(first).click()
    async def capture(route):
     if route.request.method!='POST':await route.continue_();return
     posts.append(json.loads(route.request.post_data))
     if len(posts)==1:await route.abort('failed')
     else:await route.continue_()
    await page.route('**/api/publish/xiaohongshu',capture)
    await page.get_by_role('button',name='一键发布',exact=True).click();await page.locator('.pv-pubstate.unknown').wait_for(timeout=20000)
    assert len(posts)==1 and len(server.fake.calls)==0
    await page.locator('.media-cell').nth(first).click();await page.locator('.media-cell').nth(second).click()
    selected=await page.locator('.media-cell.sel').evaluate_all('els=>els.map(e=>e.title)')
    assert selected==['project/other.png'],selected
    accept_next=False
    await page.get_by_role('button',name='一键发布',exact=True).click()
    await f.wait_for(lambda:len(dialogs)==2,15,'cancel dialog absent')
    await page.wait_for_timeout(200)
    assert len(posts)==1 and len(server.fake.calls)==0
    assert 'project/other.png' in dialogs[1] and 'project/cover.png' not in dialogs[1],dialogs[1]
    await page.reload(wait_until='domcontentloaded');await page.locator('.sidebar').wait_for();await f.open_publish(page)
    await page.locator('.publish-editor').wait_for()
    visible_after_refresh=await page.locator('.media-chip img').get_attribute('src')
    assert 'project/other.png' in visible_after_refresh,visible_after_refresh
    accept_next=True
    await page.get_by_role('button',name='一键发布',exact=True).click();await f.wait_for(lambda:len(posts)==2,15,'second POST absent')
    assert 'project/other.png' in dialogs[2] and 'project/cover.png' not in dialogs[2],dialogs[2]
    await page.locator('.pv-pubstate.ok').wait_for(timeout=20000)
    result={'first_request_never_forwarded':True,'first_request_media':posts[0]['media'],'visible_media_before_second_submit':selected,'second_request_media':posts[1]['media'],'same_request_id':posts[0]['requestId']==posts[1]['requestId'],'cancel_sent_no_post':True,'confirmation_matches_B':True,'refresh_kept_visible_B':True,'second_request_matches_visible_selection':posts[1]['media']==selected,'fake_publisher_executions':len(server.fake.calls)}
    await ctx.close();await browser.close()
  finally:await server.stop()
 result['temporary_directory_cleaned']=not pathlib.Path(temp).exists();print(json.dumps(result,ensure_ascii=True,indent=2))
asyncio.run(main())
