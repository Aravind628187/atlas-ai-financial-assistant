from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from google.api_core.exceptions import ResourceExhausted
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from app.ai.brain import handle_text_turn
from app.bot.handlers.common import get_or_create_user
from app.database import Base
from app.models import OnboardingStage, Preference, User, WatchlistItem


class DeterministicOnboardingTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def new_user(self, telegram_id: int = 100) -> User:
        user = User(telegram_id=telegram_id, onboarding_stage=OnboardingStage.ASKED_ROLE.value)
        self.db.add(user)
        self.db.flush()
        return user

    def test_common_role_responses_are_normalized(self):
        cases = {
            "student": "Student",
            "students": "Student",
            "Student": "Student",
            "analyst": "Analyst",
            "investor": "Investor",
            "college student": "Student",
            "engineering student": "Student",
            "retail investor": "Investor",
            "financial analyst": "Analyst",
            "founder": "Founder",
            "entrepreneur": "Founder",
            "finance professional": "Finance Professional",
            "financial professional": "Finance Professional",
        }
        for index, (answer, expected) in enumerate(cases.items(), 1):
            with self.subTest(answer=answer):
                user = self.new_user(1000 + index)
                reply = handle_text_turn(self.db, user, answer)
                self.assertEqual(user.role, expected)
                self.assertEqual(user.onboarding_stage, OnboardingStage.ASKED_INTERESTS.value)
                self.assertIn("Which companies, sectors, or markets do you follow?", reply)

    def test_unknown_role_is_sanitized_and_stored_without_ai(self):
        user = self.new_user()
        handle_text_turn(self.db, user, "  Macro <Strategist>\n  ")
        self.assertEqual(user.role, "Macro Strategist")
        preference = self.db.scalar(select(Preference).where(Preference.user_id == user.id, Preference.key == "role"))
        self.assertEqual(preference.value, "Macro Strategist")

    def test_required_questions_advance_in_order_and_simple_tickers_are_added(self):
        user = self.new_user()
        first = handle_text_turn(self.db, user, "student")
        self.assertIn("Which companies, sectors, or markets do you follow?", first)

        second = handle_text_turn(self.db, user, "Apple, msft and semiconductors")
        self.assertEqual(user.onboarding_stage, OnboardingStage.ASKED_MONITORING.value)
        self.assertIn("Anything you'd like me to monitor?", second)

        third = handle_text_turn(self.db, user, "$NVDA earnings")
        self.assertEqual(user.onboarding_stage, OnboardingStage.ASKED_INTELLIGENCE.value)
        self.assertIn("What type of intelligence matters most", third)

        fourth = handle_text_turn(self.db, user, "SEC filings and company research")
        self.assertEqual(user.onboarding_stage, OnboardingStage.ASKED_BRIEFING_TIME.value)
        self.assertIn("When would you like your daily briefing?", fourth)

        final = handle_text_turn(self.db, user, "around 7pm")
        self.assertEqual(user.onboarding_stage, OnboardingStage.DONE.value)
        self.assertEqual(user.briefing_hour_local, 19)
        self.assertIn("all set", final.lower())
        symbols = set(self.db.scalars(select(WatchlistItem.symbol).where(WatchlistItem.user_id == user.id)).all())
        self.assertEqual(symbols, {"AAPL", "MSFT", "NVDA"})

    def test_gemini_429_cannot_block_onboarding(self):
        user = self.new_user()
        with patch(
            "app.ai.gemini_client.gemini.generate_json",
            side_effect=ResourceExhausted("quota exhausted"),
        ) as generate:
            reply = handle_text_turn(self.db, user, "student")
        generate.assert_not_called()
        self.assertEqual(user.role, "Student")
        self.assertEqual(user.onboarding_stage, OnboardingStage.ASKED_INTERESTS.value)
        self.assertNotIn("rate-limited", reply.lower())

    def test_skip_advances_at_every_onboarding_step(self):
        user = self.new_user()
        transitions = (
            OnboardingStage.ASKED_INTERESTS.value,
            OnboardingStage.ASKED_MONITORING.value,
            OnboardingStage.ASKED_INTELLIGENCE.value,
            OnboardingStage.ASKED_BRIEFING_TIME.value,
            OnboardingStage.DONE.value,
        )
        for answer, expected_stage in zip(("skip", "not now", "later", "skip", "not now"), transitions):
            reply = handle_text_turn(self.db, user, answer)
            self.assertEqual(user.onboarding_stage, expected_stage)
            self.assertNotIn("rate-limited", reply.lower())
        self.assertIsNone(user.briefing_hour_local)

    def test_completed_user_does_not_restart_onboarding(self):
        user = User(telegram_id=200, onboarding_stage=OnboardingStage.DONE.value, role="Student")
        self.db.add(user)
        self.db.flush()
        reply = handle_text_turn(self.db, user, "Show my watchlist")
        self.assertEqual(user.onboarding_stage, OnboardingStage.DONE.value)
        self.assertNotIn("what best describes you", reply.lower())
        self.assertIn("watchlist", reply.lower())


class OnboardingPersistenceTests(unittest.TestCase):
    def test_new_user_state_persists_across_database_sessions(self):
        # Session/transaction behavior is backend-independent; production binds
        # this same flow to Neon through DATABASE_URL.
        with tempfile.TemporaryDirectory() as directory:
            engine = create_engine(f"sqlite+pysqlite:///{directory}/onboarding.db")
            Base.metadata.create_all(engine)
            with Session(engine) as first:
                user, created = get_or_create_user(first, 9_000_000_001, "new_user", "New")
                self.assertTrue(created)
                user.onboarding_stage = OnboardingStage.ASKED_ROLE.value
                handle_text_turn(first, user, "analyst")
                user_id = user.id
                first.commit()
            with Session(engine) as second:
                persisted = second.get(User, user_id)
                self.assertEqual(persisted.role, "Analyst")
                self.assertEqual(persisted.onboarding_stage, OnboardingStage.ASKED_INTERESTS.value)
            engine.dispose()

    def test_postgresql_schema_uses_bigint_for_telegram_identity(self):
        ddl = str(CreateTable(User.__table__).compile(dialect=postgresql.dialect()))
        self.assertIn("telegram_id BIGINT", ddl)


if __name__ == "__main__":
    unittest.main()
