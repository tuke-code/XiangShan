# coding=utf-8
"""
TransportResponder: outer/dcache 最小响应器。
"""

from collections import deque
import random


TL_A_PUT_FULL = 0
TL_A_PUT_PARTIAL = 1
TL_A_GET = 4
TL_A_ACQUIRE_BLOCK = 6

TL_C_PROBE_ACK = 4
TL_C_PROBE_ACK_DATA = 5
TL_C_RELEASE = 6
TL_C_RELEASE_DATA = 7

TL_D_ACCESS_ACK = 0
TL_D_ACCESS_ACK_DATA = 1
TL_D_GRANT_DATA = 5
TL_D_RELEASE_ACK = 6

DEFAULT_CACHELINE_BYTES = 64
DEFAULT_DCACHE_BEAT_BYTES = 32


def _signal_value(signal, default=None):
    try:
        return int(signal.value)
    except AttributeError:
        return default


class TransportResponder:
    """负责 outer buffer 与 dcache client 的请求响应闭环。"""

    def __init__(
        self,
        dut,
        outer_a,
        outer_d,
        dcache_a,
        dcache_b,
        dcache_c,
        dcache_d,
        dcache_e,
        ref_memory,
        drain_log: list[dict],
        *,
        outer_delay: int,
        grant_delay_min: int,
        grant_delay_max: int,
        release_ack_delay: int,
        delay_seed: int,
    ) -> None:
        self.dut = dut
        self.outer_a = outer_a
        self.outer_d = outer_d
        self.dcache_a = dcache_a
        self.dcache_b = dcache_b
        self.dcache_c = dcache_c
        self.dcache_d = dcache_d
        self.dcache_e = dcache_e
        self.ref_memory = ref_memory
        self.drain_log = drain_log
        self.outer_delay = outer_delay
        self.grant_delay_min = grant_delay_min
        self.grant_delay_max = grant_delay_max
        self.release_ack_delay = release_ack_delay
        self._delay_rng = random.Random(delay_seed)
        self._cycle = 0
        self._next_sink = 1

        self.outer_request_count = 0
        self.outer_response_count = 0
        self.outer_write_request_count = 0
        self.dcache_a_request_count = 0
        self.dcache_c_request_count = 0
        self.dcache_e_request_count = 0
        self.dcache_b_response_count = 0
        self.dcache_d_response_count = 0
        self.last_dcache_a_request = None
        self._request_ready_override = {
            "outer_a": None,
            "dcache_a": None,
            "dcache_c": None,
            "dcache_e": None,
        }

        self.reset_runtime_state()

    @property
    def outstanding_transaction_count(self) -> int:
        return (
            len(self._pending_outer_d)
            + (1 if self._active_outer_d is not None else 0)
            + len(self._pending_b)
            + (1 if self._active_b is not None else 0)
            + len(self._pending_d)
            + (1 if self._active_d is not None else 0)
            + len(self._inflight_grants)
        )

    @property
    def stats(self) -> dict[str, int]:
        return {
            "outer_request_count": self.outer_request_count,
            "outer_response_count": self.outer_response_count,
            "outer_write_request_count": self.outer_write_request_count,
            "dcache_a_request_count": self.dcache_a_request_count,
            "dcache_c_request_count": self.dcache_c_request_count,
            "dcache_e_request_count": self.dcache_e_request_count,
            "dcache_b_response_count": self.dcache_b_response_count,
            "dcache_d_response_count": self.dcache_d_response_count,
            "last_dcache_a_address": -1
            if self.last_dcache_a_request is None
            else int(self.last_dcache_a_request["address"]),
            "last_dcache_a_block_address": -1
            if self.last_dcache_a_request is None
            else int(self.last_dcache_a_request["block_addr"]),
            "last_dcache_a_source": -1
            if self.last_dcache_a_request is None
            else int(self.last_dcache_a_request["source"]),
            "last_dcache_a_is_keyword": -1
            if self.last_dcache_a_request is None
            else int(self.last_dcache_a_request["is_keyword"]),
            "pending_outer_d_count": len(self._pending_outer_d),
            "pending_b_count": len(self._pending_b),
            "pending_d_count": len(self._pending_d),
            "active_outer_d_count": 1 if self._active_outer_d is not None else 0,
            "active_b_count": 1 if self._active_b is not None else 0,
            "active_d_count": 1 if self._active_d is not None else 0,
            "inflight_grant_count": len(self._inflight_grants),
        }

    def reset_runtime_state(self) -> None:
        self._pending_outer_d = deque()
        self._active_outer_d = None
        self._pending_b = deque()
        self._active_b = None
        self._pending_d = deque()
        self._active_d = None
        self._inflight_grants = {}
        self.clear_request_ready_overrides()
        self.drive_idle()

    def reset(self) -> None:
        self._next_sink = 1
        self.outer_request_count = 0
        self.outer_response_count = 0
        self.outer_write_request_count = 0
        self.dcache_a_request_count = 0
        self.dcache_c_request_count = 0
        self.dcache_e_request_count = 0
        self.dcache_b_response_count = 0
        self.dcache_d_response_count = 0
        self.last_dcache_a_request = None
        self.reset_runtime_state()

    def drive_idle(self) -> None:
        self.outer_d.drive_idle()
        self.dcache_b.drive_idle()
        self.dcache_d.drive_idle()

    def set_request_ready_override(self, channel: str, ready: int | bool | None) -> None:
        if channel not in self._request_ready_override:
            raise ValueError(f"未知 request ready channel: {channel}")
        self._request_ready_override[channel] = None if ready is None else int(bool(ready))

    def clear_request_ready_overrides(self) -> None:
        for channel in self._request_ready_override:
            self._request_ready_override[channel] = None

    def enqueue_outer_response(
        self,
        opcode: int = TL_D_ACCESS_ACK_DATA,
        param: int = 0,
        size: int = 3,
        source: int = 0,
        request_source: int | None = None,
        sink: int = 0,
        denied: int = 0,
        data: int = 0,
        corrupt: int = 0,
        delay_cycles: int | None = None,
    ) -> None:
        delay = self.outer_delay if delay_cycles is None else delay_cycles
        self._pending_outer_d.append(
            {
                "release_cycle": self._cycle + delay,
                "opcode": opcode,
                "param": param,
                "size": size,
                "source": source,
                "request_source": source if request_source is None else request_source,
                "sink": sink,
                "denied": denied,
                "data": data,
                "corrupt": corrupt,
            }
        )

    def enqueue_b_response(
        self,
        opcode: int = 0,
        param: int = 0,
        size: int = 6,
        source: int = 0,
        address: int = 0,
        mask: int = 0,
        data: int = 0,
        corrupt: int = 0,
        delay_cycles: int | None = None,
    ) -> None:
        delay = self._sample_dcache_delay() if delay_cycles is None else delay_cycles
        self._pending_b.append(
            {
                "release_cycle": self._cycle + delay,
                "opcode": opcode,
                "param": param,
                "size": size,
                "source": source,
                "address": address,
                "mask": mask,
                "data": data,
                "corrupt": corrupt,
            }
        )

    def enqueue_d_response(
        self,
        opcode: int = TL_D_GRANT_DATA,
        param: int = 0,
        size: int = 6,
        source: int = 0,
        request_channel: str = "a",
        request_source: int | None = None,
        sink: int = 0,
        denied: int = 0,
        echo_is_keyword: int = 0,
        data: int = 0,
        corrupt: int = 0,
        delay_cycles: int | None = None,
    ) -> None:
        delay = self._sample_dcache_delay() if delay_cycles is None else delay_cycles
        self._pending_d.append(
            {
                "release_cycle": self._cycle + delay,
                "beats": deque(
                    [
                        {
                            "opcode": opcode,
                            "param": param,
                            "size": size,
                            "source": source,
                            "request_channel": request_channel,
                            "request_source": source if request_source is None else request_source,
                            "sink": sink,
                            "denied": denied,
                            "echo_isKeyword": echo_is_keyword,
                            "data": data,
                            "corrupt": corrupt,
                        }
                    ]
                ),
            }
        )

    def capture_on_rise(self, cycle: int) -> None:
        self._cycle = cycle
        if self._in_reset():
            return

        self._capture_outer_request()
        self._capture_dcache_requests()
    
    def drive_pre_step(self, cycle: int | None = None) -> None:
        if cycle is not None:
            self._cycle = int(cycle)
        self._drive_request_readies()
        if self._in_reset():
            self.drive_idle()
            return

        self._service_outer_d()
        self._service_b_channel()
        self._service_d_channel()

    def _in_reset(self) -> bool:
        return bool(_signal_value(self.dut.reset, 0) or _signal_value(self.dut.io_reset_backend, 0))

    def _sample_dcache_delay(self) -> int:
        low = min(self.grant_delay_min, self.grant_delay_max)
        high = max(self.grant_delay_min, self.grant_delay_max)
        return self._delay_rng.randint(low, high)

    def _drive_request_readies(self) -> None:
        self.outer_a.ready.value = (
            1 if self._request_ready_override["outer_a"] is None else self._request_ready_override["outer_a"]
        )
        self.dcache_a.ready.value = (
            1 if self._request_ready_override["dcache_a"] is None else self._request_ready_override["dcache_a"]
        )
        self.dcache_c.ready.value = (
            1 if self._request_ready_override["dcache_c"] is None else self._request_ready_override["dcache_c"]
        )
        self.dcache_e.ready.value = (
            1 if self._request_ready_override["dcache_e"] is None else self._request_ready_override["dcache_e"]
        )

    def _capture_outer_request(self) -> None:
        if not (self.outer_a.valid.value and self.outer_a.ready.value):
            return

        opcode = int(self.outer_a.bits_opcode.value)
        size = int(self.outer_a.bits_size.value)
        source = int(self.outer_a.bits_source.value)
        addr = int(self.outer_a.bits_address.value)
        width_bytes = max(1, 1 << size)

        self.outer_request_count += 1
        if opcode == TL_A_GET:
            data = self.ref_memory.read(addr, width_bytes)
            self.enqueue_outer_response(
                opcode=TL_D_ACCESS_ACK_DATA,
                size=size,
                source=source,
                request_source=source,
                data=data,
                corrupt=0,
            )
            return

        if opcode in (TL_A_PUT_FULL, TL_A_PUT_PARTIAL):
            self.outer_write_request_count += 1
            mask = int(self.outer_a.bits_mask.value)
            if opcode == TL_A_PUT_FULL:
                mask = (1 << width_bytes) - 1
            self.drain_log.append(
                {
                    "channel": "outer",
                    "addr": addr,
                    "data": int(self.outer_a.bits_data.value),
                    "mask": mask,
                    "width_bytes": width_bytes,
                    "cycle": self._cycle,
                }
            )
            self.enqueue_outer_response(
                opcode=TL_D_ACCESS_ACK,
                size=size,
                source=source,
                request_source=source,
                data=0,
                corrupt=0,
            )
            return

        raise AssertionError(f"outer buffer 未支持的请求 opcode={opcode}")

    def _capture_dcache_requests(self) -> None:
        if self.dcache_a.valid.value and self.dcache_a.ready.value:
            self.dcache_a_request_count += 1
            self._handle_dcache_a()

        if self.dcache_c.valid.value and self.dcache_c.ready.value:
            self.dcache_c_request_count += 1
            self._handle_dcache_c()

        if self.dcache_e.valid.value and self.dcache_e.ready.value:
            self.dcache_e_request_count += 1
            self._handle_dcache_e()

    def _handle_dcache_a(self) -> None:
        opcode = int(self.dcache_a.bits_opcode.value)
        if opcode != TL_A_ACQUIRE_BLOCK:
            raise AssertionError(f"load-only 场景仅支持 AcquireBlock，观测到 A.opcode={opcode}")

        raw_addr = int(self.dcache_a.bits_address.value)
        block_addr = raw_addr & ~(DEFAULT_CACHELINE_BYTES - 1)
        size = int(self.dcache_a.bits_size.value)
        source = int(self.dcache_a.bits_source.value)
        is_keyword = int(self.dcache_a.bits_echo_isKeyword.value)
        self.last_dcache_a_request = {
            "address": raw_addr,
            "block_addr": block_addr,
            "source": source,
            "is_keyword": is_keyword,
        }
        line = self.ref_memory.read_cacheline(block_addr, DEFAULT_CACHELINE_BYTES)
        beats = [
            int.from_bytes(line[idx: idx + DEFAULT_DCACHE_BEAT_BYTES], "little")
            for idx in range(0, DEFAULT_CACHELINE_BYTES, DEFAULT_DCACHE_BEAT_BYTES)
        ]
        if is_keyword:
            beats = list(reversed(beats))

        sink = self._alloc_sink()
        delay = self._sample_dcache_delay()
        self._inflight_grants[sink] = {"source": source, "opcode": opcode}
        self._pending_d.append(
            {
                "release_cycle": self._cycle + delay,
                "beats": deque(
                    [
                        {
                            "opcode": TL_D_GRANT_DATA,
                            "param": 0,
                            "size": size,
                            "source": source,
                            "request_channel": "a",
                            "request_source": source,
                            "sink": sink,
                            "denied": 0,
                            "echo_isKeyword": is_keyword,
                            "data": beat,
                            "corrupt": 0,
                        }
                        for beat in beats
                    ]
                ),
            }
        )

    def _handle_dcache_c(self) -> None:
        opcode = int(self.dcache_c.bits_opcode.value)
        if opcode in (TL_C_PROBE_ACK, TL_C_PROBE_ACK_DATA):
            return
        if opcode in (TL_C_RELEASE, TL_C_RELEASE_DATA):
            self._pending_d.append(
                {
                    "release_cycle": self._cycle + self.release_ack_delay,
                    "beats": deque(
                        [
                            {
                                "opcode": TL_D_RELEASE_ACK,
                                "param": 0,
                                "size": int(self.dcache_c.bits_size.value),
                                "source": int(self.dcache_c.bits_source.value),
                                "request_channel": "c",
                                "request_source": int(self.dcache_c.bits_source.value),
                                "sink": 0,
                                "denied": 0,
                                "echo_isKeyword": int(self.dcache_c.bits_echo_isKeyword.value),
                                "data": 0,
                                "corrupt": 0,
                            }
                        ]
                    ),
                }
            )
            return
        raise AssertionError(f"未支持的 DCache C 通道 opcode={opcode}")

    def _handle_dcache_e(self) -> None:
        sink = int(self.dcache_e.bits_sink.value)
        if sink not in self._inflight_grants:
            raise AssertionError(f"观测到未知的 GrantAck sink={sink}")
        del self._inflight_grants[sink]

    def _service_outer_d(self) -> None:
        if self._active_outer_d is None and self._pending_outer_d:
            if self._pending_outer_d[0]["release_cycle"] <= self._cycle:
                self._active_outer_d = self._pending_outer_d.popleft()

        if self._active_outer_d is None:
            self.outer_d.drive_idle()
            return

        response = self._active_outer_d
        if response["source"] != response["request_source"]:
            raise AssertionError(
                "outer D 通道 source 必须与前序 A 通道请求 source 一致: "
                f"request_source={response['request_source']}, d_source={response['source']}"
            )
        self.outer_d.valid.value = 1
        self.outer_d.bits_opcode.value = response["opcode"]
        self.outer_d.bits_param.value = response["param"]
        self.outer_d.bits_size.value = response["size"]
        self.outer_d.bits_source.value = response["source"]
        self.outer_d.bits_sink.value = response["sink"]
        self.outer_d.bits_denied.value = response["denied"]
        self.outer_d.bits_data.value = response["data"]
        self.outer_d.bits_corrupt.value = response["corrupt"]

        if self.outer_d.ready.value:
            self.outer_response_count += 1
            self._active_outer_d = None

    def _service_b_channel(self) -> None:
        if self._active_b is None and self._pending_b:
            if self._pending_b[0]["release_cycle"] <= self._cycle:
                self._active_b = self._pending_b.popleft()

        if self._active_b is None:
            self.dcache_b.drive_idle()
            return

        response = self._active_b
        self.dcache_b.valid.value = 1
        self.dcache_b.bits_opcode.value = response["opcode"]
        self.dcache_b.bits_param.value = response["param"]
        self.dcache_b.bits_size.value = response["size"]
        self.dcache_b.bits_source.value = response["source"]
        self.dcache_b.bits_address.value = response["address"]
        self.dcache_b.bits_mask.value = response["mask"]
        self.dcache_b.bits_data.value = response["data"]
        self.dcache_b.bits_corrupt.value = response["corrupt"]

        if self.dcache_b.ready.value:
            self.dcache_b_response_count += 1
            self._active_b = None

    def _service_d_channel(self) -> None:
        if self._active_d is None and self._pending_d:
            if self._pending_d[0]["release_cycle"] <= self._cycle:
                self._active_d = self._pending_d.popleft()

        if self._active_d is None:
            self.dcache_d.drive_idle()
            return

        beat = self._active_d["beats"][0]
        if beat.get("request_channel") == "a" and beat["source"] != beat["request_source"]:
            raise AssertionError(
                "dcache D 通道 source 必须与前序 A 通道请求 source 一致: "
                f"request_source={beat['request_source']}, d_source={beat['source']}"
            )
        self.dcache_d.valid.value = 1
        self.dcache_d.bits_opcode.value = beat["opcode"]
        self.dcache_d.bits_param.value = beat["param"]
        self.dcache_d.bits_size.value = beat["size"]
        self.dcache_d.bits_source.value = beat["source"]
        self.dcache_d.bits_sink.value = beat["sink"]
        self.dcache_d.bits_denied.value = beat["denied"]
        self.dcache_d.bits_echo_isKeyword.value = beat["echo_isKeyword"]
        self.dcache_d.bits_data.value = beat["data"]
        self.dcache_d.bits_corrupt.value = beat["corrupt"]

        if self.dcache_d.ready.value:
            self.dcache_d_response_count += 1
            self._active_d["beats"].popleft()
            if not self._active_d["beats"]:
                self._active_d = None

    def _alloc_sink(self) -> int:
        sink = self._next_sink
        self._next_sink = (self._next_sink + 1) & 0x3FF
        if self._next_sink == 0:
            self._next_sink = 1
        return sink
