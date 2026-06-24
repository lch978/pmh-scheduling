from unittest.mock import patch
import unittest
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

import helper
import scheduler


def _run_solver_with_overrides(*, days, surgeons, preassignments, availability, config, max_calls_config=None):
    mc = max_calls_config if max_calls_config is not None else {"1": 10, "2": 10, "3": 10, "4": 10, "l2g1_1a": 4}
    with patch.object(helper, "get_global_config", return_value=config), \
         patch.object(helper, "get_max_calls_config", return_value=mc), \
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
        "fairness_fallback_policy": "auto_relax",
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
        "enable_two_pass_fairness_priority": "1",
        "enable_l2g1_primary_calls": "0",
        "enable_l2g1_primary_2a_same_day_penalty": "1",
        "gamma_l2g1_primary_2a_same_day": "30",
    }


class SchedulerFairnessCreditTests(unittest.TestCase):
    def test_l2g1_primary_monthly_cap_conflicts_with_two_preassigned_1a(self):
        """L2G1 on 1A when enabled: 1A per month capped by l2g1_1a."""
        days = ["2026-06-01", "2026-06-02"]
        surgeons = [
            {"id": 1, "name": "PrimaryAB", "call_levels": "1A,1B", "nlth": False, "team": "Team 1"},
            {"id": 2, "name": "L2G1Only", "call_levels": "2A", "nlth": False, "team": "Team 2"},
            {"id": 3, "name": "L2G2", "call_levels": "2A,2B", "nlth": False, "team": "Team 3"},
            {"id": 4, "name": "Super2B", "call_levels": "2B", "nlth": False, "team": "Team 4"},
            {"id": 5, "name": "Third", "call_levels": "3", "nlth": False, "team": "Team 1"},
            {"id": 6, "name": "Fourth", "call_levels": "4", "nlth": False, "team": "Team 2"},
        ]
        cfg = _base_config()
        cfg["enable_l2g1_primary_calls"] = "1"
        cfg["enable_fairness_hard_cap"] = "0"
        cfg["enable_force_1B_weekend"] = "0"
        cfg["enable_l2g1_primary_2a_same_day_penalty"] = "0"
        preassignments = {
            days[0]: {"1A": 2},
            days[1]: {"1A": 2},
        }
        sched, _ = _run_solver_with_overrides(
            days=days,
            surgeons=surgeons,
            preassignments=preassignments,
            availability={},
            config=cfg,
            max_calls_config={"1": 10, "2": 10, "3": 10, "4": 10, "l2g1_1a": 1},
        )
        self.assertIsInstance(sched, dict)
        self.assertIn("errors", sched)

    def test_l2g1_primary_single_1a_preassignment_feasible(self):
        days = ["2026-06-01"]
        surgeons = [
            {"id": 1, "name": "PrimaryAB", "call_levels": "1A,1B", "nlth": False, "team": "Team 1"},
            {"id": 2, "name": "L2G1Only", "call_levels": "2A", "nlth": False, "team": "Team 2"},
            {"id": 3, "name": "L2G2", "call_levels": "2A,2B", "nlth": False, "team": "Team 3"},
            {"id": 4, "name": "Super2B", "call_levels": "2B", "nlth": False, "team": "Team 4"},
            {"id": 5, "name": "Third", "call_levels": "3", "nlth": False, "team": "Team 1"},
            {"id": 6, "name": "Fourth", "call_levels": "4", "nlth": False, "team": "Team 2"},
        ]
        cfg = _base_config()
        cfg["enable_l2g1_primary_calls"] = "1"
        cfg["enable_fairness_hard_cap"] = "0"
        cfg["enable_force_1B_weekend"] = "0"
        cfg["enable_l2g1_primary_2a_same_day_penalty"] = "0"
        preassignments = {days[0]: {"1A": 2}}
        sched, _ = _run_solver_with_overrides(
            days=days,
            surgeons=surgeons,
            preassignments=preassignments,
            availability={},
            config=cfg,
            max_calls_config={"1": 10, "2": 10, "3": 10, "4": 10, "l2g1_1a": 1},
        )
        self.assertIsInstance(sched, dict)
        self.assertNotIn("errors", sched)
        self.assertEqual(sched.get(days[0], {}).get("1A"), "L2G1Only")

    def test_l2g1_on_1a_forbids_urology_only_on_1b(self):
        days = ["2026-06-07"]
        surgeons = [
            {"id": 1, "name": "PrimaryAB", "call_levels": "1A,1B", "nlth": False, "team": "Team 1"},
            {"id": 2, "name": "L2G1A", "call_levels": "2A", "nlth": False, "team": "Team 2"},
            {"id": 3, "name": "L2G1B", "call_levels": "2A", "nlth": False, "team": "Team 3"},
            {"id": 4, "name": "UroOnly", "call_levels": "Urology", "nlth": False, "team": "Team 4"},
            {"id": 5, "name": "Super2B", "call_levels": "2B", "nlth": False, "team": "Team 1"},
            {"id": 6, "name": "Third", "call_levels": "3", "nlth": False, "team": "Team 2"},
        ]
        cfg = _base_config()
        cfg["enable_l2g1_primary_calls"] = "1"
        cfg["enable_fairness_hard_cap"] = "0"
        cfg["enable_force_1B_weekend"] = "0"
        cfg["enable_l2g1_primary_2a_same_day_penalty"] = "0"
        preassignments = {days[0]: {"1A": 2, "1B": 4, "Urology": 4}}
        sched, _ = _run_solver_with_overrides(
            days=days,
            surgeons=surgeons,
            preassignments=preassignments,
            availability={},
            config=cfg,
            max_calls_config={"1": 10, "2": 10, "3": 10, "4": 10, "l2g1_1a": 4, "urology_min": 0, "urology_max": 10},
        )
        self.assertIsInstance(sched, dict)
        self.assertIn("errors", sched)

    def test_l2g1_on_1a_prefers_another_l2g1_on_2a(self):
        days = ["2026-06-01"]
        surgeons = [
            {"id": 1, "name": "PrimaryAB", "call_levels": "1A,1B", "nlth": False, "team": "Team 1"},
            {"id": 2, "name": "L2G1A", "call_levels": "2A", "nlth": False, "team": "Team 2"},
            {"id": 3, "name": "L2G1B", "call_levels": "2A", "nlth": False, "team": "Team 3"},
            {"id": 4, "name": "L2G2", "call_levels": "2A,2B", "nlth": False, "team": "Team 4"},
            {"id": 5, "name": "Super2B", "call_levels": "2B", "nlth": False, "team": "Team 1"},
            {"id": 6, "name": "Third", "call_levels": "3", "nlth": False, "team": "Team 2"},
        ]
        cfg = _base_config()
        cfg["enable_l2g1_primary_calls"] = "1"
        cfg["enable_fairness_hard_cap"] = "0"
        cfg["enable_force_1B_weekend"] = "0"
        cfg["enable_l2g1_primary_2a_same_day_penalty"] = "1"
        cfg["gamma_l2g1_primary_2a_same_day"] = "10000"
        preassignments = {days[0]: {"1A": 2}}
        sched, _ = _run_solver_with_overrides(
            days=days,
            surgeons=surgeons,
            preassignments=preassignments,
            availability={},
            config=cfg,
            max_calls_config={"1": 10, "2": 10, "3": 10, "4": 10, "l2g1_1a": 4},
        )
        self.assertIsInstance(sched, dict)
        self.assertNotIn("errors", sched)
        day_assign = sched.get(days[0], {})
        self.assertEqual(day_assign.get("1A"), "L2G1A")
        self.assertIn(day_assign.get("2A"), ("L2G1B", "L2G1A"))

    def test_l2_group_cap_auto_relax_returns_schedule_when_strict_is_infeasible(self):
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
        self.assertNotIn("errors", sched_cap_on)

    def test_l2_group_cap_no_fallback_surfaces_infeasible_error(self):
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
        availability = {2: [{"date": d, "request_type": "unavailable"} for d in days]}

        cfg = _base_config()
        cfg["fairness_fallback_policy"] = "no_fallback"
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

    def test_unified_fairness_credit_sums_unavailability_and_manual(self):
        surgeon = {"manual_call_credit": -2}
        # unavailability credit=1, manual solver-credit=+2 (from -2 UI) => total +3
        self.assertEqual(scheduler._unified_fairness_credit_calls(1, surgeon), 3)

    def test_unavailability_credit_component_is_computed_per_window(self):
        surgeons = [{"id": 1}, {"id": 2}]
        availability = {
            1: [
                {"date": "2026-03-01", "request_type": "unavailable"},
                {"date": "2026-03-02", "request_type": "study_leave"},
                {"date": "2026-03-03", "request_type": "unavailable"},
            ],
            2: [
                {"date": "2026-03-01", "request_type": "no_call"},
                {"date": "2026-03-02", "request_type": "unavailable"},
            ],
        }
        days = ["2026-03-01", "2026-03-02", "2026-03-03"]
        credits = helper.compute_unavailability_credit_by_surgeon(
            surgeons=surgeons,
            availability=availability,
            days=days,
            unavail_credit_days=2,
        )
        self.assertEqual(credits[1], 1)  # 3 unavailable/study days => 1 credit at window=2
        self.assertEqual(credits[2], 0)  # only unavailable/study counted; no_call ignored

    def test_manual_credit_changes_do_not_affect_solver_when_fairness_credit_disabled(self):
        days = ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05"]
        surgeons_a = [
            {"id": 1, "name": "S1", "call_levels": "1A,1B", "nlth": False, "team": "Team 1", "manual_call_credit": 0},
            {"id": 2, "name": "S2", "call_levels": "1A,1B", "nlth": False, "team": "Team 1", "manual_call_credit": 0},
            {"id": 3, "name": "S3", "call_levels": "1A,1B", "nlth": False, "team": "Team 1", "manual_call_credit": 0},
        ]
        surgeons_b = [
            {"id": 1, "name": "S1", "call_levels": "1A,1B", "nlth": False, "team": "Team 1", "manual_call_credit": 3},
            {"id": 2, "name": "S2", "call_levels": "1A,1B", "nlth": False, "team": "Team 1", "manual_call_credit": -2},
            {"id": 3, "name": "S3", "call_levels": "1A,1B", "nlth": False, "team": "Team 1", "manual_call_credit": 1},
        ]
        cfg = _base_config()
        cfg["enable_fairness_hard_cap"] = "0"
        cfg["fairness_cap_uses_credit"] = "0"

        sched_a, _ = _run_solver_with_overrides(
            days=days,
            surgeons=surgeons_a,
            preassignments={},
            availability={},
            config=cfg,
        )
        sched_b, _ = _run_solver_with_overrides(
            days=days,
            surgeons=surgeons_b,
            preassignments={},
            availability={},
            config=cfg,
        )
        self.assertNotIn("errors", sched_a)
        self.assertNotIn("errors", sched_b)
        self.assertEqual(sched_a, sched_b)

    def test_allow_empty_prefers_zero_non_2b_empties_when_feasible(self):
        days = ["2026-06-01"]
        surgeons = [
            {"id": 1, "name": "L1A", "call_levels": "1A,1B", "nlth": False, "team": "Team 1"},
            {"id": 2, "name": "L1B", "call_levels": "1A,1B", "nlth": False, "team": "Team 2"},
            {"id": 3, "name": "L2AGroup2", "call_levels": "2A,2B", "nlth": False, "team": "Team 3"},
            {"id": 4, "name": "L2BSuper", "call_levels": "2B", "nlth": False, "team": "Team 4"},
            {"id": 5, "name": "L3", "call_levels": "3", "nlth": False, "team": "Team 1"},
            {"id": 6, "name": "L4", "call_levels": "4", "nlth": False, "team": "Team 2"},
        ]
        cfg = _base_config()
        cfg["enable_fairness_hard_cap"] = "0"

        sched, _ = _run_solver_with_overrides(
            days=days,
            surgeons=surgeons,
            preassignments={},
            availability={},
            config=cfg,
        )
        self.assertIsInstance(sched, dict)
        self.assertNotIn("errors", sched)

        assignments = sched[days[0]]
        for lvl in ["1A", "1B", "2A", "3", "4"]:
            self.assertIsNotNone(assignments.get(lvl))
        # Group-2 surgeon on 2A forces 2B empty, and this should not count against non-2B empties.
        self.assertIsNone(assignments.get("2B"))

    def test_allow_empty_uses_empty_slot_only_when_required_for_feasibility(self):
        days = ["2026-06-01"]
        surgeons = [
            {"id": 1, "name": "L1A", "call_levels": "1A,1B", "nlth": False, "team": "Team 1"},
            {"id": 2, "name": "L1B", "call_levels": "1A,1B", "nlth": False, "team": "Team 2"},
            {"id": 3, "name": "L2AGroup2", "call_levels": "2A,2B", "nlth": False, "team": "Team 3"},
            {"id": 4, "name": "L2BSuper", "call_levels": "2B", "nlth": False, "team": "Team 4"},
            {"id": 5, "name": "L3", "call_levels": "3", "nlth": False, "team": "Team 1"},
        ]
        cfg = _base_config()
        cfg["enable_fairness_hard_cap"] = "0"

        sched, _ = _run_solver_with_overrides(
            days=days,
            surgeons=surgeons,
            preassignments={},
            availability={},
            config=cfg,
        )
        self.assertIsInstance(sched, dict)
        self.assertNotIn("errors", sched)

        assignments = sched[days[0]]
        self.assertIsNone(assignments.get("4"))
        for lvl in ["1A", "1B", "2A", "3"]:
            self.assertIsNotNone(assignments.get(lvl))

    def test_allow_empty_keeps_2b_exempt_from_empty_minimum(self):
        days = ["2026-06-01", "2026-06-02"]
        surgeons = [
            {"id": 1, "name": "L1A", "call_levels": "1A,1B", "nlth": False, "team": "Team 1"},
            {"id": 2, "name": "L1B", "call_levels": "1A,1B", "nlth": False, "team": "Team 2"},
            {"id": 3, "name": "L2AGroup2", "call_levels": "2A,2B", "nlth": False, "team": "Team 3"},
            {"id": 4, "name": "L2BSuper", "call_levels": "2B", "nlth": False, "team": "Team 4"},
            {"id": 5, "name": "L3", "call_levels": "3", "nlth": False, "team": "Team 1"},
            {"id": 6, "name": "L4", "call_levels": "4", "nlth": False, "team": "Team 2"},
        ]
        cfg = _base_config()
        cfg["enable_fairness_hard_cap"] = "0"
        preassignments = {days[0]: {"1A": 1, "1B": 2, "2A": 3, "3": 5, "4": 6}}

        sched, _ = _run_solver_with_overrides(
            days=days,
            surgeons=surgeons,
            preassignments=preassignments,
            availability={},
            config=cfg,
        )
        self.assertIsInstance(sched, dict)
        self.assertNotIn("errors", sched)

        assignments = sched[days[0]]
        for lvl in ["1A", "1B", "2A", "3", "4"]:
            self.assertIsNotNone(assignments.get(lvl))
        self.assertIsNone(assignments.get("2B"))


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

    def test_same_surgeon_on_1a_and_1b_counts_once_in_g1(self):
        """A surgeon shown in both 1A and 1B on the same published day counts as
        one Level-1 call for the G1 (1A+1B) cohort, not two."""
        surgeons = [
            {"id": 1, "name": "Alice", "call_levels": "1A,1B", "nlth": False, "team": "Team 1"},
            {"id": 2, "name": "Bob", "call_levels": "1A,1B", "nlth": False, "team": "Team 1"},
        ]
        # Day 1: Alice holds BOTH 1A and 1B (e.g. via the display mirror).
        # Day 2: Bob on 1A, Alice on 1B (distinct surgeons).
        month7 = {
            "2026-07-01": {"1A": "Alice", "1B": "Alice"},
            "2026-07-02": {"1A": "Bob", "1B": "Alice"},
        }

        def fake_published(year, month):
            if year == 2026 and month == 7:
                return month7
            return None

        with patch.object(helper, "get_published_schedule_version", side_effect=fake_published):
            summary = helper.build_half_year_cohort_summary(2026, 8, surgeons=surgeons)

        g1 = next(g for g in summary["groups"] if g["key"] == "g1")
        by_name = {m["name"]: m for m in g1["members"]}
        # Alice: day1 (1A+1B same day) -> 1, day2 (1B) -> 1 => 2 Level-1 days.
        self.assertEqual(by_name["Alice"]["count"], 2)
        # Bob: day2 (1A) -> 1.
        self.assertEqual(by_name["Bob"]["count"], 1)


if __name__ == "__main__":
    unittest.main()
