"""Disposable, network-free installer fixtures; no real profile path is inherited."""

from __future__ import annotations

from contextlib import contextmanager
import http.server
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import threading
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def archive_tree(target: Path, *, changes: dict[str, bytes] | None = None) -> Path:
    """Archive tracked working bytes under one tar root, preserving VERSION's link."""
    changes = changes or {}
    names = [raw.decode() for raw in subprocess.check_output(
        ["git", "ls-files", "-z", "--cached"], cwd=ROOT).split(b"\0") if raw]
    with tarfile.open(target, "w:gz") as archive:
        for name in names:
            path = ROOT / name
            if name not in changes:
                archive.add(path, arcname="pod-source/" + name, recursive=False)
                continue
            import io
            data = changes[name]
            entry = tarfile.TarInfo("pod-source/" + name)
            entry.size = len(data)
            entry.mode = 0o644
            archive.addfile(entry, io.BytesIO(data))
    return target


def offline_wheel(directory: Path) -> Path:
    """Repackage the installed pure-Python PyYAML files as a local test wheel."""
    import yaml
    from pod.installer import PIN
    pin_version = PIN.split("==", 1)[1]
    if yaml.__version__ != pin_version:
        raise RuntimeError("The test interpreter needs the installer PyYAML pin")
    directory.mkdir(parents=True, exist_ok=True)
    result = directory / f"PyYAML-{pin_version}-py3-none-any.whl"
    package = Path(yaml.__file__).parent
    records = []
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as wheel:
        for source in sorted(package.glob("*.py")):
            name = "yaml/" + source.name
            wheel.write(source, name)
            records.append(name + ",,")
        metadata = f"PyYAML-{pin_version}.dist-info/"
        values = {
            metadata + "METADATA": f"Metadata-Version: 2.1\nName: PyYAML\nVersion: {pin_version}\n",
            metadata + "WHEEL": "Wheel-Version: 1.0\nGenerator: pod-tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        }
        for name, value in values.items():
            wheel.writestr(name, value)
            records.append(name + ",,")
        wheel.writestr(metadata + "RECORD", "\n".join(records + [metadata + "RECORD,,"]) + "\n")
    return result


STUB_NPX = '''#!/usr/bin/env python3
import json, os, pathlib, shutil, sys, time
args=sys.argv[1:]
log=os.environ.get('POD_TEST_NPX_LOG')
if log: pathlib.Path(log).write_text(json.dumps(args))
mode=os.environ.get('POD_TEST_NPX_MODE','copy')
if mode=='fail': sys.exit(17)
source=pathlib.Path(args[args.index('add')+1])/'skills'/'pod'
canonical=pathlib.Path(os.environ['HOME'])/'.agents/skills/pod'
if mode=='sleep': time.sleep(8)
if canonical.is_symlink(): sys.exit(18)
if canonical.exists(): shutil.rmtree(canonical)
if mode=='half_copy':
 canonical.mkdir(parents=True)
 shutil.copy2(source/'SKILL.md',canonical/'SKILL.md')
 time.sleep(30)
 sys.exit(19)
if mode=='partial_fail':
 canonical.mkdir(parents=True)
 shutil.copy2(source/'SKILL.md',canonical/'SKILL.md')
 sys.exit(19)
canonical.parent.mkdir(parents=True,exist_ok=True)
shutil.copytree(source,canonical,symlinks=False)
claude=pathlib.Path(os.environ.get('CLAUDE_CONFIG_DIR',str(pathlib.Path.home()/'.claude')))/'skills/pod'
claude.parent.mkdir(parents=True,exist_ok=True)
if claude.is_symlink(): claude.unlink()
if claude.exists(): sys.exit(20)
claude.symlink_to(os.path.relpath(canonical,claude.parent),target_is_directory=True)
'''


def sandbox(root: Path) -> tuple[dict[str, str], Path]:
    home, work, bin_dir = root / "home", root / "work", root / "bin"
    for path in (home, work, bin_dir, home / "tmp", home / "zsh"):
        path.mkdir(parents=True, exist_ok=True)
    python = bin_dir / "python3"
    python.symlink_to(sys.executable)
    npx = bin_dir / "npx"
    npx.write_text(STUB_NPX)
    npx.chmod(0o755)
    node = bin_dir / "node"
    node.write_text("#!/bin/sh\nexit 0\n")
    node.chmod(0o755)
    wheel = offline_wheel(root / "wheels")
    archive = archive_tree(root / "source.tar.gz")
    env = {
        "PATH": str(bin_dir) + ":/usr/bin:/bin", "HOME": str(home), "SHELL": "/bin/bash",
        "ZDOTDIR": str(home / "zsh"), "TMPDIR": str(home / "tmp"),
        "XDG_DATA_HOME": str(home / "data"), "XDG_STATE_HOME": str(home / "state"),
        "XDG_CONFIG_HOME": str(home / "config"), "CODEX_HOME": str(home / "codex"),
        "CLAUDE_CONFIG_DIR": str(home / "claude"),
        "PIP_NO_INDEX": "1", "PIP_FIND_LINKS": str(wheel.parent),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1", "npm_config_cache": str(home / "npm-cache"),
        "POD_INSTALL_SOURCE": archive.as_uri(), "POD_TEST_NPX_LOG": str(root / "npx.json"),
        "NO_COLOR": "1", "TERM": "dumb", "LANG": "C.UTF-8",
    }
    return env, archive


def run_install(env: dict[str, str], *, timeout: float = 90) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", str(ROOT / "install.sh")], env=env, cwd=Path(env["HOME"]).parent / "work",
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)


def run_pod(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    launcher = Path(env["HOME"]) / ".local/bin/pod"
    return subprocess.run([str(launcher), *args], env=env, cwd=Path(env["HOME"]).parent / "work",
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=40)


@contextmanager
def local_server(directory: Path):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(directory), **kwargs)
        def log_message(self, *_args):
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
