package xiangshan.mem.mdp.NewMdp

import org.chipsalliance.cde.config.Parameters
import chisel3._
import chisel3.util._
import chisel3.experimental.BundleLiterals._
import freechips.rocketchip.util.SeqToAugmentedSeq
import xiangshan._
import utils._
import utility._
import xiangshan.frontend.PrunedAddr
import xiangshan.frontend.bpu.StageCtrl
import xiangshan.frontend.bpu.SaturateCounter
import xiangshan.frontend.bpu.tage.PhrToTageIO
import xiangshan.frontend.bpu.tage.{Tage,UsefulResetCounter}


class PredictTagMatchResult(implicit p: Parameters) extends XSBundle with HasMdpTageTableParameters {
  val hit:          Bool            = Bool()
  val hitWayMaskOH: UInt            = UInt(MaxNumWays.W)
  val usefulCtr:    SaturateCounter = UsefulCounter()
  val distance :    UInt            = UInt(RobDistance.W)
}

class TrainTagMatchResult(implicit p: Parameters) extends XSBundle with HasMdpTageTableParameters {
  val hit:          Bool            = Bool()
  val hitWayMaskOH: UInt            = UInt(MaxNumWays.W)
  val wayFull:      Bool            = Bool()
  val tag:          UInt            = UInt(TagWidth.W)
  val usefulCtr:    SaturateCounter = UsefulCounter()
  val distance :    UInt            = UInt(RobDistance.W)
  val allWayWeakUsefulCtrs: Vec[SaturateCounter] = Vec(MaxNumWays,UsefulCounter())
}


class TrainInfo(implicit p: Parameters) extends XSBundle with HasMdpTageTableParameters {
  val valid = Bool()
  val trainNxOH    = UInt(NumTables.W)
  val hitWayMaskOH = UInt(MaxNumWays.W)
  val allocateNxOH = UInt(NumTables.W)
  val updateEntry  = new TageEntry
  val canAllocate = Bool()
  //
  val needUpdate         = Bool()
  val needAllocate       = Bool()
  val needAllWayWeak     = Bool()
  val allocateUsefulCtr  = UsefulCounter()
  val updateUsefulCtr    = UsefulCounter()
  val AllWayWeakUsefulCtrs = Vec(MaxNumWays,UsefulCounter())
}

object LoadType extends EnumUInt(6) {
  def None:           UInt = 0.U(width.W)
  def AllocateWeak:   UInt = 1.U(width.W)
  def AllocateStrong: UInt = 2.U(width.W)
  def Weak:           UInt = 3.U(width.W)
  def Strong:         UInt = 4.U(width.W)
  def AllWayWeak:     UInt = 5.U(width.W)
}

class LoadTrainType extends Bundle {
  val loadType: UInt = LoadType()
  def isNone:           Bool = loadType === LoadType.None
  def isAllocateWeak:   Bool = loadType === LoadType.AllocateWeak   //分配一个弱（2）项
  def isAllocateStrong: Bool = loadType === LoadType.AllocateStrong //分配一个强（6）项
  def isWeak:           Bool = loadType === LoadType.Weak           //对该长度该way进行弱化
  def isStrong:         Bool = loadType === LoadType.Strong         //对该长度该way进行强化
  def isAllWayWeak:     Bool = loadType === LoadType.AllWayWeak     //对该长度所有way进行弱化
}

//不考虑meta
class MdpTage(implicit p: Parameters) extends XSModule with TopHelper{
  val io = IO(new Bundle {
    val stageCtrl = Input(new StageCtrl)
    val startPc   = Input(new PrunedAddr(VAddrBits))
    val train     = Input(new MdpTrain)
    val trainReady= Output(Bool())

    val fromPhr = new PhrToTageIO
    val fromBaseResult = Input(Vec(NumMdpResultEntries, Valid(new BasePrediction)))

    val result = Output(Vec(NumMdpResultEntries, Valid(new TagePrediction)))
    // val meta   = Output(Vec(NumMdpResultEntries, Valid(new MdpTageMeta))) //TODO: delete it or add it
  })

