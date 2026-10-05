"""The single-resolver conformance gate.

Run this before shipping any change to how the engine decides *what* a call
runs on (provider, model) or *where* its files live (home, config dir).

    python3 -m agent.test_resolver_contract      (from the repo root)

The rule it exists to enforce:

    One question, one answer, one place. A provider, a model, a home and a
    config directory are each resolved by exactly one function, and every
    caller asks that function.

Why that is a rule and not a preference: the engine used to answer each of
those four questions in nine different places, and the nine did not agree.
The turn path read the policy for its provider; the compact path read the
literal string ``"openai"``. On a phone whose only configured provider was
``apinex`` the first worked and the second refused with *"provider not
available: openai"* -- a vendor its owner had never configured, named by the
engine, for a feature the user had just tapped. That is not one bug. It is
what nine answers to one question looks like, and it recurs every time a
tenth call site is written by someone reading one of the nine.

The same shape produced the home bug. Four copies of the Interlux home path
were written out in full; two of the nine guesses read ``HOME`` and three
read a hardcoded constant, so the answers agreed on the device and diverged
the moment ``HOME`` was unset -- which is exactly what a test host and a
scrubbed subprocess look like.

What it refuses to let happen:

  1. A vendor or a model named in code the engine uses to *choose*. An
     adapter may name its own vendor; the resolver may not name anyone's.
     A named default is also a data-egress decision made without the user.
  2. A second way to answer a question that already has an answer -- a new
     ``os.environ["HOME"]``, a new ``~/.interlux/agent``, a new copy of the
     Interlux path.
  3. ``resolve_run_target`` losing a call site. The helper existing is not
     the fix; the call sites using it is.
  4. A whitespace-only param silently blanking a provider the policy named.
     ``params.get(...) or policy.get(...)`` then ``.strip()`` reads as
     correct and is not: the blank string is truthy, so it wins the chain
     and is stripped to nothing afterwards.
  5. A missing provider being reported as the model talking. The old path
     yielded a ``text_delta`` reading *"No provider found: gpt-4o"* -- which
     renders as the model speaking, names the model rather than the missing
     configuration, and leaves the client unable to tell it from an answer.
"""

from __future__ import annotations

import ast
import io
import os
import sys
import tempfile
import tokenize
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import home as home_mod  # noqa: E402
from agent.home import (  # noqa: E402
    engine_config_dir,
    engine_home,
    engine_userland_dir,
)
from agent.pconfig import open_provider  # noqa: E402
from agent.policy import no_provider_message, resolve_run_target  # noqa: E402

AGENT_DIR = Path(__file__).resolve().parent
SERVE = AGENT_DIR / "serve.py"
HOME_PY = AGENT_DIR / "home.py"

# Names that belong to a vendor, not to the engine. An adapter under
# providers/ may use its own -- that is what an adapter is for. Nothing that
# *chooses* may.
VENDOR_LITERALS = {
    "openai", "anthropic", "google", "gemini", "mistral", "groq",
    "gpt-4o", "gpt-4", "gpt-4-turbo", "gpt-3.5-turbo",
    "claude-3-opus", "claude-3-sonnet", "claude-3-5-sonnet",
}

PASSED: list[str] = []
FAILED: list[str] = []


def check(ok: bool, label: str) -> None:
    (PASSED if ok else FAILED).append(label)
    print(("  ok   " if ok else "  FAIL ") + label)


# --------------------------------------------------------------------------
# reading source the way the rule means it
# --------------------------------------------------------------------------

def _engine_sources() -> list[Path]:
    """Every engine source file that must obey the rule.

    Excludes two kinds on purpose:

      * ``.bak-*`` -- committed leftovers, not code the engine runs.
      * ``test_*.py`` -- a gate has to *name* the thing it forbids in order
        to look for it. Scanning the gates with their own rule is how this
        check first failed: it found its own patterns and called them
        violations. The rule governs the engine, not the tests of the engine.
    """
    return sorted(p for p in AGENT_DIR.rglob("*.py")
                  if ".bak" not in p.name and not p.name.startswith("test_"))


def _code_only(path: Path) -> str:
    """Source with comments blanked, positions preserved.

    Comments are where a check like this gives a false pass: a line reading
    ``# this used to be "openai"`` is not a vendor default, and a docstring
    that documents the old home path is not a second copy of it. Only code
    counts, so only code is read.
    """
    src = path.read_text(encoding="utf-8", errors="replace")
    try:
        toks = [t for t in tokenize.generate_tokens(io.StringIO(src).readline)
                if t.type != tokenize.COMMENT]
    except (tokenize.TokenError, IndentationError):
        return src
    return tokenize.untokenize(toks)


