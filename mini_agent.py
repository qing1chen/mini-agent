#!/usr/bin/env python3
"""
Mini-Agent v3.0 —— 单文件版

一个不依赖任何 Agent 框架、从零实现核心 runtime 的 LLM Agent。
所有模块（Runtime、Memory、Tools、Web Server）聚合在单文件中。

功能：
  - ReAct 循环（Reason-Act-Observe），统计 Agent Loop 次数
  - 多轮对话 + 跨轮次状态持久化（JSON 文件）
  - 7 个内置工具：calculator / web_search / todo / weather / ocr / load_checker_rules / run_checker_tool
  - 附件完整性检查业务（按需加载规则文件作为长期记忆）
  - 百度 OCR 文字识别
  - 兼容 OpenAI API 格式（DeepSeek / 通义千问 / 智谱 等）
  - 内置 Flask Web 服务 + CLI 双入口

使用方式：
  python mini_agent.py                      # 启动 Web 服务 (默认端口 5000)
  python mini_agent.py --cli                # CLI 交互模式
  python mini_agent.py --cli --session abc  # 恢复已有会话
  python mini_agent.py --cli --list         # 列出所有会话
  python mini_agent.py --cli --quiet        # 安静模式

环境变量（也可写在 .env 文件中）：
  LLM_API_KEY           LLM API 密钥（必填）
  LLM_BASE_URL          API 基础地址
  LLM_MODEL             模型名称
  BAIDU_OCR_API_KEY     百度 OCR API Key
  BAIDU_OCR_SECRET_KEY  百度 OCR Secret Key
  PORT                  Web 服务端口 (默认 5000)
  AGENT_MAX_STEPS       单轮最大 loop 数 (默认 15)
"""

import argparse
import json
import math
import os
import random
import re
import sys
import time
import uuid
import base64
import threading
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional
from pathlib import Path

# ── 加载 .env 文件（不依赖 python-dotenv，手动解析）──────
def _load_dotenv(filepath: str = None):
    """简易 .env 加载器，避免必须安装 python-dotenv。"""
    if filepath is None:
        filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(filepath):
        return
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # 去掉引号
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            # 不覆盖已存在的环境变量
            if key and key not in os.environ:
                os.environ[key] = value

_load_dotenv()


# ╔══════════════════════════════════════════════════════════╗
# ║                     CONFIG 配置区                        ║
# ╚══════════════════════════════════════════════════════════╝

# ── LLM 配置（兼容 OpenAI API 格式）─────────────────────
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "4096"))

# ── 百度 OCR 配置 ────────────────────────────────────────
BAIDU_OCR_API_KEY = os.getenv("BAIDU_OCR_API_KEY", "")
BAIDU_OCR_SECRET_KEY = os.getenv("BAIDU_OCR_SECRET_KEY", "")

# ── Agent 运行时配置 ──────────────────────────────────────
MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "15"))
BASE_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
SESSION_DIR = str(BASE_DIR / "data" / "sessions")

# ── 附件检查业务配置 ──────────────────────────────────────
CHECKER_DIR = str(BASE_DIR / "data" / "checker")
INVOICE_ROOT = os.getenv("INVOICE_ROOT", str(BASE_DIR / "data" / "发票"))
SOURCE_ROOT = os.getenv("SOURCE_ROOT", str(BASE_DIR / "data" / "课题组成员文件"))

# 课题组成员名单
NAME_LIST = [
    "何奕风", "刘冰洁", "刘春震", "唐红", "尚志达", "尹俐", "崔雁杰",
    "巩月茹", "庞贤哲", "张志远", "张航", "曹子恒", "李骏一", "杜娇",
    "杨伊贝", "江豪杰", "池淑梅", "牛少坤", "王子龙", "王明月", "申钰齐",
    "程成", "褚志元", "邵劲超", "郭宗宇", "马震烁", "高树春", "黄耀鹏",
    "齐鹏飞", "陈阿莲",
]

# ── Web 服务配置 ──────────────────────────────────────────
PORT = int(os.getenv("PORT", "5000"))

# ── Redis 配置 ────────────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_SESSION_PREFIX = "agent:session:"
REDIS_SESSION_TTL = int(os.getenv("REDIS_SESSION_TTL", "86400"))  # 24h
REDIS_TASK_QUEUE = "agent:task_queue"
REDIS_RESULT_PREFIX = "agent:result:"

# ── Redis 连接（可选，连接失败降级为纯 JSON 文件模式）───────
_redis_client = None
try:
    import redis
    _redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    _redis_client.ping()
except Exception:
    _redis_client = None

# ── 雪花算法 ID 生成器 ─────────────────────────────────────
MACHINE_ID = int(os.getenv("MACHINE_ID", str(random.randint(0, 1023))))

class SnowflakeIDGenerator:
    """
    雪花算法 ID 生成器。

    64 位结构:
      | 1 bit 符号(0) | 41 bits 时间戳(ms) | 10 bits 机器ID | 12 bits 序列号 |

    特性:
      - 趋势递增，可按 ID 排序得到时间顺序
      - 分布式安全，不同 machine_id 不会冲突
      - 同一毫秒内通过序列号区分，最高 4096 个/ms
    """
    EPOCH = 1700000000000  # 自定义纪元 (2023-11-14 UTC)
    SEQUENCE_BITS = 12
    MACHINE_BITS = 10
    MAX_SEQUENCE = (1 << SEQUENCE_BITS) - 1     # 4095
    MAX_MACHINE_ID = (1 << MACHINE_BITS) - 1    # 1023

    MACHINE_SHIFT = SEQUENCE_BITS               # 12
    TIMESTAMP_SHIFT = SEQUENCE_BITS + MACHINE_BITS  # 22

    def __init__(self, machine_id: int = 0):
        if not 0 <= machine_id <= self.MAX_MACHINE_ID:
            raise ValueError(f"machine_id 必须在 0-{self.MAX_MACHINE_ID} 之间")
        self.machine_id = machine_id
        self._sequence = 0
        self._last_ts = -1
        self._lock = threading.Lock()

    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    def generate(self) -> int:
        with self._lock:
            ts = self._now_ms()
            if ts == self._last_ts:
                self._sequence = (self._sequence + 1) & self.MAX_SEQUENCE
                if self._sequence == 0:
                    # 同一毫秒序列号用尽，自旋等待下一毫秒
                    while ts <= self._last_ts:
                        ts = self._now_ms()
            else:
                self._sequence = 0
            self._last_ts = ts
            return (
                ((ts - self.EPOCH) << self.TIMESTAMP_SHIFT)
                | (self.machine_id << self.MACHINE_SHIFT)
                | self._sequence
            )

    def generate_hex(self) -> str:
        """生成 16 进制字符串 ID（16 字符）。"""
        return format(self.generate(), '016x')

    @classmethod
    def extract_timestamp(cls, snowflake_id: int) -> datetime:
        """从 ID 中提取生成时间。"""
        ts_ms = (snowflake_id >> cls.TIMESTAMP_SHIFT) + cls.EPOCH
        return datetime.fromtimestamp(ts_ms / 1000)


_snowflake = SnowflakeIDGenerator(machine_id=MACHINE_ID)


# ── Redis 消息队列配置 ─────────────────────────────────────
REDIS_SYNC_QUEUE = "agent:session_sync"   # Session 持久化同步队列

# ── 系统提示词 ────────────────────────────────────────────
SYSTEM_PROMPT = """\
你是一个智能助手 Agent。你可以通过调用工具来完成用户任务。

## 行为准则
1. 先思考是否需要工具，如果能直接回答就直接回答。
2. 如果需要工具，选择最合适的工具并提供正确参数。
3. 观察工具返回结果，决定是继续调用其他工具还是给出最终答案。
4. 每一步都要有清晰的推理过程。
5. 如果工具调用失败，尝试其他方案或告知用户。
6. 如果连续多次工具调用失败，请反思策略，不要反复用相同的错误参数重试。

## 附件检查业务
当用户提到「检查附件」「附件是否齐全」「缺少什么附件」「跑一下附件检查」等，
你应该先调用 load_checker_rules 工具加载检查规则（这是你的"长期记忆"），
然后根据加载到的规则指导自己完成附件检查流程。
规则文件是你的业务知识库，每次检查前必须先加载。

## 跨轮次状态
你可以通过 todo 工具管理任务，这些任务在多轮对话间持久保存。
当用户询问之前创建的任务时，请调用 todo 工具查询。

{state_context}
"""


# ╔══════════════════════════════════════════════════════════╗
# ║                   TRACE 执行追踪                         ║
# ╚══════════════════════════════════════════════════════════╝

class StepType(Enum):
    USER_INPUT = "user_input"
    LOOP_START = "loop_start"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    FINAL_ANSWER = "final_answer"
    ERROR = "error"
    MAX_STEPS = "max_steps_reached"


class Colors:
    GREY = "\033[90m"
    BLUE = "\033[94m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"


