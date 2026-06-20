import os
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import httpx
import google.auth
import google.auth.transport.requests
from google.adk.sessions.vertex_ai_session_service import VertexAiSessionService
from dotenv import load_dotenv

# Load environment variables from local or parent directory .env files
load_dotenv()
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

app = FastAPI(title="Manager Dashboard")

# Setup Jinja2 templates
templates = Jinja2Templates(directory="templates")

# Configuration
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT")
AGENT_RUNTIME_ID = os.environ.get("AGENT_RUNTIME_ID")

if not PROJECT_ID or not AGENT_RUNTIME_ID:
    raise ValueError("Both GOOGLE_CLOUD_PROJECT and AGENT_RUNTIME_ID environment variables must be set.")

# Extract location from AGENT_RUNTIME_ID
location = "us-east1"
if "locations/" in AGENT_RUNTIME_ID:
    parts = AGENT_RUNTIME_ID.split("locations/")
    if len(parts) > 1:
        location = parts[1].split("/")[0]

clean_engine_id = AGENT_RUNTIME_ID
if "reasoningEngines/" in AGENT_RUNTIME_ID:
    clean_engine_id = AGENT_RUNTIME_ID.split("reasoningEngines/")[-1]

# Initialize Session Service
session_service = VertexAiSessionService(
    project=PROJECT_ID,
    location=location,
    agent_engine_id=clean_engine_id
)

USER_IDS = [
    "default-user", 
    f"pubsub-invoker@{PROJECT_ID}.iam.gserviceaccount.com",
    "pubsub-user",
    "vais-query-reasoning-engine"
]

@app.get("/", response_class=HTMLResponse)
async def get_dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="dashboard.html")

@app.get("/api/pending")
async def get_pending_approvals():
    try:
        # List sessions for configured users
        sessions = []
        for uid in USER_IDS:
            try:
                list_resp = await session_service.list_sessions(app_name=AGENT_RUNTIME_ID, user_id=uid)
                sessions.extend(list_resp.sessions)
            except Exception as e:
                print(f"Error listing sessions for user {uid}: {e}")
        
        pending = []
        for session in sessions:
            # Fetch the full session history
            full_session = await session_service.get_session(
                app_name=AGENT_RUNTIME_ID,
                user_id=session.user_id,
                session_id=session.id
            )
            if not full_session:
                continue
            
            # Find all adk_request_input function calls
            calls = []
            for event in full_session.events:
                for call in event.get_function_calls():
                    if call.name == "adk_request_input":
                        calls.append((event, call))
            
            # Find all adk_request_input responses
            responses = {}
            for event in full_session.events:
                for resp in event.get_function_responses():
                    if resp.name == "adk_request_input":
                        responses[resp.id] = resp
            
            # Check for unresolved calls
            for call_event, call in calls:
                call_id = call.id
                # Get interrupt ID (often in call.args)
                interrupt_id = call.args.get("interruptId") or call.args.get("interrupt_id") or call_id
                
                # If neither call_id nor interrupt_id has a response, it's pending!
                if call_id not in responses and interrupt_id not in responses:
                    expense = full_session.state.get("parsed_expense", {})
                    risk_assessment = full_session.state.get("risk_assessment", {})
                    
                    if not expense:
                        # Fallback parsing from the prompt message
                        expense = {
                            "amount": 0.0,
                            "submitter": "Unknown",
                            "category": "Other",
                            "description": call.args.get("message", "Pending Review"),
                            "date": ""
                        }
                    
                    # Extract risk details robustly
                    risk_score = 0
                    risk_factors = []
                    reasoning = ""
                    if risk_assessment:
                        if isinstance(risk_assessment, dict):
                            risk_score = risk_assessment.get("risk_score", 0)
                            risk_factors = risk_assessment.get("risk_factors", [])
                            reasoning = risk_assessment.get("reasoning", "")
                        else:
                            risk_score = getattr(risk_assessment, "risk_score", 0)
                            risk_factors = getattr(risk_assessment, "risk_factors", [])
                            reasoning = getattr(risk_assessment, "reasoning", "")

                    pending.append({
                        "session_id": session.id,
                        "interrupt_id": interrupt_id,
                        "call_id": call_id,
                        "expense": expense,
                        "risk_score": risk_score,
                        "risk_factors": risk_factors,
                        "reasoning": reasoning
                    })
        return pending
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/action/{session_id}")
async def resume_session(session_id: str, approve: bool):
    try:
        # Get active credentials and access token
        credentials, project = google.auth.default()
        auth_request = google.auth.transport.requests.Request()
        credentials.refresh(auth_request)
        token = credentials.token
        
        # Get the pending interrupt ID for this session to resume it
        sessions = []
        for uid in USER_IDS:
            try:
                list_resp = await session_service.list_sessions(app_name=AGENT_RUNTIME_ID, user_id=uid)
                sessions.extend(list_resp.sessions)
            except Exception as e:
                print(f"Error listing sessions for user {uid}: {e}")
        
        interrupt_id = "human_decision"  # Default fallback
        session_user_id = "default-user"
        for s in sessions:
            if s.id == session_id:
                session_user_id = s.user_id
                full_session = await session_service.get_session(
                    app_name=AGENT_RUNTIME_ID,
                    user_id=s.user_id,
                    session_id=s.id
                )
                if full_session:
                    calls = []
                    for event in full_session.events:
                        for call in event.get_function_calls():
                            if call.name == "adk_request_input":
                                calls.append(call)
                    
                    responses = set()
                    for event in full_session.events:
                        for resp in event.get_function_responses():
                            if resp.name == "adk_request_input":
                                responses.add(resp.id)
                    
                    for call in calls:
                        curr_id = call.id
                        curr_interrupt_id = call.args.get("interruptId") or call.args.get("interrupt_id") or curr_id
                        if curr_id not in responses and curr_interrupt_id not in responses:
                            interrupt_id = curr_interrupt_id
                            break
                break

        # Construct resume payload message as a dictionary
        message_payload = {
            "role": "user",
            "parts": [
                {
                    "function_response": {
                        "id": interrupt_id,
                        "name": "adk_request_input",
                        "response": {
                            "approve": approve,
                            "reject": not approve,
                            "approved": approve,
                            "comment": "Approved via Manager Dashboard" if approve else "Rejected via Manager Dashboard"
                        }
                    }
                }
            ]
        }

        # Build Reasoning Engine endpoint URL
        url = f"https://{location}-aiplatform.googleapis.com/v1/{AGENT_RUNTIME_ID}:streamQuery"
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }

        payload = {
            "class_method": "async_stream_query",
            "input": {
                "user_id": session_user_id,
                "session_id": session_id,
                "message": message_payload
            }
        }

        # Execute HTTP request to resume session
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(url, headers=headers, json=payload)
            if response.status_code != 200:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Reasoning Engine returned error: {response.text}"
                )
            
            # Consume the stream to ensure the agent finishes execution
            async for line in response.aiter_lines():
                pass
                
        return {"status": "success", "session_id": session_id}
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
