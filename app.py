# Streamlit DHF Automation Pipeline (Infusion Pump)
# ------------------------------------------------
# Single-file Streamlit app that:
# 1) accepts Product Requirements (Excel upload or pasted text)
# 2) loads/merges existing Hazard Analysis (HA) + DVP outputs (local/Drive)
# 3) validates with Guardrails + optional Validation LLM
# 4) supports Human-in-the-Loop review/edit for flagged rows
# 5) generates three Excel outputs: Hazard_Analysis.xlsx, DVP.xlsx, Trace_Matrix.xlsx
# 6) persists outputs to Google Drive via Drive API (for Streamlit Cloud)
#
# Notes
# - Heavy LLM generation (Mistral 7B + LoRA) is NOT done here; this app consumes outputs produced by your training notebooks.
# - For Streamlit Cloud, use the Drive API connector below (toggle in sidebar) and store credentials in Streamlit Secrets.

import os
import io
import sys
import json
import glob
import time
import typing as t
import pandas as pd
import streamlit as st

# Optional Google Drive API (pydrive2)
try:
    from pydrive2.auth import GoogleAuth
    from pydrive2.drive import GoogleDrive
    _HAS_PYDRIVE2 = True
except Exception:
    _HAS_PYDRIVE2 = False

# Optional OpenAI for Validation LLM
try:
    import openai
    _HAS_OPENAI = True
except Exception:
    _HAS_OPENAI = False

# ------------------------------
# Configuration & Paths
# ------------------------------

def is_colab_drive_available() -> bool:
    return os.path.exists("/content/drive/MyDrive")

DEFAULT_BASE = "/content/drive/MyDrive/Colab Notebooks/Dissertation" if is_colab_drive_available() else os.path.abspath("./Dissertation")
DEFAULT_REQUIREMENTS = os.path.join(DEFAULT_BASE, "Synthetic_data_Product_Requirements.jsonl")
DEFAULT_HA_JSONL = os.path.join(DEFAULT_BASE, "Synthetic_data_Hazard_Analysis.jsonl")
DEFAULT_DVP_JSONL = os.path.join(DEFAULT_BASE, "Synthetic_data_Design_Verification_Protocol.jsonl")
DEFAULT_TM_JSONL  = os.path.join(DEFAULT_BASE, "Synthetic_data_Trace_Matrix.jsonl")

OUTPUT_DIR = os.path.join(DEFAULT_BASE, "streamlit_outputs")
os.makedirs(OUTPUT_DIR, exist_ok=True)

REQ_COLS = ["Requirement ID", "Verification ID", "Requirements"]
HA_COLS  = ["Requirement ID", "Risk ID", "Risk to Health", "HA Risk Control"]
DVP_COLS = ["Verification ID", "Verification Method", "Acceptance Criteria", "Sample Size"]
TM_OUT_COLS = [
    "Verification ID",
    "Requirement ID",
    "Requirements",
    "Risk ID(s)",
    "Risk to Health",
    "HA Risk Control(s)",
    "Verification Method",
    "Acceptance Criteria",
]

TBD = "TBD - Human / SME input"

# ------------------------------
# Google Drive API helpers (for Streamlit Cloud persistence)
# ------------------------------

def init_drive_from_secrets() -> t.Optional[GoogleDrive]:
    """Initialize Drive using Streamlit secrets.
    Secrets expected (set in Streamlit Cloud > App secrets):
      - GDRIVE_CLIENT_CONFIG (JSON for OAuth client config) OR SERVICE_ACCOUNT_JSON
      - GDRIVE_CREDENTIALS (stored credentials JSON, optional; refreshed on runtime)  
    Prefer service account for server-to-server.
    """
    if not _HAS_PYDRIVE2:
        return None
    gauth = GoogleAuth()
    # Service account path from secrets (writes to a temp file)
    svc_json = st.secrets.get("SERVICE_ACCOUNT_JSON", None)
    client_cfg = st.secrets.get("GDRIVE_CLIENT_CONFIG", None)

    if svc_json:
        svc_path = os.path.join(".secrets", "svc.json")
        os.makedirs(".secrets", exist_ok=True)
        with open(svc_path, "w", encoding="utf-8") as f:
            f.write(svc_json)
        gauth.LoadServiceConfig()
        gauth.ServiceAuth(svc_path)
    elif client_cfg:
        cfg_path = os.path.join(".secrets", "client_secrets.json")
        os.makedirs(".secrets", exist_ok=True)
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(client_cfg)
        gauth.LoadClientConfigFile(cfg_path)
        gauth.LocalWebserverAuth()  # first run requires manual auth
    else:
        return None

    return GoogleDrive(gauth)


