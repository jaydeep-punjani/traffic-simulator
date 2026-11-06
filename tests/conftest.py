import socket
import threading
import time

import pytest
import uvicorn

from trafficsim.config import Destinations, Scenario
from trafficsim.mock_collector import Collector, make_app


def scenario(**kw) -> Scenario:
    data = {
        "name": "t",
        "duration": "2h",
        "speed": 600,
        "seed": 3,
        "traffic": {"base_concurrency": 300, "regions": {"us": 1.0}},
        "verticals": {"streaming": {}, "ecom": {}, "gaming": {}},
    }
    for k, v in kw.items():
        data[k] = v
    return Scenario.model_validate(data)


@pytest.fixture
def make_scenario():
    return scenario


@pytest.fixture(scope="session")
def collector():
    """A real mock collector served by uvicorn in a background thread."""
    col = Collector(sample_every=1)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(make_app(col), host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    yield col, f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(5)


@pytest.fixture
def dest(collector):
    return Destinations(host=collector[1], default_write_key="test-key", sources={"gaming_ps5": "ps5-key"})
