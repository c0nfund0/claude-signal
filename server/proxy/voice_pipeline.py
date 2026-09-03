#!/usr/bin/env python3
"""One-shot speech-to-text / text-to-speech helper for claude-signal voice
messages, adapted from the STT/TTS approach in the sibling `aivoiceassistant`
(`vassist`) project - faster-whisper for transcription, edge-tts for synthesis.

Deliberately NOT a long-running daemon: signal_bridge.py (see its "Voice
messages" handling) invokes this as a fresh subprocess per voice note. A
resident whisper model would sit in memory for the life of the proxy - a
t3.small with 2GB RAM already runs signal-cli's JVM, Squid, nginx, and every
other daemon here, and voice messages are rare enough that paying ~1-3s of
model load per message is the better trade.

Lives in its own virtualenv (/opt/claude-signal/voice-venv) with real pip
dependencies (faster-whisper, edge-tts) - deliberately kept OUT of the system
Python that every other proxy daemon uses, so those stay pure-stdlib (see the
top of signal_bridge.py for why that matters).

Usage:
  voice_pipeline.py transcribe <audio-file> <whisper-model-size>
      -> prints {"text": "..."} as JSON on stdout
  voice_pipeline.py synthesize <text> <output-mp3-path>
      -> writes an mp3 to <output-mp3-path>
Exit code 0 on success; a message on stderr and non-zero otherwise.

English-only by design: language auto-detection was unreliable on short voice
notes (e.g. misdetecting English speech as Finnish), so both STT and TTS are
pinned to English rather than trusting a per-message guess.
"""
import asyncio
import json
import sys

_VOICE = "en-US-EmmaMultilingualNeural"


def transcribe(path, model_size):
    from faster_whisper import WhisperModel

    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, _info = model.transcribe(
        path,
        language="en",                    # skip language ID entirely - it was
                                           # misdetecting short/accented clips
                                           # as other languages (e.g. Finnish)
        beam_size=1,
        vad_filter=False,                 # a voice note is already one clean
                                           # utterance, not a live mic stream
        condition_on_previous_text=False,  # avoids runaway repetition
        temperature=0.0,
    )
    text = " ".join(seg.text.strip() for seg in segments).strip()
    print(json.dumps({"text": text}))


def synthesize(text, out_path):
    import edge_tts

    async def run():
        await edge_tts.Communicate(text=text, voice=_VOICE).save(out_path)

    asyncio.run(run())


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2

    cmd = sys.argv[1]
    try:
        if cmd == "transcribe":
            if len(sys.argv) != 4:
                print("usage: voice_pipeline.py transcribe <path> <model-size>", file=sys.stderr)
                return 2
            transcribe(sys.argv[2], sys.argv[3])
        elif cmd == "synthesize":
            if len(sys.argv) != 4:
                print("usage: voice_pipeline.py synthesize <text> <out.mp3>", file=sys.stderr)
                return 2
            synthesize(sys.argv[2], sys.argv[3])
        else:
            print(f"unknown command {cmd!r}", file=sys.stderr)
            return 2
    except Exception as exc:  # noqa: BLE001 - report failure to the caller, don't traceback
        print(f"{cmd} failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
