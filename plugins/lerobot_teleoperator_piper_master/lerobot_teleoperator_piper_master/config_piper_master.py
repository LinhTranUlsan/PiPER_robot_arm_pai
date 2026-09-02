from dataclasses import dataclass

from lerobot.teleoperators.config import TeleoperatorConfig


@TeleoperatorConfig.register_subclass("piper_master")
@dataclass(kw_only=True)
class PiperMasterConfig(TeleoperatorConfig):
    """AgileX PiPER master arm, listened to on the CAN bus it shares with its follower.

    Nothing is sent to the arm. The master already broadcasts joint-control frames
    (0x155-0x157) and a gripper-control frame (0x159) for the firmware master-slave link;
    this reads those same frames, so the recorded action is the exact command the follower
    obeyed.
    """

    # One CAN bus per master-follower cluster on this rig (can_left, can_right).
    # Always pass --teleop.port=can_right (or can_left); this default only matches one cluster.
    port: str = "can_right"
    judge_flag: bool = False

    # A teach-mode master arm is silent until someone moves it, so connect() asks the
    # operator to move it and waits this long for the first control frame. Generous by
    # design: the alternative to proving the master is on the bus is a dataset of zeros.
    connect_timeout_s: float = 30.0

    # Skip that proof. Only set False if you have already confirmed the master arm on the
    # bus another way -- nothing downstream can tell a resting master from an absent one.
    require_master_frames: bool = True
