from fastapi import APIRouter, HTTPException

from core.database.health import DatabaseError, check_database

router = APIRouter(prefix="/api/v1", tags=["system"])


@router.get("/health")
def health() -> dict[str, str]:
    try:
        database = check_database()
    except DatabaseError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return {"status": "ok", "database": database.database, "schema": database.schema}
