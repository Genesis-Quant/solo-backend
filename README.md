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

任务日志分页和下载仅通过 DolphinScheduler HTTP API 获取。Backend 不挂载或读取调度器日志文件；调度器无法获取日志时，接口返回 502 和调度器错误，不伪装成 Backend 断连或空日志。

```powershell
uv run pytest
```

Docker Compose 由上一级 Solo 工作区维护。`alembic upgrade head` 在 `solo` 中维护 `projects` 表，保存项目类型、名称、描述、Scheme Tag/commit、Algo 模板 Tag/commit、删除标记与创建/更新时间。Algo 版本复用 `template_tag`、`template_commit` 字段；Scheme 使用 `scheme_version`、`scheme_commit` 字段，旧项目未记录时为空。`solo_ds` 留给 DolphinScheduler，本迁移不修改调度器业务表。

项目接口：

| 接口 | 用途 |
| --- | --- |
| `GET /api/v1/templates/scheme-versions` | Scheme Tag 列表；`refresh=true` 强制刷新 |
| `GET /api/v1/templates/versions?kind=model&scheme_version=v1.2.0` | 指定类型下兼容所选 Scheme 的 Algo Tag 列表 |
| `GET/POST /api/v1/projects` | 项目列表 / 按模板创建项目及 uv 环境 |
| `GET/PATCH/DELETE /api/v1/projects/{id}` | 项目详情 / 修改 / 删除隐藏 |
| `GET /api/v1/projects/{id}/jupyter` | 跳转项目 Notebook |

创建时先选择 Scheme 版本，再选择主版本、次版本均一致的 Algo 模板，补丁版本可不同。模板必须声明唯一的 Scheme 依赖，覆盖整个补丁系列：`>=X.Y.0, <X.(Y+1).0`（接受 `>=1.2,<1.3` 等 PEP 440 等价简写）。例如 1.2.x 模板声明 `scheme>=1.2.0,<1.3.0`，允许 Scheme 1.2.0、1.2.7，拒绝 1.1.x、1.3.x；不允许把下界提高到某个补丁版本。URL/Git 依赖、marker、extras、多条 Scheme 依赖、精确版本、跨主次版本、缺少边界及 `!=`/`~=` 等复杂声明均不符合发布契约。创建请求示例：`{"name":"动量策略","description":"","kind":"model","scheme_version":"v1.2.0","algo_version":"v1.2.0"}`。项目 `dependencies` 保留模板范围，仅在兼容校验通过后将 `tool.uv.sources.scheme` 固定为选定 commit，`uv.lock` 精确锁定实际环境；正式执行仍校验已安装版本、wheel 内容和锁文件，不自动修改已有研究项目或环境。策略组装也要求各成果的实际 Scheme 主次版本一致，继续使用 Model 成果冻结的具体 Scheme 版本。前端报告独立按 Scheme 主版本渲染。

Scheme 版本列表同时返回 Tag、commit 和 `pyproject.toml` 中的实际 `version`，前端根据实际版本判断是否支持。

## 版本退役与准入

政策唯一来源为 `version-policy.json`。Scheme 0.1.0、1.0.0、1.0.1、1.1.0 以及 Algo 模板 0.1.0、1.0.0、1.0.1 已退役，最低活跃版本为 1.2.0。Tag/commit 是发布身份；候选成果的 `packageVersion` 由实际锁定的 Scheme 主次版本和项目内提交编号生成，不把成果编号当模板 Tag。目录、历史记录、冻结资产和报告不迁移或删除。

| 接口 | 用途 |
| --- | --- |
| `GET /api/v1/version-policy` | 查询当前政策 |
| `POST /api/v1/version-policy/projects/{id}/check` | 校验项目发布来源与本地锁定的实际 Scheme |
| `POST /api/v1/version-policy/tasks/{id}/check` | 按数据库任务 UUID 校验 kind、输入及锁文件 SHA256、冻结来源 |

项目和成果读取返回 `retired`、`retiredReason`，列表保留历史项目；退役不是 `archived`。成果状态取自该次实际 Scheme，不以当前项目创建 Tag 或新版环境替代。新建、重建环境的改名、新成果保存/提交、上游安装和策略组装拒绝退役来源，返回 422，`detail` 含 `code` 和 `reason`。描述编辑和历史报告查看不要求活跃版本。任务检查只读取数据库身份对应的 `runs/{id}`，不接受任意文件路径。提交前将验证过的 `input_sha256`、`lock_sha256` 保存到任务记录，Worker 的请求哈希必须同时匹配数据库和实际文件；任务必须处于已提交的 `queued`/`running` 状态。迁移 `0005_task_input_hashes` 仅增加四个 nullable 列，不回填旧任务；未记录接受哈希的历史任务不能重新执行，报告查看不受影响。

没有可信调度实例绑定时，不根据 `created_at`、`queued` 或相同文件哈希放行退役任务；本次退役无待处理任务，所有新的旧任务启动、重试和重放均拒绝。Runtime 调用准入发生在 `uv sync` 之前，Backend 不可用时不执行任务。已开始的进程不会因新的政策检查被主动停止。

`GET /api/v1/reports/{path}` 读取 `/shared/runs/{path}`。只开放成功运行的 `run.json` 和同目录中清单声明的 Parquet，不开放其他任务文件；Backend 不解释 Scheme 业务报告结构。前端的 `reportPath` 是相对于 `/shared/runs` 的输出目录，例如 `123/output`。

目录使用 `/shared/projects/model/动量策略`，根目录 `.solo` 保存 `project_id`、`name`、`kind`、`scheme_version`、`scheme_commit`、`algo_version`、`algo_commit`。同类同名项目返回 409；环境安装失败返回 502，并回滚目录；删除不删除源码和环境。

Backend 和 Jupyter 需要挂载相同的 `/shared/projects`、`/home/jovyan/.python` 和 `/home/jovyan/.jupyter/kernels`，并将 `HOME` 设置为 `/home/jovyan`。Backend 使用 Git、uv 创建 Python 3.12 环境；Jupyter 设置 `JUPYTER_PATH=/home/jovyan/.jupyter` 发现项目 Kernel。模板仓库固定为 `genesis-quant/solo-algos`；可通过 `GITEE_TOKEN` 增加 Gitee API 额度。`JUPYTER_URL` 配置浏览器入口，`JUPYTER_TOKEN` 用于跳转登录，不写入项目文件。
