# coding=utf-8
"""
Standalone MemBlock LSQ Web UI server.

Serves:
  - static web UI files from `webui/`
  - independent WebSocket endpoints per dashboard section
  - a control WebSocket for start/pause/reset/step commands
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event as ThreadEvent
from threading import Lock as ThreadLock
from threading import Thread
from typing import Any

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
except ImportError:
    websockets = None

    class ConnectionClosed(Exception):
        pass

from MemBlock_api import create_dut
from MemBlock_env import MemBlockEnv
from lsq_webui_backend import LsqStateTracker, SignalReader, TopicPublisher


_HERE = Path(__file__).resolve().parent
_WEBUI_ROOT = _HERE / "webui"
_TOPICS = {"overview", "vlq", "sq", "replay", "violations", "perf", "events"}

_TESTS_PATH = _HERE / "tests"
if str(_TESTS_PATH) not in sys.path:
    sys.path.insert(0, str(_TESTS_PATH))

from test_MemBlock_random_load import test_api_MemBlock_random_1000_load_requests


class ScenarioStopped(RuntimeError):
    """Raised when the running scenario is cancelled or reset."""


class ScenarioEnvProxy:
    """Proxy env used to reuse the existing random-load test case directly."""

    def __init__(self, service, env, generation: int) -> None:
        self._service = service
        self._env = env
        self._generation = generation

    def __getattr__(self, name: str):
        return getattr(self._env, name)

    def Step(self, cycles: int = 1) -> None:
        for _ in range(max(0, cycles)):
            continuous_run = False
            while True:
                if self._service._driver_stop_event.is_set():
                    raise ScenarioStopped("scenario stopped")
                if self._service._driver_run_event.is_set():
                    continuous_run = True
                    break
                if self._service._consume_driver_step_budget():
                    break
                self._service._driver_run_event.wait(0.05)
            self._env.Step(1)
            self._service._loop.call_soon_threadsafe(self._service._record_driver_step, self._generation)
            if continuous_run and self._service.tick_ms > 0:
                if self._service._driver_stop_event.wait(self._service.tick_ms / 1000.0):
                    raise ScenarioStopped("scenario stopped")

    def reset(self, cycles: int = 10, settle_cycles: int = 5) -> None:
        if self._service._driver_stop_event.is_set():
            raise ScenarioStopped("scenario stopped")
        self._env.reset(cycles=cycles, settle_cycles=settle_cycles)

    def idle_inputs(self) -> None:
        self._env.idle_inputs()


class StaticServer:
    """Background HTTP server for static assets."""

    def __init__(self, host: str, port: int, root: Path) -> None:
        self.host = host
        self.port = port
        self.root = root
        self._thread: Thread | None = None
        self._httpd: ThreadingHTTPServer | None = None

    def start(self) -> None:
        handler = partial(SimpleHTTPRequestHandler, directory=str(self.root))
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self._thread = Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=1.0)


class MemBlockWebUIService:
    """Own DUT lifetime, sampling loop, and WebSocket topic fanout."""

    def __init__(
        self,
        host: str,
        ws_port: int,
        http_port: int,
        step_cycles: int,
        reset_cycles: int,
        tick_ms: int,
    ) -> None:
        self.host = host
        self.ws_port = ws_port
        self.http_port = http_port
        self.step_cycles = max(1, step_cycles)
        self.reset_cycles = max(1, reset_cycles)
        self.tick_ms = max(1, tick_ms)
        self.static_server = StaticServer(host, http_port, _WEBUI_ROOT)
        self.topic_clients: dict[str, set[Any]] = defaultdict(set)
        self.control_clients: set[Any] = set()
        self.publisher = TopicPublisher()
        self.dut = create_dut(None)
        self.env = MemBlockEnv(self.dut)
        self.reader = SignalReader(self.dut)
        self.tracker = LsqStateTracker()
        self.running = False
        self.cycle = 0
        self.last_raw: dict[str, Any] | None = None
        self._command_lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._driver_task: asyncio.Task | None = None
        self._driver_run_event = ThreadEvent()
        self._driver_stop_event = ThreadEvent()
        self._driver_budget_lock = ThreadLock()
        self._driver_step_budget = 0
        self._driver_flush_task: asyncio.Task | None = None
        self._driver_sample_dirty = False
        self._scenario_started = False
        self._scenario_done = False
        self._scenario_error: str | None = None
        self._scenario_generation = 0

    def service_state(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "step_cycles": self.step_cycles,
            "tick_ms": self.tick_ms,
            "last_raw": self.last_raw,
            "scenario_started": self._scenario_started,
            "scenario_done": self._scenario_done,
            "scenario_error": self._scenario_error,
        }

    async def initialize(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.static_server.start()
        await self.reset()

    async def shutdown(self) -> None:
        self._scenario_generation += 1
        await self._stop_scenario()
        if self._driver_flush_task is not None:
            try:
                await self._driver_flush_task
            except Exception:
                pass
        self.static_server.stop()
        self.env.clear_callbacks()
        self.env.Finish()

    def _clear_driver_step_budget(self) -> None:
        with self._driver_budget_lock:
            self._driver_step_budget = 0

    def _grant_driver_step_budget(self, cycles: int) -> None:
        with self._driver_budget_lock:
            self._driver_step_budget += max(0, cycles)

    def _consume_driver_step_budget(self) -> bool:
        with self._driver_budget_lock:
            if self._driver_step_budget <= 0:
                return False
            self._driver_step_budget -= 1
            return True

    async def _ensure_scenario_started_locked(self) -> None:
        if self._driver_task is not None:
            if self._driver_task.done():
                try:
                    await self._driver_task
                except Exception:
                    pass
                self._driver_task = None
            else:
                return
        self._scenario_started = True
        self._scenario_done = False
        self._scenario_error = None
        self._driver_stop_event.clear()
        self._clear_driver_step_budget()
        self._driver_task = asyncio.create_task(self._run_random_load_scenario(self._scenario_generation))

    async def _stop_scenario(self) -> None:
        driver_task = None
        async with self._command_lock:
            self.running = False
            self._driver_stop_event.set()
            self._driver_run_event.set()
            self._clear_driver_step_budget()
            driver_task = self._driver_task
            self._driver_task = None
        if driver_task is not None:
            try:
                await driver_task
            except Exception:
                pass

    def _record_driver_step(self, generation: int) -> None:
        if generation != self._scenario_generation:
            return
        self.cycle += 1
        self.last_raw = self.reader.read(self.cycle)
        self.tracker.update(self.last_raw)
        self._driver_sample_dirty = True
        if self._driver_flush_task is None or self._driver_flush_task.done():
            self._driver_flush_task = asyncio.create_task(self._flush_driver_updates(generation))

    async def _flush_driver_updates(self, generation: int) -> None:
        while generation == self._scenario_generation:
            self._driver_sample_dirty = False
            await self._broadcast_all(force=False)
            await self._broadcast_control_state()
            await asyncio.sleep(0)
            if not self._driver_sample_dirty:
                break

    def _run_random_load_scenario_sync(self, generation: int) -> None:
        if self._loop is None:
            raise RuntimeError("event loop is not initialized")
        proxy = ScenarioEnvProxy(self, self.env, generation)
        test_api_MemBlock_random_1000_load_requests(proxy)

    async def _run_random_load_scenario(self, generation: int) -> None:
        error = None
        stopped = False
        try:
            await asyncio.to_thread(self._run_random_load_scenario_sync, generation)
        except ScenarioStopped:
            stopped = True
        except Exception as exc:
            error = str(exc)
        await self._finish_scenario(generation, stopped=stopped, error=error)

    async def _finish_scenario(
        self,
        generation: int,
        *,
        stopped: bool,
        error: str | None,
    ) -> None:
        if generation != self._scenario_generation:
            return
        async with self._command_lock:
            if generation != self._scenario_generation:
                return
            self._driver_task = None
            self._driver_run_event.clear()
            self._driver_stop_event.clear()
            self._clear_driver_step_budget()
            self.running = False
            if error is not None:
                self._scenario_error = error
                self._scenario_done = False
            elif stopped:
                self._scenario_done = False
            else:
                self._scenario_done = True
        await self._broadcast_control_state()

    async def reset(self) -> None:
        driver_task = None
        async with self._command_lock:
            self.running = False
            self._scenario_generation += 1
            self._driver_stop_event.set()
            self._driver_run_event.set()
            self._clear_driver_step_budget()
            driver_task = self._driver_task
            self._driver_task = None
        if driver_task is not None:
            try:
                await driver_task
            except Exception:
                pass
        async with self._command_lock:
            self._driver_run_event.clear()
            self._driver_stop_event.clear()
            self._clear_driver_step_budget()
            self._scenario_started = False
            self._scenario_done = False
            self._scenario_error = None
            self.cycle = 0
            self.env.reset(cycles=self.reset_cycles, settle_cycles=1)
            self.tracker.reset()
            self.last_raw = self.reader.read(self.cycle)
            self.tracker.update(self.last_raw)
        await self._broadcast_all(force=True)
        await self._broadcast_control_state()

    async def step_once(self, cycles: int | None = None) -> None:
        async with self._command_lock:
            actual_cycles = self.step_cycles if cycles is None else max(1, cycles)
            if self._scenario_done:
                self.running = False
            else:
                await self._ensure_scenario_started_locked()
                self.running = False
                self._driver_run_event.clear()
                self._grant_driver_step_budget(actual_cycles)
        await self._broadcast_control_state()

    async def play(self) -> None:
        async with self._command_lock:
            if self._scenario_done:
                self.running = False
            else:
                await self._ensure_scenario_started_locked()
                self.running = True
                self._driver_run_event.set()
        await self._broadcast_control_state()

    async def pause(self) -> None:
        async with self._command_lock:
            self.running = False
            self._driver_run_event.clear()
        await self._broadcast_control_state()

    async def _send_json(self, websocket, payload: dict[str, Any]) -> None:
        await websocket.send(json.dumps(payload, ensure_ascii=False))

    async def _broadcast_control_state(self, extra: dict[str, Any] | None = None) -> None:
        if not self.control_clients:
            return
        payload = {
            "type": "state",
            "cycle": self.cycle,
            "running": self.running,
            "step_cycles": self.step_cycles,
            "tick_ms": self.tick_ms,
            "http_port": self.http_port,
            "ws_port": self.ws_port,
            "scenario_started": self._scenario_started,
            "scenario_done": self._scenario_done,
            "scenario_error": self._scenario_error,
        }
        if self._scenario_error is not None:
            payload["error"] = self._scenario_error
        if extra:
            payload.update(extra)
        stale = []
        for websocket in self.control_clients:
            try:
                await self._send_json(websocket, payload)
            except ConnectionClosed:
                stale.append(websocket)
        for websocket in stale:
            self.control_clients.discard(websocket)

    async def _broadcast_topic(self, topic: str, payload: dict[str, Any]) -> None:
        if topic not in self.topic_clients or not self.topic_clients[topic]:
            return
        stale = []
        for websocket in self.topic_clients[topic]:
            try:
                await self._send_json(websocket, payload)
            except ConnectionClosed:
                stale.append(websocket)
        for websocket in stale:
            self.topic_clients[topic].discard(websocket)

    async def _broadcast_all(self, force: bool) -> None:
        for topic in _TOPICS:
            payload = self.tracker.topic_payload(topic, self.service_state())
            if force:
                self.publisher.snapshot(topic, payload)
                await self._broadcast_topic(topic, payload)
                continue
            if self.publisher.should_publish(topic, payload):
                await self._broadcast_topic(topic, payload)

    async def topic_handler(self, websocket, topic: str) -> None:
        self.topic_clients[topic].add(websocket)
        try:
            if topic == "events":
                await self._send_json(
                    websocket,
                    {"cycle": self.cycle, "events": self.tracker.events_backlog(), "backlog": True},
                )
            else:
                payload = self.tracker.topic_payload(topic, self.service_state())
                self.publisher.snapshot(topic, payload)
                await self._send_json(websocket, payload)
            await websocket.wait_closed()
        finally:
            self.topic_clients[topic].discard(websocket)

    async def control_handler(self, websocket) -> None:
        self.control_clients.add(websocket)
        try:
            await self._broadcast_control_state()
            async for raw in websocket:
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    await self._send_json(websocket, {"type": "error", "message": "invalid json"})
                    continue
                command = message.get("command")
                if command == "play":
                    await self.play()
                elif command == "pause":
                    await self.pause()
                elif command == "reset":
                    await self.reset()
                elif command == "step":
                    await self.step_once(message.get("cycles"))
                else:
                    await self._send_json(
                        websocket,
                        {"type": "error", "message": f"unknown command: {command}"},
                    )
        finally:
            self.control_clients.discard(websocket)

    async def ws_handler(self, websocket) -> None:
        path = websocket.request.path
        if path == "/ws/control":
            await self.control_handler(websocket)
            return
        if not path.startswith("/ws/"):
            await websocket.close(code=1008, reason="unsupported path")
            return
        topic = path.rsplit("/", 1)[-1]
        if topic not in _TOPICS:
            await websocket.close(code=1008, reason="unsupported topic")
            return
        await self.topic_handler(websocket, topic)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--http-port", type=int, default=9000)
    parser.add_argument("--ws-port", type=int, default=9001)
    parser.add_argument("--step-cycles", type=int, default=1)
    parser.add_argument("--reset-cycles", type=int, default=10)
    parser.add_argument("--tick-ms", type=int, default=100)
    return parser.parse_args(argv)


async def async_main(argv: list[str]) -> int:
    if websockets is None:
        raise ModuleNotFoundError(
            "missing dependency: websockets; install it in the active python environment before running lsq_webui.py"
        )
    args = parse_args(argv)
    service = MemBlockWebUIService(
        host=args.host,
        ws_port=args.ws_port,
        http_port=args.http_port,
        step_cycles=args.step_cycles,
        reset_cycles=args.reset_cycles,
        tick_ms=args.tick_ms,
    )
    await service.initialize()
    print(f"http://{args.host}:{args.http_port}")
    print(f"ws://{args.host}:{args.ws_port}/ws/control")
    try:
        server = await websockets.serve(service.ws_handler, args.host, args.ws_port)
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            server.close()
            await server.wait_closed()
    except KeyboardInterrupt:
        pass
    finally:
        await service.shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        return asyncio.run(async_main(argv))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
