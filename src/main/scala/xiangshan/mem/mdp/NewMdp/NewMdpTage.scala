// package xiangshan.mem.mdp.NewMdp

// import org.chipsalliance.cde.config.Parameters
// import chisel3._
// import chisel3.util._
// import freechips.rocketchip.util.SeqToAugmentedSeq
// import xiangshan._
// import utils._
// import utility._
// import xiangshan.frontend.PrunedAddr
// import xiangshan.frontend.bpu.StageCtrl
// import xiangshan.frontend.bpu.tage.UsefulResetCounter

// class NewMdpTageLoadDecision(implicit p: Parameters) extends XSBundle with HasMdpTageTableParameters {
//   val valid = Bool()
//   val trainNxOH    = UInt(NumTables.W)
//   val hitWayMaskOH = UInt(MaxNumWays.W)

//   val needUpdate      = Bool()
//   val updateEntry     = new TageEntry
//   val updateUsefulCtr = UsefulCounter()

//   val needAllocate      = Bool()
//   val canAllocate       = Bool()
//   val allocateNxOH      = UInt(NumTables.W)
//   val allocateUsefulCtr = UsefulCounter()
//   val allocateDistance  = UInt(RobDistance.W)
//   val allocateCfiPosition = UInt(CfiPositionWidth.W)

//   val needAllWayWeak     = Bool()
//   val allWayWeakOH       = UInt(NumTables.W)
//   val AllWayWeakUsefulCtrs = Vec(MaxNumWays, UsefulCounter())

//   val iwNdepClampBlocked = Bool()
// }

// class NewMdpTage(implicit p: Parameters) extends XSModule with TopHelper {
//   val io = IO(new Bundle {
//     val stageCtrl = Input(new StageCtrl)
//     val startPc   = Input(new PrunedAddr(VAddrBits))
//     val historySnapshot = Input(new MdpHistorySnapshot)
//     val train     = Input(new MdpTrain)
//     val trainReady = Output(Bool())
//     val fromBaseResult = Input(Vec(NumMdpResultEntries, Valid(new BasePrediction)))

//     val result = Output(Vec(NumMdpResultEntries, Valid(new TagePrediction)))
//   })

//   private val tables = TableInfos.zipWithIndex.map { case (info, i) => Module(new MdpTageTable(i, info)) }

//   private def getAllocateAndWeakTargets(
//     startTableOH: Seq[Bool],
//     wayFullMask: Seq[Bool]
//   ): (UInt, Bool, UInt) = {
//     val searchMask = Wire(UInt(startTableOH.length.W))
//     when(startTableOH.asUInt.orR) {
//       searchMask := ~(startTableOH.asUInt - 1.U) << 1.U
//     }.otherwise {
//       searchMask := ~0.U(startTableOH.length.W)
//     }

//     val allocatableMask = searchMask & (~wayFullMask.asUInt)
//     val allocateOH = Mux(
//       allocatableMask.orR,
//       PriorityEncoderOH(allocatableMask),
//       0.U(startTableOH.length.W)
//     )
//     val firstTriedOH = Mux(
//       searchMask.orR,
//       PriorityEncoderOH(searchMask),
//       0.U(startTableOH.length.W)
//     )
//     val allWayWeakOH = Mux(
//       (firstTriedOH & wayFullMask.asUInt).orR,
//       firstTriedOH,
//       0.U(startTableOH.length.W)
//     )

//     (allocateOH, allocatableMask.orR, allWayWeakOH)
//   }

//   private val usefulResetCtr = RegInit(UsefulResetCounter.Zero)

//   /* --------------------------------------------------------------------------------------------------------------
//      predict pipeline stage 0
//      -------------------------------------------------------------------------------------------------------------- */
//   private val s0_fire    = io.stageCtrl.s0_fire
//   private val s0_startPc = io.startPc

//   private val s0_foldedHist = getFoldedHist(io.historySnapshot)
//   private val s0_setIdx = VecInit((tables zip s0_foldedHist).map { case (table, hist) =>
//     table.getSetIndex(s0_startPc, hist.forIdx)
//   })

//   private val s0_bankIdx  = tables.head.getBankIndex(s0_startPc)
//   private val s0_bankMask = UIntToOH(s0_bankIdx, NumBanks)

