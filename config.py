"""从环境变量加载 Solo 配置，目录层级与 Arena 保持一致。"""

import os
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv

BACKEND_ROOT = Path(__file__).resolve().parent
load_dotenv(BACKEND_ROOT.parent / ".env")
load_dotenv(BACKEND_ROOT / ".env", override=True)


class DatabaseSettings:
    HOST = os.getenv("PGSQL_HOST", "127.0.0.1")
    PORT = int(os.getenv("PGSQL_PORT", "5432"))
    USERNAME = os.getenv("PGSQL_USER", "solo")
    PASSWORD = os.getenv("PGSQL_PASSWORD", "")
    DATABASE = os.getenv("SOLO_DATABASE", "solo")
    URL = f"postgresql+psycopg://{quote_plus(USERNAME)}:{quote_plus(PASSWORD)}@{HOST}:{PORT}/{DATABASE}"

    @classmethod
    def validate(cls) -> None:
        if not cls.PASSWORD:
            raise RuntimeError("缺少 PostgreSQL 配置：PGSQL_PASSWORD")


class DolphinSchedulerSettings:
    DATABASE = os.getenv("DOLPHINSCHEDULER_DATABASE", "solo_ds")


class SoloSettings:
    SHARED_DIR = Path(os.getenv("SOLO_SHARED_DIR", "/shared"))
    WEB_ORIGINS = os.getenv(
        "SOLO_WEB_ORIGINS", "http://127.0.0.1:5174,http://localhost:5174"
    ).split(",")
