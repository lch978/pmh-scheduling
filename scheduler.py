import datetime
from dateutil.parser import parse
from ortools.sat.python import cp_model
import itertools
import sys

# #region agent log
def _debug_log(location, message, data=None, hypothesis="H1"):
    try:
        import json as _json, time as _time, os as _os
        entry = {
            "sessionId": "e91910",
            "id": f"log_{int(_time.time()*1000)}_{location.replace(':', '_')}",
            "timestamp": int(_time.time() * 1000),
            "location": location,
            "message": message,
            "data": data or {},
            "hypothesisId": hypothesis,
            "runId": _os.environ.get("DEBUG_RUN_ID", "solver"),
        }
        with open("debug-e91910.log", "a", encoding="utf-8") as f:
            f.write(_json.dumps(entry, default=str) + "\n")
    except Exception:
        pass
# #endregion


############################# ################
# OR‑Tools Scheduling Function (with Availability Constraints)
#############################################

def _credit_calls_from_unavailability(unavailable_days: int, credit_window_days: int) -> int:
    """Return call-credit units: every `credit_window_days` unavailable days yields 1 credit."""
    if credit_window_days is None or credit_window_days < 1:
        credit_window_days = 7
    if unavailable_days <= 0:
        return 0
    return unavailable_days // credit_window_days

def _effective_fairness_cap(configured_cap: int) -> int:
    """Enforce cohort policy cap of <=1 while preserving stricter values (e.g., 0)."""
    return min(max(0, int(configured_cap)), 1)


def _manual_credit_calls_from_surgeon(surgeon: dict) -> int:
    """
    Manual call-credit bias:
    - UI signed credit: negative=fewer calls, positive=more calls
    - Solver credit semantics are inverse sign:
      positive solver credit => fewer expected calls.
    """
    # Preferred signed UI value, if available.
    if "manual_call_credit" in surgeon:
        try:
            signed_ui_credit = int(surgeon.get("manual_call_credit", 0) or 0)
        except Exception:
            signed_ui_credit = 0
        return -signed_ui_credit

    # Backward-compat fallback for split columns.
    try:
        less_credit = int(surgeon.get("manual_less_calls_credit", 0) or 0)
    except Exception:
        less_credit = 0
    try:
        more_credit = int(surgeon.get("manual_more_calls_credit", 0) or 0)
    except Exception:
        more_credit = 0
    less_credit = max(0, less_credit)
    more_credit = max(0, more_credit)
    return less_credit - more_credit


def _unified_fairness_credit_calls(unavailability_credit_calls: int, surgeon: dict) -> int:
    """Unified fairness credit = unavailability credit + manual call credit."""
    try:
        base = int(unavailability_credit_calls or 0)
    except Exception:
        base = 0
    return base + _manual_credit_calls_from_surgeon(surgeon or {})


def _horizon_prior_overall_calls(sid: int, horizon_prior_counts: dict | None) -> int:
    """Half-year prior total calls (1A/1B deduped per day + all other levels)."""
    if not isinstance(horizon_prior_counts, dict):
        return 0
    try:
        prior_level1_days = horizon_prior_counts.get("prior_level1_days")
        if isinstance(prior_level1_days, dict):
            prior_lvl1 = int(prior_level1_days.get(sid, 0))
        else:
            prior_levels = horizon_prior_counts.get("prior_levels", {})
            prior_lvl1 = int(prior_levels.get("1A", {}).get(sid, 0)) + int(prior_levels.get("1B", {}).get(sid, 0))
        prior_levels = horizon_prior_counts.get("prior_levels", {})
        prior_other = sum(
            int(prior_levels.get(lev, {}).get(sid, 0))
            for lev in ("Urology", "2A", "2B", "3", "4")
        )
        return prior_lvl1 + prior_other
    except Exception:
        return 0

