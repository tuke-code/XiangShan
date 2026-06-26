package xiangshan.backend.rob

import chisel3._
import chisel3.util._
import org.chipsalliance.cde.config.Parameters
import utility.XSPerfAccumulate
import xiangshan._
import xiangshan.backend.fu.FuType
import xiangshan.backend.rob.RobBundles.RobEntryBundle
import xiangshan.backend.trace.Itype
import yunsuan.VfaluType

object RobPerfDebugHelper {
  def getPerfDebugInfo(uop: RobEntryBundle)(implicit p: Parameters): PerfDebugInfo = {
    uop.perfDebugInfo.getOrElse(0.U.asTypeOf(new PerfDebugInfo))
  }

  def latencySum(cond: Seq[Bool], latency: Seq[UInt]): UInt = {
    cond.zip(latency).map(x => Mux(x._1, x._2, 0.U)).reduce(_ +& _)
  }

  def addRobHeadWaitCounters(
    deqNotWritebacked: Bool,
    deqHeadInfoFuType: UInt,
    deqUopCommitType: UInt,
    debugDeqUop: RobEntryBundle
  )(implicit p: Parameters): Unit = {
    XSPerfAccumulate("waitAluCycle", deqNotWritebacked && deqHeadInfoFuType === FuType.alu.U)
    XSPerfAccumulate("waitMulCycle", deqNotWritebacked && deqHeadInfoFuType === FuType.mul.U)
    XSPerfAccumulate("waitDivCycle", deqNotWritebacked && deqHeadInfoFuType === FuType.div.U)
    XSPerfAccumulate("waitBrhCycle", deqNotWritebacked && deqHeadInfoFuType === FuType.brh.U)
    XSPerfAccumulate("waitJmpCycle", deqNotWritebacked && deqHeadInfoFuType === FuType.jmp.U)
    XSPerfAccumulate("waitCsrCycle", deqNotWritebacked && deqHeadInfoFuType === FuType.csr.U)
    XSPerfAccumulate("waitFenCycle", deqNotWritebacked && deqHeadInfoFuType === FuType.fence.U)
    XSPerfAccumulate("waitBkuCycle", deqNotWritebacked && deqHeadInfoFuType === FuType.bku.U)
    XSPerfAccumulate("waitLduCycle", deqNotWritebacked && deqHeadInfoFuType === FuType.ldu.U)
    XSPerfAccumulate("waitStuCycle", deqNotWritebacked && deqHeadInfoFuType === FuType.stu.U)
    XSPerfAccumulate("waitAtmCycle", deqNotWritebacked && deqHeadInfoFuType === FuType.mou.U)

    XSPerfAccumulate("waitVfaluCycle", deqNotWritebacked && deqHeadInfoFuType === FuType.vfalu.U)
    XSPerfAccumulate("waitVfmaCycle" , deqNotWritebacked && deqHeadInfoFuType === FuType.vfma.U)
    XSPerfAccumulate("waitVfdivCycle", deqNotWritebacked && deqHeadInfoFuType === FuType.vfdiv.U)

    val vfalufuop = Seq(VfaluType.vfadd, VfaluType.vfwadd, VfaluType.vfwadd_w, VfaluType.vfsub, VfaluType.vfwsub, VfaluType.vfwsub_w, VfaluType.vfmin, VfaluType.vfmax,
      VfaluType.vfmerge, VfaluType.vfmv, VfaluType.vfsgnj, VfaluType.vfsgnjn, VfaluType.vfsgnjx, VfaluType.vfeq, VfaluType.vfne, VfaluType.vflt, VfaluType.vfle, VfaluType.vfgt,
      VfaluType.vfge, VfaluType.vfclass, VfaluType.vfmv_f_s, VfaluType.vfmv_s_f, VfaluType.vfredusum, VfaluType.vfredmax, VfaluType.vfredmin, VfaluType.vfredosum, VfaluType.vfwredosum)

    vfalufuop.zipWithIndex.map{
      case(fuoptype,i) =>  XSPerfAccumulate(s"waitVfalu_${i}Cycle", deqNotWritebacked && deqHeadInfoFuType === fuoptype && deqHeadInfoFuType === FuType.vfalu.U)
    }

    XSPerfAccumulate("waitNormalCycle", deqNotWritebacked && deqUopCommitType === CommitType.NORMAL)
    XSPerfAccumulate("waitBranchCycle", deqNotWritebacked && Itype.isBranch(debugDeqUop.traceBlockInPipe.itype))
    XSPerfAccumulate("waitLoadCycle", deqNotWritebacked   && deqUopCommitType === CommitType.LOAD)
    XSPerfAccumulate("waitStoreCycle", deqNotWritebacked  && deqUopCommitType === CommitType.STORE)
  }
}
