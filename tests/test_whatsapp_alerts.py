import io
import json
import urllib.error
from urllib.parse import urlparse

import pytest

from bpr02.whatsapp_alerts import (
    WhatsAppDeliveryError,
    WhatsAppTemplateNotifier,
    format_research_alert,
)


def event(signal="CALL", event_id="event-001"):
    return {
        "event_id": event_id,
        "event_type": "research_signal",
        "timestamp": "2026-10-28T05:54:00Z",
        "symbol": "EURUSD_otc",
        "timeframe": "M1",
        "source": "otcharts_live_stream",
        "signal": signal,
        "reason": "synthetic test candidate",
    }


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def make_notifier(path, urlopen_fn, **overrides):
    config = {
        "access_token": "secret-test-token",
        "phone_number_id": "123456789",
        "recipient": "+5491112345678",
        "template_name": "bpr02_research_alert",
        "language": "es_AR",
        "api_version": "v26.0",
        "sent_ids_path": path,
        "urlopen_fn": urlopen_fn,
    }
    config.update(overrides)
    return WhatsAppTemplateNotifier(**config)


def test_sends_only_an_approved_template_to_official_graph_endpoint_and_keeps_secret_out_of_logs(tmp_path):
    seen = {}
    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["authorization"] = request.get_header("Authorization")
        seen["timeout"] = timeout
        seen["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse({"messaging_product": "whatsapp", "messages": [{"id": "wamid.accepted-1"}]})

    state_path = tmp_path / "sent.jsonl"
    notifier = make_notifier(state_path, fake_urlopen)
    result = notifier.send_event(event())

    assert urlparse(seen["url"]).scheme == "https"
    assert urlparse(seen["url"]).netloc == "graph.facebook.com"
    assert urlparse(seen["url"]).path == "/v26.0/123456789/messages"
    assert seen["authorization"] == "Bearer secret-test-token"
    assert seen["timeout"] == 15.0
    payload = seen["payload"]
    assert payload["messaging_product"] == "whatsapp"
    assert payload["to"] == "5491112345678"
    assert payload["type"] == "template"
    assert payload["template"]["name"] == "bpr02_research_alert"
    assert payload["template"]["language"] == {"code": "es_AR"}
    text = payload["template"]["components"][0]["parameters"][0]["text"]
    assert text == format_research_alert(event())
    assert "CALL" in text and "Sin ejecución automática" in text
    assert result == {
        "status": "accepted_by_meta",
        "event_id": "event-001",
        "message_id": "wamid.accepted-1",
        "orders_submitted": 0,
    }
    saved = state_path.read_text(encoding="utf-8")
    assert "secret-test-token" not in saved
    assert "CALL" not in saved
    assert json.loads(saved)["event_id"] == "event-001"


def test_duplicate_event_is_skipped_across_notifier_restarts(tmp_path):
    state_path = tmp_path / "sent.jsonl"
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request)
        return FakeResponse({"messages": [{"id": "wamid.first"}]})

    first = make_notifier(state_path, fake_urlopen)
    assert first.send_event(event())["status"] == "accepted_by_meta"
    restarted = make_notifier(state_path, fake_urlopen)
    result = restarted.send_event(event())

    assert result["status"] == "duplicate_skipped"
    assert len(calls) == 1


@pytest.mark.parametrize("signal", ["NO TRADE", "WAIT", ""])
def test_non_directional_events_are_never_sent(tmp_path, signal):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("no request should be made for non-directional events")

    notifier = make_notifier(tmp_path / "sent.jsonl", fail_if_called)
    result = notifier.send_event(event(signal=signal))
    assert result["status"] == "skipped"
    assert result["reason"] == "not_directional_signal"


def test_template_alert_requires_timestamp_symbol_and_timeframe():
    with pytest.raises(ValueError, match="timestamp, symbol, and timeframe"):
        format_research_alert({"signal": "PUT", "symbol": "EURUSD_otc", "timeframe": "M1"})


def test_http_errors_are_safe_to_log_and_do_not_mark_event_sent(tmp_path):
    state_path = tmp_path / "sent.jsonl"

    def failing_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 400, "bad request", None,
            io.BytesIO(b'{"error":"sensitive provider detail"}'),
        )

    notifier = make_notifier(state_path, failing_urlopen)
    with pytest.raises(WhatsAppDeliveryError, match="HTTP 400") as error:
        notifier.send_event(event())
    assert "secret-test-token" not in str(error.value)
    assert "sensitive provider detail" not in str(error.value)
    assert not state_path.exists()


def test_invalid_de_duplication_file_fails_closed(tmp_path):
    state_path = tmp_path / "sent.jsonl"
    state_path.write_text("this is not JSON\n", encoding="utf-8")
    with pytest.raises(ValueError, match="cannot safely read"):
        make_notifier(state_path, lambda *args, **kwargs: FakeResponse({"messages": [{"id": "x"}]}))


def test_configuration_validation_rejects_invalid_recipient_and_template(tmp_path):
    with pytest.raises(ValueError, match="WHATSAPP_RECIPIENT"):
        make_notifier(tmp_path / "a.jsonl", lambda *args, **kwargs: None, recipient="not-a-phone")
    with pytest.raises(ValueError, match="WHATSAPP_TEMPLATE_NAME"):
        make_notifier(tmp_path / "b.jsonl", lambda *args, **kwargs: None, template_name="Not A Template")


def test_from_env_uses_defaults_and_requires_credentials(tmp_path):
    with pytest.raises(ValueError, match="WHATSAPP_ACCESS_TOKEN"):
        WhatsAppTemplateNotifier.from_env(environ={}, sent_ids_path=tmp_path / "sent.jsonl")

    notifier = WhatsAppTemplateNotifier.from_env(
        environ={
            "WHATSAPP_ACCESS_TOKEN": "env-secret",
            "WHATSAPP_PHONE_NUMBER_ID": "123456",
            "WHATSAPP_RECIPIENT": "+5491112345678",
            "WHATSAPP_TEMPLATE_NAME": "bpr02_research_alert",
        },
        sent_ids_path=tmp_path / "sent.jsonl",
        urlopen_fn=lambda *args, **kwargs: FakeResponse({"messages": [{"id": "x"}]}),
    )
    assert notifier.api_version == "v26.0"
    assert notifier.language == "es_AR"
