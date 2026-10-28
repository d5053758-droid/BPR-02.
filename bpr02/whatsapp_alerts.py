"""Opt-in WhatsApp Cloud API notifications for BPR-02 research alerts.

This module sends only approved WhatsApp template messages. It never logs
access tokens, never contacts Pocket Option, and never submits trading orders.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

DEFAULT_API_VERSION = "v26.0"
DEFAULT_LANGUAGE = "es_AR"
DEFAULT_SENT_IDS_PATH = "bpr02_whatsapp_sent.jsonl"
_TEMPLATE_NAME_RE = re.compile(r"^[a-z0-9_]{1,512}$")
_LANGUAGE_RE = re.compile(r"^[a-z]{2}(?:_[A-Z]{2})?$")
_API_VERSION_RE = re.compile(r"^v\d+\.\d+$")
_PHONE_RE = re.compile(r"^\+?[1-9]\d{7,14}$")


class WhatsAppDeliveryError(RuntimeError):
    """Safe-to-log WhatsApp error; deliberately excludes response bodies/tokens."""


def format_research_alert(event: dict[str, object]) -> str:
    """Format one directional candidate as a cautious, compact WhatsApp alert."""
    signal = str(event.get("signal", "")).upper()
    if signal not in {"CALL", "PUT"}:
        raise ValueError("only CALL or PUT research signals can be sent")
    symbol = str(event.get("symbol", "")).strip()
    timeframe = str(event.get("timeframe", "")).strip()
    timestamp = str(event.get("timestamp", "")).strip()
    if not symbol or not timeframe or not timestamp:
        raise ValueError("alert requires timestamp, symbol, and timeframe")
    return (
        f"BPR-02 | {signal} | {symbol} | {timeframe} | "
        f"Vela cerrada: {timestamp} | Candidato experimental; "
        "verificar el gráfico. Sin ejecución automática."
    )


def _event_identity(event: dict[str, object]) -> str:
    """Use the engine ID where present, else create a stable fallback identity."""
    supplied = event.get("event_id")
    if isinstance(supplied, str) and supplied.strip():
        return supplied.strip()
    identity = {
        key: event.get(key)
        for key in ("timestamp", "symbol", "timeframe", "source", "lookback", "signal")
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


class WhatsAppTemplateNotifier:
    """Send approved template notifications through Meta's official Cloud API."""

    def __init__(
        self,
        *,
        access_token: str,
        phone_number_id: str,
        recipient: str,
        template_name: str,
        language: str = DEFAULT_LANGUAGE,
        api_version: str = DEFAULT_API_VERSION,
        sent_ids_path: str | Path = DEFAULT_SENT_IDS_PATH,
        urlopen_fn: Callable[..., object] | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        if not isinstance(access_token, str) or not access_token.strip():
            raise ValueError("WHATSAPP_ACCESS_TOKEN must be non-empty")
        if not isinstance(phone_number_id, str) or not phone_number_id.isdigit():
            raise ValueError("WHATSAPP_PHONE_NUMBER_ID must contain only digits")
        if not isinstance(recipient, str) or not _PHONE_RE.fullmatch(recipient.strip()):
            raise ValueError("WHATSAPP_RECIPIENT must be an international phone number, e.g. +54911...")
        if not isinstance(template_name, str) or not _TEMPLATE_NAME_RE.fullmatch(template_name):
            raise ValueError("WHATSAPP_TEMPLATE_NAME must be a lowercase template name using letters, digits, and underscores")
        if not isinstance(language, str) or not _LANGUAGE_RE.fullmatch(language):
            raise ValueError("WHATSAPP_TEMPLATE_LANGUAGE must look like es_AR or es")
        if not isinstance(api_version, str) or not _API_VERSION_RE.fullmatch(api_version):
            raise ValueError("WHATSAPP_API_VERSION must look like v26.0")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")

        self._access_token = access_token.strip()
        self.phone_number_id = phone_number_id
        self.recipient = recipient.strip().lstrip("+")
        self.template_name = template_name
        self.language = language
        self.api_version = api_version
        self.sent_ids_path = Path(sent_ids_path)
        self._urlopen = urlopen_fn or urllib.request.urlopen
        self.timeout_seconds = float(timeout_seconds)
        self._sent_ids = self._load_sent_ids()

    @classmethod
    def from_env(
        cls,
        *,
        sent_ids_path: str | Path | None = None,
        environ: dict[str, str] | None = None,
        urlopen_fn: Callable[..., object] | None = None,
    ) -> "WhatsAppTemplateNotifier":
        env = os.environ if environ is None else environ
        return cls(
            access_token=env.get("WHATSAPP_ACCESS_TOKEN", ""),
            phone_number_id=env.get("WHATSAPP_PHONE_NUMBER_ID", ""),
            recipient=env.get("WHATSAPP_RECIPIENT", ""),
            template_name=env.get("WHATSAPP_TEMPLATE_NAME", ""),
            language=env.get("WHATSAPP_TEMPLATE_LANGUAGE", DEFAULT_LANGUAGE),
            api_version=env.get("WHATSAPP_API_VERSION", DEFAULT_API_VERSION),
            sent_ids_path=sent_ids_path or env.get("WHATSAPP_SENT_IDS_PATH", DEFAULT_SENT_IDS_PATH),
            urlopen_fn=urlopen_fn,
        )

    def _load_sent_ids(self) -> set[str]:
        if not self.sent_ids_path.exists():
            return set()
        seen: set[str] = set()
        try:
            with self.sent_ids_path.open("r", encoding="utf-8") as file:
                for line_number, line in enumerate(file, start=1):
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    event_id = record.get("event_id") if isinstance(record, dict) else None
                    if not isinstance(event_id, str) or not event_id:
                        raise ValueError(f"invalid sent-ID record on line {line_number}")
                    seen.add(event_id)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"cannot safely read WhatsApp de-duplication file: {self.sent_ids_path}") from exc
        return seen

    def _payload(self, text: str) -> dict[str, object]:
        # The approved template must contain exactly one body parameter placeholder {{1}}.
        return {
            "messaging_product": "whatsapp",
            "to": self.recipient,
            "type": "template",
            "template": {
                "name": self.template_name,
                "language": {"code": self.language},
                "components": [
                    {
                        "type": "body",
                        "parameters": [{"type": "text", "text": text}],
                    }
                ],
            },
        }

    def send_event(self, event: dict[str, object]) -> dict[str, object]:
        """Send a CALL/PUT alert once per event ID; no message is sent on duplicate."""
        signal = str(event.get("signal", "")).upper()
        if signal not in {"CALL", "PUT"}:
            return {"status": "skipped", "reason": "not_directional_signal", "orders_submitted": 0}

        event_id = _event_identity(event)
        if event_id in self._sent_ids:
            return {"status": "duplicate_skipped", "event_id": event_id, "orders_submitted": 0}

        message_text = format_research_alert(event)
        endpoint = (
            f"https://graph.facebook.com/{self.api_version}/"
            f"{self.phone_number_id}/messages"
        )
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(self._payload(message_text), ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "BPR-02-research-alerts/1.0",
            },
            method="POST",
        )
        try:
            with self._urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
            body = json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Do not log the response body: it is not needed for a useful safe error.
            raise WhatsAppDeliveryError(f"WhatsApp Cloud API returned HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise WhatsAppDeliveryError(f"WhatsApp Cloud API connection failed: {exc.reason}") from None
        except (TimeoutError, OSError):
            raise WhatsAppDeliveryError("WhatsApp Cloud API request failed due to a network or timeout error") from None
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, TypeError):
            raise WhatsAppDeliveryError("WhatsApp Cloud API returned an invalid JSON response") from None

        messages = body.get("messages") if isinstance(body, dict) else None
        first = messages[0] if isinstance(messages, list) and messages else None
        message_id = first.get("id") if isinstance(first, dict) else None
        if not isinstance(message_id, str) or not message_id:
            raise WhatsAppDeliveryError("WhatsApp Cloud API response did not confirm an accepted message")

        # Persist only after Meta acknowledges acceptance. Never persist access tokens or message contents.
        self.sent_ids_path.parent.mkdir(parents=True, exist_ok=True)
        record = {"event_id": event_id, "message_id": message_id}
        try:
            with self.sent_ids_path.open("a", encoding="utf-8", newline="\n") as file:
                file.write(json.dumps(record, sort_keys=True) + "\n")
                file.flush()
        except OSError:
            # The message was accepted, but local de-duplication could not be saved.
            # Report this explicitly; a restart could otherwise resend the same event.
            raise WhatsAppDeliveryError(
                "WhatsApp message was accepted, but the de-duplication state could not be saved"
            ) from None
        self._sent_ids.add(event_id)
        return {
            "status": "accepted_by_meta",
            "event_id": event_id,
            "message_id": message_id,
            "orders_submitted": 0,
        }