class Trace:
    """单轮对话的执行追踪，含 token 消耗和延迟指标。"""

    def __init__(self, verbose: bool = True):
        self.steps: list[dict] = []
        self.verbose = verbose
        self.start_time = datetime.now()
        self.loop_count = 0
        self.tool_call_count = 0
        # ── 指标采集 ──
        self._llm_latencies_ms: list[float] = []   # 每次 LLM 调用耗时(ms)
        self._prompt_tokens: int = 0
        self._completion_tokens: int = 0
        self._total_tokens: int = 0
        self._tool_errors: int = 0

    def add(self, step_type: StepType, content: str, metadata: dict = None):
        step = {
            "step": len(self.steps) + 1,
            "type": step_type.value,
            "content": content,
            "metadata": metadata or {},
            "timestamp": datetime.now().isoformat(),
        }
        self.steps.append(step)
        if self.verbose:
            self._print_step(step)

    def _print_step(self, step: dict):
        t = step["type"]
        c = step["content"]
        meta = step.get("metadata", {})

        if t == StepType.USER_INPUT.value:
            print(f"\n{Colors.BOLD}{'─' * 60}{Colors.RESET}")
            print(f"{Colors.BLUE}📥 用户输入:{Colors.RESET} {c}")
            print(f"{Colors.BOLD}{'─' * 60}{Colors.RESET}")
        elif t == StepType.LOOP_START.value:
            print(f"{Colors.GREY}  🔄 Agent Loop #{self.loop_count} / {c}{Colors.RESET}")
        elif t == StepType.TOOL_CALL.value:
            tool_name = meta.get("tool_name", "?")
            tool_input = meta.get("tool_input", {})
            input_str = json.dumps(tool_input, ensure_ascii=False, indent=2)
            print(f"{Colors.YELLOW}  🔧 调用工具: {tool_name}{Colors.RESET}")
            for line in input_str.split("\n"):
                print(f"{Colors.DIM}       {line}{Colors.RESET}")
        elif t == StepType.TOOL_RESULT.value:
            tool_name = meta.get("tool_name", "?")
            success = meta.get("success", True)
            icon = "✅" if success else "❌"
            print(f"{Colors.GREEN}  {icon} {tool_name} 返回:{Colors.RESET}")
            display = c[:500] + "..." if len(c) > 500 else c
            for line in display.split("\n"):
                print(f"{Colors.DIM}       {line}{Colors.RESET}")
        elif t == StepType.FINAL_ANSWER.value:
            print(f"\n{Colors.CYAN}{'─' * 60}{Colors.RESET}")
            print(f"{Colors.BOLD}{Colors.CYAN}🤖 Agent 回答:{Colors.RESET}")
            print(c)
            print(f"{Colors.CYAN}{'─' * 60}{Colors.RESET}")
        elif t == StepType.ERROR.value:
            print(f"{Colors.RED}  ⚠️  错误: {c}{Colors.RESET}")
        elif t == StepType.MAX_STEPS.value:
            print(f"{Colors.RED}  🛑 达到最大步数限制 ({c}){Colors.RESET}")

    def record_llm_call(self, latency_ms: float, usage: dict):
        """记录单次 LLM 调用的耗时和 token 消耗。"""
        self._llm_latencies_ms.append(latency_ms)
        self._prompt_tokens += usage.get("prompt_tokens", 0)
        self._completion_tokens += usage.get("completion_tokens", 0)
        self._total_tokens += usage.get("total_tokens", 0)

    def record_tool_error(self):
        self._tool_errors += 1

    @staticmethod
    def _percentile(data: list[float], p: float) -> float:
        """计算百分位数（最近秩法 nearest-rank）。"""
        if not data:
            return 0.0
        s = sorted(data)
        idx = math.ceil(len(s) * p / 100) - 1
        return s[max(0, min(idx, len(s) - 1))]

    def get_metrics(self) -> dict:
        """
        返回本轮对话的完整指标。

        包含：
          total_time_ms    端到端总耗时
          loops            Agent loop 次数
          llm_calls        LLM API 调用次数
          tool_calls       工具调用次数
          tool_errors      工具调用失败次数
          prompt_tokens    输入 token 总量
          completion_tokens 输出 token 总量
          total_tokens     token 总量
          llm_avg_ms       LLM 调用平均耗时
          llm_p50_ms       LLM 调用 P50 耗时
          llm_p90_ms       LLM 调用 P90 耗时
          llm_p99_ms       LLM 调用 P99 耗时
          llm_max_ms       LLM 调用最大耗时
        """
        total_ms = (datetime.now() - self.start_time).total_seconds() * 1000
        lats = self._llm_latencies_ms
        return {
            "total_time_ms": round(total_ms, 1),
            "loops": self.loop_count,
            "llm_calls": len(lats),
            "tool_calls": self.tool_call_count,
            "tool_errors": self._tool_errors,
            "prompt_tokens": self._prompt_tokens,
            "completion_tokens": self._completion_tokens,
            "total_tokens": self._total_tokens,
            "llm_avg_ms": round(sum(lats) / len(lats), 1) if lats else 0,
            "llm_p50_ms": round(self._percentile(lats, 50), 1),
            "llm_p90_ms": round(self._percentile(lats, 90), 1),
            "llm_p99_ms": round(self._percentile(lats, 99), 1),
            "llm_max_ms": round(max(lats), 1) if lats else 0,
        }

    def summary(self) -> str:
        m = self.get_metrics()
        tool_steps = [s for s in self.steps if s["type"] == StepType.TOOL_CALL.value]
        lines = [
            f"  Agent Loop: {m['loops']} 次  |  LLM 调用: {m['llm_calls']} 次",
            f"  工具调用: {m['tool_calls']} 次 (失败 {m['tool_errors']})",
            f"  Token: {m['prompt_tokens']} in + {m['completion_tokens']} out = {m['total_tokens']} total",
            f"  耗时: {m['total_time_ms']/1000:.1f}s  |  "
            f"LLM avg={m['llm_avg_ms']:.0f}ms  P90={m['llm_p90_ms']:.0f}ms  max={m['llm_max_ms']:.0f}ms",
        ]
        if tool_steps:
            names = [s["metadata"].get("tool_name", "?") for s in tool_steps]
            lines.append(f"  调用链: {' → '.join(names)}")
        return "\n".join(lines)

    def to_dict(self) -> list[dict]:
        return self.steps


# ╔══════════════════════════════════════════════════════════╗
# ║                   SESSION 会话管理                       ║
# ╚══════════════════════════════════════════════════════════╝

class Session:
    """
    单个会话对象。

    短期记忆：messages 列表，持久化为 JSON 文件（data/sessions/<id>.json）。
    长期记忆：state 字典（todo / notes 等），同样持久化在 JSON 中。
    """

    def __init__(self, session_id: str = None):
        self.session_id = session_id or _snowflake.generate_hex()
        self.messages: list[dict] = []
        self.state: dict[str, Any] = {
            "todos": [],
            "notes": {},
        }
        self.metadata = {
            "created_at": datetime.now().isoformat(),
            "last_active": datetime.now().isoformat(),
            "turn_count": 0,
        }
        self.traces: list[list[dict]] = []

    # ── 消息管理 ──────────────────────────────────────────

    def add_user_message(self, content: str):
        self.messages.append({"role": "user", "content": content})

    def add_assistant_message(self, content):
        if isinstance(content, str):
            self.messages.append({"role": "assistant", "content": content})
        elif isinstance(content, dict):
            self.messages.append(content)
        else:
            self.messages.append({"role": "assistant", "content": str(content)})

    def add_tool_result(self, tool_call_id: str, result: str):
        self.messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": result,
        })

    # ── 上下文窗口管理 ────────────────────────────────────

    def get_messages_for_api(self, max_turns: int = 40) -> list[dict]:
        """
        获取发送给 LLM 的消息列表。
        截断时保证不破坏 tool_call → tool_result 的配对关系。
        """
        msgs = self.messages[:]

        if len(msgs) <= max_turns:
            # 确保首条是 user 消息
            while msgs and msgs[0].get("role") not in ("user",):
                msgs = msgs[1:]
            return msgs

        # 从尾部取 max_turns 条，但要保证不截断 tool 对
        msgs = msgs[-max_turns:]

        # 如果第一条是 tool result 或 assistant(with tool_calls)，
        # 需要继续往前找到完整的 tool 调用链
        while msgs and msgs[0].get("role") in ("tool",):
            # tool result 没有对应的 tool_call，丢弃
            msgs = msgs[1:]

        # 确保首条是 user
        while msgs and msgs[0].get("role") not in ("user",):
            msgs = msgs[1:]

        return msgs

    # ── 状态管理 ──────────────────────────────────────────

    def get_state(self, key: str, default=None):
        return self.state.get(key, default)

    def set_state(self, key: str, value: Any):
        self.state[key] = value

    def get_state_summary(self) -> str:
        parts = []
        todos = self.state.get("todos", [])
        if todos:
            parts.append(f"当前待办事项({len(todos)}个):")
            for t in todos:
                status = "✅" if t.get("done") else "⬜"
                parts.append(f"  {status} [{t['id']}] {t['title']}"
                             f" (创建于 {t.get('created_at', '?')})")
        notes = self.state.get("notes", {})
        if notes:
            parts.append(f"\n已保存笔记({len(notes)}条):")
            for k, v in notes.items():
                preview = v[:80] + "..." if len(v) > 80 else v
                parts.append(f"  📝 {k}: {preview}")
        if not parts:
            return "（当前无已保存的状态数据）"
        return "\n".join(parts)

    # ── 持久化（Redis 缓存 + JSON 文件双写）────────────────

    def _to_data(self) -> dict:
        return {
            "session_id": self.session_id,
            "messages": self.messages,
            "state": self.state,
            "metadata": self.metadata,
            "traces": self.traces[-10:],
        }

    @classmethod
    def _from_data(cls, data: dict) -> "Session":
        session = cls(session_id=data["session_id"])
        session.messages = data.get("messages", [])
        session.state = data.get("state", {"todos": [], "notes": {}})
        session.metadata = data.get("metadata", {})
        session.traces = data.get("traces", [])
        return session

    def save(self):
        """
        Redis-Queue 同步策略：
        - Redis 可用时：立即写 Redis 缓存（保证读一致性）
          → 推送 JSON 持久化任务到消息队列（异步、保序）
        - Redis 不可用时：降级为直接写 JSON 文件（原子写入）
        """
        os.makedirs(SESSION_DIR, exist_ok=True)
        self.metadata["last_active"] = datetime.now().isoformat()
        data = self._to_data()
        data_json = json.dumps(data, ensure_ascii=False, indent=2)

        if _redis_client:
            try:
                # 1) 立即写 Redis 缓存，保证后续读取一致性
                cache_key = f"{REDIS_SESSION_PREFIX}{self.session_id}"
                _redis_client.setex(cache_key, REDIS_SESSION_TTL, data_json)
                # 2) 推送到消息队列，由 SyncWorker 异步写 JSON 文件
                msg = json.dumps({
                    "action": "save",
                    "session_id": self.session_id,
                    "data_json": data_json,
                    "timestamp": datetime.now().isoformat(),
                }, ensure_ascii=False)
                _redis_client.rpush(REDIS_SYNC_QUEUE, msg)
                return
            except Exception:
                pass  # Redis 异常，降级到直接写 JSON

        # 降级：无 Redis 时直接原子写 JSON
        self._write_json_direct(data_json)

    def _write_json_direct(self, data_json: str):
        """原子写入 JSON 文件（tmp + rename）。"""
        filepath = os.path.join(SESSION_DIR, f"{self.session_id}.json")
        tmp_path = filepath + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(data_json)
            os.replace(tmp_path, filepath)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    @classmethod
    def load(cls, session_id: str) -> "Session":
        """
        Read-Through：先读 Redis 缓存，命中则直接返回；
        未命中则从 JSON 文件加载并回填 Redis。
        """
        cache_key = f"{REDIS_SESSION_PREFIX}{session_id}"

        # 1) 尝试从 Redis 读取
        if _redis_client:
            try:
                cached = _redis_client.get(cache_key)
                if cached:
                    data = json.loads(cached)
                    return cls._from_data(data)
            except Exception:
                pass  # Redis 读失败，降级到文件

        # 2) 从 JSON 文件加载
        filepath = os.path.join(SESSION_DIR, f"{session_id}.json")
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"会话 {session_id} 不存在")
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        session = cls._from_data(data)

        # 3) 回填 Redis 缓存
        if _redis_client:
            try:
                _redis_client.setex(
                    cache_key, REDIS_SESSION_TTL,
                    json.dumps(data, ensure_ascii=False)
                )
            except Exception:
                pass

        return session


class SessionManager:
    def __init__(self, session_dir: str = SESSION_DIR):
        self.session_dir = session_dir
        os.makedirs(session_dir, exist_ok=True)

    def create(self) -> Session:
        session = Session()
        session.save()
        return session

    def get_or_create(self, session_id: str = None) -> Session:
        """获取已有会话或创建新会话。"""
        if session_id:
            try:
                return Session.load(session_id)
            except FileNotFoundError:
                pass
        return self.create()

    def load(self, session_id: str) -> Session:
        return Session.load(session_id)

    def delete(self, session_id: str) -> bool:
        """
        延时双删（Delayed Double-Delete）—— 通过消息队列保序执行。

        Redis 可用时：
          1. 立即删 Redis 缓存（对外不可见）
          2. 推送 delete 任务到消息队列
          3. SyncWorker 消费：删 JSON → sleep(500ms) → 再删 Redis（防并发写回）

        Redis 不可用时：直接删 JSON 文件。
        """
        filepath = os.path.join(self.session_dir, f"{session_id}.json")
        existed = os.path.exists(filepath)
        cache_key = f"{REDIS_SESSION_PREFIX}{session_id}"

        if _redis_client:
            try:
                # 立即删 Redis 缓存
                _redis_client.delete(cache_key)
                # 推送到消息队列，由 SyncWorker 执行 JSON 删除 + 延时双删
                msg = json.dumps({
                    "action": "delete",
                    "session_id": session_id,
                    "timestamp": datetime.now().isoformat(),
                }, ensure_ascii=False)
                _redis_client.rpush(REDIS_SYNC_QUEUE, msg)
                return existed
            except Exception:
                pass  # Redis 异常，降级

        # 降级：直接删 JSON
        if existed:
            os.remove(filepath)
        return existed

    def list_sessions(self) -> list[dict]:
        sessions = []
        if not os.path.exists(self.session_dir):
            return sessions
        for fname in sorted(os.listdir(self.session_dir)):
            if not fname.endswith(".json"):
                continue
            filepath = os.path.join(self.session_dir, fname)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                sessions.append({
                    "session_id": data["session_id"],
                    "turn_count": data.get("metadata", {}).get("turn_count", 0),
                    "last_active": data.get("metadata", {}).get("last_active", "?"),
                    "created_at": data.get("metadata", {}).get("created_at", "?"),
                    "todos": len(data.get("state", {}).get("todos", [])),
                })
            except (json.JSONDecodeError, KeyError):
                continue
        return sessions


