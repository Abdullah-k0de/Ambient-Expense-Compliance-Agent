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

import base64
import datetime
from enum import Enum
import json
import os
import re
from typing import Any, Literal

import google.auth
from dotenv import load_dotenv

from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.apps import App
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.models import Gemini
from google.adk.workflow import Workflow, node
from google.genai import types
from pydantic import BaseModel, Field

from . import config

load_dotenv()

# Setup environment variables
try:
    _, project_id = google.auth.default()
    if project_id:
        os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
except Exception:
    if "GOOGLE_CLOUD_PROJECT" not in os.environ:
        os.environ["GOOGLE_CLOUD_PROJECT"] = "ambient-expense-agent"

if "GOOGLE_CLOUD_LOCATION" not in os.environ:
    os.environ["GOOGLE_CLOUD_LOCATION"] = "global"
if "GOOGLE_GENAI_USE_VERTEXAI" not in os.environ:
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"


# Schemas
class ExpenseRecord(BaseModel):
    amount: float = Field(default=0.0)
    submitter: str = Field(default="Unknown")
    category: str = Field(default="Other")
    description: str = Field(default="")
    date: str = Field(default="")


class RiskAssessment(BaseModel):
    risk_score: int = Field(description="A risk score from 1 (low) to 10 (high) based on the expense details")
    risk_factors: list[str] = Field(description="List of identified risk factors or compliance concerns")
    reasoning: str = Field(description="Explanation of the risk assessment score and factors")


class ApprovalResponse(BaseModel):
    approve: bool = Field(default=False, description="Check to approve the expense claim")
    reject: bool = Field(default=False, description="Check to reject the expense claim")
    comment: str = Field(default="", description="Optional comments or notes")


# Security Patterns
SSN_REGEX = re.compile(r'\b\d{3}-\d{2}-\d{4}\b|\b\d{9}\b')
CREDIT_CARD_REGEX = re.compile(r'\b(?:\d[ -]?){13,16}\b')

INJECTION_KEYWORDS = [
    r"ignore (?:previous )?instructions",
    r"system override",
    r"bypass (?:the )?rules",
    r"auto-approve",
    r"force approval",
    r"you are now",
    r"don't review",
    r"override rules",
    r"forget what I said"
]
INJECTION_PATTERNS = [re.compile(pattern, re.IGNORECASE) for pattern in INJECTION_KEYWORDS]


# Helper function to scrub PII
def scrub_pii(text: str) -> tuple[str, list[str]]:
    redacted = []
    if SSN_REGEX.search(text):
        text = SSN_REGEX.sub("[REDACTED SSN]", text)
        redacted.append("SSN")
    if CREDIT_CARD_REGEX.search(text):
        text = CREDIT_CARD_REGEX.sub("[REDACTED CREDIT CARD]", text)
        redacted.append("Credit Card")
    return text, redacted


# Helper function to detect prompt injection
def is_prompt_injection(text: str) -> bool:
    for pattern in INJECTION_PATTERNS:
        if pattern.search(text):
            return True
    return False


# Helper function to extract and parse JSON/base64 data
def extract_expense_data(node_input: Any) -> dict:
    raw_data = {}
    if isinstance(node_input, dict):
        raw_data = node_input
    elif hasattr(node_input, "parts") and node_input.parts:
        raw_input = "".join(part.text for part in node_input.parts if part.text)
        try:
            raw_data = json.loads(raw_input)
        except Exception:
            raw_data = {"data": raw_input}
    elif isinstance(node_input, str):
        try:
            raw_data = json.loads(node_input)
        except Exception:
            raw_data = {"data": node_input}

    # Extract inner data
    data_content = raw_data.get("data")
    if not data_content:
        # Fallback: maybe the payload itself has the expense fields
        data_content = raw_data

    # Handle base64 Pub/Sub or raw JSON string
    if isinstance(data_content, str):
        try:
            decoded = base64.b64decode(data_content).decode("utf-8")
            data_content = json.loads(decoded)
        except Exception:
            try:
                data_content = json.loads(data_content)
            except Exception:
                pass

    if not isinstance(data_content, dict):
        data_content = {}
        
    return data_content


