# SPDX-License-Identifier: Apache-2.0
"""Run an offline Windows CPU model acceptance and keep measured receipts."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import urllib.request


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class Memory(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [
        (name, ctypes.c_ulonglong) for name in (
            "total_physical", "available_physical", "total_commit", "available_commit",
            "total_virtual", "available_virtual", "available_extended_virtual")]


class ProcessMemory(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("page_faults", wintypes.DWORD)] + [
        (name, ctypes.c_size_t) for name in (
            "peak_working_set", "working_set", "peak_paged_pool", "paged_pool",
            "peak_nonpaged_pool", "nonpaged_pool", "pagefile", "peak_pagefile", "private_bytes")]


class IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "read_operations", "write_operations", "other_operations",
        "read_transfer_bytes", "write_transfer_bytes", "other_transfer_bytes")]


def global_memory() -> dict:
    value = Memory()
    value.length = ctypes.sizeof(value)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):
        raise ctypes.WinError()
    return {name: getattr(value, name) for name, _ in value._fields_}


def monitor(process: subprocess.Popen, output: Path, stop: threading.Event, samples: list, errors: list) -> None:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessIoCounters.argtypes = [wintypes.HANDLE, ctypes.POINTER(IoCounters)]
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessMemory), wintypes.DWORD]
    handle = kernel.OpenProcess(0x1000 | 0x10, False, process.pid)
    try:
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        with output.open("w", encoding="utf-8") as stream:
            while not stop.is_set() and process.poll() is None:
                memory, io = ProcessMemory(), IoCounters()
                memory.cb = ctypes.sizeof(memory)
                if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(memory), memory.cb):
                    if process.poll() is not None:
                        break
                    raise ctypes.WinError(ctypes.get_last_error())
                if not kernel.GetProcessIoCounters(handle, ctypes.byref(io)):
                    raise ctypes.WinError(ctypes.get_last_error())
                sample = {"time": time.time(), **{n: getattr(memory, n) for n, _ in memory._fields_},
                          "io": {n: getattr(io, n) for n, _ in io._fields_}, "system": global_memory()}
                samples.append(sample)
                stream.write(json.dumps(sample) + "\n")
                stream.flush()
                stop.wait(0.5)
    except Exception as error:
        errors.append(str(error))
    finally:
        if handle:
            kernel.CloseHandle(handle)


def chat(http, base: str, messages: list, name: str, output: Path, timeout: int, tokens: int) -> dict:
    payload = {"model": "windows-deepseek", "messages": messages, "max_tokens": tokens,
               "temperature": 0, "seed": 42, "stream": True, "stream_options": {"include_usage": True},
               "chat_template_kwargs": {"enable_thinking": False, "drop_thinking": False}}
    write_json(output / (name + ".request.json"), payload)
    request = urllib.request.Request(base + "/v1/chat/completions", json.dumps(payload).encode(),
                                     {"Content-Type": "application/json"})
    started = time.perf_counter()
    first = None
    content, reasoning, events = "", "", []
    done = False
    with http.open(request, timeout=timeout) as response, (output / (name + ".sse")).open("wb") as raw:
        for line in response:
            raw.write(line)
            raw.flush()
            if not line.startswith(b"data: "):
                continue
            data = line[6:].strip()
            if data == b"[DONE]":
                done = True
                break
            event = json.loads(data)
            events.append(event)
            if "error" in event:
                raise RuntimeError(str(event["error"]))
            for choice in event.get("choices", []):
                delta = choice.get("delta", {})
                text = delta.get("content") or ""
                thought = delta.get("reasoning_content") or ""
                if (text or thought) and first is None:
                    first = time.perf_counter() - started
                content += text
                reasoning += thought
    result = {"name": name, "content": content, "reasoning": reasoning, "ttft_seconds": first,
              "wall_seconds": time.perf_counter() - started, "events": events,
              "timings": next((e["timings"] for e in reversed(events) if "timings" in e), {}),
              "usage": next((e["usage"] for e in reversed(events) if "usage" in e), {}),
              "content_chunks": sum(bool(c.get("delta", {}).get("content"))
                                    for e in events for c in e.get("choices", []))}
    write_json(output / (name + ".response.json"), result)
    print(json.dumps({k: v for k, v in result.items() if k != "events"}, ensure_ascii=False), flush=True)
    if not done or not content.strip():
        raise RuntimeError(name + ": incomplete stream or no assistant content")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--source-index", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget-mib", type=int, default=4096)
    parser.add_argument("--context", type=int, default=1024)
    parser.add_argument("--threads", type=int, default=12)
    parser.add_argument("--tokens", type=int, default=24)
    parser.add_argument("--decode-tokens", type=int, default=64)
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This acceptance runs on native Windows.")
    repo = Path(__file__).resolve().parents[1]
    binary = repo / "build-windows-cpu/bin/inferssd-server.exe"
    model, source = args.model.resolve(strict=True), args.source_index.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    settings = output / "settings.ssd"
    settings.write_text("inferssd-settings 1\nexpert.source = " + json.dumps([source.as_posix()]) +
                        f"\nhost.budget_mib = {args.budget_mib}\nhost.headroom_mib = 2048\nprefetch.depth = 2\n", encoding="utf-8")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    command = [str(binary), "-m", str(model), "--alias", "windows-deepseek", "-ngl", "0",
               "-c", str(args.context), "-b", "64", "-ub", "16", "-t", str(args.threads),
               "-tb", str(args.threads), "--threads-http", "2", "-np", "1", "--defer-experts",
               "--no-warmup", "--fit", "off", "--cache-ram", "0", "--host", "127.0.0.1", "--port", str(port),
               "--chat-template-file", str(repo / "engine/models/templates/deepseek-ai-DeepSeek-V4.1.jinja"),
               "--chat-template-kwargs", '{"drop_thinking":false}', "--reasoning", "off",
               "--ssd-settings", str(settings), "--ssd-telemetry", str(output / "native-events.jsonl"), "--metrics"]
    write_json(output / "launch.json", {"command": command, "memory_before": global_memory(),
               "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
               "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
               "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()})
    provenance = []
    for shard in sorted(model.parent.glob("*.gguf")):
        receipt = {"path": str(shard), "bytes": shard.stat().st_size}
        metadata = model.parent.parent / ".cache/huggingface/download" / model.parent.name / (shard.name + ".metadata")
        if metadata.is_file():
            lines = metadata.read_text(encoding="utf-8").splitlines()
            if len(lines) >= 2:
                receipt.update(download_revision=lines[0], download_etag=lines[1])
        with shard.open("rb") as stream:
            receipt["first_64k_sha256"] = hashlib.sha256(stream.read(65536)).hexdigest()
        provenance.append(receipt)
    write_json(output / "model-files.json", provenance)
    env = os.environ.copy()
    env["PATH"] = r"C:\msys64\ucrt64\bin;" + env["PATH"]
    stop, samples, errors = threading.Event(), [], []
    results, process, watcher = [], None, None
    http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    started = time.perf_counter()
    try:
        with (output / "server.stdout.log").open("wb") as stdout, (output / "server.stderr.log").open("wb") as stderr:
            process = subprocess.Popen(command, cwd=repo, env=env, stdout=stdout, stderr=stderr,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
            write_json(output / "process.json", {"pid": process.pid, "port": port})
            watcher = threading.Thread(target=monitor, args=(process, output / "resources.jsonl", stop, samples, errors))
            watcher.start()
            base = f"http://127.0.0.1:{port}"
            while time.perf_counter() - started < args.timeout:
                if process.poll() is not None:
                    raise RuntimeError(f"Server exited: {process.returncode}; see server.stderr.log")
                try:
                    with http.open(base + "/health", timeout=2) as response:
                        if json.load(response).get("status") == "ok":
                            break
                except (OSError, ValueError) as error:
                    if getattr(getattr(error, "reason", None), "winerror", None) == 10013:
                        raise RuntimeError("Sandbox denied loopback HTTP (Socket 10013)") from error
                    time.sleep(0.5)
            else:
                raise TimeoutError("Model startup timed out")
            startup = time.perf_counter() - started
            print(f"Model ready in {startup:.3f}s; pid={process.pid}; port={port}", flush=True)
            one = chat(http, base, [{"role": "user", "content": "Reply with exactly: WINDOWS_OK"}],
                       "single", output, args.timeout, args.tokens)
            results.append(one)
            history = [{"role": "user", "content": "Remember this code for our conversation: BLUE-731. Reply only: remembered."}]
            first = chat(http, base, history, "conversation-1", output, args.timeout, args.tokens)
            results.append(first)
            history += [{"role": "assistant", "content": first["content"]},
                        {"role": "user", "content": "What code did I ask you to remember? Reply only with the code."}]
            results.append(chat(http, base, history, "conversation-2", output, args.timeout, args.tokens))
            results.append(chat(http, base, [{"role": "user", "content": "Reply with exactly: WINDOWS_OK"}],
                                "single-repeat", output, args.timeout, args.tokens))
            results.append(chat(http, base, [{"role": "user", "content":
                "Explain in about 80 words how a mixture-of-experts language model uses a router and expert weights."}],
                "decode-baseline", output, args.timeout, args.decode_tokens))
            with http.open(base + "/metrics", timeout=10) as response:
                (output / "metrics.txt").write_bytes(response.read())
            if "WINDOWS_OK" not in one["content"] or "BLUE-731" not in results[2]["content"]:
                raise RuntimeError("Generated responses did not satisfy single-turn/history checks")
            if errors or not samples:
                raise RuntimeError("Resource sampling failed: " + str(errors))
            events = [json.loads(line) for line in (output / "native-events.jsonl").read_text(encoding="utf-8").splitlines()]
            if any(e["v"] != 1 or e["seq"] != i for i, e in enumerate(events)):
                raise RuntimeError("Invalid telemetry sequence")
            reads = [e for e in events if e["ev"] == "ssd.io.read"]
            if not reads or any(not e["success"] or e["pending_reads"] for e in reads):
                raise RuntimeError("SSD read telemetry missing or unsuccessful")
            settings_events = [e for e in events if e["ev"] == "ssd.settings"]
            if len(settings_events) != 1 or settings_events[0]["values"]["host.budget_mib"] != args.budget_mib:
                raise RuntimeError("Effective memory settings do not match acceptance")
            log = (output / "server.stderr.log").read_text(encoding="utf-8", errors="replace")
            if "read_ring backend=ioring" not in log or "GGUF alias: loaded" not in log or "direct reads enabled" not in log:
                raise RuntimeError("Missing native IoRing/direct GGUF source receipts")
            model_bytes = sum(p["bytes"] for p in provenance)
            if model_bytes <= samples[0]["system"]["total_physical"]:
                raise RuntimeError("This acceptance requires a model larger than physical RAM")
            write_json(output / "summary.json", {"status": "passed", "startup_seconds": startup,
                       "requests": [{k: v for k, v in r.items() if k != "events"} for r in results],
                       "peak_working_set": max(s["working_set"] for s in samples),
                       "peak_private_bytes": max(s["private_bytes"] for s in samples),
                       "samples": len(samples), "budget_mib": args.budget_mib, "context": args.context,
                       "model_bytes": model_bytes, "physical_ram": samples[0]["system"]["total_physical"],
                       "ssd_io_observations": len(reads), "ssd_demand_read_bytes": sum(e["read_bytes"] for e in reads),
                       "ssd_read_requests": sum(e["read_requests"] for e in reads),
                       "cache_hits": sum(e["cache_hits"] for e in reads),
                       "cache_misses": sum(e["cache_misses"] for e in reads),
                       "read_errors": sum(not e["success"] for e in reads),
                       "process_io_last_sample": samples[-1]["io"]})
    except Exception as error:
        write_json(output / "failure.json", {"error": str(error), "requests_completed": len(results)})
        raise
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=15)
        stop.set()
        if watcher:
            watcher.join(timeout=10)
        if process:
            write_json(output / "cleanup.json", {"pid": process.pid, "returncode": process.poll(),
                                                  "monitor_errors": errors})


if __name__ == "__main__":
    main()
