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

package xiangshan.mem.mdp.NewMdp

import chisel3._
import chisel3.util._
import utils._
import utility._
import xiangshan._
import org.chipsalliance.cde.config.Parameters
import utility.XSPerfAccumulate
import utility.sram.SRAMTemplate
import xiangshan.frontend.PrunedAddr
import xiangshan.frontend.PrunedAddrInit
import xiangshan.frontend.bpu.SaturateCounter
import xiangshan.frontend.bpu.StageCtrl
import xiangshan.frontend.bpu.HalfAlignHelper
import utility.sram.SRAMTemplate
import xiangshan.frontend.bpu.SaturateCounter
import xiangshan.frontend.bpu.WriteBuffer
import xiangshan.frontend.bpu.mbtb.MainBtbReplacer
import com.fasterxml.jackson.databind.deser.ValueInstantiators.Base
import xiangshan.frontend.bpu.CrossPageHelper
import xiangshan.frontend.bpu.BranchAttribute.RasAction.Pop


trait MdpBaseUtilHelper extends HasMdpBaseTableParameters{
  // implicit val p: Parameters = Parameters.empty // 提供一个默认的Parameters实例以满足HasXSParameter的要求

  val addrFields = AddrField(
    Seq(
      ("alignOffset", FetchBlockAlignWidth),
      ("alignBankIdx", BaseAlignBankIdxLen),
      ("internalBankIdx", BaseInternalBankIdxLen),
      ("setIdx", BaseSetIdxLen),
      ("tag", BaseTagWidth)
    ),
    maxWidth = Option(VAddrBits),
    extraFields = Seq(
      ("replacerSetIdx", FetchBlockSizeWidth, BaseSetIdxLen),
      ("targetLower", instOffsetBits, BaseTargetWidth),
      ("position", instOffsetBits, FetchBlockAlignWidth),
      ("cfiPosition", instOffsetBits, FetchBlockSizeWidth)
    )
  )

  def getSetIndex(pc: PrunedAddr): UInt =
    addrFields.extract("setIdx", pc)

  def getReplacerSetIndex(pc: PrunedAddr): UInt =
    addrFields.extract("replacerSetIdx", pc)

  def getAlignBankIndex(pc: PrunedAddr): UInt =
    addrFields.extract("alignBankIdx", pc)

  def getAlignBankIndexFromPosition(cfiPosition: UInt): UInt =
    addrFields.extractFrom("cfiPosition", "alignBankIdx", cfiPosition)

  def getTargetUpper(pc: PrunedAddr): UInt =
    pc(pc.length - 1, addrFields.getEnd("targetLower") + 1)

  def getTargetLowerBits(target: PrunedAddr): UInt =
    addrFields.extract("targetLower", target)

  def getInternalBankIndex(pc: PrunedAddr): UInt =
    addrFields.extract("internalBankIdx", pc)

  def getTag(pc: PrunedAddr): UInt =
    addrFields.extract("tag", pc)

  // detect multi-hit, return a mask indicating which way has multi-hit
  def detectMultiHit(hitMask: IndexedSeq[Bool], position: IndexedSeq[UInt]): UInt = {
    require(hitMask.length == position.length)
    require(hitMask.length >= 2)
    val multiHitMask = VecInit(Seq.fill(BaseNumWays)(false.B))
    for {
      i <- 0 until BaseNumWays
      j <- i + 1 until BaseNumWays
    } {
      val bothHit      = hitMask(i) && hitMask(j)
      val samePosition = position(i) === position(j)
      when(bothHit && samePosition) {
        multiHitMask(j) := true.B
      }
    }
    PriorityEncoderOH(multiHitMask.asUInt)
  }
}

