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
import copy
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
_TOPICS = {"overview", "vlq", "sq", "replay", "violations", "perf", "events", "traces"}

_TESTS_PATH = _HERE / "tests"
if str(_TESTS_PATH) not in sys.path:
    sys.path.insert(0, str(_TESTS_PATH))

from test_MemBlock_random_load import (
    test_api_MemBlock_mem_io_1000_load_requests,
    test_api_MemBlock_random_io_1000_load_requests,
)


_SCENARIOS = {
    "random_io": {
        "label": "random_io",
        "runner": test_api_MemBlock_random_io_1000_load_requests,
    },
    "mem_io": {
        "label": "mem_io",
        "runner": test_api_MemBlock_mem_io_1000_load_requests,
    },
}


class ScenarioStopped(RuntimeError):
    """Raised when the running scenario is cancelled or reset."""


class ThreadBoundProxy:
    """Resolve env/DUT access on the WebUI service loop thread."""

    def __init__(self, service, resolver) -> None:
        object.__setattr__(self, "_service", service)
        object.__setattr__(self, "_resolver", resolver)

    def _resolve_on_loop(self):
        return self._resolver()

    def __getattr__(self, name: str):
        return ThreadBoundProxy(
            self._service,
            lambda resolver=self._resolver, attr_name=name: getattr(resolver(), attr_name),
        )

    def __getitem__(self, index):
        return ThreadBoundProxy(
            self._service,
            lambda resolver=self._resolver, item=index: resolver()[item],
        )

    def __setattr__(self, name: str, value) -> None:
        self._service._call_on_loop_sync(
            lambda resolver=self._resolver, attr_name=name, attr_value=value: setattr(
                resolver(),
                attr_name,
                self._service._unwrap_on_loop(attr_value),
            )
        )

    def __call__(self, *args, **kwargs):
        return self._service._call_on_loop_sync(
            lambda resolver=self._resolver, call_args=args, call_kwargs=kwargs: resolver()(
                *self._service._unwrap_on_loop(call_args),
                **self._service._unwrap_on_loop(call_kwargs),
            )
        )

    def __int__(self) -> int:
        return int(self._service._call_on_loop_sync(self._resolve_on_loop))

    def __index__(self) -> int:
        return int(self)

    def __bool__(self) -> bool:
        return bool(self._service._call_on_loop_sync(self._resolve_on_loop))

    def __len__(self) -> int:
        return len(self._service._call_on_loop_sync(self._resolve_on_loop))

    def __eq__(self, other) -> bool:
        return self._service._call_on_loop_sync(
            lambda resolver=self._resolver, rhs=other: resolver() == self._service._unwrap_on_loop(rhs)
        )

    def __repr__(self) -> str:
        return repr(self._service._call_on_loop_sync(self._resolve_on_loop))


