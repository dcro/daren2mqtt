"""Decoder for the analog values and status reply (42H).

DATAINFO layout (YD/T 1363 style, all values big-endian, temperatures in 0.1 °C):

    flag u8 | SOC 0.01 % u16 | pack 0.01 V u16 | cell count u8 | cell mV u16 x m
    | ambient, pack, MOS temperature s16 x 3 | sensor count u8 | sensor temp s16 x n
    | current 0.01 A s16 (charging positive) | internal resistance u16 | SOH % u16
    | custom u8 | full 0.01 Ah u16 | remaining 0.01 Ah u16 | cycles u16
    | voltage, current, temperature, alarm, FET status u16 x 5
    | per-cell overvoltage/undervoltage protection, high/low alarm u16 x 4
    | balancing cells 1-16 u16 | balancing cells 17-32 u16 | ... (ignored)
"""

from dataclasses import dataclass
from enum import StrEnum

from daren2mqtt.protocol import ProtocolError


class Kind(StrEnum):
    ALARM = "alarm"
    PROTECTION = "protection"
    FAULT = "fault"


# (status word, bit) -> (kind, description). Bits not listed are state, not problems.
STATUS_BITS: dict[tuple[str, int], tuple[Kind, str]] = {
    ("voltage", 0): (Kind.PROTECTION, "cell overvoltage"),
    ("voltage", 1): (Kind.PROTECTION, "cell undervoltage"),
    ("voltage", 2): (Kind.PROTECTION, "pack overvoltage"),
    ("voltage", 3): (Kind.PROTECTION, "pack undervoltage"),
    ("voltage", 4): (Kind.ALARM, "cell high voltage"),
    ("voltage", 5): (Kind.ALARM, "cell low voltage"),
    ("voltage", 6): (Kind.ALARM, "pack high voltage"),
    ("voltage", 7): (Kind.ALARM, "pack low voltage"),
    ("voltage", 8): (Kind.ALARM, "cell voltage difference"),
    ("voltage", 9): (Kind.PROTECTION, "overvoltage lockout"),
    ("voltage", 10): (Kind.PROTECTION, "undervoltage lockout"),
    ("voltage", 11): (Kind.ALARM, "temperature difference"),
    ("voltage", 12): (Kind.FAULT, "cell failure"),
    ("voltage", 13): (Kind.FAULT, "fuse blown"),
    ("voltage", 14): (Kind.PROTECTION, "cell voltage difference"),
    ("current", 2): (Kind.PROTECTION, "charge overcurrent"),
    ("current", 3): (Kind.PROTECTION, "short circuit"),
    ("current", 4): (Kind.PROTECTION, "discharge overcurrent"),
    ("current", 5): (Kind.PROTECTION, "discharge overcurrent (fast)"),
    ("current", 6): (Kind.ALARM, "charge current"),
    ("current", 7): (Kind.ALARM, "discharge current"),
    ("current", 8): (Kind.PROTECTION, "overcurrent lockout"),
    ("current", 9): (Kind.PROTECTION, "reverse connection"),
    ("temperature", 0): (Kind.PROTECTION, "charge high temperature"),
    ("temperature", 1): (Kind.PROTECTION, "charge low temperature"),
    ("temperature", 2): (Kind.PROTECTION, "discharge high temperature"),
    ("temperature", 3): (Kind.PROTECTION, "discharge low temperature"),
    ("temperature", 4): (Kind.PROTECTION, "ambient high temperature"),
    ("temperature", 5): (Kind.PROTECTION, "ambient low temperature"),
    ("temperature", 6): (Kind.PROTECTION, "MOSFET high temperature"),
    ("temperature", 7): (Kind.PROTECTION, "MOSFET low temperature"),
    ("temperature", 8): (Kind.ALARM, "charge high temperature"),
    ("temperature", 9): (Kind.ALARM, "charge low temperature"),
    ("temperature", 10): (Kind.ALARM, "discharge high temperature"),
    ("temperature", 11): (Kind.ALARM, "discharge low temperature"),
    ("temperature", 12): (Kind.ALARM, "ambient high temperature"),
    ("temperature", 13): (Kind.ALARM, "ambient low temperature"),
    ("temperature", 14): (Kind.ALARM, "MOSFET high temperature"),
    ("temperature", 15): (Kind.ALARM, "MOSFET low temperature"),
    ("alarm", 0): (Kind.ALARM, "cell voltage difference"),
    ("alarm", 1): (Kind.ALARM, "charge MOSFET"),
    ("alarm", 2): (Kind.ALARM, "external storage"),
    ("alarm", 3): (Kind.ALARM, "internal communication"),
    ("alarm", 4): (Kind.ALARM, "EEPROM"),
    ("alarm", 7): (Kind.ALARM, "low state of charge"),
    ("alarm", 8): (Kind.PROTECTION, "MOSFET high temperature"),
    ("alarm", 9): (Kind.FAULT, "heater"),
    ("alarm", 10): (Kind.FAULT, "current limiter"),
    ("alarm", 11): (Kind.FAULT, "voltage sampling"),
    ("alarm", 12): (Kind.FAULT, "cell"),
    ("alarm", 13): (Kind.FAULT, "temperature sensor"),
    ("alarm", 14): (Kind.FAULT, "charge MOSFET"),
    ("alarm", 15): (Kind.FAULT, "discharge MOSFET"),
    ("fet", 2): (Kind.FAULT, "charge MOSFET"),
    ("fet", 3): (Kind.FAULT, "discharge MOSFET"),
    ("fet", 13): (Kind.FAULT, "analog front end"),
    ("fet", 14): (Kind.ALARM, "analog front end alarm pin"),
    ("fet", 15): (Kind.PROTECTION, "low battery"),
}