class MainBtbInternalBank(
    alignIdx: Int,
    bankIdx:  Int
)(implicit p: Parameters) extends XSModule with HasMdpBaseTableParameters {
  class MainBtbInternalBankIO extends Bundle {
    class Read extends Bundle {
      class Req extends Bundle {
        val setIdx: UInt = UInt(BaseSetIdxLen.W)
      }
      class Resp extends Bundle {
        val entries:  Vec[BaseTableEntry]  = Vec(BaseNumWays, new BaseTableEntry)
        val counters: Vec[SaturateCounter] = Vec(BaseNumWays, TakenCounter())
      }

      val req:  Valid[Req] = Flipped(Valid(new Req))
      val resp: Resp       = Output(new Resp)
    }

    class WriteEntry extends Bundle {
      class Req extends Bundle {
        val setIdx:  UInt         = UInt(BaseSetIdxLen.W)
        val wayMask: UInt         = UInt(BaseNumWays.W)
        val entry:   BaseTableEntry = new BaseTableEntry
      }

      val req: Valid[Req] = Flipped(Valid(new Req))
    }

    class WriteCounter extends Bundle {
      class Req extends Bundle {
        val setIdx:   UInt                 = UInt(BaseSetIdxLen.W)
        val wayMask:  UInt                 = UInt(BaseNumWays.W)
        val counters: Vec[SaturateCounter] = Vec(BaseNumWays, TakenCounter())
      }

      val req: Valid[Req] = Flipped(Valid(new Req))
    }

    // flush interface for multi-hit
    class Flush extends Bundle {
      class Req extends Bundle {
        val setIdx:  UInt = UInt(BaseSetIdxLen.W)
        val wayMask: UInt = UInt(BaseNumWays.W)
      }

      val req: Valid[Req] = Flipped(Valid(new Req))
    }

    val resetDone: Bool = Output(Bool())

    val read:         Read         = new Read
    val writeEntry:   WriteEntry   = new WriteEntry
    val writeCounter: WriteCounter = new WriteCounter
    val flush:        Flush        = new Flush
  }

  val io: MainBtbInternalBankIO = IO(new MainBtbInternalBankIO)

  // alias
  private val read         = io.read
  private val writeEntry   = io.writeEntry
  private val writeCounter = io.writeCounter
  private val flush        = io.flush

  private val entrySrams = Seq.tabulate(BaseNumWays) { wayIdx =>
    Module(
      new SRAMTemplate(
        new BaseTableEntry,
        set = BaseNumSets,
        way = 1, // Not using way in the template, preparing for future skewed assoc
        singlePort = true,
        shouldReset = true,
        holdRead = true,
        withClockGate = true,
        hasMbist = hasMbist,
        hasSramCtl = hasSramCtl,
        suffix = Option("mdp_base_entry")
      )
    ).suggestName(s"mdp_sram_entry_align${alignIdx}_bank${bankIdx}_way${wayIdx}")
  }

  // we often need to update counter, but not the whole entry, so store counters in separate SRAMs for better power
  private val counterSram = Module(new SRAMTemplate(
    TakenCounter(),
    set = BaseNumSets,
    way = BaseNumWays,
    singlePort = true,
    shouldReset = true,
    holdRead = true,
    withClockGate = true,
    hasMbist = hasMbist,
    hasSramCtl = hasSramCtl,
    suffix = Option("mdp_base_counter")
  )).suggestName(s"mdp_sram_counter_align${alignIdx}_bank${bankIdx}")

  private val entryWriteBuffer = Module(new WriteBuffer(
    new BaseTableEntrySramWriteReq,
    numEntries = BaseWriteBufferSize,
    numPorts = BaseNumWays,
    nameSuffix = s"mbtbEntryAlign${alignIdx}_Bank${bankIdx}"
  ))

  private val counterWriteBuffer = Module(new Queue(
    new BaseTableCounterSramWriteReq,
    BaseWriteBufferSize,
    pipe = true,
    flow = true
  ))

  private val resetDone = RegInit(false.B)
  when(entrySrams.map(_.io.r.req.ready).reduce(_ && _) && counterSram.io.r.req.ready) {
    resetDone := true.B
  }
  io.resetDone := resetDone

  /* *** sram -> io *** */
  // handle entry & counter together
  (entrySrams :+ counterSram).foreach { sram =>
    sram.io.r.req.valid       := read.req.valid
    sram.io.r.req.bits.setIdx := read.req.bits.setIdx
  }
  // each entry sram template has 1 way, so here we only read data.head
  read.resp.entries  := VecInit(entrySrams.map(_.io.r.resp.data.head))
  read.resp.counters := counterSram.io.r.resp.data

  /* *** writeBuffer -> sram *** */
  // entry
  (entrySrams zip entryWriteBuffer.io.read).foreach { case (way, bufRead) =>
    way.io.w.req.valid        := bufRead.valid && !way.io.r.req.valid
    way.io.w.req.bits.data(0) := bufRead.bits.entry
    way.io.w.req.bits.setIdx  := bufRead.bits.setIdx
    bufRead.ready             := way.io.w.req.ready && !way.io.r.req.valid
  }
  // counter
  counterSram.io.w.req.valid            := counterWriteBuffer.io.deq.valid && !counterSram.io.r.req.valid
  counterSram.io.w.req.bits.data        := counterWriteBuffer.io.deq.bits.counters
  counterSram.io.w.req.bits.setIdx      := counterWriteBuffer.io.deq.bits.setIdx
  counterSram.io.w.req.bits.waymask.get := counterWriteBuffer.io.deq.bits.wayMask
  counterWriteBuffer.io.deq.ready       := counterSram.io.w.req.ready && !counterSram.io.r.req.valid

  /* *** io -> writeBuffer *** */
  // entry
  private val conflict =
    writeEntry.req.valid &&
      writeEntry.req.bits.setIdx === flush.req.bits.setIdx &&
      writeEntry.req.bits.entry.tag === 0.U

  entryWriteBuffer.io.write.zipWithIndex.foreach { case (bufWrite, i) =>
    val writeValid = writeEntry.req.valid && writeEntry.req.bits.wayMask(i)
    val flushValid = flush.req.valid && flush.req.bits.wayMask(i) && !conflict
    val valid      = writeValid || flushValid
    bufWrite.valid := RegNext(valid, false.B)
    bufWrite.bits.setIdx := RegEnable(
      Mux(
        writeValid,
        writeEntry.req.bits.setIdx,
        flush.req.bits.setIdx
      ),
      valid
    )
    bufWrite.bits.entry := RegEnable(
      Mux(
        writeValid,
        writeEntry.req.bits.entry,
        0.U.asTypeOf(new BaseTableEntry)
      ),
      valid
    )
  }
  // counter, dont care flush (`hit` is controlled by entry)
  counterWriteBuffer.io.enq.valid         := writeCounter.req.valid
  counterWriteBuffer.io.enq.bits.setIdx   := writeCounter.req.bits.setIdx
  counterWriteBuffer.io.enq.bits.wayMask  := writeCounter.req.bits.wayMask
  counterWriteBuffer.io.enq.bits.counters := writeCounter.req.bits.counters
}



