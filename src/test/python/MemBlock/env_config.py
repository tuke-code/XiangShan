# coding=utf-8
"""
MemBlock verification environment configuration.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TransportConfig:
    outer_delay: int = 4
    grant_delay_min: int = 2
    grant_delay_max: int = 8
    release_ack_delay: int = 1
    delay_seed: int = 20260330


@dataclass(frozen=True)
class SequenceConfig:
    reset_cycles: int = 20
    reset_settle_cycles: int = 1
    load_queue_size: int = 72
    store_queue_size: int = 56
    load_drain_cycles: int = 256
    store_materialize_cycles: int = 300
    store_flush_cycles: int = 400
    store_settle_cycles: int = 4


@dataclass(frozen=True)
class EnvConfig:
    load_pipeline_width: int = 3
    lsq_enq_ports: int = 8
    int_issue_ports: int = 7
    int_writeback_ports: int = 7
    store_pipeline_width: int = 2
    sbuffer_write_ports: int = 2
    store_queue_size: int = 56
    rob_size: int = 512
    strict_writeback_check: bool = True
    transport: TransportConfig = field(default_factory=TransportConfig)
    sequence: SequenceConfig = field(default_factory=SequenceConfig)


DEFAULT_ENV_CONFIG = EnvConfig()
