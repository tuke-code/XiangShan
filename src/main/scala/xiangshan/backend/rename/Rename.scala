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

package xiangshan.backend.rename

import org.chipsalliance.cde.config.Parameters
import chisel3._
import chisel3.util._
import chisel3.util.experimental.decode.TruthTable
import utility._
import utils._
import xiangshan._
import xiangshan.TopDownCounters._
import xiangshan.backend.Bundles.{DecodeOutUop, RenameOutUop, connectSamePort}
import xiangshan.backend.decode.{FusionDecodeInfo, ImmUnion, Imm_Z, XSDebugDecode}
import xiangshan.backend.fu.FuType
import xiangshan.backend.{StoreBubbleReason, PipelineStallReason}
import xiangshan.backend.rename.freelist._
import xiangshan.backend.rob.{EarlyReleaseKilledRecoveryEvent, EarlyReleaseNonSpecRedefineEvent, EarlyReleaseRecoveryEvent, RobEarlyReleaseRedirectPolicy, RobEnqIO, RobPtr}
import xiangshan.mem.mdp._
import xiangshan.ExceptionNO._
import xiangshan.backend.fu.FuType._
import xiangshan.mem.{EewLog2, GenUSWholeEmul}
import xiangshan.mem.GenRealFlowNum
import xiangshan.backend.trace._
import xiangshan.backend.decode.isa.bitfield.{OPCODE5Bit, XSInstBitFields}
import xiangshan.backend.fu.NewCSR.CSROoORead
import yunsuan.{VfaluType, VipuType, VmoveType}
import xiangshan.backend.RatToVecExcpMod

class Rename(implicit p: Parameters) extends XSModule with HasCircularQueuePtrHelper with HasPerfEvents {

  // params alias
  private val numRegSrc = backendParams.numRegSrc
  private val numIntRegSrc = backendParams.numIntRegSrc
  private val numFpRegSrc  = backendParams.numFpRegSrc
  private val numVecRegSrc = backendParams.numVecRegSrc
  private val numIntRatPorts = numIntRegSrc
  private val numFpRatPorts  = numFpRegSrc
  private val numVecRatPorts = numVecRegSrc

  println(s"[Rename] numRegSrc: $numRegSrc")
  println(s"[Rename] numIntRegSrc: $numIntRegSrc")
  println(s"[Rename] numFpRegSrc: $numFpRegSrc")
  println(s"[Rename] numVecRegSrc: $numVecRegSrc")

  val io = IO(new Bundle() {
    val redirect = Flipped(ValidIO(new Redirect))
    val rabCommits = Input(new RabCommitIO)
    val vlCommits = Input(new VlCommitBundle(RabCommitWidth))
    // from csr
    val singleStep = Input(Bool())
    // from decode
    val in = Vec(RenameWidth, Flipped(DecoupledIO(new DecodeOutUop)))
    // valid vec not clear by fusion(used by compress)
    val validVec = Vec(RenameWidth, Input(Bool()))
    val isFusionVec = Vec(RenameWidth, Input(Bool()))
    val fusionCross2FtqVec = Vec(RenameWidth, Input(Bool()))
    val fusionInfo = Vec(DecodeWidth - 1, Flipped(new FusionDecodeInfo))
    // ssit read result
    val ssit = Flipped(Vec(RenameWidth, Output(new SSITEntry)))
    // waittable read result
    val waittable = Flipped(Vec(RenameWidth, Output(Bool())))
    val earlyReleaseReadComplete = Input(new EarlyReleaseReadComplete)
    val earlyReleaseNonSpecRedefine = Input(Vec(CommitWidth, Valid(new EarlyReleaseNonSpecRedefineEvent(log2Ceil(RobSize)))))
    val earlyReleaseCommit = Input(Vec(CommitWidth, Valid(UInt(log2Ceil(RobSize).W))))
    val earlyReleaseRecovery = Input(Vec(CommitWidth, Valid(new EarlyReleaseRecoveryEvent(log2Ceil(RobSize)))))
    val earlyReleaseKilledRecovery = Input(Vec(CommitWidth, Valid(new EarlyReleaseKilledRecoveryEvent(log2Ceil(RobSize)))))
    val earlyReleaseProducerWriteback = Flipped(MixedVec(backendParams.genWrite2RobBundles))
    val intReadPorts = Vec(RenameWidth, Vec(numIntRatPorts, new RatReadPort(log2Ceil(IntLogicRegs))))
    val intOldDestReadPorts = Vec(RenameWidth, new RatReadPort(log2Ceil(IntLogicRegs)))
    val fpReadPorts  = Vec(RenameWidth, Vec(numFpRatPorts,  new RatReadPort(log2Ceil(FpLogicRegs))))
    val vecReadPorts = Vec(RenameWidth, Vec(numVecRatPorts, new RatReadPort(log2Ceil(VecLogicRegs))))
    val v0ReadPorts  = Vec(RenameWidth, new RatReadPort(log2Ceil(V0LogicRegs)))
    val vlReadPorts  = Vec(RenameWidth, new RatReadPort(log2Ceil(VlLogicRegs)))
    // to dispatch1
    val out = Vec(RenameWidth, DecoupledIO(new RenameOutUop))
    // for snapshots
    val snpt = Input(new SnapshotPort)
    val snptLastEnq = Flipped(ValidIO(new RobPtr))
    val snptIsFull= Input(Bool())
    // for rat
    val hartId        = Input(UInt(8.W))
    val ratOldPdest      = Output(new RatToVecExcpMod)
    val ratDiffCommits   = Option.when(backendParams.basicDebugEn)(Input(new DiffCommitIO))
    val ratDiffVlCommits = Option.when(backendParams.basicDebugEn)(Input(new DiffVlCommitBundle(CommitWidth)))
    val diff_vl_rat      = Option.when(backendParams.basicDebugEn)(Output(Vec(1, UInt(PhyRegIdxWidth.W))))
    val ratSnpt = Input(new SnapshotPort)
    // perf only
    val debugDispatchAllFire = OptionWrapper(backendParams.debugEn, Input(Bool()))
    val debugOutValidVec = OptionWrapper(backendParams.debugEn, Vec(RenameWidth, Input(Bool())))
    val debugRobHeadFuType = Option.when(backendParams.debugEn)(Input(FuType()))
    val debugRobHeadStall = Option.when(backendParams.debugEn)(Input(Bool()))
    val debugLoadReason = Option.when(backendParams.debugEn)(Input(UInt(log2Ceil(TopDownCounters.NumStallReasons.id).W)))
    val stallReason = new Bundle {
      val in = Flipped(new StallReasonIO(RenameWidth))
      val out = new StallReasonIO(RenameWidth)
    }
  })

  io.in.zipWithIndex.map { case (o, i) =>
    o.bits.debug.foreach{ x =>
      PerfCCT.updateInstPos(x.debug_seqNum, PerfCCT.InstPos.AtRename.id.U, o.valid, clock, reset)
    }
  }

  // io alias
  private val dispatchCanAcc = io.out.head.ready

  val compressUnit = Module(new CompressUnit())
  // create free list and rat
  val intFreeList = Module(new MEFreeList(IntPhyRegs, RabCommitWidth))
  val fpFreeList = Module(new StdFreeList(FpPhyRegs - FpLogicRegs, FpLogicRegs, Reg_F, RabCommitWidth))
  val vecFreeList = Module(new StdFreeList(VfPhyRegs - VecLogicRegs, VecLogicRegs, Reg_V, RabCommitWidth, 31))
  val v0FreeList = Module(new StdFreeList(V0PhyRegs - V0LogicRegs, V0LogicRegs, Reg_V0, RabCommitWidth, 1))
  val vlFreeList = Module(new StdFreeList(VlPhyRegs - VlLogicRegs, VlLogicRegs, Reg_Vl, RabCommitWidth, 1))

  val rat = Module(new RenameTableWrapper)
  private val releaseOwnerWidth = EarlyReleaseOwner.width
  private val intEarlyReleaseTrackedPregs = backendParams.intEarlyReleaseTrackedPregs.min(IntPhyRegs)
  private val intEarlyReleaseLedgerEntries = backendParams.intEarlyReleaseLedgerEntries
  private val intEarlyReleaseCandidateWidth = 1
  private val intEarlyReleaseUctSourceWidth = 1
  private val intEarlyReleaseUctReadCompleteWidth = EarlyReleaseReadCompleteEvents.backendEventWidth
  private val intEarlyReleaseUctNonSpecRedefineWidth = CommitWidth
  private val intEarlyReleaseUctProducerWritebackWidth = backendParams.genWrite2RobBundles.length
  private val intEarlyReleaseRecoveryReadWidth = intEarlyReleaseUctReadCompleteWidth
  private val intEarlyReleaseRecoveryCancelWidth = CommitWidth * backendParams.numIntRegSrc
  val intUserCountTable = Module(new UserCountTable(
    entries = intEarlyReleaseTrackedPregs,
    ownerWidth = releaseOwnerWidth,
    pregWidthOverride = PhyRegIdxWidth,
    earlyFreeCandidateWidth = intEarlyReleaseCandidateWidth,
    countWidth = 3,
    allocWidth = RenameWidth,
    sourceUseWidth = intEarlyReleaseUctSourceWidth,
    sourceFallbackWidth = intEarlyReleaseUctSourceWidth,
    readCompleteWidth = intEarlyReleaseUctReadCompleteWidth,
    nonSpecRedefineWidth = intEarlyReleaseUctNonSpecRedefineWidth,
    clearWidth = RabCommitWidth + CommitWidth,
    cancelSourceUseWidth = intEarlyReleaseRecoveryCancelWidth,
    moveAliasWidth = RenameWidth,
    traditionalAliasRiskWidth = RabCommitWidth,
    producerWritebackWidth = intEarlyReleaseUctProducerWritebackWidth
  ))
  val intReleaseArbiter = Module(new EarlyReleaseReleaseArbiter(
    earlyWidth = intEarlyReleaseCandidateWidth,
    traditionalWidth = RabCommitWidth,
    outputWidth = RabCommitWidth,
    pregWidth = PhyRegIdxWidth,
    ownerWidth = releaseOwnerWidth,
    ledgerEntries = intEarlyReleaseLedgerEntries,
    killWidth = CommitWidth,
    allocWidth = RenameWidth,
    suppressionPregs = IntPhyRegs
  ))
  val earlyReleaseRecoveryTracker = Module(new EarlyReleaseRecoveryTracker(
    entries = RobSize,
    walkWidth = CommitWidth,
    commitWidth = CommitWidth,
    readCompleteWidth = intEarlyReleaseRecoveryReadWidth,
    cancelWidth = intEarlyReleaseRecoveryCancelWidth
  ))

  private def connectFirstValid[T <: Data](sink: ValidIO[T], sources: Seq[ValidIO[T]]): Unit = {
    val valids = sources.map(_.valid)
    sink.valid := VecInit(valids).asUInt.orR
    sink.bits := Mux(sink.valid, PriorityMux(valids, sources.map(_.bits)), 0.U.asTypeOf(sink.bits))
  }

  private def connectFirstNValid[T <: Data](sinks: Seq[ValidIO[T]], sources: Seq[ValidIO[T]]): Unit = {
    require(sinks.nonEmpty)

    for (sink <- sinks) {
      sink.valid := false.B
      sink.bits := 0.U.asTypeOf(sink.bits)
    }

    if (sources.nonEmpty) {
      val countWidth = log2Ceil(sources.length + 1).max(1)
      val validPrefixCount = Wire(Vec(sources.length + 1, UInt(countWidth.W)))
      validPrefixCount(0) := 0.U
      for (idx <- sources.indices) {
        validPrefixCount(idx + 1) := validPrefixCount(idx) + sources(idx).valid.asUInt
      }

      for (sourceIdx <- sources.indices) {
        for (sinkIdx <- sinks.indices) {
          when(sources(sourceIdx).valid && validPrefixCount(sourceIdx) === sinkIdx.U) {
            sinks(sinkIdx).valid := true.B
            sinks(sinkIdx).bits := sources(sourceIdx).bits
          }
        }
      }
    }
  }

  private def moreThanOne(valids: Seq[Bool]): Bool =
    PopCount(valids) > 1.U

  private def anyValid(valids: Seq[Bool]): Bool =
    if (valids.isEmpty) false.B else VecInit(valids).asUInt.orR

  private def oneHotOrZero(valids: Seq[Bool]): Bool =
    PopCount(valids) <= 1.U

