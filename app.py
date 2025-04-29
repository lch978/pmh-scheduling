import random
import math
import datetime
import calendar
import json
import holidays
import threading, uuid
from dateutil.parser import parse
import os
import io
import pandas as pd
from sqlalchemy import text
from flask_basicauth import BasicAuth
from dotenv import load_dotenv
from helper import *
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, g, session, send_file, abort

def create_app():
    
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY","dev-key")

    # login for stuff
    load_dotenv()
    basic_auth = BasicAuth(app)
    app.config['BASIC_AUTH_USERNAME'] = os.getenv('BASIC_AUTH_USERNAME')
    app.config['BASIC_AUTH_PASSWORD'] = os.getenv('BASIC_AUTH_PASSWORD')

    with app.app_context():
        init_db()

    solve_jobs = {}

    @app.context_processor
    def utility_processor():
        return {
            'parse_call_levels': parse_call_levels
        }
    
    @app.teardown_appcontext
    def teardown_db(exception):
        close_db(exception)
            
    #############################################
    # Surgeon Management Endpoints
    #############################################

    @app.route('/surgeons')
    @basic_auth.required
    def list_surgeons():
        surgeons = get_all_surgeons()
        if request.args.get('sort'):
            level_order = {"1A": 1, "1B": 1, "2A": 2, "2B": 2, "3": 3, "4": 4}
            def get_lowest_level(s):
                levels = parse_call_levels(s.get("call_levels", ""))
                if not levels:
                    return 99
                orders = [level_order.get(l, 99) for l in levels]
                return min(orders)
            surgeons.sort(key=lambda s: (get_lowest_level(s), s["name"].lower()))
        return render_template('surgeons_list.html', surgeons=surgeons)
    
    @app.route('/surgeons/sort_by_team_then_level')
    def list_surgeons_by_team_then_level():
        surgeons = get_all_surgeons()

        # same order you use elsewhere for levels
        level_order = {"1A":1, "1B":1, "2A":2, "2B":2, "3":3, "4":4}
        def lowest_level(s):
            levels = parse_call_levels(s.get("call_levels",""))
            if not levels:
                return float('inf')
            return min(level_order.get(l, float('inf')) for l in levels)

        # sort by: team (None last), then by lowest call level, then name
        surgeons.sort(key=lambda s: (
            (s['team'] or 'zzz'),
            lowest_level(s),
            s['name'].lower()
        ))

        return render_template('surgeons_list.html', surgeons=surgeons)

    @app.route('/surgeons/add', methods=['GET', 'POST'])
    def add_surgeon():
        if request.method == 'POST':
            name = request.form['name']
            call_levels_list = request.form.getlist('call_levels')
            call_levels = ','.join(call_levels_list)
            team  = request.form['team']
            db = get_db()
            db.execute(
                text("INSERT INTO surgeons (name, call_levels, team) VALUES (:name, :levels, :team)"),
                {"name": name, "levels": call_levels, "team": team}
)
            db.commit()
            flash("Surgeon added successfully!")
            return redirect(url_for('list_surgeons'))
        return render_template('surgeon_form.html', surgeon={}, action="Add")

    @app.route('/surgeons/edit/<int:surgeon_id>', methods=['GET', 'POST'])
    def edit_surgeon(surgeon_id):
        db = get_db()
        row = db.execute(
            text("SELECT * FROM surgeons WHERE id = :id"),
            {"id": surgeon_id}
        ).fetchone()
        if not row:
            flash("Surgeon not found!")
            return redirect(url_for('list_surgeons'))
        surgeon = dict(row)
        if request.method == 'POST':
            name = request.form['name']
            call_levels_list = request.form.getlist('call_levels')
            call_levels = ','.join(call_levels_list)
            team = request.form['team']
            db.execute(
                text("UPDATE surgeons SET name = :name, call_levels = :levels, team = team WHERE id = :id"),
                {"name": name, "levels": call_levels, "team": team, "id": surgeon_id}
            )
            db.commit()
            flash("Surgeon updated successfully!")
            return redirect(url_for('list_surgeons'))
        return render_template('surgeon_form.html', surgeon=surgeon, action="Edit")

    @app.route('/surgeons/update/<int:surgeon_id>', methods=['POST'])
    def update_surgeon_inline(surgeon_id):
        name = request.form['name']
        call_levels_list = request.form.getlist('call_levels')
        call_levels = ','.join(call_levels_list)
        team = request.form['team']

        db = get_db()
        db.execute(
                text("UPDATE surgeons SET name = :name, call_levels = :levels, team = team WHERE id = :id"),
                {"name": name, "levels": call_levels, "team": team, "id": surgeon_id}
        )
        db.commit()
        flash("Surgeon updated successfully!")
        return redirect(url_for('list_surgeons'))

    @app.route('/update_all_surgeons', methods=['POST'])
    def update_all_surgeons():
        db = get_db()
        surgeons = get_all_surgeons()
        # Prepare a single text‐Clause for performance
        stmt = text("""
            UPDATE surgeons
            SET name       = :name,
                call_levels= :levels,
                nlth       = :nlth,
                team       = :team
            WHERE id         = :id
        """)

        for surgeon in surgeons:
            sid = surgeon['id']
            name_field = f"name_{sid}"
            levels_field = f"call_levels_{sid}"
            nlth_field = f"nlth_{sid}"
            team_field = f"team_{sid}"

            new_name   = request.form.get(name_field, surgeon['name'])
            new_levels = request.form.getlist(levels_field)
            new_levels_str = ",".join(new_levels)
            new_team = request.form.get(team_field) or None

            # Checkbox: if the field is present in form data, checkbox was checked
            new_nlth = (request.form.get(nlth_field) == 'on')

            db.execute(
                stmt,
                {
                    "name":   new_name,
                    "levels": new_levels_str,
                    "nlth":   new_nlth,
                    "team":   new_team,
                    "id":     sid
                }
            )
        db.commit()
        flash("Surgeon updates applied successfully!")
        return redirect(url_for('list_surgeons'))

    #############################################
    # Maximum Calls Configuration Endpoint
    #############################################

    @app.route('/config_max_calls', methods=['GET', 'POST'])
    @basic_auth.required
    def config_max_calls():
        if request.method == 'POST':
            new_config = {
                "1": int(request.form.get("group_1", 10)),
                "2": int(request.form.get("group_2", 10)),
                "3": int(request.form.get("group_3", 10)),
                "4": int(request.form.get("group_4", 10))
            }
            update_max_calls_config(new_config)
            flash("Maximum calls configuration updated successfully!")
            return redirect(url_for('config_max_calls'))
        config = get_max_calls_config()
        return render_template('config_max_calls.html', config=config)

    #############################################
    # Global Config for No Call Request Constraint Endpoint
    #############################################

    @app.route('/global_config', methods=['GET', 'POST'])
    @basic_auth.required
    def global_config_page():
        teams = ['Team 1','Team 2','Team 3','Team 4','Urology']
        if request.method == 'POST':
            days = list(range(7))
            new_prefs = {}
            for team in teams:
                new_prefs[team] = {
                wd: int(request.form.get(f'pref_{team}_{wd}', 0))
                for wd in days
                }
            update_team_day_prefs(new_prefs)
            no_call_hard_val = request.form.get("no_call_hard", "1")
            update_global_config({
                "no_call_hard": no_call_hard_val,
                "fairness_weight": request.form.get("fairness_weight", "1000"),
                "gamma_no_call": request.form.get("gamma_no_call", "10"),
                "gamma_unavail_prev": request.form.get("gamma_unavail_prev", "5"),
                "gamma_1B": request.form.get("gamma_1B", "1"),
                "gamma_spacing": request.form.get("gamma_spacing", "10"),
                "spacing_threshold": request.form.get("spacing_threshold", "7"),
                "gamma_team_pref": request.form.get("gamma_team_pref", "10")
            })
            flash("Global configuration updated successfully!")
            return redirect(url_for('global_config_page'))
        else:
            config = get_global_config()
            prefs  = get_team_day_prefs()
            return render_template('global_config.html', config=config, prefs=prefs, teams=teams)

            
    #############################################
    # Constraint weights Endpoints
    #############################################

    @app.route('/constraint_weights', methods=['GET', 'POST'])
    def constraint_weights():
        if request.method == 'POST':
            # Retrieve new weight values from the form.
            fairness_weight = request.form.get('fairness_weight', '1000')
            gamma_no_call = request.form.get('gamma_no_call', '10')
            gamma_unavail_prev = request.form.get('gamma_unavail_prev', '5')
            gamma_1B = request.form.get('gamma_1B', '1')
            gamma_spacing = request.form.get('gamma_spacing', '10')
            spacing_threshold = request.form.get("spacing_threshold", "7")
            gamma_team_pref = request.form.get("gamma_team_pref", "10")
            # Update global configuration.
            update_global_config({
                "fairness_weight": fairness_weight,
                "gamma_no_call": gamma_no_call,
                "gamma_unavail_prev": gamma_unavail_prev,
                "gamma_1B": gamma_1B,
                "gamma_spacing": gamma_spacing,
                "spacing_threshold": spacing_threshold,
                "gamma_team_pref": gamma_team_pref
            })
            flash("Constraint weights updated successfully!")
            return redirect(url_for('constraint_weights'))
        else:
            config = get_global_config()
            return render_template('constraint_weights.html', config=config)


    #############################################
    # Schedule Generation and Saving Endpoints
    #############################################
    def run_solver_job(job_id, days, surgeons, prev_schedule, public_holidays, preassignments):
        # we only import the solver function here—never re‑import `app` or `solve_jobs`
        from scheduler import solve_schedule_or_tools

        # mark the job as running
        solve_jobs[job_id] = {
            'status': 'running',
            'best':   None,
            'cancel': False,
            'solution': None
        }

        # we need the app_context so that any DB or flask helpers will work
        with app.app_context():
            sched, cost = solve_schedule_or_tools(
                days, surgeons, prev_schedule, public_holidays, preassignments
            )

            if sched is not None:
                solve_jobs[job_id].update({
                    'status':   'done',
                    'solution': sched,
                    'best':     cost
                })
            else:
                solve_jobs[job_id]['status'] = 'failed'

    @app.route('/new_schedule', methods=['GET'])
    @basic_auth.required
    def new_schedule():
        import datetime, calendar, json
        from scheduler import solve_schedule_or_tools

        # ── 1) Ensure year/month are always in the URL ──
        if not request.args.get('year') or not request.args.get('month'):
            today = datetime.date.today()
            return redirect(url_for(
                'new_schedule',
                year=today.year,
                month=today.month
            ))

        # ── 2) Parse year & month ──
        year_sel, month_sel = get_year_month()

        # ── 3) Build list of days for that month ──
        days_sel = [
            datetime.date(year_sel, month_sel, d).isoformat()
            for d in range(1, calendar.monthrange(year_sel, month_sel)[1] + 1)
        ]

        # ── 4) HK holidays ──
        hk_h = holidays.HK(years=[year_sel])
        public_holidays = {
            d.isoformat() for d in hk_h 
            if d.year == year_sel and d.month == month_sel
        }

        # ── 5) Load previous month (for 3‑day spacing) ──
        if month_sel == 1:
            prev_year, prev_month = year_sel - 1, 12
        else:
            prev_year, prev_month = year_sel, month_sel - 1
        db = get_db()
        stmt = text(
            "SELECT schedule_data FROM saved_schedule "
            "WHERE year = :year AND month = :month"
        )
        result = db.execute(stmt, {"year": prev_year, "month": prev_month})
        prev_row = result.mappings().fetchone()
        if prev_row:
            raw = prev_row['schedule_data']
            # if Postgres gave us a dict, use it directly; otherwise parse the JSON text
            if isinstance(raw, dict):
                prev_schedule = raw
            else:
                prev_schedule = json.loads(raw)
        else:
            prev_schedule = None

        # ── Load preassignments for the current month ──
        stmt_pre = text(
            "SELECT preassignment_data FROM preassignments WHERE year = :year AND month = :month"
        )
        result_pre = db.execute(stmt_pre, {"year": year_sel, "month": month_sel})
        row_pre = result_pre.mappings().fetchone()
        print("Fetched preassignment row:", row_pre)  # Debug: show raw DB row

        if row_pre and row_pre['preassignment_data'] is not None and row_pre['preassignment_data'] != "":
            raw_pre = row_pre['preassignment_data']
            print("Raw preassignment data (raw_pre):", raw_pre)  # Debug: show raw data
            try:
                preassignments = raw_pre if isinstance(raw_pre, dict) else json.loads(raw_pre)
            except Exception as e:
                print("Error decoding preassignments:", e)
                preassignments = {}
        else:
            preassignments = {}

        print("Loaded preassignments:", preassignments)

        # ── 6) Do we already have one in the DB? ──
        row = db.execute(
            text("SELECT * FROM saved_schedule WHERE year = :y AND month = :m"),
            {"y": year_sel, "m": month_sel}
        ).fetchone()

        generate_flag = request.args.get('generate')
        if generate_flag:
            # ▶︎ Preview only; do *not* save
            surgeons = get_all_surgeons()
            sched, cost = solve_schedule_or_tools(
                days_sel,
                surgeons,
                prev_schedule=prev_schedule,
                public_holidays=public_holidays,
                preassignments=preassignments
            )
            if sched is None:
                flash("No feasible schedule found.", "error")
                return render_template('no_schedule.html')
        else:
            # ▶︎ No preview request → show the saved one (if any)
            cost = None

            # Fetch as a mapping so row['schedule_data'] works
            stmt = text(
                "SELECT schedule_data FROM saved_schedule "
                "WHERE year = :year AND month = :month"
            )
            result = db.execute(stmt, {"year": year_sel, "month": month_sel})
            row = result.mappings().fetchone()

            if row:
                data = row['schedule_data']
                # If Supabase returned a dict, use it directly; otherwise parse the JSON string
                if isinstance(data, dict):
                    sched = data
                else:
                    sched = json.loads(data)
            else:
                sched = None

        # ── 7) Compute weekends set ──
        weekend_set = {
            d for d in days_sel
            if datetime.date.fromisoformat(d).weekday() >= 5
        }

        # ── 8) Render in both cases ──
        return render_template(
            'new_schedule.html',
            schedule=sched,
            cost=cost,
            weekend_set=weekend_set,
            public_holidays=public_holidays,
            year=year_sel,
            month=month_sel
        )

    @app.route('/save_schedule', methods=['POST'])
    def save_schedule():
        # 1) Figure out which month/year we’re saving
        year_str = request.form.get('year') or request.args.get('year')
        month_str = request.form.get('month') or request.args.get('month')
        try:
            year = int(year_str)
            month = int(month_str)
        except (TypeError, ValueError):
            flash("Invalid year/month for saving schedule.", "error")
            # Redirect back to the new_schedule view, preserving whatever we have
            return redirect(url_for('new_schedule',
                                    year=year_str or None,
                                    month=month_str or None))

        # 2) Grab the JSON payload
        data = request.form.get('schedule_json')
        if not data:
            flash("No schedule data provided to save.", "error")
            return redirect(url_for('new_schedule', year=year, month=month))

        db = get_db()
        exists = db.execute(
            text("SELECT 1 FROM saved_schedule WHERE year = :y AND month = :m"),
            {"y": year, "m": month}
        ).fetchone()

        if exists:
            db.execute(
                text(
                    "UPDATE saved_schedule "
                    "SET schedule_data = :sched, date_saved = now() "
                    "WHERE year = :y AND month = :m"
                ),
                {"sched": data, "y": year, "m": month}
            )
        else:
            db.execute(
                text(
                    "INSERT INTO saved_schedule "
                    "(year, month, schedule_data, date_saved) "
                    "VALUES (:y, :m, :sched, now())"
                ),
                {"y": year, "m": month, "sched": data}
            )
        db.commit()
        flash("Schedule saved.", "success")
        return redirect(url_for('new_schedule', year=year, month=month))

    @app.route('/saved_schedule', methods=['GET'])
    def saved_schedule():
        year_sel, month_sel = get_year_month()
        # Generate the days for the selected period.
        days_sel = [datetime.date(year_sel, month_sel, d).isoformat() 
                    for d in range(1, calendar.monthrange(year_sel, month_sel)[1] + 1)]
        
        db = get_db()
        stmt = text(
            "SELECT schedule_data FROM saved_schedule "
            "WHERE year = :year AND month = :month"
        )
        result = db.execute(stmt, {"year": year_sel, "month": month_sel})
        row = result.mappings().fetchone()
        if row:
            data = row['schedule_data']
            # if Postgres gave us a dict, use it directly; otherwise parse the JSON string
            if isinstance(data, dict):
                sched = data
            else:
                sched = json.loads(data)
        else:
            flash("No saved schedule found for the selected period.")
            sched = {}

        # Use the locally computed days_sel to determine weekend_set.
        weekend_set = {d for d in days_sel if datetime.date.fromisoformat(d).weekday() >= 5}
        return render_template('saved_schedule.html', schedule=sched, weekend_set=weekend_set,
                            year=year_sel, month=month_sel)

    @app.route('/export_schedule', methods=['POST'])
    def export_schedule():
        year = int(request.form['year'])
        month = int(request.form['month'])
        sched_json = request.form.get('schedule_json')
        if not sched_json:
            flash("Nothing to export!", "error")
            return redirect(url_for('new_schedule', year=year, month=month))

        # 1) Load schedule dict
        schedule = json.loads(sched_json)

        # 2) Build schedule DataFrame
        df_sched = (
            pd.DataFrame.from_dict(schedule, orient='index')
            .rename_axis('Date')
            .reset_index()
        )
        cols = ['Date','1A','1B','2A','2B','3','4']
        df_sched = df_sched[cols]

        # 3) Compute per-surgeon stats
        level_ranks = {"1A":1,"1B":1,"2A":2,"2B":3,"3":4,"4":5}
        stats = {}
        weekend = set(d for d in schedule if datetime.date.fromisoformat(d).weekday() >= 5)
        for date, assigns in schedule.items():
            is_wk = date in weekend
            for lvl, name in assigns.items():
                if not name:
                    continue
                rec = stats.setdefault(name, {"total_calls":0,"weekend_calls":0,"min_level_rank":None})
                rec["total_calls"] += 1
                if is_wk:
                    rec["weekend_calls"] += 1
                rank = level_ranks.get(lvl,99)
                if rec["min_level_rank"] is None or rank < rec["min_level_rank"]:
                    rec["min_level_rank"] = rank
        df_stats = (
            pd.DataFrame([
                {"Surgeon": name,
                 "Total Calls": v["total_calls"],
                 "Weekend Calls": v["weekend_calls"],
                 "Min Level Rank": v["min_level_rank"]}
                for name,v in stats.items()
            ])
            .sort_values(["Min Level Rank","Surgeon"])
            .reset_index(drop=True)
        )

        # 4) Build unavailability DataFrame
        availability = get_availability_requests()
        surgeons = get_all_surgeons()
        id_to_name = {s['id']: s['name'] for s in surgeons}
        data_unav = []
        for sid, reqs in availability.items():
            for req in reqs:
                if req['date'] in schedule:
                    data_unav.append({
                        'Surgeon': id_to_name.get(sid, ''),
                        'Date': req['date'],
                        'Type': req['request_type']
                    })
        df_unav = pd.DataFrame(data_unav)

        # 5) Gather public holidays
        hk_h = holidays.HK(years=[year])
        ph = {d.isoformat() for d in hk_h if d.year == year and d.month == month}

        # 6) Write to Excel with formatting
        bio = io.BytesIO()
        with pd.ExcelWriter(bio, engine='openpyxl') as writer:
            df_sched.to_excel(writer, sheet_name='Call Schedule', index=False)
            df_stats.to_excel(writer, sheet_name='Monthly Stats', index=False)
            df_unav.to_excel(writer, sheet_name='Unavailability', index=False)

            wb = writer.book
            from openpyxl.styles import Font, PatternFill, Alignment
            # Style Call Schedule sheet
            ws1 = writer.sheets['Call Schedule']
            header_font = Font(bold=True, size=12)
            weekend_fill = PatternFill(start_color='FFF2CC', end_color='FFF2CC', fill_type='solid')
            ph_fill = PatternFill(start_color='D9E1F2', end_color='D9E1F2', fill_type='solid')

            # Format header
            for cell in ws1[1]:
                cell.font = header_font
                cell.alignment = Alignment(horizontal='center')
            # Format rows
            for row in ws1.iter_rows(min_row=2, max_row=ws1.max_row, min_col=1, max_col=1):
                cell = row[0]
                date_str = cell.value
                try:
                    dt = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
                except:
                    continue
                fill = weekend_fill if dt.weekday() >= 5 else (ph_fill if date_str in ph else None)
                if fill:
                    for c in ws1[cell.row]:
                        c.fill = fill
            # Adjust column widths
            for col in ws1.columns:
                max_length = max(len(str(cell.value)) for cell in col)
                ws1.column_dimensions[col[0].column_letter].width = max_length + 2

            # Apply simple styling to other sheets (headers bold)
            for name in ['Monthly Stats', 'Unavailability']:
                ws = writer.sheets[name]
                for cell in ws[1]:
                    cell.font = header_font
                    cell.alignment = Alignment(horizontal='center')

        bio.seek(0)
        filename = f"call_list_{year}_{month:02d}.xlsx"
        return send_file(
            bio,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=filename
        )


    @app.route('/')
    def index():
        year_sel, month_sel = get_year_month()
        db = get_db()
        row = db.execute(
            text("SELECT * FROM saved_schedule WHERE year = :year AND month = :month"),
            {"year": year_sel, "month": month_sel}
        ).fetchone()
        if row:
            return redirect(url_for('saved_schedule', year=year_sel, month=month_sel))
        else:
            return redirect(url_for('saved_schedule', year=year_sel, month=month_sel))

    #############################################
    # Availability / Unavailability Endpoint
    #############################################

    @app.route('/availability', methods=['GET', 'POST'])
    def availability():
        db = get_db()
        if request.method == 'POST':
            # Process submission of a new request over a date range.
            surgeon_id = request.form.get('surgeon_id')
            request_type = request.form.get('request_type')  # "unavailable" or "no_call"
            start_date = request.form.get('start_date')
            end_date = request.form.get('end_date')
            if not surgeon_id or not start_date or not end_date or not request_type:
                flash("Please fill in all fields.")
            else:
                try:
                    start_dt = datetime.datetime.strptime(start_date, "%Y-%m-%d").date()
                    end_dt = datetime.datetime.strptime(end_date, "%Y-%m-%d").date()
                    if start_dt > end_dt:
                        flash("Start date must be before or equal to end date.")
                        return redirect(url_for('availability', surgeon_id=surgeon_id))
                    current_dt = start_dt
                    while current_dt <= end_dt:
                        db.execute(
                            text(
                                "INSERT INTO surgeon_availability "
                                "(surgeon_id, request_type, date) "
                                "VALUES (:sid, :rtype, :dt)"
                            ),
                            {"sid": surgeon_id, "rtype": request_type, "dt": current_dt.isoformat()}
                        )
                        current_dt += datetime.timedelta(days=1)
                    db.commit()
                    flash("Request submitted successfully!")
                except Exception as e:
                    flash(f"Error processing the dates: {str(e)}")
            return redirect(url_for('availability', surgeon_id=surgeon_id))
        else:
            # GET: Display the page with existing requests grouped by date range.
            surgeon_id = request.args.get('surgeon_id')
            try:
                surgeon_id = int(surgeon_id) if surgeon_id else None
            except ValueError:
                surgeon_id = None

            events = {}
            surgeon_name = ""
            if surgeon_id:
                rows = (
                    db.execute(
                        text(
                        "SELECT sa.date, sa.request_type, s.name, s.call_levels "
                        "FROM surgeon_availability sa "
                        "JOIN surgeons s ON sa.surgeon_id = s.id "
                        "WHERE s.id = :sid "
                        "ORDER BY sa.date"
                    ),
                    {"sid": surgeon_id}
                )
                .mappings()      # ← turn each row into a dict-like Mapping
                .all()           # ← fetch all of them
                )
                if rows:
                    surgeon_name = rows[0]["name"]
                # Group records by request_type.
                grouped_by_type = {}
                for row in rows:
                    rtype = row["request_type"]
                    if rtype not in grouped_by_type:
                        grouped_by_type[rtype] = []
                    grouped_by_type[rtype].append(row["date"])
                # Group the dates in each request type into ranges.
                for rtype, date_list in grouped_by_type.items():
                    events[rtype] = group_dates(date_list)
            # Fetch all surgeons for drop-down
            surgeons = get_all_surgeons()
            return render_template('availability.html', events=events, surgeons=surgeons, selected_surgeon_id=surgeon_id, surgeon_name=surgeon_name)

    #############################################
    # Delete Availability Request Endpoint
    #############################################

    @app.route('/delete_availability', methods=['POST'])
    def delete_availability():
        surgeon_id = request.form.get('surgeon_id')
        request_type = request.form.get('request_type')
        start_date = request.form.get('start_date')
        end_date = request.form.get('end_date')
        if not surgeon_id or not request_type or not start_date or not end_date:
            flash("Missing parameters for deletion.")
            return redirect(url_for('availability', surgeon_id=surgeon_id))
        db = get_db()
        db.execute(
            text(
                "DELETE FROM surgeon_availability "
                "WHERE surgeon_id = :sid "
                "  AND request_type = :rtype "
                "  AND date BETWEEN :start AND :end"
            ),
            {"sid": surgeon_id, "rtype": request_type, "start": start_date, "end": end_date}
        )
        db.commit()
        flash("Request deleted successfully.")
        return redirect(url_for('availability', surgeon_id=surgeon_id))

    #############################################
    # Delete Surgeon Endpoint
    #############################################

    @app.route('/surgeons/delete/<int:surgeon_id>', methods=['POST'])
    def delete_surgeon(surgeon_id):
        db = get_db()
        # First, clean up any related availability records:
        db.execute(
            text("DELETE FROM surgeon_availability WHERE surgeon_id = :sid"),
            {"sid": surgeon_id}
        )
        # Then delete the surgeon:
        db.execute(
            text("DELETE FROM surgeons WHERE id = :sid"),
            {"sid": surgeon_id}
        )
        db.commit()
        flash("Surgeon deleted successfully.", "success")
        return redirect(url_for('list_surgeons'))


    #############################################
    # Stats endpoint
    #############################################

    @app.route('/stats', methods=['GET'])
    @basic_auth.required
    def stats():
        import datetime
        # Get the start and end period from query parameters.
        try:
            start_year = int(request.args.get('start_year', datetime.date.today().year))
            start_month = int(request.args.get('start_month', datetime.date.today().month))
            end_year = int(request.args.get('end_year', datetime.date.today().year))
            end_month = int(request.args.get('end_month', datetime.date.today().month))
        except ValueError:
            flash("Invalid date range provided.")
            return redirect(url_for('stats'))
        
        # We assume that each saved_schedule record has integer columns "year" and "month".
        # Query the saved_schedule table for records in the selected range.
        db = get_db()
        # One simple way: only include records where (year, month) is within the selected range.
        # Here we compare first by year, then by month.
        stmt = text("""
            SELECT year, month, schedule_data
            FROM saved_schedule
            WHERE (year > :sy OR (year = :sy AND month >= :sm))
            AND (year < :ey OR (year = :ey AND month <= :em))
            ORDER BY year, month
        """)
        result  = db.execute(stmt, {
            "sy": start_year, "sm": start_month,
            "ey": end_year,   "em": end_month
        })
        records = result.mappings().all()
        
        # Define mapping for call level ranking.
        level_ranks = {
            "1A": 1,
            "1B": 1,
            "2A": 2,
            "2B": 3,
            "3": 4,
            "4": 5
        }
        
        # Initialize dictionary to aggregate stats for each surgeon.
        # We use surgeon names as keys.
        stats_dict = {}
        
        for row in records:
            raw = row["schedule_data"]
            # raw might already be a dict (Postgres JSONB) or a JSON string
            try:
                sched = raw if isinstance(raw, dict) else json.loads(raw)
            except Exception:
                # skip corrupted entries
                continue

            for day_str, assigns in sched.items():
                try:
                    d = datetime.date.fromisoformat(day_str)
                except Exception:
                    continue
                is_weekend = (d.weekday() >= 5)
                for lvl, surgeon in assigns.items():
                    if not surgeon:
                        continue
                    rec = stats_dict.setdefault(surgeon, {
                        "total_calls": 0,
                        "weekend_calls": 0,
                        "min_level_rank": None
                    })
                    rec["total_calls"] += 1
                    if is_weekend:
                        rec["weekend_calls"] += 1
                    rank = level_ranks.get(lvl, 99)
                    if rec["min_level_rank"] is None or rank < rec["min_level_rank"]:
                        rec["min_level_rank"] = rank

        # Turn into a sorted list for the template
        stats_list = sorted(
            [
                {
                    "surgeon": name,
                    "total_calls": data["total_calls"],
                    "weekend_calls": data["weekend_calls"],
                    "min_level_rank": data["min_level_rank"]
                }
                for name, data in stats_dict.items()
            ],
            key=lambda x: (x["min_level_rank"], x["surgeon"].lower())
        )

        return render_template(
            'stats.html',
            stats=stats_list,
            start_year=start_year,
            start_month=start_month,
            end_year=end_year,
            end_month=end_month
        )

    #############################################
    # Start, poll, cancel endpoints
    #############################################

    # --- Route to start a new solve job ---
    @app.route('/start_solve', methods=['POST'])
    def start_solve():
        data = request.get_json()
        year, month = int(data['year']), int(data['month'])

        # Build days list
        days = [
            datetime.date(year, month, d).isoformat()
            for d in range(1, calendar.monthrange(year, month)[1] + 1)
        ]

        # Load previous month’s schedule
        if month == 1:
            prev_year, prev_month = year - 1, 12
        else:
            prev_year, prev_month = year, month - 1

        db = get_db()
        # --- Fetch as a mapping so we can use column names ---
        stmt = text(
            "SELECT schedule_data FROM saved_schedule "
            "WHERE year = :y AND month = :m"
        )
        result = db.execute(stmt, {"y": prev_year, "m": prev_month})
        prev_row = result.mappings().fetchone()
        # Safely parse previous schedule whether it's stored as dict or JSON string
        if prev_row:
            raw_data = prev_row['schedule_data']
            if isinstance(raw_data, dict):
                prev_schedule = raw_data
            else:
                prev_schedule = json.loads(raw_data)
        else:
            prev_schedule = None


        # Compute HK public holidays
        hk_h = holidays.HK(years=[year])
        public_holidays = {
            d.isoformat() for d in hk_h
            if d.year == year and d.month == month
        }
        # ---- Load preassignments for the current month ----
        stmt_pre = text(
            "SELECT preassignment_data FROM preassignments WHERE year = :year AND month = :month"
        )
        result_pre = db.execute(stmt_pre, {"year": year, "month": month})
        row_pre = result_pre.mappings().fetchone()
        if row_pre and row_pre['preassignment_data'] is not None and row_pre['preassignment_data'] != "":
            raw_pre = row_pre['preassignment_data']
            try:
                preassignments = raw_pre if isinstance(raw_pre, dict) else json.loads(raw_pre)
            except Exception as e:
                print("Error decoding preassignments:", e)
                preassignments = {}
        else:
            preassignments = {}

        surgeons = get_all_surgeons()

        job_id = uuid.uuid4().hex
        threading.Thread(
            target=run_solver_job,
            args=(job_id, days, surgeons, prev_schedule, public_holidays, preassignments),
            daemon=True
        ).start()
        
        return jsonify({'job_id': job_id})

    # --- Route to poll status of a job ---
    @app.route('/solve_status/<job_id>')
    def solve_status(job_id):
        job = solve_jobs.get(job_id)
        if not job:
            return jsonify({'status': 'unknown'}), 404
        resp = {
            'status': job['status'],
            'best':   job['best']
        }
        if job['status'] == 'done':
            resp['solution'] = job['solution']
        return jsonify(resp)

    # --- Route to cancel a running job ---
    @app.route('/cancel_solve/<job_id>', methods=['POST'])
    def cancel_solve(job_id):
        job = solve_jobs.get(job_id)
        if job and job['status'] == 'running':
            job['cancel'] = True
            return jsonify({'status': 'cancelling'})
        return jsonify({'status': 'not_found'}), 404



