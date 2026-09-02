import logging
import math
import time
from functools import cached_property

from lerobot.cameras import make_cameras_from_configs
from lerobot.lerobot_types import RobotAction, RobotObservation
from lerobot.robots.robot import Robot
from lerobot.robots.utils import ensure_safe_goal_position
from lerobot.utils.decorators import check_if_already_connected, check_if_not_connected

from .config_piper_bus import PiperBusConfig

logger = logging.getLogger(__name__)

JOINTS = ("joint_1", "joint_2", "joint_3", "joint_4", "joint_5", "joint_6")

# PiPER wire units -> SI. Joints are milli-degrees, gripper stroke is micro-metres.
RAD_PER_MILLIDEG = math.pi / 180.0 / 1000.0
MILLIDEG_PER_RAD = 1.0 / RAD_PER_MILLIDEG
M_PER_MICRON = 1e-6
MICRON_PER_M = 1e6


class PiperBus(Robot):
    """PiPER follower arm read from a CAN bus it shares with its firmware-paired master.

    Observation comes from 0x2A1 (joint feedback) and the gripper feedback frame, i.e. where
    the arm actually is. It never depends on who is commanding the arm, so the same class
    works while the master arm drives it (recording) and while a policy drives it (eval).
    """

    config_class = PiperBusConfig
    name = "piper_bus"

    def __init__(self, config: PiperBusConfig):
        super().__init__(config)
        self.config = config
        self.piper = None
        self.cameras = make_cameras_from_configs(config.cameras)
        self._warned_passive = False

    @property
    def _motors_ft(self) -> dict[str, type]:
        return {f"{name}.pos": float for name in (*JOINTS, "gripper")}

    @property
    def _cameras_ft(self) -> dict[str, tuple]:
        return {
            cam_key: (cam.height, cam.width, 3)
            for cam_key, cam in self.cameras.items()
        }

    @cached_property
    def observation_features(self) -> dict[str, type | tuple]:
        return {**self._motors_ft, **self._cameras_ft}

    @cached_property
    def action_features(self) -> dict[str, type]:
        return self._motors_ft

    @property
    def is_connected(self) -> bool:
        return self.piper is not None and all(cam.is_connected for cam in self.cameras.values())

    @check_if_already_connected
    def connect(self, calibrate: bool = True) -> None:
        from piper_sdk import C_PiperInterface_V2, LogLevel

        self.piper = C_PiperInterface_V2(
            self.config.port,
            judge_flag=self.config.judge_flag,
            logger_level=getattr(LogLevel, self.config.sdk_log_level.upper()),
        )
        # piper_init=False is not optional: the init routine writes to the arm, which would
        # fight the firmware master-slave link that is already driving this follower.
        self.piper.ConnectPort(can_init=False, piper_init=False, start_thread=True)

        self._wait_for_feedback()

        if not self.config.passive:
            self._enable_drivers()
            # Set the control mode ONCE, here -- not per tick in send_action(). MotionCtrl_2
            # is a mode command, not a motion command: streaming it at 30 Hz makes the
            # controller re-enter CAN control mode on every tick, which drives the CAN error
            # counters to bus-off within seconds while every JointCtrl still looks like it
            # was sent. park_arm.py has always issued it once and has never bus-offed.
            self.piper.MotionCtrl_2(0x01, 0x01, self.config.move_speed_pct, 0x00)
            time.sleep(0.05)

        for cam in self.cameras.values():
            cam.connect()

        logger.info(f"{self} connected on {self.config.port} (passive={self.config.passive}).")

    def _wait_for_feedback(self) -> None:
        """Block until 0x2A1 frames are actually arriving, so we never record zeros."""
        deadline = time.perf_counter() + self.config.connect_timeout_s
        while time.perf_counter() < deadline:
            if self.piper.GetArmJointMsgs().Hz > 0:
                return
            time.sleep(0.05)
        raise ConnectionError(
            f"No joint feedback (0x2A1) on {self.config.port} within "
            f"{self.config.connect_timeout_s}s. Check the arm is powered and the bus is up: "
            f"`ip -details link show {self.config.port}`."
        )

    def _enable_drivers(self) -> None:
        """Bring the six joint drivers up before the first write.

        While the master arm drives this follower the drivers are already enabled, but a
        policy rollout starts with the master powered off, and an arm whose drivers never
        came up silently ignores every command instead of failing.
        """
        self.piper.EnableArm(7, 0x02)
        deadline = time.perf_counter() + self.config.enable_timeout_s
        while time.perf_counter() < deadline:
            if all(self.piper.GetArmEnableStatus()):
                logger.info("All six drivers enabled.")
                return
            time.sleep(0.1)
        raise ConnectionError(
            f"Drivers still not enabled after {self.config.enable_timeout_s}s: "
            f"{self.piper.GetArmEnableStatus()}. Check the arm is powered and not in an error "
            f"state, and that the master arm is switched off so it is not holding the bus."
        )

    @property
    def is_calibrated(self) -> bool:
        return True

    def calibrate(self) -> None:
        """No-op: PiPER joint encoders are absolute and zeroed in firmware."""

    def configure(self) -> None:
        """No-op: configuring the arm would write to a bus the master arm owns."""

    @check_if_not_connected
    def get_observation(self) -> RobotObservation:
        joints = self.piper.GetArmJointMsgs().joint_state
        gripper = self.piper.GetArmGripperMsgs().gripper_state

        obs: RobotObservation = {
            f"{name}.pos": getattr(joints, name) * RAD_PER_MILLIDEG for name in JOINTS
        }
        obs["gripper.pos"] = gripper.grippers_angle * M_PER_MICRON

        for cam_key, cam in self.cameras.items():
            obs[cam_key] = cam.async_read()

        return obs

    @check_if_not_connected
    def send_action(self, action: RobotAction) -> RobotAction:
        if self.config.passive:
            if not self._warned_passive:
                logger.info(
                    "passive=True: not writing to %s. The master arm drives the follower over "
                    "firmware master-slave; a second writer on the same joint-control IDs would "
                    "fight it.",
                    self.config.port,
                )
                self._warned_passive = True
            return action

        goal = {name: float(action[f"{name}.pos"]) for name in JOINTS}
        if self.config.max_relative_target is not None:
            present_joints = self.piper.GetArmJointMsgs().joint_state
            goal = ensure_safe_goal_position(
                {n: (goal[n], getattr(present_joints, n) * RAD_PER_MILLIDEG) for n in JOINTS},
                self.config.max_relative_target,
            )

        joint_cmd = [int(round(goal[name] * MILLIDEG_PER_RAD)) for name in JOINTS]
        gripper_cmd = int(round(action["gripper.pos"] * MICRON_PER_M))

        # No MotionCtrl_2 here on purpose -- connect() already set the mode. See the note there.
        self.piper.JointCtrl(*joint_cmd)
        self.piper.GripperCtrl(abs(gripper_cmd), self.config.gripper_effort, 0x01, 0)
        return action

    @check_if_not_connected
    def disconnect(self) -> None:
        for cam in self.cameras.values():
            cam.disconnect()
        self.piper.DisconnectPort()
        self.piper = None
        logger.info(f"{self} disconnected.")
