## ADR-0012: Kafka Producer Idempotency via Idempotent Producer Config

**Status:** Accepted

**Context:**

A Kafka producer retries a `send()` after a transient network error or broker
unavailability. The broker may have already written the first attempt, resulting in a
duplicate message on the topic. This is invisible to the application — no exception
is thrown, no log is written — making it the hardest layer to detect.

**Decision:**

Enable the **idempotent producer** configuration on all services:

```yaml
spring:
  kafka:
    producer:
      properties:
        enable.idempotence: true
        max.in.flight.requests.per.connection: 5
      acks: all
      retries: 3
```

Kafka assigns each producer a unique `ProducerID` and tracks a sequence number per
partition. If the broker receives a message with the same `ProducerID + sequence`, it
deduplicates silently. No application code changes are required.

**Affected services:** all services that produce to Kafka — `order-service`,
`inventory-service`, `payment-service`, `saga-orchestrator`.

✅ **Pros:**
- Zero application code changes — purely configuration
- Deduplication happens at the broker level, strongest transport-layer guarantee
- Pairs naturally with `acks: all` for write durability
- No performance impact under normal operation

❌ **Cons:**
- Only covers duplicates within the same producer session — `ProducerID` resets on
  producer restart, so a message sent before a crash and resent after restart is not
  deduplicated at this layer (covered by ADR-0011 at the consumer layer)
- Does not cover application-level bugs that call `send()` twice intentionally
- Requires `acks: all` and `max.in.flight.requests.per.connection ≤ 5` — slight
  latency increase under high throughput

**Alternatives Considered:**

- **Kafka transactions (exactly-once):** wraps produce + consumer offset commit in a
  single atomic transaction. Eliminates duplicates across restarts but requires
  transactional producers, consumers, and coordination with the DB transaction.
  Deferred to a future phase.
- **Application-level deduplication on the consumer side only (ADR-0011):** sufficient
  for cross-restart duplicates but does not eliminate the duplicate message from the
  topic itself, increasing unnecessary consumer-side work. Both layers are complementary.
