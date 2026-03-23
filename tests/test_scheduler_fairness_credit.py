from unittest.mock import patch
import unittest
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

import helper
import scheduler


def _run_solver_with_overrides(*, days, surgeons, preassignments, availability, config):
    with patch.object(helper, "get_global_config", return_value=config), \
         patch.object(helper, "get_max_calls_config", return_value={"1": 10, "2": 10, "3": 10, "4": 10}), \
         patch.object(helper, "get_availability_requests", return_value=availability), \
         patch.object(helper, "get_team_day_prefs", return_value={}):
        return scheduler.solve_schedule_or_tools(
            days=days,
            surgeons=surgeons,
            prev_schedule=None,
            public_holidays=set(),
            preassignments=preassignments,
            time_limit_seconds=10,
            allow_empty=True,
            horizon_prior_counts=None,
        )


def _base_config():
    return {
        "fairness_weight": "1000",
        "fairness_cap_uses_credit": "0",
        "enable_fairness_hard_cap": "1",
        "fairness_hard_cap_range": "5",  # should still enforce <=1 by policy
        "gamma_no_call": "10",
        "gamma_unavail_prev": "5",
        "gamma_1B": "1",
        "gamma_balance": "100",
        "no_call_hard": "1",
        "pre_unavail_mode": "soft",
        "gamma_spacing": "10",
        "spacing_threshold": "7",
        "max_calls_level1": "10",
        "gamma_weekend_balance": "0",
        "gamma_consec_weekend": "0",
        "gamma_weekend_team_diversity": "0",
        "gamma_unavail_credit": "0",
        "unavail_credit_days": "7",
        "max_weekend_calls": "10",
        "min_calls_nlth": "0",
        "gamma_team_pref": "0",
        "gamma_2b_usage": "0",
        "enable_force_1B_weekend": "0",
        "enable_level2_supervision": "1",
        "enable_group4_2B3_ban": "0",
        "enable_max_2B_group4": "0",
        "enable_max_calls_level1": "0",
        "enable_nlth_rules": "0",
        "enable_weekend_consecutive_penalty": "0",
        "enable_weekend_balance": "0",
        "enable_weekend_team_diversity_enable": "0",
        "enable_team_day_prefs": "0",
        "enable_fairness_l2_groups": "1",
        "enable_availability_unavail_prev_penalty": "0",
        "enable_availability_nocall_penalty": "0",
        "enable_spacing_penalty": "0",
        "enable_fairness_diff_all": "0",
        "enable_deviation_sum": "0",
        "enable_unavail_credit": "0",
        "solver_debug": "0",
        "enable_two_pass_credit_priority": "1",
    }


class SchedulerFairnessCreditTests(unittest.TestCase):
    def test_l2_group_cap_is_enforced_to_one_even_when_configured_higher(self):
        days = ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05"]
        surgeons = [
            {"id": 1, "name": "L2G1", "call_levels": "2A", "nlth": False, "team": "Team 1"},
            {"id": 2, "name": "L2G2", "call_levels": "2A,2B", "nlth": False, "team": "Team 1"},
            {"id": 3, "name": "L2G3", "call_levels": "2B", "nlth": False, "team": "Team 2"},
        ]
        preassignments = {
            days[0]: {"2A": 1, "2B": 3},
            days[3]: {"2A": 1, "2B": 3},
        }
        # Force subgroup-2 surgeon to zero calls so L2 range would be 2 unless cap is disabled.
        availability = {2: [{"date": d, "request_type": "unavailable"} for d in days]}

        cfg = _base_config()
        sched_cap_on, _ = _run_solver_with_overrides(
            days=days,
            surgeons=surgeons,
            preassignments=preassignments,
            availability=availability,
            config=cfg,
        )
        self.assertIsInstance(sched_cap_on, dict)
        self.assertIn("errors", sched_cap_on)
        self.assertTrue(any("cap ≤ 1" in msg for msg in sched_cap_on.get("errors", [])))

        cfg["enable_fairness_hard_cap"] = "0"
        sched_cap_off, _ = _run_solver_with_overrides(
            days=days,
            surgeons=surgeons,
            preassignments=preassignments,
            availability=availability,
            config=cfg,
        )
        self.assertIsInstance(sched_cap_off, dict)
        self.assertNotIn("errors", sched_cap_off)

    def test_credit_window_is_configurable_every_k_days(self):
        self.assertEqual(scheduler._credit_calls_from_unavailability(0, 7), 0)
        self.assertEqual(scheduler._credit_calls_from_unavailability(6, 7), 0)
        self.assertEqual(scheduler._credit_calls_from_unavailability(7, 7), 1)
        self.assertEqual(scheduler._credit_calls_from_unavailability(15, 7), 2)
        self.assertEqual(scheduler._credit_calls_from_unavailability(7, 8), 0)
        self.assertEqual(scheduler._credit_calls_from_unavailability(8, 8), 1)

    def test_effective_fairness_cap_is_always_at_most_one(self):
        self.assertEqual(scheduler._effective_fairness_cap(0), 0)
        self.assertEqual(scheduler._effective_fairness_cap(1), 1)
        self.assertEqual(scheduler._effective_fairness_cap(2), 1)
        self.assertEqual(scheduler._effective_fairness_cap(9), 1)

    def test_manual_more_less_credit_direction(self):
        self.assertEqual(
            scheduler._manual_credit_calls_from_surgeon(
                {"manual_less_calls_credit": 3, "manual_more_calls_credit": 1}
            ),
            2,
        )
        self.assertEqual(
            scheduler._manual_credit_calls_from_surgeon(
                {"manual_less_calls_credit": 0, "manual_more_calls_credit": 2}
            ),
            -2,
        )
        self.assertEqual(
            scheduler._manual_credit_calls_from_surgeon(
                {"manual_less_calls_credit": -5, "manual_more_calls_credit": -4}
            ),
            0,
        )

    def test_two_pass_credit_priority_gives_positive_credit_more_calls_when_feasible(self):
        days = ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05"]
        surgeons = [
            {"id": 1, "name": "Credited", "call_levels": "1A,1B", "nlth": False, "team": "Team 1", "manual_call_credit": 1},
            {"id": 2, "name": "PeerA", "call_levels": "1A,1B", "nlth": False, "team": "Team 1", "manual_call_credit": 0},
            {"id": 3, "name": "PeerB", "call_levels": "1A,1B", "nlth": False, "team": "Team 1", "manual_call_credit": 0},
        ]
        cfg = _base_config()
        cfg["enable_fairness_hard_cap"] = "0"
        cfg["enable_two_pass_credit_priority"] = "1"

        sched, _ = _run_solver_with_overrides(
            days=days,
            surgeons=surgeons,
            preassignments={},
            availability={},
            config=cfg,
        )
        self.assertIsInstance(sched, dict)
        self.assertNotIn("errors", sched)

        counts = {s["name"]: 0 for s in surgeons}
        for _, assigns in sched.items():
            for lvl in ("1A", "1B"):
                name = assigns.get(lvl)
                if name in counts:
                    counts[name] += 1

        # Under hard 3-day gap constraints, exact totals vary by feasibility,
        # but credited surgeon should still end above zero-credit peers.
        self.assertGreaterEqual(
            counts["Credited"],
            max(counts["PeerA"], counts["PeerB"]) + 1
        )