#############################################
# Preassignment
#############################################
    @app.route('/preassignment', methods=['GET', 'POST'])
    @basic_auth.required
    def preassignment():
        import calendar, datetime
        from helper import get_all_surgeons, parse_call_levels
        db = get_db()
        
        if request.method == 'POST':
            # Process form submission
            preassignments = {}
            # Each field is named "preassignment_<day>_<level>"
            for key, value in request.form.items():
                if key.startswith("preassignment_"):
                    parts = key.split("_")
                    # parts[1] = day (ISO date string), parts[2] = level
                    day = parts[1]
                    level = parts[2]
                    if value:
                        preassignments.setdefault(day, {})[level] = int(value)
                    else:
                        preassignments.setdefault(day, {})[level] = None

            # Save to database – if record exists update; otherwise insert.
            year = request.args.get('year', None, type=int)
            month = request.args.get('month', None, type=int)
            if not year or not month:
                today = datetime.date.today()
                year, month = today.year, today.month

            row = db.execute(
                text("SELECT id FROM preassignments WHERE year = :year AND month = :month"),
                {"year": year, "month": month}
            ).fetchone()
            if row:
                row_dict = row._mapping  # Access the row as a mapping object
                db.execute(
                    text("UPDATE preassignments SET preassignment_data = :data, date_updated = now() WHERE id = :id"),
                    {"data": json.dumps(preassignments), "id": row_dict["id"]}
                )
            else:
                db.execute(
                    text("INSERT INTO preassignments (year, month, preassignment_data) VALUES (:year, :month, :data)"),
                    {"year": year, "month": month, "data": json.dumps(preassignments)}
                )
            db.commit()
            flash("Preassignments updated successfully!", "success")
            return redirect(url_for('preassignment', year=year, month=month))
        else:
            # GET: load the desired year/month and build a table.
            year = request.args.get('year', None, type=int)
            month = request.args.get('month', None, type=int)
            if not year or not month:
                today = datetime.date.today()
                year, month = today.year, today.month
            days = [
                datetime.date(year, month, d).isoformat()
                for d in range(1, calendar.monthrange(year, month)[1] + 1)
            ]
            # Build candidate lists for each level similar to scheduler.py logic
            surgeons = get_all_surgeons()
            candidates = {}
            for level in ["1A", "1B", "2A", "2B", "3", "4"]:
                candidate_options = [s for s in surgeons if level in parse_call_levels(s.get("call_levels", ""))]
                candidate_options.sort(key=lambda s: s["name"])
                candidates[level] = candidate_options
            # Load any existing preassignments from the database
            row = db.execute(
                text("SELECT preassignment_data FROM preassignments WHERE year = :year AND month = :month"),
                {"year": year, "month": month}
            ).fetchone()
            if row:
                row_dict = row._mapping
                preassignments = row_dict["preassignment_data"] if isinstance(row_dict["preassignment_data"], dict) else json.loads(row_dict["preassignment_data"])
            else:
                preassignments = {}
            return render_template(
                "preassignment.html",
                year=year,
                month=month,
                days=days,
                levels=["1A", "1B", "2A", "2B", "3", "4"],
                candidates=candidates,
                preassignments=preassignments
            )
        

###############################################
# End of App
################################################

    return app

#############################################
# Run the App
#############################################
if __name__=="__main__":
    create_app().run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)), debug=True)