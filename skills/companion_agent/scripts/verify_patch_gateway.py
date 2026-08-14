#!/usr/bin/env python3
"""Round-trip verification for companion-agent's patch_gateway.py templates.

After any `hermes update` (pip reinstall) the gateway-side site-packages
edits are wiped; `adapters/hermes/patch_gateway.py` re-applies them. This
script proves the patcher's own templates are still correct, so a future
re-patch cannot silently corrupt the target.

Checks (in order):
  1. Load patch_gateway.py as a module (catches syntax errors in the patcher).
  2. Template self-consistency: every `*_NEW` value carries its patch marker,
     and the OLD anchors it replaces are distinct from the NEW text.
  3. Full round-trip in a temp site-packages dir against synthetic pristine
     base.py / run.py built from the OLD anchors (indentation mirrors the
     live context the templates target), then apply patch_gateway.py and
     assert:
       - base.py gets `_send_with_bubbles` + the call-site swap
       - run.py gets the hidden-context injection
       - the result parses (ast.parse)
       - a second run is idempotent (all skips)
  4. Scheduler handling: with Hermes >= 0.18.2 native markers present the
     patcher must SKIP (no fail) — assert the skip branch fires.
  5. verify_humanpulse.py still passes (the wiring-level suite).

Run from anywhere with the Hermes venv python:
    python3 scripts/verify_patch_gateway.py
"""

from __future__ import annotations

import importlib.util
import pathlib
import shutil
import subprocess
import sys
import tempfile

SKILL_DIR = pathlib.Path(__file__).resolve().parent.parent
PATCHER = SKILL_DIR / "adapters" / "hermes" / "patch_gateway.py"

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}" + (f" — {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))


def _synthetic_base(pg) -> str:
    """Pristine base.py containing the two OLD anchors the patcher needs.

    BASE_METHOD_ANCHOR sits at method level (4-space body) exactly as the
    patcher expects; BASE_SEND_ANCHOR_OLD appears inside nested try blocks so
    its 20-space indentation matches the live call site.
    """
    return (
        "from typing import Any, Optional\n\n\n"
        "class FakeAdapter:\n"
        "    async def _send_with_retry(self, **kw):\n"
        "        return None\n\n"
        + pg.BASE_METHOD_ANCHOR
        + "        return new_text\n\n"
        "    async def _process_message_background(self, event, session_key, text_content, _reply_anchor, _final_thread_metadata, interrupt_event):\n"
        "        try:\n"
        "            try:\n"
        "                try:\n"
        + pg.BASE_SEND_ANCHOR_OLD.replace("                    result", "                    result")  # keep 20-space anchor intact
        + "                except Exception:\n"
        "                    pass\n"
        "            except Exception:\n"
        "                pass\n"
        "        except Exception:\n"
        "            pass\n"
    )


def _synthetic_run(pg) -> str:
    """Pristine run.py whose RUN_ANCHOR_OLD appears at 12-space block level.

    Mirrors the live context: ``message = (`` at 16 spaces inside a 12-space
    ``if`` block, ``f\"history.]\"`` at 20, closing paren at 16. The patcher
    inserts its 12-space ``try`` right after the closing paren.
    """
    return (
        "async def run_sync(message, history):\n"
        "    if True:\n"
        "        if True:\n"
        "            if True:\n"
        "                message = (\n"
        "                    f\"history.]\"\n"
        "                )\n"
        "            return message\n"
    )


