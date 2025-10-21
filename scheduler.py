import datetime
from dateutil.parser import parse
from ortools.sat.python import cp_model
import itertools
import sys


############################# ################
# OR‑Tools Scheduling Function (with Availability Constraints)
#############################################

def solve_schedule_or_tools(days, surgeons, prev_schedule=None, public_holidays=None, preassignments=None, time_limit_seconds: int = 30, allow_empty: bool = False, _diagnostic_run: bool = False, _relax_fairness_caps: bool = False):

    from helper import (
        get_max_calls_config,
        get_global_config,
        get_availability_requests,
        parse_call_levels,
        get_level2_group,
        get_team_day_prefs
    )
    model = cp_model.CpModel()
    constraint_mapping = {}
    diagnostics = []
    # Helper to wrap hard constraint additions.
    def add_named_constraint(name, add_function, *args, **kwargs):
        c = add_function(*args, **kwargs)
        constraint_mapping[name] = None
        return c

    num_days = len(days)
    all_levels = ["1A","1B","2A","2B","3","4"]
    all_ids    = [s["id"] for s in surgeons]
    nlth_ids = [s["id"] for s in surgeons if s.get("nlth")]
    team_day_prefs = get_team_day_prefs()

    # Load global configuration weights.
    global_config = get_global_config()
    fairness_weight = int(global_config.get("fairness_weight", "1000"))
    cap_uses_credit = str(global_config.get("fairness_cap_uses_credit", "0")) == "1"
    gamma_no_call = int(global_config.get("gamma_no_call", "10"))
    gamma_unavail_prev = int(global_config.get("gamma_unavail_prev", "5"))
    gamma_1B = int(global_config.get("gamma_1B", "1"))
    gamma_balance = int(global_config.get("gamma_balance", "100"))
    no_call_hard = global_config.get("no_call_hard", "1") == "1"
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
    gamma_balance   = max(gamma_balance,   base_max_soft * fairness_scale)
    gamma_weekend_balance = int(global_config.get("gamma_weekend_balance", "50"))
    max_weekend_calls_cfg = int(global_config.get("max_weekend_calls", "3"))
    min_calls_nlth_cfg = int(global_config.get("min_calls_nlth", "3"))
    gamma_consec_weekend = int(global_config.get("gamma_consec_weekend", "20"))
    gamma_team_pref = int(global_config.get("gamma_team_pref", "10"))
    # New: encourage balanced team presence on weekends (more diverse teams across weekends)
    gamma_weekend_team_diversity = int(global_config.get("gamma_weekend_team_diversity", "50"))
    gamma_2b_usage = int(global_config.get("gamma_2b_usage", "0"))
    gamma_fairness_l2_groups = int(global_config.get("gamma_fairness_l2_groups", "500"))
    # New: credit for unavailability (each k days → 1 fewer call, soft)
    gamma_unavail_credit = int(global_config.get("gamma_unavail_credit", "50"))
    unavail_credit_days = int(global_config.get("unavail_credit_days", "7")) or 7
    if unavail_credit_days < 1:
        unavail_credit_days = 7

    # Feature flags (on/off) for constraint families
    def is_enabled(key: str, default: str = "1") -> bool:
        return str(global_config.get(key, default)) == "1"

    enable_force_1B_weekend           = is_enabled("enable_force_1B_weekend")
    enable_level2_supervision         = is_enabled("enable_level2_supervision")
    enable_group4_2B3_ban            = is_enabled("enable_group4_2B3_ban")
    enable_max_2B_group4             = is_enabled("enable_max_2B_group4")
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

    max_config = get_max_calls_config()  # e.g., {"1":10, "2":10, "3":10, "4":10}
    
    # Use actual surgeon IDs from the database.
    id_to_surgeon = {s["id"]: s for s in surgeons}
    all_surgeon_ids = [s["id"] for s in surgeons]
    
    # --- Build Domains (using actual IDs) ---
    domain_1A = [s["id"] for s in surgeons if "1A" in parse_call_levels(s.get("call_levels", ""))]
    if not domain_1A:
        domain_1A = [-1]
    domain_1B = [s["id"] for s in surgeons if "1B" in parse_call_levels(s.get("call_levels", ""))] 
    if not domain_1B:
        domain_1B = [-1]
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

    base_domains = {
        "1A": domain_1A,     
        "1B": domain_1B,
        "2A": list(set(group1_ids + group2_ids)),  # exclude subgroup 3 from 2A
        "2B": group3_ids + group4_ids + [-1],
        "3":  domain_3,
        "4":  domain_4,
    }
    domains_by_day = {
        d: { lvl: list(base_domains[lvl]) for lvl in base_domains }
        for d in range(num_days)
    }

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
        day_to_idx = {day: i for i, day in enumerate(days)}
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
    # Compute per-surgeon unavailability credit in call units (integer number of calls to credit)
    day_set_str = set(days)
    credit_calls_per_surgeon = {s['id']: 0 for s in surgeons}
    try:
        for s_id, req_list in availability.items():
            count_unavail = 0
            for req in req_list:
                if req.get('request_type') != 'unavailable':
                    continue
                raw = req.get('date')
                if isinstance(raw, str):
                    if raw in day_set_str:
                        count_unavail += 1
                elif isinstance(raw, datetime.date):
                    if raw.isoformat() in day_set_str:
                        count_unavail += 1
            # Convert days to calls credit via floor division
            credit_calls_per_surgeon[s_id] = (count_unavail // unavail_credit_days) if unavail_credit_days > 0 else 0
    except Exception:
        # Fallback: no credits if any parsing error
        credit_calls_per_surgeon = {s['id']: 0 for s in surgeons}
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
                # If the request applies on the current date, and is an "unavailable" or "no_call" request:
                if no_call_hard:
                    if req_date == current_date and req.get('request_type') in ("unavailable","no_call"):
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
                    if req_date == current_date and req.get('request_type') in ("unavailable"):
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
        ban_by_day = {}
        for idx, pd in enumerate(last_two):
            # idx = 0 → prev-day = −2, ban target days = [0]
            # idx = 1 → prev-day = −1, ban target days = [0,1]
            prev_str = pd.isoformat()
            for lvl in all_levels:
                prev_name = prev_schedule.get(prev_str, {}).get(lvl)
                sid = name_to_id.get(prev_name)
                if sid is None:
                    continue

                for target in range(idx + 1):
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
        print("=== prev_schedule prune report ===")
        for d in sorted(ban_by_day):
            print(f"\nDay {d} ({days[d]}) carry-over bans:")
            print(f"    would-ban = {sorted(ban_by_day[d])}")
            for lvl in all_levels:
                print(f"  {lvl:>3}  pruned={pruned[d][lvl]}  skipped={skipped[d][lvl]}")
        print("=== end of prev_schedule report ===\n")

    # … your availability/no_call pruning here …

    # --- DEBUG: dump the pruned domains before creating X ---
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
            # Skip diagnostics for 2B; 2B is optional in this model
            if lvl == "2B":
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
                    print(f"Day {day_str} level {level} domain: {domains_by_day[d][level]}, preassigned: {assigned_id}")
                    # Force variable to assigned_id regardless of availability and spacing rules.
                    add_named_constraint(f"Preassignment: {day_str} level {level} fixed to surgeon {assigned_id}",
                        model.Add, X[(d, level)] == assigned_id)

    # --- Prevent same surgeon from being assigned twice on same day ---
    for d, day_str in enumerate(days):
        print(f"\nChecking level‐pairs on {day_str}:")
        for lvl1, lvl2 in itertools.combinations(all_levels, 2):
            # compute the real candidates for each slot
            c1 = set(domains_by_day[d][lvl1]) - {-1}
            c2 = set(domains_by_day[d][lvl2]) - {-1}
            # if both are to be filled, they each need ≥1 candidate...
            if not c1 or not c2:
                print(f"  • One of {lvl1}/{lvl2} has no candidates: {lvl1}→{c1}, {lvl2}→{c2}")
            # ...and together they need ≥2 **distinct** candidates
            elif len(c1 | c2) < 2:
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

    # --- Force 1B to be filled on weekends and public holidays (toggle) ---
    if enable_force_1B_weekend and not allow_empty:
        for d, day_str in enumerate(days):
            dt = datetime.datetime.strptime(day_str, "%Y-%m-%d").date()
            is_weekend_day = dt.weekday() >= 5
            is_holiday_day = public_holidays and (day_str in public_holidays)
            if is_weekend_day or is_holiday_day:
                add_named_constraint(f"Force 1B on {day_str}: 1B != -1",
                    model.Add, X[(d, "1B")] != -1)
    
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
            # 4) Never let the same person occupy both 2A and 2B.
            add_named_constraint(f"Level2 uniqueness: Day {d} 2A != 2B",
                model.Add, X[(d, "2A")] != X[(d, "2B")])
    
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
    
    # --- At most one 2B‐shift per Group 4 surgeon over the entire schedule ---
    if enable_max_2B_group4:
        for s in group4_ids:
            add_named_constraint(f"Max 2B-shifts for Group4 surgeon {s}",
                model.Add, sum(indicators[(d, "2B", s)] for d in range(num_days)) <= 1)
    
    call_count_overall = {}
    for s in all_surgeon_ids:
        call_count_overall[s] = model.NewIntVar(0, num_days * len(all_levels), f'count_all_{s}')
        add_named_constraint(f"Total calls for surgeon {s}",
            model.Add, call_count_overall[s] == sum(indicators[(d, level, s)] for d in range(num_days) for level in all_levels))
    
    if enable_max_calls_level1:
        for s_id in all_ids:
            c1 = model.NewIntVar(0, num_days*2, f"count1_{s_id}")
            add_named_constraint(f"1A+1B calls for surgeon {s_id}",
                model.Add, c1 == sum(indicators[(d, "1A", s_id)] + indicators[(d, "1B", s_id)] for d in range(num_days)))
            add_named_constraint(f"Max 1A+1B calls for surgeon {s_id}",
                model.Add, c1 <= max_calls_level1)

    # --- Hard minimum total calls for NLTH surgeons ---
    if min_calls_nlth_cfg > 0 and nlth_ids:
        for s in nlth_ids:
            add_named_constraint(f"Min total calls for NLTH surgeon {s}",
                model.Add, call_count_overall[s] >= min_calls_nlth_cfg)

    # --- Per-group fairness terms (replace overall fairness) ---
    group_fairness_diffs = []

    # Group 1: (1A + 1B)
    group_level1_ids = [s["id"] for s in surgeons if set(parse_call_levels(s.get("call_levels",""))).intersection({"1A","1B"}) and s["id"] not in nlth_ids]
    if len(group_level1_ids) > 1:
        lvl1_counts = {s: model.NewIntVar(0, num_days * 2, f"lvl1_count_{s}") for s in group_level1_ids}
        for s in group_level1_ids:
            add_named_constraint(f"(1A+1B) count for surgeon {s}",
                model.Add, lvl1_counts[s] == sum(indicators[(d, lvl, s)] for d in range(num_days) for lvl in ["1A","1B"]))
        # Optionally apply unavailability credit to fairness cap by using adjusted counts
        if cap_uses_credit:
            lvl1_adj = {s: model.NewIntVar(-num_days * 2, num_days * 2, f"lvl1_adj_{s}") for s in group_level1_ids}
            for s in group_level1_ids:
                add_named_constraint(f"(1A+1B) adjusted {s}", model.Add, lvl1_adj[s] == lvl1_counts[s] - credit_calls_per_surgeon.get(s, 0))
            fairness_vars_lvl1 = [lvl1_adj[s] for s in group_level1_ids]
        else:
            fairness_vars_lvl1 = [lvl1_counts[s] for s in group_level1_ids]
        gmax = model.NewIntVar(-num_days * 2, num_days * 2, "lvl1_max")
        gmin = model.NewIntVar(-num_days * 2, num_days * 2, "lvl1_min")
        add_named_constraint("Max (1A+1B) count", model.AddMaxEquality, gmax, fairness_vars_lvl1)
        add_named_constraint("Min (1A+1B) count", model.AddMinEquality, gmin, fairness_vars_lvl1)
        diff = model.NewIntVar(0, num_days * 2, "lvl1_diff")
        if cap_uses_credit:
            # diff over adjusted counts
            add_named_constraint("(1A+1B) diff", model.Add, diff == gmax - gmin)
        else:
            add_named_constraint("(1A+1B) diff", model.Add, diff == gmax - gmin)
        if not _relax_fairness_caps:
            add_named_constraint("Fairness cap: (1A+1B) range <= 1", model.Add, diff <= 1)
        group_fairness_diffs.append(diff)

    # Group 2: (2A + 2B) fairness within each L2 group 1, 2, and 3 separately
    for gid, members in [(1, group1_ids), (2, group2_ids), (3, group3_ids)]:
        grp_ids = [s for s in members if s not in nlth_ids]
        if len(grp_ids) <= 1:
            continue
        lvl2_counts = {s: model.NewIntVar(0, num_days * 2, f"lvl2_g{gid}_count_{s}") for s in grp_ids}
        for s in grp_ids:
            add_named_constraint(f"(2A+2B) g{gid} count for surgeon {s}",
                model.Add, lvl2_counts[s] == sum(indicators[(d, lvl, s)] for d in range(num_days) for lvl in ["2A","2B"]))
        if cap_uses_credit:
            lvl2_adj = {s: model.NewIntVar(-num_days * 2, num_days * 2, f"lvl2_g{gid}_adj_{s}") for s in grp_ids}
            for s in grp_ids:
                add_named_constraint(f"(2A+2B) g{gid} adjusted {s}", model.Add, lvl2_adj[s] == lvl2_counts[s] - credit_calls_per_surgeon.get(s, 0))
            fairness_vars_lvl2 = [lvl2_adj[s] for s in grp_ids]
        else:
            fairness_vars_lvl2 = [lvl2_counts[s] for s in grp_ids]
        gmax = model.NewIntVar(-num_days * 2, num_days * 2, f"lvl2_g{gid}_max")
        gmin = model.NewIntVar(-num_days * 2, num_days * 2, f"lvl2_g{gid}_min")
        add_named_constraint(f"Max (2A+2B) g{gid}", model.AddMaxEquality, gmax, fairness_vars_lvl2)
        add_named_constraint(f"Min (2A+2B) g{gid}", model.AddMinEquality, gmin, fairness_vars_lvl2)
        diff = model.NewIntVar(0, num_days * 2, f"lvl2_g{gid}_diff")
        add_named_constraint(f"(2A+2B) g{gid} diff", model.Add, diff == gmax - gmin)
        if not _relax_fairness_caps:
            add_named_constraint(f"Fairness cap: (2A+2B) g{gid} range <= 1", model.Add, diff <= 1)
        group_fairness_diffs.append(diff)

    # Group 3: include all surgeons with level 3, plus L2 subgroup 4; 
    # counts include level 3 for everyone, and add 2B only for subgroup 4.
    s3_union_ids = set([s["id"] for s in surgeons if "3" in parse_call_levels(s.get("call_levels",""))] + group4_ids)
    s3_ids = [sid for sid in s3_union_ids if sid not in nlth_ids]
    if len(s3_ids) > 1:
        g3_counts = {s: model.NewIntVar(0, num_days * 2, f"lvl3_union_count_{s}") for s in s3_ids}
        for s in s3_ids:
            terms = [indicators[(d, "3", s)] for d in range(num_days)]
            if s in group4_ids:
                terms += [indicators[(d, "2B", s)] for d in range(num_days)]
            add_named_constraint(f"(3 [+2B if grp4]) count for surgeon {s}",
                model.Add, g3_counts[s] == sum(terms))
        if cap_uses_credit:
            g3_adj = {s: model.NewIntVar(-num_days * 2, num_days * 2, f"lvl3_union_adj_{s}") for s in s3_ids}
            for s in s3_ids:
                add_named_constraint(f"lvl3 union adjusted {s}", model.Add, g3_adj[s] == g3_counts[s] - credit_calls_per_surgeon.get(s, 0))
            fairness_vars_g3 = [g3_adj[s] for s in s3_ids]
        else:
            fairness_vars_g3 = [g3_counts[s] for s in s3_ids]
        gmax = model.NewIntVar(-num_days * 2, num_days * 2, "lvl3_union_max")
        gmin = model.NewIntVar(-num_days * 2, num_days * 2, "lvl3_union_min")
        add_named_constraint("Max lvl3 union count", model.AddMaxEquality, gmax, fairness_vars_g3)
        add_named_constraint("Min lvl3 union count", model.AddMinEquality, gmin, fairness_vars_g3)
        diff = model.NewIntVar(0, num_days * 2, "lvl3_union_diff")
        add_named_constraint("lvl3 union diff", model.Add, diff == gmax - gmin)
        if not _relax_fairness_caps:
            add_named_constraint("Fairness cap: lvl3 union range <= 1", model.Add, diff <= 1)
        group_fairness_diffs.append(diff)

    # Group 4: level 4 only
    group4_level_ids = [s["id"] for s in surgeons if "4" in parse_call_levels(s.get("call_levels","")) and s["id"] not in nlth_ids]
    if len(group4_level_ids) > 1:
        lvl4_counts = {s: model.NewIntVar(0, num_days, f"lvl4_count_{s}") for s in group4_level_ids}
        for s in group4_level_ids:
            add_named_constraint(f"(4) count for surgeon {s}",
                model.Add, lvl4_counts[s] == sum(indicators[(d, "4", s)] for d in range(num_days)))
        if cap_uses_credit:
            lvl4_adj = {s: model.NewIntVar(-num_days, num_days, f"lvl4_adj_{s}") for s in group4_level_ids}
            for s in group4_level_ids:
                add_named_constraint(f"(4) adjusted {s}", model.Add, lvl4_adj[s] == lvl4_counts[s] - credit_calls_per_surgeon.get(s, 0))
            fairness_vars_lvl4 = [lvl4_adj[s] for s in group4_level_ids]
        else:
            fairness_vars_lvl4 = [lvl4_counts[s] for s in group4_level_ids]
        gmax = model.NewIntVar(-num_days, num_days, "lvl4_max")
        gmin = model.NewIntVar(-num_days, num_days, "lvl4_min")
        add_named_constraint("Max (4) count", model.AddMaxEquality, gmax, fairness_vars_lvl4)
        add_named_constraint("Min (4) count", model.AddMinEquality, gmin, fairness_vars_lvl4)
        diff = model.NewIntVar(0, num_days, "lvl4_diff")
        add_named_constraint("(4) diff", model.Add, diff == gmax - gmin)
        if not _relax_fairness_caps:
            add_named_constraint("Fairness cap: (4) range <= 1", model.Add, diff <= 1)
        group_fairness_diffs.append(diff)
    
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
    
    weekend_diff_terms = []
    for i, (_, grp) in enumerate([
        ("grp1", [s["id"] for s in surgeons if set(parse_call_levels(s.get("call_levels",""))).intersection({"1A","1B"})]),
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

    # Count weekend presence per team (per-day OR, not per-slot)
    team_weekend_counts = {}
    for t in teams:
        cnt = model.NewIntVar(0, len(weekend_days), f"team_weekend_count_{t}")
        if enable_weekend_team_diversity and weekend_days:
            add_named_constraint(f"Team weekend count {t}", model.Add, cnt == sum(team_presence_vars[(t,d)] for d in weekend_days))
        else:
            add_named_constraint(f"Team weekend count zero {t}", model.Add, cnt == 0)
        team_weekend_counts[t] = cnt

    # Balance: minimize max-min across teams
    if enable_weekend_team_diversity and len(teams) >= 2:
        tw_max = model.NewIntVar(0, len(weekend_days), "team_weekend_max")
        tw_min = model.NewIntVar(0, len(weekend_days), "team_weekend_min")
        add_named_constraint("Team weekend max", model.AddMaxEquality, tw_max, list(team_weekend_counts.values()))
        add_named_constraint("Team weekend min", model.AddMinEquality, tw_min, list(team_weekend_counts.values()))
        team_weekend_diff = model.NewIntVar(0, len(weekend_days), "team_weekend_diff")
        add_named_constraint("Team weekend diff", model.Add, team_weekend_diff == tw_max - tw_min)
    else:
        team_weekend_diff = model.NewIntVar(0, 0, "team_weekend_diff")
        add_named_constraint("Team weekend diff zero", model.Add, team_weekend_diff == 0)
    
    td_terms = []
    for d, day_str in enumerate(days):
        wd = datetime.datetime.strptime(day_str, "%Y-%m-%d").weekday()
        for lvl in all_levels:
            for s in surgeons:
                sid  = s['id']
                team = s.get('team')
                if team in team_day_prefs:
                    adj = team_day_prefs[team].get(wd, 0)
                    if adj != 0:
                        b = indicators[(d, lvl, sid)]
                        coef = gamma_team_pref * adj
                        td_terms.append((coef, b))
    
    # --- Soft Penalties for Availability ---
    soft_penalties_unavail_prev = []
    for i in range(num_days - 1):
        next_day = datetime.datetime.strptime(days[i+1], "%Y-%m-%d").date()
        for s_id, req_list in get_availability_requests().items():
            for req in req_list:
                raw = req["date"]
                if isinstance(raw, datetime.date):
                    req_date = raw
                else:
                    try:
                        req_date = datetime.datetime.strptime(raw, "%Y-%m-%d").date()
                    except Exception:
                        continue
                if req_date == next_day and req["request_type"] == "unavailable":
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
            for s_id, req_list in get_availability_requests().items():
                for req in req_list:
                    try:
                        req_date = datetime.datetime.strptime(req["date"], "%Y-%m-%d").date()
                    except Exception:
                        continue
                    if req_date == datetime.datetime.strptime(day, "%Y-%m-%d").date() and req["request_type"] == "no_call":
                        for lev in all_levels:
                            b = model.NewBoolVar(f'penalty_nocall_{i}_{lev}_{s_id}')
                            add_named_constraint(f"No call penalty: Day {i} {lev} equals surgeon {s_id}",
                                model.Add, X[(i, lev)] == s_id
                            ).OnlyEnforceIf(b)
                            add_named_constraint(f"No call penalty: Day {i} {lev} not equals surgeon {s_id}",
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
    for s_id, req_list in get_availability_requests().items():
        u_days = {datetime.datetime.strptime(req["date"], "%Y-%m-%d").date()
                  for req in req_list if req.get("request_type") == "unavailable"
                  if isinstance(req.get("date"), str)}
        # include only dates in current month days
        unavail_days_per_surgeon[s_id] = len(u_days & day_set)

    # Overflow above (avg - credit) measured in N-scaled units to avoid division
    unavail_overflows = []
    if enable_unavail_credit:
        for s in all_surgeon_ids:
            credit_units = (unavail_days_per_surgeon.get(s, 0) // unavail_credit_days) * N
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
    
    objective_terms = []
    # Remove overall fairness term; use per-group fairness instead
    if enable_nocall_penalty:
        objective_terms.append(gamma_no_call * penalty_nocall)
    if enable_unavail_prev_penalty:
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
    if enable_weekend_team_diversity:
        objective_terms.append(gamma_weekend_team_diversity * team_weekend_diff)
    # team day preferences
    if enable_team_day_prefs and td_terms:
        objective_terms += [- coef * b for coef, b in td_terms]

    # Add per-group fairness terms
    if group_fairness_diffs:
        objective_terms.extend([fairness_weight * diff for diff in group_fairness_diffs])

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
    # Penalize empty slots heavily so they are used only if necessary
    if allow_empty and empty_indicators:
        empty_weight_candidates = [
            fairness_weight,
            gamma_no_call,
            gamma_unavail_prev,
            gamma_balance,
            gamma_unavail_credit,
            gamma_spacing,
            gamma_weekend_balance,
            gamma_consec_weekend,
            gamma_weekend_team_diversity,
        ]
        empty_penalty_weight = max([w for w in empty_weight_candidates if isinstance(w, int)], default=1) * 10000
        objective_terms.append(empty_penalty_weight * sum(empty_indicators))
    add_named_constraint("Objective", model.Minimize, sum(objective_terms) if objective_terms else 0)
    add_named_constraint("Objective", model.Minimize, sum(objective_terms))
    
    # --- Solve the Model ---
    solver = cp_model.CpSolver()
    # Allow caller to control time limit
    try:
        solver.parameters.max_time_in_seconds = int(time_limit_seconds)
    except Exception:
        solver.parameters.max_time_in_seconds = 30
    solver.parameters.log_search_progress = True
    solver.parameters.log_to_stdout = True

    status = solver.Solve(model)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        solution = {
            days[d]: {
                level: next((s["name"] for s in surgeons if s["id"] == solver.Value(X[(d,level)])), None)
                for level in all_levels
            } for d in range(num_days)
        }
        return solution, solver.ObjectiveValue()
    else:
        print("\nModel is INFEASIBLE. Hard constraint summary:")
        for name in constraint_mapping:
            print("  ", name)
        # Return diagnostics to the caller so the UI can display them
        diagnostics.append("Fairness hard cap (range ≤ 1) within call-level cohorts could not be satisfied under current eligibility/availability.")
        # Try a diagnostic run with caps relaxed to identify violating cohorts and their ranges
        if not _diagnostic_run:
            try:
                diag_sched, _ = solve_schedule_or_tools(
                    days=days,
                    surgeons=surgeons,
                    prev_schedule=prev_schedule,
                    public_holidays=public_holidays,
                    preassignments=preassignments,
                    time_limit_seconds=time_limit_seconds,
                    allow_empty=allow_empty,
                    _diagnostic_run=True,
                    _relax_fairness_caps=True
                )
                # Only proceed if we got a schedule dict
                if isinstance(diag_sched, dict) and 'errors' not in diag_sched:
                    name_to_id = {s['name']: s['id'] for s in surgeons}
                    def range_from_counts(cnts: dict):
                        vals = list(cnts.values())
                        return (max(vals) - min(vals)) if len(vals) > 1 else 0
                    # Group 1 (1A+1B)
                    group_level1_ids = [s["id"] for s in surgeons if set(parse_call_levels(s.get("call_levels",""))).intersection({"1A","1B"}) and s["id"] not in nlth_ids]
                    g1_counts = {sid: 0 for sid in group_level1_ids}
                    for assigns in diag_sched.values():
                        for lvl in ["1A","1B"]:
                            name = assigns.get(lvl)
                            if name:
                                sid = name_to_id.get(name)
                                if sid in g1_counts:
                                    g1_counts[sid] += 1
                    g1_range = range_from_counts(g1_counts)
                    # Group 2: per L2 subgroup 1/2/3
                    g2_ranges = {}
                    for gid, members in [(1, group1_ids), (2, group2_ids), (3, group3_ids)]:
                        grp_ids = [sid for sid in members if sid not in nlth_ids]
                        counts = {sid: 0 for sid in grp_ids}
                        for assigns in diag_sched.values():
                            for lvl in ["2A","2B"]:
                                name = assigns.get(lvl)
                                if name:
                                    sid = name_to_id.get(name)
                                    if sid in counts:
                                        counts[sid] += 1
                        g2_ranges[gid] = range_from_counts(counts)
                    # Group 3: union 3 and subgroup 4 (count 3 for all; +2B if subgroup 4)
                    s3_union_ids = set([s["id"] for s in surgeons if "3" in parse_call_levels(s.get("call_levels",""))] + group4_ids)
                    s3_ids = [sid for sid in s3_union_ids if sid not in nlth_ids]
                    g3_counts = {sid: 0 for sid in s3_ids}
                    for assigns in diag_sched.values():
                        n3 = assigns.get("3")
                        if n3:
                            sid3 = name_to_id.get(n3)
                            if sid3 in g3_counts:
                                g3_counts[sid3] += 1
                        n2b = assigns.get("2B")
                        if n2b:
                            sid2b = name_to_id.get(n2b)
                            if sid2b in g3_counts and sid2b in group4_ids:
                                g3_counts[sid2b] += 1
                    g3_range = range_from_counts(g3_counts)
                    # Group 4: level 4 only
                    group4_level_ids = [s["id"] for s in surgeons if "4" in parse_call_levels(s.get("call_levels","")) and s["id"] not in nlth_ids]
                    g4_counts = {sid: 0 for sid in group4_level_ids}
                    for assigns in diag_sched.values():
                        n4 = assigns.get("4")
                        if n4:
                            sid4 = name_to_id.get(n4)
                            if sid4 in g4_counts:
                                g4_counts[sid4] += 1
                    g4_range = range_from_counts(g4_counts)
                    violating = []
                    if g1_range > 1:
                        violating.append(f"Group 1 (1A+1B) range={g1_range}")
                    for gid in [1,2,3]:
                        if g2_ranges.get(gid, 0) > 1:
                            violating.append(f"Group 2 (L2 subgroup {gid} 2A+2B) range={g2_ranges[gid]}")
                    if g3_range > 1:
                        violating.append(f"Group 3 (3 [+2B if subgroup 4]) range={g3_range}")
                    if g4_range > 1:
                        violating.append(f"Group 4 (level 4) range={g4_range}")
                    if violating:
                        diagnostics.append("Cohorts exceeding range ≤ 1:")
                        diagnostics.extend(violating)
            except Exception:
                pass
        if not diagnostics:
            diagnostics.append("No feasible assignment exists under current constraints. Try relaxing constraints or adding eligible surgeons.")
        return {"errors": diagnostics}, None
