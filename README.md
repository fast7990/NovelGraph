# 长篇小说 RAG 中台 · P0 + P1

原始文本 → 解析分章 → 父子切块 → 本地向量化 → **PostgreSQL + pgvector** → 混合检索（向量 + 词法 + RRF + 精排）→ RAG 问答（360 网关 glm-5）。

元数据、切片正文、向量、词法倒排**全在一个 PostgreSQL 实例里**，不再需要 Milvus。
知识图谱（Neo4j）为 P1 剩余阶段，本仓库已留好适配器位置。

## 已跑通的范围

| 能力 | 状态 | 实测 |
|---|---|---|
| 上传 txt/md，UTF-8 / GB18030 自动识别 | 完成 | GBK 文件识别为 `gb18030`，正文无乱码 |
| 分章 + 父子切块（父 1400 / 子 400 / 重叠 60） | 完成 | 24 章 / 38,485 字 → 144 子块 + 48 父块，覆盖度 99.71% |
| 本地 Embedding（ONNX int8 · bge-large-zh-v1.5 · 8 线程） | 完成 | **1024 维**，实测 **2.9 块/秒 ≈ 1100 字/秒**（150-400 字块） |
| pgvector 向量存储（HNSW / COSINE） | 完成 | 向量与正文同库，外键 `ON DELETE CASCADE`，删书自动清向量 |
| PostgreSQL 关系库 | 完成 | `timestamptz` + `jsonb`，连接池 8/16，autocommit 批写 |
| 切片完成度（字符进度 + 9 类质量告警 + 抽检） | 完成 | `/chunk-report` 与页面均已接通 |
| RAG 流式问答（检索 → 父块回溯 → SSE） | 完成 | 答案带 `[资料N]` 引用，实测引用命中原文 |
| 中台页面（概览 / 上传 / 切片 / 向量 / 问答） | 完成 | React 18 + Vite + Semi Design，构建产物由后端直接托管 |
| 重跑幂等 | 完成 | 同书重跑仍为 144 条向量，未产生重复行 |
| 百万字性能 | 完成 | 1,127,180 字 / 620 章 / 4,279 块 → **638.8s（10.6 min）**，写入 4279 = 存储 4279（bge-small 512 维时实测；1024 维按 1100 字/秒估算约 18-20 min） |
| 断点续跑 | 完成 | 重跑时跳过已入库 chunk，实测《赘婿》跳过 5120/21552 继续跑 |
| **混合检索（P1-M6）** | 完成 | 词法（PG tsvector + bigram）与向量双路各 50 → RRF → cross-encoder 精排 top24 → 父块去重取 8 |
| **降级链（P1-M6）** | 完成 | `hybrid / dense_only / sparse_only / empty` 四态，`/search` 与 `/chat` 的 `meta` 都带 `mode` |
| **检索评测（P1-M6）** | 完成 | `make eval` 自动造题 + 三链路 Recall@10/延迟对照 |
| Neo4j 知识图谱 | 未开始（P1） | 适配器目录已留 |

## 快速开始

```bash
# 1. 依赖（后端 uv / 前端 pnpm）
make setup

# 2. 数据库：PostgreSQL 18 + pgvector
#    2.1 装扩展（管理员运行，只复制文件）
#        tools\install_pgvector.cmd
#    2.2 建库并启用扩展
psql -U postgres -c "CREATE DATABASE novel_rag ENCODING 'UTF8'"
psql -U postgres -d novel_rag -c "CREATE EXTENSION IF NOT EXISTS vector"

# 3. 配置：复制 .env.example 后填 DATABASE_URL（含密码）/ LLM_API_KEY
cp backend/.env.example backend/.env

# 4. 启动（首次启动会自动建表 + 建 HNSW 索引）
make be        # 后端 8000（首次会加载 ONNX 模型，约 1s）
make fe        # 前端 5173（或 make build 后直接访问 8000）

# 5. 生成样本文本并上传
make sample
curl -X POST http://127.0.0.1:8000/api/v1/novels \
     -F "file=@data/samples/长夜将明.txt" -F "title=长夜将明"
```

打开 http://127.0.0.1:8000 （或 5173）即可看到上传、进度、切片完成度与问答。

## 关键取舍（都有实测依据）

**1. 不用 torch，自己写 ONNX 推理。**
CPU 场景下 torch 占 2.5G，且 `sentence-transformers` 拉模型走 huggingface_hub 的 Xet CAS，国内会 401。
现在只用 `tokenizers + onnxruntime + numpy`，模型文件从 ModelScope 拉取（`HF_ENDPOINT` 留作备用），
首启自动补齐模型，之后完全离线。BGE 系列的池化是 **CLS**，不是 mean，也不能用 pooler_output。

