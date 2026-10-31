# ReadLess Search Agent

[English](README.md) · 中文文档

ReadLess Search Agent 是一个以**来源证据、可恢复执行和私有知识检索**为核心的研究型 Agent。它不是一次模型调用包装，而是将问题理解、Query Rewrite、检索规划、并行研究、材料阅读、证据整理、答案写作和逐段核验拆成可持久化的 LangGraph 工作流。

当前仓库提供独立后端、命令行工具和 Docker Compose 部署，不包含 ReadLess 网站前端。默认通过 OpenRouter 调用 `openai/gpt-5.6-luna`，在线搜索使用本地运行的 SearxNG，私有知识向量由本地 Ollama 的 `nomic-embed-text` 生成。

## 主要能力

- 对多轮对话生成结构化摘要，保留用户目标、明确约束和未解问题。
- 使用独立 Query Rewrite 节点生成标准问题、公开搜索词、私有检索词和检索范围。
- 将复杂问题拆成多个 Search Plan，并发执行独立研究子任务。
- 通过 SearxNG 聚合 Bing、Yahoo 的候选结果，再由 Agent 选源、抓取和阅读正文。
- 对 Markdown、TXT 和文本型 PDF 建立只读私有知识索引。
- 将来源正文冻结成快照，保存精确证据区间和引用关系。
- 根据材料覆盖情况动态补搜；证据不足时返回 `partial`，不拼凑完整结论。
- 使用 PostgreSQL Checkpoint 恢复中断任务，通过 Operation Ledger 回放已完成操作。
- 使用 Letta 保存跨任务偏好、研究经验、信源模式和未解问题。
- 输出带引用的 Markdown、结构化 JSON、来源快照和执行统计。

## 技术栈与职责边界

| 组件 | 在系统中的职责 |
| --- | --- |
| Python 3.12 | Agent、API、Worker、检索和证据处理的主要实现语言 |
| LangGraph | 定义有状态主图与并行 Researcher 子图，管理节点路由和恢复入口 |
| FastAPI | 提供研究任务、状态、取消、结果、来源和指标接口 |
| GPT-5.6 Luna / OpenRouter | 执行理解、改写、规划、选源、阅读、写作和核验等模型节点 |
| SearxNG | 在本机部署元搜索服务，向 Bing、Yahoo 查询公开候选结果 |
| PostgreSQL | 保存运行状态、Checkpoint、操作账本、来源快照、证据、引用和报告 |
| Ollama | 在本机加载并运行 Embedding 模型 |
| `nomic-embed-text` | 将私有文档片段和查询转换为 768 维文本向量 |
| SQLite | 持久化私有知识片段、向量、文件版本和 Embedding 缓存 |
| Letta | 管理跨任务长期语义记忆；不负责执行生成式 Agent 循环 |
| Docker Compose | 组织 API、Worker、PostgreSQL、SearxNG、Letta 与 Ollama 服务 |

## 端到端执行流程

```text
用户问题与近期对话
        │
        ▼
对话摘要 → 意图识别 → Query Rewrite
                         ├── 公开查询 → SearxNG → 网页候选
                         └── 私有查询 → SQLite 向量索引 → 本地片段
        │
        ▼
长期记忆召回 → 初步材料调查 → Search Plan
        │
        ▼
并行 Researcher：搜索／读取 → 材料就绪判断 → 定向补搜
        │
        ▼
证据合并 → 缺口分析 → 写作 → 逐段核验 → 必要时编辑 → 最终报告

LangGraph State / Checkpoint ───────── PostgreSQL
跨任务语义记忆 ─────────────────────── Letta
私有知识向量与版本 ─────────────────── SQLite
```

### 1. 对话摘要

系统先解析带角色的近期对话，再生成三个结构化字段：

- `summary`：继续当前任务所需的简要上下文；
- `user_constraints`：用户明确提出的限制；
- `unresolved_questions`：尚未解决的问题。

摘要附带输入哈希和消息数量，并保存在本次运行中。它只用于压缩上下文，不能覆盖原始用户消息，也不能把旧回答自动提升为事实。

### 2. 意图识别与 Query Rewrite

问题理解节点确定研究目标、必需问题、时效要求、搜索语言和来源策略。独立 Query Rewrite 节点随后输出：

- `standalone_query`：消除指代、可独立理解的问题；
- `search_terms`：最多五条公开搜索词；
- `local_query`：面向私有知识库的检索词；
- `scope`：`local`、`web` 或 `hybrid`。

