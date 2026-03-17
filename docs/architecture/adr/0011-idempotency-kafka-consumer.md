## ADR-0011: Kafka Consumer Idempotency via Database Constraints

**Status:** Accepted

**Context:**

Kafka delivers messages at-least-once. A consumer can process a message, crash before
committing the offset, and receive the same message again on restart or rebalance.
This is the highest-risk idempotency layer — duplicates are silent, invisible to
clients, and can cause real data corruption: stock decremented twice, payment charged
twice, or multiple sagas spawned for the same order.

**Decision:**

Use **database unique constraints** on the business key (`orderId`) as the primary
deduplication mechanism. A duplicate message triggers a constraint violation on insert,
which is caught and logged — the message becomes a no-op. For update-heavy operations
(e.g. saga state transitions), complement with an explicit state guard in the service
layer.

Two complementary mechanisms are used depending on the operation type:

**1. Unique constraints (insert operations)**
A duplicate message triggers a constraint violation, which is caught and logged —
the message becomes a no-op.

**2. State checks (update operations)**
Before transitioning state, verify the current state is the expected predecessor.
If the record is already in the target state or beyond, skip silently. This covers
cases where the unique constraint does not apply because the record already exists.

```
// Example: saga orchestrator receiving a duplicate PaymentProcessedEvent
if (saga.getStatus() != PAYMENT_PENDING) {
    log.warn("Unexpected status {}, ignoring event", saga.getStatus());
    return;
}
```

Per-service specifics:

| Service | Operation | Mechanism |
|---|---|---|
| `payment-service` | insert `Payment` | `orderId` unique constraint → violation = already processed |
| `inventory-service` | insert `Reservation` | `orderId` unique constraint → violation = already reserved |
| `saga-orchestrator` | insert `OrderSaga` | `orderId` unique constraint → violation = saga already started |
| `saga-orchestrator` | update saga status | State check → skip if not in expected predecessor state |
| `order-service` | update order status | State check → skip if already in terminal state (`CONFIRMED`/`CANCELED`) |

Note on `inventory-service`: introducing a `Reservation` entity (tracking which orders
reserved what quantity) also improves compensation accuracy — the exact quantity to
release on payment failure is derived from the reservation record, not from the
redelivered command.

✅ **Pros:**
- Leverages existing PostgreSQL — no extra infrastructure
- DB constraint is an absolute safety net that cannot be bypassed by application bugs
- Business-key constraints double as data integrity rules
- `Reservation` entity provides an audit trail for inventory movements

❌ **Cons:**
- Constraint violation handling adds boilerplate per service
- Does not cover partial redelivery within a multi-step flow (needs state guards)
- Requires distinguishing "already processed" (safe to skip) from real errors (must not swallow)

**Alternatives Considered:**

- **Dedicated deduplication table** (`processed_messages` keyed by `topic + partition + offset`):
  more generic but adds a cross-cutting table per service and an extra lookup on every
  message. Rejected in favour of business-key constraints which are simpler and carry
  domain meaning.
- **Kafka exactly-once semantics (transactions):** strongest guarantee but requires
  transactional producers and consumers coordinated with DB transactions. Significant
  operational complexity. Deferred to a future phase if exactly-once becomes a hard
  requirement.
