package xiangshan.backend.regfile

import chisel3._
import chisel3.util._
import chisel3.util.experimental.decode._
import utility._

class V0RegFile(readProtCount: Int, WriteProtCount: Int, VLEN: Int, V0RegCount: Int) extends Module {
  private val rowWidth           = VLEN / 8
  private val rows               = VLEN / rowWidth
  private val wIdxBitWidth       = log2Up(rows)
  private val rIdxBitWidth       = log2Up(rows)
  private val wDataWidthBitWidth = log2Up(log2Up(rowWidth) + 1)
  private val rLineCountBitWidth = log2Up(log2Up(rows) + 1)
  private val RegSelBitWidth     = log2Up(V0RegCount)
  require(isPow2(VLEN), "V0reg VLEN is not Pow2")
  require(isPow2(rowWidth), "V0reg rowWidth is not Pow2")
  require(isPow2(rows), "V0reg rows is not Pow2")
  require(isPow2(V0RegCount), "V0reg V0RegNum is not Pow2")

  val readPorts = IO(Vec(
    readProtCount,
    new V0Reg.V0RegReadPort(RegSelBitWidth + rIdxBitWidth, rLineCountBitWidth, rowWidth)
  ))
  val writePorts = IO(Vec(
    WriteProtCount,
    new V0Reg.V0RegWritePort(RegSelBitWidth + wIdxBitWidth, wDataWidthBitWidth, rowWidth)
  ))

  val V0Regs      = Seq.fill(V0RegCount)(Module(new V0Reg(readProtCount, WriteProtCount, VLEN)))
  val readRegData = Wire(Vec(V0RegCount, Vec(readProtCount, UInt(rowWidth.W))))
  val wRegSel     = Wire(Vec(WriteProtCount, UInt(RegSelBitWidth.W)))
  val rRegSel     = Wire(Vec(readProtCount, UInt(RegSelBitWidth.W)))
  for (i <- 0 until WriteProtCount) {
    wRegSel(i) := writePorts(i).wIdx(RegSelBitWidth + wIdxBitWidth - 1, wIdxBitWidth)
  }
  for (i <- 0 until readProtCount) {
    rRegSel(i)          := readPorts(i).rIdx(RegSelBitWidth + rIdxBitWidth - 1, rIdxBitWidth)
    readPorts(i).rData := readRegData(rRegSel(i))(i)
  }
  for {
    regId   <- 0 until V0RegCount
    wPortId <- 0 until WriteProtCount
  } yield {
    V0Regs(regId).writePorts(wPortId).wen := writePorts(wPortId).wen & regId.U === wRegSel(
      wPortId
    )
    V0Regs(regId).writePorts(wPortId).wIdx       := writePorts(wPortId).wIdx(wIdxBitWidth - 1, 0)
    V0Regs(regId).writePorts(wPortId).wData      := writePorts(wPortId).wData
    V0Regs(regId).writePorts(wPortId).wDataWidth := writePorts(wPortId).wDataWidth
  }

  for {
    regId   <- 0 until V0RegCount
    rPortId <- 0 until readProtCount
  } yield {
    V0Regs(regId).readPorts(rPortId).ren := readPorts(rPortId).ren & regId.U === rRegSel(
      rPortId
    )
    V0Regs(regId).readPorts(rPortId).rIdx       := readPorts(rPortId).rIdx(rIdxBitWidth - 1, 0)
    V0Regs(regId).readPorts(rPortId).rLineCount := readPorts(rPortId).rLineCount
    readRegData(regId)(rPortId)                 := V0Regs(regId).readPorts(rPortId).rData
  }

}

class V0Reg(readProtCount: Int, WriteProtCount: Int, VLEN: Int) extends Module {
  private val rowWidth           = VLEN / 8
  private val rows               = VLEN / rowWidth
  private val wDataWidthBitWidth = log2Up(log2Up(rowWidth) + 1)
  private val wIdxBitWidth       = log2Up(rows)
  private val rLineCountBitWidth = log2Up(log2Up(rows) + 1)
  private val rIdxBitWidth       = log2Up(rows)

  val readPorts = IO(Vec(
    readProtCount,
    new V0Reg.V0RegReadPort(rIdxBitWidth, rLineCountBitWidth, rowWidth)
  ))
  val writePorts = IO(Vec(
    WriteProtCount,
    new V0Reg.V0RegWritePort(wIdxBitWidth, wDataWidthBitWidth, rowWidth)
  ))

  val maskReg = RegInit(VecInit.fill(rows)(0.U(rowWidth.W)))
  // write
  val expandDatas     = Wire(Vec(WriteProtCount, UInt(rowWidth.W)))
  val rowWidthMaxLog2 = log2Up(rowWidth) + 1
  val widthWIdxMappings = (0 until rowWidthMaxLog2).collect {
    case i => i.U -> (1 << i)
  }.toSeq

