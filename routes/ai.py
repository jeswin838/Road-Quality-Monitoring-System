# ================= IMPORTS =================
import os
import uuid
import time
import cv2
import numpy as np
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify
from ultralytics import YOLO
from config import Config
from utils.helpers import haversine, is_duplicate

# ================= INIT =================
ai_bp = Blueprint("ai", __name__)

# ================= MODEL (LAZY LOADING) =================
model = None

def load_model():
    global model
    if model is None:
        print("🚀 Loading YOLO model...")
        model_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'best.pt')
        if not os.path.exists(model_path):
            print("❌ MODEL NOT FOUND:", model_path)
            return None
        try:
            model = YOLO(model_path)
            print("✅ Model loaded")
        except Exception as e:
            print(f"❌ Error loading YOLO: {e}")
            return None
    return model


# ================= TRIGGER STATE =================
trigger_flag      = False
last_trigger_time = 0
last_sensor       = {"diff": 0.0, "vib": 0.0, "spike_ms": 0}
processing        = False   # Global request lock — prevents duplicate inserts


@ai_bp.route("/trigger", methods=["POST"])
def trigger():
    """ESP32 hardware trigger. Stores sensor payload and signals Android to capture."""
    global trigger_flag, last_trigger_time, last_sensor
    now = time.time()
    if now - last_trigger_time < 3:
        return jsonify({"status": "ignored", "reason": "cooldown"})

    try:
        diff     = float(request.form.get("diff",     0) or 0)
        vib      = float(request.form.get("vib",      0) or 0)
        spike_ms = float(request.form.get("spike_ms", 0) or 0)
    except (ValueError, TypeError):
        diff, vib, spike_ms = 0.0, 0.0, 0.0

    last_sensor = {"diff": diff, "vib": vib, "spike_ms": spike_ms}
    trigger_flag      = True
    last_trigger_time = now
    print(f"[SENSOR] ESP32 TRIGGER | diff={diff} vib={vib} spike_ms={spike_ms}")
    return jsonify({"status": "ok", "diff": diff, "vib": vib, "spike_ms": spike_ms})


@ai_bp.route("/check", methods=["GET"])
def check():
    """Android polling endpoint. Returns capture=True once per trigger."""
    global trigger_flag
    if trigger_flag:
        trigger_flag = False
        print("📸 Capture signal sent to Android")
        return jsonify({"capture": True})
    return jsonify({"capture": False})


# ================= UPLOAD WITH RETRY =================
def upload_with_retry(img_bytes: bytes, base_name: str):
    """Upload image bytes to Supabase storage, retrying up to 3 times."""
    from app import supabase
    for i in range(3):
        try:
            filename = f"{base_name}_{i}.jpg"
            supabase.storage.from_("pothole-images").upload(
                path=filename,
                file=img_bytes,
                file_options={"content-type": "image/jpeg"}
            )
            print("✅ Uploaded:", filename)
            return filename
        except Exception as e:
            print(f"[DB] Upload retry {i+1}/3: {e}")
            time.sleep(1)
    return None


# ================= DATABASE INSERT WITH RETRY =================
def db_insert_with_retry(supabase, record: dict, retries: int = 3) -> bool:
    """Insert a pothole record into Supabase, retrying up to `retries` times."""
    for attempt in range(1, retries + 1):
        try:
            supabase.table("potholes").insert(record).execute()
            print(f"[DB] Saved on attempt {attempt}")
            return True
        except Exception as e:
            print(f"[DB] Insert attempt {attempt}/{retries} failed: {e}")
            if attempt < retries:
                time.sleep(1)
    print("[DB] All insert attempts exhausted.")
    return False


# ================= SEVERITY ORDERING =================
SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}

def max_severity(a: str, b: str) -> str:
    """Return the higher of two severity strings. Never downgrade existing records."""
    return a if SEVERITY_RANK.get(a, 0) >= SEVERITY_RANK.get(b, 0) else b


