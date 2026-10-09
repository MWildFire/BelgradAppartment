"""Process-local provider outage guard. No sleeps, network, keys or persistent mutations."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock


@dataclass(frozen=True)
class Ticket:
    generation: int
    probe: bool = False


class CircuitBreaker:
    """Five failures open for 5 minutes; exactly one bounded half-open probe.

    Shared by all validation jobs in one monitor process. Separate CLI processes have
    independent state. Job leases/retry schedules remain the durable cross-process guard.
    """

    def __init__(
        self,
        *,
        threshold: int = 5,
        cooldown: float = 300,
        probe_lease: float = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.threshold, self.cooldown, self.probe_lease = threshold, cooldown, probe_lease
        self.clock = clock
        self.lock = Lock()
        self.generation = 0
        self.failures = 0
        self.opened_until: float | None = None
        self.probe_until: float | None = None

    def acquire(self) -> Ticket | None:
        with self.lock:
            now = self.clock()
            if self.opened_until is None:
                return Ticket(self.generation)
            if now < self.opened_until or (self.probe_until is not None and now < self.probe_until):
                return None
            # Invalidate any expired probe so a late completion cannot close the new circuit.
            self.generation += 1
            self.probe_until = now + self.probe_lease
            return Ticket(self.generation, True)

    def finish(self, ticket: Ticket, *, success: bool) -> None:
        with self.lock:
            if ticket.generation != self.generation:
                return
            if success:
                self.failures = 0
                self.opened_until = self.probe_until = None
            else:
                self.failures += 1
                if ticket.probe or self.failures >= self.threshold:
                    self.generation += 1
                    self.opened_until = self.clock() + self.cooldown
                    self.probe_until = None


anthropic_breaker = CircuitBreaker()
