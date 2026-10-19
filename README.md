# OSU P-card Audit Assistant (Part IV)

A two-tab Streamlit site over the OSU P-card database (2010-2014):

1. **Ask the database** - natural-language question -> Gemini writes a read-only SQLite query -> the app runs it and shows the SQL, a short answer and the results.
2. **Prohibited-purchase dashboard** - instructions, a year selector, a *Description* keyword search and a separate *Vendor* keyword search (comma-separated keywords, case-insensitive), with summary metrics and CSV download.

## Files
| File | Purpose |
|---|---|
| `app.py` | Streamlit interface |
| `pcard_core.py` | database access, SQL safety checks, dashboard search, Gemini calls |
| `pcards.db.gz` | the database, gzip-compressed (22 MB) because the raw file (103 MB) is over GitHub's 100 MB limit; the app unpacks it automatically on start-up |
| `requirements.txt` | dependencies |
| `.streamlit/secrets.toml.example` | template for the API key (the real `secrets.toml` is git-ignored) |

## Run locally
```bash
pip install -r requirements.txt
mkdir -p .streamlit && cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # then edit in your key
streamlit run app.py
```

## Deploy (Streamlit Community Cloud) - **API key stays out of GitHub**
1. Create a public GitHub repo and push these files (`git add .` will skip `secrets.toml` because of `.gitignore`).
   Check with `git status` that no key appears anywhere before you push.
2. On share.streamlit.io choose *New app*, pick the repo, main file `app.py`.
3. Open *App settings -> Secrets* and paste:
   ```toml
   GEMINI_API_KEY = "your-real-key"
   ```
4. Deploy, then test both tabs on the live URL.

## Safety notes
- The database is opened **read-only** (`mode=ro` + `PRAGMA query_only`), generated SQL must be a single `SELECT`/`WITH` statement, and queries stop after 20 seconds.
- The dashboard uses parameterized queries; the searched column is limited to `Description` or `Vendor`.
- Results are risk indicators, not proof of fraud.
