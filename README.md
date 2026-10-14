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

Docker Compose 由上一级 Solo 工作区维护。`alembic upgrade head` 在 `solo` 中维护项目、研究任务、独立产物和短期清理记录。项目保存明确的 `package_name`、类型、名称、描述及 Scheme/Algo 创建来源；研究任务属于项目，项目删除时级联删除。`research_artifacts` 按准确 wheel 的 SHA256 记录不可变身份与来源，不以源项目外键决定存续。`project_deletions` 只用于失败清理的 UUID 重试，完成后删除，不是永久归档表。`solo_ds` 留给 DolphinScheduler；调度实例清理由正式 HTTP API 完成，迁移不直接修改调度器业务表。

项目接口：

| 接口 | 用途 |
| --- | --- |
| `GET /api/v1/templates/scheme-versions` | Scheme Tag 列表；`refresh=true` 强制刷新 |
| `GET /api/v1/templates/versions?kind=model&scheme_version=v1.2.0` | 指定类型下兼容所选 Scheme 的 Algo Tag 列表 |
| `GET/POST /api/v1/projects` | 项目列表 / 按模板创建项目及 uv 环境 |
| `GET/PATCH/DELETE /api/v1/projects/{id}` | 项目详情 / 修改 / 彻底删除所属记录和资源 |
| `GET /api/v1/projects/{id}/jupyter` | 跳转项目 Notebook |

创建时先选择 Scheme 版本，再选择主版本、次版本均一致的 Algo 模板，补丁版本可不同。模板必须声明唯一的 Scheme 依赖，覆盖整个补丁系列：`>=X.Y.0, <X.(Y+1).0`（接受 `>=1.2,<1.3` 等 PEP 440 等价简写）。例如 1.2.x 模板声明 `scheme>=1.2.0,<1.3.0`，允许 Scheme 1.2.0、1.2.7，拒绝 1.1.x、1.3.x；不允许把下界提高到某个补丁版本。URL/Git 依赖、marker、extras、多条 Scheme 依赖、精确版本、跨主次版本、缺少边界及 `!=`/`~=` 等复杂声明均不符合发布契约。创建请求示例：`{"name":"动量策略","description":"","kind":"model","scheme_version":"v1.2.0","algo_version":"v1.2.0"}`。项目 `dependencies` 保留模板范围，仅在兼容校验通过后将 `tool.uv.sources.scheme` 固定为选定 commit，`uv.lock` 精确锁定实际环境；正式执行仍校验已安装版本、wheel 内容和锁文件，不自动修改已有研究项目或环境。策略组装也要求各成果的实际 Scheme 主次版本一致，继续使用 Model 成果冻结的具体 Scheme 版本。前端报告独立按 Scheme 主版本渲染。

Scheme 和 Algo 模板版本列表均返回 Tag、commit 和 `pyproject.toml` 中的实际 `version`，前端根据实际版本判断是否支持。自定义 Tag 不必等于包版本；退役先匹配明确的 Tag/commit 身份，再以对应 commit 的实际版本校验。新项目保存验证过的 `template_package_version`，迁移 `0006_project_template_version` 仅增加该可空列，不回填历史项目。

创建项目时保留所选 Tag 中 `research.ipynb` 的所有单元、参数、运行/报告代码和元数据，只将当前项目的包导入替换为独立模块名；不清空或重建单元列表，不执行 Notebook。Kernel 仍单独注册到该项目的冻结环境。Notebook 交互运行与“保存版本”的正式调度任务是两条独立入口。

## 版本退役与准入

政策唯一来源为 `version-policy.json`。Scheme 0.1.0、1.0.0、1.0.1、1.1.0 以及 Algo 模板 0.1.0、1.0.0、1.0.1 已退役，最低活跃版本为 1.2.0。Tag/commit 是发布身份；候选成果的 `packageVersion` 由实际锁定的 Scheme 主次版本和项目内提交编号生成，不把成果编号当模板 Tag。退役本身不迁移或删除目录、历史记录、冻结资产和报告；用户明确删除项目时，仍按项目生命周期清理所属资源。

| 接口 | 用途 |
| --- | --- |
| `GET /api/v1/version-policy` | 查询当前政策 |
| `POST /api/v1/version-policy/projects/{id}/check` | 校验项目发布来源与本地锁定的实际 Scheme |
| `POST /api/v1/version-policy/tasks/{id}/check` | 按数据库任务 UUID 校验 kind、输入及锁文件 SHA256、冻结来源 |