//   tables.zipWithIndex.foreach { case (table, tableIdx) =>
//     table.io.predictReadReq.valid         := s0_fire
//     table.io.predictReadReq.bits.setIdx   := s0_setIdx(tableIdx)
//     table.io.predictReadReq.bits.bankMask := s0_bankMask
//   }

//   /* --------------------------------------------------------------------------------------------------------------
//      predict pipeline stage 1
//      -------------------------------------------------------------------------------------------------------------- */
//   private val s1_fire       = io.stageCtrl.s1_fire
//   private val s1_startPc    = RegEnable(s0_startPc, s0_fire)
//   private val s1_foldedHist = RegEnable(s0_foldedHist, s0_fire)

//   private val s1_rawTag = VecInit((tables zip s1_foldedHist).map { case (table, hist) =>
//     table.getRawTag(s1_startPc, hist.forTag)
//   })

//   private val s1_readResp = DataHoldBypass(VecInit(tables.map(_.io.predictReadResp)), RegNext(s0_fire))

//   /* --------------------------------------------------------------------------------------------------------------
//      predict pipeline stage 2
//      -------------------------------------------------------------------------------------------------------------- */
//   private val s2_fire     = io.stageCtrl.s2_fire
//   private val s2_startPc  = RegEnable(s1_startPc, s1_fire)
//   private val s2_rawTag   = RegEnable(s1_rawTag, s1_fire)
//   private val s2_readResp = RegEnable(s1_readResp, s1_fire)
//   dontTouch(s2_startPc)

//   io.fromBaseResult.zipWithIndex.foreach { case (load, i) =>
//     val position = load.bits.cfiPosition
//     val allTableTagMatchResults = s2_readResp.zipWithIndex.map { case (tableReadResp, tableIdx) =>
//       val tag          = s2_rawTag(tableIdx) ^ position
//       val hitWayMask   = tableReadResp.entries.map(entry => entry.valid && entry.tag === tag)
//       val hitWayMaskOH = PriorityEncoderOH(hitWayMask)

//       val result = Wire(new PredictTagMatchResult).suggestName(s"new_s2_load_${i}_table_${tableIdx}_result")
//       result.hit          := hitWayMask.reduce(_ || _)
//       result.hitWayMaskOH := hitWayMaskOH.asUInt
//       result.usefulCtr    := Mux1H(hitWayMaskOH, tableReadResp.usefulCtrs)
//       result.distance     := Mux1H(hitWayMaskOH, tableReadResp.entries).distance
//       result
//     }

//     val hitTableMask = allTableTagMatchResults.map(_.hit)
//     val hitTable     = hitTableMask.reduce(_ || _)
//     val longestHistTableOH = getLongestHistTableOH(hitTableMask)
//     val prediction = Mux1H(longestHistTableOH, allTableTagMatchResults)

//     when(s2_fire) {
//       assert(PopCount(longestHistTableOH) <= 1.U, "Multiple tables hit in NewMdpTage prediction")
//     }

//     io.result(i).valid := hitTable
//     io.result(i).bits.distance := prediction.distance
//     io.result(i).bits.static   := !hitTable
//     io.result(i).bits.loadWait := prediction.distance.orR
//   }

//   /* --------------------------------------------------------------------------------------------------------------
//      train pipeline stage 0
//      -------------------------------------------------------------------------------------------------------------- */
//   private val t0_startPc = io.train.startPc
//   private val t0_loads   = io.train.loads

//   private val t0_bankIdx  = tables.head.getBankIndex(t0_startPc)
//   private val t0_bankMask = UIntToOH(t0_bankIdx, NumBanks)

//   private val t0_loadMask = t0_loads.map(_.valid)
//   private val t0_hasLoad  = t0_loadMask.reduce(_ || _)
//   private val t0_fire     = io.stageCtrl.t0_fire && t0_hasLoad

//   private val t0_needRead = true.B
//   private val t0_readBankConflict = t0_hasLoad && t0_needRead && s0_fire && t0_bankIdx === s0_bankIdx
//   io.trainReady := !t0_readBankConflict

//   private val t0_foldedHist = getFoldedHist(io.train.meta.historySnapshot)
//   private val t0_setIdx = VecInit((tables zip t0_foldedHist).map { case (table, hist) =>
//     table.getSetIndex(t0_startPc, hist.forIdx)
//   })

