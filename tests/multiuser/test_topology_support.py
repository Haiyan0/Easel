"""Docker 拓扑测试的纯逻辑回归；不依赖 Docker，也不会被集成 skip 标记跳过。"""
from __future__ import annotations

import json
import subprocess

from tests.multiuser import topology_support as support


def _ok(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, stdout, "")


def _fail(stderr: str, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 1, stdout, stderr)


def _inspect(kind: str, name: str, run_id: str) -> subprocess.CompletedProcess:
    labels = {support.RUN_LABEL: run_id}
    if kind == "container":
        payload = [{"Name": name, "Config": {"Labels": labels}}]
    else:
        payload = [{"Name": name, "Labels": labels}]
    return _ok(json.dumps(payload))


def _absent(kind: str, name: str) -> subprocess.CompletedProcess:
    if kind == "container":
        return _fail(f"Error: No such object: {name}\n")
    if kind == "network":
        return _fail(f"Error response from daemon: network {name} not found\n")
    return _fail(f"Error response from daemon: get {name}: no such volume\n")


class ScriptedRun:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, timeout=20.0):
        self.calls.append(list(argv))
        return self.handler(argv)


def _is_delete(argv: list[str]) -> bool:
    return argv[1:2] in (["rm"],) or argv[1:3] in (
        ["network", "rm"], ["volume", "rm"])


def test_volume_init_uses_owner_only_restricted_modes():
    script = support.volume_init_script()
    assert "install -d -o 10001 -g 10001 -m 0750 /control /sidecar /worker" in script
    assert "install -o 10001 -g 10001 -m 0400" in script
    assert "install -o 10001 -g 10001 -m 0600" in script
    for forbidden in ("0777", "0666", "chmod 777", "chmod 666"):
        assert forbidden not in script


def test_component_mounts_do_not_share_control_materials():
    mounts = support.component_mounts("control-vol", "sidecar-vol",
                                      "worker-vol")
    assert mounts == {
        "control": ["control-vol:/srv/easel"],
        "sidecar": ["sidecar-vol:/etc/easel:ro"],
        "worker": ["worker-vol:/check:ro"],
    }
    for component in ("sidecar", "worker"):
        joined = " ".join(mounts[component])
        assert "control-vol" not in joined
        assert "/srv/easel" not in joined


def test_volume_init_keeps_control_materials_out_of_downstream_volumes():
    script = support.volume_init_script()
    lines = script.splitlines()
    for line in lines:
        if (line.startswith("install")
                and ("master.key" in line or "control.sqlite3" in line)):
            assert "/control/" in line
    assert "/sidecar/policy.json" in script
    assert "/worker/client.py" in script
    assert "/sidecar/client.py" not in script
    assert "/worker/policy.json" not in script


def test_cleanup_skips_absent_resources_idempotently():
    names = [("container", "worker-a"), ("network", "net-a"),
             ("volume", "vol-a")]

    def handler(argv):
        kind = ("container" if argv[1:2] == ["container"]
                else "network" if argv[1:2] == ["network"]
                else "volume")
        name = argv[-1]
        if _is_delete(argv):
            raise AssertionError("不存在资源不得执行删除")
        return _absent(kind, name)

    run = ScriptedRun(handler)
    errors = support.cleanup_resources(run, names, "run-a")
    assert errors == []
    assert not [call for call in run.calls if _is_delete(call)]


def test_cleanup_does_not_delete_foreign_resources():
    names = [("container", "worker-a"), ("network", "net-a"),
             ("volume", "vol-a")]

    def handler(argv):
        labels = {"easel.test.run-id": "other-run"}
        if argv[1:2] == ["container"]:
            payload = [{"Config": {"Labels": labels}}]
        else:
            payload = [{"Labels": labels}]
        return _ok(json.dumps(payload))

    run = ScriptedRun(handler)
    errors = support.cleanup_resources(run, names, "run-a")
    assert len(errors) == 3
    assert all("ownership" in error for error in errors)
    assert not [call for call in run.calls if _is_delete(call)]


def test_cleanup_continues_after_timeout_and_nonzero_delete():
    names = [("container", "worker-a"), ("container", "worker-b"),
             ("network", "net-a"), ("volume", "vol-a")]

    def handler(argv):
        kind = ("container" if argv[1:2] == ["container"]
                else "network" if argv[1:2] == ["network"]
                else "volume")
        name = argv[-1]
        if not _is_delete(argv):
            return _inspect(kind, name, "run-a")
        if name == "worker-a":
            raise subprocess.TimeoutExpired(argv, 1)
        if name == "worker-b":
            return _fail("Error response from daemon: rm failed\n")
        return _ok()

    run = ScriptedRun(handler)
    errors = support.cleanup_resources(run, names, "run-a")
    deletes = [call[-1] for call in run.calls if _is_delete(call)]
    assert deletes == ["worker-a", "worker-b", "net-a", "vol-a"]
    assert any("worker-a" in error and "timeout" in error.lower()
               for error in errors)
    assert any("worker-b" in error for error in errors)
    assert not any("net-a" in error or "vol-a" in error for error in errors)


def test_cleanup_accepts_absent_after_failed_delete():
    names = [("container", "worker-a")]
    inspections = 0

    def handler(argv):
        nonlocal inspections
        if _is_delete(argv):
            return _fail("Error response from daemon: rm failed\n")
        inspections += 1
        if inspections == 1:
            return _inspect("container", "worker-a", "run-a")
        return _absent("container", "worker-a")

    run = ScriptedRun(handler)
    # 删除失败后复核已不存在，应按幂等成功处理。
    errors = support.cleanup_resources(run, names, "run-a")
    assert errors == []
    assert [call[-1] for call in run.calls if _is_delete(call)] == [
        "worker-a"]


def test_named_client_remains_cleanable_after_cli_timeout():
    names = [("container", "client-a")]

    def handler(argv):
        if _is_delete(argv):
            return _ok()
        return _inspect("container", "client-a", "run-a")

    run = ScriptedRun(handler)
    try:
        raise subprocess.TimeoutExpired(["docker", "run", "client-a"], 1)
    except subprocess.TimeoutExpired:
        pass
    finally:
        errors = support.cleanup_resources(run, names, "run-a")
    assert errors == []
    assert [call[-1] for call in run.calls if _is_delete(call)] == [
        "client-a"]


def test_cleanup_normal_path_deletes_labeled_resources():
    names = [("container", "worker-a"), ("network", "net-a"),
             ("volume", "vol-a")]

    def handler(argv):
        kind = ("container" if argv[1:2] == ["container"]
                else "network" if argv[1:2] == ["network"]
                else "volume")
        name = argv[-1]
        if _is_delete(argv):
            return _ok()
        return _inspect(kind, name, "run-a")

    run = ScriptedRun(handler)
    errors = support.cleanup_resources(run, names, "run-a")
    assert errors == []
    assert [call[-1] for call in run.calls if _is_delete(call)] == [
        "worker-a", "net-a", "vol-a"]
