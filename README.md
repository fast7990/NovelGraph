# NovelGraph · 长篇小说 RAG + 知识图谱中台

[English](./README.en.md) | 简体中文

**给百万字级长篇小说做「可溯源问答 + 人物关系」的系统。**
整本书清洗分章后切成父子块向量化入库；检索时由子块精确命中、回溯父块补足上下文，
回答带 `[资料N]` 引用、能跳回原文；同时把人物、势力、地点、物品及其关系抽成知识图谱，
让"谁和谁是什么关系"这类问题不必靠向量硬猜。

长篇小说的难点不在"接一个大模型"，而在三件事——**跨章检索**、**称谓分裂**（曹操 / 孟德 / 丞相
被当成三个人）、**幻觉不可查**。本系统分别用父子切块、别名对齐（并查集 + 泛称词表）、
证据原文回查来对付它们，每一处取舍都在下文附了实测依据。

```
原始文本 → 清洗 → 分章 → 父块 1400 / 子块 400 切块 → 本地 ONNX 向量化（1024 维）
        → PostgreSQL + pgvector（正文 / 向量 / 词法 / 图谱同库）
        → 混合检索：向量 + bigram 词法 → RRF 融合 → cross-encoder 精排
        → 流式问答（引用可回溯）+ 知识图谱（实体 / 关系 / 证据片段）
```

**一个 PostgreSQL 装下全部**：元数据、切片正文、向量（pgvector + HNSW）、词法倒排（tsvector）、
图谱三表。不引入 Milvus，也不引入 Neo4j——2 跳子图用递归 CTE 就够，真要换 Neo4j 只需换驱动。

| | |
|---|---|
| **适合** | 中文长篇小说（网文 / 名著）的可溯源问答、人物关系梳理、跨章检索 |
| **不适合** | 扫描版 PDF（无 OCR）、EPUB / DOCX（暂未实现解析）、需要多租户与鉴权的对外服务 |
| **运行成本** | 无需 GPU；两个本地 ONNX 模型共约 600MB，首启自动下载后完全离线；图谱抽取是唯一的大额支出（约 250–350 万 token / 60 万字） |
| **技术栈** | FastAPI · PostgreSQL 18 + pgvector · React 18 + Vite + Semi Design |

