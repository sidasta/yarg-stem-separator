"""Install the packages for this machine's hardware into the current virtual environment.

Run by the launchers ("Start Stem Separator.bat" / start-stem-separator.sh) with the
venv's own Python, after `uv venv`. Picks a PyTorch build for:
  nvidia  NVIDIA GPU (CUDA)                       - Windows and Linux
  amd     AMD Radeon GPU (ROCm)                   - Linux; Windows is experimental/untested
  cpu     anything else (works everywhere, much slower)
Set STEMSEP_DEVICE=nvidia|amd|cpu to override the automatic choice.
Uses only the standard library: nothing else is installed when this runs.
"""

import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARKER = Path(sys.prefix) / "stemsep-backend.json"
COMMON = ["soundfile", "imageio-ffmpeg", "mido", "audioread"]
# The tested PyTorch release for the NVIDIA, CPU and Linux AMD builds. (torchaudio isn't
# needed; torchvision is a dependency of the separation library and must match torch.)
TORCH = ["torch==2.11.0", "torchvision==0.26.0"]
WINDOWS = os.name == "nt"

# AMD's PyTorch-on-Windows release (Python 3.12, Adrenalin driver 26.2.2 or newer).
AMD_WIN = "https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1/"
AMD_WIN_SDK = [AMD_WIN + f for f in ("rocm_sdk_core-7.2.1-py3-none-win_amd64.whl",
                                     "rocm_sdk_devel-7.2.1-py3-none-win_amd64.whl",
                                     "rocm_sdk_libraries_custom-7.2.1-py3-none-win_amd64.whl",
                                     "rocm-7.2.1.tar.gz")]
AMD_WIN_TORCH = [AMD_WIN + f for f in ("torch-2.9.1%2Brocm7.2.1-cp312-cp312-win_amd64.whl",
                                       "torchvision-0.24.1%2Brocm7.2.1-cp312-cp312-win_amd64.whl")]


def run(cmd, check=True, **kw):
    return subprocess.run(cmd, check=check, text=True, **kw)


def output(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def gpu_names():
    if WINDOWS:
        names = output(["powershell", "-NoProfile", "-Command",
                        "(Get-CimInstance Win32_VideoController).Name"])
    else:
        names = "\n".join(line for line in output(["lspci"]).splitlines()
                          if re.search(r"VGA|3D|Display", line))
    return [n.strip() for n in names.splitlines() if n.strip()]


def detect():
    forced = os.environ.get("STEMSEP_DEVICE", "").strip().lower()
    if forced in ("nvidia", "amd", "cpu"):
        return forced, "chosen with STEMSEP_DEVICE"
    if shutil.which("nvidia-smi") and "GPU" in output(["nvidia-smi", "-L"]):
        return "nvidia", "NVIDIA GPU found"
    names = gpu_names()
    if any("nvidia" in n.lower() for n in names):
        return "nvidia", "NVIDIA GPU found"
    # Dedicated Radeon cards only; integrated "AMD Radeon(TM) Graphics" isn't supported by ROCm.
    radeon = [n for n in names if re.search(r"Radeon.*\b(RX|PRO|AI)\b|Navi\s*[34]\d", n, re.I)]
    if radeon:
        return "amd", f"AMD GPU found ({radeon[0]})"
    if not WINDOWS and Path("/dev/kfd").exists():
        return "amd", "AMD ROCm driver found"
    return "cpu", "no supported GPU found"


def pip(uv, *args):
    run([uv, "pip", "install", "--python", sys.executable, *args])


def pins():
    """Exact versions of the installed PyTorch packages, so later installs can't swap them out."""
    importlib.invalidate_caches()
    out = []
    for name in ("torch", "torchvision"):
        try:
            out.append(f"{name}=={importlib.metadata.version(name)}")
        except importlib.metadata.PackageNotFoundError:
            pass
    return out


def install(uv, device):
    if device == "amd" and WINDOWS:
        print("Installing AMD's PyTorch for Windows (experimental, needs AMD driver 26.2.2 or newer)...")
        pip(uv, *AMD_WIN_SDK)
        pip(uv, *AMD_WIN_TORCH)
        separator = "audio-separator[cpu]"
    else:
        index = {"nvidia": "cu128", "amd": "rocm7.2", "cpu": "cpu"}[device]
        print(f"Installing PyTorch ({index})...")
        pip(uv, *TORCH, "--index-url", f"https://download.pytorch.org/whl/{index}")
        separator = "audio-separator[gpu]" if device == "nvidia" else "audio-separator[cpu]"
    print("Installing the separation packages...")
    pip(uv, separator, *COMMON, *pins())


def copy_ffmpeg():
    importlib.invalidate_caches()
    import imageio_ffmpeg
    src = Path(imageio_ffmpeg.get_ffmpeg_exe())
    dst = Path(sys.executable).parent / ("ffmpeg.exe" if WINDOWS else "ffmpeg")
    shutil.copy2(src, dst)
    if not WINDOWS:
        dst.chmod(0o755)


def check(device):
    code = ("import json, torch; ok = torch.cuda.is_available(); "
            "print(json.dumps({'gpu': ok, 'name': torch.cuda.get_device_name(0) if ok else None, "
            "'torch': torch.__version__}))")
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    try:
        return json.loads(res.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"gpu": False, "name": None, "error": (res.stderr or "").strip()[-500:]}


def main():
    sys.stdout.reconfigure(line_buffering=True)  # show progress as it happens, even when piped
    uv = os.environ.get("UV") or shutil.which("uv")
    if not uv:
        sys.exit("uv not found")
    MARKER.unlink(missing_ok=True)
    device, why = detect()
    print(f"Hardware: {why} -> installing for {device.upper()} ({platform.system()})")
    install(uv, device)
    copy_ffmpeg()
    result = check(device)
    fallback_from = None
    if device == "amd" and not result.get("gpu"):
        # AMD's builds are extremely slow on the CPU, so swap in the normal CPU build instead.
        print("\nThe AMD build installed, but PyTorch can't use this GPU (unsupported card or driver):")
        if result.get("error"):
            print(result["error"])
        print("Switching to the CPU version instead...")
        run([uv, "pip", "uninstall", "--python", sys.executable, "torch", "torchvision", "torchaudio",
             "rocm", "rocm-sdk-core", "rocm-sdk-devel", "rocm-sdk-libraries-custom"], check=False)
        fallback_from, device = "amd", "cpu"
        install(uv, device)
        result = check(device)
    if device != "cpu" and not result.get("gpu"):
        print(f"\nNote: the {device.upper()} build was installed but PyTorch can't use the GPU, "
              "so splitting will run on the CPU (much slower).")
        if result.get("error"):
            print(result["error"])
    elif result.get("gpu"):
        print(f"GPU ready: {result['name']}")
    else:
        print("No supported GPU: splitting will run on the CPU (roughly 10-15 minutes per song).")
    MARKER.write_text(json.dumps({"device": device, "reason": why, "fallback_from": fallback_from, **result},
                                 indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
