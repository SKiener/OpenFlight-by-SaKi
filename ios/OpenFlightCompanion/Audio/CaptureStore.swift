import Foundation

/// Debug WAV snippets on disk (design §9.7): `Documents/AudioCaptures/<capture_id>/` holds
/// `capture.json` (session config and thresholds) and `onset-<seq:04>.wav|.json` per onset.
///
/// Ring-buffered: at most `maxFiles` snippets (and `maxBytes`) are kept; the oldest go first and a
/// capture folder without snippets left is removed, unless it is the capture being written. Not
/// thread-safe: the owner calls it from one serial queue, never from the detector's queue.
final class CaptureStore: @unchecked Sendable {
    static let folderName = "AudioCaptures"
    /// ±200 ms at 48 kHz is 76.8 KB per snippet, so 500 files are about 38 MB.
    static let defaultMaxFiles = 500
    static let defaultMaxBytes: Int64 = 300 * 1_024 * 1_024

    struct Summary: Equatable {
        var files: Int
        var bytes: Int64
    }

    /// `Documents/AudioCaptures`.
    let root: URL
    let maxFiles: Int
    let maxBytes: Int64
    private let fileManager = FileManager.default
    /// Never deleted by retention while snippets are written into it.
    var activeCaptureId: String?

    init(root: URL, maxFiles: Int = defaultMaxFiles, maxBytes: Int64 = defaultMaxBytes) {
        self.root = root
        self.maxFiles = max(1, maxFiles)
        self.maxBytes = max(1, maxBytes)
    }

    static var defaultRoot: URL {
        let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first
            ?? FileManager.default.temporaryDirectory
        return documents.appendingPathComponent(folderName, isDirectory: true)
    }

    /// `snippet_file` of the payload: relative to the app's Documents folder.
    static func snippetPath(captureId: String, seq: Int) -> String {
        "\(folderName)/\(captureId)/\(snippetName(seq: seq)).wav"
    }

    static func snippetName(seq: Int) -> String { String(format: "onset-%04d", seq) }

    private func folder(_ captureId: String) -> URL {
        root.appendingPathComponent(captureId, isDirectory: true)
    }

    func writeCaptureInfo(captureId: String, info: JSONValue) throws {
        try fileManager.createDirectory(at: folder(captureId), withIntermediateDirectories: true)
        try Data(info.jsonString().utf8).write(to: folder(captureId).appendingPathComponent("capture.json"), options: .atomic)
    }

    /// Writes one snippet and its payload sidecar, then enforces retention.
    func writeSnippet(captureId: String, seq: Int, samples: [Float], sampleRate: Double, payload: JSONValue) throws {
        let directory = folder(captureId)
        try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        let base = directory.appendingPathComponent(Self.snippetName(seq: seq))
        try WAVWriter.encode(samples, sampleRate: Int(sampleRate.rounded())).write(to: base.appendingPathExtension("wav"), options: .atomic)
        try Data(payload.jsonString().utf8).write(to: base.appendingPathExtension("json"), options: .atomic)
        enforceRetention()
    }

    private struct Snippet {
        let wav: URL
        let date: Date
        let bytes: Int64
    }

    private func snippets() -> [Snippet] {
        let keys: [URLResourceKey] = [.contentModificationDateKey, .fileSizeKey]
        guard let folders = try? fileManager.contentsOfDirectory(at: root, includingPropertiesForKeys: nil) else { return [] }
        var result: [Snippet] = []
        for folder in folders {
            guard let files = try? fileManager.contentsOfDirectory(at: folder, includingPropertiesForKeys: keys) else { continue }
            for file in files where file.pathExtension == "wav" {
                let values = try? file.resourceValues(forKeys: Set(keys))
                let sidecar = file.deletingPathExtension().appendingPathExtension("json")
                let sidecarBytes = (try? sidecar.resourceValues(forKeys: [.fileSizeKey]))?.fileSize ?? 0
                result.append(Snippet(
                    wav: file,
                    date: values?.contentModificationDate ?? .distantPast,
                    bytes: Int64((values?.fileSize ?? 0) + sidecarBytes)
                ))
            }
        }
        // Oldest first; the path breaks ties (seq order inside a capture).
        return result.sorted { $0.date == $1.date ? $0.wav.path < $1.wav.path : $0.date < $1.date }
    }

    func summary() -> Summary {
        let all = snippets()
        return Summary(files: all.count, bytes: all.reduce(0) { $0 + $1.bytes })
    }

    /// Deletes the oldest snippets until at most `maxFiles` and `maxBytes` remain, then removes
    /// capture folders left without snippets (except the active one).
    func enforceRetention() {
        var all = snippets()
        var bytes = all.reduce(0) { $0 + $1.bytes }
        while !all.isEmpty, all.count > maxFiles || bytes > maxBytes {
            let oldest = all.removeFirst()
            try? fileManager.removeItem(at: oldest.wav)
            try? fileManager.removeItem(at: oldest.wav.deletingPathExtension().appendingPathExtension("json"))
            bytes -= oldest.bytes
        }
        guard let folders = try? fileManager.contentsOfDirectory(at: root, includingPropertiesForKeys: nil) else { return }
        for folder in folders where folder.lastPathComponent != activeCaptureId {
            let files = (try? fileManager.contentsOfDirectory(atPath: folder.path)) ?? []
            if !files.contains(where: { $0.hasSuffix(".wav") }) { try? fileManager.removeItem(at: folder) }
        }
    }

    /// Zips `AudioCaptures` through `NSFileCoordinator.forUploading` (design F17) into a temporary
    /// file the share sheet can hand out. nil when there is nothing to export.
    func exportZip(to directory: URL = FileManager.default.temporaryDirectory) throws -> URL? {
        guard summary().files > 0 else { return nil }
        let stamp = ISO8601DateFormatter().string(from: Date()).replacingOccurrences(of: ":", with: "-")
        let target = directory.appendingPathComponent("OpenFlight-AudioCaptures-\(stamp).zip")
        var coordinatorError: NSError?
        var copyError: Error?
        NSFileCoordinator().coordinate(readingItemAt: root, options: .forUploading, error: &coordinatorError) { zip in
            do {
                try? fileManager.removeItem(at: target)
                try fileManager.copyItem(at: zip, to: target)
            } catch {
                copyError = error
            }
        }
        if let error = coordinatorError ?? copyError { throw error }
        return target
    }
}
