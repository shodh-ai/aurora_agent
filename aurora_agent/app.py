from dotenv import load_dotenv
load_dotenv() # Load environment variables as early as possible

import os
import asyncio
import base64
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import StreamingResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
import uuid

from .root_agent.agent import root_agent
from .browser_manager import browser_manager

# Monkey-patch json.JSONEncoder.default to handle bytes
_original_default = json.JSONEncoder().default
def _new_default(obj):
    if isinstance(obj, bytes):
        # The ADK telemetry tries to serialize the request, which fails on bytes.
        # We encode bytes to a base64 string to make it serializable.
        return base64.b64encode(obj).decode("utf-8")
    return _original_default(obj)

json.JSONEncoder.default = lambda self, obj: _new_default(obj)

# Verify that the API key is loaded
if not os.getenv("GOOGLE_API_KEY"):
    raise ValueError("GOOGLE_API_KEY not found in environment variables. Please ensure your .env file is correctly configured and located in the aurora-python directory.")

app = FastAPI()

# Initialize the session service globally
session_service = InMemorySessionService()

# Define the application name for ADK sessions
APP_NAME = "aurora"

# Dictionary to store session IDs per client host (for InMemorySessionService)
# This needs to be global to persist across requests
client_sessions = {}

# Instantiate the Runner with the root_agent, app_name, and session_service.
runner = Runner(
    agent=root_agent, 
    app_name=APP_NAME, 
    session_service=session_service
)

class ChatRequest(BaseModel):
    message: str

@app.on_event("startup")
async def startup_event():
    # Start the browser when the application starts
    await browser_manager.start_browser()

@app.on_event("shutdown")
async def shutdown_event():
    # Close the browser when the application stops
    await browser_manager.close_browser()

async def stream_agent_response(message: str, client_host: str):
    """Stream responses from the root agent using the ADK Runner."""
    print("\n--- NEW REQUEST ---")
    print(f"Received message: {message}")
    user_id = f"user_{client_host}"
    print(f"User ID: {user_id}")
    
    # Get or create session ID for this client host
    if user_id not in client_sessions:
        session_id = str(uuid.uuid4())
        client_sessions[user_id] = session_id
        # Create a new session in InMemorySessionService
        session_service.create_session(
            app_name=APP_NAME, 
            user_id=user_id, 
            session_id=session_id, 
            state={}
        )
        print(f"Created new session for {user_id}: {session_id}")
    else:
        session_id = client_sessions[user_id]
        print(f"Continuing session for {user_id}: {session_id}")

    # The agent will now use the last screenshot that was sent to the frontend.
    screenshot_bytes = browser_manager.last_sent_screenshot_bytes
    
    parts = [types.Part(text=message)]
    if screenshot_bytes:
        screenshot_part = types.Part(
            inline_data=types.Blob(
                mime_type="image/jpeg",
                data=screenshot_bytes
            )
        )
        parts.insert(0, screenshot_part)

    new_message_content = types.Content(role="user", parts=parts)
    print("\n--- CONTENT SENT TO RUNNER ---")
    if screenshot_bytes:
        print("New message content includes screenshot data.")
    else:
        print("New message content does not include screenshot data.")
    print(f"Text message: {message}")
    print("--------------------------")
    
    # The runner.run_async() method returns an async generator of events
    async for event in runner.run_async(
        user_id=user_id, 
        session_id=session_id, 
        new_message=new_message_content
    ):
        print(f"\n--- EVENT FROM RUNNER ---")
        # Only print the text content of the event to avoid logging large image strings
        if event.content and event.content.parts:
            for part in event.content.parts:
                if hasattr(part, "text") and part.text:
                    print(f"Event Text Part: {part.text}")
                elif hasattr(part, "function_call") and part.function_call:
                    print(f"Event Function Call: {part.function_call.name}")
                elif hasattr(part, "function_response") and part.function_response:
                    if part.function_response.name == "get_latest_screenshot":
                        print(f"Event Function Response: {part.function_response.name}\nResponse: (Screenshot data - omitted for brevity)")
                    else:
                        print(f"Event Function Response: {part.function_response.name}\nResponse: {part.function_response.response}")
        print("-------------------------")
        # Check if the event has content and parts, and if the part has text
        if event.content and event.content.parts:
            for part in event.content.parts:
                if hasattr(part, "text") and part.text:
                    print(f"\n--- RESPONSE TO USER ---")
                    print(part.text)
                    print("------------------------")
                    yield part.text

@app.post("/api/chat")
async def chat_handler(request: ChatRequest, req: Request):
    """Handle chat requests and stream agent responses."""
    client_host = req.client.host
    generator = stream_agent_response(request.message, client_host)
    return StreamingResponse(generator, media_type="text/plain")

@app.websocket("/ws/agent")
async def agent_websocket_endpoint(websocket: WebSocket):
    """Handle the WebSocket connection for streaming the browser view."""
    await websocket.accept()
    try:
        while True:
            # This call now also updates browser_manager.last_sent_screenshot_bytes
            screenshot_bytes = await browser_manager.get_screenshot()
            if screenshot_bytes:
                await websocket.send_bytes(screenshot_bytes)
            await asyncio.sleep(0.5) # Adjust sleep time as needed
    except WebSocketDisconnect:
        print("Client disconnected.")
    except Exception as e:
        print(f"An error occurred in WebSocket: {e}")




# Add CORS middleware to allow requests from the frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"], # Adjust for your frontend URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/test/screenshot")
async def test_screenshot():
    try:
        screenshot_path = await browser_manager.get_screenshot()
        if os.path.exists(screenshot_path):
            return FileResponse(screenshot_path)
        else:
            return {"error": "Screenshot file not found."}
    except Exception as e:
        return {"error": f"Failed to get screenshot: {e}"}

@app.get("/test/navigate")
async def test_navigate(url: str):
    try:
        result = await browser_manager.navigate(url)
        return {"status": result}
    except Exception as e:
        return {"error": f"Failed to navigate: {e}"}

if __name__ == "__main__":
    import uvicorn
    # Run the FastAPI application
    uvicorn.run(app, host="0.0.0.0", port=8000)