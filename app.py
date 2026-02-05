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

@app.delete("/playlists/{playlist_id}/videos/{video_id}")
async def remove_video_from_playlist(
    playlist_id: str,
    video_id: str,
    user_id: str = Depends(get_user_id)
):
    """Remove video from playlist"""
    # Check ownership
    playlist = await db.playlists.find_one({
        "id": playlist_id,
        "user_id": user_id
    })
    
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    
    # Remove video
    await db.playlists.update_one(
        {"id": playlist_id, "user_id": user_id},
        {
            "$pull": {"videos": {"video_id": video_id}},
            "$set": {"updated_at": datetime.utcnow()}
        }
    )
    
    return {"message": "Video removed from playlist"}

# === ENHANCED PLAYLIST FEATURES ===

@app.get("/playlists/search")
async def search_playlists(
    q: str,
    user_id: str = Depends(get_user_id)
):
    """Search user's playlists by title or description"""
    playlists = await db.playlists.find({
        "user_id": user_id,
        "$or": [
            {"title": {"$regex": q, "$options": "i"}},
            {"description": {"$regex": q, "$options": "i"}}
        ]
    }).sort("updated_at", -1).to_list(100)
    
    return {"playlists": playlists, "query": q}

@app.post("/playlists/{playlist_id}/duplicate")
async def duplicate_playlist(
    playlist_id: str,
    new_title: Optional[str] = None,
    user_id: str = Depends(get_user_id)
):
    """Duplicate a playlist"""
    # Get original playlist
    original = await db.playlists.find_one({
        "id": playlist_id,
        "user_id": user_id
    })
    
    if not original:
        raise HTTPException(status_code=404, detail="Playlist not found")
    
    # Create duplicate
    new_playlist = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "title": new_title or f"{original['title']} (Copy)",
        "description": original.get("description", ""),
        "is_public": False,  # Duplicates are private by default
        "videos": original.get("videos", []).copy(),
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow()
    }
    
    await db.playlists.insert_one(new_playlist)
    
    return {"playlist": new_playlist}

@app.patch("/playlists/{playlist_id}/visibility")
async def change_playlist_visibility(
    playlist_id: str,
    is_public: bool,
    user_id: str = Depends(get_user_id)
):
    """Change playlist visibility (public/private)"""
    result = await db.playlists.update_one(
        {"id": playlist_id, "user_id": user_id},
        {
            "$set": {
                "is_public": is_public,
                "updated_at": datetime.utcnow()
            }
        }
    )
    
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Playlist not found")
    
    return {"message": f"Playlist visibility changed to {'public' if is_public else 'private'}"}

@app.get("/playlists/public")
async def get_public_playlists(
    limit: int = 20,
    skip: int = 0
):
    """Get public playlists from all users"""
    playlists = await db.playlists.find({
        "is_public": True
    }).sort("updated_at", -1).skip(skip).limit(limit).to_list(limit)
    
    # Remove user_id from response for privacy
    for playlist in playlists:
        playlist.pop("user_id", None)
    
    return {"playlists": playlists}

@app.post("/playlists/{playlist_id}/share")
async def generate_share_link(
    playlist_id: str,
    user_id: str = Depends(get_user_id)
):
    """Generate a shareable link for a playlist"""
    # Check ownership
    playlist = await db.playlists.find_one({
        "id": playlist_id,
        "user_id": user_id
    })
    
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    
    # Generate share token
    share_token = str(uuid.uuid4())
    
    # Update playlist with share info
    await db.playlists.update_one(
        {"id": playlist_id},
        {
            "$set": {
                "share_token": share_token,
                "shared_at": datetime.utcnow(),
                "updated_at": datetime.utcnow()
            }
        }
    )
    
    share_link = f"/api/playlists/shared/{share_token}"
    
    return {"share_link": share_link, "share_token": share_token}

@app.get("/playlists/shared/{share_token}")
async def get_shared_playlist(share_token: str):
    """Access a shared playlist via token"""
    playlist = await db.playlists.find_one({"share_token": share_token})
    
    if not playlist:
        raise HTTPException(status_code=404, detail="Shared playlist not found")
    
    # Remove sensitive info
    playlist.pop("user_id", None)
    playlist.pop("share_token", None)
    
    return {"playlist": playlist}

# === ANALYTICS AND INSIGHTS ===

