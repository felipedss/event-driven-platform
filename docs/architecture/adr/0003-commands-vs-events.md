## ADR-0003: Commands vs Events — When to Use Each

**Status:** Accepted

**Date:** 2026-03-14

**Context:**
In an event-driven system, two fundamental message types are used for inter-service communication: **Commands** and **Events**. Choosing the wrong type leads to tight coupling, unclear ownership, and brittle workflows. This ADR establishes clear guidelines for when to use each.

---

## Definitions

| Concept | Description |
|---|---|
| **Command** | An instruction to a specific service to *do something*. It has one sender and one intended receiver. Example: `ProcessPaymentCommand` |
| **Event** | A notification that *something happened*. It has one publisher but can have many (or zero) consumers. Example: `PaymentProcessedEvent` |

---

## Commands

**Intent:** Tell a service to perform a specific action.

**Characteristics:**
- Imperative: named in verb form (`PlaceOrder`, `CancelPayment`)
- Addressed to a specific recipient
- May be rejected or fail (the sender often cares about the outcome)
- Typically point-to-point (one producer → one consumer)

✅ **Use Commands when:**
- You need a specific service to perform an action and you own or know that service
- The action may be rejected and you need to handle failure explicitly
- Orchestrating a workflow step (e.g., saga orchestrator → payment service)
- You need request/reply semantics over messaging

❌ **Avoid Commands when:**
- Multiple services might want to react to the same trigger (use an Event instead)
- The sender should not know about the receiver (would introduce coupling)
- Modeling facts about the domain that already occurred

**Examples in this platform:**
```
ProcessPaymentCommand      → sent by Saga Orchestrator to Payment Service
ReserveInventoryCommand    → sent by Saga Orchestrator to Inventory Service
SendNotificationCommand    → sent by Saga Orchestrator to Notification Service
```

---

## Events

**Intent:** Announce that something happened in the domain.

**Characteristics:**
- Declarative: named in past tense (`OrderPlaced`, `PaymentFailed`)
- Publisher does not know or care who listens
- Cannot be rejected — they are facts
- Naturally support fan-out (one producer → many consumers)

✅ **Use Events when:**
- Announcing a state change that other services may want to react to
- Decoupling the producer from downstream consumers
- Enabling choreography or audit trails
- Modeling domain facts (event sourcing)

❌ **Avoid Events when:**
- You need a guaranteed, targeted action on a specific service
- You need a synchronous response or acknowledgment from a specific service
- The "event" is actually an instruction disguised as a fact

**Examples in this platform:**
```
OrderCreatedEvent          → published by Order Service, consumed by Saga Orchestrator
PaymentProcessedEvent      → published by Payment Service, consumed by Saga Orchestrator
OrderConfirmedEvent        → published by Saga Orchestrator, consumed by Order Service
```

---

## Decision

Apply the following heuristics when designing a new message:

1. **Who owns the action?** If *you* are telling a specific service what to do → Command. If *you* are informing the world what just happened → Event.
2. **Can it fail?** If the receiver can reject the request → Command. If it's a fact that already occurred → Event.
3. **How many receivers?** One specific receiver → Command. Unknown or multiple receivers → Event.
4. **Naming check:** If the message name sounds like an order (`Process`, `Cancel`, `Reserve`) → Command. If it sounds like a past fact (`Processed`, `Cancelled`, `Reserved`) → Event.

**Hybrid pattern — Command via Event bus:**
The Saga Orchestrator in this platform sends commands *over Kafka* (rather than HTTP) to preserve async decoupling and durability. These messages are Commands semantically (targeted, imperative) but transported like events. Use dedicated topics per service (e.g., `payment.commands`) to preserve the "one receiver" contract and avoid consumers picking up commands not meant for them.

---

## Consequences

✅ **Positive:**
- Clear message intent reduces ambiguity in service contracts
- Events keep producers decoupled from consumers
- Commands make workflow control flow explicit and traceable
- Naming conventions serve as living documentation

❌ **Negative:**
- Teams must learn and consistently apply the distinction
- Commands over Kafka require dedicated topics to avoid accidental fan-out
- Hybrid patterns (commands on event bus) can confuse newcomers

---

## Alternatives Considered

- **Only Events everywhere (choreography):** Rejected for complex workflows — hard to track state and apply compensation logic without a central coordinator.
- **Only Commands (RPC-style messaging):** Rejected — would tightly couple all services and eliminate the fan-out and decoupling benefits of event-driven architecture.