  /* *** submodules *** */
  private val tables = TableInfos.zipWithIndex.map { case (info, i) => Module(new MdpTageTable(i, info)) }

  // reset usefulCtr of all entries when usefulResetCtr saturated
  private val usefulResetCtr = RegInit(UsefulResetCounter.Zero)
  /* --------------------------------------------------------------------------------------------------------------
     predict pipeline stage 0
     - send read request to tables
     -------------------------------------------------------------------------------------------------------------- */

  private val s0_fire    = io.stageCtrl.s0_fire
  private val s0_startPc = io.startPc

  private val s0_foldedHist = getFoldedHist(io.fromPhr.foldedPathHist)
  private val s0_setIdx = VecInit((tables zip s0_foldedHist).map { case (table, hist) =>
    table.getSetIndex(s0_startPc, hist.forIdx)
  })

  private val s0_bankIdx  = tables.head.getBankIndex(s0_startPc)
  private val s0_bankMask = UIntToOH(s0_bankIdx, NumBanks)

  tables.zipWithIndex.foreach { case (table, tableIdx) =>
    table.io.predictReadReq.valid         := s0_fire
    table.io.predictReadReq.bits.setIdx   := s0_setIdx(tableIdx)
    table.io.predictReadReq.bits.bankMask := s0_bankMask
  }
  /* --------------------------------------------------------------------------------------------------------------
     predict pipeline stage 1
     - get read data from tables
     - compute temp tag
     -------------------------------------------------------------------------------------------------------------- */

  private val s1_fire       = io.stageCtrl.s1_fire
  private val s1_startPc    = RegEnable(s0_startPc, s0_fire)
  private val s1_foldedHist = RegEnable(s0_foldedHist, s0_fire)

  // A tag without branch position, position will be hashed into after BTB result
  private val s1_rawTag = VecInit((tables zip s1_foldedHist).map { case (table, hist) =>
    table.getRawTag(s1_startPc, hist.forTag)
  })

  private val s1_readResp = DataHoldBypass(VecInit(tables.map(_.io.predictReadResp)), RegNext(s0_fire))

  /* --------------------------------------------------------------------------------------------------------------
     predict pipeline stage 2
     - get results from base
     - get prediction for each branch
     -------------------------------------------------------------------------------------------------------------- */

  private val s2_fire     = io.stageCtrl.s2_fire
  private val s2_startPc  = RegEnable(s1_startPc, s1_fire)
  private val s2_rawTag   = RegEnable(s1_rawTag, s1_fire)
  private val s2_readResp = RegEnable(s1_readResp, s1_fire)

  private val s2_loads = io.fromBaseResult
  s2_loads.zipWithIndex.foreach { case (load, i) =>
    val position = load.bits.cfiPosition
    val allTableTagMatchResults = s2_readResp.zipWithIndex.map { case (tableReadResp, tableIdx) =>
      val tag          = s2_rawTag(tableIdx) ^ position
      val hitWayMask   = tableReadResp.entries.map(entry => entry.valid && entry.tag === tag)
      val hitWayMaskOH = PriorityEncoderOH(hitWayMask)

      val result = Wire(new PredictTagMatchResult).suggestName(s"s2_load_${i}_table_${tableIdx}_result")
      result.hit          := hitWayMask.reduce(_ || _)
      result.hitWayMaskOH := hitWayMaskOH.asUInt
      result.usefulCtr    := Mux1H(hitWayMaskOH, tableReadResp.usefulCtrs)
      result.distance     := Mux1H(hitWayMaskOH, tableReadResp.entries).distance
      result
    }
    val hitTableMask = allTableTagMatchResults.map(_.hit)
    val hitTable     = hitTableMask.reduce(_ || _)
    dontTouch(hitTableMask.asUInt.suggestName(s"t2_load_${i}_hitTableMask"))
    val longestHistTableOH  = getLongestHistTableOH(hitTableMask)
    val prediction = Mux1H(longestHistTableOH, allTableTagMatchResults)
    when(s2_fire) {
      assert(PopCount(longestHistTableOH) <= 1.U, "Multiple tables hit in prediction")
    }

    io.result(i).valid := hitTable
    io.result(i).bits.distance := prediction.distance
    io.result(i).bits.static   := ~hitTable
    io.result(i).bits.loadWait := prediction.usefulCtr.isPositive
  }

