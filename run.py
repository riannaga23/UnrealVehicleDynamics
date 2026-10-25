#!/usr/bin/env python3
"""Build and run an explicit Unreal + Cesium + ArduPilot demonstration."""

import json
import os
from datetime import datetime
from pathlib import Path
import signal
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parent
PROJECT = ROOT / "unreal" / "UnrealVehicleDynamics.uproject"
PROFILES = {
    "plane": {
        "config": ROOT / "examples" / "run.json",
        "image": "uvd-ardupilot:v1",
        "dockerfile": ROOT / "ardupilot" / "Dockerfile",
        "run_prefix": "fbwa-roll",
    },
    "otter": {
        "config": ROOT / "examples" / "otter-run.json",
        "image": "uvd-ardurover:v1",
        "dockerfile": ROOT / "ardupilot" / "Dockerfile.rover",
        "run_prefix": "acro-turn",
    },
    "otter-open-loop": {
        "config": ROOT / "examples" / "otter-run.json",
        "image": None,
        "dockerfile": None,
        "run_prefix": "otter-open-loop",
    },
    "dual": {
        "plane_config": ROOT / "examples" / "dual-plane-run.json",
        "otter_config": ROOT / "examples" / "dual-otter-run.json",
        "plane_image": "uvd-ardupilot:v1",
        "otter_image": "uvd-ardurover:v1",
        "images": [
            ("uvd-ardupilot:v1", ROOT / "ardupilot" / "Dockerfile"),
            ("uvd-ardurover:v1", ROOT / "ardupilot" / "Dockerfile.rover"),
        ],
        "run_prefix": "dual",
    },
}


def load_local_env():
    path = ROOT / ".env"
    if not path.is_file():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name:
            os.environ.setdefault(name, value)


