import os
from datetime import datetime, date
from flask import Blueprint, request, jsonify, session
from werkzeug.utils import secure_filename
from config import Config
from utils.helpers import is_duplicate, filter_by_confidence, allowed_file

api_bp = Blueprint("api", __name__, url_prefix="/api")

# Lazy load supabase from app to avoid circular import issues
def get_supabase():
    from app import supabase
    return supabase

# -----------------------------------------------------------------------------
# GET /api/potholes
# -----------------------------------------------------------------------------
@api_bp.route("/potholes", methods=["GET"])
def get_potholes():
    severity   = request.args.get("severity")
    status     = request.args.get("status")
    date_from  = request.args.get("date_from")
    date_to    = request.args.get("date_to")
    confidence = request.args.get("confidence", type=float)
    limit      = request.args.get("limit",  type=int)
    sort_dir   = request.args.get("sort",   "desc").lower()
    ptype      = request.args.get("type")

    supabase = get_supabase()
    if not supabase:
        print("[!] Warning: API called but Supabase not connected.")
        return jsonify([]) # Return empty list instead of 500 for better UX

    try:
        # Fetch app settings confidence threshold as default if not passed
        conf_thresh = confidence if confidence is not None else Config.DEFAULT_CONFIDENCE_THRESHOLD
        try:
            conn = get_sqlite()
            cur = conn.cursor()
            cur.execute("SELECT detection_sensitivity FROM app_settings WHERE id = 1")
            row = cur.fetchone()
            conn.close()
            if row:
                conf_thresh = confidence if confidence is not None else float(row["detection_sensitivity"] or Config.DEFAULT_CONFIDENCE_THRESHOLD)
        except:
            pass

        # 1. Fetch AI Potholes
        p_query = supabase.table("potholes").select("*").eq("pothole", True)
        if severity: p_query = p_query.ilike("severity", severity)
        if date_from: p_query = p_query.gte("created_at", f"{date_from}T00:00:00")
        if date_to: p_query = p_query.lte("created_at", f"{date_to}T23:59:59")
        if ptype: p_query = p_query.ilike("type", ptype)
        
        p_res = p_query.order("created_at", desc=(sort_dir == "desc")).limit(500).execute()
        ai_potholes = p_res.data or []

        # 2. Fetch Approved User Reports
        u_query = supabase.table("user_reports").select("*").eq("status", "approved")
        if date_from: u_query = u_query.gte("created_at", f"{date_from}T00:00:00")
        if date_to: u_query = u_query.lte("created_at", f"{date_to}T23:59:59")
        
        u_res = u_query.order("created_at", desc=(sort_dir == "desc")).limit(200).execute()
        user_rows = u_res.data or []

        # Map user reports to pothole format
        for ur in user_rows:
            ur["image_url"] = ur.get("media_url")
            ur["severity"]  = "medium"
            ur["type"]      = "Citizen Report"
            ur["source"]    = "Citizen"

        # Combine
        rows = ai_potholes + user_rows
        print(f"[+] Unified fetch: {len(ai_potholes)} AI + {len(user_rows)} Citizen reports.")

        # Confidence filter
        rows = filter_by_confidence(rows, conf_thresh)
        
        # Merge with local assignments to get Status
        try:
            conn = get_sqlite()
            cur = conn.cursor()
            cur.execute("SELECT pothole_id, status FROM assignments")
            assigns = {r["pothole_id"]: r["status"] for r in cur.fetchall()}
            conn.close()
        except:
            assigns = {}

        dedupe = request.args.get("dedupe", "false").lower() == "true"
        
        if dedupe:
            # --- GROUPING/MERGING NEARBY DUPLICATES (25m threshold) ---
            from utils.helpers import haversine
            grouped_rows = []
            
            # Sort by creation date (newest first) so we keep the latest image/details
            rows.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            
            for r in rows:
                try:
                    lat, lon = float(r["latitude"]), float(r["longitude"])
                except (TypeError, ValueError):
                    continue
                    
                img_url = r.get("image_url")
                found_group = False
                
                for g in grouped_rows:
                    # Check 1: Identical Image URL (Highest confidence of duplicate)
                    if img_url and g.get("image_url") == img_url:
                        found_group = True
                    
                    # Check 2: Proximity (25 meters threshold)
                    else:
                        dist = haversine(lat, lon, float(g["latitude"]), float(g["longitude"]))
                        if dist < 25.0: 
                            found_group = True
                    
                    if found_group:
                        # Merge logic:
                        # 1. Keep highest severity
                        sev_map = {"high": 3, "medium": 2, "low": 1, "unknown": 0}
                        current_sev = sev_map.get((g.get("severity") or "low").lower(), 0)
                        new_sev     = sev_map.get((r.get("severity") or "low").lower(), 0)
                        if new_sev > current_sev:
                            g["severity"] = r["severity"]
                        
                        # 2. Accumulate report count
                        g["report_count"] = (g.get("report_count") or 1) + (r.get("report_count") or 1)
                        
                        # 3. Update last_reported_at if newer
                        r_ts = r.get("created_at")
                        if r_ts and r_ts > g.get("created_at", ""):
                            g["created_at"] = r_ts # Keep newest timestamp
                        
                        break
                
                if not found_group:
                    # Add as new group representative
                    if "report_count" not in r: r["report_count"] = 1
                    grouped_rows.append(r)
            
            processed_rows = grouped_rows
        else:
            # No deduplication, return raw rows
            processed_rows = rows

        final_rows = []
        for r in processed_rows:

            r_status = assigns.get(r["id"], "Pending")
            r["status"] = r_status
            
            # Apply python-side status filter
            if status and r_status.lower() != status.lower():
                continue

            # Normalize image URLs
            url = r.get("image_url")
            if url and not url.startswith("http") and not url.startswith("/static"):
                r["image_url"] = f"{Config.SUPABASE_URL}/storage/v1/object/public/potholes/{url}"
            
            final_rows.append(r)

        if limit:
            final_rows = final_rows[:limit]

        return jsonify(final_rows)
    except Exception as e:
        print(f"[!] Error in get_potholes: {e}")
        return jsonify([])

