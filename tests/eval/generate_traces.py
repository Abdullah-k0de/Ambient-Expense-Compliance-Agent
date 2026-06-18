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

import os
import sys
from dotenv import load_dotenv
# Force override any existing env vars with the new values in .env
load_dotenv(override=True)

import json
import logging
import time
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.genai import types
from google.adk.events.request_input import RequestInput
from google.adk.events.event import Event

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Ensure current directory is in path for imports
sys.path.append(os.getcwd())
from expense_agent.agent import root_agent

def serialize_content(content):
    if not content:
        return None
    if isinstance(content, dict):
        return content
    res = {"role": getattr(content, "role", "model") or "model", "parts": []}
    parts = getattr(content, "parts", []) or []
    for part in parts:
        part_dict = {}
        if hasattr(part, "text") and part.text:
            part_dict["text"] = part.text
        elif hasattr(part, "function_call") and part.function_call:
            fc = part.function_call
            part_dict["function_call"] = {
                "name": getattr(fc, "name", ""),
                "args": getattr(fc, "args", {})
            }
        elif hasattr(part, "function_response") and part.function_response:
            fr = part.function_response
            part_dict["function_response"] = {
                "name": getattr(fr, "name", ""),
                "response": getattr(fr, "response", {})
            }
        if part_dict:
            res["parts"].append(part_dict)
    return res

def event_to_dict(event, author="expense_agent"):
    if isinstance(event, RequestInput):
        return {
            "author": author,
            "content": {
                "role": "model",
                "parts": [{"text": getattr(event, "message", "")}]
            }
        }
    
    content = getattr(event, "content", None)
    if content:
        serialized = serialize_content(content)
        if serialized and serialized.get("parts"):
            return {
                "author": author,
                "content": serialized
            }
            
    output = getattr(event, "output", None)
    if output:
        if isinstance(output, str):
            text = output
        else:
            text = json.dumps(output)
        return {
            "author": author,
            "content": {
                "role": "model",
                "parts": [{"text": text}]
            }
        }
    return None

def is_interrupt_event(event):
    if isinstance(event, RequestInput):
        return True
    
    content = getattr(event, "content", None)
    if content:
        parts = getattr(content, "parts", []) or []
        for part in parts:
            if hasattr(part, "function_call") and part.function_call:
                fc = part.function_call
                name = getattr(fc, "name", "")
                if name == "adk_request_input":
                    return True
    return False

def main():
    dataset_path = "tests/eval/datasets/basic-dataset.json"
    output_path = "artifacts/traces/generated_traces.json"
    
    logger.info(f"Loading basic dataset from {dataset_path}...")
    with open(dataset_path, "r") as f:
        dataset = json.load(f)
        
    generated_cases = []
    
    for case in dataset["eval_cases"]:
        case_id = case["eval_case_id"]
        prompt_text = case["prompt"]["parts"][0]["text"]
        logger.info(f"--- Running scenario: {case_id} ---")
        logger.info(f"Input: {prompt_text}")
        
        # Construct types.Content
        initial_message = types.Content(
            role="user",
            parts=[types.Part.from_text(text=prompt_text)]
        )
        
        first_run_events = []
        second_run_events = []
        interrupted = False
        
        # Setup session and runner
        session_service = InMemorySessionService()
        session = session_service.create_session_sync(user_id=case_id, app_name="expense_agent")
        runner = Runner(agent=root_agent, session_service=session_service, app_name="expense_agent")
        
        run_config = RunConfig(streaming_mode=StreamingMode.SSE)
        
        for event in runner.run(
            new_message=initial_message,
            user_id=case_id,
            session_id=session.id,
            run_config=run_config
        ):
            first_run_events.append(event)
            if is_interrupt_event(event):
                interrupted = True
                logger.info("Workflow interrupted. Hit human decision step.")
        
        if interrupted:
            # Automate decision: approvals for clean requests, rejections for injections/rejections
            approve = True
            comment = "Approved by automated tester."
            if "reject" in case_id or "prompt_injection" in case_id:
                approve = False
                comment = "Rejected by automated tester due to safety or policy."
                
            response_payload = {
                "approve": approve,
                "reject": not approve,
                "comment": comment
            }
            logger.info(f"Automating decision response: approve={approve}, comment='{comment}'")
            
            # Construct function response message
            resume_part = types.Part(
                function_response=types.FunctionResponse(
                    name="adk_request_input",
                    id="human_decision",
                    response=response_payload
                )
            )
            resume_message = types.Content(role="user", parts=[resume_part])
            
            # Resume running
            for event in runner.run(
                new_message=resume_message,
                user_id=case_id,
                session_id=session.id,
                run_config=run_config
            ):
                second_run_events.append(event)

                    
        # Structure into turns
        turns = []
        
        # Turn 0: initial user prompt and agent events
        turn0_events = [
            {
                "author": "user",
                "content": {
                    "role": "user",
                    "parts": [{"text": prompt_text}]
                }
            }
        ]
        for event in first_run_events:
            d = event_to_dict(event, author="expense_agent")
            if d:
                turn0_events.append(d)
                
        turns.append({
            "turn_index": 0,
            "events": turn0_events
        })
        
        # Turn 1: user resume response and agent events
        if interrupted:
            turn1_events = [
                {
                    "author": "user",
                    "content": {
                        "role": "user",
                        "parts": [{
                            "function_response": {
                                "name": "adk_request_input",
                                "id": "human_decision",
                                "response": {
                                    "approve": "reject" not in case_id and "prompt_injection" not in case_id,
                                    "reject": "reject" in case_id or "prompt_injection" in case_id,
                                    "comment": "Rejected due to safety or policy." if ("reject" in case_id or "prompt_injection" in case_id) else "Approved by automated tester."
                                }
                            }
                        }]
                    }
                }
            ]
            for event in second_run_events:
                d = event_to_dict(event, author="expense_agent")
                if d:
                    turn1_events.append(d)
                    
            turns.append({
                "turn_index": 1,
                "events": turn1_events
            })
            
        generated_cases.append({
            "eval_case_id": case_id,
            "agent_data": {
                "agents": {
                    "expense_agent": {
                        "agent_id": "expense_agent",
                        "agent_type": "Workflow",
                        "instruction": "Graph-based workflow for parsing, risk reviewing, and human-verifying expenses."
                    }
                },
                "turns": turns
            }
        })
        logger.info(f"Finished case {case_id}.\n")
        # Add a small delay between cases to prevent rate limits
        time.sleep(5)
        
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        json.dump({"eval_cases": generated_cases}, f, indent=2)
    logger.info(f"All traces serialized and saved to {output_path}")

if __name__ == "__main__":
    main()
