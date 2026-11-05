from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "builtin" / "database-query"
CONFIG = SKILL / "assets" / "config.multiuser.yaml"
SCRIPTS = SKILL / "scripts"

EXPECTED_IDS = {
    "yuanmeng_mall_tracking_readonly",
    "yuanmeng_space_readonly",
    "collectibles_readonly",
}


def _app_config():
    sys.path.insert(0, str(SCRIPTS))
    try:
        from dbquery.config import AppConfig
    finally:
        sys.path.remove(str(SCRIPTS))
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
