"""Redis Relay — bridges Microsoft Teams ↔ Azure Managed Redis for remote
task delivery to the WorkIQ desktop agent.

Responsibilities:
  • Push user messages to the agent's Redis inbox stream
  • Poll the agent's Redis outbox stream for responses
  • Deliver responses back to Teams via proactive messaging
  • Check whether the desktop agent is online (presence key)
"""

import asyncio
import json
import logging
import threading
import time
import uuid

import redis
from azure.identity import DefaultAzureCredential
from redis_entraid.cred_provider import create_from_default_azure_credential

logger = logging.getLogger(__name__)


class RedisRelay:
    """Manages Redis connection, inbox push, outbox polling, and proactive
    delivery of responses back to Teams."""

    def __init__(self, endpoint: str):
        parts = endpoint.rsplit(":", 1)
        self._host = parts[0]
        self._port = int(parts[1]) if len(parts) > 1 else 10000

        self._client: redis.RedisCluster | None = None
        self._stopping = threading.Event()

        # email → ConversationReference dict (set from agent_sdk on each inbound msg)
        self._conversation_refs: dict = {}
        self._refs_lock = threading.Lock()

        # email → last_outbox_id  (track XREAD position per user)
        self._outbox_cursors: dict[str, str] = {}

        # email → last_activity_epoch  (for inactivity cleanup)
        self._active_users: dict[str, float] = {}
        self._users_lock = threading.Lock()

        self._app_id: str | None = None
        self._client_secret: str | None = None
        self._tenant_id: str | None = None

        self._poller_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _connect(self):
        """Create the Redis cluster connection using redis-entraid."""
        credential_provider = create_from_default_azure_credential(
            ("https://redis.azure.com/.default",)
        )
        self._client = redis.RedisCluster(
            host=self._host,
            port=self._port,
            ssl=True,
            ssl_cert_reqs=None,
            decode_responses=True,
            credential_provider=credential_provider,
            socket_timeout=10,
            socket_connect_timeout=10,
        )
        self._client.ping()
        self._connected_at = time.time()
        logger.info("Redis relay connected to %s:%d", self._host, self._port)

    def _ensure_connected(self):
        """Create a connection if none exists. Does NOT tear down healthy
        connections — redis-entraid handles token refresh internally."""
        if self._client is None:
            self._connect()

    def _ping_or_reconnect(self):
        """Verify the connection is alive; reconnect if not."""
        try:
            self._ensure_connected()
            self._client.ping()
        except Exception as e:
            logger.warning("Redis PING failed (%s) — reconnecting", e)
            try:
                if self._client:
                    self._client.close()
            except Exception:
                pass
            self._client = None
            self._connect()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, app_id: str, client_secret: str, tenant_id: str):
        """Start the relay (call from agent_sdk.main)."""
        self._app_id = app_id
        self._client_secret = client_secret
        self._tenant_id = tenant_id
        try:
            self._connect()
        except Exception as e:
            logger.error("Redis relay failed to connect: %s", e)
            return

        self._start_poller_thread()
        logger.info("Redis relay started — outbox poller running")

    def _start_poller_thread(self):
        """Start (or restart) the outbox poller daemon thread."""
        self._poller_thread = threading.Thread(
            target=self._poll_outbox_loop, daemon=True, name="redis-outbox-poller"
        )
        self._poller_thread.start()

    def _ensure_poller_alive(self):
        """Check if the poller thread is still running; restart if dead."""
        if self._poller_thread is None or not self._poller_thread.is_alive():
            logger.warning("Outbox poller thread is dead — restarting")
            self._ensure_connected()
            self._start_poller_thread()

    def stop(self):
        self._stopping.set()
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
        logger.info("Redis relay stopped")

    # ------------------------------------------------------------------
    # Public API (called by agent_sdk.py message handler)
    # ------------------------------------------------------------------

    def is_agent_online(self, email: str) -> dict | None:
        """Check if the desktop agent is registered in Redis.
        Returns parsed JSON info dict or None."""
        try:
            self._ping_or_reconnect()
            key = f"workiq:agents:{email.lower()}"
            raw = self._client.get(key)
            if raw:
                return json.loads(raw)
        except Exception as e:
            logger.error("Failed to check agent presence for %s: %s", email, e)
        return None

    def push_to_inbox(self, email: str, sender: str, text: str) -> str:
        """Push a message to the agent's Redis inbox stream.
        Returns the generated msg_id."""
        msg_id = uuid.uuid4().hex[:16]
        inbox_key = f"workiq:inbox:{email.lower()}"
        try:
            self._ping_or_reconnect()
            self._client.xadd(inbox_key, {
                "sender": sender,
                "text": text,
                "ts": str(time.time()),
                "msg_id": msg_id,
            })
            logger.info("Pushed to inbox %s (msg_id=%s): %.80s", inbox_key, msg_id, text)
        except Exception as e:
            logger.error("Failed to push to inbox %s: %s", inbox_key, e)
            raise
        return msg_id

    def register_active_user(self, email: str, conversation_ref):
        """Register a user as active and store their ConversationReference
        for proactive messaging."""
        email_lower = email.lower()
        with self._refs_lock:
            self._conversation_refs[email_lower] = conversation_ref
        with self._users_lock:
            self._active_users[email_lower] = time.time()
            # Use "0" so the first poll catches any responses already in
            # the stream (e.g. agent replied while the poller was dead).
            # After the first read, the cursor advances to the latest ID.
            if email_lower not in self._outbox_cursors:
                self._outbox_cursors[email_lower] = "0"

        # Ensure the poller thread is alive — it may have died due to
        # a Redis connection failure or token expiry overnight
        self._ensure_poller_alive()

    # ------------------------------------------------------------------
    # Outbox poller (background thread)
    # ------------------------------------------------------------------

    def _poll_outbox_loop(self):
        """Continuously poll outbox streams for all active users."""
        INACTIVITY_TIMEOUT = 3600  # 1 hour
        POLL_INTERVAL = 3  # seconds between poll cycles

        while not self._stopping.is_set():
            try:
                # Clean up inactive users
                now = time.time()
                with self._users_lock:
                    expired = [
                        email for email, last in self._active_users.items()
                        if (now - last) > INACTIVITY_TIMEOUT
                    ]
                    for email in expired:
                        del self._active_users[email]
                        self._outbox_cursors.pop(email, None)
                        with self._refs_lock:
                            self._conversation_refs.pop(email, None)
                        logger.info("Removed inactive user %s from outbox polling", email)

                # Get snapshot of active users
                with self._users_lock:
                    active = dict(self._active_users)

                if not active:
                    self._stopping.wait(timeout=POLL_INTERVAL)
                    continue

                # Poll each user's outbox
                for email in active:
                    if self._stopping.is_set():
                        break
                    self._poll_user_outbox(email)

                self._stopping.wait(timeout=POLL_INTERVAL)

            except redis.ConnectionError as e:
                logger.warning("Redis connection lost in outbox poller: %s — reconnecting", e)
                self._client = None
                self._stopping.wait(timeout=5)
            except Exception as e:
                logger.error("Outbox poll error: %s", e, exc_info=True)
                self._stopping.wait(timeout=5)

    def _poll_user_outbox(self, email: str):
        """Read new messages from a single user's outbox stream."""
        outbox_key = f"workiq:outbox:{email}"
        last_id = self._outbox_cursors.get(email, "$")

        try:
            self._ping_or_reconnect()
            result = self._client.xread(
                {outbox_key: last_id}, block=1000, count=10
            )
            if not result:
                return

            for _stream, messages in result:
                for msg_id, fields in messages:
                    self._outbox_cursors[email] = msg_id
                    self._handle_outbox_message(email, fields)
                    # Delete the message from the stream after delivery
                    # so it is not replayed on restart
                    try:
                        self._client.xdel(outbox_key, msg_id)
                    except Exception as e:
                        logger.warning("Failed to XDEL %s from %s: %s", msg_id, outbox_key, e)

        except redis.ConnectionError:
            raise  # let the outer loop handle reconnect
        except Exception as e:
            logger.error("Error polling outbox for %s: %s", email, e)

    def _handle_outbox_message(self, email: str, fields: dict):
        """Process a single outbox message and deliver it to Teams."""
        status = fields.get("status", "")
        text = fields.get("text", "")
        in_reply_to = fields.get("in_reply_to", "")
        task_id = fields.get("task_id", "")

        if not text:
            logger.warning("Empty outbox message for %s (task=%s) — skipping", email, task_id)
            return

        if status == "failed":
            response_text = f"❌ **Request failed**: {text}"
        else:
            response_text = text

        logger.info(
            "Outbox response for %s (task=%s, in_reply_to=%s, status=%s): %.80s",
            email, task_id, in_reply_to, status, text,
        )

        with self._refs_lock:
            conversation_ref = self._conversation_refs.get(email)

        if not conversation_ref:
            logger.warning("No conversation reference for %s — cannot deliver response", email)
            return

        self._deliver_proactive_message(conversation_ref, response_text)

    # ------------------------------------------------------------------
    # Proactive messaging via Bot Connector REST API
    # ------------------------------------------------------------------

    def _deliver_proactive_message(self, conversation_ref: dict, response_text: str):
        """Send a proactive message to Teams via the Bot Connector REST API.
        Uses the bot's own client credentials — no SDK event loop needed."""
        import msal
        import urllib.request
        import urllib.error

        service_url = conversation_ref.get("service_url", "")
        conversation = conversation_ref.get("conversation", {})
        conversation_id = conversation.get("id") if conversation else None

        if not service_url or not conversation_id:
            logger.error("Incomplete conversation reference — cannot deliver: %s", conversation_ref)
            return

        # Acquire Bot Framework token
        try:
            authority = f"https://login.microsoftonline.com/{self._tenant_id}"
            msal_app = msal.ConfidentialClientApplication(
                self._app_id,
                authority=authority,
                client_credential=self._client_secret,
            )
            token_result = msal_app.acquire_token_for_client(
                scopes=["https://api.botframework.com/.default"]
            )
            token = token_result.get("access_token") if token_result else None
            if not token:
                logger.error("Failed to acquire Bot Connector token: %s", token_result)
                return
        except Exception as e:
            logger.error("MSAL token acquisition failed: %s", e)
            return

        # Build the activity payload
        activity = {
            "type": "message",
            "text": response_text,
            "from": conversation_ref.get("bot"),
            "conversation": conversation,
            "recipient": conversation_ref.get("user"),
        }

        url = f"{service_url.rstrip('/')}/v3/conversations/{conversation_id}/activities"

        try:
            data = json.dumps(activity).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                logger.info(
                    "Proactive message delivered to %s (status=%s)",
                    conversation_id, resp.status,
                )
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:300]
            logger.error(
                "Proactive message failed: HTTP %s — %s", e.code, body,
            )
        except Exception as e:
            logger.error("Proactive message delivery failed: %s", e, exc_info=True)