def drive_upload_bytes(drive: GoogleDrive, folder_id: str, filename: str, data: bytes) -> str:
    file = drive.CreateFile({"title": filename, "parents": [{"id": folder_id}]})
    file.content = io.BytesIO(data)
    file.Upload()
    return file["id"]


def drive_download_jsonl_by_name(drive: GoogleDrive, folder_id: str, name: str) -> t.Optional[pd.DataFrame]:
    q = f"title = '{name}' and '{folder_id}' in parents and trashed = false"
    files = drive.ListFile({'q': q}).GetList()
    if not files:
        return None
    fh = io.BytesIO()
    files[0].GetContentFile("/tmp/tmp.jsonl")
    with open("/tmp/tmp.jsonl", "r", encoding="utf-8") as f:
        rows = [json.loads(ln) for ln in f if ln.strip()]
    return pd.DataFrame(rows)

# ------------------------------
# Helpers
# ------------------------------

def read_jsonl(path: str) -> pd.DataFrame:
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return pd.DataFrame(rows)


def normalize_requirements(df: pd.DataFrame) -> pd.DataFrame:
    rename_map = {}
    for c in df.columns:
        lc = c.strip().lower()
        if lc in {"requirement id", "req id", "requirement_id"}:
            rename_map[c] = "Requirement ID"
        elif lc in {"verification id", "verification_id", "verif id"}:
            rename_map[c] = "Verification ID"
        elif lc in {"requirement", "requirements", "requirement text", "requirement_desc"}:
            rename_map[c] = "Requirements"
    df = df.rename(columns=rename_map)
    for col in REQ_COLS:
        if col not in df.columns:
            df[col] = None
    return df[REQ_COLS].copy()


def agg_unique(series: pd.Series) -> str:
    vals = [str(v).strip() for v in series.dropna().astype(str) if str(v).strip() and str(v).strip().upper() != "NA"]
    uniq = sorted(set(vals), key=lambda x: vals.index(x))
    return ", ".join(uniq) if uniq else "NA"


def detect_heading(row: pd.Series) -> bool:
    req_text = str(row.get("Requirements", "")).strip()
    v_id = str(row.get("Verification ID", "")).strip()
    heading_candidates = {
        "functional requirements", "performance requirements", "environmental requirements",
        "safety requirements", "usability requirements", "design inputs", "general requirements"
    }
    if not v_id or v_id.upper() == "NA":
        return True
    if req_text.lower() in heading_candidates:
        return True
    return False


def load_sources_local(req_path: str, ha_path: t.Optional[str], dvp_path: t.Optional[str]) -> t.Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if req_path.lower().endswith(".jsonl"):
        req_df = read_jsonl(req_path)
    else:
        req_df = pd.read_excel(req_path)
    req_df = normalize_requirements(req_df)

    ha_df = read_jsonl(ha_path) if ha_path and os.path.exists(ha_path) else pd.DataFrame(columns=HA_COLS)
    dvp_df = read_jsonl(dvp_path) if dvp_path and os.path.exists(dvp_path) else pd.DataFrame(columns=DVP_COLS)
    return req_df, ha_df, dvp_df


def rollup_ha(ha_df: pd.DataFrame) -> pd.DataFrame:
    if ha_df.empty:
        return pd.DataFrame(columns=["Requirement ID", "Risk ID(s)", "Risk to Health", "HA Risk Control(s)"])
    rename_map = {}
    for c in ha_df.columns:
        lc = c.strip().lower()
        if lc in {"risk id", "risk_id"}: rename_map[c] = "Risk ID"
        elif lc in {"risk to health", "risk_to_health"}: rename_map[c] = "Risk to Health"
        elif lc in {"ha risk control", "risk control", "risk_controls"}: rename_map[c] = "HA Risk Control"
        elif lc in {"requirement id", "requirement_id"}: rename_map[c] = "Requirement ID"
    ha_df = ha_df.rename(columns=rename_map)
    for m in ["Requirement ID", "Risk ID", "Risk to Health", "HA Risk Control"]:
        if m not in ha_df.columns: ha_df[m] = None
    grouped = (
        ha_df.groupby("Requirement ID", dropna=False)
             .agg({"Risk ID": agg_unique, "Risk to Health": agg_unique, "HA Risk Control": agg_unique})
             .reset_index()
             .rename(columns={"Risk ID": "Risk ID(s)", "HA Risk Control": "HA Risk Control(s)"})
    )
    return grouped


