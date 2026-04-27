## ADR-0014: Step-Based Saga Orchestration Model

**Status:** Proposed

## Context

ADR-0006 intentionally started with a simplified saga orchestrator to validate the event-driven flow before investing in orchestration infrastructure.

That approach helped prove:

- Kafka integration
- serialization and consumers
- end-to-end request/reply flow
- basic saga persistence

However, as the workflow evolves to include inventory reservation, payment processing, and compensation logic, the current procedural orchestration model becomes harder to extend and reason about.

Adding new steps increases complexity in centralized orchestration logic and makes compensation paths harder to manage.

A more structured orchestration model is needed.

---

## Decision

Refactor the orchestrator to a step-based saga model.

Instead of hardcoding workflow logic in one orchestrator flow, each business step will encapsulate its own behavior.

Each step will own:

- execution logic
- compensation logic
- transition rules

Example steps:

- ReserveInventoryStep
- ProcessPaymentStep
- ConfirmOrderStep

Each step implements a common contract:

```java
public interface SagaStep {

    SagaStatus getStatus();

    void execute(SagaContext context);

    void compensate(SagaContext context);

    boolean canHandle(Event event);

    SagaStatus nextStatus(Event event);
}
```

The orchestrator becomes a coordinator that delegates execution to steps, rather than concentrating all workflow logic.

---

## Initial Flow

Happy path:

ORDER_CREATED
→ INVENTORY_PENDING
→ INVENTORY_RESERVED
→ PAYMENT_PENDING
→ PAYMENT_PROCESSED
→ COMPLETED

Failure path:

PAYMENT_FAILED
→ COMPENSATING
→ INVENTORY_RELEASED
→ CANCELLED

---

## Consequences

### Positive

- Clearer separation of workflow responsibilities
- Easier to add new steps
- Compensation logic stays close to the step it belongs to
- Explicit state transitions
- Better testability per step
- Cleaner foundation for retries, observability and DLQ handling later

### Negative

- More abstractions and classes
- More complex than the current implementation
- Risk of overengineering if turned into a generic workflow framework
- Transition logic needs strong testing

---

## Alternatives Considered

### Keep procedural orchestration

Rejected.

Simple initially, but becomes rigid as workflow complexity grows.

---

### Full choreography

Rejected.

Makes the business flow harder to trace and reason about.

See ADR-0003.

---

### Adopt a workflow engine (Temporal/Camunda)

Deferred.

Could provide richer guarantees, but adds operational complexity and reduces the educational value of implementing orchestration directly in this project.

---

## Implementation Notes

Keep the first version simple.

Focus only on:

- order creation
- inventory reservation
- payment processing
- compensation on failure

Avoid building a generic workflow engine too early.

---

## Related ADRs

- ADR-0003 Saga Orchestration vs Choreography
- ADR-0006 Phased Saga Implementation
- ADR-0011 Kafka Consumer Idempotency
- ADR-0013 Manual Acknowledgment and DLQ