  private def intEarlyReleasePregTrackable(preg: UInt): Bool =
    preg =/= 0.U

  val intRenamePorts = Wire(Vec(RenameWidth, new RatWritePort(log2Ceil(IntLogicRegs))))
  val fpRenamePorts  = Wire(Vec(RenameWidth, new RatWritePort(log2Ceil(FpLogicRegs))))
  val vecRenamePorts = Wire(Vec(RenameWidth, new RatWritePort(log2Ceil(VecLogicRegs))))
  val v0RenamePorts  = Wire(Vec(RenameWidth, new RatWritePort(log2Ceil(V0LogicRegs))))
  val vlRenamePorts  = Wire(Vec(RenameWidth, new RatWritePort(log2Ceil(VlLogicRegs))))

  val int_need_free = Wire(Vec(RabCommitWidth, Bool()))
  val int_old_pdest = Wire(Vec(RabCommitWidth, UInt(PhyRegIdxWidth.W)))
  val fp_old_pdest  = Wire(Vec(RabCommitWidth, UInt(PhyRegIdxWidth.W)))
  val vec_old_pdest = Wire(Vec(RabCommitWidth, UInt(PhyRegIdxWidth.W)))
  val v0_old_pdest  = Wire(Vec(RabCommitWidth, UInt(PhyRegIdxWidth.W)))
  val vl_old_pdest  = Wire(Vec(RabCommitWidth, UInt(PhyRegIdxWidth.W)))

  // debug arch ports
  val debug_int_rat = Option.when(backendParams.debugEn)(Wire(Vec(32, UInt(PhyRegIdxWidth.W))))
  val debug_fp_rat  = Option.when(backendParams.debugEn)(Wire(Vec(32, UInt(PhyRegIdxWidth.W))))
  val debug_vec_rat = Option.when(backendParams.debugEn)(Wire(Vec(31, UInt(PhyRegIdxWidth.W))))
  val debug_v0_rat  = Option.when(backendParams.debugEn)(Wire(Vec(1,  UInt(PhyRegIdxWidth.W))))
  val debug_vl_rat  = Option.when(backendParams.debugEn)(Wire(Vec(1,  UInt(PhyRegIdxWidth.W))))

  intFreeList.io.commit match {
    case commit =>
      commit.doCommit := io.rabCommits.isCommit
      commit.archAlloc := io.rabCommits.commitValid zip io.rabCommits.info map {
        case (valid, info) => valid && info.rfWen && !info.isMove
      }
  }
  intFreeList.io.debug_rat.foreach(_ := debug_int_rat.get)
  intFreeList.io.debugEarlyRelease := VecInit(Seq.fill(RabCommitWidth)(false.B))
  intFreeList.io.debugEarlyReleaseResolve := VecInit(Seq.fill(RabCommitWidth)(false.B))
  intFreeList.io.debugEarlyReleaseResolvePreg := VecInit(Seq.fill(RabCommitWidth)(0.U(PhyRegIdxWidth.W)))

  fpFreeList.io.commit match {
    case commit =>
      commit.doCommit := io.rabCommits.isCommit
      commit.archAlloc := io.rabCommits.commitValid zip io.rabCommits.info map {
        case (valid, info) =>
          valid && info.fpWen
      }
  }
  fpFreeList.io.debug_rat.foreach(_ := debug_fp_rat.get)
  fpFreeList.io.debugEarlyRelease := VecInit(Seq.fill(RabCommitWidth)(false.B))
  fpFreeList.io.debugEarlyReleaseResolve := VecInit(Seq.fill(RabCommitWidth)(false.B))
  fpFreeList.io.debugEarlyReleaseResolvePreg := VecInit(Seq.fill(RabCommitWidth)(0.U(PhyRegIdxWidth.W)))

  vecFreeList.io.commit match {
    case commit =>
      commit.doCommit := io.rabCommits.isCommit
      commit.archAlloc := io.rabCommits.commitValid zip io.rabCommits.info map {
        case (valid, info) =>
          valid && info.vecWen
      }
  }
  vecFreeList.io.debug_rat.foreach(_ := debug_vec_rat.get)
  vecFreeList.io.debugEarlyRelease := VecInit(Seq.fill(RabCommitWidth)(false.B))
  vecFreeList.io.debugEarlyReleaseResolve := VecInit(Seq.fill(RabCommitWidth)(false.B))
  vecFreeList.io.debugEarlyReleaseResolvePreg := VecInit(Seq.fill(RabCommitWidth)(0.U(PhyRegIdxWidth.W)))

  v0FreeList.io.commit match {
    case commit =>
      commit.doCommit := io.rabCommits.isCommit
      commit.archAlloc := io.rabCommits.commitValid zip io.rabCommits.info map {
        case (valid, info) =>
          valid && info.v0Wen
      }
  }
  v0FreeList.io.debug_rat.foreach(_ := debug_v0_rat.get)
  v0FreeList.io.debugEarlyRelease := VecInit(Seq.fill(RabCommitWidth)(false.B))
  v0FreeList.io.debugEarlyReleaseResolve := VecInit(Seq.fill(RabCommitWidth)(false.B))
  v0FreeList.io.debugEarlyReleaseResolvePreg := VecInit(Seq.fill(RabCommitWidth)(0.U(PhyRegIdxWidth.W)))

  vlFreeList.io.commit match {
    case commit =>
      commit.doCommit := io.vlCommits.isCommit
      commit.archAlloc := io.vlCommits.commitValid
  }
  vlFreeList.io.debug_rat.foreach(_ := debug_vl_rat.get)
  vlFreeList.io.debugEarlyRelease := VecInit(Seq.fill(RabCommitWidth)(false.B))
  vlFreeList.io.debugEarlyReleaseResolve := VecInit(Seq.fill(RabCommitWidth)(false.B))
  vlFreeList.io.debugEarlyReleaseResolvePreg := VecInit(Seq.fill(RabCommitWidth)(0.U(PhyRegIdxWidth.W)))

  val intReadPorts = rat.io.intReadPorts
  val intOldDestReadPorts = rat.io.intOldDestReadPorts
  val fpReadPorts  = rat.io.fpReadPorts
  val vecReadPorts = rat.io.vecReadPorts
  val v0ReadPorts  = rat.io.v0ReadPorts
  val vlReadPorts  = rat.io.vlReadPorts

  val intReadPortsData = VecInit(intReadPorts.map(x => VecInit(x.map(_.data))))
  val intOldDestReadPortsData = VecInit(intOldDestReadPorts.map(_.data))
  val fpReadPortsData  = VecInit(fpReadPorts.map(x => VecInit(x.map(_.data))))
  val vecReadPortsData = VecInit(vecReadPorts.map(x => VecInit(x.map(_.data))))
  val v0ReadPortsData  = VecInit(v0ReadPorts.map(x => VecInit(x.data)))
  val vlReadPortsData  = VecInit(vlReadPorts.map(x => VecInit(x.data)))

  io.intReadPorts <> intReadPorts
  io.intOldDestReadPorts <> intOldDestReadPorts
  io.fpReadPorts  <> fpReadPorts
  io.vecReadPorts <> vecReadPorts
  io.v0ReadPorts  <> v0ReadPorts
  io.vlReadPorts  <> vlReadPorts

  rat.io.snpt <> io.ratSnpt

  rat.io.intRenamePorts := intRenamePorts
  rat.io.fpRenamePorts  := fpRenamePorts
  rat.io.vecRenamePorts := vecRenamePorts
  rat.io.v0RenamePorts  := v0RenamePorts
  rat.io.vlRenamePorts  := vlRenamePorts

  rat.io.hartId := io.hartId
  rat.io.redirect := io.redirect.valid
  rat.io.rabCommits := io.rabCommits
  rat.io.vlCommits := io.vlCommits
  rat.io.diffCommits.foreach(_ := io.ratDiffCommits.get)
  rat.io.diffVlCommits.foreach(_ := io.ratDiffVlCommits.get)

  int_need_free := rat.io.int_need_free
  int_old_pdest := rat.io.int_old_pdest
  fp_old_pdest := rat.io.fp_old_pdest
  vec_old_pdest := rat.io.vec_old_pdest
  v0_old_pdest := rat.io.v0_old_pdest
  vl_old_pdest := rat.io.vl_old_pdest

  debug_int_rat.foreach(_ := rat.io.debug_int_rat.get)
  debug_fp_rat.foreach (_ := rat.io.debug_fp_rat.get)
  debug_vec_rat.foreach(_ := rat.io.debug_vec_rat.get)
  debug_v0_rat.foreach (_ := rat.io.debug_v0_rat.get)
  debug_vl_rat.foreach (_ := rat.io.debug_vl_rat.get)

  io.diff_vl_rat.foreach(_ := rat.io.diff_vl_rat.get)

  // T  : rat receive rabCommit
  // T+1: rat return oldPdest
  io.ratOldPdest match {
    case fromRat =>
      (0 until RabCommitWidth).foreach { idx =>
        val v0Valid = RegNext(
          rat.io.rabCommits.isCommit &&
          rat.io.rabCommits.isWalk &&
          rat.io.rabCommits.commitValid(idx) &&
          rat.io.rabCommits.info(idx).v0Wen
        )
        fromRat.v0OldVdPdest(idx).valid := RegNext(v0Valid)
        fromRat.v0OldVdPdest(idx).bits  := RegEnable(rat.io.v0_old_pdest(idx), v0Valid)
        val vecValid = RegNext(
          rat.io.rabCommits.isCommit &&
          rat.io.rabCommits.isWalk &&
          rat.io.rabCommits.commitValid(idx) &&
          rat.io.rabCommits.info(idx).vecWen
        )
        fromRat.vecOldVdPdest(idx).valid := RegNext(vecValid)
        fromRat.vecOldVdPdest(idx).bits  := RegEnable(rat.io.vec_old_pdest(idx), vecValid)
      }
  }

  // decide if given instruction needs allocating a new physical register (CfCtrl: from decode; RobCommitInfo: from rob)
  def needDestReg[T <: DecodeOutUop](reg_t: RegType, x: T): Bool = reg_t match {
    case Reg_I => x.rfWen
    case Reg_F => x.fpWen
    case Reg_V => x.vecWen
    case Reg_V0 => x.v0Wen
    case Reg_Vl => x.vlWen
  }
  def needDestRegCommit[T <: RabCommitInfo](reg_t: RegType, x: T): Bool = {
    reg_t match {
      case Reg_I => x.rfWen
      case Reg_F => x.fpWen
      case Reg_V => x.vecWen
      case Reg_V0 => x.v0Wen
    }
  }
  def needDestRegWalk[T <: RabCommitInfo](reg_t: RegType, x: T): Bool = {
    reg_t match {
      case Reg_I => x.rfWen
      case Reg_F => x.fpWen
      case Reg_V => x.vecWen
      case Reg_V0 => x.v0Wen
    }
  }

  // connect [redirect + walk] ports for fp & vec & int free list
  Seq(fpFreeList, vecFreeList, intFreeList, v0FreeList, vlFreeList).foreach { case fl =>
    fl.io.redirect := io.redirect.valid
    fl.io.walk := io.rabCommits.isWalk
  }
  // only when all free list and dispatch1 has enough space can we do allocation
  // when isWalk, freelist can definitely allocate
  intFreeList.io.doAllocate := fpFreeList.io.canAllocate && vecFreeList.io.canAllocate && v0FreeList.io.canAllocate && vlFreeList.io.canAllocate && dispatchCanAcc || io.rabCommits.isWalk
  fpFreeList.io.doAllocate := intFreeList.io.canAllocate && vecFreeList.io.canAllocate && v0FreeList.io.canAllocate && vlFreeList.io.canAllocate && dispatchCanAcc || io.rabCommits.isWalk
  vecFreeList.io.doAllocate := intFreeList.io.canAllocate && fpFreeList.io.canAllocate && v0FreeList.io.canAllocate && vlFreeList.io.canAllocate && dispatchCanAcc || io.rabCommits.isWalk
  v0FreeList.io.doAllocate := intFreeList.io.canAllocate && fpFreeList.io.canAllocate && vecFreeList.io.canAllocate && vlFreeList.io.canAllocate && dispatchCanAcc || io.rabCommits.isWalk
  vlFreeList.io.doAllocate := intFreeList.io.canAllocate && fpFreeList.io.canAllocate && vecFreeList.io.canAllocate && v0FreeList.io.canAllocate && dispatchCanAcc || io.rabCommits.isWalk