def merge_trace_matrix(req_df: pd.DataFrame, ha_rollup: pd.DataFrame, dvp_df: pd.DataFrame) -> pd.DataFrame:
    rename_map = {}
    for c in dvp_df.columns:
        lc = c.strip().lower()
        if lc in {"verification id", "verification_id"}: rename_map[c] = "Verification ID"
        elif lc in {"verification method", "method"}: rename_map[c] = "Verification Method"
        elif lc in {"acceptance criteria", "criteria"}: rename_map[c] = "Acceptance Criteria"
    dvp_df = dvp_df.rename(columns=rename_map)
    for col in ["Verification ID", "Verification Method", "Acceptance Criteria"]:
        if col not in dvp_df.columns: dvp_df[col] = None

    merged = pd.merge(req_df, ha_rollup, on="Requirement ID", how="left")
    merged = pd.merge(merged, dvp_df[["Verification ID", "Verification Method", "Acceptance Criteria"]], on="Verification ID", how="left")

    heading_mask = merged.apply(detect_heading, axis=1)
    for col in ["Risk ID(s)", "Risk to Health", "HA Risk Control(s)", "Verification Method", "Acceptance Criteria"]:
        merged.loc[heading_mask, col] = "NA"

    merged = merged.fillna(TBD)
    for c in TM_OUT_COLS:
        if c not in merged.columns: merged[c] = TBD
    return merged[TM_OUT_COLS]

# ------------------------------
# Guardrails & Validation
# ------------------------------

def basic_guardrails(tm_df: pd.DataFrame, allowed_methods: t.Set[str]) -> pd.DataFrame:
    """Return a DataFrame of issues with columns: row_index, column, issue."""
    issues = []
    for i, row in tm_df.iterrows():
        # Required fields presence
        for col in ["Verification ID", "Requirement ID", "Requirements"]:
            if not str(row[col]).strip() or row[col] == TBD:
                issues.append((i, col, "Missing required field"))
        # Allowed verification methods (if not NA/TBD)
        vm = str(row["Verification Method"]).strip()
        if vm not in {"NA", TBD} and allowed_methods and vm not in allowed_methods:
            issues.append((i, "Verification Method", f"Method '{vm}' not in allowlist"))
        # Acceptance criteria minimal sanity
        ac = str(row["Acceptance Criteria"]).strip()
        if ac not in {"NA", TBD} and len(ac) < 10:
            issues.append((i, "Acceptance Criteria", "Criteria too short (<10 chars)"))
    return pd.DataFrame(issues, columns=["row_index", "column", "issue"]) if issues else pd.DataFrame(columns=["row_index", "column", "issue"]) 


def llm_validate_rows(tm_df: pd.DataFrame, max_rows: int = 50) -> pd.DataFrame:
    """Use an LLM to critique questionable rows. Requires OPENAI_API_KEY in secrets.
    Returns DataFrame with columns: row_index, column, issue, suggestion.
    """
    if not _HAS_OPENAI or "OPENAI_API_KEY" not in st.secrets:
        return pd.DataFrame(columns=["row_index", "column", "issue", "suggestion"]) 

    openai.api_key = st.secrets["OPENAI_API_KEY"]

    flagged = []
    sample = tm_df.head(max_rows)
    for i, row in sample.iterrows():
        prompt = (
            "You are validating a medical device Traceability Matrix row for an infusion pump.
"
            "Return a JSON with keys: issues (list of strings) and suggestions (object mapping column->string).
"
            f"Row: {json.dumps(row.to_dict(), ensure_ascii=False)}
"
            "Rules: Ensure Verification Method is appropriate for the requirement, Acceptance Criteria are measurable,
"
            "and HA Risk Control(s) align with Risks to Health. If NA/TBD, suggest a concise improvement."
        )
        try:
            resp = openai.chat.completions.create(model=st.secrets.get("OPENAI_MODEL", "gpt-4o-mini"),
                                                  messages=[{"role":"system","content":"You are a strict validator."},
                                                            {"role":"user","content":prompt}],
                                                  temperature=0.0)
            text = resp.choices[0].message.content
            data = json.loads(text) if text.strip().startswith("{") else {"issues": [text], "suggestions": {}}
            for issue in data.get("issues", []):
                flagged.append({"row_index": i, "column": "*", "issue": issue, "suggestion": data.get("suggestions", {})})
        except Exception as e:
            flagged.append({"row_index": i, "column": "*", "issue": f"LLM error: {e}", "suggestion": {}})
    return pd.DataFrame(flagged)

