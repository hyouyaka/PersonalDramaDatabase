"""Serve update logs over the local network.

Run on the update PC, then open http://<server-ip>:8765 from another PC.
The server reads logs/daily-update-latest.log by default and can also show
daily-update-*.log and weekly-cv-update-*.log files from the same logs directory.
"""

from __future__ import annotations

import html
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(os.environ.get("DRAMA_DB_ROOT", Path(__file__).resolve().parent))
LOG_DIR = Path(os.environ.get("DRAMA_DB_LOG_DIR", ROOT / "logs"))
LOG_PREFIXES = tuple(
    prefix.strip()
    for prefix in os.environ.get("DRAMA_LOG_PREFIXES", "daily-update,weekly-cv-update").split(",")
    if prefix.strip()
)
LATEST_LOG = LOG_DIR / os.environ.get("DRAMA_LOG_LATEST", "daily-update-latest.log")
HOST = os.environ.get("DRAMA_LOG_HOST", "0.0.0.0")
PORT = int(os.environ.get("DRAMA_LOG_PORT", "8765"))
MAX_LOG_BYTES = int(os.environ.get("DRAMA_LOG_MAX_BYTES", "200000"))


def safe_log_path(name: str | None) -> Path:
    if not name or name == "latest":
        return LATEST_LOG

    candidate = (LOG_DIR / Path(name).name).resolve()
    log_dir = LOG_DIR.resolve()

    if candidate.parent != log_dir:
        raise ValueError("invalid log path")
    if candidate.suffix != ".log" or not any(candidate.name.startswith(f"{prefix}-") for prefix in LOG_PREFIXES):
        raise ValueError("invalid log name")

    return candidate


def tail_text(path: Path, max_bytes: int = MAX_LOG_BYTES) -> str:
    if not path.exists():
        return f"log not found: {path}"

    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > max_bytes:
            handle.seek(-max_bytes, 2)
            prefix = f"[showing last {max_bytes} bytes of {size} bytes]\n\n".encode("utf-8")
        else:
            prefix = b""
        data = prefix + handle.read()

    return data.decode("utf-8-sig", errors="replace")


def list_logs() -> list[dict[str, object]]:
    if not LOG_DIR.exists():
        return []

    logs_by_name: dict[str, dict[str, object]] = {}
    for prefix in LOG_PREFIXES:
        for path in LOG_DIR.glob(f"{prefix}-*.log"):
            stat = path.stat()
            logs_by_name[path.name] = {
                "name": path.name,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "is_latest": path.name == LATEST_LOG.name,
            }

    return sorted(logs_by_name.values(), key=lambda item: (bool(item["is_latest"]), float(item["mtime"])), reverse=True)


def infer_state(content: str) -> dict[str, str]:
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    last_lines = lines[-80:]
    joined = "\n".join(last_lines)

    if "=== Done final_exit=0" in joined:
        state = "success"
    elif "=== Done final_exit=" in joined:
        state = "failed"
    elif any("exit=1" in line or "ERROR" in line or "FATAL" in line for line in last_lines):
        state = "warning"
    elif lines:
        state = "running_or_recent"
    else:
        state = "empty"

    current_step = ""
    for line in reversed(lines):
        if line.startswith("=== ") and line.endswith("==="):
            current_step = line
            break

    return {"state": state, "current_step": current_step}


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: object) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def text_response(handler: BaseHTTPRequestHandler, status: int, content: str, content_type: str) -> None:
    body = content.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", f"{content_type}; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path == "/api/logs":
            json_response(
                self,
                200,
                {
                    "root": str(ROOT),
                    "log_dir": str(LOG_DIR),
                    "latest": LATEST_LOG.name,
                    "logs": list_logs(),
                },
            )
            return

        if parsed.path == "/api/log":
            name = query.get("name", ["latest"])[0]
            try:
                path = safe_log_path(name)
            except ValueError as exc:
                json_response(self, 400, {"error": str(exc)})
                return

            content = tail_text(path)
            json_response(
                self,
                200,
                {
                    "name": "latest" if path == LATEST_LOG else path.name,
                    "path": str(path),
                    "exists": path.exists(),
                    "content": content,
                    **infer_state(content),
                },
            )
            return

        if parsed.path == "/raw":
            name = query.get("name", ["latest"])[0]
            try:
                path = safe_log_path(name)
            except ValueError as exc:
                text_response(self, 400, str(exc), "text/plain")
                return
            text_response(self, 200, tail_text(path), "text/plain")
            return

        text_response(self, 200, render_page(), "text/html")


