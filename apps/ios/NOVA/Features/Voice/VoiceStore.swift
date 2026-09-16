import Foundation
import Observation

/// Voice as a preference, not a mode.
///
/// Text is the primary interface and always works. Voice is an addition that
/// can be turned on, is off until it is, and degrades to "the microphone
/// button does nothing useful" rather than to a broken app when permission is
/// refused or recognition is unavailable.
///
/// Speaking replies is a separate switch from talking to NOVA, because they
/// are separate decisions: a phone on a shared desk that reads every answer
/// aloud is a phone that gets turned off, while dictating a question in the
/// same room is unremarkable.
@MainActor
@Observable
final class VoiceStore {
    private static let speaksKey = "NOVASpeaksReplies"

    private let recogniser: any SpeechRecogniser
    private let speaker: any Speaker
    private let defaults: UserDefaults

    /// Read aloud what NOVA says. Off until switched on.
    var speaksReplies: Bool {
        didSet { defaults.set(speaksReplies, forKey: Self.speaksKey) }
    }

    private(set) var permissionDenied = false

    init(
        recogniser: any SpeechRecogniser = AppleSpeechRecogniser(),
        speaker: any Speaker = SystemSpeaker(),
        defaults: UserDefaults = .standard
    ) {
        self.recogniser = recogniser
        self.speaker = speaker
        self.defaults = defaults
        self.speaksReplies = defaults.bool(forKey: Self.speaksKey)
    }

    var isListening: Bool { recogniser.isListening }
    var transcript: String { recogniser.transcript }
    var isSpeaking: Bool { speaker.isSpeaking }
    var voiceError: VoiceError? { recogniser.error }

    /// Ask for permission if needed, then listen.
    ///
    /// Permission is requested on first use rather than at launch: being
    /// asked for the microphone before doing anything is how an app gets
    /// refused, and NOVA is entirely usable without it.
    func startListening() async {
        guard await recogniser.requestAccess() else {
            permissionDenied = true
            return
        }
        permissionDenied = false

        // Talking and listening at once means the recogniser hears NOVA.
        speaker.stop()
        try? recogniser.start()
    }

    @discardableResult
    func stopListening() -> String {
        recogniser.stop().trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func speak(_ text: String) {
        guard speaksReplies else { return }
        speaker.speak(text)
    }

    func stopSpeaking() {
        speaker.stop()
    }
}
