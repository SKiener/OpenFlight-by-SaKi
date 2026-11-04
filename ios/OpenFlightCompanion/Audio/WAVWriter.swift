import Foundation

/// Mono IEEE-float32 WAV encoding (format tag 3) for the debug snippets (design §9.7). Pure.
enum WAVWriter {
    static let formatIEEEFloat: UInt16 = 3

    /// RIFF/WAVE with an 18-byte `fmt ` chunk (cbSize 0), a `fact` chunk and the `data` chunk,
    /// as the WAVE spec asks for non-PCM formats. Samples are written bit-exact, little-endian.
    static func encode(_ samples: [Float], sampleRate: Int) -> Data {
        let channels: UInt16 = 1
        let bitsPerSample: UInt16 = 32
        let blockAlign = channels * bitsPerSample / 8
        let dataBytes = UInt32(samples.count) * UInt32(blockAlign)
        let fmtBytes: UInt32 = 18
        let factBytes: UInt32 = 4
        let riffBytes = 4 + (8 + fmtBytes) + (8 + factBytes) + (8 + dataBytes)

        var data = Data()
        data.reserveCapacity(Int(riffBytes) + 8)
        data.append(ascii: "RIFF")
        data.append(le: riffBytes)
        data.append(ascii: "WAVE")

        data.append(ascii: "fmt ")
        data.append(le: fmtBytes)
        data.append(le: formatIEEEFloat)
        data.append(le: channels)
        data.append(le: UInt32(sampleRate))
        data.append(le: UInt32(sampleRate) * UInt32(blockAlign))
        data.append(le: blockAlign)
        data.append(le: bitsPerSample)
        data.append(le: UInt16(0))  // cbSize

        data.append(ascii: "fact")
        data.append(le: factBytes)
        data.append(le: UInt32(samples.count))

        data.append(ascii: "data")
        data.append(le: dataBytes)
        for sample in samples { data.append(le: sample.bitPattern) }
        return data
    }

    /// The parts a test (or an offline script) needs back out of `encode`'s output.
    struct Decoded: Equatable {
        let formatTag: UInt16
        let channels: UInt16
        let sampleRate: UInt32
        let bitsPerSample: UInt16
        let samples: [Float]
    }

    /// Reads a file `encode` wrote (any chunk order, unknown chunks skipped); nil when malformed.
    static func decode(_ data: Data) -> Decoded? {
        let bytes = [UInt8](data)
        func u32(_ at: Int) -> UInt32? {
            guard at + 4 <= bytes.count else { return nil }
            return UInt32(bytes[at]) | UInt32(bytes[at + 1]) << 8 | UInt32(bytes[at + 2]) << 16 | UInt32(bytes[at + 3]) << 24
        }
        func u16(_ at: Int) -> UInt16? {
            guard at + 2 <= bytes.count else { return nil }
            return UInt16(bytes[at]) | UInt16(bytes[at + 1]) << 8
        }
        func tag(_ at: Int) -> String? {
            guard at + 4 <= bytes.count else { return nil }
            return String(bytes: bytes[at..<(at + 4)], encoding: .ascii)
        }
        guard tag(0) == "RIFF", tag(8) == "WAVE", let riff = u32(4), Int(riff) + 8 == bytes.count else { return nil }
        var offset = 12
        var format: (tag: UInt16, channels: UInt16, rate: UInt32, bits: UInt16)?
        var samples: [Float]?
        while offset + 8 <= bytes.count, let id = tag(offset), let size = u32(offset + 4) {
            let body = offset + 8
            guard body + Int(size) <= bytes.count else { return nil }
            if id == "fmt ", size >= 16, let t = u16(body), let c = u16(body + 2), let r = u32(body + 4), let b = u16(body + 14) {
                format = (t, c, r, b)
            } else if id == "data" {
                guard size % 4 == 0 else { return nil }
                samples = stride(from: body, to: body + Int(size), by: 4).compactMap { u32($0).map(Float.init(bitPattern:)) }
            }
            offset = body + Int(size) + Int(size % 2)
        }
        guard let format, let samples else { return nil }
        return Decoded(formatTag: format.tag, channels: format.channels, sampleRate: format.rate, bitsPerSample: format.bits, samples: samples)
    }
}

private extension Data {
    mutating func append(ascii: String) { append(contentsOf: Array(ascii.utf8)) }

    mutating func append<T: FixedWidthInteger>(le value: T) {
        var little = value.littleEndian
        Swift.withUnsafeBytes(of: &little) { append(contentsOf: $0) }
    }
}