**跳转**：[快速开始](#快速开始) · [关键取舍](#关键取舍都有实测依据) · [检索](#检索) · [知识图谱](#知识图谱) · [接口](#接口) · [已知限制](#已知限制)

## 能力清单

| 能力 | 状态 | 实测 / 说明 |
|---|---|---|
| 上传 `.txt` / `.md` / `.text`，UTF-8 / GB18030 自动识别 | 完成 | GBK 文件识别为 `gb18030`，正文无乱码。**不支持 epub / docx**，见[已知限制](#已知限制) |
| 上传阶段内容清洗（广告行 / 页码 / 连续重复行 / HTML 残留 / 半角标点） | 完成 | 7 个开关可单独关；命中计数进 `ParseResult.clean_stats`，并在处理报告里成条展示 |
| 分章 + 父子切块（父 1400 / 子 400 / 重叠 60） | 完成 | 24 章 / 38,485 字 → 144 子块 + 48 父块，覆盖度 99.71% |
| 本地 Embedding（ONNX int8 · bge-large-zh-v1.5 · 8 线程） | 完成 | **1024 维**，2.9 块/秒 ≈ 1100 字/秒（150–400 字块） |
| pgvector 向量存储（HNSW / COSINE） | 完成 | 向量与正文同库，外键 `ON DELETE CASCADE`，删书自动清向量 |
| 切片完成度档案（字符进度 + 质量告警 + 清洗命中 + 抽检） | 完成 | `/chunk-report` 与「切片完成度」页已接通 |
| 混合检索（词法 + 向量 + RRF + cross-encoder 精排） | 完成 | 双路各 50 → RRF(k=60) → 精排 top24 → 父块去重取 8 |
| 降级链 | 完成 | `hybrid / dense_only / sparse_only / empty` 四态，`/search` 与 `/chat` 的 `meta` 都带 `mode` |
| RAG 流式问答（SSE，带 `[资料N]` 引用） | 完成 | 先推 citations 再推 delta；推理模型额外推 `reasoning` 通道 |
| 知识图谱（三层抽取 + 证据回查 + 别名对齐） | 完成 | 三表 + 递归 CTE；Cytoscape.js 力导向可视化 |
| **LLM 用量计量** | 完成 | 图谱按 `job_id` 累加到 `job.stats.llm_tokens`；问答按 `session_id` 累加到 `ChatSession` 并在 `done` 事件回传本轮消耗 |
| 重跑幂等 / 断点续跑 | 完成 | 重跑仍为 144 条向量；《赘婿》实测跳过 5120/21552 继续跑 |
| 百万字入库性能 | 完成 | 1,127,180 字 / 620 章 / 4,279 块 → 638.8s（10.6 min，bge-small 512 维时实测） |
| 检索评测 | 完成 | `make eval` 自动造题 + 三链路 Recall@10 / 延迟对照 |
| 单元测试 | 完成 | 71 个用例，纯函数、无网络依赖 |

## 快速开始

### 环境要求

| 项 | 要求 | 备注 |
|---|---|---|
| Python | **3.11 或 3.12（非 Anaconda）** | ⚠️ Anaconda 的 3.12.4 会让 `import onnxruntime` **段错误**（exit 139），表现为启动时 server 子进程静默消失 |
| Node / pnpm | Node 18+ / pnpm 10 | |
| PostgreSQL | 18 + pgvector 0.8.x | 仓库 `tools/pgvector-pg18` 带 Windows 预编译包 |
| 硬件 | 6 核 CPU / 8GB 内存起 | 两个 ONNX 模型共约 600MB（326MB + 279MB），**不需要 GPU** |

### 四步跑起来

```bash
# 1. 依赖（uv / pnpm）。国内务必指定镜像，默认源在本机曾 24 分钟无进展
cd backend && UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple uv sync --python 3.12
cd ../frontend && pnpm install && pnpm rebuild esbuild   # pnpm 10 默认拦截 esbuild 构建脚本

# 2. 数据库
#   Windows 装扩展（管理员运行，只复制文件，不需要重启 PG）
#     tools\install_pgvector.cmd
psql -U postgres -c "CREATE DATABASE novel_rag ENCODING 'UTF8'"
psql -U postgres -d novel_rag -c "CREATE EXTENSION IF NOT EXISTS vector"
#   其余表结构由后端启动时 create_all 建（首次会加载 ONNX 模型，约 1–3 秒）

# 3. 配置
cp backend/.env.example backend/.env      # 填 DATABASE_URL（含密码）与 LLM_API_KEY

# 4. 启动
make be      # 后端 8000，同时托管已构建的前端
make fe      # 或者：前端 5173 开发模式（vite 代理 /api → 8000）
make build   # 或者：构建 dist 后直接访问 8000
```

打开 http://127.0.0.1:8000 （或 5173）即可使用。8 个页面：
概览 / 数据上传 / 任务中心 / 切片完成度 / 向量库 / 知识图谱 / 检索调试台 / 问答。

首次启动会自动从 ModelScope 拉取两个模型（embedding 326MB + rerank 279MB），
之后完全离线。模型目录 `backend/data/models/`。

```bash
# 生成样本文本并上传（合成文本，只验证链路）
make sample
curl -X POST http://127.0.0.1:8000/api/v1/novels \
     -F "file=@data/samples/长夜将明.txt" -F "title=长夜将明"
```

## 关键取舍（都有实测依据）

**1. 不用 torch，自己写 ONNX 推理。**
CPU 场景下 torch 占 2.5G，且 `sentence-transformers` 拉模型走 huggingface_hub 的 Xet CAS，国内会 401。
现在只依赖 `tokenizers + onnxruntime + numpy`，模型从 ModelScope 拉（`HF_ENDPOINT` 留作备用）。
BGE 系列的池化是 **CLS**，不是 mean，也不能用 `pooler_output`。

**量化版**：`Xenova/bge-large-zh-v1.5` 的 int8 动态量化权重（326MB，fp32 为 1.3GB），CPU 上快 2–3 倍。
换模型改 `EMBEDDING_MS_REPO` / `EMBEDDING_ONNX_FILE` 即可，不用动代码。

**长度分桶**：整批 padding 会把短块拖到批内最长长度上白算，实测 128 条直接 encode 比按 16 条分桶慢约 15%。
`_encode()` 内部按字符数排序后分桶，`EMBEDDING_BATCH` 是桶大小而不是单次推理上限。

**2. 进度按字符，不按块。**
块数在流式切分中事前未知，用块数算进度会出现"越跑越退"的假进度。
`chars_done / chars_total` 全程单调，SSE 每 300ms 推一帧。

**3. 向量和正文同库，靠外键解决"孤儿向量"。**
Milvus 时代最烦的不是性能而是一致性：书删了、向量还在，worker 还会继续给一本不存在的书算两小时。
现在 `chunk_vectors.chunk_id` 外键指向 `chunks(id)` 且 `ON DELETE CASCADE`，删书是一次事务里的事。
代价是向量和业务表抢同一个实例的 IO；千万级语料应把 `chunk_vectors` 拆到独立库（表结构不变，改 DSN 即可）。

**4. 推理模型的 token 预算不能按普通对话估。**
实测 `z-ai/glm-5` 一句话问答消耗 208 个 reasoning token，`max_tokens` 给小了会返回**空 content**（`finish_reason=length`），
且 `thinking:{"type":"disabled"}` 在该网关实测 6 次全不生效。所以问答默认 3000、图谱抽取默认 4500，
并把思考增量也流给前端（否则首字前有十几秒纯空白）。

**5. 入库串行，不是并行。**
8 核 CPU 上并发两个入库任务只会互相抢线程。`workers/runner.py` 是单队列单消费者，
要横向扩展时把 `submit()` 换成 Celery，调用方不用改。

**6. 维度由列类型兜底，换模型必须重建表。**
`chunk_vectors.embedding` 的类型是 `vector(1024)`，插 512 维 PG 直接拒——比应用层比对可靠。
模型实际维度仍会在加载时探测（`FatalModelError` 不参与 auto 降级），`ensure_ready()` 还会读一次列定义比对。

## 检索

三段式，代码在 `backend/app/services/retrieval/hybrid.py`：

```
两路召回（各 50）→ RRF(k=60) → 头 50 进 cross-encoder → 父块去重 → 8 段进 prompt
```

| 组件 | 实现 | 为什么这么做 |
|---|---|---|
| 词法通道 | PG `tsvector` + 自建 **bigram** 分词（`retrieval/lexical.py`） | 中文两字词（人名/物名）用 trigram 匹配不到（FTS5 实测 `MATCH '"铜灯"'` 返回空，PG 默认分词器同样不切中文）；bigram 精确命中且不引 jieba |
| 融合 | RRF（`retrieval/fusion.py`） | 稠密 cosine 有界、BM25 无界，加权必须先归一化，而归一化参数换模型就失效；RRF 只看排名 |
| 精排 | `bge-reranker-base` int8 ONNX（279MB） | 双塔看不词语间交互，cross-encoder 能看到；对 50 条候选批量打分约 0.3s |
| 降级 | `mode` 字段 + 逐级日志 | 精排未就绪只降级不中断；词法或向量任一挂掉仍能出结果 |

词法索引在**切片落库时**就建（不等向量化跑完），所以大书跑向量化期间也能按词法搜到内容，
这时 `mode=sparse_only`，前端显示"仅词法"徽标。

```bash
make eval NOVEL=nv_xxx BUILD=30   # 用 LLM 从真实切片造题并跑三档对照
make eval NOVEL=nv_xxx            # 复用已有题集
```

首轮实测（《长夜将明》144 块 / 25 题，机器生成文本）：

| 链路 | Recall@10 | p50 | p95 |
|---|---|---|---|
| dense（基线） | 0.920 | — | — |
| rrf（双路融合） | 0.960 | 485ms | 586ms |
| hybrid（+ 精排） | 0.960 | 442ms | 524ms |

注意：样本文本句式高度重复，稠密通道已接近天花板，+4pt 不代表真实长篇的收益；
延迟是在向量化任务占满 8 核时测的，空载需重测。

## 知识图谱

### 三层抽取

| 层 | 文件 | 做法 | 产出 |
|---|---|---|---|
| L1 规则 | `graph/candidates.py` | 姓氏表 + 对话引导正则（`沈鹤道："…"`）+ 称谓后缀，**零依赖，不用 jieba** | 人名候选（约 40 条），只作为提示喂给 L2 |
| L2 抽取 | `graph/extractor.py` | `LLM_MODEL_FAST` 按 **parent 块**抽实体与关系，JSON 输出 | 实体 / 关系 / evidence 原文 / 置信度 |
| L3 对齐 | `graph/aligner.py` + `lexicon.py` + `manual_aliases.py` | 别名并查集合并；190 词泛称表剔掉"丞相/掌门"这类通用称呼；支持手工别名 | 去重后的实体与关系 |

**为什么按块抽而不是按章**：一章可能几万字，一次调用塞不下（JSON 会长到不可控），
而且模型在长上下文里对"某条关系的证据在哪"会开始编。按 800–1400 字的 parent 块抽，
token 预算可控、证据可定位。

**为什么必须用 LLM**：类型体系是自定义的（6 类实体 + 22 种人际关系词），通用 NER 的标签对不上；
关系需要带 evidence 原文供回查；低置信度需要模型自评后进 `pending` 队列。
通用 NER 这三件都做不到。但也不用纯 LLM——纯 LLM 的实体漂移更严重，所以三层互补。

### 三道闸门

抽取层当场做过，过了才入库：

1. **置信度**：低于 `GRAPH_CONFIDENCE_ACTIVE`（默认 0.6）的关系不丢，标 `pending` 进人工审核队列
2. **证据回查**：`evidence` 必须能在该块原文里查到，查不到直接丢弃（这一条拦住最多幻觉）
3. **枚举归拢**：关系类型必须落在 22 个词表内，模型自造的一律不收

### 存储与可视化

```
entities(id, name, type, aliases, ...)      -- 6 类：Character/Faction/Location/Item/Event/Concept
relations(src, dst, type, confidence, status, evidence, ...)
entity_mentions(entity_id, chunk_id, ...)   -- 证据落到块
```

2 跳子图用**递归 CTE** 一次查完，不引入图数据库。前端 `GraphView` 用 Cytoscape.js + fcose，
按需加载（打包 587KB / gzip 184KB），支持实体搜索、按类型/关系筛选、详情面板。

### 成本

抽取按 parent 块调用，**成本大头在输出**（每条关系都带 evidence）。实测校准（2 块，每块约 220 字）：

| 项 | 实测 | 折算 |
|---|---|---|
| 输入 | 2,579 | 1,290 / 块（中文约 1 字 = 0.6 token） |
| 输出 | 3,931 | **1,965 / 块** |
| 合计 | 6,510 | — |

据此估算：《三国演义》605K 字 / 645 父块 ≈ **250–350 万 token**。
`max_tokens=4500` 若吃紧会触发一次重试（输出成本直接翻倍），所以截断率值得盯。

## LLM 用量计量

`services/llm/client.py` 按 `usage_tag` 分桶累加 token，**不全局计数**——同一个 client 被并发任务共用，
全局计数会把两本书的消耗混在一起，账单无法归因。

```python
llm.complete_json(messages, model=..., usage_tag=job_id)   # 调用侧只多传一个 tag
```

图谱构建以 `job_id` 作 tag，进度与收尾时写进 `job.stats`：

```json
{
  "llm_calls": 2,
  "llm_tokens": { "prompt": 2579, "completion": 3931, "total": 6510 },
  "llm_calls_without_usage": 0
}
```

网关不返回 usage 时计入 `llm_calls_without_usage`，让"数字偏小"可解释而不是看起来没消耗。
前端图谱页在抽取中显示"已消耗"、完成后显示"上次构建消耗（输入/输出）+ 耗时 + 调用次数"。

问答侧复用同一套机制，只是 tag 换成 `session_id`：`chat.py` 传入后 `memory.flush_usage()` 把用量
以 `SET x = x + n` 自增到 `ChatSession.prompt_tokens / completion_tokens`（正文与后台摘要谁先跑完谁取走，不丢账），
`done` 事件顺带回传本轮消耗，前端不必再查一次接口。

**未覆盖**：章节摘要（`ENABLE_CHAPTER_SUMMARY`）尚未接入，目前默认关闭。

## 接口

`API_PREFIX=/api/v1`。

**小说与切片**

```
POST   /novels                        上传并触发全流程（返回 novel_id + job_id）
GET    /novels                        列表
GET    /novels/{id}                   详情
DELETE /novels/{id}                   删除（级联清切片、向量、图谱）
GET    /novels/{id}/chapters          章列表
GET    /novels/{id}/chunks            切片浏览（分页 / 按层级 / 按章）
GET    /novels/{id}/chunk-report      切片完成度 + 质量告警（含清洗命中）+ 抽样
POST   /novels/{id}/chunks/rebuild    mode=full 重新解析切块；mode=vectors 只重算向量（断点续跑）
GET    /novels/{id}/excerpt           按字符区间取原文（引用跳转）
GET    /novels/{id}/vectors/sample    向量抽样（降维散点用）
```

**任务**

```
GET    /jobs                          任务列表
GET    /jobs/{id}                     任务详情（含 stats）
GET    /jobs/{id}/events              SSE：stage / progress / chunk_progress / done
POST   /jobs/{id}/cancel              取消（不删派生数据）
POST   /jobs/{id}/retry               重试
```

**检索与问答**

```
POST   /search                        检索调试（只看召回，不生成）
POST   /chat                          SSE 流式问答：citations → (reasoning) → delta → done
GET    /chat/sessions                 会话列表
POST   /chat/sessions                 新建会话
GET    /chat/sessions/{id}/messages   历史消息
DELETE /chat/sessions/{id}            删除会话
GET    /chat/sessions/{id}/export     导出
DELETE /chat/messages/{id}            删除单条消息
```

**知识图谱**

```
GET    /graph/overview                规模统计：实体数 / 关系数 / 类型分布 / 高频实体
GET    /graph/entities                实体列表（搜索 / 分页）
GET    /graph/entities/{id}           实体详情 + 关系（含 relation_total）
GET    /graph/relations               关系列表（按类型 / 状态筛选）
GET    /graph/subgraph                2 跳子图（递归 CTE）
GET    /graph/status                  建成规模 + 最近任务状态（含 llm_tokens）
POST   /graph/build                   入队构建（novel_id 必填；已有任务在跑则复用）
```

**系统**

```
GET    /health                        依赖逐项健康（database / vector / embedding / llm / lexical / rerank）+ 生效配置
GET    /system/stats                  全库统计（切片 / 向量 / 字数 / 覆盖度）
```

## 目录

```
backend/
  app/
    api/            novels · jobs(SSE) · chat(SSE) · graph · system
    services/
      ingestion/    parser 分章 · cleaner 内容清洗 · chunker 切块 · pipeline 编排
      embedding/    onnx（默认）/ tei / hash 兜底
      vector/       pgvector（默认）/ numpy 兜底
      llm/          OpenAI 兼容客户端 + 能力探测 + **token 计量**
      retrieval/    hybrid 编排 · lexical 词法 · fusion RRF
      rerank/       bge-reranker-base int8 ONNX
      chat/         intent · memory（滚动摘要 + 会话记账）· rag（prompt 组装 + 流式生成）
      graph/        candidates(L1) · extractor(L2) · aligner/lexicon/manual_aliases(L3) · store · builder
    workers/        任务队列（单队列单消费者，可替换 Celery）
  scripts/          诊断与运维脚本（用量核对 · 图谱对齐 · 实体合并 · 评测 · 迁移）
  tests/            71 个用例，纯函数、无网络依赖
frontend/           React 18 + Vite + TS + Semi Design，8 个页面
data/samples/       样本生成脚本与产物
tools/              pgvector Windows 预编译包与安装脚本 · 截图脚本
docs/               架构设计说明书 **v1.19** · 架构图（architecture.html）
```

## 前端

基于 [Semi Design](https://semi.design)（`@douyinfe/semi-ui` 2.104）：

- 外壳：`Layout` + `Nav`，hash 路由（`#overview` / `#upload` / `#tasks` / `#chunks` / `#vector` / `#graph` / `#search` / `#chat`），刷新不丢页
- 图谱：`React.lazy` 按需加载 Cytoscape，主包因此从 2.08MB 降到 1.49MB
- 问答：Semi `Chat` 组件承载对话，检索到的原文块挂在助手消息下方，右侧引用面板用 `Resizable` 拖宽（280–560px）
- 主题：只引 `_base/base.css` 拿 token，自定义样式统一用 `var(--semi-color-*)`
- 轮询统一走 `src/hooks/usePoll.ts`（条件轮询 + 可见性门控 + 单飞守卫），页面里禁止手写 `setInterval`

## 已知限制

- **只支持纯文本（`.txt` / `.md` / `.text`）**。`epub` / `docx` 未实现——虽然架构文档 L0 曾把它们列为目标。
  EPUB 要按 OPF `spine` 顺序解包 XHTML 再转纯文本（不能按文件名排序，否则章节乱序），
  临时方案是先用 calibre 转 txt。
- **无鉴权**：内网自用，上线前需加 token 或网关鉴权。
- **入库与图谱串行**：单队列单消费者，同一时刻只有一本书在跑。
- **图谱抽取受 API 并发限制**：章与章并发 `GRAPH_CONCURRENCY`（默认 3/.env 5），**章内逐块串行**，
  再加 `LLM_MAX_CONCURRENCY` 全局信号量。一本 600 章的书约 40 分钟起，瓶颈在 LLM 往返次数而非 CPU。
- **没有 GPU 加速**：两处 ONNX 都写死 `CPUExecutionProvider`，换 `onnxruntime-gpu` 需要 CUDA 12 + cuDNN 9。
  注意它只影响向量化与精排，不影响图谱抽取（那是远程 API）。
- **词法通道是 bigram + tsvector**：不做语义切分，对同义词无能为力（那是稠密通道的活）。
  要更强得上 zhparser/pg_jieba，Windows 下需额外编译。
- **pgvector 单实例**：向量与业务表共用 PG。两万级切片无压力，千万级要拆实例或上分区。
- **无迁移框架**：表结构由 `create_all` 建，改字段需手工 ALTER，稳定后再引 Alembic。
- **样本是程序生成的**：`data/samples/make_sample.py` 产出句式重复的合成文本，只验证链路，不代表检索质量。

## 数据迁移

从 SQLite + Milvus 迁到 PostgreSQL + pgvector 不必重算向量：

```bash
make db-migrate                  # SQLite 元数据 + Milvus 已有向量 → PG，并重建词法索引
make db-migrate SKIP_VECTORS=1   # 只迁元数据，向量交给 rebuild 重算
make db-migrate FORCE=1          # 目标库已有数据时强制重来（先清空）
```

`backend/scripts/migrate_to_pg.py` 会做对账，输出每本书的「切片数 / 向量数 / 词法数」，
缺的部分用 `POST /novels/{id}/chunks/rebuild?mode=vectors` 补（自带断点续跑）。

## 下一步

1. **图谱抽取提速**：章内块并发化 + 小块合并，预计 3–4 倍（当前章内串行是主要瓶颈）。
2. **成本计量补全**：`/chat` 与章节摘要接入 `usage_tag`，出按场景的成本报表。
3. **EPUB / DOCX 解析**：转纯文本后复用现有分章与切块链路。
4. **图谱质量评测**：现在只有检索有 Recall@10，图谱缺一致性指标（与基线抽取的重合率）。
5. **管理台**：任务一键重试、`pending` 关系批量审核。
6. **存储切换**：`STORAGE_BACKEND=s3` 的对象存储实现。
