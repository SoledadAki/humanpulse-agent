#!/usr/bin/env python3
"""Re-apply the HumanPulse gateway patches to a Hermes site-packages install.

`hermes update` reinstalls Hermes via pip, which wipes every edit made
directly under site-packages (including the base_bubble_bridge / bubble
delivery wiring).  This script re-applies the HumanPulse wiring so the
gateway can inject hidden time context and the proactive reply note.

What it installs:
  1. gateway/platforms/base_bubble_bridge.py     (bubble sender loader)
  2. gateway/platforms/base.py                   (QQ/WeChat bubble hook)
  3. gateway/platforms/humanpulse_bridge.py      (state/runtime loader)
  4. gateway/platforms/cron_bubble_bridge.py     (cron bubble helper)
  5. cron/scheduler.py                           (live + standalone cron bubbles)
  6. gateway/run.py                              (hidden context injection)
  7. ~/.hermes/scripts/humanpulse_*.py           (cron scripts)
  8. ~/.hermes/cron/jobs.json                    (HumanPulse session mirroring)

It is idempotent: already-patched files are detected and skipped.

Usage:
    python3 patch_gateway.py [--site-packages PATH] [--dry-run]

Default site-packages is resolved from the running interpreter.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[2]
GATEWAY_SOURCES = SKILL_ROOT / "gateway" / "platforms"
BRIDGE_SOURCE = GATEWAY_SOURCES / "humanpulse_bridge.py"
BUBBLE_BRIDGE_SOURCE = GATEWAY_SOURCES / "base_bubble_bridge.py"
CRON_BUBBLE_BRIDGE_SOURCE = GATEWAY_SOURCES / "cron_bubble_bridge.py"
CRON_SOURCE_DIR = Path(__file__).resolve().parent / "cron"
CRON_JOBS = Path.home() / ".hermes" / "cron" / "jobs.json"
HUMANPULSE_JOB_NAMES = {"humanpulse-proactive", "humanpulse-followup"}
SCHEDULER_PATCH_MARKER = (
    "# HumanPulse (companion-agent): cron bubble delivery on live + standalone paths"
)
SCHEDULER_RELATIVE_PATHS = (
    Path("gateway") / "cron" / "scheduler.py",
    Path("hermes") / "cron" / "scheduler.py",
    Path("cron") / "scheduler.py",
)

# Marker comments used to detect whether a patch is already applied.
RUN_PATCH_MARKER = "# HumanPulse (companion-agent): hidden time context + proactive"
BASE_PATCH_MARKER = "async def _send_with_bubbles("

RUN_ANCHOR_OLD = """                    f\"history.]\"
                )
"""
RUN_ANCHOR_NEW = """                    f\"history.]\"
                )

            # HumanPulse (companion-agent): hidden time context + proactive
            # reply note.  API-only — the original message is preserved for
            # persistence via _persist_user_message_override, exactly like the
            # auto-continue note above.  Every function degrades to a safe
            # no-op when the skill bridge is not installed.
            try:
                from gateway.platforms.humanpulse_bridge import (
                    update_user_activity as _hp_update_user_activity,
                    build_hidden_time_context as _hp_time_ctx,
                    build_proactive_reply_note as _hp_reply_note,
                )
                _hp_note = _hp_reply_note()
                _hp_ctx = _hp_time_ctx(history)
                _hp_update_user_activity(history)
                if isinstance(message, str) and (_hp_ctx or _hp_note):
                    if _persist_user_message_override is None:
                        _persist_user_message_override = message
                    _hp_prefix = \"\\n\\n\".join(p for p in (_hp_ctx, _hp_note) if p)
                    message = f\"[HumanPulse context]\\n{_hp_prefix}\\n\\n{message}\"
            except Exception:
                pass
"""

BASE_METHOD_ANCHOR = """    @staticmethod
    def _merge_caption(existing_text: Optional[str], new_text: str) -> str:
