"""Configuration for the WorkIQ Teams Relay Bot."""

from os import environ
from dotenv import load_dotenv

load_dotenv()


class DefaultConfig:
    """Bot and Redis configuration loaded from environment variables."""

    def __init__(self) -> None:
        # Azure Bot / M365 Agents SDK authentication
        self.PORT = int(environ.get("PORT", "3978"))
        self.TENANT_ID = environ.get("TENANT_ID", "")          # FDPO tenant
        self.HOST_TENANT_ID = environ.get("HOST_TENANT_ID", "") # CORP tenant
        self.CLIENT_ID = environ.get("CLIENT_ID", "")
        self.CLIENT_SECRET = environ.get("CLIENT_SECRET", "")

        # Azure Managed Redis
        self.AZ_REDIS_CACHE_ENDPOINT = environ.get("AZ_REDIS_CACHE_ENDPOINT", "")

        # Logging
        self.LOG_LEVEL = environ.get("LOG_LEVEL", "INFO")
