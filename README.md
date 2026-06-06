# 🤖 Mini-Agent — 从零实现的最小可用 Agent

不依赖 LangChain / OpenHands 等框架，核心 runtime 全部自研的最小可用 AI Agent。
**单文件架构**——Runtime、Memory、Tools、Redis Cache、Message Queue、Web Server 全部聚合在 `mini_agent.py` 中。

---

## 🚀 运行方式

### 方式一: Docker Compose（推荐，含 Redis）

```bash
git clone https://github.com/qing1chen/mini-agent.git
cd mini-agent

cp .env.example .env
# 编辑 .env，填入 LLM_API_KEY（必填）

docker compose up --build -d
```

启动后访问 `http://localhost:5000`（Web UI），健康检查 `curl http://localhost:5000/health`。

### 方式二: 本地运行

```bash
git clone https://github.com/qing1chen/mini-agent.git
cd mini-agent

pip install -r requirements.txt

cp .env.example .env
# 编辑 .env，填入 LLM_API_KEY（必填）

# 可选：启动 Redis（无 Redis 自动降级为纯 JSON 文件模式）
docker run -d --name redis -p 6379:6379 redis:7-alpine

# Web 模式
python mini_agent.py

# 或 CLI 模式
python mini_agent.py --cli
```

### CLI 常用命令

```bash
python mini_agent.py --cli                  # 新建会话
python mini_agent.py --cli --session abc123 # 恢复已有会话
python mini_agent.py --cli --list           # 列出所有会话
python mini_agent.py --cli --quiet          # 安静模式（减少调试输出）
```

### 依赖说明

| 包名 | 用途 |
|------|------|
| `flask` / `flask-cors` | Web 服务 + 跨域 |
| `redis` | 缓存 + 消息队列 |
| `pymupdf` | PDF 渲染 → OCR |
| `python-docx` | DOCX 文本提取 |

Redis 和附件解析库均做了 try/except 保护，缺失时自动降级，不会阻止启动。

---

## 🏗 系统设计

### 整体架构

系统有两条数据通路，服务于不同场景：

```
┌──────────────────────────────────────────────────────────┐
│                       用户入口                            │
│                Web UI / CLI / API                         │
└────────┬───────────────────────────────────┬──────────────┘
         │                                   │
    同步路径 POST /chat                  异步路径 POST /task/submit
         │                                   │
         ▼                                   ▼
┌─────────────────────────┐        ┌──────────────────────┐
│     AgentRuntime        │        │   Redis 消息队列      │
│     ReAct 循环          │        │   RPUSH → BLPOP       │
│                         │        └─────────┬────────────┘
│  LLM 推理               │                  │
│    ↓                    │                  ▼
│  有 tool_calls?         │        ┌──────────────────────┐
│  ├─ 是 → 执行 → 继续    │        │   Worker 线程         │
│  └─ 否 → 最终回答       │        │   执行工具 → SETEX    │
└────────┬────────────────┘        └──────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────┐
│                  Session（记忆层）                         │
│                                                          │
│    ┌─────────────────────────────────────────────────┐   │
│    │ Redis 缓存（带 TTL）                              │   │
│    │   读: Read-Through (miss → JSON 加载 → 回填)     │   │
│    │   写: Write-Through (同时写 Redis + JSON)        │   │
│    │   删: 延时双删 (delete → sleep → delete)         │   │
│    └─────────────────────────────────────────────────┘   │
│                          ↕                               │
│    ┌─────────────────────────────────────────────────┐   │
│    │ JSON 文件持久化                                   │   │
│    │   data/sessions/<id>.json（原子写入 tmp+rename） │   │
│    └─────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────┘
```

### 同步路径：ReAct 核心循环

`POST /chat` 走的是同步 ReAct 循环，是系统的主干路径：

```
用户消息 → Session.load()
         → 构建 messages（system prompt + state + history）
         → LLM 推理
         → 有 tool_calls → ToolRegistry.execute() → 结果注入 messages → 继续循环
         → 无 tool_calls → 输出最终回答
         → Session.save()
```

保护机制：连续 3 次工具调用失败时注入反思提示，引导 LLM 换策略；达到 `max_steps=15` 时强制返回超限提示。

### 异步路径：任务队列

`POST /task/submit` 走的是异步路径，为 OCR 等耗时工具设计：

```
用户提交 → RPUSH 任务到 Redis 队列 → 立即返回 task_id
Worker 线程 BLPOP 消费 → 执行工具 → SETEX 写结果到 Redis
用户 GET /task/result/{task_id} 轮询结果
```

注意：这条路径和 ReAct 循环是独立的。主聊天路径的工具调用在 ReAct 循环内同步执行，不经过消息队列。异步队列只在通过 `/task/submit` 接口直接提交工具任务时使用。

### 缓存策略详解

三种策略各管一个场景，互不冲突：

**Read-Through（读取时）**——调用 `Session.load()` 时，先查 Redis。命中则直接反序列化返回；未命中则从 `data/sessions/<id>.json` 加载，反序列化后回填到 Redis（设 TTL），再返回。

**Write-Through（保存时）**——调用 `Session.save()` 时，同时写两个地方：序列化后 SETEX 到 Redis（带 TTL），并原子写入 JSON 文件（先写 tmp 文件再 rename，防止写到一半崩溃导致文件损坏）。