def unreal_editor():
    configured = os.environ.get("UVD_UNREAL_EDITOR")
    candidates = [
        Path(configured) if configured else None,
        Path(
            "/Users/Shared/Epic Games/UE_5.8/Engine/Binaries/Mac/UnrealEditor.app/Contents/MacOS/UnrealEditor"
        ),
        Path("/Users/Shared/Epic Games/UE_5.8/Engine/Binaries/Mac/UnrealEditor"),
        Path("/opt/UnrealEngine/Engine/Binaries/Linux/UnrealEditor"),
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate.resolve()
    raise RuntimeError("Unreal Editor 5.8 was not found; set UVD_UNREAL_EDITOR")


def engine_root(editor):
    for parent in editor.parents:
        if (parent / "Build" / "Build.version").is_file():
            return parent
    raise RuntimeError("could not find the Unreal Engine root")


def build(editor, profile):
    subprocess.run(["cmake", "-S", str(ROOT), "-B", str(ROOT / "build")], check=True)
    subprocess.run(["cmake", "--build", str(ROOT / "build")], check=True)
    engine = engine_root(editor)
    if sys.platform == "darwin":
        script = engine / "Build" / "BatchFiles" / "Mac" / "Build.sh"
        platform = "Mac"
    elif sys.platform == "win32":
        script = engine / "Build" / "BatchFiles" / "Build.bat"
        platform = "Win64"
    else:
        script = engine / "Build" / "BatchFiles" / "Linux" / "Build.sh"
        platform = "Linux"
    subprocess.run(
        [
            str(script),
            "UnrealVehicleDynamicsEditor",
            platform,
            "Development",
            f"-Project={PROJECT}",
            "-WaitMutex",
            "-NoHotReloadFromIDE",
        ],
        check=True,
    )
    images = list(profile.get("images", []))
    if profile.get("image"):
        images.append((profile["image"], profile["dockerfile"]))
    for image, dockerfile in images:
        subprocess.run(
            [
                "docker",
                "build",
                "-t",
                image,
                "-f",
                str(dockerfile),
                str(ROOT),
            ],
            check=True,
        )


def wait_for_unreal(unreal, truth_path, timeout_s=60):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if truth_path.is_file() and truth_path.stat().st_size:
            # The component opens its UDP sockets immediately after its output
            # files. Give BeginPlay time to finish before SITL sends frame 0.
            time.sleep(0.5)
            if unreal.poll() is not None:
                raise RuntimeError("Unreal exited while UVD was starting")
            return
        returncode = unreal.poll()
        if returncode is not None:
            raise RuntimeError(
                f"Unreal exited with status {returncode} before UVD was ready"
            )
        time.sleep(0.1)
    raise RuntimeError(f"Unreal did not become ready within {timeout_s} seconds")


def home_from_config(config):
    origin = config["origin"]
    return ",".join(
        str(value)
        for value in [
            origin["latitude_deg"],
            origin["longitude_deg"],
            origin["altitude_msl_m"],
            origin["heading_deg"],
        ]
    )


def wait_for_markers(markers, unreal, controllers, description, timeout_s=120):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if all(path.is_file() for path in markers):
            return
        if unreal.poll() is not None:
            raise RuntimeError(
                f"Unreal exited with status {unreal.returncode} before {description}"
            )
        for name, controller in controllers:
            if controller.poll() is not None:
                raise RuntimeError(
                    f"{name} exited with status {controller.returncode} "
                    f"before {description}"
                )
        time.sleep(0.1)
    raise RuntimeError(f"timed out waiting for {description}")


def wait_for_controllers(controllers, unreal, timeout_s=15):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        failed = [
            (name, controller.returncode)
            for name, controller in controllers
            if controller.poll() not in (None, 0)
        ]
        if failed:
            name, returncode = failed[0]
            raise RuntimeError(f"{name} exited with status {returncode}")
        if all(controller.poll() == 0 for _name, controller in controllers):
            return
        if unreal.poll() is not None:
            raise RuntimeError(
                f"Unreal exited with status {unreal.returncode} during controller cleanup"
            )
        time.sleep(0.1)
    raise RuntimeError("timed out waiting for both controllers to exit cleanly")


def dual_controller_command(profile, vehicle, config, run_dir, container_name, sysid):
    controller = config["controller"]
    command = [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--volume",
        f"{run_dir}:/scenario",
    ]
    if sys.platform.startswith("linux"):
        command += ["--add-host", "host.docker.internal:host-gateway"]
    command += [
        "-e",
        "UVD_HOST_ADDRESS=host.docker.internal",
        "-e",
        f"UVD_HOME={home_from_config(config)}",
        "-e",
        f"UVD_RATE_HZ={round(1.0 / config['fixed_dt_s'])}",
        "-e",
        f"UVD_CONTROL_PORT={controller['control_port']}",
        "-e",
        f"UVD_SIM_PORT_IN={controller['control_port']}",
        "-e",
        f"UVD_SIM_PORT_OUT={controller['udp_port']}",
        "-e",
        f"UVD_SYSID={sysid}",
        "-e",
        f"UVD_DURATION_S={config['duration_s']}",
        "-e",
        f"UVD_RUN_DIR=/scenario/{vehicle}",
        "-e",
        f"UVD_READY_FILE=/scenario/{vehicle}.ready",
        "-e",
        "UVD_START_FILE=/scenario/start",
        "-e",
        f"UVD_DONE_FILE=/scenario/{vehicle}.done",
        "-e",
        "UVD_FINISH_FILE=/scenario/finish",
        profile[f"{vehicle}_image"],
    ]
    return command


def render_report(image, run_dir):
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--volume",
            f"{run_dir}:/run",
            "--entrypoint",
            "/opt/ardupilot-venv/bin/python",
            image,
            "/opt/ardupilot/report.py",
            "/run",
        ],
        check=True,
    )


