"""
Live Subtitle Generator - Python version
Uses: SpeechRecognition + PyAudio + Google Speech API (free tier)

Install dependencies:
    pip install SpeechRecognition pyaudio

On Windows, if pyaudio fails:
    pip install pipwin && pipwin install pyaudio
"""

import speech_recognition as sr
import sys
import datetime
import os


# ── Config ──────────────────────────────────────────────────────────────────────
LANGUAGE   = "en-US"     # Change to e.g. "zh-CN", "ja-JP", "es-ES" etc.
SAVE_LOG   = True        # Save transcript to a .txt file
ENERGY_THR = 300         # Mic sensitivity (lower = more sensitive)
PAUSE_SEC  = 0.8         # Seconds of silence before processing a phrase


def clear_line():
    sys.stdout.write("\r" + " " * 80 + "\r")
    sys.stdout.flush()


def print_subtitle(text, final=False):
    clear_line()
    if final:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {text}"
        print(line)
        return line
    else:
        # Interim — overwrite in place
        sys.stdout.write(f"\r  ... {text[:70]}")
        sys.stdout.flush()
        return None


def main():
    recognizer = sr.Recognizer()
    recognizer.energy_threshold        = ENERGY_THR
    recognizer.pause_threshold         = PAUSE_SEC
    recognizer.dynamic_energy_threshold = True

    mic = sr.Microphone()

    log_lines = []
    log_file  = None

    if SAVE_LOG:
        fname    = datetime.datetime.now().strftime("subtitle_%Y-%m-%d_%H-%M-%S.txt")
        log_file = open(fname, "w", encoding="utf-8")
        print(f"Saving transcript → {fname}")

    print("=" * 60)
    print("  Live Subtitle Generator  |  Press Ctrl+C to stop")
    print(f"  Language: {LANGUAGE}  |  Mic sensitivity: {ENERGY_THR}")
    print("=" * 60)
    print()

    # Calibrate mic noise
    print("Calibrating microphone noise… (keep quiet for 1 second)")
    with mic as source:
        recognizer.adjust_for_ambient_noise(source, duration=1)
    print(f"Done. Energy threshold set to {int(recognizer.energy_threshold)}")
    print("Listening — speak now!\n")

    def callback(_, audio):
        try:
            text = recognizer.recognize_google(audio, language=LANGUAGE)
            line = print_subtitle(text, final=True)
            if line and log_file:
                log_file.write(line + "\n")
                log_file.flush()
        except sr.UnknownValueError:
            clear_line()   # silence / unintelligible
        except sr.RequestError as e:
            clear_line()
            print(f"[ERROR] Google Speech API unavailable: {e}")

    # Start background listening
    stop_fn = recognizer.listen_in_background(mic, callback, phrase_time_limit=10)

    try:
        while True:
            pass  # main thread stays alive; callback thread handles audio
    except KeyboardInterrupt:
        print("\n\nStopping…")
    finally:
        stop_fn(wait_for_stop=False)
        if log_file:
            log_file.close()
            print(f"Transcript saved.")


if __name__ == "__main__":
    main()
