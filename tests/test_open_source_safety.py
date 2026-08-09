from ai_option_scanner import web_api, worker


def test_broker_api_is_disabled_when_switch_is_absent(monkeypatch) -> None:
    monkeypatch.delenv("AI_OPTION_ENABLE_BROKER_API", raising=False)

    assert web_api._broker_api_enabled() is False


def test_broker_api_requires_explicit_truthy_switch(monkeypatch) -> None:
    monkeypatch.setenv("AI_OPTION_ENABLE_BROKER_API", "true")

    assert web_api._broker_api_enabled() is True


def test_trading_path_gate_covers_every_execution_surface() -> None:
    blocked = (
        "/api/trading/run-now",
        "/api/auto-trade/instances",
        "/api/brokers/accounts",
        "/api/longbridge/accounts",
    )

    assert all(web_api._is_broker_or_trading_path(path) for path in blocked)
    assert not web_api._is_broker_or_trading_path("/api/scans")


def test_worker_does_not_start_trading_jobs_by_default(monkeypatch) -> None:
    calls: list[str] = []

    class Stopped:
        @staticmethod
        def is_set() -> bool:
            return True

    class ThreadStub:
        @staticmethod
        def is_alive() -> bool:
            return True

    for name in (
        "AI_OPTION_ENABLE_TRADING_SCHEDULER",
        "AI_OPTION_ENABLE_AUTO_TRADE_SCHEDULER",
        "AI_OPTION_ENABLE_ORDER_MONITOR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(worker, "load_dotenv", lambda _path: None)
    monkeypatch.setattr(worker, "redis_queue_enabled", lambda: True)
    monkeypatch.setattr(worker, "_install_signals", lambda: None)
    monkeypatch.setattr(worker, "_requeue_pending_scans", lambda: None)
    monkeypatch.setattr(worker, "_stop", Stopped())
    monkeypatch.setattr(worker, "_spawn_scan_thread", lambda _index: ThreadStub())
    monkeypatch.setattr(worker, "start_trading_scheduler", lambda: calls.append("trading"))
    monkeypatch.setattr(worker, "start_auto_trade_scheduler", lambda: calls.append("auto_trade"))
    monkeypatch.setattr(worker, "start_order_monitor", lambda: calls.append("monitor"))
    monkeypatch.setattr(worker, "start_observation_scheduler", lambda: calls.append("observation"))
    monkeypatch.setattr(worker, "start_notification_worker", lambda: calls.append("notification"))
    monkeypatch.setattr(worker, "start_post_mortem_worker", lambda: calls.append("post_mortem"))

    worker.main()

    assert not {"trading", "auto_trade", "monitor"}.intersection(calls)
    assert {"observation", "notification", "post_mortem"}.issubset(calls)