def run_dual(editor, profile, run_dir):
    plane_config = json.loads(profile["plane_config"].read_text())
    otter_config = json.loads(profile["otter_config"].read_text())
    if plane_config["fixed_dt_s"] != otter_config["fixed_dt_s"]:
        raise RuntimeError("dual vehicle configurations must use one fixed timestep")
    if plane_config["origin"] != otter_config["origin"]:
        raise RuntimeError("dual vehicle configurations must use one shared origin")
    if plane_config["duration_s"] != otter_config["duration_s"]:
        raise RuntimeError("dual vehicle configurations must use one duration")
    plane_ports = set(plane_config["controller"].values())
    otter_ports = set(otter_config["controller"].values())
    if plane_ports & otter_ports:
        raise RuntimeError("dual vehicle configurations must use distinct UDP ports")
    scenario = {
        "format": "uvd-dual-run-v1",
        "duration_s": plane_config["duration_s"],
        "fixed_dt_s": plane_config["fixed_dt_s"],
        "truth_rate_hz": 20,
        "origin": plane_config["origin"],
        "vehicles": {
            "plane": {
                "config": str(profile["plane_config"].relative_to(ROOT)),
                "controller": plane_config["controller"],
                "system_id": 1,
            },
            "otter": {
                "config": str(profile["otter_config"].relative_to(ROOT)),
                "controller": otter_config["controller"],
                "system_id": 17,
            },
        },
    }
    (run_dir / "scenario.json").write_text(
        json.dumps(scenario, indent=2, sort_keys=True) + "\n"
    )
    for vehicle in ("plane", "otter"):
        (run_dir / vehicle).mkdir()

    unreal_command = [
        str(editor),
        str(PROJECT),
        "-game",
        "-log",
        "-novsync",
        "-Multiprocess",
        f"-UvdFixedDt={plane_config['fixed_dt_s']}",
        f"-UvdPlaneRun={profile['plane_config']}",
        f"-UvdPlaneTruth={run_dir / 'plane' / 'truth.csv'}",
        f"-UvdOtterRun={profile['otter_config']}",
        f"-UvdOtterTruth={run_dir / 'otter' / 'truth.csv'}",
    ]
    unreal = subprocess.Popen(unreal_command)
    container_names = {
        "plane": f"uvd-dual-plane-{os.getpid()}",
        "otter": f"uvd-dual-otter-{os.getpid()}",
    }
    controllers = []
    interrupted = False
    scenario_complete = False

    def stop(_signal=None, _frame=None):
        nonlocal interrupted
        if _signal is not None:
            interrupted = True
        if unreal.poll() is None:
            unreal.terminate()
        for name in container_names.values():
            subprocess.run(
                ["docker", "stop", "--timeout", "7", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        wait_for_unreal(unreal, run_dir / "plane" / "truth.csv")
        wait_for_unreal(unreal, run_dir / "otter" / "truth.csv")
        plane = subprocess.Popen(
            dual_controller_command(
                profile, "plane", plane_config, run_dir, container_names["plane"], 1
            )
        )
        controllers.append(("ArduPlane", plane))
        otter = subprocess.Popen(
            dual_controller_command(
                profile, "otter", otter_config, run_dir, container_names["otter"], 17
            )
        )
        controllers.append(("ArduRover", otter))
        wait_for_markers(
            [run_dir / "plane.ready", run_dir / "otter.ready"],
            unreal,
            controllers,
            "both controllers to become ready",
        )
        (run_dir / "start").write_text("start\n")
        print("uvd: both controllers ready; releasing dual scenario", flush=True)
        wait_for_markers(
            [run_dir / "plane.done", run_dir / "otter.done"],
            unreal,
            controllers,
            "both vehicle maneuvers to complete",
        )
        (run_dir / "finish").write_text("finish\n")
        print("uvd: both maneuvers complete; closing controllers", flush=True)
        wait_for_controllers(controllers, unreal)
        scenario_complete = True
    except RuntimeError:
        if not interrupted:
            raise
    finally:
        stop()
        unreal.wait()
        for _name, controller in controllers:
            controller.wait()

    if interrupted:
        return 0
    if not scenario_complete:
        raise RuntimeError("dual scenario stopped before both vehicles completed")
    for name, controller in controllers:
        if controller.returncode != 0:
            raise RuntimeError(f"{name} exited with status {controller.returncode}")
    render_report(profile["plane_image"], run_dir / "plane")
    render_report(profile["otter_image"], run_dir / "otter")
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--volume",
            f"{run_dir}:/scenario",
            "--entrypoint",
            "/opt/ardupilot-venv/bin/python",
            profile["plane_image"],
            "/opt/ardupilot/dual_report.py",
            "/scenario",
        ],
        check=True,
    )
    print(f"uvd: report ready at {run_dir / 'report.html'}", flush=True)
    return 0


