import Foundation

/// The last `capacity` samples of the capture, addressed by absolute sample index (design §9.2).
/// After a gap it restarts at the new index; reads that reach across a gap or past the retained
/// history return nil instead of stale samples.
struct SampleHistory {
    let capacity: Int
    private var buffer: [Float]
    /// One past the newest sample.
    private(set) var endIndex: Int64 = 0
    /// The oldest sample still held (and contiguous with `endIndex`).
    private(set) var startIndex: Int64 = 0

    init(capacity: Int) {
        self.capacity = max(1, capacity)
        buffer = [Float](repeating: 0, count: self.capacity)
    }

    mutating func reset(at index: Int64) {
        endIndex = index
        startIndex = index
    }

    @inline(__always)
    mutating func append(_ x: Float) {
        buffer[Int(endIndex % Int64(capacity))] = x
        endIndex += 1
        if endIndex - startIndex > Int64(capacity) { startIndex = endIndex - Int64(capacity) }
    }

    /// Samples `[from, to)`, or nil when any of them is not held.
    func copy(from: Int64, to: Int64) -> [Float]? {
        guard from >= startIndex, to <= endIndex, to >= from else { return nil }
        var out = [Float](repeating: 0, count: Int(to - from))
        let capacity = Int64(capacity)
        buffer.withUnsafeBufferPointer { source in
            out.withUnsafeMutableBufferPointer { target in
                var written = 0
                var position = from
                while position < to {
                    let offset = Int(position % capacity)
                    let run = min(Int(to - position), Int(capacity) - offset)
                    target.baseAddress!.advanced(by: written).update(from: source.baseAddress!.advanced(by: offset), count: run)
                    written += run
                    position += Int64(run)
                }
            }
        }
        return out
    }
}
