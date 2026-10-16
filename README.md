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

## 0.1.1 启动就绪与传统类型别名

同步工作流前等待 Python Gateway，总期限为 60 秒并覆盖 DNS 解析和多地址连接；只重试连接探测，不重试用户创建、项目创建或工作流提交。无法中断的系统 DNS 由守护线程收尾，迟到成功不放行同步，迟到连接仍关闭。根目录 Compose 同时检查调度器 HTTP 和 Gateway 监听状态，避免 Backend 早于 Gateway 启动。

API 类型别名改用 `TypeAlias`，依赖注入、请求校验和响应数据不变；OpenAPI 中 `ProjectKind` 枚举由命名引用改为内联枚举。

任务日志分页和下载仅通过 DolphinScheduler HTTP API 获取。Backend 不挂载或读取调度器日志文件；调度器无法获取日志时，接口返回 502 和调度器错误，不伪装成 Backend 断连或空日志。

```powershell
uv run pytest
```

Docker Compose 由上一级 Solo 工作区维护。`alembic upgrade head` 在 `solo` 中维护项目、研究任务、独立产物和短期清理记录。项目保存明确的 `package_name`、类型、名称、描述及 Scheme/Algo 创建来源；研究任务属于项目，项目删除时级联删除。`research_artifacts` 用 UUID 记录成果身份与来源，`storage_directory` 定位独立文件目录，不以源项目外键决定存续。`project_deletions` 只用于失败清理的 UUID 重试，完成后删除，不是永久归档表。`solo_ds` 留给 DolphinScheduler；调度实例清理由正式 HTTP API 完成，迁移不直接修改调度器业务表。

项目接口：

| 接口 | 用途 |
| --- | --- |
| `GET /api/v1/templates/scheme-versions` | Scheme Tag 列表；`refresh=true` 强制刷新 |
| `GET /api/v1/templates/versions?kind=model&scheme_version=v1.2.0` | 指定类型下兼容所选 Scheme 的 Algo Tag 列表 |
| `GET/POST /api/v1/projects` | 项目列表 / 按模板创建项目及 uv 环境 |
| `GET/PATCH/DELETE /api/v1/projects/{id}` | 项目详情 / 修改 / 彻底删除所属记录和资源 |
| `GET /api/v1/projects/{id}/jupyter` | 跳转项目 Notebook |
| `POST /api/v1/projects/{id}/upgrade` | 显式升级当前 Scheme/Algo 环境契约，返回完整项目详情 |

创建时先选择 Scheme 版本，再选择主版本、次版本均一致的正式 Algo 模板，补丁版本可不同。模板必须声明唯一的 Scheme 依赖，依赖范围包含 `X.Y.0`、不包含 `X.(Y+1).0`，且所选 Scheme 满足依赖；按 PEP 440 语义判断，不限制边界数量、`~=`、extras 或 marker 等声明形式。创建请求示例：`{"name":"动量策略","description":"","kind":"model","scheme_version":"v1.2.0","algo_version":"v1.2.0"}`。项目 `dependencies` 保留模板范围，仅在兼容校验通过后将 `tool.uv.sources.scheme` 固定为选定 commit，`uv.lock` 锁定实际环境，不自动修改已有研究项目。策略组装也要求各成果的实际 Scheme 主次版本一致，继续使用 Model 成果冻结的具体 Scheme 版本。前端报告独立按 Scheme 主版本渲染。

Scheme 和 Algo 模板版本列表均返回 Tag、commit 和 `pyproject.toml` 中的实际 `version`，前端根据实际版本判断是否支持。自定义 Tag 不必等于包版本；退役先匹配明确的 Tag/commit 身份，再以对应 commit 的实际版本校验。新项目保存验证过的 `template_package_version`，迁移 `0006_project_template_version` 仅增加该可空列，不回填历史项目。

创建项目时保留所选 Tag 中 `research.ipynb` 的所有单元、参数、运行/报告代码和元数据，只将当前项目的包导入替换为独立模块名；不清空或重建单元列表，不执行 Notebook。Kernel 仍单独注册到该项目的冻结环境。Notebook 交互运行与“保存版本”的正式调度任务是两条独立入口。

## 项目环境升级

`POST /api/v1/projects/{id}/upgrade` 请求为 `{"scheme_version":"v1.6.0","algo_version":"v1.6.0"}`，成功返回与项目详情相同的完整 `ProjectRead`。仅用户显式调用时变更环境，允许降级到仍活跃且兼容的目标，也允许退役项目离开原发布；目标仍遵守发布/退役政策、Scheme/Algo 主次版本一致及模板依赖规则。目标与当前来源相同直接返回，不重新安装。

升级只更新当前 `.venv`、`pyproject.toml` / `uv.lock`、`.solo` 和项目当前 Scheme/Algo 数据库字段。Algo 是模板，不是额外安装的运行时包：这里只采用目标模板的环境依赖契约，不解包或覆盖模板源码，不修改 `src`、Notebook、用户参数代码、Kernel 注册或既有研究任务/产物/报告。用户仍需自行审查代码与新版 Scheme API 的兼容性；升级成功不表示代码已迁移。

