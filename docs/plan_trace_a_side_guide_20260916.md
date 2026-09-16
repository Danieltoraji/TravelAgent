# Plan Trace A 侧工作项指导 —— 候选池 LLM 轨迹透传

> 日期：2026-09-16 · 交付对象：A 侧（ivenmeng）· 预估改动：3 个文件、约 10 行
> 背景：B 侧已上线规划轨迹（协议见 `docs/sync_notes_plan_trace_20260916.md`）。
> 三个 LLM 调用点里，备注解析与 chat 的轨迹 B 侧已自留，**唯一需要 A 改的是
> 候选池定制 LLM（ScenicSearchPlanner）**——它的 generate 结果 dict 里有现成的
> `tool_trace` / `reviews`，目前在 `plan_for` 内被丢弃。

## 0. 现状：trace 在哪被丢的

```
BPlannerHook.generate_timeline()                    （a_side/call_llm/b_planner_hook.py）
  └─ planner_parts/data_source.py :: _search_plan_once(city)     （约 :84）
       └─ ScenicSearchPlanner.plan_for(...)                        （call_llm/scenic_search_planner.py :381）
            └─ result = client.generate(...)      ← ← ← 完整 dict（含 tool_trace/reviews）
            └─ return plan                        ← ← ← 只返回校验后的计划，result 被丢弃
```

门控：`USE_LIVE_DATA` + `USE_LLM_TOOLS` 双开才会走到这里（默认关）；关闭路径
完全不涉及本改动。

## 1. 改动规格（3 个文件，均在 A 主仓库改，改完重新镜像同步）

### 文件 1：`a_side/call_llm/scenic_search_planner.py`

**`__init__`**：加一个公开属性声明：

```python
self.last_llm_trace: Optional[Dict[str, Any]] = None
```

**`plan_for()`**（约 :381-434）：两处插入——

① `create_llm_client(...)` 之前记起点：

```python
started = time.monotonic()
```

② `result = client.generate(...)` 成功返回之后、`content = result.get("content")`
校验之前，组装并挂到实例（即使后续 `_validate_plan` 校验不过，轨迹也已保住）：

```python
self.last_llm_trace = {
    "model": getattr(client, "model_name", None),
    "elapsed_ms": int((time.monotonic() - started) * 1000),
    "tool_trace": result.get("tool_trace") or [],
    "reviews": result.get("reviews") or [],
    "content_summary": str(result.get("content") or "")[:100],
}
```

`except` 分支（LLM 调用失败）**不需要**设置——保持 None 即可，B 侧按缺席降级。

### 文件 2：`a_side/call_llm/planner_parts/data_source.py`

**`_search_plan_once()`**（约 :84）：`plan = planner.plan_for(...)` 之后加一行透传
（`self` 就是 BPlannerHook 实例——本方法是其 mixin）：

```python
self.last_llm_trace = getattr(planner, "last_llm_trace", None)
```

### 文件 3：`a_side/call_llm/b_planner_hook.py`

**`__init__`**（约 :112）：加显式声明（功能上文件 2 的 setattr 已覆盖，这里为了
可发现性与 B 侧 `getattr(planner_hook, "last_llm_trace", None)` 的契约对齐）：

```python
self.last_llm_trace: Optional[Dict[str, Any]] = None
```

## 2. 透传数据格式（契约，B 侧按此解析）

```jsonc
{
  "model": "deepseek-chat",     // client.model_name，取不到给 null
  "elapsed_ms": 1234,           // 本次 generate 耗时
  "tool_trace": [ ... ],        // generate 返回值原样搬运，不做语义加工
  "reviews": [ ... ],           // 同上
  "content_summary": "…"        // 可选，content 前 100 字
}
```

B 侧消化逻辑（已实现，供对照）：`runtime/plan_trace.py` 的
`extend_from_llm_meta()`——主步记 model/耗时/摘要，`tool_trace` 逐轮展开为
`phase=data` 工具步（参数脱敏 + result_digest 摘要），`reviews` 展开为
`phase=review` 审查步。

## 3. 硬性要求（验收一票项）

1. **纯附加**：不改任何现有返回值、签名、控制流；`plan_for` 的返回值保持
   `plan dict | None` 不变。
2. **兜底**：组装 `last_llm_trace` 的代码整体 try/except，失败 →
   `self.last_llm_trace = None` + `logger.warning`，**绝不影响规划主链路**
   （与「审查轮失败降级为无审查」同一哲学）。
3. **无新依赖**：只用 stdlib（`time` 已有）。
4. **早退路径保持 None**：门控关 / `plan_for` 返回 None 的各路径不设置该属性
   （或保持 None），B 侧自动降级为里程碑粒度。

## 4. 验收标准

- [ ] A 侧既有 pytest 全绿（本改动无行为变化，不应有任何用例需要改）；
- [ ] 手工验证（`USE_LIVE_DATA=1`、`USE_LLM_TOOLS=1`、配好 key）：
      发起一次 plan 后，`BPlannerHook` 实例上 `last_llm_trace` 含
      `model` / `elapsed_ms` / `tool_trace`；
- [ ] 手工验证降级：两个门控关闭时 `last_llm_trace is None`，规划结果与改前一致；
- [ ] 镜像同步到 B 仓库后 `test_ab_sync` 在有 A 布局的环境通过；
- [ ] B 侧端到端：plan 响应 `trace.steps` 出现 `phase=llm`（title「候选池定制」）、
      `phase=data`（source=llm_round）与 `phase=review` 步骤（有审查轮时）。

## 5. 时序与合并

- **B 侧已容缺**：镜像未合入期间，B 端轨迹功能完整（仅缺候选池 LLM 细节步），
  不阻塞 B 侧合入；
- **合并顺序**：B 侧本分支等 A 镜像更新落地后**一并合入 main**；
- 有问题直接在文档上批注或找 B 侧（钉小呆Xiaodai）对齐。
