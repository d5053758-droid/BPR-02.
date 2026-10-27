import json
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from bpr02.api import BPR02RequestHandler


@pytest.fixture
def api_server(monkeypatch, tmp_path):
    candles = tmp_path / "candles.csv"
    candles.write_text(
        "timestamp,open,high,low,close\n"
        "2026-01-01T00:00:00Z,1.10000,1.10020,1.09990,1.10010\n"
        "2026-01-01T00:00:05Z,1.10010,1.10030,1.10000,1.10020\n"
        "2026-01-01T00:00:10Z,1.10020,1.10030,1.10010,1.10015\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BPR02_API_CANDLES", str(candles))
    monkeypatch.setenv("BPR02_API_CANDLES_REPORT", str(tmp_path / "missing-report.json"))
    monkeypatch.setenv("BPR02_API_TICKS", str(tmp_path / "ticks.jsonl"))
    monkeypatch.setenv("BPR02_API_SYMBOL", "EURUSD_otc")
    monkeypatch.setenv("BPR02_API_TIMEFRAME_SECONDS", "5")
    monkeypatch.delenv("BPR02_API_TOKEN", raising=False)

    server = ThreadingHTTPServer(("127.0.0.1", 0), BPR02RequestHandler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    worker.join(timeout=2)


def _get_json(url, headers=None):
    request = Request(url, headers=headers or {}, method="GET")
    with urlopen(request, timeout=3) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def test_health_and_read_only_root(api_server):
    status, health = _get_json(api_server + "/health")
    assert status == 200
    assert health["status"] == "ok"
    assert health["orders_submitted"] == 0
    status, root = _get_json(api_server + "/")
    assert status == 200
    assert root["orders_supported"] is False
    assert "/v1/candles" in root["endpoints"]


def test_candles_endpoint_returns_real_csv_rows_and_respects_limit(api_server):
    status, payload = _get_json(api_server + "/v1/candles?symbol=EURUSD_otc&timeframe_seconds=5&limit=2")
    assert status == 200
    assert payload["count"] == 2
    assert len(payload["candles"]) == 2
    assert payload["candles"][0]["timestamp"] == "2026-01-01T00:00:05Z"
    assert payload["candles"][-1]["close"] == pytest.approx(1.10015)
    assert payload["source_metadata"]["parity_with_pocket_option_confirmed"] is False
    assert payload["orders_submitted"] == 0


def test_quality_endpoint_reports_gaps_and_data_availability(api_server):
    status, payload = _get_json(api_server + "/v1/quality")
    assert status == 200
    assert payload["status"] == "ok"
    assert payload["data_available"] is True
    assert payload["candles"] == 3
    assert payload["irregular_intervals"] == 0


def test_wrong_symbol_or_timeframe_is_rejected(api_server):
    with pytest.raises(HTTPError) as wrong_symbol:
        _get_json(api_server + "/v1/candles?symbol=GBPUSD_otc")
    assert wrong_symbol.value.code == 400
    with pytest.raises(HTTPError) as wrong_timeframe:
        _get_json(api_server + "/v1/candles?timeframe_seconds=60")
    assert wrong_timeframe.value.code == 400


def test_api_token_is_required_when_configured(api_server, monkeypatch):
    monkeypatch.setenv("BPR02_API_TOKEN", "test-only-token")
    with pytest.raises(HTTPError) as missing_token:
        _get_json(api_server + "/v1/candles")
    assert missing_token.value.code == 401
    status, payload = _get_json(
        api_server + "/v1/candles",
        headers={"Authorization": "Bearer test-only-token"},
    )
    assert status == 200
    assert payload["orders_submitted"] == 0


def test_tick_latest_reads_last_matching_tick_without_fabricating_data(api_server, tmp_path, monkeypatch):
    tick_log = tmp_path / "ticks.jsonl"
    records = [
        {"event": "connected", "payload": {"symbols": ["EURUSD_otc"]}},
        {
            "event": "tick",
            "source": "otcharts",
            "source_time_epoch_seconds": 1767225600,
            "received_at_utc": "2026-01-01T00:00:01Z",
            "payload": {"symbol": "EURUSD_otc", "time": 1767225600, "price": 1.1},
        },
        {
            "event": "tick",
            "source": "otcharts",
            "source_time_epoch_seconds": 1767225601,
            "received_at_utc": "2026-01-01T00:00:02Z",
            "payload": {"symbol": "EURUSD_otc", "time": 1767225601, "price": 1.2},
        },
    ]
    tick_log.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    monkeypatch.setenv("BPR02_API_TICKS", str(tick_log))
    status, payload = _get_json(api_server + "/v1/ticks/latest")
    assert status == 200
    assert payload["tick"]["price"] == pytest.approx(1.2)
    assert payload["tick"]["source_timestamp_utc"] == "2026-01-01T00:00:01Z"


def test_post_is_rejected_as_read_only(api_server):
    request = Request(api_server + "/v1/orders", data=b"{}", method="POST")
    with pytest.raises(HTTPError) as response:
        urlopen(request, timeout=3)
    assert response.value.code == 405


def test_invalid_csv_is_not_served(api_server, monkeypatch, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("timestamp,open,high,low,close\nnot-a-time,1,1,1,1\n", encoding="utf-8")
    monkeypatch.setenv("BPR02_API_CANDLES", str(bad))
    with pytest.raises(HTTPError) as response:
        _get_json(api_server + "/v1/candles")
    assert response.value.code == 422
