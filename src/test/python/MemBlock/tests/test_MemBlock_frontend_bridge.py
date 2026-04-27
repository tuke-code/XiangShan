# coding=utf-8
"""
frontendBridge 最小往返 smoke。
"""

import pytest


TL_A_GET = 4
TL_D_ACCESS_ACK_DATA = 1


def _bridge_roundtrip(path, *, request_fields: dict[str, int], response_fields: dict[str, int]):
    path.drive_idle()
    path.set_mem_a_ready(1)
    path.drive_frontend_a(**request_fields)
    request = path.wait_mem_a_fire(max_cycles=64)
    path.clear_frontend_a()
    path.set_mem_a_ready(0)

    path.set_frontend_d_ready(1)
    path.drive_mem_d(**response_fields)
    response = path.wait_frontend_d_fire(max_cycles=64)
    path.clear_mem_d()
    path.set_frontend_d_ready(0)
    return request, response


def test_api_MemBlock_frontend_bridge_icache_roundtrip_smoke(env):
    """icache TL 请求应穿过 frontendBridge，并把 mem-side D 响应返回给 frontend-side。"""

    env.reset(cycles=1, settle_cycles=0)
    path = env.frontend_bridge.icache
    address = 0x20004000
    response_data = int.from_bytes(bytes(range(32)), "little")

    request, response = _bridge_roundtrip(
        path,
        request_fields={
            "valid": 1,
            "bits_opcode": TL_A_GET,
            "bits_param": 0,
            "bits_size": 5,
            "bits_source": 0x3,
            "bits_address": address,
            "bits_mask": (1 << 32) - 1,
            "bits_data": 0,
            "bits_corrupt": 0,
            "bits_user_alias": 0x2,
            "bits_user_memBackType_MM": 1,
            "bits_user_reqSource": 0x11,
        },
        response_fields={
            "valid": 1,
            "bits_opcode": TL_D_ACCESS_ACK_DATA,
            "bits_param": 0,
            "bits_size": 5,
            "bits_source": 0x3,
            "bits_sink": 0,
            "bits_denied": 0,
            "bits_data": response_data,
            "bits_corrupt": 0,
        },
    )

    assert request["bits_opcode"] == TL_A_GET
    assert request["bits_size"] == 5
    assert request["bits_source"] == 0x3
    assert request["bits_address"] == address
    assert request["bits_user_alias"] == 0x2
    assert request["bits_user_memBackType_MM"] == 1
    assert request["bits_user_reqSource"] == 0x11
    assert response["bits_opcode"] == TL_D_ACCESS_ACK_DATA
    assert response["bits_source"] == 0x3
    assert response["bits_data"] == response_data


def test_api_MemBlock_frontend_bridge_instr_uncache_roundtrip_smoke(env):
    """instr_uncache TL 请求应穿桥到 mem-side，并把 mem-side D 响应返回给 frontend-side。"""

    env.reset(cycles=1, settle_cycles=0)
    path = env.frontend_bridge.instr_uncache
    address = 0x30002000
    response_data = 0x0123456789ABCDEF

    request, response = _bridge_roundtrip(
        path,
        request_fields={
            "valid": 1,
            "bits_opcode": TL_A_GET,
            "bits_param": 0,
            "bits_size": 3,
            "bits_source": 0x1,
            "bits_address": address,
            "bits_user_memPageType_NC": 1,
            "bits_user_memBackType_MM": 1,
            "bits_mask": 0xFF,
            "bits_data": 0,
            "bits_corrupt": 0,
        },
        response_fields={
            "valid": 1,
            "bits_opcode": TL_D_ACCESS_ACK_DATA,
            "bits_param": 0,
            "bits_size": 3,
            "bits_source": 0x1,
            "bits_sink": 0,
            "bits_denied": 0,
            "bits_data": response_data,
            "bits_corrupt": 0,
        },
    )

    assert request["bits_address"] == address
    assert request["bits_user_memPageType_NC"] == 1
    assert request["bits_user_memBackType_MM"] == 1
    assert response["bits_opcode"] == TL_D_ACCESS_ACK_DATA
    assert response["bits_source"] == 0x1
    assert response["bits_data"] == response_data


def test_api_MemBlock_frontend_bridge_icachectrl_roundtrip_smoke(env):
    """若当前 build 暴露 icachectrl 端口，frontendBridge 应支持最小控制面往返。"""

    env.reset(cycles=1, settle_cycles=0)
    path = env.frontend_bridge.icachectrl
    if not path.frontend_a.connected("valid"):
        pytest.skip("当前 build 未导出 frontend icachectrl 端口")

    address = 0x20
    response_data = 0x5A5A5A5A5A5A5A5A
    request, response = _bridge_roundtrip(
        path,
        request_fields={
            "valid": 1,
            "bits_opcode": TL_A_GET,
            "bits_param": 0,
            "bits_size": 3,
            "bits_source": 0x7,
            "bits_address": address,
            "bits_mask": 0xFF,
            "bits_data": 0,
            "bits_corrupt": 0,
        },
        response_fields={
            "valid": 1,
            "bits_opcode": TL_D_ACCESS_ACK_DATA,
            "bits_param": 0,
            "bits_size": 3,
            "bits_source": 0x7,
            "bits_sink": 0,
            "bits_denied": 0,
            "bits_data": response_data,
            "bits_corrupt": 0,
        },
    )

    assert request["bits_opcode"] == TL_A_GET
    assert request["bits_source"] == 0x7
    assert request["bits_address"] == address
    assert request["bits_mask"] == 0xFF
    assert response["bits_opcode"] == TL_D_ACCESS_ACK_DATA
    assert response["bits_source"] == 0x7
    assert response["bits_data"] == response_data
