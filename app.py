"""Compatibility wrapper — launch scripts and Dockerfile can call app.py."""

from agent_sdk import main

if __name__ == "__main__":
    main()
