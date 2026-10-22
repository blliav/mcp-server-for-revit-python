"""Keep public tool descriptions and local consent checks consistent."""
from mcp.types import ToolAnnotations
from .catalog import TOOL_DESCRIPTIONS

READ_ONLY = {
    "list_revit_instances", "get_revit_status", "get_revit_model_info", "list_levels", "get_revit_view",
    "list_revit_views", "get_current_view_info", "get_current_view_elements",
    "list_families", "list_family_categories", "list_category_parameters",
    "list_revit_installations",
}


class ReviewedTools:
    def __init__(self, server):
        self.server = server

    def tool(self, **kwargs):
        def register(function):
            name = function.__name__
            kwargs["description"] = TOOL_DESCRIPTIONS[name]
            kwargs["annotations"] = ToolAnnotations(
                readOnlyHint=name in READ_ONLY,
                destructiveHint=name not in READ_ONLY,
                openWorldHint=name in {"execute_revit_code", "sync_with_central",
                                       "open_document", "save_document", "launch_revit"},
            )
            return self.server.tool(**kwargs)(function)
        return register
