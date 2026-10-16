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

"""Shared test setup.

CI copies .env.example to .env before running pytest, so MODEL_NAME is set
there. Locally, a checkout without a .env would make `import app.agent` fail
with a clear RuntimeError; load the example file instead so the unit tests
run from a fresh clone. INTEGRATION_TEST keeps the serving app from wiring
Cloud Trace exporters when Application Default Credentials happen to exist
on the developer's machine.
"""

import os
import pathlib

from dotenv import load_dotenv

_RECIPE_ROOT = pathlib.Path(__file__).resolve().parent.parent

if not os.getenv("MODEL_NAME"):
    load_dotenv(_RECIPE_ROOT / ".env.example")
os.environ.setdefault("INTEGRATION_TEST", "1")
