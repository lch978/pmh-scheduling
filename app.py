import random
import math
import datetime
import calendar
import sqlite3
import json
import holidays
import threading, uuid
from dateutil.parser import parse
import os

from helper import *
from scheduler import solve_schedule_or_tools
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, g, session

app = Flask(__name__)
app.secret_key = "your_secure_key"  # Replace with your secure key

solve_jobs = {}

@app.context_processor
def utility_processor():
    return {
        'parse_call_levels': parse_call_levels
    }

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()
        


#############################################
# Surgeon Management Endpoints
#############################################

@app.route('/surgeons')
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

@app.route('/surgeons/add', methods=['GET', 'POST'])
def add_surgeon():
    if request.method == 'POST':
        name = request.form['name']
        call_levels_list = request.form.getlist('call_levels')
        call_levels = ','.join(call_levels_list)
        db = get_db()
        db.execute("INSERT INTO surgeons (name, call_levels) VALUES (?, ?)", (name, call_levels))
        db.commit()
        flash("Surgeon added successfully!")
        return redirect(url_for('list_surgeons'))
    return render_template('surgeon_form.html', surgeon={}, action="Add")

@app.route('/surgeons/edit/<int:surgeon_id>', methods=['GET', 'POST'])
def edit_surgeon(surgeon_id):
    db = get_db()
    row = db.execute("SELECT * FROM surgeons WHERE id = ?", (surgeon_id,)).fetchone()
    if not row:
        flash("Surgeon not found!")
        return redirect(url_for('list_surgeons'))
    surgeon = dict(row)
    if request.method == 'POST':
        name = request.form['name']
        call_levels_list = request.form.getlist('call_levels')
        call_levels = ','.join(call_levels_list)
        db.execute("UPDATE surgeons SET name = ?, call_levels = ? WHERE id = ?", (name, call_levels, surgeon_id))
        db.commit()
        flash("Surgeon updated successfully!")
        return redirect(url_for('list_surgeons'))
    return render_template('surgeon_form.html', surgeon=surgeon, action="Edit")

@app.route('/surgeons/update/<int:surgeon_id>', methods=['POST'])
def update_surgeon_inline(surgeon_id):
    name = request.form['name']
    call_levels_list = request.form.getlist('call_levels')
    call_levels = ','.join(call_levels_list)
    db = get_db()
    db.execute("UPDATE surgeons SET name = ?, call_levels = ? WHERE id = ?", (name, call_levels, surgeon_id))
    db.commit()
    flash("Surgeon updated successfully!")
    return redirect(url_for('list_surgeons'))

@app.route('/update_all_surgeons', methods=['POST'])
def update_all_surgeons():
    db = get_db()
    surgeons = get_all_surgeons()
    for surgeon in surgeons:
        sid = surgeon['id']
        name_field = f"name_{sid}"
        levels_field = f"call_levels_{sid}"
        nlth_field = f"nlth_{sid}"

        new_name   = request.form.get(name_field, surgeon['name'])
        new_levels = request.form.getlist(levels_field)
        new_levels_str = ",".join(new_levels)

        # Checkbox: if the field is present in form data, checkbox was checked
        new_nlth = 1 if request.form.get(nlth_field) == 'on' else 0

        db.execute(
            "UPDATE surgeons SET name = ?, call_levels = ?, nlth = ? WHERE id = ?",
            (new_name, new_levels_str, new_nlth, sid)
        )
    db.commit()
    flash("Surgeon updates applied successfully!")
    return redirect(url_for('list_surgeons'))

#############################################
# Maximum Calls Configuration Endpoint
#############################################