  for (i <- 0 until WriteProtCount) {
    expandDatas(i) := LookupTree(
      writePorts(i).wDataWidth,
      widthWIdxMappings.map { case (wWidth, actualWidth) =>
        wWidth -> FillInterleaved(rowWidth / actualWidth, writePorts(i).wData(actualWidth - 1, 0))
      }
    )
  }
  for (i <- 0 until rows) {
    val wenOH = VecInit(writePorts.map(w => w.wen && w.wIdx === i.U))
    val wData = Mux1H(wenOH, expandDatas)
    when(wenOH.asUInt.orR) {
      maskReg(i) := wData
    }
  }

  for (i <- 0 until writePorts.size - 1) {
    val hasSameWrite = writePorts.drop(i + 1).map(w =>
      w.wen && w.wIdx === writePorts(i).wIdx && writePorts(i).wen
    ).reduce(_ || _)
    assert(!hasSameWrite, "V0Reg two or more writePorts write same addr")
  }

  // read
  val readDate = Wire(Vec(readProtCount, UInt(rowWidth.W)))
  def generateOutData(rIdx: Int, lineNum: Int): UInt = {
    val readyLineNum = 1 << lineNum
    val repeatedBits = Seq.tabulate(readyLineNum) { rrowi =>
      val bits = Seq.tabulate(rowWidth / readyLineNum)(i =>
        this.maskReg(rIdx.U(rIdxBitWidth.W) + rrowi.U(rIdxBitWidth.W))(i * readyLineNum)
      )
      Cat(bits.reverse)
    }
    Cat(repeatedBits.reverse)
  }

  val decoderInputs = for {
    rLineN <- 0 to rIdxBitWidth
    rId    <- 0 until rows by (1 << rLineN)
  } yield (rLineN, rId)

  val resultsOutData = decoderInputs.map { case (rLineN, rId) =>
    generateOutData(rId, rLineN)
  }

  val decoderTable = decoderInputs.zipWithIndex.map { case ((rLineN, rId), index) =>
    val lineBits  = V0Reg.binary(BigInt(rLineN), rLineCountBitWidth)
    val idxBits   = V0Reg.binary(BigInt(rId >> rLineN), rIdxBitWidth - rLineN) + "?" * rLineN
    val inputPat  = BitPat("b" + lineBits + idxBits)
    val outputPat = BitPat("b" + V0Reg.binary(BigInt(1) << index, 2 * rows - 1))
    (inputPat, outputPat)
  }

  val default = BitPat("b" + "?" * (2 * rows - 1))
  val truthTable = TruthTable(decoderTable, default)
  val readDecoders = VecInit(
    (0 until readProtCount).map { i =>
      decoder(
        QMCMinimizer,
        Cat(readPorts(i).rLineCount, readPorts(i).rIdx),
        truthTable
      )
    }
  )
  val outReadData = RegInit(VecInit(Seq.fill(readProtCount)(0.U(rowWidth.W))))
  for (i <- 0 until readProtCount) {
    readDate(i) := Mux1H(readDecoders(i), resultsOutData)
    when(readPorts(i).ren) {
      outReadData(i) := readDate(i)
    }
    readPorts(i).rData := outReadData(i)
  }

}

object V0Reg {
  /*
   rLineCount encoding rules:
    0 -> read row count = 1 (2^0 = 1 row)
    1 -> read row count = 2 (2^1 = 2 rows)
    2 -> read row count = 4 (2^2 = 4 rows)
    ...
    n -> read row count = 2^n rows
   */
  class V0RegReadPort(rIdxBitWidth: Int, rLineCountBitWidth: Int, rowWidth: Int) extends Bundle {
    val ren        = Input(Bool())
    val rIdx       = Input(UInt(rIdxBitWidth.W))
    val rLineCount = Input(UInt(rLineCountBitWidth.W))
    val rData      = Output(UInt(rowWidth.W))
  }

  /*
   wDataWidth encoding rules:
    0 -> actual width = 1 (2^0 = 1 bit)
    1 -> actual width = 2 (2^1 = 2 bits)
    2 -> actual width = 4 (2^2 = 4 bits)
    ...
    n -> actual width = 2^n bits
   */
  class V0RegWritePort(wIdxBitsWidth: Int, wDataWidthBitWidth: Int, rowWidth: Int) extends Bundle {
    val wen        = Input(Bool())
    val wIdx       = Input(UInt(wIdxBitsWidth.W))
    val wDataWidth = Input(UInt(wDataWidthBitWidth.W))
    val wData      = Input(UInt(rowWidth.W))
  }

  def binary(value: BigInt, width: Int): String =
    if (width == 0) "" else value.toString(2).reverse.padTo(width, '0').reverse

}
