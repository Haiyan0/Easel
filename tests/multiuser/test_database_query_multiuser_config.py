from __future__ import annotations

import importlib.util
import sys
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "builtin" / "database-query"
CONFIG = SKILL / "assets" / "config.multiuser.yaml"
EXAMPLE = SKILL / "assets" / "tunnel.multiuser.example.yaml"
SCRIPTS = SKILL / "scripts"

EXPECTED_IDS = {
    "yuanmeng_mall_tracking_readonly",
    "yuanmeng_space_readonly",
    "collectibles_readonly",
}


def _app_config():
    sys.path.insert(0, str(SCRIPTS))
    # 导入技能包时禁止写入 __pycache__，避免污染技能目录并触发缓存检查
    original_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        from dbquery.config import AppConfig
    finally:
        sys.path.remove(str(SCRIPTS))
        sys.dont_write_bytecode = original_dont_write_bytecode
    return AppConfig


def test_multiuser_config_is_valid_and_enables_three_sources():
    config = _app_config().load(CONFIG)
    assert {source.id for source in config.sources} == EXPECTED_IDS
    assert all(source.enabled for source in config.sources)
    assert all(source.readonly_account_verified for source in config.sources)


def test_multiuser_sources_cover_all_project_doc_bindings():
    catalogue = yaml.safe_load(
        (ROOT / "skills/builtin/project-docs/references/projects.yaml")
        .read_text(encoding="utf-8"))
    connection_ids = {
        project["connection_id"] for project in catalogue["projects"]
        if project.get("connection_id")
    }
    config = _app_config().load(CONFIG)
    assert connection_ids == EXPECTED_IDS
    assert connection_ids <= {source.id for source in config.sources}


def _db_tunnel():
    spec = importlib.util.spec_from_file_location(
        "db_tunnel_for_config_test",
        ROOT / "deploy" / "multiuser" / "db_tunnel.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_enabled_sources_have_exactly_one_matching_forward():
    config = _app_config().load(CONFIG)
    forwards = _db_tunnel().load_forwards(EXAMPLE, allow_placeholders=True)
    enabled = {source.id for source in config.sources if source.enabled}
    counts = Counter(forward.source for forward in forwards)
    assert set(counts) == enabled
    assert all(count == 1 for count in counts.values())
    assert len({forward.listen_port for forward in forwards}) == len(forwards)
    assert all("PRIVATE KEY" not in path.read_text(encoding="utf-8")
               for path in (CONFIG, EXAMPLE))
    assert all("mysql+asyncmy://" not in path.read_text(encoding="utf-8")
               for path in (CONFIG, EXAMPLE))
