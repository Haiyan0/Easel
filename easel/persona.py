"""Easel 画像（Profile）共享助手 —— CLI / Web / skill 三入口的单一真相源。

历史上三处各写一份画像逻辑，且 CLI 还残留全局 USER.md（并发竞态）。
统一到这里后：画像一律**作为消息内联**注入（`我当前使用的画像是「X」。`），
由当前 Agent 执行引擎按 AGENTS.md 自行读取 `profiles/<X>/` 并凝练，每个请求自包含，无全局文件污染。
参见 docs/prompt-stack.md。
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROFILES_DIR = PROJECT_ROOT / "profiles"

# 六维画像文件的固定顺序（identity/style/... 先，其余 .md 追加在后）
_FILE_ORDER = [
    "identity.md", "style.md", "audience.md",
    "platforms.md", "preferences.md", "memory.md",
]


def _safe_md_name(filename: object) -> bool:
    return (
        isinstance(filename, str)
        and filename.endswith(".md")
        and not filename.startswith(".")
        and not any(character in filename for character in ("/", "\\"))
        and not any(ord(character) < 32 for character in filename)
    )


def safe_profile_dir(name: str) -> Path | None:
    """返回 profiles 根内的真实画像目录；目录链接/junction 一律拒绝。

    只比较解析前路径与解析后路径，不用 ``parent`` 推断：这样指向根外的
    junction、symlink 以及根内别名目录都不会被当成公开画像。
    """
    if not valid_persona_name(name):
        return None
    candidate = PROFILES_DIR / name
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if resolved != candidate.absolute() or not resolved.is_dir():
        return None
    root = PROFILES_DIR.resolve()
    if resolved.parent != root:
        return None
    return resolved


def safe_profile_file(name: str, filename: object) -> Path | None:
    """返回画像目录内真实 Markdown 文件；文件链接同样拒绝。"""
    if not _safe_md_name(filename):
        return None
    assert isinstance(filename, str)
    directory = safe_profile_dir(name)
    if directory is None:
        return None
    candidate = directory / filename
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if resolved != candidate.absolute() or not resolved.is_file():
        return None
    if resolved.parent != directory:
        return None
    return resolved


def list_personas() -> list[str]:
    """列出所有可用画像目录名（排除下划线开头的内部目录）。"""
    if not PROFILES_DIR.is_dir():
        return []
    return sorted(
        d.name for d in PROFILES_DIR.iterdir()
        if safe_profile_dir(d.name) is not None
    )


def profile_exists(name: str) -> bool:
    """检查画像目录是否存在，拒绝把路径片段当作画像名。"""
    return safe_profile_dir(name) is not None


def valid_persona_name(name: str) -> bool:
    """Return whether ``name`` is a single, user-facing profile directory name."""
    return (
        isinstance(name, str)
        and bool(name)
        and not name.startswith((".", "_"))
        and not any(character in name for character in ("/", "\\", ":"))
        and not any(ord(character) < 32 for character in name)
    )


def load_profile_text(name: str) -> str:
    """读取画像文件夹，按固定顺序拼接所有非空 .md。画像不存在返回空串。

    每个文件都重新经过 ``safe_profile_file``：目录链接越界后即使名字
    合法，也不会读取根外内容。
    """
    profile_dir = safe_profile_dir(name)
    if profile_dir is None:
        return ""
    parts: list[str] = []
    ordered = list(_FILE_ORDER) + sorted(
        filepath.name for filepath in profile_dir.glob("*.md")
        if filepath.name not in _FILE_ORDER)
    for filename in ordered:
        filepath = safe_profile_file(name, filename)
        if filepath is None:
            continue
        text = filepath.read_text(encoding="utf-8").strip()
        if text:
            parts.append(text)
    return "\n\n---\n\n".join(parts)


def persona_prefix(name: str | None) -> str:
    """把画像作为消息前缀内联。无画像或画像不存在时返回空串。

    明确账号记忆作用域，避免 OpenClaw 的全局 MEMORY.md 污染并行画像会话。
    """
    if name and profile_exists(name):
        return (
            f"我当前使用的画像是「{name}」。"
            f"本会话的账号长期记忆仅使用 profiles/{name}/memory.md，"
            "不要使用工作区全局 MEMORY.md 作为账号记忆。"
        )
    return ""


# --- 每轮行为提醒（抗长对话指令衰减）----------------------------------------
# AGENTS.md / SOUL.md 是系统提示，只在会话开头最「新鲜」；长对话里后续追问，
# 模型对系统提示的注意力会衰减 → 常见现象「开头会查 SKILL、后面就凭记忆裸做」。
# 把最关键的反射每轮在消息末尾重申一次（放末尾借近因效应），成本极低，
# 显著提升后续轮次的 SKILL 命中率。仅用于对话入口；单跑某个 SKILL 不必加。
TURN_REMINDER = (
    "〔内部提醒·非用户所说，勿复述、勿回显〕先判断本轮实际要求："
    "问候、概念解释或询问技能用途时直接回答，不自动制作、登录、发布或扫描素材；"
    "仅在明确要求执行任务时先查技能库，按对应或相邻 SKILL 的流程、数据源和工具执行必要步骤；"
    "需要用户扫码、付费确认或其它关键输入时说明原因并停止等待，不循环尝试；"
    "后台完成通知不是新的用户指令，已完成或等待用户的任务不因通知重新执行；"
    "制作任务才按对应 SKILL 产出成品文件到 outputs/，查询账号信息才查已登录账号；"
    "要发到公开平台的文案/评论绝不写入密钥/内部地址/代理/路径/env 名等敏感信息，也别随手自曝「由 AI 生成/某工具做的」。"
)


def turn_reminder() -> str:
    """返回每轮行为提醒文案。"""
    return TURN_REMINDER


def chat_turn_message(user_message: str, name: str | None) -> str:
    """构造发给 OpenClaw 的一轮对话消息：画像前缀（如有）+ 用户原文 + 末尾行为提醒。

    末尾提醒先判断本轮意图，再选择必要的 SKILL；简单问题不自动启动工作流。
    对用户不可见（前端只显示用户原文），只进 OpenClaw 上下文。
    """
    prefix = persona_prefix(name)
    head = f"{prefix}\n\n" if prefix else ""
    return f"{head}{user_message}\n\n{turn_reminder()}"