**量化版**：`Xenova/bge-large-zh-v1.5` 的 int8 动态量化权重（326MB，相对 fp32 1.3GB），
CPU 上快 2-3 倍。模型来源用 `EMBEDDING_MS_REPO` / `EMBEDDING_ONNX_FILE` 配置，换模型不用改代码。

**长度分桶**：整批 padding 会把短块拖到批内最长长度上白算，实测把 128 条直接 encode 比按 16 条分桶慢约 15%。
`_encode()` 内部按字符数排序后分桶，`EMBEDDING_BATCH` 是桶大小而不是单次推理上限。

**2. 进度按字符，不按块。**
块数在流式切分中事前未知，用块数算进度会出现"越跑越退"的假进度。
`chars_done / chars_total` 全程单调，SSE 每 300ms 推一帧。

**3. 向量和正文放同一个库，靠外键解决"孤儿向量"。**
Milvus 时代最烦的问题不是性能，是一致性：书删了、向量还在，worker 还会继续给一本不存在的书算两小时。
现在 `chunk_vectors.chunk_id` 外键指向 `chunks(id)` 且 `ON DELETE CASCADE`，
删书是一次事务里的事，不依赖应用层"记得删"。代价是向量和业务表抢同一个实例的 IO，
千万级语料时应把 `chunk_vectors` 拆到独立库（表结构不变，改 DSN 即可）。

**4. glm-5 是推理模型，token 预算不能按普通对话估。**
实测一句话问答消耗 208 个 reasoning token，`max_tokens` 给小了会返回**空 content**（finish_reason=length）。
现在问答默认 3000、抽取默认 4000，并对 `json_mode` 做启动探测（该网关支持）。

**5. 入库串行，不是并行。**
8 核 CPU 上并发两个入库任务只会互相抢线程。`workers/runner.py` 是单队列单消费者，
要横向扩展时把 `submit()` 换成 Celery 即可，调用方不用改。

**6. 维度由列类型兜底，换模型必须重建表。**
`chunk_vectors.embedding` 的类型是 `vector(1024)`，插 512 维 PG 直接拒——比应用层比对可靠。
模型实际输出维度仍会在加载时探测（`FatalModelError` 不参与 auto 降级），
`ensure_ready()` 还会读一次列定义比对，不一致时把重建 SQL 直接打印出来。

## 检索（P1-M6）

三段式，代码在 `backend/app/services/retrieval/hybrid.py`：

```
两路召回(各 50) → RRF(k=60) → 头 50 进 cross-encoder → 父块去重 → 8 段进 prompt
```

| 组件 | 实现 | 为什么这么做 |
|---|---|---|
| 词法通道 | PG `tsvector` + 自建 **bigram** 分词（`retrieval/lexical.py`） | 中文两字词（人名/物名）用 trigram 完全匹配不到（FTS5 实测 `MATCH '"铜灯"'` 返回空，PG 默认分词器同样不切中文）；bigram 精确命中且不引 jieba |
| 融合 | RRF（`retrieval/fusion.py`） | 稠密 cosine 有界、BM25 无界，加权必须先归一化，而归一化参数换模型就失效；RRF 只看排名 |
| 精排 | `bge-reranker-base` int8 ONNX（279MB，`rerank/`） | 双塔看不词语间交互，cross-encoder 能看到；对 50 条候选批量打分约 0.3s |
| 降级 | `mode` 字段 + 逐级日志 | 精排未就绪只降级不中断；词法/向量任一挂掉仍能出结果 |

词法索引在**切片落库时**就建（不等向量化跑完），所以大书跑向量化期间也能按词法搜到内容，
这时 `mode=sparse_only`，前端会显示"仅词法"徽标。

评测：

```bash
make eval NOVEL=nv_xxx BUILD=30     # 用 LLM 从真实切片造题并跑三档对照
make eval NOVEL=nv_xxx              # 复用已有题集
make model-rerank                   # 预下载精排模型（启动时也会后台自动下）
```

首轮实测（《长夜将明》144 块 / 25 题，机器生成文本）：

| 链路 | Recall@10 | p50 | p95 |
|---|---|---|---|
| dense（P0 基线） | 0.920 | — | — |
| rrf（双路融合） | 0.960 | 485ms | 586ms |
| hybrid（+ 精排） | 0.960 | 442ms | 524ms |

注意：该样本文本句式高度重复，稠密通道已接近天花板，+4pt 不能代表真实长篇的收益；
延迟数据是在向量化任务占满 8 核时测的，空载需重测。换到 pgvector 后需按同样方法复测
（`chunk_vectors` 有 HNSW 索引，两万级语料下预计与 Milvus 同量级）。

## 目录

