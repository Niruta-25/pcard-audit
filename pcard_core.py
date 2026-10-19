"""Core (non-UI) helpers for the OSU P-card audit app: database access,
SQL safety checks, dashboard keyword search and the Gemini calls."""
import gzip
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).parent
DB_GZ = HERE / "pcards.db.gz"                       # committed to GitHub (22 MB)
DB_LOCAL = HERE / "pcards.db"                       # optional uncompressed copy
DB_TMP = Path(tempfile.gettempdir()) / "pcards_unpacked.db"

MAX_ROWS = 5000
QUERY_TIMEOUT_S = 20

SCHEMA = """Table pcards (one row per purchase-card transaction; Oklahoma State University, 2010-2014):
  Year INTEGER                    -- calendar year of the transaction (2010..2014)
  Month INTEGER                   -- month 1..12
  FullName TEXT                   -- anonymized cardholder, e.g. 'Employee 5433B965'
  ID INTEGER
  AgencyNumber INTEGER, AgencyName TEXT   -- always the same agency (OSU)
  CardholderLastName TEXT, CardholderFirstInitial TEXT
  Description TEXT                -- item/line description, e.g. 'GENERAL PURCHASE', 'ROOM CHARGES'; can be NULL
  Amount REAL                     -- dollars; negative = return/credit
  Vendor TEXT                     -- merchant name, e.g. 'BROWNS BOTTLE SHOP'
  TransactionDate TEXT            -- text like '7/26/2014 0:00:00' (M/D/YYYY H:MM:SS), NOT ISO
  PostedDate TEXT                 -- same text format
  MCC TEXT                        -- merchant category in words, e.g. 'EATING PLACES AND RESTAURANTS',
                                  --   'LODGING--HOTELS,MOTELS,AND RESORTS', 'SERVICE STATIONS (WITH OR WITHOUT ANCILLARY SERVICES'
"""

SYSTEM_PROMPT = f"""You translate an internal auditor's question into ONE read-only SQLite query.

{SCHEMA}
Rules:
- Output ONLY the SQL statement, no explanation, no markdown fences, no trailing semicolon chatter.
- Only SELECT (or WITH ... SELECT). Never modify data.
- Use the Year and Month columns for date filtering; do not call date functions on TransactionDate.
- For text matching use UPPER(column) LIKE '%KEYWORD%'.
- If the user does not name a year, use all years.
- Treat negative Amount values as returns/credits; for 'spending' totals sum Amount unless asked otherwise.
- Round money to 2 decimals. Give readable column aliases.
- For listings add ORDER BY and LIMIT 200 unless the user asks for everything.
- If the question cannot be answered from this table, return exactly: SELECT 'This question cannot be answered from the P-card table' AS Message
"""

PROHIBITED_KEYWORDS = [
    "alcohol, liquor, wine, beer", "cash, atm", "decor", "donation, sponsor", "gas, fuel",
    "gift", "insurance", "late fee", "postage, usps, post office", "moving, relocation",
    "membership, dues", "salary, wage", "award, incentive",
]

EXAMPLE_QUESTIONS = [
    "Which 10 employees spent the most in 2014?",
    "How much was spent at each vendor with 'USPS' in the name in 2013?",
    "Show total spending per month in 2014",
    "List the 15 largest hotel charges in 2014",
    "Which vendors had returns (negative amounts) above $1,000 in 2012?",
]


# ---------------------------------------------------------------- database
def db_path() -> Path:
    if DB_LOCAL.exists():
        return DB_LOCAL
    if not DB_TMP.exists() or DB_TMP.stat().st_size == 0:
        part = DB_TMP.with_suffix(".part")
        with gzip.open(DB_GZ, "rb") as src, open(part, "wb") as dst:
            shutil.copyfileobj(src, dst)
        part.replace(DB_TMP)
    return DB_TMP


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path().as_posix()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def run_select(sql: str, params=(), max_rows: int = MAX_ROWS, timeout: int = QUERY_TIMEOUT_S):
    """Run a read-only query. Returns (columns, rows, truncated)."""
    conn = connect()
    start = time.time()

    def _abort():
        return 1 if time.time() - start > timeout else 0

    conn.set_progress_handler(_abort, 200_000)
    try:
        cur = conn.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(max_rows + 1)
        truncated = len(rows) > max_rows
        return cols, rows[:max_rows], truncated
    finally:
        conn.close()


