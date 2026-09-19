# LM Studio REST API — Capability & Observability Report

Date: 2026-09-18 · Source: official docs (lmstudio.ai/docs/developer) + community sources
Scope: what the LM Studio local server API can do, and how much can be tracked per request
(timestamps, metadata, resource usage) — for building the automated image-vs-text test harness.

---

## 1. API surfaces (all on the same port)

| Family | Prefix | Status | Notes |
|---|---|---|---|
| Native REST v1 | `/api/v1/*` | Current, recommended (since LM Studio 0.4.0) | LM Studio-specific, richest stats |
| Native REST v0 | `/api/v0/*` | Deprecated | Same OpenAI core shape + `stats`/`model_info`/`runtime` blocks |
| OpenAI-compatible | `/v1/*` | Current | Drop-in; swap `base_url` to `http://<host>:1234/v1` |
| Anthropic-compatible | `/v1/messages` | Current | `x-api-key` header |

- Default base URL: `http://localhost:1234`. (Our harness will use the LAN host.)
- Port: configurable via GUI Server Settings, `lms server start --port <n>`, or `LMS_SERVER_HOST` env. Last-used port is persisted; default 1234.
- Auth: **off by default**. When enabled, `Authorization: Bearer $LM_API_TOKEN` (native + OpenAI-compat), `x-api-key` (Anthropic-compat). Tokens managed in Developer → Server Settings.

### Verified endpoint inventory

**Native v1 (`/api/v1`)**
| Endpoint | Purpose |
|---|---|
| `GET /api/v1/models` | list all models incl. loaded instances, capabilities, quantization, variants |
| `POST /api/v1/chat` | chat/inference — stateful, MCP, images, reasoning |
| `POST /api/v1/models/load` | load model into memory with config |
| `POST /api/v1/models/unload` | unload by `instance_id` |
| `POST /api/v1/models/download` | download a model (slow — avoid in tests) |
| `GET /api/v1/models/download/status/:job_id` | poll download job |

