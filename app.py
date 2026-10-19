"""OSU P-card audit assistant (Part IV): natural-language Q&A + prohibited-purchase dashboard."""
import os

import pandas as pd
import streamlit as st

import pcard_core as core

st.set_page_config(page_title="OSU P-card Audit Assistant", page_icon="💳", layout="wide")


# ---------------------------------------------------------------- settings
def get_secret(name: str, default: str | None = None):
    """API keys come from Streamlit secrets or environment variables - never from the source code."""
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name, default)


API_KEY = get_secret("GEMINI_API_KEY")
MODEL = get_secret("GEMINI_MODEL", "gemini-2.5-flash")


@st.cache_data(show_spinner="Preparing the database…")
def available_years():
    return core.years()


def to_frame(cols, rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=cols)
    for c in ("TransactionDate", "PostedDate"):
        if c in df.columns:
            df[c] = df[c].astype(str).str.replace(" 0:00:00", "", regex=False)
    return df


def money_config(df: pd.DataFrame):
    cfg = {}
    for c in df.columns:
        if c.lower() in ("amount", "totalspent", "total", "totalamount") or "amount" in c.lower() or "total" in c.lower():
            if pd.api.types.is_numeric_dtype(df[c]):
                cfg[c] = st.column_config.NumberColumn(c, format="$%.2f")
    return cfg


st.title("💳 OSU P-card Audit Assistant")
st.caption("Oklahoma State University purchasing-card transactions, 2010-2014. "
           "Flagged items are potential exceptions that need follow-up, not proof of wrongdoing. "
           "Cardholder names are anonymized in the data.")

tab_ask, tab_dash = st.tabs(["🔎 Ask the database", "🚩 Prohibited-purchase dashboard"])

# =============================================================== TAB 1: ask
with tab_ask:
    st.subheader("Ask a question in plain English")
    st.markdown(
        "Type a question about the transactions. The assistant writes a **read-only SQL query**, runs it "
        "on the database, and shows you both the query and the results so you can verify them."
    )
    with st.expander("Table fields and example questions"):
        st.code(core.SCHEMA, language="text")
        for ex in core.EXAMPLE_QUESTIONS:
            st.markdown(f"- {ex}")

    if not API_KEY:
        st.warning("No Gemini API key is configured. Add `GEMINI_API_KEY` in the app's Secrets "
                   "(see README). The dashboard tab works without it.")

    question = st.text_area("Your question", height=90,
                            placeholder="e.g. Which employees had hotel charges above $1,000 in 2014?")
    explain = st.checkbox("Also give a short written answer", value=True)

    if st.button("Run question", type="primary", disabled=not API_KEY):
        if not question.strip():
            st.info("Please type a question first.")
        else:
            try:
                with st.spinner("Writing the SQL query…"):
                    sql = core.question_to_sql(API_KEY, MODEL, question)
                result = None
                for attempt in range(2):                       # one automatic repair attempt
                    if not core.is_safe_select(sql):
                        st.error("The generated query was not a single read-only SELECT, so it was not run.")
                        st.code(sql, language="sql")
                        break
                    try:
                        result = core.run_select(sql)
                        break
                    except Exception as e:                      # bad SQL: ask the model to fix it once
                        if attempt == 1:
                            st.error(f"The query could not be executed: {e}")
                            st.code(sql, language="sql")
                        else:
                            with st.spinner("Fixing the query…"):
                                sql = core.question_to_sql(API_KEY, MODEL, question, str(e), sql)
                if result:
                    cols, rows, truncated = result
                    st.markdown("**SQL used**")
                    st.code(sql, language="sql")
                    if explain and rows:
                        with st.spinner("Summarizing…"):
                            st.info(core.summarize_results(API_KEY, MODEL, question, sql, cols, rows))
                    if rows:
                        df = to_frame(cols, rows)
                        st.markdown(f"**Results** - {len(df):,} row(s)" + (" (first 5,000 shown)" if truncated else ""))
                        st.dataframe(df, use_container_width=True, hide_index=True, column_config=money_config(df))
                        st.download_button("Download results (CSV)", df.to_csv(index=False).encode(),
                                           "query_results.csv", "text/csv")
                    else:
                        st.info("The query ran but returned no rows.")
            except Exception as e:
                st.error(f"Something went wrong while contacting the AI service: {e}")

# =========================================================== TAB 2: dashboard
with tab_dash:
    st.subheader("Prohibited-purchase search")
    with st.expander("How to use this dashboard", expanded=True):
        st.markdown(
            """
1. **Select the year** to search.
2. Use **Search 1 (Description)** to look for a keyword in the transaction *description* field
   (for example `alcohol`, `gift card`, `deposit`).
3. Use **Search 2 (Vendor)** to look for a keyword in the *vendor* name
   (for example `post office`, `usps`, `liquor`, `bottle shop`).
4. You can enter several keywords separated by commas - a transaction is shown if it matches **any** of them.
   Searches are not case-sensitive and match part of a word.
5. Review the summary and the table (largest amounts first). Use the **MCC** column and the dates to decide which
   items need follow-up, and download the list as a CSV for your working papers.

*A match is only a potential internal-control deviation. For example, "alcohol" in a description can be a lab
chemical, and a vendor with "brewing" in its name can be a restaurant.*
            """
        )
        st.markdown("**Prohibited-purchase keyword ideas:**")
        st.markdown(" · ".join(f"`{k}`" for k in core.PROHIBITED_KEYWORDS))

    year = st.selectbox("Year to search", available_years()[::-1], index=0)

    def render(results, label):
        cols, rows, truncated = results
        if not rows:
            st.info(f"No transactions matched in {year}.")
            return
        df = to_frame(cols, rows)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Matching transactions", f"{len(df):,}")
        c2.metric("Net amount", f"${df['Amount'].sum():,.2f}")
        c3.metric("Employees", f"{df['Employee'].nunique():,}")
        c4.metric("Vendors", f"{df['Vendor'].nunique():,}")
        if truncated:
            st.caption("Only the first 5,000 matches are shown; use a more specific keyword.")
        st.dataframe(df, use_container_width=True, hide_index=True, column_config=money_config(df))
        st.download_button("Download these results (CSV)", df.to_csv(index=False).encode(),
                           f"{label}_{year}.csv", "text/csv", key=f"dl_{label}")

    col_desc, col_vendor = st.columns(2)
    with col_desc:
        st.markdown("### Search 1 · Description")
        with st.form("desc_form"):
            kw_d = st.text_input("Keyword(s) in the transaction description",
                                 placeholder="alcohol, gift card, deposit")
            go_d = st.form_submit_button("Search descriptions", type="primary")
    with col_vendor:
        st.markdown("### Search 2 · Vendor")
        with st.form("vendor_form"):
            kw_v = st.text_input("Keyword(s) in the vendor name",
                                 placeholder="post office, usps, liquor")
            go_v = st.form_submit_button("Search vendors", type="primary")

    if go_d:
        if not kw_d.strip():
            st.warning("Enter at least one description keyword.")
        else:
            st.markdown(f"#### Description results for “{kw_d}” in {year}")
            render(core.search_transactions(year, "Description", kw_d), "description_search")
    if go_v:
        if not kw_v.strip():
            st.warning("Enter at least one vendor keyword.")
        else:
            st.markdown(f"#### Vendor results for “{kw_v}” in {year}")
            render(core.search_transactions(year, "Vendor", kw_v), "vendor_search")
