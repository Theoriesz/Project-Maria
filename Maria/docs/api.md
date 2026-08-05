# Internal API

Maria v0.1 exposes internal Python interfaces between modules. These are not
public network APIs.

## Brain

`MariaBrain.process(user_input: str, confirmed: bool = False) -> BrainResponse`

Coordinates a request from input to plan, permission check, tool execution, and
memory recording.

## Planner

`Planner.create_plan(user_input: str, context: dict | None = None) -> Plan`

Returns a `Plan` containing zero or more `PlanStep` objects.

## Memory

The memory layer owns all SQLite access. Other modules should call methods such
as `record_interaction`, `get_recent_interactions`, `create_note`, `list_notes`,
and `get_note` instead of writing SQL directly.

## Tool Manager

`ToolManager.execute_tool(tool_name, action, arguments) -> ToolExecutionResult`

Tools must expose `name`, `description`, `actions`, and an `execute` method.

## Permissions

`PermissionManager.check(tool_name, action) -> PermissionResult`

Permission results are one of `allowed`, `requires_confirmation`, or `denied`.
