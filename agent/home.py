"""Where the engine's home and config live -- resolved once, for everyone.

This module exists because nine different places used to work it out for
themselves, and they did not all agree:

  * five sites used ``os.environ["HOME"] or Path.home()``,
  * three used ``os.environ["HOME"] or INTERLUX_HOME``, with the Interlux path
    written out in full -- the same absolute string, three separate times,
  * the thread store expanded ``~`` a ninth way, through its own
    ``INTERLUX_AGENT_CONFIG`` override,
  * and the config directory itself was rebuilt from scratch in three modules
    that only agreed because the string happened to match.

Two of those disagree the moment ``HOME`` is unset -- which is what a
subprocess with a scrubbed environment looks like, and what a host-side test
looks like -- and a home that differs between the thread store and the sandbox
is a thread the engine cannot find again. The Interlux path also has to be
edited in three places the day the application id changes, and one of the three
is always missed.

So: one answer, in one place, and every caller asks for it.

Resolution order, and nothing else:

  1. ``$HOME``            -- set by the launcher; on device this is the answer.
  2. ``<userland>/home``  -- **derived** from this file's own location.
  3. ``Path.home()``      -- last resort, for the host and for tests.

Step 2 is derived rather than written down on purpose. The engine always lives
at ``<userland>/agent/``, so its home is always ``<userland>/home``; naming
that absolute path in the source is how it becomes wrong when the app is
renamed, and how the three copies drift apart.
"""

from __future__ import annotations

import os
from pathlib import Path

# The engine's own directory -- ``<userland>/agent`` on device, the repo's
# ``agent/`` on the host. Its parent is the userland root.
_ENGINE_DIR = Path(__file__).resolve().parent

# Where config lives under the home, in one place because three modules used to
# spell it out separately.
_CONFIG_SUBDIR = (".interlux", "agent")


def engine_home() -> Path:
    """The user's home directory -- the root of the filesystem the agent sees.

    Not cached. ``HOME`` is read per call so a test that sets it, or a caller
    that scrubs it, gets the truth rather than whatever was true at import.
    """
    env = os.environ.get("HOME")
    if env:
        return Path(env).expanduser()
    derived = _ENGINE_DIR.parent / "home"
    if derived.is_dir():
        return derived
    return Path.home()


def engine_config_dir() -> Path:
    """Where the engine keeps its own state: threads, policy, providers, audit.

    ``INTERLUX_AGENT_CONFIG`` still wins when set -- it is the override the
    thread store has always honoured, and moving the config somewhere else for
    a test or a second instance has to keep working.
    """
    override = os.environ.get("INTERLUX_AGENT_CONFIG")
    if override:
        return Path(override).expanduser()
    return engine_home().joinpath(*_CONFIG_SUBDIR)


def engine_userland_dir() -> Path:
    """The userland root -- the home's parent, where the engine and its
    binaries live. Derived from this file, so it cannot disagree with
    :func:`engine_home` about which installation this is."""
    return engine_home().parent
