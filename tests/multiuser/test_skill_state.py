"""Task 7：技能私有状态路径、公众号配置/token 与共享写入迁移。"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SHARED = REPO / "skills" / "shared" / "scripts"
WECHAT_SCRIPTS = REPO / "skills" / "builtin" / "skill-wechat-publisher" / "scripts"


def load_runtime_paths():
    spec = importlib.util.spec_from_file_location(
        "task7_runtime_paths", SHARED / "runtime_paths.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def worker_env(tmp_path: Path, name: str = "a", **overrides: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if key.upper() not in {
               "EASEL_SERVICE_MODE", "EASEL_ROOT", "EASEL_CODE_ROOT",
               "EASEL_WORKSPACE_ROOT", "EASEL_STATE_ROOT", "PYTHONPATH",
           }}
    root = tmp_path / name
    env.update({
        "EASEL_SERVICE_MODE": "worker",
        "EASEL_CODE_ROOT": str(tmp_path / "code"),
        "EASEL_WORKSPACE_ROOT": str(root / "workspace"),
        "EASEL_STATE_ROOT": str(root / "state"),
        "HOME": str(root / "home"),
        "PYTHONIOENCODING": "utf-8",
    })
    env.update(overrides)
    return env


def run_python(code: str, env: dict[str, str], tmp_path: Path,
               cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code], cwd=str(cwd or tmp_path), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120)


# ---------------------------------------------------------------------------
# state_path 布局与校验
# ---------------------------------------------------------------------------


def test_state_path_worker_layout_is_scoped_and_never_in_code_root(
        tmp_path, monkeypatch):
    module = load_runtime_paths()
    env_a = worker_env(tmp_path, "a")
    env_b = worker_env(tmp_path, "b")
    monkeypatch.setenv("EASEL_SERVICE_MODE", "worker")

    layouts = {}
    for env in (env_a, env_b):
        for key in ("EASEL_CODE_ROOT", "EASEL_WORKSPACE_ROOT",
                    "EASEL_STATE_ROOT", "HOME"):
            monkeypatch.setenv(key, env[key])
        layouts[env["HOME"]] = {
            kind: module.state_path(kind, "web")
            for kind in ("wechat-config", "wechat-token",
                         "platform-cookie", "skill-cache")
        }
        layouts[env["HOME"]]["browser-profile"] = module.state_path(
            "browser-profile", "default")

    a, b = list(layouts.values())
    for kind in a:
        assert a[kind] != b[kind], kind
        assert not Path(a[kind]).is_relative_to(
            Path(env_a["EASEL_CODE_ROOT"])), kind
    assert a["wechat-config"].parent == (
        Path(env_a["EASEL_STATE_ROOT"]) / "skills" / "wechat")
    assert a["wechat-token"].parent == (
        Path(env_a["EASEL_STATE_ROOT"]) / "skills" / "wechat" / "tokens")
    assert a["platform-cookie"].parent == (
        Path(env_a["EASEL_STATE_ROOT"]) / "skills" / "platform-cookies")
    assert a["browser-profile"].parent == (
        Path(env_a["HOME"]) / ".easel-browser-profiles")


@pytest.mark.parametrize("account_id", [
    "", ".", "..", "../x", "a/b", "a\\b", "/abs/path", "C:\\drive",
    "~user", ".hidden", "a\0b", "a\nb", "x" * 200,
])
def test_state_path_rejects_unsafe_account_ids_before_writing(
        tmp_path, monkeypatch, account_id):
    module = load_runtime_paths()
    for key, value in worker_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    with pytest.raises(module.RuntimePathError):
        module.state_path("wechat-token", account_id)
    assert not (tmp_path / "a" / "state").exists()


def test_state_path_rejects_unknown_kind(tmp_path, monkeypatch):
    module = load_runtime_paths()
    for key, value in worker_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    with pytest.raises(module.RuntimePathError):
        module.state_path("admin-secret", "default")


# ---------------------------------------------------------------------------
# 公众号配置与 token：真实函数落到私有根，A/B 互不影响
# ---------------------------------------------------------------------------


PROBE = r"""
import json, sys
from pathlib import Path
sys.path.insert(0, {scripts!r})
sys.path.insert(0, {shared!r})
import config, wechat_token
print(json.dumps({{
    "config": str(config._config_path()),
    "token": str(wechat_token._get_token_cache_file("web")),
}}))
"""


def test_wechat_real_config_and_token_paths_are_private_in_worker(tmp_path):
    env = worker_env(tmp_path, "a")
    env["PYTHONPATH"] = ""
    result = run_python(PROBE.format(scripts=str(WECHAT_SCRIPTS),
                                     shared=str(SHARED)), env, tmp_path)
    assert result.returncode == 0, result.stderr
    paths = json.loads(result.stdout.strip().splitlines()[-1])
    assert paths["config"] == str(
        tmp_path / "a" / "state" / "skills" / "wechat" / "default.yaml")
    assert paths["token"] == str(
        tmp_path / "a" / "state" / "skills" / "wechat" / "tokens" / "web.json")


TOKEN_PROBE = r"""
import json, sys
from pathlib import Path
sys.path.insert(0, {scripts!r})
sys.path.insert(0, {shared!r})
import config, wechat_token
wechat_token.get_config = lambda account_name=None: {{
    "app_id": "app", "app_secret": "secret", "account_key": "web",
}}
wechat_token._fetch_token_with_retry = lambda **kw: {{
    "access_token": "token-{name}", "expires_in": 7200,
}}
token = wechat_token.get_access_token(force_refresh=True, account_name="web")
print(json.dumps({{"token": token,
                  "file": str(wechat_token._get_token_cache_file("web"))}}))
