## ADR-0008: Service Failure Handling in Sagas

**Status:** Accepted

**Date:** 2026-03-13

---

## Context

In a distributed e-commerce system using orchestration-based Sagas, individual services (Order, Inventory, Payment, etc.) may fail while processing their respective actions. 

For example, reserving inventory could fail due to insufficient stock, or database operations could throw exceptions. 

Without a clear strategy, failures can leave workflows stuck in intermediate states, breaking consistency and traceability.

---

## Decision

Each service must handle internal exceptions gracefully and publish a failure event to the Saga Orchestrator instead of throwing raw exceptions. The orchestrator reacts to these failure events by applying compensating actions, triggering retries if appropriate, and updating saga state.

---

## Implementation Guidelines

- Wrap critical operations in `try/catch` blocks.
- On failure, publish a dedicated failure event, such as:
  - `InventoryReservationFailedEvent`
  - `PaymentProcessingFailedEvent`
- Include sufficient context in the failure event to allow compensating actions.
- Ensure idempotency for both success and failure events.
- The orchestrator handles failure events uniformly: updates saga state and triggers compensations.

---

## Consequences

✅ **Positive:**
- Saga orchestration remains consistent even when individual services fail
- Clear visibility into workflow state and failures
- Enables automatic retries and compensating transactions
- Supports observability and auditing for all failure scenarios

❌ **Negative:**
- Additional events and topics increase message traffic
- Services must standardize failure event formats
- Orchestrator logic becomes slightly more complex to handle various failure scenarios

---

## Alternatives Considered

- **Let exceptions bubble to the orchestrator:** Rejected — causes tight coupling between services and orchestrator, reduces reliability, and breaks idempotency.
- **Retries inside services without publishing failure:** Rejected — orchestrator loses visibility of failures, making compensation harder and risking stuck workflows.
