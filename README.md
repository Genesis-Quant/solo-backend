# Solo Backend

沿用 Arena 的 `main.py`、`config.py`、`core/apps`、`core/database`、`core/scheduler`、`core/utils` 和 `alembic` 结构，使用 Python 3.12、uv、FastAPI、SQLAlchemy、psycopg、Alembic。

将 `.env.example` 复制为 `.env`，填写现有 Docker PostgreSQL 的账号和密码。应用数据库为 `solo`，调度数据库为 `solo_ds`，两个数据库需提前创建。放在 Solo 工作区内时，也会读取上一级目录的 `.env`，本目录配置优先。

```powershell
Copy-Item .env.example .env
uv sync
uv run alembic upgrade head
uv run uvicorn main:app --host 127.0.0.1 --port 8010 --reload
```

健康检查：`/health`、`/api/v1/health`；接口文档：`/docs`。

```powershell
uv run pytest
```

Docker Compose 由上一级 Solo 工作区维护。`alembic upgrade head` 在 `solo` 中维护 `projects` 表，保存项目类型、名称、描述、Scheme Tag/commit、Algo 模板 Tag/commit、删除标记与创建/更新时间。Algo 版本复用 `template_tag`、`template_commit` 字段；Scheme 使用 `scheme_version`、`scheme_commit` 字段，旧项目未记录时为空。`solo_ds` 留给 DolphinScheduler，本迁移不修改调度器业务表。

项目接口：

| 接口 | 用途 |
| --- | --- |
| `GET /api/v1/templates/scheme-versions` | Scheme Tag 列表；`refresh=true` 强制刷新 |
| `GET /api/v1/templates/versions?kind=model&scheme_version=v0.1.0` | 指定类型下兼容所选 Scheme 的 Algo Tag 列表 |
| `GET/POST /api/v1/projects` | 项目列表 / 按模板创建项目及 uv 环境 |
| `GET/PATCH/DELETE /api/v1/projects/{id}` | 项目详情 / 修改 / 删除隐藏 |
| `GET /api/v1/projects/{id}/jupyter` | 跳转项目 Notebook |

创建时先选择 Scheme 版本，再选择同主版本的 Algo。模板必须声明整个大版本范围，例如 `scheme>=1.0.0,<2.0.0`；精确版本、缩窄范围和直接 Git 依赖不符合发布契约。创建请求示例：`{"name":"动量策略","description":"","kind":"model","scheme_version":"v1.0.0","algo_version":"v1.2.0"}`。项目 `dependencies` 保留大版本范围，`tool.uv.sources.scheme` 固定选定 commit，`uv.lock` 锁定实际环境；构建 wheel 不携带项目的 uv source 覆盖。

Scheme 版本列表同时返回 Tag、commit 和 `pyproject.toml` 中的实际 `version`，前端根据实际版本判断是否支持。

`GET /api/v1/reports/{path}` 读取 `/shared/runs/{path}`。只开放成功运行的 `run.json` 和同目录中清单声明的 Parquet，不开放其他任务文件；Backend 不解释 Scheme 业务报告结构。前端的 `reportPath` 是相对于 `/shared/runs` 的输出目录，例如 `123/output`。

目录使用 `/shared/projects/model/动量策略`，根目录 `.solo` 保存 `project_id`、`name`、`kind`、`scheme_version`、`scheme_commit`、`algo_version`、`algo_commit`。同类同名项目返回 409；环境安装失败返回 502，并回滚目录；删除不删除源码和环境。

Backend 和 Jupyter 需要挂载相同的 `/shared/projects`、`/home/jovyan/.python` 和 `/home/jovyan/.jupyter/kernels`，并将 `HOME` 设置为 `/home/jovyan`。Backend 使用 Git、uv 创建 Python 3.12 环境；Jupyter 设置 `JUPYTER_PATH=/home/jovyan/.jupyter` 发现项目 Kernel。模板仓库固定为 `genesis-quant/solo-algos`；可通过 `GITEE_TOKEN` 增加 Gitee API 额度。`JUPYTER_URL` 配置浏览器入口，`JUPYTER_TOKEN` 用于跳转登录，不写入项目文件。
