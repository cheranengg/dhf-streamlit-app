import requests, os

class BackendClient:
    def __init__(self, base_url: str, token: str, timeout: int = 1200):
        self.base = base_url.rstrip("/")
        self.h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self.timeout = timeout

    def hazard_analysis(self, payload: dict) -> dict:
        return self._post("/hazard-analysis", payload)

    def dvp(self, payload: dict) -> dict:
        return self._post("/dvp", payload)

    def trace_matrix(self, payload: dict) -> dict:
        return self._post("/trace-matrix", payload)

    def _post(self, path: str, data: dict) -> dict:
        r = requests.post(self.base + path, headers=self.h, json=data, timeout=self.timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"{path} -> {r.status_code}: {r.text[:500]}")
        return r.json()
