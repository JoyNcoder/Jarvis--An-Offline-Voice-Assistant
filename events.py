"""Small synchronous event bus used by CLI and future UI/server clients."""
from threading import RLock

_subscribers = []
_lock = RLock()

def subscribe(fn):
    """Subscribe to events. Returns an unsubscribe callable."""
    with _lock:
        _subscribers.append(fn)
    def unsubscribe():
        with _lock:
            if fn in _subscribers:
                _subscribers.remove(fn)
    return unsubscribe

def emit(event, data=None):
    """Publish an event without allowing a subscriber to break Jarvis."""
    with _lock:
        listeners = tuple(_subscribers)
    for fn in listeners:
        try:
            fn(event, data)
        except Exception as error:
            print("Event subscriber error:", error)
