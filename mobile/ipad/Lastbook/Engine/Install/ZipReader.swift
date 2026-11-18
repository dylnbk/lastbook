import Foundation
import Compression

/// A streaming reader for (possibly very large) zip files: zip64, multi-GB members, stored or deflate.
/// It reads the central directory once, then copies one member at a time to a file in small pieces, checking its
/// CRC-32 and size as it writes. A whole member is never held in memory. Foundation and Compression only.
struct ZipEntry {
    let name: String            // as Python's zipfile decodes it: UTF-8 when flagged, else code page 437
    let rawName: Data
    let flags: UInt16
    let method: UInt16          // 0 stored, 8 deflate
    let dosTime: UInt16
    let dosDate: UInt16
    let crc32: UInt32
    let compressedSize: UInt64
    let size: UInt64
    let headerOffset: UInt64
    let externalAttr: UInt32
    let createSystem: UInt8

    var isDirectory: Bool { name.hasSuffix("/") }
    var isEncrypted: Bool { flags & 0x1 != 0 }
    /// The Unix file type bits (S_IFMT) from the external attributes, 0 when none were recorded.
    var unixType: UInt32 { (externalAttr >> 16) & 0o170000 }

    /// The member's DOS date and time, read as local time (as Python's time.mktime(info.date_time)).
    var date: Date? {
        var c = DateComponents()
        c.year = Int(dosDate >> 9) + 1980; c.month = Int((dosDate >> 5) & 0xF); c.day = Int(dosDate & 0x1F)
        c.hour = Int(dosTime >> 11); c.minute = Int((dosTime >> 5) & 0x3F); c.second = Int(dosTime & 0x1F) * 2
        return Calendar.current.date(from: c)
    }
}

enum ZipError: Error, CustomStringConvertible {
    case notZip(String)         // not a zip file at all, or its directory cannot be read
    case damaged(String)        // a member's bytes do not check out (CRC, size, truncated, bad deflate data)
    case unsupported(String)    // a compression method other than stored or deflate
    case writeFailed(String)    // the copy could not be written (for example the disk is full)
    case cancelled
    var description: String {
        switch self {
        case .notZip(let m), .damaged(let m), .unsupported(let m), .writeFailed(let m): return m
        case .cancelled: return "cancelled"
        }
    }
}

final class ZipReader {
    let url: URL
    let fileSize: UInt64
    private(set) var entries: [ZipEntry] = []
    /// Where the central directory starts: no member's data may run past it.
    private(set) var centralDirectoryOffset: UInt64 = 0
    private let fh: FileHandle

    static let chunk = 4 * 1024 * 1024

    init(url: URL) throws {
        self.url = url
        guard let h = try? FileHandle(forReadingFrom: url) else { throw ZipError.notZip("The file could not be opened.") }
        fh = h
        let end = (try? h.seekToEnd()) ?? 0
        fileSize = end
        try readDirectory()
    }

    deinit { try? fh.close() }

    // ------------------------------------------------------------------ reading bytes ----
    private func read(_ offset: UInt64, _ count: Int) throws -> Data {
        guard count >= 0, offset <= fileSize, UInt64(count) <= fileSize - offset else { throw ZipError.notZip("The zip file is shorter than its directory says.") }
        do {
            try fh.seek(toOffset: offset)
            let d = try fh.read(upToCount: count) ?? Data()
            guard d.count == count else { throw ZipError.notZip("The zip file is shorter than its directory says.") }
            return d
        } catch let e as ZipError { throw e } catch { throw ZipError.notZip("The zip file could not be read (\(error.localizedDescription)).") }
    }

    private static func u16(_ d: Data, _ i: Int) -> UInt16 { UInt16(d[d.startIndex + i]) | UInt16(d[d.startIndex + i + 1]) << 8 }
    private static func u32(_ d: Data, _ i: Int) -> UInt32 { UInt32(u16(d, i)) | UInt32(u16(d, i + 2)) << 16 }
    private static func u64(_ d: Data, _ i: Int) -> UInt64 { UInt64(u32(d, i)) | UInt64(u32(d, i + 4)) << 32 }