  /* --------------------------------------------------------------------------------------------------------------
     train pipeline stage 0
     - send train request to base table
     - send read request to tables
     -------------------------------------------------------------------------------------------------------------- */

  private val t0_startPc  = io.train.startPc
  private val t0_loads    = io.train.loads

  private val t0_bankIdx  = tables.head.getBankIndex(t0_startPc)
  private val t0_bankMask = UIntToOH(t0_bankIdx, NumBanks)

  private val t0_loadMask = t0_loads.map(load => load.valid)
  private val t0_hasLoad  = t0_loadMask.reduce(_ || _)

  private val t0_fire     = io.stageCtrl.t0_fire && t0_hasLoad 

  private val t0_needRead = true.B
  private val t0_readBankConflict = t0_hasLoad && t0_needRead && s0_fire && t0_bankIdx === s0_bankIdx
  io.trainReady := !t0_readBankConflict
  private val t0_foldedHist = getFoldedHist(io.fromPhr.foldedPathHist)
  private val t0_setIdx     = VecInit((tables zip t0_foldedHist).map { case (table, hist) =>
    table.getSetIndex(t0_startPc, hist.forIdx)
  })
  dontTouch(t0_setIdx)

  tables.zipWithIndex.foreach { case (table, tableIdx) =>
    table.io.trainReadReq.valid         := t0_fire && t0_needRead
    table.io.trainReadReq.bits.setIdx   := t0_setIdx(tableIdx)
    table.io.trainReadReq.bits.bankMask := t0_bankMask
  }

  when(t0_fire) {
    t0_loads.zipWithIndex.foreach { case (load, i) =>
      when(load.valid) {
        // assert(load.bits.updateType =/= MdpUpdateType.NULL, s"Load ${i} updateType should not be NULL when valid")
      }
    }
  }
  /* --------------------------------------------------------------------------------------------------------------
     train pipeline stage 1
     - get read data from tables
     - compute temp tag
     -------------------------------------------------------------------------------------------------------------- */

  private val t1_fire     = RegNext(t0_fire)
  private val t1_startPc  = RegEnable(t0_startPc, t0_fire)
  private val t1_loads    = RegEnable(t0_loads, t0_fire)

  private val t1_setIdx   = RegEnable(t0_setIdx, t0_fire)
  private val t1_bankMask = RegEnable(t0_bankMask, t0_fire)

  private val t1_foldedHist = RegEnable(t0_foldedHist, t0_fire)
  private val t1_rawTag = VecInit((tables zip t1_foldedHist).map { case (table, hist) =>
    table.getRawTag(t1_startPc, hist.forTag)
  })

  private val t1_readResp = VecInit(tables.map(_.io.trainReadResp))
  /* --------------------------------------------------------------------------------------------------------------
     train pipeline stage 2
     - update Loades' takenCtr and usefulCtr
     - allocate a new entry when mispredict
     -------------------------------------------------------------------------------------------------------------- */
  private val t2_fire     = RegNext(t1_fire)
  private val t2_startPc  = RegEnable(t1_startPc, t1_fire)
  private val t2_loads    = RegEnable(t1_loads, t1_fire)