如果范围是 `local`，系统关闭公网搜索和网页抓取。混合检索的公开搜索词在读取私有材料之前固定，避免把私有笔记内容泄露到公开搜索请求中。

### 3. 长期记忆召回

Letta 是跨运行记忆的唯一管理者，保存以下类型的信息：

- 用户明确提供的偏好；
- 可复用的研究经验；
- 可靠信源模式；
- 未解决问题；
- 与历史证据绑定的结论。

默认召回 Top 5，并将结果限制在约 2,000 tokens 内。系统会过滤过期、已被替代、来源失效或数据契约不兼容的记忆。记忆只能帮助规划，不能直接作为最终回答的事实证据；新任务仍需重新取得可引用材料。

### 4. 私有知识库索引

知识目录通过 Docker 以只读方式挂载。索引器执行以下步骤：

1. 遍历 Markdown、UTF-8 文本和文本型 PDF；
2. 拒绝符号链接、隐藏路径和越出知识库根目录的文件；
3. 计算文件 SHA-256，记录当前文件版本；
4. 按约 1,800 字符切块，相邻块重叠约 200 字符；
5. 保留文本行号或 PDF 页码及字符范围；
6. 调用本地 Ollama，以 `nomic-embed-text` 生成 768 维向量；
7. 将文本、向量、版本和进度写入 SQLite；
8. 复用未变化片段的 Embedding，并在文件变化后拒绝旧版本命中。

当前不执行 OCR。扫描 PDF、包含无文本页或触发解析警告的 PDF 会被跳过并记录原因。

### 5. 私有知识检索

`nomic-embed-text` 只负责生成向量，真正的检索逻辑由应用代码执行：

1. 将查询转换成向量；
2. 从 SQLite 读取当前片段；
3. 对所有向量计算余弦相似度；
4. 计算查询词与“文件名＋片段正文”的关键词重合度；
5. 使用 `余弦相似度 + 0.12 × 关键词得分` 排序；
6. 校验源文件仍存在且哈希与索引版本一致；
7. 返回得分超过阈值的前若干片段。

因此当前实现是适合个人知识库的**全量精确扫描＋关键词混合排序**，不是 HNSW、IVF 等近似最近邻检索。语料规模继续扩大时，可以替换为 pgvector、Qdrant、Milvus 或其他 ANN 引擎，而不改变上层证据流程。

### 6. 在线搜索与信源筛选

SearxNG 运行在本机容器中，负责把公开查询发送给配置的 Bing、Yahoo 搜索后端并标准化候选结果。“本地部署”表示元搜索服务、配置和日志由自己控制，不表示互联网搜索在本地完成。

搜索摘要仅用于发现候选，不能作为事实证据。Agent 会进一步：

1. 比较候选标题、摘要、来源角色和用户问题；
2. 优先选择原始论文／官方文档、教学解释和实现材料的互补组合；
3. 抓取公开 HTML 或纯文本正文；
4. 规范化 URL，去除常见追踪参数；
5. 根据 DOI、arXiv 标识、正文哈希等识别同一作品的不同页面；
6. 将真正阅读过的正文冻结为来源快照。

Fetcher 限制重定向、响应字节和正文字符数，遵守 robots 规则，校验公网 IP，不执行网页脚本，也不绕过登录或付费墙。

### 7. Search Plan 与并行研究

Planner 将问题拆成最少且相互独立的研究任务。每个任务包含：研究问题、所属问题维度、优先级、检索词、目标来源类型、已有 URL 和成功条件。

主图最多并发派发三个 Researcher 子图。子图在每轮中选择以下动作之一：

- 阅读已经取得但尚未处理的材料；
- 执行一条新的公开搜索；
- 抓取并阅读候选来源；
- 判断材料是否已经足以回答；
- 针对具体缺口改变搜索策略；
- 达到覆盖、预算或无进展条件后停止。

并行任务完成后统一进入 Join，避免某个任务提前写作而遗漏其他任务的证据。

### 8. 材料阅读与来源身份

Source Reader 读取冻结的原文段落，选择能够解释核心机制、关系、例子和限定条件的连续片段，并标记它们回答了哪些成功条件。

来源身份与内容支持是两件事：官方页面、论文记录或作者原始材料可以建立来源身份，但“来源是官方的”不意味着该页面中的每句话都自动通过。第三方教程可以提供有价值的解释，但不能被标记为另一个产品或论文的原始发布者。

