import Foundation

/// A thread-safe, single-consumer queue of byte chunks with timed reads.
///
/// The `Network` receive handlers run on a private dispatch queue and push here;
/// the async session API pulls with a deadline. Keeping the buffering explicit
/// means a read that times out never drops a chunk that arrives a moment later.
final class Inbox: @unchecked Sendable {
    private let lock = NSLock()
    private let timerQueue = DispatchQueue(label: "com.libkp.inbox.timer")
    private var chunks: [[UInt8]] = []
    private var failure: Error?
    private var waiters: [UInt64: CheckedContinuation<[UInt8]?, Error>] = [:]
    private var nextWaiterID: UInt64 = 0

    /// Queue a chunk, handing it straight to a waiting reader if there is one.
    func push(_ bytes: [UInt8]) {
        guard !bytes.isEmpty else { return }
        lock.lock()
        if let (id, continuation) = waiters.first {
            waiters.removeValue(forKey: id)
            lock.unlock()
            continuation.resume(returning: bytes)
            return
        }
        chunks.append(bytes)
        lock.unlock()
    }

    /// Record a terminal condition. Buffered chunks are still delivered first.
    func fail(_ error: Error) {
        lock.lock()
        if failure == nil { failure = error }
        let pending = waiters
        waiters.removeAll()
        lock.unlock()
        for (_, continuation) in pending { continuation.resume(throwing: error) }
    }

    /// Whether a terminal condition has been recorded.
    var isFailed: Bool {
        lock.lock()
        defer { lock.unlock() }
        return failure != nil
    }

    /// Take the next chunk, waiting up to `timeout` seconds. Returns `nil` when
    /// the timeout elapses with nothing queued; throws once the stream has
    /// failed and the buffer is drained.
    func next(timeout: TimeInterval) async throws -> [UInt8]? {
        try await withCheckedThrowingContinuation { continuation in
            lock.lock()
            if !chunks.isEmpty {
                let chunk = chunks.removeFirst()
                lock.unlock()
                continuation.resume(returning: chunk)
                return
            }
            if let failure {
                lock.unlock()
                continuation.resume(throwing: failure)
                return
            }
            let id = nextWaiterID
            nextWaiterID &+= 1
            waiters[id] = continuation
            lock.unlock()

            timerQueue.asyncAfter(deadline: .now() + max(timeout, 0)) { [weak self] in
                guard let self else { return }
                self.lock.lock()
                let waiter = self.waiters.removeValue(forKey: id)
                self.lock.unlock()
                waiter?.resume(returning: nil)
            }
        }
    }
}
