## ADR-0013: Kafka Consumer Manual Acknowledgment and DLQ

**Status:** Proposed

**Context:**

All four event-driven services — `order-service`, `saga-orchestrator`,
`inventory-service`, and `payment-service` — currently consume Kafka messages with the
default `AckMode.BATCH` setting, which auto-commits offsets on a 5-second interval
regardless of whether the business logic succeeded or threw an exception.

This creates a silent data-loss risk: if the consumer crashes after the offset is
committed but before the database write completes, the message is gone. Conversely,
if processing throws after the offset is committed, the message is also lost — no
retry, no dead-letter record.

There is currently no error handler, retry policy, or dead-letter queue in any
service.

**Decision:**

Switch all `@KafkaListener` containers to **manual acknowledgment** and introduce a
**`DefaultErrorHandler`** with fixed-backoff retry and a dead-letter queue (DLQ) per
topic.

### 1. Producer — idempotent producer with `acks=all`

Enable idempotent producer configuration on all four services (see ADR-0012). This is
already captured there but is a prerequisite for the consumer-side guarantees below.

### 2. Consumer — `AckMode.MANUAL_IMMEDIATE`

Set `AckMode.MANUAL_IMMEDIATE` on the listener container factory in all four services:

```java
factory.getContainerProperties().setAckMode(AckMode.MANUAL_IMMEDIATE);
```

Each `@KafkaListener` method receives an `Acknowledgment` parameter and calls
`ack.acknowledge()` only after the business logic completes successfully:

```java
@KafkaListener(topics = "order.created")
public void onOrderCreated(OrderCreatedEvent event, Acknowledgment ack) {
    sagaService.startSaga(event);  // if this throws, offset is not committed
    ack.acknowledge();             // offset committed only on success
}
```

If the method throws, the container does not commit the offset. The error handler
(below) decides whether to retry or route to DLQ.

### 3. Error handler — `DefaultErrorHandler` with fixed backoff + DLQ

A `DefaultErrorHandler` bean is wired into all four consumer configs:

```java
@Bean
public DefaultErrorHandler errorHandler(KafkaTemplate<String, Object> template) {
    var recoverer = new DeadLetterPublishingRecoverer(template,
        (rec, ex) -> new TopicPartition(rec.topic() + ".DLQ", rec.partition()));

    var backOff = new FixedBackOff(1_000L, 3L); // 3 retries, 1 second apart

    return new DefaultErrorHandler(recoverer, backOff);
}
```

After 3 failed retries the message is forwarded to a `<topic>.DLQ` topic for
out-of-band inspection. The consumer partition is unblocked and continues processing
subsequent messages.

### DLQ topics

| Service | Source topic | DLQ topic |
|---|---|---|
| order-service | `order.confirmed` | `order.confirmed.DLQ` |
| order-service | `order.cancelled` | `order.cancelled.DLQ` |
| saga-orchestrator | `order.created` | `order.created.DLQ` |
| saga-orchestrator | `payment.processed` | `payment.processed.DLQ` |
| saga-orchestrator | `payment.failed` | `payment.failed.DLQ` |
| saga-orchestrator | `inventory.reserved` | `inventory.reserved.DLQ` |
| saga-orchestrator | `inventory.reservation.failed` | `inventory.reservation.failed.DLQ` |
| saga-orchestrator | `inventory.released` | `inventory.released.DLQ` |
| saga-orchestrator | `inventory.release.failed` | `inventory.release.failed.DLQ` |
| inventory-service | `order.inventory.reserve` | `order.inventory.reserve.DLQ` |
| inventory-service | `order.inventory.release` | `order.inventory.release.DLQ` |
| payment-service | `order.payment.process` | `order.payment.process.DLQ` |

DLQ topics are declared as `NewTopic` beans in a `KafkaTopicConfig` class in each
service. Monitoring and reprocessing of DLQ messages is out of scope for this phase.

**Affected services:** `order-service`, `saga-orchestrator`, `inventory-service`, `payment-service`.

**Implementation order:**

1. Add idempotent producer config (ADR-0012 prerequisite — zero behavioral change)
2. Wire `DefaultErrorHandler` + DLQ topics (safe to add before switching AckMode)
3. Switch to `AckMode.MANUAL_IMMEDIATE` and add `Acknowledgment` params to all listeners

✅ **Pros:**
- Offsets are committed only after successful processing — no silent message loss
- Transient failures are retried automatically before routing to DLQ
- Poison-pill messages are isolated to DLQ; the partition continues processing
- DLQ gives visibility into failures for manual inspection or reprocessing
- `MANUAL_IMMEDIATE` commits the offset synchronously, preventing duplicate delivery
  on rebalance in most cases

❌ **Cons:**
- Slightly higher broker latency — each `ack.acknowledge()` triggers a synchronous
  offset commit request instead of a batched auto-commit
- 12 listener method signatures must be updated across all services (7 in
  `KafkaConsumerService`, 2 in `OrderEventConsumer`, 2 in `InventoryCommandConsumer`,
  1 in `PaymentCommandConsumer`) — mechanical but required
- DLQ messages require an operational process to monitor and reprocess; without it,
  failures become invisible again
- Does not achieve exactly-once semantics — a crash between `sagaService.startSaga()`
  completing and `ack.acknowledge()` executing can still cause a redelivery. The
  consumer-side idempotency guard (ADR-0011) covers this gap
- Future iterations may classify exceptions into retryable and non-retryable categories, 
- routing unrecoverable failures directly to DLQ to avoid unnecessary retries.

**Alternatives Considered:**

- **Keep `AckMode.BATCH` (auto-commit):** simple, zero changes. Rejected because offset
  commit is decoupled from processing success, making silent loss possible.
- **`AckMode.RECORD`:** commits offsets per record rather than in batches, simplifying offset management. 
- Rejected because this project requires explicit control over when offsets are acknowledged, making `MANUAL_IMMEDIATE` a better fit for Phase 1.
- **Kafka transactions (exactly-once):** wraps the consumer offset commit and the
  producer send in a single atomic transaction. Eliminates the redelivery gap above but
  requires transactional producers, `isolation.level=read_committed` on all consumers,
  and significant config overhead. Deferred to a future phase.
- **`FixedBackOff` vs `ExponentialBackOff`:** `ExponentialBackOff` is better for
  downstream rate-limiting scenarios but adds configuration complexity. `FixedBackOff`
  with 3 retries at 1s is sufficient for Phase 1.
  