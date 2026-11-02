"""Task 5B 主审探针：不存在的数据文件绕过 outputs 链接边界检查。

仅临时目录与假记录；Windows 使用真实 junction，POSIX 使用真实 symlink。
不修改生产代码，不触碰用户 workspace，不操作容器或网络。
"""
import argparse
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(args.repo).resolve()))
    from web import app as worker
    with tempfile.TemporaryDirectory(prefix='easel-5b-root-review-') as temp:
        root = Path(temp)
        private = root / 'private-state'
        private.mkdir()
        outputs = root / 'outputs'
        if os.name == 'nt':
            result = subprocess.run(
                ['cmd', '/c', 'mklink', '/J', str(outputs), str(private)],
                capture_output=True)
            if result.returncode != 0:
                raise RuntimeError('temporary junction creation failed')
            print('LINK_KIND=junction')
        else:
            outputs.symlink_to(private, target_is_directory=True)
            print('LINK_KIND=symlink')
        prior = worker.OUTPUTS_DIR
        worker.OUTPUTS_DIR = outputs
        try:
            cases = [
                ('IDEAS', '_ideas.json', worker.api_ideas_list,
                 worker.api_ideas_create, worker.IdeaItem(title='review-idea')),
                ('SCHEDULE', '_schedule.json', worker.api_schedule_list,
                 worker.api_schedule_create,
                 worker.ScheduleItem(title='review-schedule', date='2026-11-01')),
            ]
            for label, name, read, create, request in cases:
                print(label + '_INITIAL_READ=' + str(asyncio.run(read())))
                result = asyncio.run(create(request))
                print(label + '_FIRST_CREATE_SUCCEEDED=' + str(bool(result.get('id'))))
                print(label + '_WROTE_PRIVATE_TARGET=' + str((private / name).is_file()))
                try:
                    asyncio.run(read())
                    print(label + '_SECOND_READ=SUCCESS')
                except worker.HTTPException as error:
                    print(label + '_SECOND_READ_STATUS=' + str(error.status_code))
        finally:
            worker.OUTPUTS_DIR = prior
    print('TEMP_CLEANUP=COMPLETE')

if __name__ == '__main__':
    main()
