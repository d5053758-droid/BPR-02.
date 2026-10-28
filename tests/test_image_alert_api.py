import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from bpr02.image_alert_api import make_handler


class RecordingNotifier:
    def __init__(self):
        self.events = []

    def send_event(self, event):
        self.events.append(dict(event))
        return {
            "status": "accepted_by_meta",
            "event_id": event["event_id"],
            "message_id": "wamid.mocked",
            "orders_submitted": 0,
        }


def image_analysis_payload(**changes):
    data = {
        "analysis_id": "2ac537a3-2957-47bd-9e19-bpr02-test-001",
        "image_attached": True,
        "analysis_source": "chatgpt_image_analysis",
        "symbol": "EURUSD_otc",
        "timeframe": "M1",
        "signal": "NO TRADE",
        "reason": "La captura no muestra una confirmación suficiente.",
        "entry_trigger": "Esperar cierre y recuperación del nivel.",
        "expiry": "No aplicable sin confirmación.",
        "confirmation": "Cierre de vela y retesteo confirmado.",
        "invalidation": "El precio rompe y sostiene por encima del máximo observado.",
    }
    data.update(changes)
    return data


def running_server(notifier, token="test-action-secret"):
    handler = make_handler(action_token=token, notifier=notifier)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}"


def post_json(base_url, payload, token="test-action-secret"):
    request = urllib.request.Request(
        base_url + "/v1/image-analysis",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    return urllib.request.urlopen(request, timeout=3)


def test_authenticated_image_analysis_triggers_one_whatsapp_notification(tmp_path):
    notifier = RecordingNotifier()
    server, thread, base_url = running_server(notifier)
    try:
        with post_json(base_url, image_analysis_payload()) as response:
            body = json.loads(response.read().decode("utf-8"))
        assert body["status"] == "accepted_by_meta"
        assert body["signal"] == "NO TRADE"
        assert body["orders_submitted"] == 0
        assert len(notifier.events) == 1
        event = notifier.events[0]
        assert event["event_type"] == "image_analysis"
        assert event["source"] == "chatgpt_image_analysis"
        assert event["signal"] == "NO TRADE"
        assert event["symbol"] == "EURUSD_otc"
        assert event["timestamp"].endswith("Z")
        assert event["reason"] == "La captura no muestra una confirmación suficiente."
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_no_image_means_no_whatsapp_message(tmp_path):
    notifier = RecordingNotifier()
    server, thread, base_url = running_server(notifier)
    try:
        try:
            post_json(base_url, image_analysis_payload(image_attached=False))
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
            body = json.loads(exc.read().decode("utf-8"))
            assert body["error"] == "WhatsApp alerts require image_attached=true"
        else:
            raise AssertionError("request without an image should be rejected")
        assert notifier.events == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_invalid_action_token_is_rejected_before_processing_payload():
    notifier = RecordingNotifier()
    server, thread, base_url = running_server(notifier)
    try:
        try:
            post_json(base_url, image_analysis_payload(), token="wrong-token")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
            assert json.loads(exc.read().decode("utf-8"))["error"] == "unauthorized"
        else:
            raise AssertionError("unauthorized request should be rejected")
        assert notifier.events == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_non_chatgpt_analysis_source_is_rejected():
    notifier = RecordingNotifier()
    server, thread, base_url = running_server(notifier)
    try:
        try:
            post_json(base_url, image_analysis_payload(analysis_source="market_stream"))
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
            assert "analysis_source" in json.loads(exc.read().decode("utf-8"))["error"]
        else:
            raise AssertionError("non-image source should be rejected")
        assert notifier.events == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_bad_signal_is_rejected_without_sending():
    notifier = RecordingNotifier()
    server, thread, base_url = running_server(notifier)
    try:
        try:
            post_json(base_url, image_analysis_payload(signal="MAYBE"))
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
            assert "signal must be" in json.loads(exc.read().decode("utf-8"))["error"]
        else:
            raise AssertionError("invalid signal should be rejected")
        assert notifier.events == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_health_endpoint_does_not_require_image_or_send_message():
    notifier = RecordingNotifier()
    server, thread, base_url = running_server(notifier)
    try:
        with urllib.request.urlopen(base_url + "/health", timeout=3) as response:
            body = json.loads(response.read().decode("utf-8"))
        assert body["status"] == "ok"
        assert body["orders_submitted"] == 0
        assert notifier.events == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
