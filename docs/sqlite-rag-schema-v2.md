# SQLite RAG 索引结构 v2

历史结构说明。当前已升级为 [v3：BM25 与向量融合](sqlite-rag-bm25-v3.md)，新字段、分词、排名规则及迁移以该文档为准。

本文同步自 ReadLess 宿主开发记录；独立仓库验证见 [更新记录](UPDATE-2026-11-12.md)。

2026-11-12。实现入口：`services/rag_store.py`，索引与检索调用方：`services/private_rag.py`。

这是本机派生检索库，不是原文或研究报告的唯一存储。文件来源和网站权限继续遵循 `integrations/readless/docs/research-rag-sources.md`，旧研究的冻结证据不改写。

## 表关系

共 4 张普通表、1 张 FTS5 虚表（SQLite 另有 FTS 内部辅助表，不作为业务表管理）：

```text
files.path ← chunks.path
chunks.embedding_id → embedding_cache.id
chunks.rowid ↔ chunks_fts.rowid
metadata：整个索引的版本、进度和报告
```

向量统一存入 `embedding_cache`，由片段外键引用，避免额外的 `chunk_vectors` 再保存一份相同向量。

## files：文件及来源状态

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| path | TEXT 主键 | 相对于当前资料根目录的文件路径 |
| revision | TEXT | 原文件字节的 SHA-256；不是博客修订整数 |
| size_bytes / mtime_ns | INTEGER，可空 | 文件字节数、纳秒修改时间；变更判断仍使用哈希 |
| status | TEXT | indexed、indexing、failed；只有 indexed 参与检索 |
| error | TEXT，可空 | 最近一次索引失败原因 |
| title / source_type | TEXT | 标题和来源类别；普通文件使用 md/txt/pdf，网站可为 curated/blogs/knowledge/papers |
| article_id / article_revision | TEXT / INTEGER，可空 | 来源提供时保存文章 ID、修订号 |
| published | INTEGER，可空 | 来源提供时保存 0/1；未知不伪造为已发布 |
| provenance_json | TEXT（JSON） | 来源完整元数据，如原文件路径、网站 URL、分类、来源哈希 |
| index_signature | TEXT | 文本提取/切分规则、模型名称、维度及输入版本的组合标记 |
| indexed_at / updated_at | TEXT | UTC ISO 时间；成功索引/复查时间，状态更新时间 |

旧库迁移时不知道文件大小、修改时间和文章来源的字段保留空值，后续从真实资料刷新时补齐；不会推测补值。网站权限和当前版本仍由原有清单复查，不能仅凭此表的 published 获得访问权限。

## chunks：正文片段与引用位置

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| rowid | INTEGER 主键 | 全文索引使用的稳定内部行 ID；VACUUM 后仍保持映射 |
| id | TEXT 唯一、非空 | 兼容原有片段 ID：路径、文件修订、位置的哈希 |
| path | TEXT 外键 | 指向 files；删除文件记录时级联删除片段 |
| chunk_index | INTEGER | 文件内从 0 开始的片段序号；与 path 联合唯一 |
| location | TEXT | 保留旧引用位置格式，确保来源接口兼容 |
| start_char / end_char | INTEGER | 原文字符区间，左闭右开；PDF 相对于该页提取文本 |
| page_no / line_no | INTEGER，可空 | PDF 页码或文本起始行号，从 1 开始 |
| text | TEXT | 原文片段，不改写正文 |
| content_hash | TEXT | 片段正文 SHA-256 |
| embedding_id | TEXT 外键 | 指向可复用的 embedding_cache 记录 |
| search_text | TEXT | 路径、标题、正文的关键词索引文本；不替代引用原文 |

仍采用 1800 字符分块、200 字符重叠，排除内嵌 Base64 图片编码并保留原文位置。

## embedding_cache：向量与计算缓存

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| id | TEXT 主键 | 模型名称、维度、输入版本、实际输入哈希的组合哈希 |
| input_hash | TEXT | 实际模型输入 `search_document: 路径\n片段正文` 的 SHA-256 |
| model | TEXT | 当前为 nomic-embed-text |
| input_version | TEXT | 当前为 search_document:v1，指输入构造规则，**不是模型权重版本** |
| dimensions | INTEGER | 当前为 768 |
| vector | BLOB | 小端 float32，768 维占 3072 字节；校验长度及有限数值 |
| created_at | TEXT | UTC 创建时间 |

