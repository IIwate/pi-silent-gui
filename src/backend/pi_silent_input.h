/*
 * pi-silent-gui injected-input contract (v1)
 *
 * This header is the ONLY contract between the pi-silent-gui backend and a
 * payload DLL injected into a target GUI process. The backend owns the shared
 * memory and the handshake pipe; the DLL hooks the target's input-polling APIs
 * and reads state from here. Neither side may reorder or resize the fields
 * below without bumping PI_SILENT_INPUT_VERSION on both sides at once.
 *
 * Why this exists: on a private desktop there is no real input queue to read,
 * so window messages reach message-driven engines but never reach engines that
 * poll (GetAsyncKeyState / DirectInput / RawInput / GetCursorPos). The payload
 * makes those polls read this table instead, which is the only way to drive a
 * polling engine without touching the user's real input desktop.
 *
 * Trust boundary: same-user, same-session. This is not a sandbox. The mapping
 * and pipe are Local-namespace and readable by any process of this user.
 */
#ifndef PI_SILENT_INPUT_H
#define PI_SILENT_INPUT_H

#include <stdint.h>

#define PI_SILENT_INPUT_VERSION 1u

/* ASCII "PSI1", stored as bytes so a wrong-endian reader still fails loudly. */
#define PI_SILENT_INPUT_MAGIC "PSI1"

/* Total mapping size in bytes. Fixed so the payload can validate before use. */
#define PI_SILENT_INPUT_SIZE 512u

/* keys[] length. Indexed by Windows virtual-key code (VK_*), so VK_LBUTTON and
 * VK_RBUTTON share this table with the keyboard. */
#define PI_SILENT_INPUT_KEY_COUNT 256u

/* keys[vk] carries this bit when the key is held. A hook should translate it to
 * the API's own "down" convention, e.g. GetAsyncKeyState -> 0x8000. Zero means
 * up. Only bit 0x80 is defined; other bits are reserved and must be ignored. */
#define PI_SILENT_INPUT_KEY_DOWN 0x80u

/* flags bits. ACTIVE is set once the backend has armed the table and cleared on
 * release; a payload that reads a cleared ACTIVE bit must report no input held
 * so a lost session cannot leave a key stuck down. */
#define PI_SILENT_INPUT_FLAG_ACTIVE 0x1u

/*
 * Seqlock: cursor_x/cursor_y are two words and can tear across a poll. The
 * backend makes `seq` odd before a write and even after. A reader that needs a
 * consistent cursor pair reads seq (retry while odd), reads the fields, reads
 * seq again, and retries if it changed. Single-byte key reads do not need this.
 */
#pragma pack(push, 1)
typedef struct PiSilentInputState {
    char     magic[4];    /* PI_SILENT_INPUT_MAGIC, no NUL terminator          */
    uint32_t version;     /* PI_SILENT_INPUT_VERSION                           */
    uint32_t seq;         /* seqlock counter; odd = write in progress          */
    uint32_t flags;       /* PI_SILENT_INPUT_FLAG_*                            */
    int32_t  cursor_x;    /* faked GetCursorPos X, desktop/screen coordinates  */
    int32_t  cursor_y;    /* faked GetCursorPos Y, desktop/screen coordinates  */
    uint8_t  keys[PI_SILENT_INPUT_KEY_COUNT]; /* PI_SILENT_INPUT_KEY_DOWN bit  */
    uint8_t  reserved[PI_SILENT_INPUT_SIZE - 24 - PI_SILENT_INPUT_KEY_COUNT];
} PiSilentInputState;
#pragma pack(pop)

/*
 * Environment variables the backend sets in the TARGET process. The payload
 * reads them on load; it must not assume any fixed name derivation of its own.
 *   PI_SILENT_INPUT_SHM  -> exact CreateFileMapping/OpenFileMapping name
 *   PI_SILENT_INPUT_PIPE -> exact CreateFile pipe name to connect for handshake
 */
#define PI_SILENT_INPUT_SHM_ENV  "PI_SILENT_INPUT_SHM"
#define PI_SILENT_INPUT_PIPE_ENV "PI_SILENT_INPUT_PIPE"

/*
 * Handshake: after installing hooks the payload connects PI_SILENT_INPUT_PIPE
 * and writes exactly one UTF-8 JSON line terminated by '\n', then may close its
 * write side. The backend treats a valid line as proof that hooks are live and
 * switches the session to inject mode; a timeout or malformed line degrades the
 * session to message mode. Shape (unknown fields ignored, these are required):
 *
 *   {"proto":"pi-silent-input","version":1,"ok":true,
 *    "pid":<uint>,"bitness":32|64,"hooks":["GetAsyncKeyState", ...]}
 *
 * ok:false (with an optional "error" string) tells the backend the payload
 * loaded but could not hook, so it degrades instead of waiting for the timeout.
 */
#define PI_SILENT_INPUT_PROTO "pi-silent-input"

#endif /* PI_SILENT_INPUT_H */
