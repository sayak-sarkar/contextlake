"""The suite runs with the Hugging Face Hub switched off, and the library agrees.

`conftest.py` sets HF_HUB_OFFLINE at import. huggingface_hub reads that variable once,
into a constant, when it is first imported. If anything imports the library before
conftest loads, the variable still reads "1" and nothing is offline. So this asks the
library, not the environment.
"""

import os

import pytest


def test_the_hub_library_itself_reports_offline():
    constants = pytest.importorskip("huggingface_hub.constants")

    assert os.environ.get("HF_HUB_OFFLINE") == "1"
    assert constants.HF_HUB_OFFLINE is True
