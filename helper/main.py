import asyncio
import json
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, field_validator

from . import db
from .audio_capture import LoopbackSource, MicSource, list_input_devices
from .pipeline import build_pipeline
from .stt import WhisperTranscriber

# uvicorn imports this module rather than running it as __main__, so a
# `if __name__ == "__main__"` guard here would never fire; basicConfig()
# is a no-op if something else already configured the root logger.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

logger = logging.getLogger(__name__)

DASHBOARD_PATH = Path(__file__).parent / "dashboard.html"


class AppState:
    def __init__(self, db_path: Optional[str] = None):
        self.conn = db.connect(db_path or db.default_db_path())
        self.active_pipeline = None
        self.active_meeting_id: Optional[int] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.queues: list[asyncio.Queue] = []
        self.start_lock = threading.Lock()

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


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD_PATH.read_text(encoding="utf-8")


class StartMeetingRequest(BaseModel):
    title: Optional[str] = None


MODEL_SIZES = ("tiny", "base", "small", "medium", "large-v3")


class SettingsUpdate(BaseModel):
    mic_device_id: Optional[str] = None
    stt_language: Optional[str] = None
    whisper_model_size: Optional[str] = None

    @field_validator("whisper_model_size")
    @classmethod
    def validate_model_size(cls, value):
        if value is not None and value not in MODEL_SIZES:
            raise ValueError(f"whisper_model_size must be one of {MODEL_SIZES}")
        return value


@app.post("/meetings/start")
def start_meeting(req: StartMeetingRequest):
    with state.start_lock:
        if state.active_pipeline is not None:
            raise HTTPException(status_code=409, detail="A meeting is already in progress")

        import datetime

        title = req.title or f"Meeting {datetime.datetime.now().isoformat(timespec='seconds')}"

        settings = db.get_all_settings(state.conn)
        mic_device_id = int(settings["mic_device_id"]) if settings["mic_device_id"] else None

        mic_source = None
        try:
            mic_source = MicSource(device_index=mic_device_id)
            loopback_source = LoopbackSource()
        except Exception:
            logger.exception("Failed to open audio devices; no meeting row created")
            if mic_source is not None:
                mic_source.close()
            raise HTTPException(status_code=500, detail="Could not open audio devices")

        try:
            settings = db.get_all_settings(state.conn)
            mic_device_id = int(settings["mic_device_id"]) if settings["mic_device_id"] else None
            transcriber = WhisperTranscriber(
                model_size=settings["whisper_model_size"], language=settings["stt_language"]
            )
        except Exception:
            logger.exception("Failed to prepare transcriber from settings")
            mic_source.close()
            loopback_source.close()
            raise HTTPException(status_code=500, detail="Could not prepare speech-to-text model")

        meeting_id = db.create_meeting(state.conn, title)

        pipeline = build_pipeline(
            meeting_id, state.conn, state.broadcast, mic_source, loopback_source, transcriber
        )
        pipeline.start()
        state.active_pipeline = pipeline
        state.active_meeting_id = meeting_id
        logger.info("Meeting started meeting_id=%s title=%r", meeting_id, title)
        return {"meeting_id": meeting_id, "title": title}


@app.get("/meetings")
def list_meetings():
    rows = db.list_meetings(state.conn)
    return {"meetings": [dict(r) for r in rows]}


@app.get("/settings")
def get_settings():
    return db.get_all_settings(state.conn)


@app.put("/settings")
def update_settings(update: SettingsUpdate):
    data = update.model_dump(exclude_none=True)
    for key, value in data.items():
        db.set_setting(state.conn, key, value)
    return db.get_all_settings(state.conn)


@app.get("/audio-devices")
def audio_devices():
    try:
        devices = list_input_devices()
    except Exception:
        logger.exception("Failed to enumerate audio input devices")
        raise HTTPException(status_code=500, detail="Could not list audio devices")
    return {"devices": devices}


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
