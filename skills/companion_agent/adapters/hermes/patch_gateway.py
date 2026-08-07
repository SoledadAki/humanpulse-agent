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
  4. gateway/run.py                              (hidden context injection)
  5. ~/.hermes/scripts/humanpulse_*.py           (cron scripts)

It is idempotent: already-patched files are detected and skipped.

Usage:
    python3 patch_gateway.py [--site-packages PATH] [--dry-run]

Default site-packages is resolved from the running interpreter.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[2]
GATEWAY_SOURCES = SKILL_ROOT / "gateway" / "platforms"
BRIDGE_SOURCE = GATEWAY_SOURCES / "humanpulse_bridge.py"
BUBBLE_BRIDGE_SOURCE = GATEWAY_SOURCES / "base_bubble_bridge.py"
CRON_SOURCE_DIR = Path(__file__).resolve().parent / "cron"

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
    _apply_run_patch(site, args.dry_run)
    _install_cron_scripts(args.dry_run)
    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
