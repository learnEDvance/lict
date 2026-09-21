# LM Test — Parallel vs Serial A/B (gemma-3-270m-it, F16, LM Studio)

Two phases, same 350 prompts (10 batches × 35; `max_tokens=1200`, temp 0; tags `TASKb-r`):

- **Parallel**: all 35 of a batch fired concurrently; model `parallel=32`; batches gated on server drain-to-idle. Data: `archive/lmtest/parallel.jsonl`.
- **Serial**: same 350 requests, one at a time (`parallel=1`). Data: `archive/lmtest/serial.jsonl`.

Both phases took **~58 min of wall-clock** (parallel 3,530 s; serial 3,204 s).

## Headline results

| metric | Parallel (35-way @ 32 slots) | Serial (1 way) |
|---|---|---|
| Requests | 350 | 350 |
| HTTP 200 | **262 (74.9%)** | **350 (100%)** |
| HTTP 400 (slot-acquisition abort) | 88 | 0 |
| Output tokens produced | 273,926 | 386,611 (≈ 22 of these are 7-token stubs) |
| Wall-clock | 3,530 s | 3,204 s |
| Aggregate throughput | **78 tok/s** | **121 tok/s** |
| Per-request latency (successful) | mean 127.6 s, med 138.2 s | mean 9.2 s, med 9.9 s |
| Per-request throughput | ~9.3 tok/s | ~117 tok/s |
| Response compliance (hasTag) | 262/262 | 350/350 |

Serial produced **~41% more tokens in less wall-clock** and was 100% reliable. Parallel's intended win — wall-clock for the same work — did not materialize: the 35-way burst mostly failed to produce useful output, and the successes were ~14× slower per request.

## Failure anatomy (parallel)
- All 88 failures are HTTP 400, uniform **wall ≈ 120.0 s**, body `{"error":"Engine protocol predict stream returned an error: {code:500 ...}"}` — i.e. a request that **waited >~120 s to acquire a generation slot was killed by the server**. Retries ×3 with backoff all re-failed.
- Failures are **batch-clustered**, not random: batches 1,2,3,5,6,9,10 clean (35/35); batches 4 (31 fail), 7 (28), 8 (29) collapsed. A batch is clean when enough requests grab slots within ~120 s; when generation is slow/unlucky, the queued tail overflows the 120 s budget and the batch flips to ~10–20% success. Batch spans for failing batches were 387–470 s vs 96–435 s for clean ones.
- Consistent with the ramp experiment: **concurrency ≤ slots = 100% reliable** (32/32 at N=32, 42 s saturation, zero failures); **concurrency exceeding slots (35 > 32)** by just 3 pushes some requests past the queue-time cap and degrades reliability to ~75%.

## Serial anomalies (measured, flagged)
- 22/350 serial responses are degenerate stubs: HTTP 200, finish `stop`, content is only the echoed task tag (~6–7 tokens) returned in ~0.12 s instead of a real completion. All other 328 responses are full outputs (mean ~1,176 tokens). These stubs cluster in prompt sets 3/4/5 (tags like TASK3-27, TASK5-34) — see `serial.jsonl` rows with `wall_ms < 1000`. Root cause not pinned (no slot contention in serial); noted for follow-up.
- 291/350 hit the 1200-token `length` cap; 59 stopped early.

## Resource observations
- `nvidia-smi` samples across all phases: GPU util typically 30–50%, sustained power ~7 W (idle-ish). The 270M F16 model saturates an internal scheduler around **~300 tok/s aggregate**, not the GPU silicon.
- Serial per-stream ~117–122 tok/s matches single-stream calibration; parallel successful requests got ~9–14 tok/s each (32-way time-slicing).
- Queue depth never exceeded 3 in any phase, even at heavy saturation.

## Conclusions / answers to the load-test question
1. **LM Studio's native parallel slots are reliable only up to the configured `parallel` count** (`lms load --parallel N`). Concurrency at/below N: 100% success (ramp 1→32). Concurrency above N: queue-time aborts at ~120 s (≈25% failure at 35 vs 32).
2. **Parallelism does NOT win here**: on a 270M model driven by one SSD-backed GPU, aggregate throughput saturates ~300 tok/s by ~6 concurrent slots, so firing 35 (even if fully reliable) wouldn't beat serial. Serial is strictly better for this model/harness: higher aggregate tok/s, lower latency, zero failures.
3. **Practical recipe**: for load/batch jobs on this server use serial (or concurrency ≤ ~6) for best total throughput; keep `max_tokens` moderate to avoid the 120 s slot budget bites; if parallelism must be used, set `parallel` ≥ planned concurrency and add slot-time monitoring.
4. Data is self-contained and reproducible via `tools/lmtest/ramp.js` + `run.js` (modes `ramp|parallel|serial`) + `attrib.py` / `analyze_ramp.py`.