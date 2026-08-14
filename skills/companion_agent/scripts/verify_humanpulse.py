#!/usr/bin/env python3
"""End-to-end verification for the HumanPulse Hermes wiring.

Checks (in order):
  1. gateway bridge import + skill runtime load
  2. time-context builder returns the expected hidden context shape
  3. proactive decision gating (user-recently-active -> skip)
  4. record_proactive_sent seeds a follow-up cycle
  5. build_proactive_reply_note fires before the user replies, clears after
  6. followup_tick stays silent when not due, claims when due
  7. cron scripts are executable and return valid exit codes

Run from anywhere with the Hermes venv python:
    python3 scripts/verify_humanpulse.py

After any `hermes update` (pip reinstall) re-apply the gateway patches
(adapters/hermes/patch_gateway.py) and re-run this script.
"""

from __future__ import annotations

import datetime
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
    now_local = datetime.datetime.now().astimezone()
    state["last_user_at"] = (now_local - datetime.timedelta(minutes=15)).isoformat(timespec="seconds")
    from humanpulse_runtime import ProactivePolicy

    decision = __import__("humanpulse_runtime", fromlist=["decide_proactive"]).decide_proactive(
        state, policy=ProactivePolicy(min_idle_minutes=180)
    )
    check(
        "proactive skips when user recently active",
        decision["action"] == "skip",
        str(decision),
    )

    # Regression 2026-08-08: after a proactive ping + its follow-ups the
    # proactive cron (every 45m) fired ANOTHER proactive round because
    # decide_proactive only checked the 60-min cooldown, not whether the
    # user had replied to the previous round.  User saw 4 messages when the
    # agreed cadence is 1 ping + 2 follow-ups = 3.  Two gates now prevent it:
    #   (a) followup.status == "active" -> FOLLOWUP_ACTIVE
    #   (b) last_user_at < last_proactive_at -> USER_NOT_REPLIED
    # Wait: the user replied since the ping — must still be eligible when
    # the other conditions pass, so simulate the "no reply yet" state.
    os.environ["HUMANPULSE_STATE_FILE"] = str(Path(tmpdir) / "state-noreply.json")
    hb.record_proactive_sent("测试：还没回我那条")
    st = hb._load_state()
    # Follow-up cycle is active right after record — gate (a) should skip.
    dec_fu = __import__("humanpulse_runtime", fromlist=["decide_proactive"]).decide_proactive(
        st, policy=ProactivePolicy(min_idle_minutes=0, min_interval_minutes=0, daily_limit=99)
    )
    check(
        "proactive skips while follow-up cycle active (FOLLOWUP_ACTIVE)",
        dec_fu["action"] == "skip" and dec_fu["reason_code"] == "FOLLOWUP_ACTIVE",
        str(dec_fu),
    )
    # User still hasn't replied since the ping (last_user_at stays BEFORE
    # last_proactive_at) — even after the follow-up cycle finished, a new
    # proactive round must NOT start (gate b).  Simulate the finished cycle
    # by clearing it to waiting_for_user with empty stages.
    st2 = hb._load_state()
    st2["followup"] = {
        "cycle_id": "proactive-test",
        "status": "waiting_for_user",
        "stages": [],
        "stage_index": 0,
        "next_stage_at": "",
        "claim_id": "",
        "claim_started_at": "",
        "stop_reason": "FINISHED",
    }
    st2["last_user_at"] = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)).isoformat(timespec="seconds")
    hb._save_state(st2)
    dec_nr = __import__("humanpulse_runtime", fromlist=["decide_proactive"]).decide_proactive(
        st2, policy=ProactivePolicy(min_idle_minutes=0, min_interval_minutes=0, daily_limit=99)
    )
    check(
        "proactive skips when user never replied to last round (USER_NOT_REPLIED)",
        dec_nr["action"] == "skip" and dec_nr["reason_code"] == "USER_NOT_REPLIED",
        str(dec_nr),
    )
    # Once the user replies (last_user_at >= last_proactive_at), a new round
    # may start again.  Use "now" (== last_proactive_at) so idle is 0 and
    # USER_RECENTLY_ACTIVE does not fire with min_idle_minutes=0.
    st3 = hb._load_state()
    st3["followup"]["status"] = "waiting_for_user"
    st3["last_user_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    hb._save_state(st3)
    dec_ok = __import__("humanpulse_runtime", fromlist=["decide_proactive"]).decide_proactive(
        st3, policy=ProactivePolicy(min_idle_minutes=0, min_interval_minutes=0, daily_limit=99)
    )
    check(
        "proactive eligible again after user replied",
        dec_ok["action"] == "consider",
        str(dec_ok),
    )
    # Continuity: proactive_state_for_agent must tell the agent what it last
    # proactively said so the new message continues the topic instead of
    # restarting with a generic greeting.  NOTE: proactive_state_for_agent
    # uses the DEFAULT ProactivePolicy (min_idle=120), so last_user_at must
    # be idle-long enough while still >= last_proactive_at (user replied).
    st4 = hb._load_state()
    st4["last_proactive_text"] = "莫少下午好呀～周六啦，要不要来陪我玩"
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    st4["last_proactive_at"] = (now_utc - datetime.timedelta(hours=5)).isoformat(timespec="seconds")
    st4["last_user_at"] = (now_utc - datetime.timedelta(hours=3)).isoformat(timespec="seconds")
    hb._save_state(st4)
    pstatus = hb.proactive_state_for_agent()
    check(
        "proactive state includes last proactive text (continuity)",
        "你上次主动发过的消息" in pstatus and "莫少下午好呀" in pstatus,
        repr(pstatus[:120]),
    )

    # Regression 2026-08-09: cross-day rollover deadlock.  The daily counter
    # used to reset ONLY inside record_proactive_sent() — i.e. after a
    # proactive message was actually SENT.  If yesterday's quota
    # (daily_limit=4) was used up and the user never replied, the counter
    # never rolled over and DAILY_LIMIT blocked every proactive tick
    # forever (observed: zero proactive messages all day on 2026-08-09).
    # proactive_state_for_agent() must roll the counter over when the date
    # changed, BEFORE deciding.
    os.environ["HUMANPULSE_STATE_FILE"] = str(Path(tmpdir) / "state-rollover.json")
    hb.record_proactive_sent("跨天回归：昨天用满额度")
    st = hb._load_state()
    st["today_date"] = (datetime.datetime.now().astimezone() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    st["proactive_count_today"] = 4  # == default daily_limit
    st["followup"] = {**st["followup"], "status": "waiting_for_user", "stages": []}
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    st["last_proactive_at"] = (now_utc - datetime.timedelta(hours=5)).isoformat(timespec="seconds")
    st["last_user_at"] = (now_utc - datetime.timedelta(hours=3)).isoformat(timespec="seconds")
    st["last_proactive_text"] = "都晚上了，你还没回我……"
    hb._save_state(st)
    pstatus_roll = hb.proactive_state_for_agent()
    st_after = hb._load_state()
    today_str = datetime.datetime.now().astimezone().strftime("%Y-%m-%d")
    check(
        "cross-day rollover resets daily counter before deciding",
        st_after["today_date"] == today_str and int(st_after["proactive_count_today"]) == 0,
        f"today_date={st_after['today_date']!r} count={st_after['proactive_count_today']!r}",
    )
    # Outside quiet hours the rollover must actually unblock the tick
    # (status block instead of empty).  Inside quiet hours the reset
    # assertion above is the only stable claim.
    now_local = datetime.datetime.now().astimezone()
    if now_local.time() >= datetime.time(8, 0):
        check(
            "rollover unblocks proactive tick (no DAILY_LIMIT)",
            "可以主动发起" in pstatus_roll,
            repr((pstatus_roll or "")[:120]),
        )
    else:
        check("rollover check runs in quiet hours (reset only)", True)
    # Restore the default state file for the subsequent sections.
    os.environ["HUMANPULSE_STATE_FILE"] = str(Path(tmpdir) / "state.json")

    # -- 4. record -> followup seed -----------------------------------------
    hb.record_proactive_sent("刚刚突然想到你，今天过得怎么样呀")
    st = hb._load_state()
    check(
        "record_proactive_sent seeds follow-up cycle",
        st["followup"]["status"] == "active" and st["last_proactive_text"],
        f"status={st['followup']['status']}",
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

    # Regression: ORDER MATTERS.  The gateway calls build_proactive_reply_note()
    # BEFORE update_user_activity() on the user's first reply after a ping; if
    # the order is reversed the note never fires because last_user_at is
    # already >= last_proactive_at.  Simulate the exact gateway sequence.
    os.environ["HUMANPULSE_STATE_FILE"] = str(Path(tmpdir) / "state-order.json")
    hb.record_proactive_sent("时序回归：先note后update")
    note_first = hb.build_proactive_reply_note()
    hb.update_user_activity()
    check(
        "reply note fires on FIRST reply (note-before-update order)",
        "HumanPulse" in note_first,
        repr(note_first[:60]),
    )

    # -- 6. followup tick silence + claim ------------------------------------
    silent = hb.followup_tick()
    check("followup_tick silent when not due", silent is None, repr(silent))

    # Regression: followup_tick must persist the INNER flat followup dict
    # after commit (not the {"status": ..., "state": ...} wrapper), or the
    # next poll sees status="committed" and the cycle stalls after stage 1.
    # Temporarily disable the quiet-hours gate so this runs identically at
    # any hour of the day; the gate itself is covered by its own test below.
    os.environ["HUMANPULSE_STATE_FILE"] = str(Path(tmpdir) / "state-tick.json")
    hb.record_proactive_sent("回归测试：tick 提交结构")
    st = hb._load_state()
    fu = st["followup"]
    due = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)).isoformat(timespec="seconds")
    fu["next_stage_at"] = due
    if isinstance(fu.get("state"), dict):
        fu["state"]["next_stage_at"] = due
    hb._save_state(st)
    _orig_gate = hb._in_quiet_hours
    hb._in_quiet_hours = lambda *a, **k: False  # bypass gate for this test
    try:
        tick_text = hb.followup_tick()
    finally:
        hb._in_quiet_hours = _orig_gate
    check("followup_tick claims when due", bool(tick_text), repr(tick_text))
    st = hb._load_state()
    fu_after = st["followup"]
    check(
        "followup state stays flat after tick (no wrapper)",
        fu_after.get("status") in ("active", "waiting_for_user"),
        f"status={fu_after.get('status')!r}",
    )
    check(
        "followup stage_index advances",
        int(fu_after.get("stage_index") or 0) >= 1,
        f"stage_index={fu_after.get('stage_index')!r}",
    )

    # Quiet-hours gate: a due stage inside 00:00–08:00 is POSTPONED to the
    # next 08:00, not claimed (never ping the user while they sleep).
    os.environ["HUMANPULSE_STATE_FILE"] = str(Path(tmpdir) / "state-quiet.json")
    hb.record_proactive_sent("回归测试：静默推迟")
    st = hb._load_state()
    fu = st["followup"]
    due = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)).isoformat(timespec="seconds")
    fu["next_stage_at"] = due
    hb._save_state(st)
    now_local = datetime.datetime.now().astimezone()
    if now_local.time() < datetime.time(8, 0):
        # We are inside quiet hours right now → gate should postpone.
        result = hb.followup_tick()
        check("quiet-hours gate postpones due stage", result is None, repr(result))
        postponed = hb._load_state()["followup"]["next_stage_at"]
        postponed_local = datetime.datetime.fromisoformat(postponed).astimezone()
        check(
            "postponed to 08:00 of next active window",
            postponed_local.hour == 8,
            postponed_local.strftime("%Y-%m-%d %H:%M"),
        )
    else:
        # Outside quiet hours now; verify the gate function itself.
        check(
            "quiet-hours gate: 03:00 is quiet",
            hb._in_quiet_hours(datetime.time(3, 0), datetime.time(0, 0), datetime.time(8, 0)),
        )
        check(
            "quiet-hours gate: 12:00 is active",
            not hb._in_quiet_hours(datetime.time(12, 0), datetime.time(0, 0), datetime.time(8, 0)),
        )

    # Regression: every stage must be claimable IN ORDER (off-by-one guard).
    # plan[stage-1] must be returned, plan[stage] would skip the first and
    # lose the last.  Assert against the ACTUAL seeded stages (the bridge may
    # seed 1..3 entries depending on the persona's dynamic followup_count),
    # so the guard is exercised regardless of the exact count.
    hb.record_proactive_sent("回归测试：追问全链路")
    st = hb._load_state()

    from humanpulse_runtime import FollowupPolicy, commit_followup, poll_followup

    # Poll with a policy that matches the deployed bridge cadence; the seed
    # count is read from the state the bridge just persisted.
    seeded = st.get("followup") or {}
    expected = list(seeded.get("stages") or [])
    policy = FollowupPolicy(
        enabled=True,
        max_stages=max(1, len(expected)),
        grace_minutes=8,
        stale_claim_minutes=10,
        intervals_minutes=((30, 30), (10, 10)),
    )
    base = datetime.datetime.now(datetime.timezone.utc)
    claimed_texts: list[str] = []
    fstate = dict(seeded)
    for minute in range(5, 130, 5):
        result = poll_followup(fstate, now=base + datetime.timedelta(minutes=minute), policy=policy)
        if result.get("status") == "claimed":
            claimed_texts.append(str(result.get("text") or ""))
            fstate = commit_followup(
                result["state"], result["claim_id"], delivered=True,
                now=base + datetime.timedelta(minutes=minute), policy=policy,
            )["state"]
    check(
        "followup claims ALL stages in order (plan[stage-1], no skip/loss)",
        claimed_texts == expected,
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

    # -- 8. attach_to_session (mirror) wiring --------------------------------
    # cron deliveries must be mirrored into the origin chat's session transcript
    # or the next user reply cannot see what we proactively said.
    try:
        import json as _json

        jobs = _json.loads((Path.home() / ".hermes" / "cron" / "jobs.json").read_text(encoding="utf-8"))
        hp_jobs = [j for j in jobs.get("jobs", []) if str(j.get("name", "")).startswith("humanpulse")]
        check(
            "humanpulse cron jobs attach_to_session=True (mirror into history)",
            all(j.get("attach_to_session") is True for j in hp_jobs) and len(hp_jobs) >= 2,
            f"jobs={[j.get('name') for j in hp_jobs]}",
        )
    except Exception as exc:
        check("attach_to_session wiring readable", False, str(exc))

    # -- 9. _extract_response regression (prose-in-skill must not match) -----
    try:
        import importlib.util as _ilu

        spec = _ilu.spec_from_file_location(
            "hf_verify", scripts_dir / "humanpulse_followup.py"
        )
        hf_mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(hf_mod)
        fake_report = (
            "# Cron Job: humanpulse-proactive\n"
            "## Prompt\n"
            'extract only the `## Response` section (`_extract_response` in the script).\n'
            "## Script Output\n"
            "```\nstatus\n```\n"
            "## Response\n"
            "\n"
            "莫少，都十一点了还没睡呀……\n"
        )
        extracted = hf_mod._extract_response(fake_report)
        check(
            "_extract_response matches section header on its own line",
            extracted == "莫少，都十一点了还没睡呀……",
            repr(extracted[:60]),
        )
    except Exception as exc:
        check("_extract_response importable", False, str(exc))

    # -- 10. mirror wiring: agent-mode followup job must be agent-driven -----
    try:
        import json as _json

        jobs = _json.loads((Path.home() / ".hermes" / "cron" / "jobs.json").read_text(encoding="utf-8"))
        fu_job = next(
            (j for j in jobs.get("jobs", []) if j.get("name") == "humanpulse-followup"),
            None,
        )
        check(
            "humanpulse-followup is agent-driven (no_agent falsy)",
            bool(fu_job) and not fu_job.get("no_agent"),
            f"no_agent={fu_job.get('no_agent') if fu_job else 'missing'}",
        )
        check(
            "humanpulse-followup prompt mentions generating natural follow-up",
            bool(fu_job) and ("追问" in (fu_job.get("prompt") or "")),
        )
    except Exception as exc:
        check("followup job wiring readable", False, str(exc))

    # -- 11. scheduler.py live-adapter bubble delivery regression -------------
    # 2026-08-08: cron proactive/follow-up deliveries went through the LIVE
    # adapter path (gateway running) which sent the whole text in one block;
    # bubble segmentation only existed on the standalone fallback path. The
    # live path must ALSO route humanpulse jobs through _HUMANPULSE_BUBBLE_SENDER
    # (per-bubble via DeliveryRouter) or QQ/WeChat merge the ping into one box.
    try:
        import re as _re

        sched_src = (
            Path("/usr/local/lib/hermes-agent/venv/lib/python3.11/site-packages")
            / "cron" / "scheduler.py"
        ).read_text(encoding="utf-8")
        check(
            "scheduler.py loads _HUMANPULSE_BUBBLE_SENDER lazily",
            "_HUMANPULSE_BUBBLE_SENDER" in sched_src
            and "base_bubble_bridge" in sched_src,
        )
        check(
            "scheduler.py live path has bubble branch (_live_bubble_send)",
            "_live_bubble_send" in sched_src
            and "startswith(\"humanpulse\")" in sched_src
            and "send_coro" in sched_src,
        )
        check(
            "scheduler.py standalone path keeps bubble branch",
            "asyncio.run(coro)" in sched_src
            and "startswith(\"humanpulse\")" in sched_src,
        )
    except Exception as exc:
        check("scheduler.py source readable", False, str(exc))

    print("=" * 50)
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