  //           dispatch1 ready ++ float point free list ready ++ int free list ready ++ vec free list ready     ++ not walk
  val canOut = dispatchCanAcc && fpFreeList.io.canAllocate && intFreeList.io.canAllocate && vecFreeList.io.canAllocate && v0FreeList.io.canAllocate && vlFreeList.io.canAllocate && !io.rabCommits.isWalk

  val isLastFtqVec = io.in.map(_.bits.isLastInFtqEntry)
  val isFusionVec = io.isFusionVec
  val canRobCompressVec = io.in.map(_.bits.canRobCompress)
  // count crossftq num in may same robentry
  val crossFtqNumVec = Wire(Vec(RenameWidth, Bool()))
  // identify cross odd ftqentry
  val oddFtqVec = Wire(Vec(RenameWidth, Bool()))
  val fusionValidVec = isFusionVec.zip(io.fusionCross2FtqVec).map { case (isFusion, cross2Ftq) => isFusion & !cross2Ftq }
    for (i <- 0 until RenameWidth) {
    if (i == 0) {
      crossFtqNumVec(i) := canRobCompressVec(i) && isLastFtqVec(i)
      oddFtqVec(i) := false.B
    } else {
      crossFtqNumVec(i) := (crossFtqNumVec(i - 1) ^ isLastFtqVec(i)) && canRobCompressVec(i)
      oddFtqVec(i) := crossFtqNumVec(i - 1) && isLastFtqVec(i)
    }
  }
  dontTouch(crossFtqNumVec)
  dontTouch(oddFtqVec)
  val isFusionPair = ((isFusionVec.asUInt << 1).asUInt | isFusionVec.asUInt)(RenameWidth-1, 0).asBools
  compressUnit.io.in.zip(io.in).zip(io.validVec.zip(isFusionPair)).foreach{ case((sink, source), (valid, isFusion)) =>
    sink.valid := valid && !io.singleStep
    sink.bits := source.bits
    sink.bits.canRobCompress := source.bits.canRobCompress && (backendParams.robCompressEn.B || isFusion)
  }
  compressUnit.io.oddFtqVec := oddFtqVec
  val needRobFlags = compressUnit.io.out.needRobFlags
  val instrSizesVec = compressUnit.io.out.instrSizes
  val compressMasksVec = compressUnit.io.out.masks

  // speculatively assign the instruction with an robIdx
  val validCount = PopCount(io.in.zip(needRobFlags).zip(io.validVec).map{ case((in, needRobFlag), valid) => valid && in.bits.lastUop && needRobFlag}) // number of instructions waiting to enter rob (from decode)
  val robIdxHead = RegInit(0.U.asTypeOf(new RobPtr))
  val lastCycleMisprediction = GatedValidRegNext(io.redirect.valid && !io.redirect.bits.flushItself())
  val robIdxHeadNext = Mux(io.redirect.valid, io.redirect.bits.robIdx, // redirect: move ptr to given rob index
         Mux(lastCycleMisprediction, robIdxHead + 1.U, // mis-predict: not flush robIdx itself
           Mux(canOut, robIdxHead + validCount, // instructions successfully entered next stage: increase robIdx
                      /* default */  robIdxHead))) // no instructions passed by this cycle: stick to old value
  robIdxHead := robIdxHeadNext
  val earlyReleaseOwnerHead = RegInit(0.U(EarlyReleaseOwner.width.W))
  val earlyReleaseOwnerForLane = Wire(Vec(RenameWidth, UInt(EarlyReleaseOwner.width.W)))
  for (i <- 0 until RenameWidth) {
    earlyReleaseOwnerForLane(i) := earlyReleaseOwnerHead +
      PopCount(io.in.zip(needRobFlags).zip(io.validVec).take(i).map {
        case ((in, needRobFlag), valid) => valid && in.bits.lastUop && needRobFlag
      })
  }
  when(canOut && !io.redirect.valid) {
    earlyReleaseOwnerHead := earlyReleaseOwnerHead + validCount
  }

  /**
    * Rename: allocate free physical register and update rename table
    */
  val uops = Wire(Vec(RenameWidth, new RenameOutUop))
  uops.zip(io.in.map(_.bits)).map{ case(uop, in) => {
    uop := 0.U.asTypeOf(uop)
    connectSamePort(uop, in)
    uop.debug.foreach({ debug =>
      connectSamePort(uop.debug.get, in.debug.get)
      debug.instr := in.instr
    })
  }}
  private val fuType       = uops.map(_.fuType)
  private val fuOpType     = uops.map(_.fuOpType)
  private val vtype        = uops.map(_.vpu.vtype)
  private val sew          = vtype.map(_.vsew)
  private val lmul         = vtype.map(_.vlmul)
  private val eew          = uops.map(_.vpu.veew)
  private val mop          = fuOpType.map(fuOpTypeItem => LSUOpType.getVecLSMop(fuOpTypeItem))
  private val isVlsType    = fuType.map(fuTypeItem => isVls(fuTypeItem))
  private val isSegment    = fuType.map(fuTypeItem => isVsegls(fuTypeItem))
  private val isUnitStride = fuOpType.map(fuOpTypeItem => LSUOpType.isAllUS(fuOpTypeItem))
  private val nf           = fuOpType.zip(uops.map(_.vpu.nf)).map { case (fuOpTypeItem, nfItem) => Mux(LSUOpType.isWhole(fuOpTypeItem), 0.U, nfItem) }
  private val mulBits      = 3 // dirty code
  private val emul         = fuOpType.zipWithIndex.map { case (fuOpTypeItem, index) =>
    Mux(
      LSUOpType.isWhole(fuOpTypeItem),
      GenUSWholeEmul(nf(index)),
      Mux(
        LSUOpType.isMasked(fuOpTypeItem),
        0.U(mulBits.W),
        EewLog2(eew(index)) - sew(index) + lmul(index)
      )
    )
  }
  private val isVecUnitType = isVlsType.zip(isUnitStride).map { case (isVlsTypeItme, isUnitStrideItem) =>
    isVlsTypeItme && isUnitStrideItem
  }
  private val isfofFixVlUop   = uops.map{x => x.vpu.isVleff && x.lastUop}
  private val instType = isSegment.zip(mop).map { case (isSegementItem, mopItem) => Cat(isSegementItem, mopItem) }
  // There is no way to calculate the 'flow' for 'unit-stride' exactly:
  //  Whether 'unit-stride' needs to be split can only be known after obtaining the address.
  // For scalar instructions, this is not handled here, and different assignments are done later according to the situation.
  private val numLsElem = instType.zipWithIndex.map { case (instTypeItem, index) =>
    Mux(
      isVecUnitType(index),
      VecMemUnitStrideMaxFlowNum.U,
      GenRealFlowNum(instTypeItem, emul(index), lmul(index), eew(index), sew(index))
    )
  }
  uops.zipWithIndex.map { case(u, i) =>
    u.numLsElem := Mux(io.in(i).valid & isVlsType(i) && !isfofFixVlUop(i), numLsElem(i), 0.U)
  }

  val needVecDest    = Wire(Vec(RenameWidth, Bool()))
  val needFpDest     = Wire(Vec(RenameWidth, Bool()))
  val needIntDest    = Wire(Vec(RenameWidth, Bool()))
  val needV0Dest     = Wire(Vec(RenameWidth, Bool()))
  val needVlDest     = Wire(Vec(RenameWidth, Bool()))
  private val inHeadValid = io.in.head.valid

  val isMove = Wire(Vec(RenameWidth, Bool()))
  isMove zip io.in.map(_.bits) foreach {
    case (move, in) => move := Mux(in.exceptionVec.asUInt.orR, false.B, in.isMove)
  }

  val walkNeedIntDest = WireDefault(VecInit(Seq.fill(RenameWidth)(false.B)))
  val walkNeedFpDest = WireDefault(VecInit(Seq.fill(RenameWidth)(false.B)))
  val walkNeedVecDest = WireDefault(VecInit(Seq.fill(RenameWidth)(false.B)))
  val walkNeedV0Dest = WireDefault(VecInit(Seq.fill(RenameWidth)(false.B)))
  val walkNeedVlDest = WireDefault(VecInit(Seq.fill(RenameWidth)(false.B)))
  val walkIsMove = WireDefault(VecInit(Seq.fill(RenameWidth)(false.B)))

  val intSpecWen = Wire(Vec(RenameWidth, Bool()))
  val fpSpecWen  = Wire(Vec(RenameWidth, Bool()))
  val vecSpecWen = Wire(Vec(RenameWidth, Bool()))
  val v0SpecWen = Wire(Vec(RenameWidth, Bool()))
  val vlSpecWen = Wire(Vec(RenameWidth, Bool()))

  val walkIntSpecWen = WireDefault(VecInit(Seq.fill(RenameWidth)(false.B)))

  val walkPdest = Wire(Vec(RenameWidth, UInt(PhyRegIdxWidth.W)))
  val renameTimeOldIntPdest = Wire(Vec(RenameWidth, UInt(PhyRegIdxWidth.W)))

  val instrSize = Wire(Vec(RenameWidth, UInt((log2Ceil(RenameWidth + 1)).W)))

