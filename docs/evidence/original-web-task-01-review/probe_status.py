"""主审反例：诊断字段无效时不能终止正常 JSON-RPC reader。

分别用基线仓库和实施 worktree 的模块运行；只使用内存协议帧和假环境，
不创建子进程、文件、容器或真实模型请求。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[3]
WORKTREE = REPOSITORY / ".claude/worktrees/original-web-task1-diagnostics"

PROBE = r'''
import asyncio,json
from pathlib import Path
from types import SimpleNamespace
from easel.harnesses.base import RunRequest
from easel.harnesses.rpc import RPC

async def run(status):
    reader=asyncio.StreamReader()
    reader.feed_data((json.dumps({'jsonrpc':'2.0','id':1,'error':{
        'code':-32000,'message':'fixture error',
        'data':{'status':status,'code':'rate_limited'}}})+'\n').encode())
    reader.feed_data((json.dumps({'jsonrpc':'2.0','id':2,
                                 'result':{'ok':True}})+'\n').encode())
    reader.feed_eof()
    rpc=RPC(RunRequest('',Path.cwd(),{}),[],lambda *args:None,None,acp=True)
    rpc.process=SimpleNamespace(stdout=reader)
    first=asyncio.get_running_loop().create_future()
    second=asyncio.get_running_loop().create_future()
    rpc.pending={1:first,2:second}
    await rpc._read()
    result=[]
    for future in (first,second):
        try:result.append({'result':future.result()})
        except Exception as error:
            result.append({'error':str(error),
                           'category':getattr(error,'category',None)})
    return result

for shown,value in (('ascii429','429'),('unicode_digit','\u00b2'),
                    ('long_digits','4'*4301)):
    print(shown+'='+json.dumps(asyncio.run(run(value)),ensure_ascii=False))
'''


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    for name, root in (("baseline", REPOSITORY), ("implementation", WORKTREE)):
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", PROBE], cwd=root,
                                capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        print("SOURCE=" + name)
        print(result.stdout, end="")
        if result.returncode:
            raise RuntimeError(result.stderr)
    print("NO_RUNTIME_RESOURCES_TOUCHED=True")


if __name__ == "__main__":
    main()
