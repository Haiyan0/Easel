"""只读核对交付报告 Stop 参数，不运行任何部署脚本。"""
from pathlib import Path
import json
import re
import sys

root = Path(sys.argv[1]).resolve()
script = (root / "deploy/desktop/Stop-WorkspaceEasel.ps1").read_text(
    encoding="utf-8-sig")
block = re.search(r"(?ms)^param\((.*?)^\)", script).group(1)
parameters = re.findall(r"\$(\w+)\s*(?:=|,|$)", block)
print(json.dumps({"stop_parameters": parameters,
                  "report_stop_workspace_root_supported":
                  "WorkspaceRoot" in parameters}))