"""

BASE_BUBBLE_METHOD = '''    async def _send_with_bubbles(
        self,
        *,
        event,
        session_key: str,
        text: str,
        reply_to,
        metadata,
        interrupt_event,
    ):
        """Send independent QQ/WeChat bubbles with safe fallback."""
        _bubble_platforms = (
            getattr(Platform, "QQBOT", None),
            getattr(Platform, "WEIXIN", None),
        )
        if getattr(self, "platform", None) not in _bubble_platforms:
            return await self._send_with_retry(
                chat_id=event.source.chat_id,
                content=text,
                reply_to=reply_to,
                metadata=metadata,
            )

        try:
            from gateway.platforms.base_bubble_bridge import send_human_reply
        except Exception:
            send_human_reply = None
        if send_human_reply is None:
            return await self._send_with_retry(
                chat_id=event.source.chat_id,
                content=text,
                reply_to=reply_to,
                metadata=metadata,
            )

        def _is_cancelled() -> bool:
            if interrupt_event is not None and interrupt_event.is_set():
                return True
            try:
                return session_key in self._pending_messages
            except Exception:
                return False

        async def _send_one(bubble: str):
            return await self._send_with_retry(
                chat_id=event.source.chat_id,
                content=bubble,
                reply_to=reply_to,
                metadata=metadata,
            )

        outcome = await send_human_reply(
            text,
            send_one=_send_one,
            is_cancelled=_is_cancelled,
        )
        logger.info(
            "[%s] bubble delivery: status=%s sent=%d remaining=%d",
            self.name,
            outcome.get("status"),
            len(outcome.get("sent", []) or []),
            len(outcome.get("remaining", []) or []),
        )
        return SendResult(success=True)

'''

BASE_SEND_ANCHOR_OLD = """                    result = await self._send_with_retry(
                        chat_id=event.source.chat_id,
                        content=text_content,
                        reply_to=_reply_anchor,
                        metadata=_final_thread_metadata,
                    )
"""

BASE_SEND_ANCHOR_NEW = """                    result = await self._send_with_bubbles(
                        event=event,
                        session_key=session_key,
                        text=text_content,
                        reply_to=_reply_anchor,
                        metadata=_final_thread_metadata,
                        interrupt_event=interrupt_event,
                    )