  private val t2_setIdx   = RegEnable(t1_setIdx, t1_fire)
  private val t2_bankMask = RegEnable(t1_bankMask, t1_fire)
  private val t2_rawTag   = RegEnable(t1_rawTag, t1_fire)
  private val t2_readResp = RegEnable(t1_readResp, t1_fire)

  
  //resolveQ保证传给Mdp的信号在misPredict之后都是无效的，需要结合valid使用(已完成)
  //TODO:淘汰机制不完善，valid只是这个表项被使用了，不是这个表的数据真正有效（例如饱和技术为0的时候，这个表项应该不再有效了）
  //TODO:load.valid信号是不是没有覆盖全面呢
  private val t2_trainInfoVec = t2_loads.zipWithIndex.map { case (load, i) =>
    val tageTableTagMatchResults = t2_readResp.zipWithIndex.map { case (tableReadResp, tableIdx) =>
      val position     = load.bits.cfiPosition
      val tag          = t2_rawTag(tableIdx) ^ position
      val hitWayMask   = tableReadResp.entries.map(entry => entry.valid && entry.tag === tag)
      val hitWayMaskOH = PriorityEncoderOH(hitWayMask)
      dontTouch(tag.suggestName(s"t2_load_${i}_table_${tableIdx}_tag"))
      val result = Wire(new TrainTagMatchResult).suggestName(s"t2_Load_${i}_table_${tableIdx}_result")
      result.hit          := hitWayMask.reduce(_ || _)
      result.hitWayMaskOH := hitWayMaskOH.asUInt
      result.wayFull      := tableReadResp.entries.zip(tableReadResp.usefulCtrs).map { case (entry, ctr) =>
        entry.valid && ctr.isSaturateNegative
      }.reduce(_ && _)
      result.tag          := tag
      result.usefulCtr    := Mux1H(hitWayMaskOH, tableReadResp.usefulCtrs)
      result.distance     := Mux1H(hitWayMaskOH, tableReadResp.entries).distance
      result.allWayWeakUsefulCtrs := VecInit(tableReadResp.usefulCtrs.map(ctr => ctr.getDecrease())) //for AllWayWeakUsefulCtrs
      result
    }
    val tageHitTableMask = tageTableTagMatchResults.map(_.hit)
    val tageWayFullMask  = tageTableTagMatchResults.map(_.wayFull)
    val longestHistTableIdx = getLongestHistTableIdx(tageHitTableMask)
    val longestHistTableOH  = getLongestHistTableOH(tageHitTableMask)
    val allocateNxType   = WireDefault((LoadType.None).asTypeOf(new LoadTrainType))
    val trainNxType      = WireDefault((LoadType.None).asTypeOf(new LoadTrainType))
    dontTouch(tageHitTableMask.asUInt.suggestName(s"t2_Load_${i}_hitTableMask"))
    val provider = Mux1H(longestHistTableOH.asUInt, tageTableTagMatchResults)
    val needAllocateWeak   = allocateNxType.isAllocateWeak   && load.bits.misdependence //NOTE: mis保证了多项Allocate的情况下，只有一项真正allocate了
    val needAllocateStrong = allocateNxType.isAllocateStrong && load.bits.misdependence
    val needAllWayWeak     = allocateNxType.isAllWayWeak     && load.bits.misdependence
    val notNeedAllWayWeak  = tageHitTableMask.reduce(_ || _) && trainNxType.isAllWayWeak && VecInit(
      provider.allWayWeakUsefulCtrs.map(ctr => ctr.isSaturateNegative)
    ).reduce(_ && _)
    val notNeedUpdate      = tageHitTableMask.reduce(_ || _) && ((trainNxType.isStrong && provider.usefulCtr.isSaturatePositive) 
                                                              || (trainNxType.isWeak   && provider.usefulCtr.isSaturateNegative))
    //
    val usefulCtrUpdate = trainNxType.isStrong || trainNxType.isWeak
    val newUsefulCtr = provider.usefulCtr.getUpdate(increase = trainNxType.isStrong,en = usefulCtrUpdate)

    val trainInfo = Wire(new TrainInfo).suggestName(s"t2_Load_${i}_trainInfo")
    trainInfo.valid           := tageHitTableMask.reduce(_ || _) || needAllocateWeak || needAllocateStrong
    trainInfo.trainNxOH       := longestHistTableOH.asUInt
    trainInfo.hitWayMaskOH    := provider.hitWayMaskOH
    trainInfo.allocateNxOH    := 0.U
    trainInfo.needAllocate    := needAllocateWeak || needAllocateStrong
    trainInfo.canAllocate     := false.B
    //感觉notNeedWrite挺关键的，因为有冲突存在
    trainInfo.needAllWayWeak  := needAllWayWeak && !notNeedAllWayWeak
    trainInfo.needUpdate      := tageHitTableMask.reduce(_ || _)  && !needAllWayWeak && !notNeedUpdate
    trainInfo.allocateUsefulCtr := Mux(needAllocateStrong, UsefulCounter.InitStrong,
                                    Mux(needAllocateWeak, UsefulCounter.InitWeak, UsefulCounter.Init))
    trainInfo.updateUsefulCtr   := newUsefulCtr
    trainInfo.updateEntry.valid := !tageHitTableMask.reduce(_ || _)
    trainInfo.updateEntry.tag   := provider.tag
    trainInfo.updateEntry.distance := provider.distance
    trainInfo.AllWayWeakUsefulCtrs := provider.allWayWeakUsefulCtrs


    switch(load.bits.updateType){
      is(MdpUpdateType.M_WZ){
        // trainBaseType.loadType := LoadType.AllocateStrong
        trainNxType.loadType   := LoadType.None
      }
      is(MdpUpdateType.M_AW){
        when(tageHitTableMask.asUInt.orR){ //命中NX
          // trainBaseType        := LoadType.None
          trainNxType.loadType    := LoadType.AllWayWeak 
          allocateNxType.loadType := LoadType.AllocateWeak
          trainInfo.allocateNxOH  := getFirstNonFullTableOH(longestHistTableOH, tageWayFullMask)._1
          trainInfo.canAllocate   := getFirstNonFullTableOH(longestHistTableOH, tageWayFullMask)._2
          //Allocate and weak contain both actions, so two identities are required to indicate both actions
        }.otherwise{ //hit Base
          // trainBaseType.loadType := LoadType.Weak
          allocateNxType.loadType := LoadType.AllocateWeak
          trainInfo.allocateNxOH  := PriorityEncoderOH(~tageWayFullMask.asUInt) //get first not full idx to allocate
          trainInfo.canAllocate   := getFirstNonFullTableOH(Seq.fill(NumTables)(true.B), tageWayFullMask)._2
        }
      }
      is(MdpUpdateType.M_AS){
        when(tageHitTableMask.asUInt.orR){ //命中NX
          // trainBaseType.loadType        := LoadType.None
          trainNxType.loadType    := LoadType.AllWayWeak
          allocateNxType.loadType := LoadType.AllocateStrong
          trainInfo.allocateNxOH  := getFirstNonFullTableOH(longestHistTableOH, tageWayFullMask)._1
          trainInfo.canAllocate   := getFirstNonFullTableOH(longestHistTableOH, tageWayFullMask)._2
          //Allocate and weak contain both actions, so two identities are required to indicate both actions
        }.otherwise{ //hit Base
          // trainBaseType        := LoadType.Weak
          allocateNxType.loadType := LoadType.AllocateStrong
          trainInfo.allocateNxOH  := PriorityEncoderOH(~tageWayFullMask.asUInt) //get first not full idx to allocate
          trainInfo.canAllocate   := getFirstNonFullTableOH(Seq.fill(NumTables)(true.B), tageWayFullMask)._2
        }
      }
      is(MdpUpdateType.M_IS){
        when(tageHitTableMask.asUInt.orR){ //命中NX
          trainNxType.loadType   := LoadType.Strong
        }.otherwise{
          // trainBaseType.loadType := LoadType.Strong
        }
      }
      is(MdpUpdateType.M_IW){
        when(tageHitTableMask.asUInt.orR){ //命中NX
          trainNxType.loadType   := LoadType.Weak
        }.otherwise{
          // trainBaseType.loadType := LoadType.Weak
        }
      }
    }
    trainInfo
  }


