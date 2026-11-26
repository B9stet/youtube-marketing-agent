"""Provider adapters for the local prototype; credentials stay server-side."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
from threading import Lock
from typing import Any

import httpx

from .drafts import _response_text
from .natural_comment import JEV_QUESTIONS, _signal


class ProviderError(RuntimeError):
    pass


_credential_lock = Lock()
_session_credentials: dict[str, str] = {}


def set_session_credential(key: str) -> None:
    if not isinstance(key, str) or not key.strip() or len(key) > 1024 or any(c in key for c in "\r\n"):
        raise ValueError("API 키를 확인해 주세요.")
    with _credential_lock:
        _session_credentials["openai"] = key.strip()


def clear_session_credential() -> None:
    with _credential_lock:
        _session_credentials.pop("openai", None)


def credential_status() -> dict:
    with _credential_lock:
        in_session = bool(_session_credentials.get("openai"))
    return {"openai": "session" if in_session else "environment" if os.environ.get("OPENAI_API_KEY") else "missing"}


def _openai_key() -> str:
    with _credential_lock:
        return _session_credentials.get("openai", "") or os.environ.get("OPENAI_API_KEY", "")


def _json_response(text: str) -> dict:
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip(), flags=re.I)
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise ValueError("AI 응답이 JSON 객체가 아닙니다.")
    return value


def _vertex_token() -> str:
    try:
        import google.auth
        from google.auth.transport.requests import Request
    except ImportError as exc:
        raise ProviderError("GCP 연결에 필요한 google-auth 패키지가 설치되지 않았습니다.") from exc
    try:
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        credentials.refresh(Request())
        if not credentials.token:
            raise ValueError("인증 토큰을 가져오지 못했습니다.")
        return credentials.token
    except Exception as exc:
        raise ProviderError("GCP 기본 인증 정보를 확인해 주세요.") from exc


def generate_json(settings: dict, prompt: str, material: dict) -> dict:
    """Generate one JSON object through the selected provider, without fallback."""
    provider = settings["generation_provider"]
    model = settings["generation_model"].strip()
    if not model:
        raise ProviderError("선택한 생성 연결의 모델명을 입력해 주세요.")
    user_input = json.dumps(material, ensure_ascii=False)
    try:
        with httpx.Client(timeout=160, trust_env=provider != "local") as client:
            if provider in ("local", "openai"):
                if provider == "local":
                    url = "http://127.0.0.1:10100/v1/responses"
                    headers = {}
                else:
                    key = _openai_key()
                    if not key:
                        raise ProviderError("OpenAI API 인증 정보가 설정되지 않았습니다.")
                    url = "https://api.openai.com/v1/responses"
                    headers = {"Authorization": f"Bearer {key}"}
                payload = {"model": model, "input": [{"role": "system", "content": prompt},
                    {"role": "user", "content": user_input}], "max_output_tokens": 6500, "store": False}
                response = client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()
                result = _json_response(_response_text(data))
                return {"answer": result, "model": data.get("model", model), "usage": data.get("usage", {})}

            if provider != "gcp":
                raise ProviderError("지원하지 않는 생성 연결입니다.")
            project = settings.get("gcp_project", "").strip() or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
            region = settings.get("gcp_region", "").strip() or "global"
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{1,127}", project or ""):
                raise ProviderError("GCP 프로젝트 ID를 입력해 주세요.")
            if not re.fullmatch(r"[a-z0-9-]{2,40}", region):
                raise ProviderError("GCP 리전을 확인해 주세요.")
            if not re.fullmatch(r"[A-Za-z0-9._-]{2,100}", model):
                raise ProviderError("GCP 모델명을 확인해 주세요.")
            host = "aiplatform.googleapis.com" if region == "global" else f"{region}-aiplatform.googleapis.com"
            url = f"https://{host}/v1/projects/{project}/locations/{region}/publishers/google/models/{model}:generateContent"
            payload = {"systemInstruction": {"parts": [{"text": prompt}]},
                "contents": [{"role": "user", "parts": [{"text": user_input}]}],
                "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 6500}}
            response = client.post(url, headers={"Authorization": f"Bearer {_vertex_token()}"}, json=payload)
            response.raise_for_status()
            data = response.json()
            parts = data["candidates"][0]["content"]["parts"]
            raw = "".join(part.get("text", "") for part in parts if not part.get("thought"))
            return {"answer": _json_response(raw), "model": data.get("modelVersion", model), "usage": data.get("usageMetadata", {})}
    except ProviderError:
        raise
    except httpx.HTTPStatusError as exc:
        raise ProviderError(f"선택한 생성 연결이 요청을 처리하지 못했습니다. (HTTP {exc.response.status_code})") from exc
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, RuntimeError) as exc:
        raise ProviderError(f"생성 연결에서 응답을 읽지 못했습니다. ({type(exc).__name__})") from exc


def _existing_jev_key() -> str:
    """Reuse the credential already configured for the previous Jev PoC."""
    with _credential_lock:
        key = os.environ.get("EXPERIENTIAL_API_KEY") or os.environ.get("EXPLABS_API_KEY")
        if key:
            return key
        if os.environ.get("TYPESAFE_BASE_URL", "").rstrip("/") == "https://api.experientiallabs.ai":
            key = os.environ.get("TYPESAFE_API_KEY")
            if key:
                return key
        helper_root = Path(os.environ.get("POC_JEV_HELPER_ROOT", r"C:\.workspace\Projects\typesafe-computer-use-windows"))
        source = helper_root / "typesafe_computer_use" / "credentials.py"
        if not source.is_file():
            raise ProviderError("기존 Jev 인증 정보를 찾지 못했습니다.")
        spec = importlib.util.spec_from_file_location("prototype_jev_credentials", source)
        if spec is None or spec.loader is None:
            raise ProviderError("기존 Jev 설정을 읽지 못했습니다.")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.prepare_provider()
        if os.environ.get("TYPESAFE_BASE_URL", "").rstrip("/") != "https://api.experientiallabs.ai":
            raise ProviderError("기존 Jev 연결 주소를 확인해 주세요.")
        key = os.environ.get("TYPESAFE_API_KEY", "")
        if not key:
            raise ProviderError("기존 Jev 인증 정보를 읽지 못했습니다.")
        return key


def diagnose(settings: dict, case: dict, comment: str, others: list[str]) -> dict:
    provider = settings["jev_provider"]
    if provider == "existing":
        key, url = _existing_jev_key(), "https://api.experientiallabs.ai/v1/systemone"
    elif provider == "official":
        # Never send the gateway key to the official endpoint.
        key, url = os.environ.get("JEV_OFFICIAL_API_KEY", ""), "https://api.typesafe.ai/v1/systemone"
        if not key:
            raise ProviderError("공식 Jev 인증 정보가 설정되지 않았습니다.")
    else:
        raise ProviderError("지원하지 않는 Jev 연결입니다.")
    model = settings["jev_model"].strip()
    if not model:
        raise ProviderError("선택한 Jev 모델명을 입력해 주세요.")
    state = {name: case.get(name, "") for name in ("scene", "scene_quote", "stance", "persona", "voice", "reference_comments")}
    state.update({"comment": comment, "other_comments": others})
    payload = {"model": model, "state": state,
        "questions": {name: {"type": "noul", "instructions": question} for name, question in JEV_QUESTIONS.items()}}
    try:
        # A timeout can still be charged; do not retry an uncertain request.
        with httpx.Client(timeout=60) as client:
            response = client.post(url, headers={"Authorization": f"Bearer {key}"}, json=payload)
            response.raise_for_status()
            data = response.json()
        return {"scores": {name: _signal(data["answers"][name]["noul"]) for name in JEV_QUESTIONS},
            "model": data.get("model", model), "usage": data.get("usage", {})}
    except httpx.HTTPStatusError as exc:
        raise ProviderError(f"Jev 진단 요청에 실패했습니다. (HTTP {exc.response.status_code})") from exc
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise ProviderError(f"Jev 진단 결과를 읽지 못했습니다. ({type(exc).__name__})") from exc
