"""Authenticated endpoint for ChatGPT image-analysis -> WhatsApp alerts.

This endpoint does not receive or inspect chart pixels. A Custom GPT must inspect
an image in its conversation first, then call this action with its analysis.
The API enforces the explicit image_attached flag and a shared action token; the
Custom GPT instructions provide the image-only tool-use rule.
"""
from __future__ import annotations

import hmac
import json
import os
import sys
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .whatsapp_alerts import WhatsAppDeliveryError, WhatsAppTemplateNotifier

MAX_REQUEST_BYTES = 16_384
ALLOWED_SIGNALS = {"CALL", "PUT", "NO TRADE"}
REQUIRED_TEXT_FIELDS = ("analysis_id", "symbol", "timeframe", "signal", "reason")
OPTIONAL_TEXT_FIELDS = ("entry_trigger", "expiry", "confirmation", "invalidation")


def _validate_image_analysis(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    if payload.get("image_attached") is not True:
        raise ValueError("WhatsApp alerts require image_attached=true")
    if payload.get("analysis_source") != "chatgpt_image_analysis":
        raise ValueError("analysis_source must be chatgpt_image_analysis")

    result: dict[str, object] = {
        "event_type": "image_analysis",
        "source": "chatgpt_image_analysis",
        "mode": "research_only",
        "orders_submitted": 0,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    for field in REQUIRED_TEXT_FIELDS:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string")
        value = value.strip()
        max_length = 128 if field == "analysis_id" else (16 if field == "signal" else 220)
        if len(value) > max_length:
            raise ValueError(f"{field} exceeds maximum length {max_length}")
        result["event_id" if field == "analysis_id" else field] = value

    signal = str(result["signal"]).strip().upper()
    if signal not in ALLOWED_SIGNALS:
        raise ValueError("signal must be CALL, PUT, or NO TRADE")
    result["signal"] = signal
    for field in OPTIONAL_TEXT_FIELDS:
        value = payload.get(field, "")
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise ValueError(f"{field} must be a string when supplied")
        value = value.strip()
        if len(value) > 350:
            raise ValueError(f"{field} exceeds maximum length 350")
        if value:
            result[field] = value
    return result


def make_handler(*, action_token: str, notifier: WhatsAppTemplateNotifier):
    """Build a handler with explicit dependencies so tests never send real messages."""
    if not action_token or not action_token.strip():
        raise ValueError("BPR02_IMAGE_ALERT_ACTION_TOKEN must be non-empty")

    class ImageAlertHandler(BaseHTTPRequestHandler):
        server_version = "BPR02ImageAlert/1.0"

        def log_message(self, format: str, *args: Any) -> None:
            # Requests may contain personal trading context; do not log body/token.
            return

        def _send_json(self, status: int, body: dict[str, object]) -> None:
            encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:
            if self.path == "/health":
                self._send_json(HTTPStatus.OK, {
                    "status": "ok",
                    "mode": "image_analysis_notifications_only",
                    "orders_submitted": 0,
                })
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def do_POST(self) -> None:
            if self.path != "/v1/image-analysis":
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return

            supplied_header = self.headers.get("Authorization", "")
            prefix = "Bearer "
            supplied_token = supplied_header[len(prefix):].strip() if supplied_header.startswith(prefix) else ""
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
                raw = self.rfile.read(length)
                payload = json.loads(raw.decode("utf-8"))
                event = _validate_image_analysis(payload)
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
                return
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return

            try:
                result = notifier.send_event(event)
            except WhatsAppDeliveryError:
                # Avoid leaking provider details, token-related material, or payload text.
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": "whatsapp_delivery_failed"})
                return
            except Exception:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "notification_failed"})
                return

            self._send_json(HTTPStatus.OK, {
                "status": result.get("status", "unknown"),
                "analysis_id": event.get("event_id"),
                "signal": event.get("signal"),
                "orders_submitted": 0,
            })

    return ImageAlertHandler


def main() -> int:
    action_token = os.environ.get("BPR02_IMAGE_ALERT_ACTION_TOKEN", "")
    if not action_token.strip():
        print("ERROR: BPR02_IMAGE_ALERT_ACTION_TOKEN must be configured as a secret", file=sys.stderr)
        return 2
    try:
        notifier = WhatsAppTemplateNotifier.from_env()
        port = int(os.environ.get("PORT", "8080"))
        if not 1 <= port <= 65535:
            raise ValueError("PORT must be between 1 and 65535")
    except (ValueError, OSError) as exc:
        print(f"ERROR: invalid image-alert service configuration: {exc}", file=sys.stderr)
        return 2

    handler = make_handler(action_token=action_token, notifier=notifier)
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    print(f"BPR-02 image-analysis WhatsApp endpoint listening on port {port}; orders_submitted=0", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
