import logging
import math
import time
from functools import cached_property
from typing import Any

from lerobot.lerobot_types import RobotAction
from lerobot.teleoperators.teleoperator import Teleoperator
from lerobot.utils.utils import log_say
from lerobot.utils.decorators import check_if_already_connected, check_if_not_connected

from .config_piper_master import PiperMasterConfig

logger = logging.getLogger(__name__)

JOINTS = ("joint_1", "joint_2", "joint_3", "joint_4", "joint_5", "joint_6")

# Must match lerobot_robot_piper_bus: joints in radians, gripper stroke in metres.
RAD_PER_MILLIDEG = math.pi / 180.0 / 1000.0
M_PER_MICRON = 1e-6


class PiperMaster(Teleoperator):
    """Listen-only view of a PiPER master arm on a shared CAN bus.

    `get_action` returns the joint-control frames (0x155-0x157) and gripper-control frame
    (0x159) the master already broadcasts for the firmware master-slave link. Pair this with
    a `piper_bus` robot running `passive=True` so nothing else writes to the bus.
    """

    config_class = PiperMasterConfig
    name = "piper_master"

    def __init__(self, config: PiperMasterConfig):
        super().__init__(config)
        self.config = config
        self.piper = None
        # Latched once a real control frame has been seen. Until then, the SDK's held
        # values are zeros, and zeros recorded as actions look like valid data.
        self._seen_master = False

    @cached_property
    def action_features(self) -> dict[str, type]:
        return {f"{name}.pos": float for name in (*JOINTS, "gripper")}

    @cached_property
    def feedback_features(self) -> dict[str, type]:
        return {}

    @property
    def is_connected(self) -> bool:
        return self.piper is not None

    @check_if_already_connected
    def connect(self, calibrate: bool = True) -> None:
        from piper_sdk import C_PiperInterface_V2

        self.piper = C_PiperInterface_V2(self.config.port, judge_flag=self.config.judge_flag)
        # Read-only attach: no CAN re-init, no arm init. Both would disturb the running link.
        self.piper.ConnectPort(can_init=False, piper_init=False, start_thread=True)

        if self.config.require_master_frames:
            self._wait_for_master_frames()
        logger.info(f"{self} listening on {self.config.port}.")

    def _wait_for_master_frames(self) -> None:
        """Prove the master arm is on the bus before any recording starts.

        A teach-mode (0xFA) master arm is silent while nobody touches it, so silence alone
        says nothing -- we have to ask the operator to move it. Skipping this check is what
        produces a dataset whose action column is all zeros, which nothing downstream flags.
        """
        timeout = self.config.connect_timeout_s
        log_say(
            "Move the master arm now so I can confirm it is on the bus",
            play_sounds=True,
            blocking=False,
        )
        logger.info(
            "Waiting up to %.0fs for master-arm control frames (0x155-0x157) on %s -- "
            "MOVE THE MASTER ARM NOW. A teach-mode arm sends nothing while it rests.",
            timeout,
            self.config.port,
        )

        deadline = time.perf_counter() + timeout
        next_notice = time.perf_counter() + 5.0
        while time.perf_counter() < deadline:
            hz = self.piper.GetArmJointCtrl().Hz
            if hz > 0:
                logger.info("Master arm detected at %.0f Hz.", hz)
                self._seen_master = True
                return
            if time.perf_counter() >= next_notice:
                logger.info("  still nothing -- %.0fs left", deadline - time.perf_counter())
                next_notice += 5.0
            time.sleep(0.05)

        raise ConnectionError(
            f"No master-arm control frames (0x155-0x157) seen on {self.config.port} in "
            f"{timeout:.0f}s of moving the arm.\n"
            f"  - Master arm powered on, and on the same CAN bus as the follower?\n"
            f"  - Run `bus_scan.py 15` while pulling the master: expect 0x15x around 116/s.\n"
            f"  - If 0x2A1 reads 400/s, both arms are followers and the master-slave pairing\n"
            f"    is broken; see TROUBLESHOOTING_MASTER_SLAVE.md section 4.\n"
            f"  - To bypass this check: --teleop.require_master_frames=false"
        )

    @property
    def is_calibrated(self) -> bool:
        return True

    def calibrate(self) -> None:
        """No-op: the master arm's encoders are absolute and zeroed in firmware."""

    def configure(self) -> None:
        """No-op: this teleoperator never writes to the bus."""

    @check_if_not_connected
    def get_action(self) -> RobotAction:
        if not self._seen_master:
            # Only reachable via require_master_frames=false. Hz decays to 0 whenever the
            # master rests, so this latches on the first live frame rather than polling.
            if self.piper.GetArmJointCtrl().Hz > 0:
                self._seen_master = True
            else:
                raise RuntimeError(
                    f"Still no master-arm control frames on {self.config.port}. Every action "
                    f"read so far would be zeros. Move the master arm, or drop "
                    f"--teleop.require_master_frames=false so this is caught at connect time."
                )

        joints = self.piper.GetArmJointCtrl().joint_ctrl
        gripper = self.piper.GetArmGripperCtrl().gripper_ctrl

        action: RobotAction = {
            f"{name}.pos": getattr(joints, name) * RAD_PER_MILLIDEG for name in JOINTS
        }
        action["gripper.pos"] = gripper.grippers_angle * M_PER_MICRON
        return action

    def send_feedback(self, feedback: dict[str, Any]) -> None:
        """No-op: sending anything here would put the PC on the bus during recording."""

    @check_if_not_connected
    def disconnect(self) -> None:
        self.piper.DisconnectPort()
        self.piper = None
        self._seen_master = False
        logger.info(f"{self} disconnected.")
