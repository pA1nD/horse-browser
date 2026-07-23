// focus-watch — logs, to the millisecond, every time the dedicated agent browser
// (Chrome for Testing) steals macOS keyboard/mouse focus by coming to the front.
//
// Event-driven (NSWorkspace activation notifications), so it never misses a brief steal
// the way a polling loop would. Prints each event and appends it to
// ~/.config/horse-browser/focus.log, interleaved with the launcher's own LAUNCH/RELAUNCH
// breadcrumbs so a steal can be attributed to what horse-browser was doing at that instant.
//
// Run:  swift tools/focus-watch.swift   (or: horse-browser focus-watch)
// Stop: Ctrl-C.
import Cocoa

let TARGET = "Google Chrome for Testing"
let logPath = (NSString(string: "~/.config/horse-browser/focus.log").expandingTildeInPath)

let df = DateFormatter()
df.dateFormat = "yyyy-MM-dd HH:mm:ss.SSS"

func emit(_ s: String) {
    let line = df.string(from: Date()) + "  " + s + "\n"
    FileHandle.standardError.write(line.data(using: .utf8)!)
    if !FileManager.default.fileExists(atPath: logPath) {
        FileManager.default.createFile(atPath: logPath, contents: nil)
    }
    if let fh = FileHandle(forWritingAtPath: logPath) {
        fh.seekToEndOfFile(); fh.write(line.data(using: .utf8)!); try? fh.close()
    }
}

var last = NSWorkspace.shared.frontmostApplication?.localizedName ?? "?"
var stoleAt: Date? = nil

let nc = NSWorkspace.shared.notificationCenter
nc.addObserver(forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: .main) { note in
    let app = (note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication)?.localizedName ?? "?"
    if app == TARGET {
        stoleAt = Date()
        emit("FOCUS-STOLEN  ← Chrome for Testing came to front (from: \(last))  *** your keystrokes now go here ***")
    } else if last == TARGET, let s = stoleAt {
        let held = Int(Date().timeIntervalSince(s) * 1000)
        emit("focus-returned → \(app)  (browser held focus \(held)ms)")
        stoleAt = nil
    }
    last = app
}

emit("focus-watch started  (frontmost: \(last))  — logging to \(logPath)")
RunLoop.main.run()
