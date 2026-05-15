/***************************************************************************************
* Copyright (c) 2020-2021 Institute of Computing Technology, Chinese Academy of Sciences
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

package xiangshan.cache

import org.chipsalliance.cde.config.Parameters
import chisel3._
import chisel3.util._
import freechips.rocketchip.subsystem._
import freechips.rocketchip.diplomacy._
import freechips.rocketchip.regmapper._
import freechips.rocketchip.tilelink._
import freechips.rocketchip.interrupts._
import freechips.rocketchip.util._
import chisel3.experimental.SourceInfo
import xiangshan._
import xiangshan.backend.datapath.NewPipelineConnect

case class L1CacheCtrlParams (
  address: AddressSet,
  beatBytes: Int = 8,
  tagMaskRegWidth: Int = 64,
  dataMaskRegWidth: Int = 64,
) {
  def maxBanks    = 1
  def bankBytes   = 128

  def regWidth    = 64
  def regBytes    = regWidth / 8
  def tagMaskRegBytes = tagMaskRegWidth / 8
  def dataMaskRegBytes = dataMaskRegWidth / 8

  def ctrlOffset  = 0x0
  def delayOffset = ctrlOffset + regBytes
  def tagMaskOffset = delayOffset + regBytes
  def dataMaskOffset = tagMaskOffset + ((tagMaskRegBytes + regBytes - 1) / regBytes) * regBytes

  def nSignalComps = 2
}

class CtrlUnitCtrlBundle(implicit p: Parameters) extends XSBundle with HasDCacheParameters {
  val zero0   = UInt((60-DCacheBanks).W)  // padding bits
  val bank    = UInt(DCacheBanks.W) // bank enable
  val comp    = UInt(1.W)   // components: 1'b0 tag, 1'b1 data
  val ede     = Bool()      // error delay enable
  val persist = Bool()  // persist inject
  val ese     = Bool()      // error signaling enable
}

class CtrlUnitTagSignalingBundle(implicit p: Parameters) extends XSBundle with HasDCacheParameters {
  val valid = Bool()
  val mask  = UInt(tagBits.W)
}

class CtrlUnitDataSignalingBundle(implicit p: Parameters) extends XSBundle with HasDCacheParameters {
  val valid = Bool()
  val mask  = UInt(DCacheSRAMRowBits.W)
}

class CtrlUnit(params: L1CacheCtrlParams)(implicit p: Parameters) extends LazyModule
  with HasDCacheParameters
{
  val device: SimpleDevice = new SimpleDevice("L1DCacheCtrl", Seq("xiangshan,l1dcache_ctrl"))

  val node: TLRegisterNode = TLRegisterNode(
    address     = Seq(params.address),
    device      = device,
    beatBytes   = params.beatBytes,
    concurrency = 1,
  )

  lazy val module = new CtrlUnitImp

  class CtrlUnitImp extends LazyModuleImp(this) {
    val io_tagPseudoError = IO(DecoupledIO(new CtrlUnitTagSignalingBundle))
    val io_dataPseudoError = IO(DecoupledIO(Vec(DCacheBanks, new CtrlUnitDataSignalingBundle)))

    require(params.maxBanks > 0, "At least one bank!")
    require(params.maxBanks == 1, "Is it necessary to have more than 1 bank?")
    require(params.nSignalComps == 2, "tag and data pseudo-error components are expected")
    require(params.regWidth == 64, "control register width must preserve the 64-bit control layout!")
    require(params.tagMaskRegWidth >= tagBits, "tag mask register width must cover tagBits!")
    require(params.tagMaskRegWidth % 8 == 0, "tag mask register width must be byte addressable!")
    require(params.dataMaskRegWidth == DCacheSRAMRowBits, "data mask register width must match DCacheSRAMRowBits!")
    require(params.dataMaskRegWidth % 8 == 0, "data mask register width must be byte addressable!")
    require(
      params.dataMaskOffset + params.dataMaskRegBytes * DCacheBanks <= params.bankBytes,
      "cache-control register map must cover tag and data masks within the MMIO bank!"
    )
    val ctrlRegs  = RegInit(VecInit(Seq.fill(1)(0.U(params.regWidth.W))))
    val delayRegs = RegInit(VecInit(Seq.fill(1)(0.U(params.regWidth.W))))
    val tagMaskReg = RegInit(0.U(params.tagMaskRegWidth.W))
    val dataMaskRegs = RegInit(VecInit(Seq.fill(DCacheBanks)(0.U(DCacheSRAMRowBits.W))))
    val counterRegs = RegInit(VecInit(Seq.fill(1)(0.U(params.regWidth.W))))
    val tagPseudoErrorGen = Wire(DecoupledIO(new CtrlUnitTagSignalingBundle))
    val dataPseudoErrorGen = Wire(DecoupledIO(Vec(DCacheBanks, new CtrlUnitDataSignalingBundle)))

    val ctrlReg = ctrlRegs.head
    val ctrlRegBundle = ctrlRegs.head.asTypeOf(new CtrlUnitCtrlBundle)
    val delayReg = delayRegs.head
    val counterReg = counterRegs.head
    require(
      log2Up(params.nSignalComps) == ctrlRegBundle.comp.getWidth,
      "comp width must cover pseudo-error components!"
    )

    tagPseudoErrorGen.valid :=
      ctrlRegBundle.ese && ctrlRegBundle.comp === 0.U && (!ctrlRegBundle.ede || counterReg === 0.U)
    tagPseudoErrorGen.bits.valid := ctrlRegBundle.bank(0)
    tagPseudoErrorGen.bits.mask := tagMaskReg(tagBits - 1, 0)

    dataPseudoErrorGen.valid :=
      ctrlRegBundle.ese && ctrlRegBundle.comp === 1.U && (!ctrlRegBundle.ede || counterReg === 0.U)
    dataPseudoErrorGen.bits.zip(ctrlRegBundle.bank.asBools).zip(dataMaskRegs).map {
      case ((bankOut, bankEnable), mask) =>
        bankOut.valid := bankEnable
        bankOut.mask  := mask
    }

    when(tagPseudoErrorGen.fire || dataPseudoErrorGen.fire) {
      val newCtrlReg = WireInit(0.U.asTypeOf(ctrlRegBundle))
      newCtrlReg := ctrlRegBundle
      newCtrlReg.ese := Mux(ctrlRegBundle.persist, ctrlRegBundle.ese, false.B)

      when(newCtrlReg.ese && newCtrlReg.ede) {
        counterReg := Mux(newCtrlReg.persist, delayReg, 0.U)
      }
      ctrlReg := newCtrlReg.asUInt
    }

    ctrlRegs.map(_.asTypeOf(new CtrlUnitCtrlBundle)).zip(counterRegs).zipWithIndex.foreach {
      case ((ctl, cnt), i) =>
        when(ctl.ese && ctl.ede && cnt =/= 0.U) {
          cnt := cnt - 1.U
        }
    }

    NewPipelineConnect(
      tagPseudoErrorGen, io_tagPseudoError, io_tagPseudoError.fire, false.B,
      Option("CtrlUnitTagPseudoErrorPipelineConnect")
    )
    NewPipelineConnect(
      dataPseudoErrorGen, io_dataPseudoError, io_dataPseudoError.fire, false.B,
      Option("CtrlUnitDataPseudoErrorPipelineConnect")
    )

    def ctrlRegDesc(i: Int) =
      RegFieldDesc(
        name      = s"control_$i",
        desc      = s"Acting control of controller $i",
        group     = Some(s"controll_${i}"),
        groupDesc = Some(s"Acting control of controller ${i}"),
        reset     = Some(0)
      )

    def delayRegDesc(i: Int) =
      RegFieldDesc(
        name      = s"delay_$i",
        desc      = s"pseudo error delay $i",
        group     = Some(s"delay_${i}"),
        groupDesc = Some(s"pseudo error delay ${i}"),
        reset     = Some(0)
      )

    def tagMaskRegDesc =
      RegFieldDesc(
        name      = "tag_mask",
        desc      = "pseudo error tag toggle mask",
        group     = Some("tag_mask"),
        groupDesc = Some("pseudo error tag toggle mask"),
        reset     = Some(0)
      )

    def dataMaskRegDesc(i: Int) =
      RegFieldDesc(
        name      = s"mask_$i",
        desc      = s"pseudo error toggle mask$i",
        group     = Some(s"mask_${i}"),
        groupDesc = Some(s"pseudo error toggle mask ${i}"),
        reset     = Some(0)
      )

    def ctrlRegField(x: UInt, i: Int) = {
      RegField(params.regWidth, x, ctrlRegDesc(i))
    }

    def delayRegField(x: UInt, i: Int) = {
      RegField(params.regWidth,
        RegReadFn { valid =>
          (true.B, x)
        },
        RegWriteFn { (valid, data) =>
          when(valid) { x := data; counterRegs(i) := data }
          true.B
        },
        delayRegDesc(i)
      )
    }

    def tagMaskRegField(x: UInt) = {
      RegField(params.tagMaskRegWidth, x, tagMaskRegDesc)
    }

    def dataMaskRegField(x: UInt, i: Int) = {
      RegField(params.dataMaskRegWidth, x, dataMaskRegDesc(i))
    }

    val ctrlRegFields = ctrlRegs.zipWithIndex.map {
      case (reg, i) =>
        params.ctrlOffset -> Seq(ctrlRegField(reg, i))
    }
    val delayRegFields = delayRegs.zipWithIndex.map {
      case (reg, i) =>
        params.delayOffset -> Seq(delayRegField(reg, i))
    }
    val tagMaskRegFields = Seq(params.tagMaskOffset -> Seq(tagMaskRegField(tagMaskReg)))
    val dataMaskRegFields = dataMaskRegs.zipWithIndex.map {
      case (reg, i) =>
        (params.dataMaskOffset + params.dataMaskRegBytes * i) -> Seq(dataMaskRegField(reg, i))
    }

    node.regmap((ctrlRegFields ++ delayRegFields ++ tagMaskRegFields ++ dataMaskRegFields):_*)
  }
}