`.solo` 保留自定义字段；第一次升级新增 `source_template`，保存原 Algo tag、commit 和已记录的实际 package version，后续升级及改名保留它。原有 `scheme_version` / `algo_version` / commit 等字段表示当前生效环境，与数据库当前来源同步；既存产物和历史 Run 的冻结来源不改写。

升级与修改/删除共享项目生命周期锁，不向 Jupyter 申请升级操作锁。真正升级前查询 Jupyter 项目 busy 状态，依赖安装或保存版本期间返回 409；升级期间项目准入接口也返回 409。目标校验通过且确需变更时才关闭本项目闲置会话/Kernel；忙碌或共享 Kernel、构建/排队/运行中的研究任务返回 409，不重启 Jupyter 服务。直接在项目目录运行 `uv sync`，允许重新锁定依赖；不要求旧 `.venv` 存在，也不扫描、复制用户源码或符号链接，不额外禁止绝对本地依赖路径，依赖冲突由 uv 报告。

安装前仅在内存保存原配置、锁文件和 `.solo`。uv 安装失败时写回原文件，再运行一次 `uv sync` 恢复旧环境，返回 502 和已脱敏的 uv 输出；数据库提交失败也回写并同步恢复后抛错。不创建升级暂存目录、恢复 journal 或文件摘要，不提供跨进程退出的自动恢复。

## 版本退役与准入

政策唯一来源为 `version-policy.json`。Scheme 0.1.0、1.0.0、1.0.1、1.1.0 以及 Algo 模板 0.1.0、1.0.0、1.0.1 已退役，最低活跃版本为 1.2.0。Tag/commit 是发布身份；候选成果的 `packageVersion` 由实际锁定的 Scheme 主次版本和项目内提交编号生成，不把成果编号当模板 Tag。退役本身不迁移或删除目录、历史记录、冻结资产和报告；用户明确删除项目时，仍按项目生命周期清理所属资源。

| 接口 | 用途 |
| --- | --- |
| `GET /api/v1/version-policy` | 查询当前政策 |
| `POST /api/v1/version-policy/projects/{id}/check` | 校验项目发布来源与本地锁定的实际 Scheme |
| `POST /api/v1/version-policy/tasks/{id}/check` | 请求仅含 `kind`，按数据库任务 UUID 校验类型、提交状态与退役政策 |

项目和成果读取返回 `retired`、`retiredReason`，列表保留历史项目；退役不是 `archived`。成果状态取自该次实际 Scheme，不以当前项目创建 Tag 或新版环境替代。新建、重建环境的改名、新成果保存/提交、上游安装和策略组装拒绝退役来源，返回 422，`detail` 含 `code` 和 `reason`。描述编辑、构建失败回调和历史报告查看不要求活跃版本；失败回调只终止本次构建，不允许提交任务。改名按原锁文件冻结重建环境，验证实际来源与锁文件未变化后再提交；失败恢复原目录、环境、配置和锁文件。任务准入只读取数据库，不读取任务文件；请求为 `{"kind":"factor|model|optimize|control|execution|strategy"}`，额外字段忽略。任务须处于 `queued`/`running`；允许返回 `200 {"allowed":true}`，拒绝返回 `422 {"detail":{"code":"...","reason":"..."}}`，任务不存在返回 404。退役政策按文件修改时间缓存，变更后重新加载，不进行内容摘要比对。

首次提交和 `submit_failed` 重试在排队前统一检查已接受成果的来源与版本系列；重试复用原快照，不重新捕获或复制成果。确定性拒绝返回 422，并记录 `failed`、原因和结束时间，不调用调度器。不根据 `created_at`、`queued` 或调度实例放行退役任务；本次退役无待处理任务，所有新的旧任务启动、重试和重放均拒绝。Runtime 调用准入发生在 `uv sync` 之前，Backend 不可用时不执行任务。已开始的进程不会因新的政策检查被主动停止。

`GET /api/v1/reports/{path}` 读取 `/shared/runs/{path}`。只开放成功运行的 `run.json` 和同目录中清单声明的 Parquet，不开放其他任务文件；Backend 不解释 Scheme 业务报告结构。前端的 `reportPath` 是相对于 `/shared/runs` 的输出目录，例如 `123/output`。

目录使用 `/shared/projects/model/动量策略`，根目录 `.solo` 保存 `project_id`、`package_name`、`name`、`kind`、Scheme 和 Algo 创建来源。同类活动项目名称唯一；彻底删除后允许用相同名称创建新 UUID 的项目。`DELETE /projects/{id}` 默认 `delete_files=true`：删除项目及研究版本记录、所属 `runs/{version UUID}`（包含报告、冻结源与虚拟环境）、专属 Kernel 注册和准确匹配的 DolphinScheduler 实例，回收没有引用的未发布产物。已发布 wheel 和独立下游任务的冻结副本不受影响。`delete_files=false` 只保留 Jupyter 工作区，仍删除项目记录及所属任务；保留的物理目录会阻止同名覆盖。正在构建/运行或读取项目源码时返回 409，不能删掉使用中的资源；空闲的专属会话/Kernel 可以正常关闭。清理失败返回 `project_cleanup_incomplete`，可用原项目 UUID 重试，绝不访问后来创建的同名项目。

