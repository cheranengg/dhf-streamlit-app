# app.py — DHF Streamlit UI (Final Integration)
# -------------------------------------------------
# • Single input: Product Requirements (Excel upload OR pasted CSV text)
# • Calls FastAPI backend (HA → DVP → Trace Matrix) with bearer token
# • Guardrails are always ON (embedded checks); missing/empty => "TBD - Human / SME input"
# • Exports three Excel files: Hazard_Analysis.xlsx, DVP.xlsx, Trace_Matrix.xlsx
# • Persists outputs to Google Drive via service account (pydrive2) using a fixed Folder ID
# • No user-facing toggles for Drive or Guardrails
#
# Required Streamlit secrets (App → Settings → Secrets):
#   BACKEND_URL: https://<your-cloud-run-service>.run.app
#   BACKEND_TOKEN: <your bearer token>
#   SERVICE_ACCOUNT_JSON: <full JSON content of SA key>
#   DRIVE_FOLDER_ID: <Google Drive Folder ID to store outputs>
#
# Optional: OUTPUT_DIR (defaults to ./streamlit_outputs)
#
# Notes
# - Backend must expose POST /hazard-analysis, /dvp, /trace-matrix as per the FastAPI skeleton we provided.
# - For Drive, we use pydrive2 service account login. Folder must be shared with the SA if it’s in My Drive.

import os
import io
import json
import time
import typing as t
import pandas as pd
import streamlit as st
import requests

# --------------- Constants ---------------
TBD = "TBD - Human / SME input"
BACKEND_URL = st.secrets.get("BACKEND_URL", "http://localhost:8080")
BACKEND_TOKEN = st.secrets.get("BACKEND_TOKEN", "dev-token")
DEFAULT_DRIVE_FOLDER_ID = st.secrets.get("DRIVE_FOLDER_ID", "")  # hard-coded via secrets
OUTPUT_DIR = st.secrets.get("OUTPUT_DIR", os.path.abspath("./streamlit_outputs"))
os.makedirs(OUTPUT_DIR, exist_ok=True)

# --------------- Google Drive (pydrive2) ---------------
_HAS_DRIVE = True
try:
    from pydrive2.auth import GoogleAuth
    from pydrive2.drive import GoogleDrive
except Exception:
    _HAS_DRIVE = False


def init_drive() -> t.Optional[GoogleDrive]:
    if not _HAS_DRIVE:
        return None
    svc_json = st.secrets.get("SERVICE_ACCOUNT_JSON")
    if not svc_json:
        return None
    os.makedirs(".secrets", exist_ok=True)
    svc_path = os.path.join(".secrets", "service_account.json")
    with open(svc_path, "w", encoding="utf-8") as f:
        f.write(svc_json)
    gauth = GoogleAuth()
    # pydrive2 SA helper
    try:
        gauth.LoadServiceAccountCredentials(svc_path)
    except Exception:
        # fallback for older versions
        gauth.settings.update({
            'client_config_backend': 'service',
            'service_config': {
                'client_json_file_path': svc_path,
                'client_user_email': json.loads(svc_json).get('client_email', ''),
            },
            'oauth_scope': ['https://www.googleapis.com/auth/drive']
        })
        gauth.ServiceAuth()
    return GoogleDrive(gauth)


def drive_upload_bytes(drive: GoogleDrive, folder_id: str, filename: str, data: bytes) -> str:
    file = drive.CreateFile({"title": filename, "parents": [{"id": folder_id}]})
    bio = io.BytesIO(data)
    file.content = bio
    file.Upload()
    return file["id"]

# --------------- Helpers ---------------

def normalize_requirements(df: pd.DataFrame) -> pd.DataFrame:
    rename_map = {}
    for c in df.columns:
        lc = c.strip().lower()
        if lc in {"requirement id", "req id", "requirement_id"}:
            rename_map[c] = "Requirement ID"
        elif lc in {"verification id", "verification_id", "verif id"}:
            rename_map[c] = "Verification ID"
        elif lc in {"requirement", "requirements", "requirement text", "requirement_desc", "requirement description"}:
            rename_map[c] = "Requirements"
    df = df.rename(columns=rename_map)
    for col in ["Requirement ID", "Verification ID", "Requirements"]:
        if col not in df.columns:
            df[col] = None
    # keep only three canonical cols in order
    return df[["Requirement ID", "Verification ID", "Requirements"]].copy()


def df_to_excel_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False)
    buf.seek(0)
    return buf.read()


