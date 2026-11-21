"""Must be imported FIRST, before anything that uses huggingface."""
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent
os.environ["HF_HOME"] = str(BASE / "models" / "huggingface")
os.environ.setdefault("HF_HUB_OFFLINE", "1")