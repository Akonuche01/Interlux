"""Wire formats -- discovered, never listed.

There is no list of providers in this file, and there must never be one.
A provider is whatever the user configured: a name, a base URL, a key.
The engine serves it. A hardcoded list is a list of vendors the user may
have no key for -- shown in Settings, unusable -- and a vendor the list
does not name cannot exist at all, no matter what the user configures.

That is not a theory. The list this replaces named five vendors; the
device it runs on is configured for six, and five of them were not on the
list. They worked only because `load_provider()` quietly served a generic
adapter for any unknown name, which is also how a one-word typo silently
cost the thoughts half of the client's activity card.

There is no list of *formats* here either. A module in this package that
declares `DIALECT` is a wire format, and the engine asks each module to
describe itself:

    DIALECT         the name a config's `dialect` key uses
    ADAPTER         the class that speaks it
    CANONICAL_BASE  where it lives when a config names no base_url

`discover()` walks the package and collects them. Nothing is enumerated
centrally, so dropping a module in here that speaks a new format makes the
engine speak it, with no other edit anywhere. That matters because the
engine's reach must not be bounded by a list somebody forgot to update --
which is exactly the failure this whole file exists to undo.

Vendor-specific request fields do not belong in a format either. They go in
the provider's own `options` block in the user's config, straight into the
request body. That is how a field like Mercury's `reasoning_summary` is
expressed without the engine having to know that Mercury exists.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from pathlib import Path

from .base import BaseProvider


class UnknownDialect(ValueError):
    """The config named a wire format the engine cannot speak."""


@dataclass(frozen=True)
class DialectSpec:
    """One wire format, as it describes itself. Data only."""

    name: str
    adapter: type[BaseProvider]
    canonical_base: str = ""
    module: str = ""


# The format assumed when a config names none. Not a vendor: it is the one
# nearly every gateway, relay, local runner and vendor clone speaks.
DEFAULT_DIALECT = "openai"


def discover(package: str | None = None) -> dict[str, DialectSpec]:
    """Every wire format declared in `package`, found by asking.

    `package` is a parameter so a test can point the scan at a throwaway
    package and prove that a format added later is picked up without the
    engine being edited -- the only honest way to test that nothing is
    hardcoded.
    """
    package = package or __package__
    root = Path(importlib.import_module(package).__file__ or "").parent
    found: dict[str, DialectSpec] = {}
    for info in sorted(pkgutil.iter_modules([str(root)]),
                       key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{package}.{info.name}")
        dialect = getattr(module, "DIALECT", None)
        if not isinstance(dialect, str) or not dialect:
            continue                      # a helper, not a format
        adapter = getattr(module, "ADAPTER", None)
        if not (isinstance(adapter, type) and issubclass(adapter, BaseProvider)):
            raise ValueError(
                f"providers/{info.name}.py declares DIALECT '{dialect}' but no "
                f"ADAPTER; a format must say which class speaks it"
            )
        spec = DialectSpec(
            name=dialect,
            adapter=adapter,
            canonical_base=str(getattr(module, "CANONICAL_BASE", "") or ""),
            module=info.name,
        )
        found[dialect] = spec
        # Backward-compat aliases (e.g. "local" -> "on-device"). This is data,
        # not a vendor list: an alias only points at a format that already
        # declared itself, so nothing is enumerated centrally.
        for alias in getattr(module, "DIALECT_ALIASES", []) or []:
            if isinstance(alias, str) and alias and alias not in found:
                found[alias] = DialectSpec(
                    name=alias,
                    adapter=adapter,
                    canonical_base=spec.canonical_base,
                    module=info.name,
                )
    return found


DIALECTS: dict[str, DialectSpec] = discover()

# Convenience view: format -> where it lives. Derived, never written out.
CANONICAL_BASE: dict[str, str] = {
    name: spec.canonical_base
    for name, spec in DIALECTS.items() if spec.canonical_base
}


def dialect_of(section: dict) -> str:
    """The wire format a config section asks for. Defaults to the common one."""
    if not isinstance(section, dict):
        return DEFAULT_DIALECT
    return str(section.get("dialect") or DEFAULT_DIALECT).strip().lower()


def base_for(section: dict) -> str:
    """The base URL a section resolves to: its own, else its format's home."""
    base = (section or {}).get("base_url")
    if isinstance(base, str) and base.strip():
        return base.strip()
    return CANONICAL_BASE.get(dialect_of(section or {}), "")


def build_provider(name: str, section: dict) -> BaseProvider | None:
    """Build the adapter for one configured provider.

    `section` is the provider's resolved config: at minimum a base_url and
    an api_key, optionally a `dialect` and an `options` block. Anything the
    user did not configure is left to the format's defaults.
    """
    if not isinstance(section, dict):
        section = {}
    dialect = dialect_of(section)
    # A provider section named "local" is the on-device backend, even with no
    # explicit dialect (legacy config). The Kotlin LlamaServer still refers to
    # it as the "local" provider, so keep that name resolving after the dialect
    # was renamed to "on-device".
    if name == "local" and dialect in (DEFAULT_DIALECT, ""):
        dialect = "on-device"
    spec = DIALECTS.get(dialect)
    if spec is None:
        raise UnknownDialect(
            f"provider '{name}' asks for dialect '{dialect}'; "
            f"known dialects: {', '.join(sorted(DIALECTS))}"
        )
    options = section.get("options")
    contract = dict(options) if isinstance(options, dict) else {}
    return spec.adapter(
        str(section.get("api_key") or ""),
        base_for(section) or None,
        contract=contract,
    )
