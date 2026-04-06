"""Landlock filesystem restriction helper.

Standalone script that applies Landlock ABI v1 restrictions via ctypes
syscalls, then exec's the target command. Invoked as:

    python3 -m carpenter.sandbox._landlock_helper \
        --rw /dir1 --rw /dir2 -- command args...

Landlock restricts the calling process's filesystem access at the kernel
level without requiring any privileges. Available since Linux 5.13.

Exit codes:
    1  — Landlock syscalls failed (kernel doesn't support it, etc.)
    *  — The exec'd command's exit code
"""

import ctypes
import ctypes.util
import os
import struct
import sys

# ── Landlock ABI v1 constants ────────────────────────────────────────

# Syscall numbers (aarch64 and x86_64 share the same numbers for Landlock)
_NR_landlock_create_ruleset = 444
_NR_landlock_add_rule = 445
_NR_landlock_restrict_self = 446

# Rule types
LANDLOCK_RULE_PATH_BENEATH = 1

# Filesystem access flags (ABI v1: 13 types)
LANDLOCK_ACCESS_FS_EXECUTE = 1 << 0
LANDLOCK_ACCESS_FS_WRITE_FILE = 1 << 1
LANDLOCK_ACCESS_FS_READ_FILE = 1 << 2
LANDLOCK_ACCESS_FS_READ_DIR = 1 << 3
LANDLOCK_ACCESS_FS_REMOVE_DIR = 1 << 4
LANDLOCK_ACCESS_FS_REMOVE_FILE = 1 << 5
LANDLOCK_ACCESS_FS_MAKE_CHAR = 1 << 6
LANDLOCK_ACCESS_FS_MAKE_DIR = 1 << 7
LANDLOCK_ACCESS_FS_MAKE_REG = 1 << 8
LANDLOCK_ACCESS_FS_MAKE_SOCK = 1 << 9
LANDLOCK_ACCESS_FS_MAKE_FIFO = 1 << 10
LANDLOCK_ACCESS_FS_MAKE_BLOCK = 1 << 11
LANDLOCK_ACCESS_FS_MAKE_SYM = 1 << 12

# All ABI v1 access rights
LANDLOCK_ACCESS_FS_ALL = (1 << 13) - 1

# Read-only subset
LANDLOCK_ACCESS_FS_READ = (
    LANDLOCK_ACCESS_FS_EXECUTE
    | LANDLOCK_ACCESS_FS_READ_FILE
    | LANDLOCK_ACCESS_FS_READ_DIR
)

# Create-ruleset flag for version query
LANDLOCK_CREATE_RULESET_VERSION = 1 << 0

# prctl
PR_SET_NO_NEW_PRIVS = 38

# ── Structs ──────────────────────────────────────────────────────────


class LandlockRulesetAttr(ctypes.Structure):
    """struct landlock_ruleset_attr (ABI v1)."""
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class LandlockPathBeneathAttr(ctypes.Structure):
    """struct landlock_path_beneath_attr."""
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


# ── Syscall wrappers ────────────────────────────────────────────────

_libc = None


def _get_libc():
    global _libc
    if _libc is None:
        libc_name = ctypes.util.find_library("c")
        if libc_name is None:
            libc_name = "libc.so.6"
        _libc = ctypes.CDLL(libc_name, use_errno=True)
    return _libc


def _syscall(nr, *args):
    """Call a Linux syscall via libc.syscall()."""
    libc = _get_libc()
    libc.syscall.restype = ctypes.c_long
    return libc.syscall(ctypes.c_long(nr), *args)


def landlock_create_ruleset(attr, size, flags):
    """landlock_create_ruleset(2) — returns ruleset fd or -1."""
    return _syscall(
        _NR_landlock_create_ruleset,
        ctypes.byref(attr) if attr else ctypes.c_void_p(0),
        ctypes.c_size_t(size),
        ctypes.c_uint32(flags),
    )


def landlock_add_rule(ruleset_fd, rule_type, rule_attr, flags):
    """landlock_add_rule(2) — returns 0 on success, -1 on error."""
    return _syscall(
        _NR_landlock_add_rule,
        ctypes.c_int(ruleset_fd),
        ctypes.c_int(rule_type),
        ctypes.byref(rule_attr),
        ctypes.c_uint32(flags),
    )


