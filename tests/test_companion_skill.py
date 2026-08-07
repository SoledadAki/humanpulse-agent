import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from skills.companion_agent import (
    FollowupPolicy,
    ProactivePolicy,
    build_time_context,
    commit_followup,
    decide_proactive,
    normalize_proactive_response,
    poll_followup,
    split_reply_bubbles,
    start_followup_cycle,
    stop_followup,
    build_proactive_prompt,
    choose_proactive_angle,
)
from skills.companion_agent.adapters.hermes.send_bubbles import send_human_reply
from skills.companion_agent.adapters.hermes.gateway_bubble_bridge import send_with_bubbles
from skills.companion_agent.adapters.hermes.patch_gateway import (
    BASE_PATCH_MARKER,
    BASE_SEND_ANCHOR_NEW,
    BRIDGE_SOURCE,
    BUBBLE_BRIDGE_SOURCE,
    RUN_ANCHOR_NEW,
    RUN_PATCH_MARKER,
    _apply_base_patch,
    _apply_run_patch,
    _copy_bridge,
)
from skills.companion_agent.gateway.platforms import humanpulse_bridge
from skills.companion_agent import runtime as humanpulse_runtime
from skills.companion_agent import state as humanpulse_state


NOW = datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc)


class CompanionSkillTests(unittest.TestCase):
    def test_time_context_detects_overnight_gap(self):
        context = build_time_context(
            [{"role": "user", "timestamp": "2026-08-05T14:00:00+08:00"}],
            now=datetime(2026, 8, 6, 0, 0, tzinfo=timezone.utc),
        )
        self.assertIn("早上", context)
        self.assertIn("隔了一夜", context)
        self.assertIn("已跨 1 个自然日", context)

    def test_split_reply_bubbles_preserves_content_and_limit(self):
        text = "第一句。第二句！第三句？"
        bubbles = split_reply_bubbles(text, max_bubbles=2)
        self.assertEqual(bubbles, ["第一句。", "第二句！ 第三句？"])

    def test_proactive_decision_respects_quiet_hours_and_idle(self):
        policy = ProactivePolicy(min_idle_minutes=30, timezone="Asia/Shanghai")
        quiet = decide_proactive({"enabled": True}, now=NOW, policy=policy)
        self.assertEqual(quiet["reason_code"], "QUIET_HOURS")

        recent = decide_proactive(
            {"enabled": True, "last_user_at": "2026-08-06T11:45:00+00:00"},
            now=datetime(2026, 8, 6, 4, 0, tzinfo=timezone.utc),
            policy=policy,
        )
        self.assertEqual(recent["reason_code"], "USER_RECENTLY_ACTIVE")

    def test_proactive_prompt_uses_time_history_memory_and_recent_messages(self):
        state = {
            "enabled": True,
            "last_user_at": "2026-08-07T05:00:00+08:00",
            "proactive_count_today": 0,
            "recent_history": [
                {"role": "user", "text": "昨晚那个话题还没说完", "timestamp": "2026-08-07T05:00:00+08:00"},
            ],
            "conversation_summary": "正在讨论最近的工作压力",
            "memory_text": "用户明确喜欢红茶",
            "recent_proactive_messages": [{"text": "昨天问过你忙不忙"}],
            "proactive_window_date": "",
        }
        prompt = build_proactive_prompt(
            state,
            now=datetime(2026, 8, 7, 8, 30, tzinfo=timezone(timedelta(hours=8))),
            seed="test",
        )
        self.assertIn("当前时段：早上", prompt)
        self.assertIn("时段开启", prompt)
        self.assertIn("昨晚那个话题还没说完", prompt)
        self.assertIn("用户明确喜欢红茶", prompt)
        self.assertIn("昨天问过你忙不忙", prompt)

    def test_proactive_angle_follows_open_question_after_first_window(self):
        state = {
            "enabled": True,
            "last_user_at": "2026-08-07T05:00:00+08:00",
            "proactive_count_today": 1,
            "proactive_window_date": "2026-08-07",
            "last_proactive_text": "刚才问过你今天忙不忙",
            "recent_history": [{"role": "user", "text": "那个问题怎么办？"}],
        }
        angle = choose_proactive_angle(
            state,
            now=datetime(2026, 8, 7, 10, 0, tzinfo=timezone(timedelta(hours=8))),
            seed="test",
        )
        self.assertIn("疑问", angle)

    def test_proactive_response_normalizes_json_and_legacy_message(self):
        result = normalize_proactive_response(
            json.dumps({"action": "send", "stages": [{"bubbles": ["你好", "在吗"]}]})
        )
        self.assertEqual(result["action"], "send")
        self.assertEqual(result["stages"][0]["bubbles"], ["你好", "在吗"])

        legacy = normalize_proactive_response({"action": "send", "message": "我来冒个泡"})
        self.assertEqual(legacy, {"action": "send", "stages": [{"bubbles": ["我来冒个泡"]}]})

        fenced = normalize_proactive_response("```json\n{\"action\": \"skip\"}\n```")
        self.assertEqual(fenced, {"action": "skip", "reason_code": "NO_NATURAL_TOPIC"})

    def test_invalid_proactive_response_becomes_skip(self):
        self.assertEqual(
            normalize_proactive_response({"action": "send", "stages": []}),
            {"action": "skip", "reason_code": "INVALID_OUTPUT"},
        )

    def test_followup_cycle_waits_then_claims_and_commits_next_stage(self):
        policy = FollowupPolicy(intervals_minutes=((1, 1), (1, 1)))
        started = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        state = start_followup_cycle(
            [{"bubbles": ["第一句"]}, {"bubbles": ["还在吗"]}, {"bubbles": ["我先去忙啦"]}],
            started_at=started,
            cycle_id="demo",
            policy=policy,
        )
        due = datetime.fromisoformat(state["next_stage_at"])
        not_due = poll_followup(state, now=due - timedelta(seconds=1), policy=policy)
        self.assertEqual(not_due["status"], "not_due")

        claimed = poll_followup(state, now=due, policy=policy)
        self.assertEqual(claimed["status"], "claimed")
        self.assertEqual(claimed["text"], "还在吗")
        committed = commit_followup(
            claimed["state"], claimed["claim_id"], delivered=True, now=due, policy=policy
        )
        self.assertEqual(committed["status"], "committed")
        self.assertEqual(committed["state"]["stage_index"], 1)

    def test_followup_cycle_missed_stage_is_not_caught_up(self):
        policy = FollowupPolicy(intervals_minutes=((1, 1),), grace_minutes=5)
        started = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        state = start_followup_cycle(
            ["第一句", "后续一句"], started_at=started, policy=policy
        )
        due = datetime.fromisoformat(state["next_stage_at"])
        result = poll_followup(state, now=due + timedelta(minutes=6), policy=policy)
        self.assertEqual(result["status"], "missed")
        self.assertEqual(result["state"]["next_stage_at"], "")

    def test_user_reply_stops_pending_followup(self):
        state = start_followup_cycle(
            ["第一句", "后续一句"],
            started_at=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
            policy=FollowupPolicy(intervals_minutes=((1, 1),)),
        )
        stopped = stop_followup(state)
        self.assertEqual(stopped["status"], "waiting_for_user")
        self.assertEqual(stopped["stop_reason"], "USER_REPLIED")
        self.assertEqual(stopped["next_stage_at"], "")

    def test_hermes_sender_sends_bubbles_as_independent_calls(self):
        sent = []

        async def send_one(text):
            sent.append(text)
            return "message-id"

        result = asyncio.run(
            send_human_reply(
                "第一句。第二句！",
                send_one,
                is_cancelled=lambda: False,
            )
        )
        self.assertEqual(result["status"], "sent")
        self.assertEqual(sent, ["第一句。", "第二句！"])

    def test_hermes_gateway_bridge_cancels_before_next_bubble(self):
        sent = []
        interrupted = asyncio.Event()

        async def send_one(text):
            sent.append(text)
            interrupted.set()
            return "message-id"

        result = asyncio.run(
            send_with_bubbles(
                "第一句。第二句！第三句？",
                send_one,
                interrupt_event=interrupted,
            )
        )
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(sent, ["第一句。"])

    def test_gateway_patch_reads_proactive_note_before_user_activity_update(self):
        self.assertLess(
            RUN_ANCHOR_NEW.index("_hp_note = _hp_reply_note()"),
            RUN_ANCHOR_NEW.index("_hp_update_user_activity(history)"),
        )

    def test_gateway_patch_repairs_existing_old_call_order(self):
        with TemporaryDirectory() as directory:
            run_file = Path(directory) / "gateway" / "run.py"
            run_file.parent.mkdir(parents=True)
            run_file.write_text(
                RUN_PATCH_MARKER
                + "\n"
                + "                _hp_update_user_activity()\n"
                + "                _hp_ctx = _hp_time_ctx(history)\n"
                + "                _hp_note = _hp_reply_note()\n",
                encoding="utf-8",
            )
            _apply_run_patch(Path(directory), dry_run=False)
            repaired = run_file.read_text(encoding="utf-8")
            self.assertLess(
                repaired.index("_hp_note = _hp_reply_note()"),
                repaired.index("_hp_update_user_activity"),
            )
            self.assertIn("_hp_update_user_activity(history)", repaired)

    def test_gateway_patch_installs_both_bridges_and_bubble_send_hook(self):
        with TemporaryDirectory() as directory:
            site = Path(directory)
            platforms = site / "gateway" / "platforms"
            platforms.mkdir(parents=True)
            base_file = platforms / "base.py"
            base_file.write_text(
                "    @staticmethod\n"
                "    def _merge_caption(existing_text: Optional[str], new_text: str) -> str:\n"
                "        return new_text\n\n"
                "                    result = await self._send_with_retry(\n"
                "                        chat_id=event.source.chat_id,\n"
                "                        content=text_content,\n"
                "                        reply_to=_reply_anchor,\n"
                "                        metadata=_final_thread_metadata,\n"
                "                    )\n",
                encoding="utf-8",
            )

            bubble_dest = platforms / "base_bubble_bridge.py"
            humanpulse_dest = platforms / "humanpulse_bridge.py"
            _copy_bridge(BUBBLE_BRIDGE_SOURCE, bubble_dest, dry_run=False)
            _copy_bridge(BRIDGE_SOURCE, humanpulse_dest, dry_run=False)
            _apply_base_patch(site, dry_run=False)

            patched = base_file.read_text(encoding="utf-8")
            self.assertEqual(bubble_dest.read_bytes(), BUBBLE_BRIDGE_SOURCE.read_bytes())
            self.assertEqual(humanpulse_dest.read_bytes(), BRIDGE_SOURCE.read_bytes())
            self.assertIn(BASE_PATCH_MARKER, patched)
            self.assertIn(BASE_SEND_ANCHOR_NEW, patched)

    def test_record_proactive_sent_keeps_sent_text_as_followup_stage_zero(self):
        with TemporaryDirectory() as directory:
            old_path = humanpulse_state.DEFAULT_STATE_FILE
            humanpulse_state.DEFAULT_STATE_FILE = Path(directory) / "state.json"
            humanpulse_bridge._runtime = humanpulse_runtime
            humanpulse_bridge._state_mod = humanpulse_state
            try:
                humanpulse_state.reset_state()
                humanpulse_bridge.record_proactive_sent("刚刚突然想到你")
                state = humanpulse_state.load_state()
                self.assertEqual(state["followup"]["stages"][0], "刚刚突然想到你")
                self.assertEqual(len(state["followup"]["stages"]), 4)
            finally:
                humanpulse_state.DEFAULT_STATE_FILE = old_path

    def test_proactive_reply_note_is_available_before_activity_update(self):
        with TemporaryDirectory() as directory:
            old_path = humanpulse_state.DEFAULT_STATE_FILE
            humanpulse_state.DEFAULT_STATE_FILE = Path(directory) / "state.json"
            humanpulse_bridge._runtime = humanpulse_runtime
            humanpulse_bridge._state_mod = humanpulse_state
            try:
                humanpulse_state.reset_state()
                humanpulse_bridge.record_proactive_sent("刚刚突然想到你")
                self.assertIn("HumanPulse", humanpulse_bridge.build_proactive_reply_note())
                humanpulse_bridge.update_user_activity()
                self.assertEqual(humanpulse_bridge.build_proactive_reply_note(), "")
            finally:
                humanpulse_state.DEFAULT_STATE_FILE = old_path

    def test_user_activity_persists_compact_history_for_proactive_context(self):
        with TemporaryDirectory() as directory:
            old_path = humanpulse_state.DEFAULT_STATE_FILE
            humanpulse_state.DEFAULT_STATE_FILE = Path(directory) / "state.json"
            humanpulse_bridge._runtime = humanpulse_runtime
            humanpulse_bridge._state_mod = humanpulse_state
            try:
                humanpulse_state.reset_state()
                humanpulse_bridge.update_user_activity(
                    [{"role": "user", "content": "我刚才还想继续聊那个话题"}]
                )
                state = humanpulse_state.load_state()
                self.assertEqual(state["recent_history"][0]["text"], "我刚才还想继续聊那个话题")
            finally:
                humanpulse_state.DEFAULT_STATE_FILE = old_path

    def test_state_repairs_nested_followup_and_cron_report_pollution(self):
        with TemporaryDirectory() as directory:
            old_path = humanpulse_state.DEFAULT_STATE_FILE
            humanpulse_state.DEFAULT_STATE_FILE = Path(directory) / "state.json"
            try:
                humanpulse_state.DEFAULT_STATE_FILE.write_text(
                    json.dumps(
                        {
                            "last_proactive_at": "2026-08-07T10:00:00+00:00",
                            "last_proactive_text": "# Cron report\n" + "x" * 1400,
                            "followup": {
                                "status": "committed",
                                "state": {"status": "active", "stage_index": 1},
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                repaired = humanpulse_state.load_state()
                self.assertEqual(repaired["last_proactive_text"], "")
                self.assertEqual(repaired["last_proactive_at"], "")
                self.assertNotIn("state", repaired["followup"])
                self.assertEqual(repaired["followup"]["status"], "idle")
                persisted = json.loads(humanpulse_state.DEFAULT_STATE_FILE.read_text(encoding="utf-8"))
                self.assertNotIn("state", persisted["followup"])
            finally:
                humanpulse_state.DEFAULT_STATE_FILE = old_path

    def test_record_proactive_sent_rejects_cron_report_instead_of_seeding_followup(self):
        with TemporaryDirectory() as directory:
            old_path = humanpulse_state.DEFAULT_STATE_FILE
            humanpulse_state.DEFAULT_STATE_FILE = Path(directory) / "state.json"
            humanpulse_bridge._runtime = humanpulse_runtime
            humanpulse_bridge._state_mod = humanpulse_state
            try:
                humanpulse_state.reset_state()
                humanpulse_bridge.record_proactive_sent("## Script Output\n" + "x" * 1400)
                state = humanpulse_state.load_state()
                self.assertEqual(state["last_proactive_text"], "")
                self.assertEqual(state["proactive_count_today"], 0)
                self.assertEqual(state["followup"]["status"], "idle")
            finally:
                humanpulse_state.DEFAULT_STATE_FILE = old_path

    def test_followup_tick_persists_inner_followup_state(self):
        with TemporaryDirectory() as directory:
            old_path = humanpulse_state.DEFAULT_STATE_FILE
            humanpulse_state.DEFAULT_STATE_FILE = Path(directory) / "state.json"
            humanpulse_bridge._runtime = humanpulse_runtime
            humanpulse_bridge._state_mod = humanpulse_state
            try:
                humanpulse_state.reset_state()
                humanpulse_bridge.record_proactive_sent("主动消息")
                state = humanpulse_state.load_state()
                state["followup"]["next_stage_at"] = (
                    datetime.now(timezone.utc) - timedelta(minutes=1)
                ).isoformat()
                humanpulse_state.save_state(state)
                self.assertTrue(humanpulse_bridge.followup_tick())
                followup = humanpulse_state.load_state()["followup"]
                self.assertNotIn("state", followup)
                self.assertEqual(followup["stage_index"], 1)
            finally:
                humanpulse_state.DEFAULT_STATE_FILE = old_path


if __name__ == "__main__":
    unittest.main()
