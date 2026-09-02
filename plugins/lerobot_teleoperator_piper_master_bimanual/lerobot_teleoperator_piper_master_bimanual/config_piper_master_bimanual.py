from dataclasses import dataclass

from lerobot.teleoperators.config import TeleoperatorConfig


@TeleoperatorConfig.register_subclass("piper_master_bimanual")
@dataclass(kw_only=True)
class PiperMasterBimanualConfig(TeleoperatorConfig):
    """Both PiPER master arms, one per CAN bus, read as a single teleoperator.

    Pair with a `piper_bimanual` robot at `passive=True`. Action keys are prefixed `left_`
    and `right_` so they line up with that robot's observation keys.

    Nothing is written to either bus: each master already broadcasts its joint-control
    frames (0x155-0x157) and gripper frame (0x159) for its own firmware master-slave link,
    and this only listens.
    """

    left_port: str = "can_left"
    right_port: str = "can_right"

    judge_flag: bool = False

    # A teach-mode master is silent until moved, so connect() asks the operator to move each
    # arm in turn and waits this long PER ARM. Generous by design: the alternative to proving
    # both masters are live is a dataset where one arm's action column is all zeros.
    connect_timeout_s: float = 30.0

    # Skip that proof for both arms. Only set False if you have already confirmed both
    # masters another way -- nothing downstream can tell a resting master from an absent one,
    # and on a two-arm rig it is the quieter arm that goes unnoticed.
    require_master_frames: bool = True