@app.route('/config_max_calls', methods=['GET', 'POST'])
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
def global_config_page():
    if request.method == 'POST':
        no_call_hard_val = request.form.get("no_call_hard", "1")
        update_global_config({
            "no_call_hard": no_call_hard_val,
            "fairness_weight": request.form.get("fairness_weight", "1000"),
            "gamma_no_call": request.form.get("gamma_no_call", "10"),
            "gamma_unavail_prev": request.form.get("gamma_unavail_prev", "5"),
            "gamma_1B": request.form.get("gamma_1B", "1"),
            "gamma_spacing": request.form.get("gamma_spacing", "10"),
            "spacing_threshold": request.form.get("spacing_threshold", "7")
        })
        flash("Global configuration updated successfully!")
        return redirect(url_for('global_config_page'))
    else:
        config = get_global_config()
        return render_template('global_config.html', config=config)

          
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

        # Update global configuration.
        update_global_config({
            "fairness_weight": fairness_weight,
            "gamma_no_call": gamma_no_call,
            "gamma_unavail_prev": gamma_unavail_prev,
            "gamma_1B": gamma_1B,
            "gamma_spacing": gamma_spacing,
            "spacing_threshold": spacing_threshold
        })
        flash("Constraint weights updated successfully!")
        return redirect(url_for('constraint_weights'))
    else:
        config = get_global_config()
        return render_template('constraint_weights.html', config=config)


#############################################
# Schedule Generation and Saving Endpoints
#############################################
def run_solver_job(job_id, days, surgeons, prev_schedule, public_holidays):
    from scheduler import solve_schedule_or_tools
    with app.app_context():
        solve_jobs[job_id] = {'status':'running','best':None,'cancel':False,'solution':None}
        # call your solver end‑to‑end
        sched, cost = solve_schedule_or_tools(days, surgeons, prev_schedule, public_holidays)
        if sched:
            solve_jobs[job_id].update({'status':'done','solution':sched,'best':cost})
        else:
            solve_jobs[job_id]['status'] = 'failed'

