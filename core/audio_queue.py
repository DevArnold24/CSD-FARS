"""
CSD FARS -- Audio Feedback
--------------------------
Thread-safe, asynchronous sequential audio queue.
Ensures success chimes do not overlap if multiple students match concurrently.
"""

import threading
import queue
try:
    import simpleaudio as sa
except ImportError:
    sa = None

import config
from core import get_logger

logger = get_logger("audio_queue")


class ThreadedAudioQueue:
    def __init__(self):
        self._queue = queue.Queue()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()
        
        # Pre-load the wave object to avoid disk I/O on every play
        try:
            self._wave_obj = sa.WaveObject.from_wave_file(config.AUDIO_CHIME_PATH)
            self._is_ready = True
        except Exception as e:
            logger.error(f"Failed to load audio chime from {config.AUDIO_CHIME_PATH}: {e}")
            self._is_ready = False

    def _worker(self):
        """Background worker that pulls requests and plays audio sequentially."""
        while True:
            item = self._queue.get()
            if item is None:
                break # Sentinel to stop thread
                
            if self._is_ready:
                try:
                    play_obj = self._wave_obj.play()
                    play_obj.wait_done() # Block until audio is finished
                except Exception as e:
                    logger.error(f"Error playing audio chime: {e}")
            
            self._queue.task_done()

    def enqueue_chime(self):
        """Adds a chime request to the queue."""
        if self._is_ready:
            self._queue.put(True)
            
    def shutdown(self):
        """Stops the audio daemon."""
        self._queue.put(None)
        self._thread.join()

# Global instance to be used by the AI pipeline
audio_player = ThreadedAudioQueue()
