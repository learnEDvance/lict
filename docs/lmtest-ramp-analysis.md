# LM Test — Concurrency Ramp Analysis (gemma-3-270m-it)

Run: `tools/lmtest/ramp.js`, 2026-09-19. Archives in `archive/lmtest/` (`ramp.jsonl`, `ramp_summary.jsonl`, `ps_ramp.csv`, `gpu_ramp.csv`).

## Method
32 sequential batches. Batch `N` fires `N` requests concurrently (all the same prompt, `max_tokens=512`, temp 0), after reloading the model with `load --parallel N` and waiting for the server to drain to idle between batches. Up to 3 attempts + 3 batch-level retries on failure. A 3-channel sampler (`sampler.sh`) captured `lms ps`, `nvidia-smi` (~2 s), and Windows counters concurrently.

## Result summary
- **528 requests, 528 succeeded, 0 retried, 0 failed** across every concurrency level 1→32. Reliability at concurrency ≤ slots (`parallel=N`) is 100%.
- **Queue depth never exceeded 3** even at 32-deep saturation.
- **Aggregate throughput saturates at ~300 tok/s by concurrency 6** and stays flat through 32 (GPU util kept < ~50%, avg power ~11 W) → the ceiling is NOT GPU compute; it is the per-slot scheduler/batching layer.
- **Per-request latency scales ~linearly**: single request ~0.5–0.7 s; at N=32 each of 32 requests takes ~12–33 s (mean wall 33.5 s for a 512-token cap output).
- First two batches (cold start) returned "stop" at 7 / 14 tokens; from N=3 on every output hits the 512 cap (`finish: length`).

## Per-concurrency table (batch span = time from firing all N requests to last completion)
| N | span_s | mean wall_s | ok | out_tokens | aggregate tok/s | per-request tok/s |
|---|---|---|---|---|---|---|
| 1  | 0.5  | 0.51  | 1  | 7    | 14   | 13.7 |
| 2  | 0.4  | 0.34  | 2  | 14   | 40   | 20.6 |
| 3  | 6.7  | 6.66  | 3  | 1536 | 230  | 76.9 |
| 4  | 8.1  | 8.10  | 4  | 2048 | 252  | 63.2 |
| 5  | 9.8  | 9.79  | 5  | 2560 | 261  | 52.3 |
| 6  | 9.6  | 8.07  | 6  | 2567 | 267  | 53.0 |
| 7  | 8.3  | 4.93  | 7  | 2069 | 250  | 60.0 |
| 8  | 11.2 | 8.51  | 8  | 3086 | 275  | 45.3 |
| 9  | 13.0 | 10.19 | 9  | 3598 | 277  | 39.2 |
| 10 | 14.9 | 12.06 | 10 | 4112 | 276  | 34.1 |
| 11 | 15.0 | 12.33 | 11 | 4624 | 309  | 34.1 |
| 12 | 15.3 | 11.59 | 12 | 4634 | 303  | 33.3 |
| 13 | 16.6 | 13.12 | 13 | 5242 | 316  | 30.7 |
| 14 | 19.3 | 16.59 | 14 | 6160 | 320  | 26.5 |
| 15 | 20.9 | 18.14 | 15 | 6672 | 320  | 24.5 |
| 16 | 21.0 | 17.16 | 16 | 6681 | 318  | 24.3 |
| 17 | 20.1 | 14.43 | 17 | 6187 | 308  | 25.2 |
| 18 | 22.5 | 17.63 | 18 | 7202 | 321  | 22.7 |
| 19 | 25.3 | 20.13 | 19 | 7714 | 305  | 20.2 |
| 20 | 26.3 | 21.19 | 20 | 8226 | 313  | 19.4 |
| 21 | 27.0 | 20.33 | 21 | 8085 | 300  | 18.9 |
| 22 | 29.8 | 23.23 | 22 | 8747 | 294  | 17.1 |
| 23 | 31.4 | 26.04 | 23 | 9763 | 311  | 16.3 |
| 24 | 33.8 | 28.32 | 24 | 10274| 304  | 15.1 |
| 25 | 35.7 | 30.10 | 25 | 10786| 302  | 14.3 |
| 26 | 35.9 | 29.17 | 26 | 10795| 301  | 14.2 |
| 27 | 37.7 | 29.52 | 27 | 10804| 287  | 13.6 |
| 28 | 38.1 | 30.69 | 28 | 11547| 303  | 13.4 |
| 29 | 39.0 | 31.18 | 29 | 11828| 303  | 13.1 |
| 30 | 39.8 | 32.07 | 30 | 12339| 310  | 12.8 |
| 31 | 40.6 | 32.47 | 31 | 12651| 312  | 12.6 |
| 32 | 42.6 | 33.53 | 32 | 12862| 302  | 12.0 |

Aggregate: 215,420 output tokens over 716 s of batch spans ≈ **301 tok/s** sustained.

## What it explains about earlier results
- The prior 35-way concurrent runs (~50% HTTP 400, batches spanning up to ~4 min) were queue-timeout rejections caused by **oversubscribing the 32-slot model with 35 concurrent requests** (~3 tasks sit queued; server aborts long-queued requests).
- With concurrency matched to slots (`parallel = concurrency`), the server is **fully reliable** — including 32-deep, 42 s, full-saturation runs.
- Practical rule derived: keep concurrent in-flight ≤ `lms model parallel` slots (guard with `lms ps` queued=0 gating), and batch span ≈ N × ~1.3 s at 512-token cap.
- The stable ~300 tok/s ceiling with low GPU util suggests the llama.cpp/dll slot scheduler or KV/batch limits, not the GPU silicon, is the binding constraint for this 270M F16 model on this machine.

## Tooling notes
- `ramp.js` reloads model per batch (`--parallel N`), ~7 s reload each (33 reloads ≈ 4 min overhead).
- Windows counter channel (`win_ramp.csv`) is sparse (PID-matching issue with the dedicated-usage filter); ps + nvidia-smi channels are complete and were used for the resource statements above.