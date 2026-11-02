"""5C3 复审：首次发布已接纳但返回丢失，原UI同稿重试是否重复外部动作。"""
import asyncio,importlib.util,json,pathlib,sqlite3,sys,tempfile
ROOT=pathlib.Path(sys.argv[1]).resolve();sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('review5c3_ack',ROOT/'tests/frontend/browser_original_web_task05c3.py');f=importlib.util.module_from_spec(spec);sys.modules[spec.name]=f;spec.loader.exec_module(f)
from playwright.async_api import async_playwright
async def main():
 result={}
 with tempfile.TemporaryDirectory(prefix='easel-review-5c3-ack-') as temp:
  server=f.Server(pathlib.Path(temp),publish_mode='automated').start();posts=[]
  def publish_done_count():
   db=sqlite3.connect(server.root/'control.sqlite3')
   try:
    return db.execute("SELECT COUNT(*) FROM runs WHERE status='done' AND conversation_id IN (SELECT id FROM conversations WHERE title='发布：xiaohongshu')").fetchone()[0]
   finally:db.close()
  try:
   async with async_playwright() as p:
    browser=await p.chromium.launch();ctx=await browser.new_context(ignore_https_errors=True);page=await ctx.new_page()
    page.on('dialog',lambda dialog:asyncio.ensure_future(dialog.accept()))
    await f.login(page,server);await f.open_publish(page);await page.locator('.publish-editor').wait_for(timeout=15000)
    await page.locator('.publish-editor input.field').first.fill('丢失接纳响应复审')
    await page.locator('.publish-editor textarea.field').first.fill('两次提交保持完全相同的受控稿件')
    if await f._chip_active(page,'抖音'):await page.get_by_role('button',name='抖音',exact=True).click()
    if not await f._chip_active(page,'小红书'):await page.get_by_role('button',name='小红书',exact=True).click()
    await page.get_by_role('button',name='选择媒体').click();await f.wait_for(lambda:f.media_cell_ready(page,'project/cover.png'),10,'cover absent')
    index=await f.media_cell_index(page,'project/cover.png');await page.locator('.media-cell').nth(index).click()
    async def lose_first_ack(route):
     if route.request.method!='POST':await route.continue_();return
     posts.append(json.loads(route.request.post_data))
     if len(posts)==1:
      response=await route.fetch();assert response.status==200,await response.text()
      await route.abort('failed')
     else:await route.continue_()
    await page.route('**/api/publish/xiaohongshu',lose_first_ack)
    await page.get_by_role('button',name='一键发布',exact=True).click()
    await page.locator('.pv-pubstate.fail').wait_for(timeout=20000)
    failed_text=await page.locator('.pv-pubstate.fail').inner_text()
    await f.wait_for(lambda:publish_done_count()==1,15,'first accepted Run did not settle')
    first_calls=len(server.fake.calls)
    await page.get_by_role('button',name='一键发布',exact=True).click()
    await f.wait_for(lambda:len(server.fake.calls)>=2,15,'retry did not execute')
    await page.locator('.pv-pubstate.ok').wait_for(timeout=20000)
    normalized=[{k:v for k,v in entry.items() if k!='requestId'} for entry in posts]
    result={'first_ui_status':failed_text,'first_run_completed_before_retry':True,'first_publisher_executions':first_calls,'retry_same_payload':len(normalized)==2 and normalized[0]==normalized[1],'retry_request_id_changed':len(posts)==2 and posts[0].get('requestId')!=posts[1].get('requestId'),'publisher_executions_after_retry':len(server.fake.calls),'completed_publish_runs':publish_done_count()}
    await ctx.close();await browser.close()
  finally:await server.stop()
 result['temporary_directory_cleaned']=not pathlib.Path(temp).exists();print(json.dumps(result,ensure_ascii=True,indent=2))
asyncio.run(main())