# 1. Preprocess and Route Node
@node
def preprocess_input(ctx: Context, node_input: Any):
    """Parses Pub/Sub JSON/base64 payload and routes based on the dollar threshold."""
    data_dict = extract_expense_data(node_input)
    
    amount = float(data_dict.get("amount", 0.0))
    submitter = str(data_dict.get("submitter", "Unknown"))
    category = str(data_dict.get("category", "Other"))
    description = str(data_dict.get("description", ""))
    date = str(data_dict.get("date", ""))
    
    record = ExpenseRecord(
        amount=amount,
        submitter=submitter,
        category=category,
        description=description,
        date=date
    )
    
    ctx.state["parsed_expense"] = record.model_dump()
    
    # Rule check in Python: Under $100 -> auto-approve; >= $100 -> LLM review (via Security Checkpoint)
    if amount < config.DOLLAR_THRESHOLD:
        return Event(
            output=record.model_dump(),
            route="auto_approve"
        )
    else:
        return Event(
            output=record.model_dump(),
            route="llm_review"
        )


# 2. Security Checkpoint Node (PII Scrubbing and Prompt Injection Defense)
@node
def security_checkpoint(ctx: Context, node_input: dict):
    """Checkpoint to scrub PII and filter out prompt injections before LLM review."""
    expense = ctx.state.get("parsed_expense", {})
    desc = expense.get("description", "")
    
    # Scrub SSN and Credit Card numbers
    scrubbed_desc, redacted_categories = scrub_pii(desc)
    ctx.state["redacted_categories"] = redacted_categories
    
    # Update state-stored expense with the scrubbed description (ensures clean logs/human payload)
    expense["description"] = scrubbed_desc
    ctx.state["parsed_expense"] = expense
    
    # Detect adversarial prompt injection attempts
    if is_prompt_injection(scrubbed_desc):
        ctx.state["security_event"] = True
        
        # Bypass LLM and route directly to human with warning assessment
        security_assessment = {
            "risk_score": 10,
            "risk_factors": ["Prompt Injection Attempt Detected", "Security Policy Violation"],
            "reasoning": "Adversarial prompt injection patterns were detected in the description. The automated LLM review was bypassed to prevent model manipulation."
        }
        return Event(
            output=security_assessment,
            route="security_alert",
            state={"security_event": True, "risk_assessment": security_assessment}
        )
        
    # Build clean string for the LLM reviewer
    submitter = expense.get("submitter", "Unknown")
    amount = expense.get("amount", 0.0)
    category = expense.get("category", "Other")
    date = expense.get("date", "")
    today_str = datetime.date.today().isoformat()
    
    clean_expense_str = (
        f"Please review this expense for potential risk factors:\n"
        f"- Current Date (Today): {today_str}\n"
        f"- Submitter: {submitter}\n"
        f"- Amount: ${amount:.2f}\n"
        f"- Category: {category}\n"
        f"- Description: {scrubbed_desc}\n"
        f"- Date: {date}\n"
    )
    
    return Event(
        output=clean_expense_str,
        route="clean"
    )


