import logging
from functools import cached_property

from lerobot.cameras import make_cameras_from_configs
from lerobot.lerobot_types import RobotAction, RobotObservation
from lerobot.robots.robot import Robot
from lerobot.utils.decorators import check_if_already_connected, check_if_not_connected
from lerobot_robot_piper_bus import PiperBus, PiperBusConfig

from .config_piper_bimanual import PiperBimanualConfig

logger = logging.getLogger(__name__)

ARMS = ("left", "right")


class PiperBimanual(Robot):
    """Both PiPER clusters as one robot, so a handover fits in a single dataset.

    Composition only: it holds two `PiperBus` instances and prefixes their keys. Nothing in
    `piper_bus` is modified or subclassed, so the single-cluster path keeps working exactly
    as before -- and any fix there applies here for free.
    """

    config_class = PiperBimanualConfig
    name = "piper_bimanual"

    def __init__(self, config: PiperBimanualConfig):
        super().__init__(config)
        self.config = config

        def sub(side: str, port: str, max_rel: float | None) -> PiperBusConfig:
            return PiperBusConfig(
                id=f"{config.id}_{side}" if config.id else None,
                calibration_dir=config.calibration_dir,
                port=port,
                passive=config.passive,
                judge_flag=config.judge_flag,
                connect_timeout_s=config.connect_timeout_s,
                move_speed_pct=config.move_speed_pct,
                gripper_effort=config.gripper_effort,
                max_relative_target=max_rel,
                enable_timeout_s=config.enable_timeout_s,
                # Empty on purpose: this class owns the cameras. Handing the same scene
                # camera to both sub-robots would open one V4L2 node twice.
                cameras={},
            )

        self.arms: dict[str, PiperBus] = {
            "left": PiperBus(sub("left", config.left_port, config.left_max_relative_target)),
            "right": PiperBus(sub("right", config.right_port, config.right_max_relative_target)),
        }
        self.cameras = make_cameras_from_configs(config.cameras)

    @property
    def _motors_ft(self) -> dict[str, type]:
        return {
            f"{side}_{key}": float
            for side in ARMS
            for key in self.arms[side].action_features
        }

    @property
    def _cameras_ft(self) -> dict[str, tuple]:
        return {key: (cam.height, cam.width, 3) for key, cam in self.cameras.items()}

    @cached_property
    def observation_features(self) -> dict[str, type | tuple]:
        return {**self._motors_ft, **self._cameras_ft}

    @cached_property
    def action_features(self) -> dict[str, type]:
        return self._motors_ft

    @property
    def is_connected(self) -> bool:
        return all(a.is_connected for a in self.arms.values()) and all(
            cam.is_connected for cam in self.cameras.values()
        )

    @check_if_already_connected
    def connect(self, calibrate: bool = True) -> None:
        # Sequential, not concurrent: each sub-connect blocks until 0x2A1 feedback appears on
        # its own bus, and a failure must name which cluster is dead. The two buses are
        # independent adapters, so there is nothing to gain from overlapping them.
        for side in ARMS:
            self.arms[side].connect(calibrate)

        for cam in self.cameras.values():
            cam.connect()

        logger.info(
            "%s connected: left=%s right=%s (passive=%s), %d camera(s).",
            self,
            self.config.left_port,
            self.config.right_port,
            self.config.passive,
            len(self.cameras),
        )

    @property
    def is_calibrated(self) -> bool:
        return all(a.is_calibrated for a in self.arms.values())

    def calibrate(self) -> None:
        """No-op: PiPER encoders are absolute and zeroed in firmware, on both clusters."""

    def configure(self) -> None:
        """No-op: writing config would put the PC on buses the master arms own."""

    @check_if_not_connected
    def get_observation(self) -> RobotObservation:
        obs: RobotObservation = {}
        for side in ARMS:
            for key, value in self.arms[side].get_observation().items():
                obs[f"{side}_{key}"] = value

        for key, cam in self.cameras.items():
            obs[key] = cam.async_read()

        return obs

    @check_if_not_connected
    def send_action(self, action: RobotAction) -> RobotAction:
        sent: RobotAction = {}
        for side in ARMS:
            prefix = f"{side}_"
            arm_action = {
                key.removeprefix(prefix): value
                for key, value in action.items()
                if key.startswith(prefix)
            }
            if not arm_action:
                continue
            for key, value in self.arms[side].send_action(arm_action).items():
                sent[f"{prefix}{key}"] = value
        return sent

    @check_if_not_connected
    def disconnect(self) -> None:
        for cam in self.cameras.values():
            cam.disconnect()
        for side in ARMS:
            self.arms[side].disconnect()
        logger.info(f"{self} disconnected.")
