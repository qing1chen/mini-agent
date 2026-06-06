# AI Prompt 与问题解决记录

开发 Mini-Agent 过程中的 Prompt 设计决策、遇到的问题及解决方式。

---

## 一、System Prompt 设计

### 1.1 结构

System prompt 由两部分动态拼接：

```
[固定部分] 角色定义 + 工具使用规范 + 输出格式要求
[动态部分] 当前 Session.state 的序列化摘要
```

固定部分告诉 LLM "你是谁、有哪些工具、怎么调用"，动态部分让 LLM 在每轮推理时都能看到最新的全局状态（如当前待办列表），而不需要从冗长的历史消息中自行推断。

### 1.2 为什么 state 放在 system prompt 而不是 user message

早期尝试过把 state 作为用户消息的前缀注入，但发现两个问题：一是 LLM 容易把 state 信息当作用户的指令去执行，而不是当作背景上下文；二是当历史很长时，state 信息会被挤到 messages 数组中间，注意力衰减导致 LLM 忽略它。放在 system prompt 末尾最稳定，LLM 始终把它当作"当前环境状态"来理解。

### 1.3 工具描述的 Prompt 工程

工具的 function schema 描述需要足够精确，否则 LLM 会编造参数或误用工具。几个关键经验：

**参数用 enum 约束**——比如 `todo` 工具的 `action` 参数，明确列出 `["add", "list", "update", "delete"]`，而不是用自然语言描述"支持增删改查"。LLM 对 enum 的遵循度远高于自然语言描述。

**在 description 中给出调用示例**——对于复杂工具（如 `run_checker_tool` 有 10 个子工具），光列参数不够，需要在 description 里写明典型调用方式，如 `"调用示例: run_checker_tool(sub_tool='collect_files', category='financial')"`。

**错误返回中引导正确行为**——当 LLM 传了错误参数时，工具的错误返回不能只说"参数错误"，要返回可用选项列表和正确示例（见下方 Bug #1、#2 的修复）。

---

## 二、问题与 Bug 修复记录

### Bug #1：`collect_files` 缺少 `category` 参数

**现象**：LLM 调用 `run_checker_tool(sub_tool='collect_files')` 时不传 `category` 参数，工具报错后 LLM 反复用同样的方式重试，陷入死循环直到 `max_steps` 耗尽。

**根因**：工具的 function schema 中 `category` 标注为 required，但 LLM 不知道有哪些合法的 category 值。

**修复**：在 `collect_files` 缺少参数时，返回结构化的引导信息：

```json
{
  "error": "缺少 category 参数",
  "available_categories": ["financial", "legal", "technical", "..."],
  "example": "run_checker_tool(sub_tool='collect_files', category='financial')"
}
```

LLM 拿到 `available_categories` 后能自行选择合适的值，不再重试。

**Prompt 启示**：错误信息本身就是 Prompt 的一部分。告诉 LLM "你错了"没用，告诉它"你错了，正确的选项是 A/B/C"才能引导纠正。

---

### Bug #2：LLM 编造不存在的工具名

**现象**：LLM 偶尔会调用 `search_web`（实际名为 `web_search`）或 `check_attachment`（不存在的工具），导致工具执行失败。

**根因**：LLM 基于语义理解"猜测"工具名，而不是严格引用 function schema 中定义的名字。

**修复**：当 `ToolRegistry.execute()` 找不到工具时，返回完整的可用工具列表：

```json
{
  "error": "工具 'search_web' 不存在",
  "available_tools": ["calculator", "web_search", "todo", "weather", "ocr", "load_checker_rules", "run_checker_tool"],
  "hint": "请使用上述工具名之一"
}
```

**Prompt 启示**：不要假设 LLM 会严格遵守 schema，在运行时也要做防御性校验，并通过返回值引导纠正。

---

### Bug #3：`save_attachment_report` 的 `results` 为空

**现象**：LLM 在附件检查流程中，跳过了中间步骤直接调用 `save_attachment_report`，但此时还没有执行任何检查，所以 `results` 参数为空数组。

**根因**：LLM 把工具名当作"意图"来理解——它知道最终目标是保存报告，就直接跳到最后一步。

**修复**：当 `results` 为空时，不是简单报错，而是返回正确的工作流程提示：