项目和成果读取返回 `retired`、`retiredReason`，列表保留历史项目；退役不是 `archived`。成果状态取自该次实际 Scheme，不以当前项目创建 Tag 或新版环境替代。新建、重建环境的改名、新成果保存/提交、上游安装和策略组装拒绝退役来源，返回 422，`detail` 含 `code` 和 `reason`。描述编辑、构建失败回调和历史报告查看不要求活跃版本；失败回调只终止本次构建，不允许提交任务。改名按原锁文件冻结重建环境，验证实际来源与锁文件未变化后再提交；失败恢复原目录、环境、配置和锁文件。任务检查只读取数据库身份对应的 `runs/{id}`，不接受任意文件路径。提交前将验证过的 `input_sha256`、`lock_sha256` 保存到任务记录，Worker 的请求哈希必须同时匹配数据库和实际文件；任务必须处于已提交的 `queued`/`running` 状态。迁移 `0005_task_input_hashes` 仅增加四个 nullable 列，不回填旧任务；未记录接受哈希的历史任务不能重新执行，报告查看不受影响。

没有可信调度实例绑定时，不根据 `created_at`、`queued` 或相同文件哈希放行退役任务；本次退役无待处理任务，所有新的旧任务启动、重试和重放均拒绝。Runtime 调用准入发生在 `uv sync` 之前，Backend 不可用时不执行任务。已开始的进程不会因新的政策检查被主动停止。

`GET /api/v1/reports/{path}` 读取 `/shared/runs/{path}`。只开放成功运行的 `run.json` 和同目录中清单声明的 Parquet，不开放其他任务文件；Backend 不解释 Scheme 业务报告结构。前端的 `reportPath` 是相对于 `/shared/runs` 的输出目录，例如 `123/output`。

目录使用 `/shared/projects/model/动量策略`，根目录 `.solo` 保存 `project_id`、`package_name`、`name`、`kind`、Scheme 和 Algo 创建来源。同类活动项目名称唯一；彻底删除后允许用相同名称创建新 UUID 的项目。`DELETE /projects/{id}` 默认 `delete_files=true`：删除项目及研究版本记录、所属 `runs/{version UUID}`（包含报告、冻结源与虚拟环境）、专属 Kernel 注册和准确匹配的 DolphinScheduler 实例，回收没有引用的未发布产物。已发布 wheel 和独立下游任务的冻结副本不受影响。`delete_files=false` 只保留 Jupyter 工作区，仍删除项目记录及所属任务；保留的物理目录会阻止同名覆盖。正在构建/运行或读取项目源码时返回 409，不能删掉使用中的资源；空闲的专属会话/Kernel 可以正常关闭。清理失败返回 `project_cleanup_incomplete`，可用原项目 UUID 重试，绝不访问后来创建的同名项目。

Backend 和 Jupyter 需要挂载相同的 `/shared/projects`、`/shared/runs`、`/home/jovyan/.python` 和 `/home/jovyan/.jupyter/kernels`，并将 `HOME` 设置为 `/home/jovyan`。独立产物卷只挂载到 Backend 的 `/shared/artifacts`；Jupyter 通过产物 API 下载并验证 wheel，不直接访问产物库或源项目目录。Backend 使用 Git、uv 创建 Python 3.12 环境；Jupyter 设置 `JUPYTER_PATH=/home/jovyan/.jupyter` 发现项目 Kernel。模板仓库固定为 `genesis-quant/solo-algos`；可通过 `GITEE_TOKEN` 增加 Gitee API 额度，API 配额不足时自动经 Git 协议读取同一 Tag 及其提交中的版本文件。`JUPYTER_URL` 配置浏览器入口，`JUPYTER_API_URL` 配置服务间地址，`JUPYTER_TOKEN` 用于认证，不写入项目文件。

## 独立产物与冻结任务