  // uop calculation
  for (i <- 0 until RenameWidth) {
    (uops(i): Data).waiveAll :<= (io.in(i).bits: Data).waiveAll

    // update cf according to ssit result
    uops(i).storeSetHit := io.ssit(i).valid
    uops(i).loadWaitStrict := io.ssit(i).strict && io.ssit(i).valid
    uops(i).ssid := io.ssit(i).ssid

    // update cf according to waittable result
    uops(i).loadWaitBit := io.waittable(i)
    uops(i).crossFtq := false.B
    uops(i).crossFtqCommit := 0.U
    uops(i).ftqLastOffset := io.in(i).bits.ftqOffset
    uops(i).lastIsRVC := io.in(i).bits.isRVC
    // alloc a new phy reg
    needV0Dest(i) := io.in(i).valid && needDestReg(Reg_V0, io.in(i).bits)
    needVlDest(i) := io.in(i).valid && needDestReg(Reg_Vl, io.in(i).bits)
    needVecDest(i) := io.in(i).valid && needDestReg(Reg_V, io.in(i).bits)
    needFpDest(i) := io.in(i).valid && needDestReg(Reg_F, io.in(i).bits)
    needIntDest(i) := io.in(i).valid && needDestReg(Reg_I, io.in(i).bits)
    if (i < RabCommitWidth) {
      walkNeedIntDest(i) := io.rabCommits.walkValid(i) && needDestRegWalk(Reg_I, io.rabCommits.info(i))
      walkNeedFpDest(i) := io.rabCommits.walkValid(i) && needDestRegWalk(Reg_F, io.rabCommits.info(i))
      walkNeedVecDest(i) := io.rabCommits.walkValid(i) && needDestRegWalk(Reg_V, io.rabCommits.info(i))
      walkNeedV0Dest(i) := io.rabCommits.walkValid(i) && needDestRegWalk(Reg_V0, io.rabCommits.info(i))
      // Need no vlwen here, since walkValid only assert when there are some vl regs needed to walk.
      walkNeedVlDest(i) := io.vlCommits.walkValid(i)
      walkIsMove(i) := io.rabCommits.info(i).isMove
    }
    fpFreeList.io.allocateReq(i) := needFpDest(i)
    fpFreeList.io.walkReq(i) := walkNeedFpDest(i)
    vecFreeList.io.allocateReq(i) := needVecDest(i)
    vecFreeList.io.walkReq(i) := walkNeedVecDest(i)
    v0FreeList.io.allocateReq(i) := needV0Dest(i)
    v0FreeList.io.walkReq(i) := walkNeedV0Dest(i)
    vlFreeList.io.allocateReq(i) := needVlDest(i)
    vlFreeList.io.walkReq(i) := walkNeedVlDest(i)
    intFreeList.io.allocateReq(i) := needIntDest(i) && !isMove(i)
    intFreeList.io.walkReq(i) := walkNeedIntDest(i) && !walkIsMove(i)

    // no valid instruction from decode stage || all resources (dispatch1 + both free lists) ready
    io.in(i).ready := !io.in(0).valid || canOut

    uops(i).robIdx := robIdxHead + PopCount(io.in.zip(needRobFlags).zip(io.validVec).take(i).map{ case((in, needRobFlag), valid) => valid && in.bits.lastUop && needRobFlag})
    instrSize(i) := instrSizesVec(i) + io.fusionCross2FtqVec(i)
    uops(i).debug.foreach(_.fusionNum := PopCount(compressMasksVec(i) & Cat(io.isFusionVec.reverse)))
    val hasExceptionExceptFlushPipe = Cat(selectFrontend(uops(i).exceptionVec) :+ uops(i).exceptionVec(illegalInstr) :+ uops(i).exceptionVec(virtualInstr)).orR || TriggerAction.isDmode(uops(i).trigger)
    when(isMove(i) || hasExceptionExceptFlushPipe) {
      uops(i).numWB := 0.U
    }
    if (i > 0) {
      when(!needRobFlags(i - 1)) {
        val numFusion = PopCount(compressMasksVec(i) & (Cat(isMove.reverse) | Cat(fusionValidVec.reverse)))
        val numuops = instrSizesVec(i) - numFusion
        dontTouch(numFusion)
        dontTouch(numuops)
        uops(i).firstUop := false.B
        uops(i).ftqPtr := uops(i - 1).ftqPtr
        uops(i).ftqOffset := uops(i - 1).ftqOffset
        // rob need first uop isrvc, as it may attach interrupt to first uop(calculate pc)
        // branch need last uop isrvc, it will change in dispatch
        uops(i).isRVC := uops(i - 1).isRVC
        uops(i).numWB := instrSizesVec(i) - PopCount(compressMasksVec(i) & (Cat(isMove.reverse) | Cat(fusionValidVec.reverse)))
      }
    }
    when(!needRobFlags(i)) {
      uops(i).lastUop := false.B
      uops(i).numWB := instrSizesVec(i) - PopCount(compressMasksVec(i) & (Cat(isMove.reverse) | Cat(fusionValidVec.reverse)))
      if (i < RenameWidth - 1) {
        uops(i).crossFtqCommit := uops(i + 1).crossFtqCommit
        uops(i).crossFtq := uops(i + 1).crossFtq
      }
    }.elsewhen(needRobFlags(i)) {
      uops(i).crossFtqCommit := PopCount(compressMasksVec(i) & Cat(isLastFtqVec.reverse))
      uops(i).crossFtq := uops(i).crossFtqCommit(1) || (uops(i).crossFtqCommit(0) && !isLastFtqVec(i))
    }
    if (i < RenameWidth - 1){
      when(!needRobFlags(i)) {
        uops(i).commitType := uops(i + 1).commitType
      }
    }
    uops(i).wfflags := (compressMasksVec(i) & Cat(io.in.map(_.bits.wfflags).reverse)).orR
    uops(i).dirtyFs := (compressMasksVec(i) & Cat(io.in.map(_.bits.fpWen).reverse)).orR
    uops(i).dirtyVs := (
      compressMasksVec(i) & Cat(io.in.map(in =>
        // vector instructions' uopSplitType cannot be UopSplitType.SCA_SIM
        in.bits.uopSplitType =/= UopSplitType.SCA_SIM &&
        !UopSplitType.isAMOCAS(in.bits.uopSplitType) &&
        // vfmv.f.s, vcpop.m, vfirst.m and vmv.x.s don't change vector state
        !Seq(
          (FuType.vmove, VmoveType.vfmv_f_s), // vfmv.f.s
          (FuType.vipu, VipuType.vcpop_m),    // vcpop.m
          (FuType.vipu, VipuType.vfirst_m),   // vfirst.m
          (FuType.vmove, VmoveType.vmv_x_s)     // vmv.x.s
        ).map(x => FuTypeOrR(in.bits.fuType, x._1) && in.bits.fuOpType === x._2).reduce(_ || _)
      ).reverse)
    ).orR
    uops(i).debug.foreach(_.debug_sim_trig := (compressMasksVec(i) & Cat(io.in.map(_.bits.instr === XSDebugDecode.SIM_TRIG).reverse)).orR)
    // psrc0,psrc1,psrc2 don't require v0ReadPorts because their srcType can distinguish whether they are V0 or not
    uops(i).psrc(0) := Mux1H(uops(i).srcType(0)(2, 0), Seq(intReadPortsData(i)(0), fpReadPortsData(i)(0), vecReadPortsData(i)(0)))
    uops(i).psrc(1) := Mux1H(uops(i).srcType(1)(2, 0), Seq(intReadPortsData(i)(1), fpReadPortsData(i)(1), vecReadPortsData(i)(1)))
    uops(i).psrc(2) := Mux1H(uops(i).srcType(2)(2, 1), Seq(fpReadPortsData(i)(2), vecReadPortsData(i)(2)))
    uops(i).psrc(3) := v0ReadPortsData(i)(0)
    uops(i).psrcVl := vlReadPortsData(i).head
    uops(i).psrcIntForMove := intReadPortsData(i).head

    // int psrc2 should be bypassed from next instruction if it is fused
    if (i < RenameWidth - 1) {
      when (io.fusionInfo(i).rs2FromRs2 || io.fusionInfo(i).rs2FromRs1) {
        uops(i).psrc(1) := Mux(io.fusionInfo(i).rs2FromRs2, intReadPortsData(i + 1)(1), intReadPortsData(i + 1)(0))
      }.elsewhen(io.fusionInfo(i).rs2FromZero) {
        uops(i).psrc(1) := 0.U
      }
    }
    uops(i).isMove := isMove(i)

    // update pdest
    uops(i).pdest := MuxCase(0.U, Seq(
      needIntDest(i)    ->  intFreeList.io.allocatePhyReg(i),
      needFpDest(i)     ->  fpFreeList.io.allocatePhyReg(i),
      needVecDest(i)    ->  vecFreeList.io.allocatePhyReg(i),
      needV0Dest(i)    ->  v0FreeList.io.allocatePhyReg(i),
    ))

    uops(i).pdestVl := vlFreeList.io.allocatePhyReg(i)

    // Assign performance counters
    uops(i).debug.foreach(_.perfDebugInfo.renameTime := GTimer())

    io.out(i).valid := io.in(i).valid && intFreeList.io.canAllocate && fpFreeList.io.canAllocate && vecFreeList.io.canAllocate && v0FreeList.io.canAllocate && vlFreeList.io.canAllocate && !io.rabCommits.isWalk
    io.out(i).bits := uops(i)
    // dirty code
    if (i == 0) {
      io.out(i).bits.psrc(0) := Mux(io.out(i).bits.isLUI, 0.U, uops(i).psrc(0))
      io.out(i).bits.psrcIntForMove := Mux(io.out(i).bits.isLUI, 0.U, uops(i).psrcIntForMove)
    }
    // Todo: move these shit in decode stage
    // dirty code for fence. The lsrc is passed by imm.
    when (io.out(i).bits.fuType === FuType.fence.U) {
      io.out(i).bits.imm := Cat(io.in(i).bits.lsrc(1), io.in(i).bits.lsrc(0))
    }

    // dirty code for SoftPrefetch (prefetch.r/prefetch.w)
//    when (io.in(i).bits.isSoftPrefetch) {
//      io.out(i).bits.fuType := FuType.ldu.U
//      io.out(i).bits.fuOpType := Mux(io.in(i).bits.lsrc(1) === 1.U, LSUOpType.prefetch_r, LSUOpType.prefetch_w)
//      io.out(i).bits.selImm := SelImm.IMM_S
//      io.out(i).bits.imm := Cat(io.in(i).bits.imm(io.in(i).bits.imm.getWidth - 1, 5), 0.U(5.W))
//    }

    // write speculative rename table
    // we update rat later inside commit code
    intSpecWen(i) := needIntDest(i) && intFreeList.io.canAllocate && intFreeList.io.doAllocate && !io.rabCommits.isWalk && !io.redirect.valid
    fpSpecWen(i)  := needFpDest(i)  && fpFreeList.io.canAllocate  && fpFreeList.io.doAllocate  && !io.rabCommits.isWalk && !io.redirect.valid
    vecSpecWen(i) := needVecDest(i) && vecFreeList.io.canAllocate && vecFreeList.io.doAllocate && !io.rabCommits.isWalk && !io.redirect.valid
    v0SpecWen(i) := needV0Dest(i) && v0FreeList.io.canAllocate && v0FreeList.io.doAllocate && !io.rabCommits.isWalk && !io.redirect.valid
    vlSpecWen(i) := needVlDest(i) && vlFreeList.io.canAllocate && vlFreeList.io.doAllocate && !io.rabCommits.isWalk && !io.redirect.valid


    if (i < RabCommitWidth) {
      walkIntSpecWen(i) := walkNeedIntDest(i) && !io.redirect.valid
      walkPdest(i) := io.rabCommits.info(i).pdest
    } else {
      walkPdest(i) := io.out(i).bits.pdest
    }
  }

  /**
   * trace begin
   */
  val inVec = io.in.map(_.bits)
  val isRVCVec = inVec.map(_.isRVC)
  val nonRVCNumVec = (0 until RenameWidth).map{
    i => compressMasksVec(i).asBools.zip(isRVCVec).map{
      case (mask, isRVC) => (mask && !isRVC).asUInt
    }
  }

  /*
  encode: instrNum, nonRVCNum => commitinfo.iretire
  (instrNum((log2Ceil(RenameWidth + 1)).W), nonRVCNum(IretireWidthInPipe.W), encode(IretireWidthEncode.W))
  val instrSizeTable = Seq(
    (1, 0, 1), (1, 1, 2),
    (2, 0, 3), (2, 1, 4), (2, 2, 5),
    (3, 0, 6), (3, 1, 7), (3, 2, 8), (3, 3, 9),
    (4, 0, 10), (4, 1, 11), (4, 2, 12), (4, 3, 13), (4, 4, 14),
    (5, 0, 15), (5, 1, 16), (5, 2, 17), (5, 3, 18), (5, 4, 19), (5, 5, 20),
    (6, 0, 21), (6, 1, 22), (6, 2, 23), (6, 3, 24), (6, 4, 25), (6, 5, 26), (6, 6, 27),
  )
   */

  val instrSizeTable = (1 to RenameWidth).map{ instrNum =>
    (0 to instrNum).map( nonRVCNum => (instrNum, nonRVCNum) )
  }.flatten

