"""只读受控复审：正式Compose参数缺省时旧manual策略是否静默开放自动发布。
不运行Docker；从正式Compose取CLI argv，正式parser只捕获配置，再使用隔离任务链路。
"""
import asyncio,importlib.util,json,pathlib,sys,tempfile
ROOT=pathlib.Path(sys.argv[1]).resolve();sys.path.insert(0,str(ROOT))
import yaml
from easel import cli
compose_path=ROOT/'deploy/desktop/compose.workspace.yaml'
command=yaml.safe_load(compose_path.read_text(encoding='utf-8'))['services']['control']['command']
argv=command[command.index('serve-control'):]
argv=[('https://fixture.invalid' if '${EASEL_PUBLIC_ORIGIN' in v else v) for v in argv]
captured={}
original=cli.cmd_serve_control
cli.cmd_serve_control=lambda args:captured.update(vars(args)) or 0
try: assert cli.main(argv)==0
finally:cli.cmd_serve_control=original
mode=captured['publish_mode']
start=(ROOT/'deploy/desktop/Start-WorkspaceEasel.ps1').read_text(encoding='utf-8-sig')
param=start.split('param(',1)[1].split('\n)',1)[0]
spec=importlib.util.spec_from_file_location('review5c3manual',ROOT/'tests/frontend/browser_original_web_task05c3.py');f=importlib.util.module_from_spec(spec);sys.modules[spec.name]=f;spec.loader.exec_module(f)
import httpx
async def main():
 result={'compose_passes_publish_mode':'--publish-mode' in command,'start_accepts_publish_mode':'$PublishMode' in param,'formal_cli_resolved_publish_mode':mode}
 with tempfile.TemporaryDirectory(prefix='easel-review-5c3-manual-') as temp:
  server=f.Server(pathlib.Path(temp),publish_mode=mode)
  # 代表既有已明确选择manual的worker设置；仅读受控映射，不读真实.env。
  server.worker_app.read_settings=lambda path:{'EASEL_PUBLISH_MODE':'manual'}
  result['old_worker_policy']=server.worker_app.api_publishing_policy()
  server.start()
  try:
   async with httpx.AsyncClient(base_url=server.origin,verify=False,trust_env=False) as client:
    login=await client.post('/api/auth/exchange',json={'token':server.invites.pop(0)},headers={'Origin':server.origin});assert login.status_code==200,login.text
    csrf=login.json()['csrfToken'];policy=await client.get('/api/publishing-policy');result['control_policy']=policy.json()
    response=await client.post('/api/publish/xiaohongshu',json={'title':'受控策略复审','body':'仅在隔离替身执行','tags':'','media':['project/cover.png'],'requestId':'review-manual-migration'},headers={'Origin':server.origin,'X-CSRF-Token':csrf});result['publish_admission_status']=response.status_code
    assert response.status_code in (200,202),response.text
    for _ in range(100):
     status=await client.get('/api/publish/xiaohongshu/status');body=status.json()
     if body.get('state') in ('success','error','unknown'):break
     await asyncio.sleep(.05)
    result['fake_publisher_executions']=len(server.fake.calls);result['final_state']=body.get('state')
  finally:await server.stop()
 result['temporary_directory_cleaned']=not pathlib.Path(temp).exists();print(json.dumps(result,ensure_ascii=True,indent=2))
asyncio.run(main())
