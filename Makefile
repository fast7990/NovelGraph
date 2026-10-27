# ===== 工程操作入口。Windows 上用 Git Bash / WSL 执行，或直接用 uv / pnpm 命令 =====
SHELL := /bin/bash
BACKEND := backend
FRONTEND := frontend
API_PORT ?= 8000
WEB_PORT ?= 5173

.PHONY: help setup sync fe-install dev be fe build test lint clean model model-rerank status sample eval db-install db-migrate db-shell

help:
	@echo "make setup     安装后端(uv)+前端(pnpm)依赖"
	@echo "make dev       同时启动 API(8000) 与前端(5173)"
	@echo "make be        只启动后端"
	@echo "make fe        只启动前端开发服务器"
	@echo "make build     构建前端静态产物（后端会自动挂载 dist）"
	@echo "make test      运行后端单元测试"
	@echo "make model     预下载本地 Embedding 模型"
	@echo "make sample    生成测试样本文本"
	@echo "make eval      NOVEL=nv_xxx [BUILD=40] 检索评测（Recall@10 基线对比）"
	@echo "make model-rerank 预下载精排模型（279MB）"
	@echo "make status    查看服务健康状态"
	@echo "make db-migrate 把 SQLite + Milvus 的历史数据迁到 PostgreSQL + pgvector"
	@echo "make db-shell   连上业务库（psql）"

setup: sync fe-install

sync:
	cd $(BACKEND) && uv sync --python 3.12

fe-install:
	cd $(FRONTEND) && pnpm install

be:
	cd $(BACKEND) && uv run uvicorn app.main:app --host 127.0.0.1 --port $(API_PORT) --reload

fe:
	cd $(FRONTEND) && pnpm dev

dev:
	@echo "API: http://127.0.0.1:$(API_PORT)  前端: http://127.0.0.1:$(WEB_PORT)"
	@(cd $(BACKEND) && uv run uvicorn app.main:app --host 127.0.0.1 --port $(API_PORT) &) ; \
	 cd $(FRONTEND) && pnpm dev

build:
	cd $(FRONTEND) && pnpm build

test:
	cd $(BACKEND) && uv run pytest -q

lint:
	cd $(BACKEND) && uv run ruff check app tests

model:
	cd $(BACKEND) && uv run python -c "from app.services.embedding.onnx_embedder import OnnxEmbedder; OnnxEmbedder()._load(); print('model ready')"

# 精排模型（279MB）：服务启动时会在后台自动下，这里供提前预热/离线环境使用
model-rerank:
	cd $(BACKEND) && uv run python -c "import asyncio; from app.services.rerank.factory import warmup; asyncio.run(warmup()); print('reranker ready')"

# 检索评测：NOVEL=nv_xxx BUILD=40 造题；只跑指标则不带 BUILD
eval:
	cd $(BACKEND) && uv run python scripts/eval_retrieval.py --novel $(NOVEL) $(if $(BUILD),--build $(BUILD)) $(if $(MODES),--modes $(MODES))

sample:
	cd data/samples && python make_sample.py --chapters 24 --chars 1500

status:
	@curl -s http://127.0.0.1:$(API_PORT)/api/v1/health | python -c "import sys,json;d=json.load(sys.stdin);print('status:',d['status']);[print(' -',x['name'],x['status']) for x in d['deps']]"

# 历史数据迁移：SQLite 元数据 + Milvus 已有向量 → PostgreSQL/pgvector
db-migrate:
	cd $(BACKEND) && uv run python scripts/migrate_to_pg.py $(if $(FORCE),--force,) $(if $(SKIP_VECTORS),--skip-vectors,)

db-shell:
	"/d/Program Files/PostgreSQL/18/bin/psql.exe" "$${DATABASE_DSN:-postgresql://postgres@127.0.0.1:5432/novel_rag}"

clean:
	rm -rf $(FRONTEND)/dist