def render_page() -> str:
    title = "Drama Update Logs"
    escaped_log_dir = html.escape(str(LOG_DIR))
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #101418;
      --panel: #171d24;
      --panel-2: #202833;
      --text: #e7edf3;
      --muted: #9ba8b5;
      --line: #33404f;
      --accent: #3b82f6;
      --ok: #22c55e;
      --warn: #f59e0b;
      --bad: #ef4444;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Consolas, "Cascadia Mono", "Microsoft YaHei", monospace;
    }}
    header {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 12px;
      align-items: center;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 2;
    }}
    h1 {{
      margin: 0;
      font-size: 16px;
      font-weight: 700;
    }}
    .sub {{
      margin-top: 4px;
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }}
    .toolbar {{
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
      justify-content: flex-end;
    }}
    select, button, a.button {{
      min-height: 34px;
      border: 1px solid var(--line);
      background: var(--panel-2);
      color: var(--text);
      padding: 6px 10px;
      font: inherit;
      font-size: 13px;
      text-decoration: none;
    }}
    button, a.button {{ cursor: pointer; }}
    button:hover, a.button:hover, select:hover {{ border-color: var(--accent); }}
    main {{ padding: 12px 16px 20px; }}
    .status {{
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 10px;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 8px;
      border: 1px solid var(--line);
      background: var(--panel);
      color: var(--text);
    }}
    .badge.success {{ border-color: color-mix(in srgb, var(--ok), var(--line)); }}
    .badge.failed {{ border-color: color-mix(in srgb, var(--bad), var(--line)); }}
    .badge.warning {{ border-color: color-mix(in srgb, var(--warn), var(--line)); }}
    pre {{
      margin: 0;
      padding: 14px;
      min-height: calc(100vh - 132px);
      border: 1px solid var(--line);
      background: #080b0f;
      color: var(--text);
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      line-height: 1.45;
      font-size: 13px;
    }}
    @media (max-width: 760px) {{
      header {{ grid-template-columns: 1fr; }}
      .toolbar {{ justify-content: flex-start; }}
      select {{ width: 100%; }}
    }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>{title}</h1>
      <div class="sub">读取目录：{escaped_log_dir}</div>
    </div>
    <div class="toolbar">
      <select id="logSelect" aria-label="选择日志"></select>
      <button id="refreshButton" type="button">刷新</button>
      <a id="rawLink" class="button" href="/raw" target="_blank" rel="noreferrer">原文</a>
    </div>
  </header>
  <main>
    <div class="status">
      <span id="stateBadge" class="badge">loading</span>
      <span id="stepText"></span>
      <span id="updatedText"></span>
    </div>
    <pre id="logOutput">loading...</pre>
  </main>
  <script>
    const logSelect = document.getElementById("logSelect");
    const logOutput = document.getElementById("logOutput");
    const stateBadge = document.getElementById("stateBadge");
    const stepText = document.getElementById("stepText");
    const updatedText = document.getElementById("updatedText");
    const rawLink = document.getElementById("rawLink");
    const refreshButton = document.getElementById("refreshButton");

    let currentName = "latest";

    function formatSize(bytes) {{
      if (bytes < 1024) return `${{bytes}} B`;
      if (bytes < 1024 * 1024) return `${{(bytes / 1024).toFixed(1)}} KB`;
      return `${{(bytes / 1024 / 1024).toFixed(1)}} MB`;
    }}

    async function loadLogList() {{
      const response = await fetch(`/api/logs?t=${{Date.now()}}`);
      const data = await response.json();
      const selected = logSelect.value || currentName;
      logSelect.innerHTML = "";

      for (const item of data.logs) {{
        const option = document.createElement("option");
        option.value = item.name;
        option.textContent = `${{item.is_latest ? "latest - " : ""}}${{item.name}} (${{formatSize(item.size)}})`;
        logSelect.appendChild(option);
      }}

      if ([...logSelect.options].some(option => option.value === selected)) {{
        logSelect.value = selected;
        currentName = selected;
      }} else if ([...logSelect.options].some(option => option.value === data.latest)) {{
        logSelect.value = data.latest;
        currentName = data.latest;
      }} else if (logSelect.options.length > 0) {{
        logSelect.value = logSelect.options[0].value;
        currentName = logSelect.value;
      }} else {{
        logSelect.value = "latest";
        currentName = "latest";
      }}
    }}

    async function loadLog() {{
      const response = await fetch(`/api/log?name=${{encodeURIComponent(currentName)}}&t=${{Date.now()}}`);
      const data = await response.json();
      logOutput.textContent = data.content || "";
      stateBadge.textContent = data.state || "unknown";
      stateBadge.className = `badge ${{data.state || ""}}`;
      stepText.textContent = data.current_step || "";
      updatedText.textContent = `更新时间：${{new Date().toLocaleString()}}`;
      rawLink.href = `/raw?name=${{encodeURIComponent(currentName)}}`;
      window.scrollTo(0, document.body.scrollHeight);
    }}

    async function refreshAll() {{
      await loadLogList();
      await loadLog();
    }}

    logSelect.addEventListener("change", async () => {{
      currentName = logSelect.value || "latest";
      await loadLog();
    }});
    refreshButton.addEventListener("click", refreshAll);

    refreshAll();
    setInterval(refreshAll, 5000);
  </script>
</body>
</html>"""


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Serving logs from {LOG_DIR}")
    print(f"Open http://127.0.0.1:{PORT} on this PC")
    server.serve_forever()


if __name__ == "__main__":
    main()