class HalfYearCohortSummaryTests(unittest.TestCase):
    def test_half_year_window_months(self):
        self.assertEqual(helper.get_half_year_months_before(1), [])
        self.assertEqual(helper.get_half_year_months_before(6), [1, 2, 3, 4, 5])
        self.assertEqual(helper.get_half_year_months_before(7), [])
        self.assertEqual(helper.get_half_year_months_before(12), [7, 8, 9, 10, 11])

    def test_cohort_summary_uses_prior_published_months_only(self):
        surgeons = [
            {"id": 1, "name": "Alice", "call_levels": "1A,1B", "nlth": False, "team": "Team 1"},
            {"id": 2, "name": "Bob", "call_levels": "1A,1B", "nlth": False, "team": "Team 1"},
            {"id": 3, "name": "Carol", "call_levels": "2A", "nlth": False, "team": "Team 1"},
            {"id": 4, "name": "Dan", "call_levels": "2A,2B", "nlth": False, "team": "Team 1"},
            {"id": 5, "name": "Eve", "call_levels": "2B", "nlth": False, "team": "Team 2"},
            {"id": 6, "name": "Frank", "call_levels": "2B,3", "nlth": False, "team": "Team 2"},
            {"id": 7, "name": "Gina", "call_levels": "3", "nlth": False, "team": "Team 3"},
            {"id": 8, "name": "Hank", "call_levels": "4", "nlth": False, "team": "Team 4"},
        ]
        month7 = {
            "2026-07-01": {"1A": "Alice", "1B": "Bob", "2A": "Carol", "2B": "Eve", "3": "Gina", "4": "Hank"}
        }
        month8 = {
            "2026-08-01": {"1A": "Bob", "1B": "Alice", "2A": "Dan", "2B": "Frank", "3": "Gina", "4": "Hank"}
        }

        def fake_published(year, month):
            if year != 2026:
                return None
            if month == 7:
                return month7
            if month == 8:
                return month8
            return None

        with patch.object(helper, "get_published_schedule_version", side_effect=fake_published):
            summary = helper.build_half_year_cohort_summary(2026, 9, surgeons=surgeons)

        self.assertEqual(summary["months_included"], [7, 8])
        g1 = next(g for g in summary["groups"] if g["key"] == "g1")
        self.assertEqual(g1["average"], 2.0)
        self.assertTrue(all(m["status"] == "at" for m in g1["members"]))

        g3 = next(g for g in summary["groups"] if g["key"] == "g3")
        by_name = {m["name"]: m for m in g3["members"]}
        self.assertEqual(by_name["Frank"]["count"], 1)
        self.assertEqual(by_name["Gina"]["count"], 2)
        self.assertEqual(by_name["Frank"]["status"], "below")
        self.assertEqual(by_name["Gina"]["status"], "above")


if __name__ == "__main__":
    unittest.main()
