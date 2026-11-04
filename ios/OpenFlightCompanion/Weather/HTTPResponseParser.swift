import Foundation

/// A parsed HTTP/1.1 response.
struct HTTPResponse: Equatable {
    let status: Int
    let reason: String
    /// Header fields in order; names as received.
    let headers: [(name: String, value: String)]
    let body: Data

    func header(_ name: String) -> String? {
        headers.first { $0.name.caseInsensitiveCompare(name) == .orderedSame }?.value
    }

    static func == (lhs: HTTPResponse, rhs: HTTPResponse) -> Bool {
        lhs.status == rhs.status && lhs.reason == rhs.reason && lhs.body == rhs.body
            && lhs.headers.map(\.name) == rhs.headers.map(\.name)
            && lhs.headers.map(\.value) == rhs.headers.map(\.value)
    }
}

enum HTTPParseError: Error, Equatable {
    case malformed(String)
    /// The connection closed before the message was complete.
    case truncated
    case tooLarge
    case unsupported(String)
}

enum HTTPParseResult: Equatable {
    case incomplete
    case complete(HTTPResponse)
    case failure(HTTPParseError)
}

/// Pure HTTP/1.1 response parser for the hand-written cellular client (design F4, §8): status
/// line, headers, `Content-Length`, `Transfer-Encoding: chunked` (with extensions and trailers)
/// and read-to-EOF bodies. It is fed the whole buffer received so far.
enum HTTPResponseParser {
    /// Cap on headers plus body (design §8: 256 KiB).
    static let maxMessageBytes = 256 * 1024

    private static let crlf = Data([0x0D, 0x0A])
    private static let headerEnd = Data([0x0D, 0x0A, 0x0D, 0x0A])

    static func parse(_ buffer: Data, atEOF: Bool) -> HTTPParseResult {
        if buffer.count > maxMessageBytes { return .failure(.tooLarge) }
        var data = buffer[...]
        // Skip interim 1xx responses (e.g. 100 Continue); 101 would be an upgrade we never ask for.
        while true {
            guard let headEnd = data.range(of: headerEnd) else {
                return atEOF ? .failure(.truncated) : .incomplete
            }
            let head: Head
            switch parseHead(data[data.startIndex..<headEnd.lowerBound]) {
            case .failure(let error): return .failure(error)
            case .success(let parsed): head = parsed
            }
            let bodyStart = headEnd.upperBound
            if (100...199).contains(head.status) {
                if head.status == 101 { return .failure(.unsupported("101 Switching Protocols")) }
                data = data[bodyStart...]
                continue
            }
            return parseBody(head: head, rest: data[bodyStart...], atEOF: atEOF)
        }
    }

    private struct Head {
        let status: Int
        let reason: String
        let headers: [(name: String, value: String)]
    }

    private static func parseHead(_ bytes: Data.SubSequence) -> Result<Head, HTTPParseError> {
        guard let text = String(data: Data(bytes), encoding: .isoLatin1) else {
            return .failure(.malformed("header encoding"))
        }
        let lines = text.components(separatedBy: "\r\n")
        guard let statusLine = lines.first else { return .failure(.malformed("empty head")) }
        // "HTTP/1.1 200 OK" — the reason phrase may be empty or contain spaces.
        let parts = statusLine.split(separator: " ", maxSplits: 2, omittingEmptySubsequences: false)
        guard parts.count >= 2, parts[0].hasPrefix("HTTP/1."),
              parts[1].count == 3, let status = Int(parts[1]), (100...599).contains(status)
        else { return .failure(.malformed("status line \(statusLine)")) }
        let reason = parts.count == 3 ? String(parts[2]) : ""

        var headers: [(name: String, value: String)] = []
        for line in lines.dropFirst() {
            if line.first == " " || line.first == "\t" {
                return .failure(.malformed("obsolete header folding"))
            }
            guard let colon = line.firstIndex(of: ":") else {
                return .failure(.malformed("header line \(line)"))
            }
            let name = String(line[..<colon])
            guard !name.isEmpty, !name.contains(" "), !name.contains("\t") else {
                return .failure(.malformed("header name \(name)"))
            }
            let value = line[line.index(after: colon)...].trimmingCharacters(in: CharacterSet(charactersIn: " \t"))
            headers.append((name, value))
        }
        return .success(Head(status: status, reason: reason, headers: headers))
    }

