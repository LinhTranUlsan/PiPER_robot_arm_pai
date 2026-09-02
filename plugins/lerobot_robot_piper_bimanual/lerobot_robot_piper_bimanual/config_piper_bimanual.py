from dataclasses import dataclass, field

from lerobot.cameras import CameraConfig
from lerobot.robots.config import RobotConfig


@RobotConfig.register_subclass("piper_bimanual")
@dataclass(kw_only=True)
class PiperBimanualConfig(RobotConfig):
    """Two AgileX PiPER master-follower clusters, one CAN bus each, as a single robot.

    Wraps two `piper_bus` robots so one dataset holds both arms -- required for a task where
    the arms interact, e.g. the right arm picks and hands the object to the left arm. A
    single-arm dataset cannot represent that: the handover only makes sense if both arms'
    states are in the same frame at the same timestamp.

    Observation and action keys are prefixed `left_` and `right_`, matching how LeRobot's own
    bimanual robots (bi_so100_follower, bi_rebot_b601_follower) name theirs.

    The two clusters keep their own firmware master-slave links, exactly as in the
    single-cluster case. This class adds no writes of its own.
    """

    left_port: str = "can_left"
    right_port: str = "can_right"

    # Shared with `piper_bus`: True during recording, so the PC stays off both buses while
    # the firmware master-slave links drive the followers. False only for a policy rollout.
    passive: bool = True

    # Per-arm, because a handover puts the two arms in very different parts of their range
    # and one may need a tighter cap than the other. See piper_bus for how to size these.
    left_max_relative_target: float | None = 0.3
    right_max_relative_target: float | None = 0.3

    # Shared knobs -- identical hardware on both buses, so splitting these buys nothing.
    judge_flag: bool = False
    connect_timeout_s: float = 5.0
    move_speed_pct: int = 30
    gripper_effort: int = 1000
    enable_timeout_s: float = 5.0

    # One camera set for the whole rig: the shared scene camera plus one wrist camera per
    # cluster. Owned here, not by the sub-robots, so the scene camera is opened exactly once.
    cameras: dict[str, CameraConfig] = field(default_factory=dict)
