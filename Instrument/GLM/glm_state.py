"""Private browser state transport; backward compatible with storage_state JSON."""
import json
import os
import tempfile
from pathlib import Path

ORIGIN = "https://chat.z.ai"


def split_state(state):
    native = {k: state[k] for k in ("cookies", "origins") if k in state}
    sessions = state.get("glm_session_storage", {})
    return native, {ORIGIN: sessions.get(ORIGIN, {})}


async def restore_session_storage(context, sessions):
    # Exact origin restriction; never inject credentials into an unrelated page.
    payload = json.dumps({ORIGIN: sessions.get(ORIGIN, {})})
    await context.add_init_script(script="""(() => {
        const all = PAYLOAD;
        const values = all[location.origin];
        if (!values) return;
        for (const [key, value] of Object.entries(values)) {
            if (sessionStorage.getItem(key) === null) sessionStorage.setItem(key, value);
        }
    })();""".replace("PAYLOAD", payload))


async def capture_state(context, page):
    state = await context.storage_state(indexed_db=True)
    session = await page.evaluate("""() => location.origin === 'https://chat.z.ai'
        ? Object.fromEntries(Object.entries(sessionStorage)) : {}""")
    state["glm_session_storage"] = {ORIGIN: session}
    return state


def state_counts(state):
    return {
        "cookies": len(state.get("cookies", [])),
        "origins": len(state.get("origins", [])),
        "indexeddb_databases": sum(len(o.get("indexedDB", [])) for o in state.get("origins", [])),
        "session_storage_keys": len(state.get("glm_session_storage", {}).get(ORIGIN, {})),
    }


def save_private(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".glm-state-")
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(state, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