class ScenarioEnvProxy:
    """Proxy env used to reuse the selected test case directly."""

    def __init__(self, service, env, generation: int) -> None:
        self._service = service
        self._env = env
        self._generation = generation

    def __getattr__(self, name: str):
        return ThreadBoundProxy(
            self._service,
            lambda env=self._env, attr_name=name: getattr(env, attr_name),
        )

    def _env_call(self, func, *args, **kwargs):
        return self._service._call_on_loop_sync(func, *args, generation=self._generation, **kwargs)

    def Step(self, cycles: int = 1) -> None:
        cycles = int(cycles)
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
            self._service._call_on_loop_sync(self._env.Step, 1, generation=self._generation)
            if continuous_run and self._service.tick_ms > 0:
                if self._service._driver_stop_event.wait(self._service.tick_ms / 1000.0):
                    raise ScenarioStopped("scenario stopped")

    def reset(self, cycles: int = 10, settle_cycles: int = 5) -> None:
        cycles = int(cycles)
        settle_cycles = int(settle_cycles)
        if self._service._driver_stop_event.is_set():
            raise ScenarioStopped("scenario stopped")
        self._service._call_on_loop_sync(
            self._env.reset,
            cycles=cycles,
            settle_cycles=settle_cycles,
            generation=self._generation,
        )

    def idle_inputs(self) -> None:
        self._env_call(self._env.idle_inputs)

    def pulse_store_commit(self, count: int = 1) -> None:
        count = int(count)
        self._env_call(self._env.backend.pulse_store_commit, count)

    def drain_writebacks(self, max_cycles: int = 200) -> None:
        max_cycles = int(max_cycles)
        for _ in range(max_cycles):
            outstanding_expected = self._env_call(lambda: int(self._env.memory.outstanding_expected_count))
            outstanding_txn = self._env_call(lambda: int(self._env.memory.outstanding_transaction_count))
            if outstanding_expected == 0 and outstanding_txn == 0:
                return
            self.Step(1)
        raise TimeoutError("等待 MemoryModel 收敛超时")

    def wait_counter_growth(self, counter_name: str, baseline: int, max_cycles: int = 200) -> int:
        baseline = int(baseline)
        max_cycles = int(max_cycles)
        for _ in range(max_cycles):
            current = self._env_call(self._env.get_counter, counter_name)
            if current > baseline:
                return current
            self.Step(1)
        raise TimeoutError(f"等待 `{counter_name}` 增长超时")

    def wait_memory_quiesce(self, max_cycles: int = 200) -> None:
        max_cycles = int(max_cycles)
        for _ in range(max_cycles):
            outstanding = self._env_call(lambda: int(self._env.memory.outstanding_transaction_count))
            if outstanding == 0:
                return
            self.Step(1)
        raise TimeoutError("等待 MemoryModel 事务收敛超时")

    def wait_store_materialized(
        self,
        sq_idx: int,
        *,
        expected_addr: int,
        expected_data: int,
        expected_mmio: bool | None = None,
        require_committed: bool = False,
        max_cycles: int = 200,
    ):
        sq_idx = int(sq_idx)
        expected_addr = int(expected_addr)
        expected_data = int(expected_data)
        max_cycles = int(max_cycles)
        for _ in range(max_cycles):
            store = self._env_call(self._env.get_store_view, sq_idx)
            if (
                store is not None
                and store.allocated
                and store.addr == expected_addr
                and store.data is not None
                and (store.data & ((1 << 64) - 1)) == (expected_data & ((1 << 64) - 1))
                and store.mask != 0
                and (expected_mmio is None or store.mmio == expected_mmio)
                and (not require_committed or store.committed)
            ):
                return store
            self.Step(1)
        store = self._env_call(self._env.get_store_view, sq_idx)
        raise AssertionError(
            "等待 pending store shadow 超时: "
            f"sqIdx={sq_idx}, store={store}"
        )

    def flush_store_buffers_and_wait(self, max_cycles: int = 200, settle_cycles: int = 4) -> dict:
        max_cycles = int(max_cycles)
        settle_cycles = int(settle_cycles)
        has_flush_sb = self._env_call(lambda: hasattr(self._env.dut, "io_ooo_to_mem_flushSb"))
        if not has_flush_sb:
            raise AttributeError("当前 DUT 未导出 `io_ooo_to_mem_flushSb`")

        has_sfence = self._env_call(lambda: hasattr(self._env.dut, "io_ooo_to_mem_sfence_valid"))
        if has_sfence:
            self._env_call(setattr, self._env.dut.io_ooo_to_mem_sfence_valid, "value", 1)
            self._env_call(setattr, self._env.dut.io_ooo_to_mem_sfence_bits_rs1, "value", 0)
            self._env_call(setattr, self._env.dut.io_ooo_to_mem_sfence_bits_rs2, "value", 0)
            self._env_call(setattr, self._env.dut.io_ooo_to_mem_sfence_bits_addr, "value", 0)
            self._env_call(setattr, self._env.dut.io_ooo_to_mem_sfence_bits_id, "value", 0)
            self._env_call(setattr, self._env.dut.io_ooo_to_mem_sfence_bits_hv, "value", 0)
            self._env_call(setattr, self._env.dut.io_ooo_to_mem_sfence_bits_hg, "value", 0)
        self._env_call(setattr, self._env.dut.io_ooo_to_mem_flushSb, "value", 1)
        self.Step(1)
        if has_sfence:
            self._env_call(setattr, self._env.dut.io_ooo_to_mem_sfence_valid, "value", 0)
        self._env_call(setattr, self._env.dut.io_ooo_to_mem_flushSb, "value", 0)

        for _ in range(max_cycles):
            sb_is_empty = self._env_call(lambda: int(self._env.mem_status.sbIsEmpty.value))
            if sb_is_empty:
                self.Step(settle_cycles)
                return self._env_call(self._env.memory.finalize_and_check_drain)
            self.Step(1)
        raise TimeoutError("等待 sbuffer drain 结束超时")


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
        self._driver_cycle_lock = ThreadLock()
        self._sampling_generation_lock = ThreadLock()
        self._sampling_generation: int | None = None
        self._driver_step_budget = 0
        self._driver_cycle = 0
        self._driver_flush_task: asyncio.Task | None = None
        self._driver_sample_dirty = False
        self._scenario_started = False
        self._scenario_done = False
        self._scenario_error: str | None = None
        self._scenario_generation = 0
        self._scenario_name = "random_io"
        self.trace_filters: dict[str, str] = {}
        self.run_until_condition: dict[str, str] | None = None
        self.run_until_hit: dict[str, Any] | None = None

    def _normalize_filters(self, filters: dict[str, Any] | None) -> dict[str, str]:
        normalized: dict[str, str] = {}
        if not isinstance(filters, dict):
            return normalized
        for key in ("sched_index", "lq_idx", "rob_idx", "cause", "source", "state"):
            value = filters.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                normalized[key] = text
        return normalized

    def service_state(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "step_cycles": self.step_cycles,
            "tick_ms": self.tick_ms,
            "last_raw": self.last_raw,
            "scenario_name": self._scenario_name,
            "scenario_started": self._scenario_started,
            "scenario_done": self._scenario_done,
            "scenario_error": self._scenario_error,
            "trace_filters": copy.deepcopy(self.trace_filters),
            "run_until_condition": copy.deepcopy(self.run_until_condition),
            "run_until_hit": copy.deepcopy(self.run_until_hit),
        }

    async def initialize(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.env.add_after_step_callback(self._on_env_step)
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
        self.env.remove_after_step_callback(self._on_env_step)
        self.env.clear_callbacks()
        self.env.Finish()

    def _clear_driver_step_budget(self) -> None:
        with self._driver_budget_lock:
            self._driver_step_budget = 0

    def _current_sampling_generation(self) -> int:
        with self._sampling_generation_lock:
            if self._sampling_generation is None:
                return int(self._scenario_generation)
            return int(self._sampling_generation)

    def _reset_driver_cycle(self) -> None:
        with self._driver_cycle_lock:
            self._driver_cycle = 0

    def _claim_next_driver_cycle(self) -> int:
        with self._driver_cycle_lock:
            self._driver_cycle += 1
            return self._driver_cycle

    def _unwrap_on_loop(self, value):
        if isinstance(value, ThreadBoundProxy):
            return value._resolve_on_loop()
        if isinstance(value, tuple):
            return tuple(self._unwrap_on_loop(item) for item in value)
        if isinstance(value, list):
            return [self._unwrap_on_loop(item) for item in value]
        if isinstance(value, dict):
            return {key: self._unwrap_on_loop(item) for key, item in value.items()}
        return value

    def _call_on_loop_impl(self, func, args, kwargs, generation: int | None):
        if generation is not None:
            with self._sampling_generation_lock:
                self._sampling_generation = int(generation)
        try:
            return func(
                *self._unwrap_on_loop(args),
                **self._unwrap_on_loop(kwargs),
            )
        finally:
            if generation is not None:
                with self._sampling_generation_lock:
                    self._sampling_generation = None

    def _call_on_loop_sync(self, func, *args, generation: int | None = None, **kwargs):
        if self._loop is None:
            raise RuntimeError("event loop is not initialized")
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is self._loop:
            return self._call_on_loop_impl(func, args, kwargs, generation)
        sync_future = asyncio.run_coroutine_threadsafe(
            self._call_on_loop_rpc(func, args, kwargs, generation),
            self._loop,
        )
        return sync_future.result()

    async def _call_on_loop_rpc(self, func, args, kwargs, generation: int | None):
        return self._call_on_loop_impl(func, args, kwargs, generation)

    def _on_env_step(self) -> None:
        sample_cycle = self._claim_next_driver_cycle()
        raw = self.reader.read(sample_cycle)
        generation = self._current_sampling_generation()
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is self._loop:
            self._record_driver_snapshot(generation, raw)
            return
        self._loop.call_soon_threadsafe(self._record_driver_snapshot, generation, raw)

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
        scenario_name = self._scenario_name
        self._driver_task = asyncio.create_task(
            self._run_selected_scenario(self._scenario_generation, scenario_name)
        )

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

    def _record_driver_snapshot(self, generation: int, raw: dict[str, Any]) -> None:
        if generation != self._scenario_generation:
            return
        self.cycle = int(raw.get("cycle", self.cycle))
        self.last_raw = raw
        self.tracker.update(self.last_raw)
        if self.run_until_condition:
            matches = self.tracker.matching_traces(self.run_until_condition, only_pending=True)
            if matches:
                trace = self.tracker._public_trace(matches[0])
                self.running = False
                self._driver_run_event.clear()
                self._clear_driver_step_budget()
                self.run_until_hit = {
                    "cycle": self.cycle,
                    "trace_id": trace["trace_id"],
                    "label": trace["label"],
                    "status": trace["status"],
                }
                self.run_until_condition = None
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

    def _run_selected_scenario_sync(self, generation: int, scenario_name: str) -> None:
        if self._loop is None:
            raise RuntimeError("event loop is not initialized")
        runner = _SCENARIOS[scenario_name]["runner"]
        proxy = ScenarioEnvProxy(self, self.env, generation)
        runner(proxy)

    async def _run_selected_scenario(self, generation: int, scenario_name: str) -> None:
        error = None
        stopped = False
        try:
            await asyncio.to_thread(self._run_selected_scenario_sync, generation, scenario_name)
        except ScenarioStopped:
            stopped = True
        except Exception as exc:
            error = str(exc)
        await self._finish_scenario(generation, stopped=stopped, error=error)

    async def set_scenario(self, scenario_name: str) -> None:
        if scenario_name not in _SCENARIOS:
            raise KeyError(f"unknown scenario: {scenario_name}")
        changed = False
        async with self._command_lock:
            if self._scenario_name != scenario_name:
                self._scenario_name = scenario_name
                changed = True
        if changed:
            await self.reset()
            return
        await self._broadcast_control_state()

    async def set_filters(self, filters: dict[str, Any] | None) -> None:
        async with self._command_lock:
            self.trace_filters = self._normalize_filters(filters)
        await self._broadcast_all(force=True)
        await self._broadcast_control_state()

    async def clear_filters(self) -> None:
        await self.set_filters({})

    async def run_until(self, filters: dict[str, Any] | None = None) -> None:
        async with self._command_lock:
            actual_filters = self._normalize_filters(filters)
            if not actual_filters:
                actual_filters = copy.deepcopy(self.trace_filters)
            if not actual_filters:
                actual_filters = {"state": "writeback"}
            self.run_until_condition = actual_filters
            self.run_until_hit = None
            if self._scenario_done:
                self.running = False
            else:
                await self._ensure_scenario_started_locked()
                self.running = True
                self._driver_run_event.set()
        await self._broadcast_control_state()

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
            self.run_until_hit = None
            self.run_until_condition = None
            self.cycle = 0
            self._reset_driver_cycle()
            self.last_raw = None
            self._call_on_loop_sync(
                self.env.reset,
                cycles=self.reset_cycles,
                settle_cycles=1,
                generation=self._scenario_generation,
            )
            if self.last_raw is None:
                self.last_raw = {"cycle": 0}
            self.tracker.load_reset_snapshot(self.last_raw)
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
                self.run_until_hit = None
        await self._broadcast_control_state()

    async def play(self) -> None:
        async with self._command_lock:
            if self._scenario_done:
                self.running = False
            else:
                await self._ensure_scenario_started_locked()
                self.running = True
                self._driver_run_event.set()
                self.run_until_hit = None
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
            "scenario_name": self._scenario_name,
            "scenarios": [
                {"name": name, "label": data["label"]}
                for name, data in _SCENARIOS.items()
            ],
            "scenario_started": self._scenario_started,
            "scenario_done": self._scenario_done,
            "scenario_error": self._scenario_error,
            "trace_filters": copy.deepcopy(self.trace_filters),
            "run_until_condition": copy.deepcopy(self.run_until_condition),
            "run_until_hit": copy.deepcopy(self.run_until_hit),
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
                elif command == "step_n":
                    await self.step_once(message.get("cycles"))
                elif command == "set_scenario":
                    scenario_name = message.get("scenario")
                    if not isinstance(scenario_name, str):
                        await self._send_json(
                            websocket,
                            {"type": "error", "message": "missing scenario name"},
                        )
                        continue
                    try:
                        await self.set_scenario(scenario_name)
                    except KeyError:
                        await self._send_json(
                            websocket,
                            {"type": "error", "message": f"unknown scenario: {scenario_name}"},
                        )
                elif command == "set_filters":
                    await self.set_filters(message.get("filters"))
                elif command == "clear_filters":
                    await self.clear_filters()
                elif command == "run_until":
                    await self.run_until(message.get("filters"))
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
