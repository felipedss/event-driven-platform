## ADR-0015: Saga Timeout and Manual Review for Missing Responses

**Status:** Proposed

## Context

The Saga Orchestrator coordinates Order, Inventory, and Payment asynchronously over Kafka. While waiting for a reply, a saga sits in an intermediate status such as `INVENTORY_PENDING` or `PAYMENT_PENDING` (see `SagaStatus`).

Today, nothing detects the case where that reply never arrives — the downstream service is down, a message is lost despite the delivery guarantees in ADR-0011/ADR-0012/ADR-0013, or a consumer is stuck. The saga simply remains in the pending status indefinitely, with no visibility and no path to resolution.

This gap is already referenced by other ADRs — ADR-0016 assumes "timeout handling (ADR-0015)" exists, and ADR-0018 assumes timeouts and a manual-review signal are "currently surfaced" by this ADR — but the decision itself was never written down. This ADR fills that gap.

**Assumption:** No scheduled-task infrastructure (`@Scheduled` or equivalent) exists anywhere in the codebase today. The timeout checker described below would be the first such component in the orchestrator.

---

## Decision

Introduce an explicit timeout mechanism for pending saga steps, and a `MANUAL_REVIEW` outcome for cases where the missing reply leaves the true outcome of a step ambiguous.

### 1. Per-state timeout thresholds

Each pending status (`INVENTORY_PENDING`, `PAYMENT_PENDING`) gets its own configurable SLA, since downstream services can have very different expected response times:

```yaml
saga:
  timeouts:
    inventory-pending: 30s
    payment-pending: 60s
```

A pending state is only ever considered "stuck" once its configured threshold has been exceeded — never on elapsed time alone. This is the same principle applied in ADR-0018's Scope of Analysis: a slow step is not automatically an abnormal one.

### 2. Scheduled timeout checker

A periodic job in the orchestrator scans sagas currently in a pending status and compares their last-updated timestamp against the threshold configured for that status. This is a detection mechanism only — it does not itself execute compensation or retries.

### 3. New saga outcome: `MANUAL_REVIEW`

When a pending step exceeds its timeout, the saga transitions to a new `MANUAL_REVIEW` status rather than being automatically compensated or automatically retried.

The reasoning: a missing reply means the actual outcome of the downstream step is **unknown** — it may have succeeded, failed silently, or never been received at all. Automatically compensating (e.g., releasing inventory, issuing a refund) risks acting against a step that genuinely succeeded, which can produce worse inconsistency than doing nothing. Automatically retrying risks duplicating a step that already succeeded on the far side. Neither is safe to do blindly without stronger idempotency and reconciliation guarantees than currently exist in this codebase.

`MANUAL_REVIEW` sagas require a human (or, once available, the advisory AI Saga Analyzer from ADR-0018) to inspect saga state, domain events, and DLQ entries (ADR-0013) before deciding to retry, force a state transition, or trigger compensation. This ADR only introduces detection and the `MANUAL_REVIEW` signal — resolution tooling is out of scope.

### 4. Scope limited to ambiguous outcomes

Sagas that already received an explicit failure event (`INVENTORY_FAILED`, `PAYMENT_FAILED`) have an unambiguous signal and are unaffected by this ADR — they continue through the existing compensation flow (ADR-0008). Timeout handling only applies to the ambiguous case: no reply at all.

---

## Consequences

### Positive

- Sagas can no longer be stuck indefinitely with zero visibility
- Avoids unsafe automatic compensation or retries under an unknown outcome, preserving consistency
- Produces a concrete `MANUAL_REVIEW` signal that ADR-0018's AI Saga Analyzer can consume as one of its inputs
- Per-state configuration allows tuning for slower downstream integrations without code changes

### Negative

- `MANUAL_REVIEW` only provides value if something actually monitors and resolves it — without an operational process, stuck sagas become "stuck but labeled" instead of resolved
- Introduces the orchestrator's first scheduled component, a new class of operational concern (job scheduling, overlap prevention, clock skew)
- Threshold tuning is a trade-off: too short flags healthy-but-slow sagas as stuck; too long delays detection of real problems
- Adds a new terminal-ish status to the saga state machine (ADR-0014), increasing the number of transitions that must be tested

---

## Alternatives Considered

### No timeout handling (status quo)

Rejected. This is the exact problem the ADR exists to solve — sagas can remain stuck forever with no signal to anyone.

### Automatically compensate on timeout

Rejected as the default. The outcome of the un-replied step is unknown; compensating a step that actually succeeded can produce worse inconsistency than leaving the saga pending. May be revisited per-step in the future for steps proven safe to compensate blindly.

### Automatically retry on timeout

Rejected as the default. Retrying a step whose original request may have already succeeded downstream risks duplicating side effects (e.g., a second payment charge) unless the downstream service is fully idempotent end-to-end, which is not guaranteed today.

### Manual polling by engineers (no automated detection)

This is the current de facto state. Rejected as unscalable — it depends on someone remembering to check, with no proactive signal that anything is wrong.

---

## Related ADRs

- ADR-0005 Saga Orchestration
- ADR-0008 Service Failure Handling in Sagas
- ADR-0011 Idempotency — Kafka Consumer
- ADR-0013 Kafka Consumer Manual Acknowledgment and DLQ
- ADR-0014 Step-Based Saga Orchestration Model
- ADR-0016 Transactional Outbox Pattern
- ADR-0018 AI-Assisted Saga Analysis via Prompt Chaining
