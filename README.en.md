# NovelGraph — RAG + Knowledge Graph Platform for Long-Form Fiction

English | [简体中文](./README.md)

**Traceable Q&A and character-relationship mapping for novel-length Chinese fiction.**
A whole book is cleaned, split into chapters, chunked into parent/child pairs and embedded; retrieval
hits precisely on child chunks, walks back to the parent for context, and answers with `[资料N]`
citations that jump to the source text. Characters, factions, places and items — and the relations
between them — are extracted into a knowledge graph, so "who is related to whom" doesn't have to be
guessed from embeddings.

What makes long-form fiction hard isn't wiring up an LLM. It's three things: **cross-chapter retrieval**,
**alias fragmentation** (曹操 / 孟德 / 丞相 surfacing as three different people), and **hallucination you
can't check**. This project attacks them with parent/child chunking, alias resolution (union-find plus an
honorific lexicon), and verbatim evidence verification — each decision below is backed by a measurement.

```
raw text → clean → chapters → parent 1400 / child 400 chunks → local ONNX embeddings (1024-d)
        → PostgreSQL + pgvector (text / vectors / lexical / graph in one database)
        → hybrid retrieval: dense + bigram lexical → RRF → cross-encoder rerank
        → streaming chat (citations link back to source) + knowledge graph (entities / relations / evidence)
```

**One PostgreSQL holds everything**: metadata, chunk text, vectors (pgvector + HNSW), the lexical index
(tsvector) and the three graph tables. No Milvus, no Neo4j — a recursive CTE covers 2-hop subgraphs, and
swapping in Neo4j would only mean swapping the driver.

| | |
|---|---|
| **Good for** | Traceable Q&A over Chinese novels, character-relationship exploration, cross-chapter retrieval |
| **Not for** | Scanned PDFs (no OCR), EPUB / DOCX (parsing not implemented), multi-tenant or public-facing deployments (no auth) |
| **Operating cost** | No GPU needed; two local ONNX models totalling ~600 MB, downloaded on first run and then fully offline; graph extraction is the only significant spend (~2.5–3.5M tokens per 600K characters) |
| **Stack** | FastAPI · PostgreSQL 18 + pgvector · React 18 + Vite + Semi Design |

