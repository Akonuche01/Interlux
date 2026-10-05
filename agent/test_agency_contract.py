"""The agency conformance gate (Finding 29).

Run this before shipping any change to the failure wording, to
`_note_tool_outcome` / `_goal_state`, or to the closing branch of the round
loop in `serve.py`.

    python3 -m agent.test_agency_contract      (from the repo root)

The rule it exists to enforce:

    A tool that failed is a fact to work around, not a reason to hand the job
    back. A turn may not close as finished while a failure is unresolved.

Why that is a rule and not a preference: the engine's loop used to have no
opinion about outcomes at all. It ended when the model stopped asking for
tools, which is the same place a success ends -- so a turn whose only command
failed could close on a cheerful summary and be indistinguishable, on the
wire, from a finished job. Worse, the instruction the model was given said
"say so plainly and stop": the engine told her to quit at the first refusal
and then could not tell that she had.

What it refuses to let happen:

  1. An instruction that tells the model to stop on failure. The old wording
     is the switch -- while it reads "and stop", nothing else here matters.
  2. A failure that never clears. A retry of the same tool that succeeds is
     the recovery this exists to produce; if it cannot clear, every turn that
     ever hit a transient error gets nagged, and an alarm that fires on
     finished work is one the model learns to ignore.
  3. The engine's "unfinished" statement being appended after the round is
     announced. It would then live only in persisted history: the screen
     would still show a summary over a job that never ran.
  4. A second re-ask, or a re-ask with no round left to spend.
  5. The call sites disappearing. The helpers existing is not the fix; the
     round loop calling them is.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import serve  # noqa: E402
from agent.serve import (  # noqa: E402
    _GOAL_CHECK_PROMPT,
    _goal_state,
    _note_tool_outcome,
    _unfinished_statement,
    agency_text,
)
from agent.threads import fold_output  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(ok: bool, label: str) -> None:
    (PASSED if ok else FAILED).append(label)
    print(("  ok   " if ok else "  FAIL ") + label)


def _turn(**kw) -> dict:
    base: dict = {"id": "t1", "output": []}
    base.update(kw)
    return base


class _ScriptedProvider:
    """Plays back scripted rounds so the real round loop runs.

    The helpers can each be individually correct and still be wired to
    nothing. This is what shows the loop actually consults them.
    """

    def __init__(self, rounds: list[str]):
        self.rounds = list(rounds)
        self.seen: list[str] = []

    async def stream_turn(self, messages, model=None, images=None,
                          api="chat", stream=True):
        idx = len(self.seen)
        # The fold rides inside the last message's content.
        self.seen.append(str(messages[-1].get("content", "")))
        text = self.rounds[idx] if idx < len(self.rounds) else ""
        if text:
            yield {"type": "text_delta", "content": text}
        yield {"type": "complete"}


_CALL = '```json\n[{"tool": "bad_tool", "parameters": {}}]\n```'


def _drive(rounds: list[str], tool_returns: list[dict], max_rounds: int = 8):
    """Run one real turn. `tool_returns` is consumed one entry per call, so a
    retry can be made to succeed where the first attempt failed."""
    events: list[dict] = []
    calls: list[str] = []
    queue = list(tool_returns)

    async def fake_broadcast(payload):
        events.append(payload)

    async def fake_tool(**_kw):
        calls.append("bad_tool")
        if not queue:
            return {}
        return queue[min(len(calls) - 1, len(queue) - 1)]

    def noop(*_a, **_k):
        return None

    saved = {k: getattr(serve, k)
             for k in ("broadcast", "record_turn", "audit", "TOOLS",
                       "_drain_steer")}
    serve.broadcast = fake_broadcast
    serve.record_turn = noop
    serve.audit = type("_A", (), {"append": staticmethod(noop)})()
    serve._drain_steer = lambda _tid: ""
    serve.TOOLS = dict(serve.TOOLS)
    serve.TOOLS["bad_tool"] = fake_tool

    provider = _ScriptedProvider(rounds)
    turn = {"id": "th:0", "agent": "", "mode": "exec", "output": []}
    state = serve.new_state("th", agent="")
    try:
        asyncio.run(serve._execute_turn(
            {"id": 1}, {"max_rounds": max_rounds}, "do the thing",
            provider, "m", "th", turn, None, state, []))
    finally:
        for k, v in saved.items():
            setattr(serve, k, v)
    return turn, provider, events, calls


def _answer(turn: dict) -> str:
    return "".join(
        o.get("content", "") for o in turn["output"]
        if isinstance(o, dict) and o.get("type") == "text_delta")


def _err() -> dict:
    return {"status": "error", "stdout": "command not found"}


def _ok() -> dict:
    return {"status": "success", "stdout": "done"}


def main() -> int:
    prompt = agency_text("")

    print("\n-- no instruction tells her to stop --")
    check("and stop" not in prompt,
          "the agency prompt no longer says 'say so plainly and stop'")
    check("take a different approach" in prompt,
          "and it asks for a different approach instead")
    check("action did NOT happen" in prompt,
          "while still refusing to call a failed action done")

    folded = fold_output([
        {"tool": "shell", "result": {"status": "error", "stdout": "boom"}},
    ])
    check("FAILED" in folded, "a failed result is folded as FAILED")
    check("Report the failure to the user" not in folded,
          "the fold no longer tells her to report and give up")
    check("take a different approach" in folded,
          "and asks for a different approach instead")
    check("invent the output" in folded,
          "while still refusing invented output")

    check("and stop" not in _GOAL_CHECK_PROMPT,
          "the goal-check prompt does not tell her to stop")
    check("NOT finished" in _GOAL_CHECK_PROMPT,
          "it states the turn is not finished")
    check("do not report the task as done" in _GOAL_CHECK_PROMPT,
          "and forbids reporting it as done")

    print("\n-- recording an outcome --")
    t = _turn()
    check(_goal_state(t) == "", "a turn with no failure may close freely")
    _note_tool_outcome(t, "shell", ok=False)
    check(t.get("_failed_tools") == ["shell"], "a failure is recorded")
    _note_tool_outcome(t, "shell", ok=False)
    check(t.get("_failed_tools") == ["shell"], "recording it twice is one fact")
    _note_tool_outcome(t, "fs_read", ok=False)
    check(t.get("_failed_tools") == ["shell", "fs_read"],
          "a second tool that fails is recorded too")
    _note_tool_outcome(t, "shell", ok=True)
    check(t.get("_failed_tools") == ["fs_read"],
          "a retry that succeeds clears that tool and only that tool")
    _note_tool_outcome(t, "fs_read", ok=True)
    check(t.get("_failed_tools") == [],
          "clearing the last one leaves nothing outstanding")
    check(_goal_state(t) == "",
          "and a turn that recovered may close freely")
    _note_tool_outcome(t, "", ok=False)
    _note_tool_outcome(t, None, ok=False)  # type: ignore[arg-type]
    check(t.get("_failed_tools") == [],
          "an unnamed tool is not recorded as a failure")
    _note_tool_outcome(t, "shell", ok=True)
    check(t.get("_failed_tools") == [],
          "clearing a tool that never failed is harmless")

    print("\n-- the goal check, in order --")
    t = _turn()
    _note_tool_outcome(t, "shell", ok=False)
    check(_goal_state(t) == "re-ask",
          "an unresolved failure earns a re-ask")
    t["_goal_checked"] = True
    check(_goal_state(t) == "unfinished",
          "closing anyway is called unfinished")
    t["_unfinished_stated"] = True
    check(_goal_state(t) == "",
          "and it is stated once, never twice")

    print("\n-- what the engine says when she closes anyway --")
    t = _turn()
    _note_tool_outcome(t, "shell", ok=False)
    _note_tool_outcome(t, "fs_edit", ok=False)
    stmt = _unfinished_statement(t)
    check("shell" in stmt and "fs_edit" in stmt,
          "the statement names every tool that failed")
    check("did not finish" in stmt,
          "and says plainly that the turn did not finish")
    check("incomplete" in stmt,
          "and tells the reader to treat the work as incomplete")
    check(bool(stmt.strip()) and stmt.startswith("[engine]"),
          "and it is marked as the engine speaking")
    t = _turn()
    _note_tool_outcome(t, "shell", ok=False)
    t["_failed_tools"] = []
    check("the last action" in _unfinished_statement(t),
          "with nothing outstanding it still says something true")

    print("\n-- the loop, end to end --")
    turn, prov, events, calls = _drive(
        [_CALL, "All done! The task is complete."], [_err()])
    check(len(calls) == 1, "A: the tool ran once")
    check(len(prov.seen) == 3,
          f"A: the loop spent a third round asking (saw {len(prov.seen)})")
    check(len(prov.seen) > 2 and "NOT finished" in prov.seen[2],
          "A: the re-ask round carries the goal-check prompt")
    check(len(prov.seen) > 2 and "FAILED" in prov.seen[2],
          "A: and still carries the FAILED result in the fold")
    ans = _answer(turn)
    check("did not finish" in ans,
          "A: the answer states the turn did not finish")
    check("bad_tool" in ans, "A: and names the tool that failed")
    check("All done! The task is complete." in ans,
          "A: the summary survives, qualified by the engine")
    done = [e for e in events if e.get("type") == "round/completed"]
    check(bool(done) and done[-1]["final"] is True,
          "A: the last round is announced final")
    check(bool(done) and "did not finish" in done[-1]["text"],
          "A: the statement is inside THAT text -- what the client renders")

    turn, prov, _events, calls = _drive(
        [_CALL, _CALL, "Fixed it. Here is the result."], [_err(), _ok()])
    check(len(calls) == 2, "B: the tool ran twice")
    check(not turn.get("_failed_tools"),
          "B: nothing outstanding once the retry succeeded")
    check("did not finish" not in _answer(turn),
          "B: and the turn is NOT called unfinished -- recovery is not "
          "punished, or the alarm becomes noise")
    check(len(prov.seen) == 3,
          f"B: three rounds, no re-ask (saw {len(prov.seen)})")

    turn, prov, _events, calls = _drive(["Here is your answer."], [])
    check(not calls, "C: no tool ran")
    check(len(prov.seen) == 1, "C: one round only")
    check("did not finish" not in _answer(turn),
          "C: a clean turn is untouched")

    turn, prov, _events, calls = _drive(
        [_CALL, "I could not do it.", "Still cannot."], [_err()])
    check(len(calls) == 1, "D: the tool ran once")
    check(len(prov.seen) == 3,
          f"D: exactly one re-ask, then it closes (saw {len(prov.seen)})")
    check("did not finish" in _answer(turn),
          "D: an unresolved failure is stated as unfinished")

    print("\n-- the call sites are the fix --")
    src = Path(__file__).with_name("serve.py").read_text(encoding="utf-8")
    check("_note_tool_outcome(turn, tool_name, ok=False)" in src,
          "the round loop records a failed call")
    check("_note_tool_outcome(turn, tool_name, ok=True)" in src,
          "and records a successful one, so a retry can clear")
    check("_goal = _goal_state(turn)" in src,
          "the round loop consults the goal state")
    check('working_content += _GOAL_CHECK_PROMPT' in src,
          "and spends a round asking for a different approach")
    check("_unfinished_statement(turn)" in src,
          "and states the unfinished verdict when she closes anyway")
    check('or _goal == "re-ask"' in src,
          "the re-ask keeps the round from being announced as final")
    check(src.index("_goal = _goal_state(turn)")
          < src.index('"type": "round/completed"'),
          "the verdict is decided BEFORE the round is announced -- the "
          "statement has to be inside the text the client renders")
    check("turn[\"_had_failure\"]" not in src,
          "the old sticky flag is gone: a failure must be able to clear")

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for label in FAILED:
        print("  FAILED: " + label)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
