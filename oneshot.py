from typing import Any
import json
import socket
from dataclasses import dataclass, asdict, fields, replace, is_dataclass, field
import select

from golf.logger import get_logger

logger = get_logger(__name__)


@dataclass
class BallData:
    Speed: float
    SpinAxis: float
    TotalSpin: float
    HLA: float
    VLA: float


@dataclass
class ShotDataOptions:
    ContainsBallData: bool = True
    ContainsClubData: bool = False
    LaunchMonitorIsReady: bool | None = None
    LaunchMonitorBallDetected: bool | None = None
    IsHeartbeat: bool | None = None


@dataclass
class Shot:
    DeviceID: str = "GSPro LM 1.1"
    Units: str = "Yards"
    ShotNumber: int = -1
    APIVersion: str = "1"
    BallData: BallData = None
    ShotDataOptions: ShotDataOptions = ShotDataOptions()

    next_shot_number = 42

    def __post_init__(self):
        self.ShotNumber = Shot.next_shot_number
        Shot.next_shot_number += 1

    def as_msg(self) -> bytes:
        return bytes(json.dumps(asdict_ignore_none(self)), encoding="utf8")

    @classmethod
    def heartbeat(cls) -> "Shot":
        return Shot(ShotDataOptions=ShotDataOptions(ContainsBallData=False, ContainsClubData=False, IsHeartbeat=True))


@dataclass
class GSProPlayer:
    Handed: str | None = None
    Club: str | None = None
    DistanceToTarget: float | None = None


@dataclass
class GSProMessage:
    Code: int
    Message: str | None = None
    Player: GSProPlayer | None = None
    Xtra: dict = field(default_factory=dict)  # other attributes passed through in extra

    @classmethod
    def create_from_dict(cls, dct: dict[str, Any]) -> "GSProMessage":
        dct = dict(dct)
        kwargs = {}
        for f in fields(cls):
            if f.name in dct:
                kwargs[f.name] = dct.pop(f.name)
        kwargs["Xtra"] = dct
        return cls(**kwargs)


class GSProSession:

    def __init__(self, gspro_host="192.168.1.100", gspro_port=921):
        self.gspro_host = gspro_host
        self.gspro_port = gspro_port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.shots_and_responses = []

        logger.info(f"connecting to gspro {self.gspro_host}:{self.gspro_port}")
        self.sock.connect((self.gspro_host, self.gspro_port))
        logger.info(f"{self.sock=}")

    def send_heartbeat(self):
        self.send_shot(Shot.heartbeat())

    def recv_data(self):
        logger.info("receiving response")
        resp_bytes = self.sock.recv(2048)
        logger.info(f"recv msg size: {len(resp_bytes)}")
        msgs = [GSProMessage.create(dct) for dct in parse_gspro_data(resp_bytes)]
        self.shots_and_responses.extend(msgs)
        logger.info(resp_bytes)

    def send_shot(self, golfshot: Shot):
        logger.info(f"sending {golfshot=}")
        shot_data = golfshot.as_msg()
        logger.info(f"---------- {golfshot.ShotNumber} ------------")
        logger.info(shot_data)
        logger.info("----------------------------------------------")
        self.shots_and_responses.append(golfshot)
        nbytes_sent = self.sock.send(shot_data)

        if len(shot_data) != nbytes_sent:
            raise ValueError(f"{len(shot_data)=} {nbytes_sent=}")

    def data_available(self) -> bool:
        logger.info(f"checking data avail")
        r, _, _ = select.select([self.sock], [], [], 0)
        logger.info(f"{r=}")
        logger.info(f"{self.sock in r=}")
        return self.sock in r

    def close(self):
        self.sock.close()


def asdict_ignore_none(obj) -> dict[str, Any]:
    """Like dataclasses.asdict ignoring keys whose value is None

    :param obj: an instance of a Dataclass
    :return: a dict without any None values

    Examples:

        assert (
          asdict_ignore_none(ShotDataOptions())
          == {'ContainsBallData': True, 'ContainsClubData': False}
        )
        assert (
          asdict_ignore_none(ShotDataOptions(IsHeartbeat=False))
          == {'ContainsBallData': True, 'ContainsClubData': False, 'IsHeartbeat': False}
        )

    """

    assert is_dataclass(obj)
    result = dict()
    for f in fields(obj):
        val = getattr(obj, f.name)
        if val is not None:
            if is_dataclass(val):
                result[f.name] = asdict_ignore_none(val)
            else:
                result[f.name] = val
    return result


