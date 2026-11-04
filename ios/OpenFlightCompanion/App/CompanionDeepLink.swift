import Foundation

/// `openflight://open?url=<percent-encoded http(s) URL>`: the pairing page in Safari hands its
/// own URL (with the pairing code) over to the app, which then pairs in its web view the way
/// Safari would. A custom scheme, because universal links need https and Associated Domains
/// (design F16).
///
/// Only launch-monitor addresses are accepted: the configured host, or a host on the local
/// network (`*.local`, private and link-local IPv4). Any other link is ignored, so a web page
/// cannot point the app, and its bridge, at a foreign origin.
enum CompanionDeepLink {
    static let scheme = "openflight"
    static let openAction = "open"
    static let urlParameter = "url"

    /// The http(s) page an `openflight://open?url=…` link asks for, or nil when the link is
    /// malformed or the page is not on the launch monitor's host or the local network.
    static func target(from link: URL, configuredBase: URL) -> URL? {
        guard let components = URLComponents(url: link, resolvingAgainstBaseURL: false),
              components.scheme?.lowercased() == scheme,
              components.host?.lowercased() == openAction,
              components.path.isEmpty || components.path == "/",
              let raw = components.queryItems?.first(where: { $0.name == urlParameter })?.value,
              let target = URLComponents(string: raw),
              let targetScheme = target.scheme?.lowercased(), targetScheme == "http" || targetScheme == "https",
              let host = target.host?.lowercased(), !host.isEmpty,
              target.user == nil, target.password == nil,
              target.port.map({ (1...65_535).contains($0) }) ?? true,
              let url = target.url
        else { return nil }
        let configuredHost = configuredBase.host?.lowercased()
        guard host == configuredHost || isLocalHost(host) else { return nil }
        return url
    }

    /// `*.local` (mDNS) or a private (10/8, 172.16/12, 192.168/16) or link-local (169.254/16)
    /// IPv4 address; the Pi's hotspot (192.168.4.1, 10.42.0.1) falls in these ranges.
    static func isLocalHost(_ host: String) -> Bool {
        let host = host.lowercased()
        if host.hasSuffix(".local") {
            let name = host.dropLast(".local".count)
            return !name.isEmpty && !name.hasPrefix(".") && !name.hasSuffix(".")
        }
        guard let octets = ipv4Octets(host) else { return false }
        switch (octets[0], octets[1]) {
        case (10, _): return true
        case (172, 16...31): return true
        case (192, 168): return true
        case (169, 254): return true
        default: return false
        }
    }

    /// Strict dotted-quad decimal (no shorthand, octal or hex forms).
    private static func ipv4Octets(_ host: String) -> [Int]? {
        let parts = host.split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 4 else { return nil }
        var octets: [Int] = []
        for part in parts {
            guard (1...3).contains(part.count), part.allSatisfy(\.isASCII), part.allSatisfy(\.isNumber),
                  part.count == 1 || part.first != "0",
                  let value = Int(part), (0...255).contains(value)
            else { return nil }
            octets.append(value)
        }
        return octets
    }

    /// Handles an incoming link: switches the base URL when the page is on another launch-monitor
    /// address (the settings screen's path), then loads the page itself. False when ignored.
    @MainActor
    @discardableResult
    static func open(_ link: URL, settings: AppSettings, shell: WebShell) -> Bool {
        guard let target = target(from: link, configuredBase: settings.baseURL),
              let origin = AppSettings.normalizedBaseURL(target.absoluteString)
        else { return false }
        if origin != settings.baseURL {
            guard settings.setBaseURL(text: origin.absoluteString) else { return false }
        }
        shell.load(baseURL: settings.baseURL, page: target)
        return true
    }
}
