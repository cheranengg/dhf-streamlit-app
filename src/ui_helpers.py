import io, pandas as pd

TBD = "TBD - Human / SME input"

def df_to_excel_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False)
    buf.seek(0)
    return buf.read()

def normalize_requirements(df: pd.DataFrame) -> pd.DataFrame:
    rename = {}
    for c in df.columns:
        lc = c.strip().lower()
        if lc in {"requirement id", "req id", "requirement_id"}:
            rename[c] = "Requirement ID"
        elif lc in {"verification id", "verification_id", "verif id"}:
            rename[c] = "Verification ID"
        elif lc in {"requirement", "requirements", "requirement text", "requirement description"}:
            rename[c] = "Requirements"
    df = df.rename(columns=rename)
    for col in ["Requirement ID", "Verification ID", "Requirements"]:
        if col not in df.columns: df[col] = None
    return df[["Requirement ID", "Verification ID", "Requirements"]].copy()
