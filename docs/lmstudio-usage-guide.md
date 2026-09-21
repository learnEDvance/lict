# LM Studio — Scripting & Usage Guide (learned from live testing)

Everything needed to build scripts that drive LM Studio's OpenAI-compatible server effectively and reliably. Based on empirical testing against LM Studio (Windows host, WSL2 client, node v22, `gemma-3-270m-it` F16 GGUF) on 2026-09-19.

---

## 0. Environment quick reference

| Item | Value / command |
|---|---|
| Server base URL (from WSL2) | `http://172.24.144.1:1234` (NAT gateway; NOT the LAN IP) |
| Chat completions | `POST /v1/chat/completions` |
| Model list | `GET /v1/models` |
| CLI | `/mnt/c/Users/Obhi/.lmstudio/bin/lms.exe` |
| GPU query | `/mnt/c/Windows/System32/nvidia-smi.exe` (nvidia-smi on Windows PATH in WSL) |
| CPU/IO counters | Windows `Get-Counter` via `powershell.exe` |
| Node | v22.23.2 (native `fetch`, no deps needed) |

`lms.exe` will see Windows paths & processes: `Get-Counter` filters by process name `llama-server`.

---

## 1. Model lifecycle (the CLI you must master)

```
lms.exe load   <model> --parallel N -y   # load with N parallel slots
lms.exe unload <model>                    # unload (frees VRAM/ram)
lms.exe ps --json                         # status: {status, queued, parallel, contextLength, ...}
lms.exe server start / stop / status      # control the API server itself
lms.exe ls -l                             # list downloaded models
```

- Identifiers shown by `lms.exe ls` / `lms ps` are (mostly) what `/v1/models` returns.
- `--parallel N` sets how many requests can be *served simultaneously* by the llama.cpp/dll backend. This is the single most important knob for load scripts.
- Reloading a model takes ~7–8 s (`lms.exe load` returns after it is ready).
- After a load, `contextLength` is the batch KV size; large `parallel` × large context inflates VRAM.

---

## 2. Effective concurrency model (the hard-won knowledge)

The server behaves like **N serving slots** (`parallel`). Requests beyond N are queued. The practical rules, verified empirically:

1. **Concurrency ≤ `parallel` → 100% reliable.** A 32-step ramp with `parallel = concurrency` (1→32, 512-token outputs) yielded 528/528 success, zero retries, queue depth never > 3, even at 42 s full saturation.
2. **Concurrency > `parallel` → request aborts.** At 35 concurrent on 32 slots, ~25% of requests failed with HTTP 400 at **exactly ~120 s wall**: `{"error":"Engine protocol predict stream returned an error: {\"code\":500,...}"}`. This is a **slot-acquisition timeout**: a pooled/queued request that did not grab a serving slot within ~120 s is killed. Retries only restart the same 120 s clock.
3. **Aggregate throughput caps ~300 tok/s** on this 270M F16 model regardless of concurrency, from ~6 slots up to 32. GPU util stayed < ~50% (avg power ~7–11 W) — the binding constraint is the scheduler/batching layer, NOT the GPU silicon.
4. Per-request latency under saturation: at concurrency N each request ≈ batch span ≈ N × ~1.3 s at a 512-token cap; at 32-deep with 1200-token outputs, successful requests ran ~127–138 s and got ~9–14 tok/s each.
5. **Serial (one request at a time) beats parallel for total throughput** on this hardware: ~121 tok/s aggregate vs ~78 tok/s for a 35-way burst, with 100% vs 75% success. Use parallelism for latency-batching only when you truly need N outputs to overlap, and always when you have many independent short requests.

Consequence → **write load scripts that:**
- introspect `lms ps --json` and avoid launching a next batch until `status != generating` and `queued == 0`;
- keep in-flight ≤ `parallel`, or accept (and count) aborts;
- treat a 400 `code:500 predict stream` at ≈120 s as a *slot-drained* event, not a prompt/model error.

---

## 3. The API

### `GET /v1/models`
```json
[{"id":"gemma-3-270m-it","object":"model","created":...,"owned_by":"lmstudio"}]
```

### `POST /v1/chat/completions`
Request:
```json
{
  "model": "gemma-3-270m-it",
  "messages": [{"role":"user","content":"..."}],
  "max_tokens": 1200,
  "temperature": 0
}
```
Response fields that matter (verified):
- `id` (chatcmpl-…), `created` (1-second granularity; identical for a whole fast batch — use client timestamps for latency),
- `choices[0].finish_reason` (`stop` | `length`),
- `usage.prompt_tokens`, `usage.completion_tokens`, `usage.total_tokens`, `usage.completion_tokens_details.reasoning_tokens` (0 for this non-thinking model).
- **No CPU/GPU/storage queue fields in OpenAI responses** — resource attribution must come from separate sampling (below).
- `speed` etc. would come from `lms.exe log stream --json --stats --source model` → `llm.prediction.output.stats` (`tokensPerSecond`, `timeToFirstTokenSec`, `totalTimeSec`) for exact per-request engine numbers.

