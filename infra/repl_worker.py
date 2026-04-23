#!/usr/bin/env python3
"""
Isolated REPL worker process.
Reads JSON commands from stdin, executes code, writes JSON responses to a dedicated pipe.
Maintains persistent namespace across calls.

Protocol:
- Responses go to a dedicated fd (not stdout) to avoid contamination from user code.
- Each request includes a nonce; the response echoes it. The parent discards
  any response without a matching nonce, making fd-hijack attacks harmless.
"""
import sys
import os
import io
import ast
import asyncio
import inspect
import json
import traceback

_ASYNC_FLAG = ast.PyCF_ALLOW_TOP_LEVEL_AWAIT


def _run_async_code(code_obj, ns, mode: str):
    """Execute code that may contain top-level `await`.

    When compiled with PyCF_ALLOW_TOP_LEVEL_AWAIT, a code object with top-level
    await gets CO_COROUTINE set. `eval(code_obj, ns)` on such a code object
    returns a coroutine without running it — asyncio.run drives it to completion.

    For mode="eval", returns the result (possibly None for expr statements).
    For mode="exec", returns None.
    """
    if code_obj.co_flags & inspect.CO_COROUTINE:
        return asyncio.run(eval(code_obj, ns))
    if mode == "eval":
        return eval(code_obj, ns)
    exec(code_obj, ns)
    return None

# Persistent namespace
_namespace = {}

# Response pipe — dedicated fd, passed by parent via env var
_response_fd = None
_response_file = None

# Output limits
_MAX_CHARS = 100_000
_HEAD_LINES = 200
_TAIL_LINES = 50


def _truncate(text: str) -> str:
    """Truncate output: keep head + tail lines, cap total characters."""
    if len(text) <= _MAX_CHARS:
        lines = text.split("\n")
        if len(lines) <= _HEAD_LINES + _TAIL_LINES:
            return text

    lines = text.split("\n")
    total = len(lines)

    if total <= _HEAD_LINES + _TAIL_LINES:
        return text[:_MAX_CHARS] + f"\n\n... truncated ({len(text):,} chars total)"

    head = lines[:_HEAD_LINES]
    tail = lines[-_TAIL_LINES:]
    skipped = total - _HEAD_LINES - _TAIL_LINES
    result = "\n".join(head) + f"\n\n... {skipped:,} lines omitted ...\n\n" + "\n".join(tail)

    if len(result) > _MAX_CHARS:
        result = result[:_MAX_CHARS] + f"\n\n... truncated ({len(text):,} chars total)"

    return result


def _send_response(data: dict):
    """Write a JSON response to the dedicated response pipe."""
    line = json.dumps(data, default=str) + "\n"
    _response_file.write(line)
    _response_file.flush()


def execute_code(code: str) -> dict:
    """Execute code and return result dict."""
    global _namespace

    code_stripped = code.strip()

    # Handle special commands
    if code_stripped == "%vars":
        if not _namespace:
            return {"output": "No variables defined"}
        vars_list = []
        for name, value in _namespace.items():
            if not name.startswith("_"):
                type_name = type(value).__name__
                try:
                    repr_val = repr(value)
                except Exception:
                    repr_val = "<repr failed>"
                if len(repr_val) > 50:
                    repr_val = repr_val[:47] + "..."
                vars_list.append(f"  {name}: {type_name} = {repr_val}")
        return {"output": "Defined variables:\n" + "\n".join(vars_list) if vars_list else "No user variables defined"}

    if code_stripped == "%clear":
        _namespace.clear()
        return {"output": "Cleared all variables"}

    # Redirect sys.stdout/stderr to capture print() calls
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    sys.stdout = captured_stdout = io.StringIO()
    sys.stderr = captured_stderr = io.StringIO()

    # Redirect fd 1/2 to devnull so subprocess/os.write/C printf
    # can't contaminate our response pipe
    saved_fd1 = os.dup(1)
    saved_fd2 = os.dup(2)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.close(devnull)

    result = None
    error = None

    try:
        try:
            compiled = compile(code, "<repl>", "eval", flags=_ASYNC_FLAG)
            result = _run_async_code(compiled, _namespace, mode="eval")
        except SyntaxError:
            try:
                tree = ast.parse(code)
                if tree.body and isinstance(tree.body[-1], ast.Expr):
                    if len(tree.body) > 1:
                        body_code = compile(
                            ast.Module(body=tree.body[:-1], type_ignores=[]),
                            "<repl>", "exec", flags=_ASYNC_FLAG,
                        )
                        _run_async_code(body_code, _namespace, mode="exec")
                    last_expr = ast.Expression(body=tree.body[-1].value)
                    ast.fix_missing_locations(last_expr)
                    last_code = compile(last_expr, "<repl>", "eval", flags=_ASYNC_FLAG)
                    result = _run_async_code(last_code, _namespace, mode="eval")
                else:
                    body_code = compile(code, "<repl>", "exec", flags=_ASYNC_FLAG)
                    _run_async_code(body_code, _namespace, mode="exec")
            except SyntaxError:
                body_code = compile(code, "<repl>", "exec", flags=_ASYNC_FLAG)
                _run_async_code(body_code, _namespace, mode="exec")
    except Exception:
        error = traceback.format_exc()
    finally:
        os.dup2(saved_fd1, 1)
        os.dup2(saved_fd2, 2)
        os.close(saved_fd1)
        os.close(saved_fd2)

        sys.stdout = old_stdout
        sys.stderr = old_stderr

    # Build response
    stdout_output = captured_stdout.getvalue()
    stderr_output = captured_stderr.getvalue()

    output_parts = []
    if stdout_output:
        output_parts.append(stdout_output.rstrip())
    if stderr_output:
        output_parts.append(f"STDERR:\n{stderr_output.rstrip()}")
    if error:
        output_parts.append(error.rstrip())
    if result is not None:
        try:
            output_parts.append(repr(result))
        except Exception:
            output_parts.append("<repr failed>")

    if not output_parts:
        return {"output": "Code executed successfully (no output)"}

    return {"output": _truncate("\n".join(output_parts))}


def main():
    """Main loop: read JSON commands, execute, write JSON responses."""
    global _response_fd, _response_file

    # Response fd passed by parent via environment variable.
    # Scrub env var so user code can't discover the fd number.
    orig_fd = int(os.environ.pop("_REPL_RESPONSE_FD"))
    _response_fd = orig_fd
    _response_file = os.fdopen(_response_fd, "w", buffering=1, closefd=False)

    # Signal ready on response pipe
    _send_response({"status": "ready"})

    for line in sys.stdin:
        try:
            cmd = json.loads(line.strip())
            if cmd.get("type") == "execute":
                response = execute_code(cmd["code"])
                # Echo the nonce so parent can verify this is a real response
                if "nonce" in cmd:
                    response["nonce"] = cmd["nonce"]
            elif cmd.get("type") == "ping":
                response = {"status": "pong"}
            else:
                response = {"error": "Unknown command"}
        except json.JSONDecodeError as e:
            response = {"error": f"Invalid JSON: {e}"}
        except Exception as e:
            response = {"error": str(e)}

        _send_response(response)


if __name__ == "__main__":
    main()