# ------------------------------
# Streamlit UI
# ------------------------------

st.set_page_config(page_title="DHF Automation – Infusion Pump", layout="wide")
st.title("🧩 DHF Automation Pipeline – Infusion Pump")
st.caption("Inputs → Hazard Analysis → DVP → Trace Matrix | Guardrails, Validation LLM, HITL | Exports: Excel & Drive upload")

with st.sidebar:
    st.header("Configuration")

    base_dir = st.text_input("Base directory", value=DEFAULT_BASE, help="Location of your Dissertation folder.")

    use_drive = st.toggle("Use Google Drive API (Streamlit Cloud)", value=False, help="Enable to fetch/save files from a Drive folder ID using pydrive2.")
    drive_folder_id = st.text_input("Drive Folder ID", value="", help="Target folder containing/receiving files when Drive API is enabled.")

    st.markdown("**Source files (fallbacks)**")
    req_path_default = os.path.join(base_dir, os.path.basename(DEFAULT_REQUIREMENTS))
    ha_path_default  = os.path.join(base_dir, os.path.basename(DEFAULT_HA_JSONL))
    dvp_path_default = os.path.join(base_dir, os.path.basename(DEFAULT_DVP_JSONL))

    req_fallback = st.text_input("Requirements JSONL (fallback)", value=req_path_default)
    ha_fallback  = st.text_input("Hazard Analysis JSONL (fallback)", value=ha_path_default)
    dvp_fallback = st.text_input("DVP JSONL (fallback)", value=dvp_path_default)

    st.divider()
    st.markdown("**Uploads (override fallbacks)**")
    uploaded_req = st.file_uploader("Upload Product Requirements (Excel or JSONL)", type=["xlsx", "xls", "jsonl"])  
    uploaded_ha  = st.file_uploader("Upload Hazard Analysis (JSONL)", type=["jsonl"])  
    uploaded_dvp = st.file_uploader("Upload DVP (JSONL)", type=["jsonl"])              

    st.divider()
    st.markdown("**Guardrails**")
    methods_text = st.text_input("Allowed Verification Methods (comma-separated)", value="Physical Testing, Physical Inspection, Visual Inspection")
    allowed_methods = {m.strip() for m in methods_text.split(',') if m.strip()}

    enable_llm = st.toggle("Enable Validation LLM (OpenAI)", value=False, help="Requires OPENAI_API_KEY in secrets; validates a sample of rows.")

    st.divider()
    st.markdown("**Output directory (local runs)**")
    out_dir_input = st.text_input("Output dir", value=OUTPUT_DIR)
    if out_dir_input:
        os.makedirs(out_dir_input, exist_ok=True)
        global OUTPUT_DIR
        OUTPUT_DIR = out_dir_input

st.subheader("1) Provide Product Requirements")
col1, col2 = st.columns(2)
with col1:
    st.write("**Option A:** Upload Excel/JSONL in the sidebar (recommended).")
with col2:
    st.write("**Option B:** Paste requirements as CSV-like text.")
    sample = "Requirement ID,Verification ID,Requirements
REQ-001,VER-001,The pump shall ..."
    pasted_text = st.text_area("Paste requirements (CSV headers required)", value="", height=150, placeholder=sample)

run_btn = st.button("▶️ Run Pipeline & Validate", type="primary")

