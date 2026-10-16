"""The bounded model/tool loop. The only place model calls and tool execution happen.

Limits enforced per task: wall-clock deadline, model call count, tool call count,
per-request output tokens and the shared RMB budget. Model completion only ends
the loop; whether the files satisfy the task is judged by Eyes' independent scorer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from trae_agent.config import Config
from trae_agent.model_client import ModelClient, ModelError
from trae_agent.runtime import RunRuntime
from trae_agent.tools import TOOLS, ToolError
from trae_agent.workspace import Workspace, WorkspaceError

MAX_MANIFEST_FILE_CHARS = 32_000
MIN_REQUEST_SECONDS = 5.0
MAX_SUMMARY_CHARS = 4000
KEEP_MESSAGE_FIELDS = ("role", "content", "name", "tool_calls", "reasoning_content")

SYSTEM_PROMPT = """你是 trae-agent,一个在独立任务工作目录中执行编码任务的智能体。
你只能通过工具操作文件和运行验证,所有路径都是相对当前工作目录的相对路径。
工作方式:
1. 先用 read_file 阅读现有文件,理解任务相关代码。
2. 用 write_file 创建新文件,或 replace_text 精确修改现有文件。
3. 用 run_python 实际运行你写出的代码进行验证,根据真实输出修正问题。
4. 任务要求的所有文件都完成并验证后,用一条不带工具调用的最终消息总结:
   创建或修改了哪些文件、验证命令和结果。
