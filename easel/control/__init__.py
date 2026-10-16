"""控制平面：登录、配置、会话归属与授权路由。

本包只放控制层实现，不直接运行用户技能，也不在 web/app.py 内继续堆控制逻辑。
"""
from __future__ import annotations