class TageBaseTableAlignBank(
  alignIdx: Int
)(implicit p: Parameters) extends XSModule with HasMdpBaseTableParameters with HalfAlignHelper with MdpBaseUtilHelper{

  class TageBaseTableAlignBankIO(implicit p: Parameters) extends XSBundle with HasMdpBaseTableParameters{
    class Read extends Bundle {
      class Req extends Bundle {
        val startPc = new PrunedAddr(VAddrBits)
        val crossPage = Bool()
      }

      class Resp extends Bundle {
        val predictions = Vec(BaseNumWays, Valid(new MdpPrediction))
        val metas       = Vec(BaseNumWays, new MdpMetaEntry)
      }

      val req = Input(new Req)
      val resp = Output(new Resp)
    }
    class Write extends Bundle {
      class Req extends Bundle{
        val needWrite = Bool()
        val startPc = new PrunedAddr(VAddrBits)
        val loads   = Vec(ResolveEntryLoadNumbers, Valid(new LoadInfo))
        val metas   = Vec(BaseNumWays, new MdpMetaEntry)
        val mispredictInfo = Valid(new LoadInfo)
      }

      val req = Flipped(Valid(new Req))
    }

    val stageCtrl: StageCtrl = Input(new StageCtrl)
    val resetDone: Bool  = Output(Bool())
    val read:      Read  = new Read
    val write:     Write = new Write

    // final s3_takenMask (baseTable + tage), used to touch replacer accurately
    val s3_takenMask: Vec[Bool] = Input(Vec(BaseNumWays, Bool()))
  }
  val io = IO(new TageBaseTableAlignBankIO)
  // alias
  private val r = io.read
  private val w = io.write

  private val internalBanks = Seq.tabulate(BaseNumInternalBanks) { bankIdx =>
    Module(new MainBtbInternalBank(alignIdx, bankIdx))
  }

  private val replacer = Module(new MainBtbReplacer)

  io.resetDone := internalBanks.map(_.io.resetDone).reduce(_ && _)


  /* --------------------------------------------------------------------------------------------------------------
     stage 0
     - 
     -------------------------------------------------------------------------------------------------------------- */

  private val s0_fire             = io.stageCtrl.s0_fire
  private val s0_startPc          = r.req.startPc
  private val s0_crossPage        = r.req.crossPage
  private val s0_setIdx           = getSetIndex(s0_startPc)
  private val s0_internalBankIdx  = getInternalBankIndex(s0_startPc)
  private val s0_internalBankMask = UIntToOH(s0_internalBankIdx,BaseNumInternalBanks)
  private val s0_alignBankIdx     = getAlignBankIndex(s0_startPc)

  // mainBtb top is responsible for sending the correct startPc to alignBanks,
  // so here we should always see getAlignBankIndex(s0_startPc) == physical alignIdx.
  assert(!s0_fire || s0_alignBankIdx === alignIdx.U, "MainBtbAlignBank alignIdx mismatch")

  internalBanks.zipWithIndex.foreach { case (b, i) =>
    b.io.read.req.valid       := s0_fire && s0_internalBankMask(i)
    b.io.read.req.bits.setIdx := s0_setIdx
  }


  /* --------------------------------------------------------------------------------------------------------------
     stage 1
     - 
     -------------------------------------------------------------------------------------------------------------- */

  private val s1_fire             = io.stageCtrl.s1_fire
  private val s1_startPc          = RegEnable(s0_startPc, s0_fire)
  private val s1_crossPage        = RegEnable(s0_crossPage, s0_fire)
  private val s1_internalBankMask = RegEnable(s0_internalBankMask, s0_fire)
  private val s1_setIdx = getSetIndex(s1_startPc)
  private val s1_tag    = getTag(s1_startPc)

  private val s1_rawEntries = Mux1H(
    s1_internalBankMask,
    internalBanks.map(_.io.read.resp.entries)
  )
  private val s1_rawCounters = Mux1H(
    s1_internalBankMask,
    internalBanks.map(_.io.read.resp.counters)
  )
  // NOTE: when we calculate startPc in MainBtb top, we have selected whether lower bits should be masked
  //       (see s0_startPcVec)
  //       so here, if this alignBank is not the first alignBank of the fetch block, we'll get s2_alignedInstOffset = 0
  //       and, we'll do a (e.position >= 0) check later, which is always true
  private val s1_alignedInstOffset = getAlignedInstOffset(s1_startPc)

  // send resp
  (r.resp.predictions zip r.resp.metas zip s1_rawEntries zip s1_rawCounters).foreach { case (((pred, meta), e), c) =>
    // send rawHit for training
    val rawHit = e.valid && e.tag === s1_tag
    // filter out branches before alignedInstOffset
    // also filter out all entries if crossPage to satisfy Ifu/ICache's requirement
    val hit = rawHit && e.cfiPosition >= s1_alignedInstOffset && !s1_crossPage
    pred.valid            := hit
    pred.bits.static      := ~hit
    pred.bits.loadWait    := e.distance.orR
    pred.bits.distance    := e.distance
    pred.bits.cfiPosition := e.cfiPosition

    meta.rawHit     := rawHit
    meta.cfiPosition:= e.cfiPosition
    meta.counter    := c
  }

  // add an alias for hitMask for later use & debug purpose
  private val s1_hitMask = VecInit(r.resp.predictions.map(_.valid))
  dontTouch(s1_hitMask)

  /* --------------------------------------------------------------------------------------------------------------
     stage 2
     - 
     -------------------------------------------------------------------------------------------------------------- */

  private val s2_fire             = io.stageCtrl.s2_fire
  private val s2_startPc          = RegEnable(s1_startPc, s1_fire)

  /* --------------------------------------------------------------------------------------------------------------
    stage 3
    - 
    -------------------------------------------------------------------------------------------------------------- */

  //replacer
  private val s3_fire           = io.stageCtrl.s3_fire
  private val s3_replacerSetIdx = RegEnable(getReplacerSetIndex(s2_startPc), s2_fire)
  private val s3_takenMask      = io.s3_takenMask

  // touch taken entries only: not-taken conditional entries are considered not very useful and should be killed first
  replacer.io.predictTouch.valid        := s3_fire && s3_takenMask.reduce(_ || _)
  replacer.io.predictTouch.bits.setIdx  := s3_replacerSetIdx
  replacer.io.predictTouch.bits.wayMask := s3_takenMask.asUInt

  /* --------------------------------------------------------------------------------------------------------------
   train stage 1
   - 
   -------------------------------------------------------------------------------------------------------------- */
  private val t1_fire             = w.req.valid
  private val t1_needWrite        = w.req.bits.needWrite
  private val t1_startPc          = w.req.bits.startPc
  private val t1_loads            = w.req.bits.loads
  private val t1_meta             = w.req.bits.metas
  private val t1_mispredictInfo   = w.req.bits.mispredictInfo //TODO:换个变量名
  private val t1_setIdx           = getSetIndex(t1_startPc)
  private val t1_internalBankIdx  = getInternalBankIndex(t1_startPc)
  private val t1_internalBankMask = UIntToOH(t1_internalBankIdx, BaseNumInternalBanks)
  private val t1_alignBankIdx     = getAlignBankIndex(t1_startPc)

  /* *** update entry *** */
  // NOTE: the original rawHit result can be multi-hit (i.e. multiple rawHit && position match), so PriorityEncoderOH
  private val t1_hitMask = PriorityEncoderOH(VecInit(t1_meta.map(_.hit(t1_mispredictInfo.bits))).asUInt)
  private val t1_hit     = t1_hitMask.orR

  // Write entry only when there's a mispredict, and if:
  private val t1_entryNeedWrite = t1_mispredictInfo.valid && t1_needWrite && !t1_hit

  // Use hit wayMask if hit, else use replacer's victim way
  private val t1_entryWayMask = Mux(t1_hit, t1_hitMask, replacer.io.victim.wayMask)

  private val t1_entry = Wire(new BaseTableEntry)
  t1_entry.valid           := true.B
  t1_entry.tag             := getTag(t1_startPc)
  t1_entry.cfiPosition     := t1_mispredictInfo.bits.cfiPosition
  t1_entry.distance        := t1_mispredictInfo.bits.distance

  // similar to s0 case
  assert(!t1_fire || t1_alignBankIdx === alignIdx.U, "MdpBaseAlignBank alignIdx mismatch")

  internalBanks.zipWithIndex.foreach { case (b, i) =>
    b.io.writeEntry.req.valid        := t1_fire && t1_entryNeedWrite && t1_internalBankMask(i)
    b.io.writeEntry.req.bits.setIdx  := t1_setIdx
    b.io.writeEntry.req.bits.wayMask := t1_entryWayMask
    b.io.writeEntry.req.bits.entry   := t1_entry
  }

  // update replacer
  replacer.io.trainTouch.valid        := t1_fire && t1_entryNeedWrite
  replacer.io.trainTouch.bits.setIdx  := getReplacerSetIndex(t1_startPc)
  replacer.io.trainTouch.bits.wayMask := t1_entryWayMask
  when(t1_fire && t1_entryNeedWrite) {
    assert(PopCount(t1_entryWayMask) <= 1.U, "Replacer victim wayMask should be at-most-one-hot")
  }

  /* *** update counter *** */
  private val t1_newCounters    = Wire(Vec(BaseNumWays, UsefulCounter()))
  private val t1_counterWayMask = Wire(Vec(BaseNumWays, Bool()))

  t1_meta.zipWithIndex.foreach { case (meta, i) =>
    val hitMask = t1_loads.map { load=>
      load.valid && load.bits.updateType =/= MdpUpdateType.NULL && meta.cfiPosition === load.bits.cfiPosition
    }
    val counterUp = Mux1H(hitMask, t1_loads.map(_.bits.updateType === MdpUpdateType.M_IS)) 
    //M_IS -> N0 up  / M_AW or M_IW -> N0 down

    val entryOverridden = t1_entryNeedWrite && t1_entryWayMask(i)

    t1_counterWayMask(i) := entryOverridden || hitMask.reduce(_ || _)
    t1_newCounters(i)    := Mux(entryOverridden, UsefulCounter.InitStrong, meta.counter.getUpdate(counterUp))
  }

  // write counter anytime when needed
  private val t1_counterNeedWrite = t1_counterWayMask.reduce(_ || _)

  internalBanks.zipWithIndex.foreach { case (b, i) =>
    b.io.writeCounter.req.valid         := t1_fire && t1_counterNeedWrite && t1_internalBankMask(i)
    b.io.writeCounter.req.bits.setIdx   := t1_setIdx
    b.io.writeCounter.req.bits.wayMask  := t1_counterWayMask.asUInt
    b.io.writeCounter.req.bits.counters := t1_newCounters
  }

  /* *** multi-hit detection & flush *** */
  private val s1_multiHitMask = detectMultiHit(s1_hitMask, VecInit(s1_rawEntries.map(_.cfiPosition)))

  internalBanks.zipWithIndex.foreach { case (b, i) =>
    b.io.flush.req.valid        := s1_fire && s1_multiHitMask.orR && s1_internalBankMask(i)
    b.io.flush.req.bits.setIdx  := s1_setIdx
    b.io.flush.req.bits.wayMask := s1_multiHitMask
  }

}



