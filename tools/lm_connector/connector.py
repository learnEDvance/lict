#!/usr/bin/env python3
"""
lm_connector.py — LM Studio connector loop.

Reads numbered request files from `request rows/` (e.g. 1.txt, 2.jpg, 2.txt, ...),
groups rows by numeric prefix, builds either text-only or image+text chat payloads,
and streams them to an LM Studio OpenAI-compatible endpoint, recording:

  - per-request client timing (queued/sent/first token/done), http, token usage,
    finish_reason, server ids, output text
  - the queue state across the run (queue log + live snapshot)
  - resource usage sampled during generation (GPU via nvidia-smi, LM Studio
    queue state via lms.exe ps, Windows CPU/RAM via PowerShell Get-Counter)

Outputs, numbered to match the input row, are written to `output rows/`:
  <N>.txt       final model output text
  <N>.json      full telemetry + resource attribution for that request
Plus run-level artifacts: _samples/*.csv, _queue.log, _queue_state.json,
_run_summary.json, _run.log.

Stdlib only. Linux/WSL client talking to an LM Studio server on a Windows host.
"""

import argparse
import base64
import http.client
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent          # repo root

DEFAULTS = dict(
    base_url="http://192.168.1.3:1234",
    model="gemma-3-270m-it",
    vision_model="",                                             # e.g. google/gemma-4-12b; "" = no vision routing
    request_dir=BASE_DIR / "request rows",
    output_dir=BASE_DIR / "output rows",
    nvidia_smi="/mnt/c/Windows/System32/nvidia-smi.exe",
    lms="/mnt/c/Users/Obhi/.lmstudio/bin/lms.exe",
    async_lms=str(BASE_DIR / "..") ,                             # unused placeholder
)


def iso(ts_ms):
    return datetime.fromtimestamp(ts_ms / 1e3, tz=timezone.utc).isoformat()


def now_ms():
    return time.time() * 1e3


