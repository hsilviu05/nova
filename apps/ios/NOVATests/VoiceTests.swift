import Foundation
import Testing

@testable import NOVA

/// Voice, where it can be tested without a microphone.
///
/// The recogniser and the synthesiser are system services and are not
/// exercised here -- an audio session in a unit test proves nothing about
/// either. What *is* testable is the part NOVA wrote: the preference that
/// keeps speaking off until somebody asks for it, and the markup stripping
/// that decides what a reply sounds like out loud.
@MainActor
struct VoiceTests {
    private func store() -> VoiceStore {
        let suite = UserDefaults(suiteName: "nova.tests.\(UUID().uuidString)")!
        return VoiceStore(
            recogniser: SilentRecogniser(), speaker: RecordingSpeaker(), defaults: suite
        )
    }

    @Test("Speaking replies is off until it is turned on")
    func speakingIsOptIn() {
        // A phone on a shared desk that reads every answer aloud is a phone
        // that gets turned off.
        #expect(store().speaksReplies == false)
    }

    @Test("Nothing is spoken while the preference is off")
    func silentByDefault() {
        let speaker = RecordingSpeaker()
        let suite = UserDefaults(suiteName: "nova.tests.\(UUID().uuidString)")!
        let voice = VoiceStore(
            recogniser: SilentRecogniser(), speaker: speaker, defaults: suite
        )

        voice.speak("The API is up.")

        #expect(speaker.spoken.isEmpty)
    }

    @Test("Turning it on is remembered")
    func preferencePersists() {
        let suite = UserDefaults(suiteName: "nova.tests.\(UUID().uuidString)")!
        let first = VoiceStore(
            recogniser: SilentRecogniser(), speaker: RecordingSpeaker(), defaults: suite
        )
        first.speaksReplies = true

        let relaunched = VoiceStore(
            recogniser: SilentRecogniser(), speaker: RecordingSpeaker(), defaults: suite
        )

        #expect(relaunched.speaksReplies)
    }

    @Test("A transcript comes back trimmed")
    func transcriptIsTrimmed() {
        let recogniser = SilentRecogniser(transcript: "  check snapworth  ")
        let suite = UserDefaults(suiteName: "nova.tests.\(UUID().uuidString)")!
        let voice = VoiceStore(
            recogniser: recogniser, speaker: RecordingSpeaker(), defaults: suite
        )

        #expect(voice.stopListening() == "check snapworth")
    }

    // MARK: - What a reply sounds like

    @Test("A code block is mentioned rather than read out")
    func codeBlocksAreNotSpelled() {
        // "backtick backtick backtick bash" is what the naive version says,
        // and a terminal's replies are full of these.
        let spoken = SystemSpeaker.strippingMarkdown(
            "Run this:\n```bash\ndocker ps -a\n```\nThat lists everything."
        )

        #expect(!spoken.contains("```"))
        #expect(spoken.contains("code block"))
        #expect(spoken.contains("That lists everything."))
    }

    @Test("Inline markup is dropped, its words kept")
    func inlineMarkupIsStripped() {
        let spoken = SystemSpeaker.strippingMarkdown(
            "The `nova-api` container is **running**."
        )

        #expect(spoken == "The nova-api container is running.")
    }

    @Test("Headings and bullets lose their punctuation")
    func structureIsStripped() {
        let spoken = SystemSpeaker.strippingMarkdown("## Status\n- API up\n- Redis up")

        #expect(!spoken.contains("#"))
        #expect(!spoken.contains("- "))
        #expect(spoken.contains("API up"))
    }

    @Test("Plain text is left alone")
    func plainTextSurvives() {
        let text = "SnapWorth is healthy. API, PostgreSQL and Redis are responding."
        #expect(SystemSpeaker.strippingMarkdown(text) == text)
    }
}

// MARK: - Doubles

@MainActor
private final class SilentRecogniser: SpeechRecogniser {
    private(set) var transcript: String
    private(set) var isListening = false
    var error: VoiceError?

    init(transcript: String = "") {
        self.transcript = transcript
    }

    func requestAccess() async -> Bool { true }
    func start() throws { isListening = true }

    @discardableResult
    func stop() -> String {
        isListening = false
        return transcript
    }
}

@MainActor
private final class RecordingSpeaker: Speaker {
    private(set) var isSpeaking = false
    private(set) var spoken: [String] = []

    func speak(_ text: String) { spoken.append(text) }
    func stop() { isSpeaking = false }
}