    // ------------------------------------------------------------------ the directory ----
    private func readDirectory() throws {
        guard fileSize >= 22 else { throw ZipError.notZip("The file is not a zip file.") }
        let tailLen = Int(min(fileSize, 22 + 65535 + 20))
        let tailStart = fileSize - UInt64(tailLen)
        let tail = try read(tailStart, tailLen)
        var eocd = -1
        var i = tailLen - 22
        while i >= 0 {
            if Self.u32(tail, i) == 0x06054b50, i + 22 + Int(Self.u16(tail, i + 20)) <= tailLen { eocd = i; break }
            i -= 1
        }
        guard eocd >= 0 else { throw ZipError.notZip("The file is not a zip file.") }
        var count = UInt64(Self.u16(tail, eocd + 10))
        var cdSize = UInt64(Self.u32(tail, eocd + 12))
        var cdOffset = UInt64(Self.u32(tail, eocd + 16))
        // zip64: a locator just before the end record points at the zip64 end record
        if eocd >= 20, Self.u32(tail, eocd - 20) == 0x07064b50 {
            let recOffset = Self.u64(tail, eocd - 20 + 8)
            let rec = try read(recOffset, 56)
            guard Self.u32(rec, 0) == 0x06064b50 else { throw ZipError.notZip("The zip64 directory record is damaged.") }
            count = Self.u64(rec, 32); cdSize = Self.u64(rec, 40); cdOffset = Self.u64(rec, 48)
        }
        guard cdOffset <= fileSize, cdSize <= fileSize - cdOffset, cdSize < 1 << 31 else { throw ZipError.notZip("The zip directory is damaged.") }
        centralDirectoryOffset = cdOffset
        let cd = try read(cdOffset, Int(cdSize))
        var p = 0
        var out: [ZipEntry] = []
        out.reserveCapacity(Int(min(count, 1_000_000)))
        let cp437 = String.Encoding(rawValue: CFStringConvertEncodingToNSStringEncoding(CFStringEncoding(CFStringEncodings.dosLatinUS.rawValue)))
        while out.count < count {
            guard p + 46 <= cd.count, Self.u32(cd, p) == 0x02014b50 else { throw ZipError.notZip("The zip directory is damaged.") }
            let madeBy = Self.u16(cd, p + 4)
            let flags = Self.u16(cd, p + 8), method = Self.u16(cd, p + 10)
            let time = Self.u16(cd, p + 12), date = Self.u16(cd, p + 14)
            let crc = Self.u32(cd, p + 16)
            var csize = UInt64(Self.u32(cd, p + 20)), usize = UInt64(Self.u32(cd, p + 24))
            let nlen = Int(Self.u16(cd, p + 28)), elen = Int(Self.u16(cd, p + 30)), clen = Int(Self.u16(cd, p + 32))
            let ext = Self.u32(cd, p + 38)
            var offset = UInt64(Self.u32(cd, p + 42))
            guard p + 46 + nlen + elen + clen <= cd.count else { throw ZipError.notZip("The zip directory is damaged.") }
            let raw = cd.subdata(in: (cd.startIndex + p + 46)..<(cd.startIndex + p + 46 + nlen))
            // zip64 extra field: the sizes and offset that did not fit, in this order
            var e = p + 46 + nlen
            let eEnd = e + elen
            while e + 4 <= eEnd {
                let tag = Self.u16(cd, e), len = Int(Self.u16(cd, e + 2))
                if tag == 0x0001 {
                    var q = e + 4
                    let qEnd = min(e + 4 + len, eEnd)
                    if usize == 0xFFFF_FFFF { guard q + 8 <= qEnd else { throw ZipError.notZip("The zip64 sizes are damaged.") }; usize = Self.u64(cd, q); q += 8 }
                    if csize == 0xFFFF_FFFF { guard q + 8 <= qEnd else { throw ZipError.notZip("The zip64 sizes are damaged.") }; csize = Self.u64(cd, q); q += 8 }
                    if offset == 0xFFFF_FFFF { guard q + 8 <= qEnd else { throw ZipError.notZip("The zip64 offset is damaged.") }; offset = Self.u64(cd, q); q += 8 }
                }
                e += 4 + len
            }
            let name = flags & 0x800 != 0 ? String(decoding: raw, as: UTF8.self) : (String(data: raw, encoding: cp437) ?? String(decoding: raw, as: UTF8.self))
            out.append(ZipEntry(name: name, rawName: raw, flags: flags, method: method, dosTime: time, dosDate: date, crc32: crc,
                                compressedSize: csize, size: usize, headerOffset: offset, externalAttr: ext, createSystem: UInt8(madeBy >> 8)))
            p += 46 + nlen + elen + clen
        }
        entries = out
    }

