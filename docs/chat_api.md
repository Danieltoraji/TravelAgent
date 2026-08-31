# C 端对话接口（Travel Assistant Chat）

面向 C（React + Capacitor 前端）的旅行助手对话接口，由 B 端调用大模型
（DeepSeek/GLM，复用 `a_side/call_llm` 设施）并返回回复文本。

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

- **v1 纯对话**：无工具调用（后续 v2 可接入只读工具真源）；
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