    private static func values(_ head: Head, _ name: String) -> [String] {
        head.headers.filter { $0.name.caseInsensitiveCompare(name) == .orderedSame }.map(\.value)
    }

    private static func parseBody(head: Head, rest: Data.SubSequence, atEOF: Bool) -> HTTPParseResult {
        let encodings = values(head, "Content-Encoding").map { $0.lowercased() }
        if let encoding = encodings.first(where: { $0 != "identity" && !$0.isEmpty }) {
            return .failure(.unsupported("Content-Encoding \(encoding)"))
        }
        func done(_ body: Data) -> HTTPParseResult {
            .complete(HTTPResponse(status: head.status, reason: head.reason, headers: head.headers, body: body))
        }
        // No body for 204 and 304 (RFC 9112 §6.3).
        if head.status == 204 || head.status == 304 { return done(Data()) }

        let transferCodings = values(head, "Transfer-Encoding")
            .flatMap { $0.split(separator: ",") }
            .map { $0.trimmingCharacters(in: .whitespaces).lowercased() }
            .filter { !$0.isEmpty }
        if !transferCodings.isEmpty {
            guard transferCodings == ["chunked"] else {
                return .failure(.unsupported("Transfer-Encoding \(transferCodings.joined(separator: ","))"))
            }
            return parseChunked(rest, atEOF: atEOF).map(done)
        }

        let lengths = Set(values(head, "Content-Length").flatMap { $0.split(separator: ",") }
            .map { $0.trimmingCharacters(in: .whitespaces) })
        if !lengths.isEmpty {
            guard lengths.count == 1, let text = lengths.first, !text.isEmpty,
                  text.allSatisfy(\.isASCIIDigit), let length = Int(text)
            else { return .failure(.malformed("Content-Length")) }
            if length > maxMessageBytes { return .failure(.tooLarge) }
            if rest.count >= length { return done(Data(rest.prefix(length))) }
            return atEOF ? .failure(.truncated) : .incomplete
        }

        // Neither: the body runs to the end of the connection (we always send Connection: close).
        return atEOF ? done(Data(rest)) : .incomplete
    }

    private static func parseChunked(_ input: Data.SubSequence, atEOF: Bool) -> ChunkedResult {
        var cursor = input.startIndex
        var body = Data()
        func needMore() -> ChunkedResult { atEOF ? .failure(.truncated) : .incomplete }

        while true {
            guard let lineEnd = input[cursor...].range(of: crlf) else { return needMore() }
            let sizeLine = String(decoding: input[cursor..<lineEnd.lowerBound], as: UTF8.self)
            // Chunk extensions follow a ';' and are ignored.
            let sizeText = sizeLine.split(separator: ";", maxSplits: 1, omittingEmptySubsequences: false)
                .first.map { $0.trimmingCharacters(in: CharacterSet(charactersIn: " \t")) } ?? ""
            guard !sizeText.isEmpty, sizeText.count <= 8, sizeText.allSatisfy(\.isHexDigit),
                  let size = Int(sizeText, radix: 16)
            else { return .failure(.malformed("chunk size \(sizeLine)")) }
            cursor = lineEnd.upperBound

            if size == 0 {
                // Trailer fields, each ending in CRLF, then an empty line.
                while true {
                    guard let trailerEnd = input[cursor...].range(of: crlf) else { return needMore() }
                    let isEmptyLine = trailerEnd.lowerBound == cursor
                    cursor = trailerEnd.upperBound
                    if isEmptyLine { return .complete(body) }
                }
            }
            if body.count + size > maxMessageBytes { return .failure(.tooLarge) }
            let available = input.distance(from: cursor, to: input.endIndex)
            guard available >= size + 2 else { return needMore() }
            let chunkEnd = input.index(cursor, offsetBy: size)
            body.append(contentsOf: input[cursor..<chunkEnd])
            guard input[chunkEnd...].starts(with: crlf) else {
                return .failure(.malformed("chunk not terminated by CRLF"))
            }
            cursor = input.index(chunkEnd, offsetBy: 2)
        }
    }

    private enum ChunkedResult {
        case incomplete
        case complete(Data)
        case failure(HTTPParseError)

        func map(_ transform: (Data) -> HTTPParseResult) -> HTTPParseResult {
            switch self {
            case .incomplete: return .incomplete
            case .failure(let error): return .failure(error)
            case .complete(let body): return transform(body)
            }
        }
    }
}

private extension Character {
    var isASCIIDigit: Bool { ("0"..."9").contains(self) }
}
