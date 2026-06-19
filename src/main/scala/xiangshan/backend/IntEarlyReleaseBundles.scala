/***************************************************************************************
 * Copyright (c) 2024 Beijing Institute of Open Source Chip (BOSC)
 * Copyright (c) 2020-2024 Institute of Computing Technology, Chinese Academy of Sciences
 * Copyright (c) 2020-2021 Peng Cheng Laboratory
 *
 * XiangShan is licensed under Mulan PSL v2.
 * You can use this software according to the terms and conditions of the Mulan PSL v2.
 * You may obtain a copy of Mulan PSL v2 at:
 *          http://license.coscl.org.cn/MulanPSL2
 *
 * THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
 * EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
 * MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
 *
 * See the Mulan PSL v2 for more details.
 ***************************************************************************************/

package xiangshan.backend

import chisel3._
import chisel3.util._
import org.chipsalliance.cde.config.Parameters
import xiangshan._
import xiangshan.backend.rob.RobPtr

object IntERFallbackReason {
  val width = 4

  def none = 0.U(width.W)
  def unsupportedConsumer = 1.U(width.W)
  def unsupportedReadPath = 2.U(width.W)
  def counterSaturated = 3.U(width.W)
  def redirectKill = 4.U(width.W)
  def staleEvent = 5.U(width.W)
  def duplicateSource = 6.U(width.W)
  def invalidPdest = 7.U(width.W)
}

object IntERBundleHelper {
  def sourceIndexWidth(numSrc: Int): Int = log2Ceil(numSrc max 2)

  def logicalSrcVec(implicit p: Parameters): Vec[IntERSrcTrack] =
    Vec(p(XSCoreParamsKey).backendParams.numSrc, new IntERSrcTrack)

  def localSrcVec(n: Int)(implicit p: Parameters): Vec[IntERSrcTrack] =
    Vec(n, new IntERSrcTrack)

  def logicalRobSrcVec(implicit p: Parameters): Vec[IntERSrcRobState] =
    Vec(p(XSCoreParamsKey).backendParams.numSrc, new IntERSrcRobState)

  def localRobSrcVec(n: Int)(implicit p: Parameters): Vec[IntERSrcRobState] =
    Vec(n, new IntERSrcRobState)

  def logicalReadDoneSrcVec(implicit p: Parameters): Vec[IntERSrcReadDone] =
    Vec(p(XSCoreParamsKey).backendParams.numSrc, new IntERSrcReadDone)

  def localReadDoneSrcVec(n: Int)(implicit p: Parameters): Vec[IntERSrcReadDone] =
    Vec(n, new IntERSrcReadDone)
}

class IntERSrcTrack(implicit p: Parameters) extends XSBundle {
  val valid = Bool()
  val trackId = UInt(IntERTrackIdWidth.W)
  val trackGen = UInt(IntERTrackGenBits.W)
  val srcIdx = UInt(IntERSrcIdxWidth.W)
  val psrc = UInt(PhyRegIdxWidth.W)
}

class IntERSrcRobState(implicit p: Parameters) extends IntERSrcTrack {
  val readDone = Bool()
}

class IntERDestTrack(implicit p: Parameters) extends XSBundle {
  val valid = Bool()
  val trackId = UInt(IntERTrackIdWidth.W)
  val trackGen = UInt(IntERTrackGenBits.W)
  val pdest = UInt(PhyRegIdxWidth.W)
}

class IntERRedefTrack(implicit p: Parameters) extends XSBundle {
  val valid = Bool()
  val trackId = UInt(IntERTrackIdWidth.W)
  val trackGen = UInt(IntERTrackGenBits.W)
  val oldPdest = UInt(PhyRegIdxWidth.W)
}

class IntERUopMeta(implicit p: Parameters) extends XSBundle {
  val src = IntERBundleHelper.logicalSrcVec
  val dest = new IntERDestTrack
  val redef = new IntERRedefTrack
  val eligible = Bool()
}

class IntERSrcReadDone(implicit p: Parameters) extends XSBundle {
  val valid = Bool()
  val trackId = UInt(IntERTrackIdWidth.W)
  val trackGen = UInt(IntERTrackGenBits.W)
  val srcIdx = UInt(IntERSrcIdxWidth.W)
  val psrc = UInt(PhyRegIdxWidth.W)
}

class IntERSrcValueReadDone(implicit p: Parameters) extends XSBundle {
  val robIdx = new RobPtr
  val src = IntERBundleHelper.logicalReadDoneSrcVec
  val fallback = Bool()
  val reason = UInt(IntERFallbackReason.width.W)
}

class IntERSquashSource(implicit p: Parameters) extends XSBundle {
  val robIdx = new RobPtr
  val src = IntERBundleHelper.logicalSrcVec
}

class IntERProducerReady(implicit p: Parameters) extends XSBundle {
  val valid = Bool()
  val robIdx = new RobPtr
  val pdest = UInt(PhyRegIdxWidth.W)
}

class IntERSTGuardDec(implicit p: Parameters) extends XSBundle {
  val valid = Bool()
  val robIdx = new RobPtr
  val trackId = UInt(IntERTrackIdWidth.W)
  val trackGen = UInt(IntERTrackGenBits.W)
  val oldPdest = UInt(PhyRegIdxWidth.W)
  val fallback = Bool()
  val reason = UInt(IntERFallbackReason.width.W)
}

class IntEREarlyFreeReq(implicit p: Parameters) extends XSBundle {
  val valid = Bool()
  val pdest = UInt(PhyRegIdxWidth.W)
  val trackId = UInt(IntERTrackIdWidth.W)
  val trackGen = UInt(IntERTrackGenBits.W)
  val redefRobIdx = new RobPtr
}

class IntERCommitSuppress(implicit p: Parameters) extends XSBundle {
  val suppress = Bool()
  val oldPdest = UInt(PhyRegIdxWidth.W)
  val trackId = UInt(IntERTrackIdWidth.W)
  val trackGen = UInt(IntERTrackGenBits.W)
}
