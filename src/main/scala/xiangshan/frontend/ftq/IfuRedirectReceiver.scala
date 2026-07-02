// Copyright (c) 2024-2025 Beijing Institute of Open Source Chip (BOSC)
// Copyright (c) 2020-2025 Institute of Computing Technology, Chinese Academy of Sciences
// Copyright (c) 2020-2021 Peng Cheng Laboratory
//
// XiangShan is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// You may obtain a copy of Mulan PSL v2 at:
//          https://license.coscl.org.cn/MulanPSL2
//
// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
// EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
// MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
//
// See the Mulan PSL v2 for more details.

package xiangshan.frontend.ftq

import chisel3._
import chisel3.util._
import xiangshan.Redirect
import xiangshan.RedirectLevel
import xiangshan.Resolve
import xiangshan.frontend.FrontendRedirect

trait IfuRedirectReceiver extends HasFtqParameters {
  def receiveIfuRedirect(
      ifuRedirect:     Valid[FrontendRedirect],
      ifuResolve:      Valid[Resolve],
      specTopAddr:     UInt,
      backendRedirect: Bool
  ): (Valid[FtqPtr], Valid[Redirect], Valid[Resolve]) = {
    val redirect = WireInit(0.U.asTypeOf(Valid(new Redirect)))
    val resolve  = WireInit(0.U.asTypeOf(Valid(new Resolve)))

    redirect.valid          := ifuRedirect.valid && !backendRedirect
    redirect.bits.ftqIdx    := ifuRedirect.bits.ftqIdx
    redirect.bits.ftqOffset := ifuRedirect.bits.ftqOffset
    redirect.bits.level     := RedirectLevel.flushAfter
    redirect.bits.isRVC     := ifuRedirect.bits.isRVC
    redirect.bits.attribute := ifuRedirect.bits.attribute
    redirect.bits.pc        := ifuRedirect.bits.pc
    redirect.bits.target    := Mux(ifuRedirect.bits.attribute.isReturn, specTopAddr, ifuRedirect.bits.target)
    redirect.bits.taken     := ifuRedirect.bits.taken
    redirect.bits.isMisPred := true.B

    resolve := ifuResolve
    // override valid and target
    resolve.valid       := ifuResolve.valid && !backendRedirect
    resolve.bits.target := Mux(ifuResolve.bits.attribute.isReturn, specTopAddr, ifuResolve.bits.target.toUInt)

    // specTopAddr is read from metaQueue using ifuRedirect.bits.ftqIdx, here assert to prevent misuse
    assert(
      !ifuResolve.valid || ifuRedirect.bits.ftqIdx === ifuResolve.bits.ftqIdx,
      "ifuRedirect and ifuResolve should have the same ftqIdx to reuse specTopAddr"
    )

    val ftqIdx = Wire(Valid(new FtqPtr))
    ftqIdx.valid := redirect.valid
    ftqIdx.bits  := redirect.bits.ftqIdx

    (ftqIdx, RegNext(redirect), RegNext(resolve, init = 0.U.asTypeOf(Valid(new Resolve))))
  }
}