**OpenAI-compatible (`/v1`)** — exactly 5 documented endpoints:
`GET /v1/models`, `POST /v1/chat/completions`, `POST /v1/completions` (legacy)`, `POST /v1/embeddings`, `POST /v1/responses` (Responses API / Codex).

**Not present (verified):** `/v1/rerank` (open feature request only), image generation `/v1/images/*`, moderation, edits. The only image capability is **vision input**.

---

## 2. `/v1/chat/completions` — the critical endpoint

Payload params (documented set): `model`, `top_p`, `top_k`, `messages`, `temperature`, `max_tokens`, `stream`, `stop`, `presence_penalty`, `frequency_penalty`, `logit_bias`, `repeat_penalty`, `seed`.
- `top_k`, `repeat_penalty` are LM Studio extensions.
- Also supported (via changelog): `tools`/`tool_choice`, `response_format` (`json_schema`|`text`), `stream_options.include_usage`, `draft_model` (speculative decoding), `ttl`.
- `max_completion_tokens` is NOT in the documented set — use `max_tokens` (`-1` = unlimited).

Response shape (documented example, tool-call case):

```json
{
  "id": "chatcmpl-gb1t1uqzefudice8ntxd9i",
  "object": "chat.completion",
  "created": 1730913210,
  "model": "lmstudio-community/qwen2.5-7b-instruct",
  "choices": [{ "index": 0, "logprobs": null, "finish_reason": "tool_calls",
                "message": { "role": "assistant", "tool_calls": [...] } }],
  "usage": { "prompt_tokens": 263, "completion_tokens": 34, "total_tokens": 297 },
  "system_fingerprint": "lmstudio-community/qwen2.5-7b-instruct"
}
```

- `created` is a **Unix timestamp in seconds** (response-side creation time; not a documented latency anchor).
- `usage = {prompt_tokens, completion_tokens, total_tokens}`. In streaming it is only present when `stream_options: { include_usage: true }`.
- Structured output returns JSON as a **string** inside `choices[0].message.content` (must be parsed).
- Tool-call arguments arrive as a JSON **string** in `message.tool_calls[i].function.arguments`.

### Deprecated v0 response = richest single source of stats

```json
{
  "id": "chatcmpl-i3gkjwthhw96whukek9tz",
  "object": "chat.completion",
  "created": 1731990317,
  "model": "granite-3.0-2b-instruct",
  "choices": [{ "index": 0, "logprobs": null, "finish_reason": "stop",
                "message": { "role": "assistant", "content": "..." } }],
  "usage": { "prompt_tokens": 24, "completion_tokens": 53, "total_tokens": 77 },
  "stats": {
    "tokens_per_second": 51.4, "time_to_first_token": 0.111,
    "generation_time": 0.954, "stop_reason": "eosFound"
  },
  "model_info": { "arch": "granite", "quant": "Q4_K_M", "format": "gguf", "context_length": 4096 },
  "runtime": { "name": "llama.cpp-...", "version": "1.3.0", "supported_formats": ["gguf"] }
}
```

v0 gives: request `id` (`chatcmpl-…`), `created`, `finish_reason`, OpenAI `usage`, plus `stats.generation_time`, `stats.stop_reason`.

### Native v1 `/api/v1/chat` response (incl. stream `chat.end.result`)

```json
{
  "model_instance_id": "openai/gpt-oss-20b",
  "output": [ { "type": "reasoning", "content": "..." },
              { "type": "tool_call", "tool": "model_search", "arguments": {...}, "output": "..." },
              { "type": "message", "content": "..." } ],
  "stats": {
    "input_tokens": 329, "total_output_tokens": 268, "reasoning_output_tokens": 5,
    "tokens_per_second": 43.73, "time_to_first_token_seconds": 0.781,
    "model_load_time_seconds": 2.656        // present ONLY when a cold load happened
  },
  "response_id": "resp_02b2017dbc06c12bfc353a2ed6c2b802f8cc682884bb5716"
}
```

- No `id`, no `created`, no `finish_reason` on v1 — the only stable handle is `response_id` (present when `store: true`, the default).
- Stateful continuation via `store`/`previous_response_id`/`response_id`.

---

## 3. Streaming

Two distinct contracts:

**Native `/api/v1/chat`** SSE uses named events, in order:
`chat.start` → [`model_load.start/progress/end`] → [`prompt_processing.start/progress/end`] → [`reasoning.start/delta/end`] → [`tool_call.*`] → [`message.start/delta/end`] → [`error`] → `chat.end`.
`chat.end.result` == the full non-streaming body (aggregated stats included). Events carry **no server timestamps** (timing = client wall-clock only). Errors mid-stream still end with `chat.end` "with whatever was generated".

**OpenAI-compat `/v1/chat/completions`** streaming = standard SSE `data:` lines, `object: "chat.completion.chunk"`, deltas in `choices[].delta.content`. Usage only with `stream_options.include_usage: true` (fixed in 0.3.19+). Tool-call fragments must be accumulated client-side (`id` appears only on the first fragment).

`/v1/responses` events: `response.created`, `response.output_text.delta`, `response.completed`. `/v1/messages` events: `message_start/content_block_*`/`message_delta`/`message_stop`.

---

## 4. Per-request resource usage — what CAN be tracked

**Recorded by the server, per request:**
- token counts (prompt/completion/total; v1 also reasoning split),
- `tokens_per_second`,
- `time_to_first_token_seconds` (server-measured),
- `generation_time` (**v0 only**; on v1 derive from `total_output_tokens / tokens_per_second`),
- `model_load_time_seconds` (**only when the model was loaded for that request** = cold start),
- `finish_reason`/`stop_reason`,
- `created` timestamp (seconds; **v0/OpenAI-compat only**),
- request id (`chatcmpl-…`/`cmpl-…`; v1 `response_id` only while `store:true`).

**Not returned by the API (must measure client-side):** total end-to-end latency, queue-wait time, per-token timings, HTTP timing headers. Approach: monotonic wall-clock around the request + timestamping each SSE delta for per-chunk inter-arrival times (first delta ≈ observed TTFT).

**Hardware usage (CPU %/RAM/VRAM live usage): the API exposes NONE.**
- No `/v1/system/info`; the docs endpoint 404s; no Prometheus/metrics endpoint.
- Closest items: `lms runtime survey --json` → static **capacity** (RAM/VRAM totals, GPU/CPU info, engine version); `lms load --estimate-only` → **estimated** (not measured) VRAM/RAM; `GET /api/v1/models` `loaded_instances[].config` → load config (context_length, parallel, flash_attention).
- Live usage requires external polling: `nvidia-smi`/`rocm-smi`/psutil.

**Disk/log observability:**
- Dated server logs: `~/.cache/lm-studio/server-logs/` (Windows: `%USERPROFILE%\.cache\lm-studio\server-logs\`), contain timestamps + full request/response bodies at INFO/DEBUG. **Format not guaranteed** — LM Studio advises against relying on it.
- `lms log stream --json [--stats]` → live `llm.prediction.input`/`output` events incl. timestamp, model + stats (heavier but structured).
- App conversations stored as JSON under `~/.lmstudio/conversations/` (structure not guaranteed).

**Hard gaps (cannot be tracked):** per-request CPU/VRAM/RAM, exact VRAM attribution per instance, queue position/wait time, per-token server timestamps, and (on v1) any created-time/id unless stateful.

**Recommended per-request telemetry record** (what the harness should log for every call):

```jsonc
{
  "client_request_id": "<uuid by harness>",
  "server_*id": "chatcmpl-… / resp_…",
  "endpoint": "/v1/chat/completions | /api/v1/chat",
  "stream": false,
  "model": "<model_instance_id>",
  "load_config": { "context_length":…, "parallel":…, "flash_attention":… },
  "t_sent / t_first_byte / t_first_delta / t_done",
  "total_latency_ms", "ttft_observed_ms",
  "input_tokens / output_tokens / reasoning_output_tokens / total_tokens",
  "tokens_per_second", "time_to_first_token_seconds", "generation_time_seconds",
  "model_load_time_seconds", "finish_reason", "stop_reason",
  "system": { "ramCapacityBytes", "vramCapacityBytes", "gpuName", "cpuName" },   // static, once/session
  "lmstudio_version", "runtime_version"
}
```

---

## 5. Headless automation for "fresh instance per test"

- **`lms` CLI + `llmster` daemon** (0.4.x): `lms daemon up`, `lms server start [--port N] [--bind 0.0.0.0]`, `lms server status --json --quiet`. One HTTP server per instance — multi-port requires one instance per port (container/VM, `CUDA_VISIBLE_DEVICES` pinning).
- **Lifecycle over REST:** `POST /api/v1/models/load` (returns `{status:"loaded", instance_id, load_time_seconds}`; blocks until ready), `POST /api/v1/models/unload` (`instance_id` required), `GET /api/v1/models` reports `loaded_instances` (empty array = not loaded). `lms load`/`lms unload`/`lms ps --json` mirror this.
- **Readiness probe:** after load, poll `lms ps --json` or `GET /api/v1/models`; a `/v1/chat/completions` returning HTTP 200 is the functional uptime signal (there is no `/health` endpoint; `GET /v1/models` returns 200 when up). A bogus `"model":"check"` probe returns 400 and can be misread — use real identifiers.
- **Per-test isolation loop:** `lms unload --all` → confirm `lms ps --json` empty → `lms load <short-key> --yes --parallel 1` (blocks; returns exact identifier) → verify → one request capturing `model_load_time_seconds`/`time_to_first_token_seconds`/`tokens_per_second` (or SSE `model_load.*`/`prompt_processing.*`/`message.*`) → unload. Record the load tax for cold-start tests; keep JIT off during experiments.
- **CLI gotchas:** `lms load` rejects full `org/repo@quant` keys (use short key from `lms ls` or `--exact <file>`); use the exact identifier from `lms ps --json` as the API `model`; `--parallel` only reliably settable via CLI/SDK (REST load rejects it); reasoning models may pad outputs via `reasoning.parsing` unless disabled.
- **Concurrency:** pre-0.4.0 requests to a model are strictly serial + queued (no 429, no rate limiter); 0.4.0+ llama.cpp uses continuous batching / `n_parallel` (default 4), unified KV cache, still queues beyond slots. `lms ps --json` shows queued prediction count. MLX engine: no continuous batching yet.
- **Memory caveats:** unload may not fully release VRAM/RAM (bug-tracker #511/#588), long sessions can leak (fix: periodic daemon restart); guardrails may refuse loads; wrong device assignment can OOM.
- **Auth for LAN binding:** bind `0.0.0.0` + require auth recommended; send `Authorization: Bearer`.

---

## 6. Errors & compatibility

- Native error shape: `{"type":"error","error":{type,message,code?,param?}}` with types `invalid_request | unknown | mcp_connection_error | plugin_connection_error | not_implemented | model_not_found | job_not_found | internal_error` (no enumerated HTTP codes).
- OpenAI-compat errors follow OpenAI format `{"error":{message,type,param,code}}` (0.3.18+: correct format on streaming too). Treat HTTP codes as OpenAI convention (400/404/500); N/A to enumerate.
- Reference clients that drop-in: openai-python/JS (`base_url` swap), Vercel AI SDK `createOpenAICompatible({name:'lmstudio', baseURL:'http://localhost:1234/v1'})`, LiteLLM provider `lm_studio/` + `LM_STUDIO_API_BASE`.

---

## 7. Key decisions these findings imply for the harness

1. Use `/v1/chat/completions` (stateless, message-passing, OpenAI-shaped) or `/api/v1/chat` (richer stats + stateful IDs). v0 is deprecated but is the only surface returning `generation_time`.
2. For image input tests: vision input IS supported on chat endpoints (base64 data URLs) — the harness's rasterized 8×8 char SVGs must be rendered to PNG data URLs.
3. Record `created`, `usage`, `tokens_per_second`, `time_to_first_token_seconds` from responses, plus client-side wall-clock; hardware usage must come from external sampling (nvidia-smi/rocm-smi/psutil) because LM Studio exposes none.
4. Fresh-instance-per-test = explicit `lms load`/unload loop (or `/api/v1/models/load|unload`), JIT disabled, `parallel 1`, and record `model_load_time_seconds`.
5. Streaming usage requires `stream_options.include_usage`; never rely on logs for forensics (format unguaranteed) — the harness should buffer everything itself.

---

### Primary sources
- https://lmstudio.ai/docs/developer/rest · /openai-compat · /openai-compat/chat-completions · /structured-output · /tools · /models · /embeddings · /completions · /responses
- https://lmstudio.ai/docs/developer/rest/endpoints (v0, verbatim JSON) · /chat · /list · /load · /unload · /stateful-chats · /streaming-events
- https://lmstudio.ai/docs/developer/core/server · /core/server/settings · /core/authentication · /core/headless
- https://lmstudio.ai/docs/developer/api-changelog · https://lmstudio.ai/blog/0.4.0 · https://lmstudio.ai/blog/lmstudio-v0.3.26
- https://lmstudio.ai/docs/cli (server-start/stop/status, log-stream, local-models/{load,ls,ps}, runtime)
- Community: lmstudio-ai/lms, lmstudio-ai/lmstudio-bug-tracker (#511,#588,#1874,#2295,…), lms-mon, LM-Studio-Bench, vipinpg.com/lm-studio-metrics, markaicode.com/lm-studio-benchmark, litellm docs, ai-sdk.dev