//   tables.zipWithIndex.foreach { case (table, tableIdx) =>
//     table.io.trainReadReq.valid         := t0_fire && t0_needRead
//     table.io.trainReadReq.bits.setIdx   := t0_setIdx(tableIdx)
//     table.io.trainReadReq.bits.bankMask := t0_bankMask
//   }

//   /* --------------------------------------------------------------------------------------------------------------
//      train pipeline stage 1
//      -------------------------------------------------------------------------------------------------------------- */
//   private val t1_fire     = RegNext(t0_fire)
//   private val t1_startPc  = RegEnable(t0_startPc, t0_fire)
//   private val t1_loads    = RegEnable(t0_loads, t0_fire)
//   private val t1_setIdx   = RegEnable(t0_setIdx, t0_fire)
//   private val t1_bankMask = RegEnable(t0_bankMask, t0_fire)

//   private val t1_foldedHist = RegEnable(t0_foldedHist, t0_fire)
//   private val t1_rawTag = VecInit((tables zip t1_foldedHist).map { case (table, hist) =>
//     table.getRawTag(t1_startPc, hist.forTag)
//   })
//   private val t1_readResp = VecInit(tables.map(_.io.trainReadResp))

//   /* --------------------------------------------------------------------------------------------------------------
//      train pipeline stage 2
//      -------------------------------------------------------------------------------------------------------------- */
//   private val t2_fire     = RegNext(t1_fire)
//   private val t2_startPc  = RegEnable(t1_startPc, t1_fire)
//   private val t2_loads    = RegEnable(t1_loads, t1_fire)
//   private val t2_setIdx   = RegEnable(t1_setIdx, t1_fire)
//   private val t2_bankMask = RegEnable(t1_bankMask, t1_fire)
//   private val t2_rawTag   = RegEnable(t1_rawTag, t1_fire)
//   private val t2_readResp = RegEnable(t1_readResp, t1_fire)
//   dontTouch(t2_startPc)

//   private val t2_loadDecisionVec = t2_loads.zipWithIndex.map { case (load, i) =>
//     val tageTableTagMatchResults = t2_readResp.zipWithIndex.map { case (tableReadResp, tableIdx) =>
//       val position     = load.bits.cfiPosition
//       val tag          = t2_rawTag(tableIdx) ^ position
//       val hitWayMask   = tableReadResp.entries.map(entry => entry.valid && entry.tag === tag && load.valid)
//       val hitWayMaskOH = PriorityEncoderOH(hitWayMask)

//       val result = Wire(new TrainTagMatchResult).suggestName(s"new_t2_load_${i}_table_${tableIdx}_result")
//       result.hit          := hitWayMask.reduce(_ || _)
//       result.hitWayMaskOH := hitWayMaskOH.asUInt
//       result.wayFull      := tableReadResp.entries.zip(tableReadResp.usefulCtrs).map { case (entry, ctr) =>
//         entry.valid && !ctr.isSaturateNegative
//       }.reduce(_ && _)
//       result.tag          := tag
//       result.usefulCtrs   := tableReadResp.usefulCtrs
//       result.distance     := Mux1H(hitWayMaskOH, tableReadResp.entries).distance
//       result.allWayWeakUsefulCtrs := VecInit(tableReadResp.usefulCtrs.map(_.getDecrease()))
//       result
//     }

//     val tageHitTableMask = tageTableTagMatchResults.map(_.hit)
//     val tageWayFullMask  = tageTableTagMatchResults.map(_.wayFull)
//     val providerTableOHSeq = getLongestHistTableOH(tageHitTableMask)
//     val providerTableOH    = providerTableOHSeq.asUInt
//     val providerHit        = tageHitTableMask.reduce(_ || _)
//     val provider = WireDefault(0.U.asTypeOf(new TrainTagMatchResult))
//     when(providerHit) {
//       provider := Mux1H(providerTableOH, tageTableTagMatchResults)
//     }
//     val rawUsefulCtr = WireDefault(UsefulCounter.Zero)
//     when(providerHit) {
//       rawUsefulCtr := Mux1H(provider.hitWayMaskOH, provider.usefulCtrs)
//     }

//     val allWayWeakTargetUsefulCtrsRaw = Wire(Vec(MaxNumWays, UsefulCounter()))
//     val allWayWeakTargetUsefulCtrs = Wire(Vec(MaxNumWays, UsefulCounter()))
//     allWayWeakTargetUsefulCtrsRaw := VecInit(Seq.fill(MaxNumWays)(UsefulCounter.Zero))
//     allWayWeakTargetUsefulCtrs := VecInit(Seq.fill(MaxNumWays)(UsefulCounter.Zero))

