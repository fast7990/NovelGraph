# 项目长期约定（长篇小说 RAG + 知识图谱）

## 后端
- 单库 PostgreSQL + pgvector，**不引入 Neo4j / Milvus**；图谱用 entities / relations / entity_mentions 三表 + 递归 CTE
- 抽取模型固定 `deepseek-chat-v3`（`z-ai/glm-5` 是推理模型，content 为空，thinking 开关不生效）
- 取消信号两套，不许合并：`cancel(novel_id)` 要清派生数据，`cancel_job(job_id)` 一个字节都不删
- 本地跑服务用项目 venv：`backend/.venv/Scripts/python.exe`，不要用全局 python
- **venv 解释器必须是 3.11.9（Store 版）**：Anaconda 的 3.12.4 会让 `import onnxruntime` 直接 segfault（exit 139），启动时表现为 server 子进程静默消失
- `uv sync` 必须带 `UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple`，默认源在本机 20+ 分钟无进展

## 前端
- 轮询**一律走 `src/hooks/usePoll.ts`**，禁止在页面里手写 `setInterval`
- 副作用依赖里禁止出现「被该副作用写入的状态」（会自激成请求风暴）
- 轮询必须带条件（有活任务才轮）、可见性门控、单飞守卫
- 构建用 `./node_modules/.bin/vite build`（`npx pnpm build` 在本机坏了）
- vite 固定 `host: '127.0.0.1'`（默认只听 ::1）
- **`<Card title="字符串">` 禁用**：Semi 对字符串标题硬编码 `Typography.Title ellipsis`，内部 `ReactResizeObserver` 走 `findDOMNode`，StrictMode 下刷弃用告警。统一用 `src/components/CardTitle.tsx`
- `Popconfirm`/`Tooltip` 的子元素必须是原生 DOM（`<span>`），放 `<Button>` 会触发同样的 findDOMNode 告警
- 控制台必须保持 0 告警；8 个页面全扫一遍再提交

## 排查环境
- 本机设了 `http_proxy=127.0.0.1:52313`，`curl` 测本地服务必须加 `--noproxy '*'`，否则返回假的 502
- httpx 同理，脚本里用 `trust_env=False`

## 工作习惯
- 性能问题先用日志量化 QPS、按客户端端口归因，再改代码；不猜
- 文档 `docs/架构设计说明书.md` 与 `docs/architecture.html` 必须跟着实现同步改，版本号递增
