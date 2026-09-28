#!/usr/bin/env python3
"""Zero-cost local stand-in for the OpenAI Chat Completions API.

Lets you exercise the whole AI Saga Analyzer pipeline (both prompt-chain stages, the
provider client, stage validation, and the controller) without calling the real OpenAI
API or spending any money. Uses only the Python standard library.

Usage:
    python3 mock_openai_server.py [port]   # default port 4000

Then point the orchestrator at it (no code/config changes needed — these are just env
var overrides for the existing ai.openai.* @ConfigurationProperties):
    export OPENAI_API_KEY=not-used
    export AI_OPENAI_BASE_URL=http://localhost:4000/v1
    mvn spring-boot:run

It distinguishes Execution Analysis vs Failure Classification requests by the prompt
text our stages actually send (see ExecutionAnalysisStage/FailureClassificationStage's
buildUserPrompt), and returns the SagaStatus -> ExecutionAssessment mapping our own
FailureClassificationStage validates against (plan section 2.3), so responses pass
validation and you'll see genuine `complete: true` results.

To see the fabrication/validation guards reject something instead, edit
ASSESSMENT_BY_STATUS below to return a deliberately wrong value (e.g. force
PAYMENT_FAILED -> TERMINAL_FAILURE) and watch the response come back
`complete: false` with a VALIDATION_FAILED stage summary.
"""

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

ASSESSMENT_BY_STATUS = {
    "STARTED": "IN_PROGRESS",
    "INVENTORY_PENDING": "IN_PROGRESS",
    "INVENTORY_CONFIRMED": "IN_PROGRESS",
    "INVENTORY_FAILED": "TERMINAL_FAILURE",
    "PAYMENT_PENDING": "IN_PROGRESS",
    "PAYMENT_CONFIRMED": "IN_PROGRESS",
    "PAYMENT_FAILED": "COMPENSATING",
    "COMPENSATING": "COMPENSATING",
    "COMPLETED": "TERMINAL_SUCCESS",
    "CANCELLED": "TERMINAL_FAILURE",
}


def stage1_response(user_content: str) -> dict:
    status_match = re.search(r"Current status:\s*(\S+)", user_content)
    phases_match = re.search(r"Deterministically reached phases:\s*\[(.*?)\]", user_content)
    status = status_match.group(1) if status_match else "UNKNOWN"
    phases = [p.strip() for p in (phases_match.group(1) if phases_match else "").split(",") if p.strip()]
    return {
        "currentState": status,
        "reachedPhases": phases,
        "narrativeSummary": f"Saga is currently in status {status}.",
        "dataCompleteness": {
            "onlyCurrentStatusSnapshotAvailable": True,
            "stepByStepEventHistoryAvailable": False,
            "dlqCheckAvailable": False,
            "retryCountKnown": False,
            "limitations": [
                "Only the current status snapshot is available; no step-by-step event "
                "history, DLQ contents, or retry counts exist in this system."
            ],
        },
    }


def stage2_response(user_content: str) -> dict:
    status_match = re.search(r"Execution Analysis current state:\s*(\S+)", user_content)
    status = status_match.group(1) if status_match else "UNKNOWN"
    assessment = ASSESSMENT_BY_STATUS.get(status, "UNKNOWN")
    return {
        "category": "UNKNOWN",
        "executionAssessment": assessment,
        "confidence": 0.9,
        "reasoning": f"Status {status} maps to {assessment} per the saga's state machine.",
    }


class MockOpenAiHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        messages = body.get("messages", [])
        user_content = next((m["content"] for m in messages if m.get("role") == "user"), "")

        # Stage 1's prompt starts with "Saga ID: ..."; stage 2's with "Execution Analysis
        # current state: ..." (see each stage's buildUserPrompt).
        content = stage1_response(user_content) if "Saga ID:" in user_content else stage2_response(user_content)

        response = {
            "model": "mock-model",
            "choices": [{"message": {"role": "assistant", "content": json.dumps(content)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10},
        }
        payload = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):  # quieter default logging
        print("[mock-openai]", *args)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 4000
    print(f"Mock OpenAI server listening on http://localhost:{port}/v1/chat/completions")
    HTTPServer(("localhost", port), MockOpenAiHandler).serve_forever()
