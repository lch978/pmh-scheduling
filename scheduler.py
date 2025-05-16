import datetime
from dateutil.parser import parse
from ortools.sat.python import cp_model
import itertools

############################# ################
# OR‑Tools Scheduling Function (with Availability Constraints)
#############################################

def solve_schedule_or_tools(days, surgeons, prev_schedule=None, public_holidays=None, preassignments=None):
    from helper import (
        get_max_calls_config,
        get_global_config,
        get_availability_requests,
        parse_call_levels,
        get_level2_group,
        get_team_day_prefs
    )
    model = cp_model.CpModel()
    num_days = len(days)
    all_levels = ["1A","1B","2A","2B","3","4"]
    all_ids    = [s["id"] for s in surgeons]
    nlth_ids = [s["id"] for s in surgeons if s.get("nlth")]

    team_day_prefs = get_team_day_prefs()

    # Load global configuration weights.
    global_config = get_global_config()
    fairness_weight = int(global_config.get("fairness_weight", "1000"))
    gamma_no_call = int(global_config.get("gamma_no_call", "10"))
    gamma_unavail_prev = int(global_config.get("gamma_unavail_prev", "5"))
    gamma_1B = int(global_config.get("gamma_1B", "1"))
    gamma_balance = int(global_config.get("gamma_balance", "100"))
    no_call_hard = global_config.get("no_call_hard", "1") == "1"
    gamma_spacing = int(global_config.get("gamma_spacing", "10"))
    spacing_threshold = int(global_config.get("spacing_threshold", "7"))
    max_calls_level1 = int(global_config.get("max_calls_level1", "10"))  # whatever key you used

    gamma_weekend_balance = int(global_config.get("gamma_weekend_balance", "50"))
    gamma_consec_weekend = int(global_config.get("gamma_consec_weekend", "20"))
    gamma_team_pref = int(global_config.get("gamma_team_pref", "10"))

    # Get maximum calls configuration.
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

    # Precompute the three groups:
    group1_ids = [s["id"] for s in surgeons if get_level2_group(s) == 1]  # needs 2B supervision
    group2_ids = [s["id"] for s in surgeons if get_level2_group(s) == 2]  # no supervision needed
    group3_ids = [s["id"] for s in surgeons if get_level2_group(s) == 3]  # supervisors only
    group4_ids = [s["id"] for s in surgeons if get_level2_group(s) == 4]  # supervisors who are also 3rd call

    base_domains = {
        "1A": domain_1A,     
        "1B": domain_1B,
        "2A": list(set(group1_ids + group2_ids + group3_ids)),
        "2B": group3_ids + group4_ids + [-1],
        "3":  domain_3,
        "4":  domain_4,
    }
    domains_by_day = {
        d: { lvl: list(base_domains[lvl]) for lvl in base_domains }
        for d in range(num_days)
    }

    availability = get_availability_requests()

    for d, day_str in enumerate(days):
        current_date = datetime.datetime.strptime(day_str, "%Y-%m-%d").date()
        for s_id, req_list in availability.items():
            for req in req_list:
                # req is a tuple: (surgeon_id, request_type, date)
                raw = req.get('date')
                if isinstance(raw, datetime.date):
                    req_date = raw
                else:
                    try:
                        req_date = datetime.datetime.strptime(raw, "%Y-%m-%d").date()
                    except:
                        continue
                if req_date == current_date and req.get('request_type') in ("unavailable","no_call") and no_call_hard:
                    for lvl in all_levels:
                        if s_id in domains_by_day[d][lvl]:
                            domains_by_day[d][lvl].remove(s_id)
    
    indicator_1B = {}

    # --- Decision Variables ---
    X = {}
    for d in range(num_days):
        for lvl in all_levels:
            dom = domains_by_day[d][lvl]
            # if your bans emptied it, leave a single “–1” so the solver can mark infeasible
            if not dom:
                dom = [-1]
            X[(d, lvl)] = model.NewIntVarFromDomain(
                cp_model.Domain.FromValues(dom),
                f"X_{d}_{lvl}"
            )

        # rebuild your 1B indicator off of the new X[(d,"1B")]
        b1B = model.NewBoolVar(f"b1B_{d}")
        model.Add(X[(d, "1B")] != -1).OnlyEnforceIf(b1B)
        model.Add(X[(d, "1B")] == -1).OnlyEnforceIf(b1B.Not())
        indicator_1B[d] = b1B

    print("Preassignments:", preassignments)
    # ----- Apply Preassignment Constraints -----
    # If preassignments is provided, force the variable to match the assigned surgeon.
    # Expected format: { "YYYY-MM-DD": { "1A": surgeon_id, "3": surgeon_id, ... }, ... }
    if preassignments:
        for d, day_str in enumerate(days):
            if day_str in preassignments:
                for level, surgeon_id in preassignments[day_str].items():
                    # Ensure surgeon_id is an integer, if not already.
                    try:
                        assigned_id = int(surgeon_id)
                    except Exception:
                        print(f"Error converting surgeon id {surgeon_id} on {day_str} at level {level}")
                        continue
                    # Debug print: show available domain for that slot.
                    print(f"Day {day_str} level {level} domain: {domains_by_day[d][level]}, preassigned: {assigned_id}")
                    # Enforce the constraint unconditionally:
                    model.Add(X[(d, level)] == assigned_id)