def run_cmd(cmd, timeout=15):
    """Run a command as a list (no shell), return (rc, stdout, stderr)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return -1, "", str(e)


# ----------------------------------------------------------------------------
# Request discovery
# ----------------------------------------------------------------------------

TEXT_EXTS = {".txt", ".md", ".text"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def discover_rows(request_dir):
    """Group files by numeric prefix. Returns list of dicts sorted numerically:
    {num:int, files:[...], text:str|None, images:[abs paths]}"""
    if not request_dir.exists():
        raise SystemExit(f"request dir not found: {request_dir}")
    groups = {}
    for f in request_dir.iterdir():
        if not f.is_file() or f.name.startswith("_"):
            continue
        m = re.match(r"^(\d+)\.", f.name)
        if not m:
            print(f"[skip] non-numeric file: {f.name}")
            continue
        num = int(m.group(1))
        groups.setdefault(num, []).append(f)
    rows = []
    for num in sorted(groups):
        files = sorted(groups[num], key=lambda p: p.name)
        text_parts, images = [], []
        for p in files:
            ext = p.suffix.lower()
            if ext in IMAGE_EXTS:
                images.append(p)
            elif ext in TEXT_EXTS:
                text_parts.append(p.read_text(encoding="utf-8", errors="replace"))
        rows.append({
            "num": num,
            "files": [p.name for p in files],
            "text": "\n".join(text_parts).strip() if text_parts else None,
            "images": images,
        })
    return rows


def data_url(path):
    ext = path.suffix.lower().lstrip(".")
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "gif": "gif",
            "webp": "webp", "bmp": "bmp"}.get(ext, ext)
    b64 = base64.b64encode(path.read_bytes()).decode()
    return f"data:image/{mime};base64,{b64}"


def build_messages(row, default_image_prompt=""):
    """Return (model_override_or_None, messages). Image rows get content arrays."""
    if not row["images"]:
        return None, [{"role": "user", "content": row["text"] or ""}]
    content = []
    img_prompt = (row["text"] or default_image_prompt).strip()
    if img_prompt:
        content.append({"type": "text", "text": img_prompt})
    for img in row["images"]:
        content.append({"type": "image_url", "image_url": {"url": data_url(img)}})
    return None, [{"role": "user", "content": content}]


# ----------------------------------------------------------------------------
# HTTP streaming client (stdlib)
# ----------------------------------------------------------------------------

def post_chat(base_url, payload, timeout=600):
    """
    POST /v1/chat/completions with streaming. Yields telemetry dict via fields.
    Returns dict with: http_status, reason, headers_ms, ttft_ms, total_ms,
    content, reasoning, first_chunk(meta), usage, finish_reason, raw_error.
    """
    url = base_url if isinstance(base_url, str) else base_url
    u = argparse.Namespace()
    import urllib.parse
    parts = urllib.parse.urlparse(url)
    host = parts.hostname
    port = parts.port or 80
    use_ssl = parts.scheme == "https"
    if use_ssl:
        import ssl
        conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)

    body = json.dumps(payload).encode()
    res = {"http_status": None, "content": "", "reasoning": "",
           "usage": None, "finish_reason": None, "meta": {},
           "raw_error": None, "http_error_body": None,
           "ttft_ms": None, "headers_ms": None, "total_ms": None}
    t_start = now_ms()
    t_first = None
    try:
        conn.putrequest("POST", parts.path or "/v1/chat/completions")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        resp = conn.getresponse()          # headers received -> 'first byte'
        res["headers_ms"] = now_ms() - t_start
        res["http_status"] = resp.status
        res["reason"] = resp.reason
        if res["http_status"] != 200:
            raw = resp.read().decode("utf-8", "replace")
            try:
                res["http_error_body"] = json.loads(raw)
            except Exception:
                res["http_error_body"] = raw[:400]
            res["total_ms"] = now_ms() - t_start
            conn.close()
            return res

        if payload.get("stream"):
            # SSE: lines "data: {...}", terminal "data: [DONE]"
            for raw_line in resp:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except Exception:
                    continue
                if not res["meta"]:
                    res["meta"] = {
                        "id": chunk.get("id"), "created": chunk.get("created"),
                        "model": chunk.get("model"),
                        "system_fingerprint": chunk.get("system_fingerprint"),
                    }
                choices = chunk.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    c = delta.get("content")
                    if c:
                        if t_first is None:
                            t_first = now_ms()
                        res["content"] += c
                    if t_first is None and delta.get("reasoning_content"):
                        t_first = now_ms()
                    res["reasoning"] += delta.get("reasoning_content") or ""
                    if choices[0].get("finish_reason"):
                        res["finish_reason"] = choices[0]["finish_reason"]
                if chunk.get("usage"):
                    res["usage"] = chunk["usage"]
            # finish_reason may only appear on next-to-last chunk; fallback guess
            if res["finish_reason"] is None and res["content"]:
                res["finish_reason"] = "stop"
        else:
            raw = resp.read().decode("utf-8", "replace")
            try:
                data = json.loads(raw)
            except Exception:
                res["raw_error"] = raw[:400]
            else:
                res["usage"] = data.get("usage")
                msg = (data.get("choices") or [{}])[0].get("message") or {}
                res["content"] = msg.get("content") or ""
                res["reasoning"] = msg.get("reasoning_content") or ""
                res["finish_reason"] = (data.get("choices") or [{}])[0].get("finish_reason")
                res["meta"] = {"id": data.get("id"), "created": data.get("created"),
                               "model": data.get("model"),
                               "system_fingerprint": data.get("system_fingerprint")}
        res["total_ms"] = now_ms() - t_start
    except Exception as e:
        res["raw_error"] = str(e)
        res["total_ms"] = now_ms() - t_start
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if t_first is not None:
        res["ttft_ms"] = t_first - t_start
    return res


# ----------------------------------------------------------------------------
# Model lifecycle REST helpers
# ----------------------------------------------------------------------------

def api_post(base_url, path, payload, timeout=120):
    """POST JSON to the native LM Studio REST API. Returns (http_status, json)."""
    import urllib.parse
    parts = urllib.parse.urlparse(base_url)
    body = json.dumps(payload).encode()
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
    try:
        conn.putrequest("POST", path)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        r = conn.getresponse()
        raw = r.read().decode("utf-8", "replace")
        try:
            return r.status, json.loads(raw)
        except Exception:
            return r.status, {"raw": raw[:300]}
    except Exception as e:
        return 0, {"error": str(e)}
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ----------------------------------------------------------------------------
# Resource samplers (background threads)
# ----------------------------------------------------------------------------

class Sampler:
    """Poll a command periodically; keep in-memory ring + write CSV."""
    def __init__(self, name, cmd, parse, interval, stop, out_dir):
        self.name = name
        self.cmd = cmd
        self.parse = parse
        self.interval = interval
        self.stop = stop
        self.out_dir = out_dir
        self.samples = []           # list of dicts with 'ts_ms'
        self.lock = threading.Lock()
        self.last = {}
        self._thread = None

    def start(self):
        self.thread = threading.Thread(target=self._loop, daemon=True, name=self.name)
        self.thread.start()

    def _loop(self):
        os.makedirs(self.out_dir, exist_ok=True)
        path = self.out_dir / f"{self.name}.csv"
        while not self.stop.is_set():
            t0 = now_ms()
            rc, out, err = run_cmd(self.cmd)
            if rc == 0 and out.strip():
                for row in self.parse(out):
                    row = {"ts_ms": now_ms(), **row}
                    with self.lock:
                        self.samples.append(row)
                        if len(self.samples) > 20000:
                            del self.samples[:4000]
                        self.last = row
                    with open(path, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps(row) + "\n")
            else:
                with self.lock:
                    self.last = {"ts_ms": now_ms(), "err": (err or "").strip()[:120]}
            # pace to interval even if the command itself took time
            elapsed = now_ms() - t0
            self.stop.wait(max(0.05, self.interval - elapsed / 1e3))

    def snapshot(self):
        with self.lock:
            return list(self.samples), dict(self.last)

    def window(self, start_ms, end_ms, key=None):
        got = []
        with self.lock:
            for s in self.samples:
                if start_ms - 0.001 <= s["ts_ms"] <= end_ms + 0.001:
                    got.append(s)
        return got


def parse_gpu(out):
    rows = []
    for line in out.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            continue
        try:
            rows.append({
                "nvgpu_ts": parts[0],
                "gpu_util_pct": float(parts[1]),
                "gpu_mem_util_pct": float(parts[2]),
                "power_w": float(parts[3]),
                "gpu_mem_mib": float(parts[4]),
                "sm_mhz": float(parts[5]),
                "mem_mhz": float(parts[6]),
            })
        except ValueError:
            continue
    return rows


def parse_ps(out):
    rows = []
    try:
        data = json.loads(out)
    except Exception:
        return rows
    if not isinstance(data, list):
        return rows
    if not data:
        return [{"status": "none_loaded", "queued": 0, "instances": 0}]
    for item in data:
        rows.append({
            "model": item.get("modelKey") or item.get("identifier") or item.get("model") or "",
            "status": item.get("status", ""),
            "queued": item.get("queued", 0),
            "parallel": item.get("parallel", 0),
            "context_length": item.get("contextLength", 0),
        })
    return rows


PS_SCRIPT = (
    '(Get-Counter -Counter "%(p)s" -SampleInterval 1 -MaxSamples 1).CounterSamples | '
    'ForEach-Object { "%(f)s" -f $_.Path,$_.CookedValue }'
)


def parse_win(out):
    rows, rec = [], {}
    for line in out.strip().splitlines():
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        try:
            rec[key.strip()] = float(val.strip())
        except ValueError:
            continue
    if rec:
        def find(sub):
            for k, v in rec.items():
                if sub in k:
                    return v
            return None
        rows.append({
            "llama_cpu_pct": find("llama-server)% Processor Time"),
            "llama_ws_mb": (find("Working Set") / 1e6) if find("Working Set") is not None else None,
            "total_cpu_pct": find("Processor(_Total)% Processor Time"),
            "avail_kb": find("Available Kbytes"),
        })
    return rows


def build_win_cmd():
    counters = [
        "\\Process(llama-server)\\% Processor Time",
        "\\Process(llama-server)\\Working Set",
        "\\Processor(_Total)\\% Processor Time",
        "\\Memory\\Available Kbytes",
    ]
    arg = ",".join("'%s'" % c for c in counters)
    script = (
        f"(Get-Counter -Counter {arg} -SampleInterval 1 -MaxSamples 1)"
        ".CounterSamples | ForEach-Object { '{0}={1}' -f $_.Path,$_.CookedValue }"
    )
    return ["powershell.exe", "-NoProfile", "-Command", script]


# ----------------------------------------------------------------------------
# Queue logging
# ----------------------------------------------------------------------------

class QueueLog:
    def __init__(self, out_dir):
        self.out_dir = out_dir
        self.log_path = out_dir / "_queue.log"
        self.state_path = out_dir / "_queue_state.json"
        self.states = {}
        self.lock = threading.Lock()

    def _append(self, num, state, extra=None):
        rec = {"ts": iso(now_ms()), "num": num, "state": state}
        if extra:
            rec.update(extra)
        with self.lock:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
            self.states[num] = state
            with open(self.state_path, "w", encoding="utf-8") as fh:
                json.dump({"ts": iso(now_ms()), "states": self.states}, fh, indent=2)


# ----------------------------------------------------------------------------
# Worker pool
# ----------------------------------------------------------------------------

class Connector:
    def __init__(self, cfg, rows, samplers, qlog):
        self.cfg = cfg
        self.rows = rows
        self.samplers = samplers
        self.qlog = qlog
        self.results = []
        self.lock = threading.Lock()
        self.index = 0
        self.sysinfo = {}

    def next_row(self):
        with self.lock:
            if self.index >= len(self.rows):
                return None
            r = self.rows[self.index]
            self.index += 1
            return r

    def get_loaded_ids(self):
        rc, out, err = run_cmd([self.cfg["lms"], "ps", "--json"])
        if rc != 0:
            return []
        try:
            data = json.loads(out)
        except Exception:
            return []
        if not isinstance(data, list):
            return []
        return [i.get("identifier") or i.get("modelKey") or i.get("model")
                for i in data if isinstance(i, dict)]

    def ensure_model(self, target):
        """Make sure `target` is the loaded LLM; unload others, load target otherwise."""
        loaded = [m for m in self.get_loaded_ids() if m]
        if target in loaded:
            return {"switched": False, "target": target, "load_time_seconds": 0,
                    "loaded_before": loaded}
        unloaded = []
        for inst in loaded:
            if inst != target:
                status, res = api_post(self.cfg["base_url"], "/api/v1/models/unload",
                                       {"instance_id": inst}, timeout=60)
                unloaded.append({"instance_id": inst, "http": status,
                                 "response": {k: v for k, v in res.items() if k not in ("raw",)}})
        t0 = now_ms()
        status, res = api_post(self.cfg["base_url"], "/api/v1/models/load",
                               {"model": target}, timeout=300)
        waited = 0.0
        for _ in range(120):
            if target in self.get_loaded_ids():
                break
            time.sleep(0.5)
            waited += 0.5
        return {
            "switched": True, "target": target,
            "load_http": status,
            "load_response": {k: v for k, v in res.items() if k not in ("raw",)},
            "load_time_seconds": res.get("load_time_seconds"),
            "ready_wait_s": round(waited, 1),
            "elapsed_ms": round(now_ms() - t0, 1),
            "unloaded": unloaded, "loaded_before": loaded,
        }

    def payload_for(self, row, target_model):
        extra = {}
        vision_override, messages = build_messages(row, self.cfg["default_image_prompt"])
        payload = {
            "model": target_model,
            "messages": messages,
            "max_tokens": self.cfg["max_tokens"],
            "temperature": self.cfg["temperature"],
            "stream": self.cfg["stream"],
        }
        if self.cfg["stream"]:
            payload["stream_options"] = {"include_usage": True}
        if row["images"]:
            if target_model == self.cfg["vision_model"] and self.cfg["vision_model"]:
                extra["routed_to_vision"] = True
                payload["reasoning_effort"] = "none"
            else:
                extra["routed_to_vision"] = False
        return payload, extra

    def target_model_for(self, row):
        if row["images"] and self.cfg["vision_model"]:
            return self.cfg["vision_model"]
        return self.cfg["model"]

    def run_row(self, row):
        num = row["num"]
        target = self.target_model_for(row)
        self.qlog._append(num, "queued", {"files": row["files"], "target_model": target})
        t_picked = now_ms()
        mswitch = None
        if self.cfg["dry_run"]:
            mswitch = {"switched": False, "simulated": True, "plan": target}
        elif self.cfg["auto_switch"]:
            mswitch = self.ensure_model(target)
            if mswitch.get("switched"):
                self.qlog._append(num, "model_switch",
                                  {"to": target,
                                   "load_time_seconds": mswitch.get("load_time_seconds"),
                                   "unloaded": [u.get("instance_id") for u in mswitch.get("unloaded", [])]})
        payload, extra = self.payload_for(row, target)
        body = {
            "request_num": num,
            "request_files": row["files"],
            "has_images": bool(row["images"]),
            "has_text": row["text"] is not None,
            "target_model": target,
            "model_switch": mswitch,
            "payload": {"model": payload["model"], "stream": payload["stream"],
                        "max_tokens": payload["max_tokens"],
                        "temperature": payload["temperature"],
                        "reasoning_effort": payload.get("reasoning_effort")},
            "extra": extra,
        }
        if self.cfg["dry_run"]:
            body["dry_run"] = True
            body["message_preview"] = json.dumps(payload["messages"])[:2000]
            self.finish_row(num, body, None)
            return body

        self.qlog._append(num, "sending")
        t_sent_abs = now_ms()
        attempt = 0
        res = None
        while attempt <= self.cfg["retries"]:
            attempt += 1
            res = post_chat(self.cfg["base_url"], payload, timeout=self.cfg["timeout"])
            if res["http_status"] == 200 and not res["raw_error"]:
                break
            if attempt <= self.cfg["retries"]:
                time.sleep(self.cfg["backoff_s"] * attempt)
        body.update({
            "attempts": attempt,
            "t_queued": iso(t_picked),
            "t_sent": iso(t_sent_abs),
        })
        self.finish_row(num, body, res)
        return body

    def finish_row(self, num, body, res):
        body["t_done"] = iso(now_ms())
        ok = res is not None and res["http_status"] == 200 and res["raw_error"] is None
        if ok:
            body.update({
                "ok": True,
                "http_status": res["http_status"],
                "reason": res.get("reason"),
                "headers_ms": res.get("headers_ms"),
                "ttft_ms": res.get("ttft_ms"),
                "total_ms": res.get("total_ms"),
                "finish_reason": res.get("finish_reason"),
                "usage": res.get("usage"),
                "server_ids": res.get("meta"),
                "content": res.get("content"),
                "reasoning_content": res.get("reasoning"),
            })
            comp = (res.get("usage") or {}).get("completion_tokens") or 0
            gen_ms = max(0.001, res.get("total_ms", 0) - (res.get("ttft_ms") or 0))
            body["tokens_per_second_client"] = round(comp / (gen_ms / 1e3), 2)
        else:
            body.update({
                "ok": False,
                "http_status": res.get("http_status") if res else None,
                "raw_error": res.get("raw_error") if res else "no response",
                "http_error_body": res.get("http_error_body") if res else None,
            })
        # resource attribution over the request window
        t_start_ms = now_ms()
        if res:
            total = res.get("total_ms") or 0
            start_ms = t_start_ms - total - 250
            end_ms = t_start_ms + 50
            body["resource_samples_during"] = {
                name: s.window(start_ms, end_ms)
                for name, s in self.samplers.items()
            }
            body["resources"] = self.aggregate_resources(body["resource_samples_during"])
        with self.lock:
            self.results.append(body)
        self.qlog._append(num, "done" if ok else "error",
                          {"http": body.get("http_status"), "ok": ok})
        self.write_row(num, body)
        self.clear_input_files(body)

    def clear_input_files(self, body):
        """Consume successfully processed rows from the request dir (queue behavior)."""
        if self.cfg["dry_run"] or not self.cfg["clear_input"]:
            return
        if not body.get("ok"):
            return
        cleared = []
        for name in body.get("request_files") or []:
            p = self.cfg["request_dir"] / name
            try:
                if p.exists() and p.is_file():
                    p.unlink()
                    cleared.append(name)
            except OSError as e:
                print(f"  [!] could not clear {p}: {e}")
        if cleared:
            self.qlog._append(body["request_num"], "input_cleared", {"files": cleared})

    @staticmethod
    def aggregate_resources(samples):
        ag = {}
        gpu = samples.get("gpu", [])
        ps = samples.get("ps", [])
        win = samples.get("win", [])
        if gpu:
            ag["gpu_util_max_pct"] = max(s["gpu_util_pct"] for s in gpu)
            ag["gpu_util_avg_pct"] = statistics.mean(s["gpu_util_pct"] for s in gpu)
            ag["power_max_w"] = max(s["power_w"] for s in gpu)
            ag["power_avg_w"] = statistics.mean(s["power_w"] for s in gpu)
            ag["gpu_mem_max_mib"] = max(s["gpu_mem_mib"] for s in gpu)
        if ps:
            ag["ps_states"] = [{"status": s.get("status"), "queued": s.get("queued"),
                                "parallel": s.get("parallel")} for s in ps]
        if win:
            xs = [s.get("llama_cpu_pct") for s in win if s.get("llama_cpu_pct") is not None]
            if xs:
                ag["llama_cpu_max_pct"] = max(xs)
                ag["llama_cpu_avg_pct"] = statistics.mean(xs)
            ws = [s.get("llama_ws_mb") for s in win if s.get("llama_ws_mb") is not None]
            if ws:
                ag["llama_ws_max_mb"] = max(ws)
        ag["window_samples"] = sum(len(v) for v in samples.values())
        return ag

    def write_row(self, num, body):
        out_dir = self.cfg["output_dir"]
        (out_dir / f"{num}.txt").write_text(body.get("content") or "", encoding="utf-8")
        slim = {k: v for k, v in body.items() if k not in ("content", "reasoning_content")}
        slim["output_txt"] = f"{num}.txt"
        slim["output_first80"] = (body.get("content") or "")[:80]
        (out_dir / f"{num}.json").write_text(
            json.dumps(slim, indent=2), encoding="utf-8")

    def run(self):
        self.qlog._append("*", "run_start")
        workers = self.cfg["workers"]
        if workers <= 1:
            while True:
                row = self.next_row()
                if row is None:
                    break
                self.run_row(row)
        else:
            threads = []
            for _ in range(workers):
                t = threading.Thread(target=self._worker_loop, daemon=True)
                t.start()
                threads.append(t)
            for t in threads:
                t.join()
        self.qlog._append("*", "run_end")

    def _worker_loop(self):
        while True:
            row = self.next_row()
            if row is None:
                return
            self.run_row(row)


# ----------------------------------------------------------------------------
# System info
# ----------------------------------------------------------------------------

def gather_system_info(cfg):
    info = {"time": iso(now_ms()), "host": __import__("platform").node()}
    rc, out, err = run_cmd([cfg["lms"], "runtime", "survey", "--json"], timeout=30)
    if rc == 0:
        try:
            s = json.loads(out)
            eng = (s.get("engines") or [{}])[0]
            info["lms_runtime"] = {
                "name": eng.get("name"), "version": eng.get("version"),
                "engine": eng.get("engine"),
            }
            hs = eng.get("hardwareSurvey") or {}
            info["cpu"] = ((hs.get("cpuSurveyResult") or {}).get("cpuInfo") or {}).get("name")
            gpu = ((hs.get("gpuSurveyResult") or {}).get("gpuInfo") or [{}])[0]
            info["gpu"] = gpu.get("name")
            info["vram_bytes"] = gpu.get("totalMemoryCapacityBytes")
            mi = eng.get("memoryInfo") or {}
            info["ram_bytes"] = mi.get("ramCapacity")
            info["lms_survey_raw"] = s
        except Exception as e:
            info["lms_survey_error"] = str(e)
    rc, out, err = run_cmd([cfg["nvidia_smi"],
                            "--query-gpu=name,memory.total,driver_version",
                            "--format=csv,noheader"], timeout=15)
    if rc == 0:
        info["nvidia_smi"] = [p.strip() for p in out.strip().split(",")]
    # model list snapshot
    try:
        import urllib.parse
        parts = urllib.parse.urlparse(cfg["base_url"])
        c = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=8)
        c.request("GET", "/v1/models")
        r = c.getresponse()
        info["models"] = [m["id"] for m in json.loads(r.read()).get("data", [])]
        c.close()
    except Exception as e:
        info["models_error"] = str(e)
    return info


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def build_samplers(cfg, stop):
    out = cfg["output_dir"] / "_samples"
    samplers = {}
    samplers["gpu"] = Sampler(
        "gpu",
        [cfg["nvidia_smi"], "--query-gpu=timestamp,utilization.gpu,utilization.memory,"
         "power.draw,memory.used,clocks.sm,clocks.mem", "--format=csv,noheader,nounits"],
        parse_gpu, cfg["sample_interval"], stop, out)
    samplers["ps"] = Sampler(
        "ps", [cfg["lms"], "ps", "--json"], parse_ps, cfg["sample_interval"], stop, out)
    samplers["win"] = Sampler(
        "win", build_win_cmd(), parse_win, cfg["sample_interval"], stop, out)
    return samplers


def write_summary(cfg, sysinfo, results):
    out_dir = cfg["output_dir"]
    ok = [r for r in results if r.get("ok")]
    fail = [r for r in results if not r.get("ok")]
    usage = [r.get("usage") or {} for r in ok]
    col = lambda k, fn: fn([u.get(k) or 0 for u in usage]) if usage else 0
    total_in = col("prompt_tokens", sum)
    total_out = sum(r["usage"]["completion_tokens"] for r in ok if r.get("usage"))
    wall_start = min(r.get("t_queued") or iso(now_ms()) for r in results) if results else None
    wall_end = max(r.get("t_done") or iso(now_ms()) for r in results) if results else None
    latency = [r["total_ms"] for r in ok if r.get("total_ms")]
    ttft = [r["ttft_ms"] for r in ok if r.get("ttft_ms")]
    from collections import Counter
    by_model = Counter(r.get("payload", {}).get("model") for r in ok)
    switches = [{"num": r["request_num"], "from": (r.get("model_switch") or {}).get("loaded_before"),
                 "to": (r.get("model_switch") or {}).get("target"),
                 "load_time_seconds": (r.get("model_switch") or {}).get("load_time_seconds"),
                 "ready_wait_s": (r.get("model_switch") or {}).get("ready_wait_s")}
                for r in ok if r.get("model_switch", {}).get("switched")]
    summary = {
        "run_time": iso(now_ms()),
        "config": {k: cfg[k] for k in ("base_url", "model", "vision_model", "max_tokens",
                                       "temperature", "stream", "workers", "retries",
                                       "sample_interval", "dry_run", "auto_switch",
                                       "clear_input")},
        "system": sysinfo,
        "totals": {
            "requests": len(results), "ok": len(ok), "failed": len(fail),
            "wall_start": wall_start, "wall_end": wall_end,
            "wall_secs": round((datetime.fromisoformat(wall_end) -
                                datetime.fromisoformat(wall_start)).total_seconds(), 2),
            "prompt_tokens": total_in, "completion_tokens": total_out,
            "total_tokens": total_in + total_out,
            "aggregate_tokens_per_sec": (round((total_in + total_out) /
                ((datetime.fromisoformat(wall_end) - datetime.fromisoformat(wall_start)).total_seconds()), 2)
                if wall_start and wall_end and (datetime.fromisoformat(wall_end) - datetime.fromisoformat(wall_start)).total_seconds() > 0 else None),
            "ok_by_model": dict(by_model),
        },
        "model_switches": switches,
        "latency_ms": {"mean": statistics.mean(latency) if latency else None,
                       "median": statistics.median(latency) if latency else None,
                       "p95": quantile(latency, 0.95)},
        "ttft_ms": {"mean": statistics.mean(ttft) if ttft else None,
                    "median": statistics.median(ttft) if ttft else None},
        "failures": [{"num": r["request_num"], "http": r.get("http_status"),
                      "err": r.get("raw_error"), "body": r.get("http_error_body"),
                      "attempts": r.get("attempts")} for r in fail],
        "queue_states_seen": summarize_states(cfg["output_dir"]),
    }
    (out_dir / "_run_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def quantile(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    k = int(len(xs) * q)
    return xs[min(k, len(xs) - 1)]


def summarize_states(out_dir):
    p = out_dir / "_queue.log"
    if not p.exists():
        return {}
    from collections import Counter
    c = Counter()
    for line in p.read_text().splitlines():
        try:
            c[json.loads(line)["state"]] += 1
        except Exception:
            pass
    return dict(c)


def main():
    ap = argparse.ArgumentParser(description="LM Studio connector loop")
    ap.add_argument("--base-url", default=DEFAULTS["base_url"])
    ap.add_argument("--model", default=DEFAULTS["model"])
    ap.add_argument("--vision-model", default=DEFAULTS["vision_model"])
    ap.add_argument("--request-dir", default=str(DEFAULTS["request_dir"]))
    ap.add_argument("--output-dir", default=str(DEFAULTS["output_dir"]))
    ap.add_argument("--lms", default=DEFAULTS["lms"])
    ap.add_argument("--nvidia-smi", default=DEFAULTS["nvidia_smi"])
    ap.add_argument("--max-tokens", type=int, default=200)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--stream", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--auto-switch", action=argparse.BooleanOptionalAction, default=True,
                    help="unload/load models so only the needed one is resident per row type")
    ap.add_argument("--clear-input", action=argparse.BooleanOptionalAction, default=True,
                    help="delete successfully processed input rows from the request dir "
                         "(consuming-queue behavior); failed rows are kept")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--backoff-s", type=float, default=0.5)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--sample-interval", type=float, default=2.0)
    ap.add_argument("--default-image-prompt",
                    default="Analyze the image you were given and describe exactly what you see, "
                            "including colors and gradients, in one sentence.")
    ap.add_argument("--dry-run", action="store_true", help="build payloads but do not send")
    cfg = vars(ap.parse_args())
    cfg["request_dir"] = Path(cfg["request_dir"])
    cfg["output_dir"] = Path(cfg["output_dir"])
    (cfg["output_dir"] / "_samples").mkdir(parents=True, exist_ok=True)

    rows = discover_rows(cfg["request_dir"])
    if not rows:
        raise SystemExit(f"no numbered request files found in {cfg['request_dir']}")
    print(f"[connector] {len(rows)} request rows | text-model={cfg['model']} "
          f"| vision-model={cfg['vision_model'] or '-'} | auto-switch={cfg['auto_switch']} "
          f"| workers={cfg['workers']} | stream={cfg['stream']} | out={cfg['output_dir']}")

    stop = threading.Event()
    samplers = build_samplers(cfg, stop)
    for s in samplers.values():
        s.start()

    sysinfo = gather_system_info(cfg)
    qlog = QueueLog(cfg["output_dir"])
    log_path = cfg["output_dir"] / "_run.log"
    conn = Connector(cfg, rows, samplers, qlog)
    t0 = now_ms()

    # stdout progress
    conn2_done = {"n": 0}

    orig_run_row = conn.run_row
    def run_row_progress(row):
        orig_run_row(row)
        conn2_done["n"] += 1
        print(f"[{row['num']:>3}] done ({conn2_done['n']}/{len(rows)})", flush=True)
    conn.run_row = run_row_progress

    try:
        conn.run()
    finally:
        stop.set()
        for s in samplers.values():
            s.thread.join(timeout=10)

    summary = write_summary(cfg, sysinfo, conn.results)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(summary, indent=2) + "\n")

    print(f"\n[connector] run finished in {summary['totals'].get('wall_secs')}s | "
          f"ok={summary['totals']['ok']}/{summary['totals']['requests']} | "
          f"prompt_tok={summary['totals']['prompt_tokens']} out_tok={summary['totals']['completion_tokens']} "
          f"| agg_tok/s={summary['totals'].get('aggregate_tokens_per_sec')}")
    print(f"[connector] summary -> {cfg['output_dir'] / '_run_summary.json'}")


if __name__ == "__main__":
    main()