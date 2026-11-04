import CoreMotion
import Foundation

/// Accelerometer samples for the page's TI tilt calibration, in the W3C DeviceMotion frame.
///
/// The page cannot use Device Motion itself: it is served over plain http, which is not a
/// secure context (design F9), so WebKit never delivers `devicemotion` to it. Pure, so the
/// conversion and the sequence bookkeeping are testable without CoreMotion.
struct MotionSampleBuffer {
    static let standardGravity = 9.80665

    struct Sample: Equatable {
        let seq: Int64
        /// `CMLogItem.timestamp` (seconds since boot) in ms.
        let tMs: Double
        /// `accelerationIncludingGravity` in m/s²: x right, y toward the top, z out of the screen.
        let gravity: (x: Double, y: Double, z: Double)

        static func == (lhs: Sample, rhs: Sample) -> Bool {
            lhs.seq == rhs.seq && lhs.tMs == rhs.tMs && lhs.gravity == rhs.gravity
        }
    }

    let capacity: Int
    private(set) var samples: [Sample] = []
    private var lastSeq: Int64 = 0

    init(capacity: Int) {
        self.capacity = capacity
    }

    /// CoreMotion reports g with the opposite sign of W3C (face up reads z = −1 g, W3C +9.81 m/s²).
    static func w3cGravity(x: Double, y: Double, z: Double) -> (x: Double, y: Double, z: Double) {
        (-x * standardGravity, -y * standardGravity, -z * standardGravity)
    }

    mutating func append(timestamp: TimeInterval, x: Double, y: Double, z: Double) {
        guard timestamp.isFinite, x.isFinite, y.isFinite, z.isFinite else { return }
        lastSeq += 1
        samples.append(Sample(seq: lastSeq, tMs: timestamp * 1000, gravity: Self.w3cGravity(x: x, y: y, z: z)))
        if samples.count > capacity { samples.removeFirst(samples.count - capacity) }
    }

    /// Drops the samples; `seq` keeps counting so a page polling with an old `after_seq` never
    /// mistakes new samples for ones it already has.
    mutating func clear() {
        samples.removeAll()
    }

    func json(after seq: Int, running: Bool) -> JSONValue {
        let newer = samples.filter { $0.seq > Int64(seq) }.map { sample -> JSONValue in
            .object([
                "seq": .int(sample.seq),
                "t_ms": .number(sample.tMs),
                "gravity_mps2": .array([.number(sample.gravity.x), .number(sample.gravity.y), .number(sample.gravity.z)]),
            ])
        }
        return .object(["running": .bool(running), "samples": .array(newer)])
    }
}

/// `motion.start` / `motion.samples` / `motion.stop`: CMMotionManager accelerometer at 50 Hz,
/// only while the page asks for it (stopped on a new page and in the background).
@MainActor
final class MotionProvider {
    static let intervalS = 0.02
    /// About 10 s at 50 Hz: the page polls every 100 ms.
    static let capacity = 512

    private let manager = CMMotionManager()
    private var buffer = MotionSampleBuffer(capacity: MotionProvider.capacity)

    var isAvailable: Bool { manager.isAccelerometerAvailable }
    var isRunning: Bool { manager.isAccelerometerActive }

    func start() -> Result<JSONValue, BridgeError> {
        guard manager.isAccelerometerAvailable else {
            return .failure(BridgeError(.unavailable, "no accelerometer"))
        }
        if !manager.isAccelerometerActive {
            buffer.clear()
            manager.accelerometerUpdateInterval = Self.intervalS
            manager.startAccelerometerUpdates(to: .main) { [weak self] data, _ in
                guard let data else { return }
                MainActor.assumeIsolated {
                    self?.buffer.append(
                        timestamp: data.timestamp,
                        x: data.acceleration.x, y: data.acceleration.y, z: data.acceleration.z
                    )
                }
            }
        }
        return .success(.object([
            "running": .bool(true),
            "interval_ms": .number(Self.intervalS * 1000),
        ]))
    }

    func samples(after seq: Int) -> JSONValue {
        buffer.json(after: seq, running: isRunning)
    }

    @discardableResult
    func stop() -> JSONValue {
        if manager.isAccelerometerActive { manager.stopAccelerometerUpdates() }
        buffer.clear()
        return .object(["running": .bool(false)])
    }
}