class MdpTageBaseTable(implicit p: Parameters) extends XSModule with HasMdpBaseTableParameters with HalfAlignHelper with MdpBaseUtilHelper with CrossPageHelper{
  class MdpTageBaseTableIO extends Bundle{
    val stageCtrl    = Input(new StageCtrl)
    val startPc      = Input(PrunedAddr(VAddrBits))
    val train        = Input(new MdpTrain)
    val trainReady   = Output(Bool())

    val result       = Output(Vec(NumMdpResultEntries, Valid(new BasePrediction)))
    val meta         = Output(new MdpBaseMeta)
    
    val s3_takenMask = Input(Vec(NumMdpResultEntries, Bool()))
    val resetDone    = Output(Bool()) //TODO:
  }
  val io = IO(new MdpTageBaseTableIO)

  // print params
  private val alignBanks = Seq.tabulate(BaseNumAlignBanks) { alignIdx =>
    Module(new TageBaseTableAlignBank(alignIdx))
  }

  io.trainReady := true.B
  io.resetDone := true.B
  private val s0_fire, s1_fire, s2_fire, s3_fire = Wire(Bool())
  alignBanks.foreach { b =>
    b.io.stageCtrl.s0_fire := s0_fire
    b.io.stageCtrl.s1_fire := s1_fire
    b.io.stageCtrl.s2_fire := s2_fire
    b.io.stageCtrl.s3_fire := s3_fire
    // alignBank does not care t0, it's using t1 only
    b.io.stageCtrl.t0_fire := false.B
  }