# prevent same surgeon from being assigned twice on same day

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


    ### for fully staffing
    fully_staffed = []
    BigDayPenalty = 100  # big enough to outweigh any other trade‑off

    for d in range(num_days):
        # one BoolVar per (d,level) signaling “that slot is filled”
        b_filled = {}
        for level in all_levels:
            b = model.NewBoolVar(f"filled_{d}_{level}")
            model.Add( X[(d,level)] != -1 ).OnlyEnforceIf(b)
            model.Add( X[(d,level)] == -1 ).OnlyEnforceIf(b.Not())
            b_filled[level] = b

        # now a BoolVar that says “*all* levels that day are filled”
        f = model.NewBoolVar(f"fully_staffed_day_{d}")
        # f → each b_filled[level]
        for b in b_filled.values():
            model.AddImplication(f, b)
        # if *every* b_filled[level] is true, then f must be true:
        #   sum(b_filled) ≥ L → f = 1
        model.Add( sum(b_filled.values()) >= len(all_levels) ).OnlyEnforceIf(f)
        # if any slot empty, f can be 0
        fully_staffed.append(f)
        total_1B = model.NewIntVar(0, num_days, 'total_1B')
        model.Add(total_1B == sum(indicator_1B[d] for d in range(num_days)))

    indicators = {}
        # --- Prevent any surgeon from having two calls on the same day ---
    for d in range(num_days):
        for lvl1 in all_levels:
            for lvl2 in all_levels:
                if lvl1 >= lvl2:
                    continue
                # only enforce uniqueness if both slots are actually filled
                b1 = model.NewBoolVar(f"filled_{d}_{lvl1}_dupcheck")
                b2 = model.NewBoolVar(f"filled_{d}_{lvl2}_dupcheck")
                # b1 == 1 ↔ X[(d,lvl1)] != -1
                model.Add(X[(d, lvl1)] != -1).OnlyEnforceIf(b1)
                model.Add(X[(d, lvl1)] == -1).OnlyEnforceIf(b1.Not())
                # b2 == 1 ↔ X[(d,lvl2)] != -1
                model.Add(X[(d, lvl2)] != -1).OnlyEnforceIf(b2)
                model.Add(X[(d, lvl2)] == -1).OnlyEnforceIf(b2.Not())
                # if both filled, they must not be the same surgeon
                model.Add(X[(d, lvl1)] != X[(d, lvl2)]).OnlyEnforceIf([b1, b2])

    # --- Constraint Set 2: 3-Day Gap ---
    for d in range(num_days):
        for d2 in range(d + 1, min(num_days, d + 3)):
            for lev1 in all_levels:
                for lev2 in all_levels:
                    b1 = model.NewBoolVar(f'nonempty_{d}_{lev1}')
                    b2 = model.NewBoolVar(f'nonempty_{d2}_{lev2}')
                    model.Add(X[(d, lev1)] != -1).OnlyEnforceIf(b1)
                    model.Add(X[(d, lev1)] == -1).OnlyEnforceIf(b1.Not())
                    model.Add(X[(d2, lev2)] != -1).OnlyEnforceIf(b2)
                    model.Add(X[(d2, lev2)] == -1).OnlyEnforceIf(b2.Not())
                    model.Add(X[(d, lev1)] != X[(d2, lev2)]).OnlyEnforceIf([b1, b2])

