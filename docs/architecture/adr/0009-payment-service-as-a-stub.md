## ADR-0009: Payment Service as a Stub

**Status:** Accepted

**Date:** 2026-03-13

---

## Context

In the early stages of the platform, we want to implement a distributed workflow with the Saga Orchestrator coordinating order, inventory, and payment steps. 

However, integrating with a real payment gateway introduces additional complexity, external dependencies, and security concerns.

---

## Decision

Implement the Payment Service as a stub that simulates payment processing without connecting to a real payment gateway. The stub will:

- Accept `ProcessPaymentCommand` messages
- Randomly or deterministically respond with `PaymentProcessedEvent` or `PaymentFailedEvent`
- Maintain internal state for testing and workflow validation

---

## Implementation Details

- The service exposes the same commands/events interface as a real payment service
- Responses are deterministic or configurable for testing scenarios
- Internal logic is minimal, focusing on state transitions and event publishing

---

## Consequences

✅ **Positive:**
- Simplifies development and testing of the Saga workflow
- Removes dependency on external payment systems during early development
- Allows focus on orchestrator logic, event handling, and eventual consistency
- Enables safe experimentation without risking real financial transactions

❌ **Negative:**
- Does not represent real-world payment failures accurately
- Cannot validate integration-specific edge cases (timeouts, network errors, etc.)
- May need significant changes once real payment integration is required

---

## Alternatives Considered

- **Full payment gateway integration:** Rejected — adds complexity, requires credentials, and slows down early development.
- **Mocked responses in tests only:** Rejected — does not allow end-to-end testing of the orchestration workflow in a live environment.
