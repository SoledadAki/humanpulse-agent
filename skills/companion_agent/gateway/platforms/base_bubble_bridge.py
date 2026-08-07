"""Bridge from the Hermes gateway to the companion-agent bubble sender.

The companion-agent skill lives in the user's skills directory (not on
``sys.path``), so ``base.py`` cannot import ``send_bubbles`` directly.
This module loads the skill's ``send_bubbles.py`` (and its ``runtime``
dependency) from disk and re-exports ``send_human_reply``.  If the skill is
not installed, ``send_human_reply`` stays ``None`` and the gateway falls
back to its original single-send behavior.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    from hermes_constants import get_hermes_home
except Exception:  # pragma: no cover - defensive
    def get_hermes_home() -> str:  # type: ignore[misc]
        return os.path.expanduser("~/.hermes")


def _find_skill_dir() -> Path | None:
    """Locate the companion-agent skill directory across common layouts."""
    home = Path(get_hermes_home())
    candidates = [
        home / "skills" / "companion-agent",
        home / "skills" / "companion_agent",
        home / "skills" / "humanpulse-agent",
        Path(os.path.expanduser("~/.hermes/skills/companion-agent")),
        Path(os.path.expanduser("~/.hermes/skills/companion_agent")),
        Path(os.path.expanduser("~/.hermes/skills/humanpulse-agent")),
    ]
    # Profile layouts: ~/.hermes/profiles/<name>/skills/...
    profiles = home / "profiles"
    try:
        if profiles.is_dir():
            for profile in profiles.iterdir():
                candidates.append(profile / "skills" / "companion-agent")
                candidates.append(profile / "skills" / "companion_agent")
                candidates.append(profile / "skills" / "humanpulse-agent")
    except OSError:
        pass
    for candidate in candidates:
        if candidate.is_dir() and (candidate / "runtime.py").is_file():
            return candidate
    return None


def _load_module_from_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_send_human_reply():
    skill_dir = _find_skill_dir()
    if skill_dir is None:
        logger.debug("companion-agent skill not found; bubble delivery disabled")
        return None
    try:
        # send_bubbles.py imports `from runtime import split_reply_bubbles`
        # when the `skills.companion_agent.runtime` package import fails, so
        # register `runtime` under the module name it expects.
        runtime = _load_module_from_path(
            "companion_runtime",
            skill_dir / "runtime.py",
        )
        sys.modules["runtime"] = runtime
        bubbles = _load_module_from_path(
            "companion_send_bubbles",
            skill_dir / "adapters" / "hermes" / "send_bubbles.py",
        )
        return getattr(bubbles, "send_human_reply", None)
    except Exception as exc:
        logger.warning("Failed to load companion bubble sender: %s", exc)
        return None


send_human_reply = _load_send_human_reply()
