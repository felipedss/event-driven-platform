# Distributed E-Commerce Platform

## Modeling Distributed Consistency with Saga Orchestration

This repository contains an experimental implementation of a distributed e-commerce platform designed to explore architectural challenges in modern event-driven systems.

The project focuses on modeling distributed workflows, managing eventual consistency, and handling failure scenarios in microservices-based environments.

Rather than abstracting complexity behind heavy frameworks, this implementation prioritizes explicit modeling of distributed transactions and architectural trade-offs.

## 🎯 Purpose
The objective of this project is to investigate and document practical challenges involved in:
- Distributed transaction coordination without 2PC
- Saga orchestration vs choreography
- State management in long-running workflows
- Failure handling and compensating actions
- Idempotency and retry mechanisms
- Service autonomy and bounded contexts

The system is intentionally built to evolve over time, allowing architectural decisions to be revisited and refined.

## 🏗 Architecture Overview
The platform consists of independent microservices:

- Order Service
- Payment Service
- Inventory Service
- Notification Service
- Saga Orchestrator

Each service:
- Owns its database
- Communicates primarily through domain events, minimizing tight coupling between services
- Maintains clear bounded context boundaries

A dedicated Saga Orchestrator coordinates distributed workflows across services.


## 🔁 Saga Orchestration Approach
This project implements a custom orchestration-based Saga pattern.

Key characteristics:
- Explicit state machine modeling
- Persisted saga state
- Compensation logic for failure scenarios
- Idempotent operations
- Retry and timeout handling

The goal is to maintain full visibility and control over distributed workflow execution.

Future iterations may include comparison with workflow engines to evaluate abstraction trade-offs.

## 📂 Architectural Documentation

All major technical decisions are documented using Architecture Decision Records (ADR):

```code
docs/architecture/adr/
```

Each ADR includes:
- Context
- Decision
- Rationale
- Trade-offs
- Alternatives considered

This ensures transparency and traceability in architectural evolution.