  /* --------------------------------------------------------------------------------------------------------------
    stage 0
    - send read request to SRAM
    -------------------------------------------------------------------------------------------------------------- */
  s0_fire  := io.stageCtrl.s0_fire
  private val s0_startPc = io.startPc
  // rotate read addresses according to the first align bank index
  // e.g. if NumAlignBanks = 4, startPc locates in alignBank 1,
  // startPc + (i << FetchBlockAlignWidth) will be located in alignBank (1 + i) % 4,
  // i.e. we have VecInit.tabulate(...)'s alignBankIdx = (1, 2, 3, 0),
  // they always needs to goes to physical alignBank (0, 1, 2, 3),
  // so we need to rotate it right by 1.
  private val s0_rotator = VecRotate(getAlignBankIndex(s0_startPc))
  private val s0_startPcVec = s0_rotator.rotate(
    VecInit.tabulate(BaseNumAlignBanks) { i =>
      if (i == 0)
        s0_startPc // keep lower bits for the first one
      else
        getAlignedPc(s0_startPc + (i << FetchBlockAlignWidth).U) // use aligned for others
    }
  )

  alignBanks.zipWithIndex.foreach { case (b, i) =>
    b.io.read.req.startPc   := s0_startPcVec(i)
    b.io.read.req.crossPage := isCrossPage(s0_startPcVec(i), s0_startPc)
  }

