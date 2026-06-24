package xiangshan.backend.vector.vagq

import chisel3._
import chisel3.util._


trait HasVAGQHelper { this: HasVAGQParameters =>
  protected def entryAt(entries: Vec[VAGQEntry], idx: UInt): VAGQEntry = {
    Mux1H((0 until vagqSize).map(i => (idx === i.U(vagqEntryIdxWidth.W)) -> entries(i)))
  }

  protected def idxValid(idx: UInt): Bool = idx < vagqSize.U
}