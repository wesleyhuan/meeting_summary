import logging
import threading
import time
import tkinter as tk

import websocket

from .formatting import format_caption

logger = logging.getLogger(__name__)


class OverlayApp:
    def __init__(self, ws_url: str = "ws://localhost:8000/live"):
        self.ws_url = ws_url
        self.root = tk.Tk()
        self.root.title("Live Subtitle")
        self.root.attributes("-topmost", True)
        self.root.overrideredirect(True)
        self.root.geometry("600x80+100+100")
        self.root.configure(bg="#0d0d0f")
        self.label = tk.Label(
            self.root,
            text="Waiting for helper…",
            fg="#e8e8f0",
            bg="#0d0d0f",
            font=("Segoe UI", 16, "bold"),
            wraplength=580,
            justify="left",
        )
        self.label.pack(expand=True, fill="both", padx=12, pady=12)
        self._bind_drag()
        self._ws_thread = threading.Thread(target=self._run_ws_loop, daemon=True)

    def _bind_drag(self) -> None:
        drag_origin = {"x": 0, "y": 0}

        def start(event):
            drag_origin["x"], drag_origin["y"] = event.x, event.y

        def move(event):
            x = self.root.winfo_x() + event.x - drag_origin["x"]
            y = self.root.winfo_y() + event.y - drag_origin["y"]
            self.root.geometry(f"+{x}+{y}")

        self.root.bind("<ButtonPress-1>", start)
        self.root.bind("<B1-Motion>", move)

    def _set_text(self, text: str) -> None:
        self.root.after(0, lambda: self.label.config(text=text))

    def _run_ws_loop(self) -> None:
        def on_message(_ws, message):
            self._set_text(format_caption(message))

        def on_error(_ws, error):
            logger.error("Overlay WebSocket error: %s", error)
            self._set_text("Waiting for helper…")

        def on_close(_ws, *_args):
            logger.info("Overlay WebSocket closed; will retry")
            self._set_text("Waiting for helper…")

        while True:
            app = websocket.WebSocketApp(
                self.ws_url, on_message=on_message, on_error=on_error, on_close=on_close
            )
            app.run_forever()
            time.sleep(1)

    def run(self) -> None:
        self._ws_thread.start()
        self.root.mainloop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    OverlayApp().run()
