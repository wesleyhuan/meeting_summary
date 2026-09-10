import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from . import db
from .audio_capture import LoopbackSource, MicSource
from .pipeline import build_pipeline
from .stt import WhisperTranscriber

# uvicorn imports this module rather than running it as __main__, so a
# `if __name__ == "__main__"` guard here would never fire; basicConfig()
# is a no-op if something else already configured the root logger.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

logger = logging.getLogger(__name__)


class AppState:
    def __init__(self, db_path: Optional[str] = None):
        self.conn = db.connect(db_path or db.default_db_path())
        self.active_pipeline = None
        self.active_meeting_id: Optional[int] = None
        self.transcriber = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.queues: list[asyncio.Queue] = []

    def broadcast(self, message: dict) -> None:
        payload = json.dumps(message)
        if self.loop is None:
            logger.warning("Broadcast attempted before event loop was ready; dropping message")
            return
        for queue in list(self.queues):
            self.loop.call_soon_threadsafe(queue.put_nowait, payload)


state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.loop = asyncio.get_running_loop()
    logger.info("Helper service started")
    yield
    logger.info("Helper service shutting down")


app = FastAPI(lifespan=lifespan)


class StartMeetingRequest(BaseModel):
    title: Optional[str] = None


@app.post("/meetings/start")
def start_meeting(req: StartMeetingRequest):
    if state.active_pipeline is not None:
        raise HTTPException(status_code=409, detail="A meeting is already in progress")

    import datetime

    title = req.title or f"Meeting {datetime.datetime.now().isoformat(timespec='seconds')}"

    if state.transcriber is None:
        state.transcriber = WhisperTranscriber()

    mic_source = None
    try:
        mic_source = MicSource()
        loopback_source = LoopbackSource()
    except Exception:
        logger.exception("Failed to open audio devices; no meeting row created")
        if mic_source is not None:
            mic_source.close()
        raise HTTPException(status_code=500, detail="Could not open audio devices")

    meeting_id = db.create_meeting(state.conn, title)

    pipeline = build_pipeline(
        meeting_id, state.conn, state.broadcast, mic_source, loopback_source, state.transcriber
    )
    pipeline.start()
    state.active_pipeline = pipeline
    state.active_meeting_id = meeting_id
    logger.info("Meeting started meeting_id=%s title=%r", meeting_id, title)
    return {"meeting_id": meeting_id, "title": title}


@app.post("/meetings/stop")
def stop_meeting():
    if state.active_pipeline is None:
        raise HTTPException(status_code=409, detail="No meeting is in progress")

    pipeline = state.active_pipeline
    meeting_id = state.active_meeting_id
    stop_error = None
    try:
        pipeline.stop()
    except Exception:
        logger.exception("Error stopping pipeline for meeting_id=%s", meeting_id)
        stop_error = True
    finally:
        state.active_pipeline = None
        state.active_meeting_id = None
        db.end_meeting(state.conn, meeting_id)

    if stop_error:
        raise HTTPException(status_code=500, detail="Error stopping meeting")

    logger.info("Meeting stopped meeting_id=%s", meeting_id)
    return {"status": "stopped"}


@app.get("/meetings/{meeting_id}/transcript")
def get_transcript(meeting_id: int):
    meeting = db.get_meeting(state.conn, meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    rows = db.get_transcript(state.conn, meeting_id)
    return {"meeting_id": meeting_id, "segments": [dict(r) for r in rows]}


@app.websocket("/live")
async def live_ws(websocket: WebSocket):
    await websocket.accept()
    queue: asyncio.Queue = asyncio.Queue()
    state.queues.append(queue)
    logger.info("WebSocket client connected; %s total", len(state.queues))
    try:
        while True:
            payload = await queue.get()
            await websocket.send_text(payload)
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    finally:
        state.queues.remove(queue)
