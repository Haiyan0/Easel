"""Task 5C1：worker 技能准备与画像基线边界。"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException

from web import app as worker
from easel import persona


def _skill_tree(root: Path) -> Path:
    builtin = root / "builtin" / "demo"
    builtin.mkdir(parents=True)
    (builtin / "SKILL.md").write_text(
        "---\nlayer: general\ndescription: demo skill\n---\n\n# Demo\n",
        encoding="utf-8")
    return root


def test_skill_prepare_returns_only_canonical_prompt(tmp_path, monkeypatch):
    skills = _skill_tree(tmp_path / "skills")
    monkeypatch.setattr(worker, "SKILLS_DIR", skills)
    prepared = asyncio.run(worker.api_skill_prepare(
        worker.SkillPrepareRequest(skill="demo", input="写一段文案")))
    assert prepared["skill"] == "demo"
    assert prepared["needsApi"] is False
    assert prepared["apiConfigured"] is True
    assert "请执行 /demo" in prepared["prompt"]
    assert "写一段文案" in prepared["prompt"]

    with pytest.raises(HTTPException) as missing:
        asyncio.run(worker.api_skill_prepare(
            worker.SkillPrepareRequest(skill="missing", input="x")))
    assert missing.value.status_code == 404
    with pytest.raises(HTTPException) as empty:
        asyncio.run(worker.api_skill_prepare(
            worker.SkillPrepareRequest(skill="demo", input=" ")))
    assert empty.value.status_code == 400


def test_profile_baseline_creates_six_dimensions_and_reuses(tmp_path,
                                                            monkeypatch):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    monkeypatch.setattr(worker, "PROFILES_DIR", profiles)
    monkeypatch.setattr(persona, "PROFILES_DIR", profiles)
    request = worker.ProfileBaselineRequest(
        name="Unicode 画像",
        form={"direction": "数码内容", "platforms": ["小红书"]})
    created = asyncio.run(worker.api_profile_baseline(request))
    assert created["created"] is True
    assert "数码内容" in created["instruction"]
    target = profiles / "Unicode 画像"
    assert target.is_dir()
    assert {path.name for path in target.glob("*.md")} == {
        "identity.md", "style.md", "audience.md", "platforms.md",
        "preferences.md", "memory.md",
    }

    reused = asyncio.run(worker.api_profile_baseline(
        worker.ProfileBaselineRequest(
            name="Unicode 画像", form={}, reuse=True)))
    assert reused["created"] is False
    assert reused["instruction"]

    with pytest.raises(HTTPException) as conflict:
        asyncio.run(worker.api_profile_baseline(
            worker.ProfileBaselineRequest(name="Unicode 画像", form={})))
    assert conflict.value.status_code == 409


def test_profile_baseline_rejects_link_target_before_write(tmp_path,
                                                           monkeypatch):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    private = tmp_path / "private"
    private.mkdir()
    target = profiles / "linked"
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(target), str(private)],
            capture_output=True, text=True)
        if result.returncode != 0:
            pytest.skip("当前环境不允许创建目录 junction")
    else:
        try:
            target.symlink_to(private, target_is_directory=True)
        except OSError:
            pytest.skip("当前环境不允许创建目录 symlink")
    monkeypatch.setattr(worker, "PROFILES_DIR", profiles)
    monkeypatch.setattr(persona, "PROFILES_DIR", profiles)
    with pytest.raises(HTTPException) as rejected:
        asyncio.run(worker.api_profile_baseline(
            worker.ProfileBaselineRequest(name="linked", form={})))
    assert rejected.value.status_code == 500
    assert list(private.iterdir()) == []


def test_profile_baseline_rejects_path_like_names(tmp_path, monkeypatch):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    monkeypatch.setattr(worker, "PROFILES_DIR", profiles)
    monkeypatch.setattr(persona, "PROFILES_DIR", profiles)
    for name in ("../x", "a/b", "a\\b", "_hidden", ".hidden", ""):
        with pytest.raises(HTTPException) as error:
            asyncio.run(worker.api_profile_baseline(
                worker.ProfileBaselineRequest(name=name, form={})))
        assert error.value.status_code == 400
    assert list(profiles.iterdir()) == []