Backend 和 Jupyter 需要挂载相同的 `/shared/projects`、`/shared/runs`、`/home/jovyan/.python` 和 `/home/jovyan/.jupyter/kernels`，并将 `HOME` 设置为 `/home/jovyan`。独立产物卷只挂载到 Backend 的 `/shared/artifacts`；Jupyter 通过产物 API 下载 wheel，不直接访问产物库或源项目目录。Backend 使用 Git、uv 创建 Python 3.12 环境；Jupyter 设置 `JUPYTER_PATH=/home/jovyan/.jupyter` 发现项目 Kernel。模板仓库固定为 `genesis-quant/solo-algos`；可通过 `GITEE_TOKEN` 增加 Gitee API 额度，API 配额不足时自动经 Git 协议读取同一 Tag 及其提交中的版本文件。`JUPYTER_URL` 配置浏览器入口，`JUPYTER_API_URL` 配置服务间地址，`JUPYTER_TOKEN` 用于认证，不写入项目文件。

## 独立产物与冻结任务

| 接口 | 用途 |
| --- | --- |
| `POST /api/v1/projects/{id}/versions/{version_id}/publish` | 发布成功研究版本及实际依赖闭包；同一成果重复发布幂等 |
| `GET /api/v1/artifacts?published=true` | 独立发布目录；源项目删除后仍可访问 |
| `GET /api/v1/artifacts/{id}` | 按成果 UUID 查询身份、来源和依赖 |
| `GET /api/v1/artifacts/{id}/wheel` | 直接下载 wheel，不计算文件摘要 |
| `DELETE /api/v1/artifacts/{id}` | 永久删除选定已发布成果；有实际引用时返回 409 |
| `POST /api/v1/artifacts/register` | `{"project_id":"生产方 UUID","wheel":"共享目录内路径","entry":"可选入口"}` |
| `POST /api/v1/projects/{id}/dependencies` | `{"artifacts":["成果 UUID"]}`，记录项目当前接受依赖 |

成果以 UUID 为身份，每次注册新建记录，未发布成果允许同包同版本；已发布成果的 package/version 唯一。包名、版本及元数据入口从 wheel 的 METADATA 读取，入口只校验格式，不要求存在同名 `.py` 文件。普通本地/Git 库也可用当前项目的 UUID 注册为依赖成果。注册 wheel 可位于共享工作区内任意位置，来源及退役判定仍按 `project_id` 指定的生产方项目；注册不等于发布。

新成果位于 `/shared/artifacts/{id}`。发布复制实际本地依赖闭包及参数检查环境，不复制虚拟环境，不改写原任务。清单保留 UUID、组件和接受依赖，不含文件摘要或数据库封存锚点。派生环境只重定位实际依赖来源，保留版本与索引配置，未被锁文件选中的本地 source 不阻止发布。已有发布目录不覆盖，也不按内容比对恢复。

成果删除保护项目当前接受依赖、所有策略及其他成果依赖闭包中的 UUID 引用，返回 409 和引用列表；不按 package 名称、源项目或文件内容推断引用。删除只操作选定 UUID 对应的存储目录，不删除研究任务、报告和任务自己的冻结副本。文件先按 UUID 暂存，数据库提交后清理；清理失败可用原 UUID 重试，不能覆盖或删除后来注册的成果。`gc_unreferenced` 保留原名称和调用方式，只回收没有引用的未发布成果。安装事务、回执、TTL、续期及 complete/abort 接口均已删除。

正式 `input.json` 的组件字段为 `package`、`version`、`wheel`、`entry`，因子另有 `params`；`run.json` 保留 protocol、status、reports 等非哈希字段。版本提交及策略组装不计算或保存文件摘要，数据库 `accepted_artifacts` 仅保存成果 UUID 与来源快照，供发布、依赖引用与退役判定使用。策略可按研究版本 UUID 或 `{"artifacts":{"model":"成果 UUID"}}` 选择组件；同一请求每个环境只同步一次、组件只校验一次，参数额外字段由各 Form 的 Pydantic 配置处理。调度器暂时不可用保留任务状态；本地输入、锁文件或成功报告缺失、损坏则标记失败并说明原因。

迁移 `0009_uuid_artifacts` 将旧 `sha256` 字符串原样回填 `storage_directory`，随后删除成果及任务哈希列和安装事务表；不读取、重命名任何成果文件目录。旧成果仍使用原目录名，新成果目录使用 UUID。该迁移不支持降级，`downgrade` 明确报错，避免丢失存储目录映射。

成果详情返回 `storageDirectory`。Jupyter 读取旧 `.solo-wheels` 路径时，使用所属项目 `artifactDependencies` 中已接受的 UUID 查询成果，再核对存储目录、包名和文件名；不根据同包同版本猜测成果，不计算文件摘要，不迁移或复制原 wheel。缺少对应记录或映射不一致时明确拒绝安装。
