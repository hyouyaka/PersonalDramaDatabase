"""Serve update logs over the local network.

Run on the update PC, then open http://<server-ip>:8765 from another PC.
The server reads logs/daily-update-latest.log by default and can also show
daily-update-*.log and weekly-cv-update-*.log files from the same logs directory.
"""

from __future__ import annotations

import html
import json
import os
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from danmaku_detail_log import DETAIL_PREFIX


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


def safe_detail_path(name: str) -> Path:
    if Path(name).name != name or not name.startswith(DETAIL_PREFIX) or not name.endswith(".jsonl"):
        raise ValueError("invalid detail log name")
    date.fromisoformat(name[len(DETAIL_PREFIX):-6])
    path = (LOG_DIR / name).resolve()
    if path.parent != LOG_DIR.resolve():
        raise ValueError("invalid detail log path")
    return path


def list_detail_logs() -> list[dict[str, object]]:
    result = []
    for path in LOG_DIR.glob(f"{DETAIL_PREFIX}*.jsonl"):
        try:
            safe_detail_path(path.name)
            result.append({"name": path.name, "size": path.stat().st_size})
        except (ValueError, OSError):
            continue
    return sorted(result, key=lambda item: str(item["name"]), reverse=True)


def query_episode_details(name: str, filters: dict[str, str], offset: int = 0, limit: int = 100) -> dict:
    path = safe_detail_path(name)
    if offset < 0 or not 1 <= limit <= 200:
        raise ValueError("offset must be >= 0 and limit must be between 1 and 200")
    rows = []
    line = b""
    with path.open("rb") as source:
        size = path.stat().st_size
        source.seek(offset)
        while source.tell() < size:
            start = source.tell()
            line = source.readline()
            if not line.endswith(b"\n"):
                # A writer may still be finishing the last record.
                source.seek(start)
                break
            try:
                row = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(row, dict):
                continue
            if any(str(row.get(key, "")).lower() != value.lower() for key, value in filters.items() if value):
                continue
            rows.append(row)
            if len(rows) >= limit:
                break
        cursor = source.tell()
    return {"name": name, "rows": rows, "next_offset": cursor if cursor < size and line.endswith(b"\n") else None}


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

    def download_file(self, path: Path) -> None:
        try:
            source = path.open("rb")
        except FileNotFoundError:
            json_response(self, 404, {"error": "log not found"})
            return
        with source:
            source.seek(0, 2)
            remaining = source.tell()
            source.seek(0)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
            self.send_header("Content-Length", str(remaining))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                while remaining:
                    chunk = source.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path == "/api/detail-logs":
            json_response(self, 200, {"logs": list_detail_logs()})
            return

        if parsed.path in ("/api/episode-details", "/download-details"):
            name = query.get("name", [""])[0]
            try:
                path = safe_detail_path(name)
                if parsed.path == "/download-details":
                    self.download_file(path)
                else:
                    filters = {key: query.get(key, [""])[0] for key in ("platform", "drama_id", "episode_id", "run_id", "complete")}
                    result = query_episode_details(name, filters, int(query.get("offset", ["0"])[0]), int(query.get("limit", ["100"])[0]))
                    json_response(self, 200, result)
            except ValueError as exc:
                json_response(self, 400, {"error": str(exc)})
            except FileNotFoundError:
                json_response(self, 404, {"error": "detail log not found"})
            return

        if parsed.path == "/download":
            try:
                self.download_file(safe_log_path(query.get("name", ["latest"])[0]))
            except ValueError as exc:
                json_response(self, 400, {"error": str(exc)})
            return

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
    select, input, button, a.button {{
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
    details {{ margin-bottom: 16px; padding: 12px; border: 1px solid var(--line); background: var(--panel); }}
    summary {{ cursor: pointer; font-weight: bold; }}
    .filters {{ display: flex; gap: 12px; flex-wrap: wrap; margin: 16px 0; align-items: end; }}
    .filters label {{ display: grid; gap: 6px; font-size: 12px; }}
    .filters input {{ width: 180px; }}
    .detail-table {{ overflow: auto; max-height: 440px; margin: 12px 0; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 12px; }}
    th, td {{ padding: 8px; border: 1px solid var(--line); text-align: left; white-space: nowrap; }}
    th {{ position: sticky; top: 0; background: var(--panel-2); }}
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
      <a id="downloadLogLink" class="button" href="/download">下载完整主日志</a>
    </div>
  </header>
  <main>
    <details>
      <summary>分集弹幕明细</summary>
      <p class="sub">只记录抓取条数；完整表示本次接口/分页完成，不代表平台全部历史弹幕。每页最多 100 条，完整下载包含所选日期的所有记录。</p>
      <form id="detailForm" class="filters">
        <label>日期文件<select id="detailFile" required></select></label>
        <label>平台<select id="detailPlatform"><option value="">全部</option><option value="missevan">猫耳</option><option value="manbo">漫播</option></select></label>
        <label>剧集 ID<input id="detailDrama" placeholder="如 93038"></label>
        <label>分集 ID<input id="detailEpisode"></label>
        <label>运行 ID<input id="detailRun"></label>
        <label>采集是否完整<select id="detailComplete"><option value="">全部</option><option value="true">完整</option><option value="false">不完整</option></select></label>
        <button type="submit">查询</button>
        <a id="detailDownload" class="button" hidden>下载当日完整 JSONL</a>
      </form>
      <div id="detailStatus" class="sub" role="status">尚未查询</div>
      <div class="detail-table"><table>
        <thead><tr><th>时间（UTC）</th><th>运行 ID</th><th>平台</th><th>剧集 ID</th><th>分集 ID</th><th>分集标题</th><th>尝试次数</th><th>抓取条数</th><th>采集是否完整</th></tr></thead>
        <tbody id="detailRows"></tbody>
      </table></div>
      <button id="detailNext" type="button" disabled>下一页</button>
    </details>
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
    const detailFile = document.getElementById("detailFile");
    const detailRows = document.getElementById("detailRows");
    const detailStatus = document.getElementById("detailStatus");
    const detailNext = document.getElementById("detailNext");
    let detailFilters = null;
    let detailOffset = null;
    let detailQuerySerial = 0;

    function updateDetailDownload() {{
      const link = document.getElementById("detailDownload");
      link.hidden = !detailFile.value;
      link.href = `/download-details?name=${{encodeURIComponent(detailFile.value)}}`;
    }}

    async function loadDetailFiles() {{
      const response = await fetch("/api/detail-logs");
      if (!response.ok) throw new Error("无法读取明细文件列表");
      const data = await response.json();
      const selected = detailFile.value;
      detailFile.replaceChildren();
      for (const item of data.logs) {{
        const option = document.createElement("option");
        option.value = item.name;
        option.textContent = `${{item.name}} (${{formatSize(item.size)}})`;
        detailFile.appendChild(option);
      }}
      if ([...detailFile.options].some(option => option.value === selected)) detailFile.value = selected;
      updateDetailDownload();
      if (!data.logs.length) detailStatus.textContent = "暂无明细文件，下次每日采集后生成";
    }}

    async function queryDetails(offset = 0) {{
      const serial = ++detailQuerySerial;
      detailNext.disabled = true;
      detailStatus.textContent = "查询中…";
      try {{
        const params = new URLSearchParams({{...detailFilters, offset: String(offset)}});
        const response = await fetch(`/api/episode-details?${{params}}`);
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "查询失败");
        if (serial !== detailQuerySerial) return;
        detailRows.replaceChildren();
        for (const row of data.rows) {{
          const tr = document.createElement("tr");
          const values = [row.time, row.run_id, row.platform, row.drama_id, row.episode_id,
            row.episode_title, row.attempt, row.fetched_count ?? "未知", row.complete ? "完整" : "不完整"];
          for (const value of values) {{
            const td = document.createElement("td");
            td.textContent = String(value ?? "");
            tr.appendChild(td);
          }}
          detailRows.appendChild(tr);
        }}
        detailOffset = data.next_offset;
        detailNext.disabled = detailOffset == null;
        detailStatus.textContent = `本页 ${{data.rows.length}} 条${{detailOffset == null ? "，已到末尾" : "，还有更多记录"}}`;
      }} catch (error) {{
        if (serial === detailQuerySerial) detailStatus.textContent = error.message;
      }}
    }}

    document.getElementById("detailForm").addEventListener("submit", event => {{
      event.preventDefault();
      if (!detailFile.value) return;
      detailFilters = {{name: detailFile.value, platform: document.getElementById("detailPlatform").value,
        drama_id: document.getElementById("detailDrama").value.trim(),
        episode_id: document.getElementById("detailEpisode").value.trim(),
        run_id: document.getElementById("detailRun").value.trim(),
        complete: document.getElementById("detailComplete").value}};
      queryDetails();
    }});
    detailNext.addEventListener("click", () => {{ if (detailOffset != null) queryDetails(detailOffset); }});
    detailFile.addEventListener("change", () => {{
      ++detailQuerySerial;
      detailNext.disabled = true;
      detailRows.replaceChildren();
      detailStatus.textContent = "尚未查询";
      updateDetailDownload();
    }});

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
      document.getElementById("downloadLogLink").href = `/download?name=${{encodeURIComponent(currentName)}}`;
      if (!document.querySelector("details").open) window.scrollTo(0, document.body.scrollHeight);
    }}

    async function refreshAll() {{
      await loadLogList();
      await loadLog();
    }}

    logSelect.addEventListener("change", async () => {{
      currentName = logSelect.value || "latest";
      await loadLog();
    }});
    refreshButton.addEventListener("click", () => {{ refreshAll(); loadDetailFiles().catch(error => detailStatus.textContent = error.message); }});

    refreshAll();
    loadDetailFiles().catch(error => detailStatus.textContent = error.message);
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
