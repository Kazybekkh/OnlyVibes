#!/usr/bin/env python3
"""
OnlyVibes hub — orchestra server (protocol v2, see shared/PROTOCOL.md).

A human types a product prompt on the board -> a PLAN task is dispatched to a worker,
whose agent decomposes it into build tasks -> worker daemons on every laptop claim tasks,
stream transcripts/progress here, and agents talk to each other through /api/agent/*.

Layout (all in server/):
    main.py       FastAPI app: routes, WebSockets, static files, entrypoint
    hub.py        all state transitions (single asyncio lock) + background loops
    scheduler.py  task -> worker assignment policy (pure)
    planning.py   plan validation + ref -> id mapping (pure)
    store.py      in-memory state + atomic JSON persistence
    peers.py      non-blocking per-socket senders
    legacy.py     v1 passive monitor (reporter ingest, Mistral summarize)
    config.py     Settings from env
    tests/        `uv run pytest -q`

Endpoints:
  Orchestra
    WS   /worker                       worker daemons (hello, heartbeat, task_*, plan_result
                                       -> welcome, run_task, cancel_task, chat)
    WS   /board                        browsers: snapshot, then project/task/event/message/worker
                                       frames, ping every 15 s
    GET  /api/state                    {projects, tasks, workers, messages, laptops}
    GET  /api/projects                 list of projects
    POST /api/projects                 {prompt, repo?, base_branch?} -> Project (+ PLAN task)
    GET  /api/projects/{id}            {project, tasks}
    POST /api/projects/{id}/tasks      human-added task
    GET  /api/tasks/{id}               {task, events}
    POST /api/tasks/{id}/retry|cancel|done
    POST /api/messages                 {project_id, to?, text, name?}
    GET  /api/workers
    /api/agent/say|inbox|progress|board|plan   for the `ov` CLI (Bearer <run_task.token>)
  Legacy (unchanged behaviour)
    WS   /ws         <- reporters stream laptop status messages (shared/schema.json)
    WS   /view       -> 3D viewers get all laptop states 2x/s; besides reporter laptops it now also
                        lists one synthesized laptop per connected worker whose "agents" are the
                        tasks it runs (merged into the reporter entry with the same tailscale_ip)
    POST /summarize  -> {"last_log": "..."} -> {"summary": "..."} via Mistral Small
    GET  /api/layout -> real-table AR layout {ref, laptops: {key: {x,y,z,yaw,owner}}} (also in /view)
    POST /api/layout -> replace it (control key required), saved to data/layout.json
    POST /api/xrpose -> latest headset + controller pose from an XR session (control key, in memory)
    GET  /api/xrpose -> that pose (control key); {pose, age_ms} so a laptop can spectate
  Static
    GET  /           -> web/index.html (kanban board)
    GET  /ar         -> web/ar.html (Three.js / WebXR view, with voice)
    GET  /spectate   -> same page in laptop spectator mode (3D scene + headset/controllers)
    GET  /mic        -> web/mic.html (fallback voice mic page, role=web)
    GET  /probe      -> web/probe.html (Zapbox capability probe)
  Voice (server/voice.py): push-to-talk speech-to-text only
    POST /api/voice/token -> short-lived Voxtral STT token (control key required), POST /api/voice/log,
    GET /api/voice/health. The transcript goes to POST /api/control action=redirect.
    GET  /<file>     -> anything else in web/ (board.js, board.css, ...)

Errors are JSON: {"error": {"code": "...", "message": "..."}} with a 4xx status.
State is persisted (debounced, atomic) to server/data/state.json + server/data/events/*.json and
reloaded at startup; tasks that were claimed/in_progress (or requeued by the shutdown's
disconnects) go back to todo and wait ~30 s for their worker to re-hello and re-attach them.

Env vars:
    PORT              listen port when run as `uv run main.py` (default 8000)
    HOST              bind address for `uv run main.py` (default 0.0.0.0)
    PUBLIC_URL        base URL agents use to reach the hub (run_task.server_http), e.g.
                      http://100.107.184.10:8000 — default: derived from the Host header the
                      worker used for WS /worker
    OV_SHARED_SECRET  if set, WS /worker requires ?secret=<value> (workers: append it to SERVER_URL)
    OV_DATA_DIR       where state is persisted (default server/data)
    OV_WEB_DIR        static files dir (default ../web)
    MISTRAL_API_KEY   enables summarize() through the Mistral API; without it the
                      stub falls back to a truncated raw log line
    MISTRAL_MODEL     model used by summarize() (default mistral-small-latest)
    STALE_AFTER       seconds without updates before a reporter laptop is dropped (default 5)

Run:
    cd server
    uv run uvicorn main:app --host 0.0.0.0 --port 8000
    # or: uv run main.py
    # tests: uv run pytest -q
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from config import Settings
from hub import Hub, WorkerConn
from legacy import summarize
from models import ApiError, now
from peers import Peer, serve_peer
from voice import make_router

INBOX_MAX_WAIT = 60.0


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


async def _json_body(request: Request) -> dict:
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ApiError(400, "bad_json", "request body must be valid JSON") from None
    if not isinstance(data, dict):
        raise ApiError(400, "bad_json", "request body must be a JSON object")
    return data


def _http_base(ws: WebSocket, public_url: str) -> str:
    """Base URL agents on the worker's laptop should use for `ov` (run_task.server_http)."""
    if public_url:
        return public_url
    proto = ws.headers.get("x-forwarded-proto") or ("https" if ws.url.scheme in ("wss", "https") else "http")
    host = ws.headers.get("x-forwarded-host") or ws.headers.get("host")
    if not host:
        host = f"{ws.url.hostname}:{ws.url.port}" if ws.url.port else (ws.url.hostname or "localhost")
    return f"{proto.split(',')[0].strip()}://{host.split(',')[0].strip()}"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    hub = Hub(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await hub.start()
        try:
            yield
        finally:
            await hub.stop()

    app = FastAPI(title="OnlyVibes hub", version="2", lifespan=lifespan)
    app.state.hub = hub
    app.state.settings = settings

    def require_control_key(request: Request) -> None:
        """When OV_CONTROL_SECRET is set, control endpoints require ?key=<secret>
        or the X-OV-Key header; otherwise 403. Viewing endpoints stay open."""
        secret = settings.control_secret
        if not secret:
            return
        key = request.query_params.get("key") or request.headers.get("x-ov-key") or ""
        if key != secret:
            raise StarletteHTTPException(
                status_code=403,
                detail="control locked: pass ?key=<OV_CONTROL_SECRET> or header X-OV-Key",
            )

    # ------------------------------------------------------------------ errors
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return _error(exc.status, exc.code, exc.message)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return _error(exc.status_code, code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(400, "bad_request", str(exc.errors()))

    # ------------------------------------------------------------ human / board
    @app.get("/api/state")
    async def api_state() -> dict:
        async with hub.lock:
            return {"ts": now(), **hub.state(), "laptops": hub.legacy_laptops(now())}

    @app.get("/api/projects")
    async def list_projects() -> list:
        async with hub.lock:
            return hub.state()["projects"]

    @app.post("/api/projects")
    async def create_project(request: Request) -> dict:
        body = await _json_body(request)
        async with hub.lock:
            return hub.human_create_project(body)

    @app.get("/api/projects/{project_id}")
    async def get_project(project_id: str) -> dict:
        async with hub.lock:
            return hub.project_view(project_id)

    @app.post("/api/projects/{project_id}/tasks")
    async def add_task(project_id: str, request: Request) -> dict:
        body = await _json_body(request)
        async with hub.lock:
            return hub.human_add_task(project_id, body)

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str) -> dict:
        async with hub.lock:
            return hub.task_view(task_id)

    @app.post("/api/tasks/{task_id}/retry", dependencies=[Depends(require_control_key)])
    async def retry_task(task_id: str) -> dict:
        async with hub.lock:
            return hub.human_retry(task_id)

    @app.post("/api/tasks/{task_id}/cancel", dependencies=[Depends(require_control_key)])
    async def cancel_task(task_id: str) -> dict:
        async with hub.lock:
            return hub.human_cancel(task_id)

    @app.post("/api/tasks/{task_id}/done")
    async def done_task(task_id: str) -> dict:
        async with hub.lock:
            return hub.human_done(task_id)

    @app.post("/api/messages")
    async def post_message(request: Request) -> dict:
        body = await _json_body(request)
        async with hub.lock:
            return hub.human_message(body)

    @app.get("/api/workers")
    async def list_workers() -> list:
        async with hub.lock:
            return hub.state()["workers"]

    # ------------------------------------------------------------- agent API
    @app.post("/api/agent/say")
    async def agent_say(request: Request) -> dict:
        body = await _json_body(request)
        async with hub.lock:
            task = hub.agent_auth(request.headers.get("authorization"))
            return hub.agent_say(task, body)

    @app.get("/api/agent/inbox")
    async def agent_inbox(request: Request, since: str = "", wait: float = 0.0) -> dict:
        deadline = time.monotonic() + max(0.0, min(float(wait), INBOX_MAX_WAIT))
        while True:
            async with hub.lock:
                task = hub.agent_auth(request.headers.get("authorization"))
                messages, cursor, new_message = hub.agent_inbox(task, since)
            remaining = deadline - time.monotonic()
            if messages or remaining <= 0:
                return {"messages": messages, "cursor": cursor}
            try:
                await asyncio.wait_for(new_message.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                pass

    @app.post("/api/agent/progress")
    async def agent_progress(request: Request) -> dict:
        body = await _json_body(request)
        async with hub.lock:
            task = hub.agent_auth(request.headers.get("authorization"))
            return hub.agent_progress(task, body)

    @app.get("/api/agent/board")
    async def agent_board(request: Request) -> dict:
        async with hub.lock:
            task = hub.agent_auth(request.headers.get("authorization"))
            return hub.agent_board(task)

    @app.post("/api/agent/plan")
    async def agent_plan(request: Request) -> dict:
        body = await _json_body(request)
        async with hub.lock:
            task = hub.agent_auth(request.headers.get("authorization"))
            return hub.agent_plan(task, body)

    # ------------------------------------------------------------ WS /worker
    @app.websocket("/worker")
    async def ws_worker(websocket: WebSocket) -> None:
        if settings.shared_secret and not secrets.compare_digest(
                websocket.query_params.get("secret", ""), settings.shared_secret):
            print(f"[server] rejected worker {websocket.client}: bad or missing ?secret=")
            await websocket.close(code=4401)
            return
        await websocket.accept()
        peer = Peer(websocket, f"worker@{websocket.client}", max_queue=20000)
        http_base = _http_base(websocket, settings.public_url)
        conn: WorkerConn | None = None

        async def on_text(text: str) -> None:
            nonlocal conn
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                return
            if not isinstance(msg, dict):
                return
            try:
                async with hub.lock:
                    if msg.get("type") == "hello":
                        if conn is None or hub.conns.get(conn.worker_id) is not conn:
                            conn = hub.worker_hello(peer, msg, http_base, now())
                    elif conn is None:
                        hub.warn_once(f"prehello:{msg.get('type')}", f"ignoring {msg.get('type')!r} before hello")
                    else:
                        hub.worker_message(conn, msg, now())
            except ApiError as exc:
                peer.send({"type": "error", "code": exc.code, "message": exc.message})
                peer.close(4400)
            except Exception:  # a bug must not kill the connection
                traceback.print_exc()

        try:
            await serve_peer(peer, on_text)
        finally:
            if conn is not None:
                async with hub.lock:
                    hub.worker_disconnected(conn, now())

    # ------------------------------------------------------------- WS /board
    @app.websocket("/board")
    async def ws_board(websocket: WebSocket) -> None:
        await websocket.accept()
        peer = Peer(websocket, f"board@{websocket.client}")
        async with hub.lock:
            hub.add_board_peer(peer)
        try:
            await serve_peer(peer)
        finally:
            async with hub.lock:
                hub.board_peers.discard(peer)

    # ---------------------------------------------------------------- legacy
    # summary cache + rate limit for the legacy /ws path (ported from kazy/ar-fixes):
    # (laptop_key, agent_name) -> (last_log, summary); at most 1 Mistral call per
    # agent per SUMMARY_RATE_LIMIT_S seconds, and only when last_log changed
    summary_cache: dict[tuple[str, str], tuple[str, str]] = {}
    summary_rate: dict[tuple[str, str], float] = {}
    SUMMARY_RATE_LIMIT_S = 5.0

    async def get_summary(laptop_key: str, agent: dict) -> str:
        name = agent.get("name", "")
        log = (agent.get("last_log") or "").strip()
        key = (laptop_key, name)
        cached = summary_cache.get(key)
        if cached and cached[0] == log:
            return cached[1]
        if time.time() - summary_rate.get(key, 0.0) < SUMMARY_RATE_LIMIT_S:
            return cached[1] if cached else ""
        summary_rate[key] = time.time()
        s = await summarize(log, settings.mistral_api_key, settings.mistral_model)
        summary_cache[key] = (log, s)
        return s

    # laptop key (tailscale_ip, else hostname) -> that laptop's reporter socket, so the AR view
    # can send it whitelisted control requests (POST /api/control); request id -> answer future
    reporter_socks: dict[str, WebSocket] = {}
    control_waiters: dict[str, asyncio.Future] = {}

    @app.websocket("/ws")
    async def ws_reporter(websocket: WebSocket) -> None:
        """Receive status messages from reporters (schema: shared/schema.json)."""
        await websocket.accept()
        print(f"[server] reporter connected: {websocket.client}")
        my_keys: set[str] = set()
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue
                if data.get("type") == "control_result":
                    fut = control_waiters.get(str(data.get("id") or ""))
                    if fut is not None and not fut.done():
                        fut.set_result({"ok": bool(data.get("ok")), "message": str(data.get("message") or "")[:300]})
                    continue
                data.setdefault("ts", time.time())
                ikey = (data.get("tailscale_ip") or "").strip()
                if not ikey or ikey == "127.0.0.1":
                    ikey = data.get("laptop") or "unknown"
                for k in (ikey, str(data.get("laptop") or "")):
                    if k:
                        reporter_socks[k] = websocket
                        my_keys.add(k)
                # fill in summaries for agents that arrived without one
                todo = [a for a in data.get("agents", []) if not a.get("summary") and a.get("last_log")]
                if todo:
                    sums = await asyncio.gather(*(get_summary(ikey, a) for a in todo))
                    for a, s in zip(todo, sums):
                        a["summary"] = s
                async with hub.lock:
                    hub.legacy.ingest(data)
        except WebSocketDisconnect:
            print(f"[server] reporter disconnected: {websocket.client}")
        finally:
            for k in my_keys:
                if reporter_socks.get(k) is websocket:
                    del reporter_socks[k]

    @app.post("/api/control", dependencies=[Depends(require_control_key)])
    async def api_control(request: Request):
        """AR view -> stop / resume / say for one agent card.

        Orchestra task cards (task_id): stop = cancel, resume = retry, say = message to that
        task's agent inbox. Reporter cards: forwarded to that laptop's reporter, which resolves
        the card name locally and runs only its fixed whitelist.
        """
        body = await _json_body(request)
        action = str(body.get("action") or "")
        if action not in ("stop", "resume", "say", "focus", "redirect"):
            return _error(400, "bad_request", "action must be stop, resume, say, focus or redirect")
        text = " ".join(str(body.get("text") or "").split())[:500]
        # redirect (voice): interrupt the agent and give it `text` as its new prompt
        if action == "redirect" and not text:
            return _error(400, "bad_request", "text is required")
        task_id = str(body.get("task_id") or "")
        if task_id and action == "focus":
            return {"ok": False, "message": "orchestra tasks run headless on the worker: no window to open"}
        if task_id:
            async with hub.lock:
                task = hub.task_view(task_id)["task"]
                if action == "stop":
                    hub.human_cancel(task_id)
                    return {"ok": True, "message": f"cancelled {task['key']}"}
                if action == "resume":
                    hub.human_retry(task_id)
                    return {"ok": True, "message": f"requeued {task['key']}"}
                if not text:
                    return _error(400, "bad_request", "text is required")
                # say / redirect: orchestra agents read their inbox between steps
                hub.human_message({"project_id": task["project_id"], "to": task_id, "text": text, "name": "AR"})
                return {"ok": True, "message": f"sent to {task['key']}"}
        laptop, agent = str(body.get("laptop") or ""), str(body.get("agent") or "")
        sock = reporter_socks.get(laptop)
        if sock is None or not agent:
            return _error(404, "not_found", "that laptop's reporter is not connected")
        rid = secrets.token_hex(6)
        fut = asyncio.get_running_loop().create_future()
        control_waiters[rid] = fut
        try:
            await sock.send_text(json.dumps({"type": "control", "id": rid, "agent": agent,
                                             "action": action, "text": text}))
            # redirect waits for the old process to exit before resuming it
            return await asyncio.wait_for(fut, timeout=25.0 if action == "redirect" else 5.0)
        except asyncio.TimeoutError:
            return _error(504, "timeout", "no answer from that laptop: its reporter may be an older "
                                          "version (pull and restart it)")
        except Exception:
            return _error(502, "unreachable", "could not reach that laptop's reporter")
        finally:
            control_waiters.pop(rid, None)

    # ------------------------------------------------------------ AR table layout
    # Real-table placement of the virtual laptops (web/ar.html "Place laptops"). Positions
    # are metres in a frame anchored on the reference laptops: ref[0] is the origin, and the
    # direction ref[0] -> ref[1] fixes the rotation, so a new WebXR session only has to
    # re-place those two laptops and every other one snaps into place.
    layout_path = settings.data_dir / "layout.json"
    layout_state: dict = {"ref": [], "laptops": {}}
    try:
        layout_state.update(json.loads(layout_path.read_text()))
    except (OSError, ValueError):
        pass

    def _clean_layout(body: dict) -> dict:
        import math
        laptops_in = body.get("laptops")
        if not isinstance(laptops_in, dict) or len(laptops_in) > 64:
            raise ApiError(400, "bad_request", "laptops must be an object with at most 64 entries")
        laptops: dict[str, dict] = {}
        for key, v in laptops_in.items():
            if not isinstance(key, str) or not key or len(key) > 120 or not isinstance(v, dict):
                raise ApiError(400, "bad_request", "each laptop needs a key and an {x,y,z,yaw} object")
            entry = {}
            for f in ("x", "y", "z", "yaw"):
                n = v.get(f, 0.0)
                if isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) or abs(n) > 1000:
                    raise ApiError(400, "bad_request", f"{key}.{f} must be a finite number")
                entry[f] = round(float(n), 4)
            entry["owner"] = str(v.get("owner") or "")[:80]
            laptops[key] = entry
        ref = [str(k) for k in (body.get("ref") or []) if str(k) in laptops][:2]
        if laptops and not ref:
            ref = [sorted(laptops)[0]]
        return {"ref": ref, "laptops": laptops, "updated_at": now()}

    @app.get("/api/layout")
    async def get_layout() -> dict:
        return layout_state

    @app.post("/api/layout", dependencies=[Depends(require_control_key)])
    async def post_layout(request: Request) -> dict:
        clean = _clean_layout(await _json_body(request))
        layout_state.clear()
        layout_state.update(clean)
        layout_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = layout_path.with_name("layout.json.tmp")
        tmp.write_text(json.dumps(clean, indent=2))
        os.replace(tmp, layout_path)
        return layout_state

    # ------------------------------------------------------------ XR spectator pose
    # Latest headset + controller pose from web/ar.html, kept in memory only. A laptop
    # browser at /spectate polls this ~20 Hz and draws the same 3D scene for the audience.
    xr_pose: dict | None = None
    xr_pose_at: float = 0.0

    def _finite_vec(v, n: int, *, lo: float, hi: float, name: str) -> list[float]:
        import math
        if not isinstance(v, (list, tuple)) or len(v) != n:
            raise ApiError(400, "bad_request", f"{name} must be a list of {n} numbers")
        out: list[float] = []
        for i, x in enumerate(v):
            if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x < lo or x > hi:
                raise ApiError(400, "bad_request", f"{name}[{i}] must be a finite number in range")
            out.append(round(float(x), 6))
        return out

    def _quat(v, name: str) -> list[float]:
        import math
        q = _finite_vec(v, 4, lo=-2, hi=2, name=name)
        norm = math.sqrt(sum(x * x for x in q)) or 1.0
        return [round(x / norm, 6) for x in q]

    def _clean_xrpose(body: dict) -> dict:
        import math
        if not isinstance(body, dict):
            raise ApiError(400, "bad_request", "body must be a JSON object")
        head = body.get("head")
        if not isinstance(head, dict):
            raise ApiError(400, "bad_request", "head is required")
        clean: dict = {
            "mode": str(body.get("mode") or "")[:8],
            "head": {
                "pos": _finite_vec(head.get("pos"), 3, lo=-100, hi=100, name="head.pos"),
                "quat": _quat(head.get("quat"), "head.quat"),
            },
            "controllers": [],
            "world": None,
            "selected": None,
        }
        ctrls = body.get("controllers") or []
        if not isinstance(ctrls, list) or len(ctrls) > 4:
            raise ApiError(400, "bad_request", "controllers must be a short list")
        for i, c in enumerate(ctrls):
            if not isinstance(c, dict):
                raise ApiError(400, "bad_request", f"controllers[{i}] must be an object")
            hand = str(c.get("hand") or "")[:8]
            if not c.get("connected", True):
                clean["controllers"].append({"connected": False, "hand": hand})
                continue
            ray = c.get("ray", 0.5)
            if isinstance(ray, bool) or not isinstance(ray, (int, float)) or not math.isfinite(ray) or ray < 0 or ray > 20:
                raise ApiError(400, "bad_request", f"controllers[{i}].ray must be a finite length")
            clean["controllers"].append({
                "connected": True,
                "hand": hand,
                "pos": _finite_vec(c.get("pos"), 3, lo=-100, hi=100, name=f"controllers[{i}].pos"),
                "quat": _quat(c.get("quat"), f"controllers[{i}].quat"),
                "trigger": bool(c.get("trigger")),
                "squeeze": bool(c.get("squeeze")),
                "ray": round(float(ray), 4),
            })
        world = body.get("world")
        if world is not None:
            if not isinstance(world, dict):
                raise ApiError(400, "bad_request", "world must be an object")
            sc = world.get("scale", 1)
            if isinstance(sc, bool) or not isinstance(sc, (int, float)) or not math.isfinite(sc) or sc <= 0 or sc > 20:
                raise ApiError(400, "bad_request", "world.scale must be a positive finite number")
            clean["world"] = {
                "pos": _finite_vec(world.get("pos"), 3, lo=-100, hi=100, name="world.pos"),
                "quat": _quat(world.get("quat"), "world.quat"),
                "scale": round(float(sc), 6),
            }
        def _card_ref(v, name: str):
            if v is None:
                return None
            if not isinstance(v, dict):
                raise ApiError(400, "bad_request", f"{name} must be an object or null")
            ak = str(v.get("ak") or "")[:120]
            laptop = str(v.get("laptop") or v.get("laptopKey") or "")[:120]
            return {"ak": ak, "laptop": laptop} if (ak or laptop) else None

        clean["selected"] = _card_ref(body.get("selected"), "selected")
        clean["hover"] = _card_ref(body.get("hover"), "hover")
        clean["panel"] = bool(body.get("panel"))
        clean["grabbed"] = bool(body.get("grabbed"))
        clean["ptt"] = bool(body.get("ptt"))
        voice = body.get("voice")
        vtext = ""
        if isinstance(voice, dict):
            vtext = str(voice.get("text") or "")[:200]
        elif isinstance(voice, str):
            vtext = voice[:200]
        clean["voice"] = {"text": vtext} if vtext else None
        return clean

    def _xrpose_payload() -> dict:
        if xr_pose is None:
            return {"pose": None, "age_ms": None}
        return {"pose": xr_pose, "age_ms": int(max(0.0, (now() - xr_pose_at) * 1000))}

    @app.get("/api/xrpose", dependencies=[Depends(require_control_key)])
    async def get_xrpose() -> dict:
        return _xrpose_payload()

    @app.post("/api/xrpose", dependencies=[Depends(require_control_key)])
    async def post_xrpose(request: Request) -> dict:
        nonlocal xr_pose, xr_pose_at
        xr_pose = _clean_xrpose(await _json_body(request))
        xr_pose_at = now()
        return _xrpose_payload()

    # ---------------------------------------------------------------- voice (Ron's voice-integration)
    app.include_router(make_router(settings, require_control_key))

    @app.websocket("/view")
    async def ws_view(websocket: WebSocket) -> None:
        """Broadcast the full laptop-state snapshot to every viewer, 2x per second."""
        await websocket.accept()
        print(f"[server] viewer connected: {websocket.client}")
        try:
            while True:
                async with hub.lock:
                    t = now()
                    msg = json.dumps({"type": "state", "ts": t, "laptops": hub.legacy_laptops(t),
                                      "layout": layout_state})
                await websocket.send_text(msg)
                await asyncio.sleep(0.5)
        except (WebSocketDisconnect, RuntimeError):
            print(f"[server] viewer disconnected: {websocket.client}")

    @app.post("/summarize")
    async def api_summarize(request: Request) -> dict:
        body = await _json_body(request)
        return {"summary": await summarize(str(body.get("last_log", "")), settings.mistral_api_key,
                                           settings.mistral_model)}

    # ---------------------------------------------------------------- static
    web_dir: Path = settings.web_dir

    def _page(name: str):
        f = web_dir / name
        if f.is_file():
            return FileResponse(f)
        return _error(404, "not_found", f"web/{name} not found yet — parallel work in progress")

    @app.get("/", include_in_schema=False)
    async def index():
        return _page("index.html")

    @app.get("/ar", include_in_schema=False)
    async def ar():
        return _page("ar.html")

    @app.get("/spectate", include_in_schema=False)
    async def spectate():
        return _page("ar.html")  # same page; JS turns on spectator mode at /spectate

    @app.get("/mic", include_in_schema=False)
    async def mic_page():
        return _page("mic.html")  # fallback mic (role=web), e.g. the laptop browser

    @app.get("/probe", include_in_schema=False)
    async def probe_page():
        return _page("probe.html")  # Zapbox capability probe

    # LAST: everything else under / resolves to web/ (board.js, board.css, ar.html, ...)
    if web_dir.is_dir():
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return app


app = create_app()


def serve() -> None:
    import uvicorn

    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    serve()