def landlock_restrict_self(ruleset_fd, flags):
    """landlock_restrict_self(2) — returns 0 on success, -1 on error."""
    return _syscall(
        _NR_landlock_restrict_self,
        ctypes.c_int(ruleset_fd),
        ctypes.c_uint32(flags),
    )


def probe_landlock_version():
    """Probe Landlock ABI version without creating a ruleset.

    Returns the ABI version (int >= 1) on success, or -1 if unsupported.
    """
    return _syscall(
        _NR_landlock_create_ruleset,
        ctypes.c_void_p(0),
        ctypes.c_size_t(0),
        ctypes.c_uint32(LANDLOCK_CREATE_RULESET_VERSION),
    )


# ── Main logic ───────────────────────────────────────────────────────


def _add_path_rule(ruleset_fd, path, access):
    """Add a Landlock path-beneath rule for the given directory."""
    fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    try:
        attr = LandlockPathBeneathAttr()
        attr.allowed_access = access
        attr.parent_fd = fd
        ret = landlock_add_rule(ruleset_fd, LANDLOCK_RULE_PATH_BENEATH, attr, 0)
        if ret < 0:
            errno = ctypes.get_errno()
            print(
                f"landlock_add_rule failed for {path}: errno={errno}",
                file=sys.stderr,
            )
            return False
    finally:
        os.close(fd)
    return True


def apply_landlock(write_dirs):
    """Apply Landlock restrictions: read-only root, read-write for write_dirs.

    Returns True on success, False on failure.
    """
    # Create ruleset covering all filesystem access types
    attr = LandlockRulesetAttr()
    attr.handled_access_fs = LANDLOCK_ACCESS_FS_ALL

    ruleset_fd = landlock_create_ruleset(
        attr, ctypes.sizeof(attr), 0
    )
    if ruleset_fd < 0:
        errno = ctypes.get_errno()
        print(
            f"landlock_create_ruleset failed: errno={errno}",
            file=sys.stderr,
        )
        return False

    try:
        # Allow read+execute globally
        if not _add_path_rule(ruleset_fd, "/", LANDLOCK_ACCESS_FS_READ):
            return False

        # Allow full access to each write dir
        for d in write_dirs:
            if os.path.isdir(d):
                if not _add_path_rule(ruleset_fd, d, LANDLOCK_ACCESS_FS_ALL):
                    return False

        # Set no-new-privs (required before landlock_restrict_self)
        libc = _get_libc()
        libc.prctl.restype = ctypes.c_int
        ret = libc.prctl(
            ctypes.c_int(PR_SET_NO_NEW_PRIVS),
            ctypes.c_ulong(1),
            ctypes.c_ulong(0),
            ctypes.c_ulong(0),
            ctypes.c_ulong(0),
        )
        if ret != 0:
            print("prctl(PR_SET_NO_NEW_PRIVS) failed", file=sys.stderr)
            return False

        # Restrict self
        ret = landlock_restrict_self(ruleset_fd, 0)
        if ret < 0:
            errno = ctypes.get_errno()
            print(
                f"landlock_restrict_self failed: errno={errno}",
                file=sys.stderr,
            )
            return False
    finally:
        os.close(ruleset_fd)

    return True


def parse_args(argv):
    """Parse CLI arguments: --rw PATH [...] -- command args...

    Returns (write_dirs, command).
    """
    write_dirs = []
    i = 0
    while i < len(argv):
        if argv[i] == "--rw":
            if i + 1 >= len(argv):
                print("--rw requires a path argument", file=sys.stderr)
                sys.exit(1)
            write_dirs.append(argv[i + 1])
            i += 2
        elif argv[i] == "--":
            command = argv[i + 1:]
            if not command:
                print("No command specified after --", file=sys.stderr)
                sys.exit(1)
            return write_dirs, command
        else:
            print(f"Unknown argument: {argv[i]}", file=sys.stderr)
            sys.exit(1)
    print("Missing -- separator and command", file=sys.stderr)
    sys.exit(1)


def main(argv=None):
    """Entry point: parse args, apply Landlock, exec command."""
    if argv is None:
        argv = sys.argv[1:]

    write_dirs, command = parse_args(argv)

    if not apply_landlock(write_dirs):
        print("Failed to apply Landlock restrictions", file=sys.stderr)
        sys.exit(1)

    # Replace this process with the target command
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
