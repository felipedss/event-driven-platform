## ADR-0005: Phased Saga Implementation — Basic First, Step-Based Later

**Status:** Accepted

**Context:**
Building a full step-based saga engine from the start requires significant upfront investment before any end-to-end flow is validated. The risk is spending time on infrastructure that turns out to need rethinking once real integration pain points are discovered. We need a working system that proves the event-driven flow (Order Service → Orchestrator → reply) before committing to the internal orchestrator design.

**Decision:**
Implement the saga orchestrator in two phases:

- **Phase 1 (current):** Straightforward implementation. On receiving `OrderCreatedEvent`, the orchestrator immediately persists a saga record (`STARTED → COMPLETED`) and publishes `OrderConfirmedEvent`. No actual inventory or payment steps are executed. Goal: validate Kafka integration, serialization, and the full round-trip event flow.

- **Phase 2 (planned):** Refactor to a step-based model. Implement concrete `SagaStep` classes (e.g. `ReserveInventoryStep`, `ChargePaymentStep`) with `execute()` and `compensate()` methods, driven by a generic saga engine. Each step is idempotent and carries its own rollback logic.

The `SagaStep` interface, `SagaContext`, and all `SagaStatus` states are already defined in Phase 1 to make the Phase 2 refactor straightforward.

**Consequences:**

✅ **Positive:**
- End-to-end flow validated early with minimal complexity
- Kafka topics, serialization, and consumer configuration proven before saga logic is layered in
- `SagaStep` interface ready — Phase 2 is a refactor, not a rewrite
- Easier to onboard and reason about Phase 1 code

❌ **Negative:**
- Phase 1 does not exercise compensation logic — failures are not handled
- Technical debt: the `INVENTORY_PENDING`, `PAYMENT_PENDING`, etc. states exist in the schema but are unused until Phase 2
- Risk of Phase 2 being deferred indefinitely if Phase 1 "just works"

**Alternatives Considered:**
- Full step-based implementation from day one (rejected — too much upfront complexity before flow is validated)
- Choreography without an orchestrator (rejected — see ADR-0003)
