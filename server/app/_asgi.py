"""ASGI 入口：``uvicorn app._asgi:app``。

单独一层是为了让测试只导入 ``create_app``，不会在导入时创建数据库或连 NapCat。
"""

from __future__ import annotations

from .main import create_app

app = create_app()
