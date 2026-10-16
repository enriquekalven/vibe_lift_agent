# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Boot check: the serving app starts without credentials and lists the agent.

Mirrors what the repo's container check probes (GET /list-apps and the A2A
agent card) but runs in-process, so it is cheap enough for every pytest run.
"""

import warnings

from fastapi.testclient import TestClient


def test_server_boots_and_lists_the_agent() -> None:
    warnings.filterwarnings("ignore")
    from app.fast_api_app import app

    with TestClient(app) as client:  # entering the context runs the lifespan
        listed = client.get("/list-apps")
        assert listed.status_code == 200
        assert "app" in listed.json()
        card_routes = [r.path for r in app.routes if "well-known" in r.path]
        assert card_routes, "A2A agent card route was not attached"
        assert client.get(card_routes[0]).status_code == 200
