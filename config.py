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
    ENABLED = os.getenv("DOLPHINSCHEDULER_ENABLED", "false").lower() == "true"
    HOST = os.getenv("DOLPHINSCHEDULER_HOST", "127.0.0.1")
    API_PORT = int(os.getenv("DOLPHINSCHEDULER_API_PORT", "12346"))
    GATEWAY_PORT = int(os.getenv("DOLPHINSCHEDULER_GATEWAY_PORT", "25334"))
    BASE_URL = f"http://{HOST}:{API_PORT}/dolphinscheduler"
    GATEWAY_TOKEN = os.getenv("DOLPHINSCHEDULER_PYTHON_GATEWAY_TOKEN", "")
    USERNAME = os.getenv("DOLPHINSCHEDULER_USERNAME", "solo-scheduler")
    PASSWORD = os.getenv("DOLPHINSCHEDULER_PASSWORD", "")
    PROJECT = "solo-runtime"
    TENANT = "root"
    WORKER_GROUP = "default"
    RUNTIME_COMMAND = "/opt/solo-runtime/.venv/bin/solo-manage"

    @classmethod
    def configure_sdk_environment(cls) -> None:
        if not cls.PASSWORD or not cls.GATEWAY_TOKEN:
            raise RuntimeError("缺少 DolphinScheduler 密码或 Python Gateway token")
        os.environ.update({
            "PYDS_JAVA_GATEWAY_ADDRESS": cls.HOST,
            "PYDS_JAVA_GATEWAY_PORT": str(cls.GATEWAY_PORT),
            "PYDS_JAVA_GATEWAY_AUTH_TOKEN": cls.GATEWAY_TOKEN,
            "PYDS_USER_NAME": cls.USERNAME,
            "PYDS_USER_PASSWORD": cls.PASSWORD,
            "PYDS_USER_TENANT": cls.TENANT,
            "PYDS_WORKFLOW_USER": cls.USERNAME,
            "PYDS_WORKFLOW_PROJECT": cls.PROJECT,
            "PYDS_WORKFLOW_WORKER_GROUP": cls.WORKER_GROUP,
            "PYDS_WORKFLOW_TIME_ZONE": "Asia/Shanghai",
        })


class SoloSettings:
    HOME_DIR = Path.home()
    SHARED_DIR = Path(os.getenv("SOLO_SHARED_DIR", "/shared"))
    TEMPLATE_REPOSITORY = "genesis-quant/solo-algos"
    SCHEME_REPOSITORY = "genesis-quant/solo-algo-scheme"
    GITEE_TOKEN = os.getenv("GITEE_TOKEN", "")
    JUPYTER_URL = os.getenv("JUPYTER_URL", "http://127.0.0.1:8888").rstrip("/")
    JUPYTER_TOKEN = os.getenv("JUPYTER_TOKEN", "")
    WEB_ORIGINS = os.getenv(
        "SOLO_WEB_ORIGINS", "http://127.0.0.1:5174,http://localhost:5174"
    ).split(",")