@app.get("/analytics/overview")
async def get_user_analytics(user_id: str = Depends(get_user_id)):
    """Get user's library analytics overview"""
    # Count favorites
    favorites = await db.favorites.find_one({"user_id": user_id}) or {"video_ids": []}
    total_favorites = len(favorites.get("video_ids", []))
    
    # Count playlists
    total_playlists = await db.playlists.count_documents({"user_id": user_id})
    
    # Count videos in all playlists
    playlists = await db.playlists.find({"user_id": user_id}).to_list(None)
    total_playlist_videos = sum(len(p.get("videos", [])) for p in playlists)
    
    # History stats
    history = await db.history.find_one({"user_id": user_id}) or {"items": []}
    history_items = history.get("items", [])
    total_watched = len(history_items)
    
    # Recent activity (last 7 days)
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    recent_history = [
        item for item in history_items 
        if item.get("watched_at", datetime.min) > seven_days_ago
    ]
    
    return {
        "overview": {
            "total_favorites": total_favorites,
            "total_playlists": total_playlists,
            "total_playlist_videos": total_playlist_videos,
            "total_watched": total_watched,
            "recent_activity": len(recent_history)
        },
        "recent_playlists": await db.playlists.find(
            {"user_id": user_id}
        ).sort("updated_at", -1).limit(5).to_list(5),
        "recent_history": recent_history[:10]
    }

@app.get("/analytics/watch-time")
async def get_watch_time_analytics(
    user_id: str = Depends(get_user_id),
    days: int = 30
):
    """Get watch time analytics for the user"""
    start_date = datetime.utcnow() - timedelta(days=days)
    
    history = await db.history.find_one({"user_id": user_id}) or {"items": []}
    history_items = history.get("items", [])
    
    # Filter by date range
    recent_items = [
        item for item in history_items
        if item.get("watched_at", datetime.min) > start_date
    ]
    
    # Group by date
    daily_stats = {}
    for item in recent_items:
        date_key = item["watched_at"].strftime("%Y-%m-%d")
        if date_key not in daily_stats:
            daily_stats[date_key] = {
                "date": date_key,
                "videos_watched": 0,
                "total_progress": 0
            }
        daily_stats[date_key]["videos_watched"] += 1
        daily_stats[date_key]["total_progress"] += item.get("progress", 0)
    
    # Sort by date
    sorted_stats = sorted(daily_stats.values(), key=lambda x: x["date"])
    
    return {
        "period": f"{days} days",
        "total_videos": len(recent_items),
        "total_time_seconds": sum(item.get("progress", 0) for item in recent_items),
        "daily_stats": sorted_stats
    }

# === IMPORT/EXPORT FEATURES ===

@app.get("/export/data")
async def export_user_data(user_id: str = Depends(get_user_id)):
    """Export all user library data"""
    # Get all user data
    favorites = await db.favorites.find_one({"user_id": user_id}) or {}
    playlists = await db.playlists.find({"user_id": user_id}).to_list(None)
    history = await db.history.find_one({"user_id": user_id}) or {}
    
    export_data = {
        "export_date": datetime.utcnow().isoformat(),
        "user_id": user_id,
        "favorites": favorites.get("video_ids", []),
        "playlists": playlists,
        "history": history.get("items", [])
    }
    
    return {"export_data": export_data}

@app.post("/import/playlist")
async def import_playlist(
    playlist_data: dict,
    user_id: str = Depends(get_user_id)
):
    """Import playlist data"""
    try:
        # Validate and clean playlist data
        new_playlist = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "title": playlist_data.get("title", "Imported Playlist"),
            "description": playlist_data.get("description", ""),
            "is_public": False,
            "videos": playlist_data.get("videos", []),
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow()
        }
        
        await db.playlists.insert_one(new_playlist)
        
        return {"message": "Playlist imported successfully", "playlist_id": new_playlist["id"]}
        
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Import failed: {str(e)}")

# === RECOMMENDATIONS ===

@app.get("/recommendations/playlists")
async def get_playlist_recommendations(
    user_id: str = Depends(get_user_id),
    limit: int = 10
):
    """Get playlist recommendations based on user's favorites"""
    # Get user's favorites
    favorites = await db.favorites.find_one({"user_id": user_id}) or {"video_ids": []}
    favorite_videos = set(favorites.get("video_ids", []))
    
    if not favorite_videos:
        # If no favorites, return popular public playlists
        playlists = await db.playlists.find({
            "is_public": True,
            "user_id": {"$ne": user_id}
        }).sort("updated_at", -1).limit(limit).to_list(limit)
        
        return {"recommendations": playlists, "reason": "popular_playlists"}
    
    # Find playlists that contain user's favorite videos
    recommended_playlists = []
    
    async for playlist in db.playlists.find({
        "is_public": True,
        "user_id": {"$ne": user_id},
        "videos.video_id": {"$in": list(favorite_videos)}
    }).limit(limit * 2):
        
        # Calculate similarity score
        playlist_videos = set(v.get("video_id") for v in playlist.get("videos", []))
        similarity = len(favorite_videos.intersection(playlist_videos))
        
        playlist["similarity_score"] = similarity
        recommended_playlists.append(playlist)
    
    # Sort by similarity and limit
    recommended_playlists.sort(key=lambda x: x["similarity_score"], reverse=True)
    recommended_playlists = recommended_playlists[:limit]
    
    # Remove sensitive data
    for playlist in recommended_playlists:
        playlist.pop("user_id", None)
        playlist.pop("similarity_score", None)
    
    return {"recommendations": recommended_playlists, "reason": "similar_interests"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)