    /// Where a member's bytes begin, from its local header (whose name must match the directory's, as zipfile checks).
    func dataOffset(_ e: ZipEntry) throws -> UInt64 {
        let h = try read(e.headerOffset, 30)
        guard Self.u32(h, 0) == 0x04034b50 else { throw ZipError.damaged("A file's header in the zip is damaged (\(e.name.prefix(80))).") }
        let nlen = Int(Self.u16(h, 26)), elen = Int(Self.u16(h, 28))
        let name = try read(e.headerOffset + 30, nlen)
        guard name == e.rawName else { throw ZipError.damaged("A file's name in the zip differs from its directory entry (\(e.name.prefix(80))).") }
        return e.headerOffset + 30 + UInt64(nlen) + UInt64(elen)
    }

    // ------------------------------------------------------------------ extracting ----
    /// Writes one member to `out` (an open file), checking that it never grows past its declared size and that the
    /// CRC-32 matches. `progress(bytes)` is called with each piece written. `shouldContinue` returning false cancels.
    /// Returns the number of bytes written.
    @discardableResult
    func extract(_ e: ZipEntry, to out: FileHandle, progress: (Int) -> Void = { _ in }, shouldContinue: () -> Bool = { true }) throws -> UInt64 {
        guard e.method == 0 || e.method == 8 else { throw ZipError.unsupported("A file in the zip uses a kind of compression Lastbook does not accept (\(e.name.prefix(80))).") }
        let start = try dataOffset(e)
        guard start <= fileSize, e.compressedSize <= fileSize - start else { throw ZipError.damaged("The zip file is shorter than it says (\(e.name.prefix(80))).") }
        var crc = CRC32()
        var written: UInt64 = 0
        func emit(_ p: UnsafeRawBufferPointer) throws {
            guard p.count > 0 else { return }
            written += UInt64(p.count)
            if written > e.size { throw ZipError.damaged("A file in the zip is larger than it says (\(e.name.prefix(80))).") }
            crc.update(p)
            do { try out.write(contentsOf: Data(bytesNoCopy: UnsafeMutableRawPointer(mutating: p.baseAddress!), count: p.count, deallocator: .none)) }
            catch { throw ZipError.writeFailed("A file could not be written (\(error.localizedDescription)).") }
            progress(p.count)
        }
        do { try fh.seek(toOffset: start) } catch { throw ZipError.damaged("The zip file could not be read.") }
        var remaining = e.compressedSize
        func nextChunk() throws -> Data {
            let n = Int(min(UInt64(Self.chunk), remaining))
            guard let d = try? fh.read(upToCount: n), d.count == n else { throw ZipError.damaged("The zip file is shorter than it says (\(e.name.prefix(80))).") }
            remaining -= UInt64(n)
            return d
        }
        if e.method == 0 {
            while remaining > 0 {
                guard shouldContinue() else { throw ZipError.cancelled }
                let d = try nextChunk()
                try d.withUnsafeBytes { try emit($0) }
            }
        } else {
            try inflate(nextChunk: nextChunk, hasMore: { remaining > 0 }, emit: emit, shouldContinue: shouldContinue, name: e.name)
        }
        guard written == e.size else { throw ZipError.damaged("A file in the zip came out the wrong size (\(e.name.prefix(80))).") }
        guard crc.value == e.crc32 else { throw ZipError.damaged("A file in the zip is damaged: its CRC-32 does not match (\(e.name.prefix(80))).") }
        return written
    }