def years():
    _, rows, _ = run_select("SELECT DISTINCT Year FROM pcards ORDER BY Year")
    return [r[0] for r in rows]


# --------------------------------------------------------------- SQL safety
_BANNED = re.compile(r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|vacuum|reindex|truncate)\b", re.I)


def clean_sql(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```(?:sql)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    return text.strip().rstrip(";").strip()


def is_safe_select(sql: str) -> bool:
    s = clean_sql(sql)
    if not s or ";" in s:
        return False
    if not re.match(r"^(select|with)\b", s, re.I):
        return False
    return not _BANNED.search(s)


# ---------------------------------------------------------- dashboard search
def parse_keywords(text: str, limit: int = 10):
    return [k.strip() for k in (text or "").split(",") if k.strip()][:limit]


def search_transactions(year: int, field: str, keyword_text: str):
    """Keyword search of Description or Vendor for one year.
    Several keywords may be separated by commas (matches ANY of them)."""
    if field not in ("Description", "Vendor"):          # whitelist: field is interpolated
        raise ValueError("field must be Description or Vendor")
    kws = parse_keywords(keyword_text)
    if not kws:
        return [], [], False
    esc = lambda k: k.upper().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    clause = " OR ".join([f"UPPER({field}) LIKE ? ESCAPE '\\'"] * len(kws))
    sql = f"""SELECT TransactionDate, PostedDate, FullName AS Employee, Vendor, Description, Amount, MCC
              FROM pcards
              WHERE Year = ? AND ({clause})
              ORDER BY Amount DESC"""
    params = [year] + [f"%{esc(k)}%" for k in kws]
    return run_select(sql, params)


# --------------------------------------------------------------------- Gemini
_CLIENTS = {}


def _client(api_key: str):
    """One long-lived client per key. (A client that is created and immediately garbage-collected
    closes its connection, which causes 'Cannot send a request, as the client has been closed'.)"""
    if api_key not in _CLIENTS:
        from google import genai          # imported lazily so the helpers above work without it
        _CLIENTS[api_key] = genai.Client(api_key=api_key)
    return _CLIENTS[api_key]


def gemini_text(api_key: str, model: str, prompt: str, system: str | None = None) -> str:
    from google.genai import types
    client = _client(api_key)             # keep a reference for the whole call
    cfg = types.GenerateContentConfig(temperature=0, system_instruction=system) if system else \
          types.GenerateContentConfig(temperature=0)
    resp = client.models.generate_content(model=model, contents=prompt, config=cfg)
    return resp.text or ""


def question_to_sql(api_key: str, model: str, question: str, previous_error: str | None = None,
                    previous_sql: str | None = None) -> str:
    prompt = f"Auditor question: {question}"
    if previous_error:
        prompt += f"\n\nYour previous query failed.\nQuery: {previous_sql}\nSQLite error: {previous_error}\nReturn a corrected query."
    return clean_sql(gemini_text(api_key, model, prompt, SYSTEM_PROMPT))


def summarize_results(api_key: str, model: str, question: str, sql: str, cols, rows) -> str:
    preview = "\n".join([", ".join(cols)] + [", ".join("" if v is None else str(v) for v in r) for r in rows[:30]])
    prompt = (f"An auditor asked: {question}\nThe SQL used was: {sql}\n"
              f"Result preview ({min(len(rows), 30)} of {len(rows)} rows):\n{preview}\n\n"
              "Answer the question in 2-4 plain sentences using only these results. "
              "Mention that results are potential exceptions, not proof of wrongdoing, only if relevant.")
    return gemini_text(api_key, model, prompt)
