#!/bin/sh
# Pod one-shot bootstrap. The complete function must parse before any action runs.
main() {
    pod_fail() {
        pod_failure=$1
        pod_exit=$2
        if [ -t 1 ]; then
            pod_mark='[x]'
            case "${LC_ALL:-${LC_CTYPE:-${LANG:-C}}}" in
                *UTF-8*|*utf-8*|*UTF8*|*utf8*)
                    if [ "${TERM:-}" != dumb ]; then pod_mark='✗'; fi ;;
            esac
            if [ -z "${NO_COLOR+x}" ] && [ "${TERM:-}" != dumb ]; then
                pod_mark=$(printf '\033[38;5;160m%s\033[0m' "$pod_mark")
            fi
            printf '\n  Install stopped ---------------------------------------------\n' >&2
            printf '  %s  %s\n' "$pod_mark" "$pod_failure" >&2
            printf '  State  No Pod skill or launcher was promoted.\n' >&2
            printf '  Existing agent sessions and workers unchanged.\n' >&2
            printf '  Next   Resolve the cause, then rerun:\n' >&2
            printf '  curl -fsSL https://raw.githubusercontent.com/j3w1/pod/main/install.sh | sh\n' >&2
            printf '  Exit code %s\n' "$pod_exit" >&2
        else
            printf 'pod-install: %s\n' "$pod_failure" >&2
        fi
        return "$pod_exit"
    }
    if [ "$(uname -s)" != Linux ]; then pod_fail 'Linux is required' 1; return $?; fi
    if [ "$(id -u)" -eq 0 ]; then pod_fail 'refuse root; run as your user' 1; return $?; fi
    pod_python=
    for candidate in python3 python3.14 python3.13; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; assert sys.version_info >= (3, 13); import ensurepip, curses' </dev/null >/dev/null 2>&1; then
            pod_python=$(command -v "$candidate")
            break
        fi
    done
    if [ -z "$pod_python" ]; then pod_fail 'Python 3.13+ with venv, ensurepip and curses is required' 1; return $?; fi
    if ! command -v npx >/dev/null 2>&1; then pod_fail 'Node and npx are required' 1; return $?; fi
    pod_source=${POD_INSTALL_SOURCE:-https://codeload.github.com/j3w1/pod/tar.gz/refs/heads/main}
    case "$pod_source" in https://*|file://*|http://127.0.0.1:*|http://localhost:*) ;; *) pod_fail 'POD_INSTALL_SOURCE must be HTTPS, file, or local loopback HTTP' 2; return $?;; esac
    pod_stage=$(mktemp -d "${TMPDIR:-/tmp}/pod-install.XXXXXXXX") || { pod_fail 'could not create a temporary stage' 1; return $?; }
    trap 'rm -rf "$pod_stage"' EXIT HUP TERM
    trap 'pod_fail "interrupted before completion" 130; exit 130' INT
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --max-time 120 "$pod_source" -o "$pod_stage/source.tar.gz" </dev/null || { pod_fail 'download failed' 1; return $?; }
    elif command -v wget >/dev/null 2>&1; then
        wget -q -T 120 -O "$pod_stage/source.tar.gz" "$pod_source" </dev/null || { pod_fail 'download failed' 1; return $?; }
    else
        pod_fail 'curl or wget is required' 1; return $?
    fi
    mkdir "$pod_stage/tree" || { pod_fail 'could not prepare the temporary stage' 1; return $?; }
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
) || { pod_fail 'source archive is invalid' 1; return $?; }
    "$pod_python" "$pod_tree/skills/pod/scripts/pod.py" __install "$pod_tree" </dev/null
    return $?
}
main "$@"
