# 长篇小说 RAG 中台 · P0

原始文本 → 解析分章 → 父子切块 → 本地向量化 → Milvus 3.0 → RAG 问答（360 网关 glm-5）。
知识图谱（Neo4j）为 P1 阶段，本仓库已留好适配器位置。

## 已跑通的范围

| 能力 | 状态 | 实测 |
|---|---|---|
| 上传 txt/md，UTF-8 / GB18030 自动识别 | 完成 | GBK 文件识别为 `gb18030`，正文无乱码 |
| 分章 + 父子切块（父 1400 / 子 400 / 重叠 60） | 完成 | 24 章 / 38,485 字 → 144 子块 + 48 父块，覆盖度 99.71% |
| 本地 Embedding（ONNX · bge-small-zh-v1.5 · 4 线程） | 完成 | 512 维，**实测 6.7 块/秒**（300-400 字块） |
| Milvus 3.0（Zilliz Serverless）写入与检索 | 完成 | 集合自建、强一致计数、按 novel_id 过滤 |
| 切片完成度（字符进度 + 9 类质量告警 + 抽检） | 完成 | `/chunk-report` 与页面均已接通 |
| RAG 流式问答（检索 → 父块回溯 → SSE） | 完成 | 答案带 `[资料N]` 引用，实测引用命中原文 |
| 中台页面（概览 / 上传 / 切片 / 向量 / 问答） | 完成 | React + Vite，构建产物由后端直接托管 |
| 重跑幂等 | 完成 | 同书重跑仍为 144 条向量，未产生重复行 |
| 百万字性能 | 完成 | 1,127,180 字 / 620 章 / 4,279 块 → **638.8s（10.6 min）**，写入 4279 = 存储 4279 |
| Neo4j 知识图谱 | 未开始（P1） | 适配器目录已留 |

## 快速开始

```bash
# 1. 依赖（后端 uv / 前端 pnpm）
make setup

# 2. 配置：复制 .env.example 后填 MILVUS_URI / MILVUS_TOKEN / LLM_API_KEY
cp backend/.env.example backend/.env

# 3. 启动
make be        # 后端 8000（首次会加载 ONNX 模型，约 1s）
make fe        # 前端 5173（或 make build 后直接访问 8000）

# 4. 生成样本文本并上传
make sample
curl -X POST http://127.0.0.1:8000/api/v1/novels \
     -F "file=@data/samples/长夜将明.txt" -F "title=长夜将明"
```

打开 http://127.0.0.1:8000 （或 5173）即可看到上传、进度、切片完成度与问答。

## 关键取舍（都有实测依据）

**1. 不用 torch，自己写 ONNX 推理。**
CPU 4 核场景下 torch 占 2.5G，且 `sentence-transformers` 拉模型走 huggingface_hub 的 Xet CAS，国内会 401。
现在只用 `tokenizers + onnxruntime + numpy`，模型文件从 ModelScope 拉取（`HF_ENDPOINT` 留作备用），
首启自动补齐 94MB 模型，之后完全离线。BGE 系列的池化是 **CLS**，不是 mean。

**2. 进度按字符，不按块。**
块数在流式切分中事前未知，用块数算进度会出现"越跑越退"的假进度。
`chars_done / chars_total` 全程单调，SSE 每 300ms 推一帧。

**3. Milvus 查询必须带强一致。**
实测第一次跑完 `vectors_written=144` 但 `count(*)` 只有 128 —— Zilliz Serverless 默认 Bounded 一致性读到的是旧快照。
已改为 `consistency_level=Strong`（`MILVUS_CONSISTENCY` 可调），重跑后 144/144 对齐。

**4. glm-5 是推理模型，token 预算不能按普通对话估。**
实测一句话问答消耗 208 个 reasoning token，`max_tokens` 给小了会返回**空 content**（finish_reason=length）。
现在问答默认 3000、抽取默认 4000，并对 `json_mode` 做启动探测（该网关支持）。

**5. 入库串行，不是并行。**
4 核 CPU 上并发两个入库任务只会互相抢线程。`workers/runner.py` 是单队列单消费者，
要横向扩展时把 `submit()` 换成 Celery 即可，调用方不用改。

## 目录

```
backend/
  app/
    api/            novels / jobs(SSE) / chat(SSE) / system
    services/
      ingestion/    parser 分章 · chunker 切块 · pipeline 编排
      embedding/    onnx（默认）/ tei / hash 兜底
      vector/       milvus（默认）/ numpy 兜底
      llm/          OpenAI 兼容客户端 + 能力探测
      retrieval/    稠密召回 + 父块回溯 + 上下文预算
      chat/         prompt 组装 + 流式生成
    workers/        任务队列（可替换 Celery）
  tests/            15 个用例，纯函数，无网络依赖
frontend/           React + Vite + TS，5 个页面
data/samples/       样本生成脚本与产物
docs/               架构设计说明书 v1.1 · OpenAPI · 架构图
```

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

- **检索只有稠密通道**：bge-small 不出稀疏向量，BM25 混合检索与 RRF、reranker 在 P1。
- **无鉴权**：内网自用。上线前需加 token 或网关鉴权。
- **SQLite 单机**：换 PostgreSQL 只改 `DATABASE_URL`，已在配置层留好。
- **章摘要未开启**：`ENABLE_CHAPTER_SUMMARY=false`，开启后每章调一次 LLM，会显著加长入库时间。
- **样本是程序生成的**：`data/samples/make_sample.py` 产出的是句式重复的合成文本，只验证链路，不代表真实检索质量。

## 下一步（P1）

1. Neo4j：实体/关系抽取（glm-5 JSON 模式 + evidence 原文校验）、实体对齐、图谱可视化。
2. 混合检索：Milvus 服务端 BM25 + 稠密 RRF 融合 → bge-reranker 精排。
3. 评测集：30-50 条人工问题，量化 Recall@10 与引用准确率，把"优化"变成可比较的数字。
4. 存储切换：`STORAGE_BACKEND=s3` 的对象存储实现。
