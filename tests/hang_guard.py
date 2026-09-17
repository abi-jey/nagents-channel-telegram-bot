"""Shared watchdog deadline for progress waits in tests.

A short numeric deadline turns a progress check into a latency assertion: a
loaded runner (notably the installed-wheel Windows job) can exceed it, so the
watchdog cancels unrelated work and fails an unrelated test. Use this guard for
every wait that is not itself asserting a timeout.
"""

HANG_GUARD = 60.0
