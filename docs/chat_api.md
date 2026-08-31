# C 端对话接口（Travel Assistant Chat）

面向 C（React + Capacitor 前端）的旅行助手对话接口，由 B 端调用大模型
（DeepSeek/GLM，复用 `a_side/call_llm` 设施）并返回回复文本。

**v2（2026-09-01）**：对话中模型可调用私有工具 `update_timeline`
**直接修改后端时间轴**——用户说「把故宫挪到下午」，模型输出结构化新时间轴
→ 服务端校验 → 应用 → 记入 `/api/replans`（source=chat）。C 端**零改动**：
App 的轮询逻辑检测到 replan 数量变化即自动刷新 `/api/timeline/`。

## 〇、v2 对话改时间轴

### 工作方式

1. 用户消息进入对话（带行程上下文系统提示词 + `update_timeline` 工具描述）；
2. 模型判断需要调整行程时，输出完整新时间轴（与 `GET /api/timeline/`
   返回结构同构，Schema 见代码 `views.CHAT_TIMELINE_TOOL`）；
3. 服务端校验（必填字段 / 日期 / 时间格式 / 景点非空），失败则把错误
   回填给模型自动重试（最多 3 轮工具调用）；
4. 校验通过 → `runtime.apply_timeline_from_chat`：替换时间轴、重建监控
   规则、**保留已预约状态**，并记录：
   - `/api/replans/` 新增一条（`source: "chat"`、`decision.diff_summary` 含
     `[added] / [removed] / [rescheduled]` 改动点）——App 据此自动刷新；
   - `/api/timeline/history/` 新增一条（reason=对话调整）；
5. 模型继续生成自然语言总结（如「已把景山公园挪到 15:00」）。

### C 端说明

- **无需任何改动**：对话请求/响应契约不变（仍是 `{message, history}` →
  `{reply, elapsed_ms}`）；时间轴变化经既有 replan 轮询自动呈现。
- 可选增强：通知中心可展示 `source="chat"` 的调整卡片（读 `/api/replans/`
  的 `diff_summary`），非必须。

### 限制（v2 范围）

- 服务端只做**结构 + 基础语义校验**（时间格式/日期连续/景点非空）；
  闭馆、交通可行性等深度校验留待 v2.2；
- `update_timeline` 是**对话私有工具**（不进通用工具面，其他接口不可调用）；
- 模型可能拒绝修改（如需求冲突）→ 正常对话回复，不调工具；
- 未建行程时工具返回错误，模型会告知用户先规划。

---

## 一、接口契约

```
POST /api/chat/
Content-Type: application/json
```

### 请求体

```json
{
  "message": "我们第一天去哪？",
  "history": [
    {"role": "user", "content": "你好"},
    {"role": "assistant", "content": "你好！有什么可以帮你？"}
  ]
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `message` | 是 | 用户当前消息（去空白后非空） |
| `history` | 否 | 会话历史（**C 端维护**，服务端无状态）；仅最近 20 条生效，非法项（非 user/assistant、空 content）自动过滤 |

### 响应

```json
{
  "reply": "第一天上午去故宫博物院（09:00 入园），下午去景山公园。",
  "elapsed_ms": 3120
}
```

| 状态码 | 含义 |
| --- | --- |
| 200 | 成功，`reply` 为助手回复文本 |
| 400 | 参数不合法（空 body / 空 message / history 非数组） |
| 502 | LLM 未配置（缺 `DEEPSEEK_API_KEY` / `GLM_API_KEY`）或调用失败（超时/网络），`error` 字段含原因 |

### 上下文注入（服务端自动）

系统提示词会注入当前运行时状态，让助手能回答「我的行程」类问题：

- 当前行程摘要（城市、日期、每天景点 + 到达时间）
- 用户需求（目的地 / 天数 / 预算 / 偏好标签 / 必去景点）
- 最近一次重规划的原因（如有）

示例提问：`我们第一天去哪？`、`故宫几点关门？`、`预算够不够？`、`行程能调整吗？`

### 限制

- **v2 支持对话改时间轴**（见上文「〇」节）；工具调用仅限 `update_timeline`
  私有工具，无其它工具（真源查询留 v2.2）；
- 非流式：3–10 秒返回（取决于模型），**C 端必须展示 loading 态**；
- 无鉴权（与现有单用户 demo 一致）；公网部署时注意成本，后续可加 token。

---

## 二、C 端接入示例（React + TypeScript）

### 1. api.ts 增加方法

```ts
export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
}

export interface ChatResult {
  reply: string;
  elapsed_ms: number;
}

// 在 api 对象里追加：
async chat(message: string, history: ChatMessage[]): Promise<ChatResult> {
  if (USE_MOCK) {
    await new Promise((r) => setTimeout(r, 800));
    return {
      reply: `（mock）收到：${message}。演示环境请连接真实后端。`,
      elapsed_ms: 800,
    };
  }
  return fetchJSON<ChatResult>('/chat/', {
    method: 'POST',
    body: JSON.stringify({ message, history }),
  });
},
```

### 2. 对话面板组件（示意）

```tsx
function ChatPanel({ requestId }: { requestId: string }) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const send = async () => {
    const text = input.trim();
    if (!text || loading) return;
    const history = messages.slice(-20);
    setMessages((prev) => [...prev, { role: 'user', content: text }]);
    setInput('');
    setLoading(true);
    setError(null);
    try {
      const { reply } = await api.chat(text, history);
      setMessages((prev) => [...prev, { role: 'assistant', content: reply }]);
    } catch (e) {
      setError(e instanceof Error ? e.message : '对话失败，请稍后重试');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex h-full flex-col">
      <div className="flex-1 space-y-2 overflow-y-auto p-3">
        {messages.map((m, i) => (
          <div key={i} className={m.role === 'user' ? 'text-right' : 'text-left'}>
            <span className={`inline-block rounded-lg px-3 py-2 text-sm ${
              m.role === 'user' ? 'bg-blue-500 text-white' : 'bg-gray-100'
            }`}>
              {m.content}
            </span>
          </div>
        ))}
        {loading && <div className="text-sm text-gray-400">助手思考中…</div>}
        {error && <div className="text-sm text-red-500">{error}</div>}
      </div>
      <div className="flex gap-2 border-t p-2">
        <input
          className="flex-1 rounded border px-3 py-2 text-sm"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && send()}
          placeholder="问你的行程助手…"
          disabled={loading}
        />
        <button
          className="rounded bg-blue-500 px-4 py-2 text-sm text-white disabled:opacity-50"
          onClick={send}
          disabled={loading || !input.trim()}
        >
          发送
        </button>
      </div>
    </div>
  );
}
```

### 3. 入口建议

- 首页右下角悬浮按钮（打开半屏对话面板）；
- 或「通知中心」旁加一个对话 Tab。
- 会话历史存组件 state 即可（刷新即清）；如需跨会话保留可存 localStorage。

---

## 三、验证

```bash
# 先建行程（对话才有上下文）
curl -X POST http://39.96.89.133:8000/api/plan/ \
  -H "Content-Type: application/json" \
  -d '{"content": {"destination": "北京", "start_date": "2026-08-23", "days": 2,
       "visitor_number": 2,
       "constraints": {"budget": 2000, "must_visit": ["故宫"]},
       "preferences": {"preferred_tags": ["历史文化"]}}}'

# 对话（应引用真实行程）
curl -X POST http://39.96.89.133:8000/api/chat/ \
  -H "Content-Type: application/json" \
  -d '{"message": "我们第一天去哪？"}'
# → {"reply": "第一天上午…", "elapsed_ms": …}
```