  when(t2_fire) {
    t2_trainInfoVec.zipWithIndex.foreach { case (info, i) =>
      when(info.valid && ~info.needAllocate) {
        // 检查valid trainInfo的基本一致性
        assert(info.trainNxOH.orR, s"Train info ${i} trainNxOH should be valid when info is valid")
        assert(info.hitWayMaskOH.orR, s"Train info ${i} hitWayMaskOH should be valid when info is valid")
        
      }
      when(info.valid && info.needAllocate) {
        // 如果needAllocate，必须要有有效的分配信息
        assert(info.allocateNxOH.orR, s"Train info ${i} allocateNxOH should be valid when needAllocate")
      }
    }
  }
  val mdpTageTrainCnt = PopCount(t2_trainInfoVec.map(info => info.valid && t2_fire))
  val mdpTageTrainAllocate = PopCount(t2_trainInfoVec.map(info => info.valid && info.needAllocate && t2_fire))
  val mdpTageTrainAllWayWeak = PopCount(t2_trainInfoVec.map(info => info.valid && info.needAllWayWeak && t2_fire))
  val mdpTageTrainUpdate = PopCount(t2_trainInfoVec.map(info => info.valid && info.needUpdate && t2_fire))

  XSPerfAccumulate("mdp_tage_train_cnt", mdpTageTrainCnt)
  XSPerfAccumulate("mdp_tage_train_allocate", mdpTageTrainAllocate)
  XSPerfAccumulate("mdp_tage_train_all_way_weak", mdpTageTrainAllWayWeak)
  XSPerfAccumulate("mdp_tage_train_update", mdpTageTrainUpdate)

