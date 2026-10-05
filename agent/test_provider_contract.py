"""The provider conformance gate.

Run this before shipping any change to `providers/`, `pconfig.py` or
`serve.py`.

    python3 -m agent.test_provider_contract      (from the repo root)

The rule it exists to enforce, in one line:

    A provider is configuration. A wire format is code. The engine may
    know formats. It may not know vendors.

What it refuses to let happen:

  1. A provider table coming back. There must be no list of vendors in the
     source -- not because lists are ugly, but because such a list offers
     the user vendors they hold no key for, and hides any vendor it does
     not name. The list this gate replaced named five; the device it ran
     on was configured for six, and five of them were not on it.
  2. A new provider needing code. A section the engine has never seen must
     produce a working provider on its own, with the full vocabulary.
  3. A dialect dropping part of the vocabulary. Thinking on the wire must
     become `reasoning_delta`; text must become `text_delta`; usage must
     become `usage`; the stream must end with `complete`.
  4. A provider behaving differently because of the host it is pointed at.
  5. A vendor's request fields having nowhere to go but code. `options` in
     the user's own config section must reach the request body.
  6. A name that was never configured being served anyway.
  7. A domain baked into the engine. The only hosts allowed anywhere in
     the provider layer are the canonical homes of the formats.
"""

from __future__ import annotations

import asyncio
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import providers as providers_pkg  # noqa: E402
from agent.pconfig import (configured, configured_provider_names,  # noqa: E402
                           load_provider)
from agent.providers import (CANONICAL_BASE, DEFAULT_DIALECT,  # noqa: E402
                             DIALECTS, base_for, build_provider, dialect_of,
                             discover)
from agent.providers import anthropic as anthropic_mod  # noqa: E402
from agent.providers import openai as openai_mod  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []

AGENT_DIR = Path(__file__).resolve().parent


def check(ok: bool, label: str) -> None:
    (PASSED if ok else FAILED).append(label)
    print(("  ok   " if ok else "  FAIL ") + label)


# A stream carrying every shape any vendor speaks. Each dialect reads the
# shape its own format uses and ignores the rest. Deliberately not tailored
# per provider: the point is that thinking on the wire becomes
# reasoning_delta, whichever dialect it arrived in.
VENDOR_STREAM = [
    {"choices": [{"delta": {"reasoning_content": "a deepseek fragment "}}]},
    {"choices": [{"delta": {"content": "Hello "}}]},
    {"reasoning_summary": {"content": "a mercury summary"},
     "choices": [{"delta": {}}]},
    {"reasoning_summary": {"content": "a mercury summary, longer"},
     "choices": [{"delta": {}}]},
    {"type": "content_block_delta",
     "delta": {"type": "thinking_delta", "thinking": "a claude thought"}},
    {"choices": [{"delta": {"content": "world"}}]},
    {"type": "content_block_delta", "delta": {"text": " there"}},
    {"usage": {"prompt_tokens": 3, "completion_tokens": 5,
               "total_tokens": 8}},
]


def stub_transport(captured: list[dict]):
    """Stand in for `post_sse`: capture the request, replay a stream."""
    async def stub(url, headers, payload, chunks_from, reasoning_from,
                   timeout=120, usage_from=None):
        captured.append({"url": url, "payload": payload})
        for obj in VENDOR_STREAM:
            if usage_from is not None:
                fragment = usage_from(obj)
                if fragment:
                    yield {"type": "usage", "usage": fragment}
            for chunk in chunks_from(obj):
                yield {"type": "text_delta", "content": chunk}
            for thought in reasoning_from(obj):
                yield {"type": "reasoning_delta", "content": thought}
    return stub


async def drive(section: dict, name: str = "probe") -> tuple[list[dict], dict]:
    captured: list[dict] = []
    original = (openai_mod.post_sse, anthropic_mod.post_sse)
    openai_mod.post_sse = stub_transport(captured)
    anthropic_mod.post_sse = stub_transport(captured)
    try:
        provider = build_provider(name, section)
        events = []
        async for event in provider.stream_turn(
            [{"role": "user", "content": "hello"}], model="test-model"
        ):
            events.append(event)
    finally:
        openai_mod.post_sse, anthropic_mod.post_sse = original
    return events, captured[0]


