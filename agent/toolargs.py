"""Reconcile model-emitted tool arguments against the tool's real signature.

The model does not call tools; it *writes text* that we parse into a name
and a parameter object. Nothing between that text and
``TOOLS[name](**params)`` ever checked the parameters against the function
they are about to be handed to. Two bugs came out of that gap, and both
looked like something else entirely from the outside:

  * A value of the *right name but wrong type*. Hermes-style XML carries
    every ``<arg_value>`` as a string, so ``timeout`` arrived as ``"10"``.
    ``run_shell`` accepted it, ``asyncio.wait_for`` raised ``TypeError``
    deep inside, and ``shell.py``'s broad ``except`` turned that into the
    one-liner ``unsupported operand type(s) for +: 'float' and 'str'``.
    The tool reported "error" with a message no reader -- human or model
    -- could act on, and the model, asked to summarise, invented output.

  * A parameter named the way a *different* model family names it.
    ``fs_edit(path, old, new)`` was called with ``oldString``/``newString``,
    which raises ``TypeError`` at the call site: the tool body never runs.

This module is the one place that closes that gap. It is deliberately
conservative -- it will rename and retype, but it will not guess:

  * rename known aliases (``oldString`` -> ``old``) when the target is a
    real parameter of *this* function and the source is not;
  * coerce scalars to the annotated type (``"10"`` -> ``10``);
  * parse a JSON string into a list/dict when the annotation wants one;
  * drop unknown keys only if the call is still complete without them;
  * otherwise refuse with a message that names the accepted parameters,
    so the model gets a correction it can actually use next round.

Refusing loudly beats guessing silently: a wrong guess writes the wrong
bytes to the wrong file.
"""

from __future__ import annotations

import inspect
import json
import logging
import types
import typing

logger = logging.getLogger("toolargs")


class ArgError(Exception):
    """The call cannot be reconciled to the tool's signature.

    Carries the pieces separately so the caller can log the short detail
    and still hand the model a message that names what the tool accepts.
    """

    def __init__(self, tool: str, detail: str, accepted: str) -> None:
        super().__init__(f"{tool}: {detail}. {tool} accepts: {accepted}")
        self.tool = tool
        self.detail = detail
        self.accepted = accepted


# Model families disagree on parameter spelling. Keys are compared after
# lower-casing and stripping '-' and '_', so oldString / old_string /
# old-string all land on the same entry.
_ALIASES: dict[str, str] = {
    # surgical edit
    "oldstring": "old",
    "oldstringcontent": "old",
    "oldtext": "old",
    "searchstring": "old",
    "find": "old",
    "newstring": "new",
    "newtext": "new",
    "replacestring": "new",
    "replace": "new",
    "replacement": "new",
    # file paths
    "filepath": "path",
    "filename": "path",
    "file": "path",
    "target": "path",
    # shell
    "cmd": "command",
    "commandline": "command",
    "script": "command",
    "workingdirectory": "cwd",
    "workdir": "cwd",
    "workingdir": "cwd",
    "directory": "cwd",
    "dir": "cwd",
    # search
    "maxresults": "max_hits",
    "limit": "max_hits",
    "query": "pattern",
    "regex": "pattern",
    "searchterm": "pattern",
    # file body
    "text": "content",
    "contents": "content",
    "body": "content",
    "data": "content",
}

_SCALARS = (str, int, float, bool)
_CONTAINERS = (list, set, tuple, frozenset)


def _accepted(sig: inspect.Signature) -> str:
    """Parameter names a caller may pass by keyword, for error messages."""
    names = [
        p.name
        for p in sig.parameters.values()
        if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
    ]
    return ", ".join(names) if names else "(none)"


def _unwrap(ann: object) -> object | None:
    """Peel ``X | None`` down to ``X``; None when nothing safe to do.

    Returns the bare annotation for a single-type Optional, and None for
    a genuine multi-type union (``int | str``) -- there is no correct
    coercion for an ambiguous target, so we leave the value alone.
    """
    if ann is inspect.Parameter.empty or ann is None:
        return None
    origin = typing.get_origin(ann)
    if origin is typing.Union or origin is types.UnionType:
        args = [a for a in typing.get_args(ann) if a is not type(None)]
        return args[0] if len(args) == 1 else None
    return ann


