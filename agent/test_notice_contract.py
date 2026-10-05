"""The turn-notice conformance gate.

Run this before shipping any change to `_turn_notice` or to the completion
path in `serve.py`.

    python3 -m agent.test_notice_contract      (from the repo root)

The rule it exists to enforce:

    The engine states why a turn ran no tools. The client renders that
    sentence; it does not work the reason out for itself.

Why that is a rule and not a preference: only the engine can tell a model
that asked for nothing from a model that asked for something the engine
could not read. The parser knows (`_unreadable_calls`), and the turn looks
identical from the other end of the socket either way. A client inferring it
from its own screen cannot separate those two, so it reports a parse failure
as a model that ignores the tool format -- and the user goes looking for the
wrong fix.

What it refuses to let happen:

  1. A notice on a turn that ran tools. That turn needs no explanation.
  2. `no-tool-call` winning over `unreadable-call-syntax`. The second is the
     more specific and more actionable fact, and it is the one the engine
     used to throw away in a log line.
  3. A notice on a truncated turn. Truncation is already reported, and that
     turn's last round is narration rather than an answer.
  4. A notice with an empty `code` or `text`, which the wire contract
     requires and which would render as a blank row.
  5. The call site disappearing. `_turn_notice` existing is not the fix; the
     completion path calling it is.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.serve import _turn_notice  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(ok: bool, label: str) -> None:
    (PASSED if ok else FAILED).append(label)
    print(("  ok   " if ok else "  FAIL ") + label)


def _turn(**kw) -> dict:
    base: dict = {"id": "t1", "output": []}
    base.update(kw)
    return base


def _text(s: str) -> list:
    return [{"type": "text_delta", "content": s}]


def main() -> int:
    print("\n-- a turn that ran tools needs no explanation --")
    for tools in (["shell"], ["shell", "fs_read"]):
        check(_turn_notice(_turn(), tools) is None,
              f"no notice when tools ran ({tools})")

    print("\n-- a turn that answered without tools --")
    n = _turn_notice(_turn(output=_text("Here is your answer.")), [])
    check(n is not None, "a notice is stated")
    check(n is not None and n["code"] == "no-tool-call",
          "and it is named no-tool-call")
    check(n is not None and bool(n["text"].strip()),
          "and it carries a sentence")

    print("\n-- the fact that used to die in a log line --")
    n = _turn_notice(_turn(output=_text("Done."), _unreadable_calls=True), [])
    check(n is not None and n["code"] == "unreadable-call-syntax",
          "unreadable call syntax is named as such")
    check(n is not None and n["code"] != "no-tool-call",
          "and it does NOT come out as no-tool-call -- the whole point")
    check(n is not None and "could not read" in n["text"],
          "and the sentence says the engine could not read the format")

    print("\n-- the boundaries --")
    check(_turn_notice(_turn(output=_text("text")), []) is not None,
          "no-tool-call needs the turn to have said something")
    check(_turn_notice(_turn(output=[]), []) is None,
          "silence alone is not a tool-format fault")
    check(_turn_notice(_turn(output=_text("x"), truncated=True), []) is None,
          "a truncated turn is not also called tool-less")
    check(_turn_notice(
        _turn(output=_text("x"), truncated=True, _unreadable_calls=True), []
    ) is not None,
        "but a parse failure is still stated on a truncated turn")
    check(_turn_notice(
        _turn(output=[{"type": "text_delta", "content": "   "}]), []) is None,
        "whitespace is not an answer")
    check(_turn_notice(
        _turn(output=[], _unreadable_calls=True), []) is not None,
        "an unreadable call is stated even when the turn said nothing")

    print("\n-- the wire contract --")
    for name, turn in (
        ("no-tool-call", _turn(output=_text("a"))),
        ("unreadable-call-syntax",
         _turn(output=_text("a"), _unreadable_calls=True)),
    ):
        n = _turn_notice(turn, [])
        check(n is not None and bool(n.get("code")),
              f"{name}: code is present")
        check(n is not None and bool(str(n.get("text", "")).strip()),
              f"{name}: text is present")
        check(n is not None and set(n.keys()) == {"code", "text"},
              f"{name}: carries exactly code and text")

    print("\n-- the call site is the fix --")
    src = Path(__file__).with_name("serve.py").read_text(encoding="utf-8")
    check("_turn_notice(turn, tools_run)" in src,
          "the completion path calls _turn_notice")
    check('completed["notice"] = notice' in src,
          "and puts the result on the turn/completed payload")
    check('turn["_unreadable_calls"] = True' in src,
          "and the parser's verdict is recorded on the turn")

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for label in FAILED:
        print("  FAILED: " + label)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
