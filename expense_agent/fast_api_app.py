# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import json
import logging
import os

from fastapi import FastAPI, Request
from google.adk.cli.fast_api import get_fast_api_app

from expense_agent.app_utils.telemetry import setup_telemetry
from expense_agent.app_utils.typing import Feedback

# Setup standard Python logging for console logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

setup_telemetry()

allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)

# Artifact bucket for ADK (created by Terraform, passed via env var)
logs_bucket_name = os.environ.get("LOGS_BUCKET_NAME")

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# In-memory session configuration - no persistent storage
session_service_uri = None

artifact_service_uri = f"gs://{logs_bucket_name}" if logs_bucket_name else None

app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,  # Enable chat/Dev UI
    artifact_service_uri=artifact_service_uri,
    allow_origins=allow_origins,
    session_service_uri=session_service_uri,
    otel_to_cloud=False,  # Disable Cloud Trace / OTel exports
    trigger_sources=["pubsub"],  # Enable Pub/Sub trigger source
)
app.title = "ambient-expense-agent"
app.description = "API for interacting with the Agent ambient-expense-agent"


# Custom middleware to normalize fully-qualified Pub/Sub subscription paths
@app.middleware("http")
async def normalize_pubsub_subscription(request: Request, call_next):
    logger.info(f"Middleware intercepted request to path: {request.url.path}, method: {request.method}")
    if request.url.path.endswith("/trigger/pubsub") and request.method == "POST":
        body = await request.body()
        logger.info(f"Middleware reading body: {body.decode('utf-8', errors='ignore')}")
        if body:
            try:
                data = json.loads(body)
                subscription = data.get("subscription")
                logger.info(f"Middleware found subscription: {subscription}")
                if subscription and isinstance(subscription, str) and "/" in subscription:
                    normalized = subscription.split("/")[-1]
                    logger.info(f"Middleware normalizing subscription from {subscription} to {normalized}")
                    data["subscription"] = normalized
                    new_body = json.dumps(data).encode("utf-8")
                    # Update both cache and stream receiver
                    request._body = new_body
                    async def receive():
                        return {"type": "http.request", "body": new_body, "more_body": False}
                    request._receive = receive
            except Exception:
                logger.exception("Failed to normalize Pub/Sub subscription path in middleware")
    return await call_next(request)


@app.post("/feedback")
def collect_feedback(feedback: Feedback) -> dict[str, str]:
    """Collect and log feedback.

    Args:
        feedback: The feedback data to log

    Returns:
        Success message
    """
    logger.info(f"Feedback received: {feedback.model_dump()}")
    return {"status": "success"}


# Main execution serving on port 8080
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)

