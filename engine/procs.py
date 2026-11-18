"""Whether a process is still there, asked the way each system allows. Nothing here ends or signals a process."""
import os


def alive(pid):
    """True when a process with that number is running (or cannot be judged: someone else's). No number: True."""
    if not pid:
        return True
    if os.name == 'nt':
        # Not os.kill(pid, 0): on Windows that sends Ctrl-C to the process's group.
        import ctypes
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.OpenProcess.restype = ctypes.c_void_p         # a handle is as wide as a pointer: not to be cut to 32 bits
        h = k.OpenProcess(0x1000, False, int(pid))      # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5         # access denied: it is there, and someone else's
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code))
        k.CloseHandle(ctypes.c_void_p(h))
        return bool(ok) and code.value == 259           # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True
