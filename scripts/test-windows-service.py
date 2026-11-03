# SPDX-License-Identifier: Apache-2.0
"""Check HTTP chat, SSE and disconnect cancellation on an idle local server."""
from __future__ import annotations

import argparse
import http.client
import json
from pathlib import Path
import socket
import time
import urllib.error
import urllib.parse
import urllib.request


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(base: str, output: Path) -> None:
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(path):
        with client.open(base + path, timeout=30) as response:
            return json.load(response)

    def request(prompt, **extra):
        return {"model": "windows-deepseek", "messages": [{"role": "user", "content": prompt}],
                "temperature": 0, "seed": 42, "max_tokens": 24,
                "chat_template_kwargs": {"enable_thinking": False}, **extra}

    assert get("/health")["status"] == "ok"
    assert any(m["id"] == "windows-deepseek" for m in get("/v1/models")["data"])
    assert not any(s["is_processing"] for s in get("/slots")), "Wait for the browser/user request to finish first."
    with client.open(base + "/", timeout=10) as response:
        assert response.status == 200 and "text/html" in response.headers.get("Content-Type", "")
    try:
        client.open(urllib.request.Request(base + "/v1/chat/completions", b"{}", {"Content-Type": "application/json"}), timeout=10)
    except urllib.error.HTTPError as error:
        assert error.code == 400, error.code
    else:
        raise AssertionError("Malformed chat request was accepted")
    payload = request("Reply with exactly: WINDOWS_API_OK", stream=False)
    with client.open(urllib.request.Request(base + "/v1/chat/completions", json.dumps(payload).encode(),
                                        {"Content-Type": "application/json"}), timeout=300) as response:
        plain = json.load(response)
    write(output / "nonstream.json", plain)
    assert "WINDOWS_API_OK" in plain["choices"][0]["message"]["content"]
    print("Non-stream API and input validation passed.", flush=True)

    def stream(name, cancel=False):
        payload = request("Explain how a mixture-of-experts language model routes tokens to its experts in about 100 words.",
                          stream=True, max_tokens=256 if cancel else 32, stream_options={"include_usage": True})
        target = urllib.parse.urlsplit(base)
        connection = http.client.HTTPConnection(target.hostname, target.port, timeout=300)
        events, chunks, done, first, last = [], 0, False, None, None
        started = time.monotonic()
        try:
            connection.request("POST", "/v1/chat/completions", json.dumps(payload).encode(), {"Content-Type": "application/json"})
            transport = connection.sock
            response = connection.getresponse()
            assert response.status == 200 and "text/event-stream" in response.getheader("Content-Type", "")
            with (output / (name + ".sse")).open("wb") as raw:
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
                    assert "error" not in event, event
                    events.append(event)
                    if any(c.get("delta", {}).get("content") for c in event.get("choices", [])):
                        now = time.monotonic() - started
                        first = now if first is None else first
                        last = now
                        chunks += 1
                        if cancel and chunks == 2:
                            before = get("/slots")
                            assert any(s["is_processing"] for s in before), "Cancellation test finished before disconnect"
                            write(output / "slots-before-cancel.json", before)
                            transport.shutdown(socket.SHUT_RDWR)
                            break
            response.close()
        finally:
            connection.close()
        result = {"chunks": chunks, "done": done, "first_content_seconds": first,
                  "last_content_seconds": last, "wall_seconds": time.monotonic() - started, "events": events}
        write(output / (name + ".json"), result)
        if cancel:
            assert chunks == 2 and not done
        else:
            assert done and chunks >= 2 and last - first > 0.1, "No incremental content observed"
        return result

    streamed = stream("stream")
    print(f"Incremental SSE passed: {streamed['chunks']} content chunks.", flush=True)
    cancelled = stream("cancel", cancel=True)
    began = time.monotonic()
    deadline = began + 30
    while time.monotonic() < deadline:
        slots = get("/slots")
        if not any(s["is_processing"] for s in slots):
            break
        time.sleep(0.2)
    else:
        raise AssertionError("Disconnected request still occupies a slot after 30 seconds")
    write(output / "slots-after-cancel.json", slots)
    cancellation_seconds = time.monotonic() - began
    payload = request("Reply with exactly: AFTER_CANCEL_OK", stream=False)
    with client.open(urllib.request.Request(base + "/v1/chat/completions", json.dumps(payload).encode(),
                                        {"Content-Type": "application/json"}), timeout=300) as response:
        recovered = json.load(response)
    write(output / "after-cancel.json", recovered)
    assert "AFTER_CANCEL_OK" in recovered["choices"][0]["message"]["content"]
    write(output / "summary.json", {"status": "passed", "url": base, "stream_content_chunks": streamed["chunks"],
          "stream_content_span_seconds": streamed["last_content_seconds"] - streamed["first_content_seconds"],
          "cancel_content_chunks": cancelled["chunks"], "slot_release_seconds": cancellation_seconds,
          "nonstream": True, "post_cancel_recovery": True, "web_html": True, "invalid_request_rejected": True})
    print(f"Disconnect cancellation and recovery passed; slot released in {cancellation_seconds:.3f}s.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:18080")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    address = urllib.parse.urlsplit(args.url)
    if address.scheme != "http" or address.hostname != "127.0.0.1" or address.path not in ("", "/"):
        parser.error("Use the local launcher's http://127.0.0.1:PORT URL")
    args.output.mkdir(parents=True, exist_ok=False)
    try:
        run(args.url.rstrip("/"), args.output)
    except Exception as error:
        write(args.output / "failure.json", {"error": str(error)})
        raise


if __name__ == "__main__":
    main()