  //allocate
  private val t2_needAllocateLoadOH = t2_trainInfoVec.map(info => info.valid && info.needAllocate)
  
  when(t2_fire) {
    assert(PopCount(t2_needAllocateLoadOH) <= 1.U)
  }
  private val t2_needAllocate          = t2_needAllocateLoadOH.reduce(_ || _)
  private val t2_allocateLoad          = Mux1H(t2_needAllocateLoadOH, t2_loads)
  private val t2_allocateLoadTrainInfo = Mux1H(t2_needAllocateLoadOH, t2_trainInfoVec)
  private val t2_tageTableCanAllocateWayMask = t2_readResp.map { tableReadResp =>
    tableReadResp.entries.zip(tableReadResp.usefulCtrs).map { case (entry, usefulCtr) =>
      !entry.valid || entry.valid && usefulCtr.isSaturateNegative
    }.asUInt
  }
  private val t2_canAllocate = t2_allocateLoadTrainInfo.canAllocate //TODO:canAllocate 没考虑usefulctr的情况
  private val t2_allocate = t2_needAllocate && t2_canAllocate       //TODO: can allocate

  private val t2_allocateTableOH     = t2_allocateLoadTrainInfo.allocateNxOH
  private val t2_allocateWayMask     = Mux1H(t2_allocateTableOH,t2_tageTableCanAllocateWayMask)
  private val t2_allocateWayOH       = PriorityEncoderOH(t2_allocateWayMask)
  dontTouch(t2_allocateTableOH)
  dontTouch(t2_allocateWayOH)
  private val t2_allocateEntry = {
    val rawTag      = Mux1H(t2_allocateTableOH, t2_rawTag)
    val position    = t2_allocateLoad.bits.cfiPosition
    val entry       = Wire(new TageEntry)
    entry.valid := true.B
    entry.tag   := rawTag ^ position
    entry.distance := t2_allocateLoad.bits.distance
    entry
  }
  when(t2_fire && t2_allocate) {
    assert(t2_allocateTableOH.orR, "Allocate table OH should be valid when allocating")
    assert(t2_allocateWayOH.orR, "Allocate way OH should be valid when allocating")
    
    // 确保选择的way确实可用
    val allocatedTableIdx = OHToUInt(t2_allocateTableOH)
    val allocatedWayIdx = OHToUInt(t2_allocateWayOH)
    val wayIsAvailable = Mux1H(t2_allocateTableOH, t2_tageTableCanAllocateWayMask)(allocatedWayIdx)
    assert(wayIsAvailable, cf"Allocated way ${allocatedWayIdx} in table ${allocatedTableIdx} should be available")
    
    // 确保allocate的entry是有效的
    assert(t2_allocateEntry.valid, "Allocated entry should be valid")
    assert(t2_allocateEntry.tag =/= 0.U, "Allocated entry tag should not be zero")
  }

