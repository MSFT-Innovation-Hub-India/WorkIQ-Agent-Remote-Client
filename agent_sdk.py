"""WorkIQ Teams Relay Bot — M365 Agents SDK application.

Receives messages from Teams users, pushes them to the user's Redis inbox
stream, and delivers responses from the Redis outbox back to Teams via
proactive messaging.

Multi-tenant: authorizes both FDPO (TENANT_ID) and CORP (HOST_TENANT_ID)
tenants so the bot can be registered in FDPO but consumed by CORP Teams users.
"""

import asyncio
import logging
import re
import threading
from os import environ

from dotenv import load_dotenv

try:
    from microsoft_agents.activity import load_configuration_from_env
    from microsoft_agents.authentication.msal import MsalConnectionManager
    from microsoft_agents.hosting.aiohttp import CloudAdapter
    from microsoft_agents.hosting.core import (
        AgentApplication,
        Authorization,
        MemoryStorage,
        MessageFactory,
        TurnContext,
        TurnState,
    )
except ImportError:
    from microsoft.agents.activity import load_configuration_from_env  # type: ignore[import-not-found]
    from microsoft.agents.authentication.msal import MsalConnectionManager  # type: ignore[import-not-found]
    from microsoft.agents.hosting.aiohttp import CloudAdapter  # type: ignore[import-not-found]
    from microsoft.agents.hosting.core import (  # type: ignore[import-not-found]
        AgentApplication,
        Authorization,
        MemoryStorage,
        MessageFactory,
        TurnContext,
        TurnState,
    )

from config import DefaultConfig
from redis_relay import RedisRelay
from start_server import start_server

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
    force=True,
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

config = DefaultConfig()


def _mirror_service_connection_settings() -> None:
    """Populate M365 Agents SDK service-connection env vars from our
    base auth settings when they are not already set."""
    mapping = {
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID": "CLIENT_ID",
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET": "CLIENT_SECRET",
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID": "TENANT_ID",
    }
    for target, source in mapping.items():
        if environ.get(target):
            continue
        source_value = environ.get(source)
        if source_value:
            environ[target] = source_value


_mirror_service_connection_settings()
agents_sdk_config = load_configuration_from_env(environ)

# ---------------------------------------------------------------------------
# M365 Agents SDK setup
# ---------------------------------------------------------------------------

storage = MemoryStorage()
connection_manager = MsalConnectionManager(**agents_sdk_config)
adapter = CloudAdapter(connection_manager=connection_manager)
authorization = Authorization(storage, connection_manager, **agents_sdk_config)

bot_app = AgentApplication[TurnState](
    storage=storage,
    adapter=adapter,
    authorization=authorization,
    **agents_sdk_config,
)

# ---------------------------------------------------------------------------
# Redis Relay
# ---------------------------------------------------------------------------

redis_relay: RedisRelay | None = None

# Cache: Teams from_property.id → user email (avoid repeated lookups)
_email_cache: dict[str, str] = {}
_email_cache_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_user_email(context: TurnContext) -> str | None:
    """Resolve the Teams user's email address.

    Strategy:
      1. Check the in-memory cache (keyed by from_property.id)
      2. Call the Bot Connector REST API to get the member profile
         (GET {service_url}/v3/conversations/{id}/members/{id})
         — this uses the bot's own credentials, no Graph/admin consent needed.
      3. Fallback: check if from_property.name contains an email.
    """
    from_id = (
        context.activity.from_property.id
        if context.activity.from_property
        else None
    )
    if not from_id:
        return None

    # Check cache
    with _email_cache_lock:
        cached = _email_cache.get(from_id)
    if cached:
        return cached

    email = None

    # --- Bot Connector REST API: get member profile ---------------------
    # This calls the Bot Framework service (not Graph), so it works with
    # the bot's own app credentials across tenants.
    try:
        service_url = context.activity.service_url
        conversation_id = context.activity.conversation.id if context.activity.conversation else None

        if service_url and conversation_id:
            import aiohttp as _aiohttp
            import msal

            # Acquire token for Bot Framework using client credentials
            authority = f"https://login.microsoftonline.com/{config.TENANT_ID}"
            msal_app = msal.ConfidentialClientApplication(
                config.CLIENT_ID,
                authority=authority,
                client_credential=config.CLIENT_SECRET,
            )
            token_result = msal_app.acquire_token_for_client(
                scopes=["https://api.botframework.com/.default"]
            )
            token = token_result.get("access_token") if token_result else None

            if token:
                url = f"{service_url.rstrip('/')}/v3/conversations/{conversation_id}/members/{from_id}"

                async with _aiohttp.ClientSession() as session:
                    async with session.get(
                        url,
                        headers={"Authorization": f"Bearer {token}"},
                    ) as resp:
                        if resp.status == 200:
                            member_data = await resp.json()
                            logger.info("Bot Connector member data: %s", member_data)
                            email = (
                                member_data.get("email")
                                or member_data.get("userPrincipalName")
                            )
                        else:
                            body = await resp.text()
                            logger.warning(
                                "Bot Connector get-member returned %s: %s",
                                resp.status, body[:200],
                            )
            else:
                logger.warning("Failed to acquire Bot Connector token: %s", token_result)
    except Exception as exc:
        logger.warning("Bot Connector get-member failed: %s", exc)

    # Fallback: some channels embed email directly in from_property.name
    if not email:
        name = (
            context.activity.from_property.name
            if context.activity.from_property
            else ""
        )
        if name and "@" in name:
            email = name

    # Normalize FDPO guest UPN → CORP email
    # e.g. "sansri_microsoft.com#EXT#@fdpo.onmicrosoft.com" → "sansri@microsoft.com"
    if email and "#EXT#" in email:
        local_part = email.split("#EXT#")[0]        # "sansri_microsoft.com"
        # Replace the last underscore with @ to reconstruct the real email
        last_underscore = local_part.rfind("_")
        if last_underscore > 0:
            email = local_part[:last_underscore] + "@" + local_part[last_underscore + 1:]

    if email:
        with _email_cache_lock:
            _email_cache[from_id] = email.lower()
        return email.lower()

    return None


