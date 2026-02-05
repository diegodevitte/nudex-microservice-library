from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from motor.motor_asyncio import AsyncIOMotorClient
from typing import List, Optional
import os
import asyncio
import json
from datetime import datetime
import uuid

# Models
class VideoAction(BaseModel):
    video_id: str
    action: str  # "add" or "remove"

class Favorite(BaseModel):
    user_id: str
    video_ids: List[str] = []
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class HistoryItem(BaseModel):
    video_id: str
    watched_at: datetime = Field(default_factory=datetime.utcnow)
    progress: int = 0  # seconds watched

class History(BaseModel):
    user_id: str
    items: List[HistoryItem] = []
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class PlaylistVideo(BaseModel):
    video_id: str
    added_at: datetime = Field(default_factory=datetime.utcnow)

class Playlist(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    name: str
    description: Optional[str] = ""
    videos: List[PlaylistVideo] = []
    is_public: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class PlaylistCreate(BaseModel):
    name: str
    description: Optional[str] = ""
    is_public: bool = False

class PlaylistUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_public: Optional[bool] = None

# Config
MONGODB_URL = os.getenv("MONGODB_URL", "mongodb://localhost:27017/nudex_library")
PORT = int(os.getenv("PORT", "8083"))

# Global instances
app = FastAPI(title="NUDEX Library Service", version="1.0.0")
db_client: AsyncIOMotorClient = None
db = None

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Dependency to get user ID from header
async def get_user_id(x_user_id: str = Header(..., alias="x-user-id")):
    if not x_user_id:
        raise HTTPException(status_code=401, detail="User ID required")
    return x_user_id

# Database connection
@app.on_event("startup")
async def startup_db():
    global db_client, db
    db_client = AsyncIOMotorClient(MONGODB_URL)
    db = db_client.nudex_library
    
    # Create indexes
    await db.favorites.create_index("user_id", unique=True)
    await db.history.create_index("user_id", unique=True)
    await db.playlists.create_index([("user_id", 1), ("name", 1)])

@app.on_event("shutdown")
async def shutdown_db():
    if db_client:
        db_client.close()

# Health check
@app.get("/health")
async def health_check():
    try:
        # Test database connection
        await db.command("ping")
        db_status = "connected"
    except:
        db_status = "disconnected"
    
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "1.0.0",
        "database": db_status
    }

# === FAVORITES ===

@app.get("/favorites")
async def get_favorites(user_id: str = Depends(get_user_id)):
    """Get user's favorite videos"""
    favorite = await db.favorites.find_one({"user_id": user_id})
    
    if not favorite:
        return {"user_id": user_id, "video_ids": []}
    
    return {
        "user_id": favorite["user_id"],
        "video_ids": favorite["video_ids"],
        "count": len(favorite["video_ids"])
    }

@app.post("/favorites")
async def toggle_favorite(
    action: VideoAction,
    user_id: str = Depends(get_user_id)
):
    """Add or remove video from favorites"""
    favorite = await db.favorites.find_one({"user_id": user_id})
    
    if not favorite:
        favorite = {
            "user_id": user_id,
            "video_ids": [],
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow()
        }
    
    video_ids = set(favorite.get("video_ids", []))
    
    if action.action == "add":
        video_ids.add(action.video_id)
        action_performed = "added"
    else:
        video_ids.discard(action.video_id)
        action_performed = "removed"
    
    # Update document
    await db.favorites.update_one(
        {"user_id": user_id},
        {
            "$set": {
                "user_id": user_id,
                "video_ids": list(video_ids),
                "updated_at": datetime.utcnow()
            }
        },
        upsert=True
    )
    
    # TODO: Publish event to RabbitMQ
    
    return {
        "action": action_performed,
        "video_id": action.video_id,
        "total_favorites": len(video_ids)
    }

# === HISTORY ===

@app.get("/history")
async def get_history(
    limit: int = 50,
    user_id: str = Depends(get_user_id)
):
    """Get user's watch history"""
    history = await db.history.find_one({"user_id": user_id})
    
    if not history:
        return {"user_id": user_id, "items": []}
    
    # Sort by watched_at desc and limit
    items = sorted(
        history.get("items", []), 
        key=lambda x: x.get("watched_at", datetime.min),
        reverse=True
    )[:limit]
    
    return {
        "user_id": history["user_id"],
        "items": items,
        "count": len(items)
    }