def main():
    load_local_env()
    profile_name = sys.argv[1] if len(sys.argv) > 1 else "plane"
    if profile_name not in PROFILES or len(sys.argv) > 2:
        choices = " | ".join(PROFILES)
        raise RuntimeError(f"usage: ./run.py [{choices}]")
    profile = PROFILES[profile_name]
    editor = unreal_editor()
    build(editor, profile)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = ROOT / "runs" / f"{profile['run_prefix']}-{timestamp}"
    if run_dir.exists():
        run_dir = run_dir.with_name(f"{run_dir.name}-{os.getpid()}")
    run_dir.mkdir(parents=True)
    print(f"uvd: writing run to {run_dir}", flush=True)
    if profile_name == "dual":
        return run_dual(editor, profile, run_dir)
    config_path = profile["config"]
    config = json.loads(config_path.read_text())
    home = home_from_config(config)
    rate = round(1.0 / config["fixed_dt_s"])
    container_name = f"uvd-{profile_name}-{os.getpid()}"
    unreal_command = [
        str(editor),
        str(PROJECT),
        "-game",
        "-log",
        "-novsync",
        "-Multiprocess",
        f"-UvdRun={config_path}",
        f"-UvdFixedDt={config['fixed_dt_s']}",
        f"-UvdTruth={run_dir / 'truth.csv'}",
    ]
    if profile_name == "otter-open-loop":
        unreal_command.append("-UvdOpenLoop")
    unreal = subprocess.Popen(unreal_command)
    if not profile["image"]:
        try:
            return unreal.wait()
        except KeyboardInterrupt:
            unreal.terminate()
            return unreal.wait()

    try:
        wait_for_unreal(unreal, run_dir / "truth.csv")
    except BaseException:
        unreal.terminate()
        unreal.wait()
        raise

    docker_command = [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--volume",
        f"{run_dir}:/run",
    ]
    if sys.platform.startswith("linux"):
        docker_command += ["--add-host", "host.docker.internal:host-gateway"]
    docker_command += [
        "-e",
        "UVD_HOST_ADDRESS=host.docker.internal",
        "-e",
        f"UVD_HOME={home}",
        "-e",
        f"UVD_RATE_HZ={rate}",
        "-e",
        f"UVD_CONTROL_PORT={config['controller']['control_port']}",
        "-e",
        "UVD_RUN_DIR=/run",
        profile["image"],
    ]
    controller = subprocess.Popen(docker_command)
    stopping = False

    def stop(_signal=None, _frame=None):
        nonlocal stopping
        if _signal is not None:
            stopping = True
        if unreal.poll() is None:
            unreal.terminate()
        subprocess.run(
            ["docker", "stop", "--timeout", "3", container_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        while unreal.poll() is None and controller.poll() is None:
            time.sleep(0.5)
        controller_finished = controller.poll() is not None
    finally:
        stop()
        unreal.wait()
        controller.wait()
    if stopping:
        return 0
    if controller_finished:
        if controller.returncode:
            return controller.returncode
        render_report(profile["image"], run_dir)
        print(f"uvd: report ready at {run_dir / 'report.html'}", flush=True)
        return 0
    return unreal.returncode or controller.returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"uvd: {error}", file=sys.stderr)
        sys.exit(1)
