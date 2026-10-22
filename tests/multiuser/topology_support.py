"""Docker 拓扑测试的隔离准备与安全清理辅助。

不依赖 Docker，便于纯逻辑回归。所有资源必须带本次随机 run ID 标签；
清理前先 inspect 校验归属，归属不明或属于其他运行时不删除。单项删除
超时/失败只记录并继续清理其余资源，由调用方汇总报告。
"""
from __future__ import annotations

import json
import re
import subprocess

RUN_LABEL = "easel.test.run-id"


def run_labels(run_id: str) -> dict[str, str]:
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id")
    return {RUN_LABEL: run_id}


def label_args(run_id: str) -> list[str]:
    result: list[str] = []
    for name, value in run_labels(run_id).items():
        result.extend(["--label", f"{name}={value}"])
    return result


def volume_init_script() -> str:
    """一次性 root 初始化助手只写本次测试卷，创建 UID 10001 私有权限。

    目录 0750、脚本/策略/测试 key 0400、SQLite 及其附属文件 0600；
    不使用 0777/0666，也不修改宿主临时目录权限。
    """
    return """set -eu
install -d -o 10001 -g 10001 -m 0750 /srv/easel
for file in control_app.py master.key policy.json client.py; do
  install -o 10001 -g 10001 -m 0400 "/seed/$file" "/srv/easel/$file"
done
for file in /seed/control.sqlite3*; do
  [ -e "$file" ] || continue
  install -o 10001 -g 10001 -m 0600 "$file" "/srv/easel/${file##*/}"
done
"""


def _inspect_argv(kind: str, name: str) -> list[str]:
    if kind == "container":
        return ["docker", "container", "inspect", name]
    if kind == "network":
        return ["docker", "network", "inspect", name]
    if kind == "volume":
        return ["docker", "volume", "inspect", name]
    raise ValueError(f"unsupported resource kind: {kind}")


def _delete_argv(kind: str, name: str) -> list[str]:
    if kind == "container":
        return ["docker", "rm", "-f", name]
    if kind == "network":
        return ["docker", "network", "rm", name]
    if kind == "volume":
        return ["docker", "volume", "rm", name]
    raise ValueError(f"unsupported resource kind: {kind}")


def resource_labels(kind: str, stdout: str) -> dict[str, str] | None:
    """从单对象 inspect 输出读取 Labels；结构非法返回 None。"""
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError):
        return None
    if (not isinstance(payload, list) or len(payload) != 1
            or not isinstance(payload[0], dict)):
        return None
    entry = payload[0]
    if kind == "container":
        config = entry.get("Config")
        if not isinstance(config, dict):
            return None
        labels = config.get("Labels")
    else:
        labels = entry.get("Labels")
    return labels if isinstance(labels, dict) else None


def _short(value: object, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    return text[-limit:]


def _absent(kind: str, result: subprocess.CompletedProcess,
            name: str) -> bool:
    text = str(getattr(result, "stderr", "") or "").strip()
    escaped = re.escape(name)
    if kind == "container":
        return re.search(
            rf"(?:No such container|No such object):\s*{escaped}(?:\s|$)",
            text, re.IGNORECASE) is not None
    if kind == "network":
        return re.search(
            rf"(?:network\s+{escaped}\s+not found"
            rf"|No such network:\s*{escaped}(?:\s|$))",
            text, re.IGNORECASE) is not None
    if kind == "volume":
        return "no such volume" in text.lower()
    return False


def _cleanup_one(run, kind: str, name: str, run_id: str) -> str | None:
    inspected = run(_inspect_argv(kind, name), timeout=20.0)
    if inspected.returncode != 0:
        if _absent(kind, inspected, name):
            return None
        return f"{kind} {name}: inspect failed: {_short(inspected.stderr)}"
    labels = resource_labels(kind, inspected.stdout)
    if labels is None:
        return f"{kind} {name}: inspect invalid"
    if labels.get(RUN_LABEL) != run_id:
        return f"{kind} {name}: ownership mismatch; not deleted"
    try:
        deleted = run(_delete_argv(kind, name), timeout=20.0)
    except Exception as error:  # noqa: BLE001 - 单项失败必须继续清理
        return f"{kind} {name}: delete {type(error).__name__}: {_short(error)}"
    if deleted.returncode == 0:
        return None
    try:
        current = run(_inspect_argv(kind, name), timeout=20.0)
    except Exception as error:  # noqa: BLE001 - 单项失败必须继续清理
        return (f"{kind} {name}: delete failed; re-inspect "
                f"{type(error).__name__}: {_short(error)}")
    if current.returncode != 0 and _absent(kind, current, name):
        return None
    return f"{kind} {name}: delete failed: {_short(deleted.stderr)}"


def cleanup_resources(run, resources, run_id: str) -> list[str]:
    """逐项安全清理；返回全部错误，不抛出单项删除异常。"""
    errors: list[str] = []
    for kind, name in resources:
        try:
            error = _cleanup_one(run, kind, name, run_id)
        except Exception as exc:  # noqa: BLE001 - 单项失败必须继续清理
            error = (f"{kind} {name}: cleanup {type(exc).__name__}: "
                     f"{_short(exc)}")
        if error is not None:
            errors.append(error)
    return errors
