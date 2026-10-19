"""Temporary verification harness (requests is not installed in this environment)."""

import sys
import types
from pathlib import Path
import tempfile

fake = types.ModuleType("requests")


class _RequestException(Exception):
    pass


class _Response:
    pass


fake.RequestException = _RequestException
fake.Response = _Response
fake.exceptions = types.SimpleNamespace(
    RequestException=_RequestException,
    SSLError=_RequestException,
    ConnectionError=_RequestException,
    Timeout=_RequestException,
    HTTPError=_RequestException,
    ChunkedEncodingError=_RequestException,
)
fake.post = fake.get = lambda *a, **k: None
sys.modules.setdefault("requests", fake)
sys.modules.setdefault("requests.exceptions", fake.exceptions)

sys.path.insert(0, str(Path(__file__).parent))

from ut_analyzer.adapters.python.adapter import PythonAdapter  # noqa: E402
from ut_analyzer.core.generation.llm_errors import (  # noqa: E402
    ContextLengthError,
    GenerationError,
)
from ut_analyzer.core.generation.llm_settings import LLMSettings  # noqa: E402
from ut_analyzer.core.generation.test_generator import (  # noqa: E402
    generate_proposal_for_gap,
)
from ut_analyzer.models import EnvironmentInfo, QualityGap, TestConventions  # noqa: E402

RESPONSE = (
    "=== TEST: test_add | no | adds two numbers | 1, 2 | 3\n"
    "def test_add():\n"
    "    assert add(1, 2) == 3\n"
    "=== END TEST ==="
)


class Client:
    def __init__(self, failures):
        self.failures = failures
        self.prompts = []

    def generate(self, prompt, system="", **kw):
        self.prompts.append(prompt)
        if len(self.prompts) <= self.failures:
            raise ContextLengthError("maximum context length is 8192 tokens")
        return RESPONSE


def run(failures):
    tmp = Path(tempfile.mkdtemp())
    source = tmp / "calc.py"
    source.write_text("def add(a, b):\n    return a + b\n" + "# filler\n" * 4000)
    gap = QualityGap(
        source_file=source, reason="no test file", untested_functions=["add"]
    )
    client = Client(failures)
    change = generate_proposal_for_gap(
        gap=gap,
        adapter=PythonAdapter(),
        env=EnvironmentInfo(language="python", test_framework="pytest"),
        conventions=TestConventions(),
        test_files=[],
        client=client,
        test_dir=tmp / "tests",
        root=tmp,
        llm_settings=LLMSettings(model="m", context_tokens=128_000),
    )
    return client, change


client, change = run(1)
print("attempts:", len(client.prompts))
print("prompt sizes:", [len(p) for p in client.prompts])
print("tests:", [t.name for t in change.tests])
assert len(client.prompts) == 2
assert len(client.prompts[1]) < len(client.prompts[0])

try:
    run(99)
except GenerationError as exc:
    print("---- persistent overflow message ----")
    print(str(exc)[:600])
else:
    raise AssertionError("expected GenerationError")


class ProseClient(Client):
    def generate(self, prompt, system="", **kw):
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            return "I'm sorry, I cannot help with that."
        return RESPONSE


import ut_analyzer.core.generation.test_generator as tg  # noqa: E402

_orig = Client
Client = ProseClient
client, change = run(0)
print("prose retry attempts:", len(client.prompts), [len(p) for p in client.prompts])
assert len(client.prompts) == 2
assert [t.name for t in change.tests] == ["test_add"]
print("OK")