# ╔══════════════════════════════════════════════════════════╗
# ║                   TOOLS 工具定义                         ║
# ╚══════════════════════════════════════════════════════════╝

class Tool:
    def __init__(self, name: str, description: str, parameters: dict, func: Callable):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.func = func

    def execute(self, params: dict, session: Session) -> dict:
        try:
            return self.func(params, session)
        except Exception as e:
            return {"success": False, "error": f"{type(e).__name__}: {str(e)}"}

    def to_api_format(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool):
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def execute(self, name: str, params: dict, session: Session) -> dict:
        tool = self._tools.get(name)
        if not tool:
            return {
                "success": False,
                "error": f"未知工具: {name}",
                "available_tools": list(self._tools.keys()),
                "hint": "请从 available_tools 中选择正确的工具名",
            }
        return tool.execute(params, session)

    def get_api_tools(self) -> list[dict]:
        return [t.to_api_format() for t in self._tools.values()]

    def list_names(self) -> list[str]:
        return list(self._tools.keys())


# ── 工具 1: calculator ────────────────────────────────────

def _calculator(params: dict, session: Session) -> dict:
    expression = params.get("expression", "")
    if not expression:
        return {"success": False, "error": "未提供表达式"}
    safe_names = {
        "abs": abs, "round": round, "min": min, "max": max,
        "sum": sum, "pow": pow, "int": int, "float": float,
        "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos,
        "tan": math.tan, "log": math.log, "log10": math.log10,
        "log2": math.log2, "exp": math.exp, "ceil": math.ceil,
        "floor": math.floor, "pi": math.pi, "e": math.e,
    }
    forbidden = ["import", "exec", "eval", "open", "os.", "sys.",
                 "__", "lambda", "class", "def ", "globals", "locals"]
    expr_lower = expression.lower()
    for f in forbidden:
        if f in expr_lower:
            return {"success": False, "error": f"不允许的操作: {f}"}
    try:
        result = eval(expression, {"__builtins__": {}}, safe_names)
        return {"success": True, "expression": expression, "result": result}
    except Exception as e:
        return {"success": False, "error": f"计算错误: {str(e)}"}

calculator_tool = Tool(
    name="calculator",
    description="计算数学表达式。支持四则运算、幂运算、sqrt/sin/cos/log 等函数。",
    parameters={
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "要计算的数学表达式，如 '2**10' 或 'sqrt(144)'"
            }
        },
        "required": ["expression"],
    },
    func=_calculator,
)


# ── 工具 2: web_search（模拟）─────────────────────────────

_MOCK_SEARCH_DB = {
    "python": [
        {"title": "Python 官方文档", "url": "https://docs.python.org",
         "snippet": "Python 是一种解释型、面向对象的高级编程语言。最新版 3.12 带来了更好的错误提示和性能改进。"},
        {"title": "Python 教程 | 菜鸟教程", "url": "https://www.runoob.com/python3",
         "snippet": "Python3 基础教程，包含完整的语法参考和实例。"},
    ],
    "agent": [
        {"title": "AI Agent 架构设计模式", "url": "https://arxiv.org",
         "snippet": "ReAct 模式是当前主流的 Agent 架构，通过交替进行推理(Reasoning)和行动(Acting)来解决问题。"},
        {"title": "LLM Agent 综述 2024", "url": "https://arxiv.org/abs/2401.xxxxx",
         "snippet": "大语言模型驱动的智能体综述，涵盖规划、记忆、工具使用等核心能力。"},
    ],
    "react": [
        {"title": "ReAct: Synergizing Reasoning and Acting", "url": "https://arxiv.org/abs/2210.03629",
         "snippet": "ReAct 模式让 LLM 交替生成推理链和动作，在问答和决策任务上表现出色。"},
    ],
    "langchain": [
        {"title": "为什么我们不需要 LangChain", "url": "https://blog.example.com",
         "snippet": "从零实现 Agent runtime 只需 200 行代码，框架反而增加了不必要的复杂性。"},
    ],
    "redis": [
        {"title": "Redis 官方文档", "url": "https://redis.io",
         "snippet": "Redis 是一个开源的内存数据结构存储，用作数据库、缓存和消息代理。"},
    ],
    "天气": [
        {"title": "中国天气网", "url": "https://weather.com.cn",
         "snippet": "中国天气网提供全国各城市天气预报查询，包括温度、湿度、风力等。"},
    ],
}

def _web_search(params: dict, session: Session) -> dict:
    query = params.get("query", "").strip()
    if not query:
        return {"success": False, "error": "搜索查询不能为空"}
    num_results = min(params.get("num_results", 3), 5)
    results = []
    query_lower = query.lower()
    for keyword, entries in _MOCK_SEARCH_DB.items():
        if keyword in query_lower or query_lower in keyword:
            results.extend(entries)
    if not results:
        results = [
            {"title": f"搜索结果: {query}", "url": f"https://search.example.com?q={urllib.parse.quote(query)}",
             "snippet": f"关于「{query}」的模拟搜索结果。实际部署时可替换为真实搜索 API。"}
        ]
    return {"success": True, "query": query, "results": results[:num_results],
            "note": "当前为模拟搜索，可替换为 Serper/Tavily 等真实 API"}

web_search_tool = Tool(
    name="web_search",
    description="搜索互联网获取信息。返回相关网页的标题、链接和摘要。（当前为模拟数据）",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "num_results": {"type": "integer", "description": "返回数量，默认3，最大5"},
        },
        "required": ["query"],
    },
    func=_web_search,
)


# ── 工具 3: todo（跨轮次持久化）───────────────────────────

def _todo(params: dict, session: Session) -> dict:
    action = params.get("action", "list")
    todos: list[dict] = session.get_state("todos", [])

    if action == "list":
        if not todos:
            return {"success": True, "message": "当前没有待办事项", "todos": []}
        return {"success": True, "todos": todos, "total": len(todos)}
    elif action == "add":
        title = params.get("title", "").strip()
        if not title:
            return {"success": False, "error": "标题不能为空"}
        max_id = max((t.get("id", 0) for t in todos), default=0)
        new_todo = {
            "id": max_id + 1, "title": title, "done": False,
            "priority": params.get("priority", "normal"),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        todos.append(new_todo)
        session.set_state("todos", todos)
        return {"success": True, "message": f"已创建 #{new_todo['id']}", "todo": new_todo}
    elif action == "toggle":
        todo_id = params.get("id")
        if todo_id is None:
            return {"success": False, "error": "请提供 ID"}
        for t in todos:
            if t["id"] == todo_id:
                t["done"] = not t["done"]
                session.set_state("todos", todos)
                return {"success": True, "message": f"#{todo_id} → {'完成' if t['done'] else '未完成'}"}
        return {"success": False, "error": f"未找到 ID={todo_id}"}
    elif action == "update":
        todo_id = params.get("id")
        if todo_id is None:
            return {"success": False, "error": "请提供 ID"}
        for t in todos:
            if t["id"] == todo_id:
                if params.get("title"):
                    t["title"] = params["title"]
                if params.get("priority"):
                    t["priority"] = params["priority"]
                session.set_state("todos", todos)
                return {"success": True, "message": f"#{todo_id} 已更新", "todo": t}
        return {"success": False, "error": f"未找到 ID={todo_id}"}
    elif action == "delete":
        todo_id = params.get("id")
        if todo_id is None:
            return {"success": False, "error": "请提供 ID"}
        before = len(todos)
        todos = [t for t in todos if t["id"] != todo_id]
        if len(todos) == before:
            return {"success": False, "error": f"未找到 ID={todo_id}"}
        session.set_state("todos", todos)
        return {"success": True, "message": f"#{todo_id} 已删除"}
    else:
        return {"success": False, "error": f"未知操作: {action}，可选: list/add/toggle/update/delete"}

todo_tool = Tool(
    name="todo",
    description="管理待办事项列表。支持 list/add/toggle/update/delete。数据跨轮次持久保存。",
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "add", "toggle", "update", "delete"],
                       "description": "操作类型"},
            "id": {"type": "integer", "description": "待办 ID（toggle/update/delete 时必填）"},
            "title": {"type": "string", "description": "标题（add/update 时使用）"},
            "priority": {"type": "string", "enum": ["high", "normal", "low"],
                         "description": "优先级（默认 normal）"},
        },
        "required": ["action"],
    },
    func=_todo,
)


# ── 工具 4: weather（模拟数据）────────────────────────────

_WEATHER_DATA = {
    "北京": {"temp": 32, "condition": "晴", "humidity": 45, "wind": "北风3级"},
    "上海": {"temp": 28, "condition": "多云", "humidity": 72, "wind": "东南风2级"},
    "济南": {"temp": 34, "condition": "晴", "humidity": 40, "wind": "南风3级"},
    "广州": {"temp": 30, "condition": "阵雨", "humidity": 85, "wind": "南风2级"},
    "深圳": {"temp": 29, "condition": "多云", "humidity": 80, "wind": "东南风3级"},
    "杭州": {"temp": 27, "condition": "阴", "humidity": 68, "wind": "东风2级"},
    "成都": {"temp": 26, "condition": "阴", "humidity": 75, "wind": "微风"},
}

def _weather(params: dict, session: Session) -> dict:
    city = params.get("city", "").strip()
    if not city:
        return {"success": False, "error": "请提供城市名称"}
    for k, v in _WEATHER_DATA.items():
        if city in k or k in city:
            return {"success": True, "city": k, "temperature": f"{v['temp']}°C",
                    "condition": v["condition"], "humidity": f"{v['humidity']}%",
                    "wind": v["wind"], "note": "模拟数据，可替换为真实天气 API"}
    return {"success": True, "city": city, "temperature": f"{random.randint(15, 38)}°C",
            "condition": random.choice(["晴", "多云", "小雨", "阴"]),
            "humidity": f"{random.randint(30, 90)}%",
            "wind": random.choice(["北风2级", "南风3级", "东风1级", "微风"]),
            "note": "未收录城市，返回随机模拟数据"}

weather_tool = Tool(
    name="weather",
    description="查询城市天气信息（当前为模拟数据）。",
    parameters={
        "type": "object",
        "properties": {"city": {"type": "string", "description": "城市名称，如'北京'、'上海'"}},
        "required": ["city"],
    },
    func=_weather,
)


# ── 工具 5: ocr（百度 OCR）────────────────────────────────

_baidu_access_token_cache: dict = {"token": None, "expires_at": 0}

def _get_baidu_access_token() -> str:
    now = time.time()
    if _baidu_access_token_cache["token"] and now < _baidu_access_token_cache["expires_at"]:
        return _baidu_access_token_cache["token"]
    url = (f"https://aip.baidubce.com/oauth/2.0/token"
           f"?grant_type=client_credentials"
           f"&client_id={BAIDU_OCR_API_KEY}&client_secret={BAIDU_OCR_SECRET_KEY}")
    req = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    token = data["access_token"]
    _baidu_access_token_cache["token"] = token
    _baidu_access_token_cache["expires_at"] = now + data.get("expires_in", 2592000) - 60
    return token

