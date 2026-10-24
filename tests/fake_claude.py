"""A stand-in for `claude -p`: records how it was called, answers by the mode file in its cwd.

usage: fake_claude.py <record.json> [claude args...]
modes (file `fake_mode` in cwd): ok (default), error, garbage, sleep, denied
"""
import json
import os
import sys
import time

record, args = sys.argv[1], sys.argv[2:]
prompt = sys.stdin.read()
with open(record, "w", encoding="utf-8") as f:
    json.dump({"args": args, "stdin": prompt, "env": sorted(os.environ), "cwd": os.getcwd()}, f)

mode = open("fake_mode").read().strip() if os.path.exists("fake_mode") else "ok"
session = args[args.index("--resume") + 1] if "--resume" in args else "sess-new"
if mode == "sleep":
    time.sleep(30)
if mode == "garbage":
    print("this is not json")
    sys.exit(0)
out = {"type": "result", "subtype": "success", "is_error": False, "result": "echo: " + prompt.strip(),
       "session_id": session, "total_cost_usd": 0.01, "num_turns": 1, "permission_denials": []}
if mode == "error":
    out.update(subtype="error_max_turns", is_error=True, result="")
if mode == "denied":
    out["permission_denials"] = [{"tool_name": "Read", "tool_input": {"file_path": "../x"}}]
print(json.dumps(out))