"""


def test_wechat_token_cache_is_per_workspace_and_skips_shared_dir(tmp_path):
    before = set(WECHAT_SCRIPTS.glob(".token_cache*"))
    files = {}
    for name in ("a", "b"):
        env = worker_env(tmp_path, name)
        result = run_python(TOKEN_PROBE.format(
            scripts=str(WECHAT_SCRIPTS), shared=str(SHARED), name=name),
            env, tmp_path)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout.strip().splitlines()[-1])
        files[name] = Path(data["file"])
        assert json.loads(files[name].read_text(encoding="utf-8"))[
            "token"] == f"token-{name}"
    assert files["a"] != files["b"]
    assert files["a"].is_file() and files["b"].is_file()
    assert set(WECHAT_SCRIPTS.glob(".token_cache*")) == before


def test_wechat_worker_does_not_read_shared_skill_config(tmp_path):
    env = worker_env(tmp_path, "a")
    code = (
        "import sys, json\n"
        f"sys.path.insert(0, {str(WECHAT_SCRIPTS)!r})\n"
        f"sys.path.insert(0, {str(SHARED)!r})\n"
        "import config\n"
        "print(json.dumps({'found': str(config._find_unified_yaml() or '')}))\n"
    )
    result = run_python(code, env, tmp_path)
    assert result.returncode == 0, result.stderr
    found = json.loads(result.stdout.strip().splitlines()[-1])["found"]
    assert found == ""
    assert str(WECHAT_SCRIPTS.parent) not in found


# ---------------------------------------------------------------------------
# 其他共享写入者
# ---------------------------------------------------------------------------


def test_account_stats_analytics_dir_uses_workspace_outputs(tmp_path):
    code = (
        "import sys, json\n"
        f"sys.path.insert(0, {str(SHARED)!r})\n"
        "import account_stats\n"
        "print(json.dumps({'dir': str(account_stats.ANALYTICS_DIR)}))\n"
    )
    env = worker_env(tmp_path, "a")
    result = run_python(code, env, tmp_path)
    assert result.returncode == 0, result.stderr
    directory = json.loads(result.stdout.strip().splitlines()[-1])["dir"]
    assert directory == str(
        tmp_path / "a" / "workspace" / "outputs" / "_analytics")


def test_bili_cookie_defaults_to_private_state_in_worker(tmp_path):
    code = (
        "import sys, json\n"
        f"sys.path.insert(0, {str(SHARED)!r})\n"
        "import bili_login\n"
        "print(json.dumps({'cookie': str(bili_login.DEFAULT_COOKIE)}))\n"
    )
    env = worker_env(tmp_path, "a")
    result = run_python(code, env, tmp_path)
    assert result.returncode == 0, result.stderr
    cookie = json.loads(result.stdout.strip().splitlines()[-1])["cookie"]
    assert cookie == str(
        tmp_path / "a" / "state" / "skills" / "platform-cookies"
        / "bilibili-default.json")


def test_wechat_logout_only_clears_current_workspace(tmp_path, monkeypatch):
    import web.app as web

    monkeypatch.setattr(web, "_SERVICE_MODE", "worker")

    def use(name: str) -> None:
        for key, value in worker_env(tmp_path, name).items():
            monkeypatch.setenv(key, value)

    use("a")
    web._wechat_save_credentials("app-a", "secret-a", name="A")
    token_a = Path(os.environ["EASEL_STATE_ROOT"]) / "skills" / "wechat" / "tokens" / "web.json"
    token_a.parent.mkdir(parents=True, exist_ok=True)
    token_a.write_text('{"token": "a"}', encoding="utf-8")

    use("b")
    web._wechat_save_credentials("app-b", "secret-b", name="B")
    token_b = Path(os.environ["EASEL_STATE_ROOT"]) / "skills" / "wechat" / "tokens" / "web.json"
    token_b.parent.mkdir(parents=True, exist_ok=True)
    token_b.write_text('{"token": "b"}', encoding="utf-8")

    use("a")
    web._wechat_clear_credentials()
    assert "web" not in (web._wechat_load_yaml().get("accounts") or {})
    assert not token_a.exists()
    assert token_b.is_file()


def test_web_worker_helpers_use_private_browser_and_cookie_paths(
        tmp_path, monkeypatch):
    import web.app as web

    monkeypatch.setattr(web, "_SERVICE_MODE", "worker")
    for key, value in worker_env(tmp_path, "a").items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(web, "BROWSER_PROFILES",
                        tmp_path / "a" / "home" / ".easel-browser-profiles")

    assert web._browser_profile_path("xiaohongshu") == (
        tmp_path / "a" / "home" / ".easel-browser-profiles"
        / "xiaohongshu" / "default")
    assert web._bili_cookie_path() == (
        tmp_path / "a" / "state" / "skills" / "platform-cookies"
        / "bilibili-default.json")


def test_builtin_analytics_writers_use_workspace_outputs(tmp_path):
    builtin = REPO / "skills" / "builtin"
    code = (
        "import sys, json\n"
        f"sys.path.insert(0, {str(SHARED)!r})\n"
        f"sys.path.insert(0, {str(builtin / 'skill-data-tracker' / 'scripts')!r})\n"
        f"sys.path.insert(0, {str(builtin / 'skill-publish-analytics' / 'scripts')!r})\n"
        f"sys.path.insert(0, {str(builtin / 'skill-publish-log' / 'scripts')!r})\n"
        "import track, analyze, log\n"
        "print(json.dumps({\n"
        " 'snap': str(track.SNAP_ROOT),\n"
        " 'track_log': str(track.OUTPUTS_DIR / '_analytics' / 'follower-log.json'),\n"
        " 'analyze': str(analyze.DEFAULT_DATA),\n"
        " 'log': str(log.DEFAULT_DATA),\n"
        "}))\n"
    )
    env = worker_env(tmp_path, "a")
    result = run_python(code, env, tmp_path)
    assert result.returncode == 0, result.stderr
    paths = json.loads(result.stdout.strip().splitlines()[-1])
    outputs = tmp_path / "a" / "workspace" / "outputs"
    assert Path(paths["snap"]) == outputs / "_analytics" / "snapshots"
    assert Path(paths["track_log"]) == outputs / "_analytics" / "follower-log.json"
    assert Path(paths["analyze"]) == outputs / "_analytics" / "publish-log.json"
    assert Path(paths["log"]) == outputs / "_analytics" / "publish-log.json"


def test_browser_scripts_use_nested_private_profiles_in_worker(tmp_path):
    code = (
        "import sys, json\n"
        f"sys.path.insert(0, {str(SHARED)!r})\n"
        "import account_stats, douyin_publish, web_publisher, weixin_mp_stats\n"
        "print(json.dumps({\n"
        " 'xhs': str(account_stats._profile_dir('xiaohongshu', None)),\n"
        " 'douyin': str(douyin_publish._profile_dir(None)),\n"
        " 'kuaishou': str(web_publisher._profile_dir('kuaishou', None)),\n"
        " 'wechat': str(weixin_mp_stats._profile_dir(None)),\n"
        "}))\n"
    )
    env = worker_env(tmp_path, "a")
    result = run_python(code, env, tmp_path)
    assert result.returncode == 0, result.stderr
    paths = json.loads(result.stdout.strip().splitlines()[-1])
    root = tmp_path / "a" / "home" / ".easel-browser-profiles"
    assert Path(paths["xhs"]) == root / "xiaohongshu" / "default"
    assert Path(paths["douyin"]) == root / "douyin" / "default"
    assert Path(paths["kuaishou"]) == root / "kuaishou" / "default"
    assert Path(paths["wechat"]) == root / "wechat-oa" / "default"


def test_worker_rejects_profile_base_override(tmp_path):
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(SHARED)!r})\n"
        "import xhs_publish\n"
        "try:\n"
        "    xhs_publish._profile_dir('C:/arbitrary')\n"
        "except Exception as e:\n"
        "    print(type(e).__name__)\n"
        "else:\n"
        "    print('NO-ERROR')\n"
    )
    env = worker_env(tmp_path, "a")
    result = run_python(code, env, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("RuntimePathError")


def test_builtin_bili_cookie_uses_private_state(tmp_path):
    builtin = REPO / "skills" / "builtin" / "skill-bilibili-upload" / "scripts"
    code = (
        "import sys, json\n"
        f"sys.path.insert(0, {str(SHARED)!r})\n"
        f"sys.path.insert(0, {str(builtin)!r})\n"
        "import bili_upload\n"
        "print(json.dumps({'cookie': bili_upload._default_cookie()}))\n"
    )
    env = worker_env(tmp_path, "a")
    result = run_python(code, env, tmp_path)
    assert result.returncode == 0, result.stderr
    cookie = json.loads(result.stdout.strip().splitlines()[-1])["cookie"]
    assert Path(cookie) == (
        tmp_path / "a" / "state" / "skills" / "platform-cookies"
        / "bilibili-default.json")


def test_script_default_outputs_use_workspace_in_worker(tmp_path):
    code = (
        "import sys, json\n"
        f"sys.path.insert(0, {str(SHARED)!r})\n"
        "import douyin_publish, xhs_publish, weixin_mp_stats\n"
        "print(json.dumps({\n"
        " 'douyin': str(douyin_publish.DEFAULT_QR_OUT),\n"
        " 'xhs': str(xhs_publish.DEFAULT_QR_OUT),\n"
        " 'weixin': str(weixin_mp_stats.LOGIN_DIR),\n"
        "}))\n"
    )
    env = worker_env(tmp_path, "a")
    result = run_python(code, env, tmp_path)
    assert result.returncode == 0, result.stderr
    paths = json.loads(result.stdout.strip().splitlines()[-1])
    workspace = tmp_path / "a" / "workspace" / "outputs" / "_login"
    assert Path(paths["douyin"]).parent == workspace
    assert Path(paths["xhs"]).parent == workspace
    assert Path(paths["weixin"]) == workspace
