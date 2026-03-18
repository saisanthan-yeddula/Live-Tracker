import json
import asyncio
import uuid
from datetime import datetime
from typing import Dict, Set, List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
import redis.asyncio as redis
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel

app = FastAPI()

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

import os

# Redis setup (Railway)
# Prioritize REDIS_URL, then individual Railway variables, finally hardcoded public fallback
REDIS_URL = os.getenv("REDIS_URL")
if not REDIS_URL:
    # Use PUBLIC URL from screenshot as fallback (Internal DNS sometimes fails)
    REDIS_URL = "redis://default:iAdwbOHWUsfKhmZAAmwnBpPiKPhCEnQv@centerbeam.proxy.rlwy.net:52033"

redis_client = redis.from_url(REDIS_URL, decode_responses=True)

# MongoDB setup (Railway)
MONGO_URL = "mongodb://mongo:HUSXLthePQtOuHFLYRMtpBiTbPeppidq@gondola.proxy.rlwy.net:29702"
mongo_client = AsyncIOMotorClient(MONGO_URL)
db = mongo_client.live_tracker
users_collection = db.users
orders_collection = db.orders
history_collection = db.location_history

# Role Mapping: 1: Admin, 2: User, 3: Agent
ROLE_ADMIN = 1
ROLE_USER = 2
ROLE_AGENT = 3

# Models
class UserCreate(BaseModel):
    name: str
    password: str
    role_id: int

class LoginRequest(BaseModel):
    name: str
    password: str

class OrderCreate(BaseModel):
    user_id: str
    user_name: str
    lat: float
    lng: float
    address: str

class OrderAssign(BaseModel):
    agent_id: str
    agent_name: str

# WebSocket Tracking State
agent_subscribers: Dict[str, Set[WebSocket]] = {}
admin_subscribers: Set[WebSocket] = set()
ACTIVE_AGENTS_KEY = "active_agents"

# --- Authentication & User Management ---

@app.post("/login")
async def login(req: LoginRequest):
    # Special case: If no users exist, allow creating the first admin
    count = await users_collection.count_documents({})
    if count == 0:
        # Create default admin if requested
        if req.name == "admin@gmail.com" and req.password == "1234":
            new_admin = {
                "_id": str(uuid.uuid4()),
                "name": req.name,
                "password": req.password,
                "role_id": ROLE_ADMIN
            }
            await users_collection.insert_one(new_admin)
            return {
                "user_id": new_admin["_id"],
                "name": new_admin["name"],
                "role_id": new_admin["role_id"]
            }

    user = await users_collection.find_one({"name": req.name, "password": req.password})
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    return {
        "user_id": str(user["_id"]),
        "name": user["name"],
        "role_id": user.get("role_id", ROLE_USER)
    }

@app.post("/admin/create-user")
async def create_user(user: UserCreate, admin_id: str = Header(...)):
    # Simple check for admin_id role (In real app, use JWT)
    admin = await users_collection.find_one({"_id": admin_id, "role_id": ROLE_ADMIN})
    if not admin:
        # If it's the very first user, allow creating admin
        count = await users_collection.count_documents({})
        if count > 0:
            raise HTTPException(status_code=403, detail="Only admins can create users")
    
    new_user = user.dict()
    new_user["_id"] = str(uuid.uuid4())
    await users_collection.insert_one(new_user)
    return {"message": "User created successfully", "user_id": new_user["_id"]}

@app.get("/admin/users/{role_id}")
async def get_users_by_role(role_id: int):
    users = await users_collection.find({"role_id": role_id}).to_list(100)
    return users

# --- Order Management ---

@app.post("/orders")
async def create_order(order: OrderCreate):
    new_order = order.dict()
    new_order["_id"] = str(uuid.uuid4())
    new_order["status"] = "waiting_for_assignment"
    new_order["created_at"] = datetime.now().isoformat()
    new_order["agent_id"] = None
    new_order["agent_name"] = None
    await orders_collection.insert_one(new_order)
    return new_order

@app.get("/orders")
async def get_orders(user_id: Optional[str] = None, agent_id: Optional[str] = None):
    query = {}
    if user_id: query["user_id"] = user_id
    if agent_id: query["agent_id"] = agent_id
    orders = await orders_collection.find(query).to_list(100)
    return orders

@app.patch("/orders/{order_id}/assign")
async def assign_order(order_id: str, assign: OrderAssign):
    result = await orders_collection.update_one(
        {"_id": order_id},
        {"$set": {
            "agent_id": assign.agent_id,
            "agent_name": assign.agent_name,
            "status": "assigned"
        }}
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Order not found")
    return {"message": "Order assigned successfully"}

# --- Real-time Tracking (Updated with Orders) ---

@app.get("/")
async def root():
    return {"message": "Live Tracker API with User/Order Management"}

@app.get("/agents")
async def list_agents():
    return {"agents": list(await redis_client.smembers(ACTIVE_AGENTS_KEY))}

@app.websocket("/ws/agent/{agent_id}")
async def agent_websocket(websocket: WebSocket, agent_id: str):
    await websocket.accept()
    await redis_client.sadd(ACTIVE_AGENTS_KEY, agent_id)
    try:
        while True:
            data_str = await websocket.receive_text()
            data = json.loads(data_str)
            data["agent_id"] = agent_id
            
            await redis_client.set(f"agent:{agent_id}:location", json.dumps(data))
            await history_collection.insert_one(data.copy())
            
            if agent_id in agent_subscribers:
                for client in agent_subscribers[agent_id]:
                    try: await client.send_text(json.dumps(data))
                    except: pass
            
            for admin_ws in admin_subscribers:
                try: await admin_ws.send_text(json.dumps(data))
                except: pass
                    
    except WebSocketDisconnect:
        await redis_client.srem(ACTIVE_AGENTS_KEY, agent_id)
    except Exception as e:
        print(f"Error in agent_websocket: {e}")

@app.websocket("/ws/user/{agent_id}")
async def user_websocket(websocket: WebSocket, agent_id: str):
    await websocket.accept()
    if agent_id not in agent_subscribers:
        agent_subscribers[agent_id] = set()
    agent_subscribers[agent_id].add(websocket)
    
    current_location = await redis_client.get(f"agent:{agent_id}:location")
    if current_location:
        await websocket.send_text(current_location)
        
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if agent_id in agent_subscribers:
            agent_subscribers[agent_id].remove(websocket)

@app.websocket("/ws/admin")
async def admin_websocket(websocket: WebSocket):
    await websocket.accept()
    admin_subscribers.add(websocket)
    
    agents = await redis_client.smembers(ACTIVE_AGENTS_KEY)
    for agent_id in agents:
        loc = await redis_client.get(f"agent:{agent_id}:location")
        if loc: await websocket.send_text(loc)
            
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        admin_subscribers.remove(websocket)

@app.get("/health")
async def health():
    return {"status": "ok", "port": os.environ.get("PORT", "8000")}

if __name__ == "__main__":
    import uvicorn
    import os
    # Get port from Railway's environment variable
    port = int(os.environ.get("PORT", 8000))
    print(f"Starting server on port {port}...")
    # Use "main:app" so uvicorn can find the app in this file
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