@app.post("/history")
async def add_to_history(
    item: HistoryItem,
    user_id: str = Depends(get_user_id)
):
    """Add video to watch history"""
    # Remove existing entry for this video
    await db.history.update_one(
        {"user_id": user_id},
        {"$pull": {"items": {"video_id": item.video_id}}}
    )
    
    # Add new entry at the beginning
    history_item = {
        "video_id": item.video_id,
        "watched_at": datetime.utcnow(),
        "progress": item.progress
    }
    
    await db.history.update_one(
        {"user_id": user_id},
        {
            "$push": {"items": {"$each": [history_item], "$position": 0}},
            "$set": {"updated_at": datetime.utcnow()},
            "$setOnInsert": {"user_id": user_id}
        },
        upsert=True
    )
    
    # Keep only last 100 items
    await db.history.update_one(
        {"user_id": user_id},
        {"$push": {"items": {"$each": [], "$slice": 100}}}
    )
    
    return {"message": "Added to history", "video_id": item.video_id}

# === PLAYLISTS ===

@app.get("/playlists")
async def get_playlists(user_id: str = Depends(get_user_id)):
    """Get user's playlists"""
    cursor = db.playlists.find({"user_id": user_id})
    playlists = []
    
    async for playlist in cursor:
        playlist["_id"] = str(playlist["_id"])
        playlists.append(playlist)
    
    return {"playlists": playlists, "count": len(playlists)}

@app.post("/playlists")
async def create_playlist(
    playlist_data: PlaylistCreate,
    user_id: str = Depends(get_user_id)
):
    """Create new playlist"""
    # Check if playlist name already exists for user
    existing = await db.playlists.find_one({
        "user_id": user_id,
        "name": playlist_data.name
    })
    
    if existing:
        raise HTTPException(status_code=400, detail="Playlist name already exists")
    
    playlist = Playlist(
        user_id=user_id,
        name=playlist_data.name,
        description=playlist_data.description or "",
        is_public=playlist_data.is_public
    )
    
    result = await db.playlists.insert_one(playlist.dict())
    
    # TODO: Publish event to RabbitMQ
    
    return {
        "id": str(result.inserted_id),
        "message": "Playlist created successfully"
    }

@app.put("/playlists/{playlist_id}")
async def update_playlist(
    playlist_id: str,
    update_data: PlaylistUpdate,
    user_id: str = Depends(get_user_id)
):
    """Update playlist"""
    # Check ownership
    playlist = await db.playlists.find_one({
        "id": playlist_id,
        "user_id": user_id
    })
    
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    
    # Prepare update data
    update_fields = {"updated_at": datetime.utcnow()}
    
    if update_data.name is not None:
        update_fields["name"] = update_data.name
    if update_data.description is not None:
        update_fields["description"] = update_data.description
    if update_data.is_public is not None:
        update_fields["is_public"] = update_data.is_public
    
    await db.playlists.update_one(
        {"id": playlist_id, "user_id": user_id},
        {"$set": update_fields}
    )
    
    return {"message": "Playlist updated successfully"}

@app.delete("/playlists/{playlist_id}")
async def delete_playlist(
    playlist_id: str,
    user_id: str = Depends(get_user_id)
):
    """Delete playlist"""
    result = await db.playlists.delete_one({
        "id": playlist_id,
        "user_id": user_id
    })
    
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Playlist not found")
    
    return {"message": "Playlist deleted successfully"}

@app.post("/playlists/{playlist_id}/videos")
async def add_video_to_playlist(
    playlist_id: str,
    video_action: VideoAction,
    user_id: str = Depends(get_user_id)
):
    """Add video to playlist"""
    # Check ownership
    playlist = await db.playlists.find_one({
        "id": playlist_id,
        "user_id": user_id
    })
    
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    
    # Add video
    video_item = {
        "video_id": video_action.video_id,
        "added_at": datetime.utcnow()
    }
    
    await db.playlists.update_one(
        {"id": playlist_id, "user_id": user_id},
        {
            "$push": {"videos": video_item},
            "$set": {"updated_at": datetime.utcnow()}
        }
    )
    
    return {"message": "Video added to playlist"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)