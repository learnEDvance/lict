# LM Studio — Live Server Test Report

Date: 2026-09-18 · Target: `http://172.24.144.1:1234` (LM Studio on Windows host `192.168.1.7`, reached from WSL via NAT gateway)  
Purpose: verify every claim in `docs/lmstudio-api-report.md` against a live LM Studio instance, and prove the image-input pipeline for the test harness. All probes were non-destructive (no model downloads, no settings changes; only the embedding model was loaded+unloaded as part of a controlled lifecycle test). Server state restored at end.

---

## 0. Environment discovered

- HTTP server: **Express** (`X-Powered-By: Express`); no `Server`/version headers anywhere. Auth: **not enforced** (any `Authorization`/`x-api-key` value accepted, 200).
- Installed models (OpenAI `/v1/models`):

| id | type | quant | ctx | vision | tool_use | reasoning | notes |
|---|---|---|---|---|---|---|---|
| `gemma-3-270m-it` | llm | F16 | 32768 | – | – | – | **primary text model**; works |
| `google/gemma-4-12b` | llm | Q4_K_M | 262144 | ✅ | ✅ | ✅(on) | **only usable vision model** (with reasoning off) |
| `google/gemma-4-e4b` | llm | Q4_K_M | 131072 | ✅ | ✅ | ✅ | **cannot load** (`Engine protocol startup was aborted`) |
| `prism-ml/bonsai-27b` | llm | Q1_0 | 262144 | ✅ | ✅ | ✅ | **cannot load** (`{"error":"terminated"}`) |
| `text-embedding-nomic-embed-text-v1.5` | embedding | Q4_K_M | 2048 | – | – | – | works; 768-dim |

- Loaded default config for `gemma-3-270m-it`: `parallel:32`, `flash_attention:true`, `offload_kv_cache_to_gpu:true`, ctx 32768.
- Models auto-load on first request (JIT). Eviction via TTL observed (auto-unloaded between sessions ~minutes).
- Route fallbacks: unknown `/v1/*` path → **HTTP 200** `{"error":"Unexpected endpoint or method. (…)"}`; unknown `/api/v1/*` path → **HTTP 404** same body. `GET /v1/completions` (POST-only) → **200** error body (not 405).

---

## 1. `/v1/chat/completions` (OpenAI-compat)

**Baseline response (verbatim, trimmed):**
```json
{
  "id": "chatcmpl-2q04fcimkdoib350fta2t",
  "object": "chat.completion",
  "created": 1789742286,
  "model": "gemma-3-270m-it",
  "choices": [{ "index": 0,
    "message": { "role": "assistant", "content": "…", "reasoning_content": "", "tool_calls": [] },
    "logprobs": null, "finish_reason": "stop" }],
  "usage": { "prompt_tokens": 13, "completion_tokens": 14, "total_tokens": 27,
             "completion_tokens_details": { "reasoning_tokens": 0 } },
  "stats": {},
  "system_fingerprint": "gemma-3-270m-it"
}
```

**Field audit (live observations):**
- `created` = **epoch seconds, request-receipt time** (matches `Date` header; back-to-back calls share the same value; does not encode completion time). **Not a latency anchor.**
- `id` = opaque `chatcmpl-<26 alnum>`; unique per request.
- `usage` — accurate (verified: truncation at max_tokens 8 → `completion_tokens:8`).
- `stats` = `{}` on the **chat** path; populated on the legacy **completions** path with draft-token counters.
- `reasoning_content`, `tool_calls`, `completion_tokens_details`, `system_fingerprint` are LM Studio cosmetics (always present/defaulted; `system_fingerprint` just echoes the model name).

**Parameter behavior:**
- `temperature:0` + `seed`: outputs identical across repeats (deterministic at temp 0 even without seed).
- `max_tokens` works (`finish_reason:"length"` at exact cap); `max_tokens:-1` = unlimited (self-bounded by EOS: 62 tok for "30 words about dogs").
- `stop` sequences honored (stopped at first token matching philosophy in test).
- `top_p`, `top_k`, `repeat_penalty`, `presence_penalty`, `frequency_penalty`, `logit_bias` all accepted (200); negligible output effect on a 270m model.
- `temperature:3` accepted (no range validation). Nonsense params (`severity`) ignored.
- `draft_model`, `ttl` recognized and **rejected at predict time** if misconfigured (400). `reasoning:{effort:…}` on 270m → **500 crash** — never send.
- `response_format.type` must be `json_schema` or `text`; `json_object` → 400.
- Unknown `model` → **HTTP 200 silently served by the loaded default model** (no error!). A harness must validate `model` itself.
- `messages` missing/empty → 400 with clear body; content-as-array (OpenAI format) accepted.