# -----------------------------------------------------------------------------
# POST /api/pothole  — add new record
# -----------------------------------------------------------------------------
@api_bp.route("/pothole", methods=["POST"])
def add_pothole():
    supabase = get_supabase()
    if not supabase:
        return jsonify({"error": "Supabase not connected"}), 500

    data      = request.form
    latitude  = float(data.get("latitude",  0))
    longitude = float(data.get("longitude", 0))
    severity  = data.get("severity",  "Low")
    ptype     = data.get("type",      "pothole")
    confidence= float(data.get("confidence", 0.9))
    pothole   = True

    # 1. Duplicate check with logic to update instead of insert
    existing_p = is_duplicate(supabase, latitude, longitude)
    
    if existing_p:
        # Update existing pothole
        pid = existing_p["id"]
        supabase.table("potholes").update({
            "confidence": confidence,
            "report_count": (existing_p.get("report_count") or 1) + 1,
            "last_reported_at": datetime.now().isoformat()
        }).eq("id", pid).execute()
        
        return jsonify({"id": pid, "message": "Duplicate detected. Updated existing record."}), 200

    # 2. Image upload
    image_url = ""
    if "image" in request.files:
        f = request.files["image"]
        if f and allowed_file(f.filename, Config.ALLOWED_EXTENSIONS):
            filename  = secure_filename(f.filename)
            save_path = os.path.join(Config.UPLOAD_FOLDER, filename)
            f.save(save_path)
            image_url = f"/static/uploads/{filename}"

    # 3. Insert new pothole
    try:
        res = supabase.table("potholes").insert({
            "latitude": latitude,
            "longitude": longitude,
            "severity": severity,
            "type": ptype,
            "confidence": confidence,
            "pothole": pothole,
            "image_url": image_url,
            "report_count": 1,
            "last_reported_at": datetime.now().isoformat()
        }).execute()

        if not res.data:
            return jsonify({"error": "Failed to insert record"}), 500

        print(f"[+] Created new pothole record #{new_id}")
        return jsonify({"id": new_id, "message": "Pothole added"}), 201
    except Exception as e:
        print(f"[!] Insert Pothole Error: {e}")
        return jsonify({"error": str(e)}), 500

# -----------------------------------------------------------------------------
# PUT /api/pothole/<id>  — update status / severity
# -----------------------------------------------------------------------------
@api_bp.route("/pothole/<int:pid>", methods=["PUT"])
def update_pothole(pid):
    supabase = get_supabase()
    data = request.get_json(silent=True) or {}
    update_data = {}

    # 1. Update Severity in Supabase
    if "severity" in data:
        update_data["severity"] = data["severity"]
        supabase.table("potholes").update(update_data).eq("id", pid).execute()

    # 2. Update Status in SQLite
    if "status" in data:
        new_status = data["status"]
        # Normalize "Completed" to "Fixed" (User prefers Fixed)
        if new_status == "Completed":
            new_status = "Fixed"
            
        conn = get_sqlite()
        cur = conn.cursor()
        # Check if assignment exists
        cur.execute("SELECT id FROM assignments WHERE pothole_id = ?", (pid,))
        row = cur.fetchone()
        if row:
            cur.execute("UPDATE assignments SET status = ? WHERE pothole_id = ?", (new_status, pid))
        else:
            # Create a "System" assignment if it's being marked without a worker
            cur.execute("INSERT INTO assignments (pothole_id, worker_name, status, notes) VALUES (?, ?, ?, ?)",
                        (pid, "System", new_status, "Status updated via dashboard"))
        conn.commit()
        conn.close()

    if not "severity" in data and not "status" in data:
        return jsonify({"error": "No fields to update"}), 400

    return jsonify({"message": "Pothole updated successfullly"})

# -----------------------------------------------------------------------------
# DELETE /api/pothole/<id>
# -----------------------------------------------------------------------------
@api_bp.route("/pothole/<int:pid>", methods=["DELETE"])
def delete_pothole(pid):
    supabase = get_supabase()
    supabase.table("potholes").delete().eq("id", pid).execute()
    return jsonify({"message": "Deleted"})

# -----------------------------------------------------------------------------
# GET /api/stats  — summary counts
# -----------------------------------------------------------------------------
# Simple In-Memory Cache for Stats
_stats_cache = {"data": None, "time": None}

