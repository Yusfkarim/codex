#!/usr/bin/env python3
"""codex_chat.py — talk to Codex/ChatGPT with the exported token, no Codex CLI needed.

    python3 codex_chat.py "your prompt here"
    python3 codex_chat.py -m gpt-5.6-terra "your prompt here"
    python3 codex_chat.py --raw "your prompt here"     # full SSE dump

Reads the token from auth.json next to this script (or $CODEX_HOME/auth.json).
"""
import argparse
import json
import os
import pathlib
import sys
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
CODEX_HOME = pathlib.Path(os.environ.get("CODEX_HOME", HERE))
AUTH = CODEX_HOME / "auth.json"
ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"
MODELS = "https://chatgpt.com/backend-api/codex/models?client_version=0.167.2"


def load_tokens():
    if not AUTH.exists():
        sys.exit(f"no auth.json at {AUTH}")
    data = json.loads(AUTH.read_text())
    tokens = data.get("tokens", {})
    return tokens.get("access_token"), tokens.get("account_id")


def list_models(token, account_id):
    req = urllib.request.Request(MODELS)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("chatgpt-account-id", account_id)
    req.add_header("User-Agent", "codex_cli_rs/0.167.2")
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    return [m["slug"] for m in data.get("models", [])]


def chat(token, account_id, prompt, model, raw=False):
    body = json.dumps({
        "model": model,
        "instructions": "You are Codex, an agentic coding assistant.",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
        "store": False,
        "stream": True,
    }).encode()
    req = urllib.request.Request(ENDPOINT, data=body)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("chatgpt-account-id", account_id)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream")
    req.add_header("User-Agent", "codex_cli_rs/0.167.2")
    req.add_header("originator", "codex_cli_rs")

    out = []
    with urllib.request.urlopen(req, timeout=180) as resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                event = json.loads(payload)
            except Exception:
                continue
            if raw:
                print(payload)
            for item in event.get("output", []) or []:
                for part in item.get("content", []) or []:
                    if part.get("text"):
                        out.append(part["text"])
            delta = event.get("delta") or (event.get("response", {}) or {}).get("delta")
            if isinstance(delta, str):
                out.append(delta)
    return "".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt", nargs="*")
    ap.add_argument("-m", "--model", default="gpt-6-luna")
    ap.add_argument("--list-models", action="store_true")
    ap.add_argument("--raw", action="store_true")
    args = ap.parse_args()

    token, account_id = load_tokens()
    if not token:
        sys.exit("no access_token in auth.json")

    if args.list_models:
        for slug in list_models(token, account_id):
            print(slug)
        return 0

    prompt = " ".join(args.prompt).strip()
    if not prompt:
        ap.error("a prompt is required")

    answer = chat(token, account_id, prompt, args.model, raw=args.raw)
    if not args.raw:
        print(answer)


if __name__ == "__main__":
    main()