**延时双删（删除时）**——调用 `Session.delete()` 时，面临并发竞态问题：删掉 Redis 缓存后，另一个并发的 `load()` 请求可能刚好从 JSON 读到旧数据并回填 Redis，导致"删了又出现"。延时双删的流程：删 Redis → 删 JSON 文件 → sleep 短暂时间 → 再删一次 Redis。第二次删除确保并发回填的脏数据也被清理。

---

## 🧠 Memory 的召回时机与放置方式

### Session 内存结构

每个 Session 持久化三类数据：

```json
{
  "session_id": "abc123",
  "history": [
    {"role": "user", "content": "帮我创建一个任务"},
    {"role": "assistant", "content": "已创建任务 #1", "tool_calls": [...]},
    {"role": "tool", "content": "...", "tool_call_id": "..."}
  ],
  "state": {
    "todos": [{"id": 1, "text": "完成笔试题", "status": "pending"}]
  },
  "traces": [...]
}
```

三类数据的角色不同：`history` 是完整对话记录，LLM 靠它理解上下文；`state` 是工具运行时的结构化状态（如待办列表），跨轮次持久化；`traces` 是每步工具调用的执行记录，用于调试。

### 召回时机：何时加载 Memory

Memory 的召回发生在 ReAct 循环的入口处，即 `AgentRuntime.run()` 被调用时：

```
用户发送消息
  → Session.load(session_id)     ← 此处触发 Memory 召回
  → Read-Through: Redis 命中 → 直接用
                   Redis miss → 从 JSON 加载 → 回填 Redis
  → 拿到完整的 history + state
  → 进入 ReAct 循环
```

每一轮对话开始时只召回一次。循环内的多步工具调用不重复召回，而是在内存中累积，循环结束后通过 `Session.save()` 一次性持久化。

### 放置方式：Memory 在 messages 中的位置

召回的 Memory 被组装成 LLM 的 messages 数组，结构如下：

```
messages = [
  {
    "role": "system",
    "content": system_prompt + state 摘要
  },
  ...history（完整的对话历史）,
  {
    "role": "user",
    "content": 本轮用户消息
  }
]
```

关键设计决策：

**state 放在 system prompt 中**——工具的结构化状态（如当前待办列表）被序列化后拼接到 system prompt 末尾。这样 LLM 在每一轮推理时都能看到最新的全局状态，而不需要从历史消息中自己推断。

**history 完整保留**——所有历史消息（包括 tool 角色的消息）按原始顺序排列在 system 和当前用户消息之间。LLM 靠历史消息理解对话脉络，也能看到之前工具调用的输入输出。

**长期记忆通过工具加载**——业务规则等长期知识不常驻 system prompt，而是通过 `load_checker_rules` 工具按需加载。加载后的内容作为 tool 消息进入 history，后续轮次通过 history 自然可见。这避免了 system prompt 膨胀。

### 跨轮次状态持久化

以 todo 工具为例，状态如何跨轮次保持：

```
第 1 轮：用户说"创建任务 X"
  → load session (空 state)
  → LLM 调用 todo.add → state.todos = [{id:1, text:"X", status:"pending"}]
  → save session → state 持久化到 Redis + JSON

第 2 轮：用户说"我的任务呢？"
  → load session → state.todos = [{id:1, ...}]   ← 自动恢复
  → state 摘要注入 system prompt → LLM 看到当前有 1 个任务
  → LLM 调用 todo.list → 返回任务列表
```

---

## 🔧 工具清单

| 工具 | 类型 | 说明 |
|------|------|------|
| `calculator` | 真实 | 安全数学表达式求值 |
| `web_search` | Mock | 网络搜索（可替换为 Serper/Tavily API） |
| `todo` | 真实 | 待办事项 CRUD，状态持久化在 Session.state |
| `weather` | Mock | 城市天气查询 |
| `ocr` | 真实 | 百度 OCR 文字识别 |
| `load_checker_rules` | 真实 | 加载业务检查规则（长期记忆） |
| `run_checker_tool` | 真实 | 附件检查工具集（10 个子工具） |

---

## 📡 API 接口

| Method | Path | 说明 |
|--------|------|------|
| `POST` | `/chat` | 同步对话（主路径） |
| `POST` | `/task/submit` | 异步工具调用（OCR 等耗时任务） |
| `GET` | `/task/result/{task_id}` | 轮询异步任务结果 |
| `GET` | `/sessions` | 列出所有会话 |
| `GET` | `/history/{sid}` | 获取会话历史 |
| `GET` | `/traces/{sid}` | 获取执行 trace |
| `DELETE` | `/session/{sid}` | 删除会话（触发延时双删） |
| `GET` | `/health` | 健康检查（含 Redis 状态） |

---

## 📂 项目结构

```
mini-agent/
├── mini_agent.py           ← 全部代码（单文件架构）
├── .env.example            ← 环境变量模板
├── .env                    ← 实际配置（gitignore）
├── requirements.txt        ← Python 依赖
├── Dockerfile              ← Docker 镜像
├── docker-compose.yml      ← Agent + Redis 一键部署
├── README.md
├── AI-PROMPT-LOG.md        ← AI Prompt 与问题解决记录
└── data/                   ← 运行时数据（自动创建，gitignore）
    ├── sessions/           ← 会话 JSON 文件
    └── checker/references/ ← 检查规则（长期记忆）
```
