# A1 R2 红绿证据

日期：2026-10-27（Asia/Shanghai）

## 红：上一提交 `a2b83fa` 的 Test 脚本跑当前 45 项 Desktop 测试

```text
C:/Users/hyh/anaconda3/envs/Agent/python.exe -m pytest tests/multiuser/test_desktop_deployment.py -q
10 failed, 35 passed in 28.86s
```

失败项（共 10）：

```text
FAILED tests/multiuser/test_desktop_deployment.py::test_diagnose_accepts_expected_container
FAILED tests/multiuser/test_desktop_deployment.py::test_diagnose_rejects_actual_wiring_mismatch[wrong_mount_source-[fail] control_volume_source]
FAILED tests/multiuser/test_desktop_deployment.py::test_diagnose_rejects_actual_wiring_mismatch[wrong_key_mount_source-[fail] key_volume_source]
FAILED tests/multiuser/test_desktop_deployment.py::test_diagnose_rejects_actual_wiring_mismatch[key_writable-[fail] key_volume_readonly]
FAILED tests/multiuser/test_desktop_deployment.py::test_diagnose_rejects_actual_wiring_mismatch[wrong_origin-[fail] command_matches]
FAILED tests/multiuser/test_desktop_deployment.py::test_diagnose_rejects_actual_wiring_mismatch[wrong_command-[fail] command_matches]
FAILED tests/multiuser/test_desktop_deployment.py::test_diagnose_rejects_actual_wiring_mismatch[extra_port-[fail] only_published_port]
FAILED tests/multiuser/test_desktop_deployment.py::test_diagnose_rejects_actual_wiring_mismatch[wrong_compose_project-[fail] compose_project]
FAILED tests/multiuser/test_desktop_deployment.py::test_diagnose_rejects_actual_wiring_mismatch[wrong_compose_service-[fail] compose_service]
FAILED tests/multiuser/test_desktop_deployment.py::test_diagnose_rejects_actual_wiring_mismatch[wrong_entrypoint-[fail] command_matches]
```

失败共同原因：静态契约检查失败后仍调用 key helper；合法路径 helper 未使用 readonly，且以实际 keyVolumeName 生成挂载。

## 绿：当前 Test 脚本跑同一 45 项

```text
C:/Users/hyh/anaconda3/envs/Agent/python.exe -m pytest tests/multiuser/test_desktop_deployment.py -q
45 passed in 26.76s
```

## 真实 Docker 合法诊断

- 镜像：`sha256:0f63a617aa1f95090edbc3073e6447dbfd1c2bc6b15e95f0c7e193e789bc917f`
- 初始化：`key=created`；启动健康；`Test-Easel.ps1` 20 项全部通过。
- 独立 readonly helper：预期 `...-keys` 卷、`dst=/keys,readonly`，输出 `key=exists`。
- 清理：随机测试容器/卷已删除，无残留；未使用全局 prune。
- 未改变镜像应用内容/依赖；未重做 ngrok 双身份场景。

完整运行摘要见 [report.md](report.md)。