def parse_gspro_data(data: bytes) -> list[dict[str, Any]]:
    """Parse data from gspro into a list of json decoded objects

    :param resp: chunk of data received from gspro
    :return: list of json decoded objects

    Example:
        data = bytes('{"Code":200,"Message":"Ball Data received","Player":null}{"Code":201,"Message":"GSPro Player Information","Player":{"Handed":"RH","Club":"DR","DistanceToTarget":380.0}}{"Code":202,"Message":"GSPro ready","Player":null}{"Code":203,"Message":"GSPro round ended","Player":null}',
                     encoding="utf8")
        msgs = parse_gspro_data(data)
        print(len(msgs))  # 4
        print(msgs[0])    # {"Code': 200, 'Message': 'Ball Data received', 'Player': None}
        print(msgs[1])    # {'Code': 201, 'Message': 'GSPro Player Information', 'Player': {'Handed': 'RH', 'Club': 'DR', 'DistanceToTarget': 380.0}}
        print(msgs[2])    # {'Code': 202, 'Message': 'GSPro ready', 'Player': None}
        print(msgs[3])    # {'Code': 203, 'Message': 'GSPro round ended', 'Player': None}

    """

    data = data.decode()  # convert to str
    len_processed = 0
    result = []

    while len_processed < len(data):
        assert data[len_processed] == "{", f"{len_processed=} {data[len_processed]=}"
        msg = None
        start = len_processed
        while msg is None:
            idx_of_close_bracket = data.find("}", start)
            substr = data[len_processed: idx_of_close_bracket+1]
            # print(f"trying {start=} {idx_of_close_bracket=} {substr=}")
            try:
                msg = json.loads(substr)
                # print(f"parsed {len(substr)} bytes {substr=}")
            except json.decoder.JSONDecodeError:
                start = start + len(substr)
        result.append(msg)
        len_processed = len_processed + len(substr)
    return result


def main():
    def get_balldata_field(prefix) -> int | None:
        matches = []
        for f in fields(BallData):
            if f.name.lower().startswith(prefix):
                matches.append(f)
        return matches[0] if len(matches) == 1 else None

    curr_shot = Shot(BallData=BallData(Speed=75, SpinAxis=13.2, TotalSpin=9000, HLA=-5, VLA=29))
    conn = GSProSession()
    shots_hit = []

    while True:
        data = input("what's next? ")
        logger.info(f"data: {data=}")

        parts = data.split()
        cmd = parts[0]
        args = parts[1:]

        field_to_set = get_balldata_field(cmd.lower())

        if field_to_set and len(args) > 0:
            try:
                new_val = field_to_set.type(args[0])
                logger.info(f"setting {field_to_set.name} to {new_val}")
                curr_shot = replace(curr_shot, BallData=replace(curr_shot.BallData, **{field_to_set.name: new_val}))
            except ValueError as e:
                logger.error(f"{e=}")
        else:
            if cmd == "last":
                print(json.dumps(asdict(curr_shot)["BallData"], indent=2))
            elif cmd == "hit":
                curr_shot = replace(curr_shot, **{"ShotNumber": curr_shot.ShotNumber + 1})
                conn.send_shot(curr_shot)
                shots_hit.append(curr_shot)
            elif cmd == "hb":
                conn.send_heartbeat()
            elif cmd == "avail":
                logger.info(f"{conn.data_available()=}")
            elif cmd == "recv":
                conn.recv_data()
            elif cmd == "quit":
                conn.close()
                break
            else:
                print(f"unrecognized command {cmd=}")


def test_serialize():
    hb = Shot.heartbeat()
    hb_dct = asdict_ignore_none(hb)

    assert "BallData" not in hb_dct
    assert hb_dct["ShotDataOptions"]["IsHeartbeat"]


