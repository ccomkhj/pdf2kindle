"""Submit an EPUB through the user's configured macOS Mail account."""

from pathlib import Path
import subprocess


_SCRIPT = """
on run argv
    set destination to item 1 of argv
    set bookFile to (POSIX file (item 2 of argv)) as alias
    set bookTitle to item 3 of argv
    tell application "Mail"
        if (count of smtp servers) is 0 then error "Configure an outgoing account in Apple Mail."
        set msg to make new outgoing message with properties {subject:bookTitle, visible:false}
        tell msg
            make new to recipient at end of to recipients with properties {address:destination}
            make new attachment at after last paragraph with properties {file name:bookFile}
        end tell
        if not (send msg) then error "Mail did not accept the message."
    end tell
end run
"""


def send_epub(epub: Path, destination: str) -> None:
    try:
        result = subprocess.run(
            ["osascript", "-", destination, str(epub.resolve(strict=True)), epub.stem],
            input=_SCRIPT, text=True, capture_output=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"Missing {exc.filename}. Sending requires macOS Mail and an existing EPUB.") from exc
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Apple Mail submission failed.")