  /* --------------------------------------------------------------------------------------------------------------
    stage 1
    - 
    -------------------------------------------------------------------------------------------------------------- */

  s1_fire := io.stageCtrl.s1_fire
  io.result := VecInit(alignBanks.flatMap(_.io.read.resp.predictions))
  io.meta.entries := VecInit(alignBanks.map(_.io.read.resp.metas)) //TODO:

  /* --------------------------------------------------------------------------------------------------------------
    stage 2
    - 
    -------------------------------------------------------------------------------------------------------------- */

  s2_fire := io.stageCtrl.s2_fire

  /* --------------------------------------------------------------------------------------------------------------
    stage 3
    - 
    -------------------------------------------------------------------------------------------------------------- */
  s3_fire := io.stageCtrl.s3_fire
  alignBanks.zipWithIndex.foreach { case (b, i) =>
    b.io.s3_takenMask := io.s3_takenMask.slice(i * BaseNumWays, (i + 1) * BaseNumWays)
  }
  
  /* --------------------------------------------------------------------------------------------------------------
   train stage 0
   - 
   -------------------------------------------------------------------------------------------------------------- */

  private val t0_fire = io.stageCtrl.t0_fire
  private val t0_train = io.train

  /* --------------------------------------------------------------------------------------------------------------
   train stage 1
   - 
   -------------------------------------------------------------------------------------------------------------- */