"""

SCHEDULER_HELPER = '''

{marker}
def _humanpulse_scope_token(value):
    value = getattr(value, "value", value)
    return str(value or "").rsplit(".", 1)[-1].upper()


def _humanpulse_scope_value(scope, keys):
    for key in keys:
        value = scope.get(key)
        if value is not None:
            if isinstance(value, dict):
                value = value.get("name") or value.get("platform") or value
            return value
    for value in scope.values():
        name = getattr(value, "name", None)
        if name:
            return name
        if isinstance(value, dict) and value.get("name"):
            return value["name"]
    return None


def _humanpulse_should_bubble(scope):
    job = _humanpulse_scope_value(
        scope, ("job", "cron_job", "current_job", "job_name")
    )
    if isinstance(job, dict):
        job = job.get("name")
    job_name = str(getattr(job, "name", job) or "").lower()
    platform = _humanpulse_scope_value(
        scope, ("platform", "platform_name", "target_platform", "adapter")
    )
    platform_name = _humanpulse_scope_token(platform)
    if platform_name not in {"QQBOT", "WEIXIN"}:
        for value in scope.values():
            token = _humanpulse_scope_token(value)
            if token in {"QQBOT", "WEIXIN"}:
                platform_name = token
                break
    return job_name.startswith("humanpulse") and platform_name in {"QQBOT", "WEIXIN"}


async def _humanpulse_send_bubbles(text, *, send_one, scope):
    if not _humanpulse_should_bubble(scope):
        return await send_one(text)
    try:
        from gateway.platforms.cron_bubble_bridge import send_cron_reply
    except Exception:
        return await send_one(text)
    return await send_cron_reply(text, send_one=send_one)
'''.replace("{marker}", SCHEDULER_PATCH_MARKER)


def _resolve_site_packages() -> Path:
    # Prefer the hermes-agent venv site-packages, fall back to sys.path.
    here = Path("/usr/local/lib/hermes-agent/venv/lib/python3.11/site-packages")
    if here.exists():
        return here
    for entry in sys.path:
        p = Path(entry)
        if (p / "gateway" / "run.py").exists():
            return p
    raise SystemExit("Cannot locate Hermes site-packages; pass --site-packages explicitly.")


def _copy_bridge(source: Path, dest: Path, dry_run: bool) -> None:
    if not source.exists():
        print(f"[warn] bridge source missing: {source}")
        return
    if dest.exists() and dest.read_bytes() == source.read_bytes():
        print(f"[skip] {dest.name} already current ({dest})")
        return
    print(f"[{'dry-run' if dry_run else 'copy'}] {source} -> {dest}")
    if not dry_run:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.copy2(dest, dest.with_suffix(dest.suffix + ".humanpulse.bak"))
        shutil.copy2(source, dest)


def _apply_base_patch(site: Path, dry_run: bool) -> None:
    target = site / "gateway" / "platforms" / "base.py"
    if not target.exists():
        print(f"[warn] base.py not found: {target}")
        return
    text = target.read_text(encoding="utf-8")
    if BASE_PATCH_MARKER in text:
        print(f"[skip] base.py already patched ({target})")
        return
    missing = [
        label
        for label, anchor in (
            ("method insertion", BASE_METHOD_ANCHOR),
            ("final send", BASE_SEND_ANCHOR_OLD),
        )
        if anchor not in text
    ]
    if missing:
        print(f"[fail] base.py anchors not found: {', '.join(missing)}")
        print("       Hermes version may differ; use references/hermes-gateway-humanpulse-wiring.md.")
        return
    print(f"[{'dry-run' if dry_run else 'patch'}] base.py bubble delivery ({target})")
    if not dry_run:
        backup = target.with_suffix(".py.humanpulse.bak")
        backup.write_text(text, encoding="utf-8")
        patched = text.replace(
            BASE_METHOD_ANCHOR,
            BASE_BUBBLE_METHOD + BASE_METHOD_ANCHOR,
            1,
        ).replace(BASE_SEND_ANCHOR_OLD, BASE_SEND_ANCHOR_NEW, 1)
        target.write_text(patched, encoding="utf-8")
        print(f"       backup: {backup}")


def _apply_run_patch(site: Path, dry_run: bool) -> None:
    target = site / "gateway" / "run.py"
    if not target.exists():
        print(f"[warn] run.py not found: {target}")
        return
    text = target.read_text(encoding="utf-8")
    if RUN_PATCH_MARKER in text:
        old_order = """                _hp_update_user_activity()
                _hp_ctx = _hp_time_ctx(history)
                _hp_note = _hp_reply_note()
"""
        new_order = """                _hp_note = _hp_reply_note()
                _hp_ctx = _hp_time_ctx(history)
                _hp_update_user_activity(history)
