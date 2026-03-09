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
*
*
* Acknowledgement
*
* This implementation is inspired by several key papers:
* [1] George Z. Chrysos, and Joel S. Emer. "[Memory dependence prediction using store sets.]
* (https://doi.org/10.1109/ISCA.1998.694770)" 25th Annual International Symposium on Computer Architecture (ISCA).
* 1998.
***************************************************************************************/

package xiangshan.mem.mdp.NewMdp

import org.chipsalliance.cde.config.Parameters
import chisel3._
import chisel3.util._
import xiangshan._
import utils._
import utility._
import xiangshan.frontend.PrunedAddr
import xiangshan.frontend.ftq.{FtqPtr,ResolveQueuePtr}
import xiangshan.frontend.bpu.StageCtrl
import xiangshan.frontend.bpu.SaturateCounter 
import xiangshan.frontend.bpu.HalfAlignHelper
import xiangshan.frontend.bpu.CompareMatrix
import xiangshan.frontend.bpu.tage.PhrToTageIO

// 一个支持多端口入队、单端口出队的队列
class MdpResolveQueue(implicit p: Parameters) extends XSModule with HasMdpParameters  with HalfAlignHelper with HasCircularQueuePtrHelper{
  val io = IO(new Bundle {
    val toMdpResolveUpdate = Input(Vec(backendParams.LduCnt + 1, Valid(new MdpUpdate))) // 多端口入队
    val updateStartPc = Input(Vec(backendParams.LduCnt + 1,PrunedAddr(VAddrBits)))
    //
    val mdpTrain = Decoupled((new MdpResolveEntry))  // 单端口出队

    val backendRedirect:    Bool   = Input(Bool())
    val backendRedirectPtr: FtqPtr = Input(new FtqPtr) 
    val bpuEnqueue:         Bool   = Input(Bool())     //TODO:?
    val bpuEnqueuePtr:      FtqPtr = Input(new FtqPtr)
  })

  private val mem = RegInit(VecInit(Seq.fill(MdpResolveQueueSize)(0.U.asTypeOf(Valid(new MdpResolveEntry)))))

  private val enqPtr = RegInit(ResolveQueuePtr(false.B, 0.U))
  private val deqPtr = RegInit(ResolveQueuePtr(false.B, 0.U))

  private val full = distanceBetween(enqPtr, deqPtr) >= (MdpResolveQueueSize - 4).U

  private val mdpResolve = io.toMdpResolveUpdate
  mdpResolve.zipWithIndex.foreach { case (entry, i) =>
    entry.valid := io.toMdpResolveUpdate(i).valid && io.toMdpResolveUpdate(i).bits.updateType =/= MdpPredictStatuses.NULL
  }

  private val hit = mdpResolve.map { load =>
    mem.map(entry =>
      load.valid && entry.valid && !entry.bits.flushed && entry.bits.ftqIdx === load.bits.ftqIdx
    ).reduce(_ || _)
  }
  private val hitIndex = mdpResolve.map { load =>
    mem.indexWhere(entry =>
      load.valid && entry.valid && !entry.bits.flushed && entry.bits.ftqIdx === load.bits.ftqIdx
    )
  }
  private val hitPrevious = mdpResolve.zipWithIndex.map { case (load, i) =>
    mdpResolve.take(i).map(previousload =>
      previousload.valid && load.valid && previousload.bits.ftqIdx === load.bits.ftqIdx
    )
  }
  private val needNewEntry = mdpResolve.zipWithIndex.map { case (load, i) =>
    load.valid && !hit(i) && !hitPrevious(i).fold(false.B)(_ || _)
  }

  private val enqIndex = WireDefault(VecInit.fill(backendParams.LduCnt + 1)(0.U(log2Ceil(MdpResolveQueueSize).W)))
  enqIndex := VecInit((0 until backendParams.LduCnt + 1).map { i =>
    val newIndex = MuxCase(
      (enqPtr + PopCount(needNewEntry.take(i))).value,
      hitPrevious(i).zipWithIndex.map { case (hit, j) => (hit, enqIndex(j)) }
    )

    Mux(hit(i), hitIndex(i), newIndex)
  })
  when(!full)(enqPtr := enqPtr + PopCount(needNewEntry))

  mdpResolve.zipWithIndex.foreach { case (load, i) =>
    when(load.valid && !full) {
      val startPc = io.updateStartPc(i)
      mem(enqIndex(i)).valid := true.B
      mem(enqIndex(i)).bits.ftqIdx  := load.bits.ftqIdx
      mem(enqIndex(i)).bits.startPc := startPc
      
      val firstEmpty = mem(enqIndex(i)).bits.loads.indexWhere(!_.valid)
      val loadSlot = mem(enqIndex(i)).bits.loads(firstEmpty + PopCount(hitPrevious(i)))
      loadSlot.valid := true.B
      loadSlot.bits.updateType  := load.bits.updateType
      loadSlot.bits.cfiPosition := getAlignedPosition(startPc, load.bits.ftqOffset)._1
      loadSlot.bits.distance    := load.bits.distance
    }
  }

  //TODO:
  // mem.foreach { entry =>
  //   when(entry.valid &&
  //     (backendRedirect.reduce(_ || _) && entry.bits.ftqIdx > backendRedirectPtr ||
  //       io.bpuEnqueue && entry.bits.ftqIdx.value === io.bpuEnqueuePtr.value)) {
  //     entry.bits.flushed := true.B
  //   }
  // }

  private val deqValid = mem(deqPtr.value).valid && !mdpResolve.map(branch =>
    branch.valid && branch.bits.ftqIdx === mem(deqPtr.value).bits.ftqIdx
  ).reduce(_ || _)

  io.mdpTrain.valid := deqValid && !mem(deqPtr.value).bits.flushed
  io.mdpTrain.bits  := mem(deqPtr.value).bits

  when(io.mdpTrain.fire || mem(deqPtr.value).valid && mem(deqPtr.value).bits.flushed) {
    deqPtr := deqPtr + 1.U

    mem(deqPtr.value).valid        := false.B
    mem(deqPtr.value).bits.flushed := false.B
    mem(deqPtr.value).bits.loads.foreach(_.valid := false.B)
  }
  
}


class MdpMASCOT(implicit p: Parameters) extends XSModule with HasMdpParameters {
  val io = IO(new Bundle {
    val stageCtrl  = Input(new StageCtrl)
    val fromBpu    = Input(new MdpToBpuIO)
    val fromPhr    = Input(new PhrToTageIO)

    val meta       = Output(new MdpBaseMeta)
    val basePred   = Output(Vec(NumMdpResultEntries, Valid(new MdpPrediction)))
    val finalPred  = Output(Vec(NumMdpResultEntries, Valid(new MdpPrediction)))

    val toTrain = Flipped(Decoupled(new MdpTrain))
    //
    // val csrCtrl = Input(new CustomCSRCtrlIO)
  })


  /* submodules */
  private val base = Module(new MdpTageBaseTable)
  private val tage = Module(new MdpTage)
  //base表预测distance和cfiPosition和置信度
  //tage表预测distance和置信度
  //base的第i项预测结果对应tage第二项，根据实际结果决定真正选择的信号

  val stageCtrl = WireDefault(io.stageCtrl)
  io.toTrain.ready  := base.io.trainReady && tage.io.trainReady

  //MDP
  //s0 stage
  base.io.stageCtrl := stageCtrl
  base.io.startPc   := io.fromBpu.startPc
  tage.io.stageCtrl := stageCtrl
  tage.io.startPc   := io.fromBpu.startPc
  tage.io.fromPhr.foldedPathHist := io.fromPhr.foldedPathHist
  //s1 stage
  private val s1_prediction = {
    val prediction = Wire(Vec(NumMdpResultEntries, Valid(new MdpPrediction)))
    (base.io.result zip prediction).map{ case (base, pred) =>
      pred.valid := base.valid
      // pred.bits  := Mux(tage.valid, tage.bits, base.bits)
      pred.bits.cfiPosition := base.bits.cfiPosition
      pred.bits.static      := base.bits.static
      pred.bits.loadWait    := base.bits.loadWait
      pred.bits.distance    := base.bits.distance
    }
    prediction
  }
  io.basePred := s1_prediction
  //s2 stage
  private val s2_baseResult = base.io.result
  tage.io.fromBaseResult := s2_baseResult

  private val s2_tageResult = tage.io.result
  io.meta := base.io.meta

  private val s2_takenMask = VecInit(s2_baseResult.zip(s2_tageResult).map{ case(base, tage) =>
    (base.valid && base.bits.loadWait) || (tage.valid && tage.bits.loadWait)
  })
  //s3 stage
  private val s3_baseResult = RegEnable(s2_baseResult, io.stageCtrl.s3_fire)
  private val s3_tageResult = RegEnable(s2_tageResult, io.stageCtrl.s3_fire)
  private val s3_takenMask  = RegEnable(s2_takenMask, io.stageCtrl.s3_fire)
  //TODO:静态预测器
  private val s3_prediction = {
    val prediction = Wire(Vec(NumMdpResultEntries, Valid(new MdpPrediction)))
    (s3_baseResult zip s3_tageResult zip prediction).map{ case ((base, tage), pred) =>
      pred.valid := Mux(tage.valid, true.B   , base.valid)
      // pred.bits  := Mux(tage.valid, tage.bits, base.bits)
      pred.bits.cfiPosition := base.bits.cfiPosition
      pred.bits.static   := Mux(tage.valid, tage.bits.static, base.bits.static)
      pred.bits.loadWait := Mux(tage.valid, tage.bits.loadWait, base.bits.loadWait)
      pred.bits.distance := Mux(tage.valid, tage.bits.distance, base.bits.distance)
    }
    prediction
  }

  base.io.s3_takenMask := s3_takenMask
  io.finalPred := s3_prediction 


  //t0 stage 
  stageCtrl.t0_fire := io.toTrain.fire //override
  private val train = WireDefault(io.toTrain.bits)
  private val t0_compareMatrix = CompareMatrix(VecInit(io.toTrain.bits.loads.map(_.bits.cfiPosition)))
  private val t0_firstMispredictMask = t0_compareMatrix.getLowerElementMask(
    VecInit(io.toTrain.bits.loads.map(b => b.valid && b.bits.misdependence))
  )
  train.loads.zipWithIndex.foreach { case (b, i) =>
    b.valid := io.toTrain.bits.loads(i).valid && t0_firstMispredictMask(i)
  }
  tage.io.fromPhr.foldedPathHistForTrain := io.fromPhr.foldedPathHistForTrain
  base.io.train := train
  tage.io.train := train

  val mdpTageTrainReadBankConflict = ~tage.io.trainReady && io.toTrain.valid
  XSPerfAccumulate("mdpTageTrainReadBankConflict", mdpTageTrainReadBankConflict)
}