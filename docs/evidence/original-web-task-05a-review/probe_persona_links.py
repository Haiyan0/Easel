"""Task 5A 主审探针：画像目录/Markdown 文件链接越过 profiles 边界。

所有目标和 sentinel 都在本轮 TemporaryDirectory；不读取真实文件。
Windows 无软链接权限时仅对目录使用真实 junction，文件软链接如实 not-run。
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
    from easel import persona
    from easel.control.workspace_api import canonical_persona_name
    secret = 'PRIVATE-STATE-SENTINEL'
    with tempfile.TemporaryDirectory(prefix='easel-5a-persona-review-') as temp:
        root = Path(temp)
        profiles = root / 'profiles'
        profiles.mkdir()
        private = root / 'private-state'
        private.mkdir()
        (private / 'identity.md').write_text(secret, encoding='utf-8')
        old = worker.PROFILES_DIR, persona.PROFILES_DIR
        worker.PROFILES_DIR = persona.PROFILES_DIR = profiles
        try:
            directory = profiles / 'public-profile'
            try:
                directory.symlink_to(private, target_is_directory=True)
                kind = 'symlink'
            except OSError:
                if os.name != 'nt':
                    raise
                result = subprocess.run(
                    ['cmd', '/c', 'mklink', '/J', str(directory), str(private)],
                    capture_output=True)
                if result.returncode != 0:
                    raise RuntimeError('temporary junction creation failed')
                kind = 'junction'
            print('DIRECTORY_LINK_KIND=' + kind)
            print('CONTROL_NAME_ACCEPTED=' +
                  str(canonical_persona_name('public-profile') == 'public-profile'))
            detail = asyncio.run(worker.api_persona('public-profile'))
            files = asyncio.run(worker.api_persona_files('public-profile'))
            print('DIRECTORY_DETAIL_EXPOSES_PRIVATE=' +
                  str(secret in detail['content']))
            print('DIRECTORY_FILES_EXPOSE_PRIVATE=' +
                  str(any(secret in item['content'] for item in files['files'])))
            normal = profiles / 'normal-profile'
            normal.mkdir()
            (normal / 'identity.md').write_text('normal-profile', encoding='utf-8')
            file_link = normal / 'style.md'
            try:
                file_link.symlink_to(private / 'identity.md')
            except OSError as error:
                print('FILE_SYMLINK=NOT_RUN:os_error_' +
                      str(getattr(error, 'winerror', None) or error.errno))
            else:
                detail = asyncio.run(worker.api_persona('normal-profile'))
                files = asyncio.run(worker.api_persona_files('normal-profile'))
                print('FILE_SYMLINK_DETAIL_EXPOSES_PRIVATE=' +
                      str(secret in detail['content']))
                print('FILE_SYMLINK_FILES_EXPOSE_PRIVATE=' +
                      str(any(secret in item['content'] for item in files['files'])))
        finally:
            worker.PROFILES_DIR, persona.PROFILES_DIR = old
    print('TEMP_CLEANUP=COMPLETE')

if __name__ == '__main__':
    main()
