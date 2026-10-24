"""Generates sample_meeting.mp3 from the scripted dialogue below.

Requires: espeak-ng and ffmpeg on PATH.   Usage: python generate_sample_audio.py
(The text-to-speech voices are synthetic, so Whisper transcribes them very reliably: ideal for a demo.)
"""

import subprocess
import tempfile
import wave
from pathlib import Path

DIALOGUE = [
    ("m", "Priya: Good morning everyone. Thanks for joining the kickoff for the Acme Retail pilot."),
    ("f", "Anita: Thanks Priya. Our main goal is to launch the pilot in two stores by November fifteenth."),
    ("m", "Ravi: That is tight but doable. I suggest we use PostgreSQL for the data layer, and host everything on one cloud server."),
    ("f", "Anita: That works for us. We have approved a budget of twelve thousand dollars for the pilot."),
    ("m", "Priya: Great. So we have decided on three things. First, the launch date is November fifteenth. Second, we use PostgreSQL. Third, the budget is twelve thousand dollars."),
    ("m", "Ravi: One risk I want to flag. The payment vendor limits their A P I to one hundred requests per minute, which could slow down busy stores."),
    ("f", "Anita: Also, our store managers are only available for training in the first week of November, so the schedule leaves very little room for delay."),
    ("m", "Priya: Understood. Let us assign actions. Ravi, please set up the staging environment by November fifth."),
    ("m", "Ravi: Will do. I will have staging ready by November fifth."),
    ("m", "Priya: I will send the revised proposal to Anita by this Friday."),
    ("f", "Anita: And I will share the sample sales data with Ravi by Monday."),
    ("m", "Priya: One open question. Do we need single sign on for the store managers? Anita, can you check with your I T team?"),
    ("f", "Anita: Yes, I will find out and let you know. Thanks everyone."),
]
VOICES = {"m": "en-us+m3", "f": "en-us+f3"}


def main() -> None:
    out_dir = Path(__file__).parent
    with tempfile.TemporaryDirectory() as tmp:
        merged = Path(tmp) / "merged.wav"
        params, frames = None, []
        for i, (who, line) in enumerate(DIALOGUE):
            part = Path(tmp) / f"{i}.wav"
            subprocess.run(["espeak-ng", "-v", VOICES[who], "-s", "150", "-w", str(part), line], check=True)
            with wave.open(str(part)) as w:
                params = params or w.getparams()
                frames.append(w.readframes(w.getnframes()))
                frames.append(b"\x00\x00" * int(params.framerate * 0.5))  # half-second pause
        with wave.open(str(merged), "wb") as out:
            out.setparams(params)
            out.writeframes(b"".join(frames))
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(merged), "-ac", "1", "-b:a", "48k",
             str(out_dir / "sample_meeting.mp3")],
            check=True,
        )
    (out_dir / "sample_transcript.txt").write_text("\n".join(l for _, l in DIALOGUE) + "\n", encoding="utf-8")
    print("Wrote sample_meeting.mp3 and sample_transcript.txt")


if __name__ == "__main__":
    main()
