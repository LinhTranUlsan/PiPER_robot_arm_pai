from dataclasses import dataclass, field

from lerobot.cameras import CameraConfig
from lerobot.robots.config import RobotConfig


@RobotConfig.register_subclass("piper_bus")
@dataclass(kw_only=True)
class PiperBusConfig(RobotConfig):
    """AgileX PiPER follower sharing one CAN bus with a firmware-paired master arm.

    The master-slave link lives in the arm firmware, so during recording the PC must stay
    off the wire: `passive=True` turns `send_action` into a no-op. Set `passive=False`
    only when a policy is driving the arm and the master arm is powered off.
    """

    # One CAN bus per master-follower cluster on this rig (can_left, can_right).
    # Always pass --robot.port=can_right (or can_left); this default only matches one cluster.
    port: str = "can_right"
    passive: bool = True

    # piper_sdk's own log level. It logs every failed CAN send at ERROR, and `send_action`
    # ignores those return values anyway -- so on a bus that drops frames the terminal fills
    # with thousands of identical lines and buries everything that matters, including LeRobot's
    # own warnings. Set "CRITICAL" to watch what the robot actually does.
    #
    # This only hides the messages, it does NOT stop the frames being lost. Check the counters
    # for that: `ip -details -statistics link show <port> | grep -A1 re-started`.
    sdk_log_level: str = "WARNING"

    # Passed to C_PiperInterface_V2. `judge_flag` probes whether the CAN port is healthy;
    # PCIe-to-CAN modules need it off.
    judge_flag: bool = False

    # Seconds to wait after connecting for the first 0x2A1 frames to arrive.
    connect_timeout_s: float = 5.0

    # Only used when passive=False. Percentage of max joint speed for MotionCtrl_2.
    move_speed_pct: int = 30

    # Only used when passive=False. Gripper torque in 0.001 N*m.
    gripper_effort: int = 1000

    # Only used when passive=False. Caps |commanded - measured| per joint, in radians, so a
    # policy whose first action targets the demonstrations' start pose cannot fling an arm
    # parked somewhere else. None disables the cap.
    #
    # Size it from the goal-vs-measured LEAD in the demonstrations, NOT from per-tick action
    # velocity -- a position-tracked arm only moves while its target leads where it is, so
    # capping the lead caps the reachable speed. On this dataset the lead ran to 0.29 rad
    # (p99.9 = 0.15); 0.3 clips none of it, while 0.05 clipped 7.7% of the demonstrations'
    # own frames and left the arm crawling behind every command.
    max_relative_target: float | None = 0.3

    # Only used when passive=False. Seconds to wait for all six drivers to report enabled.
    enable_timeout_s: float = 5.0

    cameras: dict[str, CameraConfig] = field(default_factory=dict)
