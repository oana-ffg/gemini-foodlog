"""Create the single-file image consumed by the public FoodLog flasher."""

from pathlib import Path
import subprocess

Import("env")  # type: ignore[name-defined]  # Provided by PlatformIO/SCons.


def merge_firmware(source, target, env) -> None:
    del source, target
    build_dir = Path(env.subst("$BUILD_DIR"))
    platform = env.PioPlatform()
    esptool = Path(platform.get_package_dir("tool-esptoolpy")) / "esptool.py"
    framework = Path(platform.get_package_dir("framework-arduinoespressif32"))
    output = build_dir / "foodlog-camera-fnk0085.bin"
    command = [
        env.subst("$PYTHONEXE"),
        str(esptool),
        "--chip",
        "esp32s3",
        "merge_bin",
        "-o",
        str(output),
        # The board-native PlatformIO upload boots this hardware in DIO mode.
        # Forcing QIO in a merged bootloader causes a watchdog reset loop.
        "--flash_mode",
        "dio",
        "--flash_freq",
        "80m",
        "--flash_size",
        "8MB",
        "0x0",
        str(build_dir / "bootloader.bin"),
        "0x8000",
        str(build_dir / "partitions.bin"),
        "0xe000",
        str(framework / "tools" / "partitions" / "boot_app0.bin"),
        "0x10000",
        str(build_dir / "firmware.bin"),
    ]
    subprocess.run(command, check=True)


env.AddPostAction("$BUILD_DIR/${PROGNAME}.bin", merge_firmware)
