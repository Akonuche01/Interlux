"""The tool-call conformance gate.

Run this before shipping any change to `calls/` or to the round loop in
`serve.py`.

    python3 -m agent.test_call_contract      (from the repo root)

The rule it exists to enforce:

    A model may write a call in any shape. The engine's job is to
    understand it, not to require one spelling. A shape the engine has
    never met is a missing module, never a broken turn.

What it refuses to let happen:

  1. A language list. Languages are discovered by asking, so a module
     dropped in later works with no engine change -- asserted here by
     pointing the scan at a throwaway package.
  2. Raw call syntax reaching the chat window. Whenever call-shaped text is
     matched, it must come out of the text -- including when nothing
     parsed, which is the case that used to leak a wall of JSON into chat.
  3. A code sample being executed as a tool request. Valid JSON that names
     no tool stays an answer.
  4. A round carrying two languages executing twice.
  5. A failed attempt being delivered as an answer. `broke` must be set so
     the caller can spend a round asking for a clean re-emit.

The corpus below is the shapes real models have actually emitted, including
the ones that caused outages. It is checked as a whole: any change to the
parser that alters one of these verdicts fails here rather than on the
device.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.calls import DIALECTS, extract, discover  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(ok: bool, label: str) -> None:
    (PASSED if ok else FAILED).append(label)
    print(("  ok   " if ok else "  FAIL ") + label)


# name, text, expected calls, expected found, expected broke
CORPUS: list[tuple[str, str, int, bool, bool]] = [
    ("empty", "", 0, False, False),
    ("plain answer", "The answer is 42.", 0, False, False),
    ("closed fence",
     'Here you go.\n```json\n[{"tool": "run_shell", '
     '"parameters": {"cmd": "ls"}}]\n```\n', 1, True, False),
    ("closed fence, two calls",
     '```json\n[{"tool": "a", "parameters": {}}, '
     '{"tool": "b", "parameters": {"x": 1}}]\n```', 2, True, False),
    ("unclosed fence",
     'Working on it.\n```json\n[{"tool": "fs_read", '
     '"parameters": {"path": "/tmp/x"}}]', 1, True, False),
    ("unclosed fence, broken json",
     'Working on it.\n```json\n[{"tool": "fs_read", "parameters": {"path"',
     0, True, True),
    ("fence-less array",
     'Sure: [{"tool": "git_status", "parameters": {}}] done.', 1, True, False),
    ("fence-less bare object",
     '{"tool": "git_status", "parameters": {}}', 0, False, False),
    ("fence-less tool_calls wrapper",
     '{"tool_calls": [{"tool": "git_status", "parameters": {}}]}',
     1, True, False),
    ("openai naming",
     '```json\n[{"name": "run_shell", '
     '"arguments": "{\\"cmd\\": \\"pwd\\"}"}]\n```', 1, True, False),
    ("flat sibling keys",
     '```json\n[{"tool": "run_shell", "cmd": "pwd"}]\n```', 1, True, False),
    ("code sample, not a call",
     'Use it like this:\n```json\n{"a": 1, "b": [2, 3]}\n```\nThat is all.',
     0, True, False),
    ("python code sample",
     '```python\nprint("[{\\"tool\\": \\"x\\"}]")\n```', 0, False, False),
    ("hermes xml arg pairs",
     '<tool_call>run_shell<arg_key>cmd</arg_key>'
     '<arg_value>pwd</arg_value></tool_call>', 1, True, False),
    ("hermes xml json body",
     '<tool_call>{"tool": "run_shell", "parameters": {"cmd": "pwd"}}'
     '</tool_call>', 1, True, False),
    ("xml bare tag",
     '<tool_call><shell><cmd>pwd</cmd></shell></tool_call>', 1, True, False),
    ("xml unclosed",
     '<tool_call>run_shell<arg_key>cmd</arg_key><arg_value>pwd</arg_value>',
     1, True, False),
    ("xml unparseable",
     '<tool_call>!!! not anything !!!</tool_call>', 0, True, True),
    ("zw shredded tag",
     '<tool\u200bcall>run_shell<arg_key>cmd</arg_key>'
     '<arg_value>p\u200bwd</arg_value></tool\u200bcall>', 1, True, False),
    ("fence and xml together",
     '```json\n[{"tool": "a", "parameters": {}}]\n```\n'
     '<tool_call>b<arg_key>k</arg_key><arg_value>v</arg_value></tool_call>',
     1, True, False),
    ("json without a tool name",
     '```json\n[{"parameters": {"x": 1}}]\n```', 0, True, False),
    ("empty fence", '```json\n```', 0, True, False),
    ("asterisk divider", 'Answer.\n*****\nMore.', 0, False, False),
]

# Text that must never survive into the chat window once matched.
SYNTAX_MARKERS = ("```json", "<tool_call>", "<toolcall>", "<arg_key>",
                  "<arg_value>")


def main() -> int:
    print("languages are discovered, never listed")
    names = [d.name for d in DIALECTS]
    check(len(DIALECTS) >= 2, f"more than one language is present -- {names}")
    check(all(d.run_when in ("always", "no-calls") for d in DIALECTS),
          "every language declares a valid run_when")
    check(all(isinstance(d.priority, int) for d in DIALECTS),
          "every language declares a priority")
    check(names == [d.name for d in discover()],
          "discovery is deterministic across calls")

    with tempfile.TemporaryDirectory() as tmp:
        package = Path(tmp) / "probe_calls"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
        (package / "oddball.py").write_text(
            "from agent.calls.base import CallParse\n"
            "DIALECT = 'oddball'\n"
            "PRIORITY = 5\n"
            "def parse(text):\n"
            "    if '<odd>' not in text:\n"
            "        return CallParse(text=text)\n"
            "    return CallParse(calls=[{'tool': 'x', 'parameters': {}}],\n"
            "                     text=text.replace('<odd>', ''), found=True)\n"
            "ADAPTER = parse\n",
            encoding="utf-8")
        sys.path.insert(0, tmp)
        try:
            found = discover("probe_calls")
        finally:
            sys.path.remove(tmp)
            for name in [m for m in sys.modules if m.startswith("probe_calls")]:
                del sys.modules[name]
    check([d.name for d in found] == ["oddball"],
          "a module declaring DIALECT becomes a language"
          + f" -- got {[d.name for d in found]}")
    check(bool(found) and found[0].priority == 5,
          "the new language brings its own priority")
    check(bool(found) and found[0].parse("<odd>").calls,
          "the new language is actually callable")

    print("\nthe corpus behaves")
    for name, text, want_calls, want_found, want_broke in CORPUS:
        calls, cleaned, found, broke = extract(text)
        check(len(calls) == want_calls,
              f"{name}: {want_calls} call(s) -- got {len(calls)}")
        check(found == want_found, f"{name}: found={want_found}")
        check(broke == want_broke, f"{name}: broke={want_broke}")
        if found:
            leaked = [m for m in SYNTAX_MARKERS if m in cleaned]
            check(not leaked,
                  f"{name}: no raw call syntax survives"
                  + ("" if not leaked else f" -- leaked {leaked}"))

    print("\nthe verdicts that caused outages")
    calls, _c, _f, _b = extract(
        'Use it like this:\n```json\n{"a": 1}\n```\nThat is all.')
    check(not calls, "a code sample in an answer is never executed")
    calls, _c, _f, _b = extract(
        '```json\n[{"tool": "a", "parameters": {}}]\n```\n'
        '<tool_call>b<arg_key>k</arg_key><arg_value>v</arg_value></tool_call>')
    check(len(calls) == 1,
          f"a two-language round executes exactly once -- got {len(calls)}")
    calls, _c, found, broke = extract('<tool_call>garbage</tool_call>')
    check(found and broke and not calls,
          "a failed attempt is reported, not delivered as an answer")
    calls, _c, _f, _b = extract(
        '```json\n[{"tool": "a", "parameters": {}}]\n```')
    check(calls and calls[0]["tool"] == "a", "a normal call still parses")

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for label in FAILED:
        print("  FAILED: " + label)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