**Jump to**: [Getting started](#getting-started) · [Design decisions](#design-decisions-each-backed-by-a-measurement) · [Retrieval](#retrieval) · [Knowledge graph](#knowledge-graph) · [API](#api) · [Known limitations](#known-limitations)

## What works

| Capability | Status | Notes / measured |
|---|---|---|
| Upload `.txt` / `.md` / `.text`, auto-detect UTF-8 / GB18030 | Done | GBK files detected as `gb18030`, no mojibake. **No epub / docx** — see [Known limitations](#known-limitations) |
| Content cleaning at upload (ad lines / page numbers / repeated lines / HTML residue / half-width punctuation) | Done | Seven independent switches; hit counts go into `ParseResult.clean_stats` and surface as report entries |
| Chapter parsing + parent/child chunking (parent 1400 / child 400 / overlap 60) | Done | 24 chapters / 38,485 chars → 144 child + 48 parent chunks, 99.71% coverage |
| Local embedding (ONNX int8 · bge-large-zh-v1.5 · 8 threads) | Done | **1024 dims**, 2.9 chunks/s ≈ 1100 chars/s (150–400 char chunks) |
| pgvector storage (HNSW / COSINE) | Done | Same DB as chunk text; FK `ON DELETE CASCADE` cleans vectors when a book is deleted |
| Chunk completeness report (char progress + quality warnings + cleaning hits + sampling) | Done | `/chunk-report` and the Chunks page |
| Hybrid retrieval (lexical + dense + RRF + cross-encoder) | Done | 50 + 50 candidates → RRF(k=60) → rerank top-24 → dedupe to 8 parent chunks |
| Degradation chain | Done | Four modes `hybrid / dense_only / sparse_only / empty`, surfaced as `meta.mode` on `/search` and `/chat` |
| Streaming RAG chat (SSE, `[资料N]` citations) | Done | Emits citations first, then deltas; reasoning models also stream a `reasoning` channel |
| Knowledge graph (3-stage extraction + evidence check + alias resolution) | Done | Three tables + recursive CTE; Cytoscape.js force-directed view |
| **LLM usage metering** | Done | Graph builds accumulate per `job_id` into `job.stats.llm_tokens`; chat accumulates per `session_id` into `ChatSession` and returns the round's usage on the `done` event |
| Idempotent re-runs / resumable | Done | Re-running yields the same 144 vectors; a 21,552-chunk book resumed at 5,120 |
| Million-character ingestion | Done | 1,127,180 chars / 620 chapters / 4,279 chunks → 638.8 s (10.6 min, measured at bge-small 512 dims) |
| Retrieval evaluation | Done | `make eval` generates questions and compares three pipelines on Recall@10 + latency |
| Unit tests | Done | 71 cases, pure functions, no network |

## Getting started

### Requirements

| Item | Requirement | Note |
|---|---|---|
| Python | **3.11 or 3.12, non-Anaconda** | ⚠️ Anaconda's 3.12.4 makes `import onnxruntime` **segfault** (exit 139); the symptom is a server subprocess disappearing at startup with no traceback |
| Node / pnpm | Node 18+, pnpm 10 | |
| PostgreSQL | 18 + pgvector 0.8.x | Prebuilt Windows binaries ship in `tools/pgvector-pg18` |
| Hardware | 6 cores / 8 GB RAM | Two ONNX models total ~600 MB (326 MB + 279 MB). **No GPU needed** |

### Four steps

```bash
# 1. Dependencies (uv / pnpm). Use a mirror in mainland China — the default index once
#    stalled for 24 minutes on this machine without downloading a single package.
cd backend && UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple uv sync --python 3.12
cd ../frontend && pnpm install && pnpm rebuild esbuild   # pnpm 10 blocks esbuild's build script by default

# 2. Database
#   Windows: install the extension (run as Administrator; copies files, no PG restart needed)
#     tools\install_pgvector.cmd
psql -U postgres -c "CREATE DATABASE novel_rag ENCODING 'UTF8'"
psql -U postgres -d novel_rag -c "CREATE EXTENSION IF NOT EXISTS vector"
#   Remaining tables are created by create_all at startup (first run loads ONNX models, 1–3 s)

# 3. Configuration
cp backend/.env.example backend/.env      # fill DATABASE_URL (with password) and LLM_API_KEY

# 4. Run
make be      # backend on :8000, also serves the built frontend
make fe      # or: frontend dev server on :5173 (vite proxies /api → :8000)
make build   # or: build dist and open :8000 directly
```

Open http://127.0.0.1:8000 (or :5173). Eight pages: Overview / Upload / Tasks / Chunks / Vectors /
Knowledge Graph / Retrieval debug / Chat.

On first start the two models are pulled from ModelScope (embedding 326 MB + reranker 279 MB), after
which the system is fully offline. Models live in `backend/data/models/`.

```bash
# Generate a sample book and upload it (synthetic text — pipeline smoke test only)
make sample
curl -X POST http://127.0.0.1:8000/api/v1/novels \
     -F "file=@data/samples/长夜将明.txt" -F "title=长夜将明"
```

## Design decisions (each backed by a measurement)

**1. No torch — hand-rolled ONNX inference.**
Torch costs 2.5 GB on CPU, and `sentence-transformers` pulls weights through huggingface_hub's Xet CAS,
which 401s from mainland China. We depend only on `tokenizers + onnxruntime + numpy` and fetch models
from ModelScope (`HF_ENDPOINT` kept as a fallback). BGE pooling is **CLS**, not mean — and
`pooler_output` is wrong too, it's `tanh(dense(CLS))` which BGE was not trained to use.

**Quantized weights**: `Xenova/bge-large-zh-v1.5` int8 dynamic quantization — 326 MB instead of 1.3 GB
fp32, 2–3× faster on CPU. Switching models is a config change (`EMBEDDING_MS_REPO` /
`EMBEDDING_ONNX_FILE`), not a code change.

**Length bucketing**: padding the whole batch to its longest member wastes compute on short chunks.
Encoding 128 at once measured ~15% slower than bucketing by 16. `_encode()` sorts by length and buckets
internally, so `EMBEDDING_BATCH` is a bucket size, not an inference batch limit.

**2. Progress is measured in characters, not chunks.**
Chunk count is unknown up front during streaming splits, so chunk-based progress visibly goes backwards.
`chars_done / chars_total` is monotonic for the whole run; SSE emits a frame every 300 ms.

**3. Vectors live in the same database, and the FK prevents orphans.**
The hard part of the Milvus era was never throughput — it was consistency: the book is deleted, the
vectors remain, and a worker happily spends two hours embedding a nonexistent novel. Now
`chunk_vectors.chunk_id` references `chunks(id)` with `ON DELETE CASCADE`, so deletion is one transaction.
The cost is that vectors compete with business tables for the same instance's IO; at 10M+ chunks, move
`chunk_vectors` to its own database (schema unchanged, just a different DSN).

**4. Token budgets for reasoning models are nothing like chat budgets.**
Measured: one short question on `z-ai/glm-5` burns 208 reasoning tokens, and an undersized `max_tokens`
returns **empty content** with `finish_reason=length`. `thinking:{"type":"disabled"}` was ineffective in
6/6 attempts on that gateway. So chat defaults to 3000, graph extraction to 4500, and the reasoning
stream is forwarded to the UI (otherwise the first ten seconds are blank).

**5. Ingestion is serial, not parallel.**
On an 8-core box, two concurrent ingestion jobs just fight over threads. `workers/runner.py` is a single
queue with a single consumer; to scale out, replace `submit()` with Celery — callers don't change.

**6. The column type is the source of truth for dimensions.**
`chunk_vectors.embedding` is `vector(1024)`, so inserting 512 dims is rejected by PostgreSQL — far more
reliable than an application-level check. Actual model output is still probed at load time
(`FatalModelError` never participates in the auto-downgrade), and `ensure_ready()` reads the column
definition and prints the rebuild SQL on mismatch.

## Retrieval

Three stages, implemented in `backend/app/services/retrieval/hybrid.py`:

```
two channels (50 each) → RRF(k=60) → top 50 into cross-encoder → parent dedupe → 8 chunks into the prompt
```

| Component | Implementation | Why |
|---|---|---|
| Lexical channel | PG `tsvector` + custom **bigram** tokenizer (`retrieval/lexical.py`) | Two-character Chinese terms (names, objects) are missed by trigram matching (FTS5 `MATCH '"铜灯"'` returned nothing; PG's default parser doesn't segment Chinese either). Bigrams hit exactly, and no jieba dependency |
| Fusion | RRF (`retrieval/fusion.py`) | Dense cosine is bounded, BM25 is not — weighted fusion needs normalization whose parameters break when the model changes. RRF only looks at rank |
| Rerank | `bge-reranker-base` int8 ONNX (279 MB) | Bi-encoders can't see term interactions; a cross-encoder can. Scoring 50 candidates takes ~0.3 s |
| Degradation | `mode` field + per-stage logging | Reranker not ready → degrade, don't fail. Either channel down → still returns results |

The lexical index is built **when chunks are written**, not after embedding finishes, so a large book is
lexically searchable while vectors are still being computed (that's `mode=sparse_only`, badged in the UI).

```bash
make eval NOVEL=nv_xxx BUILD=30   # generate questions from real chunks, compare three pipelines
make eval NOVEL=nv_xxx            # reuse an existing question set
```

First run (144 chunks / 25 questions, synthetic text):

| Pipeline | Recall@10 | p50 | p95 |
|---|---|---|---|
| dense (baseline) | 0.920 | — | — |
| rrf (dual channel) | 0.960 | 485 ms | 586 ms |
| hybrid (+ rerank) | 0.960 | 442 ms | 524 ms |

Caveat: the sample text is highly repetitive, so the dense channel is already near its ceiling; +4pt does
not represent gains on real long-form fiction. Latency was measured while all 8 cores were busy embedding,
so re-measure idle.

## Knowledge graph

### Three-stage extraction

| Stage | File | Approach | Output |
|---|---|---|---|
| L1 rules | `graph/candidates.py` | Surname table + dialogue-lead regex (`沈鹤道："…"`) + honorific suffixes. **Zero dependencies, no jieba** | ~40 name candidates, used only as prompt hints for L2 |
| L2 extraction | `graph/extractor.py` | `LLM_MODEL_FAST` extracts entities and relations **per parent chunk**, JSON output | Entities / relations / verbatim evidence / confidence |
| L3 resolution | `graph/aligner.py` + `lexicon.py` + `manual_aliases.py` | Alias union-find; a 190-word honorific lexicon strips generic titles like "丞相"; manual aliases supported | Deduplicated entities and relations |

**Why per chunk, not per chapter**: a chapter can be tens of thousands of characters — one call won't fit
(JSON output becomes unbounded), and over a long context the model starts inventing where a relation's
evidence came from. At 800–1400 characters per call, the token budget is bounded and evidence is locatable.

**Why an LLM at all**: the type system is custom (6 entity types + 22 relation types), so generic NER
labels don't map; relations must carry verbatim evidence for verification; low-confidence items need
self-assessed scores to enter a `pending` review queue. Generic NER provides none of these. But it isn't
LLM-only either — pure LLM extraction drifts badly on entities, hence the three complementary stages.

### Three quality gates

Applied at extraction time; nothing enters the graph without passing:

1. **Confidence** — relations below `GRAPH_CONFIDENCE_ACTIVE` (default 0.6) are kept but marked `pending`
   for human review, not discarded
2. **Evidence check** — `evidence` must be findable in the source chunk; otherwise the relation is dropped
   (this gate catches the most hallucination)
3. **Enumeration** — relation types must match the 22-term vocabulary; invented types are rejected

### Storage and visualization

```
entities(id, name, type, aliases, ...)      -- Character / Faction / Location / Item / Event / Concept
relations(src, dst, type, confidence, status, evidence, ...)
entity_mentions(entity_id, chunk_id, ...)   -- evidence anchored to chunks
```

2-hop subgraphs are one recursive CTE query — no graph database. The frontend uses Cytoscape.js + fcose,
lazy-loaded (587 KB / 184 KB gzipped), with entity search, type/relation filters and a detail panel.

### Cost

Extraction runs once per parent chunk, and **the cost is dominated by output** (every relation carries
verbatim evidence). Calibrated measurement (2 chunks, ~220 chars each):

| Metric | Measured | Per chunk |
|---|---|---|
| Prompt tokens | 2,579 | 1,290 (Chinese ≈ 0.6 token per character) |
| Completion tokens | 3,931 | **1,965** |
| Total | 6,510 | — |

Extrapolated: a 605K-character book with 645 parent chunks ≈ **2.5–3.5M tokens**. If `max_tokens=4500`
runs short, extraction retries once — which doubles output cost. Truncation rate is worth watching.

## LLM usage metering

`services/llm/client.py` accumulates tokens into buckets keyed by `usage_tag`. It deliberately does **not**
count globally: a single client instance is shared by concurrent jobs, and global counters would merge two
books' consumption into one unattributable number.

```python
llm.complete_json(messages, model=..., usage_tag=job_id)   # callers add one argument
```

Graph builds pass `job_id`, and the totals land in `job.stats`:

```json
{
  "llm_calls": 2,
  "llm_tokens": { "prompt": 2579, "completion": 3931, "total": 6510 },
  "llm_calls_without_usage": 0
}
```

When a gateway omits `usage`, the call increments `llm_calls_without_usage` — so an under-count is
explainable rather than silently looking like "no spend". The graph page shows live consumption while
extracting and a summary (input/output, duration, call count) afterwards.

Chat reuses the same machinery with `session_id` as the tag: `chat.py` passes it, then
`memory.flush_usage()` increments `ChatSession.prompt_tokens / completion_tokens` with `SET x = x + n`
(body and background summary both drain the bucket, so nothing is lost), and the `done` event carries the
round's usage back to the client without an extra request.

**Not covered yet**: chapter summaries (`ENABLE_CHAPTER_SUMMARY`), which are off by default.

## API

`API_PREFIX=/api/v1`.

**Novels and chunks**

```
POST   /novels                        Upload + enqueue the full pipeline (returns novel_id + job_id)
GET    /novels                        List
GET    /novels/{id}                   Detail
DELETE /novels/{id}                   Delete (cascades chunks, vectors, graph)
GET    /novels/{id}/chapters          Chapter list
GET    /novels/{id}/chunks            Browse chunks (paginated / by level / by chapter)
GET    /novels/{id}/chunk-report      Completeness + quality warnings (incl. cleaning hits) + sampling
POST   /novels/{id}/chunks/rebuild    mode=full reparses and re-chunks; mode=vectors recomputes vectors only (resumable)
GET    /novels/{id}/excerpt           Fetch source text by char range (citation jump)
GET    /novels/{id}/vectors/sample    Vector sampling (for the projection scatter)
```

**Jobs**

```
GET    /jobs                          List
GET    /jobs/{id}                     Detail (includes stats)
GET    /jobs/{id}/events              SSE: stage / progress / chunk_progress / done
POST   /jobs/{id}/cancel              Cancel (derived data untouched)
POST   /jobs/{id}/retry               Retry
```

**Retrieval and chat**

```
POST   /search                        Retrieval debug (recall only, no generation)
POST   /chat                          SSE: citations → (reasoning) → delta → done
GET    /chat/sessions                 List sessions
POST   /chat/sessions                 Create session
GET    /chat/sessions/{id}/messages   Message history
DELETE /chat/sessions/{id}            Delete session
GET    /chat/sessions/{id}/export     Export
DELETE /chat/messages/{id}            Delete a single message
```

**Knowledge graph**

```
GET    /graph/overview                Scale stats: entity/relation counts, type distribution, top entities
GET    /graph/entities                Entity list (search / pagination)
GET    /graph/entities/{id}           Entity detail + relations (with relation_total)
GET    /graph/relations               Relation list (filter by type / status)
GET    /graph/subgraph                2-hop subgraph (recursive CTE)
GET    /graph/status                  Build scale + latest job status (includes llm_tokens)
POST   /graph/build                   Enqueue a build (novel_id required; an in-flight job is reused)
```

**System**

```
GET    /health                        Per-dependency health (database / vector / embedding / llm / lexical / rerank) + effective config
GET    /system/stats                  Global stats (chunks / vectors / characters / coverage)
```

## Layout

```
backend/
  app/
    api/            novels · jobs(SSE) · chat(SSE) · graph · system
    services/
      ingestion/    parser (chapters) · cleaner (content cleaning) · chunker · pipeline orchestration
      embedding/    onnx (default) / tei / hash fallback
      vector/       pgvector (default) / numpy fallback
      llm/          OpenAI-compatible client + capability probing + **token metering**
      retrieval/    hybrid orchestration · lexical · RRF fusion
      rerank/       bge-reranker-base int8 ONNX
      chat/         intent · memory (rolling summary + session accounting) · rag (prompt assembly + streaming)
      graph/        candidates(L1) · extractor(L2) · aligner/lexicon/manual_aliases(L3) · store · builder
    workers/        Task queue (single queue, single consumer; swappable for Celery)
  scripts/          Diagnostics and ops (usage checks · graph alignment · entity merge · eval · migration)
  tests/            71 cases, pure functions, no network
frontend/           React 18 + Vite + TS + Semi Design, 8 pages
data/samples/       Sample generator and output
tools/              Prebuilt pgvector for Windows, installer, screenshot scripts
docs/               Architecture spec **v1.19** · architecture diagram (architecture.html)
```

## Frontend

Built on [Semi Design](https://semi.design) (`@douyinfe/semi-ui` 2.104):

- Shell: `Layout` + `Nav`, hash routing (`#overview` / `#upload` / `#tasks` / `#chunks` / `#vector` /
  `#graph` / `#search` / `#chat`) so refresh keeps the page
- Graph: Cytoscape lazy-loaded via `React.lazy`, cutting the main bundle from 2.08 MB to 1.49 MB
- Chat: Semi's `Chat` component; retrieved source chunks render under the assistant message, and the
  citation panel is width-draggable (280–560 px) via `Resizable`
- Theming: only `_base/base.css` for tokens; custom CSS uses `var(--semi-color-*)`
- Polling goes through `src/hooks/usePoll.ts` exclusively (conditional, visibility-gated, single-flight);
  no hand-written `setInterval` in pages

## Known limitations

- **Plain text only (`.txt` / `.md` / `.text`)**. `epub` / `docx` are not implemented, although the
  architecture doc's L0 layer once listed them as targets. EPUB requires walking the OPF `spine` order and
  converting XHTML to text (sorting by filename scrambles chapters). Workaround: convert with calibre first.
- **No authentication**: intended for intranet use. Add a token or gateway auth before exposing it.
- **Serial ingestion and graph builds**: one queue, one consumer — one book at a time.
- **Graph extraction is bound by API concurrency**: chapters run concurrently (`GRAPH_CONCURRENCY`,
  default 3 / 5 in `.env`) but **chunks within a chapter are serial**, on top of the global
  `LLM_MAX_CONCURRENCY` semaphore. A 600-chapter book takes 40+ minutes; the bottleneck is round-trips,
  not CPU.
- **No GPU acceleration**: both ONNX sessions hard-code `CPUExecutionProvider`. Switching to
  `onnxruntime-gpu` needs CUDA 12 + cuDNN 9. Note it would only speed up embedding and reranking — not
  graph extraction, which is a remote API.
- **Lexical channel is bigram + tsvector**: no semantic segmentation, so synonyms are out of scope
  (that's the dense channel's job). Stronger Chinese segmentation needs zhparser/pg_jieba, which requires
  extra compilation on Windows.
- **Single pgvector instance**: vectors and business tables share one PG. Fine at 20K chunks; at 10M+,
  split instances or partition.
- **No migration framework**: tables come from `create_all`; schema changes need manual ALTER. Adopt
  Alembic once the schema settles.
- **Samples are synthetic**: `data/samples/make_sample.py` emits repetitive text. It validates the
  pipeline, not retrieval quality.

## Data migration

Migrating from SQLite + Milvus to PostgreSQL + pgvector does not require recomputing vectors:

```bash
make db-migrate                  # SQLite metadata + existing Milvus vectors → PG, rebuild lexical index
make db-migrate SKIP_VECTORS=1   # metadata only; let rebuild recompute vectors
make db-migrate FORCE=1          # wipe the target and start over
```

`backend/scripts/migrate_to_pg.py` reconciles per book and prints `chunks / vectors / lexical` counts;
fill gaps with `POST /novels/{id}/chunks/rebuild?mode=vectors` (resumable).

## Roadmap

1. **Speed up graph extraction**: parallelize chunks within a chapter and merge small chunks — roughly
   3–4× (serial-per-chapter is the dominant cost today).
2. **Complete cost metering**: wire `usage_tag` into `/chat` and chapter summaries for per-scenario reports.
3. **EPUB / DOCX parsing**: convert to plain text, then reuse the existing chaptering and chunking.
4. **Graph quality evaluation**: retrieval has Recall@10; the graph has no consistency metric yet
   (overlap against a baseline extraction).
5. **Admin console**: one-click job retry, batch review of `pending` relations.
6. **Storage backend**: implement `STORAGE_BACKEND=s3`.
