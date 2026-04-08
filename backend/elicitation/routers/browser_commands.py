"""REST endpoints for the browser command queue.

The Chrome extension polls GET /browser-commands/pending to pick up commands
and POSTs results to POST /browser-commands/{command_id}/result.

POST /browser-commands/execute is a synchronous endpoint that creates a command,
waits for the extension to execute it, and returns the result.  This lets
external callers (CLI, Claude Code) drive the browser without async Python.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.elicitation.browser_bridge import execute_command, get_pending_commands, set_result

router = APIRouter(prefix="/browser-commands", tags=["browser-commands"])


class BrowserCommandOut(BaseModel):
    id: str
    command_type: str
    params: dict


class CommandResultIn(BaseModel):
    success: bool
    data: dict | None = None
    error: str | None = None


class ExecuteCommandIn(BaseModel):
    command_type: str
    params: dict = {}


@router.get("/pending", response_model=list[BrowserCommandOut])
async def get_pending():
    """Return pending commands for the Chrome extension to execute."""
    commands = get_pending_commands()
    return [
        BrowserCommandOut(id=cmd.id, command_type=cmd.command_type, params=cmd.params)
        for cmd in commands
    ]


@router.post("/{command_id}/result")
async def post_result(command_id: str, body: CommandResultIn):
    """Receive execution result from the Chrome extension."""
    result_dict = {
        "success": body.success,
        "data": body.data,
        "error": body.error,
    }
    found = set_result(command_id, result_dict)
    if not found:
        raise HTTPException(404, f"Command {command_id} not found or already completed")
    return {"status": "ok"}


@router.post("/execute")
async def execute(body: ExecuteCommandIn):
    """Execute a browser command synchronously.

    Creates a command, waits for the Chrome extension to pick it up and
    return the result (up to 30 s), then returns the result.

    This is the primary endpoint for external callers (CLI, Claude Code)
    to drive the browser.
    """
    try:
        result = await execute_command(body.command_type, body.params)
    except TimeoutError as exc:
        raise HTTPException(504, str(exc)) from exc
    return result
