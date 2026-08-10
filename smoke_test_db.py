#!/usr/bin/env python3
"""
smoke_test_db.py — fast DB connectivity smoke test for the CBC/B2C pipeline.

Run this BEFORE a pipeline run to confirm the planning DB is reachable, and to
pinpoint WHICH layer fails (config / DNS / network route / TCP / auth / query)
so a failure like "(2003) Can't connect to MySQL server ... Network is
unreachable" is diagnosed in seconds instead of a full pipeline crash.

Usage:  python smoke_test_db.py
Exit:   0 = DB reachable and query OK ; non-zero = failure (reason printed).
"""
from __future__ import annotations
import socket
import sys
import time


def main() -> int:
    print("=" * 62)
    print("  CBC/B2C — DB CONNECTIVITY SMOKE TEST")
    print("=" * 62)

    # ── 1. Config ───────────────────────────────────────────────────────────
    try:
        import cbc_env
        cfg = cbc_env.db_config()
    except Exception as e:
        print(f"[1/5] CONFIG      ✗  cannot load DB config: {e}")
        print("      → check .env has JKT_DB_HOST/PORT/USER/PASSWORD/DATABASE")
        return 2
    host, port = cfg["host"], int(cfg["port"])
    print(f"[1/5] CONFIG      ✓  host={host}  port={port}  db={cfg['database']}  user={cfg['user']}")

    # ── 2. DNS resolve ──────────────────────────────────────────────────────
    try:
        ip = socket.gethostbyname(host)
        print(f"[2/5] DNS         ✓  {host} → {ip}")
    except Exception as e:
        print(f"[2/5] DNS         ✗  cannot resolve {host}: {e}")
        print("      → host name is wrong, or no DNS. If it's already an IP this is unusual.")
        return 3

    # ── 3. TCP connect (the layer that raised 'Network is unreachable') ─────
    t0 = time.time()
    try:
        with socket.create_connection((host, port), timeout=6):
            dt = (time.time() - t0) * 1000
            print(f"[3/5] TCP {port:<5}   ✓  connected in {dt:.0f} ms")
    except socket.timeout:
        print(f"[3/5] TCP {port:<5}   ✗  TIMEOUT after 6s — host silently dropping packets")
        print("      → firewall/security-group not allowing your IP, or DB host down.")
        return 4
    except OSError as e:
        # errno 51/101 = Network unreachable ; 61 = Connection refused
        en = getattr(e, "errno", None)
        print(f"[3/5] TCP {port:<5}   ✗  {e}  (errno={en})")
        if en in (51, 101):   # ENETUNREACH
            print("      → NETWORK UNREACHABLE: no route to the host. Most likely causes:")
            print("        • VPN not connected (this DB usually needs the office/GCP VPN)")
            print("        • you're on a different network than the run that worked")
            print("        • the DB VM was stopped / its IP changed (check GCP console)")
        elif en == 61:        # ECONNREFUSED
            print("      → CONNECTION REFUSED: host is up but nothing is listening on the port")
            print("        (MySQL stopped, or wrong port).")
        else:
            print("      → transport-level failure before MySQL handshake.")
        return 4

    # ── 4. MySQL auth + engine ──────────────────────────────────────────────
    try:
        from sqlalchemy import text
        from cbc_env import make_engine
        eng = make_engine()
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
        print("[4/5] MYSQL AUTH  ✓  handshake + SELECT 1 OK")
    except Exception as e:
        msg = str(e).splitlines()[0]
        print(f"[4/5] MYSQL AUTH  ✗  {msg}")
        print("      → TCP works but MySQL rejected us: bad user/password/db, or host not")
        print("        allowed for this user. Check JKT_DB_USER / JKT_DB_PASSWORD / grants.")
        return 5

    # ── 5. A real pipeline table (proves the schema the run needs is there) ──
    try:
        from sqlalchemy import text
        from cbc_env import make_engine
        eng = make_engine()
        with eng.connect() as c:
            n = c.execute(text("SELECT COUNT(*) FROM Master_Building_Allowable_Machines")).scalar()
        print(f"[5/5] SCHEMA      ✓  Master_Building_Allowable_Machines rows = {n}")
    except Exception as e:
        msg = str(e).splitlines()[0]
        print(f"[5/5] SCHEMA      ⚠  connected but a core table read failed: {msg}")
        print("      → connectivity is fine; this is a schema/permissions issue, not network.")
        return 6

    print("-" * 62)
    print("  RESULT: ✓ DB reachable and healthy — safe to run the pipeline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
