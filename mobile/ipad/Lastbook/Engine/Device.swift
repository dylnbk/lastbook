import Foundation

/// What to call the thing Lastbook is running on, in the sentences a person reads: an iPhone or an iPad.
/// The app is one app for both. Every sentence that said "this iPad" asks here instead.
///
/// Read from the model's own name (hw.machine: "iPhone15,2", "iPad13,4"), which needs no UIKit and no main thread, so the
/// engine can ask from any queue and the Mac parity tool (tools/apidump) still compiles. There it answers "iPad": the
/// answers that tool compares were written for one.
enum Device {
    static let name: String = {
        var size = 0
        sysctlbyname("hw.machine", nil, &size, nil, 0)
        var buf = [CChar](repeating: 0, count: max(size, 1))
        sysctlbyname("hw.machine", &buf, &size, nil, 0)
        // In the simulator hw.machine is the Mac's; the simulated model is in the environment.
        let model = ProcessInfo.processInfo.environment["SIMULATOR_MODEL_IDENTIFIER"] ?? String(cString: buf)
        return model.hasPrefix("iPhone") || model.hasPrefix("iPod") ? "iPhone" : "iPad"
    }()
    static var isPhone: Bool { name == "iPhone" }
    /// The app's own folder as Files shows it: "On My iPhone" or "On My iPad".
    static var files: String { "On My \(name)" }
    /// The system, where a sentence names it.
    static var system: String { isPhone ? "iOS" : "iPadOS" }
}
