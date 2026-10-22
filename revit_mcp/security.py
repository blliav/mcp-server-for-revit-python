# -*- coding: utf-8 -*-
"""Authentication and in-Revit review for bridge requests."""
import json
from pyrevit import routes
from .pairing import validate_request

# Advanced-user opt-out, set in %APPDATA%\pyRevit\pyRevit_config.ini:
#   [mcp-server-for-revit-python.extension]
#   skip_review = true
CONFIG_SECTION = "mcp-server-for-revit-python.extension"
SKIP_REVIEW_OPTION = "skip_review"


def review_skipped():
    """Return True only when the user config explicitly sets skip_review.

    Read on every request so editing the ini takes effect without reloading
    pyRevit. Any problem reading the config keeps the review dialog on.
    """
    try:
        from pyrevit.userconfig import user_config
        if user_config is None:
            return False
        reload_config = getattr(user_config, "reload", None)
        if reload_config is not None:
            reload_config()
        if not user_config.has_section(CONFIG_SECTION):
            return False
        value = user_config.get_section(CONFIG_SECTION).get_option(
            SKIP_REVIEW_OPTION, False)
    except Exception:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def authorize_request(request, action=None):
    """Return a denial response, or None when the request may proceed."""
    # pyRevit's dispatcher can post a route response to callbackUrl. Remove it
    # before returning so even a denial cannot initiate an undeclared request.
    if isinstance(request.data, dict) and "callbackUrl" in request.data:
        request.data.pop("callbackUrl", None)
        return routes.make_response(data={"error": "Callbacks are not supported."}, status=400)
    if not validate_request(request.headers):
        return routes.make_response(data={"error": "Bridge is disconnected or pairing is invalid."}, status=401)
    # skip_review only removes the dialog below; callbackUrl rejection and the
    # pairing check above always run.
    if action and not review_skipped():
        from pyrevit import forms
        try:
            data = json.loads(request.data) if isinstance(request.data, str) else request.data
            preview = json.dumps(data, indent=2, ensure_ascii=True)
        except (ValueError, TypeError):
            return routes.make_response(data={"error": "Invalid request payload."}, status=400)
        warning = "An MCP client requests: {}.\nReview the full request below before allowing it.".format(action)
        if action == "Execute Python code":
            warning += "\nThis code is NOT sandboxed. It can access files, network services and any data available to your Windows account."
        if not forms.alert(warning, sub_msg=preview, title="Revit MCP request review",
                           ok=False, yes=True, no=True, warn_icon=True):
            return routes.make_response(data={"error": "The user declined the request in Revit."}, status=403)
    return None