def _coerce(name: str, value: object, ann: object) -> tuple[object, str]:
    """Return (value, note). Raise ValueError with a readable reason.

    A note is a non-empty string when the value was changed, so the
    caller can log exactly which arguments the model got wrong -- that
    pattern is the signal for whether the prompt needs rewording.
    """
    target = _unwrap(ann)
    if target is None or value is None:
        return value, ""

    # bool is checked before int: bool is a subclass of int.
    if target is bool:
        if isinstance(value, bool):
            return value, ""
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("true", "1", "yes", "y", "on"):
                return True, f"{name}: {value!r} -> true"
            if low in ("false", "0", "no", "n", "off"):
                return False, f"{name}: {value!r} -> false"
            raise ValueError(f"{name} expects true/false, got {value!r}")
        if isinstance(value, (int, float)):
            return bool(value), f"{name}: {value} -> {bool(value)}"
        raise ValueError(f"{name} expects true/false, got {value!r}")

    if target is int:
        if isinstance(value, bool):
            raise ValueError(f"{name} expects a whole number, got a boolean")
        if isinstance(value, int):
            return value, ""
        if isinstance(value, float):
            if value.is_integer():
                return int(value), f"{name}: {value} -> {int(value)}"
            raise ValueError(f"{name} expects a whole number, got {value!r}")
        if isinstance(value, str):
            raw = value.strip()
            try:
                return int(raw), f"{name}: {value!r} -> {int(raw)}"
            except ValueError:
                pass
            try:
                as_float = float(raw)
            except ValueError:
                raise ValueError(
                    f"{name} expects a whole number, got {value!r}") from None
            if as_float.is_integer():
                return int(as_float), f"{name}: {value!r} -> {int(as_float)}"
            raise ValueError(f"{name} expects a whole number, got {value!r}")
        raise ValueError(f"{name} expects a whole number, got {value!r}")

    if target is float:
        if isinstance(value, bool):
            raise ValueError(f"{name} expects a number, got a boolean")
        if isinstance(value, float):
            return value, ""
        if isinstance(value, int):
            return float(value), f"{name}: {value} -> {float(value)}"
        if isinstance(value, str):
            raw = value.strip()
            try:
                return float(raw), f"{name}: {value!r} -> {float(raw)}"
            except ValueError:
                raise ValueError(
                    f"{name} expects a number, got {value!r}") from None
        raise ValueError(f"{name} expects a number, got {value!r}")

    if target is str:
        if isinstance(value, str):
            return value, ""
        if isinstance(value, (int, float, bool)):
            return str(value), f"{name}: {value!r} -> {str(value)!r}"
        # A container handed to a str parameter would stringify to a repr;
        # leave it so the tool's own validation can reject it clearly.
        return value, ""

    origin = typing.get_origin(target)
    if origin is dict and isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value, ""
        if isinstance(parsed, dict):
            return parsed, f"{name}: parsed JSON string -> object"
        return value, ""
    if origin in _CONTAINERS and isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value, ""
        if isinstance(parsed, list):
            try:
                conv = origin(parsed)
            except Exception:
                return value, ""
            return conv, f"{name}: parsed JSON string -> {origin.__name__}"
        return value, ""

    return value, ""


def reconcile(tool: str, func: object, params: object) -> tuple[dict, list[str]]:
    """Reconcile ``params`` to ``func``'s signature.

    Returns ``(call_params, notes)`` where notes are the adjustments made
    (rename / retype), for logging. Raises :class:`ArgError` when the call
    cannot be made to fit without guessing.

    A callable whose signature cannot be read (C extension, exotic
    decorator) is passed through untouched rather than refused -- this
    layer must never be the reason a working tool stops working.
    """
    if not isinstance(params, dict):
        raise ArgError(
            tool,
            f"parameters must be an object, got {type(params).__name__}",
            "(an object of named arguments)")

    try:
        sig = inspect.signature(func)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return dict(params), []

    by_name = {
        p.name: p
        for p in sig.parameters.values()
        if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
    }
    takes_kwargs = any(
        p.kind is p.VAR_KEYWORD for p in sig.parameters.values())

    notes: list[str] = []
    out: dict = {}
    unknown: list[str] = []

    for key, value in params.items():
        name = key
        if name not in by_name and isinstance(key, str):
            alias = _ALIASES.get(key.strip().lower().replace("-", "").replace("_", ""))
            if alias and alias in by_name:
                notes.append(f"{key} -> {alias}")
                name = alias
        if name not in by_name:
            if takes_kwargs:
                out[name] = value
            else:
                unknown.append(key)
            continue
        try:
            value, note = _coerce(name, value, by_name[name].annotation)
        except ValueError as exc:
            raise ArgError(tool, str(exc), _accepted(sig)) from None
        if note:
            notes.append(note)
        out[name] = value

    missing = [
        p.name
        for p in by_name.values()
        if p.default is inspect.Parameter.empty and p.name not in out
    ]
    if missing:
        detail = f"missing required parameter(s): {', '.join(missing)}"
        if unknown:
            detail += f"; did not accept: {', '.join(unknown)}"
        raise ArgError(tool, detail, _accepted(sig))

    if unknown:
        # Only optional parameters can be dropped without changing the
        # call's completeness -- but a silent drop is still a lie, so it
        # is recorded and logged.
        notes.append(f"ignored unknown parameter(s): {', '.join(unknown)}")

    return out, notes
