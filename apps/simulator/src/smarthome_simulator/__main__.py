"""Smart home device simulator.

    python -m smarthome_simulator plan --homes 3 --devices-per-home 20 > plan.json
    python -m smarthome_simulator run fleet.json [--speed 60] [--fault-rate 1]

`plan` describes homes and devices (no secrets). The hub registers the plan and returns
a fleet file with per-device credentials, which `run` uses. Depends only on
`device-protocol`, exactly like real firmware would.
"""

import argparse
import asyncio
import json
import logging
import random
import signal
import sys
from pathlib import Path
from typing import Any

from smarthome_simulator.catalog import DeviceType
from smarthome_simulator.devices import Faults, SimulatedDevice
from smarthome_simulator.environment import HomeEnvironment
from smarthome_simulator.plan import build_plan
from smarthome_simulator.runtime import BrokerEndpoint, DeviceRunner, now


def faults_for(rng: random.Random, scale: float) -> Faults:
    if scale <= 0:
        return Faults()
    return Faults(
        drops_per_hour=0.5 * scale,
        noise_probability=0.01 * scale,
        malformed_probability=0.002 * scale,
        low_battery=rng.random() < 0.05 * scale,
    )


def build_runners(fleet: dict[str, Any], *, speed: float, fault_rate: float) -> list[DeviceRunner]:
    mqtt = fleet["mqtt"]
    broker = BrokerEndpoint(host=mqtt["host"], port=int(mqtt["port"]), ca_file=mqtt["ca_file"])
    seed = int(fleet.get("seed", 0))
    started = now()
    runners = []
    for home in fleet["homes"]:
        rng = random.Random(f"{seed}:{home['home_id']}")  # noqa: S311 - simulation
        env = HomeEnvironment(rng=rng, started_at=started, speed=speed)
        for spec in home["devices"]:
            device_rng = random.Random(f"{seed}:{spec['device_id']}")  # noqa: S311
            device = SimulatedDevice(
                device_id=spec["device_id"],
                home_id=home["home_id"],
                kind=DeviceType(spec["type"]),
                room=spec["room"],
                env=env,
                rng=device_rng,
                faults=faults_for(device_rng, fault_rate),
            )
            runners.append(DeviceRunner(device, spec["password"], broker))
    return runners


async def run(fleet: dict[str, Any], *, speed: float, fault_rate: float) -> None:
    runners = build_runners(fleet, speed=speed, fault_rate=fault_rate)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    logging.getLogger("smarthome_simulator").info(
        json.dumps({"event": "simulator_started", "devices": len(runners), "speed": speed})
    )
    async with asyncio.TaskGroup() as tasks:
        for runner in runners:
            tasks.create_task(runner.run(stop))


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(message)s")
    parser = argparse.ArgumentParser(prog="smarthome_simulator")
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="print a fleet plan (homes and devices) as JSON")
    plan.add_argument("--homes", type=int, default=3)
    plan.add_argument("--devices-per-home", type=int, default=20)
    plan.add_argument("--seed", type=int, default=42)
    go = sub.add_parser("run", help="run a registered fleet against the broker")
    go.add_argument("fleet", type=Path)
    go.add_argument("--speed", type=float, default=1.0, help="simulated seconds per real second")
    go.add_argument("--fault-rate", type=float, default=1.0, help="0 disables injected faults")
    args = parser.parse_args()

    if args.command == "plan":
        print(
            json.dumps(
                build_plan(
                    homes=args.homes, devices_per_home=args.devices_per_home, seed=args.seed
                ),
                indent=2,
            )
        )
        return 0
    fleet = json.loads(args.fleet.read_text())
    asyncio.run(run(fleet, speed=args.speed, fault_rate=args.fault_rate))
    return 0


if __name__ == "__main__":
    sys.exit(main())
