#!/usr/bin/env python3
"""One-shot installer for companion-agent on Hermes (QQ/WeChat).

Installs the complete HumanPulse wiring so the skill works out of the box:

  1. Copies this skill directory into ~/.hermes/skills/companion-agent/
     (backup of an existing dir is kept as companion-agent.bak.HUMANPULSE).
  2. Applies the gateway patch (adapters/hermes/patch_gateway.py) — copies
     the bridge modules into site-packages, patches gateway/run.py hidden
     context injection, and routes cron deliveries through bubbles.  The
     patcher is idempotent and keeps .humanpulse.bak backups.
  3. Creates the two cron jobs (humanpulse-proactive, humanpulse-followup)
     if they do not already exist, using hermes cron create with the wiring
     from references/hermes-cron-wiring.md.
  4. Clears the disable switches (HERMES_HUMANPULSE_CONTEXT,
     HERMES_BUBBLE_DELIVERY) from ~/.hermes/.env so the wiring activates.
  5. Runs scripts/verify_humanpulse.py and reports each failure.

Run with the Hermes python environment:

    python3 adapters/hermes/install.py [--dry-run]

--dry-run only prints what would change (no writes).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[2]  # skills/companion_agent (install.py is in adapters/hermes/)
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
DEST_SKILL = HERMES_HOME / "skills" / "companion-agent"
ENV_FILE = HERMES_HOME / ".env"
CRON_JOBS = HERMES_HOME / "cron" / "jobs.json"
JOB_NAMES = ("humanpulse-proactive", "humanpulse-followup")

# (name, schedule, script, prompt) — the same values as references/hermes-cron-wiring.md
CRON_JOBS_DEF = (
    (
        "humanpulse-proactive",
        "every 45m",
        "humanpulse_proactive.py",
        (
            "你是一个带 HumanPulse 时间感/主动聊天能力的陪伴角色。\n\n"
            "上方 ## Script Output 是 HumanPulse 主动消息判定结果：\n"
            "- 如果脚本输出为空或没有 Script Output 区块，说明现在不适合主动发消息，请回复 [SILENT]。\n"
            "- 如果脚本输出包含 \"HumanPulse 主动消息判定：可以主动发起一条消息\"，说明可以主动说一句话。\n\n"
            "当可以主动发消息时：以角色身份自然地说 1-2 个短气泡，像自然想到对方一样冒泡。\n"
            "可以问一句近况、分享一个小念头、或表达想念。绝对不要提定时器、扫描、脚本、技能、\n"
            "HumanPulse 等机制词汇，也不要说\"我刚刚检测到\"之类的话。语气要自然、有真人感。\n\n"
            "如果实在没有想说的，回复 [SILENT] 保持安静，不要硬凑。"
        ),
    ),
    (
        "humanpulse-followup",
        "every 5m",
        "humanpulse_followup.py",
        (
            "根据上方 HumanPulse 追问上下文生成一条自然的后续消息。"
            "遵循当前人设，不默认撒娇；不要提脚本、定时器或沉默时长。"
            "没有自然内容时输出 [SILENT]。"
        ),
    ),
)


def log(msg: str) -> None:
    print(msg, flush=True)


def _cron_job_names() -> set[str]:
    try:
        data = json.loads(CRON_JOBS.read_text(encoding="utf-8"))
        return {str(job.get("name") or "") for job in data.get("jobs", []) if isinstance(job, dict)}
    except Exception:
        return set()


def _install_skill(dry_run: bool) -> None:
    dest = DEST_SKILL
    if dest.is_dir():
        backup = dest.parent / f"{dest.name}.bak.HUMANPULSE"
        if not backup.exists():
            if dry_run:
                log(f"[dry-run] would backup existing skill -> {backup}")
            else:
                log(f"[backup] {dest} -> {backup}")
                shutil.copytree(dest, backup)
        else:
            log(f"[skip] backup already exists ({backup})")
    if dry_run:
        log(f"[dry-run] would copy {SKILL_DIR} -> {dest}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(SKILL_DIR, dest, ignore=shutil.ignore_patterns("__pycache__"))
    log(f"[install] skill copied -> {dest}")


def _apply_gateway_patch(dry_run: bool, site_packages: str | None = None) -> int:
    patcher = SKILL_DIR / "adapters" / "hermes" / "patch_gateway.py"
    cmd = [sys.executable, str(patcher)]
    if site_packages:
        cmd += ["--site-packages", site_packages]
    if dry_run:
        cmd.append("--dry-run")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    log(proc.stdout.rstrip())
    if proc.stderr.strip():
        log(proc.stderr.rstrip())
    return proc.returncode


def _create_cron_jobs(dry_run: bool) -> None:
    existing = _cron_job_names()
    for name, schedule, script, prompt in CRON_JOBS_DEF:
        if name in existing:
            log(f"[skip] cron job already exists: {name}")
            continue
        cmd = [
            "hermes", "cron", "create",
            "--name", name,
            "--script", script,
            "--skill", "companion-agent",
            "--deliver", "origin",
            schedule, prompt,
        ]
        if dry_run:
            log(f"[dry-run] would create cron job: {name} ({schedule})")
            continue
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode == 0:
            log(f"[cron] created: {name}")
            if proc.stdout.strip():
                log(f"        {proc.stdout.strip().splitlines()[-1]}")
        else:
            log(f"[FAIL] could not create cron job {name}: {proc.stderr.strip() or proc.stdout.strip()}")


def _clear_env_switches(dry_run: bool) -> None:
    if not ENV_FILE.exists():
        log(f"[skip] no .env file ({ENV_FILE})")
        return
    text = ENV_FILE.read_text(encoding="utf-8")
    original = text
    for key in ("HERMES_HUMANPULSE_CONTEXT", "HERMES_BUBBLE_DELIVERY"):
        pattern = re.compile(rf"^#?\s*{key}\s*=.*$", flags=re.MULTILINE)
        text, count = pattern.subn("", text)
        if count:
            log(f"[env] removed disable switch: {key}")
    if text == original:
        log("[skip] no HumanPulse disable switches in .env")
        return
    if dry_run:
        return
    backup = ENV_FILE.with_suffix(ENV_FILE.suffix + ".humanpulse.bak")
    if not backup.exists():
        shutil.copy2(ENV_FILE, backup)
        log(f"[env] backup -> {backup}")
    ENV_FILE.write_text(text, encoding="utf-8")
    log("[env] .env updated")


def _verify(dry_run: bool) -> int:
    verifier = SKILL_DIR / "scripts" / "verify_humanpulse.py"
    if dry_run:
        log("[dry-run] would run verify_humanpulse.py")
        return 0
    if not verifier.exists():
        log(f"[FAIL] verifier missing: {verifier}")
        return 1
    proc = subprocess.run([sys.executable, str(verifier)], capture_output=True, text=True, timeout=300)
    log(proc.stdout.rstrip())
    if proc.stderr.strip():
        log(proc.stderr.rstrip())
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="show what would change")
    parser.add_argument(
        "--site-packages",
        default=None,
        help="Hermes site-packages dir (default: resolved by patch_gateway.py)",
    )
    args = parser.parse_args()

    dry = args.dry_run
    log(f"companion-agent Hermes installer (skill dir: {SKILL_DIR})")
    log("=" * 56)
    _install_skill(dry)
    patch_rc = _apply_gateway_patch(dry, args.site_packages)
    _create_cron_jobs(dry)
    _clear_env_switches(dry)
    if patch_rc != 0:
        log("[warn] gateway patch reported errors; see output above")
    log("=" * 56)
    verify_rc = _verify(dry)
    if dry:
        log("dry-run complete.")
        return 0
    if patch_rc == 0 and verify_rc == 0:
        log("INSTALL OK — restart the Hermes gateway for the wiring to take effect.")
        return 0
    log("INSTALL INCOMPLETE — fix the failures above, then re-run this script.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