input_hash、model、input_version、dimensions 联合唯一。更换文件修订但片段实际输入不变时可复用；移动文件改变路径，因此会重新计算。若同名模型的权重被替换，仍需显式更新索引签名/模型标识并重建，不宣称已检测 Ollama 模型权重摘要。

中断/失败后保留已完成的向量，供重试使用。成功完成一次无跳过文件的完整扫描后，清理不被任何片段引用的向量；定向扫描不清理其他文件的缓存。

## chunks_fts：全文索引

FTS5 external-content 虚表，索引 chunks.search_text，并通过 INSERT/UPDATE/DELETE 触发器与正文片段保持一致。英文按字母数字词，中文按单个汉字预分词，不宣称已实现中文词语分词。

检索中的关键词候选筛选已接入普通文件路径；调用方提供独立标题映射时保持原有关键词计算，避免新旧标题差异造成漏算。超过 128 个不同检索词时回退到原计算。语义分数、关键词覆盖率、标题加权的组合公式不变，标题与正文之间增加换行分隔，避免边界词粘连。

FTS5 支持 BM25，但本次**没有把 BM25 得分接入融合排序**，也没有增加重排模型。向量仍在 Python 中执行精确扫描；BLOB 不等于 ANN 向量索引。

## metadata：索引运行状态

`key TEXT PRIMARY KEY, value TEXT`。保留 text_index_version、progress、report，并增加 schema_version。报告包含新算/复用片段数和跳过原因。

Schema 版本同时记录于 `PRAGMA user_version=2`。读取高于当前支持版本的库会拒绝运行，避免旧代码误改新库。

## 迁移及更新

- 新代码首次打开旧库前，通过 SQLite backup API 创建同目录 `<原库名>.v1-backup.sqlite`，包含已提交 WAL 数据；不覆盖已有备份。迁移不调用模型。
- 在一个事务中创建新表、转换 JSON 向量、重建全文索引并切换版本。保留有效片段的原 ID、文件哈希、位置、正文，以及旧 metadata；失败整体回滚。
- 旧 cache 中没有对应正文的孤立条目无法还原模型输入，不搬入新缓存，仍保留在备份里。
- 新的文件索引先标记 indexing，向量按批缓存，全部成功且文件哈希未变化后，事务替换片段、全文索引及有效状态。失败文件不参与检索，中断后刷新可以重试。
- 新版索引进程对同一个库使用非阻塞文件锁，避免两次刷新交叉写入；SQLite 开启 WAL 和外键，锁等待上限为 30 秒。升级时应先停止旧版索引任务，旧版程序不识别新锁和新表结构。
- 文件删除在完整扫描时级联清理片段和全文索引。检索仍检查实际源文件哈希，缺失、修改或读取失败的文件不能返回旧片段。
- 回退需停掉所有相关进程，将当前库和 WAL/SHM 配套保留，再恢复备份及旧代码；不能只把新库交给旧代码使用。

## 验证和启用状态

已增加受控测试覆盖旧库迁移/重复打开、迁移失败回滚、来源与位置保持、float32 校验、未变片段复用、删除清理、失败重试、处理中源文件变化、FTS 同步、同库写锁及取消。使用真实 SQLite，向量服务替身，不消耗生成模型额度。

执行结果：后端单元测试 172 项通过；RAG 相关定向测试 26 项通过（包含于单元测试中）；`npm test` 414 项通过、2 项跳过、0 失败；`npm run verify` 通过。新增存储模块、索引模块及结构测试的 Ruff 检查通过。没有执行依赖真实 Docker/PostgreSQL 的整套集成测试，也没有声明模型回答验收通过。

本机 Docker 状态检查在 12 秒内没有响应，不能确认研究容器可用。本次不声称 Docker 中的既有索引已迁移，也未生成新的研究回答。Docker 恢复后通过既有 `docker compose up -d --build` 加载新代码；首次使用各库时自动迁移，刷新资料时补齐来源字段。
