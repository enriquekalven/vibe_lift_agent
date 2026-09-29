"""FastAPI application for VibeLift Cloud Run deployment and ADK framework."""

import logging
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
  sys.path.insert(0, _ROOT)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger(__name__)

# Comma-separated CORS allowlist applied to both the ADK app and the fallback app. The default '*'
# keeps the previous behavior; the Cloud Run service is IAM-protected either way.
_ORIGINS = [o.strip() for o in os.environ.get('ALLOWED_ORIGINS', '*').split(',') if o.strip()]
_ALLOW_ALL = '*' in _ORIGINS

try:
  from google.adk.cli.fast_api import get_fast_api_app
  adk_app = get_fast_api_app(
      agents_dir=_ROOT,
      web=False,
      allow_origins=['*'] if _ALLOW_ALL else _ORIGINS,
      auto_create_session=True,
  )
  app = adk_app
except Exception:  # pylint: disable=broad-except
  # Keep serving the dashboard and /mcp if the ADK API server cannot start, but log it: in this mode
  # the ADK routes (/run, /run_sse, /list-apps, /health) are missing.
  logger.exception('ADK FastAPI app failed to load; serving VibeLift routes without the ADK API server')
  app = FastAPI(
      title='VibeLift | Analytics Platform for Agent Optimization',
      description='Cloud Run & ADK runtime for autonomous multi-objective agent telemetry and AlphaEvolve optimization',
      version='1.0.0',
  )
  app.add_middleware(
      CORSMiddleware,
      allow_origins=['*'] if _ALLOW_ALL else _ORIGINS,
      allow_credentials=not _ALLOW_ALL,
      allow_methods=['*'],
      allow_headers=['*'],
  )

from vibelift import mcp_server
from vibelift import server

controller = server._global_controller

# Register streamable HTTP MCP routes at /mcp
mcp_server.register_mcp_routes(app)

# Dashboard UI, agent card, and JSON APIs (including the live Gemini Enterprise fleet).
server.register_api_routes(app, controller)


if __name__ == '__main__':
  import uvicorn
  port = int(os.environ.get('PORT', '8080'))
  uvicorn.run(app, host='0.0.0.0', port=port)