def _ocr(params: dict, session: Session) -> dict:
    image_path = params.get("image_path", "")
    image_b64 = params.get("image_base64", "")
    if not image_path and not image_b64:
        return {"success": False, "error": "请提供 image_path 或 image_base64"}
    if not BAIDU_OCR_API_KEY or not BAIDU_OCR_SECRET_KEY:
        return {"success": False, "error": "未配置百度 OCR API Key，请在 .env 中设置 BAIDU_OCR_API_KEY 和 BAIDU_OCR_SECRET_KEY"}
    if image_path and not image_b64:
        image_path = os.path.expanduser(image_path)
        if not os.path.isfile(image_path):
            return {"success": False, "error": f"文件不存在: {image_path}"}
        with open(image_path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("utf-8")
    try:
        access_token = _get_baidu_access_token()
    except Exception as e:
        return {"success": False, "error": f"获取 access_token 失败: {e}"}
    ocr_url = f"https://aip.baidubce.com/rest/2.0/ocr/v1/accurate_basic?access_token={access_token}"
    body = urllib.parse.urlencode({"image": image_b64}).encode("utf-8")
    req = urllib.request.Request(ocr_url, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"success": False, "error": f"OCR 请求失败: {e}"}
    if "error_code" in result:
        return {"success": False, "error": f"百度 OCR 错误 {result['error_code']}: {result.get('error_msg', '')}"}
    words_list = [item["words"] for item in result.get("words_result", [])]
    return {"success": True, "text": "\n".join(words_list), "lines": words_list,
            "total_lines": len(words_list)}

ocr_tool = Tool(
    name="ocr",
    description="图片文字识别（OCR）。传入图片路径或 Base64，返回识别的文本。使用百度 OCR API。",
    parameters={
        "type": "object",
        "properties": {
            "image_path": {"type": "string", "description": "图片文件路径"},
            "image_base64": {"type": "string", "description": "图片 Base64 编码"},
        },
        "required": [],
    },
    func=_ocr,
)


# ── 工具 6: load_checker_rules（加载附件检查规则 = 长期记忆）──

def _load_checker_rules(params: dict, session: Session) -> dict:
    """加载附件检查规则文件到上下文中。"""
    category = params.get("category", "").strip()
    refs_dir = os.path.join(CHECKER_DIR, "references")

    if not os.path.isdir(refs_dir):
        return {
            "success": False,
            "error": f"规则目录不存在: {refs_dir}",
            "hint": "请将 rules_common.md 等规则文件放入 data/checker/references/ 目录",
        }

    loaded = {}

    # 加载通用规则（始终加载）
    common_path = os.path.join(refs_dir, "rules_common.md")
    if os.path.isfile(common_path):
        with open(common_path, "r", encoding="utf-8") as f:
            loaded["rules_common"] = f.read()

    # 加载工具参考（首次加载时）
    if not category or category == "all":
        tools_path = os.path.join(refs_dir, "tools.md")
        if os.path.isfile(tools_path):
            with open(tools_path, "r", encoding="utf-8") as f:
                loaded["tools_reference"] = f.read()

    # 加载类别专用规则
    if category and category != "all":
        cat_name = category if category.startswith("rules_") else f"rules_{category}"
        cat_path = os.path.join(refs_dir, f"{cat_name}.md")
        if os.path.isfile(cat_path):
            with open(cat_path, "r", encoding="utf-8") as f:
                loaded[cat_name] = f.read()
        else:
            available = [f for f in os.listdir(refs_dir) if f.endswith(".md")]
            return {
                "success": False,
                "error": f"未找到规则文件: {cat_name}.md",
                "available_files": available,
                "hint": f"可用类别: {', '.join(f.replace('rules_','').replace('.md','') for f in available if f.startswith('rules_'))}",
            }

    if not loaded:
        return {"success": False, "error": "未找到任何规则文件"}

    parts = []
    for name, content in loaded.items():
        parts.append(f"═══ {name} ═══\n{content}")

    return {
        "success": True,
        "loaded_files": list(loaded.keys()),
        "total_chars": sum(len(v) for v in loaded.values()),
        "rules_content": "\n\n---\n\n".join(parts),
    }

load_checker_rules_tool = Tool(
    name="load_checker_rules",
    description=(
        "加载附件检查的业务规则（长期记忆）。"
        "当用户要求检查附件时必须先调用。"
        "category 留空加载通用规则+工具参考，传入类别名（如'打车'/'出差'/'材料'）加载专用规则。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "类别名（打车/出差/加班餐/材料等），留空加载通用规则",
            },
        },
        "required": [],
    },
    func=_load_checker_rules,
)


# ── 工具 7: run_checker_tool（附件检查工具集）─────────────

import sqlite3
import shutil

_used_source_attachments: set = set()


def _extract_person(filename: str) -> str:
    for n in NAME_LIST:
        if n in filename:
            return n
    return ""


def _checker_get_config(args: dict) -> dict:
    return {
        "success": True,
        "name_list": NAME_LIST,
        "source_root": SOURCE_ROOT,
        "invoice_root": INVOICE_ROOT,
        "categories": ["打车", "出差", "加班餐", "打印", "快递", "材料"],
        "checker_dir": CHECKER_DIR,
    }


def _checker_get_ocr_names(args: dict) -> dict:
    db_path = os.path.join(CHECKER_DIR, "invoices.db")
    if not os.path.isfile(db_path):
        return {"success": False, "ocr_names": [], "error": f"数据库不存在: {db_path}"}
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r["name"] for r in cur.fetchall()]
        names = []
        for tbl in tables:
            cur = conn.execute(f"PRAGMA table_info([{tbl}])")
            cols = [r["name"] for r in cur.fetchall()]
            for col in ["旧文件名", "filename", "name", "old_filename"]:
                if col in cols:
                    cur = conn.execute(f"SELECT DISTINCT [{col}] FROM [{tbl}] WHERE [{col}] IS NOT NULL")
                    names = [r[0] for r in cur.fetchall() if r[0]]
                    if names:
                        break
            if names:
                break
        conn.close()
        return {"success": True, "ocr_names": names, "count": len(names)}
    except Exception as e:
        return {"success": False, "ocr_names": [], "error": str(e)}


def _checker_collect_files(args: dict) -> dict:
    category = (args.get("category", "")
                or args.get("filter", "")
                or args.get("type", "")
                or args.get("cat", ""))
    if not category:
        return {
            "success": False,
            "error": "请提供 category 参数",
            "available_categories": ["打车", "出差", "加班餐", "打印", "快递", "材料"],
            "hint": "例如: tool_name='collect_files', tool_args={'category': '打车'}",
        }
    if not INVOICE_ROOT or not os.path.isdir(INVOICE_ROOT):
        return {"success": False, "error": f"发票根目录不存在: {INVOICE_ROOT}。请设置环境变量 INVOICE_ROOT。"}
    cat_dir = os.path.join(INVOICE_ROOT, category)
    if not os.path.isdir(cat_dir):
        available = [d for d in os.listdir(INVOICE_ROOT) if os.path.isdir(os.path.join(INVOICE_ROOT, d))]
        return {"success": False, "error": f"类别目录不存在: {cat_dir}",
                "available_categories": available}
    files = []
    for root, dirs, fnames in os.walk(cat_dir):
        for fname in fnames:
            full_path = os.path.join(root, fname)
            rel_parent = os.path.relpath(root, cat_dir)
            files.append({
                "name": fname,
                "full_path": full_path,
                "parent": rel_parent,
                "person": _extract_person(fname),
            })
    return {"success": True, "category": category, "files": files, "total": len(files)}


def _checker_collect_source_candidates(args: dict) -> dict:
    person = args.get("person", "")
    if not person:
        return {"success": False, "error": "请提供 person 参数", "candidates": []}
    if not SOURCE_ROOT or not os.path.isdir(SOURCE_ROOT):
        return {"success": False, "error": f"来源目录不存在: {SOURCE_ROOT}", "candidates": []}
    person_dir = os.path.join(SOURCE_ROOT, person)
    if not os.path.isdir(person_dir):
        return {"success": True, "person": person, "candidates": [],
                "message": f"未找到 {person} 的目录: {person_dir}"}
    ocr_data = _checker_get_ocr_names({})
    ocr_set = set(ocr_data.get("ocr_names", []))
    candidates = []
    for root, dirs, fnames in os.walk(person_dir):
        for fname in fnames:
            full_path = os.path.join(root, fname)
            if fname in ocr_set:
                continue
            if full_path in _used_source_attachments:
                continue
            candidates.append({
                "name": fname,
                "full_path": full_path,
                "parent": os.path.relpath(root, SOURCE_ROOT),
                "person": person,
            })
    return {"success": True, "person": person, "candidates": candidates, "total": len(candidates)}


