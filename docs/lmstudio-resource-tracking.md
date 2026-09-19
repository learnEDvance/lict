# LM Studio — Per-Request Resource Tracking: Capability & Recorder Design

Date: 2026-09-19 · Target: `http://172.24.144.1:1234` (LM Studio on Windows host, reached from WSL2 via NAT gateway `172.24.144.1`)  
Scope: determine the maximum achievable per-request resource-usage tracking (CPU %, GPU %/VRAM/MHz, RAM, disk I/O, power) for requests to this LM Studio server, with verified commands and a concrete recorder design. All findings below were **verified live** on 2026-09-19 (LM Studio 0.4.24 build 1, llama.cpp engine `2.41.0`, NVIDIA driver 32.0.16.1064 / nvidia-smi 610.64, CUDA 13.3). No settings/app code were modified; only two tiny test requests (temp 0, `max_tokens` ≤ 8) plus GET probes were made; server state restored to "nothing loaded" afterwards.

---

## A. Executive summary — what is achievable and its accuracy ceiling

**Verdict: exact per-request hardware attribution is NOT possible. Interval-integrated attribution IS possible, at process/GPU granularity, with a ~1 s resolution ceiling for the GPU/process counters and ~100 ms best-case via nvidia-smi.**

Why exact per-request attribution fails (three structural reasons, all verified):

1. **The server exposes no hardware telemetry.** Every candidate endpoint (`/api/v1/status`, `runtime`, `telemetry`, `metrics`, `system`, `compute`, `logs`, `jobs`, `queue`, `cache`, …) is either 404 or a plain error envelope. The OpenAI-compat `/v1/chat/completions` returns `stats: {}`; the native API returns only token/timing stats (see §B). No per-request CPU/GPU/RAM/disk/power fields exist anywhere on the HTTP API.
2. **All inference for a model runs in one process.** The entire server + engine live in `LM Studio.exe` (PID 15940, owns port 1234). Compute is handed to a child **`llama-server.exe`** (observed PID 7672; one per loaded model). GPU/CPU/VRAM counters exist **per process**, never per request — concurrent requests to the same model are indistinguishable in any hardware counter.
3. **The Windows hardware counters themselves are ~1 Hz-native** (`Get-Counter` samples the GPU Engine / GPU Process Memory / Process counters roughly once per second). A 3-token request completing in ~92 ms is invisible to a 1 s counter sample except as a small bump. Per-request accuracy is therefore only meaningful for requests that last ≳1 s, or for an *aggregate burst*.

**What the achievable ceiling is:**

| Resource | Best achievable per-request attribution | Verified method | Notes / errors |
|---|---|---|---|
| GPU utilization % | Process-level (`llama-server.exe` PID) via `\GPU Engine(pid_<llama>_…_engtype_*) \Utilization Percentage` — attribute per request only by time-bucketing | `Get-Counter` | ~1 s refresh; during a 92 ms request a 1 s sample reads ~10 % × duty cycle |
| VRAM (MiB) | Process-level dedicated memory via `\GPU Process Memory(pid_<llama>_…)\Dedicated Usage` (measured **1239 MB** for this model) | `Get-Counter` | Static per loaded model; per-request *delta* is KV-cache growth inside the shared unified KV cache → not separable per request |
| GPU clocks / power / aggregate util | Whole-GPU only, via `nvidia-smi -lms 100` query loop (SM 210→1500 MHz, mem 405→5501 MHz, power 4–14 W spikes seen) | `nvidia-smi` | `power.draw` only; no per-process power; `\Power Meter(*)` returns 0 on this box |
| CPU % | Process-level via `\Process(llama-server)\% Processor Time` (and per CPU-usage process variants) | `Get-Counter` | Same ~1 s ceiling; precise *elapsed* CPU-seconds available from `Get-Process` CPU property |
| RAM (private/working-set) | Process-level via `\Process(llama-server)\Working Set` / `Private Bytes` (measured 1.34 GB WS / 2.29 GB private) | `Get-Counter` | Mostly static while model loaded; per-request delta is quantization buffers/graph — negligible & inseparable |
| Disk I/O | Process-level `\Process(llama-server)\IO Read/Write Bytes/sec` + whole-system `\PhysicalDisk(_Total)\Disk Bytes/sec` | `Get-Counter` | During inference ~0 (model is paged in RAM); real I/O is model mmap load on cold start |
| Power (system) | NOT available | — | `\Power Meter(*)\Power` = 0 on this laptop (no sensor exposed); WSL can't read motherboard power |