@api_bp.route("/stats", methods=["GET"])
def get_stats():
    global _stats_cache
    now_ts = datetime.now()
    if _stats_cache["data"] and _stats_cache["time"] and (now_ts - _stats_cache["time"]).total_seconds() < 30:
        return jsonify(_stats_cache["data"])

    supabase = get_supabase()
    if not supabase: return jsonify({"total":0, "today":0, "fixed":0, "pending":0})
    
    try:
        # Fetch with limit to prevent performance degradation on large datasets
        p_res = supabase.table("potholes").select("id, latitude, longitude, created_at").eq("pothole", True).order("created_at", desc=True).limit(1000).execute()
        u_res = supabase.table("user_reports").select("id, latitude, longitude, created_at").eq("status", "approved").order("created_at", desc=True).limit(500).execute()
        
        all_rows = (p_res.data or []) + (u_res.data or [])

        # 2. Group/Deduplicate everything to get accurate UNIQUE count (50m)
        # Sort by lat first to allow early-break optimization (future-proof)
        all_rows.sort(key=lambda x: float(x.get("latitude", 0)))
        
        unique_potholes = []
        for r in all_rows:
            try:
                lat, lon = float(r["latitude"]), float(r["longitude"])
            except: continue
            
            found = False
            for g in unique_potholes:
                # Basic optimization: if lat difference is already > 50m (~0.0005 deg), skip
                if abs(lat - float(g["latitude"])) > 0.001: continue
                
                from utils.helpers import haversine
                if haversine(lat, lon, float(g["latitude"]), float(g["longitude"])) < 50.0:
                    found = True
                    break
            if not found:
                unique_potholes.append(r)
        
        total = len(unique_potholes)

        # 3. Today's detections (grouped)
        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        today_unique = []
        for r in unique_potholes:
            if r.get("created_at", "").startswith(today_str):
                today_unique.append(r)
        today = len(today_unique)
        
        # 4. Status derived from assignments
        fixed, pending, inprog = 0, 0, 0
        try:
            conn = get_sqlite()
            cur = conn.cursor()
            cur.execute("SELECT pothole_id, status FROM assignments")
            assigns = {row[0]: row[1] for row in cur.fetchall()}
            conn.close()
            
            # Count based on unique group representatives
            for p in unique_potholes:
                s = assigns.get(p["id"], "Pending")
                if s in ("Completed", "Fixed"): fixed += 1
                elif s == "In Progress": inprog += 1
                else: pending += 1
        except:
            # Fallback if SQLite fails
            pending = total

        stats_data = {
            "total": total, 
            "today": today, 
            "fixed": fixed,
            "pending": pending, 
            "in_progress": inprog
        }
        _stats_cache = {"data": stats_data, "time": now_ts}
        return jsonify(stats_data)
    except Exception as e:
        print(f"[!] Stats API Error: {e}")
        return jsonify({"total":0, "today":0, "fixed":0, "pending":0, "error": str(e)})


# -----------------------------------------------------------------------------
# GET /api/analytics
# -----------------------------------------------------------------------------
@api_bp.route("/analytics", methods=["GET"])
def analytics():
    supabase = get_supabase()
    if not supabase: return jsonify({"timeseries":[], "severity_dist":[], "type_dist":[], "top_areas":[]})
    
    try:
        period = request.args.get("period", "daily")   # daily | weekly | monthly
        target_conf = request.args.get("confidence", type=float, default=0.0)

        import datetime
        now = datetime.datetime.utcnow()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        
        # Define time threshold based on period
        if period == "weekly":
            start_date = today_start - datetime.timedelta(days=7)
        elif period == "monthly":
            start_date = today_start - datetime.timedelta(days=30)
        else:
            # daily section takes current day data
            start_date = today_start
        
        # 1. Fetch AI Potholes
        p_res = supabase.table("potholes").select("id, latitude, longitude, severity, created_at, type").eq("pothole", True).gte("confidence", target_conf).gte("created_at", start_date.strftime("%Y-%m-%dT%H:%M:%S")).execute()
        p_rows = p_res.data or []

        # 2. Fetch Approved User Reports
        u_res = supabase.table("user_reports").select("id, latitude, longitude, created_at, description").eq("status", "approved").gte("created_at", start_date.strftime("%Y-%m-%dT%H:%M:%S")).execute()
        u_rows = u_res.data or []

        # Map user reports
        for ur in u_rows:
            ur["severity"] = "Medium"
            ur["type"]     = "Citizen Report"

        rows = p_rows + u_rows

        # Python-side processing for Supabase
        from collections import defaultdict

        # Time series
        ts_dict = defaultdict(int)
        sev_dict = defaultdict(int)
        type_dict = defaultdict(int)
        area_dict = defaultdict(int)

        for r in rows:
            # Time series parsing - always group by day to make it "work like daily"
            dt = r.get("created_at", "")
            if dt:
                dt_obj = datetime.datetime.fromisoformat(dt.replace("Z", "+00:00") if "Z" in dt else dt)
                label = dt_obj.strftime("%Y-%m-%d")
                ts_dict[label] += 1
            
            # Severity
            sev = r.get("severity", "Unknown") or "Unknown"
            sev_dict[sev] += 1

            # Type
            pt = r.get("type", "Unknown") or "Unknown"
            type_dict[pt] += 1

            # Area
            lat = round(float(r.get("latitude", 0)), 3)
            lon = round(float(r.get("longitude", 0)), 3)
            area_dict[f"{lat},{lon}"] += 1

        timeseries = [{"label": k, "count": v} for k, v in sorted(ts_dict.items())][-30:]
        severity_dist = [{"severity": k, "count": v} for k, v in sev_dict.items()]
        type_dist = [{"type": k, "count": v} for k, v in type_dict.items()]
        
        top_areas_raw = sorted(area_dict.items(), key=lambda x: x[1], reverse=True)[:10]
        top_areas = []
        for coord, c in top_areas_raw:
            lat, lon = coord.split(",")
            top_areas.append({"lat": float(lat), "lon": float(lon), "count": c})

        return jsonify({
            "timeseries": timeseries,
            "severity_dist": severity_dist,
            "type_dist": type_dist,
            "top_areas": [ta for ta in top_areas if isinstance(ta, dict)]
        })
    except Exception as e:
        print(f"[!] Analytics API Error: {e}")
        return jsonify({"timeseries":[], "severity_dist":[], "type_dist":[], "top_areas":[], "error": str(e)})