//     val providerIsNdep = provider.distance === 0.U
//     val isAW = load.valid && load.bits.updateType === MdpUpdateType.M_AW
//     val isAS = load.valid && load.bits.updateType === MdpUpdateType.M_AS
//     val isIS = load.valid && load.bits.updateType === MdpUpdateType.M_IS
//     val isIW = load.valid && load.bits.updateType === MdpUpdateType.M_IW

//     val awProviderMismatch = isAW && providerHit && !providerIsNdep
//     val asProviderMismatch = isAS && providerHit && (providerIsNdep || provider.distance =/= load.bits.distance)
//     val rewriteProvider = rawUsefulCtr.isSaturateNegative && (awProviderMismatch || asProviderMismatch)
//     val providerWeakReq  = providerHit && (isAW || isAS) && !rewriteProvider
//     val providerStrongReq = providerHit && (isIS || isIW)
//     val needAllocateWeak   = isAW && (!providerHit || !rewriteProvider)
//     val needAllocateStrong = isAS && (!providerHit || !rewriteProvider)
//     val needAllocate       = needAllocateWeak || needAllocateStrong

//     val providerAllocInfo = getAllocateAndWeakTargets(providerTableOHSeq, tageWayFullMask)
//     val baseAllocInfo = getAllocateAndWeakTargets(Seq.fill(NumTables)(false.B), tageWayFullMask)
//     val allocateTableOH = Mux(providerHit, providerAllocInfo._1, baseAllocInfo._1)
//     val allocateCanCommit = Mux(providerHit, providerAllocInfo._2, baseAllocInfo._2)
//     val allWayWeakOH = Mux(needAllocate, Mux(providerHit, providerAllocInfo._3, baseAllocInfo._3), 0.U)

//     when(allWayWeakOH.orR) {
//       allWayWeakTargetUsefulCtrsRaw := Mux1H(allWayWeakOH, tageTableTagMatchResults).usefulCtrs
//     }
//     allWayWeakTargetUsefulCtrs := VecInit(allWayWeakTargetUsefulCtrsRaw.map(_.getDecrease()))

//     val needAllWayWeak     = allWayWeakOH.orR
//     val notNeedAllWayWeak  = needAllWayWeak && VecInit(
//       allWayWeakTargetUsefulCtrsRaw.map(_.isSaturateNegative)
//     ).reduce(_ && _)
//     val notNeedUpdate = (
//       (
//         (providerStrongReq && rawUsefulCtr.isSaturatePositive) ||
//         (providerWeakReq && rawUsefulCtr.isSaturateNegative)
//       ) && providerHit
//     ) || !(providerStrongReq || providerWeakReq)

//     val newUsefulCtr = Wire(UsefulCounter())
//     newUsefulCtr := rawUsefulCtr
//     when(providerWeakReq) {
//       newUsefulCtr := rawUsefulCtr.getDecrease()
//     }.elsewhen(providerStrongReq) {
//       when(load.bits.updateType === MdpUpdateType.M_IW && provider.distance === 0.U) {
//         newUsefulCtr := rawUsefulCtr.getIncrease(en = rawUsefulCtr.value <= 2.U)
//       }.otherwise {
//         newUsefulCtr := rawUsefulCtr.getIncrease()
//       }
//     }

//     val needUpdate = providerHit && (rewriteProvider || !notNeedUpdate)