# ---------------------------------------------------------------------------
# Message handler
# ---------------------------------------------------------------------------

NON_COMMAND_MESSAGE_PATTERN = re.compile(r"^(?!/).*$", re.DOTALL)


@bot_app.message(NON_COMMAND_MESSAGE_PATTERN)
async def on_message(context: TurnContext, state: TurnState):
    """Handle every non-slash-command message from Teams."""
    try:
        user_message = context.activity.text or ""
        sender_name = (
            context.activity.from_property.name
            if context.activity.from_property
            else "Unknown"
        )

        # ---- Multi-tenant authorization --------------------------------
        tenant_id = None
        try:
            if context.activity.conversation and hasattr(context.activity.conversation, "tenant_id"):
                tenant_id = context.activity.conversation.tenant_id
            elif context.activity.channel_data and "tenant" in context.activity.channel_data:
                tenant_id = context.activity.channel_data["tenant"].get("id")
        except Exception as exc:
            logger.warning("Could not extract tenant_id: %s", exc)

        if tenant_id:
            if tenant_id == config.HOST_TENANT_ID:
                logger.info("User %s from HOST (CORP) tenant — authorized", sender_name)
            elif tenant_id == config.TENANT_ID:
                logger.info("User %s from FDPO tenant — authorized", sender_name)
            else:
                logger.warning("User %s from unauthorized tenant: %s", sender_name, tenant_id)
                await context.send_activity(
                    MessageFactory.text("❌ **Access Denied**: Unauthorized tenant.")
                )
                return
        else:
            logger.warning("No tenant ID found for user %s — allowing (emulator?)", sender_name)

        # ---- Resolve user email ----------------------------------------
        user_email = await _get_user_email(context)
        if not user_email:
            await context.send_activity(
                MessageFactory.text(
                    "I couldn't determine your email address. "
                    "Please ensure you're using this bot from Microsoft Teams."
                )
            )
            return

        logger.info("Message from %s (%s): %s", sender_name, user_email, user_message[:100])

        # ---- Redis relay -----------------------------------------------
        if redis_relay is None:
            await context.send_activity(
                MessageFactory.text(
                    "⚠️ The relay service is not configured. "
                    "Redis endpoint is missing."
                )
            )
            return

        # Check agent online
        agent_info = redis_relay.is_agent_online(user_email)
        if not agent_info:
            await context.send_activity(
                MessageFactory.text(
                    "Your WorkIQ agent is **not currently running** on your desktop. "
                    "Please start it and try again."
                )
            )
            return

        # Store / refresh conversation reference for proactive replies
        activity = context.activity
        conversation_ref = {
            "activity_id": activity.id,
            "user": {"id": activity.from_property.id, "name": activity.from_property.name} if activity.from_property else None,
            "bot": {"id": activity.recipient.id, "name": activity.recipient.name} if activity.recipient else None,
            "conversation": {"id": activity.conversation.id, "tenant_id": getattr(activity.conversation, "tenant_id", None)} if activity.conversation else None,
            "channel_id": activity.channel_id,
            "service_url": activity.service_url,
        }
        redis_relay.register_active_user(user_email, conversation_ref)

        # Push to Redis inbox
        try:
            msg_id = redis_relay.push_to_inbox(user_email, sender_name, user_message)
        except Exception as exc:
            logger.error("Failed to push to Redis inbox: %s", exc)
            await context.send_activity(
                MessageFactory.text("⚠️ Failed to deliver your message. Please try again.")
            )
            return

        # No ack — the proactive reply from the outbox poller is sufficient.

    except Exception as exc:
        logger.error("Error in message handler: %s", exc, exc_info=True)
        await context.send_activity(
            MessageFactory.text("I encountered an error processing your message. Please try again.")
        )


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------


@bot_app.error
async def on_error(context: TurnContext, error: Exception):
    logger.error("Unhandled error: %s", error, exc_info=True)
    try:
        await context.send_activity(
            MessageFactory.text("Sorry, I encountered an unexpected error. Please try again.")
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    """Start the bot server and Redis relay."""
    global redis_relay

    logger.info("=" * 50)
    logger.info("WorkIQ Teams Relay Bot starting")
    logger.info("=" * 50)

    # Initialize Redis relay if configured
    if config.AZ_REDIS_CACHE_ENDPOINT:
        redis_relay = RedisRelay(endpoint=config.AZ_REDIS_CACHE_ENDPOINT)
        # The event loop is created by aiohttp; we'll set it after run_app starts.
        # For now pass None — the poller thread uses asyncio.run_coroutine_threadsafe
        # which needs the loop, so we start the relay right before the server.
        redis_relay.start(
            app_id=config.CLIENT_ID,
            client_secret=config.CLIENT_SECRET,
            tenant_id=config.TENANT_ID,  # FDPO tenant — CORP blocks client_credentials via CA
        )
        logger.info("Redis relay initialized (endpoint=%s)", config.AZ_REDIS_CACHE_ENDPOINT)
    else:
        logger.warning("AZ_REDIS_CACHE_ENDPOINT not set — Redis relay disabled")

    start_server(
        agent_application=bot_app,
        auth_configuration=connection_manager.get_default_connection_configuration(),
    )


if __name__ == "__main__":
    main()