# 3. LLM Risk Review Node
llm_risk_review = LlmAgent(
    name="llm_risk_review",
    model=Gemini(
        model=config.MODEL_NAME,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=(
        "You are an expense compliance reviewer. Review the provided expense details. "
        "Identify any potential risk factors (e.g. personal expenses, suspicious amounts, "
        "compliance issues, excessive pricing) and assign a risk score from 1 to 10."
    ),
    output_schema=RiskAssessment,
    output_key="risk_assessment",
)


# 4. Auto-Approve Node (Under threshold)
@node
def auto_approve(ctx: Context, node_input: dict):
    """Instantly auto-approves expenses under the threshold without LLM intervention."""
    yield Event(
        output={"status": "approved", "approver": "system", "risk": None},
        state={"approval_status": "approved", "approver_type": "system"}
    )


# 5. Human Approval Node (Pauses workflow using RequestInput)
@node(rerun_on_resume=True)
def human_approval(ctx: Context, node_input: dict):
    """Pauses the workflow for a human decision after the LLM has completed the risk assessment."""
    expense = ctx.state.get("parsed_expense", {})
    risk = node_input  # From llm_risk_review OR security_checkpoint
    
    if not ctx.resume_inputs or "human_decision" not in ctx.resume_inputs:
        msg = (
            f"Alert: Human Approval Required for expense of ${expense.get('amount'):.2f} "
            f"({expense.get('description')}) submitted by {expense.get('submitter')}.\n"
            f"Risk Score: {risk.get('risk_score')}/10\n"
            f"Risk Factors: {', '.join(risk.get('risk_factors', []))}\n"
            f"Reasoning: {risk.get('reasoning')}\n"
            f"Do you want to approve or reject this expense?"
        )
        yield RequestInput(
            interrupt_id="human_decision",
            message=msg,
            response_schema=ApprovalResponse
        )
        return
        
    response_data = ctx.resume_inputs["human_decision"]
    if isinstance(response_data, dict):
        approved = response_data.get("approve", False)
        rejected = response_data.get("reject", False)
    elif isinstance(response_data, str):
        val = response_data.strip().lower()
        approved = val in ["approve", "approved", "yes", "y", "true"]
        rejected = val in ["reject", "rejected", "no", "n", "false"]
    elif isinstance(response_data, bool):
        approved = response_data
        rejected = not response_data
    else:
        approved = bool(getattr(response_data, "approve", False))
        rejected = bool(getattr(response_data, "reject", False))
        
    if approved and not rejected:
        status = "approved"
    else:
        # Defaults to rejected if both are checked, neither is checked, or reject is checked
        status = "rejected"
        
    yield Event(
        output={"status": status, "approver": "human", "risk": risk},
        state={"approval_status": status, "approver_type": "human"}
    )


# 6. Record Outcome Node (Aggregates state and writes message)
@node
def record_outcome(ctx: Context, node_input: Any):
    """Records the final approval/rejection decision and updates the expense history."""
    expense = ctx.state.get("parsed_expense", {})
    
    # Safely handle node_input types
    input_dict = node_input if isinstance(node_input, dict) else {}
    status = input_dict.get("status") or "unknown"
    approver = input_dict.get("approver") or "unknown"
    risk = input_dict.get("risk")
    
    outcome_record = {
        "expense": expense,
        "status": status,
        "approver": approver,
        "risk": risk
    }
    
    history = ctx.state.get("history", [])
    history.append(outcome_record)
    
    msg = (
        f"Expense for '{expense.get('description')}' (${expense.get('amount'):.2f}) "
        f"submitted by {expense.get('submitter')} was {status.upper()} by {approver.upper()}."
    )
    yield Event(
        output=msg,
        content=types.Content(role="model", parts=[types.Part.from_text(text=msg)]),
        state={"history": history}
    )



# Workflow definition
root_agent = Workflow(
    name="expense_workflow",
    edges=[
        ("START", preprocess_input),
        (preprocess_input, {"auto_approve": auto_approve, "llm_review": security_checkpoint}),
        (security_checkpoint, {"clean": llm_risk_review, "security_alert": human_approval}),
        (auto_approve, record_outcome),
        (llm_risk_review, human_approval),
        (human_approval, record_outcome),
    ],
    description="Graph-based workflow for parsing, risk reviewing, and human-verifying expenses."
)

app = App(
    root_agent=root_agent,
    name="expense_agent",
)