网页正文、私有文档和记忆都视为不可信数据，不能通过文档内文字改变系统提示词、工具权限或证据规则。

### 9. 证据、主张与引用

PostgreSQL 保存不可变的来源快照及其 SHA-256。证据记录包含：

- 来源 ID；
- 原文起止字符位置；
- 精确原文；
- 标准化主张；
- 支持或反驳关系；
- 对应研究条件；
- 来源身份与一手性范围。

最终引用绑定到具体证据和回答段落，而不是简单链接到一个网页。保存报告前会再次检查来源正文、字符区间和引用关系，避免引用偏移或来源被替换。

### 10. 缺口分析与动态补搜

初始研究完成后，系统从全部任务和全部证据评估必需问题是否已经得到解释。材料“提到关键词”不等于已经解释完整；同一论文的摘要页、PDF 页和 HTML 页也不会被当作三份独立证据。

若存在重要缺口，系统最多启动一批补充任务，并保留原成功条件和缺口 ID。补搜必须针对具体缺失内容，不能为了增加来源数量而重复查询。

### 11. 答案生成与核验

Writer 只能使用已经取得的原始片段作为事实基础，并返回结构化段落、列表、表格或代码块。引用由系统根据段落与证据关系生成，模型不手写引用编号。

Report Verifier 再次逐段检查：

- 事实是否被原文支持；
- 推理的前提与逻辑关系是否成立；
- 示例是否清楚标记为示意；
- 日期、身份、实测结果、接口约定等重要主张是否有合适的一手证据；
- 每个必需问题是否真正得到回答。

发现实质问题时，Editor 使用同一批证据修订完整答案，再进行复核。证据不足、预算耗尽或达到时限时，系统返回 `partial` 并保留局限，不自动删除失败段落后冒充完整答案。

### 12. 有状态执行与恢复

LangGraph State 是单次任务的工作记忆，包含理解结果、Query Rewrite、调查材料、长期记忆快照、计划历史、子任务结果、证据 ID、缺口、草稿和预算状态。

PostgreSQL Checkpoint 在节点边界同步保存状态。同一 `run_id` 恢复时继续原图，而不是重新创建任务。模型、搜索和抓取等外部操作先写入 Operation Ledger，再返回图节点；如果服务在“远端成功、Checkpoint 尚未写入”之间中断，恢复时可以回放已保存结果，减少重复调用。

API 还要求 `Idempotency-Key`：同一所有者使用相同 Key 和相同请求会得到原任务；相同 Key 对应不同请求会被拒绝。

## 分层 Memory Engineering

| 层级 | 内容 | 存储位置 | 生命周期 |
| --- | --- | --- | --- |
| 对话上下文 | 最近用户消息、旧回答和来源线索 | API 请求 | 当前请求 |
| 对话摘要 | 目标、限制、修正和未解问题 | PostgreSQL Operation／Checkpoint | 当前运行 |
| 工作记忆 | 计划、进度、已读来源、证据和草稿 | LangGraph State＋PostgreSQL | 当前运行，可恢复 |
| 私有知识 | 文档片段、向量、文件版本和位置 | SQLite | 跨运行，随索引更新 |
| 长期记忆 | 偏好、经验、信源模式、问题和有依据结论 | Letta | 跨运行，可替代或失效 |
| 操作缓存 | 模型、搜索、抓取、阅读的已完成结果 | PostgreSQL Operation Ledger | 当前运行重放 |

这种分层避免把所有信息都塞进一次模型调用，也避免把用户偏好、历史回答、私有笔记和可引用事实混成同一种“记忆”。

## 模型与上下文预算

GPT-5.6 Luna 的服务端理论上下文大于当前应用实际使用量。系统按节点配置更小的输入／输出预算，以控制成本、延迟和失败范围：

| 节点 | 最大输入 | 最大输出 |
| --- | ---: | ---: |
| 对话摘要 | 16K | 2K |
| Query Rewrite | 16K | 1.8K |
| 问题理解 | 12K | 1.2K |
| 搜索规划 | 16K | 2.4K |
| 信源筛选 | 10K | 3K |
| 材料阅读 | 16K | 2.4K |
| 材料就绪判断 | 24K | 3K |
| 答案写作 | 48K | 8K |
| 整篇核验 | 64K | 12K |
| 完整编辑 | 64K | 8K |

