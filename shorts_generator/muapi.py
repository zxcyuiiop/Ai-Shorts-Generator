"""Thin MuAPI client: submit a job, poll until it finishes, return the result."""
import time
from typing import Any, Dict, Optional

import requests

from .config import (
    MUAPI_BASE_URL,
    POLL_INTERVAL_SECONDS,
    POLL_TIMEOUT_SECONDS,
    require_api_key,
)


class MuAPIError(RuntimeError):
    pass


def _headers() -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "x-api-key": require_api_key(),
    }


def submit(endpoint: str, payload: Dict[str, Any]) -> str:
    """POST to /api/v1/{endpoint} and return the request_id."""
    url = f"{MUAPI_BASE_URL}/{endpoint.lstrip('/')}"
    resp = requests.post(url, json=payload, headers=_headers(), timeout=60)
    if resp.status_code >= 400:
        raise MuAPIError(f"{endpoint} submit failed [{resp.status_code}]: {resp.text}")
    data = resp.json()
    request_id = data.get("request_id") or data.get("id")
    if not request_id:
        raise MuAPIError(f"{endpoint} response had no request_id: {data}")
    return str(request_id)


def fetch_result(request_id: str) -> Dict[str, Any]:
    """GET the latest result for a request_id."""
    url = f"{MUAPI_BASE_URL}/predictions/{request_id}/result"
    resp = requests.get(url, headers=_headers(), timeout=30)
    if resp.status_code >= 400:
        raise MuAPIError(f"poll failed [{resp.status_code}]: {resp.text}")
    return resp.json()


def poll(
    request_id: str,
    interval: float = POLL_INTERVAL_SECONDS,
    timeout: float = POLL_TIMEOUT_SECONDS,
    label: Optional[str] = None,
) -> Dict[str, Any]:
    """Block until the prediction is done; return the final payload."""
    deadline = time.time() + timeout
    last_status = None
    while time.time() < deadline:
        data = fetch_result(request_id)
        status = (data.get("status") or "").lower()
        if status and status != last_status:
            print(f"[muapi] {label or request_id}: {status}", flush=True)
            last_status = status

        if status in ("completed", "succeeded", "success"):
            return data
        if status in ("failed", "error"):
            raise MuAPIError(f"{label or request_id} failed: {data}")

        time.sleep(interval)

    raise MuAPIError(f"{label or request_id} timed out after {timeout}s")


def run(endpoint: str, payload: Dict[str, Any], label: Optional[str] = None) -> Dict[str, Any]:
    """Submit then poll. Returns the final result payload."""
    request_id = submit(endpoint, payload)
    return poll(request_id, label=label or endpoint)