def solve_schedule_or_tools(days, surgeons, prev_schedule=None, public_holidays=None, preassignments=None, time_limit_seconds: int = 30, allow_empty: bool = False, _diagnostic_run: bool = False, _relax_fairness_caps: bool = False, horizon_prior_counts=None):

    from helper import (
        get_max_calls_config,
        get_global_config,
        get_availability_requests,
        compute_unavailability_credit_by_surgeon,
        parse_call_levels,
        get_level2_group,
        get_level34_subgroup_ids,
        get_team_day_prefs
    )
    def _parse_req_date(raw):
        if isinstance(raw, datetime.date):
            return raw
        if isinstance(raw, str):
            try:
                return datetime.datetime.strptime(raw, "%Y-%m-%d").date()
            except Exception:
                return None
        return None

    model = cp_model.CpModel()
    constraint_mapping = {}
    diagnostics = []
    solver_mode_used = "relaxed_fairness_caps" if _relax_fairness_caps else "strict_fairness_caps"
    # Helper to wrap hard constraint additions.
    def add_named_constraint(name, add_function, *args, **kwargs):
        c = add_function(*args, **kwargs)
        constraint_mapping[name] = None
        return c

    num_days = len(days)
    day_to_idx = {day: idx for idx, day in enumerate(days)}
    all_levels = ["1A","1B","Urology","2A","2B","3","4"]
    all_ids    = [s["id"] for s in surgeons]
    nlth_ids = [s["id"] for s in surgeons if s.get("nlth")]
    team_day_prefs = get_team_day_prefs()
    TEAM_DAY_NO_CALL = 2
    team_day_no_call_by_weekday = {wd: set() for wd in range(7)}
    for team, by_wd in team_day_prefs.items():
        if not isinstance(by_wd, dict):
            continue
        for wd_raw, pref_raw in by_wd.items():
            try:
                wd = int(wd_raw)
                pref = int(pref_raw)
            except Exception:
                continue
            if pref == TEAM_DAY_NO_CALL and 0 <= wd <= 6:
                team_day_no_call_by_weekday[wd].add(team)
    # Conservative signed bounds for fairness totals/ranges, including horizon carry-over.
    fairness_abs_bound = max(1000, num_days * len(all_levels) * 100)

    # Load global configuration weights.
    global_config = get_global_config()
    fairness_weight = int(global_config.get("fairness_weight", "1000"))
    cap_uses_credit = str(global_config.get("fairness_cap_uses_credit", "0")) == "1"
    enable_fairness_hard_cap = str(global_config.get("enable_fairness_hard_cap", "1")) == "1"
    fairness_fallback_policy = str(global_config.get("fairness_fallback_policy", "auto_relax") or "auto_relax").strip().lower()
    if fairness_fallback_policy not in ("auto_relax", "no_fallback"):
        fairness_fallback_policy = "auto_relax"
    try:
        fairness_hard_cap_range = int(global_config.get("fairness_hard_cap_range", "1"))
    except Exception:
        fairness_hard_cap_range = 1
    # Policy: keep cohorts unchanged, enforce max-min <= 1 when hard cap is enabled.
    # If configured stricter (0), preserve it.
    fairness_hard_cap_effective = _effective_fairness_cap(fairness_hard_cap_range)
    # Cap enforcement mode (per the call-balance design):
    #   - "no_fallback": cap is a HARD constraint; infeasibility surfaces an error.
    #   - "auto_relax" (default): cap is SOFT and per-cohort. The solver keeps
    #     every cohort at <= cap whenever feasible and exceeds it by the minimum
    #     amount only in the specific cohort(s) where <= cap is impossible,
    #     instead of dropping the cap globally.
    use_hard_cap = (
        enable_fairness_hard_cap
        and not _relax_fairness_caps
        and fairness_fallback_policy == "no_fallback"
    )
    use_soft_cap = (
        enable_fairness_hard_cap
        and not _relax_fairness_caps
        and fairness_fallback_policy == "auto_relax"
    )
    gamma_no_call = int(global_config.get("gamma_no_call", "10"))
    gamma_unavail_prev = int(global_config.get("gamma_unavail_prev", "5"))
    gamma_1B = int(global_config.get("gamma_1B", "1"))
    gamma_balance = int(global_config.get("gamma_balance", "100"))
    no_call_hard = global_config.get("no_call_hard", "1") == "1"
    pre_unavail_mode = global_config.get("pre_unavail_mode", "soft")
    if pre_unavail_mode not in ("hard", "soft", "off"):
        pre_unavail_mode = "soft"
    gamma_spacing = int(global_config.get("gamma_spacing", "10"))
    spacing_threshold = int(global_config.get("spacing_threshold", "7"))
    max_calls_level1 = int(global_config.get("max_calls_level1", "10"))

    # Strengthen call-number fairness so it's the strongest soft objective (besides empty-slot penalty).
    other_soft_weights = [
        int(global_config.get("gamma_no_call", "10")),
        int(global_config.get("gamma_unavail_prev", "5")),
        int(global_config.get("gamma_spacing", "10")),
        int(global_config.get("gamma_weekend_balance", "50")),
        int(global_config.get("gamma_consec_weekend", "20")),
        int(global_config.get("gamma_weekend_team_diversity", "50")),
        int(global_config.get("gamma_unavail_credit", "50")),
    ]
    base_max_soft = max([w for w in other_soft_weights if isinstance(w, int)], default=1)
    # Heavier scaling in allow-empty mode; still keep empty-slot penalty highest elsewhere in objective.
    fairness_scale = 200 if allow_empty else 100
    fairness_weight = max(fairness_weight, base_max_soft * fairness_scale)
    # NOTE: gamma_balance is the weight of the call-distribution (L1 dispersion)
    # term and is taken straight from config (default 100). It must NOT be scaled
    # up to fairness_weight, otherwise interior-distribution flattening would
    # compete with (instead of being subordinate to) the max-min range objective.
    gamma_weekend_balance = int(global_config.get("gamma_weekend_balance", "50"))
    max_weekend_calls_cfg = int(global_config.get("max_weekend_calls", "3"))
    min_calls_nlth_cfg = int(global_config.get("min_calls_nlth", "3"))
    gamma_consec_weekend = int(global_config.get("gamma_consec_weekend", "20"))
    gamma_team_pref = int(global_config.get("gamma_team_pref", "10"))
    # New: encourage balanced team presence on weekends (more diverse teams across weekends)
    gamma_weekend_team_diversity = int(global_config.get("gamma_weekend_team_diversity", "50"))
    gamma_2b_usage = int(global_config.get("gamma_2b_usage", "0"))
    # New: discourage assigning urology-only surgeons to weekend Urology calls (soft).
    gamma_urology_weekend = int(global_config.get("gamma_urology_weekend", "50"))
    gamma_fairness_l2_groups = int(global_config.get("gamma_fairness_l2_groups", "500"))
    # New: credit for unavailability (each k days → 1 fewer call, soft)
    gamma_unavail_credit = int(global_config.get("gamma_unavail_credit", "50"))
    unavail_credit_days = int(global_config.get("unavail_credit_days", "7")) or 7
    if unavail_credit_days < 1:
        unavail_credit_days = 7
    enable_two_pass_fairness_priority = str(global_config.get("enable_two_pass_fairness_priority", "1")) == "1"

    # Feature flags (on/off) for constraint families
    def is_enabled(key: str, default: str = "1") -> bool:
        return str(global_config.get(key, default)) == "1"

    enable_force_1B_weekend           = is_enabled("enable_force_1B_weekend")
    enable_level2_supervision         = is_enabled("enable_level2_supervision")
    enable_group4_2B3_ban            = is_enabled("enable_group4_2B3_ban")
    enable_max_calls_level1          = is_enabled("enable_max_calls_level1")
    enable_nlth_rules                = is_enabled("enable_nlth_rules")
    enable_weekend_consecutive_pen   = is_enabled("enable_weekend_consecutive_penalty")
    enable_weekend_balance           = is_enabled("enable_weekend_balance")
    enable_weekend_team_diversity    = is_enabled("enable_weekend_team_diversity_enable")
    enable_team_day_prefs            = is_enabled("enable_team_day_prefs")
    enable_2b_usage_penalty          = is_enabled("enable_2b_usage_penalty", "0")  # kept for compatibility, not required if gamma>0
    enable_fairness_l2_groups        = is_enabled("enable_fairness_l2_groups", "1")
    enable_unavail_prev_penalty      = is_enabled("enable_availability_unavail_prev_penalty")
    enable_nocall_penalty            = is_enabled("enable_availability_nocall_penalty")
    enable_spacing_penalty           = is_enabled("enable_spacing_penalty")
    enable_fairness_diff_all         = is_enabled("enable_fairness_diff_all")
    enable_deviation_sum             = is_enabled("enable_deviation_sum")
    enable_unavail_credit            = is_enabled("enable_unavail_credit")
    enable_l2g1_primary_calls        = is_enabled("enable_l2g1_primary_calls", "0")
    enable_l2g1_primary_2a_same_day_penalty = is_enabled("enable_l2g1_primary_2a_same_day_penalty", "1")
    enable_urology_weekend_penalty   = is_enabled("enable_urology_weekend_penalty", "1")
    try:
        gamma_l2g1_primary_2a_same_day = int(global_config.get("gamma_l2g1_primary_2a_same_day", "30"))
    except Exception:
        gamma_l2g1_primary_2a_same_day = 30

    max_config = get_max_calls_config()  # e.g., {"1":10, "2":10, "3":10, "4":10, "l2g1_1a":4}
    
    # Use actual surgeon IDs from the database.
    id_to_surgeon = {s["id"]: s for s in surgeons}
    all_surgeon_ids = [s["id"] for s in surgeons]
    
    # --- Build Domains (using actual IDs) ---
    domain_1A = [s["id"] for s in surgeons if "1A" in parse_call_levels(s.get("call_levels", ""))]
    if not domain_1A:
        domain_1A = [-1]
    domain_1B = [s["id"] for s in surgeons if "1B" in parse_call_levels(s.get("call_levels", ""))]
    # Always allow -1 in 1B so 1B can be left empty on days when a urology-only
    # surgeon takes the Urology slot (1B is then clinically covered by 1A).
    if not domain_1B:
        domain_1B = [-1]
    else:
        domain_1B = domain_1B + [-1]
    domain_Urology = [s["id"] for s in surgeons if "Urology" in parse_call_levels(s.get("call_levels", ""))]
    if domain_Urology:
        domain_Urology = domain_Urology + [-1]
    else:
        domain_Urology = [-1]
    domain_2A = [s["id"] for s in surgeons if "2A" in parse_call_levels(s.get("call_levels", ""))]
    if not domain_2A:
        domain_2A = [-1]
    domain_2B = [s["id"] for s in surgeons if "2B" in parse_call_levels(s.get("call_levels", "")) and get_level2_group(s) == 3]
    if domain_2B:
        domain_2B = domain_2B + [-1]
    else:
        domain_2B = [-1]
    domain_3 = [s["id"] for s in surgeons if "3" in parse_call_levels(s.get("call_levels", ""))]
    if not domain_3:
        domain_3 = [-1]
    domain_4 = [s["id"] for s in surgeons if "4" in parse_call_levels(s.get("call_levels", ""))]
    if not domain_4:
        domain_4 = [-1]

    # Precompute grouping for supervision
    group1_ids = [s["id"] for s in surgeons if get_level2_group(s)==1]  # needs 2B supervision
    group2_ids = [s["id"] for s in surgeons if get_level2_group(s)==2]  # no supervision needed
    group3_ids = [s["id"] for s in surgeons if get_level2_group(s)==3]  # supervisors only
    group4_ids = [s["id"] for s in surgeons if get_level2_group(s)==4]  # supervisors who are also 3rd call
    level34_ids = get_level34_subgroup_ids(surgeons)

    if enable_l2g1_primary_calls and group1_ids:
        raw_1a = [x for x in domain_1A if x != -1]
        for sid in group1_ids:
            if sid not in raw_1a:
                raw_1a.append(sid)
        domain_1A = raw_1a if raw_1a else [-1]

    base_domains = {
        "1A": domain_1A,     
        "1B": domain_1B,
        "Urology": domain_Urology,
        "2A": list(set(group1_ids + group2_ids)),  # exclude subgroup 3 from 2A
        "2B": group3_ids + group4_ids + [-1],
        "3":  domain_3,
        "4":  domain_4,
    }
    domains_by_day = {
        d: { lvl: list(base_domains[lvl]) for lvl in base_domains }
        for d in range(num_days)
    }

    # #region agent log
    _debug_log(
        "scheduler.py:domains-initial",
        "Initial base domains + urology config",
        {
            "num_days": num_days,
            "num_surgeons": len(surgeons),
            "allow_empty": allow_empty,
            "relax_fairness_caps": _relax_fairness_caps,
            "diagnostic_run": _diagnostic_run,
            "domain_1A_ids": [x for x in domain_1A if x != -1],
            "domain_1B_ids": [x for x in domain_1B if x != -1],
            "domain_Urology_ids": [x for x in domain_Urology if x != -1],
            "urology_min_raw_cfg": max_config.get("urology_min"),
            "urology_max_raw_cfg": max_config.get("urology_max"),
            "enable_force_1B_weekend": enable_force_1B_weekend,
            "enable_l2g1_primary_calls": enable_l2g1_primary_calls,
            "group1_ids": group1_ids,
        },
        hypothesis="H1,H2,H3,H5",
    )
    # #endregion

    # If allow_empty is requested, ensure every slot can be left empty by including -1 in its domain
    if allow_empty:
        for d in range(num_days):
            for lvl in all_levels:
                if -1 not in domains_by_day[d][lvl]:
                    domains_by_day[d][lvl].append(-1)

    availability = get_availability_requests()

    # --- Precompute preassigned pairs early (day-index, level) -> surgeon_id (int) ---
    preassigned_early = {}
    if preassignments:
        for day_str, lvls in preassignments.items():
            if day_str not in day_to_idx:
                continue
            d_idx = day_to_idx[day_str]
            if not isinstance(lvls, dict):
                continue
            for lvl, sid in lvls.items():
                if sid in [None, ""]:
                    continue
                try:
                    preassigned_early[(d_idx, lvl)] = int(sid)
                except Exception:
                    continue
    # Unified fairness credit per surgeon = unavailability credit + manual call credit.
    # This is only used in fairness math (caps/objectives/diagnostics), not hard assignment constraints.
    fairness_credit_calls_per_surgeon = compute_unavailability_credit_by_surgeon(
        surgeons=surgeons,
        availability=availability,
        days=days,
        unavail_credit_days=unavail_credit_days,
    )
    for surgeon_obj in surgeons:
        sid = surgeon_obj.get("id")
        if sid is None:
            continue
        fairness_credit_calls_per_surgeon[sid] = _unified_fairness_credit_calls(
            fairness_credit_calls_per_surgeon.get(sid, 0),
            surgeon_obj,
        )
    for d, day_str in enumerate(days):
        current_date = datetime.datetime.strptime(day_str, "%Y-%m-%d").date()
        for s_id, req_list in availability.items():
            for req in req_list:
                raw = req.get('date')
                if isinstance(raw, datetime.date):
                    req_date = raw
                else:
                    try:
                        req_date = datetime.datetime.strptime(raw, "%Y-%m-%d").date()
                    except Exception:
                        continue
                # If the request applies on the current date, and is an unavailable/study_leave/no_call request:
                if no_call_hard:
                    if req_date == current_date and req.get('request_type') in ("unavailable","study_leave","no_call"):
                        for lvl in all_levels:
                            # Only remove if the domain has more than one candidate
                            if s_id in domains_by_day[d][lvl]:
                                # Do not remove if this (day,lvl) is preassigned to this surgeon
                                if preassigned_early.get((d, lvl)) == s_id:
                                    continue
                                if len(domains_by_day[d][lvl]) > 1:
                                    domains_by_day[d][lvl].remove(s_id)
                                else:
                                    print(f"Warning: Not removing surgeon {s_id} from Day {day_str}, level {lvl} because it would empty the domain.")
                if not no_call_hard:
                    if req_date == current_date and req.get('request_type') == "unavailable":
                        for lvl in all_levels:
                            # Only remove if the domain has more than one candidate
                            if s_id in domains_by_day[d][lvl]:
                                # Protect preassigned (day,lvl)
                                if preassigned_early.get((d, lvl)) == s_id:
                                    continue
                                if len(domains_by_day[d][lvl]) > 1:
                                    domains_by_day[d][lvl].remove(s_id)
                                else:
                                    print(f"Warning: Not removing surgeon {s_id} from Day {day_str}, level {lvl} because it would empty the domain.")
        if no_call_hard:
            blocked_teams = team_day_no_call_by_weekday.get(current_date.weekday(), set())
            if blocked_teams:
                for surgeon in surgeons:
                    s_id = surgeon.get("id")
                    if s_id is None:
                        continue
                    if surgeon.get("team") not in blocked_teams:
                        continue
                    for lvl in all_levels:
                        if s_id in domains_by_day[d][lvl]:
                            if preassigned_early.get((d, lvl)) == s_id:
                                continue
                            if len(domains_by_day[d][lvl]) > 1:
                                domains_by_day[d][lvl].remove(s_id)

    if pre_unavail_mode == "hard":
        for s_id, req_list in availability.items():
            for req in req_list:
                if req.get('request_type') not in ("unavailable",):
                    continue
                req_date = _parse_req_date(req.get('date'))
                if not req_date:
                    continue
                prev_str = (req_date - datetime.timedelta(days=1)).isoformat()
                d_idx = day_to_idx.get(prev_str)
                if d_idx is None:
                    continue
                for lvl in all_levels:
                    dom = domains_by_day[d_idx][lvl]
                    if s_id not in dom:
                        continue
                    if preassigned_early.get((d_idx, lvl)) == s_id:
                        continue
                    if len(dom) <= 1:
                        continue
                    dom.remove(s_id)

    # --- Pre-solve domain diagnostics: flag empty domains (excluding -1) ---
    # Skip 2B (always optional) and Urology (optional on weekdays; weekend/PH
    # check is handled separately below).
    try:
        for d_idx, day_str in enumerate(days):
            for lvl in all_levels:
                if lvl in ("2B", "Urology"):
                    continue
                effective = [sid for sid in domains_by_day[d_idx][lvl] if sid != -1]
                if len(effective) == 0:
                    diagnostics.append(f"No eligible surgeons for {lvl} on {day_str} after eligibility/pruning.")
    except Exception:
        pass

    # #region agent log
    try:
        _tmp_u_and_1b = [
            s["id"] for s in surgeons
            if "Urology" in parse_call_levels(s.get("call_levels", ""))
            and "1B" in parse_call_levels(s.get("call_levels", ""))
        ]
        _asym = []
        for _d in range(num_days):
            for _sid in _tmp_u_and_1b:
                _in_1b = _sid in domains_by_day[_d]["1B"]
                _in_uro = _sid in domains_by_day[_d]["Urology"]
                if _in_1b != _in_uro:
                    _asym.append({"day": days[_d], "sid": _sid, "in_1B": _in_1b, "in_Urology": _in_uro})
        _min_1a = min(len([s for s in domains_by_day[d]["1A"] if s != -1]) for d in range(num_days))
        _min_1b = min(len([s for s in domains_by_day[d]["1B"] if s != -1]) for d in range(num_days))
        _min_uro = min(len([s for s in domains_by_day[d]["Urology"] if s != -1]) for d in range(num_days))
        _debug_log(
            "scheduler.py:domains-post-pruning",
            "Domains after availability/no_call/prev_schedule pruning",
            {
                "min_1A_candidates_across_days": _min_1a,
                "min_1B_candidates_across_days": _min_1b,
                "min_Urology_candidates_across_days": _min_uro,
                "urology_and_1b_count": len(_tmp_u_and_1b),
                "urology_and_1b_ids": _tmp_u_and_1b,
                "asymmetric_pruning_issues": _asym[:20],
                "asymmetric_pruning_total": len(_asym),
            },
            hypothesis="H2",
        )
    except Exception as _e:
        _debug_log("scheduler.py:domains-post-pruning", "log-error", {"err": str(_e)}, hypothesis="H2")
    # #endregion

    solver_debug = str(global_config.get("solver_debug", "0")) == "1"
    if solver_debug:
        print("Debug: Available domains after availability/no_call adjustments")
        for d in range(num_days):
            print(f"Day {days[d]}:")
            for lvl in all_levels:
                print(f"  Level {lvl}: {domains_by_day[d][lvl]}")

    # --- Previous-month spacing (prune domains) ---
    if prev_schedule:
        # 1) map names→IDs
        name_to_id = { s["name"]: s["id"] for s in surgeons }

        # 2) parse & sort all prev-month dates, then keep just the last two days (−2, −1)
        prev_dates = sorted(
            datetime.datetime.strptime(day, "%Y-%m-%d").date()
            for day in prev_schedule
        )
        last_two = prev_dates[-2:]   # [day−2, day−1]

        # 3) collect bans by *target* day (0-based)
        #    - anyone on day−2 bans target day 0
        #    - anyone on day−1 bans target days 0 and 1
        #
        # Compute target range from the ACTUAL date distance to the first
        # scheduled day, not from the element's index in last_two.  If only
        # one of the two days has on-call data, using the index would
        # incorrectly treat day−1 as day−2 and miss banning day 1.
        first_day = datetime.datetime.strptime(days[0], "%Y-%m-%d").date()
        ban_by_day = {}
        for pd in last_two:
            distance = (first_day - pd).days  # 1 for day−1, 2 for day−2, …
            if distance <= 0 or distance > 2:
                continue  # outside the 3-day window
            # ban target days 0 … (2 − distance) inclusive
            max_target = 2 - distance  # 1 for day−1, 0 for day−2
            prev_str = pd.isoformat()
            for lvl in all_levels:
                prev_name = prev_schedule.get(prev_str, {}).get(lvl)
                sid = name_to_id.get(prev_name)
                if sid is None:
                    continue

                for target in range(max_target + 1):
                    if target >= num_days:
                        break
                    ban_by_day.setdefault(target, set()).add(sid)

        # 4) prepare per-day debug structures
        pruned  = {d: {lvl: [] for lvl in all_levels} for d in ban_by_day}
        skipped = {d: {lvl: [] for lvl in all_levels} for d in ban_by_day}

        # 5) prune domains_by_day using ban_by_day
        for d, banned_ids in ban_by_day.items():
            for lvl in all_levels:
                dom = domains_by_day[d][lvl]
                for sid in banned_ids:
                    if sid not in dom:
                        continue
                    # Do not remove if this (day,lvl) is preassigned to this surgeon
                    if preassigned_early.get((d, lvl)) == sid:
                        continue
                    if len(dom) > 1:
                        dom.remove(sid)
                        pruned[d][lvl].append(sid)
                    else:
                        skipped[d][lvl].append(sid)

        # --- DEBUG: report what prev_schedule actually pruned/skipped ---
        if solver_debug:
            print("=== prev_schedule prune report ===")
            for d in sorted(ban_by_day):
                print(f"\nDay {d} ({days[d]}) carry-over bans:")
                print(f"    would-ban = {sorted(ban_by_day[d])}")
                for lvl in all_levels:
                    print(f"  {lvl:>3}  pruned={pruned[d][lvl]}  skipped={skipped[d][lvl]}")
            print("=== end of prev_schedule report ===\n")

    # … your availability/no_call pruning here …

    # --- DEBUG: dump the pruned domains before creating X ---
    if solver_debug:
        print("=== Domains AFTER pruning ===")
        for d, day_str in enumerate(days):
            print(f"Day {d:02d} ({day_str}):")
            for lvl in all_levels:
                print(f"  {lvl:>3}: {domains_by_day[d][lvl]}")
        print("=== end of domain dump ===\n")

    # Ensure all preassigned surgeons are present in domains before variable creation
    for (d_idx, lvl), sid in preassigned_early.items():
        if sid not in domains_by_day.get(d_idx, {}).get(lvl, []):
            domains_by_day[d_idx][lvl].append(sid)

    # --- Preflight feasibility checks to produce helpful diagnostics ---
    # 1) Empty domain checks (post-pruning)
    for d, day_str in enumerate(days):
        for lvl in all_levels:
            # Skip diagnostics for 2B and Urology; both are optional on weekdays.
            # Weekend/holiday Urology check is handled below in section (2).
            if lvl in ("2B", "Urology"):
                continue
            candidates = [sid for sid in domains_by_day[d][lvl] if sid != -1]
            if not candidates:
                diagnostics.append(f"No eligible surgeon for level {lvl} on {day_str}.")

    # 2) Weekend/holiday 1B requirement if enabled
    try:
        is_weekend_day = [datetime.datetime.strptime(day, "%Y-%m-%d").weekday() >= 5 for day in days]
    except Exception:
        is_weekend_day = [False for _ in days]
    is_holiday_day = [bool(public_holidays and (day in public_holidays)) for day in days]
    if global_config.get("enable_force_1B_weekend", "1") == "1":
        for d, day_str in enumerate(days):
            if is_weekend_day[d] or is_holiday_day[d]:
                cand_1b = [sid for sid in domains_by_day[d]["1B"] if sid != -1]
                if not cand_1b:
                    diagnostics.append(f"1B must be filled on {day_str} (weekend/holiday) but no eligible 1B candidate.")
                cand_uro = [sid for sid in domains_by_day[d]["Urology"] if sid != -1]
                if not cand_uro and not allow_empty:
                    diagnostics.append(f"Urology should be filled on {day_str} (weekend/holiday) but no eligible Urology candidate.")

    # 3) Supervision logic: if 2A can only be Group1 (needs supervisor) but 2B has no supervisors
    # Determine supervision groups again for clarity
    g1 = [s["id"] for s in surgeons if get_level2_group(s) == 1]
    g2 = [s["id"] for s in surgeons if get_level2_group(s) == 2]
    g3 = [s["id"] for s in surgeons if get_level2_group(s) == 3]
    g4 = [s["id"] for s in surgeons if get_level2_group(s) == 4]
    supervisors = set(g3 + g4)
    for d, day_str in enumerate(days):
        cand_2a = set([sid for sid in domains_by_day[d]["2A"] if sid != -1])
        cand_2b = set([sid for sid in domains_by_day[d]["2B"] if sid != -1])
        if cand_2a and (cand_2a.issubset(set(g1))) and not (cand_2b & supervisors):
            diagnostics.append(f"2A on {day_str} requires a 2B supervisor, but none available.")

    # 4) Preassignment conflicts: specified surgeon not in domain
    if preassignments:
        for d, day_str in enumerate(days):
            if day_str in preassignments:
                for level, surgeon_id in preassignments[day_str].items():
                    if surgeon_id in [None, ""]:
                        continue
                    try:
                        assigned_id = int(surgeon_id)
                    except Exception:
                        continue
                    dom = domains_by_day[d].get(level, [])
                    if assigned_id not in dom:
                        diagnostics.append(f"Preassignment conflict on {day_str} {level}: surgeon {assigned_id} not eligible.")

    # --- Decision Variables ---
    X = {}

    for d in range(num_days):
        for lvl in all_levels:
            dom = domains_by_day[d][lvl]
            if not dom:
                dom = [-1]
            X[(d, lvl)] = model.NewIntVarFromDomain(
                cp_model.Domain.FromValues(dom),
                f"X_{d}_{lvl}"
            )

    if solver_debug:
        print("Preassignments:", preassignments)
    # ----- Apply Preassignment Constraints -----
    # If preassignments is provided, force the variable to match the assigned surgeon.
    # Expected format: { "YYYY-MM-DD": { "1A": surgeon_id, "3": surgeon_id, ... }, ... }
    # Track fixed preassignments for later rules to respect (override)
    preassigned_fixed = {}
    if preassignments:
        for d, day_str in enumerate(days):
            if day_str in preassignments:
                for level, surgeon_id in preassignments[day_str].items():
                    if surgeon_id in [None, ""]:
                        continue
                    try:
                        assigned_id = int(surgeon_id)
                    except Exception:
                        print(f"Error converting surgeon id {surgeon_id} on {day_str} at level {level}")
                        continue
                    preassigned_fixed[(d, level)] = assigned_id
                    # Debug print: show available domain for that slot.
                    if solver_debug:
                        print(f"Day {day_str} level {level} domain: {domains_by_day[d][level]}, preassigned: {assigned_id}")
                    # Force variable to assigned_id regardless of availability and spacing rules.
                    add_named_constraint(f"Preassignment: {day_str} level {level} fixed to surgeon {assigned_id}",
                        model.Add, X[(d, level)] == assigned_id)

    # --- Prevent same surgeon from being assigned twice on same day ---
    # Exception: (1B, Urology) pair is exempt because a 1B+Urology surgeon
    # is intentionally mirrored into both slots on the same day (see mirror
    # constraint below).
    for d, day_str in enumerate(days):
        if solver_debug:
            print(f"\nChecking level‐pairs on {day_str}:")
        for lvl1, lvl2 in itertools.combinations(all_levels, 2):
            if {lvl1, lvl2} == {"1B", "Urology"}:
                continue
            # compute the real candidates for each slot
            c1 = set(domains_by_day[d][lvl1]) - {-1}
            c2 = set(domains_by_day[d][lvl2]) - {-1}
            # if both are to be filled, they each need ≥1 candidate...
            if not c1 or not c2:
                if solver_debug:
                    print(f"  • One of {lvl1}/{lvl2} has no candidates: {lvl1}→{c1}, {lvl2}→{c2}")
            # ...and together they need ≥2 **distinct** candidates
            elif len(c1 | c2) < 2:
                if solver_debug:
                    print(f"  ✖ Pair ({lvl1},{lvl2}) has only {len(c1|c2)} distinct candidates: {c1|c2}")
            b1 = model.NewBoolVar(f"filled_{d}_{lvl1}_dupcheck")
            b2 = model.NewBoolVar(f"filled_{d}_{lvl2}_dupcheck")
            add_named_constraint(f"Dup-check: Day {day_str} {lvl1} filled",
                model.Add, X[(d, lvl1)] != -1
            ).OnlyEnforceIf(b1)
            add_named_constraint(f"Dup-check: Day {day_str} {lvl1} empty",
                model.Add, X[(d, lvl1)] == -1
            ).OnlyEnforceIf(b1.Not())
            add_named_constraint(f"Dup-check: Day {day_str} {lvl2} filled",
                model.Add, X[(d, lvl2)] != -1
            ).OnlyEnforceIf(b2)
            add_named_constraint(f"Dup-check: Day {day_str} {lvl2} empty",
                model.Add, X[(d, lvl2)] == -1
            ).OnlyEnforceIf(b2.Not())
            add_named_constraint(f"Uniqueness: Day {day_str} {lvl1} != {lvl2} if both filled",
                model.Add, X[(d, lvl1)] != X[(d, lvl2)]
            ).OnlyEnforceIf([b1, b2])
    
    # --- Constraint Set 2: 3-Day Gap ---
    for d in range(num_days):
        for d2 in range(d + 1, min(num_days, d + 3)):
            for lev1 in all_levels:
                for lev2 in all_levels:
                    b1 = model.NewBoolVar(f'nonempty_{d}_{lev1}')
                    b2 = model.NewBoolVar(f'nonempty_{d2}_{lev2}')
                    add_named_constraint(f"3-Day Gap: Day {d} {lev1} non-empty", 
                        model.Add, X[(d, lev1)] != -1
                    ).OnlyEnforceIf(b1)
                    add_named_constraint(f"3-Day Gap: Day {d} {lev1} empty", 
                        model.Add, X[(d, lev1)] == -1
                    ).OnlyEnforceIf(b1.Not())
                    add_named_constraint(f"3-Day Gap: Day {d2} {lev2} non-empty", 
                        model.Add, X[(d2, lev2)] != -1
                    ).OnlyEnforceIf(b2)
                    add_named_constraint(f"3-Day Gap: Day {d2} {lev2} empty", 
                        model.Add, X[(d2, lev2)] == -1
                    ).OnlyEnforceIf(b2.Not())
                    add_named_constraint(f"3-Day Gap: Day {d} {lev1} != Day {d2} {lev2} if both filled",
                        model.Add, X[(d, lev1)] != X[(d2, lev2)]
                    ).OnlyEnforceIf([b1, b2])
    
    indicator_1B = {}
    
    for d in range(num_days):
        # rebuild your 1B indicator off of the final X[(d, "1B")]
        b1B = model.NewBoolVar(f"b1B_{d}")
        add_named_constraint(f"Indicator 1B day {days[d]}: X[(d,'1B')] filled", 
            model.Add, X[(d, "1B")] != -1
        ).OnlyEnforceIf(b1B)
        add_named_constraint(f"Indicator 1B day {days[d]}: X[(d,'1B')] NOT filled", 
            model.Add, X[(d, "1B")] == -1
        ).OnlyEnforceIf(b1B.Not())
        indicator_1B[d] = b1B

    # NOTE: The previous "force 1B != -1 on weekend/PH" rule is now superseded by
    # the more general "1B may be empty only if a urology-only surgeon is on
    # Urology" rule added later (after indicators are built). That rule applies
    # to every day, so explicit weekend force is no longer necessary.

    # --- Force Urology to be filled on weekends and public holidays (mirrors 1B rule) ---
    if enable_force_1B_weekend:
        for d, day_str in enumerate(days):
            dt = datetime.datetime.strptime(day_str, "%Y-%m-%d").date()
            is_weekend_day = dt.weekday() >= 5
            is_holiday_day = public_holidays and (day_str in public_holidays)
            if is_weekend_day or is_holiday_day:
                real_uro = [sid for sid in domains_by_day[d]["Urology"] if sid != -1]
                if not real_uro:
                    continue
                add_named_constraint(f"Force Urology on {day_str}: Urology != -1",
                    model.Add, X[(d, "Urology")] != -1)
    
    # --- Level‑2 Grouping & Supervision Constraints (toggle) ---
    if enable_level2_supervision:
        for d in range(num_days):
            # 1) If a group‑1 surgeon is on 2A, must have someone in 2B.
            for s in group1_ids:
                b1 = model.NewBoolVar(f"lvl2_grp1_day{d}_is_s{s}")
                add_named_constraint(f"Level2 group1: Day {d} 2A == {s}",
                    model.Add, X[(d, "2A")] == s
                ).OnlyEnforceIf(b1)
                add_named_constraint(f"Level2 group1: Day {d} 2A != {s}",
                    model.Add, X[(d, "2A")] != s
                ).OnlyEnforceIf(b1.Not())
                add_named_constraint(f"Level2 group1: Day {d} if 2A=={s} then 2B != -1",
                    model.Add, X[(d, "2B")] != -1
                ).OnlyEnforceIf(b1)
            # 2) If a group‑2 or 3 surgeon is on 2A, forbid any 2B.
            for s in group2_ids:
                b2 = model.NewBoolVar(f"lvl2_grp2_day{d}_is_s{s}")
                add_named_constraint(f"Level2 group2: Day {d} 2A == {s}",
                    model.Add, X[(d, "2A")] == s
                ).OnlyEnforceIf(b2)
                add_named_constraint(f"Level2 group2: Day {d} 2A != {s}",
                    model.Add, X[(d, "2A")] != s
                ).OnlyEnforceIf(b2.Not())
                add_named_constraint(f"Level2 group2: Day {d} if 2A=={s} then 2B == -1",
                    model.Add, X[(d, "2B")] == -1
                ).OnlyEnforceIf(b2)
            for s in group3_ids:
                # Explicitly forbid subgroup 3 from 2A (hard ban)
                add_named_constraint(f"Level2 group3 ban on 2A: Day {d} 2A != {s}",
                    model.Add, X[(d, "2A")] != s)
            # 3) Never allow Group 4 in 2A.
            for s in group4_ids:
                add_named_constraint(f"Level2 group4 ban: Day {d} 2A != {s}",
                    model.Add, X[(d, "2A")] != s)
            # Note: global same-day uniqueness across levels is enforced earlier when both are filled.
    
    # ── Prevent Group-4 surgeons from covering both 2B and 3 on the same day (toggle) ──
    if enable_group4_2B3_ban:
        for d in range(num_days):
            for s1 in group4_ids:
                for s2 in group4_ids:
                    add_named_constraint(f"Group4 ban: Day {d} 2B != {s1} OR 3 != {s2}",
                        model.AddForbiddenAssignments, [ X[(d, "2B")], X[(d, "3")] ], [ [s1, s2] ])
    
    # --- Constraint Set 3: Maximum Calls per Group ---
    indicators = {}
    for d in range(num_days):
        for lev in all_levels:
            for s_id in all_ids:
                b = model.NewBoolVar(f"ind_{d}_{lev}_{s_id}")
                add_named_constraint(f"Indicator: Day {d} {lev} == {s_id}",
                    model.Add, X[(d, lev)] == s_id
                ).OnlyEnforceIf(b)
                add_named_constraint(f"Indicator: Day {d} {lev} != {s_id}",
                    model.Add, X[(d, lev)] != s_id
                ).OnlyEnforceIf(b.Not())
                indicators[(d, lev, s_id)] = b

    # --- Level-1 "on call" per day: a surgeon who holds 1A and/or 1B on day d
    # counts as ONE level-1 call that day, never two. We use the OR (max) of the
    # 1A and 1B indicators instead of their sum, so that if the same surgeon ever
    # occupies both slots on the same day (or the display mirror shows them in
    # both), every per-surgeon tally (overall calls, 1A+1B cohort fairness, the
    # max-calls-per-month cap, NLTH minimums via call_count_overall) charges a
    # single call. With the current per-day uniqueness rule (X[1A] != X[1B]) at
    # most one of the two indicators is 1, so OR == sum and this is a no-op; it
    # only changes behavior in the same-surgeon-both-slots case the user wants.
    lvl1_any = {}
    non_lvl1_levels = [lev for lev in all_levels if lev not in ("1A", "1B")]
    for s_id in all_ids:
        for d in range(num_days):
            b = model.NewBoolVar(f"lvl1_any_s{s_id}_d{d}")
            add_named_constraint(
                f"Level-1 on-call (1A or 1B): Day {d} surgeon {s_id}",
                model.AddMaxEquality, b,
                [indicators[(d, "1A", s_id)], indicators[(d, "1B", s_id)]],
            )
            lvl1_any[(s_id, d)] = b

    # --- Urology level membership (used for min/max + mirror logic) ---
    urology_ids = [
        s["id"] for s in surgeons
        if "Urology" in parse_call_levels(s.get("call_levels", ""))
    ]
    urology_only_ids = [
        s["id"] for s in surgeons
        if parse_call_levels(s.get("call_levels", "")) == ["Urology"]
    ]
    urology_and_1b_ids = [
        s["id"] for s in surgeons
        if "Urology" in parse_call_levels(s.get("call_levels", ""))
        and "1B" in parse_call_levels(s.get("call_levels", ""))
    ]

    # --- 1B -> Urology mirror: when a 1B+Urology surgeon is on 1B, they must also be Urology that day. ---
    for s_id in urology_and_1b_ids:
        for d in range(num_days):
            add_named_constraint(
                f"1B-Urology mirror: Day {days[d]} surgeon {s_id}",
                model.Add, X[(d, "Urology")] == s_id,
            ).OnlyEnforceIf(indicators[(d, "1B", s_id)])

    # --- Min/Max calls per month for Urology-only surgeons (from max_calls_config) ---
    try:
        urology_min_cfg = int(max_config.get("urology_min", 0))
    except Exception:
        urology_min_cfg = 0
    try:
        urology_max_cfg = int(max_config.get("urology_max", num_days))
    except Exception:
        urology_max_cfg = num_days
    if urology_min_cfg < 0:
        urology_min_cfg = 0
    if urology_max_cfg < urology_min_cfg:
        urology_max_cfg = urology_min_cfg

    # --- Per-day flag: a urology-only surgeon is on Urology this day. ---
    # When this flag is true, we allow X[d, "1B"] to be -1 (1B is then clinically
    # covered by 1A on that same day). When false, 1B must be filled. This applies
    # to all days, including weekends/PH.
    uro_only_on_d_var = {}
    for d in range(num_days):
        u_only = model.NewBoolVar(f"uro_only_on_d_{d}")
        if urology_only_ids:
            # u_only == sum(indicators[d, Urology, sid] for sid in urology_only_ids)
            # Indicators are mutually exclusive for X[d, "Urology"] so the sum is 0/1.
            add_named_constraint(
                f"Urology-only on Urology indicator: Day {days[d]}",
                model.Add,
                u_only == sum(indicators[(d, "Urology", sid)] for sid in urology_only_ids),
            )
        else:
            add_named_constraint(
                f"Urology-only on Urology indicator (none) Day {days[d]}",
                model.Add, u_only == 0,
            )
        uro_only_on_d_var[d] = u_only

    # --- Rule: 1B may be empty only when a urology-only surgeon is on Urology. ---
    for d in range(num_days):
        b_1b_empty = model.NewBoolVar(f"1b_empty_d_{d}")
        add_named_constraint(
            f"1B empty indicator: Day {days[d]} 1B == -1",
            model.Add, X[(d, "1B")] == -1,
        ).OnlyEnforceIf(b_1b_empty)
        add_named_constraint(
            f"1B empty indicator: Day {days[d]} 1B != -1",
            model.Add, X[(d, "1B")] != -1,
        ).OnlyEnforceIf(b_1b_empty.Not())
        # If 1B is empty on day d, then a urology-only surgeon must be on Urology that day.
        add_named_constraint(
            f"1B may be empty only if urology-only on Urology: Day {days[d]}",
            model.AddImplication, b_1b_empty, uro_only_on_d_var[d],
        )

    if urology_only_ids:
        for s_id in urology_only_ids:
            uro_count = model.NewIntVar(0, num_days, f"urology_count_{s_id}")
            add_named_constraint(
                f"Urology-only count for surgeon {s_id}",
                model.Add,
                uro_count == sum(indicators[(d, "Urology", s_id)] for d in range(num_days)),
            )
            if urology_max_cfg < num_days:
                add_named_constraint(
                    f"Max Urology calls for surgeon {s_id}",
                    model.Add, uro_count <= urology_max_cfg,
                )
            if urology_min_cfg > 0:
                add_named_constraint(
                    f"Min Urology calls for surgeon {s_id}",
                    model.Add, uro_count >= urology_min_cfg,
                )

    if enable_l2g1_primary_calls and group1_ids and urology_only_ids:
        for d in range(num_days):
            for s in group1_ids:
                for u in urology_only_ids:
                    add_named_constraint(
                        f"L2G1 on 1A forbids urology-only on 1B: Day {days[d]} 1A={s} 1B!={u}",
                        model.Add,
                        X[(d, "1B")] != u,
                    ).OnlyEnforceIf(indicators[(d, "1A", s)])

    # #region agent log
    try:
        _weekend_ph_days = []
        for _d, _day_str in enumerate(days):
            _dt = datetime.datetime.strptime(_day_str, "%Y-%m-%d").date()
            if _dt.weekday() >= 5 or (public_holidays and _day_str in public_holidays):
                _weekend_ph_days.append(_day_str)
        _debug_log(
            "scheduler.py:urology-constraints",
            "Urology mirror + min/max constraints installed",
            {
                "urology_ids": urology_ids,
                "urology_only_ids": urology_only_ids,
                "urology_and_1b_ids": urology_and_1b_ids,
                "urology_min_cfg": urology_min_cfg,
                "urology_max_cfg": urology_max_cfg,
                "num_urology_only": len(urology_only_ids),
                "num_urology_and_1b": len(urology_and_1b_ids),
                "num_weekend_ph_days": len(_weekend_ph_days),
                "weekend_ph_days": _weekend_ph_days,
                "num_days": num_days,
                "max_constraint_applied": urology_max_cfg < num_days,
                "min_constraint_applied": urology_min_cfg > 0,
            },
            hypothesis="H1,H3,H5",
        )
    except Exception as _e:
        _debug_log("scheduler.py:urology-constraints", "log-error", {"err": str(_e)}, hypothesis="H1,H3")
    # #endregion

    # --- Max 2B / level-3 calls per month for L2 Group 4 (2B+3 supervisors) ---
    if group4_ids:
        try:
            max_l2g4_2b = int(max_config.get("l2g4_max_2b", 1))
        except Exception:
            max_l2g4_2b = 1
        try:
            max_l2g4_3 = int(max_config.get("l2g4_max_3", num_days))
        except Exception:
            max_l2g4_3 = num_days
        max_l2g4_2b = max(0, min(max_l2g4_2b, num_days))
        max_l2g4_3 = max(0, min(max_l2g4_3, num_days))
        for s_id in group4_ids:
            c2b = model.NewIntVar(0, num_days, f"l2g4_count_2b_{s_id}")
            add_named_constraint(
                f"L2 Group 4 level-2B count for surgeon {s_id}",
                model.Add,
                c2b == sum(indicators[(d, "2B", s_id)] for d in range(num_days)),
            )
            add_named_constraint(
                f"Max level-2B calls for L2 Group 4 surgeon {s_id}",
                model.Add,
                c2b <= max_l2g4_2b,
            )
            c3 = model.NewIntVar(0, num_days, f"l2g4_count_3_{s_id}")
            add_named_constraint(
                f"L2 Group 4 level-3 count for surgeon {s_id}",
                model.Add,
                c3 == sum(indicators[(d, "3", s_id)] for d in range(num_days)),
            )
            add_named_constraint(
                f"Max level-3 calls for L2 Group 4 surgeon {s_id}",
                model.Add,
                c3 <= max_l2g4_3,
            )

    # --- Max level-3 / level-4 calls per month for Level 3+4 subgroup ---
    if level34_ids:
        try:
            max_lvl3 = int(max_config.get("level34_max_3", max_config.get("3", num_days)))
        except Exception:
            max_lvl3 = num_days
        try:
            max_lvl4 = int(max_config.get("level34_max_4", max_config.get("4", num_days)))
        except Exception:
            max_lvl4 = num_days
        max_lvl3 = max(0, min(max_lvl3, num_days))
        max_lvl4 = max(0, min(max_lvl4, num_days))
        for s_id in level34_ids:
            c3 = model.NewIntVar(0, num_days, f"level34_count_3_{s_id}")
            add_named_constraint(
                f"Level 3+4 subgroup level-3 count for surgeon {s_id}",
                model.Add,
                c3 == sum(indicators[(d, "3", s_id)] for d in range(num_days)),
            )
            add_named_constraint(
                f"Max level-3 calls for Level 3+4 surgeon {s_id}",
                model.Add,
                c3 <= max_lvl3,
            )
            c4 = model.NewIntVar(0, num_days, f"level34_count_4_{s_id}")
            add_named_constraint(
                f"Level 3+4 subgroup level-4 count for surgeon {s_id}",
                model.Add,
                c4 == sum(indicators[(d, "4", s_id)] for d in range(num_days)),
            )
            add_named_constraint(
                f"Max level-4 calls for Level 3+4 surgeon {s_id}",
                model.Add,
                c4 <= max_lvl4,
            )
    
    call_count_overall = {}
    for s in all_surgeon_ids:
        call_count_overall[s] = model.NewIntVar(0, num_days * len(all_levels), f'count_all_{s}')
        # 1A+1B contribute a single call per day (lvl1_any); all other levels are
        # summed normally.
        add_named_constraint(f"Total calls for surgeon {s}",
            model.Add, call_count_overall[s] == (
                sum(lvl1_any[(s, d)] for d in range(num_days))
                + sum(indicators[(d, level, s)] for d in range(num_days) for level in non_lvl1_levels)
            ))
    
    if enable_max_calls_level1:
        for s_id in all_ids:
            c1 = model.NewIntVar(0, num_days, f"count1_{s_id}")
            add_named_constraint(f"1A+1B calls for surgeon {s_id}",
                model.Add, c1 == sum(lvl1_any[(s_id, d)] for d in range(num_days)))
            add_named_constraint(f"Max 1A+1B calls for surgeon {s_id}",
                model.Add, c1 <= max_calls_level1)

    if enable_l2g1_primary_calls and group1_ids:
        try:
            max_l2g1_1a = int(max_config.get("l2g1_1a", max_config.get("l2g1_1ab", 4)))
        except Exception:
            max_l2g1_1a = 4
        if max_l2g1_1a < 0:
            max_l2g1_1a = 0
        for s_id in group1_ids:
            c_l2g1 = model.NewIntVar(0, num_days, f"count_l2g1_1a_{s_id}")
            add_named_constraint(
                f"L2G1 1A count for surgeon {s_id}",
                model.Add,
                c_l2g1 == sum(indicators[(d, "1A", s_id)] for d in range(num_days)),
            )
            add_named_constraint(
                f"Max L2G1 1A calls for surgeon {s_id}",
                model.Add,
                c_l2g1 <= max_l2g1_1a,
            )

        # Group-wide monthly cap: total 1A calls held by all L2G1 (2A-only)
        # surgeons combined cannot exceed this configured maximum.
        try:
            max_l2g1_1a_total = int(max_config.get("l2g1_1a_total", 8))
        except Exception:
            max_l2g1_1a_total = 8
        if max_l2g1_1a_total < 0:
            max_l2g1_1a_total = 0
        total_l2g1_1a = model.NewIntVar(0, num_days * len(group1_ids), "count_l2g1_1a_total")
        add_named_constraint(
            "L2G1 1A total count across 2A surgeons",
            model.Add,
            total_l2g1_1a == sum(
                indicators[(d, "1A", s_id)]
                for s_id in group1_ids
                for d in range(num_days)
            ),
        )
        add_named_constraint(
            "Max L2G1 1A calls across all 2A surgeons (monthly)",
            model.Add,
            total_l2g1_1a <= max_l2g1_1a_total,
        )

    # --- Hard minimum total calls for NLTH surgeons ---
    if min_calls_nlth_cfg > 0 and nlth_ids:
        for s in nlth_ids:
            add_named_constraint(f"Min total calls for NLTH surgeon {s}",
                model.Add, call_count_overall[s] >= min_calls_nlth_cfg)

    # Prior credit calls across horizon window (same policy as current-month credit).
    prior_credit_calls_per_surgeon = {}
    if isinstance(horizon_prior_counts, dict):
        try:
            raw_prior_credit = horizon_prior_counts.get("prior_unavail_credit_calls", {}) or {}
            prior_credit_calls_per_surgeon = {int(k): int(v) for k, v in raw_prior_credit.items()}
        except Exception:
            prior_credit_calls_per_surgeon = {}

    # --- Per-group fairness terms (replace overall fairness) ---
    group_fairness_diffs = []
    # Soft per-cohort cap overflow (how much a cohort's range exceeds the cap).
    # Minimized lexicographically before everything else so <= cap holds whenever
    # feasible and is exceeded by the minimum only in cohorts where it's impossible.
    group_cap_overflows = []

    def _add_cohort_cap_overflow(tag, diff_var):
        """Soft cap: overflow = max(0, diff - cap). Returns the overflow var.

        Used in auto_relax mode so the <= cap target is kept per-cohort whenever
        feasible and exceeded by the minimum only in the cohort(s) where it is
        impossible, instead of dropping the cap across every cohort.
        """
        ov = model.NewIntVar(0, fairness_abs_bound * 2, f"{tag}_cap_overflow")
        add_named_constraint(f"{tag} cap overflow >= diff - cap",
            model.Add, ov >= diff_var - fairness_hard_cap_effective)
        group_cap_overflows.append(ov)
        return ov

    # L1 dispersion terms. The range (max-min) keeps the spread within the hard
    # cap (<=1) where feasible, but range is blind to the interior distribution:
    # [4,6,6,6,6,6] and [5,5,5,6,6,6] both have range 2, yet the second is far
    # more balanced. These terms add the scaled sum of absolute deviations from
    # each cohort mean, so among all min-range solutions the solver prefers the
    # flattest one (and degrades gracefully when a cohort must exceed range 1).
    group_l1_terms = []

    def _accumulate_l1_dispersion(tag, totals):
        """Append scaled |total_s - mean| terms for a cohort to group_l1_terms.

        We avoid fractional means by scaling: dev_s = n*total_s - sum(totals),
        which equals n*(total_s - mean). Minimizing sum_s |dev_s| minimizes the
        L1 distance to the mean for the whole cohort.
        """
        if not enable_deviation_sum:
            return
        n = len(totals)
        if n <= 1:
            return
        dev_bound = 2 * fairness_abs_bound * n
        sum_t = model.NewIntVar(-fairness_abs_bound * n, fairness_abs_bound * n, f"{tag}_l1_sum")
        add_named_constraint(f"{tag} L1 sum", model.Add, sum_t == sum(totals))
        for idx, t in enumerate(totals):
            dev = model.NewIntVar(-dev_bound, dev_bound, f"{tag}_l1_dev_{idx}")
            add_named_constraint(f"{tag} L1 dev {idx}", model.Add, dev == n * t - sum_t)
            a = model.NewIntVar(0, dev_bound, f"{tag}_l1_abs_{idx}")
            add_named_constraint(f"{tag} L1 abs {idx}", model.AddAbsEquality, a, dev)
            group_l1_terms.append(a)

    # Group 1: (1A + 1B); optionally merge Level-2 Group 1 (supervised 2A-only) for fairness when enabled
    group_level1_ids = [s["id"] for s in surgeons if set(parse_call_levels(s.get("call_levels",""))).intersection({"1A","1B"}) and s["id"] not in nlth_ids]
    if enable_l2g1_primary_calls and group1_ids:
        group_level1_ids = sorted(set(group_level1_ids) | {sid for sid in group1_ids if sid not in nlth_ids})
    if len(group_level1_ids) > 1:
        lvl1_counts = {s: model.NewIntVar(0, num_days, f"lvl1_count_{s}") for s in group_level1_ids}
        for s in group_level1_ids:
            # Count once per day when the same surgeon holds both 1A and 1B.
            add_named_constraint(f"(1A+1B) count for surgeon {s}",
                model.Add, lvl1_counts[s] == sum(lvl1_any[(s, d)] for d in range(num_days)))
        fairness_vars_lvl1 = []
        for s in group_level1_ids:
            # prior horizon counts (1A+1B), counting a same-day 1A+1B as one call.
            # Prefer the per-day deduplicated count when available; fall back to
            # prior 1A + prior 1B for older horizon payloads without it.
            prior_total = 0
            if isinstance(horizon_prior_counts, dict):
                try:
                    prior_level1_days = horizon_prior_counts.get("prior_level1_days")
                    if isinstance(prior_level1_days, dict):
                        prior_total = int(prior_level1_days.get(s, 0))
                    else:
                        prior_levels = horizon_prior_counts.get("prior_levels", {})
                        prior_total = int(prior_levels.get("1A", {}).get(s, 0)) + int(prior_levels.get("1B", {}).get(s, 0))
                except Exception:
                    prior_total = 0
            # Credit semantics for fairness caps:
            # +1 credit means "one fewer expected raw call", so we add credit to
            # the adjusted fairness total (not subtract). This pushes the solver
            # to give fewer raw calls to credited surgeons.
            cur_var = lvl1_counts[s]
            if cap_uses_credit:
                cur_adj = model.NewIntVar(-num_days * 2, num_days * 2, f"lvl1_cur_adj_{s}")
                add_named_constraint(f"(1A+1B) current adj {s}", model.Add, cur_adj == cur_var + fairness_credit_calls_per_surgeon.get(s, 0))
                cur_var = cur_adj
            prior_credit = prior_credit_calls_per_surgeon.get(s, 0) if cap_uses_credit else 0
            total = model.NewIntVar(-fairness_abs_bound, fairness_abs_bound, f"lvl1_total_{s}")
            add_named_constraint(f"(1A+1B) horizon total {s}", model.Add, total == cur_var + prior_total + prior_credit)
            fairness_vars_lvl1.append(total)
        gmax = model.NewIntVar(-fairness_abs_bound, fairness_abs_bound, "lvl1_max")
        gmin = model.NewIntVar(-fairness_abs_bound, fairness_abs_bound, "lvl1_min")
        add_named_constraint("Max (1A+1B) count", model.AddMaxEquality, gmax, fairness_vars_lvl1)
        add_named_constraint("Min (1A+1B) count", model.AddMinEquality, gmin, fairness_vars_lvl1)
        diff = model.NewIntVar(0, fairness_abs_bound * 2, "lvl1_diff")
        add_named_constraint("(1A+1B) diff", model.Add, diff == gmax - gmin)
        if use_hard_cap:
            add_named_constraint("Fairness cap: (1A+1B) range <= cap", model.Add, diff <= fairness_hard_cap_effective)
        elif use_soft_cap:
            _add_cohort_cap_overflow("lvl1", diff)
        group_fairness_diffs.append(diff)
        _accumulate_l1_dispersion("lvl1", fairness_vars_lvl1)

    # Group 2 (updated): single (2A + 2B) fairness across ALL L2 subgroups 1, 2, and 3 combined
    l2_union_ids = [s for s in (list(set(group1_ids + group2_ids + group3_ids))) if s not in nlth_ids]
    if len(l2_union_ids) > 1:
        lvl2_counts_all = {s: model.NewIntVar(0, num_days * 2, f"lvl2_all_count_{s}") for s in l2_union_ids}
        for s in l2_union_ids:
            add_named_constraint(f"(2A+2B) all-L2 count for surgeon {s}",
                model.Add, lvl2_counts_all[s] == sum(indicators[(d, lvl, s)] for d in range(num_days) for lvl in ["2A","2B"]))
        fairness_vars_lvl2_all = []
        for s in l2_union_ids:
            prior_2a = 0
            prior_2b = 0
            if isinstance(horizon_prior_counts, dict):
                try:
                    prior_levels = horizon_prior_counts.get("prior_levels", {})
                    prior_2a = int(prior_levels.get("2A", {}).get(s, 0))
                    prior_2b = int(prior_levels.get("2B", {}).get(s, 0))
                except Exception:
                    prior_2a = prior_2b = 0
            prior_total = prior_2a + prior_2b
            cur_var = lvl2_counts_all[s]
            if cap_uses_credit:
                cur_adj = model.NewIntVar(-num_days * 2, num_days * 2, f"lvl2_all_cur_adj_{s}")
                add_named_constraint(f"(2A+2B) all-L2 current adj {s}", model.Add, cur_adj == cur_var + fairness_credit_calls_per_surgeon.get(s, 0))
                cur_var = cur_adj
            prior_credit = prior_credit_calls_per_surgeon.get(s, 0) if cap_uses_credit else 0
            total = model.NewIntVar(-fairness_abs_bound, fairness_abs_bound, f"lvl2_all_total_{s}")
            add_named_constraint(f"(2A+2B) all-L2 horizon total {s}", model.Add, total == cur_var + prior_total + prior_credit)
            fairness_vars_lvl2_all.append(total)
        gmax = model.NewIntVar(-fairness_abs_bound, fairness_abs_bound, "lvl2_all_max")
        gmin = model.NewIntVar(-fairness_abs_bound, fairness_abs_bound, "lvl2_all_min")
        add_named_constraint("Max (2A+2B) all-L2", model.AddMaxEquality, gmax, fairness_vars_lvl2_all)
        add_named_constraint("Min (2A+2B) all-L2", model.AddMinEquality, gmin, fairness_vars_lvl2_all)
        diff = model.NewIntVar(0, fairness_abs_bound * 2, "lvl2_all_diff")
        add_named_constraint("(2A+2B) all-L2 diff", model.Add, diff == gmax - gmin)
        if use_hard_cap:
            add_named_constraint("Fairness cap: (2A+2B) all-L2 range <= cap", model.Add, diff <= fairness_hard_cap_effective)
        elif use_soft_cap:
            _add_cohort_cap_overflow("lvl2_all", diff)
        group_fairness_diffs.append(diff)
        _accumulate_l1_dispersion("lvl2_all", fairness_vars_lvl2_all)

    # Group 3: include all surgeons with level 3, plus L2 subgroup 4;
    # counts include level 3 for everyone, level 4 for Level 3+4 subgroup, and 2B for subgroup 4.
    s3_union_ids = set([s["id"] for s in surgeons if "3" in parse_call_levels(s.get("call_levels",""))] + group4_ids)
    s3_ids = [sid for sid in s3_union_ids if sid not in nlth_ids]
    if len(s3_ids) > 1:
        g3_counts = {s: model.NewIntVar(0, num_days * 3, f"lvl3_union_count_{s}") for s in s3_ids}
        for s in s3_ids:
            terms = [indicators[(d, "3", s)] for d in range(num_days)]
            if s in group4_ids:
                terms += [indicators[(d, "2B", s)] for d in range(num_days)]
            if s in level34_ids:
                terms += [indicators[(d, "4", s)] for d in range(num_days)]
            add_named_constraint(f"(3 [+4 if 3+4] [+2B if grp4]) count for surgeon {s}",
                model.Add, g3_counts[s] == sum(terms))
        fairness_vars_g3 = []
        for s in s3_ids:
            prior_3 = 0
            prior_2b_if_grp4 = 0
            prior_4_if_level34 = 0
            if isinstance(horizon_prior_counts, dict):
                try:
                    prior_levels = horizon_prior_counts.get("prior_levels", {})
                    prior_3 = int(prior_levels.get("3", {}).get(s, 0))
                    if s in group4_ids:
                        prior_2b_if_grp4 = int(prior_levels.get("2B", {}).get(s, 0))
                    if s in level34_ids:
                        prior_4_if_level34 = int(prior_levels.get("4", {}).get(s, 0))
                except Exception:
                    prior_3 = prior_2b_if_grp4 = prior_4_if_level34 = 0
            prior_total = prior_3 + prior_2b_if_grp4 + prior_4_if_level34
            cur_var = g3_counts[s]
            if cap_uses_credit:
                cur_adj = model.NewIntVar(-num_days * 3, num_days * 3, f"lvl3_union_cur_adj_{s}")
                add_named_constraint(f"lvl3 union current adj {s}", model.Add, cur_adj == cur_var + fairness_credit_calls_per_surgeon.get(s, 0))
                cur_var = cur_adj
            prior_credit = prior_credit_calls_per_surgeon.get(s, 0) if cap_uses_credit else 0
            total = model.NewIntVar(-fairness_abs_bound, fairness_abs_bound, f"lvl3_union_total_{s}")
            add_named_constraint(f"lvl3 union horizon total {s}", model.Add, total == cur_var + prior_total + prior_credit)
            fairness_vars_g3.append(total)
        gmax = model.NewIntVar(-fairness_abs_bound, fairness_abs_bound, "lvl3_union_max")
        gmin = model.NewIntVar(-fairness_abs_bound, fairness_abs_bound, "lvl3_union_min")
        add_named_constraint("Max lvl3 union count", model.AddMaxEquality, gmax, fairness_vars_g3)
        add_named_constraint("Min lvl3 union count", model.AddMinEquality, gmin, fairness_vars_g3)
        diff = model.NewIntVar(0, fairness_abs_bound * 2, "lvl3_union_diff")
        add_named_constraint("lvl3 union diff", model.Add, diff == gmax - gmin)
        if use_hard_cap:
            add_named_constraint("Fairness cap: lvl3 union range <= cap", model.Add, diff <= fairness_hard_cap_effective)
        elif use_soft_cap:
            _add_cohort_cap_overflow("lvl3_union", diff)
        group_fairness_diffs.append(diff)
        _accumulate_l1_dispersion("lvl3_union", fairness_vars_g3)

    # Group 4: level 4 only (exclude Level 3+4 subgroup; they balance via G3)
    group4_level_ids = [
        s["id"] for s in surgeons
        if "4" in parse_call_levels(s.get("call_levels",""))
        and s["id"] not in nlth_ids
        and s["id"] not in level34_ids
    ]
    if len(group4_level_ids) > 1:
        lvl4_counts = {s: model.NewIntVar(0, num_days, f"lvl4_count_{s}") for s in group4_level_ids}
        for s in group4_level_ids:
            add_named_constraint(f"(4) count for surgeon {s}",
                model.Add, lvl4_counts[s] == sum(indicators[(d, "4", s)] for d in range(num_days)))
        fairness_vars_lvl4 = []
        for s in group4_level_ids:
            prior_4 = 0
            if isinstance(horizon_prior_counts, dict):
                try:
                    prior_levels = horizon_prior_counts.get("prior_levels", {})
                    prior_4 = int(prior_levels.get("4", {}).get(s, 0))
                except Exception:
                    prior_4 = 0
            cur_var = lvl4_counts[s]
            if cap_uses_credit:
                cur_adj = model.NewIntVar(-num_days, num_days, f"lvl4_cur_adj_{s}")
                add_named_constraint(f"(4) current adj {s}", model.Add, cur_adj == cur_var + fairness_credit_calls_per_surgeon.get(s, 0))
                cur_var = cur_adj
            prior_credit = prior_credit_calls_per_surgeon.get(s, 0) if cap_uses_credit else 0
            total = model.NewIntVar(-fairness_abs_bound, fairness_abs_bound, f"lvl4_total_{s}")
            add_named_constraint(f"(4) horizon total {s}", model.Add, total == cur_var + prior_4 + prior_credit)
            fairness_vars_lvl4.append(total)
        gmax = model.NewIntVar(-fairness_abs_bound, fairness_abs_bound, "lvl4_max")
        gmin = model.NewIntVar(-fairness_abs_bound, fairness_abs_bound, "lvl4_min")
        add_named_constraint("Max (4) count", model.AddMaxEquality, gmax, fairness_vars_lvl4)
        add_named_constraint("Min (4) count", model.AddMinEquality, gmin, fairness_vars_lvl4)
        diff = model.NewIntVar(0, fairness_abs_bound * 2, "lvl4_diff")
        add_named_constraint("(4) diff", model.Add, diff == gmax - gmin)
        if use_hard_cap:
            add_named_constraint("Fairness cap: (4) range <= cap", model.Add, diff <= fairness_hard_cap_effective)
        elif use_soft_cap:
            _add_cohort_cap_overflow("lvl4", diff)
        group_fairness_diffs.append(diff)
        _accumulate_l1_dispersion("lvl4", fairness_vars_lvl4)

    # Overall total-call fairness among surgeons with identical call-level eligibility.
    # Enabled by default via enable_fairness_diff_all; keeps total calls within cap
    # for peers who can cover the same levels (in addition to per-cohort caps).
    if enable_fairness_diff_all:
        from collections import defaultdict

        peer_groups = defaultdict(list)
        for s in surgeons:
            sid = s.get("id")
            if sid is None or sid in nlth_ids:
                continue
            levels_key = tuple(sorted(parse_call_levels(s.get("call_levels", ""))))
            if not levels_key:
                continue
            peer_groups[levels_key].append(sid)
        for peer_idx, (levels_key, peer_ids) in enumerate(sorted(peer_groups.items(), key=lambda x: x[0])):
            if len(peer_ids) <= 1:
                continue
            tag = f"overall_peer_{peer_idx}"
            fairness_vars_peer = []
            for sid in peer_ids:
                prior_total = _horizon_prior_overall_calls(sid, horizon_prior_counts)
                cur_var = call_count_overall[sid]
                if cap_uses_credit:
                    cur_adj = model.NewIntVar(
                        -num_days * len(all_levels),
                        num_days * len(all_levels),
                        f"{tag}_cur_adj_{sid}",
                    )
                    add_named_constraint(
                        f"{tag} current adj {sid}",
                        model.Add,
                        cur_adj == cur_var + fairness_credit_calls_per_surgeon.get(sid, 0),
                    )
                    cur_var = cur_adj
                prior_credit = prior_credit_calls_per_surgeon.get(sid, 0) if cap_uses_credit else 0
                total = model.NewIntVar(-fairness_abs_bound, fairness_abs_bound, f"{tag}_total_{sid}")
                add_named_constraint(
                    f"{tag} horizon total {sid}",
                    model.Add,
                    total == cur_var + prior_total + prior_credit,
                )
                fairness_vars_peer.append(total)
            gmax = model.NewIntVar(-fairness_abs_bound, fairness_abs_bound, f"{tag}_max")
            gmin = model.NewIntVar(-fairness_abs_bound, fairness_abs_bound, f"{tag}_min")
            add_named_constraint(f"Max {tag}", model.AddMaxEquality, gmax, fairness_vars_peer)
            add_named_constraint(f"Min {tag}", model.AddMinEquality, gmin, fairness_vars_peer)
            diff = model.NewIntVar(0, fairness_abs_bound * 2, f"{tag}_diff")
            add_named_constraint(f"{tag} diff", model.Add, diff == gmax - gmin)
            if use_hard_cap:
                add_named_constraint(
                    f"Fairness cap: {tag} range <= cap",
                    model.Add,
                    diff <= fairness_hard_cap_effective,
                )
            elif use_soft_cap:
                _add_cohort_cap_overflow(tag, diff)
            group_fairness_diffs.append(diff)
            _accumulate_l1_dispersion(tag, fairness_vars_peer)

    # --- Then, create a per-surgeon, per-day “assigned” BoolVar ----
    assigned = {}
    for s in all_surgeon_ids:
        for d in range(num_days):
            a = model.NewBoolVar(f"assigned_s{s}_d{d}")
            add_named_constraint(f"Assigned check: Day {d} surgeon {s} >=1 slot",
                model.Add, sum(indicators[(d, level, s)] for level in all_levels) >= 1
            ).OnlyEnforceIf(a)
            add_named_constraint(f"Assigned check: Day {d} surgeon {s} ==0 slots",
                model.Add, sum(indicators[(d, level, s)] for level in all_levels) == 0
            ).OnlyEnforceIf(a.Not())
            assigned[(s, d)] = a

    # --- NLTH constraint (toggle) --- allow NLTH surgeons only on days where today AND tomorrow are weekend or PH
    is_weekend = [datetime.datetime.strptime(day, "%Y-%m-%d").weekday() >= 5 for day in days]
    is_ph = [day in public_holidays for day in days]
    is_wk_or_ph = [w or ph for w, ph in zip(is_weekend, is_ph)]
    if enable_nlth_rules:
        for d in range(num_days):
            allow_nlth = (d < num_days - 1) and is_wk_or_ph[d] and is_wk_or_ph[d+1]
            if not allow_nlth:
                for lvl in all_levels:
                    for s_id in nlth_ids:
                        # If this (day, level) is preassigned to this NLTH surgeon, allow override
                        fixed_id = preassigned_fixed.get((d, lvl))
                        if fixed_id is not None and fixed_id == s_id:
                            continue
                        add_named_constraint(f"NLTH ban: Day {days[d]}, level {lvl} cannot be surgeon {s_id}",
                            model.Add, X[(d, lvl)] != s_id)
    
    # --- Soft penalties for weekend call balance (toggle) ---
    w_call = {}
    for s in all_surgeon_ids:
        for d in range(num_days):
            w = model.NewBoolVar(f"weekend_call_s{s}_d{d}")
            w_call[(s,d)] = w
            if enable_weekend_balance:
                if is_weekend[d]:
                    add_named_constraint(f"Weekend call assignment: Day {days[d]}, surgeon {s}",
                        model.Add, w == assigned[(s,d)])
                else:
                    add_named_constraint(f"Weekday weekend call: Day {days[d]}, surgeon {s}",
                        model.Add, w == 0)
            else:
                add_named_constraint(f"Weekend call disabled: Day {days[d]}, surgeon {s}", model.Add, w == 0)
    
    weekend_count = {}
    for s in all_surgeon_ids:
        wc = model.NewIntVar(0, num_days, f"weekend_count_s{s}")
        add_named_constraint(f"Count weekend calls for surgeon {s}",
            model.Add, wc == sum(w_call[(s,d)] for d in range(num_days)))
        weekend_count[s] = wc
        # Hard cap for non-NLTH only, using configurable value
        if enable_weekend_balance and s not in nlth_ids:
            add_named_constraint(f"Max weekend calls for surgeon {s}", model.Add, wc <= max_weekend_calls_cfg)
    
    consec_penalties = []
    if enable_weekend_consecutive_pen:
        for s in all_surgeon_ids:
            for d in range(num_days-1):
                if is_weekend[d] and is_weekend[d+1]:
                    b = model.NewBoolVar(f"consec_wknd_s{s}_d{d}")
                    add_named_constraint(f"Consecutive weekend: surgeon {s} days {d} & {d+1} both assigned",
                        model.AddBoolAnd, [w_call[(s,d)], w_call[(s,d+1)]]
                    ).OnlyEnforceIf(b)
                    add_named_constraint(f"Consecutive weekend negation: surgeon {s} days {d} & {d+1} not both assigned",
                        model.AddBoolOr, [w_call[(s,d)].Not(), w_call[(s,d+1)].Not()]
                    ).OnlyEnforceIf(b.Not())
                    consec_penalties.append(b)
    
    if consec_penalties:
        consec_penalty = model.NewIntVar(0, len(consec_penalties), "penalty_consec_weekend")
        add_named_constraint("Consecutive weekend penalty", model.Add, consec_penalty == sum(consec_penalties))
    else:
        consec_penalty = model.NewIntVar(0, 0, "penalty_consec_weekend")
        add_named_constraint("Consecutive weekend penalty zero", model.Add, consec_penalty == 0)
    
    weekend_grp1_ids = [s["id"] for s in surgeons if set(parse_call_levels(s.get("call_levels",""))).intersection({"1A", "1B"})]
    if enable_l2g1_primary_calls and group1_ids:
        weekend_grp1_ids = sorted(set(weekend_grp1_ids) | set(group1_ids))

    weekend_diff_terms = []
    for i, (_, grp) in enumerate([
        ("grp1", weekend_grp1_ids),
        ("grp2", [s["id"] for s in surgeons if ( "2A" in parse_call_levels(s.get("call_levels","")) or "2B" in parse_call_levels(s.get("call_levels",""))) and "3" not in parse_call_levels(s.get("call_levels",""))]),
        ("grp3", [s["id"] for s in surgeons if "3" in parse_call_levels(s.get("call_levels",""))]),
        ("grp4", [s["id"] for s in surgeons if "4" in parse_call_levels(s.get("call_levels",""))])
    ], start=1):
        # Exclude NLTH surgeons from the fairness group
        grp_clean = [s for s in grp if s not in nlth_ids]
        if len(grp_clean) > 1:
            max_w = model.NewIntVar(0, num_days, f"max_wknd_grp{i}")
            min_w = model.NewIntVar(0, num_days, f"min_wknd_grp{i}")
            add_named_constraint(f"Max weekend count for group {i}", model.AddMaxEquality, max_w, [weekend_count[s] for s in grp])
            add_named_constraint(f"Min weekend count for group {i}", model.AddMinEquality, min_w, [weekend_count[s] for s in grp])
            diff = model.NewIntVar(0, num_days, f"diff_wknd_grp{i}")
            add_named_constraint(f"Weekend difference for group {i}", model.Add, diff == max_w - min_w)
            if enable_weekend_balance:
                weekend_diff_terms.append(diff)

    # --- Weekend team diversity balance (soft): balance how many weekend days each team appears on ---
    # Build team -> surgeon IDs map (exclude None teams)
    teams = sorted({s.get("team") for s in surgeons if s.get("team")})
    team_to_ids = {t: [s["id"] for s in surgeons if s.get("team") == t] for t in teams}
    weekend_days = [d for d in range(num_days) if is_weekend[d]]

    team_presence_vars = { }
    if enable_weekend_team_diversity:
        for t in teams:
            for d in weekend_days:
                p = model.NewBoolVar(f"presence_team_{t}_d{d}")
                team_presence_vars[(t,d)] = p
                # p == OR(indicators[(d, level, s)] for all levels and s in team t)
                ors = [indicators[(d, lvl, sid)] for lvl in all_levels for sid in team_to_ids[t]]
                if ors:
                    add_named_constraint(f"Team presence upper {t} d{d}", model.Add, p <= sum(ors))
                    for v in ors:
                        add_named_constraint(f"Team presence lower {t} d{d}", model.Add, v <= p)
                else:
                    add_named_constraint(f"Team presence none {t} d{d}", model.Add, p == 0)

    # Build unique presence terms for weekend diversity objective (with carryover)
    unique_presence_vars = []
    if enable_weekend_team_diversity and weekend_days:
        prev_levels_for_carry = ["1A","1B","2A","2B"]
        for t in teams:
            for d in weekend_days:
                carry = model.NewBoolVar(f"carry_team_{t}_d{d}")
                if d - 1 >= 0:
                    ors_prev = [indicators[(d-1, lvl, sid)] for lvl in prev_levels_for_carry for sid in team_to_ids[t]]
                    if ors_prev:
                        add_named_constraint(f"Carry upper {t} d{d}", model.Add, carry <= sum(ors_prev))
                        for v in ors_prev:
                            add_named_constraint(f"Carry lower {t} d{d}", model.Add, v <= carry)
                    else:
                        add_named_constraint(f"Carry none {t} d{d}", model.Add, carry == 0)
                else:
                    add_named_constraint(f"Carry out of range {t} d{d}", model.Add, carry == 0)

                # unique presence = OR(day presence, carry)
                pu = model.NewBoolVar(f"presence_unique_team_{t}_d{d}")
                pd = team_presence_vars.get((t,d)) if enable_weekend_team_diversity else None
                if pd is not None:
                    add_named_constraint(f"PU ge presence {t} d{d}", model.Add, pu >= pd)
                    add_named_constraint(f"PU ge carry {t} d{d}", model.Add, pu >= carry)
                    add_named_constraint(f"PU le sum {t} d{d}", model.Add, pu <= pd + carry)
                    unique_presence_vars.append(pu)

    # Count weekend presence per team (per-day OR, not per-slot)
    team_weekend_counts = {}
    for t in teams:
        cnt = model.NewIntVar(0, len(weekend_days), f"team_weekend_count_{t}")
        if enable_weekend_team_diversity and weekend_days:
            add_named_constraint(f"Team weekend count {t}", model.Add, cnt == sum(team_presence_vars[(t,d)] for d in weekend_days))
        else:
            add_named_constraint(f"Team weekend count zero {t}", model.Add, cnt == 0)
        team_weekend_counts[t] = cnt

    # Deprecated balance metric retained as zero to keep variable references simple
    team_weekend_diff = model.NewIntVar(0, 0, "team_weekend_diff")
    add_named_constraint("Team weekend diff zero (replaced by unique teams objective)", model.Add, team_weekend_diff == 0)
    
    td_terms = []
    for d, day_str in enumerate(days):
        wd = datetime.datetime.strptime(day_str, "%Y-%m-%d").weekday()
        for lvl in all_levels:
            for s in surgeons:
                sid  = s['id']
                team = s.get('team')
                if team in team_day_prefs:
                    try:
                        adj = int(team_day_prefs[team].get(wd, 0))
                    except Exception:
                        adj = 0
                    if adj in (-1, 1):
                        b = indicators[(d, lvl, sid)]
                        coef = gamma_team_pref * adj
                        td_terms.append((coef, b))
    
    # --- Soft Penalties for Availability ---
    include_unavail_prev_penalty = (pre_unavail_mode == "soft") and enable_unavail_prev_penalty
    soft_penalties_unavail_prev = []
    if include_unavail_prev_penalty:
        for i in range(num_days - 1):
            next_day = datetime.datetime.strptime(days[i+1], "%Y-%m-%d").date()
            for s_id, req_list in availability.items():
                for req in req_list:
                    req_date = _parse_req_date(req.get("date"))
                    if not req_date:
                        continue
                    if req_date == next_day and req.get("request_type") in ("unavailable",):
                        for lev in all_levels:
                            b = model.NewBoolVar(f'penalty_unavailprev_{i}_{lev}_{s_id}')
                            add_named_constraint(f"Availability Prev: Day {i} {lev} equals surgeon {s_id}",
                                model.Add, X[(i, lev)] == s_id
                            ).OnlyEnforceIf(b)
                            add_named_constraint(f"Availability Prev: Day {i} {lev} not equals surgeon {s_id}",
                                model.Add, X[(i, lev)] != s_id
                            ).OnlyEnforceIf(b.Not())
                            soft_penalties_unavail_prev.append(b)
    
    soft_penalties_nocall = []
    if not no_call_hard:
        for i, day in enumerate(days):
            target_date = datetime.datetime.strptime(day, "%Y-%m-%d").date()
            for s_id, req_list in availability.items():
                for req in req_list:
                    try:
                        req_date = _parse_req_date(req.get("date"))
                    except Exception:
                        continue
                    if req_date and req_date == target_date and req.get("request_type") == "no_call":
                        for lev in all_levels:
                            b = model.NewBoolVar(f'penalty_nocall_{i}_{lev}_{s_id}')
                            add_named_constraint(f"No call penalty: Day {i} {lev} equals surgeon {s_id}",
                                model.Add, X[(i, lev)] == s_id
                            ).OnlyEnforceIf(b)
                            add_named_constraint(f"No call penalty: Day {i} {lev} not equals surgeon {s_id}",
                                model.Add, X[(i, lev)] != s_id
                            ).OnlyEnforceIf(b.Not())
                            soft_penalties_nocall.append(b)
        # Team-day No Call mode in soft mode: penalize assigning surgeons from blocked teams
        for i, day in enumerate(days):
            target_date = datetime.datetime.strptime(day, "%Y-%m-%d").date()
            blocked_teams = team_day_no_call_by_weekday.get(target_date.weekday(), set())
            if not blocked_teams:
                continue
            for surgeon in surgeons:
                s_id = surgeon.get("id")
                if s_id is None:
                    continue
                if surgeon.get("team") not in blocked_teams:
                    continue
                for lev in all_levels:
                    b = model.NewBoolVar(f"penalty_team_nocall_{i}_{lev}_{s_id}")
                    add_named_constraint(
                        f"Team no-call penalty: Day {i} {lev} equals surgeon {s_id}",
                        model.Add, X[(i, lev)] == s_id
                    ).OnlyEnforceIf(b)
                    add_named_constraint(
                        f"Team no-call penalty: Day {i} {lev} not equals surgeon {s_id}",
                        model.Add, X[(i, lev)] != s_id
                    ).OnlyEnforceIf(b.Not())
                    soft_penalties_nocall.append(b)
    
    penalty_unavail_prev = model.NewIntVar(0, num_days * len(all_levels) * 10, 'penalty_unavail_prev')
    if soft_penalties_unavail_prev:
        add_named_constraint("Penalty unavail_prev", model.Add, penalty_unavail_prev == sum(soft_penalties_unavail_prev))
    else:
        add_named_constraint("Penalty unavail_prev zero", model.Add, penalty_unavail_prev == 0)
    
    penalty_nocall = model.NewIntVar(0, num_days * len(all_levels) * 10, 'penalty_nocall')
    if soft_penalties_nocall:
        add_named_constraint("Penalty nocall", model.Add, penalty_nocall == sum(soft_penalties_nocall))
    else:
        add_named_constraint("Penalty nocall zero", model.Add, penalty_nocall == 0)
    
    # --- Total calls (used for unavailability credit scaling) ---
    N = len(all_surgeon_ids)
    T = model.NewIntVar(0, num_days * len(all_levels) * N, "T")
    add_named_constraint("Total calls T", model.Add, T == sum(call_count_overall[s] for s in all_surgeon_ids))
    
    # --- Unavailability credit (soft): for each 7 days unavailable, allow one fewer call without penalty ---
    # Count per-surgeon unavailable days in this month
    unavail_days_per_surgeon = {s_id: 0 for s_id in all_surgeon_ids}
    day_set = {datetime.datetime.strptime(d, "%Y-%m-%d").date() for d in days}
    for s_id, req_list in availability.items():
        u_days = {
            datetime.datetime.strptime(req["date"], "%Y-%m-%d").date()
            for req in req_list
            if req.get("request_type") in ("unavailable","study_leave")
            if isinstance(req.get("date"), str)
        }
        # include only dates in current month days
        unavail_days_per_surgeon[s_id] = len(u_days & day_set)

    # Overflow above (avg - credit) measured in N-scaled units to avoid division
    unavail_overflows = []
    if enable_unavail_credit:
        for s in all_surgeon_ids:
            credit_units = _credit_calls_from_unavailability(unavail_days_per_surgeon.get(s, 0), unavail_credit_days) * N
            # os >= call_count[s]*N - T + credit_units ; os >= 0
            os = model.NewIntVar(0, num_days * len(all_levels) * N, f"unavail_overflow_{s}")
            tmp = model.NewIntVar(-num_days * len(all_levels) * N, num_days * len(all_levels) * N, f"tmp_overflow_{s}")
            add_named_constraint(f"Tmp overflow expr {s}", model.Add, tmp == call_count_overall[s] * N - T + credit_units)
            add_named_constraint(f"Overflow lower bound {s}", model.Add, os >= tmp)
            add_named_constraint(f"Overflow nonneg {s}", model.Add, os >= 0)
            unavail_overflows.append(os)

    # --- Create soft‐penalties for any two calls within spacing_threshold days ---
    spacing_penalties = []
    for s in all_surgeon_ids:
        for d in range(num_days):
            for d2 in range(d + 1, min(num_days, d + spacing_threshold)):
                b = model.NewBoolVar(f"close_{s}_{d}_{d2}")
                add_named_constraint(f"Spacing: Surgeon {s} days {d} & {d2} both assigned",
                    model.AddBoolAnd, [assigned[(s, d)], assigned[(s, d2)]]
                ).OnlyEnforceIf(b)
                add_named_constraint(f"Spacing negation: Surgeon {s} days {d} & {d2} not both assigned",
                    model.AddBoolOr, [assigned[(s, d)].Not(), assigned[(s, d2)].Not()]
                ).OnlyEnforceIf(b.Not())
                spacing_penalties.append(b)
    
    penalty_spacing = model.NewIntVar(0, len(spacing_penalties), "penalty_spacing")
    if spacing_penalties:
        add_named_constraint("Penalty spacing", model.Add, penalty_spacing == sum(spacing_penalties))
    else:
        add_named_constraint("Penalty spacing zero", model.Add, penalty_spacing == 0)
    
    # --- Strongly discourage empty slots; only use when necessary (allow_empty mode) ---
    empty_indicators = []
    if allow_empty:
        for d in range(num_days):
            for lvl in all_levels:
                if lvl == "2B":
                    continue  # do not penalize empty 2B slots
                b = model.NewBoolVar(f"empty_{d}_{lvl}")
                add_named_constraint(f"Empty slot on day {days[d]} level {lvl}",
                    model.Add, X[(d, lvl)] == -1
                ).OnlyEnforceIf(b)
                add_named_constraint(f"Non-empty slot on day {days[d]} level {lvl}",
                    model.Add, X[(d, lvl)] != -1
                ).OnlyEnforceIf(b.Not())
                empty_indicators.append(b)

    l2g1_missing_2a_pair_flags = []
    if (
        enable_l2g1_primary_calls
        and enable_l2g1_primary_2a_same_day_penalty
        and gamma_l2g1_primary_2a_same_day > 0
        and group1_ids
    ):
        for d in range(num_days):
            ors_1a = [indicators[(d, "1A", sid)] for sid in group1_ids]
            ors_2a = [indicators[(d, "2A", sid)] for sid in group1_ids]
            any_1a = model.NewBoolVar(f"l2g1_any_1a_d{d}")
            any_2a = model.NewBoolVar(f"l2g1_any_2a_d{d}")
            missing_pair_d = model.NewBoolVar(f"l2g1_missing_2a_pair_d{d}")
            add_named_constraint(f"L2G1 any 1A on {days[d]}", model.AddMaxEquality, any_1a, ors_1a)
            add_named_constraint(f"L2G1 any 2A on {days[d]}", model.AddMaxEquality, any_2a, ors_2a)
            add_named_constraint(
                f"L2G1 missing 2A pair on {days[d]}",
                model.AddBoolAnd,
                [any_1a, any_2a.Not()],
            ).OnlyEnforceIf(missing_pair_d)
            add_named_constraint(
                f"L2G1 has 2A pair on {days[d]}",
                model.AddBoolOr,
                [any_1a.Not(), any_2a],
            ).OnlyEnforceIf(missing_pair_d.Not())
            l2g1_missing_2a_pair_flags.append(missing_pair_d)

    objective_terms = []
    # Remove overall fairness term; use per-group fairness instead
    if enable_nocall_penalty:
        objective_terms.append(gamma_no_call * penalty_nocall)
    if include_unavail_prev_penalty:
        objective_terms.append(gamma_unavail_prev * penalty_unavail_prev)
    # Remove deviation-from-average fairness term
    if enable_unavail_credit and 'unavail_overflows' in locals() and unavail_overflows:
        objective_terms.append(gamma_unavail_credit * sum(unavail_overflows))
    if enable_spacing_penalty:
        objective_terms.append(gamma_spacing * penalty_spacing)
    if enable_weekend_balance and weekend_diff_terms:
        objective_terms.append(gamma_weekend_balance * (sum(weekend_diff_terms) * 10))
    if enable_weekend_consecutive_pen and isinstance(consec_penalty, cp_model.IntVar):
        objective_terms.append(gamma_consec_weekend * consec_penalty)
    if enable_weekend_team_diversity and unique_presence_vars:
        # maximize unique teams per weekend day by minimizing negative sum
        objective_terms.append(- gamma_weekend_team_diversity * sum(unique_presence_vars))
    if l2g1_missing_2a_pair_flags:
        objective_terms.append(gamma_l2g1_primary_2a_same_day * sum(l2g1_missing_2a_pair_flags))
    # team day preferences
    if enable_team_day_prefs and td_terms:
        objective_terms += [- coef * b for coef, b in td_terms]

    # Soft per-cohort cap overflow: penalize exceeding the <= cap target even more
    # strongly than in-cap range, so the solver only ever goes over the cap in a
    # cohort when it is genuinely impossible to stay within it.
    cap_overflow_total = None
    if group_cap_overflows:
        cap_overflow_total = model.NewIntVar(0, fairness_abs_bound * 2 * len(group_cap_overflows), "cap_overflow_total")
        add_named_constraint("Cap overflow total", model.Add, cap_overflow_total == sum(group_cap_overflows))
        objective_terms.append((fairness_weight * 1000) * cap_overflow_total)

    # Add per-group fairness terms
    if group_fairness_diffs:
        objective_terms.extend([fairness_weight * diff for diff in group_fairness_diffs])
    fairness_total = None
    if group_fairness_diffs:
        fairness_total = model.NewIntVar(0, fairness_abs_bound * 2 * len(group_fairness_diffs), "fairness_total")
        add_named_constraint("Fairness total", model.Add, fairness_total == sum(group_fairness_diffs))
    l1_total = None
    if enable_deviation_sum and group_l1_terms:
        _l1_ub = 2 * fairness_abs_bound * max(1, len(all_surgeon_ids)) * len(group_l1_terms)
        l1_total = model.NewIntVar(0, _l1_ub, "fairness_l1_total")
        add_named_constraint("Fairness L1 total", model.Add, l1_total == sum(group_l1_terms))

    # --- Soft penalty for using 2B (reduce supervisor overload when not required) ---
    # Apply whenever gamma_2b_usage > 0 (toggle is optional)
    if gamma_2b_usage > 0:
        two_b_usage_terms = []
        for d in range(num_days):
            # Penalize any non-empty 2B assignment
            b = model.NewBoolVar(f"use_2B_d{d}")
            add_named_constraint(f"2B used on day {days[d]}", model.Add, X[(d, "2B")] != -1).OnlyEnforceIf(b)
            add_named_constraint(f"2B not used on day {days[d]}", model.Add, X[(d, "2B")] == -1).OnlyEnforceIf(b.Not())
            two_b_usage_terms.append(b)
        objective_terms.append(gamma_2b_usage * sum(two_b_usage_terms))

    # --- Soft penalty: reduce urology-only surgeon calls on weekends/PH ---
    # uro_only_on_d_var[d] is 1 when a urology-only surgeon holds the Urology call
    # that day. Penalizing it on weekend and public-holiday days nudges the solver
    # to cover those Urology calls with surgeons who also carry other levels
    # (e.g. 1B+Urology), when feasible, without forbidding urology-only calls outright.
    if enable_urology_weekend_penalty and gamma_urology_weekend > 0 and urology_only_ids:
        uro_weekend_terms = [uro_only_on_d_var[d] for d in range(num_days) if is_wk_or_ph[d]]
        if uro_weekend_terms:
            objective_terms.append(gamma_urology_weekend * sum(uro_weekend_terms))

    normal_objective_expr = sum(objective_terms) if objective_terms else 0
    empty_count_expr = sum(empty_indicators) if empty_indicators else 0

    def _build_solver():
        _solver = cp_model.CpSolver()
        try:
            _solver.parameters.max_time_in_seconds = int(time_limit_seconds)
        except Exception:
            _solver.parameters.max_time_in_seconds = 30
        _solver.parameters.log_search_progress = True
        _solver.parameters.log_to_stdout = True
        # Use a parallel portfolio of search workers to converge faster within the
        # time budget. This matters because the soft cap relies on the solver
        # actually reaching the minimum-range solution before the limit.
        try:
            _solver.parameters.num_search_workers = 8
        except Exception:
            pass
        return _solver

    # --- Solve the Model ---
    # In allow-empty mode, use lexicographic objective priority:
    # 1) minimize count of empty non-2B slots, 2) optimize normal objective.
    # Outside allow-empty, preserve existing fairness-first two-pass behavior.
    use_two_pass_fairness = enable_two_pass_fairness_priority and (fairness_total is not None)

    def _solve_secondary_objective():
        # Lexicographic fairness priority (each stage is locked before the next):
        #   0) minimize the total soft-cap overflow so every cohort stays within
        #      the <= cap (max-min <= 1) target whenever feasible, and exceeds it
        #      by the minimum only in the cohort(s) where it is impossible.
        #   1) minimize the sum of cohort ranges (max-min). With the hard cap on,
        #      this stays <= 1; with the soft cap it tightens the spread further.
        #   2) minimize L1 dispersion so that, among all equally-tight-range
        #      solutions, the interior is as flat (well distributed) as possible.
        #   3) minimize the remaining soft objective.
        lex_stages = []
        if use_two_pass_fairness and cap_overflow_total is not None:
            # Highest priority: keep every cohort within the <= cap target,
            # exceeding it only by the minimum where it is impossible.
            lex_stages.append(("fairness cap overflow", cap_overflow_total))
        if use_two_pass_fairness and fairness_total is not None:
            lex_stages.append(("fairness range total", fairness_total))
        if l1_total is not None:
            lex_stages.append(("fairness L1 dispersion", l1_total))

        if lex_stages:
            last_solver = None
            last_status = None
            for stage_name, stage_expr in lex_stages:
                add_named_constraint(f"Objective {stage_name}", model.Minimize, stage_expr)
                stage_solver = _build_solver()
                stage_status = stage_solver.Solve(model)
                if stage_status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                    return stage_solver, stage_status
                best_stage = int(stage_solver.Value(stage_expr))
                add_named_constraint(f"Lock {stage_name}", model.Add, stage_expr <= best_stage)
                last_solver, last_status = stage_solver, stage_status
            add_named_constraint("Objective pass normal", model.Minimize, normal_objective_expr)
            final_solver = _build_solver()
            final_status = final_solver.Solve(model)
            if final_status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                return final_solver, final_status
            return last_solver, last_status

        add_named_constraint("Objective", model.Minimize, normal_objective_expr)
        solver_single = _build_solver()
        status_single = solver_single.Solve(model)
        return solver_single, status_single

    if allow_empty and empty_indicators:
        add_named_constraint("Objective pass0 empty count", model.Minimize, empty_count_expr)
        solver_empty = _build_solver()
        status_empty = solver_empty.Solve(model)
        # #region agent log
        _debug_log(
            "scheduler.py:pass0-empty-count",
            "allow_empty pass0 solve status",
            {
                "status": int(status_empty),
                "status_str": solver_empty.StatusName(status_empty),
                "best_empty_count": int(solver_empty.Value(empty_count_expr)) if status_empty in (cp_model.OPTIMAL, cp_model.FEASIBLE) else None,
                "allow_empty": allow_empty,
                "relax_fairness_caps": _relax_fairness_caps,
            },
            hypothesis="H1,H4",
        )
        # #endregion
        if status_empty in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            best_empty_count = int(solver_empty.Value(empty_count_expr))
            add_named_constraint("Lock pass0 empty count", model.Add, empty_count_expr <= best_empty_count)
            solver, status = _solve_secondary_objective()
            # If secondary optimization cannot find a feasible solution in time,
            # return the feasible minimum-empty solution from pass0.
            if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                solver, status = solver_empty, status_empty
        else:
            solver, status = solver_empty, status_empty
    else:
        solver, status = _solve_secondary_objective()
    # #region agent log
    try:
        _empty_by_level = {}
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            for _lvl in all_levels:
                _empty_by_level[_lvl] = sum(1 for _d in range(num_days) if solver.Value(X[(_d, _lvl)]) == -1)
        _debug_log(
            "scheduler.py:final-solve-status",
            "Final solve status and empty counts",
            {
                "status": int(status),
                "status_str": solver.StatusName(status) if hasattr(solver, 'StatusName') else str(status),
                "allow_empty": allow_empty,
                "relax_fairness_caps": _relax_fairness_caps,
                "diagnostic_run": _diagnostic_run,
                "solver_mode_used": solver_mode_used,
                "num_days": num_days,
                "empty_counts_by_level": _empty_by_level,
            },
            hypothesis="H1,H3,H4,H5",
        )
    except Exception as _e:
        _debug_log("scheduler.py:final-solve-status", "log-error", {"err": str(_e)}, hypothesis="H1")
    # #endregion
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        solution = {
            days[d]: {
                level: next((s["name"] for s in surgeons if s["id"] == solver.Value(X[(d,level)])), None)
                for level in all_levels
            } for d in range(num_days)
        }
        # Display-level mirror: when a urology-only surgeon is on Urology, the 1B
        # slot is left empty by the solver but is shown as the 1A surgeon (who
        # clinically covers 1B duty that day). The solver-side 1A+1B fairness
        # cohort is unaffected because we don't add a 1B indicator for that day.
        for d in range(num_days):
            try:
                if solver.Value(uro_only_on_d_var[d]) == 1:
                    solution[days[d]]["1B"] = solution[days[d]]["1A"]
            except Exception:
                pass
        solution["__solver_mode__"] = solver_mode_used
        return solution, solver.ObjectiveValue()
    else:
        # #region agent log
        _debug_log(
            "scheduler.py:infeasible-entered",
            "Solver infeasible, considering auto-relax fallback",
            {
                "enable_fairness_hard_cap": enable_fairness_hard_cap,
                "fairness_fallback_policy": fairness_fallback_policy,
                "_diagnostic_run": _diagnostic_run,
                "_relax_fairness_caps": _relax_fairness_caps,
                "allow_empty": allow_empty,
            },
            hypothesis="H1,H3,H5",
        )
        # #endregion
        # Strict fairness-cap first with optional auto-relax fallback.
        if (
            enable_fairness_hard_cap
            and fairness_fallback_policy == "auto_relax"
            and not _diagnostic_run
            and not _relax_fairness_caps
        ):
            try:
                fallback_sched, fallback_cost = solve_schedule_or_tools(
                    days=days,
                    surgeons=surgeons,
                    prev_schedule=prev_schedule,
                    public_holidays=public_holidays,
                    preassignments=preassignments,
                    time_limit_seconds=time_limit_seconds,
                    allow_empty=allow_empty,
                    _diagnostic_run=True,
                    _relax_fairness_caps=True,
                    horizon_prior_counts=horizon_prior_counts,
                )
                if isinstance(fallback_sched, dict) and "errors" not in fallback_sched:
                    fallback_sched["__solver_mode__"] = "auto_relax_fairness_caps"
                    if solver_debug:
                        print("[FAIRNESS] Strict cap infeasible; returning auto-relaxed fallback schedule.")
                    return fallback_sched, fallback_cost
            except Exception:
                pass

        if solver_debug:
            print("\nModel is INFEASIBLE. Hard constraint summary:")
            for name in constraint_mapping:
                print("  ", name)
        # Return diagnostics to the caller so the UI can display them
        if enable_fairness_hard_cap:
            diagnostics.append(f"Fairness hard cap within call-level cohorts could not be satisfied (cap ≤ {fairness_hard_cap_effective}) under current eligibility/availability.")
            if fairness_fallback_policy == "auto_relax":
                diagnostics.append("Auto-relax fallback was attempted but no feasible fallback schedule was found.")
        else:
            diagnostics.append("No feasible assignment exists under current constraints. Try relaxing constraints or adding eligible surgeons.")

        # --- Detailed Availability & Constraint Analysis ---
        analysis = None
        try:
            analysis = {"culprits": [], "sections": [], "eligibility": []}

            # -- Collect all scored issues for culprit ranking --
            scored_issues = []

            # 1) Bottleneck slots: days/levels with ≤3 eligible surgeons
            bn_items = []
            for d_idx, day_str in enumerate(days):
                for lvl in all_levels:
                    if lvl == "2B":
                        continue
                    eligible = [sid for sid in domains_by_day[d_idx][lvl] if sid != -1]
                    base_count = len([sid for sid in base_domains.get(lvl, []) if sid != -1])
                    if base_count == 0:
                        continue
                    unavail = base_count - len(eligible)
                    if len(eligible) <= 3:
                        dt = datetime.datetime.strptime(day_str, "%Y-%m-%d").date()
                        is_wk = dt.weekday() >= 5
                        is_hol = day_str in (public_holidays or set())
                        names = [id_to_surgeon[sid]["name"] for sid in eligible if sid in id_to_surgeon]
                        severity = "critical" if len(eligible) <= 1 else ("high" if len(eligible) <= 2 else "medium")
                        tag = " [Weekend]" if is_wk else (" [Holiday]" if is_hol else "")
                        item = {
                            "day": day_str, "level": lvl, "eligible": len(eligible),
                            "pool": base_count, "names": names, "tag": tag.strip(),
                            "severity": severity,
                            "text": f"{day_str}{tag} {lvl}: {len(eligible)}/{base_count} eligible → {', '.join(names) or 'NONE'}"
                        }
                        bn_items.append(item)
                        score = (3 - len(eligible)) * 10 + (5 if is_wk or is_hol else 0)
                        scored_issues.append((score, "bottleneck", item))

            if bn_items:
                bn_items.sort(key=lambda x: (x["eligible"], x["day"]))
                analysis["sections"].append({
                    "title": "Bottleneck Slots",
                    "subtitle": f"{len(bn_items)} day/level slots with ≤3 eligible surgeons after availability pruning",
                    "icon": "🔴",
                    "items": bn_items
                })

            # 2) Supervision conflicts
            sup_items = []
            supervisors_set = set(group3_ids + group4_ids)
            for d_idx, day_str in enumerate(days):
                cand_2a = [sid for sid in domains_by_day[d_idx]["2A"] if sid != -1]
                cand_2b_super = [sid for sid in domains_by_day[d_idx]["2B"] if sid in supervisors_set]
                if not cand_2a:
                    continue
                all_need_super = all(sid in group1_ids for sid in cand_2a)
                if all_need_super and not cand_2b_super:
                    names_2a = [id_to_surgeon[sid]["name"] for sid in cand_2a if sid in id_to_surgeon]
                    item = {
                        "day": day_str, "level": "2A/2B", "severity": "critical",
                        "names_2a": names_2a,
                        "text": f"{day_str}: all 2A candidates need supervision ({', '.join(names_2a)}) but no 2B supervisor available"
                    }
                    sup_items.append(item)
                    scored_issues.append((50, "supervision", item))

            if sup_items:
                analysis["sections"].append({
                    "title": "Supervision Conflicts",
                    "subtitle": f"{len(sup_items)} day(s) where 2A needs a 2B supervisor but none is available",
                    "icon": "⛔",
                    "items": sup_items
                })

            # 3) Leave clustering
            level_pools = {
                "1A/1B": sorted(set(sid for sid in (domain_1A + domain_1B) if sid != -1)),
                "2A (group 1+2)": sorted(set(group1_ids + group2_ids)),
                "2B supervisors (group 3+4)": sorted(set(group3_ids + group4_ids)),
                "Level 3": sorted(set(sid for sid in domain_3 if sid != -1)),
                "Level 4": sorted(set(sid for sid in domain_4 if sid != -1)),
            }
            cluster_items = []
            for pool_label, pool_ids in level_pools.items():
                if len(pool_ids) < 2:
                    continue
                for d_idx, day_str in enumerate(days):
                    unavail_names = []
                    for sid in pool_ids:
                        for req in availability.get(sid, []):
                            req_date = _parse_req_date(req.get('date'))
                            if req_date and req_date.isoformat() == day_str and req.get('request_type') in ('unavailable', 'study_leave', 'no_call'):
                                unavail_names.append(id_to_surgeon.get(sid, {}).get("name", f"ID {sid}"))
                                break
                    threshold = max(2, len(pool_ids) // 2)
                    if len(unavail_names) >= threshold:
                        pct = round(100 * len(unavail_names) / len(pool_ids))
                        severity = "critical" if pct >= 75 else ("high" if pct >= 60 else "medium")
                        item = {
                            "day": day_str, "pool": pool_label, "unavailable": len(unavail_names),
                            "total": len(pool_ids), "pct": pct, "names": unavail_names,
                            "severity": severity,
                            "text": f"{day_str} {pool_label}: {len(unavail_names)}/{len(pool_ids)} ({pct}%) unavailable — {', '.join(unavail_names)}"
                        }
                        cluster_items.append(item)
                        scored_issues.append((pct // 10, "clustering", item))

            if cluster_items:
                cluster_items.sort(key=lambda x: (-x["pct"], x["day"]))
                analysis["sections"].append({
                    "title": "Leave Clustering",
                    "subtitle": f"{len(cluster_items)} instances where ≥50% of a level pool is unavailable on the same day",
                    "icon": "📋",
                    "items": cluster_items
                })

            # 4) Weekend/holiday pressure
            wk_items = []
            for d_idx, day_str in enumerate(days):
                if not (is_weekend[d_idx] or is_ph[d_idx]):
                    continue
                tight = []
                for lvl in ["1A", "1B", "2A", "3", "4"]:
                    eligible = [sid for sid in domains_by_day[d_idx][lvl] if sid != -1]
                    base_count = len([sid for sid in base_domains.get(lvl, []) if sid != -1])
                    unavail = base_count - len(eligible)
                    if len(eligible) <= 3 and unavail > 0:
                        tight.append({"level": lvl, "eligible": len(eligible), "pool": base_count})
                if tight:
                    tag = "Weekend" if is_weekend[d_idx] else "Holiday"
                    wk_items.append({
                        "day": day_str, "tag": tag, "levels": tight, "severity": "medium",
                        "text": f"{day_str} [{tag}] " + ", ".join(f"{t['level']}: {t['eligible']}/{t['pool']}" for t in tight)
                    })

            if wk_items:
                analysis["sections"].append({
                    "title": "Weekend / Holiday Pressure",
                    "subtitle": f"{len(wk_items)} weekend/holiday day(s) with tight staffing",
                    "icon": "📅",
                    "items": wk_items
                })

            # 5) Per-level eligibility summary
            for lvl in all_levels:
                if lvl == "2B":
                    continue
                counts = [len([sid for sid in domains_by_day[d][lvl] if sid != -1]) for d in range(num_days)]
                base_count = len([sid for sid in base_domains.get(lvl, []) if sid != -1])
                min_c = min(counts) if counts else 0
                avg_c = round(sum(counts) / len(counts), 1) if counts else 0
                min_day = days[counts.index(min_c)] if counts else ""
                analysis["eligibility"].append({
                    "level": lvl, "pool": base_count, "min": min_c,
                    "min_day": min_day, "avg": avg_c
                })

            # -- Build culprits: top issues by score --
            scored_issues.sort(key=lambda x: -x[0])
            seen_keys = set()
            for score, issue_type, item in scored_issues:
                key = (item.get("day"), item.get("level", item.get("pool", "")))
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                reason = ""
                if issue_type == "supervision":
                    reason = "All 2A candidates need a 2B supervisor but none is available"
                elif issue_type == "bottleneck":
                    reason = f"Only {item['eligible']} of {item['pool']} surgeons eligible (rest on leave or blocked by 3-day rule)"
                elif issue_type == "clustering":
                    reason = f"{item['unavailable']} of {item['total']} ({item['pct']}%) surgeons in {item['pool']} are on leave"
                analysis["culprits"].append({
                    "day": item.get("day", ""),
                    "level": item.get("level", item.get("pool", "")),
                    "severity": item.get("severity", "medium"),
                    "reason": reason
                })
                if len(analysis["culprits"]) >= 5:
                    break

        except Exception as _analysis_err:
            import traceback
            print(f"[ANALYSIS] ERROR: {_analysis_err}")
            print(traceback.format_exc())
            diagnostics.append(f"[Analysis error: {_analysis_err}]")

        # Try a diagnostic run with caps relaxed to identify violating cohorts and their ranges (only if cap enabled)
        if enable_fairness_hard_cap and not _diagnostic_run:
            try:
                # give the diagnostic run a larger minimum budget to find a witness schedule
                diag_time = max(60, int(time_limit_seconds) if isinstance(time_limit_seconds, int) else 60)
                diag_sched, _ = solve_schedule_or_tools(
                    days=days,
                    surgeons=surgeons,
                    prev_schedule=prev_schedule,
                    public_holidays=public_holidays,
                    preassignments=preassignments,
                    time_limit_seconds=diag_time,
                    allow_empty=allow_empty,
                    _diagnostic_run=True,
                    _relax_fairness_caps=True,
                    horizon_prior_counts=horizon_prior_counts
                )
                # Only proceed if we got a schedule dict
                if isinstance(diag_sched, dict) and 'errors' not in diag_sched:
                    name_to_id = {s['name']: s['id'] for s in surgeons}
                    id_to_name = {s['id']: s['name'] for s in surgeons}
                    prior_levels = horizon_prior_counts.get("prior_levels", {}) if isinstance(horizon_prior_counts, dict) else {}
                    prior_credit = horizon_prior_counts.get("prior_unavail_credit_calls", {}) if isinstance(horizon_prior_counts, dict) else {}

                    def range_from_counts(cnts: dict):
                        vals = list(cnts.values())
                        return (max(vals) - min(vals)) if len(vals) > 1 else 0
                    def pretty_counts(cnts: dict):
                        if not cnts:
                            return "(none)"
                        parts = []
                        for sid in sorted(cnts.keys(), key=lambda x: id_to_name.get(x, f"ID{x}")):
                            parts.append(f"{id_to_name.get(sid, f'ID {sid}')}={cnts[sid]}")
                        return ", ".join(parts)

                    # Group 1 (1A+1B); include L2G1 when primary-call feature is enabled (matches solver fairness cohort)
                    group_level1_ids = [s["id"] for s in surgeons if set(parse_call_levels(s.get("call_levels",""))).intersection({"1A","1B"}) and s["id"] not in nlth_ids]
                    if enable_l2g1_primary_calls and group1_ids:
                        group_level1_ids = sorted(set(group_level1_ids) | {sid for sid in group1_ids if sid not in nlth_ids})
                    g1_raw = {sid: 0 for sid in group_level1_ids}
                    for assigns in diag_sched.values():
                        if not isinstance(assigns, dict):
                            continue
                        # Same surgeon on 1A and 1B that day = one Level-1 call.
                        lvl1_sids_today = set()
                        for lvl in ["1A","1B"]:
                            name = assigns.get(lvl)
                            if name:
                                sid = name_to_id.get(name)
                                if sid in g1_raw:
                                    lvl1_sids_today.add(sid)
                        for sid in lvl1_sids_today:
                            g1_raw[sid] += 1
                    prior_level1_days = horizon_prior_counts.get("prior_level1_days") if isinstance(horizon_prior_counts, dict) else None
                    def _prior_level1(sid):
                        if isinstance(prior_level1_days, dict):
                            return int(prior_level1_days.get(sid, 0))
                        return int(prior_levels.get("1A", {}).get(sid, 0)) + int(prior_levels.get("1B", {}).get(sid, 0))
                    g1_counts = {
                        sid: (
                            g1_raw.get(sid, 0)
                            + _prior_level1(sid)
                            + (fairness_credit_calls_per_surgeon.get(sid, 0) if cap_uses_credit else 0)
                            + (int(prior_credit.get(sid, 0)) if cap_uses_credit else 0)
                        )
                        for sid in g1_raw
                    }
                    g1_range = range_from_counts(g1_counts)

                    # Group 2 (updated): L2 union range for (2A+2B)
                    l2_union_ids = [sid for sid in list(set(group1_ids + group2_ids + group3_ids)) if sid not in nlth_ids]
                    g2_raw = {sid: 0 for sid in l2_union_ids}
                    for assigns in diag_sched.values():
                        for lvl in ["2A","2B"]:
                            name = assigns.get(lvl)
                            if name:
                                sid = name_to_id.get(name)
                                if sid in g2_raw:
                                    g2_raw[sid] += 1
                    g2_counts_all = {
                        sid: (
                            g2_raw.get(sid, 0)
                            + int(prior_levels.get("2A", {}).get(sid, 0))
                            + int(prior_levels.get("2B", {}).get(sid, 0))
                            + (fairness_credit_calls_per_surgeon.get(sid, 0) if cap_uses_credit else 0)
                            + (int(prior_credit.get(sid, 0)) if cap_uses_credit else 0)
                        )
                        for sid in g2_raw
                    }
                    g2_union_range = range_from_counts(g2_counts_all)

                    # Group 3: union 3 and subgroup 4 (count 3 for all; +4 if 3+4; +2B if subgroup 4)
                    s3_union_ids = set([s["id"] for s in surgeons if "3" in parse_call_levels(s.get("call_levels",""))] + group4_ids)
                    s3_ids = [sid for sid in s3_union_ids if sid not in nlth_ids]
                    g3_raw = {sid: 0 for sid in s3_ids}
                    for assigns in diag_sched.values():
                        n3 = assigns.get("3")
                        if n3:
                            sid3 = name_to_id.get(n3)
                            if sid3 in g3_raw:
                                g3_raw[sid3] += 1
                        n2b = assigns.get("2B")
                        if n2b:
                            sid2b = name_to_id.get(n2b)
                            if sid2b in g3_raw and sid2b in group4_ids:
                                g3_raw[sid2b] += 1
                        n4 = assigns.get("4")
                        if n4:
                            sid4 = name_to_id.get(n4)
                            if sid4 in g3_raw and sid4 in level34_ids:
                                g3_raw[sid4] += 1
                    g3_counts = {
                        sid: (
                            g3_raw.get(sid, 0)
                            + int(prior_levels.get("3", {}).get(sid, 0))
                            + (int(prior_levels.get("2B", {}).get(sid, 0)) if sid in group4_ids else 0)
                            + (int(prior_levels.get("4", {}).get(sid, 0)) if sid in level34_ids else 0)
                            + (fairness_credit_calls_per_surgeon.get(sid, 0) if cap_uses_credit else 0)
                            + (int(prior_credit.get(sid, 0)) if cap_uses_credit else 0)
                        )
                        for sid in g3_raw
                    }
                    g3_range = range_from_counts(g3_counts)

                    # Group 4: level 4 only (exclude Level 3+4 subgroup)
                    group4_level_ids = [
                        s["id"] for s in surgeons
                        if "4" in parse_call_levels(s.get("call_levels",""))
                        and s["id"] not in nlth_ids
                        and s["id"] not in level34_ids
                    ]
                    g4_raw = {sid: 0 for sid in group4_level_ids}
                    for assigns in diag_sched.values():
                        n4 = assigns.get("4")
                        if n4:
                            sid4 = name_to_id.get(n4)
                            if sid4 in g4_raw:
                                g4_raw[sid4] += 1
                    g4_counts = {
                        sid: (
                            g4_raw.get(sid, 0)
                            + int(prior_levels.get("4", {}).get(sid, 0))
                            + (fairness_credit_calls_per_surgeon.get(sid, 0) if cap_uses_credit else 0)
                            + (int(prior_credit.get(sid, 0)) if cap_uses_credit else 0)
                        )
                        for sid in g4_raw
                    }
                    g4_range = range_from_counts(g4_counts)

                    violating = []
                    cap_val = fairness_hard_cap_effective
                    if g1_range > cap_val:
                        violating.append(f"Group 1 (1A+1B) range={g1_range} > cap={cap_val}")
                    if g2_union_range > cap_val:
                        violating.append(f"Group 2 (L2 union 2A+2B) range={g2_union_range} > cap={cap_val}")
                    if g3_range > cap_val:
                        violating.append(f"Group 3 (3 [+4 if 3+4] [+2B if subgroup 4]) range={g3_range} > cap={cap_val}")
                    if g4_range > cap_val:
                        violating.append(f"Group 4 (level 4) range={g4_range} > cap={cap_val}")
                    if violating:
                        diagnostics.append(f"Cohorts exceeding range ≤ {cap_val}:")
                        diagnostics.extend(violating)
                    else:
                        diagnostics.append("Diagnostics could not identify a specific cohort exceeding the cap; try increasing time or adjusting constraints.")
                    if cap_uses_credit:
                        diagnostics.append(
                            "Unified fairness credit (current month): "
                            + pretty_counts({sid: fairness_credit_calls_per_surgeon.get(sid, 0) for sid in fairness_credit_calls_per_surgeon})
                        )
                        diagnostics.append(
                            "Unified fairness credit (prior horizon): "
                            + pretty_counts({int(sid): int(prior_credit.get(sid, 0)) for sid in prior_credit})
                        )
                    diagnostics.append(f"Group 1 adjusted counts: {pretty_counts(g1_counts)} (range={g1_range})")
                    diagnostics.append(f"Group 2 adjusted counts: {pretty_counts(g2_counts_all)} (range={g2_union_range})")
                    diagnostics.append(f"Group 3 adjusted counts: {pretty_counts(g3_counts)} (range={g3_range})")
                    diagnostics.append(f"Group 4 adjusted counts: {pretty_counts(g4_counts)} (range={g4_range})")

                    # Report specific unfilled slots (None) in diagnostic schedule
                    unfilled = []
                    for day_str, assigns in diag_sched.items():
                        for lvl in all_levels:
                            if assigns.get(lvl) in [None, "", "-"]:
                                unfilled.append((day_str, lvl))
                    if unfilled:
                        diagnostics.append("Unfilled slots in diagnostic run (may indicate tight eligibility/linked constraints):")
                        diagnostics.extend([f"{d} {lvl}" for d, lvl in unfilled])

                        # Include eligible counts for those slots
                        try:
                            avail = get_availability_requests()
                            def has_level(s, L):
                                return L in parse_call_levels(s.get('call_levels',''))
                            by_level = {L: [s for s in surgeons if has_level(s, L)] for L in all_levels}
                            date_to_d = {ds: datetime.datetime.strptime(ds, "%Y-%m-%d").date() for ds in days}
                            blocked = {}
                            for s in surgeons:
                                sid = s['id']
                                bset = set()
                                for req in avail.get(sid, []):
                                    raw = req.get('date')
                                    try:
                                        d = raw if isinstance(raw, datetime.date) else datetime.date.fromisoformat(raw)
                                    except Exception:
                                        continue
                                    if d in date_to_d.values() and req.get('request_type') in ('unavailable','study_leave','no_call'):
                                        bset.add(d)
                                blocked[sid] = bset
                            for d_str, lvl in unfilled:
                                d_date = date_to_d.get(d_str)
                                if not d_date:
                                    continue
                                elig = [s for s in by_level.get(lvl, []) if d_date not in blocked.get(s['id'], set())]
                                diagnostics.append(f"Eligible count for {d_str} {lvl}: {len(elig)}")
                        except Exception:
                            pass
            except Exception:
                pass
        if not diagnostics:
            diagnostics.append("No feasible assignment exists under current constraints. Try relaxing constraints or adding eligible surgeons.")
        result = {"errors": diagnostics}
        if analysis:
            result["analysis"] = analysis
        return result, None