  private val t1_fire  = RegNext(t0_fire)
  private val t1_train = RegEnable(t0_train, t0_fire)

  private val t1_startPc  = t1_train.startPc
  private val t1_rotator  = VecRotate(getAlignBankIndex(t1_startPc))
  private val t1_startPcVec = t1_rotator.rotate(
    VecInit.tabulate(BaseNumAlignBanks)(i => getAlignedPc(t1_startPc + (i << FetchBlockAlignWidth).U))
  )
  private val t1_loads    = t1_train.loads
  private val t1_meta     = t1_train.meta.base
  private val t1_mispredictInfo = t1_train.mispredictLoad

  private val t1_writeAlignBankIdx  = getAlignBankIndexFromPosition(t1_mispredictInfo.bits.cfiPosition)
  private val t1_writeAlignBankMask = t1_rotator.rotate(VecInit(UIntToOH(t1_writeAlignBankIdx).asBools))

  alignBanks.zipWithIndex.foreach { case (b, i) =>
    b.io.write.req.valid          := t1_fire
    b.io.write.req.bits.needWrite := t1_writeAlignBankMask(i)
    b.io.write.req.bits.startPc   := t1_startPcVec(i)
    b.io.write.req.bits.loads     := t1_loads
    b.io.write.req.bits.metas     := t1_meta.entries(i)
    // see comments in MainBtbAlignBank.scala
    b.io.write.req.bits.mispredictInfo := t1_mispredictInfo
  }

  val mdpBaseTrainCnt = PopCount(t1_train.loads.map(v => v.valid && t1_fire))
  XSPerfAccumulate("mdpBaseTrainCnt", mdpBaseTrainCnt)
}



