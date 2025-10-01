#!/usr/bin/env python3
"""
Command and Control Server
Manages remote clients, executes commands, and distributes work queues
"""

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Dict, List, Optional, Any
import asyncio
import uuid
import json
from datetime import datetime
from collections import defaultdict
import uvicorn

app = FastAPI(title="C2 Server")

# Data models
class ClientRegistration(BaseModel):
    hostname: str
    platform: str
    ip_address: str

class CommandRequest(BaseModel):
    client_ids: List[str]
    command: str
    timeout: Optional[int] = 30

class WorkItem(BaseModel):
    task_id: str
    command: str
    args: Dict[str, Any] = {}

class WorkQueue(BaseModel):
    items: List[WorkItem]

# Global state
clients: Dict[str, dict] = {}
websockets: Dict[str, WebSocket] = {}
command_results: Dict[str, dict] = {}
work_queues: Dict[str, List[WorkItem]] = defaultdict(list)
pending_commands: Dict[str, asyncio.Future] = {}

@app.get("/")
async def root():
    return {
        "service": "C2 Server",
        "status": "running",
        "clients_connected": len(clients)
    }

@app.post("/register")
async def register_client(reg: ClientRegistration):
    """Register a new client"""
    client_id = str(uuid.uuid4())
    clients[client_id] = {
        "id": client_id,
        "hostname": reg.hostname,
        "platform": reg.platform,
        "ip_address": reg.ip_address,
        "registered_at": datetime.now().isoformat(),
        "last_seen": datetime.now().isoformat(),
        "status": "registered"
    }
    return {"client_id": client_id, "message": "Client registered successfully"}

@app.get("/clients")
async def list_clients():
    """List all registered clients"""
    return {"clients": list(clients.values())}

@app.get("/clients/{client_id}")
async def get_client(client_id: str):
    """Get details about a specific client"""
    if client_id not in clients:
        raise HTTPException(status_code=404, detail="Client not found")
    return clients[client_id]

@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    """WebSocket connection for real-time client communication"""
    await websocket.accept()
    
    if client_id not in clients:
        await websocket.close(code=1008, reason="Client not registered")
        return
    
    websockets[client_id] = websocket
    clients[client_id]["status"] = "connected"
    
    try:
        while True:
            data = await websocket.receive_json()
            
            # Handle command results
            if data.get("type") == "command_result":
                cmd_id = data.get("command_id")
                if cmd_id in pending_commands:
                    pending_commands[cmd_id].set_result(data)
                command_results[cmd_id] = {
                    "client_id": client_id,
                    "result": data,
                    "timestamp": datetime.now().isoformat()
                }
            
            # Handle heartbeat
            elif data.get("type") == "heartbeat":
                clients[client_id]["last_seen"] = datetime.now().isoformat()
                await websocket.send_json({"type": "heartbeat_ack"})
            
            # Handle work completion
            elif data.get("type") == "work_complete":
                task_id = data.get("task_id")
                print(f"Work item {task_id} completed by {client_id}")
                
    except WebSocketDisconnect:
        if client_id in websockets:
            del websockets[client_id]
        clients[client_id]["status"] = "disconnected"
    except Exception as e:
        print(f"WebSocket error for {client_id}: {e}")
        if client_id in websockets:
            del websockets[client_id]
        clients[client_id]["status"] = "error"

@app.post("/execute")
async def execute_command(cmd_req: CommandRequest):
    """Execute a command on one or more clients"""
    results = {}
    
    for client_id in cmd_req.client_ids:
        if client_id not in clients:
            results[client_id] = {"error": "Client not found"}
            continue
        
        if client_id not in websockets:
            results[client_id] = {"error": "Client not connected"}
            continue
        
        command_id = str(uuid.uuid4())
        future = asyncio.Future()
        pending_commands[command_id] = future
        
        # Send command to client
        await websockets[client_id].send_json({
            "type": "execute",
            "command_id": command_id,
            "command": cmd_req.command,
            "timeout": cmd_req.timeout
        })
        
        # Wait for result with timeout
        try:
            result = await asyncio.wait_for(future, timeout=cmd_req.timeout)
            results[client_id] = result
        except asyncio.TimeoutError:
            results[client_id] = {"error": "Command timeout"}
        finally:
            if command_id in pending_commands:
                del pending_commands[command_id]
    
    return {"results": results}

@app.post("/work/queue")
async def add_work_queue(queue_name: str, queue: WorkQueue):
    """Add work items to a queue"""
    work_queues[queue_name].extend(queue.items)
    return {
        "queue_name": queue_name,
        "items_added": len(queue.items),
        "total_items": len(work_queues[queue_name])
    }

@app.get("/work/queue/{queue_name}")
async def get_work_queue(queue_name: str):
    """Get work queue status"""
    return {
        "queue_name": queue_name,
        "items": work_queues[queue_name],
        "count": len(work_queues[queue_name])
    }

@app.post("/work/distribute/{queue_name}")
async def distribute_work(queue_name: str):
    """Distribute work items across connected clients"""
    if queue_name not in work_queues or not work_queues[queue_name]:
        raise HTTPException(status_code=404, detail="Queue not found or empty")
    
    connected_clients = [cid for cid in clients if cid in websockets]
    
    if not connected_clients:
        raise HTTPException(status_code=400, detail="No clients connected")
    
    distribution = defaultdict(list)
    items = work_queues[queue_name].copy()
    
    # Round-robin distribution
    for idx, item in enumerate(items):
        client_id = connected_clients[idx % len(connected_clients)]
        distribution[client_id].append(item)
    
    # Send work to clients
    for client_id, work_items in distribution.items():
        await websockets[client_id].send_json({
            "type": "work_batch",
            "queue_name": queue_name,
            "items": [item.dict() for item in work_items]
        })
    
    # Clear the queue
    work_queues[queue_name].clear()
    
    return {
        "distributed": True,
        "clients": len(distribution),
        "items_per_client": {cid: len(items) for cid, items in distribution.items()}
    }

@app.delete("/clients/{client_id}")
async def remove_client(client_id: str):
    """Remove a client from the system"""
    if client_id not in clients:
        raise HTTPException(status_code=404, detail="Client not found")
    
    if client_id in websockets:
        await websockets[client_id].close()
        del websockets[client_id]
    
    del clients[client_id]
    return {"message": "Client removed successfully"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
