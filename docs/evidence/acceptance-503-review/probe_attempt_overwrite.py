"""只在临时目录复现 attempt 覆盖，不连接 Docker 或访问用户凭证。"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace


def main() -> None:
    root = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(root))
    from easel.control.types import WorkerHandle

    spec = importlib.util.spec_from_file_location(
        "acceptance_503_provisioner_probe",
        root / "deploy/multiuser/provisioner.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    workspace = "a" * 32
    worker = "b" * 64
    gateway = "c" * 64
    original_attempt = "d" * 32
    later_attempt = "e" * 32
    with tempfile.TemporaryDirectory(prefix="easel-attempt-probe-") as temp:
        provisioner = module.Provisioner.__new__(module.Provisioner)
        provisioner.settings = SimpleNamespace(workspace_root=Path(temp))
        provisioner._attempts = {}
        provisioner._locks = {}
        provisioner._gateway = object()
        handle = WorkerHandle(
            workspace, worker, "http://127.0.0.1:7860", "fake-token", 1,
            gateway_instance_id=gateway)
        provisioner._record_attempt(
            workspace, original_attempt, handle,
            worker_created=False, gateway_created=True)
        provisioner._record_attempt(
            workspace, later_attempt, handle,
            worker_created=False, gateway_created=False)
        record = provisioner._load_attempt(workspace)
        outcome = {"original_attempt_overwritten":
                   record["attempt_id"] != original_attempt}
        try:
            asyncio.run(provisioner.abort(
                workspace, worker, gateway, attempt_id=original_attempt,
                scope="gateway", worker_created=False, gateway_created=True))
        except module.ProvisionerError as error:
            outcome["original_abort_error"] = error.code
        provisioner._attempts.clear()
        outcome["record_loadable_after_restart"] = (
            provisioner._load_attempt(workspace) is not None)
        print(json.dumps(outcome))
    print("TEMP_CLEANUP=ok; LIVE_RESOURCES_TOUCHED=false")


if __name__ == "__main__":
    main()