CURRENT_CHARGING = 1 << 0
CURRENT_DISCHARGING = 1 << 1
FET_CHARGE_ON = 1 << 0
FET_DISCHARGE_ON = 1 << 1


@dataclass(frozen=True, slots=True)
class Analog:
    soc: float  # %
    voltage: float  # V
    cells_mv: tuple[int, ...]
    temp_ambient: float  # °C
    temp_pack: float
    temp_mos: float
    temp_sensors: tuple[float, ...]
    current: float  # A, positive = charging (as on the wire)
    soh: int  # %
    full_ah: float
    remaining_ah: float
    cycles: int
    status: dict[str, int]  # voltage, current, temperature, alarm, fet
    balancing_mask: int  # bit n = cell n+1

    @property
    def power(self) -> float:
        """W, positive = charging."""
        return round(self.voltage * self.current, 1)

    @property
    def state(self) -> str:
        bits = self.status["current"]
        if bits & CURRENT_CHARGING:
            return "charging"
        if bits & CURRENT_DISCHARGING:
            return "discharging"
        return "idle"

    @property
    def charge_mos(self) -> bool:
        return bool(self.status["fet"] & FET_CHARGE_ON)

    @property
    def discharge_mos(self) -> bool:
        return bool(self.status["fet"] & FET_DISCHARGE_ON)

    @property
    def balancing_cells(self) -> list[int]:
        return [n + 1 for n in range(32) if self.balancing_mask >> n & 1]

    @property
    def cell_min(self) -> tuple[int, int]:
        """(mV, 1-based cell number)."""
        mv = min(self.cells_mv)
        return mv, self.cells_mv.index(mv) + 1

    @property
    def cell_max(self) -> tuple[int, int]:
        mv = max(self.cells_mv)
        return mv, self.cells_mv.index(mv) + 1

    def flags(self, kind: Kind) -> list[str]:
        """Active problems of one kind, in table order, without duplicates."""
        active: list[str] = []
        for (word, bit), (k, text) in STATUS_BITS.items():
            if k is kind and self.status[word] >> bit & 1 and text not in active:
                active.append(text)
        return active


class _Reader:
    def __init__(self, data: bytes):
        self._data, self._pos = data, 0

    def take(self, n: int) -> bytes:
        if self._pos + n > len(self._data):
            raise ProtocolError(f"42H reply truncated at byte {self._pos} of {len(self._data)}")
        chunk = self._data[self._pos : self._pos + n]
        self._pos += n
        return chunk

    def u8(self) -> int:
        return self.take(1)[0]

    def u16(self) -> int:
        return int.from_bytes(self.take(2), "big")

    def s16(self) -> int:
        return int.from_bytes(self.take(2), "big", signed=True)


def decode_analog(info: bytes) -> Analog:
    r = _Reader(info)
    r.u8()  # data flag
    soc = r.u16() / 100
    voltage = r.u16() / 100
    cell_count = r.u8()
    if not 1 <= cell_count <= 32:
        raise ProtocolError(f"implausible cell count {cell_count}")
    cells = tuple(r.u16() for _ in range(cell_count))
    temp_ambient, temp_pack, temp_mos = (r.s16() / 10 for _ in range(3))
    sensor_count = r.u8()
    if sensor_count > 16:
        raise ProtocolError(f"implausible temperature sensor count {sensor_count}")
    sensors = tuple(r.s16() / 10 for _ in range(sensor_count))
    current = r.s16() / 100
    r.u16()  # internal resistance, not populated on the boards seen
    soh = r.u16()
    r.u8()  # custom byte
    full_ah = r.u16() / 100
    remaining_ah = r.u16() / 100
    cycles = r.u16()
    status = {word: r.u16() for word in ("voltage", "current", "temperature", "alarm", "fet")}
    for _ in range(4):
        r.u16()  # per-cell protection/alarm masks, summarised in the voltage status word
    balancing = r.u16() | r.u16() << 16
    if not any(cells) or soc > 100:
        raise ProtocolError("implausible values in 42H reply")
    return Analog(
        soc=soc,
        voltage=voltage,
        cells_mv=cells,
        temp_ambient=temp_ambient,
        temp_pack=temp_pack,
        temp_mos=temp_mos,
        temp_sensors=sensors,
        current=current,
        soh=soh,
        full_ah=full_ah,
        remaining_ah=remaining_ah,
        cycles=cycles,
        status=status,
        balancing_mask=balancing,
    )