def basic_guardrails_df(df: pd.DataFrame, required_cols: t.List[str]) -> pd.DataFrame:
    issues = []
    for i, row in df.iterrows():
        for c in required_cols:
            val = str(row.get(c, "")).strip()
            if not val or val in {"NA", TBD}:
                issues.append((i, c, "Missing or TBD"))
    return pd.DataFrame(issues, columns=["row_index", "column", "issue"]) if issues else pd.DataFrame(columns=["row_index", "column", "issue"]) 


def fill_tbd(df: pd.DataFrame, cols: t.List[str]) -> pd.DataFrame:
    out = df.copy()
    for c in cols:
        if c not in out.columns:
            out[c] = TBD
        out[c] = out[c].fillna(TBD)
        out.loc[out[c].astype(str).str.strip() == "", c] = TBD
    return out


def call_backend(endpoint: str, payload: dict) -> dict:
    url = f"{BACKEND_URL.rstrip('/')}{endpoint}"
    headers = {"Authorization": f"Bearer {BACKEND_TOKEN}", "Content-Type": "application/json"}
    r = requests.post(url, headers=headers, json=payload, timeout=1200)
    if r.status_code >= 400:
        raise RuntimeError(f"Backend error {r.status_code}: {r.text[:500]}")
    return r.json()

# --------------- UI ---------------

st.set_page_config(page_title="DHF Automation – Infusion Pump", layout="wide")
st.title("🧩 DHF Automation – Infusion Pump (Final)")
st.caption("Product Requirements → Hazard Analysis → DVP → Trace Matrix | Guardrails + HITL | Exports to Drive")

st.markdown("**Provide Product Requirements** (choose one):")
colA, colB = st.columns(2)
with colA:
    uploaded = st.file_uploader("Upload Product Requirements (Excel .xlsx)", type=["xlsx", "xls"]) 
with colB:
    sample = "Requirement ID,Verification ID,Requirements\nREQ-001,VER-001,The pump shall ..."
    pasted = st.text_area("Paste as CSV (with headers)", value="", height=140, placeholder=sample)

run_btn = st.button("▶️ Generate DHF Package", type="primary")

