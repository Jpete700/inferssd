# SPDX-License-Identifier: Apache-2.0
"""Start, inspect and stop the native Windows InferSSD CPU server."""
from __future__ import annotations

import argparse
from collections import deque
import csv
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT
PACKAGE_MANIFEST = ROOT / "inferssd-package.json"
FRAMEWORK_SRC = ROOT / "framework/src"
CHAT_TEMPLATE = ROOT / "engine/models/templates/deepseek-ai-DeepSeek-V4.1.jinja"


def default_binary() -> Path:
    packaged = ROOT / "bin/inferssd-server.exe"
    if packaged.is_file():
        return packaged
    return ROOT / "build-windows-cpu/bin/inferssd-server.exe"


def default_state() -> Path:
    if PACKAGE_MANIFEST.is_file():
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "InferSSD/windows-server"
        return Path.home() / "AppData/Local/InferSSD/windows-server"
    return ROOT / "build-windows-cpu/windows-server"


DEFAULT_STATE = default_state()
ACTIVE = {"preparing", "loading", "ready", "stopping"}


def save(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def api():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        "GetProcessTimes": ([wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, wintypes.BOOL),
        "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        "TerminateProcess": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        getattr(kernel, name).argtypes = arguments
        getattr(kernel, name).restype = result
    return kernel


def creation_time(kernel, handle) -> int:
    values = [wintypes.FILETIME() for _ in range(4)]
    if not kernel.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)):
        raise ctypes.WinError(ctypes.get_last_error())
    return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime


class ProcessRef:
    """A held process handle, checked against creation time before any stop."""
    def __init__(self, pid: int, expected: int | None = None):
        self.kernel = api()
        self.handle = self.kernel.OpenProcess(0x100000 | 0x1000 | 1, False, pid)
        self.created = None
        if not self.handle and ctypes.get_last_error() not in (0, 87, 1168):
            raise ctypes.WinError(ctypes.get_last_error())
        if self.handle:
            try:
                self.created = creation_time(self.kernel, self.handle)
                if expected is not None and self.created != expected:
                    self.close()
            except BaseException:
                self.close()
                raise

    def alive(self) -> bool:
        return bool(self.handle) and self.kernel.WaitForSingleObject(self.handle, 0) == 258

    def terminate(self) -> None:
        if self.alive() and not self.kernel.TerminateProcess(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class Job:
    """Closing the supervisor's sole job handle also kills its server tree."""
    def __init__(self):
        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("minimum_ws", ctypes.c_size_t),
                        ("maximum_ws", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", ctypes.c_ulonglong * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

        self.kernel = api()
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, process: subprocess.Popen) -> None:
        child = self.kernel.OpenProcess(0x100 | 1, False, process.pid)
        try:
            if not child or not self.kernel.AssignProcessToJobObject(self.handle, child):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            if child:
                self.kernel.CloseHandle(child)

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def current(state_file: Path) -> dict:
    state = load(state_file)
    if state.get("status") in ACTIVE:
        owner = ProcessRef(state["supervisor_pid"], state["supervisor_created"])
        try:
            if not owner.alive():
                state = {**state, "status": "interrupted", "error": "Supervisor exited; its server job was closed."}
        finally:
            owner.close()
    return state


def runtime_environment(directory: str | None) -> dict:
    env = os.environ.copy()
    if directory:
        runtime = Path(directory)
        if not runtime.is_dir():
            raise ValueError(f"Runtime DLL directory does not exist: {runtime}")
    elif (ROOT / "bin/libgcc_s_seh-1.dll").is_file():
        # A packaged build keeps the MinGW runtime next to the server binary.
        runtime = ROOT / "bin"
    else:
        runtime = Path("C:/msys64/ucrt64/bin")
    if runtime.is_dir():
        env["PATH"] = str(runtime.resolve()) + os.pathsep + env.get("PATH", "")
    return env


def preflight(config: dict) -> tuple[list[Path], dict]:
    binary, model = Path(config["binary"]), Path(config["model"])
    if not binary.is_file():
        if PACKAGE_MANIFEST.is_file():
            raise ValueError(f"Server binary not found: {binary}. Re-extract the package or pass --binary.")
        raise ValueError("Server binary not found; run scripts/build-windows-cpu.ps1 first or pass --binary.")
    if not model.is_file():
        raise ValueError(f"Model file not found: {model}")
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            listener.bind(("127.0.0.1", config["port"]))
        except OSError as error:
            raise ValueError(f"Cannot bind port {config['port']}; choose another --port or stop its server: {error}") from error
    match = re.fullmatch(r"(.+)-(\d{5})-of-(\d{5})\.gguf", model.name)
    if match:
        prefix, number, count = match.groups()
        if number != "00001":
            raise ValueError("--model must point to the first GGUF shard (00001).")
        shards = [model.parent / f"{prefix}-{i:05d}-of-{count}.gguf" for i in range(1, int(count) + 1)]
    else:
        shards = [model]
    missing = [str(p) for p in shards if not p.is_file()]
    if missing:
        raise ValueError("Incomplete GGUF shard set: " + ", ".join(missing))
    sys.path.insert(0, str(FRAMEWORK_SRC))
    from inferssd.source.header import read
    metadata = read(str(model)).kv
    if metadata.get("general.architecture") != "deepseek41":
        raise ValueError("This Windows launcher currently accepts the validated deepseek41 model profile only.")
    probe = subprocess.run([str(binary), "--version"], env=runtime_environment(config["runtime_dir"]),
                           capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    if probe.returncode:
        raise ValueError(f"Server cannot start (exit {probe.returncode}); check --runtime-dir and the UCRT64 DLLs.")
    return shards, {"version": (probe.stdout + probe.stderr).decode("utf-8", "replace"),
                    "architecture": metadata["general.architecture"],
                    "model_bytes": sum(p.stat().st_size for p in shards)}


def doctor(args) -> dict:
    """Check the launcher, runtime, package payload and optional model without starting."""
    checks = []

    def record(name, function):
        try:
            checks.append({"check": name, "ok": True, "detail": function()})
        except Exception as error:
            checks.append({"check": name, "ok": False, "error": str(error)})

    def python_check():
        if sys.version_info < (3, 9):
            raise RuntimeError("Python 3.9 or later is required.")
        return {"version": sys.version.split()[0], "executable": sys.executable,
                "bits": 8 * ctypes.sizeof(ctypes.c_void_p)}

    def package_check():
        manifest = load(PACKAGE_MANIFEST)
        if not manifest:
            return {"layout": "source checkout", "root": str(ROOT)}
        return {"layout": "package", "version": manifest.get("version"),
                "git_revision": manifest.get("git_revision")}

    def server_check():
        binary = Path(args.binary)
        if not binary.is_file():
            raise FileNotFoundError(f"Server binary not found: {binary}")
        probe = subprocess.run([str(binary), "--version"], env=runtime_environment(args.runtime_dir),
                               capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        if probe.returncode:
            raise RuntimeError(f"Server cannot start (exit {probe.returncode}); check --runtime-dir and the DLLs.")
        text = (probe.stdout + probe.stderr).decode("utf-8", "replace").strip()
        return {"path": str(binary), "version": text.splitlines()[0] if text else ""}

    def runtime_check():
        names = ("libgcc_s_seh-1.dll", "libstdc++-6.dll", "libwinpthread-1.dll")
        directories = []
        if args.runtime_dir:
            directories.append(Path(args.runtime_dir))
        directories.extend((Path(args.binary).parent, Path("C:/msys64/ucrt64/bin")))
        for directory in directories:
            found = [name for name in names if (directory / name).is_file()]
            if len(found) == len(names):
                return {"directory": str(directory), "dlls": list(names)}
        raise FileNotFoundError("The MinGW runtime DLLs were not found next to the server; pass --runtime-dir.")

    def framework_check():
        sys.path.insert(0, str(FRAMEWORK_SRC))
        from inferssd.source import gguf_alias, header
        if CHAT_TEMPLATE.is_file():
            template = CHAT_TEMPLATE
        else:
            raise FileNotFoundError(f"Chat template not found: {CHAT_TEMPLATE}")
        return {"header": str(Path(header.__file__)), "gguf_alias": str(Path(gguf_alias.__file__)),
                "template": str(template)}

    def state_check():
        state_dir = Path(args.state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        probe = state_dir / ".doctor"
        probe.write_text("ok", encoding="ascii")
        probe.unlink()
        return {"directory": str(state_dir), "writable": True}

    def port_check():
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(("127.0.0.1", args.port))
        return {"address": f"127.0.0.1:{args.port}", "available": True}

    def model_check():
        model = Path(args.model)
        if not model.is_file():
            raise FileNotFoundError(f"Model file not found: {model}")
        match = re.fullmatch(r"(.+)-(\d{5})-of-(\d{5})\.gguf", model.name)
        if match:
            prefix, number, count = match.groups()
            if number != "00001":
                raise ValueError("--model must point to the first GGUF shard (00001).")
            shards = [model.parent / f"{prefix}-{i:05d}-of-{count}.gguf" for i in range(1, int(count) + 1)]
        else:
            shards = [model]
        missing = [str(path) for path in shards if not path.is_file()]
        if missing:
            raise ValueError("Incomplete GGUF shard set: " + ", ".join(missing))
        sys.path.insert(0, str(FRAMEWORK_SRC))
        from inferssd.source.header import read
        architecture = read(str(model)).kv.get("general.architecture")
        if architecture != "deepseek41":
            raise ValueError("This Windows package accepts the validated deepseek41 model profile only.")
        return {"architecture": architecture, "shards": len(shards),
                "bytes": sum(path.stat().st_size for path in shards)}

    record("python", python_check)
    record("package", package_check)
    record("server", server_check)
    record("runtime", runtime_check)
    record("framework", framework_check)
    record("state", state_check)
    record("port", port_check)
    if args.model is not None:
        record("model", model_check)
    return {"status": "ok" if all(check["ok"] for check in checks) else "failed",
            "root": str(ROOT), "checks": checks}


def stop_requested(run: Path, token: str) -> bool:
    return load(run / "stop.json").get("token") == token


def supervise(config: dict) -> int:
    import msvcrt
    state_file, run = Path(config["state_file"]), Path(config["run"])
    lock = state_file.with_suffix(".lock").open("a+b")
    process = job = None
    owner = ProcessRef(os.getpid())
    state = {**config, "supervisor_pid": os.getpid(), "supervisor_created": owner.created,
             "status": "preparing", "url": f"http://127.0.0.1:{config['port']}", "server_pid": None}
    owner.close()
    try:
        lock.seek(0)
        if lock.read(1) == b"":
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError("Another launcher owns this port's state; use status or stop first.")
        save(state_file, state)
        shards, facts = preflight(config)
        save(run / "preflight.json", facts)
        if stop_requested(run, config["token"]):
            state["status"] = "stopped"
            return 0
        from inferssd.source.gguf_alias import FIELDS, build_rows
        rows, summary = build_rows(shards, {"up", "gate", "down"}, None)
        if set(summary["by_type"]) != {"MXFP4"}:
            raise ValueError("The validated DeepSeek expert format is MXFP4; this quantization is unsupported.")
        with (run / "experts.tsv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS, delimiter="\t")
            writer.writeheader()
            writer.writerows(rows)
        save(run / "experts.json", summary)
        del rows
        (run / "settings.ssd").write_text(
            "inferssd-settings 1\nexpert.source = " + json.dumps([(run / "experts.tsv").as_posix()]) +
            f"\nhost.budget_mib = {config['memory_mib']}\nhost.headroom_mib = 2048\nprefetch.depth = 2\n", encoding="utf-8")
        command = [config["binary"], "-m", config["model"], "--alias", "windows-deepseek", "-ngl", "0",
                   "-c", str(config["context"]), "-b", "64", "-ub", "16", "-t", str(config["threads"]),
                   "-tb", str(config["threads"]), "-np", "1", "--threads-http", "4", "--defer-experts",
                   "--fit", "off", "--no-warmup", "--cache-ram", "0", "--reasoning", "off",
                   "--host", "127.0.0.1", "--port", str(config["port"]), "--metrics", "--slots",
                   "--chat-template-file", str(CHAT_TEMPLATE),
                   "--chat-template-kwargs", '{"drop_thinking":false}', "--ssd-settings", str(run / "settings.ssd"),
                   "--ssd-telemetry", str(run / "native-events.jsonl")]
        save(run / "launch.json", {"command": command})
        job = Job()
        with (run / "server.stdout.log").open("wb") as stdout, (run / "server.stderr.log").open("wb") as stderr:
            process = subprocess.Popen(command, cwd=REPO, env=runtime_environment(config["runtime_dir"]),
                                       stdout=stdout, stderr=stderr, creationflags=subprocess.CREATE_NO_WINDOW)
            job.assign(process)
            child = ProcessRef(process.pid)
            state.update(status="loading", server_pid=process.pid, server_created=child.created)
            child.close()
            save(state_file, state)
            http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            deadline = time.monotonic() + config["timeout"]
            while not stop_requested(run, config["token"]):
                if process.poll() is not None:
                    raise RuntimeError(f"Server exited ({process.returncode}); see {run / 'server.stderr.log'}")
                if state["status"] != "ready":
                    if time.monotonic() > deadline:
                        raise TimeoutError("Model startup timed out; inspect the server logs.")
                    try:
                        with http.open(state["url"] + "/health", timeout=2) as response:
                            if json.load(response).get("status") == "ok":
                                state["status"] = "ready"
                                save(state_file, state)
                                print(f"Ready: {state['url']}\nAPI: {state['url']}/v1\nLogs: {run}", flush=True)
                    except urllib.error.HTTPError as error:
                        if error.code != 503:
                            raise
                    except urllib.error.URLError as error:
                        if getattr(error.reason, "winerror", None) == 10013:
                            raise RuntimeError("Socket 10013: allow local loopback HTTP for this launcher.") from error
                time.sleep(0.2)
        state["status"] = "stopped"
        return 0
    except KeyboardInterrupt:
        state["status"] = "stopped"
        return 0
    except Exception as error:
        state.update(status="failed", error=str(error))
        print(f"Startup/service failed: {error}", file=sys.stderr, flush=True)
        return 1
    finally:
        if process and process.poll() is None:
            process.terminate()
        if job:
            job.close()
        if process:
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=15)
        state["server_exit_code"] = process.returncode if process else None
        state["finished_utc"] = datetime.now(timezone.utc).isoformat()
        # A contender that failed to acquire the lock must not overwrite its owner.
        if load(state_file).get("token") == config["token"]:
            save(state_file, state)
        save(run / "result.json", state)
        lock.close()


def request_stop(state: dict, timeout: float = 30) -> dict:
    if state.get("status") not in ACTIVE:
        return state
    owner = ProcessRef(state["supervisor_pid"], state["supervisor_created"])
    try:
        if owner.alive():
            save(Path(state["run"]) / "stop.json", {"token": state["token"]})
            if owner.kernel.WaitForSingleObject(owner.handle, int(timeout * 1000)) == 258:
                owner.terminate()  # Closing its job handles also terminates its server.
                if owner.kernel.WaitForSingleObject(owner.handle, 15000) != 0:
                    raise RuntimeError("Supervisor did not exit; inspect its log and state before retrying.")
        return current(Path(state["state_file"]))
    finally:
        owner.close()


def main(argv=None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] == "_supervise":
        internal = argparse.ArgumentParser(add_help=False)
        internal.add_argument("--config", type=Path, required=True)
        return supervise(load(internal.parse_args(arguments[1:]).config))
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("start", "serve", "status", "stop", "logs", "doctor"):
        command = commands.add_parser(action)
        command.add_argument("--port", type=int, default=8080)
        command.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
        if action in ("start", "serve"):
            command.add_argument("--model", type=Path, required=True, help="first GGUF shard")
            command.add_argument("--memory-mib", type=int, default=4096)
            command.add_argument("--context", type=int, default=1024)
            command.add_argument("--threads", type=int, default=min(12, os.cpu_count() or 1))
            command.add_argument("--binary", type=Path, default=default_binary())
            command.add_argument("--runtime-dir", help="directory containing the UCRT64 runtime DLLs")
            command.add_argument("--timeout", type=int, default=300)
            command.add_argument("--open-browser", action="store_true")
        if action == "doctor":
            command.add_argument("--model", type=Path, help="optional first GGUF shard to validate")
            command.add_argument("--binary", type=Path, default=default_binary())
            command.add_argument("--runtime-dir", help="directory containing the UCRT64 runtime DLLs")
        if action == "logs":
            command.add_argument("--lines", type=int, default=40)
    args = parser.parse_args(arguments)
    if os.name != "nt":
        parser.error("This launcher requires native Windows.")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be 1..65535")
    args.state_dir = args.state_dir.resolve()
    if args.action == "doctor":
        report = doctor(args)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "ok" else 1
    state_file = args.state_dir / f"port-{args.port}.json"
    state = current(state_file)
    if args.action == "status":
        print(json.dumps(state or {"status": "stopped", "port": args.port}, ensure_ascii=False, indent=2))
        return 0 if state.get("status") == "ready" else 2
    if args.action == "stop":
        state = request_stop(state)
        print(json.dumps(state or {"status": "stopped", "port": args.port}, ensure_ascii=False, indent=2))
        return 0
    if args.action == "logs":
        if not state:
            raise ValueError("No launch recorded for this port.")
        for name in ("supervisor.log", "server.stderr.log"):
            path = Path(state["run"]) / name
            print(f"--- {path} ---")
            if path.exists():
                with path.open(encoding="utf-8", errors="replace") as stream:
                    print("".join(deque(stream, maxlen=max(1, args.lines))), end="")
        return 0
    if state.get("status") in ACTIVE:
        raise ValueError(f"A server is already managed on port {args.port}; use status or stop first.")
    if args.memory_mib < 1024 or not 128 <= args.context <= 131072 or not 1 <= args.threads <= 255 or args.timeout < 5:
        parser.error("Require memory >= 1024 MiB, context 128..131072, threads 1..255 and timeout >= 5 seconds.")
    token = uuid.uuid4().hex
    run = args.state_dir / "runs" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + token[:8])
    run.mkdir(parents=True)
    config = {"model": str(args.model.resolve()), "binary": str(args.binary.resolve()),
              "port": args.port, "memory_mib": args.memory_mib, "context": args.context,
              "threads": args.threads, "runtime_dir": args.runtime_dir, "timeout": args.timeout,
              "token": token, "run": str(run), "state_file": str(state_file)}
    save(run / "config.json", config)
    if args.action == "serve":
        if args.open_browser:
            parser.error("Use start --open-browser, or open the URL printed after serve becomes ready.")
        return supervise(config)
    with (run / "supervisor.log").open("wb") as log:
        supervisor = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_supervise", "--config", str(run / "config.json")],
                                      stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        deadline = time.monotonic() + args.timeout + 30
        while time.monotonic() < deadline:
            state = current(state_file)
            if state.get("token") == token and state.get("status") == "ready":
                print(json.dumps(state, ensure_ascii=False, indent=2))
                if args.open_browser:
                    webbrowser.open(state["url"])
                return 0
            if state.get("token") == token and state.get("status") == "stopped":
                supervisor.wait(timeout=15)
                print(json.dumps(state, ensure_ascii=False, indent=2))
                return 0
            if supervisor.poll() is not None:
                detail = state.get("error", "See the supervisor log for details.") if state.get("token") == token else "Another launcher may own this state."
                raise RuntimeError(f"Launch failed: {detail}\nLogs: {run}")
            time.sleep(0.2)
        raise TimeoutError(f"Startup timed out; see {run}")
    except BaseException:
        save(run / "stop.json", {"token": token})
        try:
            supervisor.wait(timeout=30)
        except subprocess.TimeoutExpired:
            supervisor.terminate()
            supervisor.wait(timeout=15)
        raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Startup interrupted; the owned server was stopped.", file=sys.stderr)
        raise SystemExit(130)
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        print(f"InferSSD: {error}", file=sys.stderr)
        raise SystemExit(1)