### Enforce 3 day gap for the last 3 days of the previous month as well               
    if prev_schedule:
    # Map surgeon names to IDs (assuming prev_schedule stores names)
        name_to_id = {s["name"]: s["id"] for s in surgeons}
        # Parse previous schedule dates into date objects and sort them.
        prev_dates = sorted([
            datetime.datetime.strptime(dstr, "%Y-%m-%d").date()
            for dstr in prev_schedule.keys()
        ])
        # Get the last three days from the previous month
        last_three = prev_dates[-3:]
        
        # Build banned sets for each of the first three days.
        banned_day0 = set()  # For current day 1: ban surgeons from all last 3 days
        banned_day1 = set()  # For current day 2: ban surgeons from the last 2 days
        banned_day2 = set()  # For current day 3: ban surgeons from the last day

        for pd in last_three:
            for level in all_levels:
                prev_name = prev_schedule.get(pd.isoformat(), {}).get(level)
                if prev_name:
                    s_id = name_to_id.get(prev_name)
                    if s_id is not None:
                        banned_day0.add(s_id)
        
        for pd in last_three[-2:]:
            for level in all_levels:
                prev_name = prev_schedule.get(pd.isoformat(), {}).get(level)
                if prev_name:
                    s_id = name_to_id.get(prev_name)
                    if s_id is not None:
                        banned_day1.add(s_id)
        
        for pd in last_three[-1:]:
            for level in all_levels:
                prev_name = prev_schedule.get(pd.isoformat(), {}).get(level)
                if prev_name:
                    s_id = name_to_id.get(prev_name)
                    if s_id is not None:
                        banned_day2.add(s_id)

        # Now remove these banned surgeons from the domains of day 0, 1, and 2 respectively.
        if num_days >= 1:
            for lvl in all_levels:
                for s in banned_day0:
                    model.Add(X[(0, lvl)] != s)
        if num_days >= 2:
            for lvl in all_levels:
                for s in banned_day1:
                    model.Add(X[(1, lvl)] != s)
        if num_days >= 3:
            for lvl in all_levels:
                for s in banned_day2:
                    model.Add(X[(2, lvl)] != s)

    # --- Force 1B to be filled on weekends and public holidays ---
    for d, day_str in enumerate(days):
        dt = datetime.datetime.strptime(day_str, "%Y-%m-%d").date()
        is_weekend = dt.weekday() >= 5
        is_ph      = public_holidays and (day_str in public_holidays)
        if is_weekend or is_ph:
            # slot 1B must not be the “–1” hole
            model.Add( X[(d, "1B")] != -1 )

    # --- Level‑2 Grouping & Supervision Constraints ---
    for d in range(num_days):
        # domains for 2B are pure supervisors + hole
        # (this was already done in base_domains before var creation)
        # now enforce the supervision rules:

        # 1) If a group‑1 surgeon is on 2A, we *must* have someone in 2B:
        for s in group1_ids:
            b1 = model.NewBoolVar(f"lvl2_grp1_day{d}_is_s{s}")
            model.Add(X[(d, "2A")] == s).OnlyEnforceIf(b1)
            model.Add(X[(d, "2A")] != s).OnlyEnforceIf(b1.Not())
            model.Add(X[(d, "2B")] != -1).OnlyEnforceIf(b1)

        # 2) If a group‑2 or 3 surgeon is on 2A, they need no supervision → forbid any 2B:
        for s in group2_ids:
            b2 = model.NewBoolVar(f"lvl2_grp2_day{d}_is_s{s}")
            model.Add(X[(d, "2A")] == s).OnlyEnforceIf(b2)
            model.Add(X[(d, "2A")] != s).OnlyEnforceIf(b2.Not())
            model.Add(X[(d, "2B")] == -1).OnlyEnforceIf(b2)

        for s in group3_ids:
            b3 = model.NewBoolVar(f"lvl2_grp3_day{d}_is_s{s}")
            model.Add(X[(d, "2A")] == s).OnlyEnforceIf(b3)
            model.Add(X[(d, "2A")] != s).OnlyEnforceIf(b3.Not())
            model.Add(X[(d, "2B")] == -1).OnlyEnforceIf(b3)

        # 3) Never allow Group 4 in 2A (they only supervise):
        for s in group4_ids:
            model.Add(X[(d, "2A")] != s)

        # 4) Never let the same person occupy both slots:
        model.Add(X[(d, "2A")] != X[(d, "2B")])

    # ── Prevent Group-4 surgeons from covering both 2B and 3 on the same day ──
    for d in range(num_days):
        for s1 in group4_ids:
            for s2 in group4_ids:
                # forbid the pair (2B=s1, 3=s2)
                model.AddForbiddenAssignments(
                    [ X[(d, "2B")], X[(d, "3")] ],
                    [ [s1, s2] ]
                )

    # --- Constraint Set 3: Maximum Calls per Group ---
    for d in range(num_days):
        for lev in all_levels:
            for s_id in all_ids:
                b = model.NewBoolVar(f"ind_{d}_{lev}_{s_id}")
                model.Add(X[(d, lev)] == s_id).OnlyEnforceIf(b)
                model.Add(X[(d, lev)] != s_id).OnlyEnforceIf(b.Not())
                indicators[(d, lev, s_id)] = b

    # --- At most one 2B‐shift per Group 4 surgeon over the entire schedule ---
    for s in group4_ids:
        # Sum up all the days where s is in 2B, force ≤ 1
        model.Add(
            sum(indicators[(d, "2B", s)] for d in range(num_days))
            <= 1
        )

    call_count_overall = {}
    for s in all_surgeon_ids:
        call_count_overall[s] = model.NewIntVar(0, num_days * len(all_levels), f'count_all_{s}')
        model.Add(call_count_overall[s] == sum(indicators[(d, level, s)] for d in range(num_days) for level in all_levels))
    
    for s_id in all_ids:
        c1 = model.NewIntVar(0, num_days*2, f"count1_{s_id}")
        model.Add(
            c1 == sum(indicators[(d, "1A", s_id)] + indicators[(d, "1B", s_id)]
                        for d in range(num_days))
        )
        model.Add(c1 <= max_calls_level1)

    non_nlth_surgeons = [s for s in all_surgeon_ids if s not in nlth_ids]

    max_all = model.NewIntVar(0, num_days * len(all_levels), 'max_all')
    min_all = model.NewIntVar(0, num_days * len(all_levels), 'min_all')
    model.AddMaxEquality(max_all, [call_count_overall[s] for s in non_nlth_surgeons])
    model.AddMinEquality(min_all, [call_count_overall[s] for s in non_nlth_surgeons])
    diff_all = model.NewIntVar(0, num_days * len(all_levels), 'diff_all')
    model.Add(diff_all == max_all - min_all)


    # --- Balance within skill‐groups: no more than 1 call difference ---

    # Build the four balancing groups by surgeon ID:
    grp_defs = [
        ("grp1", [s["id"] for s in surgeons if set(parse_call_levels(s.get("call_levels",""))).intersection({"1A","1B"})]),
        ("grp2", [s["id"] for s in surgeons if ("2A" in parse_call_levels(s.get("call_levels","")) or "2B" in parse_call_levels(s.get("call_levels",""))) and "3" not in parse_call_levels(s.get("call_levels",""))]),
        ("grp3", [s["id"] for s in surgeons if "3" in parse_call_levels(s.get("call_levels",""))]),
        ("grp4", [s["id"] for s in surgeons if "4" in parse_call_levels(s.get("call_levels",""))])
    ]
    
    for i, (_, grp) in enumerate(grp_defs, start=1):
        # remove any NLTH
        grp_clean = [s for s in grp if s not in nlth_ids]
        if len(grp_clean) > 1:
            max_g = model.NewIntVar(0, num_days * len(all_levels), f"max_calls_group{i}")
            min_g = model.NewIntVar(0, num_days * len(all_levels), f"min_calls_group{i}")
            model.AddMaxEquality(max_g, [call_count_overall[s] for s in grp_clean])
            model.AddMinEquality(min_g, [call_count_overall[s] for s in grp_clean])
            model.Add(max_g - min_g <= 1)

    # 4) Exempt NLTH from all that—and instead just keep calls between 2-3:
    for s in nlth_ids:
        model.Add(call_count_overall[s] <= 3)
        model.Add(call_count_overall[s] >= 2)

    # --- Then, create a per-surgeon, per-day “assigned” BoolVar ----
    assigned = {}
    for s in all_surgeon_ids:
        for d in range(num_days):
            a = model.NewBoolVar(f"assigned_s{s}_d{d}")
            # a == 1 ↔ sum over levels of indicators[d, level, s] ≥ 1
            model.Add(
                sum(indicators[(d, level, s)] for level in all_levels) >= 1
            ).OnlyEnforceIf(a)
            model.Add(
                sum(indicators[(d, level, s)] for level in all_levels) == 0
            ).OnlyEnforceIf(a.Not())
            assigned[(s, d)] = a

    # --- NLTH constraint: allow NLTH surgeons only on days where today AND tomorrow are weekend or PH ---

    # # Precompute flags for each calendar day
    is_weekend = [
        datetime.datetime.strptime(day, "%Y-%m-%d").weekday() >= 5
        for day in days
    ]
    is_ph = [
        (public_holidays is not None and day in public_holidays)
        for day in days
    ]
    is_wk_or_ph = [
        wk or ph for wk, ph in zip(is_weekend, is_ph)
    ]

    is_ph = [day in public_holidays for day in days]
    is_wk_or_ph = [w or ph for w,ph in zip(is_weekend, is_ph)]

    for d in range(num_days):
        # only allow NLTH if today AND the next day are wk/PH
        allow_nlth = (d < num_days - 1) and is_wk_or_ph[d] and is_wk_or_ph[d+1]
        if not allow_nlth:
            for lvl in all_levels:
                for s_id in nlth_ids:
                    # Hard ban
                    model.Add(X[(d, lvl)] != s_id)

    # soft penalties for weekend call balance
    # 2) build a BoolVar w_call[s,d] == “s is assigned on a weekend d”
    w_call = {}
    for s in all_surgeon_ids:
        for d in range(num_days):
            w = model.NewBoolVar(f"weekend_call_s{s}_d{d}")
            w_call[(s,d)] = w
            if is_weekend[d]:
                # on weekends, w==assigned
                model.Add(w == assigned[(s,d)])
            else:
                # on weekdays, force w==0
                model.Add(w == 0)

    # 3) count weekend calls per surgeon and hard‐limit to 3
    weekend_count = {}
    for s in all_surgeon_ids:
        wc = model.NewIntVar(0, num_days, f"weekend_count_s{s}")
        model.Add(wc == sum(w_call[(s,d)] for d in range(num_days)))
        weekend_count[s] = wc
        model.Add(wc <= 3)   # hard cap: no more than 3 weekend calls

    consec_penalties = []
    for s in all_surgeon_ids:
        for d in range(num_days-1):
            if is_weekend[d] and is_weekend[d+1]:
                b = model.NewBoolVar(f"consec_wknd_s{s}_d{d}")
                # b == 1 ↔ both w_call[(s,d)] and w_call[(s,d+1)]
                model.AddBoolAnd([w_call[(s,d)], w_call[(s,d+1)]]).OnlyEnforceIf(b)
                model.AddBoolOr([w_call[(s,d)].Not(), w_call[(s,d+1)].Not()]).OnlyEnforceIf(b.Not())
                consec_penalties.append(b)

    # aggregate consecutive‐weekend penalty
    if consec_penalties:
        consec_penalty = model.NewIntVar(0, len(consec_penalties), "penalty_consec_weekend")
        model.Add(consec_penalty == sum(consec_penalties))
    else:
        consec_penalty = model.NewIntVar(0, 0, "penalty_consec_weekend")
        model.Add(consec_penalty == 0)

    # 5) within‐group weekend‐balance soft constraints
    weekend_diff_terms = []
    for i, (_, grp) in enumerate(grp_defs, start=1):
        # remove any NLTH
        grp_clean = [s for s in grp if s not in nlth_ids]
        if len(grp_clean) > 1:
            max_w = model.NewIntVar(0, num_days, f"max_wknd_grp{i}")
            min_w = model.NewIntVar(0, num_days, f"min_wknd_grp{i}")
            model.AddMaxEquality(max_w, [weekend_count[s] for s in grp])
            model.AddMinEquality(min_w, [weekend_count[s] for s in grp])
            diff = model.NewIntVar(0, num_days, f"diff_wknd_grp{i}")
            model.Add(diff == max_w - min_w)
            weekend_diff_terms.append(diff)


    # now collect a list of (coeff, BoolVar) for the soft penalties

    #Soft constraint for team preference of the day

    td_terms = []

    for d, day_str in enumerate(days):
        wd = datetime.datetime.strptime(day_str, "%Y-%m-%d").weekday()
        for lvl in all_levels:
            # the indicator b==1 if that slot is filled by surgeon s
            for s in surgeons:
                sid  = s['id']
                team = s.get('team')
                if team in team_day_prefs:
                    adj = team_day_prefs[team].get(wd, 0)
                    if adj != 0:
                        b = indicators[(d, lvl, sid)]
                        coef = gamma_team_pref * adj
                        # a positive adj means “penalize assignments on that day for that team”
                        td_terms.append((coef, b))


    # --- Soft Penalties for Availability ---
    soft_penalties_unavail_prev = []
    # Penalty for assigning someone on a day they flagged unavailable the previous day
    for i in range(num_days - 1):
        # Compute the date object for the next day in the schedule
        next_day = datetime.datetime.strptime(days[i+1], "%Y-%m-%d").date()
        for s_id, req_list in get_availability_requests().items():
            for req in req_list:
                raw = req["date"]
                # Postgres may return a date object already
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
                        model.Add(X[(i, lev)] == s_id).OnlyEnforceIf(b)
                        model.Add(X[(i, lev)] != s_id).OnlyEnforceIf(b.Not())
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
                            model.Add(X[(i, lev)] == s_id).OnlyEnforceIf(b)
                            model.Add(X[(i, lev)] != s_id).OnlyEnforceIf(b.Not())
                            soft_penalties_nocall.append(b)
    
    penalty_unavail_prev = model.NewIntVar(0, num_days * len(all_levels) * 10, 'penalty_unavail_prev')
    if soft_penalties_unavail_prev:
        model.Add(penalty_unavail_prev == sum(soft_penalties_unavail_prev))
    else:
        model.Add(penalty_unavail_prev == 0)
    
    penalty_nocall = model.NewIntVar(0, num_days * len(all_levels) * 10, 'penalty_nocall')
    if soft_penalties_nocall:
        model.Add(penalty_nocall == sum(soft_penalties_nocall))
    else:
        model.Add(penalty_nocall == 0)
    
    # --- Additional Fairness: Penalize Deviation from Average Calls ---
    # Let T be the total calls and N be the number of surgeons.
    N = len(all_surgeon_ids)
    T = model.NewIntVar(0, num_days * len(all_levels) * N, "T")
    model.Add(T == sum(call_count_overall[s] for s in all_surgeon_ids))
    deviations = {}
    for s in all_surgeon_ids:
        # diff = (call_count_overall[s]*N - T)
        diff = model.NewIntVar(-num_days * len(all_levels) * N, num_days * len(all_levels) * N, f"diff_{s}")
        model.Add(diff == call_count_overall[s] * N - T)
        deviations[s] = model.NewIntVar(0, num_days * len(all_levels) * N, f"dev_{s}")
        model.Add(deviations[s] >= diff)
        model.Add(deviations[s] >= -diff)
    deviation_sum = model.NewIntVar(0, num_days * len(all_levels) * N, "deviation_sum")
    model.Add(deviation_sum == sum(deviations[s] for s in all_surgeon_ids))
    
        # --- Create soft‐penalties for any two calls within spacing_threshold days ---
    spacing_penalties = []
    for s in all_surgeon_ids:
        for d in range(num_days):
            for d2 in range(d + 1,
                             min(num_days, d + spacing_threshold)):
                b = model.NewBoolVar(f"close_{s}_{d}_{d2}")
                # b == 1 ↔ assigned[s,d] AND assigned[s,d2]
                model.AddBoolAnd([assigned[(s, d)], assigned[(s, d2)]]).OnlyEnforceIf(b)
                model.AddBoolOr(
                    [assigned[(s, d)].Not(), assigned[(s, d2)].Not()]
                ).OnlyEnforceIf(b.Not())
                spacing_penalties.append(b)

    penalty_spacing = model.NewIntVar(0, len(spacing_penalties), "penalty_spacing")
    if spacing_penalties:
        model.Add(penalty_spacing == sum(spacing_penalties))
    else:
        model.Add(penalty_spacing == 0)

    # --- Finally, include this in your objective with its weight ---
    # existing objective: fairness_weight * diff_all ... + gamma_balance * deviation_sum
    # just add: + gamma_spacing * penalty_spacing
    objective_terms = [
        fairness_weight * diff_all
        - gamma_1B * total_1B
        + gamma_no_call * penalty_nocall
        + gamma_unavail_prev * penalty_unavail_prev
        + gamma_balance * deviation_sum
        + gamma_spacing * penalty_spacing
        - BigDayPenalty * sum(fully_staffed)
        + gamma_weekend_balance * (sum(weekend_diff_terms) * 10)
        + gamma_consec_weekend   * consec_penalty
        - coef * b for coef, b in td_terms
    ]
    model.Minimize( sum(objective_terms) )

    # --- Solve the Model ---
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds   = 30    # so it will keep going if you don’t cancel
    solver.parameters.log_search_progress   = True   # print logs to console
    solver.parameters.log_to_stdout         = True

    status = solver.Solve(model)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        solution = {
          days[d]: {
            level: ( next(
                      (s["name"] for s in surgeons 
                       if s["id"] == solver.Value(X[(d,level)])), None
                    ))
            for level in all_levels
          } for d in range(num_days)
        }
        return solution, solver.ObjectiveValue()
    return None, None