---

## 2. Streaming (OpenAI SSE)

- Shape: standard — first chunk folds `role`+`content`, middle chunks `delta.content`, final chunk `delta:{}` + `finish_reason`, then `data: [DONE]`. Correlates: `object`=`chat.completion.chunk`, `id`/`created`/`model`/`system_fingerprint` per chunk.
- `stream_options.include_usage:true` → trailing chunk with `choices:[]` + full `usage` (correct OpenAI ordering). Without the flag, no usage in stream.
- **Concatenated deltas == non-streaming content exactly** (byte-identical, verified at temp 0).
- No `delta.reasoning_content` emitted in streaming (even when non-stream shows the field).
- Errors mid-stream: validation errors → plain JSON 400 (no SSE). Bad model → **200 then non-standard** `event: error` + `data:{"error":{"message":"terminated"}}` (no `[DONE]`).
- Measured pacing (gemma-3-270m-it, warm): TTFT ≈ **76 ms** end-to-end; steady ≈ **8 ms/chunk**; stream-to-`[DONE]` ≈ 227 ms.
- Legacy `/v1/completions` streaming works; deltas in `choices[].text`; final chunk carries `usage` **inline** (no separate usage chunk) and always (no flag needed).

---

## 3. Embeddings

- Works: `object:"list"`, `data[0].embedding` length **768**, model echoed exactly, `usage` present but **all-zero** (don't trust usage).
- Batch input (array) works; `dimensions:128` **silently ignored** (still 768).
- Semantics sane: `cos(cat-phrase, cat)` 0.74 > `cos(cat, dog)` 0.50; L2 norm = 1.0.
- Errors: no `model` or LLM-as-model → 400 `"No models loaded. Please load a model..."` (misleading message). Empty input accepted (embeds empty string).

---

## 4. Anthropic-compat `/v1/messages` and `Responses` `/v1/responses`

- `/v1/messages`: **exists**, non-SSE JSON, Anthropic shape `{id:"msg_…", type:"message", content:[{type:"text",…}], usage:{input_tokens, output_tokens, cache_read_input_tokens}}`, `stop_reason:"end_turn"`. Works with/without any key. (Auth not enforced.)
- `/v1/responses`: **exists**. Non-stream: full `resp_…`/`status:"completed"`/`output` shape. Stream: 19 SSE events `response.created…response.completed` (no `[DONE]`; carries `sequence_number`).

---

## 5. Tool calling & structured output — verdicts

| capability | result |
|---|---|
| Tool call emission (gemma-3-270m-it) | **NO** — model returns prose, `tool_calls:[]`, finish `length`/`stop` |
| `tool_choice:"required"` | accepted (200) but ignored on 270m |
| `tool_choice` object form `{type:"function",…}` | **400** `"Invalid tool_choice type: 'object'"` |
| Multi-turn tool protocol (`role:"tool"`) | **400** — Gemma template rejects: *"Conversation roles must alternate user/assistant"* |
| `response_format` `json_schema` (strict, additionalProperties:false) | ✅ **WORKS** — valid JSON string in `message.content` |
| `response_format` `json_object` | 400 (unsupported value) |

**Conclusion:** do **not** build the harness on function calling. Do use forced `json_schema` for any structured output.

---

## 6. Vision / image input — THE core result ✅

Assets: our generator rasterized the 8×8 grid into 64×64 PNGs (`L`, `H`, `A`, `7`).

- `google/gemma-4-12b` is the **only working vision model**; it defaults to **reasoning ON**, which steals the entire small `max_tokens` budget → empty `content` (`finish_reason:"length"`, all tokens in `reasoning_content`). Must disable reasoning:
  - OpenAI endpoint: top-level **`"reasoning_effort":"none"`** → clean single-char answers.
  - Native `/api/v1/chat`: **`"reasoning":"off"`** (rejects `reasoning_effort`/`reasoning:false`).
- With reasoning off, **4/4 glyphs transcribed correctly and deterministically** (L→"L", H→"H", A→"A" ×3, 7→"7"), 2 tokens/request.
- OpenAI `image_url` data-URL format and native `{type:"image", data_url}` both work.
- Non-vision model (`gemma-3-270m-it`) fed an image → **HTTP 400** `"The provided messages contain images, but gemma-3-270m-it does not support image inputs."` — clean rejection, no hallucination. Route images only to the vision model.

---

## 7. Native `/api/v1` API

- `POST /api/v1/chat` — rich consistent telemetry on every request: `{input_tokens, total_output_tokens, reasoning_output_tokens, tokens_per_second, time_to_first_token_seconds}` + `model_instance_id` + `response_id` (`resp_<40hex>`). `model_load_time_seconds` appears **only** when a load occurred in that request (cold start).
- Stateful chats: `store:true` → `response_id`; `previous_response_id` continuation works (prompt_tokens grew 19→48, prior turn concatenated). `store:false` → no `response_id`.
- Streaming events (ordered): `chat.start → [model_load.start/progress/end] → prompt_processing.start/progress/end → message.start/delta/end → chat.end`. `chat.end.result.stats` carries the full aggregated stats + `model_load_time_seconds` (2.907 s when cold). No per-token timing on `message.delta`.
- Lifecycle (`/api/v1/models/load|unload`): load `text-embedding-nomic-embed-text-v1.5` → `{type:"embedding", instance_id, load_time_seconds:2.467, status:"loaded"}`; unload → `{instance_id}`; verified unloaded after. Loaded LLM left untouched and **restored** to gemma-3-270m-it at end.
- Errors: structured envelope `{"error":{message,type,code,param}}`.

---

## 8. Timing study (gemma-3-270m-it)

- `created` matches wall-clock `t_start`/`t_end` within ~1 s and ticks with the real-time clock once per second — **request-receipt second, not per-request unique**.
- Warm single-token-output latency: mean ≈ **225 ms ± 138** (trimmed mean ≈ **156 ms ± 12**; one 500 ms outlier trial).
- 3 concurrent requests: 379–398 ms each, all 200, total span ≈ 398 ms (true overlap, no queueing at n=3; parallel=32 holds).
- Long prompt (2,466 tokens): 642 ms, prefill ≈ **3,860 tok/s**, wall grew only **2.85×** for a **224×** token increase (sub-linear prefill; no tok/s degradation).
- Decode ≈ **77–82 tok/s**; `max_tokens:-1` self-bounded (62 tok, `stop`).
- `stats` always `{}` on chat → **compute tok/s client-side**.

---

## 9. Reliability / gotchas (observed live)

1. Intermittent first-call failures: `Engine protocol predict request failed: fetch failed` (400), `Failed to load model … Operation canceled` (400), or an HTML 500 — then succeed on immediate retry. Harness must **retry once** on these.
2. JIT/TTL eviction: idle models unload after a TTL (~minutes); next call cold-loads (load appears in stats / takes seconds). Frozen tests should pre-warm with one throwaway request or use explicit load.
3. `gemma-4-12b` default reasoning-on can swallow `max_tokens` → empty `content`; always send `reasoning_effort:"none"` (OpenAI) / `reasoning:"off"` (native) for transcription.
4. Unknown model ids silently fall back to the loaded model (200) — validate model names client-side.
5. Auth is disabled and route-not-found returns 200 — don't rely on HTTP status for endpoint-existence checks.
6. Multiple loaded models coexist (gemma-3-270m-it + gemma-4-12b both resident at once); loading a new model can evict others — pin a single model per test to keep state predictable.

---

## 10. Harness design recommendations

- **Text pipeline:** `POST /v1/chat/completions`, model `gemma-3-270m-it`, `stream_options.include_usage:true` when streaming; record `id`, `created`, `usage`, `finish_reason` + client wall-clock (`t_sent`, `t_first_byte`, `t_done`).
- **Image pipeline:** `POST /v1/chat/completions`, model `google/gemma-4-12b`, images as base64 `image_url` data-URLs (PNG, 64×64 from our rasterizer), **`reasoning_effort:"none"`**, `temperature:0`, small `max_tokens`.
- **Telemetry per request:** as in `docs/lmstudio-api-report.md` §4, with `tokens_per_second` computed client-side (`completion_tokens / generation wall`). `created` is only a coarse timestamp anchor.
- **Isolation per test:** explicit `POST /api/v1/models/load` → run → `POST /api/v1/models/unload` (or keep `gemma-3-270m-it` resident and reset context by starting a fresh conversation with `store:false`). One retry on transient engine errors; treat `reasoning_effort`/`reasoning` as mandatory controls on the 12b.
- **Non-goals:** tool calling, `json_object`, embeddings `dimensions`, per-request hardware telemetry (not available — use external `nvidia-smi`/`rocm-smi` sampling if needed).

---

## 11. 35-way concurrency & per-request tracking (gemma-3-270m-it)

- **Test:** 35 concurrent `POST /v1/chat/completions`, each a ~57-token prompt (`Task <i> … begin with "TASK<i>"`) + `max_tokens:384`, `temperature:0`, all fired in one `Promise.all` burst. Client captured `t_start`/`t_headers`/`t_end` per request; server-side state polled via `lms ps --json` at ~400 ms throughout.
- **Throughput:** 35/35 succeeded — zero errors, zero retries. Burst span **58.6 s** vs serial estimate **25.6 min** → **×26 speedup**. Aggregate output ≈ **229 tok/s** (13,440 completion tokens / 58.6 s) vs ~80 tok/s single-stream → multi-request batching lifts system throughput ~3× while per-request latency collapses (mean wall **43.8 s** ≈ 10.5 tok/s/request under 32-way contention vs ~5 s for the same 384 tokens unloaded).
- **Queueing (server view):** `lms ps --json` showed **`queued:3`** from burst start (35 requests vs `parallel:32` → exactly 3 oversubscribed), dropping 3→2 at 39.8 s, 2→1 at 41.1 s, 1→0 at 42.3 s; server `idle` at 58.2 s. Matches client-side shape: a wave of ~30 finishing ≈42 s and five stragglers carried to ~58 s (tasks 10, 11, 34, 35…). 46/80 samples `generating`, 34 `idle`; `contextLength` reports the full slot window.
- **Per-request tracking — what works and what doesn't under concurrency:**
  - ✅ **`id`** is unique per request (35/35 distinct `chatcmpl-*`) — the only unambiguous server-side request handle; without it, 35 concurrent bodies are indistinguishable.
  - ✅ **`usage`** is exact per request; `completion_tokens` hit exactly 384 with `finish_reason:"length"` on all 35.
  - ✅ **Response→request correlation:** prompt tags were echoed — 35/35 responses contained their own `TASK<i>`, so content reliably links each response to its request.
  - ⚠️ **`created`** was the **same value for all 35** (single second-resolution epoch, ≈ −430 ms from client start) → cannot order, count, or queue-measure requests; it is a per-batch receipt second, not a unique timestamp.
  - ❌ No per-request server timing/queue fields on the `/v1` chat path (`stats:{}`) → latency/queue must be reconstructed client-side (wall splits + per-tag verification) and server-side via `lms ps --json` polling.
- **Practical tracking recipe:** unique client task id ↔ server `id`, record `t_sent/t_first_byte/t_done`, verify a payload tag in output, snapshot `lms ps --json` for `status`/`queued`, and treat `created` only as a coarse clock anchor.

---

### Artifacts
Raw request/response dumps were kept in the agent sandbox temp dirs during testing; the timing dataset (`a_trial_*.json/.meta`, `c_*.json`, `d_*.json`, `e_none.json`) is in `/tmp/opencode/lmtest/out/timing/REPORT.md`; the 35-way concurrency run (`results.json` with all 35 request rows, `report.json` aggregate stats, `warmup.json`, `ps_samples.txt` 80 server-state samples) is in `/tmp/opencode/lmtest/out/conc35/`. Server left in a clean state: `gemma-3-270m-it` loaded, everything else unloaded.