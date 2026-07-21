#!/usr/bin/env python3
"""
Shared portfolio store — the state behind tracker.html.

One JSON document ({"portfolios":[...]}) shared by everyone with the passcode,
held in Postgres so it survives Render's ephemeral filesystem (free instances
wipe local disk on every deploy and on idle spin-down).

Writes carry the version the editor loaded. If someone else saved in the
meantime the versions differ and the write is REJECTED rather than applied —
otherwise two people editing at once silently clobber each other, which is the
one failure mode a shared tracker can't have.

Env:
  DATABASE_URL  Postgres connection string (Neon/Supabase). Absent → in-memory
                fallback for local dev; data does NOT persist.
  PF_PASSCODE   Shared passcode. Required whenever DATABASE_URL is set, so a
                real deployment can never come up unprotected by accident.
"""
import os, json, time, threading, hmac

DATABASE_URL = (os.environ.get('DATABASE_URL') or '').strip()
PASSCODE     = (os.environ.get('PF_PASSCODE') or '').strip()

DOC_ID  = 'default'
EMPTY   = {'portfolios': []}

# In-memory fallback (local dev only — lost on restart).
_MEM      = {'doc': dict(EMPTY), 'version': 0, 'updated_at': 0, 'updated_by': ''}
_MEM_LOCK = threading.Lock()

_INIT_DONE = False
_INIT_LOCK = threading.Lock()


class StoreError(Exception):
    """Storage failed in a way the client should see as 5xx."""


class Conflict(Exception):
    """Someone else saved first; caller should reload and retry."""
    def __init__(self, current):
        super().__init__('version conflict')
        self.current = current


# ── auth ──────────────────────────────────────────────────────────────────────
def configured():
    """(ok, message) — is this deployment safe to serve?"""
    if DATABASE_URL and not PASSCODE:
        return False, ('Server misconfigured: PF_PASSCODE is not set. Refusing to '
                       'serve shared portfolios unprotected on a public URL.')
    return True, ''


def check_passcode(supplied):
    # No passcode configured + no database == local dev; allow.
    if not PASSCODE:
        return not DATABASE_URL
    return hmac.compare_digest(str(supplied or ''), PASSCODE)


def persistent():
    return bool(DATABASE_URL)


# ── postgres ──────────────────────────────────────────────────────────────────
def _connect():
    try:
        import psycopg
    except ImportError as e:                       # dep missing → surface clearly
        raise StoreError('psycopg is not installed on the server') from e
    try:
        return psycopg.connect(DATABASE_URL, connect_timeout=10)
    except Exception as e:
        raise StoreError(f'could not reach the database: {e}') from e


def _ensure_schema(conn):
    global _INIT_DONE
    if _INIT_DONE:
        return
    with _INIT_LOCK:
        if _INIT_DONE:
            return
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS shared_pf (
                    id         TEXT PRIMARY KEY,
                    doc        JSONB       NOT NULL,
                    version    INTEGER     NOT NULL DEFAULT 0,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_by TEXT        NOT NULL DEFAULT ''
                )
            """)
            conn.commit()
        _INIT_DONE = True


def _row_to_state(row):
    if not row:
        return {'doc': dict(EMPTY), 'version': 0, 'updated_at': 0, 'updated_by': ''}
    doc, version, updated_at, updated_by = row
    if isinstance(doc, str):            # driver may hand back raw JSON text
        doc = json.loads(doc)
    return {'doc': doc or dict(EMPTY),
            'version': int(version),
            'updated_at': updated_at.timestamp() if updated_at else 0,
            'updated_by': updated_by or ''}


# ── public API ────────────────────────────────────────────────────────────────
def load():
    if not DATABASE_URL:
        with _MEM_LOCK:
            return json.loads(json.dumps(_MEM))     # deep copy
    with _connect() as conn:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute('SELECT doc, version, updated_at, updated_by '
                        'FROM shared_pf WHERE id = %s', (DOC_ID,))
            return _row_to_state(cur.fetchone())


def save(doc, base_version, who=''):
    """Persist doc if base_version still matches. Raises Conflict if not."""
    if not isinstance(doc, dict) or not isinstance(doc.get('portfolios'), list):
        raise StoreError('document must be an object with a "portfolios" array')
    who = (who or '').strip()[:60]

    if not DATABASE_URL:
        with _MEM_LOCK:
            if int(base_version) != _MEM['version']:
                raise Conflict(json.loads(json.dumps(_MEM)))
            _MEM.update(doc=json.loads(json.dumps(doc)), version=_MEM['version'] + 1,
                        updated_at=time.time(), updated_by=who)
            return json.loads(json.dumps(_MEM))

    payload = json.dumps(doc)
    with _connect() as conn:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            # First write: INSERT only if absent, and only when the editor also
            # believed the doc was new (base_version 0).
            if int(base_version) == 0:
                cur.execute("""
                    INSERT INTO shared_pf (id, doc, version, updated_at, updated_by)
                    VALUES (%s, %s::jsonb, 1, now(), %s)
                    ON CONFLICT (id) DO NOTHING
                    RETURNING doc, version, updated_at, updated_by
                """, (DOC_ID, payload, who))
                row = cur.fetchone()
                if row:
                    conn.commit()
                    return _row_to_state(row)
                # Row already existed → someone got there first.
                cur.execute('SELECT doc, version, updated_at, updated_by '
                            'FROM shared_pf WHERE id = %s', (DOC_ID,))
                raise Conflict(_row_to_state(cur.fetchone()))

            cur.execute("""
                UPDATE shared_pf
                   SET doc = %s::jsonb, version = version + 1,
                       updated_at = now(), updated_by = %s
                 WHERE id = %s AND version = %s
             RETURNING doc, version, updated_at, updated_by
            """, (payload, who, DOC_ID, int(base_version)))
            row = cur.fetchone()
            if row:
                conn.commit()
                return _row_to_state(row)

            conn.rollback()
            cur.execute('SELECT doc, version, updated_at, updated_by '
                        'FROM shared_pf WHERE id = %s', (DOC_ID,))
            raise Conflict(_row_to_state(cur.fetchone()))