def _checker_lookup_invoice_details(args: dict) -> dict:
    filename = (args.get("filename", "")
                or args.get("invoice_name", "")
                or args.get("name", "")
                or args.get("file", ""))
    if not filename:
        return {"success": False, "error": "请提供 filename 参数（发票文件名，如 '尚志达+100.0+30uF电容.pdf'）"}
    db_path = os.path.join(CHECKER_DIR, "invoices.db")
    if not os.path.isfile(db_path):
        return {"success": False, "error": f"数据库不存在: {db_path}"}
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r["name"] for r in cur.fetchall()]
        result = {}
        for tbl in tables:
            cur = conn.execute(f"PRAGMA table_info([{tbl}])")
            cols = [r["name"] for r in cur.fetchall()]
            for col in ["旧文件名", "filename", "name", "old_filename"]:
                if col in cols:
                    cur = conn.execute(f"SELECT * FROM [{tbl}] WHERE [{col}] = ?", (filename,))
                    row = cur.fetchone()
                    if row:
                        result = dict(row)
                        break
            if result:
                break
        conn.close()
        if not result:
            return {"success": False, "error": f"未找到发票: {filename}"}
        return {"success": True, "invoice": result}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _checker_extract_attachment_text(args: dict) -> dict:
    filepath = args.get("filepath", "") or args.get("full_path", "")
    # ── BugFix: LLM 常用 filename 而非 filepath，自动在已知目录中搜索 ──
    if not filepath:
        filename = args.get("filename", "") or args.get("name", "")
        if filename:
            for search_root in [INVOICE_ROOT, SOURCE_ROOT]:
                if search_root and os.path.isdir(search_root):
                    for root, dirs, fnames in os.walk(search_root):
                        if filename in fnames:
                            filepath = os.path.join(root, filename)
                            break
                if filepath:
                    break
    if not filepath:
        return {"success": False, "text": None,
                "error": "请提供 filepath 或 full_path 参数（也可提供 filename，将自动搜索）"}
    if not os.path.isfile(filepath):
        return {"success": False, "text": None, "error": f"文件不存在: {filepath}"}
    ext = os.path.splitext(filepath)[1].lower()
    if ext in (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tiff"):
        result = _ocr({"image_path": filepath}, None)
        if result.get("success"):
            return {"success": True, "text": result["text"], "method": "image_ocr"}
        return {"success": False, "text": None, "error": result.get("error", "OCR 失败")}
    if ext == ".pdf":
        try:
            import fitz
            doc = fitz.open(filepath)
            parts = []
            for i, page in enumerate(doc):
                if i >= 3:
                    parts.append(f"（共 {len(doc)} 页，仅识别前 3 页）")
                    break
                pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
                img_b64 = base64.b64encode(pix.tobytes("png")).decode("utf-8")
                r = _ocr({"image_base64": img_b64}, None)
                if r.get("success"):
                    parts.append(r["text"])
            doc.close()
            if parts:
                return {"success": True, "text": "\n".join(parts), "method": "pdf_ocr"}
            return {"success": False, "text": None, "error": "PDF 页面 OCR 均失败"}
        except ImportError:
            return {"success": False, "text": None, "error": "需要安装 pymupdf: pip install pymupdf"}
        except Exception as e:
            return {"success": False, "text": None, "error": f"PDF OCR 失败: {e}"}
    if ext == ".docx":
        try:
            from docx import Document as DocxDocument
            doc = DocxDocument(filepath)
            parts = [p.text for p in doc.paragraphs if p.text.strip()]
            return {"success": True, "text": "\n".join(parts), "method": "docx_text"}
        except ImportError:
            return {"success": False, "text": None, "error": "需要安装 python-docx: pip install python-docx"}
        except Exception as e:
            return {"success": False, "text": None, "error": f"读取 docx 失败: {e}"}
    return {"success": False, "text": None, "error": f"不支持的文件类型: {ext}"}


def _checker_copy_file(args: dict) -> dict:
    src = args.get("src", "")
    dst_dir = args.get("dst_dir", "")
    if not src or not os.path.isfile(src):
        return {"success": False, "error": f"源文件不存在: {src}"}
    if not dst_dir:
        return {"success": False, "error": "请提供 dst_dir 参数"}
    os.makedirs(dst_dir, exist_ok=True)
    dst_path = os.path.join(dst_dir, os.path.basename(src))
    shutil.copy2(src, dst_path)
    if args.get("mark_used", True):
        _used_source_attachments.add(src)
    return {"success": True, "dst_path": dst_path, "dst_name": os.path.basename(dst_path)}


# 住宿费标准表
_ACCOMMODATION_STANDARDS = [
    {"province": "北京市",             "cat1": 1100, "cat2": 700, "cat3": 550, "peak_period": "",               "peak1": 0,    "peak2": 0,   "peak3": 0},
    {"province": "上海市",             "cat1": 1100, "cat2": 700, "cat3": 550, "peak_period": "",               "peak1": 0,    "peak2": 0,   "peak3": 0},
    {"province": "三亚市",             "cat1": 1100, "cat2": 700, "cat3": 550, "peak_period": "10-4月",         "peak1": 1200, "peak2": 800, "peak3": 600},
    {"province": "江苏省",             "cat1": 900,  "cat2": 600, "cat3": 500, "peak_period": "",               "peak1": 0,    "peak2": 0,   "peak3": 0},
    {"province": "浙江省",             "cat1": 900,  "cat2": 600, "cat3": 500, "peak_period": "",               "peak1": 0,    "peak2": 0,   "peak3": 0},
    {"province": "福建省",             "cat1": 900,  "cat2": 600, "cat3": 500, "peak_period": "",               "peak1": 0,    "peak2": 0,   "peak3": 0},
    {"province": "河南省",             "cat1": 900,  "cat2": 600, "cat3": 500, "peak_period": "4-5月上旬(洛阳市)", "peak1": 1200, "peak2": 780, "peak3": 650},
    {"province": "广东省",             "cat1": 900,  "cat2": 600, "cat3": 500, "peak_period": "",               "peak1": 0,    "peak2": 0,   "peak3": 0},
    {"province": "四川省",             "cat1": 900,  "cat2": 600, "cat3": 500, "peak_period": "",               "peak1": 0,    "peak2": 0,   "peak3": 0},
    {"province": "云南省",             "cat1": 900,  "cat2": 600, "cat3": 500, "peak_period": "",               "peak1": 0,    "peak2": 0,   "peak3": 0},
    {"province": "天津市",             "cat1": 900,  "cat2": 600, "cat3": 500, "peak_period": "",               "peak1": 0,    "peak2": 0,   "peak3": 0},
    {"province": "河北省",             "cat1": 800,  "cat2": 500, "cat3": 400, "peak_period": "7-9月、11-3月",  "peak1": 1200, "peak2": 750, "peak3": 600},
    {"province": "山西省",             "cat1": 800,  "cat2": 500, "cat3": 400, "peak_period": "",               "peak1": 0,    "peak2": 0,   "peak3": 0},
    {"province": "内蒙古",             "cat1": 800,  "cat2": 500, "cat3": 400, "peak_period": "7-10月",         "peak1": 1200, "peak2": 750, "peak3": 600},
    {"province": "辽宁省",             "cat1": 800,  "cat2": 500, "cat3": 400, "peak_period": "7-9月",          "peak1": 960,  "peak2": 600, "peak3": 480},
    {"province": "吉林省",             "cat1": 800,  "cat2": 500, "cat3": 400, "peak_period": "7-9月",          "peak1": 960,  "peak2": 600, "peak3": 480},
    {"province": "黑龙江省",           "cat1": 800,  "cat2": 500, "cat3": 400, "peak_period": "6-9月",          "peak1": 960,  "peak2": 600, "peak3": 480},
    {"province": "安徽省",             "cat1": 800,  "cat2": 500, "cat3": 400, "peak_period": "",               "peak1": 0,    "peak2": 0,   "peak3": 0},
    {"province": "江西省",             "cat1": 800,  "cat2": 500, "cat3": 400, "peak_period": "",               "peak1": 0,    "peak2": 0,   "peak3": 0},
    {"province": "山东省",             "cat1": 800,  "cat2": 500, "cat3": 400, "peak_period": "7-9月",          "peak1": 960,  "peak2": 600, "peak3": 480},
    {"province": "湖北省",             "cat1": 800,  "cat2": 500, "cat3": 400, "peak_period": "",               "peak1": 0,    "peak2": 0,   "peak3": 0},
    {"province": "湖南省",             "cat1": 800,  "cat2": 500, "cat3": 400, "peak_period": "",               "peak1": 0,    "peak2": 0,   "peak3": 0},
    {"province": "广西",               "cat1": 800,  "cat2": 500, "cat3": 400, "peak_period": "1-2月、7-9月",   "peak1": 1040, "peak2": 750, "peak3": 520},
    {"province": "海南省(不含三亚市)", "cat1": 800,  "cat2": 500, "cat3": 400, "peak_period": "11-3月",         "peak1": 1040, "peak2": 750, "peak3": 520},
    {"province": "重庆市",             "cat1": 800,  "cat2": 500, "cat3": 400, "peak_period": "",               "peak1": 0,    "peak2": 0,   "peak3": 0},
    {"province": "贵州省",             "cat1": 800,  "cat2": 500, "cat3": 400, "peak_period": "",               "peak1": 0,    "peak2": 0,   "peak3": 0},
    {"province": "西藏",               "cat1": 800,  "cat2": 500, "cat3": 400, "peak_period": "6-9月",          "peak1": 1200, "peak2": 750, "peak3": 600},
    {"province": "陕西省",             "cat1": 800,  "cat2": 500, "cat3": 400, "peak_period": "",               "peak1": 0,    "peak2": 0,   "peak3": 0},
    {"province": "甘肃省",             "cat1": 800,  "cat2": 500, "cat3": 400, "peak_period": "",               "peak1": 0,    "peak2": 0,   "peak3": 0},
    {"province": "青海省",             "cat1": 800,  "cat2": 500, "cat3": 400, "peak_period": "5-9月",          "peak1": 1200, "peak2": 750, "peak3": 600},
    {"province": "宁夏",               "cat1": 800,  "cat2": 500, "cat3": 400, "peak_period": "",               "peak1": 0,    "peak2": 0,   "peak3": 0},
    {"province": "新疆",               "cat1": 800,  "cat2": 500, "cat3": 400, "peak_period": "",               "peak1": 0,    "peak2": 0,   "peak3": 0},
]

_SPECIAL_PERSONS = {
    "陈阿莲": {"title": "二级教授", "train_seat": "一等座", "accommodation_cat": 1},
}

_CITY_TO_PROVINCE = {
    "郑州": "河南省", "洛阳": "河南省", "开封": "河南省",
    "济南": "山东省", "青岛": "山东省", "烟台": "山东省", "威海": "山东省",
    "南京": "江苏省", "苏州": "江苏省", "无锡": "江苏省",
    "杭州": "浙江省", "宁波": "浙江省", "温州": "浙江省",
    "广州": "广东省", "深圳": "广东省", "珠海": "广东省",
    "成都": "四川省", "重庆": "重庆市", "武汉": "湖北省",
    "长沙": "湖南省", "西安": "陕西省", "昆明": "云南省",
    "福州": "福建省", "厦门": "福建省", "合肥": "安徽省",
    "南昌": "江西省", "哈尔滨": "黑龙江省", "长春": "吉林省",
    "沈阳": "辽宁省", "大连": "辽宁省", "太原": "山西省",
    "石家庄": "河北省", "呼和浩特": "内蒙古", "南宁": "广西",
    "海口": "海南省(不含三亚市)", "三亚": "三亚市", "贵阳": "贵州省",
    "拉萨": "西藏", "兰州": "甘肃省", "西宁": "青海省",
    "银川": "宁夏", "乌鲁木齐": "新疆", "天津": "天津市",
}


def _checker_lookup_accommodation(args: dict) -> dict:
    province = args.get("province", "")
    person = args.get("person", "")
    month = args.get("month")
    if not province:
        return {"success": False, "error": "请提供 province 参数"}

    query = province.strip()
    mapped = _CITY_TO_PROVINCE.get(query.replace("市", ""))
    if mapped:
        query = mapped

    matched = None
    for std in _ACCOMMODATION_STANDARDS:
        if std["province"] == query:
            matched = std
            break
    if not matched:
        q = query.replace("省", "").replace("市", "").replace("自治区", "")
        for std in _ACCOMMODATION_STANDARDS:
            p = std["province"].replace("省", "").replace("市", "").replace("自治区", "").replace("(不含三亚市)", "")
            if q in p or p in q:
                matched = std
                break

    if not matched:
        return {"success": False, "error": f"未找到「{province}」的住宿标准，请尝试传入省份名（如'河南省'而非城市名）"}

    special = _SPECIAL_PERSONS.get(person)
    cat_level = special["accommodation_cat"] if special else 3
    cat_labels = {1: "一类", 2: "二类", 3: "三类"}
    is_peak = False
    if month and matched["peak_period"]:
        for part in re.split(r'[、，]', matched["peak_period"]):
            part = re.sub(r'[（(][^）)]*[）)]', '', part).replace("月", "").strip()
            m = re.match(r'(\d+)\s*[-–]\s*(\d+)', part)
            if m:
                s, e = int(m.group(1)), int(m.group(2))
                months = list(range(s, e + 1)) if s <= e else list(range(s, 13)) + list(range(1, e + 1))
                if month in months:
                    is_peak = True
                    break
    limit = matched[f"peak{cat_level}"] if is_peak and matched[f"peak{cat_level}"] > 0 else matched[f"cat{cat_level}"]
    return {
        "success": True,
        "province": matched["province"], "category_label": cat_labels[cat_level],
        "is_peak": is_peak, "limit": limit,
        "special_person": f"{person}（{special['title']}）" if special else None,
    }


def _checker_check_seat(args: dict) -> dict:
    person = args.get("person", "")
    seat_info = args.get("seat_info", "")
    transport_type = args.get("transport_type", "")
    special = _SPECIAL_PERSONS.get(person)
    if transport_type in ("高铁", "动车", "火车"):
        standard = special.get("train_seat", "二等座") if special else "二等座"
        seat_rank = {"二等座": 1, "一等座": 2, "商务座": 3}
        passed = seat_rank.get(seat_info, 0) <= seat_rank.get(standard, 1)
    elif transport_type == "飞机":
        standard = "经济舱"
        passed = seat_info in ("经济舱",)
    else:
        standard = "未知"
        passed = True
    return {
        "success": True,
        "person": person, "seat_info": seat_info, "standard": standard,
        "pass": passed,
        "message": "符合标准" if passed else f"超标: 实际{seat_info}，标准{standard}",
    }


def _checker_save_report(args: dict) -> dict:
    # ── BugFix: 兼容 LLM 的多种传参结构 ──
    # 情况1: args = {"results": [...]}                  (标准)
    # 情况2: args = {"report": {"results": [...], ...}} (LLM 嵌套)
    # 情况3: args = {"report": [...]}                   (LLM 直接列表)
    results = args.get("results", [])
    if not results:
        report = args.get("report", {})
        if isinstance(report, list):
            results = report
        elif isinstance(report, dict):
            results = report.get("results", [])
    if not results:
        return {
            "success": False,
            "error": "results 为空，请先使用 collect_files 收集文件并检查后再保存报告",
            "hint": "工作流: get_config → collect_files(category=...) → 逐个检查 → save_attachment_report(results=[...])",
        }

    # ── BugFix: LLM 用英文/别名字段时，自动映射到数据库中文列名 ──
    _FIELD_ALIASES = {
        "旧文件名": ["旧文件名", "filename", "name", "file", "invoice_name", "fname"],
        "附件状态": ["附件状态", "status", "check_result", "result", "check_status"],
        "缺少类型": ["缺少类型", "missing_type", "missing_attachments", "missing", "缺少附件"],
        "匹配附件": ["匹配附件", "matched", "matched_attachment", "match"],
        "附件路径": ["附件路径", "path", "attachment_path", "file_path"],
        "生成文件": ["生成文件", "generated", "generated_file", "output_file"],
        "校验详情": ["校验详情", "detail", "details", "check_detail", "check_result", "reason", "备注"],
        "附件类别": ["附件类别", "category", "type", "attachment_type"],
    }
    def _map_record(r: dict) -> dict:
        mapped = {}
        for db_col, aliases in _FIELD_ALIASES.items():
            val = ""
            for alias in aliases:
                if alias in r and r[alias]:
                    val = str(r[alias])
                    break
            mapped[db_col] = val
        # 如果 校验详情 为空但有其他描述字段，做兜底拼接
        if not mapped["校验详情"]:
            parts = []
            for k in ("person", "人员", "amount", "金额", "check_result", "result"):
                if k in r and r[k]:
                    parts.append(f"{k}: {r[k]}")
            if parts:
                mapped["校验详情"] = "; ".join(parts)
        return mapped

    db_path = os.path.join(CHECKER_DIR, "records.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("""CREATE TABLE IF NOT EXISTS attachment_report (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            旧文件名 TEXT, 附件状态 TEXT, 缺少类型 TEXT,
            匹配附件 TEXT, 附件路径 TEXT, 生成文件 TEXT,
            校验详情 TEXT, 附件类别 TEXT, created_at TEXT
        )""")
        for r in results:
            m = _map_record(r)
            conn.execute(
                "INSERT INTO attachment_report (旧文件名,附件状态,缺少类型,匹配附件,附件路径,生成文件,校验详情,附件类别,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (m["旧文件名"], m["附件状态"], m["缺少类型"],
                 m["匹配附件"], m["附件路径"], m["生成文件"],
                 m["校验详情"], m["附件类别"], datetime.now().isoformat()),
            )
        conn.commit()
        conn.close()
        return {"success": True, "records_written": len(results)}
    except Exception as e:
        return {"success": False, "error": str(e)}


_CHECKER_TOOL_ROUTER = {
    "get_config": _checker_get_config,
    "get_ocr_names": _checker_get_ocr_names,
    "collect_files": _checker_collect_files,
    "collect_source_candidates": _checker_collect_source_candidates,
    "lookup_invoice_details": _checker_lookup_invoice_details,
    "extract_attachment_text": _checker_extract_attachment_text,
    "copy_file": _checker_copy_file,
    "lookup_accommodation_standard": _checker_lookup_accommodation,
    "check_seat_class": _checker_check_seat,
    "save_attachment_report": _checker_save_report,
}


def _run_checker_tool(params: dict, session: Session) -> dict:
    tool_name = params.get("tool_name", "")
    tool_args = params.get("tool_args", {})
    # ── BugFix: LLM 有时将 tool_args 序列化为 JSON 字符串，需要解析 ──
    if isinstance(tool_args, str):
        try:
            tool_args = json.loads(tool_args)
        except (json.JSONDecodeError, TypeError):
            tool_args = {}
    if not isinstance(tool_args, dict):
        tool_args = {}
    if not tool_name:
        return {
            "success": False,
            "error": "请提供 tool_name 参数",
            "available_tools": list(_CHECKER_TOOL_ROUTER.keys()),
        }
    handler = _CHECKER_TOOL_ROUTER.get(tool_name)
    if not handler:
        return {
            "success": False,
            "error": f"未知工具: {tool_name}",
            "available_tools": list(_CHECKER_TOOL_ROUTER.keys()),
            "hint": "请检查工具名拼写是否正确",
        }
    try:
        result = handler(tool_args)
        # 确保返回的 dict 有 success 字段
        if isinstance(result, dict) and "success" not in result:
            result["success"] = True
        return result
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {e}"}

run_checker_tool_tool = Tool(
    name="run_checker_tool",
    description=(
        "调用附件检查子工具。传入 tool_name 和 tool_args。"
        "需要先用 load_checker_rules 加载规则了解业务流程。\n"
        "各子工具及其参数：\n"
        "1. get_config: 无参数。返回 name_list/source_root/invoice_root/categories 等配置。\n"
        "2. get_ocr_names: 无参数。返回 invoices.db 中已有的文件名列表。\n"
        "3. collect_files: {category: '材料'|'打车'|'出差'|'加班餐'|'打印'|'快递'}。收集该类别下所有发票文件，返回 name/full_path/person。\n"
        "4. collect_source_candidates: {person: '尚志达'}。搜索该人员目录下可用的附件候选文件。\n"
        "5. lookup_invoice_details: {filename: '尚志达+100.0+30uF电容.pdf'}。从 invoices.db 查询发票详情（商品名称/金额/开票日期等）。\n"
        "6. extract_attachment_text: {filepath: '/full/path/to/file.pdf'} 或 {filename: 'xxx.pdf'}（自动搜索）。提取文件文字内容（PDF用OCR，图片用OCR，docx直接读取）。\n"
        "7. copy_file: {src: '源文件完整路径', dst_dir: '目标目录路径'}。复制附件到指定目录。\n"
        "8. lookup_accommodation_standard: {province: '山东省'|'济南', person: '姓名'(可选), month: 7(可选)}。查住宿费标准。\n"
        "9. check_seat_class: {person: '姓名', seat_info: '二等座', transport_type: '高铁'|'飞机'}。校验座位等级是否符合标准。\n"
        "10. save_attachment_report: {results: [{旧文件名:'xxx.pdf', 附件状态:'齐全'|'缺失', 缺少类型:'转账截图', 匹配附件:'', 附件路径:'', 校验详情:'详细说明', 附件类别:'材料'},...]}。保存检查结果到 records.db。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "tool_name": {
                "type": "string",
                "description": "工具函数名",
                "enum": list(_CHECKER_TOOL_ROUTER.keys()),
            },
            "tool_args": {
                "type": "object",
                "description": "工具参数（JSON 对象），具体字段见上方各子工具说明",
            },
        },
        "required": ["tool_name"],
    },
    func=_run_checker_tool,
)


# ── 工具注册 ──────────────────────────────────────────────

def create_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(calculator_tool)
    registry.register(web_search_tool)
    registry.register(todo_tool)
    registry.register(weather_tool)
    registry.register(ocr_tool)
    registry.register(load_checker_rules_tool)
    registry.register(run_checker_tool_tool)
    return registry


# ╔══════════════════════════════════════════════════════════╗
# ║         REDIS 消息队列（异步工具任务处理）                ║
# ╚══════════════════════════════════════════════════════════╝

class TaskQueue:
    """
    基于 Redis List 的消息队列，用于异步工具任务处理。

    架构：
      Producer (Web API) → RPUSH task_queue → Consumer (Worker 线程)
                                                  ↓
                                            执行工具 → SETEX result:<task_id>

    适用场景：长耗时的 OCR、文件扫描等工具调用。
    对于同步场景（CLI 模式），直接调用 ToolRegistry.execute() 不经过队列。
    """

    def __init__(self, tools: ToolRegistry, session_manager: "SessionManager"):
        self.tools = tools
        self.sm = session_manager
        self._running = False
        self._worker_thread: Optional[threading.Thread] = None

    def submit(self, task_id: str, session_id: str,
               tool_name: str, tool_input: dict, timeout: int = 300) -> bool:
        """提交异步工具任务到 Redis 队列。"""
        if not _redis_client:
            return False
        task = json.dumps({
            "task_id": task_id,
            "session_id": session_id,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "submitted_at": datetime.now().isoformat(),
            "timeout": timeout,
        }, ensure_ascii=False)
        try:
            _redis_client.rpush(REDIS_TASK_QUEUE, task)
            return True
        except Exception:
            return False

    def get_result(self, task_id: str, timeout: int = 30) -> Optional[dict]:
        """轮询获取异步任务结果。"""
        if not _redis_client:
            return None
        result_key = f"{REDIS_RESULT_PREFIX}{task_id}"
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                result = _redis_client.get(result_key)
                if result:
                    _redis_client.delete(result_key)
                    return json.loads(result)
            except Exception:
                pass
            time.sleep(0.5)
        return {"success": False, "error": f"任务 {task_id} 超时 ({timeout}s)"}

    def start_worker(self):
        """启动后台 Worker 线程，从队列消费任务。"""
        if not _redis_client or self._running:
            return
        self._running = True
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()

    def stop_worker(self):
        self._running = False

    def _worker_loop(self):
        """Worker 主循环：BLPOP 阻塞等待任务 → 执行 → 写结果。"""
        while self._running:
            try:
                # BLPOP 阻塞 5 秒等待新任务
                item = _redis_client.blpop(REDIS_TASK_QUEUE, timeout=5)
                if not item:
                    continue
                _, task_json = item
                task = json.loads(task_json)

                task_id = task["task_id"]
                session_id = task["session_id"]
                tool_name = task["tool_name"]
                tool_input = task["tool_input"]

                # 加载 session 执行工具
                try:
                    session = self.sm.load(session_id)
                except FileNotFoundError:
                    session = Session(session_id)

                result = self.tools.execute(tool_name, tool_input, session)
                session.save()

                # 写结果到 Redis，设 TTL 防止结果堆积
                result_key = f"{REDIS_RESULT_PREFIX}{task_id}"
                _redis_client.setex(
                    result_key,
                    task.get("timeout", 300),
                    json.dumps(result, ensure_ascii=False, default=str)
                )

            except Exception as e:
                # Worker 不能因单个任务崩溃
                if self._running:
                    time.sleep(1)


# ╔══════════════════════════════════════════════════════════╗
# ║   REDIS 消息队列（Session 持久化同步：Redis ⇄ JSON）      ║
# ╚══════════════════════════════════════════════════════════╝

class SessionSyncQueue:
    """
    基于 Redis List 的 Session 持久化同步队列。

    解决问题：Session.save() 需要同时维护 Redis 缓存和 JSON 文件的一致性。
    直接双写在并发场景下可能产生竞态条件（如写 JSON 过程中另一个请求读到旧文件回填 Redis）。

    架构：
      Session.save()  → SETEX Redis（立即，保证读一致性）
                      → RPUSH sync_queue（异步持久化到 JSON）
                                    ↓
                          SyncWorker (BLPOP)
                                    ↓
                          原子写 JSON（tmp + rename）

      SessionManager.delete() → DEL Redis（立即，对外不可见）
                              → RPUSH sync_queue
                                    ↓
                          SyncWorker → DEL JSON → sleep(500ms)
                                     → DEL Redis（延时双删）

    优势（相比直接双写）：
      - JSON 写操作通过队列保序，消除并发竞态
      - 延时双删由队列统一调度，不再零散 spawn 线程
      - Redis 不可用时自动降级为直接写 JSON
    """

    def __init__(self):
        self._running = False
        self._worker_thread: Optional[threading.Thread] = None

    def start_worker(self):
        """启动 SyncWorker 后台线程。"""
        if not _redis_client or self._running:
            return
        self._running = True
        self._worker_thread = threading.Thread(
            target=self._worker_loop, daemon=True, name="SessionSyncWorker"
        )
        self._worker_thread.start()

    def stop_worker(self):
        self._running = False

    def _worker_loop(self):
        """SyncWorker 主循环：BLPOP 阻塞等待同步任务。"""
        while self._running:
            try:
                item = _redis_client.blpop(REDIS_SYNC_QUEUE, timeout=5)
                if not item:
                    continue
                _, msg_json = item
                msg = json.loads(msg_json)
                action = msg.get("action")

                if action == "save":
                    self._handle_save(msg)
                elif action == "delete":
                    self._handle_delete(msg)

            except Exception:
                if self._running:
                    time.sleep(1)

    def _handle_save(self, msg: dict):
        """消费 save 任务：原子写 JSON 文件。"""
        session_id = msg["session_id"]
        data_json = msg["data_json"]
        os.makedirs(SESSION_DIR, exist_ok=True)
        filepath = os.path.join(SESSION_DIR, f"{session_id}.json")
        tmp_path = filepath + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(data_json)
            os.replace(tmp_path, filepath)
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def _handle_delete(self, msg: dict):
        """消费 delete 任务：删 JSON + 延时双删 Redis。"""
        session_id = msg["session_id"]
        filepath = os.path.join(SESSION_DIR, f"{session_id}.json")

        # 删 JSON 文件
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
            except OSError:
                pass

        # 延时双删：等 500ms 后再删一次 Redis（防止并发读写回填旧数据）
        time.sleep(0.5)
        cache_key = f"{REDIS_SESSION_PREFIX}{session_id}"
        try:
            _redis_client.delete(cache_key)
        except Exception:
            pass


# ╔══════════════════════════════════════════════════════════╗
# ║                LLM CLIENT（OpenAI 兼容）                 ║
# ╚══════════════════════════════════════════════════════════╝

class LLMError(Exception):
    pass


class LLMClient:
    """兼容 OpenAI API 格式的 LLM 客户端。"""

    def __init__(self, api_key: str = None, base_url: str = None, model: str = None):
        self.api_key = api_key or LLM_API_KEY
        self.base_url = (base_url or LLM_BASE_URL).rstrip("/")
        self.model = model or LLM_MODEL
        if not self.api_key:
            raise ValueError(
                "未设置 LLM API Key。\n"
                "请在 .env 文件中设置 LLM_API_KEY，或设置环境变量 LLM_API_KEY。"
            )

    def chat(self, messages: list[dict], system: str = "", tools: list[dict] = None,
             max_tokens: int = None, max_retries: int = 2) -> dict:
        api_messages = []
        if system:
            api_messages.append({"role": "system", "content": system})
        api_messages.extend(messages)

        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or LLM_MAX_TOKENS,
            "messages": api_messages,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        # 自动处理 base_url 是否已包含 /v1
        url = self.base_url
        if not url.endswith("/chat/completions"):
            if not url.endswith("/v1"):
                url = url + "/v1"
            url = url + "/chat/completions"

        last_error = None
        for attempt in range(max_retries + 1):
            try:
                req = urllib.request.Request(url, data=body, headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                })
                with urllib.request.urlopen(req, timeout=120) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                last_error = e
                error_body = ""
                try:
                    error_body = e.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                if e.code == 429:
                    wait = min(2 ** attempt * 2, 30)
                    print(f"  ⏳ API 限流 (429)，等待 {wait}s...")
                    time.sleep(wait)
                elif e.code >= 500 and attempt < max_retries:
                    time.sleep(2 ** attempt)
                else:
                    raise LLMError(f"API 错误 {e.code}: {error_body[:500]}") from e
            except urllib.error.URLError as e:
                last_error = e
                if attempt < max_retries:
                    time.sleep(2 ** attempt)
                else:
                    raise LLMError(f"连接失败: {str(e)}") from e
        raise LLMError(f"API 调用失败，已重试 {max_retries} 次: {last_error}")


# ╔══════════════════════════════════════════════════════════╗
# ║                    运行结果封装                           ║
# ╚══════════════════════════════════════════════════════════╝

class RunResult:
    """
    AgentRuntime.run() 的返回值，封装回答文本和运行指标。

    使用方式：
      result = agent.run("你好", session)
      print(result)              # 直接当字符串用 → 回答文本
      print(result.metrics)      # 获取指标 dict
      print(result.answer)       # 回答文本
    """

    def __init__(self, answer: str, metrics: dict, trace_steps: list[dict]):
        self.answer = answer
        self.metrics = metrics
        self.trace_steps = trace_steps

    def __str__(self) -> str:
        return self.answer

    def __repr__(self) -> str:
        return f"RunResult(answer={self.answer[:50]!r}..., tokens={self.metrics.get('total_tokens', 0)})"

    def to_dict(self) -> dict:
        return {"answer": self.answer, "metrics": self.metrics}


# ╔══════════════════════════════════════════════════════════╗
# ║              AGENT RUNTIME（核心运行时）                  ║
# ╚══════════════════════════════════════════════════════════╝

class AgentRuntime:
    """
    Agent 运行时引擎 — ReAct 循环。

    每次调用 LLM = 1 个 loop，一个 loop 可触发 0~N 次工具调用。
    连续错误 >= 3 时注入反思提示，帮助 LLM 调整策略。
    """

    def __init__(self, llm: LLMClient, tools: ToolRegistry,
                 max_steps: int = MAX_STEPS, verbose: bool = True):
        self.llm = llm
        self.tools = tools
        self.max_steps = max_steps
        self.verbose = verbose

    def run(self, user_input: str, session: Session) -> RunResult:
        trace = Trace(verbose=self.verbose)

        # 1. 记录用户输入
        trace.add(StepType.USER_INPUT, user_input)
        session.add_user_message(user_input)
        session.metadata["turn_count"] = session.metadata.get("turn_count", 0) + 1

        # 2. 构建系统提示词
        state_context = session.get_state_summary()
        system_prompt = SYSTEM_PROMPT.format(state_context=state_context)

        # 3. ReAct 循环
        loop_count = 0
        consecutive_errors = 0

        while loop_count < self.max_steps:
            loop_count += 1
            trace.loop_count = loop_count
            trace.add(StepType.LOOP_START, f"max={self.max_steps}")

            # 连续错误反馈机制
            if consecutive_errors >= 3:
                feedback = (
                    f"[系统提示] 已连续 {consecutive_errors} 次工具调用失败。"
                    "请停下来重新审视策略：1) 检查参数是否正确；2) 是否用了错误的工具名；"
                    "3) 是否应该换一种方法。如果无法完成，直接告知用户。"
                )
                session.add_user_message(feedback)
                consecutive_errors = 0  # 重置计数

            # 3a. 调用 LLM（计时 + token 采集）
            try:
                api_messages = session.get_messages_for_api()
                t0 = time.time()
                response = self.llm.chat(
                    messages=api_messages,
                    system=system_prompt,
                    tools=self.tools.get_api_tools(),
                )
                latency_ms = (time.time() - t0) * 1000
                usage = response.get("usage", {})
                trace.record_llm_call(latency_ms, usage)
            except LLMError as e:
                trace.add(StepType.ERROR, str(e))
                error_msg = f"抱歉，AI 服务调用失败: {str(e)}"
                session.add_assistant_message(error_msg)
                session.traces.append(trace.to_dict())
                session.save()
                return RunResult(error_msg, trace.get_metrics(), trace.to_dict())

            # 3b. 解析响应
            choice = response.get("choices", [{}])[0]
            message = choice.get("message", {})
            text_content = message.get("content", "") or ""
            tool_calls = message.get("tool_calls", []) or []

            # 3c. 无工具调用 → 最终回答
            if not tool_calls:
                final_answer = text_content.strip() or "(无回答内容)"
                trace.add(StepType.FINAL_ANSWER, final_answer)
                session.add_assistant_message(final_answer)
                session.traces.append(trace.to_dict())
                session.save()
                if self.verbose:
                    print(f"\n{trace.summary()}")
                return RunResult(final_answer, trace.get_metrics(), trace.to_dict())

            # 3d. 有工具调用 → 保存 assistant 消息
            assistant_msg = {"role": "assistant", "content": text_content or None}
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                    "type": "function",
                    "function": {
                        "name": tc.get("function", {}).get("name", ""),
                        "arguments": tc.get("function", {}).get("arguments", "{}"),
                    },
                }
                for tc in tool_calls
            ]
            session.add_assistant_message(assistant_msg)

            # 3e. 逐个执行工具调用
            has_error = False
            for tc in tool_calls:
                func_info = tc.get("function", {})
                tool_name = func_info.get("name", "")
                tool_call_id = tc.get("id", f"call_{uuid.uuid4().hex[:8]}")
                try:
                    tool_input = json.loads(func_info.get("arguments", "{}"))
                except json.JSONDecodeError:
                    tool_input = {}

                trace.add(StepType.TOOL_CALL, f"调用 {tool_name}", {
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "tool_call_id": tool_call_id,
                })
                trace.tool_call_count += 1

                result = self.tools.execute(tool_name, tool_input, session)
                success = result.get("success", True) if isinstance(result, dict) else True
                if not success:
                    has_error = True
                    trace.record_tool_error()
                result_str = json.dumps(result, ensure_ascii=False, default=str)

                trace.add(StepType.TOOL_RESULT, result_str, {
                    "tool_name": tool_name, "success": success,
                })

                session.add_tool_result(tool_call_id, result_str)

            if has_error:
                consecutive_errors += 1
            else:
                consecutive_errors = 0

        # 4. 超出最大 loop 次数
        trace.add(StepType.MAX_STEPS, str(self.max_steps))
        limit_msg = f"抱歉，我在 {self.max_steps} 个 loop 内未能完成任务。已执行的操作已保存。"
        session.add_assistant_message(limit_msg)
        session.traces.append(trace.to_dict())
        session.save()
        if self.verbose:
            print(f"\n{trace.summary()}")
        return RunResult(limit_msg, trace.get_metrics(), trace.to_dict())


# ╔══════════════════════════════════════════════════════════╗
# ║              WEB SERVER（Flask 内置）                     ║
# ╚══════════════════════════════════════════════════════════╝

def create_web_app():
    """创建 Flask Web 应用。"""
    try:
        from flask import Flask, request, jsonify, send_from_directory
        from flask_cors import CORS
    except ImportError:
        print("❌ Web 模式需要 Flask: pip install flask flask-cors")
        sys.exit(1)

    app = Flask(__name__, static_folder="static")
    CORS(app)

    llm = LLMClient()
    tools = create_default_registry()
    sm = SessionManager()

    # 启动 Redis 消息队列 Workers
    task_queue = TaskQueue(tools, sm)
    task_queue.start_worker()          # 异步工具任务处理

    sync_queue = SessionSyncQueue()
    sync_queue.start_worker()          # Session Redis ⇄ JSON 持久化同步

    def _get_agent() -> AgentRuntime:
        return AgentRuntime(llm=llm, tools=tools, max_steps=MAX_STEPS, verbose=False)

    # ── 健康检查 ──
    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({
            "status": "ok",
            "model": llm.model,
            "api_base": llm.base_url,
            "tools": tools.list_names(),
            "max_steps": MAX_STEPS,
            "redis": "connected" if _redis_client else "not_available",
        })

    # ── 聊天接口 ──
    @app.route("/chat", methods=["POST"])
    def chat():
        data = request.get_json(force=True)
        message = data.get("message", "").strip()
        session_id = data.get("session_id", "")
        if not message:
            return jsonify({"error": "message 不能为空"}), 400

        session = sm.get_or_create(session_id)
        agent = _get_agent()

        try:
            result = agent.run(message, session)
        except Exception as e:
            return jsonify({"error": str(e), "session_id": session.session_id}), 500

        return jsonify({
            "answer": result.answer,
            "session_id": session.session_id,
            "turn_count": session.metadata.get("turn_count", 0),
            "metrics": result.metrics,
        })

    # ── 会话管理 ──
    @app.route("/sessions", methods=["GET"])
    def list_sessions():
        return jsonify({"sessions": sm.list_sessions()})

    @app.route("/history/<session_id>", methods=["GET"])
    def get_history(session_id):
        try:
            session = sm.load(session_id)
            # 过滤出用户可见的消息
            visible = []
            for msg in session.messages:
                role = msg.get("role", "")
                if role in ("user", "assistant") and isinstance(msg.get("content"), str):
                    visible.append({"role": role, "content": msg["content"]})
            return jsonify({"session_id": session_id, "messages": visible})
        except FileNotFoundError:
            return jsonify({"error": f"会话 {session_id} 不存在"}), 404

    @app.route("/traces/<session_id>", methods=["GET"])
    def get_traces(session_id):
        try:
            session = sm.load(session_id)
            return jsonify({"session_id": session_id, "traces": session.traces})
        except FileNotFoundError:
            return jsonify({"error": f"会话 {session_id} 不存在"}), 404

    @app.route("/session/<session_id>", methods=["DELETE"])
    def delete_session(session_id):
        if sm.delete(session_id):
            return jsonify({"message": f"会话 {session_id} 已删除"})
        return jsonify({"error": f"会话 {session_id} 不存在"}), 404

    # ── 异步工具任务（Redis 消息队列）──
    @app.route("/task/submit", methods=["POST"])
    def submit_task():
        """
        提交异步工具任务到 Redis 消息队列。

        请求体: {"session_id": "...", "tool_name": "ocr", "tool_input": {...}}
        响应:   {"task_id": "...", "status": "queued"}
        """
        if not _redis_client:
            return jsonify({"error": "Redis 未连接，无法使用异步任务"}), 503
        data = request.get_json(force=True)
        session_id = data.get("session_id", "")
        tool_name = data.get("tool_name", "")
        tool_input = data.get("tool_input", {})
        if not tool_name:
            return jsonify({"error": "tool_name 不能为空"}), 400
        task_id = f"task_{_snowflake.generate_hex()}"
        ok = task_queue.submit(task_id, session_id, tool_name, tool_input)
        if not ok:
            return jsonify({"error": "任务提交失败"}), 500
        return jsonify({"task_id": task_id, "status": "queued"})

    @app.route("/task/result/<task_id>", methods=["GET"])
    def get_task_result(task_id):
        """
        轮询获取异步任务结果。

        任务完成 → {"status": "done", "result": {...}}
        任务超时 → {"status": "timeout"}
        """
        if not _redis_client:
            return jsonify({"error": "Redis 未连接"}), 503
        result_key = f"{REDIS_RESULT_PREFIX}{task_id}"
        try:
            result = _redis_client.get(result_key)
            if result:
                _redis_client.delete(result_key)
                return jsonify({"status": "done", "result": json.loads(result)})
            return jsonify({"status": "pending", "task_id": task_id})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── 静态文件（Web UI）──
    @app.route("/")
    def index():
        static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
        index_path = os.path.join(static_dir, "index.html")
        if os.path.isfile(index_path):
            return send_from_directory(static_dir, "index.html")
        # 如果没有 static/index.html，返回内置简易页面
        return _builtin_html()

    return app


def _builtin_html() -> str:
    """内置极简 Web UI，无需单独的 HTML 文件。"""
    return """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Mini-Agent</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, "Noto Sans SC", sans-serif; background: #0f1117; color: #e0e0e0; height: 100vh; display: flex; flex-direction: column; }
  .header { padding: 16px 24px; border-bottom: 1px solid #2a2d35; display: flex; align-items: center; gap: 12px; }
  .header h1 { font-size: 18px; font-weight: 600; }
  .header .badge { font-size: 11px; background: #2563eb; color: #fff; padding: 2px 8px; border-radius: 10px; }
  .chat-area { flex: 1; overflow-y: auto; padding: 24px; display: flex; flex-direction: column; gap: 16px; }
  .msg { max-width: 80%; padding: 12px 16px; border-radius: 12px; line-height: 1.6; white-space: pre-wrap; word-break: break-word; font-size: 14px; }
  .msg.user { align-self: flex-end; background: #2563eb; color: #fff; border-bottom-right-radius: 4px; }
  .msg.assistant { align-self: flex-start; background: #1e2028; border: 1px solid #2a2d35; border-bottom-left-radius: 4px; }
  .msg.system { align-self: center; color: #888; font-size: 12px; }
  .input-area { padding: 16px 24px; border-top: 1px solid #2a2d35; display: flex; gap: 12px; }
  .input-area input { flex: 1; padding: 12px 16px; border-radius: 8px; border: 1px solid #2a2d35; background: #1a1c24; color: #e0e0e0; font-size: 14px; outline: none; }
  .input-area input:focus { border-color: #2563eb; }
  .input-area button { padding: 12px 24px; border-radius: 8px; background: #2563eb; color: #fff; border: none; cursor: pointer; font-size: 14px; font-weight: 500; }
  .input-area button:hover { background: #1d4ed8; }
  .input-area button:disabled { opacity: 0.5; cursor: not-allowed; }
  .typing { display: inline-block; } .typing span { animation: blink 1.4s infinite; } .typing span:nth-child(2) { animation-delay: 0.2s; } .typing span:nth-child(3) { animation-delay: 0.4s; }
  @keyframes blink { 0%,80%,100% { opacity: 0; } 40% { opacity: 1; } }
</style>
</head>
<body>
<div class="header">
  <h1>🤖 Mini-Agent</h1>
  <span class="badge">v3.0</span>
</div>
<div class="chat-area" id="chat"></div>
<div class="input-area">
  <input id="input" placeholder="输入消息..." autocomplete="off" />
  <button id="btn" onclick="send()">发送</button>
</div>
<script>
let sid = '';
const chat = document.getElementById('chat');
const input = document.getElementById('input');
const btn = document.getElementById('btn');
input.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });

function addMsg(role, text) {
  const d = document.createElement('div');
  d.className = 'msg ' + role;
  d.textContent = text;
  chat.appendChild(d);
  chat.scrollTop = chat.scrollHeight;
  return d;
}

async function send() {
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  addMsg('user', text);
  btn.disabled = true;
  const loading = addMsg('assistant', '');
  loading.innerHTML = '<span class="typing"><span>●</span><span>●</span><span>●</span></span>';
  try {
    const res = await fetch('/chat', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ message: text, session_id: sid }) });
    const data = await res.json();
    if (data.error) { loading.textContent = '❌ ' + data.error; }
    else { loading.textContent = data.answer; sid = data.session_id; }
  } catch (e) { loading.textContent = '❌ 网络错误: ' + e.message; }
  btn.disabled = false;
  input.focus();
}

addMsg('system', '欢迎使用 Mini-Agent！输入消息开始对话。');
input.focus();
</script>
</body>
</html>"""


# ╔══════════════════════════════════════════════════════════╗
# ║                    MAIN 入口                             ║
# ╚══════════════════════════════════════════════════════════╝

BANNER = """
╔══════════════════════════════════════════════════════╗
║              🤖  Mini-Agent  v3.0                    ║
║                                                      ║
║  单文件版 · 从零实现的最小可用 Agent                  ║
║  多轮对话 · 工具调用 · 跨轮次状态 · 附件检查          ║
║                                                      ║
║  命令:  /help  /tools  /state  /trace  /new  /quit   ║
╚══════════════════════════════════════════════════════╝
"""

HELP_TEXT = """
可用命令：
  /help      显示此帮助
  /tools     列出所有可用工具
  /state     查看当前会话状态
  /trace     查看上一轮的执行追踪
  /sessions  列出所有已保存的会话
  /new       创建新会话
  /quit      退出程序
"""


def run_cli(args):
    """CLI 交互模式入口。"""
    if not LLM_API_KEY:
        print("❌ 未设置 LLM_API_KEY。请在 .env 中配置。")
        sys.exit(1)

    sm = SessionManager()

    if args.list:
        sessions = sm.list_sessions()
        if not sessions:
            print("暂无已保存的会话。")
        else:
            print(f"\n已保存的会话 ({len(sessions)} 个)：\n")
            for s in sessions:
                print(f"  📋 {s['session_id']}  轮次={s['turn_count']}  "
                      f"待办={s['todos']}  最后活跃={s['last_active']}")
        return

    try:
        llm = LLMClient()
    except ValueError as e:
        print(f"❌ {e}")
        sys.exit(1)

    tools = create_default_registry()
    verbose = not args.quiet
    agent = AgentRuntime(llm=llm, tools=tools, max_steps=args.max_steps, verbose=verbose)

    if args.session:
        try:
            session = sm.load(args.session)
            print(f"✅ 已恢复会话: {session.session_id}")
        except FileNotFoundError:
            print(f"❌ 会话 {args.session} 不存在")
            sys.exit(1)
    else:
        session = sm.create()
        print(BANNER)
        print(f"  会话 ID:  {session.session_id}")
        print(f"  模型:     {llm.model}")
        print(f"  API:      {llm.base_url}")
        print(f"  最大步数: {args.max_steps}")
        print(f"  工具:     {', '.join(tools.list_names())}\n")

    while True:
        try:
            user_input = input("\n🧑 你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\n👋 再见！")
            session.save()
            break

        if not user_input:
            continue

        if user_input.startswith("/"):
            cmd = user_input.lower().split()[0]
            if cmd in ("/quit", "/exit"):
                print("👋 再见！会话已保存。")
                session.save()
                break
            elif cmd == "/help":
                print(HELP_TEXT)
            elif cmd == "/tools":
                print("\n可用工具：")
                for t in tools.get_api_tools():
                    print(f"  🔧 {t['function']['name']}: {t['function']['description'][:80]}")
            elif cmd == "/state":
                print(f"\n会话状态 ({session.session_id})：")
                print(session.get_state_summary())
            elif cmd == "/trace":
                if session.traces:
                    last = session.traces[-1]
                    print(f"\n上一轮执行追踪 ({len(last)} 步)：")
                    for step in last:
                        print(f"  [{step['step']}] {step['type']}: {step['content'][:100]}")
                else:
                    print("暂无执行追踪。")
            elif cmd == "/sessions":
                for s in sm.list_sessions():
                    marker = "→" if s["session_id"] == session.session_id else " "
                    print(f"  {marker} {s['session_id']}  轮次={s['turn_count']}  待办={s['todos']}")
            elif cmd == "/new":
                session.save()
                session = sm.create()
                print(f"✅ 新会话: {session.session_id}")
            else:
                print(f"未知命令: {cmd}，输入 /help 查看帮助")
            continue

        try:
            agent.run(user_input, session)
        except Exception as e:
            print(f"\n❌ 运行错误: {type(e).__name__}: {e}")
            if verbose:
                import traceback
                traceback.print_exc()


def main():
    parser = argparse.ArgumentParser(description="Mini-Agent v3.0 — 单文件 AI Agent")
    parser.add_argument("--cli", action="store_true", help="启动 CLI 交互模式（默认启动 Web 服务）")
    parser.add_argument("--session", "-s", help="[CLI] 恢复指定会话 ID")
    parser.add_argument("--list", "-l", action="store_true", help="[CLI] 列出所有会话")
    parser.add_argument("--quiet", "-q", action="store_true", help="[CLI] 安静模式")
    parser.add_argument("--max-steps", type=int, default=MAX_STEPS, help="单轮最大 loop 数")
    parser.add_argument("--port", "-p", type=int, default=PORT, help="[Web] 服务端口")
    args = parser.parse_args()

    if args.cli or args.list or args.session:
        run_cli(args)
    else:
        # Web 服务模式
        if not LLM_API_KEY:
            print("❌ 未设置 LLM_API_KEY。请在 .env 中配置。")
            sys.exit(1)
        app = create_web_app()
        print(f"\n🚀 Mini-Agent Web 服务已启动")
        print(f"   地址: http://0.0.0.0:{args.port}")
        print(f"   模型: {LLM_MODEL}")
        print(f"   API:  {LLM_BASE_URL}\n")
        app.run(host="0.0.0.0", port=args.port, debug=False)


if __name__ == "__main__":
    main()