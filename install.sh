#!/bin/sh
# Pod one-shot bootstrap. The complete function must parse before any action runs.
main() {
    if [ "$(uname -s)" != Linux ]; then echo 'pod-install: Linux is required' >&2; return 1; fi
    if [ "$(id -u)" -eq 0 ]; then echo 'pod-install: refuse root; run as your user' >&2; return 1; fi
    pod_python=
    for candidate in python3 python3.14 python3.13; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; assert sys.version_info >= (3, 13); import ensurepip, curses' </dev/null >/dev/null 2>&1; then
            pod_python=$(command -v "$candidate")
            break
        fi
    done
    if [ -z "$pod_python" ]; then echo 'pod-install: Python 3.13+ with venv, ensurepip and curses is required' >&2; return 1; fi
    if ! command -v npx >/dev/null 2>&1; then echo 'pod-install: Node and npx are required' >&2; return 1; fi
    pod_source=${POD_INSTALL_SOURCE:-https://codeload.github.com/j3w1/pod/tar.gz/refs/heads/main}
    case "$pod_source" in https://*|file://*|http://127.0.0.1:*|http://localhost:*) ;; *) echo 'pod-install: POD_INSTALL_SOURCE must be HTTPS, file, or local loopback HTTP' >&2; return 2;; esac
    pod_stage=$(mktemp -d "${TMPDIR:-/tmp}/pod-install.XXXXXXXX") || return 1
    trap 'rm -rf "$pod_stage"' EXIT HUP TERM
    trap 'exit 130' INT
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --max-time 120 "$pod_source" -o "$pod_stage/source.tar.gz" </dev/null || { echo 'pod-install: download failed' >&2; return 1; }
    elif command -v wget >/dev/null 2>&1; then
        wget -q -T 120 -O "$pod_stage/source.tar.gz" "$pod_source" </dev/null || { echo 'pod-install: download failed' >&2; return 1; }
    else
        echo 'pod-install: curl or wget is required' >&2; return 1
    fi
    mkdir "$pod_stage/tree" || return 1
    pod_tree=$("$pod_python" - "$pod_stage/source.tar.gz" "$pod_stage/tree" <<'PY'
import pathlib, sys, tarfile
archive, target = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
try:
    if archive.stat().st_size > 64 * 1024 * 1024:
        raise ValueError('oversized source archive')
    with tarfile.open(archive, 'r:gz') as source:
        members = source.getmembers()
        if len(members) > 1024 or sum(member.size for member in members) > 64 * 1024 * 1024:
            raise ValueError('oversized source archive')
        for member in members:
            path = pathlib.PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts or member.islnk() or member.isdev():
                raise ValueError('unsafe source archive')
        source.extractall(target, filter='data')
    roots = [path for path in target.iterdir() if path.is_dir() and (path/'skills/pod/installer.py').is_file()]
    if len(roots) != 1:
        raise ValueError('source archive has no single Pod bundle')
    print(roots[0])
except (OSError, ValueError, tarfile.TarError):
    sys.exit(1)
PY
) || { echo 'pod-install: source archive is invalid' >&2; return 1; }
    "$pod_python" "$pod_tree/skills/pod/scripts/pod.py" __install "$pod_tree" </dev/null
    return $?
}
main "$@"
