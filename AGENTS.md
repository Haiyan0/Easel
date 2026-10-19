# AGENTS.md

本文件为 Codex（Codex.ai/code）在此仓库中编写代码时提供指导。

## Python 环境

当前机台 Python 路径：**C:\Users\hyh\anaconda3\envs\Agent\python.exe**（conda 环境，勿用系统 `python` 泛指）

当前工作项目目录：**C:\Code\Easel**

脚本运行环境：**Powershell**

运行系统：**win11 系统**

## 中文交流规范

- 计划、审查和逻辑解释必须使用**中文**
- 代码注释使用**简体中文**
- 代码标识符保持英文
- **git 提交信息使用简体中文**（如 `feat: 新增服务端认证会话`）

## 工作流程

1. **开始任务前**: 先确定流程节点和验收计划，以中文展示步骤状态（已完成/进行中/待处理）
2. **Karpathy 准则**: 先思考再编码、极简实现、精确定位修改、目标驱动验收

## 核心规则

1. **收到任务时，先检查是否有匹配的 skill** — 哪怕只有 1% 的可能性也要检查
2. **设计先于编码** — 收到功能需求时，先用 brainstorming skill 做需求分析
3. **测试先于实现** — 写代码前先写测试（TDD）
4. **验证先于完成** — 声称完成前必须运行验证命令

## 构建与运行命令

本机台已配置 conda 环境 `Agent`，无需重复安装。

### 测试

```bash
# 运行全部测试
pytest tests/

# 运行单个测试文件
pytest tests/xxx/xxx.py -v

# 运行单个测试
pytest tests/xxx/xxx.py::xxx -v
```