import sqlite3

def get_sqlite():
    db_path = "local_db.sqlite"
    # Ensure database file exists in production (it won't persist on free Render tier)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # Check if table exists, if not create it (this is a safeguard)
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS assignments (id INTEGER PRIMARY KEY AUTOINCREMENT, pothole_id INTEGER, worker_name TEXT, status TEXT, notes TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        cur.execute("CREATE TABLE IF NOT EXISTS app_settings (id INTEGER PRIMARY KEY, detection_sensitivity FLOAT, alert_threshold INTEGER, map_lat FLOAT, map_lon FLOAT, map_zoom INTEGER, auto_refresh_sec INTEGER)")
        conn.commit()
    except Exception as e:
        print(f"[!] SQLite Init Warning: {e}")
    return conn

# -----------------------------------------------------------------------------
# Assignments
# -----------------------------------------------------------------------------
@api_bp.route("/assignments", methods=["GET"])
def list_assignments():
    try:
        # We still need to join with Supabase potholes to get lat/lon for the frontend
        supabase = get_supabase()
        potholes_res = supabase.table("potholes").select("id, latitude, longitude, severity").execute()
        p_dict = {p.get("id"): p for p in potholes_res.data} if potholes_res.data else {}

        conn = get_sqlite()
        cur = conn.cursor()
        cur.execute("SELECT * FROM assignments")
        rows = [dict(row) for row in cur.fetchall()]
        conn.close()

        for r in rows:
            pid = r.get("pothole_id")
            p_info = p_dict.get(pid, {})
            r["latitude"] = p_info.get("latitude")
            r["longitude"] = p_info.get("longitude")
            r["severity"] = p_info.get("severity")
            r["pothole_status"] = r.get("status")
        
        return jsonify(rows)
    except Exception as e:
        print("Assignments table error:", e)
        return jsonify([])

@api_bp.route("/assignments", methods=["POST"])
def create_assignment():
    data = request.get_json(silent=True) or {}
    pothole_id  = data.get("pothole_id")
    worker_name = data.get("worker_name", "").strip()
    notes       = data.get("notes", "")
    if not pothole_id or not worker_name:
        return jsonify({"error": "pothole_id and worker_name required"}), 400

    conn = get_sqlite()
    cur = conn.cursor()
    cur.execute("INSERT INTO assignments (pothole_id, worker_name, notes, status) VALUES (?, ?, ?, 'Pending')",
                (pothole_id, worker_name, notes))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()

    return jsonify({"id": new_id, "message": "Assigned"}), 201

@api_bp.route("/assignments/<int:aid>", methods=["PUT"])
def update_assignment(aid):
    data   = request.get_json(silent=True) or {}
    status = data.get("status")
    if not status:
        return jsonify({"error": "status required"}), 400

    conn = get_sqlite()
    cur = conn.cursor()
    cur.execute("UPDATE assignments SET status = ? WHERE id = ?", (status, aid))
    conn.commit()
    conn.close()

    return jsonify({"message": "Updated"})

@api_bp.route("/assignments/<int:aid>", methods=["DELETE"])
def delete_assignment(aid):
    conn = get_sqlite()
    cur = conn.cursor()
    cur.execute("DELETE FROM assignments WHERE id = ?", (aid,))
    conn.commit()
    conn.close()
    return jsonify({"message": "Deleted"})

# -----------------------------------------------------------------------------
# Settings
# -----------------------------------------------------------------------------
@api_bp.route("/settings", methods=["GET"])
def get_settings():
    try:
        conn = get_sqlite()
        cur = conn.cursor()
        cur.execute("SELECT * FROM app_settings WHERE id = 1")
        row = cur.fetchone()
        conn.close()
        return jsonify(dict(row) if row else {})
    except:
        return jsonify({})

@api_bp.route("/settings", methods=["POST"])
def save_settings():
    data = request.get_json(silent=True) or {}
    allowed_fields = ["detection_sensitivity", "alert_threshold", "map_center_lat",
                "map_center_lon", "map_zoom", "auto_refresh_seconds",
                "notification_sound"]
    update_data = {k: v for k, v in data.items() if k in allowed_fields}
    
    if not update_data:
        return jsonify({"error": "No fields provided"}), 400

    conn = get_sqlite()
    cur = conn.cursor()
    
    cur.execute("SELECT id FROM app_settings WHERE id = 1")
    row = cur.fetchone()
    
    if row:
        set_clause = ", ".join([f"{k} = ?" for k in update_data.keys()])
        values = list(update_data.values()) + [1]
        cur.execute(f"UPDATE app_settings SET {set_clause} WHERE id = ?", values)
    else:
        update_data["id"] = 1
        cols = ", ".join(update_data.keys())
        qmarks = ", ".join(["?"] * len(update_data))
        cur.execute(f"INSERT INTO app_settings ({cols}) VALUES ({qmarks})", list(update_data.values()))
        
    conn.commit()
    conn.close()
        
    return jsonify({"message": "Settings saved"})

# -----------------------------------------------------------------------------
# Auth & User Management
# -----------------------------------------------------------------------------
@api_bp.route("/register", methods=["POST"])
def register():
    from app import bcrypt
    supabase = get_supabase()
    data = request.get_json(silent=True) or {}
    name     = data.get("name", "").strip()
    email    = data.get("email", "").strip()
    password = data.get("password", "").strip()
    # SECURITY: Ignore incoming role, always force "user" for public register
    role     = "user"

    if not name or not email or not password:
        return jsonify({"error": "All fields are required"}), 400

    # Check duplicate
    try:
        existing = supabase.table("users").select("id").eq("email", email).execute()
        if existing.data:
            return jsonify({"error": "Email already exists"}), 409
    except Exception as e:
        if "PGRST205" in str(e):
            return jsonify({"error": "Table 'users' missing in Supabase. Please run the SQL setup script."}), 404
        pass

    pw_hash = bcrypt.generate_password_hash(password).decode("utf-8")
    
    try:
        res = supabase.table("users").insert({
            "name": name,
            "email": email,
            "password": pw_hash,
            "role": role
        }).execute()
        return jsonify({"message": "Registration successful", "user": res.data[0]}), 201
    except Exception as e:
        err_msg = str(e)
        if "PGRST205" in err_msg or "does not exist" in err_msg.lower():
            return jsonify({"error": "Table 'users' missing in Supabase. Please run the SQL setup script."}), 404
        return jsonify({"error": f"Registration failed: {err_msg}"}), 500

@api_bp.route("/create-admin", methods=["POST"])
def create_admin():
    """Secure endpoint only for admins to create other admins"""
    if session.get("role") != "admin":
        return jsonify({"error": "Unauthorized. Only admins can create admin accounts."}), 403

    from app import bcrypt
    supabase = get_supabase()
    data = request.get_json(silent=True) or {}
    name     = data.get("name", "").strip()
    email    = data.get("email", "").strip()
    password = data.get("password", "").strip()
    role     = data.get("role", "admin").lower() # Defaults to admin

    if not name or not email or not password:
        return jsonify({"error": "All fields are required"}), 400

    # Strict role validation
    if role not in ["admin", "user"]:
        return jsonify({"error": "Invalid role"}), 400

    # Check duplicate
    try:
        existing = supabase.table("users").select("id").eq("email", email).execute()
        if existing.data:
            return jsonify({"error": "Email already exists"}), 409
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500

    pw_hash = bcrypt.generate_password_hash(password).decode("utf-8")
    
    try:
        res = supabase.table("users").insert({
            "name": name,
            "email": email,
            "password": pw_hash,
            "role": role
        }).execute()
        return jsonify({"message": f"{role.capitalize()} created successfully", "user": res.data[0]}), 201
    except Exception as e:
        return jsonify({"error": f"Account creation failed: {str(e)}"}), 500

@api_bp.route("/login", methods=["POST"])
def login():
    from app import bcrypt
    supabase = get_supabase()
    data     = request.get_json(silent=True) or {}
    email    = (data.get("email") or "").strip()
    password = (data.get("password") or "").strip()

    if not email or not password:
        return jsonify({"error": "Email and password required"}), 400

    try:
        res = supabase.table("users").select("*").eq("email", email).execute()
        user = res.data[0] if res.data else None

        if user and bcrypt.check_password_hash(user.get("password", ""), password):
            session["user_id"] = user["id"]
            session["role"]    = user["role"]
            session["name"]    = user["name"]
            return jsonify({"message": "OK", "role": user["role"], "name": user["name"]})
    except Exception as e:
        err_msg = str(e)
        if "PGRST205" in err_msg or "does not exist" in err_msg.lower():
            return jsonify({"error": "Table 'users' missing in Supabase. Please run the SQL setup script."}), 404
        return jsonify({"error": f"Database error: {err_msg}"}), 500
    
    return jsonify({"error": "Invalid email or password"}), 401

@api_bp.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return jsonify({"message": "Logged out successfully"})

@api_bp.route("/me", methods=["GET"])
def me():
    if "user_id" in session:
        return jsonify({
            "id": session["user_id"], 
            "role": session.get("role"),
            "name": session.get("name")
        })
    return jsonify({"error": "Not logged in"}), 401

@api_bp.route("/users", methods=["GET"])
def list_users():
    if session.get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403
    supabase = get_supabase()
    res = supabase.table("users").select("id, name, email, role").execute()
    return jsonify(res.data)

@api_bp.route("/users/<int:uid>", methods=["DELETE"])
def delete_user(uid):
    if session.get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403
    get_supabase().table("users").delete().eq("id", uid).execute()
    return jsonify({"message": "Deleted"})


# -----------------------------------------------------------------------------
# POST /api/safe-route  — Safe pothole-avoiding route
# -----------------------------------------------------------------------------
import math, requests as http_requests

def _make_circle_polygon(lat, lon, radius_m=30, num_points=8):
    """
    Reduced complexity to 8 points and 30m radius to stay within ORS limits.
    """
    coords = []
    lat_rad = math.radians(lat)
    delta_lat = radius_m / 111320.0
    delta_lon = radius_m / (111320.0 * math.cos(lat_rad)) if math.cos(lat_rad) != 0 else 0

    for i in range(num_points + 1):
        angle = 2 * math.pi * i / num_points
        c_lon = lon + delta_lon * math.cos(angle)
        c_lat = lat + delta_lat * math.sin(angle)
        coords.append([c_lon, c_lat])

    return coords


def _build_avoid_multipolygon(potholes, radius_m=30):
    """Build a GeoJSON MultiPolygon for ORS avoid_polygons."""
    polygons = []
    for p in potholes:
        try:
            lat = float(p["latitude"])
            lon = float(p["longitude"])
            ring = _make_circle_polygon(lat, lon, radius_m)
            polygons.append([ring])
        except:
            continue
    return { "type": "MultiPolygon", "coordinates": polygons }


def _call_ors(start_lonlat, end_lonlat, avoid_polygons=None, api_key=""):
    url = "https://api.openrouteservice.org/v2/directions/driving-car/geojson"
    headers = {
        "Authorization": api_key,
        "Content-Type": "application/json"
    }
    body = { "coordinates": [start_lonlat, end_lonlat] }
    if avoid_polygons and avoid_polygons.get("coordinates"):
        body["options"] = {"avoid_polygons": avoid_polygons}

    try:
        print(f"[>] Calling ORS API... (Avoid: {len(avoid_polygons['coordinates']) if avoid_polygons else 0} polygons)")
        resp = http_requests.post(url, json=body, headers=headers, timeout=10)
        
        if resp.status_code != 200:
            err_msg = f"ORS API Error {resp.status_code}"
            try:
                err_msg += f": {resp.json().get('error', {}).get('message')}"
            except: pass
            print(f"[!] {err_msg}")
            raise Exception(err_msg)
            
        data = resp.json()
        coords_raw = data["features"][0]["geometry"]["coordinates"]
        route_latlon = [[c[1], c[0]] for c in coords_raw]
        print(f"[+] Route received: {len(route_latlon)} points.")
        return route_latlon
    except Exception as e:
        print(f"[!] ORS Request Failed: {e}")
        raise e


def _route_intersects_potholes(route_latlon, potholes, radius_m=60):
    """
    Quick Python-side check: does any route point fall within radius_m of
    any high-severity pothole?  Uses flat-earth approximation (fine for <1 km).
    """
    for rlat, rlon in route_latlon:
        for p in potholes:
            try:
                plat = float(p["latitude"])
                plon = float(p["longitude"])
            except (TypeError, KeyError, ValueError):
                continue
            dlat = (rlat - plat) * 111_320
            dlon = (rlon - plon) * 111_320 * math.cos(math.radians(plat))
            if math.sqrt(dlat ** 2 + dlon ** 2) < radius_m:
                return True
    return False


# -- Helper for Point-to-Line Segment distance --
def _point_to_route_dist(p_lat, p_lon, route):
    """Calculates the minimum distance from a point to a polyline in meters."""
    from utils.helpers import haversine
    if not route or len(route) < 2: return 999999
    
    min_dist = 999999
    
    # Local Cartesian approximation for small distances
    lat_avg = math.radians(route[0][0])
    scale_x = math.cos(lat_avg)
    
    for i in range(len(route) - 1):
        a = route[i]
        b = route[i+1]
        
        # P, A, B coordinates scaled for Cartesian distance
        px, py = p_lon * scale_x, p_lat
        ax, ay = a[1] * scale_x, a[0]
        bx, by = b[1] * scale_x, b[0]
        
        vx, vy = bx - ax, by - ay # Segment vector
        wx, wy = px - ax, py - ay # Point-to-start vector
        
        mag_v_sq = vx*vx + vy*vy
        if mag_v_sq == 0:
            d = haversine(p_lat, p_lon, a[0], a[1])
        else:
            t = max(0, min(1, (wx * vx + wy * vy) / mag_v_sq))
            # Closest point on segment in Cartesian
            cx, cy = ax + t * vx, ay + t * vy
            # Map back to lat/lon for accurate Haversine
            d = haversine(p_lat, p_lon, cy, cx / scale_x)
            
        if d < min_dist:
            min_dist = d
            
    return min_dist

@api_bp.route("/safe-route", methods=["POST"])
def safe_route():
    from config import Config
    from utils.helpers import haversine
    data   = request.get_json(silent=True) or {}
    start  = data.get("start")   # [lat, lon]
    end    = data.get("end")     # [lat, lon]

    if not start or not end or len(start) != 2 or len(end) != 2:
        return jsonify({"error": "start and end [lat, lon] required"}), 400

    api_key = Config.ORS_API_KEY
    if not api_key or api_key == "YOUR_OPENROUTESERVICE_API_KEY":
        return jsonify({"error": "ORS API key not configured"}), 503

    supabase = get_supabase()

    # 1. Fetch BOTH AI and Pending/Approved Citizen reports
    try:
        p_res = supabase.table("potholes") \
            .select("id, latitude, longitude, severity, pothole") \
            .eq("pothole", True) \
            .execute()
        u_res = supabase.table("user_reports") \
            .select("id, latitude, longitude, status") \
            .in_("status", ["approved", "pending"]) \
            .execute()
            
        ai_potholes = p_res.data or []
        user_potholes = u_res.data or []
        
        # Map user reports to same format
        for ur in user_potholes:
            ur["severity"] = "medium"
            ur["pothole"] = True
            
        all_potholes = ai_potholes + user_potholes
    except Exception as e:
        return jsonify({"error": f"Database error: {e}"}), 500

    # 2. Filter active ones (not fixed)
    try:
        conn = get_sqlite()
        cur  = conn.cursor()
        cur.execute("SELECT pothole_id, status FROM assignments")
        assigns = {r["pothole_id"]: r["status"] for r in cur.fetchall()}
        conn.close()
    except Exception: assigns = {}

    active_potholes = [
        p for p in all_potholes
        if assigns.get(p["id"], "Pending").lower() not in ("completed", "fixed")
    ]

    # 3. Proximity filter (15km radius for processing)
    proximate_potholes = []
    for p in active_potholes:
        p_lat, p_lon = float(p["latitude"]), float(p["longitude"])
        if haversine(p_lat, p_lon, start[0], start[1]) < 15000 or \
           haversine(p_lat, p_lon, end[0], end[1]) < 15000:
            proximate_potholes.append(p)
    
    # 4. ORS Call (Avoid High severity)
    high_potholes = [p for p in proximate_potholes if (p.get("severity") or "").lower() == "high"]
    start_ll, end_ll = [start[1], start[0]], [end[1], end[0]]
    avoid_mp = _build_avoid_multipolygon(high_potholes[:20], radius_m=40) if high_potholes else None
    
    try:
        route_latlon = _call_ors(start_ll, end_ll, avoid_mp, api_key)
    except Exception as e:
        print(f"[!] Falling back to basic route (avoidance failed: {e})")
        try:
            route_latlon = _call_ors(start_ll, end_ll, None, api_key)
        except Exception as e2:
            return jsonify({"error": "Routing service unavailable", "detail": str(e2)}), 503

    # 5. GEOMETRIC ACCURACY check & DEDUPLICATION (50m locations)
    THRESHOLD = 100.0
    detected_raw = []
    
    for p in proximate_potholes:
        dist = _point_to_route_dist(float(p["latitude"]), float(p["longitude"]), route_latlon)
        if dist <= THRESHOLD:
            detected_raw.append({
                "id": p["id"],
                "lat": float(p["latitude"]),
                "lon": float(p["longitude"]),
                "severity": (p.get("severity") or "medium").lower(),
                "dist": dist
            })

    # Deduplicate detected potholes to show unique LOCATIONS
    detected_on_route = []
    encountered_severities = set()
    
    for p in detected_raw:
        found = False
        for g in detected_on_route:
            # If within 50m of an already detected location, merge/ignore
            if haversine(p["lat"], p["lon"], g["lat"], g["lon"]) < 50.0:
                # Keep the one closest to the route or highest severity
                if p["severity"] == "high" and g["severity"] != "high":
                    g["severity"] = "high"
                found = True
                break
        if not found:
            detected_on_route.append(p)
            encountered_severities.add(p["severity"])

    # 6. Safety classification
    safety = "safe"
    if "high" in encountered_severities:
        safety = "unsafe"
    elif "medium" in encountered_severities or "low" in encountered_severities:
        safety = "moderate"
    else:
        safety = "safe"

    # Debug Logging
    print(f"[NAV] Route Points: {len(route_latlon)}")
    print(f"[NAV] Nearby Potholes: {len(proximate_potholes)}")
    print(f"[NAV] Detected on route: {len(detected_on_route)}")
    if detected_on_route:
        print(f"[NAV] First dist: {detected_on_route[0]['dist']:.2f}m")

    return jsonify({
        "route": route_latlon,
        "safety": safety,
        "potholes_on_path": len(detected_on_route),
        "detected_potholes": detected_on_route,
        "avoided_potholes": len(high_potholes) if safety != "unsafe" else 0
    })


# -----------------------------------------------------------------------------
# GET /api/alerts — Unified AI + User Reports Grouped by Location
# -----------------------------------------------------------------------------
@api_bp.route("/alerts", methods=["GET"])
@api_bp.route("/all-potholes", methods=["GET"])
def get_alerts_unified():
    supabase = get_supabase()
    from utils.helpers import human_time, haversine
    
    # 1. Fetch AI Potholes (active/pothole=true) - Limit to most recent 500 for performance
    p_res = supabase.table("potholes").select("*").eq("pothole", True).order("created_at", desc=True).limit(500).execute()
    potholes = p_res.data or []
    
    # 2. Fetch User Reports (Approved + Pending) - Limit to most recent 200
    u_res = supabase.table("user_reports").select("*").in_("status", ["approved", "pending"]).order("created_at", desc=True).limit(200).execute()
    user_reports = u_res.data or []
    
    # 3. Get Assignments for Status
    try:
        conn = get_sqlite()
        cur = conn.cursor()
        cur.execute("SELECT pothole_id, status FROM assignments")
        assigns = {row[0]: row[1] for row in cur.fetchall()}
        conn.close()
    except:
        assigns = {}
    
    final_groups = []
    
    # A. First, process all AI potholes as anchors
    for p in potholes:
        lat, lon = float(p["latitude"]), float(p["longitude"])
        
        # Check if it merges with an existing group in our list
        found = False
        for g in final_groups:
            if g["type"] == "pothole" and haversine(lat, lon, g["latitude"], g["longitude"]) < 50.0:
                # Merge logic
                g["report_count"] = (g.get("report_count") or 1) + (p.get("report_count") or 1)
                p_ts = p.get("last_reported_at") or p.get("created_at")
                if p_ts and (not g["last_reported_at"] or p_ts > g["last_reported_at"]):
                    g["last_reported_at"] = p_ts
                found = True
                break
        
        if not found:
            p_ts = p.get("last_reported_at") or p.get("created_at")
            final_groups.append({
                "id": p["id"],
                "latitude": lat,
                "longitude": lon,
                "severity": (p.get("severity") or "medium").lower(),
                "report_count": p.get("report_count") or 1,
                "last_reported_at": p_ts,
                "status": assigns.get(p["id"], "Pending"),
                "image": p.get("image_url"),
                "all_images": [p.get("image_url")] if p.get("image_url") else [],
                "source": "AI",
                "type": "pothole",
                "description": p.get("type", "Pothole detected by AI")
            })

    # B. Next, process all user reports
    for ur in user_reports:
        lat, lon = float(ur["latitude"]), float(ur["longitude"])
        ur_img = ur.get("media_url")
        
        # 1. Try to merge with an AI pothole group
        merged = False
        for g in final_groups:
            if g["type"] == "pothole" and haversine(lat, lon, g["latitude"], g["longitude"]) < 50.0:
                g["report_count"] += 1
                g["source"] = "AI + Citizen"
                if ur_img: g["all_images"].append(ur_img)
                ur_ts = ur.get("created_at")
                if ur_ts and (not g["last_reported_at"] or ur_ts > g["last_reported_at"]):
                    g["last_reported_at"] = ur_ts
                merged = True
                break
        
        if merged: continue

        # 2. Try to merge with an existing User Report group
        for g in final_groups:
            if g["type"] == "user_report" and haversine(lat, lon, g["latitude"], g["longitude"]) < 50.0:
                g["report_count"] += 1
                if ur_img: g["all_images"].append(ur_img)
                ur_ts = ur.get("created_at")
                if ur_ts and (not g["last_reported_at"] or ur_ts > g["last_reported_at"]):
                    g["last_reported_at"] = ur_ts
                merged = True
                break
        
        if merged: continue

        # 3. Create new standalone User Report group
        final_groups.append({
            "id": ur["id"],
            "latitude": lat,
            "longitude": lon,
            "severity": "medium", # Default for user reports
            "report_count": 1,
            "last_reported_at": ur.get("created_at"),
            "status": ur.get("status", "pending"),
            "image": ur_img,
            "all_images": [ur_img] if ur_img else [],
            "source": "Citizen",
            "type": "user_report",
            "description": ur.get("description") or "Citizen reported hazard"
        })

    # 5. Final Formatting & Normalization
    for g in final_groups:
        g["last_seen"] = human_time(g["last_reported_at"])
        # Normalize image URLs
        def normalize(url):
            if url and not url.startswith("http") and not url.startswith("/static"):
                 return f"{Config.SUPABASE_URL}/storage/v1/object/public/potholes/{url}"
            return url
            
        g["image"] = normalize(g["image"])
        g["all_images"] = [normalize(url) for url in g["all_images"] if url]
        
    return jsonify(final_groups)


# -----------------------------------------------------------------------------
# USER REPORTING MODULE
# -----------------------------------------------------------------------------

@api_bp.route("/report", methods=["POST"])
def submit_report():
    supabase = get_supabase()
    file = request.files.get("media")
    lat  = request.form.get("lat", type=float)
    lon  = request.form.get("lon", type=float)
    desc = request.form.get("description", "")
    uid  = session.get("user_id")

    if not file or lat is None or lon is None:
        return jsonify({"error": "Missing required fields (media, lat, lon)"}), 400

    # 1. Validate and Upload Media to 'user-reports' bucket
    ALLOWED = {"jpg", "jpeg", "png", "webp", "mp4"}
    ext = file.filename.rsplit(".", 1)[1].lower()
    if ext not in ALLOWED:
        return jsonify({"error": "Unsupported file format"}), 400

    try:
        fname = f"report_{int(datetime.now().timestamp())}_{secure_filename(file.filename)}"
        ftype = "video" if ext == "mp4" else "image"
        
        # Upload
        file_bytes = file.read()
        supabase.storage.from_("user-reports").upload(fname, file_bytes, {"content-type": file.content_type})
        media_url = supabase.storage.from_("user-reports").get_public_url(fname)

        # 2. Insert into user_reports table (Always store every report as PENDING)
        report_data = {
            "latitude": lat,
            "longitude": lon,
            "media_url": media_url,
            "type": ftype,
            "description": desc,
            "status": "pending", 
            "user_id": uid
        }
        supabase.table("user_reports").insert(report_data).execute()

        return jsonify({"message": "Report submitted and awaiting review.", "url": media_url}), 201
    except Exception as e:
        print(f"[!] Report Error: {e}")
        return jsonify({"error": f"Submission failed: {str(e)}"}), 500


@api_bp.route("/user-reports", methods=["GET"])
def get_user_reports():
    status = request.args.get("status")
    supabase = get_supabase()
    
    query = supabase.table("user_reports").select("*")
    if status and status != 'all':
        query = query.eq("status", status)
        
    res = query.order("created_at", desc=True).execute()
    return jsonify(res.data)


@api_bp.route("/user-reports/action", methods=["POST"])
def report_action():
    if session.get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403
        
    data = request.get_json()
    rid  = data.get("id")
    action = data.get("action") # 'approve' or 'reject'
    
    supabase = get_supabase()
    
    try:
        if action == "approve":
            # 1. Get report data
            res = supabase.table("user_reports").select("*").eq("id", rid).single().execute()
            report = res.data
            if not report: return jsonify({"error": "Report not found"}), 404
            
            lat, lon = report["latitude"], report["longitude"]
            
            # 2. Check for duplicates before creating new pothole
            existing_pothole = is_duplicate(supabase, lat, lon)
            
            if existing_pothole:
                # Update existing
                pid = existing_pothole["id"]
                new_count = (existing_pothole.get("report_count") or 1) + 1
                
                supabase.table("potholes").update({
                    "last_reported_at": datetime.now().isoformat(),
                    "report_count": new_count
                }).eq("id", pid).execute()
            else:
                # Insert new pothole
                p_data = {
                    "latitude": lat,
                    "longitude": lon,
                    "image_url": report["media_url"],
                    "severity": "medium",
                    "pothole": True,
                    "type": "pothole",
                    "confidence": 0.9,
                    "report_count": 1,
                    "last_reported_at": datetime.now().isoformat()
                }
                supabase.table("potholes").insert(p_data).execute()
            
            # 3. Update report status
            supabase.table("user_reports").update({"status": "approved"}).eq("id", rid).execute()
            
        elif action == "reject":
            supabase.table("user_reports").update({"status": "rejected"}).eq("id", rid).execute()
            
        return jsonify({"message": f"Report {action}d successfully"})
    except Exception as e:
        return jsonify({"error": f"Action failed: {str(e)}"}), 500