### Other routes (checked, mostly absent)
- `/metrics` returns an HTTP-200 body that is actually an error page; ignore.
- `/api/v1/version|server|system|status|runtime|telemetry` → 404 (no telemetry routes in this build).
- Native `/api/v1/chat` is an older passthrough with `tokens_per_second`/`time_to_first_token_seconds` in payload; logs output stats too.

---

## 4. Reliability & retry patterns

- **Drain gate between batches** (IMPORTANT for bursts):
  ```js
  async function awaitIdle(){ for(;;){ const q=queueState(); if(q.status!=='generating'&&q.queued<=0) break; sleep(1000);} sleep(1000); }
  ```
- **Retry with backoff, cap attempts.** Aborts are deterministic per slot-starved window; an immediate retry re-hits the same window. Backoff: 3 s, 6 s, 12 s, … cap ~60 s.
- **Client-side timeouts**: 600 s covers worst serial long-generation; aborts surface as HTTP 400 anyway.
- **Unbounded retry-until-success** is what you want when "the batch must complete" (e.g., hypothesis runs) — but you must accept it can take much longer than nominal when the server enters a collapse window.
- **Compliance check**: instruct the model to begin with an exact token (`TASK3-14`); verify `content.includes(tag)`. 350/350 serial responses did this correctly. Watch for rare **degenerate stubs** (200 OK, ~7-token tag echo in <0.2 s) — treat as an anomaly, not a real completion.

---

## 5. Measuring and attribution

Per-request (client-side, always measured by you):
```
headers_ms  = time to response headers (≈ time-to-first-token + overhead)
wall_ms     = start → response end (full generation)
tok/s       = completion_tokens / (wall_ms/1000)
```

Whole-system sampling (1–2 s interval) while a load runs:
- `lms.exe ps --json` → `status`, `queued`, `parallel` (pairs with client concurrency).
- `nvidia-smi.exe` → whole-GPU power/util/mem (interval ~2 s). Format: `nvidia-smi --query-gpu=power.draw,utilization.gpu,memory.used,clocks.sm,clocks.mem --format=csv,noheader`.
- Windows `Get-Counter` via a persistent-ish powershell loop for per-process `llama-server` CPU/working-set and per-PID GPU engine usage. **PID-based filters are portable; LUID-keyed filter strings are NOT** (they change after reboot).
- **Exact per-request hardware attribution is impossible** at sub-second scale under concurrency. Use: serial runs for trustworthy per-request numbers, or token-weighted totals over sampling intervals for bursts. See `docs/lmstudio-resource-tracking.md`.

Practical sampler (three channels, detached):
```bash
lms.exe ps --json >> ps.csv            # ~1 s loop
nvidia-smi ... csv >> gpu.csv          # ~2 s loop
powershell Get-Counter loop >> win.csv # PID-based llama-server filters
```

---

## 6. Copy-paste templates

### Minimal single request (node, zero deps)
```js
const resp = await fetch('http://172.24.144.1:1234/v1/chat/completions',{
  method:'POST', headers:{'Content-Type':'application/json'},
  body: JSON.stringify({ model:'gemma-3-270m-it',
    messages:[{role:'user',content:'Explain TCP congestion control.'}],
    max_tokens:1500, temperature:0 }) });
const body = await resp.json();
console.log(resp.status, body.choices?.[0]?.message?.content);
```

### Paced fixed-concurrency batch runner (reliable ≤ slots)
See `tools/lmtest/run.js` (mode `parallel`) — Promise.all over a batch, `awaitIdle()` between batches, 3 attempts + backoff, appends JSONL with `tag,start_abs,headers_ms,wall_ms,status,err,errBody,id,created,finish,usage,content_len,first80,hasTag,attempts`.

### Sliding-window pipeline (refill on completion)
See `tools/lmtest/queue.js` (mode `pipeline`) — a fixed pool of in-flight workers; on resolve, a successful request frees a slot for the next queued task; failures retry with backoff until success.

### Concurrency ramp
See `tools/lmtest/ramp.js` — reloads model per level with `--parallel N`, per-batch retries, samples while running.

### Running detached (so the shell returns)
```bash
nohup bash -c 'bash sampler.sh TAG OUT & SP=$!; node runner.js MODE > OUT/log 2>&1; kill $SP; wait $SP' >/dev/null 2>&1 &
```

---

## 7. Pitfalls checklist

- WSL2: use the **NAT gateway IP** (`172.x.x.1` from WSL) for the Windows host, not the LAN IP.
- `/tmp` is wiped on reboot — persist run data + tooling inside the repo.
- LM Studio updates/restarts: reverify `lms ps` (LUID, routes) after any update.
- Don't trust `/metrics`; read `lms.exe log stream --json --stats --source model` instead.
- Node's `created` is second-granularity; measure latency client-side.
- `max_tokens` scaling: long jobs (1200+) saturate slots → prefer serial or concurrency ≤ ~6 for best total throughput.
- Keep boundary conditions real: burst batches that recover via retries still burn the full 120 s abort per attempt.