//     val decision = Wire(new NewMdpTageLoadDecision).suggestName(s"new_t2_load_${i}_decision")
//     decision.valid             := providerHit || needAllocate
//     decision.trainNxOH         := providerTableOH
//     decision.hitWayMaskOH      := provider.hitWayMaskOH
//     decision.needUpdate        := needUpdate
//     decision.updateEntry.valid := true.B
//     decision.updateEntry.tag   := provider.tag
//     decision.updateEntry.distance := Mux(
//       rewriteProvider,
//       Mux(isAS, load.bits.distance, 0.U),
//       provider.distance
//     )
//     decision.updateUsefulCtr := Mux(
//       rewriteProvider,
//       Mux(isAS, UsefulCounter.InitStrong, UsefulCounter.InitWeak),
//       newUsefulCtr
//     )
//     decision.needAllocate        := needAllocate
//     decision.canAllocate         := needAllocate && allocateCanCommit
//     decision.allocateNxOH        := Mux(needAllocate, allocateTableOH, 0.U)
//     decision.allocateUsefulCtr   := Mux(needAllocateStrong, UsefulCounter.InitStrong, UsefulCounter.InitWeak)
//     decision.allocateDistance    := load.bits.distance
//     decision.allocateCfiPosition := load.bits.cfiPosition
//     decision.needAllWayWeak      := needAllWayWeak && !notNeedAllWayWeak
//     decision.allWayWeakOH        := allWayWeakOH
//     decision.AllWayWeakUsefulCtrs := allWayWeakTargetUsefulCtrs
//     decision.iwNdepClampBlocked := isIW &&
//       providerHit && provider.distance === 0.U && rawUsefulCtr.value >= 2.U

//     decision
//   }

//   when(t2_fire) {
//     t2_loadDecisionVec.zipWithIndex.foreach { case (decision, i) =>
//       when(decision.valid && !decision.needAllocate) {
//         assert(decision.trainNxOH.orR, s"NewMdpTage decision ${i} must have provider table when valid without allocation")
//         assert(decision.hitWayMaskOH.orR, s"NewMdpTage decision ${i} must have provider way when valid without allocation")
//       }
//       when(decision.valid && decision.needAllocate && decision.canAllocate) {
//         assert(decision.allocateNxOH.orR, s"NewMdpTage decision ${i} must have allocate target when allocation can commit")
//       }
//     }
//   }

//   private val t2_needAllocate = t2_loadDecisionVec.map(decision => decision.valid && decision.needAllocate).reduce(_ || _)
//   private val t2_tableUpdateWayMask = TableInfos.indices.map { tableIdx =>
//     t2_loadDecisionVec.map { decision =>
//       Mux(decision.needUpdate && decision.trainNxOH(tableIdx), decision.hitWayMaskOH, 0.U(MaxNumWays.W))
//     }.reduce(_ | _)
//   }
//   private val t2_tageTableCanAllocateWayMask = t2_readResp.zipWithIndex.map { case (tableReadResp, tableIdx) =>
//     val notValidMask = tableReadResp.entries.map(!_.valid).asUInt
//     val zeroUsefulMask = tableReadResp.entries.zip(tableReadResp.usefulCtrs).map { case (entry, usefulCtr) =>
//       entry.valid && usefulCtr.isSaturateNegative
//     }.asUInt
//     val rawAllocateWayMask = Mux(notValidMask.orR, notValidMask, zeroUsefulMask)
//     rawAllocateWayMask & ~t2_tableUpdateWayMask(tableIdx)
//   }
//   private val t2_canAllocate =
//     t2_loadDecisionVec.map(decision => Mux(decision.valid && decision.needAllocate, decision.canAllocate, true.B)).reduce(_ && _)

//   tables.zipWithIndex.foreach { case (table, tableIdx) =>
//     implicit val info: MdpTageTableInfo = TableInfos(tableIdx)

//     val tableAllocateReqByLoad = t2_loadDecisionVec.map { decision =>
//       decision.valid && decision.needAllocate && decision.canAllocate && decision.allocateNxOH(tableIdx)
//     }
//     val tableAllocateReq = tableAllocateReqByLoad.reduce(_ || _)
//     val tableAllocateLoadOH = PriorityEncoderOH(tableAllocateReqByLoad)
//     val tableAllocateDecision = WireDefault(0.U.asTypeOf(new NewMdpTageLoadDecision))
//     when(tableAllocateReq) {
//       tableAllocateDecision := Mux1H(tableAllocateLoadOH, t2_loadDecisionVec)
//     }

//     val tableAllocateWayOH = Mux(
//       tableAllocateReq,
//       PriorityEncoderOH(t2_tageTableCanAllocateWayMask(tableIdx)),
//       0.U(MaxNumWays.W)
//     )
//     val tableAllocateEntry = Wire(new TageEntry)
//     tableAllocateEntry.valid := tableAllocateReq
//     tableAllocateEntry.tag := t2_rawTag(tableIdx) ^ tableAllocateDecision.allocateCfiPosition
//     tableAllocateEntry.distance := tableAllocateDecision.allocateDistance

