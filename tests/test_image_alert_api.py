import base64
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from bpr02.image_alert_api import analyze_image_with_openai, make_handler

PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic-test-image-bytes"
IMAGE_URL = "data:image/png;base64," + base64.b64encode(PNG).decode("ascii")


class FakeNotifier:
    def __init__(self):
        self.events = []

    def send_event(self, event):
        self.events.append(dict(event))
        return {"status": "accepted_by_meta", "event_id": event["event_id"],
                "message_id": "wamid.mocked", "orders_submitted": 0}


def result(**changes):
    payload = {
        "symbol": "EURUSD_otc", "timeframe": "M1", "signal": "NO TRADE",
        "reason": "La captura no muestra una confirmación suficiente.",
        "entry_trigger": "Esperar cierre y recuperación.",
        "expiry": "No se puede estimar solo con esta captura.",
        "confirmation": "Cierre de vela y retesteo.",
        "invalidation": "La estructura visible deja de ser válida.",
    }
    payload.update(changes)
    return payload


def payload(**changes):
    data = {
        "analysis_id": "2ac537a3-2957-47bd-9e19-bpr02-test-001",
        "image_attached": True, "image_data_url": IMAGE_URL,
    }
    data.update(changes)
    return data


def start_server(notifier, analyzer=None, token="test-secret"):
    handler = make_handler(action_token=token, notifier=notifier, vision_analyzer=analyzer)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}"


def post(base, data, token="test-secret"):
    req = urllib.request.Request(
        base + "/v1/analyze-image",
        data=json.dumps(data).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=3)


def stop(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_real_image_is_analyzed_before_whatsapp_send():
    notifier = FakeNotifier()
    analyzed = []
    server, thread, base = start_server(notifier, analyzer=lambda image: analyzed.append(image) or result())
    try:
        with post(base, payload()) as response:
            body = json.loads(response.read())
        assert body["status"] == "analysis_complete"
        assert body["signal"] == "NO TRADE"
        assert body["whatsapp_status"] == "accepted_by_meta"
        assert body["orders_submitted"] == 0
        assert analyzed == [IMAGE_URL]
        assert len(notifier.events) == 1
        assert notifier.events[0]["source"] == "mobile_web_image_analysis"
        assert notifier.events[0]["timestamp"].endswith("Z")
    finally:
        stop(server, thread)


def test_no_image_means_no_analysis_and_no_whatsapp():
    notifier, calls = FakeNotifier(), []
    server, thread, base = start_server(notifier, analyzer=lambda image: calls.append(image) or result())
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            post(base, payload(image_attached=False))
        assert err.value.code == 400
        assert json.loads(err.value.read())["error"] == "an_actual_image_is_required"
        assert calls == [] and notifier.events == []
    finally:
        stop(server, thread)


def test_missing_or_invalid_image_is_rejected_before_analysis():
    for changes in ({"image_data_url": ""}, {"image_data_url": "https://example.org/chart.png"}):
        notifier, calls = FakeNotifier(), []
        server, thread, base = start_server(notifier, analyzer=lambda image: calls.append(image) or result())
        try:
            with pytest.raises(urllib.error.HTTPError) as err:
                post(base, payload(**changes))
            assert err.value.code == 400
            assert calls == [] and notifier.events == []
        finally:
            stop(server, thread)


def test_bad_token_does_not_analyze_or_send():
    notifier, calls = FakeNotifier(), []
    server, thread, base = start_server(notifier, analyzer=lambda image: calls.append(image) or result())
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            post(base, payload(), token="wrong")
        assert err.value.code == 401
        assert calls == [] and notifier.events == []
    finally:
        stop(server, thread)


def test_vision_failure_means_no_whatsapp_send():
    notifier = FakeNotifier()
    def fail(image):
        raise RuntimeError("private provider detail")
    server, thread, base = start_server(notifier, analyzer=fail)
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            post(base, payload())
        assert err.value.code == 502
        assert json.loads(err.value.read())["error"] == "image_analysis_failed_no_whatsapp_sent"
        assert notifier.events == []
    finally:
        stop(server, thread)


def test_invalid_model_signal_does_not_send():
    notifier = FakeNotifier()
    server, thread, base = start_server(notifier, analyzer=lambda image: result(signal="MAYBE"))
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            post(base, payload())
        assert err.value.code == 502
        assert notifier.events == []
    finally:
        stop(server, thread)


def test_home_page_and_health_are_available_without_sending():
    notifier = FakeNotifier()
    server, thread, base = start_server(notifier)
    try:
        with urllib.request.urlopen(base + "/") as response:
            html = response.read().decode()
        assert "Captura del gráfico" in html
        assert "Analizar captura y enviar WhatsApp" in html
        with urllib.request.urlopen(base + "/health") as response:
            status = json.loads(response.read())
        assert status["status"] == "ok"
        assert notifier.events == []
    finally:
        stop(server, thread)


def test_responses_api_receives_image_as_data_url_and_returns_json_analysis():
    captured = {}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return json.dumps({"output_text": json.dumps(result())}).encode()
    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["auth"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data)
        return Response()

    analyzed = analyze_image_with_openai(
        IMAGE_URL, api_key="openai-test-secret", model="gpt-6.1-terra",
        urlopen_fn=fake_urlopen,
    )
    assert captured["url"] == "https://api.openai.com/v1/responses"
    assert captured["auth"] == "Bearer openai-test-secret"
    assert captured["timeout"] == 45
    assert captured["body"]["store"] is False
    image_parts = [p for item in captured["body"]["input"] for p in item.get("content", [])
                   if p.get("type") == "input_image"]
    assert image_parts[0]["image_url"] == IMAGE_URL
    assert analyzed["signal"] == "NO TRADE"


def test_invalid_image_never_calls_openai():
    called = []
    with pytest.raises(ValueError, match="only base64 PNG"):
        analyze_image_with_openai("https://example.org/image.png", api_key="x",
                                  urlopen_fn=lambda *a, **k: called.append(True))
    assert not called