@dataclass(frozen=True, slots=True)
class Device:
    """Device information reply (51H): three 10-byte ASCII fields, then the firmware version."""

    hardware: str
    product: str
    model: str
    firmware: str


def _ascii(raw: bytes) -> str:
    return "".join(chr(b) for b in raw if 0x20 <= b < 0x7F).strip()


def decode_device(info: bytes) -> Device:
    if len(info) < 33:
        raise ProtocolError(f"51H reply truncated: {len(info)} bytes")
    firmware = ".".join(f"{b:02d}" for b in info[30:33])
    return Device(_ascii(info[0:10]), _ascii(info[10:20]), _ascii(info[20:30]), firmware)


@dataclass(frozen=True, slots=True)
class Counters:
    """Lifetime counters from parameter module 4 (B0H)."""

    design_ah: float
    charged_ah: int
    discharged_ah: int
    charged_kwh: float  # wraps at 6553.5 kWh
    discharged_kwh: float


def decode_counters(info: bytes) -> Counters:
    """B0H, command group, operation, module, function, length, then the module data:
    remaining, full and design capacity (u16, 0.01 Ah), charged and discharged capacity
    (u32, 1 Ah), charged and discharged energy (u16, 0.1 kWh)."""
    if len(info) < 24 or info[0] != 0xB0 or info[3] != 0x04:
        raise ProtocolError(f"unexpected reply to the counters request: {info[:6].hex()}")

    def field(offset: int, size: int) -> int:
        return int.from_bytes(info[offset : offset + size], "big")

    return Counters(
        design_ah=field(10, 2) / 100,
        charged_ah=field(12, 4),
        discharged_ah=field(16, 4),
        charged_kwh=field(20, 2) / 10,
        discharged_kwh=field(22, 2) / 10,
    )


@dataclass(frozen=True, slots=True)
class ProtectionCounts:
    """How often each protection has tripped (83H), as kept by the BMS."""

    overcharge: int
    overdischarge: int
    overcurrent: int
    temperature: int
    short_circuit: int


def decode_protection_counts(info: bytes) -> ProtectionCounts:
    """83H, operation, then u16 counters: overcharge, over-discharge, overcurrent, temperature,
    short circuit, and more that are not used here."""
    if len(info) < 12 or info[0] != 0x83:
        raise ProtocolError(f"unexpected reply to the protection counts request: {info[:2].hex()}")
    counts = [int.from_bytes(info[i : i + 2], "big") for i in range(2, 12, 2)]
    return ProtectionCounts(*counts)


@dataclass(frozen=True, slots=True)
class Thresholds:
    """Configured thresholds (80H), in mV."""

    cell_overvoltage_protection: int
    cell_undervoltage_protection: int
    cell_high_voltage_alarm: int
    cell_low_voltage_alarm: int
    balancing_start: int
    balancing_delta: int


# 80H is a list of u16. Each protection and alarm is a (threshold, delay, release) triple; these
# are the word indexes of the values used here.
_THRESHOLD_WORDS = {
    "cell_overvoltage_protection": 0,
    "cell_undervoltage_protection": 3,
    "cell_high_voltage_alarm": 48,
    "cell_low_voltage_alarm": 51,
    "balancing_start": 100,
    "balancing_delta": 101,
}


def decode_thresholds(info: bytes) -> Thresholds:
    if len(info) < 2 * (max(_THRESHOLD_WORDS.values()) + 1):
        raise ProtocolError(f"80H reply truncated: {len(info)} bytes")
    values = {k: int.from_bytes(info[2 * i : 2 * i + 2], "big") for k, i in _THRESHOLD_WORDS.items()}
    cells = [v for k, v in values.items() if k.startswith("cell_")]
    if not all(1500 <= mv <= 5000 for mv in cells):  # another layout would give nonsense here
        raise ProtocolError(f"implausible cell thresholds: {cells} mV")
    return Thresholds(**values)
