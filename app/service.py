"""轻量服务层（可选）：供 C 的 Web 前端调用。

需要安装 fastapi + uvicorn（见 requirements.txt）：
    pip install -r requirements.txt
    uvicorn app.service:app --reload --port 8000

核心代码零依赖；未安装 fastapi 时本模块的 app 为 None，不影响其他功能。
"""

from __future__ import annotations

from typing import Any, Dict

from tools import default_registry

try:  # 可选依赖
    from fastapi import FastAPI, HTTPException
    _HAS_FASTAPI = True
except ImportError:  # pragma: no cover
    _HAS_FASTAPI = False
    FastAPI = None  # type: ignore[assignment]


def create_app() -> Any:
    """构建 FastAPI 应用。未安装 fastapi 时抛出 RuntimeError。"""
    if not _HAS_FASTAPI:
        raise RuntimeError("fastapi not installed. Run: pip install -r requirements.txt")
    app = FastAPI(title="TravelAgent Service", version="0.1.0")

    @app.get("/health")
    def health() -> Dict[str, str]:
        return {"status": "ok", "project": "TravelAgent"}

    @app.get("/tools")
    def list_tools() -> Dict[str, Any]:
        return {"tools": default_registry.names()}

    @app.post("/tools/{name}/invoke")
    def invoke_tool(name: str, payload: Dict[str, Any] = {}) -> Dict[str, Any]:
        try:
            return default_registry.call(name, **payload).to_dict()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return app


app = create_app() if _HAS_FASTAPI else None