```json
{
  "error": "results 为空，请先完成以下步骤",
  "workflow": [
    "1. load_checker_rules() → 加载检查规则",
    "2. collect_files(category='...') → 收集待检查文件",
    "3. 逐个执行检查子工具 → 收集结果",
    "4. save_attachment_report(results=[...]) → 保存报告"
  ]
}
```

**Prompt 启示**：对于多步骤工作流，在工具的错误返回中内嵌完整流程说明，等于给 LLM 一份"运行时 Prompt"，比在 system prompt 里写长段流程描述更有效，因为它出现在 LLM 实际犯错的上下文中。

---

### Bug #4：连续错误不调整策略

**现象**：LLM 遇到工具调用失败后，用完全相同的参数反复重试，不会主动换一种方式。

**根因**：LLM 缺乏"反思"提示，它不知道自己已经连续失败了多次。

**修复**：在 `AgentRuntime` 中加入连续错误计数器，当 `consecutive_errors >= 3` 时，向 messages 注入一条反思提示：

```json
{
  "role": "system",
  "content": "你已经连续 3 次工具调用失败。请停下来分析错误原因，考虑：1) 是否在使用正确的工具？2) 参数是否正确？3) 是否需要换一种方法？请先说明你的分析，再决定下一步。"
}
```

注入位置是 messages 数组的末尾、下一次 LLM 推理之前，这样 LLM 在生成下一步时会优先看到这条反思要求。

**Prompt 启示**：ReAct 循环中 LLM 自身没有"挫败感"——它不会因为失败而改变策略。需要在运行时检测异常模式并动态注入 Prompt 来触发策略调整。

---

## 三、技术决策记录

### 3.1 为什么选择 ReAct 而不是 Plan-then-Execute

Plan-then-Execute（先规划完整步骤再依次执行）在工具调用可能失败、需要动态调整的场景中不够灵活。ReAct 每一步都经过 LLM 推理，能根据上一步的结果决定下一步，天然支持错误恢复和策略调整。代价是 LLM 调用次数更多，但对于一个以工具调用为核心的 Agent，灵活性比效率更重要。

### 3.2 为什么单文件而不是多模块

这是一个面试/展示项目，单文件的好处是评审者打开一个文件就能看到全貌，不需要在多个文件之间跳转理解调用关系。所有模块通过类和函数隔离，逻辑上仍然是分层的：`LLMClient` → `ToolRegistry` → `AgentRuntime` → `Session` → Web/CLI 入口。

### 3.3 Redis 降级策略

Redis 不是必须的。`Session` 类在初始化时尝试连接 Redis，连接失败则标记 `redis_available = False`，所有读写操作自动降级为纯 JSON 文件模式。这保证了项目在没有 Redis 的环境中也能直接运行，降低了评审者的部署门槛。

### 3.4 缓存一致性：为什么不用消息队列做 Redis/JSON 同步

最初考虑过用消息队列异步同步 Redis 和 JSON（写 Redis → 发消息 → 消费者写 JSON），但这引入了最终一致性问题：如果消费者还没来得及写 JSON 就崩溃了，数据会丢失。对于会话数据这种不能丢的场景，Write-Through（同步双写）更安全——写操作阻塞直到 Redis 和 JSON 都写完才返回，牺牲一点延迟换取强一致性。

延时双删只在 DELETE 场景使用，解决的是"删除后并发回填"的竞态问题，和写入路径的同步策略互不干扰。

### 3.5 长期记忆的按需加载

业务规则等长期知识（如附件检查规则）没有常驻 system prompt，而是通过 `load_checker_rules` 工具按需加载。原因是这些规则文本很长，常驻会占用大量 token 配额；并且不是每轮对话都需要。加载后的内容作为 tool 角色的消息进入 history，后续轮次通过 history 回放自然可见，直到会话结束。

---

## 四、未来改进方向

**History 压缩**——当前 history 完整保留所有消息，长对话会撑满 context window。可以考虑在 history 超过一定长度后，让 LLM 对早期对话做摘要压缩，只保留摘要 + 最近 N 轮原始消息。

**流式输出**——当前 `/chat` 接口等 ReAct 循环完全结束后才返回。可以改为 SSE 流式推送，让用户实时看到 LLM 的思考过程和工具调用状态。

**异步路径与主路径打通**——当前 `/task/submit` 的异步结果只能通过轮询获取，不会自动进入 Session history。可以让 Worker 执行完毕后将结果写入 Session，下一轮对话时 LLM 能看到异步任务的结果。