# ================= UPDATE EXISTING POTHOLE =================
def update_existing_pothole(supabase, pothole_id: int, new_severity: str):
    """
    Update an existing pothole record:
    - Severity is only raised, never lowered.
    - last_reported_at is always refreshed.
    - report_count is incremented via RPC.
    """
    try:
        # Fetch existing severity
        existing = supabase.table("potholes").select("severity").eq("id", pothole_id).execute()
        if existing.data:
            old_severity = existing.data[0].get("severity", "low")
            final_severity = max_severity(old_severity, new_severity)
        else:
            final_severity = new_severity

        supabase.table("potholes") \
            .update({
                "severity": final_severity,
                "last_reported_at": datetime.now(timezone.utc).isoformat()
            }) \
            .eq("id", pothole_id) \
            .execute()

        # Increment report count via RPC
        try:
            supabase.rpc("increment_report_count", {"row_id": pothole_id}).execute()
        except Exception as rpc_err:
            print(f"[DB] RPC increment failed (non-critical): {rpc_err}")

        print(f"[DB] Pothole {pothole_id} updated → severity={final_severity}, count++")
    except Exception as e:
        print(f"[DB] ❌ Update failed: {e}")


# ================= SPIKE VALIDATION =================
def classify_spike(spike_ms: float) -> str:
    """
    Categorize spike by duration:
      < 200ms  → noise
      200–600ms → pothole
      > 600ms  → speed_breaker
    """
    if spike_ms <= 0:
        return "unknown"  # Not yet reported by ESP32, don't block
    elif spike_ms < 200:
        return "noise"
    elif spike_ms <= 600:
        return "pothole"
    else:
        return "speed_breaker"