注意:只创建任务明确要求的文件;不要编造验证结果;工具会返回真实执行输出。"""


@dataclass
class LoopOutcome:
    status: str  # succeeded | failed | timed_out
    error_code: str | None = None
    error_message: str | None = None
    summary: str | None = None
    stop_reasons: list[str] = field(default_factory=list)


class AgentLoop:
    def __init__(
        self,
        config: Config,
        client: ModelClient,
        workspace: Workspace,
        runtime: RunRuntime,
        emit,
    ) -> None:
        self.config = config
        self.client = client
        self.workspace = workspace
        self.runtime = runtime
        self.emit = emit

    def run(
        self,
        prompt: str,
        initial_files: dict[str, str],
        materialized: list[str],
        deadline: datetime,
    ) -> LoopOutcome:
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": self._user_message(prompt, initial_files, materialized)},
        ]
        outcome: LoopOutcome | None = None
        self.emit(
            "task.started",
            {
                "model_id": self.config.model_id,
                "thinking": self.config.thinking,
                "initial_files": materialized,
                "deadline": deadline.isoformat(),
            },
        )
        call_index = 0
        while outcome is None:
            outcome = self._step(messages, deadline, call_index)
            call_index += 1
        self.emit(
            "task.ended",
            {"status": outcome.status, "error_code": outcome.error_code},
        )
        return outcome

    def _step(
        self, messages: list[dict], deadline: datetime, call_index: int
    ) -> LoopOutcome | None:
        remaining = self.runtime.remaining_seconds(deadline)
        if remaining <= MIN_REQUEST_SECONDS:
            return LoopOutcome(
                status="timed_out",
                error_code="deadline_exceeded",
                error_message=f"Task deadline reached with {remaining:.1f}s remaining",
            )
        if self.runtime.model_calls >= self.config.max_model_calls:
            return self._limit_outcome(
                "model_call_limit_exceeded",
                f"Model call limit {self.config.max_model_calls} reached",
            )

        if self.config.capture_model_io:
            self.emit(
                "model.input",
                {
                    "messages": [
                        {k: v for k, v in m.items() if k != "reasoning_content"} for m in messages
                    ]
                },
            )
        self.emit(
            "model.request",
            {
                "call_index": call_index,
                "message_count": len(messages),
                "max_output_tokens": self.config.max_output_tokens,
            },
        )
        self.runtime.model_calls += 1
        try:
            response = self.client.complete(
                messages,
                timeout_seconds=max(
                    MIN_REQUEST_SECONDS, min(self.config.request_timeout_seconds, remaining - 1.0)
                ),
                call_index=call_index,
            )
        except ModelError as error:
            self.emit(
                "model.error", {"call_index": call_index, "code": error.code, **error.diagnostics}
            )
            return LoopOutcome(
                status="failed",
                error_code=error.code,
                error_message=error.message,
            )
        usage = response.usage or {}
        self.runtime.model_prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.runtime.model_completion_tokens += int(usage.get("completion_tokens") or 0)
        self.runtime.cost_rmb += response.cost_rmb
        response_data = {
            **response.diagnostics,
            "call_index": call_index,
            "finish_reason": response.finish_reason,
            "tool_call_count": len(response.tool_calls),
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "latency_ms": response.latency_ms,
            "cost_rmb": round(response.cost_rmb, 6),
        }
        if self.config.capture_model_io:
            response_data["content"] = _content_text(response.message.get("content"))
        self.emit("model.response", response_data)

        if response.tool_calls:
            return self._run_tool_calls(messages, response, deadline)
        return self._finish_from_message(response)

    def _run_tool_calls(
        self, messages: list[dict], response, deadline: datetime
    ) -> LoopOutcome | None:
        message = response.message
        kept = {key: message[key] for key in KEEP_MESSAGE_FIELDS if key in message}
        if "content" not in kept:
            kept["content"] = None
        messages.append(kept)
        for index, tool_call in enumerate(response.tool_calls):
            if self.runtime.tool_calls >= self.config.max_tool_calls:
                return self._limit_outcome(
                    "tool_call_limit_exceeded",
                    f"Tool call limit {self.config.max_tool_calls} reached",
                )
            name, arguments, call_id, parse_error = _parse_tool_call(tool_call)
            self.emit(
                "tool.call",
                {
                    "tool_call_id": call_id,
                    "name": name,
                    "arguments_bytes": len(arguments) if arguments is not None else 0,
                    "batch_index": index,
                    **({"arguments": arguments} if self.config.capture_model_io else {}),
                },
            )
            if parse_error is not None:
                result = json.dumps({"ok": False, "error": parse_error}, ensure_ascii=False)
            else:
                result = self._execute_tool(name, arguments)
            self.emit(
                "tool.result",
                {
                    "tool_call_id": call_id,
                    "name": name,
                    "ok": _result_ok(result),
                    "result_bytes": len(result),
                    **({"result": result} if self.config.capture_model_io else {}),
                },
            )
            messages.append({"role": "tool", "tool_call_id": call_id, "content": result})
        return None

    def _execute_tool(self, name: str, arguments: dict) -> str:
        tool = TOOLS.get(name)
        if tool is None:
            return json.dumps({"ok": False, "error": f"unknown tool: {name}"}, ensure_ascii=False)
        try:
            return tool(self.workspace, self.runtime, arguments)
        except (ToolError, WorkspaceError) as error:
            # Rejected calls are reported to the model verbatim; never faked as success.
            return json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False)
        except Exception as error:  # unexpected tool failure is reported, never faked
            return json.dumps(
                {"ok": False, "error": f"tool crashed: {type(error).__name__}"},
                ensure_ascii=False,
            )

    def _finish_from_message(self, response) -> LoopOutcome:
        if response.finish_reason == "stop":
            text = _content_text(response.message.get("content"))
            return LoopOutcome(
                status="succeeded",
                summary=text[:MAX_SUMMARY_CHARS] if text else "",
            )
        if response.finish_reason == "length":
            return LoopOutcome(
                status="failed",
                error_code="output_truncated",
                error_message="Model output hit max_completion_tokens before completing",
            )
        return LoopOutcome(
            status="failed",
            error_code=f"model_finish_{response.finish_reason or 'none'}",
            error_message=f"Model ended with finish_reason={response.finish_reason!r}",
        )

    def _limit_outcome(self, code: str, message: str) -> LoopOutcome:
        self.emit("loop.limits", {"code": code, "message": message})
        return LoopOutcome(status="failed", error_code=code, error_message=message)

    def _user_message(self, prompt: str, files: dict[str, str], materialized: list[str]) -> str:
        parts = [prompt.strip()]
        if materialized:
            parts.append("\n初始文件已写入工作目录:")
            for name in materialized:
                content = files[name]
                if len(content) > MAX_MANIFEST_FILE_CHARS:
                    parts.append(
                        f"### {name}\n(内容较长,共 {len(content)} 字符,请用 read_file 读取)"
                    )
                else:
                    parts.append(f"### {name}\n{content}")
        else:
            parts.append("\n工作目录当前为空。")
        return "\n".join(parts)


def _parse_tool_call(tool_call: dict) -> tuple[str, dict | None, str, str | None]:
    function = tool_call.get("function") or {}
    name = function.get("name") or "<missing>"
    call_id = tool_call.get("id") or f"call_{id(tool_call)}"
    raw = function.get("arguments")
    arguments_raw = raw if isinstance(raw, str) else json.dumps(raw or {})
    try:
        parsed = json.loads(arguments_raw)
    except ValueError as error:
        return name, None, call_id, f"invalid tool arguments JSON: {error}"
    if not isinstance(parsed, dict):
        return name, None, call_id, "tool arguments must be a JSON object"
    return name, parsed, call_id, None


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def _result_ok(result: str) -> bool:
    try:
        return bool(json.loads(result).get("ok"))
    except ValueError:
        return False
