"""One-use, expiring approvals for a local user interface.

The worker requesting permission waits without holding the broker's lock. The
interface receives the complete proposal and must explicitly approve its ID.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import secrets
import threading
import time


MAX_DETAILS_BYTES = 512 * 1024
MAX_PENDING_APPROVALS = 4


@dataclass
class _Approval:
    record: dict
    deadline: float
    event: threading.Event = field(default_factory=threading.Event)
    approved: bool = False


class ApprovalBroker:
    """Deliver complete proposals to the UI and await an explicit decision.

    IDs identify only one pending decision. Expiry, cancellation, and shutdown
    deny permission and remove the ID, so it cannot approve a later proposal.
    """

    def __init__(self, timeout: float = 180.0) -> None:
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout < 0):
            raise ValueError("Approval timeout must be a finite, nonnegative number.")
        self._timeout = float(timeout)
        self._lock = threading.Lock()
        self._pending: dict[str, _Approval] = {}
        self._closed = False

    @staticmethod
    def _copy_details(details: dict) -> dict:
        if not isinstance(details, dict):
            raise ValueError("Approval details must be a JSON object.")
        try:
            encoded = json.dumps(details, ensure_ascii=False, allow_nan=False,
                                 separators=(",", ":"))
            size = len(encoded.encode("utf-8"))
            if size > MAX_DETAILS_BYTES:
                raise ValueError("Approval details exceed the 512 KiB limit.")
            return json.loads(encoded)
        except (TypeError, OverflowError, RecursionError, UnicodeEncodeError) as exc:
            raise ValueError("Approval details must contain valid JSON data.") from exc

    def _expire_locked(self, now: float) -> None:
        for approval_id, approval in list(self._pending.items()):
            if approval.deadline <= now:
                self._pending.pop(approval_id)
                approval.event.set()

    def request(self, summary: str, details: dict) -> bool:
        """Wait for one explicit decision; return False if no approval arrives.

        Invalid or oversized proposals raise ValueError instead of presenting
        an incomplete proposal for approval. Capacity, expiry, and shutdown
        return False, so callers can handle them as denied permission.
        """
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("Approval summary must be a nonempty string.")
        copied_details = self._copy_details(details)
        with self._lock:
            now = time.monotonic()
            self._expire_locked(now)
            if self._closed or len(self._pending) >= MAX_PENDING_APPROVALS:
                return False
            approval_id = secrets.token_urlsafe(18)
            while approval_id in self._pending:
                approval_id = secrets.token_urlsafe(18)
            created_at = time.time()
            approval = _Approval(
                record={"id": approval_id, "summary": summary,
                        "details": copied_details, "created_at": created_at,
                        "expires_at": created_at + self._timeout},
                deadline=now + self._timeout,
            )
            self._pending[approval_id] = approval
        try:
            approval.event.wait(max(0.0, approval.deadline - time.monotonic()))
            with self._lock:
                return approval.approved
        finally:
            with self._lock:
                if self._pending.get(approval_id) is approval:
                    self._pending.pop(approval_id)

    def pending(self) -> list[dict]:
        """Return independent copies of complete, unexpired proposals."""
        with self._lock:
            self._expire_locked(time.monotonic())
            return [json.loads(json.dumps(approval.record, ensure_ascii=False,
                                         allow_nan=False))
                    for approval in self._pending.values()]

    def decide(self, approval_id: str, approved: bool) -> bool:
        """Resolve one current ID and report whether a decision was accepted."""
        if type(approved) is not bool:
            raise ValueError("Approval decision must be true or false.")
        if not isinstance(approval_id, str):
            return False
        with self._lock:
            self._expire_locked(time.monotonic())
            approval = self._pending.pop(approval_id, None)
            if approval is None:
                return False
            approval.approved = approved
            approval.event.set()
            return True

    def cancel_all(self) -> None:
        """Deny pending requests while allowing future requests."""
        with self._lock:
            approvals = list(self._pending.values())
            self._pending.clear()
            for approval in approvals:
                approval.event.set()

    def close(self) -> None:
        """Deny pending requests and permanently deny future requests."""
        with self._lock:
            self._closed = True
            approvals = list(self._pending.values())
            self._pending.clear()
            for approval in approvals:
                approval.event.set()