def test_resp():
    data = bytes(
        '{"Code":200,"Message":"Ball Data received","Player":null}{"Code":201,"Message":"GSPro Player Information","Player":{"Handed":"RH","Club":"DR","DistanceToTarget":380.0}}{"Code":202,"Message":"GSPro ready","Player":null}{"Code":203,"Message":"GSPro round ended","Player":null}',
        encoding="utf8")
    msgs = parse_gspro_data(data)
    assert len(msgs) == 4

    objs = [GSProMessage.create_from_dict(dct) for dct in msgs]
    assert len(objs) == 4

    for msg, obj in zip(msgs, objs):
        print(msg)
        assert msg["Code"] == obj.Code
        assert msg["Message"] == obj.Message
        if msg["Player"] is None:
            assert obj.Player is None


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        if sys.argv[1] == "test":
            logger.info("test_serialize")
            test_serialize()
            test_resp()
    else:
        main()
    logger.info("Bye!")

#
#
#
# shot = {
#     "DeviceID": "GSPro LM 1.1",  			#required - unqiue per launch monitor / prooject type
#     "Units": "Yards",						#default yards
#     "ShotNumber": 14,						#required - auto increment from LM
#     "APIversion": "1",						#required - "1" is current version
#     "BallData": {
#         "Speed": 99.5,						#required
#         "SpinAxis": 13.2,					#required
#         "TotalSpin": 3250.0,				#required
#         "HLA": 2.3,							#required
#         "VLA": 14.3,						#required
#         # "CarryDistance": 256.5				#optional
#     },
#     "ClubData": {
#         "Speed": 0.0,
#         "AngleOfAttack": 0.0,
#         "FaceToTarget": 0.0,
#         "Lie": 0.0,
#         "Loft": 0.0,
#         "Path": 0.0,
#         "SpeedAtImpact": 0.0,
#         "VerticalFaceImpact": 0.0,
#         "HorizontalFaceImpact": 0.0,
#         "ClosureRate": 0.0
#     },
#     "ShotDataOptions": {
#         "ContainsBallData": True,			#required
#         "ContainsClubData": False,			#required
#         # "LaunchMonitorIsReady": True, 		#not required
#         # "LaunchMonitorBallDetected": True, 	#not required
#         # "IsHeartBeat": False 				#not required
#     }
# }
#
#
#
# # create an INET, STREAMing socket
# logger.info(f"creating socket")
# s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
#
# logger.info(f"connecting to gspro {gspro_host}:{gspro_port}")
# s.connect((gspro_host, gspro_port))
# logger.info(f"{s=}")
# logger.info(f"{s.getblocking()=}")
# logger.info(f"{s.gettimeout()=}")
#
# shot_bytes = bytes(json.dumps(shot), encoding="utf8")
# print(f"{len(shot_bytes)}")
#
# logger.info(f"sending shot of {len(shot_bytes)} bytes")
# resp = s.send(shot_bytes)
#
# logger.info(f"send returned {resp}")
#
#
# logger.info(f"trying recv")
# chunk = s.recv(2048)
# logger.info(f"recv msg size: {len(chunk)}")
#
# resp_json = chunk.decode()
# print(resp_json)
# resp = json.loads(resp_json)
#
# for k, v in resp.items():
#     print(f"{k}: {v}")
#
#
# logger.info(f"trying recv again")
# chunk = s.recv(2048)
# logger.info(f"recv msg size: {len(chunk)}")
# logger.info(f"{chunk.decode()=}")
#
#
# s.close()
#
# #
# # m = """{"Code":200,"Message":"Ball Data received","Player":null}{"Code":201,"Message":"GSPro Player Information","Player":{"Handed":"RH","Club":"DR","DistanceToTarget":380.0}}{"Code":202,"Message":"GSPro ready","Player":null}{"Code":203,"Message":"GSPro round ended","Player":null}"""
# # m[40:50]
# #
# # {"Code":201,"Message":"GSPro Player Information",
# #  "Player":{"Handed":"RH","Club":"DR","DistanceToTarget":380.0}
# # }
# # {"Code":202,"Message":"GSPro ready","Player":None
# #  }
# # {"Code":203,"Message":"GSPro round ended","Player":None}