# ================= YOLO INFERENCE ON SINGLE FRAME =================
def run_inference(m, img: np.ndarray) -> tuple:
    """Run YOLO on a single image. Returns (results, detections_list)."""
    detections = []
    results = None
    try:
        start = time.time()
        results = m.predict(source=img, conf=0.4, imgsz=640, verbose=False)
        elapsed = (time.time() - start) * 1000
        print(f"[AI] Inference: {elapsed:.0f}ms")
        for r in results:
            if r.boxes is None:
                continue
            for box in r.boxes:
                cls  = int(box.cls[0])
                conf = float(box.conf[0])
                x1, y1, x2, y2 = map(float, box.xyxy[0])
                w_px, h_px = x2 - x1, y2 - y1
                detections.append({
                    "type":       m.names[cls],
                    "confidence": round(conf, 2),
                    "box":        [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
                    "max_dim":    round(max(w_px, h_px), 1)
                })
    except Exception as e:
        print(f"[AI] Inference error: {e}")
    return results, detections


# ================= BEST FRAME SELECTION =================
def select_best_frame(frames: list) -> tuple:
    """
    Given a list of (results, detections, img) tuples,
    pick the frame with the highest (confidence * max_dim) score.
    Returns (results, detections, img) of the best frame.
    """
    best_score = -1
    best = frames[0]
    for frame in frames:
        results, detections, img = frame
        if detections:
            primary = max(detections, key=lambda d: d["max_dim"])
            score = primary["confidence"] * primary["max_dim"]
            if score > best_score:
                best_score = score
                best = frame
    return best


# ================= DECISION LOGIC =================
def decide_severity(diff: float, sensor: dict, detections: list) -> str:
    """
    Strict Sensor-Authority Severity Logic:
      - AI confirms pothole existence (conf >= 0.5 required)
      - Sensor diff determines severity level
      - If AI fails, only very strong sensor signal allowed (fallback)
    """
    # --- CASE 1: AI DETECTS ---
    if detections:
        primary = max(detections, key=lambda d: d["max_dim"])
        conf = primary["confidence"]
        print(f"[AI] detected pothole | conf={conf:.2f} max_dim={primary['max_dim']:.0f}px")

        if conf < 0.5:
            print("[AI] Confidence too low — ignored")
            return "ignored"

        # Sensor strictly controls severity
        if diff > 40:
            return "high"
        elif diff > 25:
            return "medium"
        elif diff > 10:
            return "low"
        else:
            return "ignored"

    # --- CASE 2: AI FAILS ---
    print("[AI] No detection")
    if diff >= 45 and sensor["vib"] > 0:
        print("[FUSION] Sensor fallback → LOW")
        return "low"

    return "ignored"


# ================= /analyze ENDPOINT =================
@ai_bp.route("/analyze", methods=["POST"])
def analyze():
    """
    Main fusion pipeline:
    1. Validate inputs
    2. Load sensor state
    3. Classify spike shape
    4. Run YOLO inference on all submitted frames
    5. Select best frame
    6. Decide severity (sensor authority)
    7. Upload annotated image
    8. Persist (insert or update duplicate)
    """
    global processing
    if processing:
        print("[FUSION] Already processing → skip")
        return jsonify({"status": "busy"})

    processing = True
    try:
        from app import supabase
        from utils.helpers import is_duplicate

        m = load_model()
        if m is None:
            return jsonify({"error": "Model not loaded"}), 500

        # ----- 1. Extract Inputs -----
        files = request.files.getlist("image")   # Support multi-frame upload
        if not files:
            files = [request.files.get("image")]  # Fallback to single frame
        files = [f for f in files if f is not None]

        lat = request.form.get("lat", type=float)
        lon = request.form.get("lon", type=float)

        if not files or lat is None or lon is None:
            return jsonify({"error": "Missing image, lat, or lon"}), 400

        # ----- 2. Sensor State -----
        diff     = last_sensor.get("diff",     0.0)
        vib      = last_sensor.get("vib",      0.0)
        spike_ms = last_sensor.get("spike_ms", 0)
        sensor   = {"diff": diff, "vib": vib, "spike_ms": spike_ms}
        print(f"[SENSOR] diff={diff} vib={vib} spike_ms={spike_ms}")

        # ----- 3. Spike Classification -----
        spike_class = classify_spike(spike_ms)
        print(f"[SENSOR] spike_class={spike_class}")
        if spike_class == "noise":
            print("[FUSION] Noise spike — ignoring")
            return jsonify({"status": "ignored", "reason": "noise_spike"})
        if spike_class == "speed_breaker":
            print("[FUSION] Speed breaker spike — ignoring")
            return jsonify({"status": "ignored", "reason": "speed_breaker"})
        # Unknown (spike_ms=0) is allowed through — don't block real detections

        # ----- 4. Decode & Infer All Frames -----
        frames = []
        for f in files:
            file_bytes = f.read()
            np_arr = np.frombuffer(file_bytes, np.uint8)
            img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
            if img is None:
                continue
            results, detections = run_inference(m, img)
            frames.append((results, detections, img))

        if not frames:
            return jsonify({"error": "All frames invalid"}), 400

        # ----- 5. Best Frame Selection -----
        best_results, best_detections, best_img = select_best_frame(frames)
        print(f"[AI] Best frame: {len(best_detections)} detection(s)")

        # ----- 6. Decision Logic -----
        decision = decide_severity(diff, sensor, best_detections)

        if best_detections:
            db_type = "ai_verified"
        elif decision != "ignored":
            db_type = "sensor_fallback"
        else:
            db_type = "none"

        print(f"[FUSION] decision={decision.upper()} | db_type={db_type}")
        if decision == "ignored":
            reason = "diff too low" if diff <= 10 else ("no AI detection" if not best_detections else "low confidence")
            print(f"[FUSION] ignored reason: {reason}")
            return jsonify({
                "status":     "ignored",
                "decision":   decision,
                "diff":       diff,
                "spike_ms":   spike_ms,
                "detections": best_detections
            })

        # ----- 7. Select Primary Detection -----
        primary = max(best_detections, key=lambda d: d["max_dim"]) if best_detections \
                  else {"confidence": 0.0, "type": "sensor_only"}

        # ----- 8. Annotate & Upload Image -----
        image_url = None
        try:
            if best_results is not None:
                annotated = best_results[0].plot()
            else:
                annotated = best_img  # Raw frame if no results
            _, buffer = cv2.imencode('.jpg', annotated)
            base_name = f"fusion_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
            filename  = upload_with_retry(buffer.tobytes(), base_name)
            if filename:
                image_url = f"{Config.SUPABASE_URL}/storage/v1/object/public/pothole-images/{filename}"
                print("[FUSION] Image uploaded")
            else:
                print("[FUSION] Image upload failed — aborting save")
        except Exception as e:
            print(f"[FUSION] ❌ Image upload error: {e}")

        if image_url is None:
            return jsonify({"status": "upload_failed", "decision": decision}), 500

        # ----- 9. Persist (Update or Insert) -----
        try:
            duplicate = is_duplicate(supabase, lat, lon, threshold_m=5.0)
        except Exception as e:
            print(f"[DB] Duplicate check error: {e}")
            duplicate = None

        if duplicate:
            print(f"[FUSION] Duplicate ({duplicate['id']}) → updating")
            update_existing_pothole(supabase, duplicate['id'], decision)
            return jsonify({
                "status":     "updated",
                "decision":   decision,
                "diff":       diff,
                "spike_ms":   spike_ms,
                "detections": best_detections,
                "image_url":  image_url
            })

        # New pothole — insert
        db_insert_with_retry(supabase, {
            "latitude":   lat,
            "longitude":  lon,
            "severity":   decision,
            "confidence": primary["confidence"],
            "image_url":  image_url,
            "type":       db_type,
            "pothole":    True,
            "created_at": datetime.now(timezone.utc).isoformat()
        })
        print("[DB] New pothole saved")

        # ----- 10. Response -----
        return jsonify({
            "status":     "success",
            "decision":   decision,
            "diff":       diff,
            "spike_ms":   spike_ms,
            "detections": best_detections,
            "image_url":  image_url
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[FUSION] ❌ Unhandled error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        processing = False


# ================= USER REPORT ENDPOINT =================
@ai_bp.route("/user-report", methods=["POST"])
def user_report():
    """Manual User Report. Runs AI validation, sets Verified/Rejected status, and saves to DB."""
    from app import supabase
    m = load_model()
    if m is None:
        return jsonify({"error": "Model not loaded"}), 500

    file = request.files.get("image")
    lat  = request.form.get("lat") or request.form.get("latitude")
    lon  = request.form.get("lon") or request.form.get("longitude")

    if not file or not lat or not lon:
        return jsonify({"error": "Missing image or location"}), 400

    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except ValueError:
        return jsonify({"error": "Invalid coordinates"}), 400

    # Decode Image
    try:
        file_bytes = file.read()
        np_arr = np.frombuffer(file_bytes, np.uint8)
        img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        if img is None:
            raise Exception("Decode failed")
    except Exception:
        return jsonify({"error": "Invalid image data"}), 400

    h, w, _ = img.shape
    img_area = w * h

    # AI Inference
    results = m.predict(source=img, conf=0.3, imgsz=640, verbose=False)

    detections = []
    pothole_detected = False

    for r in results:
        if r.boxes is None:
            continue
        for box in r.boxes:
            cls  = int(box.cls[0])
            conf = float(box.conf[0])
            x1, y1, x2, y2 = map(float, box.xyxy[0])

            bbox_area = (x2 - x1) * (y2 - y1)
            ratio = (bbox_area / img_area) * 100
            if ratio > 5.0:   severity = "high"
            elif ratio > 2.0: severity = "medium"
            else:             severity = "low"

            class_name = m.names[cls].lower()
            if class_name in ["pothole", "crack"] and conf > 0.5:
                pothole_detected = True

            detections.append({
                "type":       class_name,
                "confidence": round(conf, 2),
                "severity":   severity,
                "box":        [x1, y1, x2, y2]
            })

    status = "Verified" if pothole_detected else "Rejected (Not Pothole)"

    # Save Annotated Image
    annotated  = results[0].plot()
    _, buffer  = cv2.imencode(".jpg", annotated)
    base_name  = f"user_{status.lower().replace(' ', '_')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    filename   = upload_with_retry(buffer.tobytes(), base_name)
    if not filename:
        return jsonify({"error": "upload_failed"}), 500

    image_url = f"{Config.SUPABASE_URL}/storage/v1/object/public/pothole-images/{filename}"

    # Save to Database
    try:
        report_status = "approved" if pothole_detected else "rejected"
        supabase.table("user_reports").insert({
            "latitude":    lat_f,
            "longitude":   lon_f,
            "media_url":   image_url,
            "type":        "image",
            "description": "AI Verified Pothole" if pothole_detected else "Rejected: Not a Pothole",
            "status":      report_status,
            "created_at":  datetime.now(timezone.utc).isoformat()
        }).execute()

        if pothole_detected:
            primary = next(
                (d for d in detections if d["type"] in ["pothole", "crack"]),
                {"severity": "low", "confidence": 0, "type": "none"}
            )
            supabase.table("potholes").insert({
                "latitude":  lat_f,
                "longitude": lon_f,
                "severity":  primary["severity"],
                "image_url": image_url,
                "confidence": primary["confidence"],
                "type":      primary["type"],
                "pothole":   True,
                "status":    "pending",
                "created_at": datetime.now(timezone.utc).isoformat()
            }).execute()
    except Exception as e:
        print(f"[DB] ❌ User report insert error: {e}")

    return jsonify({
        "status":           status,
        "pothole_detected": pothole_detected,
        "detections":       detections,
        "image_url":        image_url
    })
