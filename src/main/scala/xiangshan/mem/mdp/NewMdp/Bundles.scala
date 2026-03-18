package xiangshan.mem.mdp.NewMdp
import org.chipsalliance.cde.config.Parameters
import chisel3._
import chisel3.util._
import xiangshan._
import utils._
import utility._
import xiangshan.frontend.PrunedAddr
import xiangshan.frontend.ftq.FtqPtr
import xiangshan.frontend.bpu.StageCtrl
import xiangshan.frontend.bpu.SaturateCounter 
import xiangshan.frontend.bpu.WriteReqBundle
import xiangshan.frontend.bpu.history.phr.PhrMeta
import xiangshan.backend.rob.RobPtr

//ctrlFlow
class MdpPredictInfo(implicit p: Parameters) extends XSBundle with HasMdpParameters{ 
  val static   = Bool() // from static predictor
  val loadWait = Bool() // high : load is blocked
  val distance = UInt(RobDistance.W)
  def getWaitStoreRobIdx(loadRobIdx: RobPtr): RobPtr = loadRobIdx - distance
  //loadRobIdx - distance = storeRobIdx
}



//
class MdpToBpuIO(implicit p: Parameters) extends XSBundle with HasMdpParameters{
  val startPc = PrunedAddr(VAddrBits)
}

class Prediction(implicit p: Parameters) extends XSBundle with HasMdpParameters{
  val static   = Bool() // from static predictor
  val loadWait = Bool() // high : load is blocked
  val distance = UInt(RobDistance.W)
}

class BasePrediction(implicit p: Parameters) extends Prediction{
  val cfiPosition = UInt(CfiPositionWidth.W)
}

class TagePrediction(implicit p: Parameters) extends Prediction{}

class MdpPrediction(implicit p: Parameters)  extends Prediction{
  val cfiPosition = UInt(CfiPositionWidth.W)
}

class LoadInfo(implicit p: Parameters) extends XSBundle with HasMdpParameters{
  val updateType  = UInt(MdpUpdateType.width.W)
  val cfiPosition = UInt(CfiPositionWidth.W)
  val distance    = UInt(RobDistance.W)
  def misdependence: Bool = this.updateType === MdpUpdateType.M_WZ || this.updateType === MdpUpdateType.M_AS
  /* three type for misdependence(type: M_WZ、M_AS)
    1. static predict no-dependence ,real dependence
    2. mdp predict no-dependence ,real dependence
    3. mdp predict dependence ,real dependence other address
  */
}


class MdpMetaEntry(implicit p: Parameters) extends XSBundle with HasMdpParameters{
  val rawHit      = Bool()
  val counter     = UsefulCounter()
  val cfiPosition = UInt(CfiPositionWidth.W)

  def hit(load: LoadInfo): Bool = rawHit && cfiPosition === load.cfiPosition
}

class MdpBaseMeta(implicit p: Parameters) extends XSBundle with HasMdpBaseTableParameters{
  val entries = Vec(BaseNumAlignBanks, Vec(BaseNumWays, new MdpMetaEntry))
}

class MdpMeta(implicit p: Parameters) extends XSBundle with HasMdpParameters{
  val base = new MdpBaseMeta
  val phr  = new PhrMeta
}

class MdpTrain(implicit p: Parameters) extends XSBundle with HasMdpParameters{
  val startPc = PrunedAddr(VAddrBits)
  val loads   = Vec(ResolveEntryBranchNumber, Valid(new LoadInfo))
  val meta    = new MdpMeta
  def mispredictLoad: Valid[LoadInfo] =
    Mux1H(loads.map(b => (b.valid && b.bits.misdependence, b)))
}

class MdpUpdate(implicit p: Parameters) extends XSBundle with HasMdpParameters with HasCircularQueuePtrHelper{
  //loadpc、loadFtqIdx、loadFtqOffset
  val pc          = PrunedAddr(VAddrBits)
  val ftqIdx      = new FtqPtr()
  val ftqOffset   = UInt(FetchBlockInstOffsetWidth.W) 
  val updateType  = UInt(MdpUpdateType.width.W)
  val distance    = UInt(RobDistance.W)      //loadRobIdx - distance = storeRobIdx
  def getDistance[T <: CircularQueuePtr[T]](enq_ptr: T, deq_ptr: T): UInt = 
    distanceBetween(enq_ptr, deq_ptr)
  def updateIsNull:           Bool = updateType === MdpUpdateType.NULL
  def updateIsWriteZero:      Bool = updateType === MdpUpdateType.M_WZ
  def updateIsAllocateWeak:   Bool = updateType === MdpUpdateType.M_AW
  def updateIsAllocateStrong: Bool = updateType === MdpUpdateType.M_AS
  def updateIsNxStrong:       Bool = updateType === MdpUpdateType.M_IS
  def updateIsNxWeak:         Bool = updateType === MdpUpdateType.M_IW
}

class MdpResolveEntry(implicit p: Parameters) extends XSBundle with HasMdpParameters{
  val ftqIdx = new FtqPtr
  val flushed = Bool()
  val startPc = PrunedAddr(VAddrBits)
  val loads = Vec(ResolveEntryLoadNumbers, Valid(new LoadInfo))
}
  /* --------------------------------------------------------------------------------------------------------------
    TageBaseTable 
    -------------------------------------------------------------------------------------------------------------- */


class BaseTableCounterSramWriteReq(implicit p: Parameters) extends XSBundle  with HasMdpBaseTableParameters {
  val setIdx:   UInt                 = UInt(BaseSetIdxLen.W)
  val wayMask:  UInt                 = UInt(BaseNumWays.W)
  val counters: Vec[SaturateCounter] = Vec(BaseNumWays, TakenCounter())
}

class BaseTableEntrySramWriteReq(implicit p: Parameters) extends WriteReqBundle with HasMdpBaseTableParameters {
  val setIdx:       UInt           = UInt(BaseSetIdxLen.W)
  val entry:        BaseTableEntry = new BaseTableEntry
  override def tag: Option[UInt] = Some(Cat(entry.tag, entry.cfiPosition)) // use entry's tag directly
}

class BaseTableEntry(implicit p: Parameters) extends XSBundle  with HasMdpBaseTableParameters {
  val valid:       Bool           = Bool()
  val tag  :       UInt           = UInt(BaseTagWidth.W)
  val cfiPosition: UInt           = UInt(CfiPositionWidth.W)
  val distance:    UInt           = UInt(RobDistance.W)
}

  /* --------------------------------------------------------------------------------------------------------------
    TageTable 
    -------------------------------------------------------------------------------------------------------------- */

class MdpTageResp(implicit p: Parameters)  extends XSBundle with HasMdpParameters{
  val loadWait = Vec(FetchMdpWidth, Bool())
  val distance = Vec(FetchMdpWidth, UInt(RobDistance.W))
}