"""
        if old_order in text:
            print(f"[{'dry-run' if dry_run else 'repair'}] run.py HumanPulse call order ({target})")
            if not dry_run:
                backup = target.with_suffix(".py.humanpulse.bak")
                backup.write_text(text, encoding="utf-8")
                target.write_text(text.replace(old_order, new_order, 1), encoding="utf-8")
                print(f"       backup: {backup}")
            return
        if "_hp_note = _hp_reply_note()" in text and "_hp_update_user_activity()" in text:
            print(f"[{'dry-run' if dry_run else 'repair'}] run.py history capture ({target})")
            if not dry_run:
                backup = target.with_suffix(".py.humanpulse.bak")
                backup.write_text(text, encoding="utf-8")
                target.write_text(text.replace("_hp_update_user_activity()", "_hp_update_user_activity(history)", 1), encoding="utf-8")
                print(f"       backup: {backup}")
            return
        print(f"[skip] run.py already patched ({target})")
        return
    if RUN_ANCHOR_OLD not in text:
        print("[fail] run.py anchor not found — Hermes version may differ; patch manually.")
        print("       Anchor expected right after the interrupted-turn safety note.")
        return
    print(f"[{'dry-run' if dry_run else 'patch'}] run.py injection ({target})")
    if not dry_run:
        backup = target.with_suffix(".py.humanpulse.bak")
        backup.write_text(text, encoding="utf-8")
        patched = text.replace(RUN_ANCHOR_OLD, RUN_ANCHOR_NEW, 1)
        target.write_text(patched, encoding="utf-8")
        print(f"       backup: {backup}")


def _find_call_end(text: str, opening: int) -> int | None:
    depth = 0
    quote = ""
    escaped = False
    for index in range(opening, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _wrap_scheduler_delivery_call(text: str, pattern: str, start: int = 0) -> tuple[str, int]:
    match = re.search(pattern + r"\s*\(", text[start:])
    if match is None:
        return text, 0
    receiver_start = start + match.start()
    opening = start + match.end() - 1
    closing = _find_call_end(text, opening)
    if closing is None:
        return text, 0
    call = text[receiver_start : closing + 1]
    if "text_to_send" not in call:
        return text, 0
    bubble_call = re.sub(
        r"\btext\s*=\s*text_to_send",
        "text=_hp_bubble",
        call,
        count=1,
    )
    if bubble_call == call:
        bubble_call = bubble_call.replace("text_to_send", "_hp_bubble", 1)
    line_start = text.rfind("\n", 0, receiver_start) + 1
    await_start = text.rfind("await ", line_start, receiver_start + 1)
    if await_start < line_start:
        return text, 0
    indent = re.match(r"[ \t]*", text[line_start:]).group(0)
    body_indent = indent + "    "
    replacement = (
        "await _humanpulse_send_bubbles(\n"
        f"{body_indent}text_to_send,\n"
        f"{body_indent}send_one=lambda _hp_bubble: {bubble_call},\n"
        f"{body_indent}scope=locals(),\n"
        f"{indent})"
    )
    return text[:await_start] + replacement + text[closing + 1 :], 1


def _scheduler_path(site: Path) -> Path | None:
    for relative in SCHEDULER_RELATIVE_PATHS:
        target = site / relative
        if target.exists():
            return target
    return None


def _apply_scheduler_patch(site: Path, dry_run: bool) -> None:
    """Route both Hermes cron delivery paths through independent bubbles."""

    target = _scheduler_path(site)
    if target is None:
        print(f"[warn] scheduler.py not found under: {site}")
        return
    text = target.read_text(encoding="utf-8")
    if SCHEDULER_PATCH_MARKER in text:
        print(f"[skip] scheduler.py already patched ({target})")
        return

    patched = text
    patched, live_count = _wrap_scheduler_delivery_call(
        patched,
        r"router\.\s*_deliver_to_platform",
    )
    patched, standalone_count = _wrap_scheduler_delivery_call(
        patched,
        r"(?:[\w.]+\.)?_send_to_platform",
    )
    if not live_count and not standalone_count:
        print(f"[fail] scheduler.py delivery anchors not found ({target})")
        print("       Hermes version may differ; patch the live and standalone sends manually.")
        return
    print(
        f"[{'dry-run' if dry_run else 'patch'}] scheduler.py bubble delivery "
        f"(live={live_count}, standalone={standalone_count}) ({target})"
    )
    if not dry_run:
        backup = target.with_suffix(".py.humanpulse.bak")
        future = re.search(
            r"^from __future__ import[^\n]*\n",
            patched,
            flags=re.MULTILINE,
        )
        if future is None:
            with_helper = SCHEDULER_HELPER + patched
        else:
            with_helper = (
                patched[: future.end()]
                + SCHEDULER_HELPER
                + patched[future.end() :]
            )
        target.write_text(
            with_helper,
            encoding="utf-8",
        )
        backup.write_text(text, encoding="utf-8")
        print(f"       backup: {backup}")


def _install_cron_scripts(dry_run: bool) -> None:
    destination = Path.home() / ".hermes" / "scripts"
    for name in ("humanpulse_proactive.py", "humanpulse_followup.py"):
        source = CRON_SOURCE_DIR / name
        target = destination / name
        if not source.exists():
            print(f"[warn] cron source missing: {source}")
            continue
        if target.exists() and target.read_bytes() == source.read_bytes():
            print(f"[skip] cron script already current ({target})")
            continue
        print(f"[{'dry-run' if dry_run else 'copy'}] {source} -> {target}")
        if not dry_run:
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def _enable_cron_session_mirroring(
    jobs_path: Path = CRON_JOBS,
    *,
    dry_run: bool,
) -> None:
    """Enable mirroring only for the two HumanPulse jobs."""
    if not jobs_path.exists():
        print(f"[warn] cron jobs file not found: {jobs_path}")
        return
    try:
        data = json.loads(jobs_path.read_text(encoding="utf-8"))
        jobs = data.get("jobs")
        if not isinstance(jobs, list):
            print(f"[warn] invalid cron jobs shape: {jobs_path}")
            return
    except Exception as exc:
        print(f"[warn] cannot read cron jobs: {exc}")
        return
    changed = []
    found = set()
    for job in jobs:
        if not isinstance(job, dict) or job.get("name") not in HUMANPULSE_JOB_NAMES:
            continue
        found.add(job["name"])
        if job.get("attach_to_session") is not True:
            job["attach_to_session"] = True
            changed.append(job["name"])
        if job["name"] == "humanpulse-followup" and job.get("no_agent") is not False:
            job["no_agent"] = False
            if job["name"] not in changed:
                changed.append(job["name"])
    missing = HUMANPULSE_JOB_NAMES - found
    if missing:
        print(f"[warn] HumanPulse cron jobs not found: {', '.join(sorted(missing))}")
    if not changed:
        print(f"[skip] HumanPulse cron session mirroring already enabled ({jobs_path})")
        return
    print(f"[{'dry-run' if dry_run else 'patch'}] attach_to_session=True for: {', '.join(sorted(changed))}")
    if dry_run:
        return
    backup = jobs_path.with_suffix(jobs_path.suffix + ".humanpulse.bak")
    shutil.copy2(jobs_path, backup)
    temp = jobs_path.with_suffix(jobs_path.suffix + ".humanpulse.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(jobs_path)
    print(f"       backup: {backup}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-packages", default=None, help="Hermes site-packages dir")
    parser.add_argument("--dry-run", action="store_true", help="show what would change")
    args = parser.parse_args()

    site = Path(args.site_packages) if args.site_packages else _resolve_site_packages()
    print(f"site-packages: {site}")
    _copy_bridge(
        BUBBLE_BRIDGE_SOURCE,
        site / "gateway" / "platforms" / "base_bubble_bridge.py",
        args.dry_run,
    )
    _apply_base_patch(site, args.dry_run)
    _copy_bridge(
        BRIDGE_SOURCE,
        site / "gateway" / "platforms" / "humanpulse_bridge.py",
        args.dry_run,
    )
    _copy_bridge(
        CRON_BUBBLE_BRIDGE_SOURCE,
        site / "gateway" / "platforms" / "cron_bubble_bridge.py",
        args.dry_run,
    )
    _apply_scheduler_patch(site, args.dry_run)
    _apply_run_patch(site, args.dry_run)
    _install_cron_scripts(args.dry_run)
    _enable_cron_session_mirroring(dry_run=args.dry_run)
    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