| 接口 | 用途 |
| --- | --- |
| `POST /api/v1/projects/{id}/versions/{version_id}/publish` | 明确发布成功研究版本的准确 wheel 与依赖闭包；同一内容重复发布幂等 |
| `GET /api/v1/artifacts?published=true` | 独立发布目录；源项目删除后仍可访问 |
| `GET /api/v1/artifacts/{id}/wheel` | 下载校验 SHA256 后的原始 wheel |
| `DELETE /api/v1/artifacts/{id}` | 永久删除选定成果登记及其独立 wheel/发布环境；成功返回 204 |
| `POST /api/v1/artifacts/register` | 接受已验证源项目构建的准确本地 wheel；注册不等于发布 |
| `POST /api/v1/projects/{id}/dependencies/installations` | 开始或续期消费者安装事务，保护尚未接受的准确产物 |
| `POST /api/v1/projects/{id}/dependencies` | 在环境安装成功后接受实际锁定依赖的不可变快照 |
| `POST /api/v1/projects/{id}/dependencies/installations/{install_id}/complete` | 文件系统提交成功后释放临时安装引用 |
| `POST /api/v1/projects/{id}/dependencies/installations/{install_id}/abort` | 文件系统回滚后恢复原接受快照并释放本次引用 |

研究产物以 package/version/SHA256、入口、Scheme/模板来源和依赖快照绑定，不回查源项目是否仍存在。发布时复制独立依赖闭包和用于参数检查的派生环境到 `/shared/artifacts/{sha256}`，不复制虚拟环境、不改写原任务输入或锁文件。发布清单的原始字节 SHA256 在同一发布事务中封存到数据库；执行前先核验这个不可变锚点，再校验必需文件、wheel 和来源快照，不能通过同时改写文件和清单绕过校验。派生环境仅重定位本地文件，保留准确版本和镜像/私有索引配置。策略可用 `{"artifacts":{"model":"artifact UUID"}}` 选择发布产物；选择和安装以该成果封存的准确锁闭包为准，不再次扩大到上游未选中的源码开发依赖。仍存在的研究版本 UUID 仅作为兼容适配入口，接受后任务就独立绑定自己的产物快照。发布提交失败时保留可验证的完整待发布 bundle；按原研究版本重试会重新核对接受快照和原始文件后采用，不修改任何字节。提交已成功但确认丢失时，以数据库封存真值返回结果；无法确认真值则保留文件并返回 503，不能盲删已发布文件。

成果删除仅回收选定 UUID 的登记记录及 `/shared/artifacts/{sha256}` 中的 wheel 和发布环境，不按来源删除项目、研究任务、报告或任务自己的冻结副本。项目当前安装依赖、未过期的未接受安装事务、尚未确认完成或回滚的已接受安装回执、其他登记产物依赖闭包和未完整接受的策略选择仍需要登记记录，返回 409 并说明引用者；已保存准确快照和输入/锁哈希的任务不因其产物 UUID 而阻止删除。删除后登记查询和下载返回 404，不能再用该登记进行新安装、策略组装或重新发布。文件先在同卷按原 UUID 暂存，数据库删除提交后清理；部分清理失败返回 503 `artifact_cleanup_incomplete`（含 `deleted=true` 与原 `id`），重试只处理原暂存目录，不访问后来新登记的同 SHA 目录。

提交前将 `accepted_artifacts` 与准确输入/锁文件哈希一起持久化。运行准入验证本任务的快照、原始字节和退役政策，不读取源项目、源研究任务或已删除的产物登记。Worker 在安装前、安装后和执行后验证所接受的本地 wheel 字节。迁移 `0007_artifact_registry` 仅扩展结构；旧任务快照回填使用显式 `core.apps.artifacts.backfill.backfill_tasks`，保留原输入、锁文件、wheel、报告及 NULL 接受哈希，不能用迁移制造已接受任务。完成回填、备份后，才能通过正式删除接口清理旧归档项目。

迁移 `0008_artifact_install_seal` 增加发布清单锚点和 `artifact_installations` 引用，不为旧发布清单补造信任哈希。安装事务按消费者 UUID 和 `install_id` 隔离，保护原接受快照、暂存产物及接受回执；未接受事务每 5 分钟续期，30 分钟未续期后可回收。已接受回执不能用过期时间推断文件系统完成或回滚，必须保护原、新快照至显式 `complete` 或 `abort`，并在确认前拒绝另一次依赖接受。准确依赖接受只覆盖最终锁定的研究包和本地 wheel，不强行安装不生效的 marker 或源码开发依赖。接受响应丢失时，Jupyter 回滚文件系统后用原事务回执恢复接受状态；成功提交后调用 `complete`，仅清理失败不会回滚已经提交的环境。文件系统结果及原事务 UUID 保存在项目内 `.solo-installation.json`；断网后由后续操作或 Jupyter 的恢复检查重试，完成和回滚接口均验证当前准确锁与 wheel 后释放回执，不按 TTL 丢弃待补偿状态。