    /// Raw deflate (Compression's COMPRESSION_ZLIB), streamed.
    private func inflate(nextChunk: () throws -> Data, hasMore: () -> Bool, emit: (UnsafeRawBufferPointer) throws -> Void,
                         shouldContinue: () -> Bool, name: String) throws {
        let outCap = 1 << 20
        let dst = UnsafeMutablePointer<UInt8>.allocate(capacity: outCap)
        defer { dst.deallocate() }
        let srcCap = Self.chunk
        let src = UnsafeMutablePointer<UInt8>.allocate(capacity: srcCap)
        defer { src.deallocate() }
        let s = UnsafeMutablePointer<compression_stream>.allocate(capacity: 1)
        defer { s.deallocate() }
        guard compression_stream_init(s, COMPRESSION_STREAM_DECODE, COMPRESSION_ZLIB) == COMPRESSION_STATUS_OK else {
            throw ZipError.damaged("The deflate decoder could not start.")
        }
        defer { compression_stream_destroy(s) }
        s.pointee.src_ptr = UnsafePointer(src)
        s.pointee.src_size = 0
        while true {
            guard shouldContinue() else { throw ZipError.cancelled }
            if s.pointee.src_size == 0 && hasMore() {
                let d = try nextChunk()
                d.copyBytes(to: src, count: d.count)
                s.pointee.src_ptr = UnsafePointer(src)
                s.pointee.src_size = d.count
            }
            let finalize = !hasMore()
            let before = s.pointee.src_size
            s.pointee.dst_ptr = dst
            s.pointee.dst_size = outCap
            let st = compression_stream_process(s, finalize ? Int32(COMPRESSION_STREAM_FINALIZE.rawValue) : 0)
            let produced = outCap - s.pointee.dst_size
            try emit(UnsafeRawBufferPointer(start: dst, count: produced))
            if st == COMPRESSION_STATUS_END { return }
            if st == COMPRESSION_STATUS_ERROR { throw ZipError.damaged("A file in the zip is damaged: its compressed data is not valid (\(name.prefix(80))).") }
            if finalize && produced == 0 && s.pointee.src_size == before {
                throw ZipError.damaged("A file in the zip is damaged: its compressed data ends too soon (\(name.prefix(80))).")
            }
        }
    }
}

/// CRC-32 (IEEE, as zip uses), slicing by 8.
struct CRC32 {
    private(set) var value: UInt32 = 0
    private static let tables: [[UInt32]] = {
        var t = [[UInt32]](repeating: [UInt32](repeating: 0, count: 256), count: 8)
        for i in 0..<256 {
            var c = UInt32(i)
            for _ in 0..<8 { c = c & 1 != 0 ? 0xEDB88320 ^ (c >> 1) : c >> 1 }
            t[0][i] = c
        }
        for i in 0..<256 { for k in 1..<8 { t[k][i] = (t[k - 1][i] >> 8) ^ t[0][Int(t[k - 1][i] & 0xFF)] } }
        return t
    }()
    private static let flat: [UInt32] = tables.flatMap { $0 }

    mutating func update(_ buf: UnsafeRawBufferPointer) {
        guard let base = buf.baseAddress?.assumingMemoryBound(to: UInt8.self) else { return }
        var c = ~value
        var i = 0
        let n = buf.count
        Self.flat.withUnsafeBufferPointer { (t: UnsafeBufferPointer<UInt32>) -> Void in
            while i + 8 <= n {
                // A step at a time, each with its type said. Written as three long expressions, the compiler in Xcode 16.4
                // (Swift 6.1) gave up on this closure: "unable to type-check this expression in reasonable time".
                let b0 = UInt32(base[i]), b1 = UInt32(base[i + 1]), b2 = UInt32(base[i + 2]), b3 = UInt32(base[i + 3])
                let b4 = UInt32(base[i + 4]), b5 = UInt32(base[i + 5]), b6 = UInt32(base[i + 6]), b7 = UInt32(base[i + 7])
                let lo: UInt32 = b0 | (b1 << 8) | (b2 << 16) | (b3 << 24)
                let a: UInt32 = c ^ lo
                let b: UInt32 = b4 | (b5 << 8) | (b6 << 16) | (b7 << 24)
                let x7: UInt32 = t[7 * 256 + Int(a & 0xFF)]
                let x6: UInt32 = t[6 * 256 + Int((a >> 8) & 0xFF)]
                let x5: UInt32 = t[5 * 256 + Int((a >> 16) & 0xFF)]
                let x4: UInt32 = t[4 * 256 + Int(a >> 24)]
                let x3: UInt32 = t[3 * 256 + Int(b & 0xFF)]
                let x2: UInt32 = t[2 * 256 + Int((b >> 8) & 0xFF)]
                let x1: UInt32 = t[1 * 256 + Int((b >> 16) & 0xFF)]
                let x0: UInt32 = t[Int(b >> 24)]
                c = x7 ^ x6 ^ x5 ^ x4 ^ x3 ^ x2 ^ x1 ^ x0
                i += 8
            }
            while i < n {
                let at = Int((c ^ UInt32(base[i])) & 0xFF)
                c = t[at] ^ (c >> 8)
                i += 1
            }
        }
        value = ~c
    }

    mutating func update(_ d: Data) { d.withUnsafeBytes { update($0) } }
}