  //NOTE:Allocate也划分为AllocateWeak和AllocateStrong
  //一个table表遍历需要覆盖所有情况？
  tables.zipWithIndex.foreach { case (table, tableIdx) =>
    implicit val info: MdpTageTableInfo = TableInfos(tableIdx) // used by NumWays

    val writeWayMask    = Wire(Vec(NumWays, Bool()))
    val writeEntries    = Wire(Vec(NumWays, new TageEntry))
    val writeUsefulCtrs = Wire(Vec(NumWays, UsefulCounter()))
    val allWayNeedWeakMask = t2_trainInfoVec.map { info =>
      info.valid && info.needAllWayWeak && info.trainNxOH(tableIdx) 
    }
    (0 until NumWays).foreach { wayIdx =>
      val hitMask = t2_trainInfoVec.map { info =>
        info.valid && info.needUpdate && info.trainNxOH(tableIdx) && info.hitWayMaskOH(wayIdx)
      }
      when(t2_fire) {
        assert(PopCount(hitMask) <= 1.U)
      }
      val updateEn = hitMask.reduce(_ || _)
      val allocateEn = t2_allocate && t2_allocateTableOH(tableIdx) && t2_allocateWayOH(wayIdx)
      val weakWayEn  = allWayNeedWeakMask.reduce(_ || _)
      val allocateUsefulCtr = Mux1H(hitMask, t2_trainInfoVec).allocateUsefulCtr
      val updateEntry       = Mux1H(hitMask, t2_trainInfoVec).updateEntry
      val updateUsefulCtr   = Mux1H(hitMask, t2_trainInfoVec).updateUsefulCtr
      val weakWayEntry      = Mux1H(allWayNeedWeakMask, t2_trainInfoVec).updateEntry
      val weakWayUsefulCtr  = Mux1H(allWayNeedWeakMask, t2_trainInfoVec).AllWayWeakUsefulCtrs(wayIdx)
      writeWayMask(wayIdx)    := updateEn || allocateEn || weakWayEn
      writeEntries(wayIdx)    := Mux(allocateEn, t2_allocateEntry,
                                  Mux(weakWayEn, weakWayEntry, updateEntry))
      writeUsefulCtrs(wayIdx) := Mux(allocateEn, allocateUsefulCtr,
                                  Mux(weakWayEn, weakWayUsefulCtr, updateUsefulCtr))
      when(t2_fire) {
        val operations = Seq(updateEn, allocateEn, weakWayEn).map(_.asUInt)
        val opCount = PopCount(operations.reduce(_ | _))
        assert(opCount <= 1.U, cf"Multiple write operations on table ${tableIdx} way ${wayIdx}: update=${updateEn}, allocate=${allocateEn}, weak=${weakWayEn}")
      }
    }
    table.io.writeReq.valid                := t2_fire && writeWayMask.reduce(_ || _)
    table.io.writeReq.bits.setIdx          := t2_setIdx(tableIdx)
    table.io.writeReq.bits.bankMask        := t2_bankMask
    table.io.writeReq.bits.wayMask         := writeWayMask.asUInt
    table.io.writeReq.bits.entries         := writeEntries
    table.io.writeReq.bits.usefulCtrs      := writeUsefulCtrs

    table.io.resetUseful := t2_fire && usefulResetCtr.isSaturatePositive
  }
  
  when(t2_fire) {
    when(usefulResetCtr.isSaturatePositive) {
      usefulResetCtr.resetZero()
    }.elsewhen(t2_needAllocate && !t2_canAllocate) {
      usefulResetCtr.selfIncrease()
    }
  }

}