if run_btn:
    drive = None
    if use_drive:
        if not _HAS_PYDRIVE2:
            st.error("pydrive2 not installed. Add it to requirements.txt.")
        else:
            drive = init_drive_from_secrets()
            if not drive:
                st.error("Drive initialization failed. Check Streamlit secrets.")

    with st.spinner("Loading sources and normalizing..."):
        # Resolve Requirements
        if uploaded_req is not None:
            if uploaded_req.name.lower().endswith(".jsonl"):
                req_bytes = uploaded_req.read().decode("utf-8").splitlines()
                req_rows = [json.loads(ln) for ln in req_bytes if ln.strip()]
                req_df = pd.DataFrame(req_rows)
            else:
                req_df = pd.read_excel(uploaded_req)
        elif pasted_text.strip():
            req_df = pd.read_csv(io.StringIO(pasted_text))
        else:
            if use_drive and drive and drive_folder_id:
                req_df = drive_download_jsonl_by_name(drive, drive_folder_id, os.path.basename(DEFAULT_REQUIREMENTS)) or pd.DataFrame()
                if req_df.empty: req_df, _, _ = load_sources_local(req_fallback, None, None)
            else:
                req_df, _, _ = load_sources_local(req_fallback, None, None)
        req_df = normalize_requirements(req_df)

        # Resolve HA
        if uploaded_ha is not None:
            ha_rows = [json.loads(ln) for ln in uploaded_ha.read().decode("utf-8").splitlines() if ln.strip()]
            ha_df = pd.DataFrame(ha_rows)
        else:
            if use_drive and drive and drive_folder_id:
                ha_df = drive_download_jsonl_by_name(drive, drive_folder_id, os.path.basename(DEFAULT_HA_JSONL)) or pd.DataFrame(columns=HA_COLS)
            else:
                ha_df = read_jsonl(ha_fallback) if os.path.exists(ha_fallback) else pd.DataFrame(columns=HA_COLS)

        # Resolve DVP
        if uploaded_dvp is not None:
            dvp_rows = [json.loads(ln) for ln in uploaded_dvp.read().decode("utf-8").splitlines() if ln.strip()]
            dvp_df = pd.DataFrame(dvp_rows)
        else:
            if use_drive and drive and drive_folder_id:
                dvp_df = drive_download_jsonl_by_name(drive, drive_folder_id, os.path.basename(DEFAULT_DVP_JSONL)) or pd.DataFrame(columns=DVP_COLS)
            else:
                dvp_df = read_jsonl(dvp_fallback) if os.path.exists(dvp_fallback) else pd.DataFrame(columns=DVP_COLS)

        ha_roll = rollup_ha(ha_df)
        tm_df = merge_trace_matrix(req_df, ha_roll, dvp_df)

    st.success("Sources loaded and merged.")

    # ---------------- Guardrails & Validation ----------------
    st.subheader("2) Guardrails & Validation")
    basic_issues = basic_guardrails(tm_df, allowed_methods)
    st.write(f"**Basic guardrails issues found:** {len(basic_issues)}")
    if not basic_issues.empty:
        st.dataframe(basic_issues, use_container_width=True)

    llm_issues = pd.DataFrame()
    if enable_llm:
        with st.spinner("Running Validation LLM on a sample of rows..."):
            llm_issues = llm_validate_rows(tm_df, max_rows=50)
        st.write(f"**Validation LLM issues found:** {len(llm_issues)}")
        if not llm_issues.empty:
            st.dataframe(llm_issues, use_container_width=True)

    # ---------------- Human-in-the-Loop ----------------
    st.subheader("3) Human-in-the-Loop Review")
    flagged_rows = set(basic_issues["row_index"].tolist()) | set(llm_issues["row_index"].tolist())
    hitl_df = tm_df.copy()
    if flagged_rows:
        hitl_df["_FLAGGED"] = hitl_df.index.isin(flagged_rows).map({True: "⚠️", False: ""})
        st.info("Edit the flagged rows below, then click Save.")
    edited = st.experimental_data_editor(hitl_df, use_container_width=True, num_rows="dynamic")

    if st.button("💾 Save Edits (apply to Trace Matrix)"):
        tm_df = edited.drop(columns=["_FLAGGED"], errors="ignore")
        st.success("Edits applied.")

    # ---------------- Exports ----------------
    st.subheader("4) Exports")

    def df_to_excel_bytes(df: pd.DataFrame) -> bytes:
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            df.to_excel(writer, index=False)
        buffer.seek(0)
        return buffer.read()

    # Prepare HA & DVP export previews
    ha_export = ha_roll.rename(columns={"Risk ID(s)": "Risk ID(s)", "Risk to Health": "Risk to Health", "HA Risk Control(s)": "HA Risk Control(s)"})
    dvp_norm = dvp_df[["Verification ID", "Verification Method", "Acceptance Criteria"]].copy() if not dvp_df.empty else pd.DataFrame(columns=["Verification ID", "Verification Method", "Acceptance Criteria"])
    dvp_norm = dvp_norm.fillna(TBD)

    tm_bytes  = df_to_excel_bytes(tm_df)
    ha_bytes  = df_to_excel_bytes(ha_export)
    dvp_bytes = df_to_excel_bytes(dvp_norm)

    # Save locally
    tm_path  = os.path.join(OUTPUT_DIR, "Trace_Matrix.xlsx"); open(tm_path, "wb").write(tm_bytes)
    ha_path  = os.path.join(OUTPUT_DIR, "Hazard_Analysis.xlsx"); open(ha_path, "wb").write(ha_bytes)
    dvp_path = os.path.join(OUTPUT_DIR, "Design_Verification_Protocol.xlsx"); open(dvp_path, "wb").write(dvp_bytes)

    colA, colB, colC = st.columns(3)
    with colA:
        st.download_button("⬇️ Trace_Matrix.xlsx", data=tm_bytes, file_name="Trace_Matrix.xlsx")
    with colB:
        st.download_button("⬇️ Hazard_Analysis.xlsx", data=ha_bytes, file_name="Hazard_Analysis.xlsx")
    with colC:
        st.download_button("⬇️ DVP.xlsx", data=dvp_bytes, file_name="Design_Verification_Protocol.xlsx")

    if use_drive and drive and drive_folder_id:
        with st.spinner("Uploading outputs to Google Drive..."):
            tm_id = drive_upload_bytes(drive, drive_folder_id, "Trace_Matrix.xlsx", tm_bytes)
            ha_id = drive_upload_bytes(drive, drive_folder_id, "Hazard_Analysis.xlsx", ha_bytes)
            dvp_id = drive_upload_bytes(drive, drive_folder_id, "Design_Verification_Protocol.xlsx", dvp_bytes)
        st.success("Uploaded to Drive.")
        st.write({"Trace_Matrix.xlsx": tm_id, "Hazard_Analysis.xlsx": ha_id, "Design_Verification_Protocol.xlsx": dvp_id})

