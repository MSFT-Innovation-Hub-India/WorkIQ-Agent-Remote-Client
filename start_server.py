"""aiohttp hosting bootstrap for the WorkIQ Teams Relay Bot.

Provides:
  POST /api/messages  — Bot Framework message endpoint
  GET  /health        — Container App health probe
"""

from os import environ

try:
    from microsoft_agents.hosting.core import AgentApplication, AgentAuthConfiguration
    from microsoft_agents.hosting.aiohttp import (
        start_agent_process,
        jwt_authorization_middleware,
        CloudAdapter,
    )
except ImportError:
    from microsoft.agents.hosting.core import AgentApplication, AgentAuthConfiguration  # type: ignore[import-not-found]
    from microsoft.agents.hosting.aiohttp import (  # type: ignore[import-not-found]
        start_agent_process,
        jwt_authorization_middleware,
        CloudAdapter,
    )

from aiohttp.web import Request, Response, Application, run_app, json_response


def start_server(
    agent_application: AgentApplication,
    auth_configuration: AgentAuthConfiguration,
):
    """Start the aiohttp server for the agent application."""

    async def entry_point(req: Request) -> Response:
        agent: AgentApplication = req.app["agent_app"]
        adapter: CloudAdapter = req.app["adapter"]
        return await start_agent_process(req, agent, adapter)

    async def health(req: Request) -> Response:
        return json_response({"status": "healthy"})

    app = Application(middlewares=[jwt_authorization_middleware])
    app.router.add_post("/api/messages", entry_point)
    app.router.add_get("/health", health)
    app["agent_configuration"] = auth_configuration
    app["agent_app"] = agent_application
    app["adapter"] = agent_application.adapter

    port = int(environ.get("PORT", "3978"))
    host = environ.get("HOST", "0.0.0.0")

    print(f"Starting WorkIQ relay server on {host}:{port}")
    print(f"  Messages endpoint : http://{host}:{port}/api/messages")
    print(f"  Health endpoint   : http://{host}:{port}/health")

    try:
        run_app(app, host=host, port=port)
    except Exception as error:
        print(f"Error starting server: {error}")
        raise error
