"""
app.py — Flask API for the B2C scheduler (deployment Phase 4, planning mode v1).

Matches the existing JKT planning contract so the frontend calls it identically:

    POST /app/v1/jkt/planning-scheduling/plan/generate-plan   {"plan_id": "<id>"}
    GET  /app/v1/jkt/planning-scheduling/health

Response (200):  {status, mode, plan_id, elapsed_seconds}
Error:           {status, stage, mode, plan_id, message}  + HTTP 400/404/409/422/500

SYNCHRONOUS: the engine runs inside the request (~2-4 min) and the response
returns when done, carrying elapsed_seconds (same as the reference contract).
Runs are serialised process-wide (_RUN_LOCK) so two requests never clobber.

v1 decisions: planning mode only (no simulation); a re-run of a plan_id
OVERWRITES its rows (write_db overwrite=True) — 409 is used ONLY for a
concurrent run in progress.

    python app.py            # dev server (API_HOST / API_PORT from env)
"""
from __future__ import annotations

import glob
import os
import threading
import time
import traceback

from flask import Blueprint, Flask, jsonify, request, send_file

import connection as conn
from main import PLAN_OUTPUT_DIR, run_plan

MODE = "planning"
PREFIX = "/app/v1/jkt/planning-scheduling"
MAX_PLAN_ID_LEN = 100      # matches plan_id VARCHAR(100) in every DB table

_RUN_LOCK = threading.Lock()

app = Flask(__name__)
bp = Blueprint("planning", __name__, url_prefix=PREFIX)


def _err(stage: str, plan_id, message: str, code: int):
    return (
        jsonify({
            "status": "error",
            "stage": stage,
            "mode": MODE,
            "plan_id": plan_id,
            "message": message,
        }),
        code,
    )


@bp.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "mode": MODE}), 200


@bp.route("/plan/download/<plan_id>/<kind>", methods=["GET"])
def download_plan(plan_id: str, kind: str):
    """Download a generated workbook: kind = 'building' | 'curing'.

    Files are written by run_plan() into PLAN_OUTPUT_DIR as
    bc_building_schedule_<plan_id>_<date>.xlsx / bc_curing_b2c_<plan_id>_<date>.xlsx
    — the plan start date is not known to the caller, so we glob on plan_id.
    """
    prefixes = {"building": "bc_building_schedule", "curing": "bc_curing_b2c"}
    if kind not in prefixes:
        return _err("validate", plan_id,
                    "kind must be 'building' or 'curing'", 400)
    plan_id = (plan_id or "").strip()
    # plan_id lands in a filesystem glob — reject anything path-like.
    if not plan_id or "/" in plan_id or "\\" in plan_id or ".." in plan_id:
        return _err("validate", plan_id, "invalid plan_id", 400)

    pattern = os.path.join(PLAN_OUTPUT_DIR, f"{prefixes[kind]}_{plan_id}_*.xlsx")
    matches = sorted(glob.glob(pattern))
    if not matches:
        return _err("read", plan_id,
                    f"no {kind} workbook found for plan_id {plan_id} "
                    f"(has the plan been generated?)", 404)

    path = matches[-1]      # newest if a plan was re-run
    return send_file(path, as_attachment=True,
                     download_name=os.path.basename(path))


@bp.route("/plan/generate-plan", methods=["POST"])
def generate_plan():
    # ── validate request body ─────────────────────────────────────────────
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _err("validate", None, "request body must be a JSON object", 400)
    plan_id = body.get("plan_id")
    if not isinstance(plan_id, str) or not plan_id.strip():
        return _err("validate", plan_id, "plan_id must be a non-empty string", 400)
    plan_id = plan_id.strip()
    if len(plan_id) > MAX_PLAN_ID_LEN:
        return _err("validate", plan_id,
                    f"plan_id must be <= {MAX_PLAN_ID_LEN} chars", 422)

    # ── serialise runs (409 if one is already in progress) ────────────────
    if not _RUN_LOCK.acquire(blocking=False):
        return _err("lock", plan_id, "another plan is currently running", 409)

    t0 = time.time()
    try:
        run_plan(plan_id, created_by="api")          # read_db → engine → write_db
        elapsed = round(time.time() - t0, 1)
        return jsonify({
            "status": "success",
            "mode": MODE,
            "plan_id": plan_id,
            "elapsed_seconds": elapsed,
        }), 200
    except ValueError as e:
        # read_db raises ValueError when the plan_id has no input rows
        return _err("read", plan_id, str(e), 404)
    except Exception as e:  # noqa: BLE001 — surface engine/write failure
        traceback.print_exc()
        return _err("schedule", plan_id, str(e), 500)
    finally:
        _RUN_LOCK.release()


app.register_blueprint(bp)


if __name__ == "__main__":
    app.run(
        host=os.environ.get("API_HOST", "0.0.0.0"),
        port=int(os.environ.get("API_PORT", "8000")),
        threaded=True,
    )