  for (i <- 0 until RenameWidth) {
    // iretire
    val nonRVCNum = Wire(UInt((log2Ceil(RenameWidth + 1).W)))
    nonRVCNum := nonRVCNumVec(i).reduce(_ +& _)
    uops(i).traceBlockInPipe.iretire := chisel3.util.experimental.decode.decoder(
      (instrSize(i) ## nonRVCNum),
      TruthTable(
        instrSizeTable.zipWithIndex.map { case (table, encode) =>
          (BitPat(((table._1 << log2Ceil(RenameWidth + 1)) + table._2).U((2 * log2Ceil(RenameWidth + 1)).W)),
            BitPat((encode + 1).U(IretireWidthEncoded.W)))
        },
        BitPat.N(IretireWidthEncoded)
      )
    )
    // ilastsize
    val lastIsRVC = isRVCVec(i)
    when (!needRobFlags(i)) {
      if (i + 1 < RenameWidth) {
        uops(i).traceBlockInPipe.ilastsize := uops(i + 1).traceBlockInPipe.ilastsize
        uops(i).traceBlockInPipe.itype := uops(i + 1).traceBlockInPipe.itype
      }
    }.elsewhen(needRobFlags(i)) {
      uops(i).traceBlockInPipe.ilastsize := Mux(lastIsRVC, Ilastsize.HalfWord, Ilastsize.Word)

      // CSR systemop instruction excluding ebreak & ecall
      val csrAddr = Imm_Z().getCSRAddr(uops(i).imm(Imm_Z().len - 1, 0))
      val isXret = FuType.isCsr(uops(i).fuType) && CSROpType.isSystemOp(uops(i).fuOpType) && (csrAddr(11, 1).orR)
      uops(i).traceBlockInPipe.itype := Mux(
        isXret,
        Itype.ExpIntReturn,
        Itype.jumpTypeGen(inVec(i).fuType, inVec(i).fuOpType, inVec(i).ldest.asTypeOf(new OpRegType), inVec(i).lsrc(0).asTypeOf(new OpRegType))
      )
    }
  }
  /**
   * trace end
   */

  /**
    * How to set psrc:
    * - bypass the pdest to psrc if previous instructions write to the same ldest as lsrc
    * - default: psrc from RAT
    * How to set pdest:
    * - Mux(isMove, psrc, pdest_from_freelist).
    *
    * The critical path of rename lies here:
    * When move elimination is enabled, we need to update the rat with psrc.
    * However, psrc maybe comes from previous instructions' pdest, which comes from freelist.
    *
    * If we expand these logic for pdest(N):
    * pdest(N) = Mux(isMove(N), psrc(N), freelist_out(N))
    *          = Mux(isMove(N), Mux(bypass(N, N - 1), pdest(N - 1),
    *                           Mux(bypass(N, N - 2), pdest(N - 2),
    *                           ...
    *                           Mux(bypass(N, 0),     pdest(0),
    *                                                 rat_out(N))...)),
    *                           freelist_out(N))
    */
  // a simple functional model for now
  io.out(0).bits.pdest := Mux(isMove(0), uops(0).psrc.head, uops(0).pdest)
  renameTimeOldIntPdest(0) := intOldDestReadPortsData(0)

  // psrc(n) + pdest(1)
  // bypassCond(j)(i)(k): src(j) of uop(i) depends on dest of uop(k)
  val bypassCond: Vec[MixedVec[UInt]] = Wire(Vec(numRegSrc, MixedVec(List.tabulate(RenameWidth-1)(i => UInt((i+1).W)))))
  // bypassCondVl(i)(k): vl of uop(i) depends on dest of uop(k)
  val bypassCondVl: MixedVec[UInt] = Wire(MixedVec(Seq.tabulate(RenameWidth - 1)(i => UInt((i + 1).W))))
  require(io.in(0).bits.srcType.size == io.in(0).bits.numSrc)
  private val pdestLoc = io.in.head.bits.srcType.size // 2 vector src: v0, vl&vtype
  println(s"[Rename] idx of pdest in bypassCond $pdestLoc")
  for (i <- 1 until RenameWidth) {
    val v0Cond = io.in(i).bits.srcType.zipWithIndex.map{ case (s, i) =>
      if (i == 3) (s === SrcType.vp) || (s === SrcType.v0)
      else false.B
    }
    val vlCond = io.in(i).bits.vlRen
    val vecCond = io.in(i).bits.srcType.map(_ === SrcType.vp)
    val fpCond  = io.in(i).bits.srcType.map(_ === SrcType.fp)
    val intCond = io.in(i).bits.srcType.map(_ === SrcType.xp)
    val lsrcVec = io.in(i).bits.lsrc

    for ((
      (intRen, fpRen, vecRen, v0Ren),
      (lsrc, j)
    ) <- (intCond lazyZip fpCond lazyZip vecCond lazyZip v0Cond) lazyZip lsrcVec.zipWithIndex) {
      val destToSrc = io.in.take(i).zipWithIndex.map { case (in, j) =>
        val indexMatch = in.bits.ldest === lsrc
        val writeMatch = intRen && needIntDest(j) || fpRen && needFpDest(j) || vecRen && needVecDest(j)
        val v0Match = v0Ren && needV0Dest(j)
        indexMatch && writeMatch || v0Match
      }
      bypassCond(j)(i - 1) := VecInit(destToSrc).asUInt
    }
    bypassCondVl(i - 1) := VecInit(io.in.take(i).map(_.bits.vlWen && io.in(i).bits.vlRen)).asUInt
    // For the LUI instruction: psrc(0) is from register file and should always be zero.
    io.out(i).bits.psrc(0) := Mux(io.out(i).bits.isLUI, 0.U, io.out.take(i).map(_.bits.pdest).zip(bypassCond(0)(i-1).asBools).foldLeft(uops(i).psrc(0)) {
      (z, next) => Mux(next._2, next._1, z)
    })
    io.out(i).bits.psrcIntForMove := Mux(io.out(i).bits.isLUI, 0.U, io.out.take(i).map(_.bits.pdest).zip(bypassCond(0)(i-1).asBools).foldLeft(uops(i).psrcIntForMove) {
      (z, next) => Mux(next._2, next._1, z)
    })
    io.out(i).bits.psrc(1) := io.out.take(i).map(_.bits.pdest).zip(bypassCond(1)(i-1).asBools).foldLeft(uops(i).psrc(1)) {
      (z, next) => Mux(next._2, next._1, z)
    }
    io.out(i).bits.psrc(2) := io.out.take(i).map(_.bits.pdest).zip(bypassCond(2)(i-1).asBools).foldLeft(uops(i).psrc(2)) {
      (z, next) => Mux(next._2, next._1, z)
    }
    io.out(i).bits.psrc(3) := io.out.take(i).map(_.bits.pdest).zip(bypassCond(3)(i-1).asBools).foldLeft(uops(i).psrc(3)) {
      (z, next) => Mux(next._2, next._1, z)
    }
    io.out(i).bits.psrcVl := MuxCase(
      uops(i).psrcVl,
      (bypassCondVl(i-1).asBools zip io.out.take(i).map(_.bits.pdestVl)).reverse
    )
    io.out(i).bits.pdest := Mux(isMove(i), io.out(i).bits.psrcIntForMove, uops(i).pdest)
    renameTimeOldIntPdest(i) := io.out.take(i).map(_.bits.pdest).zip(io.in.take(i).map(prev =>
      prev.valid && needDestReg(Reg_I, prev.bits) && prev.bits.ldest === io.in(i).bits.ldest
    )).foldLeft(intOldDestReadPortsData(i)) {
      (old, next) => Mux(next._2, next._1, old)
    }

    // Todo: better implementation for fields reuse
    // For fused-lui-load, load.src(0) is replaced by the imm.
    val last_is_lui = io.in(i - 1).bits.selImm === SelImm.IMM_U && io.in(i - 1).bits.srcType(0) =/= SrcType.pc
    val this_is_load = io.in(i).bits.fuType === FuType.ldu.U
    val lui_to_load = io.in(i - 1).valid && io.in(i - 1).bits.rfWen && io.in(i - 1).bits.ldest === io.in(i).bits.lsrc(0)
    val fused_lui_load = last_is_lui && this_is_load && lui_to_load
    when (fused_lui_load) {
      // The first LOAD operand (base address) is replaced by LUI-imm and stored in imm
      val lui_imm = io.in(i - 1).bits.imm(ImmUnion.U.len - 1, 0)
      val ld_imm = io.in(i).bits.imm(ImmUnion.I.len - 1, 0)
      require(io.out(i).bits.imm.getWidth >= lui_imm.getWidth + ld_imm.getWidth)
      io.out(i).bits.srcType(0) := SrcType.imm
      io.out(i).bits.imm := Cat(lui_imm, ld_imm)
    }
  }

  val renameEarlyReleaseMetadata = Wire(Vec(RenameWidth, new EarlyReleaseMetadata))
  for (i <- 0 until RenameWidth) {
    val canTrackNewEr = io.out(i).bits.firstUop
    renameEarlyReleaseMetadata(i) := EarlyReleaseRenameMetadata.build(
      enable = backendParams.enableIntEarlyRelease.B && io.out(i).valid && canTrackNewEr && !io.out(i).bits.isMove,
      srcTypes = io.out(i).bits.srcType.take(numIntRegSrc),
      psrcs = io.out(i).bits.psrc.take(numIntRegSrc),
      oldIntPdestValid = io.out(i).valid &&
        canTrackNewEr &&
        io.out(i).bits.rfWen &&
        !io.out(i).bits.isMove &&
        intEarlyReleasePregTrackable(renameTimeOldIntPdest(i)),
      oldIntPdest = renameTimeOldIntPdest(i),
      redefinerRobIdx = earlyReleaseOwnerForLane(i)
    )
  }

  intUserCountTable.io.lookup := 0.U
  private val uctSourceUseSources = Wire(Vec(RenameWidth * numIntRegSrc, Valid(UInt(PhyRegIdxWidth.W))))
  private val uctSourceFallbackSources = Wire(Vec(RenameWidth * numIntRegSrc, Valid(UInt(PhyRegIdxWidth.W))))
  uctSourceUseSources.foreach(_ := 0.U.asTypeOf(Valid(UInt(PhyRegIdxWidth.W))))
  uctSourceFallbackSources.foreach(_ := 0.U.asTypeOf(Valid(UInt(PhyRegIdxWidth.W))))
  private val sameCycleSourceOwners = Wire(Vec(RenameWidth * numIntRegSrc, Valid(UInt(EarlyReleaseOwner.width.W))))
  sameCycleSourceOwners.foreach(_ := 0.U.asTypeOf(Valid(UInt(EarlyReleaseOwner.width.W))))
  for (i <- 0 until RenameWidth) {
    val metadata = renameEarlyReleaseMetadata(i)
    val canTrackNewEr = io.out(i).bits.firstUop
    intUserCountTable.io.alloc(i).valid := backendParams.enableIntEarlyRelease.B &&
      io.out(i).fire &&
      canTrackNewEr &&
      io.out(i).bits.rfWen &&
      !io.out(i).bits.isMove &&
      io.out(i).bits.pdest =/= 0.U &&
      intEarlyReleasePregTrackable(io.out(i).bits.pdest)
    intUserCountTable.io.alloc(i).bits.preg := io.out(i).bits.pdest
    intUserCountTable.io.alloc(i).bits.owner :=
      metadata.redefinerRobIdx
    intUserCountTable.io.moveAlias(i).valid := backendParams.enableIntEarlyRelease.B &&
      io.out(i).fire &&
      canTrackNewEr &&
      io.out(i).bits.isMove &&
      io.out(i).bits.rfWen &&
      io.out(i).bits.pdest =/= 0.U &&
      intEarlyReleasePregTrackable(io.out(i).bits.pdest)
    intUserCountTable.io.moveAlias(i).bits := io.out(i).bits.pdest

    for (srcIdx <- 0 until numIntRegSrc) {
      val sourceIdx = i * numIntRegSrc + srcIdx
      val sameUopDuplicate = if (srcIdx == 0) {
        false.B
      } else {
        VecInit((0 until srcIdx).map { prev =>
          SrcType.isXp(io.out(i).bits.srcType(prev)) &&
            io.out(i).bits.psrc(prev) === io.out(i).bits.psrc(srcIdx)
        }).asUInt.orR
      }
      val compressedNonFirstSourceFallback = backendParams.enableIntEarlyRelease.B &&
        io.out(i).fire &&
        !io.out(i).bits.firstUop &&
        SrcType.isXp(io.out(i).bits.srcType(srcIdx)) &&
        io.out(i).bits.psrc(srcIdx) =/= 0.U &&
        intEarlyReleasePregTrackable(io.out(i).bits.psrc(srcIdx)) &&
        !sameUopDuplicate
      uctSourceFallbackSources(sourceIdx).valid := compressedNonFirstSourceFallback
      uctSourceFallbackSources(sourceIdx).bits := io.out(i).bits.psrc(srcIdx)
      val trackedSourceUse = backendParams.enableIntEarlyRelease.B &&
        io.out(i).fire &&
        io.out(i).bits.firstUop &&
        metadata.countedIntSrcs(srcIdx).valid &&
        intEarlyReleasePregTrackable(metadata.countedIntSrcs(srcIdx).preg)
      uctSourceUseSources(sourceIdx).valid := trackedSourceUse
      uctSourceUseSources(sourceIdx).bits := metadata.countedIntSrcs(srcIdx).preg
      val sameCycleProducerOwner = Wire(Valid(UInt(EarlyReleaseOwner.width.W)))
      sameCycleProducerOwner := 0.U.asTypeOf(Valid(UInt(EarlyReleaseOwner.width.W)))
      for (prev <- 0 until i) {
        val previousLaneAllocates = intUserCountTable.io.alloc(prev).valid &&
          intUserCountTable.io.allocAccepted(prev) &&
          io.out(prev).bits.pdest === metadata.countedIntSrcs(srcIdx).preg
        when(trackedSourceUse && previousLaneAllocates) {
          sameCycleProducerOwner.valid := true.B
          sameCycleProducerOwner.bits := renameEarlyReleaseMetadata(prev).redefinerRobIdx
        }
      }
      val sourceSelectedForUct = !anyValid(uctSourceUseSources.take(sourceIdx).map(_.valid))
      sameCycleSourceOwners(sourceIdx).valid := trackedSourceUse && (
        sameCycleProducerOwner.valid ||
          (sourceSelectedForUct && intUserCountTable.io.sourceUseOwner(0).valid)
      )
      sameCycleSourceOwners(sourceIdx).bits := Mux(
        sameCycleProducerOwner.valid,
        sameCycleProducerOwner.bits,
        intUserCountTable.io.sourceUseOwner(0).bits
      )
    }
    io.out(i).bits.earlyRelease := EarlyReleaseRenameMetadata.attachSourceOwners(
      metadata = metadata,
      sourceOwners = sameCycleSourceOwners.slice(i * numIntRegSrc, (i + 1) * numIntRegSrc)
    )
  }
  connectFirstValid(intUserCountTable.io.sourceUse(0), uctSourceUseSources)
  connectFirstValid(intUserCountTable.io.sourceFallback(0), uctSourceFallbackSources)
  intUserCountTable.io.fallbackAll :=
    backendParams.enableIntEarlyRelease.B &&
      (moreThanOne(uctSourceUseSources.map(_.valid)) || moreThanOne(uctSourceFallbackSources.map(_.valid)))
  for (i <- 0 until RabCommitWidth) {
    val aliasRisk = EarlyReleaseTraditionalAliasRisk.align(
      realCommitLane = io.rabCommits.isCommit && io.rabCommits.commitValid(i),
      renameTableNeedFree = int_need_free(i),
      oldPdest = int_old_pdest(i)
    )
    intUserCountTable.io.traditionalAliasRisk(i).valid := backendParams.enableIntEarlyRelease.B &&
      aliasRisk.valid &&
      intEarlyReleasePregTrackable(aliasRisk.bits)
    intUserCountTable.io.traditionalAliasRisk(i).bits := aliasRisk.bits
  }

  private val gatedEarlyReleaseReadComplete =
    Wire(Vec(intEarlyReleaseRecoveryReadWidth, Valid(new EarlyReleaseReadCompleteEvent)))
  for (i <- 0 until intEarlyReleaseRecoveryReadWidth) {
    gatedEarlyReleaseReadComplete(i).valid := backendParams.enableIntEarlyRelease.B &&
      io.earlyReleaseReadComplete.events(i).valid
    gatedEarlyReleaseReadComplete(i).bits := io.earlyReleaseReadComplete.events(i).bits
  }
  earlyReleaseRecoveryTracker.io.readComplete := gatedEarlyReleaseReadComplete
  private val uctReadCompleteSources =
    earlyReleaseRecoveryTracker.io.firstReadComplete.map { source =>
      val event = Wire(Valid(new UserCountTableSourceEvent(PhyRegIdxWidth, releaseOwnerWidth)))
      event.valid := backendParams.enableIntEarlyRelease.B &&
        source.valid &&
        intEarlyReleasePregTrackable(source.bits.preg)
      event.bits.preg := source.bits.preg
      event.bits.owner := source.bits.producerOwner
      event
    }
  connectFirstNValid(intUserCountTable.io.readComplete, uctReadCompleteSources)

  for ((sink, source) <- earlyReleaseRecoveryTracker.io.commit.zip(io.earlyReleaseCommit)) {
    sink.valid := backendParams.enableIntEarlyRelease.B && source.valid
    sink.bits := source.bits
  }

  private val recoveryWalkEvents = Wire(Vec(CommitWidth, Valid(new EarlyReleaseRecoveryEvent(log2Ceil(RobSize)))))
  private val killedRecoveryEvents = Wire(Vec(CommitWidth, Valid(new EarlyReleaseKilledRecoveryEvent(log2Ceil(RobSize)))))
  for (i <- 0 until CommitWidth) {
    recoveryWalkEvents(i).valid := backendParams.enableIntEarlyRelease.B && io.earlyReleaseRecovery(i).valid
    recoveryWalkEvents(i).bits := io.earlyReleaseRecovery(i).bits
    killedRecoveryEvents(i).valid := backendParams.enableIntEarlyRelease.B && io.earlyReleaseKilledRecovery(i).valid
    killedRecoveryEvents(i).bits := io.earlyReleaseKilledRecovery(i).bits
  }
  earlyReleaseRecoveryTracker.io.walk := recoveryWalkEvents
  earlyReleaseRecoveryTracker.io.killed := killedRecoveryEvents

  private val uctCancelSourceUseSources =
    earlyReleaseRecoveryTracker.io.cancelSourceUse.map { source =>
      val event = Wire(Valid(new UserCountTableSourceEvent(PhyRegIdxWidth, releaseOwnerWidth)))
      event.valid := backendParams.enableIntEarlyRelease.B &&
        source.valid &&
        intEarlyReleasePregTrackable(source.bits.preg)
      event.bits := source.bits
      event
    }
  connectFirstNValid(intUserCountTable.io.cancelSourceUse, uctCancelSourceUseSources)

  private val uctProducerWritebackSources =
    io.earlyReleaseProducerWriteback.map { source =>
      val event = Wire(Valid(new UserCountTableProducerWriteback(PhyRegIdxWidth, releaseOwnerWidth)))
      event.valid := RobEarlyReleaseRedirectPolicy.allowProducerWriteback(
        enable = backendParams.enableIntEarlyRelease.B,
        valid = source.valid,
        intWen = source.bits.intWen.getOrElse(false.B),
        pdestNonZero = source.bits.pdest =/= 0.U,
        robIdx = source.bits.robIdx,
        redirect = io.redirect
      ) && intEarlyReleasePregTrackable(source.bits.pdest)
      event.bits.preg := source.bits.pdest
      event.bits.owner := source.bits.earlyReleaseOwner
      event
    }
  connectFirstNValid(intUserCountTable.io.producerWriteback, uctProducerWritebackSources)

  private val uctNonSpecRedefineSources =
    io.earlyReleaseNonSpecRedefine.map { source =>
      val event = Wire(Valid(new UserCountTableRedefine(PhyRegIdxWidth, releaseOwnerWidth)))
      event.valid := backendParams.enableIntEarlyRelease.B &&
        source.valid &&
        intEarlyReleasePregTrackable(source.bits.oldIntPdest)
      event.bits.preg := source.bits.oldIntPdest
      event.bits.owner := source.bits.owner
      event
    }
  connectFirstNValid(intUserCountTable.io.nonSpecRedefine, uctNonSpecRedefineSources)

  private val releasedPregClearValid = VecInit(intReleaseArbiter.io.release.map(release =>
    RegNext(backendParams.enableIntEarlyRelease.B && release.valid, false.B)
  ))
  private val releasedPregClearBits = VecInit(intReleaseArbiter.io.release.map(release =>
    RegEnable(release.bits, backendParams.enableIntEarlyRelease.B && release.valid)
  ))
  for (i <- 0 until RabCommitWidth) {
    intUserCountTable.io.clear(i).valid := releasedPregClearValid(i) &&
      intEarlyReleasePregTrackable(releasedPregClearBits(i).preg)
    intUserCountTable.io.clear(i).bits.preg := releasedPregClearBits(i).preg
    intUserCountTable.io.clear(i).bits.owner := releasedPregClearBits(i).owner
  }
  for (i <- 0 until CommitWidth) {
    val clearIdx = RabCommitWidth + i
    if (i < RabCommitWidth) {
      val producerClear = EarlyReleaseWalkProducerCleanup.build(
        walkValid = io.rabCommits.isWalk && io.rabCommits.walkValid(i),
        info = io.rabCommits.info(i),
        pregWidth = PhyRegIdxWidth,
        ownerWidth = releaseOwnerWidth
      )
      intUserCountTable.io.clear(clearIdx).valid := backendParams.enableIntEarlyRelease.B &&
        producerClear.valid &&
        intEarlyReleasePregTrackable(producerClear.bits.preg)
      intUserCountTable.io.clear(clearIdx).bits := producerClear.bits
    } else {
      intUserCountTable.io.clear(clearIdx).valid := false.B
      intUserCountTable.io.clear(clearIdx).bits := 0.U.asTypeOf(intUserCountTable.io.clear(clearIdx).bits)
    }
  }
  for (i <- 0 until RabCommitWidth) {
    intUserCountTable.io.traditionalRelease(i).valid := false.B
    intUserCountTable.io.traditionalRelease(i).bits := 0.U.asTypeOf(intUserCountTable.io.traditionalRelease(i).bits)
  }
  for (i <- RabCommitWidth until RabCommitWidth + CommitWidth) {
    intUserCountTable.io.traditionalRelease(i).valid := false.B
    intUserCountTable.io.traditionalRelease(i).bits := 0.U.asTypeOf(intUserCountTable.io.traditionalRelease(i).bits)
  }

  for (i <- 0 until intEarlyReleaseCandidateWidth) {
    intReleaseArbiter.io.early(i).valid := backendParams.enableIntEarlyRelease.B &&
      intUserCountTable.io.earlyFreeCandidates(i).valid
    intReleaseArbiter.io.early(i).bits.preg := intUserCountTable.io.earlyFreeCandidates(i).bits.preg
    intReleaseArbiter.io.early(i).bits.owner := intUserCountTable.io.earlyFreeCandidates(i).bits.owner
    intUserCountTable.io.earlyFreeCandidateAccepted(i) := backendParams.enableIntEarlyRelease.B &&
      intReleaseArbiter.io.earlyAccepted(i)
  }

  for (i <- 0 until RenameWidth) {
    val doIntAllocate = intFreeList.io.canAllocate &&
      intFreeList.io.doAllocate &&
      !io.rabCommits.isWalk &&
      !io.redirect.valid &&
      intFreeList.io.allocateReq(i)
    intReleaseArbiter.io.alloc(i).valid := backendParams.enableIntEarlyRelease.B && doIntAllocate
    intReleaseArbiter.io.alloc(i).bits := intFreeList.io.allocatePhyReg(i)
  }

  val traditionalIntReleaseValid = Wire(Vec(RabCommitWidth, Bool()))
  val traditionalIntReleasePreg = Wire(Vec(RabCommitWidth, UInt(PhyRegIdxWidth.W)))
  val traditionalIntReleaseOwner = Wire(Vec(RabCommitWidth, UInt(releaseOwnerWidth.W)))
  for (i <- 0 until RabCommitWidth) {
    val traditionalOwnerSource = EarlyReleaseTraditionalReleaseOwner.select(
      actualRobIdx = None,
      metadataRobIdx = io.rabCommits.info(i).earlyRelease.redefinerRobIdx
    )
    val traditionalRealCommit = io.rabCommits.isCommit && io.rabCommits.commitValid(i)
    val traditionalCommitStage1 = RegNext(traditionalRealCommit, false.B)
    val traditionalCommitAligned = RegNext(traditionalCommitStage1, false.B)
    val renameTableNeedFree = int_need_free(i)
    val releaseNeedFree = RegNext(renameTableNeedFree, false.B)
    val releaseCommitAligned = RegNext(traditionalCommitAligned, false.B)
    val commitOwnerStage1 = RegNext(traditionalOwnerSource, 0.U(releaseOwnerWidth.W))
    val alignedOldPdest = RegNext(int_old_pdest(i), 0.U(PhyRegIdxWidth.W))
    val releaseOldPdest = RegNext(alignedOldPdest, 0.U(PhyRegIdxWidth.W))
    val alignedOwner = RegNext(commitOwnerStage1, 0.U(releaseOwnerWidth.W))
    val releaseOwner = RegNext(alignedOwner, 0.U(releaseOwnerWidth.W))

    traditionalIntReleaseValid(i) := releaseNeedFree && releaseCommitAligned
    traditionalIntReleasePreg(i) := releaseOldPdest
    traditionalIntReleaseOwner(i) := releaseOwner
    intReleaseArbiter.io.traditional(i).valid :=
      backendParams.enableIntEarlyRelease.B && traditionalIntReleaseValid(i) && traditionalIntReleasePreg(i) =/= 0.U
    intReleaseArbiter.io.traditional(i).bits.preg := traditionalIntReleasePreg(i)
    intReleaseArbiter.io.traditional(i).bits.owner := traditionalIntReleaseOwner(i)
  }

  for (i <- 0 until CommitWidth) {
    intReleaseArbiter.io.killOwner(i).valid := backendParams.enableIntEarlyRelease.B &&
      earlyReleaseRecoveryTracker.io.killOwner(i).valid
    intReleaseArbiter.io.killOwner(i).bits := earlyReleaseRecoveryTracker.io.killOwner(i).bits
  }

  private val traditionalAcceptedForUct = VecInit(intReleaseArbiter.io.traditionalAccepted.map(accepted =>
    RegNext(backendParams.enableIntEarlyRelease.B && accepted, false.B)
  ))
  private val traditionalAcceptedBitsForUct = VecInit(intReleaseArbiter.io.traditional.zip(intReleaseArbiter.io.traditionalAccepted).map {
    case (release, accepted) => RegEnable(release.bits, backendParams.enableIntEarlyRelease.B && accepted)
  })
  for (i <- 0 until RabCommitWidth) {
    intUserCountTable.io.traditionalRelease(i).valid := traditionalAcceptedForUct(i) &&
      intEarlyReleasePregTrackable(traditionalAcceptedBitsForUct(i).preg)
    intUserCountTable.io.traditionalRelease(i).bits := traditionalAcceptedBitsForUct(i)
  }

  private val newErEnabled = backendParams.enableIntEarlyRelease.B
  private val newErRejectedDecrement = NewErPerfDebug.rejectedDecrement(intUserCountTable.io.rejected)
  XSError(
    NewErPerfDebug.duplicateReleaseError(newErEnabled, intReleaseArbiter.io.duplicate),
    "New-ER attempted duplicate integer physical register release\n"
  )
  XSError(
    NewErPerfDebug.releaseOverflowError(newErEnabled, intReleaseArbiter.io.overflow),
    "New-ER integer release arbiter overflowed MEFreeList ports\n"
  )
  XSError(NewErPerfDebug.underflowError(newErEnabled, intUserCountTable.io.underflow), "New-ER UCT underflow\n")
  XSError(
    newErEnabled && newErRejectedDecrement,
    "New-ER UCT rejected decrement\n"
  )

  XSPerfAccumulate("new_er_early_release", NewErPerfDebug.countWhenEnabled(newErEnabled, intReleaseArbiter.io.early.map(_.valid)))
  XSPerfAccumulate(
    "new_er_traditional_release",
    NewErPerfDebug.countWhenEnabled(newErEnabled, intReleaseArbiter.io.traditionalAccepted)
  )
  XSPerfAccumulate(
    "new_er_traditional_suppress",
    NewErPerfDebug.countWhenEnabled(newErEnabled, intReleaseArbiter.io.traditionalSuppressed)
  )
  XSPerfAccumulate(
    "new_er_move_fallback",
    NewErPerfDebug.countWhenEnabled(newErEnabled, io.out.map(out => out.fire && out.bits.isMove))
  )
  XSPerfAccumulate("new_er_walk_cancel", NewErPerfDebug.countWhenEnabled(newErEnabled, earlyReleaseRecoveryTracker.io.cancelSourceUse.map(_.valid)))
  XSPerfAccumulate("new_er_recovery_walk_observe", NewErPerfDebug.countWhenEnabled(newErEnabled, io.earlyReleaseRecovery.map(_.valid)))

  val genSnapshot = Cat(io.out.map(out => out.fire && out.bits.snapshot)).orR
  val lastCycleCreateSnpt = RegInit(false.B)
  lastCycleCreateSnpt := genSnapshot && !io.snptIsFull
  val sameSnptDistance = (RobCommitWidth * 4).U
  // notInSameSnpt: 1.robidxHead - snapLastEnq >= sameSnptDistance 2.no snap
  val notInSameSnpt = GatedValidRegNext(distanceBetween(robIdxHeadNext, io.snptLastEnq.bits) >= sameSnptDistance || !io.snptLastEnq.valid)
  val allowSnpt = if (EnableRenameSnapshot) notInSameSnpt && !lastCycleCreateSnpt && io.in.head.bits.firstUop else false.B
  io.out.zip(io.in).foreach{ case (out, in) => out.bits.snapshot := allowSnpt && FuType.isJump(in.bits.fuType) && in.fire }
  io.out.map{ x =>
    x.bits.hasException := Cat(selectFrontend(x.bits.exceptionVec) :+ x.bits.exceptionVec(illegalInstr) :+ x.bits.exceptionVec(virtualInstr)).orR || TriggerAction.isDmode(x.bits.trigger)
  }
  if(backendParams.debugEn){
    dontTouch(robIdxHeadNext)
    dontTouch(notInSameSnpt)
    dontTouch(genSnapshot)
    fusionValidVec.foreach{ fusionValid =>
      dontTouch(fusionValid)
    }
  }
  intFreeList.io.snpt := io.snpt
  fpFreeList.io.snpt := io.snpt
  vecFreeList.io.snpt := io.snpt
  v0FreeList.io.snpt := io.snpt
  vlFreeList.io.snpt := io.snpt
  intFreeList.io.snpt.snptEnq := genSnapshot
  fpFreeList.io.snpt.snptEnq := genSnapshot
  vecFreeList.io.snpt.snptEnq := genSnapshot
  v0FreeList.io.snpt.snptEnq := genSnapshot
  vlFreeList.io.snpt.snptEnq := genSnapshot

  /**
    * Instructions commit: update freelist and rename table
    */
  for (i <- 0 until RabCommitWidth) {
    val commitValid = io.rabCommits.isCommit && io.rabCommits.commitValid(i)
    val walkValid = io.rabCommits.isWalk && io.rabCommits.walkValid(i)

    // I. RAT Update
    // When redirect happens (mis-prediction), don't update the rename table
    intRenamePorts(i).wen  := intSpecWen(i)
    intRenamePorts(i).addr := inVec(i).ldest(log2Ceil(IntLogicRegs) - 1, 0)
    intRenamePorts(i).data := io.out(i).bits.pdest

    fpRenamePorts(i).wen  := fpSpecWen(i)
    fpRenamePorts(i).addr := inVec(i).ldest(log2Ceil(FpLogicRegs) - 1, 0)
    fpRenamePorts(i).data := fpFreeList.io.allocatePhyReg(i)

    vecRenamePorts(i).wen := vecSpecWen(i)
    vecRenamePorts(i).addr := inVec(i).ldest(log2Ceil(VecLogicRegs) - 1, 0)
    vecRenamePorts(i).data := vecFreeList.io.allocatePhyReg(i)

    v0RenamePorts(i).wen := v0SpecWen(i)
    v0RenamePorts(i).addr := inVec(i).ldest(log2Ceil(V0LogicRegs) - 1, 0)
    v0RenamePorts(i).data := v0FreeList.io.allocatePhyReg(i)

    vlRenamePorts(i).wen := vlSpecWen(i)
    vlRenamePorts(i).addr := 0.U // only one vl reg
    vlRenamePorts(i).data := vlFreeList.io.allocatePhyReg(i)

    // II. Free List Update
    intFreeList.io.freeReq(i) := Mux(
      backendParams.enableIntEarlyRelease.B,
      intReleaseArbiter.io.release(i).valid,
      int_need_free(i)
    )
    intFreeList.io.debugEarlyRelease(i) := backendParams.enableIntEarlyRelease.B &&
      intReleaseArbiter.io.release(i).valid &&
      intReleaseArbiter.io.releaseIsEarly(i)
    intFreeList.io.debugEarlyReleaseResolve(i) := backendParams.enableIntEarlyRelease.B &&
      intReleaseArbiter.io.traditionalSuppressed(i)
    intFreeList.io.debugEarlyReleaseResolvePreg(i) := intReleaseArbiter.io.traditional(i).bits.preg
    intFreeList.io.freePhyReg(i) := Mux(
      backendParams.enableIntEarlyRelease.B,
      intReleaseArbiter.io.release(i).bits.preg,
      RegNext(int_old_pdest(i))
    )
    fpFreeList.io.freeReq(i)  := GatedValidRegNext(commitValid && needDestRegCommit(Reg_F, io.rabCommits.info(i)))
    fpFreeList.io.freePhyReg(i) := fp_old_pdest(i)
    vecFreeList.io.freeReq(i)  := GatedValidRegNext(commitValid && needDestRegCommit(Reg_V, io.rabCommits.info(i)))
    vecFreeList.io.freePhyReg(i) := vec_old_pdest(i)
    v0FreeList.io.freeReq(i) := GatedValidRegNext(commitValid && needDestRegCommit(Reg_V0, io.rabCommits.info(i)))
    v0FreeList.io.freePhyReg(i) := v0_old_pdest(i)
    vlFreeList.io.freeReq(i) := GatedValidRegNext(io.vlCommits.isCommit && io.vlCommits.commitValid(i))
    vlFreeList.io.freePhyReg(i) := vl_old_pdest(i)
  }

  /*
  Debug and performance counters
   */
  def printRenameInfo(in: DecoupledIO[DecodeOutUop], out: DecoupledIO[RenameOutUop]) = {
    val pc = if(backendParams.debugEn) in.bits.debug.get.pc else 0.U
    XSInfo(out.fire, p"pc:${Hexadecimal(pc)} in(${in.valid},${in.ready}) " +
      p"lsrc(0):${in.bits.lsrc(0)} -> psrc(0):${out.bits.psrc(0)} " +
      p"lsrc(1):${in.bits.lsrc(1)} -> psrc(1):${out.bits.psrc(1)} " +
      p"lsrc(2):${in.bits.lsrc(2)} -> psrc(2):${out.bits.psrc(2)} " +
      p"ldest:${in.bits.ldest} -> pdest:${out.bits.pdest}\n"
    )
  }

  for ((x,y) <- io.in.zip(io.out)) {
    printRenameInfo(x, y)
  }

  io.out.map { case x =>
    when(x.valid && x.bits.rfWen) {
      assert(x.bits.ldest =/= 0.U, "rfWen cannot be 1 when Int regfile ldest is 0")
    }
  }


  val inValidVec = io.in.map(_.valid)
  val inReadyVec = io.in.map(_.ready)
  val outValidVec = io.debugOutValidVec.getOrElse(VecInit.fill(RenameWidth)(false.B))
  val outReadyVec = io.out.map(_.ready)
  val outFireVec = outReadyVec.zip(outValidVec).map { case (ready, valid) =>
    ready && valid
  }
  val dispatchAllFire = io.debugDispatchAllFire.getOrElse(false.B)

  // pre pipe stall/bubble
  val decodeReason = io.stallReason.in.reason
  val decodeStall  = !inValidVec.reduce(_ || _)
  val decodeBubble = !inValidVec.reduce(_ && _) && !decodeStall
  val renameStall = !(inReadyVec.reduce(_ || _))
  val renameStallReason = Wire(chiselTypeOf(io.stallReason.in.reason(0)))

  val outHasValidAllFire = outValidVec.reduce(_ || _) && dispatchAllFire


  val decodeBubbleValidVec = WireInit(VecInit.fill(RenameWidth)(false.B))
  val decodeBubbleReasonVec = Wire(chiselTypeOf(io.stallReason.in.reason))

  // prepipe bubble
  for (i <- 0 until RenameWidth) {
    val decodeBubbleValid = decodeBubble && !inValidVec(i)
    val bubbleStore = Module(new StoreBubbleReason(log2Ceil(TopDownCounters.NumStallReasons.id)))
    bubbleStore.io.bubbleValid := decodeBubbleValid
    bubbleStore.io.bubbleReason := decodeReason(i)
    bubbleStore.io.redirect := io.redirect.valid
    bubbleStore.io.reasonFire := outHasValidAllFire && !renameStall

    decodeBubbleValidVec(i) := bubbleStore.io.outReasonValid
    decodeBubbleReasonVec(i) := bubbleStore.io.outReason
  }

  // current pipe
  // current stall
  // bad speculation (redirect)
  val redirectStall       = io.redirect.valid
  val ctrlRedirectStall   = io.redirect.bits.debugIsCtrl
  val mvioRedirectStall   = io.redirect.bits.debugIsMemVio
  val otherRedirectStall  = redirectStall && !(ctrlRedirectStall || mvioRedirectStall)
  val redirectStallReason = MuxCase(BackendOtherCoreStall.id.U, Seq(
    ctrlRedirectStall  -> ControlRedirectStall.id.U,
    mvioRedirectStall  -> MemVioRedirectStall.id.U,
    otherRedirectStall -> OtherRedirectStall.id.U,
  ))

  // bad pseculation  (rabwalk)
  val debugRedirect = RegEnable(io.redirect.bits, io.redirect.valid)
  val recStall      = io.rabCommits.isWalk
  val ctrlRecStall  = debugRedirect.debugIsCtrl
  val mvioRecStall  = debugRedirect.debugIsMemVio
  val otherRecStall = recStall && !(ctrlRecStall || mvioRecStall)
  val recStallReason = MuxCase(BackendOtherCoreStall.id.U, Seq(
    ctrlRecStall  -> ControlRecoveryStall.id.U,
    mvioRecStall  -> MemVioRecoveryStall.id.U,
    otherRecStall -> OtherRecoveryStall.id.U,
  ))
  XSPerfAccumulate("recovery_stall", recStall)
  XSPerfAccumulate("control_recovery_stall", ctrlRecStall && recStall)
  XSPerfAccumulate("mem_violation_recovery_stall", mvioRecStall && recStall)
  XSPerfAccumulate("other_recovery_stall", otherRecStall && recStall)
  // freelist stall (we temporarily make the priority: int > fp > vec > v0 > vl)
  val notRecStall = !io.out.head.valid && !recStall
  val intFlStall  = !intFreeList.io.canAllocate
  val fpFlStall   = !fpFreeList.io.canAllocate
  val vecFlStall  = !vecFreeList.io.canAllocate
  val v0FlStall   = !v0FreeList.io.canAllocate
  val vlFlStall   = !vlFreeList.io.canAllocate
  val multiFlStall = PopCount(Cat(
    !intFreeList.io.canAllocate,
    !fpFreeList.io.canAllocate,
    !vecFreeList.io.canAllocate,
    !v0FreeList.io.canAllocate,
    !vlFreeList.io.canAllocate,
  )) > 1.U

  // TODO make all stall reason option to remove getorElse
  val robHeadStall = io.debugRobHeadStall.getOrElse(false.B)
  val robHeadFutype = io.debugRobHeadFuType.getOrElse(0.U)
  val ldReason = io.debugLoadReason.getOrElse(0.U)

  val robHeadStallReason = MuxCase(OtherNotReadyStall.id.U, Seq(
    FuType.isAMO(robHeadFutype)          -> AtomicStall.id.U          ,
    FuType.isStoreVstore(robHeadFutype)  -> StoreStall.id.U           ,
    FuType.isLoadVload(robHeadFutype)    -> ldReason                  ,
    FuType.isDivSqrt(robHeadFutype)      -> DivStall.id.U             ,
    FuType.isInt(robHeadFutype)          -> IntNotReadyStall.id.U     ,
    FuType.isFArith(robHeadFutype)       -> FPNotReadyStall.id.U      ,
  ))
  val freelistStall = intFlStall || fpFlStall || vecFlStall || v0FlStall || vlFlStall
  val freelistStallReason = MuxCase(BackendOtherCoreStall.id.U, Seq(
    robHeadStall  -> robHeadStallReason,
    multiFlStall  -> MultiFlStall.id.U,
    intFlStall    -> IntFlStall.id.U,
    fpFlStall     -> FpFlStall.id.U,
    vecFlStall    -> VecFlStall.id.U,
    v0FlStall     -> V0FlStall.id.U,
    vlFlStall     -> VlFlStall.id.U,
  ))
  renameStallReason := MuxCase(BackendOtherCoreStall.id.U, Seq(
    redirectStall -> redirectStallReason,
    recStall      -> recStallReason,
    freelistStall -> freelistStallReason,
  ))

  // current pipe bubble
  /** Attention: Special care is needed if this stage may generate its own bubble in later cases
   */
  val renameBubble = false.B
  val renameBubbleReason = 0.U


  io.stallReason.out.reason.zipWithIndex.foreach{ case (stallReason, idx) =>
    val inValid = inValidVec(idx)
    val outValid = outValidVec(idx)
    val inReason = decodeReason(idx)
    // TopDown collect pre pipe reason
    val prePipeStall = decodeStall
    val prePipeBubble = decodeBubbleValidVec(idx)
    val prePipeStallReason = inReason
    val prePieBubbleReason = decodeBubbleReasonVec(idx)
    // TopDown count current stage stall
    val redirect = redirectStall
    val redirectReason = redirectStallReason

    // as decode not generate bubble now, Topdown donot collect current stage Bubble
    // if decode generate bubble later, Topdown should add here

    val stallReasonPipe = Module(new PipelineStallReason(log2Ceil(TopDownCounters.NumStallReasons.id)))
    stallReasonPipe.io.rightFire := outFireVec(idx)
    stallReasonPipe.io.rightHasFire := outHasValidAllFire
    stallReasonPipe.io.prePipeStall := prePipeStall
    stallReasonPipe.io.prePipeStallReason := prePipeStallReason
    stallReasonPipe.io.prePipeBubble := prePipeBubble
    stallReasonPipe.io.prePipeBubbleReason := prePieBubbleReason
    stallReasonPipe.io.redirect := redirect
    stallReasonPipe.io.redirectReason := redirectReason
    stallReasonPipe.io.currentPipeStall := renameStall
    stallReasonPipe.io.currentPipeStallReason := renameStallReason
    stallReasonPipe.io.currentPipeBubble := renameBubble
    stallReasonPipe.io.currentPipeBubbleReason := renameBubbleReason

    stallReason := stallReasonPipe.io.outReason
  }

  XSDebug(io.rabCommits.isWalk, p"Walk Recovery Enabled\n")
  XSDebug(io.rabCommits.isWalk, p"validVec:${Binary(io.rabCommits.walkValid.asUInt)}\n")
  for (i <- 0 until RabCommitWidth) {
    val info = io.rabCommits.info(i)
    XSDebug(io.rabCommits.isWalk && io.rabCommits.walkValid(i), p"[#$i walk info] " +
      p"ldest:${info.ldest} rfWen:${info.rfWen} fpWen:${info.fpWen} vecWen:${info.vecWen} v0Wen:${info.v0Wen}")
  }

  XSDebug(p"inValidVec: ${Binary(Cat(io.in.map(_.valid)))}\n")

  XSPerfAccumulate("in_valid_count", PopCount(io.in.map(_.valid)))
  XSPerfAccumulate("in_fire_count", PopCount(io.in.map(_.fire)))
  XSPerfAccumulate("in_valid_not_ready_count", PopCount(io.in.map(x => x.valid && !x.ready)))
  XSPerfAccumulate("wait_cycle", !io.in.head.valid && dispatchCanAcc)
  for (i <- 1 to RenameWidth){
    XSPerfAccumulate(s"load_num_$i", PopCount(io.in.map(x => x.fire && FuType.isLoad(x.bits.fuType))) === i.U)
    XSPerfAccumulate(s"store_num_$i", PopCount(io.in.map(x => x.fire && FuType.isStore(x.bits.fuType))) === i.U)
    XSPerfAccumulate(s"bju_num_$i", PopCount(io.in.map(x => x.fire && FuType.isBrh(x.bits.fuType))) === i.U)
  }

  // These stall reasons could overlap each other, but we configure the priority as fellows.
  // walk stall > dispatch stall > int freelist stall > fp freelist stall
  private val inHeadStall = io.in.head match { case x => x.valid && !x.ready }
  private val stallForWalk      = inHeadValid &&  io.rabCommits.isWalk
  private val stallForDispatch  = inHeadValid && !io.rabCommits.isWalk && !dispatchCanAcc
  private val stallForIntFL     = inHeadValid && !io.rabCommits.isWalk && dispatchCanAcc && fpFreeList.io.canAllocate && vecFreeList.io.canAllocate && v0FreeList.io.canAllocate && vlFreeList.io.canAllocate && !intFreeList.io.canAllocate
  private val stallForFpFL      = inHeadValid && !io.rabCommits.isWalk && dispatchCanAcc && intFreeList.io.canAllocate && vecFreeList.io.canAllocate && v0FreeList.io.canAllocate && vlFreeList.io.canAllocate && !fpFreeList.io.canAllocate
  private val stallForVecFL     = inHeadValid && !io.rabCommits.isWalk && dispatchCanAcc && intFreeList.io.canAllocate && fpFreeList.io.canAllocate && v0FreeList.io.canAllocate && vlFreeList.io.canAllocate && !vecFreeList.io.canAllocate
  private val stallForV0FL      = inHeadValid && !io.rabCommits.isWalk && dispatchCanAcc && intFreeList.io.canAllocate && fpFreeList.io.canAllocate && vecFreeList.io.canAllocate && vlFreeList.io.canAllocate && !v0FreeList.io.canAllocate
  private val stallForVlFL      = inHeadValid && !io.rabCommits.isWalk && dispatchCanAcc && intFreeList.io.canAllocate && fpFreeList.io.canAllocate && vecFreeList.io.canAllocate && v0FreeList.io.canAllocate && !vlFreeList.io.canAllocate
  XSPerfAccumulate("stall_cycle",          inHeadStall)
  XSPerfAccumulate("stall_cycle_walk",     stallForWalk)
  XSPerfAccumulate("stall_cycle_dispatch", stallForDispatch)
  XSPerfAccumulate("stall_cycle_int",      stallForIntFL)
  XSPerfAccumulate("stall_cycle_fp",       stallForFpFL)
  XSPerfAccumulate("stall_cycle_vec",      stallForVecFL)
  XSPerfAccumulate("stall_cycle_vec",      stallForV0FL)
  XSPerfAccumulate("stall_cycle_vec",      stallForVlFL)

  XSPerfHistogram("in_valid_range",  PopCount(io.in.map(_.valid)),  true.B, 0, DecodeWidth + 1, 1)
  XSPerfHistogram("in_fire_range",   PopCount(io.in.map(_.fire)),   true.B, 0, DecodeWidth + 1, 1)
  XSPerfHistogram("out_valid_range", PopCount(io.out.map(_.valid)), true.B, 0, DecodeWidth + 1, 1)
  XSPerfHistogram("out_fire_range",  PopCount(io.out.map(_.fire)),  true.B, 0, DecodeWidth + 1, 1)

  XSPerfAccumulate("move_instr_count", PopCount(io.out.map(out => out.fire && out.bits.isMove)))
  val is_fused_lui_load = io.out.map(o => o.fire && o.bits.fuType === FuType.ldu.U && o.bits.srcType(0) === SrcType.imm)
  XSPerfAccumulate("fused_lui_load_instr_count", PopCount(is_fused_lui_load))

  val renamePerf = Seq(
    ("rename_in                  ", PopCount(io.in.map(_.valid & io.in(0).ready ))),
    ("rename_waitinstr           ", PopCount((0 until RenameWidth).map(i => io.in(i).valid && !io.in(i).ready))),
    ("rename_stall               ", inHeadStall),
    ("rename_stall_cycle_walk    ", inHeadValid &&  io.rabCommits.isWalk),
    ("rename_stall_cycle_dispatch", inHeadValid && !io.rabCommits.isWalk && !dispatchCanAcc),
    ("rename_stall_cycle_int     ", inHeadValid && !io.rabCommits.isWalk && dispatchCanAcc && fpFreeList.io.canAllocate && vecFreeList.io.canAllocate && v0FreeList.io.canAllocate && vlFreeList.io.canAllocate && !intFreeList.io.canAllocate),
    ("rename_stall_cycle_fp      ", inHeadValid && !io.rabCommits.isWalk && dispatchCanAcc && intFreeList.io.canAllocate && vecFreeList.io.canAllocate && v0FreeList.io.canAllocate && vlFreeList.io.canAllocate && !fpFreeList.io.canAllocate),
    ("rename_stall_cycle_vec     ", inHeadValid && !io.rabCommits.isWalk && dispatchCanAcc && intFreeList.io.canAllocate && fpFreeList.io.canAllocate && v0FreeList.io.canAllocate && vlFreeList.io.canAllocate && !vecFreeList.io.canAllocate),
    ("rename_stall_cycle_v0      ", inHeadValid && !io.rabCommits.isWalk && dispatchCanAcc && intFreeList.io.canAllocate && fpFreeList.io.canAllocate && vecFreeList.io.canAllocate && vlFreeList.io.canAllocate && !v0FreeList.io.canAllocate),
    ("rename_stall_cycle_vl      ", inHeadValid && !io.rabCommits.isWalk && dispatchCanAcc && intFreeList.io.canAllocate && fpFreeList.io.canAllocate && vecFreeList.io.canAllocate && v0FreeList.io.canAllocate && !vlFreeList.io.canAllocate),
  )
  val intFlPerf = intFreeList.getPerfEvents
  val fpFlPerf = fpFreeList.getPerfEvents
  val vecFlPerf = vecFreeList.getPerfEvents
  val v0FlPerf = v0FreeList.getPerfEvents
  val vlFlPerf = vlFreeList.getPerfEvents
  val perfEvents = renamePerf ++ intFlPerf ++ fpFlPerf ++ vecFlPerf ++ v0FlPerf ++ vlFlPerf
  generatePerfEvent()
}