def kinds(events: list[dict]) -> list[str]:
    return [e.get("type", "") for e in events]


def vocabulary(events: list[dict], label: str) -> None:
    seen = kinds(events)
    check("text_delta" in seen, f"{label}: emits text_delta")
    check("reasoning_delta" in seen, f"{label}: emits reasoning_delta")
    check("usage" in seen, f"{label}: emits usage")
    check(seen[-1:] == ["complete"], f"{label}: ends with complete")
    check(
        any(e["content"].strip() for e in events
            if e.get("type") == "reasoning_delta"),
        f"{label}: thinking is not empty",
    )


async def main() -> int:
    print("there is no provider table")
    check(not hasattr(providers_pkg, "PROVIDERS"),
          "the engine exposes no list of providers")
    check(not (AGENT_DIR / "providers" / "registry.py").exists(),
          "no registry module")
    vendor_modules = [p.name for p in (AGENT_DIR / "providers").glob("*.py")
                      if p.name not in {
                          "__init__.py", "base.py", "dialects.py",
                          "openai.py", "anthropic.py", "on_device.py",
                          "media.py", "responses.py", "streaming.py"}]
    check(not vendor_modules,
          "no module is named after a vendor" +
          ("" if not vendor_modules else " -- " + ", ".join(vendor_modules)))

    # Names that belonged to the retired table. A leftover reference is not
    # cosmetic: these sat inside function bodies, so the module imported
    # cleanly and only failed when the path was used -- the worst possible
    # shape for a bug to have.
    retired = ("PROVIDERS", "CURATED_MODELS", "DEFAULT_BASES",
               "LIVE_MODELS_OK", "ProviderSpec", "STRICT_PROVIDER_NAMES",
               "InceptionLabsProvider", "TokenHarborProvider")
    stale = []
    for path in sorted(AGENT_DIR.rglob("*.py")):
        if "__pycache__" in path.parts or ".bak" in path.name:
            continue
        if path.name == Path(__file__).name:
            # This file names them on purpose, as the thing to look for.
            continue
        text = path.read_text(encoding="utf-8")
        for word in retired:
            for number, line in enumerate(text.splitlines(), 1):
                if re.search(rf"\b{word}\b", line):
                    stale.append(f"{path.name}:{number}:{word}")
    check(not stale,
          "no reference to the retired provider table survives"
          + ("" if not stale else " -- " + ", ".join(stale[:8])))

    print("\nevery dialect delivers the whole vocabulary")
    for dialect in sorted(DIALECTS):
        events, request = await drive(
            {"dialect": dialect, "base_url": "https://example.invalid/v1",
             "api_key": "k"})
        vocabulary(events, dialect)
        check(request["payload"].get("model") == "test-model",
              f"{dialect}: the requested model reaches the request")

    print("\na provider the engine has never seen just works")
    brand_new = {
        "base_url": "https://brand-new-gateway.example/v1",
        "api_key": "k",
        # No dialect named: the OpenAI chat-completions format is assumed,
        # which is what nearly every gateway, relay and clone speaks.
    }
    events, request = await drive(brand_new, "brand-new-gateway")
    vocabulary(events, "brand-new-gateway")
    check(dialect_of(brand_new) == DEFAULT_DIALECT,
          "an unnamed dialect defaults to the openai format")
    check(request["payload"]["model"] == "test-model",
          "brand-new-gateway: request is well formed")
    check("messages" in request["payload"],
          "brand-new-gateway: request carries the conversation")

    print("\na vendor's own fields come from config, not code")
    with_options, request = await drive(
        {"base_url": "https://example.invalid/v1", "api_key": "k",
         "options": {"reasoning_summary": True, "top_k": 40}},
        "vendor-with-options")
    check(request["payload"].get("reasoning_summary") is True,
          "options: reasoning_summary reaches the request")
    check(request["payload"].get("top_k") == 40,
          "options: an arbitrary field reaches the request")
    without, plain = await drive(
        {"base_url": "https://example.invalid/v1", "api_key": "k"},
        "vendor-without-options")
    check("reasoning_summary" not in plain["payload"],
          "no options: nothing extra is sent")

    print("\nthe host must not decide anything")
    for dialect in sorted(DIALECTS):
        section = {"dialect": dialect, "api_key": "k"}
        _a, first = await drive({**section,
                                 "base_url": "https://one.example/v1"})
        _b, second = await drive({**section,
                                  "base_url": "https://two.example/v9"})
        check(first["payload"] == second["payload"],
              f"{dialect}: identical request on a different host")

    print("\nthe format's canonical home fills an empty base_url")
    for dialect, home in CANONICAL_BASE.items():
        check(base_for({"dialect": dialect}) == home,
              f"{dialect}: falls back to {home}")
    check(base_for({"base_url": "https://mine.example/v1"})
          == "https://mine.example/v1",
          "a configured base_url always wins")

    print("\nwhat was never configured is not served")
    check(load_provider("never-configured", {}) is None,
          "an unconfigured name yields no provider")
    check(load_provider("never-configured", {"dialect": "openai"}) is not None,
          "the same name with a section does yield one")
    check(configured({"api_key": ""}) is False,
          "an empty section is not configured")
    check(configured({"api_key": "k"}) is True,
          "a key alone is a configured provider")
    check(load_provider("bad-dialect",
                        {"dialect": "martian",
                         "base_url": "https://x.example/v1"}) is None,
          "an unknown dialect is refused, not silently served")

    config = {
        "my-gateway": {"base_url": "https://mine.example/v1"},
        "agents": {"ag_1": {"my-gateway": {"api_key": "k"},
                            "their-gateway": {"api_key": "k"}}},
    }
    listed = configured_provider_names("ag_1", config)
    check(listed == ["my-gateway", "their-gateway"],
          "only configured names are listed" + f" -- got {listed}")
    check(not {"anthropic", "tokenharbor", "openai"} & set(listed),
          "a vendor nobody configured is never offered")

    print("\na format added later is picked up with no engine change")
    with tempfile.TemporaryDirectory() as tmp:
        package = Path(tmp) / "probe_formats"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
        (package / "oddball.py").write_text(
            "from agent.providers.base import BaseProvider\n"
            "from agent.providers.openai import OpenAIProvider\n"
            "DIALECT = 'oddball'\n"
            "CANONICAL_BASE = 'https://oddball.example/v1'\n"
            "class OddballProvider(OpenAIProvider):\n"
            "    pass\n"
            "ADAPTER = OddballProvider\n",
            encoding="utf-8")
        sys.path.insert(0, tmp)
        try:
            found = discover("probe_formats")
        finally:
            sys.path.remove(tmp)
            for name in [m for m in sys.modules if m.startswith("probe_formats")]:
                del sys.modules[name]
    check(set(found) == {"oddball"},
          "a module declaring DIALECT becomes a format"
          + f" -- got {sorted(found)}")
    check(found.get("oddball", None) is not None
          and found["oddball"].adapter.__name__ == "OddballProvider",
          "the new format brings its own adapter")
    check(found.get("oddball", None) is not None
          and found["oddball"].canonical_base == "https://oddball.example/v1",
          "the new format brings its own canonical base")
    check("helper" not in found,
          "a module that declares nothing stays a helper")

    print("\nthe engine knows no vendors")
    domains = set()
    scan = [p for p in (AGENT_DIR / "providers").glob("*.py")
            if ".bak" not in p.name]
    scan += [AGENT_DIR / "pconfig.py", AGENT_DIR / "serve.py"]
    for path in scan:
        found = re.findall(r"https?://[A-Za-z0-9._-]+",
                           path.read_text(encoding="utf-8"))
        domains |= {d for d in found if "example" not in d}
    allowed = {re.match(r"https?://[A-Za-z0-9._-]+", u).group(0)
               for u in CANONICAL_BASE.values()}
    check(domains <= allowed,
          "the only hosts in the engine are the formats' canonical homes"
          + ("" if domains <= allowed
             else " -- unexpected: " + ", ".join(sorted(domains - allowed))))

    offenders = []
    domain = re.compile(r"https?://|\.(?:com|ai|io|net|org)(?:/|$|\b)")
    for path in scan:
        for number, line in enumerate(path.read_text(encoding="utf-8")
                                      .splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("if ", "elif ")) and domain.search(stripped):
                offenders.append(f"{path.name}:{number}: {stripped}")
    check(not offenders,
          "no conditional compares a host"
          + ("" if not offenders else " -- " + "; ".join(offenders)))

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for label in FAILED:
        print("  FAILED: " + label)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
