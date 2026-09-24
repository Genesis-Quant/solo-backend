import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from config import SoloSettings

from core.database.health import DatabaseError, check_database

router = APIRouter(prefix="/api/v1", tags=["system"])


@router.get("/reports/{path:path}")
def report_file(path: str) -> FileResponse:
    """读取共享 runs 目录中已完成运行的清单及其声明的 Parquet。"""
    root = (SoloSettings.SHARED_DIR / "runs").resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(404, "报告文件不存在")
    manifest_path = target.parent / "run.json"
    if not manifest_path.resolve().is_relative_to(root):
        raise HTTPException(404, "报告文件不存在")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(404, "报告尚未完成") from None
    if not isinstance(manifest, dict) or manifest.get("status") != "success":
        raise HTTPException(409, "报告尚未完成")
    reports = manifest.get("reports")
    if not isinstance(reports, dict):
        raise HTTPException(409, "报告清单无效")
    if target.name != "run.json" and (target.suffix != ".parquet" or target.name not in reports.values()):
        raise HTTPException(404, "文件未列入报告清单")
    return FileResponse(target, media_type="application/json" if target.name == "run.json" else "application/octet-stream")


@router.get("/health")
def health() -> dict[str, str]:
    try:
        database = check_database()
    except DatabaseError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return {"status": "ok", "database": database.database, "schema": database.schema}
