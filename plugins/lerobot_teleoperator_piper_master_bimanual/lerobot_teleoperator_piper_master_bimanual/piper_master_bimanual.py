import logging
from functools import cached_property
from typing import Any

from lerobot.lerobot_types import RobotAction
from lerobot.teleoperators.teleoperator import Teleoperator
from lerobot.utils.decorators import check_if_already_connected, check_if_not_connected
from lerobot.utils.utils import log_say
from lerobot_teleoperator_piper_master import PiperMaster, PiperMasterConfig

from .config_piper_master_bimanual import PiperMasterBimanualConfig

logger = logging.getLogger(__name__)

ARMS = ("left", "right")


class PiperMasterBimanual(Teleoperator):
    """Both master arms as one teleoperator, prefixing each side's action keys.

    Composition only -- it holds two `PiperMaster` instances. `piper_master` is untouched, so
    the single-cluster path is unaffected.
    """

    config_class = PiperMasterBimanualConfig
    name = "piper_master_bimanual"

    def __init__(self, config: PiperMasterBimanualConfig):
        super().__init__(config)
        self.config = config

        def sub(side: str, port: str) -> PiperMasterConfig:
            return PiperMasterConfig(
                id=f"{config.id}_{side}" if config.id else None,
                calibration_dir=config.calibration_dir,
                port=port,
                judge_flag=config.judge_flag,
                connect_timeout_s=config.connect_timeout_s,
                require_master_frames=config.require_master_frames,
            )

        self.arms: dict[str, PiperMaster] = {
            "left": PiperMaster(sub("left", config.left_port)),
            "right": PiperMaster(sub("right", config.right_port)),
        }

    @cached_property
    def action_features(self) -> dict[str, type]:
        return {
            f"{side}_{key}": float
            for side in ARMS
            for key in self.arms[side].action_features
        }

    @cached_property
    def feedback_features(self) -> dict[str, type]:
        return {}

    @property
    def is_connected(self) -> bool:
        return all(a.is_connected for a in self.arms.values())

    @check_if_already_connected
    def connect(self, calibrate: bool = True) -> None:
        # One arm at a time, and say which. The sub-connect blocks asking for movement, so
        # without naming the side the operator does not know which arm to move -- and moving
        # the wrong one just burns the timeout.
        for side in ARMS:
            port = self.arms[side].config.port
            if self.config.require_master_frames:
                log_say(f"Move the {side} master arm now", play_sounds=True, blocking=False)
                logger.info("Waiting for the %s master arm on %s -- MOVE THAT ARM NOW.", side, port)
            self.arms[side].connect(calibrate)
            if self.config.require_master_frames:
                logger.info("%s master arm confirmed live on %s.", side, port)
            else:
                # Nothing was proven: require_master_frames=False skips the check entirely,
                # and saying "confirmed" here would hide that both arms may be silent.
                logger.warning(
                    "%s master arm attached on %s but NOT verified "
                    "(require_master_frames=False). If it is resting, its actions are zeros.",
                    side, port,
                )

        logger.info(f"{self} listening on both buses.")

    @property
    def is_calibrated(self) -> bool:
        return all(a.is_calibrated for a in self.arms.values())

    def calibrate(self) -> None:
        """No-op: master-arm encoders are absolute and zeroed in firmware."""

    def configure(self) -> None:
        """No-op: this teleoperator never writes to either bus."""

    @check_if_not_connected
    def get_action(self) -> RobotAction:
        action: RobotAction = {}
        for side in ARMS:
            for key, value in self.arms[side].get_action().items():
                action[f"{side}_{key}"] = value
        return action

    def send_feedback(self, feedback: dict[str, Any]) -> None:
        """No-op: writing here would put the PC on a bus during recording."""

    @check_if_not_connected
    def disconnect(self) -> None:
        for side in ARMS:
            self.arms[side].disconnect()
        logger.info(f"{self} disconnected.")
