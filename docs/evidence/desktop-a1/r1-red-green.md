# A1 R1 红绿证据

日期：2026-10-27（Asia/Shanghai）

## 红：旧脚本 `1cee445` 跑当前 44 项 Desktop 测试

```text
C:/Users/hyh/anaconda3/envs/Agent/python.exe -m pytest tests/multiuser/test_desktop_deployment.py -q
28 failed, 16 passed in 37.39s
```

失败项（共 28）：

```text
FAILED tests/multiuser/test_desktop_deployment.py::test_start_rejects_volume_contract_violations[foreign_control-volume_not_owned]
FAILED tests/multiuser/test_desktop_deployment.py::test_start_rejects_volume_contract_violations[foreign_keys-volume_not_owned]
FAILED tests/multiuser/test_desktop_deployment.py::test_start_rejects_volume_contract_violations[missing_labels-volume_not_owned]
FAILED tests/multiuser/test_desktop_deployment.py::test_start_rejects_volume_contract_violations[name_mismatch-volume_not_owned]
FAILED tests/multiuser/test_desktop_deployment.py::test_start_rejects_volume_contract_violations[wrong_role-volume_not_owned]
FAILED tests/multiuser/test_desktop_deployment.py::test_start_rejects_volume_contract_violations[bad_json-volume_inspect_invalid]
FAILED tests/multiuser/test_desktop_deployment.py::test_start_rejects_volume_contract_violations[empty_inspect-volume_inspect_invalid]
FAILED tests/multiuser/test_desktop_deployment.py::test_start_rejects_volume_contract_violations[multiple_inspect-volume_inspect_invalid]
FAILED tests/multiuser/test_desktop_deployment.py::test_start_rejects_missing_volume_before_helper
FAILED tests/multiuser/test_desktop_deployment.py::test_test_easel_rejects_volume_contract_before_container_inspect[foreign_control-volume_not_owned]
FAILED tests/multiuser/test_desktop_deployment.py::test_test_easel_rejects_volume_contract_before_container_inspect[missing_labels-volume_not_owned]
FAILED tests/multiuser/test_desktop_deployment.py::test_test_easel_rejects_volume_contract_before_container_inspect[name_mismatch-volume_not_owned]
FAILED tests/multiuser/test_desktop_deployment.py::test_test_easel_rejects_volume_contract_before_container_inspect[bad_json-volume_inspect_invalid]
FAILED tests/multiuser/test_desktop_deployment.py::test_diagnose_accepts_expected_container
FAILED tests/multiuser/test_desktop_deployment.py::test_diagnose_rejects_actual_wiring_mismatch[wrong_mount_source-[fail] control_volume_source]
FAILED tests/multiuser/test_desktop_deployment.py::test_diagnose_rejects_actual_wiring_mismatch[bind_mount-[fail] key_volume_source]
FAILED tests/multiuser/test_desktop_deployment.py::test_diagnose_rejects_actual_wiring_mismatch[key_writable-[fail] key_volume_readonly]
FAILED tests/multiuser/test_desktop_deployment.py::test_diagnose_rejects_actual_wiring_mismatch[wrong_origin-[fail] command_matches]
FAILED tests/multiuser/test_desktop_deployment.py::test_diagnose_rejects_actual_wiring_mismatch[wrong_command-[fail] command_matches]
FAILED tests/multiuser/test_desktop_deployment.py::test_diagnose_rejects_actual_wiring_mismatch[extra_port-[fail] only_published_port]
FAILED tests/multiuser/test_desktop_deployment.py::test_diagnose_rejects_actual_wiring_mismatch[wrong_compose_project-[fail] compose_project]
FAILED tests/multiuser/test_desktop_deployment.py::test_diagnose_rejects_actual_wiring_mismatch[wrong_compose_service-[fail] compose_service]
FAILED tests/multiuser/test_desktop_deployment.py::test_diagnose_rejects_actual_wiring_mismatch[wrong_entrypoint-[fail] command_matches]
FAILED tests/multiuser/test_desktop_deployment.py::test_diagnose_rejects_actual_wiring_mismatch[empty_inspect-container_inspect_invalid]
FAILED tests/multiuser/test_desktop_deployment.py::test_diagnose_rejects_actual_wiring_mismatch[multiple_inspect-container_inspect_invalid]
FAILED tests/multiuser/test_desktop_deployment.py::test_diagnose_rejects_actual_wiring_mismatch[invalid_inspect-container_inspect_invalid]
FAILED tests/multiuser/test_desktop_deployment.py::test_diagnose_rejects_actual_wiring_mismatch[missing_config-container_inspect_invalid]
FAILED tests/multiuser/test_desktop_deployment.py::test_diagnose_rejects_actual_wiring_mismatch[missing_mounts-container_inspect_invalid]
```

## 绿：当前脚本跑同一 44 项

```text
C:/Users/hyh/anaconda3/envs/Agent/python.exe -m pytest tests/multiuser/test_desktop_deployment.py -q
44 passed in 27.07s
```

## 真实 Docker 复跑摘要

- 镜像：`sha256:3df01c1d2072a05c757e2d00bc43a31bcff93d2db5622dd5654ff9f90975e335`
- 外来卷直接 Start：exit 1 `volume_not_owned`，无容器创建。
- 合法项目首次初始化：`key=created`；启动健康；`Test-Easel.ps1` 20 项通过。
- 停止后重开：无运行容器；再次初始化 `key=exists`；启动健康；`Test-Easel.ps1` 20 项再次通过。
- 清理：随机测试容器/卷已删除，无残留；未使用全局 prune。
- 仅修改部署脚本/测试/文档，未改镜像应用内容/依赖；按审计单保留已有 A1 ngrok 浏览器证据，未重做浏览器场景。

完整运行摘要见 [report.md](report.md)。
