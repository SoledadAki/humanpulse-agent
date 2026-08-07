import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone

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
)
from skills.companion_agent.adapters.hermes.send_bubbles import send_human_reply
from skills.companion_agent.adapters.hermes.gateway_bubble_bridge import send_with_bubbles


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


if __name__ == "__main__":
    unittest.main()
