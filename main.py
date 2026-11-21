"""CLI entry point for Jarvis."""
import env_setup
from session import JarvisSession

if __name__ == "__main__":
    JarvisSession().start()
