# -*- coding: utf-8 -*-
"""skip_review in the pyRevit user config removes only the review dialog."""
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from revit_mcp import pairing

ROOT = Path(__file__).resolve().parents[2]
SECTION = "mcp-server-for-revit-python.extension"


class FakeUserConfig(object):
    """Stand-in for pyrevit.userconfig.user_config."""

    def __init__(self, sections=None):
        self.sections = sections if sections is not None else {}
        self.reloads = 0

    def reload(self):
        self.reloads += 1

    def has_section(self, name):
        return name in self.sections

    def get_section(self, name):
        values = self.sections[name]
        return SimpleNamespace(get_option=lambda option, default_value=None: values.get(option, default_value))


@pytest.fixture
def paired_user(monkeypatch, tmp_path):
    if os.name != "nt":
        pytest.skip("Windows DPAPI required")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    pairing.enable_pairing()
    return pairing.get_token()


@pytest.fixture
def security(monkeypatch):
    """Load revit_mcp/security.py against a mocked pyRevit with a user config."""
    alert = Mock(return_value=False)
    routes = SimpleNamespace(make_response=lambda data, status=200: SimpleNamespace(data=data, status=status))
    config = FakeUserConfig()
    monkeypatch.setitem(sys.modules, "pyrevit", SimpleNamespace(routes=routes, forms=SimpleNamespace(alert=alert)))
    monkeypatch.setitem(sys.modules, "pyrevit.userconfig", SimpleNamespace(user_config=config))
    spec = importlib.util.spec_from_file_location("revit_mcp._tested_review_security", ROOT / "revit_mcp/security.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return SimpleNamespace(module=module, alert=alert, config=config)


def _code_request():
    return SimpleNamespace(data={"code": "print('hi')"}, headers=pairing.authorization_headers())


@pytest.mark.parametrize("value", [True, "true", "True", "1", "yes"])
def test_skip_review_on_skips_only_the_dialog(security, paired_user, value):
    security.config.sections[SECTION] = {"skip_review": value}
    assert security.module.authorize_request(_code_request(), "Execute Python code") is None
    security.alert.assert_not_called()


@pytest.mark.parametrize("sections", [
    {},
    {SECTION: {}},
    {SECTION: {"skip_review": False}},
    {SECTION: {"skip_review": "false"}},
    {SECTION: {"skip_review": "0"}},
    {"some-other.extension": {"skip_review": True}},
])
def test_skip_review_off_or_missing_shows_the_dialog(security, paired_user, sections):
    security.config.sections.update(sections)
    assert security.module.authorize_request(_code_request(), "Execute Python code").status == 403
    security.alert.assert_called_once()


def test_skip_review_is_read_on_every_request(security, paired_user):
    authorize = security.module.authorize_request
    security.config.sections[SECTION] = {"skip_review": True}
    assert authorize(_code_request(), "Execute Python code") is None
    security.config.sections[SECTION]["skip_review"] = False
    assert authorize(_code_request(), "Execute Python code").status == 403
    assert security.config.reloads == 2
    security.alert.assert_called_once()


def test_config_errors_keep_the_dialog(security, paired_user):
    def broken(name):
        raise RuntimeError("config unavailable")
    security.config.sections[SECTION] = {"skip_review": True}
    security.config.get_section = broken
    assert security.module.authorize_request(_code_request(), "Execute Python code").status == 403
    security.alert.assert_called_once()


def test_missing_user_config_keeps_the_dialog(security, paired_user, monkeypatch):
    monkeypatch.setitem(sys.modules, "pyrevit.userconfig", SimpleNamespace(user_config=None))
    assert security.module.authorize_request(_code_request(), "Execute Python code").status == 403
    security.alert.assert_called_once()


def test_skip_review_never_skips_pairing(security, paired_user):
    security.config.sections[SECTION] = {"skip_review": True}
    for headers in ({}, {"Authorization": "Bearer wrong"},
                    dict(pairing.authorization_headers(), Origin="https://example.com")):
        request = SimpleNamespace(data={"code": "print('hi')"}, headers=headers)
        assert security.module.authorize_request(request, "Execute Python code").status == 401
    security.alert.assert_not_called()


def test_skip_review_never_skips_callback_rejection(security, paired_user):
    security.config.sections[SECTION] = {"skip_review": True}
    request = SimpleNamespace(data={"code": "x", "callbackUrl": "https://example.com/c"},
                              headers=pairing.authorization_headers())
    assert security.module.authorize_request(request, "Execute Python code").status == 400
    assert "callbackUrl" not in request.data
    security.alert.assert_not_called()
