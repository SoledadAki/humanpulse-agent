#!/usr/bin/env python3
"""End-to-end verification for the HumanPulse Hermes wiring.

Checks (in order):
  1. gateway bridge import + skill runtime load
  2. legacy state pollution is repaired before use
  3. gateway injection reads the proactive note before activity is updated
  4. time-context builder returns the expected hidden context shape
  5. proactive decision gating (user-recently-active -> skip)
  6. record_proactive_sent keeps delivered text as follow-up stage 0
  7. build_proactive_reply_note fires before the user replies, clears after
  8. followup_tick persists the inner follow-up state without nesting it
  9. cron scripts are executable and return valid exit codes

Run from anywhere with the Hermes venv python:
    python3 scripts/verify_humanpulse.py

After any `hermes update` (pip reinstall) re-apply the gateway patches
(adapters/hermes/patch_gateway.py) and re-run this script.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
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


def main() -> int:
    print("HumanPulse Hermes wiring verification")
    print("=" * 50)

    # Point the bridge at a temp state file so verification never touches
    # the live state.
    tmpdir = tempfile.mkdtemp(prefix="humanpulse-verify-")
    os.environ["HUMANPULSE_STATE_FILE"] = str(Path(tmpdir) / "state.json")

    # -- 1. bridge + runtime ------------------------------------------------
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    try:
        from gateway.platforms import humanpulse_bridge as hb
    except Exception as exc:
        check("bridge import", False, str(exc))
        return 1
    check("bridge import", True)
    check("skill runtime loads", hb._load_modules(), str(hb._locate_skill_dir()))

    state_path = Path(tmpdir) / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "last_proactive_at": "2026-08-07T10:00:00+00:00",
                "last_proactive_text": "# Cron report\n" + "x" * 1400,
                "followup": {"status": "committed", "state": {"status": "active"}},
            }
        ),
        encoding="utf-8",
    )
    repaired = hb._load_state()
    check(
        "legacy state pollution is repaired",
        repaired.get("last_proactive_text") == ""
        and "state" not in repaired.get("followup", {})
        and repaired.get("followup", {}).get("status") == "idle",
        repr(repaired),
    )

    gateway_run = Path(hb.__file__).resolve().parents[1] / "run.py"
    run_text = gateway_run.read_text(encoding="utf-8") if gateway_run.exists() else ""
    note_pos = run_text.find("_hp_note = _hp_reply_note()")
    update_pos = run_text.find("_hp_update_user_activity(history)")
    if update_pos < 0:
        update_pos = run_text.find("_hp_update_user_activity()")
    check(
        "gateway reads proactive note before updating user activity",
        note_pos >= 0 and update_pos >= 0 and note_pos < update_pos,
        str(gateway_run),
    )
    check(
        "gateway keeps HumanPulse context API-only",
        "_persist_user_message_override" in run_text
        and "[HumanPulse context]" in run_text,
        str(gateway_run),
    )

    # -- 2. time context ----------------------------------------------------
    ctx = hb.build_hidden_time_context(
        [
            {"role": "user", "content": "你好", "timestamp": "2026-08-07T09:00:00+08:00"},
            {"role": "assistant", "content": "你好呀", "timestamp": "2026-08-07T09:00:05+08:00"},
        ]
    )
    check(
        "time context has current time + continuity",
        "当前本地时间" in ctx and "连续性判断" in ctx,
        ctx.splitlines()[0] if ctx else "EMPTY",
    )

    # -- 3. proactive gating ------------------------------------------------
    state = hb._load_state()
    state["last_user_at"] = "2026-08-07T18:50:00+08:00"  # 15 min ago
    from humanpulse_runtime import ProactivePolicy

    decision = __import__("humanpulse_runtime", fromlist=["decide_proactive"]).decide_proactive(
        state, policy=ProactivePolicy(min_idle_minutes=180)
    )
    check(
        "proactive skips when user recently active",
        decision["action"] == "skip",
        str(decision),
    )

    # -- 4. record -> followup seed -----------------------------------------
    proactive_text = "刚刚突然想到你，今天过得怎么样呀"
    hb.record_proactive_sent(proactive_text)
    st = hb._load_state()
    check(
        "record_proactive_sent seeds follow-up cycle",
        st["followup"]["status"] == "active" and st["last_proactive_text"],
        f"status={st['followup']['status']}",
    )
    check(
        "delivered proactive text is follow-up stage 0",
        st["followup"]["stages"][0] == proactive_text,
        repr(st["followup"]["stages"]),
    )

    # -- 5. reply note fires / clears ---------------------------------------
    note_before = hb.build_proactive_reply_note()
    check("proactive reply note fires before user reply", "HumanPulse" in note_before)
    hb.update_user_activity()
    note_after = hb.build_proactive_reply_note()
    check("reply note clears after user reply", note_after == "")
    check(
        "update_user_activity stops follow-up",
        hb._load_state()["followup"]["status"] == "waiting_for_user",
    )

    # -- 6. followup tick silence + claim ------------------------------------
    silent = hb.followup_tick()
    check("followup_tick silent when not due", silent is None, repr(silent))

    hb.record_proactive_sent("回归测试：桥接追问状态")
    due_state = hb._load_state()
    import datetime

    due_state["followup"]["next_stage_at"] = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)
    ).isoformat()
    hb._save_state(due_state)
    delivered = hb.followup_tick()
    persisted_followup = hb._load_state()["followup"]
    check("followup_tick returns the first follow-up", bool(delivered), repr(delivered))
    check(
        "followup_tick persists inner state without nesting",
        "state" not in persisted_followup and persisted_followup.get("stage_index") == 1,
        repr(persisted_followup),
    )

    # Regression: every stage must be claimable IN ORDER (off-by-one guard).
    # plan[stage-1] must be returned, plan[stage] would skip the first and
    # lose the last.
    hb.record_proactive_sent("回归测试：追问全链路")
    st = hb._load_state()
    from humanpulse_runtime import FollowupPolicy, commit_followup, poll_followup

    policy = FollowupPolicy(
        enabled=True,
        max_stages=3,
        grace_minutes=8,
        stale_claim_minutes=10,
        intervals_minutes=((30, 40), (12, 20), (6, 10)),
    )
    base = datetime.datetime.now(datetime.timezone.utc)
    claimed_texts: list[str] = []
    fstate = dict(st["followup"])
    for minute in range(5, 130, 5):
        result = poll_followup(fstate, now=base + datetime.timedelta(minutes=minute), policy=policy)
        if result.get("status") == "claimed":
            claimed_texts.append(str(result.get("text") or ""))
            fstate = commit_followup(
                result["state"], result["claim_id"], delivered=True,
                now=base + datetime.timedelta(minutes=minute), policy=policy,
            )["state"]
    check(
        "followup claims ALL stages in order",
        claimed_texts == [
            "刚说到一半……其实我还有句话想跟你说",
            "那个……你是不是在忙呀？",
            "好啦不打扰你了，等你忙完记得回来找我",
        ],
        str(claimed_texts),
    )

    # -- 7. cron scripts -----------------------------------------------------
    scripts_dir = Path.home() / ".hermes" / "scripts"
    for script_name in ("humanpulse_proactive.py", "humanpulse_followup.py"):
        script = scripts_dir / script_name
        check(f"cron script exists: {script_name}", script.exists())
        if script.exists():
            proc = subprocess.run(
                [sys.executable, str(script)],
                capture_output=True,
                text=True,
                timeout=60,
                env={**os.environ, "HUMANPULSE_STATE_FILE": str(Path(tmpdir) / "state.json")},
            )
            check(f"cron script runs (exit=0): {script_name}", proc.returncode == 0, proc.stderr.strip()[:200] or "ok")

    print("=" * 50)
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