```
backend/
  app/
    api/            novels / jobs(SSE) / chat(SSE) / system
    services/
      ingestion/    parser 分章 · chunker 切块 · pipeline 编排
      embedding/    onnx（默认）/ tei / hash 兜底
      vector/       pgvector（默认）/ numpy 兜底
      llm/          OpenAI 兼容客户端 + 能力探测
      retrieval/    hybrid 三路编排 · lexical 词法 · fusion RRF
      rerank/       bge-reranker-base int8 ONNX
      chat/         prompt 组装 + 流式生成
    workers/        任务队列（可替换 Celery）
  tests/            15 个用例，纯函数，无网络依赖
frontend/           React + Vite + TS + Semi Design，5 个页面
data/samples/       样本生成脚本与产物
scripts/            迁移（migrate_to_pg）· 检索评测（eval_retrieval）
tools/              pgvector Windows 预编译包与安装脚本
docs/               架构设计说明书 v1.6 · 架构图（architecture.html）
```

## 前端

界面基于 [Semi Design](https://semi.design)（`@douyinfe/semi-ui` 2.104）搭建：

- 外壳：`Layout` + `Nav`，页面用 hash 路由（`#overview` / `#upload` / `#chunks` / `#vector` / `#chat`），刷新不丢页
- 列表与统计：`Card` / `Table` / `Tag` / `Progress` / `Select` / `SideSheet` / `Banner`
- 问答：Semi 的 `Chat` 组件承载对话（Markdown 渲染、停止生成、清除上下文、消息操作栏），
  检索到的原文块用 `chatBoxRenderConfig.renderChatBoxContent` 挂在助手消息下方，
  右侧引用面板用 `Resizable` 拖拽调宽（280–560px）
- 主题：只引 `_base/base.css` 拿 token，组件样式由 Semi 按需带入；自定义样式里统一用 `var(--semi-color-*)`

## 接口

```
POST   /api/v1/novels                     上传并触发全流程
GET    /api/v1/novels                     列表
GET    /api/v1/novels/{id}/chunk-report   切片完成度 + 告警
GET    /api/v1/novels/{id}/chunks         切片浏览（分页/按层级/按章）
POST   /api/v1/novels/{id}/chunks/rebuild 全量重跑（幂等）
GET    /api/v1/novels/{id}/excerpt        按字符区间取原文（引用跳转）
GET    /api/v1/jobs/{id}/events           SSE：stage / progress / chunk_progress / done
POST   /api/v1/search                     检索调试（只看召回，不生成）
POST   /api/v1/chat                       SSE 流式问答，先推 citations 再推 delta
GET    /api/v1/health                     四类依赖逐项健康 + 生效配置
```

## 已知限制

- **词法通道是 bigram + tsvector**：不做中文词性/语义切分，靠两字窗口+OR+`ts_rank_cd` 排序，对"同义词"无能为力（那是稠密通道的活）。要更强得上 zhparser/pg_jieba，Windows 下需要额外编译。
- **pgvector 单实例**：向量和业务表共用一个 PG。两万级切片没有压力（HNSW 建索引秒级），千万级要拆实例或上分区。
- **精排模型 279MB**：首次启动后台下载，未就绪期间检索自动退回 RRF 排序（`mode=hybrid` 但 `detail.reranked=false`）。
- **无鉴权**：内网自用。上线前需加 token 或网关鉴权。
- **无迁移框架**：表结构由 `create_all` 建，改字段需手工 ALTER。表结构稳定后再引 Alembic。
- **章摘要未开启**：`ENABLE_CHAPTER_SUMMARY=false`，开启后每章调一次 LLM，会显著加长入库时间。
- **样本是程序生成的**：`data/samples/make_sample.py` 产出的是句式重复的合成文本，只验证链路，不代表真实检索质量。

## 数据迁移

从 SQLite + Milvus 迁到 PostgreSQL + pgvector 不必重算向量：

```bash
make db-migrate              # SQLite 元数据 + Milvus 已有向量 → PG，并重建词法索引
make db-migrate SKIP_VECTORS=1   # 只迁元数据，向量交给 rebuild 重算
make db-migrate FORCE=1      # 目标库已有数据时强制重来（先清空）
```

脚本 `backend/scripts/migrate_to_pg.py` 会做对账，输出每本书的「切片数 / 向量数 / 词法数」，
缺的部分用 `POST /novels/{id}/chunks/rebuild?mode=vectors` 补（自带断点续跑）。

## 下一步（P1 剩余）

1. **M5 图谱**：`GraphStore` 抽象 + 实体/关系抽取（glm-5 JSON 模式 + evidence 原文校验）+ 实体对齐；
   存储先用 SQLite 兼容层（边表 + 2 跳查询），接口按 Cypher 语义设计，换 Neo4j 只换驱动。
2. **M9 可视化**：图谱 2 跳子图（按 label/关系类型/强度筛选）+ 向量 UMAP 降维散点（预计算）。
3. **M10 完整管理台**：四模块 + 统计面板 + 任务一键重试。
4. **评测补强**：《赘婿》向量化完成后复测混合检索，验证 "+8pt" 验收线。
5. **存储切换**：`STORAGE_BACKEND=s3` 的对象存储实现。