每次输入额度包含系统提示词、JSON Schema、用户问题、摘要和材料，不是全部留给正文。单个研究任务默认总预算为 400K tokens，分为研究、收尾和记忆三个额度池；它是多次模型调用的总预算，不是单次上下文窗口。

## 运行限制与停止条件

默认配置包括：

- 最多 3 个并行 Researcher；
- 每个任务最多 4 轮；
- 最多 1 批补充研究；
- 最多 16 次搜索、30 个页面抓取；
- 最多 400K 总 tokens；
- 最长运行 1,200 秒，其中收尾阶段最多 420 秒；
- 模型、搜索、抓取均有独立 HTTP 尝试上限和重试退避。

所有实际额度、停止原因、未知 token、已知费用和记忆状态都会进入运行结果。

## 快速启动

需要 Docker Desktop 或支持 Docker Compose 的 Docker Engine。

```bash
cp .env.example .env
```

编辑 `.env`，填写：

- `OPENROUTER_API_KEY`：OpenRouter API Key；
- `API_BEARER_TOKEN`：本地研究 API 的访问令牌；
- `POSTGRES_PASSWORD`：PostgreSQL 密码；
- `VAULT_PATH`：可选，本地知识目录。

建议用随机值生成后两项，例如：

```bash
openssl rand -hex 32
```

启动服务：

```bash
docker compose up -d --build
docker compose exec embeddings ollama pull nomic-embed-text
docker compose exec api research-agent provision-memory
docker compose --profile indexing run --rm vault-indexer
curl http://127.0.0.1:8000/readyz
```

API 文档位于 <http://127.0.0.1:8000/docs>。

## 发起研究任务

在 API 容器中运行 CLI：

```bash
docker compose exec api research-agent run \
  --query '请解释 Prefix Caching，并标注来源。' \
  --wait --output /tmp/report.md
```

也可以直接调用 API：

```bash
set -a
source .env
set +a

curl -X POST http://127.0.0.1:8000/v1/research \
  -H "Authorization: Bearer $API_BEARER_TOKEN" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{"query":"请介绍 Transformer 的核心机制。","memory_mode":"read_write"}'
```

CLI 支持：

```bash
research-agent status <run-id>
research-agent cancel <run-id>
```

## 建立和更新私有索引

设置 `.env` 中的 `VAULT_PATH` 后运行：

```bash
docker compose --profile indexing run --rm vault-indexer
```

索引器支持增量更新：文件哈希未变化时复用已完成向量；文件变化时只有全部新片段成功后才发布该版本。删除文件后重建索引会移除对应片段。

## 导入显式偏好

系统不会扫描笔记猜测用户偏好。复制示例并填写用户明确提供的信息：

```bash
cp configs/user-preferences.example.json configs/user-preferences.json
docker compose exec api research-agent import-preferences \
  /app/configs/user-preferences.json
```

`configs/user-preferences.json` 已加入 `.gitignore`。

## 本地开发与测试

```bash
uv sync --frozen
uv run pytest tests/unit -q
uv run pytest tests/integration/test_adaptive_workflow.py -q
```

整理发布时已验证：

- 151 项单元测试通过；
- 17 项自适应研究流程集成测试通过；
- Docker 镜像构建通过；
- Docker Compose 配置检查通过。

离线测试使用假的模型、搜索、抓取和记忆适配器，不消耗 API 额度。Live 测试必须显式开启，并指向已经完成的真实任务，避免重复产生费用。

## 数据与隐私边界

- `.env`、私有偏好、知识库、SQLite 索引、数据库 Volume 和真实研究报告不应提交到 Git。
- 私有知识目录以只读方式挂载，索引器不会修改原文件。
- Embedding 在本地 Ollama 中生成；被召回的私有片段仍会发送给 OpenRouter，用于答案写作和核验。
- 明确的私有资料问题关闭公网搜索；混合问题只允许使用读取私有资料前生成的公开搜索词。
- Compose 端口默认只绑定 `127.0.0.1`。
- 当前实现是本机单用户系统，不具备公网多租户部署所需的登录、租户隔离、TLS、用户额度和生产级密钥管理。
- Letta 版本为了当前兼容性固定为 `0.16.8`；生产部署前应评估迁移到维护中的版本。

更多安全说明见 [SECURITY.md](SECURITY.md)，架构摘要见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 许可证

仓库目前未附带开源许可证。除非仓库所有者后续加入许可证文件，否则默认不授予复制、修改或再分发权利。