st.markdown("---")
st.subheader("Directory Structure & Hosting Guidance")
st.markdown(
    f"""
**Recommended structure (Colab/local):**
```
{DEFAULT_BASE if is_colab_drive_available() else './Dissertation'}/
├── Synthetic_data_Product_Requirements.jsonl
├── Synthetic_data_Hazard_Analysis.jsonl
├── Synthetic_data_Design_Verification_Protocol.jsonl
├── Synthetic_data_Trace_Matrix.jsonl   # optional GT
├── mistral_finetuned_Hazard_Analysis/
├── mistral_finetuned_Design_Verification_Protocol/
├── mistral_finetuned_Trace_Matrix/
└── streamlit_outputs/
```

**Streamlit Cloud setup:**
- Add **pydrive2** and **openai** to requirements.txt (plus pandas, openpyxl, streamlit).
- Set **App secrets**:
  - `SERVICE_ACCOUNT_JSON` **or** `GDRIVE_CLIENT_CONFIG` (OAuth client JSON)
  - `OPENAI_API_KEY` (optional, for Validation LLM)
  - `OPENAI_MODEL` (optional, default `gpt-4o-mini`)
- In the sidebar, toggle **Use Google Drive API** and paste your **Drive Folder ID** where the app should read/write artifacts.

**Guardrails & HITL:**
- Basic rule checks (required fields, allowlisted Verification Methods, minimal criteria length)
- Optional Validation LLM to critique rows and propose suggestions
- Human-in-the-Loop editable table for fixing flagged rows before export
- All unresolved/missing values remain `{TBD}` in exports
"""
)