@app.route('/new_schedule', methods=['GET'])
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
    prev_row = db.execute(
        "SELECT schedule_data FROM saved_schedule WHERE year=? AND month=?",
        (prev_year, prev_month)
    ).fetchone()
    prev_schedule = (
        json.loads(prev_row['schedule_data'])
        if prev_row else None
    )

    # ── 6) Do we already have one in the DB? ──
    row = db.execute(
        "SELECT * FROM saved_schedule WHERE year=? AND month=?",
        (year_sel, month_sel)
    ).fetchone()

    generate_flag = request.args.get('generate')
    if generate_flag:
        # ▶︎ Preview only; do *not* save
        surgeons = get_all_surgeons()
        sched, cost = solve_schedule_or_tools(
            days_sel,
            surgeons,
            prev_schedule=prev_schedule,
            public_holidays=public_holidays
        )
        if sched is None:
            flash("No feasible schedule found.", "error")
            return render_template('no_schedule.html')
    else:
        # ▶︎ No preview request → show the saved one (if any)
        cost = None
        sched = (
            json.loads(row['schedule_data'])
            if row else None
        )

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
    year, month = int(request.form['year']), int(request.form['month'])
    data = request.form.get('schedule_json')
    if not data:
        flash("No schedule data provided to save.", "error")
        return redirect(url_for('new_schedule', year=year, month=month))

    db = get_db()
    exists = db.execute(
        "SELECT 1 FROM saved_schedule WHERE year=? AND month=?",
        (year, month)
    ).fetchone()
    if exists:
        db.execute(
            "UPDATE saved_schedule SET schedule_data=?, date_saved=datetime('now') "
            "WHERE year=? AND month=?",
            (data, year, month)
        )
    else:
        db.execute(
            "INSERT INTO saved_schedule (year, month, schedule_data, date_saved) "
            "VALUES (?, ?, ?, datetime('now'))",
            (year, month, data)
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
    row = db.execute("SELECT * FROM saved_schedule WHERE year = ? AND month = ?", 
                     (year_sel, month_sel)).fetchone()
    if row:
        sched = json.loads(row['schedule_data'])
    else:
        flash("No saved schedule found for the selected period.")
        sched = {}
    # Use the locally computed days_sel to determine weekend_set.
    weekend_set = {d for d in days_sel if datetime.date.fromisoformat(d).weekday() >= 5}
    return render_template('saved_schedule.html', schedule=sched, weekend_set=weekend_set,
                           year=year_sel, month=month_sel)

@app.route('/')
def index():
    year_sel, month_sel = get_year_month()
    db = get_db()
    row = db.execute("SELECT * FROM saved_schedule WHERE year = ? AND month = ?", (year_sel, month_sel)).fetchone()
    if row:
        return redirect(url_for('saved_schedule', year=year_sel, month=month_sel))
    else:
        return redirect(url_for('new_schedule', year=year_sel, month=month_sel))

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
                    db.execute("INSERT INTO surgeon_availability (surgeon_id, request_type, date) VALUES (?, ?, ?)",
                               (surgeon_id, request_type, current_dt.isoformat()))
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
            rows = db.execute(
                "SELECT sa.date, sa.request_type, s.name, s.call_levels FROM surgeon_availability sa JOIN surgeons s ON sa.surgeon_id = s.id WHERE s.id = ? ORDER BY sa.date",
                (surgeon_id,)
            ).fetchall()
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
    db.execute("DELETE FROM surgeon_availability WHERE surgeon_id = ? AND request_type = ? AND date BETWEEN ? AND ?",
               (surgeon_id, request_type, start_date, end_date))
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
    db.execute("DELETE FROM surgeon_availability WHERE surgeon_id = ?", (surgeon_id,))
    # Then delete the surgeon:
    db.execute("DELETE FROM surgeons WHERE id = ?", (surgeon_id,))
    db.commit()
    flash("Surgeon deleted successfully.", "success")
    return redirect(url_for('list_surgeons'))


#############################################
# Stats endpoint
#############################################

@app.route('/stats', methods=['GET'])
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
    records = db.execute(
        """
        SELECT * FROM saved_schedule 
        WHERE 
          (year > ? OR (year = ? AND month >= ?))
          AND
          (year < ? OR (year = ? AND month <= ?))
        ORDER BY year, month
        """, 
        (start_year, start_year, start_month, end_year, end_year, end_month)
    ).fetchall()
    
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
    
    # Process each saved schedule record.
    for record in records:
        schedule_data = json.loads(record["schedule_data"])
        # schedule_data is assumed to be a dictionary mapping day string to assignments.
        for day, assignments in schedule_data.items():
            try:
                day_obj = datetime.date.fromisoformat(day)
            except Exception:
                continue
            # Determine if the day is a weekend.
            is_weekend = day_obj.weekday() >= 5
            for level, surgeon in assignments.items():
                if surgeon and surgeon.strip() != "":
                    if surgeon not in stats_dict:
                        stats_dict[surgeon] = {
                            "total_calls": 0,
                            "weekend_calls": 0,
                            "min_level_rank": None
                        }
                    stats_dict[surgeon]["total_calls"] += 1
                    if is_weekend:
                        stats_dict[surgeon]["weekend_calls"] += 1
                    # Determine rank for this level.
                    rank = level_ranks.get(level, 99)
                    if stats_dict[surgeon]["min_level_rank"] is None or rank < stats_dict[surgeon]["min_level_rank"]:
                        stats_dict[surgeon]["min_level_rank"] = rank
    
    # Convert dictionary to list and sort by min_level_rank (lowest first), then by surgeon name.
    stats_list = []
    for surgeon, data in stats_dict.items():
        stats_list.append({
            "surgeon": surgeon,
            "total_calls": data["total_calls"],
            "weekend_calls": data["weekend_calls"],
            "min_level_rank": data["min_level_rank"]
        })
    stats_list.sort(key=lambda x: (x["min_level_rank"], x["surgeon"].lower()))
    
    return render_template('stats.html', stats=stats_list,
                           start_year=start_year, start_month=start_month,
                           end_year=end_year, end_month=end_month)

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
    prev_row = db.execute(
        "SELECT schedule_data FROM saved_schedule WHERE year=? AND month=?",
        (prev_year, prev_month)
    ).fetchone()
    prev_schedule = json.loads(prev_row['schedule_data']) if prev_row else None

    # Compute HK public holidays
    hk_h = holidays.HK(years=[year])
    public_holidays = {
        d.isoformat() for d in hk_h
        if d.year == year and d.month == month
    }

    surgeons = get_all_surgeons()

    job_id = uuid.uuid4().hex
    threading.Thread(
        target=run_solver_job,
        args=(job_id, days, surgeons, prev_schedule, public_holidays),
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
# Run the App
#############################################
if __name__ == '__main__':
    # On Windows, disable the threaded server to avoid socket.fromfd
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True, use_reloader=False)


