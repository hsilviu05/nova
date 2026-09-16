import AVFoundation
import Foundation
import Observation
import Speech

/// Turning speech into text.
///
/// A protocol because the implementation is going to change. Apple's
/// recogniser is what ships first -- it is on the phone, it needs no setup,
/// and it works offline for most locales. The endpoint NOVA is aiming at is
/// a local Whisper on the same Mac the model runs on, which is both more
/// accurate and more private. Nothing above this line should have to know
/// which one it got.
@MainActor
protocol SpeechRecogniser: AnyObject {
    /// What has been heard so far, updated as it is heard.
    var transcript: String { get }
    var isListening: Bool { get }
    var error: VoiceError? { get }

    func requestAccess() async -> Bool
    func start() throws
    /// Stop and return the final transcript.
    @discardableResult
    func stop() -> String
}

/// Turning text into speech.
@MainActor
protocol Speaker: AnyObject {
    var isSpeaking: Bool { get }
    func speak(_ text: String)
    func stop()
}

enum VoiceError: Error, Equatable {
    case notPermitted
    case unavailable
    case failed(String)

    var message: String {
        switch self {
        case .notPermitted:
            "NOVA needs permission to use the microphone and speech recognition. Grant it in Settings."
        case .unavailable:
            "Speech recognition isn't available on this device right now."
        case let .failed(reason):
            reason
        }
    }
}

/// Speech recognition through Apple's on-device recogniser.
///
/// `requiresOnDeviceRecognition` is set wherever the device supports it. That
/// is not a performance choice: without it, audio is sent to Apple's servers,
/// and a terminal whose entire premise is that nothing leaves the network
/// should not quietly make an exception for the microphone.
@MainActor
@Observable
final class AppleSpeechRecogniser: SpeechRecogniser {
    private(set) var transcript = ""
    private(set) var isListening = false
    var error: VoiceError?

    private let recogniser = SFSpeechRecognizer(locale: Locale.current)
    private let engine = AVAudioEngine()
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?

    var isAvailable: Bool {
        recogniser?.isAvailable ?? false
    }

    func requestAccess() async -> Bool {
        let speech = await withCheckedContinuation { continuation in
            SFSpeechRecognizer.requestAuthorization { status in
                continuation.resume(returning: status == .authorized)
            }
        }
        guard speech else {
            error = .notPermitted
            return false
        }

        let microphone = await AVAudioApplication.requestRecordPermission()
        if !microphone { error = .notPermitted }
        return microphone
    }

    func start() throws {
        guard let recogniser, recogniser.isAvailable else {
            error = .unavailable
            throw VoiceError.unavailable
        }

        stopEngine()
        transcript = ""
        error = nil

        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playAndRecord, mode: .measurement, options: .duckOthers)
        try session.setActive(true, options: .notifyOthersOnDeactivation)

        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        if recogniser.supportsOnDeviceRecognition {
            request.requiresOnDeviceRecognition = true
        }
        self.request = request

        let input = engine.inputNode
        input.installTap(onBus: 0, bufferSize: 1024, format: input.outputFormat(forBus: 0)) {
            buffer, _ in
            request.append(buffer)
        }

        engine.prepare()
        try engine.start()
        isListening = true

        task = recogniser.recognitionTask(with: request) { [weak self] result, failure in
            Task { @MainActor in
                guard let self else { return }
                if let result {
                    self.transcript = result.bestTranscription.formattedString
                }
                if failure != nil || result?.isFinal == true {
                    self.stopEngine()
                }
            }
        }
    }

    @discardableResult
    func stop() -> String {
        stopEngine()
        return transcript
    }

    private func stopEngine() {
        guard isListening || engine.isRunning else { return }

        engine.stop()
        engine.inputNode.removeTap(onBus: 0)
        request?.endAudio()
        task?.cancel()
        request = nil
        task = nil
        isListening = false

        // Handing the session back so other audio resumes. Failing here is
        // not worth surfacing: the recording has already stopped, which is
        // what the caller asked for.
        try? AVAudioSession.sharedInstance().setActive(
            false, options: .notifyOthersOnDeactivation
        )
    }
}

/// Speech synthesis through the system voice.
///
/// Speaking is opt-in and off by default. A phone that reads every reply
/// aloud on a desk in a shared room is a phone that gets turned off, and the
/// text is always there to read.
@MainActor
@Observable
final class SystemSpeaker: NSObject, Speaker, AVSpeechSynthesizerDelegate {
    private(set) var isSpeaking = false
    private let synthesiser = AVSpeechSynthesizer()

    override init() {
        super.init()
        synthesiser.delegate = self
    }

    func speak(_ text: String) {
        let spoken = Self.strippingMarkdown(text)
        guard !spoken.isEmpty else { return }

        stop()
        let utterance = AVSpeechUtterance(string: spoken)
        utterance.voice = AVSpeechSynthesisVoice(language: Locale.current.identifier)
        // Slightly faster than the default, which reads as laboured for
        // anything longer than a sentence.
        utterance.rate = AVSpeechUtteranceDefaultSpeechRate * 1.05

        isSpeaking = true
        synthesiser.speak(utterance)
    }

    func stop() {
        if synthesiser.isSpeaking {
            synthesiser.stopSpeaking(at: .immediate)
        }
        isSpeaking = false
    }

    nonisolated func speechSynthesizer(
        _ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance
    ) {
        Task { @MainActor in self.isSpeaking = false }
    }

    nonisolated func speechSynthesizer(
        _ synthesizer: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance
    ) {
        Task { @MainActor in self.isSpeaking = false }
    }

    /// Remove the markup NOVA uses for the screen before reading aloud.
    ///
    /// A reply with a fenced code block is common and unreadable aloud:
    /// "backtick backtick backtick bash". The block is replaced by a
    /// mention of it rather than dropped silently, so the listener knows
    /// something was there.
    static func strippingMarkdown(_ text: String) -> String {
        // Extended delimiters throughout: a backtick inside a bare `/.../`
        // literal ends the literal, and three of these patterns are about
        // backticks.
        var result = text.replacing(
            #/```[\s\S]*?```/#, with: " (there's a code block on screen) "
        )
        result = result.replacing(#/`([^`]*)`/#) { match in String(match.1) }
        result = result.replacing(#/\*\*([^*]*)\*\*/#) { match in String(match.1) }
        result = result.replacing(#/^#{1,6}[ \t]*/#.anchorsMatchLineEndings(), with: "")
        result = result.replacing(#/^[-*][ \t]+/#.anchorsMatchLineEndings(), with: "")
        return result.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}
