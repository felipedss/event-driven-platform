## ADR-0016: Transactional Outbox Pattern for Reliable Event Publishing

**Status:** Proposed

## Context

Every service in this platform performs two side-effects when handling a business command:

1. Writes state to PostgreSQL (e.g., persists an `Order`, updates a `SagaInstance`)
2. Publishes an event to Kafka (e.g., `OrderCreatedEvent`, `InventoryReservedEvent`)

These two operations are not atomic. No XA/two-phase commit spans PostgreSQL and Kafka in this stack, so any of the following failures leaves the system in an inconsistent state:

- DB commits, Kafka publish fails → event is lost, downstream services are never notified
- Kafka publish succeeds, DB rollback follows → event exists with no matching state on disk
- Service crashes between the DB commit and the `send()` call → silent data loss

ADR-0012 (idempotent producer) and ADR-0011 (consumer deduplication) reduce duplicates at the transport layer but do not solve this atomicity gap. A message that was never sent cannot be deduplicated.

This risk is most acute in two places today:

- **Order Service**: `OrderService` saves the order and immediately calls `kafkaTemplate.send()`. A crash between the two leaves an order with no saga triggered.
- **Saga Orchestrator**: `SagaOrchestrationService` updates `SagaInstance` state and publishes the next step command. A failed publish stalls the saga silently.

---

## Decision

Implement the **Transactional Outbox Pattern** in both the order service and the saga orchestrator.

Instead of publishing directly to Kafka, the service writes the event as a row to an `outbox` table **inside the same database transaction** as the business write. A separate relay process reads unprocessed outbox rows and publishes them to Kafka, then marks them as processed.

### Outbox Table Schema

```sql
CREATE TABLE outbox_events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    topic       VARCHAR(255)  NOT NULL,
    payload     TEXT          NOT NULL,  -- JSON-serialized event
    created_at  TIMESTAMP     NOT NULL DEFAULT now(),
    published_at TIMESTAMP    NULL
);
```

`published_at IS NULL` means the event has not yet been dispatched to Kafka.

### Service-Side Change

Replace every `kafkaTemplate.send(topic, event)` call with an outbox write:

```java
// Before
kafkaTemplate.send("order.created", orderCreatedEvent);

// After — inside the existing @Transactional method
outboxRepository.save(OutboxEvent.of("order.created", orderCreatedEvent));
```

The service transaction now covers both the business entity and the outbox row. Kafka is no longer touched inline.

### Relay: Polling Publisher

A `@Scheduled` component runs inside each service and flushes unprocessed outbox rows:

```java
@Scheduled(fixedDelay = 1000)
@Transactional
public void relay() {
    List<OutboxEvent> pending = outboxRepository.findByPublishedAtIsNull();
    for (OutboxEvent event : pending) {
        kafkaTemplate.send(event.getTopic(), event.getPayload()).get(); // sync, throws on failure
        event.setPublishedAt(Instant.now());
        outboxRepository.save(event);
    }
}
```

The relay uses the existing idempotent producer (ADR-0012). If `send()` throws, the row remains unprocessed and the next relay tick retries. The consumer deduplication layer (ADR-0011) absorbs any rare duplicate that survives a retry.

### Outbox Cleanup

Processed rows (`published_at IS NOT NULL`) are retained for 24 hours for observability, then deleted by a low-priority scheduled job.

---

## Affected Services

- `event-driven-order-service` — `OrderService.createOrder()`
- `event-driven-saga-orchestrator` — all methods in `SagaOrchestrationService` that publish step commands or compensation events

---

## Consequences

### Positive

- Atomicity between DB write and event publishing — no dual-write gap
- Events are never silently lost even if the service crashes mid-request
- At-least-once delivery is guaranteed; combined with ADR-0011 and ADR-0012 this is effectively exactly-once end-to-end
- No new infrastructure dependencies — the outbox lives in the existing PostgreSQL instance
- Observable: unprocessed rows older than N seconds can trigger an alert

### Negative

- Additional latency: events are published up to ~1 second later than before (relay poll interval)
- Extra table and scheduled job per service
- `relay()` must be thread-safe under concurrent scheduler executions — use `SELECT ... FOR UPDATE SKIP LOCKED` if multiple relay threads are introduced later
- Outbox table grows unbounded without cleanup

---

## Alternatives Considered

### Keep direct Kafka publish (status quo)

Rejected.

Acceptable during early prototyping (ADR-0006). At the current phase — with real compensation flows (ADR-0014) and timeout handling (ADR-0015) — a silent lost event breaks saga correctness in a way that is hard to detect and recover from.

---

### Kafka Transactions (exactly-once semantics)

Deferred.

Kafka transactions can wrap a produce + consumer offset commit atomically, which eliminates duplicates entirely. However, they do not cover the DB ↔ Kafka gap unless the DB write is also inside the transaction (not supported without XA). Adds significant producer and consumer configuration complexity. Revisit in a future phase.

---

### Change Data Capture with Debezium

Deferred.

Debezium reads PostgreSQL WAL changes and publishes them to Kafka, eliminating the in-process relay entirely. This is the production-grade approach at scale, but requires running a Debezium connector and Kafka Connect, which increases operational complexity beyond what this educational project currently needs. The polling relay provides the same atomicity guarantee with simpler infrastructure.

---

## Implementation Notes

Start with the order service as a proof-of-concept before rolling out to the orchestrator.

Keep the relay poll interval at 1 second for now; it can be tuned or made event-driven (e.g., notify on transaction commit) later.

Do not introduce `SELECT ... FOR UPDATE SKIP LOCKED` until there is a real need for parallel relay threads — premature locking adds complexity without benefit today.

---

## Related ADRs

- ADR-0006 Phased Saga Implementation
- ADR-0011 Kafka Consumer Idempotency
- ADR-0012 Kafka Producer Idempotency
- ADR-0014 Step-Based Saga Orchestration Model
- ADR-0015 Saga Timeout and Manual Review for Missing Responses