def _string_literals(tree: ast.AST) -> list[str]:
    """Every string constant that is not a docstring.

    Docstrings describe; they do not decide. ``threads.py`` saying state
    lives under ``~/.interlux/agent/`` is documentation, and stripping it
    from the scan is what lets the check stay strict about real literals
    without punishing the comments that explain the design.
    """
    docstrings: set[int] = set()
    owners = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    for node in ast.walk(tree):
        if not isinstance(node, owners):
            continue
        body = getattr(node, "body", [])
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            docstrings.add(id(first.value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant)
            and isinstance(n.value, str)
            and id(n) not in docstrings]


def main() -> int:
    print("\n-- what a call runs on: param, then policy, then nothing --")
    cases = (
        ({"provider": "apinex", "model": "m-1"},
         {"default_provider": "x", "default_model": "y"},
         ("apinex", "m-1"), "the call's own param wins"),
        ({}, {"default_provider": "apinex", "default_model": "m-2"},
         ("apinex", "m-2"), "the policy answers when the call is silent"),
        ({}, {}, ("", ""),
         "an unconfigured install resolves to nothing, not a vendor"),
        ({"provider": "  ", "model": ""},
         {"default_provider": "apinex", "default_model": "m-2"},
         ("apinex", "m-2"),
         "a whitespace-only param falls through instead of blanking"),
        ({"provider": None}, {"default_provider": "apinex"},
         ("apinex", ""), "an explicit None param falls through"),
        ({"model": "m-9"}, {"default_provider": "apinex"},
         ("apinex", "m-9"), "provider and model resolve independently"),
        ({"provider": "apinex"}, {"default_provider": "x"},
         ("apinex", ""), "a param provider with no policy model"),
    )
    for params, policy, want, label in cases:
        got = resolve_run_target(params, policy)
        check((got.get("provider"), got.get("model")) == want,
              f"{label} -> {got}")

    check(set(resolve_run_target({}, {}).keys()) == {"provider", "model"},
          "the resolver returns exactly provider and model")

    print("\n-- why a call could not run, said to the person who can fix it --")
    empty = no_provider_message("")
    named = no_provider_message("apinex")
    check("Settings" in empty,
          "nothing configured points at Settings")
    check("apinex" in named,
          "a named provider is named back")
    check("Settings" in named,
          "and the named case also says where to fix it")
    for vendor in ("openai", "anthropic"):
        check(vendor not in empty.lower() and vendor not in named.lower(),
              f"the message invents no vendor ({vendor})")
    check(no_provider_message("") != no_provider_message("apinex"),
          "an unconfigured install and a bad name read differently")

    print("\n-- an unconfigured provider is None, never a stand-in --")
    check(open_provider("") is None,
          "the empty name resolves to no adapter")
    check(open_provider("never-configured-anywhere") is None,
          "an unknown name resolves to no adapter")
    # The anti-patch assertion: the helper must not reach for whatever
    # happens to be configured when the name it was handed is unknown. That
    # fallback is how "the user deleted openai" turned into a call silently
    # going to a different vendor than the one on screen.
    pc = _code_only(AGENT_DIR / "pconfig.py")
    body = pc.split("def open_provider(", 1)[-1].split("\ndef ", 1)[0]
    check("configured_provider_names" not in body
          and "or load_provider" not in body,
          "open_provider substitutes nothing when the name is unknown")

    print("\n-- the home: HOME, then the engine's own location, then the host --")
    saved_home = os.environ.pop("HOME", None)
    saved_engine_dir = home_mod._ENGINE_DIR
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "home").mkdir()
            fake_engine = root / "agent"
            fake_engine.mkdir()
            home_mod._ENGINE_DIR = fake_engine
            check(engine_home() == root / "home",
                  "with HOME unset, home is derived from the engine's location")
            check(engine_userland_dir() == root,
                  "and the userland root is that home's parent")
            check(engine_config_dir() == root / "home" / ".interlux" / "agent",
                  "and config lands under it, not somewhere else")

            os.environ["HOME"] = str(root / "elsewhere")
            check(engine_home() == root / "elsewhere",
                  "HOME wins over the derived answer when it is set")
            os.environ.pop("HOME", None)

            os.environ["INTERLUX_AGENT_CONFIG"] = str(root / "cfg")
            check(engine_config_dir() == root / "cfg",
                  "INTERLUX_AGENT_CONFIG still overrides the config dir")
            check(engine_home() == root / "home",
                  "and overriding config does not move the home")
            os.environ.pop("INTERLUX_AGENT_CONFIG", None)

        os.environ.pop("HOME", None)
        last = engine_home()
        derived = home_mod._ENGINE_DIR.parent / "home"
        check(last == (derived if derived.is_dir() else Path.home()),
              "with no HOME and no sibling home/, it falls back to Path.home()")
    finally:
        home_mod._ENGINE_DIR = saved_engine_dir
        os.environ.pop("INTERLUX_AGENT_CONFIG", None)
        if saved_home is not None:
            os.environ["HOME"] = saved_home

    check(str(engine_config_dir()).startswith(str(engine_home())),
          "config always lives under the home it was resolved with")

    print("\n-- no vendor named where the engine chooses --")
    serve_src = _code_only(SERVE)
    serve_strings = _string_literals(ast.parse(serve_src))
    named = sorted({s for s in serve_strings if s.strip().lower() in VENDOR_LITERALS})
    check(not named, f"serve.py names no vendor in code (found: {named})")
    for model in ("gpt-4o", "gpt-4", "claude-3-opus"):
        check(model not in serve_strings,
              f"serve.py carries no {model} default")

    for module in ("home.py", "policy.py", "pconfig.py"):
        path = AGENT_DIR / module
        if not path.exists():
            continue
        strings = _string_literals(ast.parse(_code_only(path)))
        hits = sorted({s for s in strings if s.strip().lower() in VENDOR_LITERALS})
        check(not hits, f"{module} names no vendor in code (found: {hits})")

    print("\n-- every call site asks the resolver --")
    check(serve_src.count("resolve_run_target(") >= 2,
          "both the turn path and the compact path call resolve_run_target")
    check('resolve_run_target(params' in serve_src,
          "the turn path resolves from its own params")
    check("no_provider_message(" in serve_src,
          "and a missing provider is reported with the shared message")
    check('params.get("provider",' not in serve_src,
          "no call site supplies a provider default of its own")
    check('"provider": "openai"' not in serve_src
          and "'provider': 'openai'" not in serve_src,
          "no call site pins a provider by name")
    check("open_provider(" in serve_src,
          "adapters are looked up through the one helper")
    check("load_provider(provider_name" not in serve_src
          and "load_provider(str(params" not in serve_src,
          "the two-step lookup is gone from every call site")

    print("\n-- one way to find the home --")
    check("engine_home()" in serve_src,
          "serve.py asks engine_home()")
    check('f"{engine_home()}"' in serve_src or "{engine_home()}" in serve_src,
          "and the tool preamble interpolates it rather than naming a path")

    readers: list[str] = []
    for path in _engine_sources():
        if path.name == "home.py":
            continue
        src = _code_only(path)
        for pattern in ('os.environ.get("HOME")', 'os.environ["HOME"]',
                        "Path.home()", 'expanduser("~")'):
            if pattern in src:
                readers.append(f"{path.name}: {pattern}")
    check(not readers,
          f"nothing outside home.py resolves the home itself ({readers})")

    print("\n-- one way to find the config dir --")
    literal_holders: list[str] = []
    for path in _engine_sources():
        if path.name == "home.py":
            continue
        strings = _string_literals(ast.parse(_code_only(path)))
        if "INTERLUX_AGENT_CONFIG" in strings:
            literal_holders.append(f"{path.name}: reads the override itself")
        for s in strings:
            if ".interlux" in s:
                literal_holders.append(f"{path.name}: {s!r}")
    check(not literal_holders,
          f"nothing outside home.py rebuilds the config path ({literal_holders})")

    print("\n-- no absolute installation path in the engine --")
    abs_holders: list[str] = []
    fragments = ("/data/user/0/", "com.keneristudios.interlux", "files/userland")
    for path in _engine_sources():
        src = _code_only(path)
        tree = ast.parse(src)
        strings = _string_literals(tree)
        for frag in fragments:
            if any(frag in s for s in strings):
                abs_holders.append(f"{path.name}: {frag!r} in code")
        # INTERLUX_HOME was the name of the constant that held the path. The
        # string check above cannot see a bare identifier, so look for it as
        # one -- a re-introduced constant would be read, not quoted.
        if any(isinstance(n, ast.Name) and n.id == "INTERLUX_HOME"
               for n in ast.walk(tree)):
            abs_holders.append(f"{path.name}: INTERLUX_HOME used as a name")
        if "INTERLUX_HOME" in _string_literals(tree):
            abs_holders.append(f"{path.name}: INTERLUX_HOME as a string")
    check(not abs_holders,
          f"the engine names no absolute installation path ({abs_holders})")

    print("\n-- a missing provider is an error, not the model talking --")
    check('f"No provider found: {model}"' not in serve_src,
          "the old text_delta that named the model is gone")
    check('{"type": "error", "message": no_provider_message("")}' in serve_src,
          "the no-provider path yields an error carrying the shared message")

    print("\n-- the helper is not the fix; the call sites are --")
    check("resolve_run_target" in (AGENT_DIR / "policy.py").read_text(
        encoding="utf-8"),
        "resolve_run_target is defined in policy.py")
    check(HOME_PY.exists() and "def engine_home" in HOME_PY.read_text(
        encoding="utf-8"),
        "engine_home is defined in home.py")

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for label in FAILED:
        print("  FAILED: " + label)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