def main() -> int:
    print("patch_gateway.py round-trip verification")
    print("=" * 50)

    # -- 1. patcher imports ------------------------------------------------
    try:
        spec = importlib.util.spec_from_file_location("pg_verify", PATCHER)
        pg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pg)
        check("patch_gateway.py imports", True)
    except Exception as exc:
        check("patch_gateway.py imports", False, str(exc))
        return 1

    # -- 2. template self-consistency -------------------------------------
    check(
        "RUN_ANCHOR_NEW carries patch marker",
        pg.RUN_PATCH_MARKER in pg.RUN_ANCHOR_NEW,
        pg.RUN_PATCH_MARKER,
    )
    check(
        "BASE_BUBBLE_METHOD carries patch marker",
        pg.BASE_PATCH_MARKER in pg.BASE_BUBBLE_METHOD,
        pg.BASE_PATCH_MARKER,
    )
    check(
        "RUN_ANCHOR_OLD differs from RUN_ANCHOR_NEW",
        pg.RUN_ANCHOR_OLD != pg.RUN_ANCHOR_NEW,
    )
    check(
        "BASE_SEND_ANCHOR_OLD differs from BASE_SEND_ANCHOR_NEW",
        pg.BASE_SEND_ANCHOR_OLD != pg.BASE_SEND_ANCHOR_NEW,
    )
    check(
        "SCHEDULER_NATIVE_MARKERS defined",
        len(pg.SCHEDULER_NATIVE_MARKERS) >= 2,
        str(pg.SCHEDULER_NATIVE_MARKERS),
    )

    # -- 3. base.py + run.py round-trip in a temp dir ----------------------
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="pg-verify-"))
    try:
        (tmp / "gateway" / "platforms").mkdir(parents=True)
        pristine_base = _synthetic_base(pg)
        pristine_run = _synthetic_run(pg)
        (tmp / "gateway" / "platforms" / "base.py").write_text(pristine_base, encoding="utf-8")
        (tmp / "gateway" / "run.py").write_text(pristine_run, encoding="utf-8")
        check(
            "synthetic pristine files are valid Python",
            _py_compiles(pristine_base) and _py_compiles(pristine_run),
        )

        r1 = subprocess.run(
            [sys.executable, str(PATCHER), "--site-packages", str(tmp)],
            capture_output=True, text=True, timeout=120,
        )
        patched_base = (tmp / "gateway" / "platforms" / "base.py").read_text(encoding="utf-8")
        patched_run = (tmp / "gateway" / "run.py").read_text(encoding="utf-8")
        check(
            "base.py patched with bubble method",
            r1.returncode == 0
            and "[patch] base.py bubble delivery" in r1.stdout
            and "async def _send_with_bubbles" in patched_base
            and "self._send_with_bubbles(" in patched_base,
            r1.stdout.strip()[-300:],
        )
        check(
            "run.py patched with injection",
            "[patch] run.py injection" in r1.stdout
            and "HumanPulse (companion-agent): hidden time context" in patched_run,
            r1.stdout.strip()[-300:],
        )
        check(
            "patched files parse",
            _py_compiles(patched_base) and _py_compiles(patched_run),
        )

        r2 = subprocess.run(
            [sys.executable, str(PATCHER), "--site-packages", str(tmp)],
            capture_output=True, text=True, timeout=120,
        )
        check(
            "second run idempotent (all skips)",
            r2.returncode == 0
            and "[skip] base.py already patched" in r2.stdout
            and "[skip] run.py already patched" in r2.stdout
            and "[patch]" not in r2.stdout,
            r2.stdout.strip()[-300:],
        )

        # -- 4. scheduler native-marker skip --------------------------------
        (tmp / "cron").mkdir(exist_ok=True)
        (tmp / "cron" / "scheduler.py").write_text(
            "def _deliver_result():\n"
            "    _HUMANPULSE_BUBBLE_SENDER = None\n"
            "    is_humanpulse_job = False\n"
            "    def _live_bubble_send(): pass\n"
            "    return None\n",
            encoding="utf-8",
        )
        r3 = subprocess.run(
            [sys.executable, str(PATCHER), "--site-packages", str(tmp)],
            capture_output=True, text=True, timeout=120,
        )
        check(
            "scheduler with native markers is skipped (no fail)",
            r3.returncode == 0
            and "already has HumanPulse cron bubble delivery" in r3.stdout
            and "[fail]" not in r3.stdout,
            r3.stdout.strip()[-300:],
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # -- 5. wiring-level suite (when the skill is installed) -----------------
    # verify_humanpulse.py needs the skill installed under ~/.hermes/skills/
    # so the bridge can load runtime.py.  If it is not installed yet (fresh
    # clone, or after a manual disable), skip instead of failing — the point
    # of THIS script is proving the patcher templates, not the install state.
    skill_dir = pathlib.Path.home() / ".hermes" / "skills" / "companion-agent"
    if not (skill_dir / "runtime.py").exists():
        print("[SKIP] verify_humanpulse.py — skill not installed (run adapters/hermes/install.py first)")
    else:
        r4 = subprocess.run(
            [sys.executable, str(SKILL_DIR / "scripts" / "verify_humanpulse.py")],
            capture_output=True, text=True, timeout=300,
        )
        check(
            "verify_humanpulse.py passes",
            r4.returncode == 0,
            (r4.stdout.strip().splitlines() or [""])[-1][:120],
        )

    print("=" * 50)
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


def _py_compiles(source: str) -> bool:
    try:
        compile(source, "<synthetic>", "exec")
        return True
    except SyntaxError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
