import XCTest
@testable import OpenFlightCompanion

final class HTTPResponseParserTests: XCTestCase {
    private func bytes(_ text: String) -> Data { Data(text.utf8) }

    private func complete(_ text: String, atEOF: Bool = false, file: StaticString = #filePath, line: UInt = #line) -> HTTPResponse? {
        switch HTTPResponseParser.parse(bytes(text), atEOF: atEOF) {
        case .complete(let response): return response
        case let other:
            XCTFail("expected a complete response, got \(other)", file: file, line: line)
            return nil
        }
    }

    static let contentLength = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 13\r\n\r\n{\"ok\":\"true\"}"
    static let chunked = "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Type: application/json\r\n\r\n"
        + "5;name=value\r\n{\"a\":\r\n"
        + "A\r\n[1,2,3,4]}\r\n"
        + "0\r\nX-Trailer: done\r\nX-Other: 1\r\n\r\n"

    func testContentLength() throws {
        let response = try XCTUnwrap(complete(Self.contentLength))
        XCTAssertEqual(response.status, 200)
        XCTAssertEqual(response.reason, "OK")
        XCTAssertEqual(response.header("content-type"), "application/json")
        XCTAssertEqual(String(decoding: response.body, as: UTF8.self), "{\"ok\":\"true\"}")
    }

    func testBytesAfterContentLengthAreIgnored() throws {
        let response = try XCTUnwrap(complete(Self.contentLength + "garbage"))
        XCTAssertEqual(response.body.count, 13)
    }

    func testChunkedWithExtensionsAndTrailers() throws {
        let response = try XCTUnwrap(complete(Self.chunked))
        XCTAssertEqual(String(decoding: response.body, as: UTF8.self), "{\"a\":[1,2,3,4]}")
    }

    func testChunkedSizeIsHexAndCaseInsensitive() throws {
        let body = String(repeating: "x", count: 26)
        let text = "HTTP/1.1 200 OK\r\ntransfer-encoding: Chunked\r\n\r\n1a\r\n\(body)\r\n0\r\n\r\n"
        XCTAssertEqual(try XCTUnwrap(complete(text)).body.count, 26)
    }

    /// Every prefix is incomplete (and truncated at EOF); the full message parses.
    func testSplitAtEveryByteBoundary() {
        for message in [Self.contentLength, Self.chunked] {
            let data = bytes(message)
            for cut in 0..<data.count {
                let prefix = data.prefix(cut)
                XCTAssertEqual(HTTPResponseParser.parse(prefix, atEOF: false), .incomplete, "cut \(cut)")
                XCTAssertEqual(HTTPResponseParser.parse(prefix, atEOF: true), .failure(.truncated), "cut \(cut) at EOF")
            }
            guard case .complete = HTTPResponseParser.parse(data, atEOF: false) else {
                return XCTFail("full message incomplete")
            }
        }
    }

    func testBodyUntilEOFWithoutLength() throws {
        let text = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{\"x\":1}"
        XCTAssertEqual(HTTPResponseParser.parse(bytes(text), atEOF: false), .incomplete)
        XCTAssertEqual(String(decoding: try XCTUnwrap(complete(text, atEOF: true)).body, as: UTF8.self), "{\"x\":1}")
    }

    func testErrorStatusesParse() throws {
        let notFound = try XCTUnwrap(complete("HTTP/1.1 404 Not Found\r\nContent-Length: 2\r\n\r\n{}"))
        XCTAssertEqual(notFound.status, 404)
        XCTAssertEqual(notFound.reason, "Not Found")
        let serverError = try XCTUnwrap(complete("HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\n\r\n"))
        XCTAssertEqual(serverError.status, 500)
        XCTAssertEqual(serverError.reason, "Internal Server Error")
        XCTAssertTrue(serverError.body.isEmpty)
    }

    func testEmptyReasonPhrase() throws {
        XCTAssertEqual(try XCTUnwrap(complete("HTTP/1.1 200 \r\nContent-Length: 0\r\n\r\n")).reason, "")
        XCTAssertEqual(try XCTUnwrap(complete("HTTP/1.1 204\r\n\r\n")).status, 204)
    }

    func testInterimContinueIsSkipped() throws {
        let response = try XCTUnwrap(complete("HTTP/1.1 100 Continue\r\n\r\n" + Self.contentLength))
        XCTAssertEqual(response.status, 200)
        XCTAssertEqual(response.body.count, 13)
    }

    func testTruncatedBodies() {
        let shortLength = "HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{\"short\":1}"
        XCTAssertEqual(HTTPResponseParser.parse(bytes(shortLength), atEOF: true), .failure(.truncated))
        let unterminatedChunks = "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nabcde\r\n"
        XCTAssertEqual(HTTPResponseParser.parse(bytes(unterminatedChunks), atEOF: true), .failure(.truncated))
        XCTAssertEqual(HTTPResponseParser.parse(Data(), atEOF: true), .failure(.truncated))
    }

    func testOversizeBody() {
        let declared = "HTTP/1.1 200 OK\r\nContent-Length: \(HTTPResponseParser.maxMessageBytes + 1)\r\n\r\n"
        XCTAssertEqual(HTTPResponseParser.parse(bytes(declared), atEOF: false), .failure(.tooLarge))

        let hugeChunk = "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n\(String(HTTPResponseParser.maxMessageBytes + 1, radix: 16))\r\n"
        XCTAssertEqual(HTTPResponseParser.parse(bytes(hugeChunk), atEOF: false), .failure(.tooLarge))

        var buffer = bytes("HTTP/1.1 200 OK\r\n\r\n")
        buffer.append(Data(repeating: 0x41, count: HTTPResponseParser.maxMessageBytes))
        XCTAssertEqual(HTTPResponseParser.parse(buffer, atEOF: false), .failure(.tooLarge))
    }

    func testMalformedMessages() {
        let cases = [
            "HTTP/2 200 OK\r\n\r\n",
            "ICY 200 OK\r\n\r\n",
            "HTTP/1.1 2000 OK\r\n\r\n",
            "HTTP/1.1 abc OK\r\n\r\n",
            "HTTP/1.1 200 OK\r\nNoColonHere\r\n\r\n",
            "HTTP/1.1 200 OK\r\nBad Name: x\r\n\r\n",
            "HTTP/1.1 200 OK\r\nA: b\r\n folded\r\n\r\n",
            "HTTP/1.1 200 OK\r\nContent-Length: 5\r\nContent-Length: 6\r\n\r\nabcdef",
            "HTTP/1.1 200 OK\r\nContent-Length: -1\r\n\r\n",
            "HTTP/1.1 200 OK\r\nContent-Length: 1e3\r\n\r\n",
            "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\n",
            "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabcXX0\r\n\r\n",
        ]
        for text in cases {
            guard case .failure(.malformed) = HTTPResponseParser.parse(bytes(text), atEOF: true) else {
                return XCTFail("accepted \(text.debugDescription): \(HTTPResponseParser.parse(bytes(text), atEOF: true))")
            }
        }
    }

    func testUnsupportedEncodings() {
        let gzip = "HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: 0\r\n\r\n"
        XCTAssertEqual(HTTPResponseParser.parse(bytes(gzip), atEOF: true), .failure(.unsupported("Content-Encoding gzip")))
        let gzipChunked = "HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip, chunked\r\n\r\n"
        guard case .failure(.unsupported) = HTTPResponseParser.parse(bytes(gzipChunked), atEOF: true) else {
            return XCTFail("gzip transfer coding accepted")
        }
        let identical = "HTTP/1.1 200 OK\r\nContent-Length: 2, 2\r\n\r\nok"
        XCTAssertEqual(String(decoding: (try? XCTUnwrap(complete(identical)))?.body ?? Data(), as: UTF8.self), "ok")
    }
}
