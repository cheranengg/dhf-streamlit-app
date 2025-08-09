import io, os, json
from typing import Optional
try:
    from pydrive2.auth import GoogleAuth
    from pydrive2.drive import GoogleDrive
    _HAS = True
except Exception:
    _HAS = False

def init_drive(service_account_json: str) -> Optional["GoogleDrive"]:
    if not _HAS or not service_account_json:
        return None
    os.makedirs(".secrets", exist_ok=True)
    svc_path = os.path.join(".secrets", "service_account.json")
    with open(svc_path, "w", encoding="utf-8") as f:
        f.write(service_account_json)
    gauth = GoogleAuth()
    # Preferred helper in newer pydrive2
    try:
        gauth.LoadServiceAccountCredentials(svc_path)
    except Exception:
        gauth.settings.update({
            'client_config_backend': 'service',
            'service_config': {
                'client_json_file_path': svc_path,
                'client_user_email': json.loads(service_account_json).get('client_email', ''),
            },
            'oauth_scope': ['https://www.googleapis.com/auth/drive']
        })
        gauth.ServiceAuth()
    return GoogleDrive(gauth)

def upload_bytes(drive: "GoogleDrive", folder_id: str, name: str, data: bytes) -> str:
    f = drive.CreateFile({"title": name, "parents": [{"id": folder_id}]})
    f.content = io.BytesIO(data)
    f.Upload()
    return f["id"]
