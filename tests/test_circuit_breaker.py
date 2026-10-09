from src.matching.circuit_breaker import CircuitBreaker


def test_outage_has_bounded_requests_and_one_half_open_probe():
    clock = [0.0]
    breaker = CircuitBreaker(clock=lambda: clock[0])
    for _ in range(5):
        ticket = breaker.acquire()
        assert ticket is not None
        breaker.finish(ticket, success=False)
    assert breaker.acquire() is None
    clock[0] = 300
    probe = breaker.acquire()
    assert probe is not None and probe.probe
    assert breaker.acquire() is None
    breaker.finish(probe, success=True)
    assert breaker.acquire() is not None


def test_late_success_cannot_close_new_outage_or_new_probe():
    clock = [0.0]
    breaker = CircuitBreaker(threshold=1, clock=lambda: clock[0])
    stale, failure = breaker.acquire(), breaker.acquire()
    breaker.finish(failure, success=False)
    breaker.finish(stale, success=True)
    assert breaker.acquire() is None
    clock[0] = 300
    stale_probe = breaker.acquire()
    clock[0] = 361
    current_probe = breaker.acquire()
    breaker.finish(stale_probe, success=True)
    assert breaker.acquire() is None
    breaker.finish(current_probe, success=False)
    clock[0] = 400
    assert breaker.acquire() is None
