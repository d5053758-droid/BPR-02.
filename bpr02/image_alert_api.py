"""Mobile image-upload endpoint: inspect an actual chart image, then report it to WhatsApp.

Unlike a text-only webhook, the send route requires actual image bytes encoded in a
validated data URL. The service uses OpenAI's Responses API for visual analysis and
sends the analysis only after that call succeeds. It has no trading execution code.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

from .whatsapp_alerts import WhatsAppDeliveryError, WhatsAppTemplateNotifier

MAX_REQUEST_BYTES = 6 * 1024 * 1024
MAX_IMAGE_BYTES = 4 * 1024 * 1024
ALLOWED_SIGNALS = {"CALL", "PUT", "NO TRADE"}
_IMAGE_DATA_URL_RE = re.compile(r"^data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=]+)$")
DEFAULT_VISION_MODEL = "gpt-6.1-terra"

_SYSTEM_PROMPT = """Eres un analista visual prudente de capturas de gráficos para BPR-02.
Analiza solamente la imagen adjunta; no tienes precios en vivo ni acceso al broker.
No prometas resultados ni presentes el análisis como una certeza o una orden.
Devuelve solo un objeto JSON con estas claves: is_chart (boolean), symbol, timeframe,
signal, reason, entry_trigger, expiry, confirmation e invalidation (las otras son strings).
Si la imagen NO es claramente un gráfico de mercado, is_chart=false. Si sí parece un
gráfico pero está borroso, faltan etiquetas o la estructura/confirmación no es evidente,
is_chart=true y signal=NO TRADE, explicando la limitación. Cuando is_chart=false, el
servicio NO enviará WhatsApp.
No inventes precio actual, niveles exactos, temporalidad, confirmación ni expiración.
Usa solo la información legible; si algo no se ve, escribe "No legible en la captura".
Explica en español, brevemente. entry_trigger debe ser condición a verificar, no orden
de ejecución. expiry debe ser estimación prudente solo si el contexto visible la justifica;
en caso contrario, "No se puede estimar solo con esta captura".
"""

_ANALYSIS_FIELDS = (
    "symbol",
    "timeframe",
    "signal",
    "reason",
    "entry_trigger",
    "expiry",
    "confirmation",
    "invalidation",
)


def _decode_image_data_url(value: object) -> tuple[str, bytes]:
    if not isinstance(value, str) or len(value) > MAX_REQUEST_BYTES:
        raise ValueError("image_data_url is required and must be a supported image smaller than 4 MB")
    match = _IMAGE_DATA_URL_RE.fullmatch(value)
    if not match:
        raise ValueError("only base64 PNG, JPEG or WEBP images are accepted")
    mime_type, encoded = match.groups()
    try:
        image = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("image data is not valid base64") from exc
    if not image or len(image) > MAX_IMAGE_BYTES:
        raise ValueError("image must be smaller than 4 MB")
    if mime_type == "image/png" and not image.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("image content does not match PNG format")
    if mime_type == "image/jpeg" and not image.startswith(b"\xff\xd8\xff"):
        raise ValueError("image content does not match JPEG format")
    if mime_type == "image/webp" and not (image.startswith(b"RIFF") and image[8:12] == b"WEBP"):
        raise ValueError("image content does not match WEBP format")
    return mime_type, image


def _extract_output_text(response_body: object) -> str:
    if not isinstance(response_body, dict):
        raise ValueError("OpenAI vision response must be a JSON object")
    top_text = response_body.get("output_text")
    if isinstance(top_text, str) and top_text.strip():
        return top_text.strip()
    output = response_body.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                    text = part.get("text")
                    if isinstance(text, str) and text.strip():
                        return text.strip()
    raise ValueError("OpenAI vision response had no text output")


def analyze_image_with_openai(
    image_data_url: str,
    *,
    api_key: str | None = None,
    model: str | None = None,
    urlopen_fn: Callable[..., object] | None = None,
) -> dict[str, object]:
    """Analyze a supplied image via the official Responses API; image data is never logged."""
    secret = (api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")).strip()
    if not secret:
        raise ValueError("OPENAI_API_KEY must be configured as a secret")
    selected_model = (model or os.environ.get("OPENAI_VISION_MODEL", DEFAULT_VISION_MODEL)).strip()
    if not selected_model or len(selected_model) > 100:
        raise ValueError("OPENAI_VISION_MODEL must be a non-empty model ID")

    # Validate the data URL before making any external request.
    _decode_image_data_url(image_data_url)
    request_body = {
        "model": selected_model,
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": _SYSTEM_PROMPT}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "Analiza esta captura de gráfico y devuelve el JSON solicitado. "
                            "No uses información externa ni supongas que el precio sigue igual."
                        ),
                    },
                    {
                        "type": "input_image",
                        "image_url": image_data_url,
                        "detail": "high",
                    },
                ],
            },
        ],
        "text": {"format": {"type": "json_object"}},
        "max_output_tokens": 700,
        "store": False,
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "BPR-02-image-upload/1.0",
        },
        method="POST",
    )
    opener = urlopen_fn or urllib.request.urlopen
    try:
        with opener(request, timeout=45) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"OpenAI vision API returned HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OpenAI vision API connection failed: {exc.reason}") from None
    except (TimeoutError, OSError):
        raise RuntimeError("OpenAI vision request failed due to a network or timeout error") from None
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, TypeError):
        raise RuntimeError("OpenAI vision API returned an invalid response") from None

    try:
        parsed = json.loads(_extract_output_text(body))
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("OpenAI vision model did not return the required analysis JSON") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("OpenAI vision model output must be a JSON object")

    is_chart = parsed.get("is_chart")
    if not isinstance(is_chart, bool):
        raise RuntimeError("OpenAI vision model did not return a valid is_chart boolean")
    normalized: dict[str, object] = {"is_chart": is_chart}
    for field in _ANALYSIS_FIELDS:
        value = parsed.get(field, "")
        if not isinstance(value, str):
            raise RuntimeError(f"OpenAI vision model returned invalid field: {field}")
        value = value.strip()
        if field in {"symbol", "timeframe", "signal", "reason"} and not value:
            raise RuntimeError(f"OpenAI vision model omitted required field: {field}")
        if len(value) > 350:
            value = value[:350]
        normalized[field] = value
    normalized["signal"] = str(normalized["signal"]).upper()
    if normalized["signal"] not in ALLOWED_SIGNALS:
        raise RuntimeError("OpenAI vision model returned an unsupported signal")
    return normalized


def make_handler(
    *,
    action_token: str,
    notifier: WhatsAppTemplateNotifier,
    vision_analyzer: Callable[[str], dict[str, object]] | None = None,
):
    """Build a handler with injectable dependencies so tests send no real messages."""
    if not action_token or not action_token.strip():
        raise ValueError("BPR02_IMAGE_ALERT_ACTION_TOKEN must be non-empty")

    def analyze(image_data_url: str) -> dict[str, object]:
        if vision_analyzer is not None:
            return vision_analyzer(image_data_url)
        return analyze_image_with_openai(image_data_url)

    class ImageAlertHandler(BaseHTTPRequestHandler):
        server_version = "BPR02ImageUpload/1.0"

        def log_message(self, format: str, *args: Any) -> None:
            # Do not log request bodies, images, bearer tokens, or private chart content.
            return

        def _send_json(self, status: int, body: dict[str, object]) -> None:
            encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:
            if self.path == "/health":
                self._send_json(HTTPStatus.OK, {
                    "status": "ok",
                    "mode": "image_upload_then_whatsapp",
                    "orders_submitted": 0,
                })
                return
            if self.path in {"/", "/index.html"}:
                page = Path(__file__).resolve().parent.parent / "web" / "image_upload.html"
                try:
                    content = page.read_bytes()
                except OSError:
                    self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "upload_page_unavailable"})
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(content)
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def do_POST(self) -> None:
            if self.path != "/v1/analyze-image":
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return

            supplied_header = self.headers.get("Authorization", "")
            prefix = "Bearer "
            supplied_token = (
                supplied_header[len(prefix):].strip()
                if supplied_header.startswith(prefix) else ""
            )
            if not supplied_token or not hmac.compare_digest(supplied_token, action_token.strip()):
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return

            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                self._send_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "content_type_must_be_application_json"})
                return
            raw_length = self.headers.get("Content-Length", "")
            try:
                length = int(raw_length)
            except (TypeError, ValueError):
                self._send_json(HTTPStatus.LENGTH_REQUIRED, {"error": "content_length_required"})
                return
            if length <= 0 or length > MAX_REQUEST_BYTES:
                self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "invalid_content_length"})
                return

            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
                return
            if not isinstance(payload, dict):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "request_body_must_be_object"})
                return
            if payload.get("image_attached") is not True:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "an_actual_image_is_required"})
                return
            try:
                _mime_type, image_bytes = _decode_image_data_url(payload.get("image_data_url"))
                image_data_url = payload["image_data_url"]
                analysis_value = payload.get("analysis_id")
                if not isinstance(analysis_value, str):
                    raise ValueError("analysis_id must be a UUID string")
                analysis_id = str(UUID(analysis_value))
            except (ValueError, KeyError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return

            try:
                analysis = analyze(image_data_url)
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            except Exception:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": "image_analysis_failed_no_whatsapp_sent"})
                return

            # Fail closed even if a model/client adapter returns malformed fields.
            if not isinstance(analysis, dict):
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": "invalid_image_analysis_no_whatsapp_sent"})
                return
            is_chart = analysis.get("is_chart")
            if not isinstance(is_chart, bool):
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": "invalid_image_analysis_no_whatsapp_sent"})
                return
            normalized: dict[str, object] = {"is_chart": is_chart}
            for field in _ANALYSIS_FIELDS:
                value = analysis.get(field, "")
                if not isinstance(value, str) or (field in {"symbol", "timeframe", "signal", "reason"} and not value.strip()):
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"error": "invalid_image_analysis_no_whatsapp_sent"})
                    return
                normalized[field] = value.strip()[:350]
            normalized["signal"] = str(normalized["signal"]).upper()
            if normalized["signal"] not in ALLOWED_SIGNALS:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": "invalid_image_analysis_no_whatsapp_sent"})
                return
            analysis = normalized

            if analysis["is_chart"] is False:
                self._send_json(HTTPStatus.OK, {
                    "status": "image_not_recognized",
                    "analysis_id": analysis_id,
                    **analysis,
                    "whatsapp_status": "not_sent_no_chart_detected",
                    "orders_submitted": 0,
                })
                return

            # Message event ID is tied to the client submission ID. A retry after a
            # successful send is deduplicated using Meta's accepted-message journal.
            image_fingerprint = hashlib.sha256(image_bytes).hexdigest()[:16]
            event: dict[str, object] = {
                **analysis,
                "event_id": analysis_id,
                "event_type": "image_analysis",
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "source": "mobile_web_image_analysis",
                "mode": "research_only",
                "image_sha256_prefix": image_fingerprint,
                "orders_submitted": 0,
            }
            try:
                delivery = notifier.send_event(event)
            except WhatsAppDeliveryError:
                self._send_json(HTTPStatus.BAD_GATEWAY, {
                    "error": "whatsapp_delivery_failed",
                    "analysis": analysis,
                    "analysis_id": analysis_id,
                    "orders_submitted": 0,
                })
                return
            except Exception:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                    "error": "notification_failed",
                    "orders_submitted": 0,
                })
                return

            self._send_json(HTTPStatus.OK, {
                "status": "analysis_complete",
                "analysis_id": analysis_id,
                **analysis,
                "whatsapp_status": delivery.get("status", "unknown"),
                "meta_message_id": delivery.get("message_id"),
                "orders_submitted": 0,
            })

    return ImageAlertHandler


def main() -> int:
    action_token = os.environ.get("BPR02_IMAGE_ALERT_ACTION_TOKEN", "")
    if not action_token.strip():
        print("ERROR: BPR02_IMAGE_ALERT_ACTION_TOKEN must be configured as a secret", file=sys.stderr)
        return 2
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        print("ERROR: OPENAI_API_KEY must be configured as a secret; API usage is billed separately from ChatGPT", file=sys.stderr)
        return 2
    try:
        notifier = WhatsAppTemplateNotifier.from_env()
        port = int(os.environ.get("PORT", "8080"))
        if not 1 <= port <= 65535:
            raise ValueError("PORT must be between 1 and 65535")
    except (ValueError, OSError) as exc:
        print(f"ERROR: invalid image-upload service configuration: {exc}", file=sys.stderr)
        return 2

    handler = make_handler(action_token=action_token, notifier=notifier)
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    print(f"BPR-02 image upload service listening on port {port}; orders_submitted=0", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