if run_btn:
    # -------- Parse Requirements --------
    with st.spinner("Parsing requirements..."):
        if uploaded is not None:
            try:
                req_df = pd.read_excel(uploaded)
            except Exception as e:
                st.error(f"Failed to read Excel: {e}")
                st.stop()
        elif pasted.strip():
            try:
                req_df = pd.read_csv(io.StringIO(pasted))
            except Exception as e:
                st.error(f"Failed to parse pasted CSV: {e}")
                st.stop()
        else:
            st.error("Please upload an Excel file or paste CSV text.")
            st.stop()
        req_df = normalize_requirements(req_df)

    # Show preview
    st.success(f"Loaded {len(req_df)} requirements.")
    st.dataframe(req_df.head(20), use_container_width=True)

    # -------- Call Backend: HA --------
    with st.spinner("Running Hazard Analysis (backend)... this can take a while"):
        ha_payload = {
            "requirements": [
                {
                    "Requirement ID": r["Requirement ID"],
                    "Verification ID": r.get("Verification ID"),
                    "Requirements": r.get("Requirements"),
                }
                for _, r in req_df.iterrows()
            ]
        }
        ha_resp = call_backend("/hazard-analysis", ha_payload)
        ha_rows = ha_resp.get("ha", [])
        ha_df = pd.DataFrame(ha_rows)

    # Guardrail normalize HA
    ha_df = fill_tbd(
        ha_df,
        [
            "requirement_id","risk_id","risk_to_health","hazard","hazardous_situation",
            "harm","sequence_of_events","severity_of_harm","p0","p1","poh","risk_index","risk_control",
        ],
    )

    st.subheader("Hazard Analysis (preview)")
    st.dataframe(ha_df.head(20), use_container_width=True)

    # -------- Call Backend: DVP --------
    with st.spinner("Generating Design Verification Protocol (backend)..."):
        dvp_payload = {
            "requirements": ha_payload["requirements"],
            "ha": ha_rows,
        }
        dvp_resp = call_backend("/dvp", dvp_payload)
        dvp_rows = dvp_resp.get("dvp", [])
        dvp_df = pd.DataFrame(dvp_rows)

    dvp_df = fill_tbd(dvp_df, ["verification_id","verification_method","acceptance_criteria","sample_size","test_procedure"]) 
    st.subheader("Design Verification Protocol (preview)")
    st.dataframe(dvp_df.head(20), use_container_width=True)

    # -------- Call Backend: Trace Matrix --------
    with st.spinner("Building Trace Matrix (backend)..."):
        tm_payload = {
            "requirements": ha_payload["requirements"],
            "ha": ha_rows,
            "dvp": dvp_rows,
        }
        tm_resp = call_backend("/trace-matrix", tm_payload)
        tm_rows = tm_resp.get("trace_matrix", [])
        tm_df = pd.DataFrame(tm_rows)

    # Normalize / Guardrails on TM
    tm_required_cols = [
        "verification_id","requirement_id","requirements",
        "risk_ids","risks_to_health","ha_risk_controls","verification_method","acceptance_criteria",
    ]
    tm_df = fill_tbd(tm_df, tm_required_cols)

    st.subheader("Trace Matrix (preview)")
    st.dataframe(tm_df.head(20), use_container_width=True)

    # -------- HITL quick editor on flagged rows (always on) --------
    st.subheader("Human-in-the-Loop (edit before export)")
    issues = basic_guardrails_df(tm_df, ["verification_id","requirement_id","requirements"]) 
    if not issues.empty:
        st.info(f"Guardrails flagged {len(issues)} issue(s). You can edit the table below.")
    tm_df_edit = st.experimental_data_editor(tm_df, use_container_width=True, num_rows="dynamic")
    if st.button("Apply edits"):
        tm_df = tm_df_edit.copy()
        st.success("Edits applied to Trace Matrix.")

    # -------- Exports --------
    st.subheader("Exports")
    def save_and_get(path: str, data: bytes):
        with open(path, "wb") as f:
            f.write(data)
        return path

    # Prepare clean exports
    ha_export_cols = [
        "requirement_id","risk_id","risk_to_health","hazard","hazardous_situation",
        "harm","sequence_of_events","severity_of_harm","p0","p1","poh","risk_index","risk_control",
    ]
    dvp_export_cols = ["verification_id","verification_method","acceptance_criteria","sample_size","test_procedure"]
    tm_export_cols = [
        "verification_id","requirement_id","requirements",
        "risk_ids","risks_to_health","ha_risk_controls","verification_method","acceptance_criteria",
    ]

    ha_bytes = df_to_excel_bytes(ha_df[ha_export_cols])
    dvp_bytes = df_to_excel_bytes(dvp_df[dvp_export_cols])
    tm_bytes  = df_to_excel_bytes(tm_df[tm_export_cols])

    ha_path = save_and_get(os.path.join(OUTPUT_DIR, "Hazard_Analysis.xlsx"), ha_bytes)
    dvp_path = save_and_get(os.path.join(OUTPUT_DIR, "Design_Verification_Protocol.xlsx"), dvp_bytes)
    tm_path  = save_and_get(os.path.join(OUTPUT_DIR, "Trace_Matrix.xlsx"), tm_bytes)

    col1, col2, col3 = st.columns(3)
    with col1:
        st.download_button("⬇️ Hazard_Analysis.xlsx", data=ha_bytes, file_name="Hazard_Analysis.xlsx")
    with col2:
        st.download_button("⬇️ DVP.xlsx", data=dvp_bytes, file_name="Design_Verification_Protocol.xlsx")
    with col3:
        st.download_button("⬇️ Trace_Matrix.xlsx", data=tm_bytes, file_name="Trace_Matrix.xlsx")

    # -------- Drive Upload (always, if configured) --------
    if DEFAULT_DRIVE_FOLDER_ID:
        with st.spinner("Uploading to Google Drive..."):
            drive = init_drive()
            if drive:
                try:
                    ha_id = drive_upload_bytes(drive, DEFAULT_DRIVE_FOLDER_ID, "Hazard_Analysis.xlsx", ha_bytes)
                    dvp_id = drive_upload_bytes(drive, DEFAULT_DRIVE_FOLDER_ID, "Design_Verification_Protocol.xlsx", dvp_bytes)
                    tm_id  = drive_upload_bytes(drive, DEFAULT_DRIVE_FOLDER_ID, "Trace_Matrix.xlsx", tm_bytes)
                    st.success("Uploaded to Drive.")
                    st.write({
                        "Hazard_Analysis.xlsx": ha_id,
                        "Design_Verification_Protocol.xlsx": dvp_id,
                        "Trace_Matrix.xlsx": tm_id,
                    })
                except Exception as e:
                    st.warning(f"Drive upload failed: {e}")
            else:
                st.info("Drive not initialized (missing SERVICE_ACCOUNT_JSON secret?). Files saved locally only.")

st.markdown("---")
st.markdown(
    f"""
**Files & Hosting**
- Outputs saved locally to: `{OUTPUT_DIR}` and uploaded to Google Drive folder `{DEFAULT_DRIVE_FOLDER_ID or '—'}` when configured.
- Backend is expected at `{BACKEND_URL}` with bearer auth.
- Guardrails are enforced in backend and here (null/empty ⇒ `{TBD}`).
"""
)