//     val tableWeakReqByLoad = t2_loadDecisionVec.map { decision =>
//       decision.valid && decision.needAllWayWeak && decision.allWayWeakOH(tableIdx)
//     }
//     val tableWeakReq = tableWeakReqByLoad.reduce(_ || _)
//     val tableWeakDecision = WireDefault(0.U.asTypeOf(new NewMdpTageLoadDecision))
//     when(tableWeakReq) {
//       tableWeakDecision := Mux1H(PriorityEncoderOH(tableWeakReqByLoad), t2_loadDecisionVec)
//     }

//     val tableUpdateReqByWay = Wire(Vec(NumWays, Bool()))
//     val tableUpdateDecisionByWay = Wire(Vec(NumWays, new NewMdpTageLoadDecision))
//     (0 until NumWays).foreach { wayIdx =>
//       val updateReqByLoad = t2_loadDecisionVec.map { decision =>
//         decision.valid && decision.needUpdate && decision.trainNxOH(tableIdx) && decision.hitWayMaskOH(wayIdx)
//       }
//       tableUpdateReqByWay(wayIdx) := updateReqByLoad.reduce(_ || _)
//       tableUpdateDecisionByWay(wayIdx) := 0.U.asTypeOf(new NewMdpTageLoadDecision)
//       when(tableUpdateReqByWay(wayIdx)) {
//         tableUpdateDecisionByWay(wayIdx) := Mux1H(PriorityEncoderOH(updateReqByLoad), t2_loadDecisionVec)
//       }
//       when(t2_fire) {
//         assert(
//           PopCount(updateReqByLoad) <= 1.U,
//           cf"NewMdpTage table ${tableIdx} way ${wayIdx} has multiple update requesters"
//         )
//       }
//     }

//     val writeWayMask    = Wire(Vec(NumWays, Bool()))
//     val writeEntries    = Wire(Vec(NumWays, new TageEntry))
//     val writeUsefulCtrs = Wire(Vec(NumWays, UsefulCounter()))

//     (0 until NumWays).foreach { wayIdx =>
//       val updateReq = tableUpdateReqByWay(wayIdx)
//       val updateDecision = tableUpdateDecisionByWay(wayIdx)
//       val allocateEn = tableAllocateReq && tableAllocateWayOH(wayIdx)
//       val weakWayEn  = tableWeakReq && !updateReq && !allocateEn

//       writeWayMask(wayIdx) := updateReq || allocateEn
//       writeEntries(wayIdx) := Mux(allocateEn, tableAllocateEntry, updateDecision.updateEntry)
//       writeUsefulCtrs(wayIdx) := Mux(
//         allocateEn,
//         tableAllocateDecision.allocateUsefulCtr,
//         Mux(weakWayEn, tableWeakDecision.AllWayWeakUsefulCtrs(wayIdx), updateDecision.updateUsefulCtr)
//       )

//       when(t2_fire) {
//         assert(
//           PopCount(Cat(weakWayEn, allocateEn, updateReq)) <= 1.U,
//           cf"NewMdpTage table ${tableIdx} way ${wayIdx} has overlapping update/allocate/weak operations"
//         )
//       }
//     }

//     table.io.writeReq.valid           := t2_fire && writeWayMask.reduce(_ || _)
//     table.io.writeReq.bits.setIdx     := t2_setIdx(tableIdx)
//     table.io.writeReq.bits.bankMask   := t2_bankMask
//     table.io.writeReq.bits.wayMask    := writeWayMask.asUInt
//     table.io.writeReq.bits.entries    := writeEntries
//     table.io.writeReq.bits.usefulCtrs := writeUsefulCtrs

//     table.io.allWayWeakReq.valid           := tableWeakReq && t2_fire
//     table.io.allWayWeakReq.bits.setIdx     := t2_setIdx(tableIdx)
//     table.io.allWayWeakReq.bits.bankIdx    := OHToUInt(t2_bankMask)
//     table.io.allWayWeakReq.bits.usefulCtrs := writeUsefulCtrs
//     table.io.resetUseful := t2_fire && usefulResetCtr.isSaturatePositive
//   }

//   when(t2_fire) {
//     when(usefulResetCtr.isSaturatePositive) {
//       usefulResetCtr.resetZero()
//     }.elsewhen(t2_needAllocate && !t2_canAllocate) {
//       usefulResetCtr.selfIncrease()
//     }
//   }
// }