**Practical accuracy statement:** for a single isolated request of ≥ ~1 s duration, the elapsed-CPU-seconds, process/GPU util %, and VRAM high-water can be attributed to that request with small error (process counters are exclusive to `llama-server`; while only one model is loaded, `llama-server` *is* the request's compute). For requests shorter than ~200 ms (typical for `gemma-3-270m-it`), any %-based counter is noise; you can still attribute **wall-time-exclusive CPU seconds** and **token-derived work** precisely.

---

## B. Verified API surface (what exposes what)

### B.1 Telemetry endpoints — all fail

Read-only GET probes on `http://172.24.144.1:1234` (each `-m 5`):

| Path | Result |
|---|---|
| `/api/v1/models` | ✅ **200** — model/instance registry (below) |
| `/api/v1/models/{id}`, `/api/v1/models/search`, `/api/v1/models/{id}/load-config` | ❌ 404 `{"error":"Unexpected endpoint or method. (GET …)"}` |
| `/api/v1/status`, `/runtime`, `/server`, `/logs`, `/jobs`, `/queue`, `/cache`, `/telemetry`, `/system`, `/compute`, `/health`, `/version`, `/info`, `/metrics`, `/config`, `/server-settings`, `/load-config` | ❌ 404 same envelope |
| `/api/v0/models` | ✅ **200** — model list incl. per-model `state: loaded/not-loaded` (no hardware) |
| `/api/v0/embedding`, `/api/v0/chat` (GET) | ❌ **HTTP 200** with error body (v0 fallback is 200) |

Route-fallback quirk (from prior report, re-confirmed): unknown `/api/v1/*` → 404; unknown `/v1/*` and `/api/v0/*` → HTTP 200 error bodies. **HTTP status is not a reliable existence test.**

The only "config-ish" data the API exposes (verified, `GET /api/v1/models`) is the **loaded instance config**:

```json
"loaded_instances": [{ "id": "gemma-3-270m-it",
  "config": { "context_length": 32768, "eval_batch_size": 32768, "physical_batch_size": 2048,
              "parallel": 32, "flash_attention": true, "context_checkpoints": 32,
              "speculative_draft_*": false/…, "offload_kv_cache_to_gpu": true },
  "remaining_ttl_seconds": 432 }]
```

No field anywhere describes measured CPU/GPU/RAM/disk/power.

### B.2 `lms.exe` CLI surface (verified verbatim)

```
lms --help:        chat get load unload ls ps import | server{start,stop,status}
                   log{stream} | link | runtime{ls,select,remove,update,get,survey}
                   dev{clone,push,dev,login,logout,whoami}
```
No `metrics`/`telemetry`/`top` command exists. Useful options:
- `lms ps --json` → per-loaded-model `{modelKey, path, sizeBytes, quantization{F16,16}, identifier, ttlMs, lastUsedTime, status: idle|loading|generating, queued, parallel, contextLength, …}`
- `lms server status --json` → `{"running":true,"port":1234}` (nothing more)
- `lms server start` → `-p/--port, --bind, --cors` (no flags for metrics)
- `lms runtime survey --json` → static **capacity**: CPU name (`AMD Ryzen 7 7445HS w/ Radeon 740M Graphics`), RAM 16,395,882,496 B, VRAM 4,294,443,008 B, `gpuInfo` (RTX 3050 Laptop, deviceId 0, CUDA, compute 8.6), `visibleDevices[0]`. No live usage.
- `lms log stream` → **the per-request timing source** (see B.3).

### B.3 `lms log stream` — the server's only live per-request telemetry (KEY FINDING)

`lms log stream --json --stats --source model` pushes two structured events per request:

```json
{"timestamp":1789795491321,"data":{"type":"llm.prediction.input",
  "input":"<bos>…user…", "modelPath":"…gemma-3-270m-it-F16.gguf","modelIdentifier":"gemma-3-270m-it"}}
{"timestamp":1789795491321,"data":{"type":"llm.prediction.output","output":"Ok\n",
  "stats":{"stopReason":"eosFound","tokensPerSecond":57.24098454493418,"timeToFirstTokenSec":0.057613,
           "totalTimeSec":0.092553,"promptTokensCount":13,"predictedTokensCount":3,"totalTokensCount":16},
  "modelIdentifier":"gemma-3-270m-it"}}
```

So per request the server exposes: **tokensPerSecond, timeToFirstTokenSec, totalTimeSec, prompt/predicted/total tokens, stopReason** — mirroring the native-API stats. No request-id links the event to a `chatcmpl-*`, so correlation is by timestamp + model + client window.

`lms log stream --json --source server` additionally streams the **llama.cpp engine trace**, including a per-request `slot print_timing` line and the load path:

```
[DEBUG] 0.00.067.438 I srv load_model: loading model 'C:\Users\Obhi\.lmstudio\models\…\gemma-3-270m-it-F16.gguf'
[DEBUG] 0.02.187.708 I slot... id 31 | task 0 | prompt eval time =  57.61 ms / 13 tokens (4.43 ms / tok, 225.64 tok/s)
[DEBUG] 0.02.282.698 I slot print_timing: id 31 | task 0 | eval time = 34.94 ms / 3 tokens (17.47 ms / tok, 57.24 tok/s)
                           total time = 92.55 ms / 16 tokens · graphs reused = 2
[INFO][gemma-3-270m-it] Generated prediction: { "id": "chatcmpl-58ap…", … }
```

Also observed: the engine spins an **internal llama.cpp HTTP server** (`llama_server: listening on http://127.0.0.1:<ephemeral>`; observed port 59934) that could in principle expose llama.cpp `/metrics`+`/slots`. **It is bound to Windows 127.0.0.1 only and is NOT reachable from WSL2** (verified `curl` to 127.0.0.1/172.24.144.1/192.168.1.7:59934 → no connection) — treat the internal server as out of reach.

### B.4 HTTP response headers (no resource data)

Verbatim headers from a tiny `POST /v1/chat/completions` (temp 0, `max_tokens 4`):
```
HTTP/1.1 200 OK
X-Powered-By: Express
Access-Control-Allow-Origin: *
Access-Control-Allow-Headers: *
Content-Type: application/json; charset=utf-8
Content-Length: 598
ETag: W/"256-…"
Date: Sat, 19 Sep 2026 05:24:51 GMT
Connection: keep-alive
Keep-Alive: timeout=5
```
No `X-Request-Id`, no `Server-Timing`, no trace/`X-*` id, no `Set-Cookie`. `system_fingerprint` just echoes the model name; `created` is a coarse receipt-second that is identical across concurrent requests.

### B.5 On-disk logs/caches

- `.lmstudio/server-logs/` is **empty** (HttpServer file logging is `"off"` in `.internal/http-server-config.json`; the buffer lives in memory and is only drainable via `lms log stream`).
- `AppData/Roaming/LM Studio/logs/main.log` (Electron main log) records only load-time decisions (GPU config, `Live GPU memory info … Used 533.97 MB` for this model, `Model load size estimate … 559.12 MB + Context 294.96 MB = 854.08 MB`). **No per-request lines.**
- `.internal/api-prediction-history/packs/*.json` stores per-request input/output **content only** (no timings, no resources).

---

## C. Windows-side counter inventory — verified on this box

All commands below were executed from WSL2 via `/mnt/c/...` and produced the shown output. `typeperf` is **blocked** (needs Performance Log Users group/elevation) but `powershell Get-Counter` **works unprivileged** — use PowerShell.

### C.0 Process map (verified)
```
LM Studio.exe   PID 15940 (parent of everything; owns 0.0.0.0:1234 and the llmster engine)
└─ llama-server.exe PID 7672   ← the compute worker for gemma-3-270m-it
   (exe: .lmstudio\extensions\backends\llama.cpp-win-x86_64-nvidia-cuda12-avx2-2.41.0\llama-server.exe)
└─ LM Studio.exe gpu-process PID 8088 (Electron UI, ~58 MB on NVIDIA)
```
Expected: one `llama-server.exe` per *loaded model* (verify with `Get-CimInstance Win32_Process | ? {$_.Name -like 'llama-server*'}`).

### C.1 GPU — nvidia-smi (works; the fast + power + clocks path)
```bash
# whole-GPU, ~100 ms loop (works; long-form --loop-ms does NOT, use -lms):
/mnt/c/Windows/System32/nvidia-smi.exe -lms 100 --query-gpu=timestamp,pstate,power.draw,utilization.gpu,utilization.memory,memory.used,memory.free,clocks.sm,clocks.mem --format=csv,noheader,nounits
# process list (WDDM caveat: used_gpu_memory = [N/A]):
/mnt/c/Windows/System32/nvidia-smi.exe --query-compute-apps=pid,process_name,used_gpu_memory,used_memory --format=csv,noheader,nounits
/mnt/c/Windows/System32/nvidia-smi.exe dmon -s puctm -d 1         # aggregate table (1 s)
```
Observed this box: idle `P8, 4.0 W, 210 MHz SM / 405 MHz mem, 0 %`; during model load + request `P3, 12–14 W, SM→1500 MHz, mem→5501 MHz, util 5–6 %`; resident VRAM after load `1280 MiB` (nvidia-smi) / `1239 MB` (counter, §C.2). Power limit column is `[N/A]` in WDDM query output.

### C.2 GPU — per-PID utilization + VRAM counters (the attribution crux)
```bash
# per-process-per-engine utilization (compute engines are the ones that matter):
powershell.exe -NoProfile -Command "(Get-Counter -Counter '\\GPU Engine(*)\\Utilization Percentage' -SampleInterval 1 -MaxSamples 1).CounterSamples | % { '{0} = {1:N1}' -f \$_.Path,\$_.CookedValue }"
# per-process-per-GPU dedicated/shared VRAM:
powershell.exe -NoProfile -Command "(Get-Counter -Counter '\\GPU Process Memory(*)\\Dedicated Usage' -SampleInterval 1 -MaxSamples 1).CounterSamples | % { if (\$_.CookedValue -gt 0) { '{0} = {1:N1} MB' -f \$_.Path,(\$_.CookedValue/1MB) } }"
```
Verified outputs: engine paths appear as `\GPU Engine(pid_7672_luid_0x00000000_0x00208c64_phys_0_eng_2_engtype_compute 0)\utilization percentage`; VRAM as `\GPU Process Memory(pid_7672_luid_0x00000000_0x00208c64_phys_0)\dedicated usage = 1239.2 MB`.

**LUID↔GPU mapping (verified via `\GPU Adapter Memory(*)\Dedicated Usage` plus nvidia-smi cross-check):** LUID `0x208c64` = **NVIDIA RTX 3050** (1300.5 MB used), LUID `0xf118` = **AMD Radeon 740M iGPU** (262 MB — desktop/terminal/Electron rendering), `0x10e8e` = unused. **The model lives on the NVIDIA GPU** (`pid_7672` under `0x208c64`). Filter counter samples by the llama-server PID **and** the NVIDIA LUID to exclude UI traffic.

### C.3 CPU, RAM, disk — per-process counters
```bash
powershell.exe -NoProfile -Command "(Get-Counter -Counter '\\Process(llama-server)\% Processor Time','\\Process(llama-server)\Working Set','\\Process(llama-server)\Private Bytes','\\Process(llama-server)\IO Read Bytes/sec','\\Process(llama-server)\IO Write Bytes/sec','\\Process(llama-server)\IO Read Operations/sec','\\Process(llama-server)\IO Write Operations/sec','\\PhysicalDisk(_Total)\\Disk Bytes/sec','\\Memory\\Available Kbytes','\\Processor(_Total)\\% Processor Time' -SampleInterval 1 -MaxSamples 1).CounterSamples | % { if (\$_.CookedValue -ne 0) { '{0} = {1}' -f \$_.Path,\$_.CookedValue } }"
```
Verified: `% Processor Time` works (use **`% Processor Time`, not `% Processor Usage`** — the latter doesn't exist and errors `c0000bb9` / typeperf says "No valid counters"). Idle llama-server: Working Set **1.34 GB**, Private Bytes **2.29 GB**. `\Process(llama-server)` may have `#1..#n` instances for multiple models.
Per-process aggregate CPU-seconds also available (no counter set needed): `(Get-Process llama-server).CPU` (elapsed CPU in seconds / ProcessTime) and `.WorkingSet64`.

### C.4 Power — effectively unavailable
`\Power Meter(*)\Power` counter set exists but returns **0** on this box (no exposed sensor). GPU power is available only whole-GPU via nvidia-smi `power.draw`. No per-process or CPU-package power path from WSL.

### C.5 System identity (verified)
CPU `AMD Ryzen 7 7445HS w/ Radeon 740M` (AVX/AVX2), RAM 16.4 GB (≈7.1 GB free), GPU `NVIDIA GeForce RTX 3050 Laptop GPU` 4 GB + iGPU. Storage: models at `C:\Users\Obhi\.lmstudio\models\<owner>\<model>-GGUF\…gguf`; `gemma-3-270m-it-F16.gguf` = **542,834,816 B (518 MiB)** and is the only on-disk read hotspot per cold load; during warmed inference there is essentially no disk I/O (model is cached in RAM).

---

## D. Recommended recorder design

### D.1 Architecture
One WSL-side recorder process per load test that:
1. **Never fires its own requests.** It *observes* a burst generated by the harness while the recorder snapshots counters.
2. Runs **four collectors in parallel**, each appending timestamped CSV to its own file:
   - **A. server timing** — one persistent `lms.exe log stream --json --stats --source model`; parse `llm.prediction.output` to get per-request `{t_server_finish, tokensPerSecond, timeToFirstTokenSec, totalTimeSec, promptTokensCount, predictedTokensCount}` (correlate by `modelIdentifier` + finish time).
   - **B. queue state** — `lms.exe ps --json` at ~1 Hz → `status`, `queued`, `parallel`, `remaining TTL` (proves when the server was actually `generating` vs `idle`, and shows oversubscription).
   - **C. process/GPU counters** — a PowerShell loop at ~1 Hz over: `\GPU Engine(pid_<llama-server>_*)\Utilization Percentage`, `\GPU Process Memory(pid_<llama-server>_*)\Dedicated Usage` and `\Shared Usage`, `\Process(llama-server)\% Processor Time`, `\Working Set`, `\IO Read/Write Bytes/sec`.
   - **D. GPU aggregate** — `nvidia-smi.exe -lms 100 --query-gpu=timestamp,power.draw,utilization.gpu,memory.used,clocks.sm,clocks.mem` (whole-GPU power/clocks/util; the only sub-second stream).
3. The harness logs **client-side** per request: unique `client_request_id`, payload tag (echoed by the model), `t_sent`, `t_first_byte`, `t_done`, and (from the response) `id`/`chatcmpl-*`, `usage`, `created`, `system_fingerprint`. **Server state is free of per-request totals** → all resource numbers below are *derived*.

### D.2 Interval-integrated attribution method
For each counter stream, maintain a **time series of samples** `(t_k, v_k)`. Build a per-request window `W_r = [t_sent_r, t_done_r]` (client wall clock) and attribute:

- **% -type counters (GPU Engine util, Process CPU %, disk I/O rate):** integrate by area. `contribution_r = Σ_k v_k·overlap_k / period_k` where `period_k` is the sample period containing the busy component. Because `Get-Counter` reports an average over its ~1 s sample window, a request fully inside one sample yields `v_k` ≈ busy_duration/sample_window × 100 — correct if you rescale by the sample's real span. **For serial (non-overlapping) requests this is exact up to counter refresh quantization (~±1 sample period).**
- **Level counters (VRAM dedicated, working set, private bytes):** record high-water and baseline: `attributed_resident = max(v during W_r) − v before first request` when starting from an unloaded model; otherwise take the delta across the *load* interval only, and treat post-load VRAM as a fixed per-model constant to add to each request's footprint (state that it is not per-request separable).
- **Elapsed-CPU / tokens:** the most exact per-request numbers are `completion_tokens`, `prompt_tokens`, `timeToFirstTokenSec`, `totalTimeSec` (server-stats), and, for CPU, `(Get-Process llama-server).CPU` over the window (true CPU-seconds, not a %).

**Recommendation (practical baseline recipe):**
1. **Serial, clean-window runs for budget calibration:** set `parallel:1` (or unload the second model so only one `llama-server` exists), fire requests ≥1 s apart, keep `max_tokens` large enough that decode dominates (≳1 s). Collect A–D + client windows. Per-request CPU-s, GPU-util %, and VRAM high-water are then unambiguous.
2. **Burst runs for throughput curves:** keep A–D running, attribute counters to the *burst* `[min t_sent, max t_done]`, then apply token-weighted proration to each request using `usage.total_tokens`. Report per-request numbers with the explicit caveat "shared-process attribution, token-weighted."
3. **Overlap validation:** sum the per-request attributed CPU-% / GPU-util-% across a serial batch and compare with the process/GPU counter sums over the same total span — they should match to within one sample period. For bursts, compare burst-total GPU-util × duration ⌊ matches ⌋ the integral of the nvidia-smi GPU util stream; the residual is the overlap error. If residuals exceed ~10 % at your cadence, raise `max_tokens`/request length or drop the proration claim.
4. **Discovery & hygiene:** discover llama PID = `Get-CimInstance Win32_Process | ? {$_.Name -like 'llama-server*'}`; confirm the NVIDIA LUID once per session via `\GPU Adapter Memory(*)\Dedicated Usage` matching nvidia-smi's `memory.used`; confirm one llama-server per model before drawing per-model conclusions.

### D.3 Cadence / resolution summary
| Stream | Cadence | Resolution available |
|---|---|---|
| `lms log stream --stats` (server timing/tokens) | event-driven (per request) | exact |
| `lms ps --json` (status/queued) | 1 Hz (launcher overhead ~200 ms/iter) | 1 s |
| `Get-Counter` GPU Engine / GPU Process Mem / Process | ~1 Hz native | 1 s |
| `nvidia-smi -lms 100` (GPU aggregate) | 100 ms | 100 ms (power/clocks/util/mem) |
| client-side wall clock | event-driven | exact per request |

**Per-request attribution accuracy ceiling:** exact for per-request tokens, TTFT, total-time, and (with serial runs) elapsed CPU-seconds and VRAM high-water; ±~1 s / ~10 % for %-counters on sub-second requests; whole-GPU power/clocks only in aggregate, not per request.

---

## E. What is NOT possible

- ❌ **Any per-request hardware field returned by the server.** No endpoint, response field, or header carries CPU/GPU/RAM/disk/power. `stats` on the OpenAI-chat path is `{}`; headers carry only HTTP metadata.
- ❌ **Per-request VRAM attribution.** The KV cache is unified and shared across parallel slots; a request's VRAM "use" is inseparable from the loaded model + cache baseline. You can only measure the model-level footprint and the request-visible context growth (exposed only at engine internals you can't reach from WSL).
- ❌ **Per-request power.** Only whole-GPU `power.draw` from nvidia-smi; no process or package power (`\Power Meter` = 0).
- ❌ **Sub-second hardware resolution.** `Get-Counter` refreshes ~1 Hz; sub-VRAM-ms spikes are missed. 100 ms is the floor (nvidia-smi loop).
- ❌ **Distinguishing concurrent requests on the same model** in any hardware counter — one `llama-server` process serves all parallel slots; only token-weighted proration is possible.
- ❌ **Reaching the internal per-model llama.cpp HTTP server** (bound to Windows loopback; unreachable from WSL2) — its `/metrics`/`/slots` would have been the only finer-grained source.
- ❌ **Disk per-request separation** during warmed inference (≈0 I/O anyway; only cold model mmap is visible, whole-process).
- ❌ **Exact token throughput on the OpenAI path** (`stats:{}`) — derivable as `completion_tokens / (t_done − t_first_byte)`, or exact from `lms log stream --stats` / the native `/api/v1/chat`.

---

### Appendix — verification artifacts
Live-captured during this investigation (kept in the agent sandbox): response headers/body of the two tiny requests, `lms log stream` model+server event streams, nvidia-smi 1 s sample series showing the load spike, GPU Engine / GPU Process Memory / Process counter enumerations, and `lms ps --json` samples. Server left idle with no models loaded (matching discover-time state).